"""Pure source/Schema/arithmetic checks; no runner, model, network or real data writes."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

import jsonschema

from audit_core.audit_rag import part_schema, reference_fields, reference_prompt
from audit_core.audit_self_check import stage_self_check
from audit_core.common import AuditError
from audit_core.fee_policy_checks import validate_workbook_comparison
from audit_core.pdf_evidence import audit_decision, audit_schema
from audit_core.pdf_policy import AMOUNT_CLARIFICATION, APPLICATION_COMPARISON_RULES, rules
from audit_core.qince_amount_basis import (APPLICATION_TOTAL_CLARIFICATION, complete_application_sum,
    linked_application_basis, normalize_application_total_check)
from tests.pdf_test_support import flags_for


def case_for_amounts(amounts=('600.00',), budget='600', claim='600'):
    documents, units = [], []
    def add(uid, label, values):
        facts = [key + '=' + json.dumps(value, ensure_ascii=False) for key, value in values.items()]
        units.append({'unit_id': uid, 'source_kind': 'qince', 'source_file': label, 'native_facts': facts})
        documents.append({'unit_id': uid, 'roles': [], 'facts': facts, 'limitations': []})
    for index, amount in enumerate(amounts):
        add('verification-' + str(index), '勤策核销明细', {
            'verify_code': 'HX_SYNTHETIC', 'activity_code': 'SQ_SYNTHETIC', 'activity_cm_id': str(index),
            'customer_id': 'CUSTOMER-' + str(index)})
        add('application-' + str(index), '勤策申请明细', {
            'verify_code': 'HX_SYNTHETIC', 'applyCode': 'SQ_SYNTHETIC', 'subId': str(index),
            'cusId': 'CUSTOMER-' + str(index), 'applyAmount': amount})
    for uid, fact in [('contract', f'活动预算金额：“{budget} 元”。'),
                      ('settlement', f'表格合计为“{claim} 元”。')]:
        units.append({'unit_id': uid, 'source_file': uid + '.jpg'})
        documents.append({'unit_id': uid, 'roles': [], 'facts': [fact], 'limitations': []})
    return {'scenario': 'personnel_incentive', 'documents': documents, 'units': units,
            'materials': [{'id': 'promotion_contract', 'state': 'present', 'source_ids': ['contract']},
                          {'id': 'settlement', 'state': 'present', 'source_ids': ['settlement']}],
            'flags': flags_for('personnel_incentive', temporary_staff=True)}


def check_for(case, rule_id='contract_budget_amount', *, operation='value', number='600'):
    uid = 'contract' if rule_id == 'contract_budget_amount' else 'settlement'
    fact = next(d['facts'][0] for d in case['documents'] if d['unit_id'] == uid)
    basis = linked_application_basis(case['documents'], case['units'])
    return {'rule_id': rule_id, 'status': 'pass', 'reason': '比较原始金额。',
            'source_ids': [uid, *[v['unit_id'] for v in basis['terms']]],
            'comparisons': [{'label': '原始金额比较', 'value_kind': 'amount',
                'left': {'unit_id': uid, 'quote': fact, 'number': number},
                'right': deepcopy(basis['terms']), 'operation': operation,
                'operator': 'eq' if rule_id == 'contract_budget_amount' else 'le'}]}


def decide(case, check):
    return audit_decision(case['scenario'], case['flags'], {'checks': [check]},
        case['documents'], case['materials'], units=case['units'],
        selected_rule_ids=[check['rule_id']])['checks'][0]


def schema_for(case, rule_id):
    fields = {'application_total': reference_fields(case['documents'], case['units'])['application_total']}
    full = audit_schema(case['scenario'], materials=case['materials'], units=case['units'],
                        documents=case['documents'], include_photo_reviews=False)
    schema = part_schema(full, [rule_id], fields)
    # This isolates the generated numeric-check contract, without unrelated optional roots.
    return schema['properties']['checks']['items']


class ApplicationTotalSourceContractTests(unittest.TestCase):
    def setUp(self):
        guard = patch('socket.create_connection', side_effect=AssertionError('Network is forbidden in these tests'))
        guard.start()
        self.addCleanup(guard.stop)

    def test_single_complete_row_value_normalizes_for_budget_eq_and_claim_le(self):
        for rule_id in ('contract_budget_amount', 'application_claim_ceiling'):
            with self.subTest(rule=rule_id):
                case = case_for_amounts()
                check = check_for(case, rule_id)
                original = deepcopy(check)
                jsonschema.Draft202012Validator(schema_for(case, rule_id)).validate(check)
                result = decide(case, check)
                self.assertEqual(result['status'], 'pass')
                self.assertEqual(result['comparisons'][0]['operation'], 'sum')
                self.assertEqual(result['comparisons'][0]['right'], original['comparisons'][0]['right'])
                self.assertEqual(result['calculations'][0]['right_value'], '600.00')
                record = result['normalized_application_totals'][0]
                self.assertEqual(record['original_comparison'], original['comparisons'][0])
                self.assertEqual(record['normalized_comparison'], result['comparisons'][0])
                self.assertEqual(record['complete_application_basis']['row_count'], 1)
                self.assertEqual(check, original)

    def test_complete_single_and_multi_sum_remain_canonical(self):
        for amounts in (('600.00',), ('600.00', '100.00')):
            with self.subTest(amounts=amounts):
                total = '600' if len(amounts) == 1 else '700'
                case = case_for_amounts(amounts, budget=total)
                check = check_for(case, operation='sum', number=total)
                jsonschema.Draft202012Validator(schema_for(case, check['rule_id'])).validate(check)
                result = decide(case, check)
                self.assertEqual(result['status'], 'pass')
                self.assertEqual(len(result['comparisons'][0]['right']), len(amounts))
                self.assertNotIn('normalized_application_totals', result)

    def test_normalized_single_row_advances_existing_stage_self_test_without_rework(self):
        case = case_for_amounts()
        value = {'checks': [check_for(case, key) for key in ('contract_budget_amount', 'application_claim_ceiling')]}
        original = deepcopy(value)
        selected = [c['rule_id'] for c in value['checks']]
        decision = audit_decision(case['scenario'], case['flags'], value, case['documents'],
            case['materials'], units=case['units'], selected_rule_ids=selected)
        report = stage_self_check(value, decision, case, selected, stage='budget-contract')
        self.assertTrue(report['can_advance'])
        self.assertEqual(report['rework'], [])
        self.assertEqual(report['checker_model_calls'], 0)
        self.assertEqual(value, original)

    def test_partial_sum_or_single_value_from_multirow_scope_is_rejected_by_schema_and_guard(self):
        case = case_for_amounts(('600.00', '100.00'), budget='700')
        for operation in ('sum', 'value'):
            with self.subTest(operation=operation):
                check = check_for(case, operation=operation, number='700')
                check['comparisons'][0]['right'].pop()
                self.assertTrue(list(jsonschema.Draft202012Validator(schema_for(case, check['rule_id'])).iter_errors(check)))
                with self.assertRaises(AuditError):
                    decide(case, check)

    def test_zero_is_a_valid_complete_single_row_amount(self):
        case = case_for_amounts(('0',), budget='0', claim='0')
        for rule_id in ('contract_budget_amount', 'application_claim_ceiling'):
            with self.subTest(rule=rule_id):
                check = check_for(case, rule_id, number='0')
                jsonschema.Draft202012Validator(schema_for(case, rule_id)).validate(check)
                result = decide(case, check)
                self.assertEqual(result['status'], 'pass')
                self.assertEqual(result['calculations'][0]['right_value'], '0')
                self.assertEqual(reference_fields(case['documents'], case['units'])['application_total']['state'], 'available')

    def test_real_budget_and_claim_601_versus_600_are_business_failures(self):
        case = case_for_amounts(budget='601', claim='601')
        for rule_id in ('contract_budget_amount', 'application_claim_ceiling'):
            with self.subTest(rule=rule_id):
                result = decide(case, check_for(case, rule_id, number='601'))
                self.assertEqual(result['status'], 'fail')
                self.assertFalse(result['calculations'][0]['matches'])
                self.assertEqual(result['calculations'][0]['left_value'], '601')
                self.assertEqual(result['calculations'][0]['right_value'], '600.00')

    def test_decimal_difference_is_not_rounded_away(self):
        case = case_for_amounts(budget='600.0000000000000001')
        self.assertEqual(decide(case, check_for(case, number='600.0000000000000001'))['status'], 'fail')

    def test_wrong_source_role_and_self_reference_cannot_be_normalized(self):
        for variant in ('other-source', 'contract-as-application', 'settlement-as-contract', 'wrong-row-role'):
            with self.subTest(variant=variant):
                case = case_for_amounts()
                check = check_for(case)
                comparison = check['comparisons'][0]
                if variant == 'other-source':
                    comparison['right'][0]['unit_id'] = 'OTHER'
                elif variant == 'contract-as-application':
                    comparison['right'] = [deepcopy(comparison['left'])]
                elif variant == 'settlement-as-contract':
                    comparison['left']['unit_id'] = 'settlement'
                else:
                    next(u for u in case['units'] if u['unit_id'] == 'application-0')['source_file'] = '勤策核销明细'
                with self.assertRaises(AuditError):
                    normalize_application_total_check(case['scenario'], check, case['documents'], case['materials'], case['units'])

    def test_wrong_application_cross_verification_missing_rows_and_conflicting_duplicates_disable_proof(self):
        for variant in ('wrong-application', 'wrong-verification', 'missing-amount', 'missing-link', 'conflicting-duplicate', 'conflicting-field'):
            with self.subTest(variant=variant):
                case = case_for_amounts()
                check = check_for(case)
                application = next(d for d in case['documents'] if d['unit_id'] == 'application-0')
                if variant == 'wrong-application':
                    application['facts'][1] = 'applyCode="SQ_OTHER"'
                elif variant == 'wrong-verification':
                    application['facts'][0] = 'verify_code="HX_OTHER"'
                elif variant == 'missing-amount':
                    application['facts'] = [f for f in application['facts'] if not f.startswith('applyAmount=')]
                elif variant == 'missing-link':
                    case['documents'] = [d for d in case['documents'] if d['unit_id'] != 'verification-0']
                elif variant == 'conflicting-field':
                    application['facts'].append('applyAmount="601"')
                else:
                    duplicate = deepcopy(application)
                    duplicate['unit_id'] = 'conflicting'
                    duplicate['facts'][-1] = 'applyAmount="601"'
                    case['documents'].append(duplicate)
                    case['units'].append({'unit_id': 'conflicting', 'source_kind': 'qince', 'source_file': '勤策申请明细'})
                self.assertIsNone(linked_application_basis(case['documents'], case['units']))
                self.assertTrue(list(jsonschema.Draft202012Validator(schema_for(case, check['rule_id'])).iter_errors(check)))
                with self.assertRaises(AuditError):
                    normalize_application_total_check(case['scenario'], check, case['documents'], case['materials'], case['units'])

    def test_duplicate_identical_rows_deduplicate_but_operands_must_not_double_count(self):
        case = case_for_amounts()
        check = check_for(case, operation='sum')
        duplicate = deepcopy(next(d for d in case['documents'] if d['unit_id'] == 'application-0'))
        duplicate['unit_id'] = 'same-row-again'
        case['documents'].append(duplicate)
        case['units'].append({'unit_id': duplicate['unit_id'], 'source_kind': 'qince', 'source_file': '勤策申请明细'})
        self.assertEqual(linked_application_basis(case['documents'], case['units'])['row_count'], 1)
        self.assertEqual(decide(case, check)['status'], 'pass')
        check['comparisons'][0]['right'] *= 2
        with self.assertRaises(AuditError):
            decide(case, check)

    def test_nonliteral_quote_is_rejected_even_when_program_can_derive_an_amount(self):
        for quoted_budget in (True, False):
            case = case_for_amounts()
            if not quoted_budget:
                next(d for d in case['documents'] if d['unit_id'] == 'contract')['facts'] = ['活动预算金额：600元']
            check = check_for(case, 'application_claim_ceiling')
            check['comparisons'][0]['left']['quote'] = '合计为 600 元'
            original = deepcopy(check)
            with self.assertRaises(AuditError):
                decide(case, check)
            self.assertEqual(check, original)
        case = case_for_amounts()
        next(d for d in case['documents'] if d['unit_id'] == 'contract')['facts'] = ['活动预算金额：600元']
        check = check_for(case)
        check['comparisons'][0]['left']['quote'] = '预算600元'
        with self.assertRaises(AuditError):
            decide(case, check)

    def test_original_amount_and_operator_cannot_be_replaced_to_hide_bad_evidence(self):
        for variant in ('bad-number', 'bad-right-quote', 'wrong-operator', 'wrong-kind'):
            with self.subTest(variant=variant):
                case = case_for_amounts()
                check = check_for(case)
                c = check['comparisons'][0]
                if variant == 'bad-number': c['left']['number'] = '601'
                elif variant == 'bad-right-quote': c['right'][0]['quote'] = 'applyAmount=600'
                elif variant == 'wrong-operator': c['operator'] = 'le'
                else: c['value_kind'] = 'quantity'
                with self.assertRaises(AuditError): decide(case, check)

    def test_independent_application_header_stays_a_single_value(self):
        case = case_for_amounts()
        case['units'].append({'unit_id': 'header', 'source_kind': 'qince', 'source_file': '勤策申请主档'})
        case['documents'].append({'unit_id': 'header', 'facts': ['applyAmount="600.00"'], 'roles': [], 'limitations': []})
        check = check_for(case)
        check['source_ids'].append('header')
        check['comparisons'][0]['right'] = [{'unit_id': 'header', 'quote': 'applyAmount="600.00"', 'number': '600.00'}]
        jsonschema.Draft202012Validator(schema_for(case, check['rule_id'])).validate(check)
        result = decide(case, check)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['comparisons'][0]['operation'], 'value')
        self.assertNotIn('normalized_application_totals', result)
        check['comparisons'][0]['operation'] = 'sum'
        self.assertTrue(list(jsonschema.Draft202012Validator(schema_for(case, check['rule_id'])).iter_errors(check)))
        with self.assertRaises(AuditError): decide(case, check)

    def test_source_schema_and_guard_share_complete_scope_across_registered_scenarios(self):
        for scenario, rule_id in sorted(APPLICATION_COMPARISON_RULES):
            if rule_id not in {'contract_budget_amount', 'application_claim_ceiling'}: continue
            with self.subTest(scenario=scenario, rule=rule_id):
                case = case_for_amounts()
                case['scenario'] = scenario
                check = check_for(case, rule_id)
                if scenario == 'promotional_display': check['source_assertions'] = []
                self.assertIsNotNone(complete_application_sum(check['comparisons'][0], case['documents'], case['units']))
                jsonschema.Draft202012Validator(schema_for(case, rule_id)).validate(check)
                validate_workbook_comparison(scenario, rule_id, check['comparisons'][0], case['documents'], case['materials'], case['units'])

    def test_actual_prompt_and_policy_use_the_same_generic_source_contract(self):
        case = case_for_amounts()
        info = reference_fields(case['documents'], case['units'])['application_total']
        self.assertEqual(info['values'][0]['source_fields']['linked_application_rows']['row_count'], 1)
        self.assertIn(APPLICATION_TOTAL_CLARIFICATION, reference_prompt({'application_total': info}))
        self.assertIn(APPLICATION_TOTAL_CLARIFICATION, AMOUNT_CLARIFICATION)


if __name__ == '__main__':
    unittest.main()
