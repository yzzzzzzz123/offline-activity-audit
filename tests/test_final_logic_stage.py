"""Final-stage routing and correction with isolated model responses and sources."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.final_logic_review import SKILL, context, review
from audit_core.model_judgment import save_judgments, approved_checks
from audit_core.model_metrics import collect_model_artifacts
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_workflow import PdfWorkflow
from audit_core.pdf_policy import rules
from audit_core.scenario_registry import SCENARIO_LABELS
from tests.test_audit_rag_contract import case_for
from tests.pdf_test_support import flags_for, unknown_checks


def fixture(scenario='personnel_incentive'):
    case, _ = case_for(1)
    case['scenario'] = scenario
    case['flags'] = flags_for(scenario)
    value = unknown_checks(scenario, case['flags'])
    packet = {'archive_id': case['archive_id'], 'source_archive': '隔离资料.zip',
        'classification': {'candidate_scenarios': [scenario], 'flags': case['flags'], 'materials': case['materials']},
        'route': {'matched': True, 'scenario': scenario}, 'units': case['units'], 'documents': case['documents'],
        'audit_evidence': value}
    provider = PdfEvidenceProvider('fixture', 'medium')
    provider._audit_cases[case['archive_id']] = case
    return case, packet, provider


def accepted(**extra):
    return {'ready': True, 'findings': [], 'notes': [], **extra}


def summary_answer(request, **changes):
    answer = {'ready': True, 'issues': [], 'items': [
        {'source_problem_ids': [p['id']], 'text': p['text'], 'uncertainty_preserved': True}
        for p in request['problems']]}
    answer.update(changes)
    return answer


class FinalLogicStageTests(TestCase):
    def setup_flow(self, folder, scenario='personnel_incentive'):
        case, packet, provider = fixture(scenario)
        flow = PdfWorkflow('20261111-summary-test', 'fixture', folder, Path(folder)/'model', provider,
                           None, None, packets=[packet])
        save_judgments(case, packet['audit_evidence'], [c['rule_id'] for c in packet['audit_evidence']['checks']])
        flow.evidence(); flow.decision()
        return case, packet, provider, flow

    def test_final_stage_checks_summary_once_and_sealing_reuses_it_without_reauditing(self):
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            case, packet, provider, flow = self.setup_flow(folder)
            before = deepcopy(packet)
            calls = []
            def call(root, skill, schema, prompt, **kwargs):
                self.assertEqual(skill, SKILL)
                self.assertEqual(kwargs['images'], [])
                self.assertFalse(kwargs['allow_repair'])
                request = json.loads(prompt.partial_variables['request'])
                self.assertIn('draft', request)
                self.assertNotIn('archives', request)
                self.assertNotIn('documents', request)
                calls.append(request)
                answer = summary_answer(request)
                kwargs['validator'](answer)
                return answer
            with patch.object(provider, '_call', side_effect=call), \
                 patch('audit_core.final_logic_review.context', side_effect=AssertionError('no sources')), \
                 patch('audit_core.final_logic_review.review', side_effect=AssertionError('no raw review')), \
                 patch('audit_core.final_logic_review.rework', side_effect=AssertionError('no business rework')):
                flow.logic_review()
                flow.verification()
                flow.verification()
            self.assertEqual(len(calls), 1)
            self.assertEqual(packet, before)
            self.assertEqual(flow.logic_review_result['status'], 'passed')
            self.assertEqual(flow.logic_review_result['scope'], 'summary_with_targeted_rework')
            self.assertEqual(flow.logic_review_result['rework_rounds'], 0)
            self.assertEqual(flow.receipt['reviewed_analysis_summary'], flow.summary_review_result['text'])

    def test_hundreds_of_original_images_never_enter_final_review(self):
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            case, packet, provider, flow = self.setup_flow(folder)
            for i in range(300):
                unit = {'unit_id': f'unused-{i}', 'image': Path(folder)/f'original-{i}.png',
                        'source_file': f'original-{i}.png', 'locator': str(i), 'native_facts': [], 'limitations': []}
                packet['units'].append(unit)
                packet['documents'].append({'unit_id': unit['unit_id'], 'roles': ['photo'],
                                            'facts': ['RAW_IMAGE_FACT_MUST_NOT_ENTER_FINAL_' + str(i)], 'limitations': []})
            captured = []
            def call(root, skill, schema, prompt, **kwargs):
                self.assertEqual(kwargs['images'], [])
                text = prompt.format();captured.append(text)
                self.assertNotIn('RAW_IMAGE_FACT_MUST_NOT_ENTER_FINAL_', text)
                self.assertNotIn('original-299.png', text)
                return summary_answer(json.loads(prompt.partial_variables['request']))
            with patch.object(provider, '_call', side_effect=call):
                flow.logic_review();flow.verification()
            self.assertEqual(len(captured), 1)
            self.assertEqual(flow.logic_review_result['images_sent'], 0)

    def test_unresolvable_summary_conflict_is_recorded_without_rework_or_false_pass(self):
        from audit_core.model_authority import execution
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
            _, packet, provider, flow = self.setup_flow(folder)
            before = deepcopy(packet)
            def call(root, skill, schema, prompt, **kwargs):
                return summary_answer(json.loads(prompt.partial_variables['request']), ready=False,
                                      issues=['总结与已有结论矛盾，不能仅修正文案解决。'])
            with patch.object(provider, '_call', side_effect=call) as calls, \
                 patch('audit_core.final_logic_review.rework', side_effect=AssertionError('no audit rework')):
                flow.logic_review();flow.verification()
            self.assertEqual(calls.call_count, 1)
            self.assertEqual(packet, before)
            self.assertEqual(flow.logic_review_result['status'], 'not_passed')
            self.assertEqual(flow.receipt['verification']['summary_review']['status'], 'unreviewed')
            self.assertNotIn('reviewed_analysis_summary', flow.receipt)
            self.assertIn('矛盾', flow.logic_review_result['notes'][0])

    def test_summary_cannot_reopen_image_source_or_tool_access(self):
        from audit_core.model_authority import execution
        for key in ('image_requests', 'source_requests', 'tool_requests'):
            with self.subTest(key=key), TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
                _, packet, provider, flow = self.setup_flow(folder)
                before = deepcopy(packet)
                def call(root, skill, schema, prompt, **kwargs):
                    answer = summary_answer(json.loads(prompt.partial_variables['request']), **{key: ['do-not-execute']})
                    kwargs['validator'](answer)
                    return answer
                with patch.object(provider, '_call', side_effect=call) as calls:
                    flow.logic_review();flow.verification()
                self.assertEqual(calls.call_count, 1)
                self.assertEqual(packet, before)
                self.assertEqual(flow.logic_review_result['status'], 'not_passed')

    def test_summary_conflict_reruns_only_the_named_check_then_reviews_updated_summary(self):
        from audit_core.model_authority import execution
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
            _, packet, provider, flow = self.setup_flow(folder)
            other_packet = deepcopy(packet)
            other_packet['archive_id'] = 'a002'
            other_packet['source_archive'] = '另一资料包.zip'
            flow.packets.append(other_packet)
            flow.evidence();flow.decision()
            other_before = deepcopy(other_packet)
            before = deepcopy(packet['audit_evidence'])
            final_calls, rework_calls = [], []
            def call(root, skill, schema, prompt, **kwargs):
                request = json.loads(prompt.partial_variables['request'])
                if skill == SKILL:
                    self.assertEqual(kwargs['images'], [])
                    self.assertNotIn('documents', request)
                    final_calls.append(request)
                    if len(final_calls) == 1:
                        self.assertIn('a001:check:contract_signed', [t['target_id'] for t in request['review_targets']])
                        return summary_answer(request, ready=False, findings=[{
                            'target_ids': ['a001:check:contract_signed'], 'reason': '总结签章结论与已保存依据矛盾。'}])
                    return summary_answer(request)
                self.assertEqual(kwargs['label'], '模型自主阶段自测')
                checks = request['model_result']['checks']
                self.assertEqual([c['rule_id'] for c in checks], ['contract_signed'])
                rework_calls.append(request)
                return {'ready': True, 'notes': [], 'checks': [
                    {**checks[0], 'status': 'pass', 'reason': '按本单签章依据完成定向复核。'},
                    {'rule_id': 'pos_fields', 'status': 'fail', 'reason': '不应覆盖无关项目', 'source_ids': []}]}
            with patch.object(provider, '_call', side_effect=call), \
                 patch.object(provider, 'classify', side_effect=AssertionError('no full intake')), \
                 patch.object(provider, 'audit', side_effect=AssertionError('no full audit')):
                flow.logic_review(); flow.verification()
            self.assertEqual(len(final_calls), 2)
            self.assertEqual(len(rework_calls), 1)
            self.assertEqual(flow.logic_review_result['status'], 'passed')
            self.assertEqual(flow.logic_review_result['rework_rounds'], 1)
            self.assertEqual(other_packet, other_before)
            for old, current in zip(before['checks'], packet['audit_evidence']['checks']):
                if old['rule_id'] == 'contract_signed':
                    self.assertEqual(current['status'], 'pass')
                else:
                    self.assertEqual(current, old)

    def test_one_photo_conflict_reopens_only_that_photo_and_keeps_other_photos(self):
        from audit_core.model_authority import execution
        from audit_core.pdf_evidence import _merge_photo_reviews
        from tests.test_optional_staff_and_display_photos import photo_case, review as photo_review
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
            root = Path(folder);case = photo_case(root, count=4)
            evidence = unknown_checks(case['scenario'], case['flags'])
            evidence['photo_reviews'] = [photo_review('p01', status='unknown'),
                                         *[photo_review('p' + str(i).zfill(2)) for i in range(2, 5)]]
            _merge_photo_reviews(evidence['checks'], evidence['photo_reviews'])
            packet = {'archive_id': 'a001', 'source_archive': '逐图隔离.zip',
                'classification': {'flags': case['flags'], 'materials': case['materials']},
                'route': {'matched': True, 'scenario': case['scenario']},
                'units': case['units'], 'documents': case['documents'], 'audit_evidence': evidence}
            provider = PdfEvidenceProvider('fixture', 'medium');provider._audit_cases['a001'] = case
            flow = PdfWorkflow('20261111-one-photo', 'fixture', root, root/'model', provider, None, None, packets=[packet])
            flow.evidence();flow.decision()
            other_photos = deepcopy(evidence['photo_reviews'][1:])
            other_checks = [deepcopy(c) for c in evidence['checks'] if c['rule_id'] != 'display_watermark']
            final_calls, image_calls, rework_calls = [], [], []
            def call(stage, skill, schema, prompt, **kwargs):
                request = json.loads(prompt.partial_variables['request'])
                if skill == SKILL:
                    self.assertEqual(kwargs['images'], [])
                    self.assertNotIn('requested_sources', request)
                    final_calls.append(request)
                    if len(final_calls) == 1:
                        return summary_answer(request, ready=False, findings=[{
                            'target_ids': ['a001:photo:p01:display_watermark'], 'reason': '仅第一张日期判断矛盾。'}])
                    return summary_answer(request)
                rework_calls.append(request);image_calls.append(kwargs['images'])
                self.assertEqual([c['rule_id'] for c in request['model_result']['checks']], ['display_watermark'])
                self.assertNotIn('p02', [d['unit_id'] for d in request['documents']])
                self.assertEqual(request['available_original_images'], ['p01.png'])
                check = deepcopy(request['model_result']['checks'][0])
                if not kwargs['images']:
                    return {'ready': False, 'notes': [], 'checks': [check], 'image_requests': ['p01.png']}
                self.assertEqual([p.name for p in kwargs['images']], ['p01.png'])
                return {'ready': True, 'notes': [], 'checks': [{**check, 'status': 'pass', 'reason': '仅复核第一张，日期符合。'}]}
            with patch.object(provider, '_call', side_effect=call), \
                 patch.object(provider, 'audit', side_effect=AssertionError('no whole photo audit')):
                flow.logic_review();flow.verification()
            self.assertEqual(len(final_calls), 2)
            self.assertEqual([len(x) for x in image_calls], [0, 1])
            self.assertEqual(evidence['photo_reviews'][1:], other_photos)
            self.assertEqual([c for c in evidence['checks'] if c['rule_id'] != 'display_watermark'], other_checks)
            self.assertEqual(flow.logic_review_result['status'], 'passed')

    def test_same_conflict_without_new_evidence_stops_without_repeating_other_steps(self):
        from audit_core.model_authority import execution
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
            _, _, provider, flow = self.setup_flow(folder)
            calls = []
            def call(root, skill, schema, prompt, **kwargs):
                request = json.loads(prompt.partial_variables['request']);calls.append(skill)
                if skill == SKILL:
                    return summary_answer(request, ready=False, findings=[{
                        'target_ids': ['a001:check:contract_signed'], 'reason': '同一矛盾仍未解决。'}])
                return {'ready': True, 'notes': [], 'checks': request['model_result']['checks']}
            with patch.object(provider, '_call', side_effect=call):
                flow.logic_review();flow.verification()
            self.assertEqual(calls.count(SKILL), 2)
            self.assertEqual(len(calls), 3)
            self.assertEqual(flow.logic_review_result['status'], 'not_passed')
            self.assertEqual(flow.logic_review_result['rework_rounds'], 1)

    def test_distinct_conflicts_can_make_progress_across_more_than_two_rounds(self):
        from audit_core.model_authority import execution
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
            _, packet, provider, flow = self.setup_flow(folder)
            selected = ['contract_signed', 'pos_fields', 'pos_arithmetic']
            reviews, reworked = [], []
            def call(root, skill, schema, prompt, **kwargs):
                request = json.loads(prompt.partial_variables['request'])
                if skill == SKILL:
                    reviews.append(request)
                    if len(reviews) <= len(selected):
                        rule = selected[len(reviews)-1]
                        return summary_answer(request, ready=False, findings=[{
                            'target_ids': ['a001:check:' + rule], 'reason': '仅核对当前发现的这一处矛盾。'}])
                    return summary_answer(request)
                check = request['model_result']['checks'][0]
                reworked.append(check['rule_id'])
                return {'ready': True, 'notes': [], 'checks': [{**check, 'status': 'pass',
                    'reason': '已依据本单资料解决本项矛盾。'}]}
            with patch.object(provider, '_call', side_effect=call):
                flow.logic_review();flow.verification()
            self.assertEqual(reworked, selected)
            self.assertEqual(len(reviews), 4)
            self.assertEqual(flow.logic_review_result['rework_rounds'], 3)
            self.assertEqual(flow.logic_review_result['status'], 'passed')

    def test_summary_transport_failure_preserves_original_and_does_not_retry_at_sealing(self):
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            _, packet, provider, flow = self.setup_flow(folder)
            before = deepcopy(packet)
            with patch.object(provider, '_call', side_effect=TimeoutError('synthetic model unavailable')) as calls:
                flow.logic_review();flow.verification()
            self.assertEqual(calls.call_count, 1)
            self.assertEqual(packet, before)
            self.assertEqual(flow.logic_review_result['status'], 'unavailable')
            self.assertNotIn('reviewed_analysis_summary', flow.receipt)
            self.assertEqual(flow.receipt['verification']['summary_review']['status'], 'unreviewed')

    def test_all_eight_scenarios_keep_unknowns_and_do_not_change_business_rules(self):
        from audit_core.model_authority import execution
        for scenario in SCENARIO_LABELS:
            with self.subTest(scenario=scenario), TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
                _, packet, provider, flow = self.setup_flow(folder, scenario)
                before = deepcopy(packet)
                def call(root, skill, schema, prompt, **kwargs):
                    self.assertEqual(skill, SKILL);self.assertEqual(kwargs['images'], [])
                    request = json.loads(prompt.partial_variables['request'])
                    self.assertNotIn('rules', request)
                    return summary_answer(request)
                with patch.object(provider, '_call', side_effect=call) as calls:
                    flow.logic_review();flow.verification()
                self.assertEqual(calls.call_count, 1)
                self.assertEqual(packet, before)
                self.assertEqual(flow.logic_review_result['status'], 'passed')
                self.assertTrue(any(c['status'] == 'unknown' for c in packet['audit_evidence']['checks']))

    def test_omitted_real_problem_keeps_the_original_draft(self):
        from audit_core.model_authority import execution
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), execution():
            _, _, provider, flow = self.setup_flow(folder)
            with patch.object(provider, '_call', return_value={'ready': True, 'issues': [], 'items': []}):
                flow.logic_review();flow.verification()
            self.assertEqual(flow.logic_review_result['status'], 'not_passed')
            self.assertNotIn('reviewed_analysis_summary', flow.receipt)

    def test_sealing_alone_never_starts_model_review(self):
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            _, _, provider, flow = self.setup_flow(folder)
            with patch.object(provider, '_call', side_effect=AssertionError('sealing must not call AI')):
                flow.verification()
            self.assertNotEqual(flow.receipt['verification']['logic_review'].get('status'), 'passed')

    def test_main_chain_keeps_summary_stage_between_decision_and_verification(self):
        from audit_core.workflow import build_audit_chain
        names = ['intake', 'analysis', 'evidence', 'decision', 'logic_review', 'verification']
        self.assertEqual([s.name for s in build_audit_chain([(n, getattr(PdfWorkflow, n)) for n in names]).steps], names)
