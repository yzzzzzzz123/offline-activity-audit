"""Workflow and node amounts are synthetic; no network or model requests."""
from copy import deepcopy
import json
from types import SimpleNamespace
import unittest

from audit_core.audit_rag import retrieve, reference_fields
from audit_core.fee_policy_checks import constrain_application_check, approval_sources
from audit_core.qince_approval_state import constrain_approval_state, SOURCE_LABEL
from audit_core.qince_context import context_sources, VERIFICATION
from tests.test_qince_context import fixture


def projected(status='3', reverse='0', **extra):
    raw = fixture()
    raw[VERIFICATION][0].update(verify_status=status, reverse_status=reverse,
                                verify_amount='14000.00', audit_time='2026-11-04 14:29:16', **extra)
    return raw, *context_sources(raw, 'HX_TEST', 'amount-state')


def check(status='pass', rule='claim_ceiling'):
    return {'rule_id': rule, 'status': status, 'reason': '隔离测试',
            'source_ids': [], 'comparisons': []}


class ApprovalStateTests(unittest.TestCase):
    def test_real_source_projection_keeps_original_status_without_changing_raw(self):
        raw = fixture()
        raw[VERIFICATION][0].update(verify_status='3', reverse_status='0', verify_amount='14000.00')
        original = deepcopy(raw)
        units, docs = context_sources(raw, 'HX_TEST', 'amount-state')
        state = next(d for u, d in zip(units, docs) if u['source_file'] == SOURCE_LABEL)
        self.assertIn('verify_status="3"', state['facts'])
        self.assertIn('verify_amount="14000.00"', state['facts'])
        self.assertEqual(raw, original)

    def test_pending_state_cannot_become_a_final_approval_pass(self):
        _, units, docs = projected()
        result = constrain_application_check('personnel_incentive', SimpleNamespace(id='claim_ceiling', text='结算实际核销金额核对'),
                                             check(), docs, [], units)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('审批中', result['reason'])
        self.assertIn('14000.00元', result['reason'])
        self.assertIn('尚未形成', result['reason'])
        self.assertNotIn('未提供', result['reason'])
        self.assertTrue(result['source_ids'])

    def test_other_nonfinal_states_keep_their_actual_meaning(self):
        for status, name in [('0', '待审批'), ('2', '被打回'), ('4', '已作废'), ('5', '已撤回')]:
            with self.subTest(status=status):
                _, units, docs = projected(status)
                result = constrain_approval_state(check(), docs, units)
                self.assertEqual(result['status'], 'unknown')
                self.assertIn(name, result['reason'])

    def test_approved_normal_state_exposes_the_original_final_node_header_amount(self):
        _, units, docs = projected('1')
        self.assertIsNone(constrain_approval_state(check(), docs, units))
        approvals = approval_sources(docs, [], units)
        self.assertEqual(len(approvals), 1)
        document = next(d for d in docs if d['unit_id'] in approvals)
        self.assertIn('verify_amount="14000.00"', document['facts'])
        self.assertNotIn('approvedAmount=', str(document['facts']))

    def test_valid_approved_amount_still_uses_existing_independent_source(self):
        _, units, docs = projected('1', approvedAmount='14000.00')
        self.assertIsNone(constrain_approval_state(check(), docs, units))
        self.assertTrue(approval_sources(docs, [], units))
        indexed = reference_fields(docs, units)['approved_total']
        self.assertEqual(indexed['state'], 'available')
        self.assertTrue(any(v['source_fields'].get('verify_status') == '1' for v in indexed['values']))

    def test_pending_header_is_never_indexed_as_final_approval(self):
        _, units, docs = projected()
        self.assertEqual(approval_sources(docs, [], units), set())
        self.assertEqual(reference_fields(docs, units)['approved_total']['state'], 'missing')

    def test_approved_state_without_normal_reverse_status_does_not_promote_node_amount(self):
        raw = fixture()
        raw[VERIFICATION][0].update(verify_status='1', verify_amount='14000.00')
        units, docs = context_sources(raw, 'HX_TEST', 'amount-state')
        self.assertEqual(approval_sources(docs, [], units), set())

    def test_explicit_amount_does_not_overrule_pending_status(self):
        _, units, docs = projected('3', approvedAmount='14000.00')
        result = constrain_approval_state(check(), docs, units)
        self.assertEqual(result['status'], 'unknown')

    def test_reversed_amount_is_not_a_current_final_approval(self):
        for reverse in ('1', '2'):
            _, units, docs = projected('1', reverse)
            self.assertIn('红冲', constrain_approval_state(check(), docs, units)['reason'])

    def test_budget_retrieval_keeps_the_whole_workflow_source(self):
        _, units, docs = projected()
        selected = retrieve(docs, [], units, 'budget', set())
        state = next(d for u, d in zip(units, docs) if u['source_file'] == SOURCE_LABEL)
        self.assertIn(state, selected)
        self.assertFalse(any('verify_status' in str(d) for d in retrieve(docs, [], units, 'dates', set())))

    def test_other_verification_never_contributes_its_workflow(self):
        raw = fixture()
        raw[VERIFICATION].append({'verify_code':'OTHER', 'verify_status':'3', 'verify_amount':'98765', 'details':[]})
        units, docs = context_sources(raw, 'HX_TEST', 'amount-state')
        self.assertNotIn('98765', json.dumps(docs))
        self.assertIsNone(constrain_approval_state(check(), docs, units))

    def test_legacy_inputs_without_workflow_are_unchanged(self):
        units, docs = context_sources(fixture(), 'HX_TEST', 'amount-state')
        self.assertIsNone(constrain_approval_state(check(), docs, units))
        self.assertEqual(len(units), 3)

    def test_conflicting_workflow_sources_are_not_arbitrarily_selected(self):
        _, units, docs = projected()
        state = next(d for u, d in zip(units, docs) if u['source_file'] == SOURCE_LABEL)
        second = deepcopy(state)
        second['unit_id'] = 'CONFLICT'
        second['facts'] = [f.replace('verify_status="3"', 'verify_status="1"') for f in second['facts']]
        units.append({'unit_id':'CONFLICT', 'source_kind':'qince', 'source_file':SOURCE_LABEL})
        result = constrain_approval_state(check(), docs + [second], units)
        self.assertEqual(result['status'], 'unknown')
        self.assertIn('冲突', result['reason'])

    def test_no_guard_on_unrelated_or_not_applicable_checks(self):
        _, units, docs = projected()
        self.assertIsNone(constrain_approval_state(check(rule='contract_budget_amount'), docs, units))
        self.assertIsNone(constrain_approval_state(check(status='not_applicable'), docs, units))


if __name__ == '__main__':
    unittest.main()
