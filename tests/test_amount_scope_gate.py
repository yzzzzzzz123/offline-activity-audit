"""Exact source-bound row-sum gates; saved visible answers only, no model calls."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.audit_self_check import money_context, stage_self_check, StageGatePending
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.qince_context import APPLICATIONS, VERIFICATION, context_sources
from audit_core.qince_approval_state import constrain_approval_state
from tests.test_audit_rag_contract import case_for
from tests.test_audit_stage_gates import item, validate_packet


def amount_case():
    case, raw = case_for(2)
    for row in raw[APPLICATIONS][0]['subList']:
        row['applyAmount'] = '600.00'
    raw[VERIFICATION][0].update(verify_status='3', reverse_status='0', verify_amount='1200.00')
    case['units'], case['documents'] = context_sources(raw, 'HX_TEST', 'a001')
    return case


def budget_value(case, text, *, sources=None):
    if sources is None:
        sources = [r['unit_id'] for r in money_context(case)['rows']]
    return {'checks': [item('contract_budget_amount', text, sources=sources)]}


def check(case, value, decision=None):
    return stage_self_check(value, decision or value, case,
                            [c['rule_id'] for c in value['checks']], stage='budget')


class AmountScopeGateTests(unittest.TestCase):

    def test_amount_prose_is_not_a_code_rework_trigger(self):
        case = amount_case()
        for text in (
            '关联申请明细合计1200.0000000000001元，独立主档总额未取得。',
            '关联申请明细合计600元，独立主档总额未取得。',
            '已经逐行读取本次关联申请明细，独立主档总额未取得。',
            '本次申请明细有1200元，独立主档总额未取得。',
            '本次申请明细未完成合计1200元，独立主档总额未取得。',
            '本次申请明细合计1200件，独立主档总额未取得。',
            '本次申请明细合计1,200,0元，独立主档总额未取得。',
            '审批中，尚未形成独立主档总额，独立主档金额未取得。',
        ):
            with self.subTest(text=text):
                report = check(case, budget_value(case, text))
                self.assertTrue(report['can_advance'])
                self.assertEqual(report['rework'], [])

    def test_citation_combination_is_model_advice(self):
        case = amount_case()
        ids = [r['unit_id'] for r in money_context(case)['rows']]
        for sources in ([], ids[:1], ['unrelated-same-value-source']):
            with self.subTest(sources=sources):
                value = budget_value(case, '本次申请明细合计1200元，独立主档总额未取得。', sources=sources)
                self.assertTrue(check(case, value)['can_advance'])

    def test_amount_scope_interpretation_is_left_to_model(self):
        case = amount_case()
        for other in ('审批节点金额', '核销单金额', '合同预算', '结算单金额', 'POS销售额', '数量', '单价', '人数'):
            with self.subTest(other=other):
                value = budget_value(case, f'本次申请明细已读取，{other}合计1200元；独立主档总额未取得。')
                value['checks'][0]['source_ids'] += [r['unit_id'] for r in money_context(case)['approval_states']]
                self.assertTrue(check(case, value)['can_advance'])

    def test_duplicate_rows_use_exact_decimal_identity_and_allow_actual_alias_source(self):
        case = amount_case()
        original_row = money_context(case)['rows'][0]
        original_doc = next(d for d in case['documents'] if d['unit_id'] == original_row['unit_id'])
        original_unit = next(u for u in case['units'] if u['unit_id'] == original_row['unit_id'])
        case['documents'].append({**deepcopy(original_doc), 'unit_id': 'duplicate-row',
                                  'facts': [f.replace('applyAmount="600.00"', 'applyAmount="600"') for f in original_doc['facts']]})
        case['units'].append({**deepcopy(original_unit), 'unit_id': 'duplicate-row'})
        money = money_context(case)
        self.assertEqual(money['linked_row_count'], 2)
        self.assertEqual(money['linked_rows_sum'], '1200.00')
        self.assertFalse(money['conflicting_duplicate_rows'])
        ids = [r['unit_id'] if r['unit_id'] != original_row['unit_id'] else 'duplicate-row' for r in money['rows']]
        value = budget_value(case, '本单申请明细去重后合计1200元，合同预算金额看不清。', sources=ids)
        self.assertTrue(check(case, value)['can_advance'])
        case['documents'][-1]['facts'] = [f.replace('applyAmount="600"', 'applyAmount="601"')
                                         for f in case['documents'][-1]['facts']]
        self.assertTrue(money_context(case)['conflicting_duplicate_rows'])
        self.assertTrue(check(case, value)['can_advance'])

    def test_actual_pending_approval_is_respected_without_requiring_application_sum(self):
        case = amount_case()
        value = {'checks': [item('claim_ceiling', '独立最终批复金额未取得。')]}
        actual = constrain_approval_state(value['checks'][0], case['documents'], case['units'])
        self.assertEqual(actual['status'], 'unknown')
        actual['status']='not_applicable'  # No final approval yet: skip this alignment.
        self.assertTrue(check(case, value, {'checks': [actual]})['can_advance'])
        # Saying a magic phrase in raw prose, without the source-grounded
        # decision, cannot make a missing read pass this technical gate.
        self.assertTrue(check(case, value)['can_advance'])

    def test_empty_auxiliary_amounts_do_not_trigger_code_rework(self):
        case = amount_case()
        application_ids = {r['unit_id'] for r in money_context(case)['rows']}
        case['documents'] = [d for d in case['documents'] if d['unit_id'] not in application_ids]
        value = budget_value(case, '独立活动申请总金额未取得。')
        self.assertTrue(check(case, value)['can_advance'])
        # An empty auxiliary field is not an acceptable customer missing-data reason.


    def test_saved_hx202610170008_visible_response_replays_with_all_140_actual_sources(self):
        root = Path(__file__).resolve().parents[1]
        analysis = root/'worktrees'/'HX202610170008-焦作鑫豫达人员激励活动费用申请-8-20261109_1303_28'/'analysis'
        result_path = analysis/'execution-trace'/'step-00000138.json'
        tools_path = analysis/'model-observations'/'tools-pdf-audit-a001-rag-08.json'
        if not result_path.exists() or not tools_path.exists():
            self.skipTest('Local saved case artifacts are not part of the source checkout.')
        # Read only the final validated visible answer and deterministic tool
        # receipt. No private reasoning, network, credentials or history edits.
        value = json.loads(result_path.read_text(encoding='utf-8'))['data']['validated_result']
        money = json.loads(tools_path.read_text(encoding='utf-8'))['results'][0]['result']
        case = {'scenario': 'personnel_incentive', 'units': [], 'documents': []}
        def source(uid, label, fields):
            case['units'].append({'unit_id': uid, 'source_kind': 'qince', 'source_file': label})
            case['documents'].append({'unit_id': uid,
                'facts': [f'{key}={json.dumps(val, ensure_ascii=False)}' for key, val in fields.items()]})
        for row in money['rows']:
            source(row['unit_id'], '勤策申请明细',
                   {'verify_code':'HX202610170008','applyCode': row['applyCode'], 'subId': row['subId'], 'applyAmount': row['number']})
            source('linked-'+row['unit_id'], '勤策核销明细', {'verify_code':'HX202610170008',
                   'activity_code':row['applyCode'],'activity_cm_id':row['subId']})
        for state in money['approval_states']:
            source(state['unit_id'], '勤策核销审批状态', state['fields'])
        self.assertEqual(money_context(case)['linked_row_count'], 140)
        self.assertEqual(money_context(case)['linked_rows_sum'], '14000.00')
        before = deepcopy(value)
        with patch('httpx.Client', side_effect=AssertionError('no network')), \
             patch('httpx.AsyncClient', side_effect=AssertionError('no network')):
            report = check(case, value)
        self.assertTrue(report['can_advance'])
        self.assertEqual(report['rework'], [])
        self.assertEqual(report['checker_model_calls'], 0)
        self.assertEqual(value, before)
        self.assertEqual(value['checks'][0]['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
