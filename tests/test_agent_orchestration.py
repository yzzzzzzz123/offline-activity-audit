"""Synthetic, offline checks for role boundaries, collection and targeted rework."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Lock
from unittest import TestCase
from unittest.mock import patch

from audit_core.agent_orchestration import (WORKERS, owner, scoped_case, rule_specs,
    summary_reports, expand_sources, restore_reports)
from audit_core.common import AuditError
from audit_core.execution_trace import collect_execution_trace, record_step, technical_inventory
from audit_core.model_authority import execution, decision
from audit_core.model_metrics import collect_model_artifacts, record_model_attempt
from audit_core.model_settings import ModelSelection
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.run_conversation import current, current_agent_id, for_provider, RunConversation
from audit_core.scenario_registry import SCENARIO_ORDER
from tests.pdf_test_support import flags_for, classification


def fixture_case(scenario='promotional_display'):
    roles = ['display_photos', 'shelf_photos', 'pos_excel', 'promotion_contract',
             'settlement', 'pos', 'activity_application', 'approved_claim', 'material_list']
    documents = [{'unit_id': role, 'roles': [role], 'facts': ['RAW_ONLY_'+role], 'limitations': []}
                 for role in roles]
    # Enough rows to expose accidentally supplying the POS body to every role.
    documents[2]['facts'] = ['RAW_ONLY_pos_excel row '+str(i) for i in range(150)]
    return {'archive_id': 'a01', 'scenario': scenario, 'flags': flags_for(scenario),
        'materials': [{'id': role, 'state': 'present', 'source_ids': [role]} for role in roles],
        'documents': documents,
        'units': [{'unit_id': role, 'source_file': role+'.txt', 'source_kind': 'fixture', 'locator': 'page 1'}
                  for role in roles]}


class FixtureProvider(PdfEvidenceProvider):
    """The real conversation/agent plumbing with a local, deterministic response."""
    def __init__(self):
        super().__init__('fixture', 'medium', connection=ModelSelection(
            provider='deepseek', model='deepseek-flash', api_key='synthetic-only-not-a-key'))
        self.captured = []
        self.capture_lock = Lock()
        self.barrier = None
        self.fail_role = None

    def _call_stage(self, root, skill, schema, prompt, *, images, label, validator, **options):
        role = current_agent_id()
        variables = prompt.partial_variables
        request = json.loads(variables.get('request', '{}'))
        if 'candidate_scenarios' in schema['properties']:
            docs = json.loads(variables['documents'])
            value = classification(variables['scenario'], [d['unit_id'] for d in docs])
        elif 'items' in schema['properties']:
            value = {'ready': True, 'issues': [], 'findings': [],
                     'items': [{'source_problem_ids': [p['id']], 'text': p['text'],
                                'uncertainty_preserved': True} for p in request['problems']]}
        elif 'model_result' in request:
            value = {**deepcopy(request['model_result']), 'ready': True, 'notes': []}
        else:
            if self.barrier is not None:
                self.barrier.wait(timeout=10)
            if role == self.fail_role:
                raise AuditError('synthetic worker unavailable')
            ids = [d['unit_id'] for d in request.get('documents', [])]
            value = {'checks': [{'rule_id': spec['id'], 'status': 'unknown' if i == 0 else 'pass',
                'reason': role+'：章面部分遮挡，暂不能确认。' if i == 0 else role+'：本项已核对。',
                'source_ids': ids[:1]} for i, spec in enumerate(request['rules'])]}
        content = [{'type': 'text', 'text': prompt.format()}]
        session = current()
        messages = session.messages('synthetic stage', content) if session else content
        with self.capture_lock:
            self.captured.append({'role': role, 'request': request, 'messages': messages,
                                  'session': session.session_id if session else None, 'label': label})
        record_model_attempt({'provider': 'deepseek', 'model': 'deepseek-flash', 'status': 'success',
                              'usage': {'input_tokens': 11, 'output_tokens': 3}})
        record_step('ai.synthetic_fixture', label, {'agent_id': role})
        if session:
            session.append(content, json.dumps(value, ensure_ascii=False), label=label)
        return value


def offline_helpers(stack):
    for name, result in [('prepare_product_references', ({}, [])), ('prepare_pos_identities', {}),
                         ('template_comparison', ({}, []))]:
        stack.enter_context(patch('audit_core.pdf_materials.'+name, return_value=result))
    stack.enter_context(patch('httpx.Client', side_effect=AssertionError('network prohibited')))
    stack.enter_context(patch('httpx.AsyncClient', side_effect=AssertionError('network prohibited')))
    stack.enter_context(patch('socket.create_connection', side_effect=AssertionError('network prohibited')))


class AgentOrchestrationTests(TestCase):
    def test_all_eight_types_keep_every_rule_in_one_owner_and_all_original_rows(self):
        from audit_core.pdf_policy import rules
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario):
                case = fixture_case(scenario)
                scopes = {role: scoped_case(case, role) for role in WORKERS}
                assigned = [s['id'] for c in scopes.values() for s in c['_agent_rule_specs']]
                self.assertEqual(len(assigned), len(set(assigned)))
                self.assertTrue({r.id for r in rules(scenario)}.issubset(assigned))
                self.assertIn('photo_authenticity', assigned)
                for role in WORKERS:
                    for doc in scopes[role]['documents']:
                        self.assertIn(doc, case['documents'])
                self.assertNotIn('pos_excel', {d['unit_id'] for d in scopes['visual']['documents']})
                self.assertNotIn('display_photos', {d['unit_id'] for d in scopes['numbers']['documents']})
                document_ids = {d['unit_id'] for d in scopes['documents']['documents']}
                if scenario in {'entry_fee', 'self_procured_gift_material'}:
                    self.assertIn('pos_excel', document_ids)
                else:
                    self.assertNotIn('pos_excel', document_ids)

    def test_real_default_path_has_three_concurrent_workers_and_a_result_only_main(self):
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario), TemporaryDirectory() as folder, ExitStack() as stack:
                offline_helpers(stack)
                root = Path(folder); analysis = root/'analysis'
                stack.enter_context(collect_model_artifacts(analysis))
                stack.enter_context(collect_execution_trace(analysis))
                provider = FixtureProvider()
                # The gift aggregate must await the photo specialist's answer;
                # other scenarios still exercise all three parallel workers.
                provider.barrier = None if scenario == 'self_procured_gift_material' else Barrier(3)
                case = fixture_case(scenario); before = deepcopy(case)
                result = provider.audit(case, root/'calls')
                self.assertEqual(case, before)
                self.assertEqual({c['rule_id'] for c in result['checks']}, {r['id'] for r in rule_specs(scenario)})
                self.assertEqual([r['agent_id'] for r in summary_reports(provider)], list(WORKERS))
                self.assertEqual(len({c['session'] for c in provider.captured}), 3)
                with execution():
                    provider.review_summary({'sheets': []}, root/'summary')
                main = provider.captured[-1]
                self.assertEqual(main['role'], 'main')
                wire = json.dumps(main['messages'], ensure_ascii=False)
                self.assertNotIn('RAW_ONLY_', wire)
                self.assertEqual(len(main['request']['agent_reports']), 3)
                self.assertIn('章面部分遮挡', wire)
                self.assertIn('疑似修改', json.dumps(provider.captured, ensure_ascii=False))
                for call in provider.captured:
                    wire = json.dumps(call['messages'], ensure_ascii=False)
                    if call['role'] != 'numbers' and not (call['role'] == 'documents' and scenario in {'entry_fee', 'self_procured_gift_material'}):
                        self.assertNotIn('RAW_ONLY_pos_excel', wire)
                    if call['role'] != 'visual':
                        self.assertNotIn('RAW_ONLY_display_photos', wire)
                    if call['role'] == 'documents' and scenario == 'self_procured_gift_material':
                        self.assertEqual(call['request']['photo_agent_report']['agent_id'], 'visual')
                metrics = [json.loads(line) for line in (analysis/'model-metrics.jsonl').read_text('utf-8').splitlines()]
                self.assertEqual(len(metrics), 7)  # 3 audits, 3 self-checks, 1 main summary
                self.assertEqual({m['agent_id'] for m in metrics}, {'main', *WORKERS})
                self.assertEqual(sum(m['usage']['input_tokens'] for m in metrics), 77)
                _, inventory = technical_inventory(root)
                steps = inventory['steps']
                self.assertEqual(len({s['sequence'] for s in steps}), len(steps))
                self.assertEqual({s['agent_id'] for s in steps}, {'main', *WORKERS})

    def test_inventory_does_not_enter_main_or_later_worker_history(self):
        with TemporaryDirectory() as folder, ExitStack() as stack:
            offline_helpers(stack)
            stack.enter_context(collect_model_artifacts(Path(folder)/'analysis'))
            provider = FixtureProvider(); case = fixture_case()
            stack.enter_context(patch.object(provider, '_read_documents', return_value=case['documents']))
            provider.classify({**case, 'selected_scenario': case['scenario']}, Path(folder))
            self.assertEqual(provider.captured[0]['role'], 'documents')
            self.assertIsNone(provider.captured[0]['session'])
            provider.audit(case, Path(folder)/'audit')
            self.assertNotIn('RAW_ONLY_pos_excel', json.dumps(next(c for c in provider.captured[1:]
                if c['role'] == 'documents')['messages']))
            self.assertNotIn('main', getattr(provider, '_agent_conversations', {}))

    def test_unknown_sources_are_requested_explicitly_and_kept_whole(self):
        case = fixture_case(); scoped = scoped_case(case, 'visual')
        self.assertNotIn('pos_excel', {d['unit_id'] for d in scoped['documents']})
        self.assertEqual(expand_sources(scoped, case, ['pos_excel']), ['pos_excel'])
        self.assertEqual(next(d for d in scoped['documents'] if d['unit_id']=='pos_excel'), case['documents'][2])
        self.assertEqual(expand_sources(scoped, case, ['pos_excel']), [])
        with self.assertRaises(AuditError): expand_sources(scoped, case, ['another-run-source'])

    def test_one_worker_failure_preserves_successful_reports_and_does_not_complete(self):
        with TemporaryDirectory() as folder, ExitStack() as stack:
            offline_helpers(stack)
            stack.enter_context(collect_model_artifacts(Path(folder)/'analysis'))
            provider = FixtureProvider(); provider.fail_role = 'numbers'
            with self.assertRaises(AuditError): provider.audit(fixture_case(), Path(folder)/'audit')
            self.assertEqual({r['agent_id'] for r in provider._agent_reports['a01']}, {'visual', 'documents'})
            self.assertNotIn('main', getattr(provider, '_agent_conversations', {}))

    def test_persisted_main_never_imports_legacy_raw_conversation(self):
        with TemporaryDirectory() as folder:
            root = Path(folder); profile = {'provider':'deepseek','model':'deepseek-flash'}
            legacy = RunConversation(profile, root)
            legacy.append([{'type':'text','text':'LEGACY_RAW_BUNDLE'}], '{}')
            for role in ('main', *WORKERS):
                session = RunConversation(profile, root, agent_id=role, separated=True)
                session.append([{'type':'text','text':role+'_PRIVATE'}], '{}')
            for role in ('main', *WORKERS):
                restored = RunConversation(profile, root, agent_id=role, separated=True)
                wire = json.dumps(restored.messages('next', []))
                self.assertNotIn('LEGACY_RAW_BUNDLE', wire)
                self.assertIn(role+'_PRIVATE', wire)
                for other in {'main', *WORKERS}-{role}:
                    self.assertNotIn(other+'_PRIVATE', wire)

    def test_targeted_rework_returns_to_owner_and_keeps_unrelated_results(self):
        from audit_core.final_logic_review import rework, summary_targets
        with TemporaryDirectory() as folder, ExitStack() as stack:
            offline_helpers(stack)
            stack.enter_context(collect_model_artifacts(Path(folder)/'analysis'))
            provider = FixtureProvider(); case = fixture_case()
            evidence = provider.audit(case, Path(folder)/'audit')
            gate = classification(case['scenario'], [d['unit_id'] for d in case['documents']])
            gate['materials'] = deepcopy(case['materials'])
            gate['flags'] = deepcopy(case['flags'])
            packet = {**case, 'route': {'scenario':case['scenario']}, 'classification': gate,
                      'audit_evidence': evidence, 'result': decision(case['scenario'], evidence, case['documents'])}
            _, targets = summary_targets([packet]); before = deepcopy(evidence)
            key = 'a01:check:display_size'
            observed = []
            def review(worker, scoped, value, initial, root, selected, validate, **kwargs):
                observed.append((worker._agent_id, current_agent_id(), deepcopy(scoped['documents'])))
                value['checks'][0]['reason'] = '仅尺寸项的具体限制已复核。'
                return value
            stack.enter_context(patch('audit_core.model_self_check.review_stage', side_effect=review))
            with execution(): rework(provider, [packet], [{'target_ids':[key],'reason':'尺寸理由有冲突。'}], targets, Path(folder)/'rework')
            self.assertEqual(observed[0][:2], ('visual', 'visual'))
            self.assertNotIn('pos_excel', {d['unit_id'] for d in observed[0][2]})
            for old, new in zip(before['checks'], evidence['checks']):
                if old['rule_id'] != 'display_size': self.assertEqual(old, new)
            main_reports = summary_reports(provider)
            self.assertIn('仅尺寸项的具体限制已复核', json.dumps(main_reports, ensure_ascii=False))
            restored = FixtureProvider(); restore_reports(restored, [packet])
            self.assertEqual(summary_reports(restored), main_reports)

    def test_pending_rule_is_a_rework_target_never_an_implicit_pass(self):
        from audit_core.final_logic_review import summary_targets
        packet = {'archive_id':'a01', 'audit_evidence': {'checks':[], '_agent_mode':'main_three',
                  '_agent_assignments': {'visual':['display_size']}}, 'result': {'checks':[]}}
        records, targets = summary_targets([packet])
        self.assertIn('a01:check:display_size', targets)
        self.assertEqual(records[0]['execution_status'], 'pending')
        self.assertNotIn('status', records[0])

    def test_tools_remain_in_own_role_and_survive_targeted_scope_changes(self):
        from audit_core.tool_observations import remember, observations
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            full = fixture_case()
            numbers = scoped_case(full, 'numbers')
            remember(numbers, 'pos_calculation', fields=[], code=None, source_ids=['pos_excel'],
                     result={'complete':True, 'original_rows':150})
            narrowed = scoped_case(full, 'numbers')
            narrowed['documents'] = [d for d in narrowed['documents'] if d['unit_id']=='pos_excel']
            self.assertEqual(observations(narrowed)[0]['result']['original_rows'], 150)
            self.assertEqual(observations(scoped_case(full, 'visual')), [])
            self.assertEqual(observations(scoped_case(full, 'documents')), [])
            changed = deepcopy(full); changed['documents'][2]['facts'].append('NEW ORIGINAL ROW')
            self.assertEqual(observations(scoped_case(changed, 'numbers')), [])

    def test_rekeyed_connection_cannot_reuse_worker_conversation(self):
        from dataclasses import replace
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            provider = FixtureProvider()
            for_provider(provider)
            provider.connection = replace(provider.connection, api_key='another-synthetic-key')
            with self.assertRaises(AuditError): for_provider(provider)

    def test_main_does_not_mark_missing_worker_outputs_complete(self):
        from audit_core.summary_review import _validate_answer
        request = {'problems':[], 'review_targets':[], 'agent_reports':[
            {'agent_id':'visual','pending_rule_ids':['display_size']}]}
        answer = {'ready':True,'issues':[],'items':[]}
        with execution(): _validate_answer(answer, request)
        self.assertFalse(answer['ready'])
        self.assertEqual(answer['items'], [])
        self.assertIn('子Agent', answer['issues'][0])

    def test_self_check_can_fetch_an_additional_complete_source(self):
        from audit_core.model_self_check import review_stage
        full = fixture_case(); case = scoped_case(full, 'visual')
        provider = FixtureProvider(); provider._agent_id = 'visual'
        provider._agent_expand_sources = lambda target, requested: expand_sources(target, full, requested)
        value = {'checks':[{'rule_id':'photo_authenticity','status':'unknown',
                           'reason':'需要核对对应来源。','source_ids':['display_photos']}]}
        requests = []
        def reply(root, skill, schema, prompt, **kwargs):
            requests.append(json.loads(prompt.partial_variables['request']))
            return {**value, 'ready':len(requests)>1, 'notes':[],
                    **({'source_requests':['pos_excel']} if len(requests)==1 else {})}
        with TemporaryDirectory() as folder, execution(), patch.object(provider, '_call', side_effect=reply):
            review_stage(provider, case, value, value, Path(folder), ['photo_authenticity'], lambda v, ids:v)
        self.assertEqual(len(requests), 2)
        returned = next(d for d in requests[-1]['documents'] if d['unit_id']=='pos_excel')
        self.assertEqual(returned, full['documents'][2])

    def test_all_eight_full_workflows_use_team_then_main_summary(self):
        from audit_core.pdf_workflow import PdfWorkflow
        from audit_core.scenario_registry import SCENARIO_LABELS
        from tests.pdf_test_support import bundle
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario), TemporaryDirectory() as folder, ExitStack() as stack:
                offline_helpers(stack)
                root=Path(folder); bundle(root/'input')
                provider=FixtureProvider()
                def read(case, *args):
                    return [{'unit_id':u['unit_id'], 'roles':['submitted'],
                             'facts':['WORKFLOW_RAW_ONLY'], 'limitations':[]} for u in case['units']]
                stack.enter_context(patch.object(provider, '_read_documents', side_effect=read))
                flow=PdfWorkflow('isolated-team-'+scenario, 'fixture', root/'input', root/'model',
                                 provider, None, None, biz_type=SCENARIO_LABELS[scenario])
                receipt=flow.run()
                self.assertEqual(flow.packets[0]['audit_evidence']['_agent_mode'], 'main_three')
                self.assertEqual(receipt['verification']['logic_review']['status'], 'passed')
                main=[c for c in provider.captured if c['role']=='main']
                self.assertEqual(len(main), 1)
                self.assertEqual(len(main[0]['request']['agent_reports']), 3)
                self.assertNotIn('WORKFLOW_RAW_ONLY', json.dumps(main[0]['messages']))
