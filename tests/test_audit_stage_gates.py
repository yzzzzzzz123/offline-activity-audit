"""Formal stage gates/tools with synthetic sources; never call a real model."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import jsonschema

from audit_core.audit_self_check import stage_self_check, money_context, StageGatePending
from audit_core.audit_tools import execute_tool, dispatch_requests
from audit_core.audit_rag import reference_fields, retrieve, ROLE_GROUPS
from audit_core.common import AuditError
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.model_settings import api_selection
from audit_core.pdf_policy import rules
from audit_core.qince_context import APPLICATIONS, VERIFICATION, _linked_applications, context_sources, load_context
from audit_core.qince_references import (reference_needs, fetch_references, CUSTOMERS, PRODUCTS, SKU_CODES,
                                        PRIMITIVE_REPORT, CHECKED_REPORT, RESULTS, ACTUAL)
from audit_core.scenario_registry import SKILL_BY_SCENARIO
from audit_core.stage_context import compile_context, stage_plan, encode_context
from audit_core.qwen_model import QwenChatModel, extract_qwen_json, system_prompt
from audit_core.prompts import bound_prompt
from langchain_core.messages import HumanMessage
from tests.test_audit_rag_contract import case_for
from tests.test_qince_references import references


def item(rule='contract_dealer', reason='费用代垫经销商未取得。', status='unknown', sources=()):
    return {'rule_id': rule, 'reason': reason, 'status': status, 'source_ids': list(sources), 'comparisons': []}


def validate_packet(value, selected):
    if set(c['rule_id'] for c in value['checks']) != set(selected):
        raise AuditError('wrong rules')
    return value


class SelfCheckTests(unittest.TestCase):
    def test_available_field_interpretation_is_left_to_model(self):
        case, _ = case_for(1)
        for reason, allowed in [('费用代垫经销商未取得。', True), ('合同印章模糊，无法辨认完整名称。', True)]:
            sources = reference_fields(case['documents'],case['units'])['advance_dealer']['values'][0]['source_ids']
            value = {'checks': [item(reason=reason, sources=sources)]}
            with patch('httpx.Client', side_effect=AssertionError('no network')):
                result = stage_self_check(value, value, case, ['contract_dealer'], stage='names')
            self.assertEqual(result['can_advance'], allowed)
            self.assertEqual(result['checker_model_calls'], 0)

    def test_true_missing_field_is_not_automatically_technical_failure(self):
        case, _ = case_for(1)
        case['documents'] = [d for d in case['documents'] if not d['unit_id'].startswith('a001-qince')]
        case['units'] = [u for u in case['units'] if u.get('source_kind') != 'qince']
        value = {'checks': [item()]}
        self.assertTrue(stage_self_check(value, value, case, ['contract_dealer'], stage='x')['can_advance'])

    def test_reference_field_states_are_collected_without_code_veto(self):
        case, _ = case_for(1)
        value = {'checks': [item(reason='名称一致。', status='pass', sources=['contract'])]}
        self.assertTrue(stage_self_check(value, value, case, ['contract_dealer'], stage='x')['can_advance'])
        ids = reference_fields(case['documents'], case['units'])['advance_dealer']['values'][0]['source_ids']
        value['checks'][0]['source_ids'] += ids
        self.assertTrue(stage_self_check(value, value, case, ['contract_dealer'], stage='x')['can_advance'])

    def test_line_amount_is_summed_exactly_but_not_fabricated_header_or_approval(self):
        case, raw = case_for(2)
        for row in raw[APPLICATIONS][0]['subList']: row['applyAmount'] = '600.00'
        case['units'], case['documents'] = context_sources(raw, 'HX_TEST', 'a001')
        result = money_context(case)
        self.assertEqual(result['linked_rows_sum'], '1200.00')
        self.assertEqual(result['linked_row_count'], 2)
        self.assertIn('不能冒充', result['scope'])
        value = {'checks': [item('contract_budget_amount', '结算单6000元已读取，但系统申请总金额均未取得，只有行金额600元。')]}
        report = stage_self_check(value, value, case, ['contract_budget_amount'], stage='money')
        self.assertTrue(report['can_advance'])
        self.assertEqual(report['rework'], [])
        value['checks'][0]['reason'] = '关联申请明细合计1200元；合同的预算金额看不清，暂时不能完成比较。'
        value['checks'][0]['source_ids'] = [row['unit_id'] for row in result['rows']]
        self.assertTrue(stage_self_check(value, value, case, ['contract_budget_amount'], stage='money')['can_advance'])

    def test_code_does_not_classify_customer_prose_by_keywords(self):
        case, _ = case_for(1)
        value = {'checks': [item('contract_activity_dates', '问题在日期对应，不是缺少门牌。')]}
        self.assertTrue(stage_self_check(value, value, case, ['contract_activity_dates'], stage='dates')['can_advance'])
        value['checks'][0]['reason'] = '“确认确认商店”印章模糊，无法辨认日期。'
        value['checks'][0]['source_ids'] = reference_fields(case['documents'],case['units'])['activity_period']['values'][0]['source_ids']
        self.assertTrue(stage_self_check(value, value, case, ['contract_activity_dates'], stage='dates')['can_advance'])

    def test_incomplete_or_extra_rule_coverage_cannot_advance(self):
        case, _ = case_for(1)
        for checks in ([], [item(), item()]):
            with self.assertRaises(AuditError):
                stage_self_check({'checks': checks}, {'checks': checks}, case, ['contract_dealer'], stage='x')




class ToolAndBudgetTests(unittest.TestCase):
    def test_tools_are_case_scoped_readonly_and_closed(self):
        case, _ = case_for(1); original = deepcopy(case)
        with patch('httpx.Client', side_effect=AssertionError('no network')):
            for name in ('reference_fields', 'linked_application_amounts', 'source_inventory', 'pos_calculation'):
                self.assertEqual(execute_tool(case, name)['model_calls'], 0)
        self.assertEqual(case, original)
        with self.assertRaises(AuditError): execute_tool(case, 'shell')
        with self.assertRaises(AuditError):
            dispatch_requests(case, [{'tool': 'reference_fields', 'rule_ids': ['contract_dealer'], 'fields': ['activity_period']}], ['contract_dealer'])

    def test_cli_reads_saved_case_and_returns_machine_json_without_model(self):
        case, _ = case_for(1)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'case.json'; source.write_text(json.dumps(case), encoding='utf-8')
            script = Path(__file__).resolve().parents[1]/'skills/orchestrate-offline-audit/scripts/audit_tools.py'
            result = subprocess.run([sys.executable, '-B', str(script), '--case', str(source), 'reference_fields'],
                                    text=True, capture_output=True, encoding='utf-8', check=True)
            self.assertEqual(json.loads(result.stdout)['model_calls'], 0)

    def test_all_eight_generated_stage_plans_cover_exact_rules_without_io_caps(self):
        for scenario in SKILL_BY_SCENARIO:
            plan = stage_plan(scenario)
            self.assertCountEqual([r for p in plan['stages'] for r in p['rule_ids']], [r.id for r in rules(scenario)])
            self.assertTrue(all(len(p['rule_ids']) <= 4 for p in plan['stages']))
            for packet in plan['stages']:
                context = compile_context(scenario, packet['rule_ids'], {})
                self.assertEqual([r['id'] for r in context['rules']], packet['rule_ids'])
                self.assertTrue(encode_context(context))
                self.assertIsNone(context['max_text_bytes'])
                self.assertIsNone(context['max_output_tokens'])

    def test_actual_api_system_uses_only_current_stage_without_output_limit(self):
        from tests.test_model_transport_timeouts import stream_response
        from audit_core import qwen_model
        context = compile_context('personnel_incentive', ['contract_signed'], {})
        captured = []
        def complete(selection, content, **kwargs):
            captured.append(kwargs)
            return '{"ok":true}', {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); schema = root/'schema.json'
            schema.write_text(json.dumps({'type': 'object', 'required': ['ok'], 'properties': {'ok': {'const': True}}}))
            with patch.object(qwen_model, 'complete_json', side_effect=complete):
                extract_qwen_json(connection=api_selection({'provider': 'deepseek', 'api_key': 'fixture'}),
                    root=root, skill=SKILL_BY_SCENARIO['personnel_incentive'], schema_path=schema,
                    prompt=bound_prompt('仅本批资料'), images=[], label='synthetic', validator=lambda v: v,
                    stage_context=context, max_attempts=1)
        self.assertEqual(len(captured), 1)
        self.assertIsNone(captured[0]['max_tokens'])
        self.assertNotIn('--- SKILL.md ---', captured[0]['system'])
        self.assertNotIn('settlement_pos_quantity', captured[0]['system'])

    def test_large_text_is_sent_intact_without_application_rejection(self):
        model = QwenChatModel(selection=api_selection({'provider': 'ppio', 'api_key': 'sk_fixture_never_send'}),
                              images=(), system_prompt='system', model_root=Path('.'))
        original = '大'*50_000  # More than the former 128 KB hard limit.
        with patch('audit_core.qwen_model.complete_json', return_value=('{"ok":true}', {})) as complete:
            model._generate([HumanMessage(content=[{'type': 'text', 'text': original}])])
            complete.assert_called_once()
            self.assertEqual(complete.call_args.args[1], [{'type': 'text', 'text': original}])
            self.assertIsNone(complete.call_args.kwargs['max_tokens'])


class QinceMinimalFetchTests(unittest.TestCase):
    def test_unspecified_purpose_does_not_query_auxiliary_interfaces(self):
        raw = references(); client = MagicMock()
        client.pages.side_effect = lambda method, params: raw.get(method, [])
        linked = _linked_applications(raw[VERIFICATION], raw[APPLICATIONS])
        fetch_references(client, {k: raw[k] for k in (VERIFICATION, APPLICATIONS, 'queryRegularSale')}, 'HX_TEST', linked)
        methods = {c.args[0] for c in client.pages.call_args_list}
        self.assertEqual(methods, set())
        self.assertFalse(methods & {PRIMITIVE_REPORT, CHECKED_REPORT, RESULTS, ACTUAL, SKU_CODES})

    def test_only_visual_rules_fetch_terminal_and_only_display_fetches_products_and_dimensions(self):
        self.assertIn('dimensions', reference_needs('promotional_display'))
        for scenario in ('personnel_incentive', 'pos_target_incentive', 'poster_material', None, 'unknown'):
            self.assertEqual(reference_needs(scenario), set())
        for scenario in ('entry_fee', 'giveaway_promotion', 'self_procured_gift_material', 'price_difference_support'):
            self.assertEqual(reference_needs(scenario), {'terminals'})
        self.assertNotIn('historical_photos', reference_needs('promotional_display'))

    def test_runtime_passes_scenario_without_second_connection(self):
        with patch('audit_core.qince_context.QinceClient.from_environment') as factory:
            load_context('HX_TEST', scenario='pos_target_incentive')
            factory.return_value.fetch.assert_called_once_with('HX_TEST', include_references=True, scenario='pos_target_incentive')

    def test_photo_stage_excludes_amount_and_manager_but_keeps_store_dates_and_skus(self):
        case, _ = case_for(1)
        docs = retrieve(case['documents'], case['materials'], case['units'], 'photos', ROLE_GROUPS['photos'][0])
        text = json.dumps(docs, ensure_ascii=False)
        self.assertNotIn('applyAmount=', text)
        self.assertNotIn('dealerName=', text)
        self.assertIn('startDate=', text)
        self.assertIn('cusName=', text)


if __name__ == '__main__': unittest.main()
