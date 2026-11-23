"""Model authority, helper takeover and source isolation, without real I/O."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_judgment import provisional, save_judgments, approved_checks, source_guard
from audit_core.model_self_check import review_stage
from audit_core.audit_tools import execute_tool, dispatch_requests, assist
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from tests.test_audit_rag_contract import case_for


def fixture():
    case, _ = case_for(1)
    check = {'rule_id': 'contract_signed', 'status': 'unknown',
        'reason': '合同印章需要核对。', 'source_ids': [case['documents'][0]['unit_id']],
        'comparisons': []}
    return case, {'checks': [check]}


class ModelLedExecutionTests(TestCase):
    def test_self_check_receives_identity_proof_and_optional_reference_scope(self):
        from audit_core.product_reference_batches import product_identity_support
        from audit_core.fee_policy_source import APPLICATION_CLARIFICATION
        case, value = fixture()
        case['scenario'] = 'entry_fee'
        case['flags']['_reference_available'] = {'entry_application_products': False}
        check = value['checks'][0]
        check.update(rule_id='entry_application_products', reason='商品编码已完成商品库对应。')
        references = {'rule_ids': ['entry_sku'], 'references': [
            {'product_code': 'sku-a', 'product_name': '商品甲', 'barcode_69': '6970000000001',
             'view_id': 'front', 'image': 'front.jpg'},
            {'product_code': 'sku-a', 'product_name': '商品甲', 'barcode_69': '6970000000001',
             'view_id': 'back', 'image': 'back.jpg'}],
            'resolutions': [{'status': 'resolved', 'product_code': 'sku-a',
                'request': {'product_code': {'unit_id': check['source_ids'][0], 'quote': '原编码', 'value': 'sku-a'}}}]}
        original = deepcopy(references)
        support = product_identity_support(references)
        self.assertEqual(len(support['identities']), 1)
        self.assertEqual(support['resolutions'], references['resolutions'])
        self.assertNotIn('front.jpg', json.dumps(support))
        self.assertEqual(references, original)
        case['_product_identity_support'] = support
        provider = PdfEvidenceProvider('fixture', 'medium')
        output = {'ready': True, 'notes': [], 'checks': [{**check, 'status': 'not_applicable',
            'reason': '本次没有独立系统条码参照，不增加此项额外对齐，已有商品身份对应保留。'}]}
        def call(root, skill, schema, prompt, **kwargs):
            request = json.loads(prompt.partial_variables['request'])
            self.assertEqual(request['product_identity_support'], support)
            context = prompt.partial_variables['_audit_stage_context']
            self.assertEqual(context['policies']['application_scope'], APPLICATION_CLARIFICATION)
            self.assertFalse(context['rules'][0]['applicable'])
            self.assertEqual(kwargs['images'], [])
            kwargs['validator'](output)
            return output
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider, '_call', side_effect=call):
            result = review_stage(provider, case, value, value, Path(folder)/'stage',
                ['entry_application_products'], lambda *a:value)
        self.assertEqual(result['checks'][0]['status'], 'not_applicable')

    def test_semantic_code_rejection_is_advice_and_original_sources_remain(self):
        case, value = fixture()
        before = deepcopy((case, value))
        def reject():
            raise AuditError('旧解析器不支持该排列')
        result = provisional(case, value, ['contract_signed'], reject)
        self.assertEqual(result['program_advice']['status'], 'model_takeover')
        self.assertFalse(result['program_advice']['authoritative'])
        self.assertEqual(result['checks'], value['checks'])
        self.assertEqual((case, value), before)

    def test_invalid_source_is_not_an_overrideable_tool_interpretation(self):
        case, value = fixture()
        value['checks'][0]['source_ids'] = ['another-run-source']
        with self.assertRaises(AuditError):
            provisional(case, value, ['contract_signed'], lambda: value)

    def test_completed_model_judgment_wins_over_program_and_is_run_source_bound(self):
        case, value = fixture()
        value['checks'][0].update(status='pass', reason='模型复核后确认合同签章有效。')
        args = (case['scenario'], case['flags'], value, case['documents'], case['materials'])
        with TemporaryDirectory() as folder:
            with collect_model_artifacts(Path(folder)/'one'):
                save_judgments(case, value, ['contract_signed'])
                with patch('audit_core.pdf_evidence._program_audit_decision', side_effect=AssertionError('must not override model')):
                    decision = audit_decision(*args, units=case['units'], selected_rule_ids=['contract_signed'])
                self.assertEqual(decision['checks'][0]['status'], 'pass')
                self.assertEqual(decision['checks'][0]['reason'], value['checks'][0]['reason'])
                changed = deepcopy(value); changed['checks'][0]['reason'] = '不同回答'
                self.assertEqual(approved_checks(*args[:2], changed, *args[3:]), {})
                changed_docs = deepcopy(case['documents']); changed_docs[0]['facts'].append('来源变更')
                self.assertEqual(approved_checks(*args[:3], changed_docs, case['materials']), {})
            with collect_model_artifacts(Path(folder)/'two'):
                self.assertEqual(approved_checks(*args), {})

    def test_model_can_correct_status_and_prose_without_reentering_semantic_gate(self):
        case, value = fixture(); before = deepcopy(value)
        provider = PdfEvidenceProvider('fixture', 'medium')
        candidate = deepcopy(value['checks']);candidate[0].update(status='pass', reason='合同签章完整。')
        output = {'ready': True, 'checks': candidate, 'notes': ['程序原先误判，采用资料实际内容。']}
        def call(root, skill, schema, prompt, **kwargs):
            self.assertIn('最终判断', prompt.format())
            self.assertIn('program_advice', prompt.format())
            kwargs['validator'](output)
            return output
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider, '_call', side_effect=call) as model:
            result = review_stage(provider, case, value,
                {'checks': [{**value['checks'][0], 'status': 'fail'}], 'program_advice': {'status': 'available'}},
                Path(folder)/'stage', ['contract_signed'],
                lambda *a: (_ for _ in ()).throw(AssertionError('no code business gate')))
            self.assertEqual(result['checks'][0]['status'], 'pass')
            self.assertEqual(model.call_count, 1)
        self.assertEqual(value, before)

    def test_tool_exception_returns_takeover_not_customer_failure(self):
        case, _ = fixture()
        with patch('audit_core.activity_periods.case_activity_periods', side_effect=ValueError('unsupported layout')):
            result = execute_tool(case, 'activity_periods')
        self.assertEqual(result['status'], 'model_takeover')
        self.assertEqual(result['fallback'], 'read_original_stage_sources')
        self.assertNotIn('business_status', result)

    def test_unknown_tool_and_cross_stage_request_are_still_rejected(self):
        case, _ = fixture()
        with self.assertRaises(AuditError):execute_tool(case, 'shell')
        with self.assertRaises(AuditError):
            dispatch_requests(case, [{'tool': 'source_inventory', 'rule_ids': ['other'], 'fields': []}], ['contract_signed'])

    def test_native_optimization_failure_uses_caller_fallback(self):
        with patch('audit_core.execution_trace.record_step') as trace:
            result = assist('native-layout', lambda: (_ for _ in ()).throw(ValueError('new layout')), fallback={})
        self.assertEqual(result, {})
        self.assertTrue(trace.call_args.args[2]['model_takeover'])

    def test_unfinished_model_self_check_is_soft_and_cannot_create_completed_authority(self):
        case, value = fixture()
        provider = PdfEvidenceProvider('fixture', 'medium')
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider, '_call', return_value={'ready': False, 'checks': value['checks'], 'notes': ['未完成']}):
            result = review_stage(provider, case, value, value, Path(folder)/'stage', ['contract_signed'], lambda *a: value)
            self.assertEqual(result, value)
            self.assertEqual(approved_checks(case['scenario'],case['flags'],value,case['documents'],case['materials']), {})
            self.assertTrue(provider._soft_check_advice[0]['soft_gate'])
            receipts = [json.loads(p.read_text(encoding='utf-8')) for p in (Path(folder)/'analysis').glob('model-judgments/**/*.json')]
            self.assertTrue(receipts)
            self.assertTrue(all(r['authority']=='provisional_model_result' for r in receipts))

    def test_stage_no_longer_reworks_on_wording_regex(self):
        case, value = fixture()
        value['checks'][0]['reason'] = '暂时暂时不能确认，问题在对应，不是缺件。'
        provider = PdfEvidenceProvider('fixture', 'medium')
        with TemporaryDirectory() as folder, patch.object(provider, '_review_stage_summary', return_value=value) as review, \
             patch.object(provider, '_call', side_effect=AssertionError('no keyword-driven rework')):
            result = provider._finish_audit_stage(case, value, Path(folder)/'stage', ['contract_signed'], lambda *a: value, images=[])
        self.assertIs(result, value)
        self.assertEqual(review.call_count, 1)

    def test_unmatched_quote_is_preserved_for_model_review_without_weakening_source_identity(self):
        case, value = fixture()
        uid = value['checks'][0]['source_ids'][0]
        value['checks'][0]['reference_evidence'] = {'written_name': {'unit_id': uid, 'quote': '不存在的原文', 'value': '伪造'},
            'seal_name': None, 'periods': []}
        original=deepcopy(value)
        advice=source_guard(value, case['documents'], ['contract_signed'])
        self.assertFalse(advice['authoritative'])
        self.assertEqual(advice['status'],'model_takeover')
        self.assertEqual(advice['issues'][0]['constraint'],'quote_source_match')
        self.assertEqual(advice['issues'][0]['quote'],'不存在的原文')
        self.assertEqual(value,original)
        value['checks'][0]['reference_evidence']['written_name']['unit_id']='another-run'
        with self.assertRaises(AuditError):source_guard(value, case['documents'], ['contract_signed'])

    def test_large_context_is_not_preemptively_split_for_normal_execution(self):
        from audit_core.audit_rag import batches
        case, _ = fixture()
        case['documents'][0]['facts'].append('原始资料' * 50000)
        self.assertEqual(len(batches(case, provider_limits_only=True)), 1)

    def test_removed_maintenance_api_and_ui_are_unavailable(self):
        import urllib.error
        import urllib.request
        from tests.test_model_switching import ModelSettingsAPITests
        server = ModelSettingsAPITests(); server.setUp()
        try:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                server.opener.open(server.base + '/api/repair-items')
            self.assertEqual(raised.exception.code, 404); raised.exception.close()
            request = urllib.request.Request(server.base + '/api/runs/test-run/repair-items/' + 'a'*24 + '/resolve',
                data=b'{"note":"removed"}', headers={'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as raised:
                server.opener.open(request)
            self.assertEqual(raised.exception.code, 404); raised.exception.close()
            html = (Path(__file__).resolve().parents[1] / 'skills/orchestrate-offline-audit/assets/offline-activity-audit.html').read_text(encoding='utf-8')
            self.assertNotIn('as-tab-repairs', html)
            self.assertNotIn('/api/repair-items', html)
            self.assertNotIn('attachRecordRepairs', html)
        finally:
            server.doCleanups()

    def test_original_and_derived_flags_share_the_same_source_bound_receipt(self):
        case, value = fixture()
        augmented = {**case, 'flags': {**case['flags'], '_recorded_approval': False,
            '_reference_available': {'contract_budget_amount': False}}}
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            save_judgments(augmented, value, ['contract_signed'])
            approved = approved_checks(case['scenario'], case['flags'], value, case['documents'], case['materials'])
            self.assertIn('contract_signed', approved)

    def test_self_check_requests_only_the_original_image_it_needs(self):
        case, value = fixture(); provider = PdfEvidenceProvider('fixture', 'medium')
        calls = []
        with TemporaryDirectory() as folder:
            pictures = [Path(folder)/'one.jpg', Path(folder)/'two.jpg']
            for picture in pictures:picture.write_bytes(b'isolated-fixture')
            def call(root, skill, schema, prompt, **kwargs):
                calls.append(kwargs['images'])
                output = {'ready': len(calls)>1, 'notes': [], 'checks': value['checks'],
                    'image_requests': ['two.jpg'] if len(calls)==1 else []}
                kwargs['validator'](output)
                return output
            with patch.object(provider, '_call', side_effect=call):
                result = review_stage(provider, case, value, value, Path(folder)/'stage', ['contract_signed'],
                    lambda *a:value, images=pictures)
        self.assertEqual(calls, [[], [pictures[1]]])
        self.assertEqual(result, value)

    def test_full_provider_and_final_assembly_keep_model_correction(self):
        from tests.pdf_test_support import unknown_checks
        from audit_core.pdf_policy import rules
        case, _ = fixture()
        provider = PdfEvidenceProvider('fixture', 'medium')
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            def call(root, skill, schema, prompt, **kwargs):
                selected = schema['properties']['checks']['items']['properties']['rule_id'].get('enum')
                if selected is None:
                    selected = [r.id for r in rules(case['scenario'])]
                value = unknown_checks(case['scenario'], case['flags'])
                value = {key: item for key, item in value.items() if key in schema['properties']}
                value['checks'] = [c for c in value['checks'] if c['rule_id'] in selected]
                if 'ready' in schema['properties']:
                    for check in value['checks']:
                        if check['rule_id']=='contract_signed':
                            check.update(status='pass',reason='模型复核确认签章有效。')
                    value.update(ready=True,notes=[])
                kwargs['validator'](value)
                return value
            with patch.object(provider, '_call', side_effect=call), \
                 patch('audit_core.pdf_materials.prepare_product_references',return_value=({'rule_ids':[],'references':[]},[])), \
                 patch('audit_core.pdf_materials.prepare_pos_identities',return_value={'applicable':False,'rows':[],'limitations':[]}), \
                 patch('audit_core.pdf_evidence._program_audit_decision', side_effect=AuditError('旧程序路径不支持本次布局')):
                evidence = provider.audit(case, Path(folder)/'model')
                final = audit_decision(case['scenario'],case['flags'],evidence,case['documents'],case['materials'],units=case['units'])
        self.assertEqual(len(final['checks']),len(rules(case['scenario'])))
        signed = next(c for c in final['checks'] if c['rule_id']=='contract_signed')
        self.assertEqual(signed['status'],'pass')
        self.assertEqual(signed['reason'],'模型复核确认签章有效。')
