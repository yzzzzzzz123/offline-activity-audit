"""Generic settlement roles and vertical-layout proofs; no model or business API."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import jsonschema

from audit_core.audit_rag import part_schema
from audit_core.audit_self_check import stage_self_check
from audit_core.common import AuditError, CheckEvidenceError
from audit_core.comparison_policy import single_settlement_expense
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.pdf_evidence import AUDIT_SCHEMA, audit_decision, audit_schema
from audit_core.pdf_policy import MATERIALS, rules
from tests.pdf_test_support import flags_for
from tests.test_shared_failure_repairs import comparison, operand, settlement_fixture, decide


def vertical_fixture(total='2635.50', claim=None, quantity='3', days='7', price='125.50'):
    facts = [f'人数：{quantity}（人）。', f'天数：{days} 天。', f'单日工资：{price}（元）/天。',
             f'合计：{total} 元。']
    if claim is not None:
        facts.append(f'实际核销金额：{claim} 元。')
    docs = [{'unit_id': 'expense-original', 'roles': ['结算单'], 'facts': facts, 'limitations': []}]
    materials = [{'id': 'settlement', 'state': 'present', 'source_ids': ['expense-original'], 'reason': '原件'}]
    op = lambda fact, n: operand(fact, n, 'expense-original')
    c = comparison(op(facts[3], total), [op(facts[i], n) for i, n in enumerate((quantity, days, price))],
                   'product', 'eq')
    evidence = {'checks': [{'rule_id': 'settlement_calculation', 'status': 'pass', 'reason': '原字段复算',
        'source_ids': ['expense-original'], 'comparisons': [c]}]}
    return docs, materials, evidence


def schema_for(fixture):
    docs, materials, _ = fixture
    return part_schema(audit_schema('personnel_incentive', materials=materials, units=[],
        documents=docs, include_photo_reviews=False), ['settlement_calculation'], {})


def schema_value(fixture):
    return {**deepcopy(fixture[2]), 'reference_reads': []}


class SettlementComparisonScopeTests(unittest.TestCase):
    def test_vertical_original_fields_pass_in_every_applicable_scenario_without_mutation(self):
        scenarios = [s for s in MATERIALS if 'settlement_calculation' in {r.id for r in rules(s)}]
        self.assertEqual(len(scenarios), 5)
        for scenario in scenarios:
            data = vertical_fixture()
            before = deepcopy(data)
            with self.subTest(scenario=scenario):
                result = decide(data, scenario)
                self.assertEqual(result['status'], 'pass')
                self.assertEqual(result['comparisons'], data[2]['checks'][0]['comparisons'])
                self.assertEqual(result['calculations'][0]['right_value'], '2635.50')
                self.assertEqual(data, before)

    def test_role_and_arithmetic_are_independent_of_document_ids_and_numeric_values(self):
        for quantity, days, price in [('4', '9', '81.125'), ('0', '3', '9.33'), ('1', '1', '1')]:
            total = str(Decimal(quantity) * Decimal(days) * Decimal(price))
            data = vertical_fixture(total=total, quantity=quantity, days=days, price=price)
            self.assertEqual(decide(data)['status'], 'pass')

    def test_quantity_unit_price_vertical_expense_uses_the_same_generic_proof(self):
        data = vertical_fixture(total='39.90')
        data[0][0]['facts'] = ['采购数量：3 件。', '采购单价：13.30 元/件。', '合计：39.90 元。']
        c = data[2]['checks'][0]['comparisons'][0]
        c['left'] = operand(data[0][0]['facts'][2], '39.90', 'expense-original')
        c['right'] = [operand(f, n, 'expense-original') for f, n in zip(data[0][0]['facts'][:2], ['3', '13.30'])]
        self.assertEqual(decide(data)['status'], 'pass')

    def test_complete_prose_restatement_keeps_original_roles_and_units(self):
        data = vertical_fixture()
        summary = '正文：聘请促销人员 3 名，工资为 125.50 元/天，共聘请 7 天，明细如下。'
        data[0][0]['facts'].insert(0, summary)
        self.assertEqual(decide(data)['status'], 'pass')
        for changed in (summary.replace('125.50', '130'), summary.replace('元/天', '元/月'),
                        summary.replace('共聘请 7 天', '共聘请 8 天'), '另聘请人员3名，工资为125.50元/天，聘请7天。',
                        '其他费用为70元。'):
            data[0][0]['facts'][0] = changed
            self.assertIsNone(single_settlement_expense(data[0]))
            with self.assertRaises(AuditError): decide(data)

    def test_valid_vertical_expense_advances_existing_stage_without_model_rework(self):
        for claim in (None, '2500'):
            docs, materials, value = vertical_fixture(claim=claim)
            before = deepcopy(value)
            case = {'scenario': 'personnel_incentive', 'flags': flags_for('personnel_incentive'),
                    'documents': docs, 'materials': materials, 'units': []}
            selected = ['settlement_calculation']
            decision = audit_decision(case['scenario'], case['flags'], value, docs, materials,
                                      units=[], selected_rule_ids=selected)
            report = stage_self_check(value, decision, case, selected, stage='settlement-scope')
            self.assertTrue(report['can_advance'])
            self.assertEqual(report['rework'], [])
            self.assertEqual(report['checker_model_calls'], 0)
            self.assertEqual(value, before)

    def test_actual_under_and_over_statements_and_sub_cent_difference_remain_failures(self):
        for total in ('2634.50', '2636.50', '2635.5001'):
            with self.subTest(total=total):
                result = decide(vertical_fixture(total=total))
                self.assertEqual(result['status'], 'fail')
                self.assertFalse(result['calculations'][0]['matches'])

    def test_one_original_amount_has_one_generation_comparison(self):
        data = vertical_fixture()
        schema = schema_for(data)
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(schema_value(data), schema)
        duplicate = schema_value(data)
        duplicate['checks'][0]['comparisons'] *= 2
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(duplicate, schema)

    def test_generation_cannot_turn_total_into_claim_or_use_contract(self):
        for mode in ('le', 'ge', 'contract', 'sum', 'quantity'):
            data = vertical_fixture()
            value = schema_value(data)
            c = value['checks'][0]['comparisons'][0]
            if mode in ('le', 'ge'): c.update(operator=mode, label='实际核销金额')
            elif mode == 'contract': c['right'][0]['unit_id'] = 'contract-original'
            elif mode == 'sum': c['operation'] = 'sum'
            else: c['value_kind'] = 'quantity'
            with self.subTest(mode=mode), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(value, schema_for(data))

    def test_contract_source_is_reported_before_operation_at_the_actual_index(self):
        data = vertical_fixture()
        data[0].append({'unit_id': 'contract-original', 'facts': ['合同预算：2635.50 元。'], 'limitations': []})
        data[1].append({'id': 'promotion_contract', 'state': 'present', 'source_ids': ['contract-original'], 'reason': '原件'})
        data[2]['checks'][0]['source_ids'].append('contract-original')
        extra = comparison(operand('合同预算：2635.50 元。', '2635.50', 'contract-original'),
            [deepcopy(data[2]['checks'][0]['comparisons'][0]['left'])], 'value', 'ge')
        data[2]['checks'][0]['comparisons'].append(extra)
        before = deepcopy(data)
        with self.assertRaises(CheckEvidenceError) as caught:
            decide(data)
        self.assertEqual(caught.exception.validation_code, 'comparison_source')
        self.assertEqual(caught.exception.field_path, 'comparisons/1/left/unit_id')
        self.assertIn('结算', str(caught.exception))
        self.assertEqual(data, before)

    def test_invalid_extra_is_never_silently_removed_or_reported_as_business_failure(self):
        data = vertical_fixture()
        extra = deepcopy(data[2]['checks'][0]['comparisons'][0])
        extra.update(label='实际核销金额', operator='le')
        data[2]['checks'][0]['comparisons'].append(extra)
        before = deepcopy(data)
        with self.assertRaises(CheckEvidenceError) as caught:
            decide(data)
        self.assertEqual(caught.exception.field_path, 'comparisons/1/operator')
        self.assertEqual(data, before)

    def test_real_independent_claim_is_added_and_cannot_escape_ceiling(self):
        for claim, status in [('2600', 'pass'), ('2635.50', 'pass'), ('2635.5001', 'fail'), ('2800', 'fail')]:
            data = vertical_fixture(claim=claim)
            before = deepcopy(data)
            with self.subTest(claim=claim):
                result = decide(data)
                self.assertEqual(result['status'], status)
                self.assertEqual([c['operator'] for c in result['comparisons']], ['eq', 'le'])
                self.assertEqual(result['comparisons'][0], data[2]['checks'][0]['comparisons'][0])
                self.assertEqual(result['comparisons'][1]['left']['number'], claim)
                self.assertIn('settlement_basis_completion', result)
                self.assertEqual(data, before)

    def test_claim_only_does_not_hide_wrong_original_total(self):
        data = vertical_fixture(total='2600', claim='2500')
        c = data[2]['checks'][0]['comparisons'][0]
        c['left'] = operand(data[0][0]['facts'][-1], '2500', 'expense-original')
        c['operator'] = 'le'
        result = decide(data)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual([c['operator'] for c in result['comparisons']], ['le', 'eq'])

    def test_independent_claim_schema_keeps_legitimate_forward_and_reverse_comparisons(self):
        for reverse in (False, True):
            data = vertical_fixture(claim='2500')
            c = comparison(operand(data[0][0]['facts'][-1], '2500', 'expense-original'),
                           [deepcopy(data[2]['checks'][0]['comparisons'][0]['left'])], 'value', 'le')
            if reverse:
                c['left'], c['right'], c['operator'] = c['right'][0], [c['left']], 'ge'
            data[2]['checks'][0]['comparisons'].append(c)
            jsonschema.validate(schema_value(data), schema_for(data))
            result = decide(data)
            self.assertEqual(result['status'], 'pass')
            self.assertEqual(len(result['comparisons']), 2)
            self.assertEqual(result['comparisons'][-1], c)

    def test_ambiguous_partial_multi_page_or_multi_row_does_not_gain_vertical_proof(self):
        for mode in ('duplicate', 'second_amount', 'second_fee', 'partial', 'rows', 'other_page', 'other_factors'):
            data = vertical_fixture()
            if mode == 'duplicate': data[0][0]['facts'].append('人数：2（人）。')
            elif mode == 'second_amount': data[0][0]['facts'].append('培训金额：70 元。')
            elif mode == 'second_fee': data[0][0]['facts'].append('其他费用：70 元。')
            elif mode == 'partial': data[0][0]['limitations'] = ['表格局部无法读取。']
            elif mode == 'rows': data[0][0]['facts'].append('第2行：另一笔费用。')
            elif mode == 'other_factors': data[0][0]['facts'] += ['采购数量：2 件。', '采购单价：5 元/件。']
            else:
                data[0].append({'unit_id': 'expense-other', 'facts': ['另一笔费用。'], 'limitations': []})
                data[1][0]['source_ids'].append('expense-other')
            with self.subTest(mode=mode):
                self.assertIsNone(single_settlement_expense(data[0]))
                with self.assertRaises(AuditError): decide(data)

    def test_duplicate_missing_or_forged_factors_and_nonliteral_quote_stay_rejected(self):
        for mode in ('duplicate', 'missing', 'forged', 'quote'):
            data = vertical_fixture()
            c = data[2]['checks'][0]['comparisons'][0]
            if mode == 'duplicate': c['right'][1] = deepcopy(c['right'][0])
            elif mode == 'missing': c['right'].pop()
            elif mode == 'forged': c['right'][0]['number'] = '99'
            else: c['right'][0]['quote'] = '人数为3人'
            with self.subTest(mode=mode), self.assertRaises(AuditError): decide(data)

    def test_multi_sku_scope_and_full_coverage_are_preserved(self):
        data = settlement_fixture()
        jsonschema.validate(schema_value(data), schema_for(data))
        self.assertEqual(len(decide(data)['comparisons']), 4)
        data[2]['checks'][0]['comparisons'][-2]['right'].pop()
        with self.assertRaises(AuditError): decide(data)

    def test_multi_sku_independent_claim_cannot_be_omitted_by_the_model(self):
        for claim, status in [(13, 'pass'), (15, 'fail')]:
            data = settlement_fixture(claim=claim)
            data[2]['checks'][0]['comparisons'].pop()
            result = decide(data)
            self.assertEqual(result['status'], status)
            self.assertEqual(result['comparisons'][-1]['operation'], 'sum')
            self.assertEqual(len(result['comparisons'][-1]['right']), 2)
            self.assertEqual(result['comparisons'][-1]['operator'], 'le')

    def test_unreadable_or_repeated_rows_cannot_support_program_claim_completion(self):
        for mode in ('unreadable', 'duplicate', 'partial'):
            data = settlement_fixture()
            data[2]['checks'][0]['comparisons'].pop()
            if mode == 'unreadable': data[0][0]['facts'].append('第3行：数量1；奖励金额无法辨认。')
            elif mode == 'duplicate': data[0][0]['facts'].append('第1行：奖励金额6。')
            else: data[0][0]['limitations'] = ['表格缺行。']
            with self.subTest(mode=mode), self.assertRaises(CheckEvidenceError) as caught:
                decide(data)
            self.assertEqual(caught.exception.validation_code, 'comparison_scope')
            self.assertEqual(caught.exception.field_path, 'comparisons')

    def test_incomplete_source_may_remain_unknown_without_inventing_comparisons(self):
        data = vertical_fixture(claim='2500')
        data[0][0]['limitations'] = ['表格局部无法读取。']
        data[2]['checks'][0].update(status='unknown', reason='计费数据尚不能完整确认', comparisons=[])
        before = deepcopy(data)
        result = decide(data)
        self.assertEqual(result['status'], 'unknown')
        self.assertEqual(result['comparisons'], [])
        self.assertEqual(data, before)

    def test_parser_reports_source_and_scope_without_targeted_retry_context(self):
        for mode, path, code in [('source', 'checks/0/comparisons/1/right/0/unit_id', 'comparison_source'),
                                 ('scope', 'checks/0/comparisons/0/right', 'comparison_scope'),
                                 ('coverage', 'checks/0/comparisons', 'comparison_scope')]:
            data = vertical_fixture()
            if mode == 'source':
                extra = deepcopy(data[2]['checks'][0]['comparisons'][0])
                extra['right'][0]['unit_id'] = 'contract-original'
                data[2]['checks'][0]['comparisons'].append(extra)
            elif mode == 'scope': data[2]['checks'][0]['comparisons'][0]['right'].pop()
            else:
                data = settlement_fixture()
                data[2]['checks'][0]['comparisons'].pop()
                data[0][0]['limitations'] = ['表格缺行。']
            with TemporaryDirectory() as temporary:
                schema = Path(temporary) / 'schema.json'
                schema.write_text(json.dumps(AUDIT_SCHEMA), encoding='utf-8')
                parser = EvidenceOutputParser(schema_path=schema,
                    post_validate=lambda value: decide((data[0], data[1], value)))
                with self.assertRaises(AuditError): parser.parse(json.dumps(data[2]))
                self.assertEqual(parser.last_diagnostics['validation_paths'], [path])
                self.assertEqual(parser.last_diagnostics['validation_code'], code)
                self.assertEqual(parser.last_retry_context, '')


if __name__ == '__main__':
    unittest.main()
