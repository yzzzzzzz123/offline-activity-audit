"""Generation and repeated aggregates must agree with strict source validation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from audit_core.common import AuditError
from audit_core.fee_policy_checks import validate_workbook_comparison
from audit_core.pdf_evidence import COMPARISON, audit_decision, audit_schema, calculate, validate
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_fee_workbook_policy import comparison, doc, mat, operand
from tests.test_optional_staff_and_display_photos import photo_case, review


class SourceConstraintCompletionTests(unittest.TestCase):
    def test_direct_unit_prices_from_multiple_rows_must_be_separate_comparisons(self):
        documents = {'claim': doc('claim', '结算单价4.5'),
                     'row1': doc('row1', '申请单价4.5'),
                     'row2': doc('row2', '申请单价4.5')}
        combined = comparison(operand('claim', '结算单价4.5', '4.5'),
            [operand('row1', '申请单价4.5', '4.5'), operand('row2', '申请单价4.5', '4.5')],
            kind='unit_price')
        before = deepcopy(combined)
        with self.assertRaises(AuditError):
            validate(combined, COMPARISON)
        with self.assertRaisesRegex(AuditError, '直接比较只能引用一个对应值'):
            calculate(combined, documents)
        for right in combined['right']:
            separate = {**combined, 'right': [right]}
            validate(separate, COMPARISON)
            self.assertTrue(calculate(separate, documents)['matches'])
        self.assertEqual(combined, before)

    def test_direct_value_constraint_preserves_real_sum_and_product_calculations(self):
        documents = {'claim': doc('claim', '结算金额9'),
                     'a': doc('a', '第一笔4.5'), 'b': doc('b', '第二笔4.5'),
                     'quantity': doc('quantity', '数量2')}
        for operation, right in (
            ('sum', [operand('a', '第一笔4.5', '4.5'), operand('b', '第二笔4.5', '4.5')]),
            ('product', [operand('a', '第一笔4.5', '4.5'), operand('quantity', '数量2', '2')]),
        ):
            value = comparison(operand('claim', '结算金额9', '9'), right, operation=operation)
            with self.subTest(operation=operation):
                validate(value, COMPARISON)
                self.assertTrue(calculate(value, documents)['matches'])

    def budget_case(self, header=False):
        materials = [mat('promotion_contract', 'contract'), mat('settlement', 'settlement')]
        units = [{'unit_id': 'row', 'source_kind': 'qince', 'source_file': '勤策申请明细'}]
        if header:
            units.append({'unit_id': 'header', 'source_kind': 'qince', 'source_file': '勤策申请主档'})
        schema = audit_schema('promotional_display', materials=materials, units=units,
                              include_photo_reviews=False)
        evidence = unknown_checks('promotional_display', flags_for('promotional_display'))
        evidence.pop('photo_reviews')
        return schema, evidence

    def test_application_line_without_total_is_generated_as_unknown_not_a_fabricated_sum(self):
        schema, evidence = self.budget_case()
        validate(evidence, schema)
        for rule_id, left in [('contract_budget_amount', 'contract'),
                              ('application_claim_ceiling', 'settlement')]:
            for operation in ('value', 'sum'):
                value = deepcopy(evidence)
                check = next(c for c in value['checks'] if c['rule_id'] == rule_id)
                check.update(status='pass', source_ids=[left, 'row'], comparisons=[
                    comparison(operand(left, '金额4800', 4800),
                               [operand('row', 'applyAmount="600.00"', '600.00')], operation=operation)])
                # Static generation may express a sum; completeness and field
                # roles require actual documents, checked by the runtime kernel.
                validate(value, schema)
                with self.subTest(rule=rule_id, operation=operation), self.assertRaises(AuditError):
                    validate_workbook_comparison('promotional_display',rule_id,check['comparisons'][0],[],[],
                        [{'unit_id':'row','source_kind':'qince','source_file':'勤策申请明细'}])
                check.update(status='unknown', comparisons=[])
                validate(value, schema)

    def test_original_header_total_is_available_once_and_application_lines_stay_excluded(self):
        schema, evidence = self.budget_case(header=True)
        for rule_id, left, operator in [('contract_budget_amount', 'contract', 'eq'),
                                       ('application_claim_ceiling', 'settlement', 'le')]:
            value = deepcopy(evidence)
            check = next(c for c in value['checks'] if c['rule_id'] == rule_id)
            check.update(status='pass', source_ids=[left, 'header'], comparisons=[
                comparison(operand(left, '金额4800', 4800),
                           [operand('header', '申请总金额4800', 4800)], operator=operator)])
            validate(value, schema)
            for invalid in ('row', 'sum', 'repeated_header'):
                bad = deepcopy(value)
                c = next(c for c in bad['checks'] if c['rule_id'] == rule_id)['comparisons'][0]
                if invalid == 'row':
                    c['right'][0]['unit_id'] = 'row'
                elif invalid == 'sum':
                    c['operation'] = 'sum'
                else:
                    c['right'] *= 2
                with self.subTest(rule=rule_id, invalid=invalid), self.assertRaises(AuditError):
                    if invalid=='repeated_header':validate(bad, schema)
                    else:
                        validate_workbook_comparison('promotional_display',rule_id,c,
                            [doc('contract','金额4800'),doc('settlement','金额4800'),doc('row','applyAmount="600.00"'),doc('header','申请总金额4800')],
                            [mat('promotion_contract','contract'),mat('settlement','settlement')],
                            [{'unit_id':'row','source_kind':'qince','source_file':'勤策申请明细'},
                             {'unit_id':'header','source_kind':'qince','source_file':'勤策申请主档'}])

    def test_validated_photo_sources_supply_the_published_aggregate_without_rewriting_raw_ai(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = photo_case(Path(tmp), 2)
            evidence = unknown_checks('promotional_display', case['flags'])
            evidence['photo_reviews'] = [review('p01'), review('p02')]
            aggregate = next(c for c in evidence['checks'] if c['rule_id'] == 'display_brand')
            aggregate.update(status='pass', source_ids=['p01', 'p02'], reason='各图可见货品为参半。')
            raw = deepcopy(evidence)
            result = audit_decision('promotional_display', case['flags'], evidence,
                                    case['documents'], case['materials'], units=case['units'])
            self.assertEqual(evidence, raw)
            check = next(c for c in result['checks'] if c['rule_id'] == 'display_brand')
            self.assertEqual(check['status'], 'pass')
            self.assertIn('contract', check['source_ids'])
            evidence['photo_reviews'][1]['checks'][1].update(status='fail', reason='本图可见其他品牌实物混摆。')
            result = audit_decision('promotional_display', case['flags'], evidence,
                                    case['documents'], case['materials'], units=case['units'])
            self.assertEqual(next(c for c in result['checks'] if c['rule_id'] == 'display_brand')['status'], 'fail')

    def test_primary_photo_contract_sources_and_unknown_ids_remain_strict(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = photo_case(Path(tmp), 1)
            for invalid in ('missing_photo_contract', 'unissued_aggregate_source'):
                evidence = unknown_checks('promotional_display', case['flags'])
                evidence['photo_reviews'] = [review('p01')]
                if invalid == 'missing_photo_contract':
                    evidence['photo_reviews'][0]['checks'][1]['source_ids'] = ['p01']
                else:
                    aggregate = next(c for c in evidence['checks'] if c['rule_id'] == 'display_brand')
                    aggregate.update(status='pass', source_ids=['other-invoice'], reason='参半货品。')
                with self.subTest(invalid=invalid), self.assertRaises(AuditError):
                    audit_decision('promotional_display', case['flags'], evidence,
                                   case['documents'], case['materials'], units=case['units'])

    def test_no_per_photo_evidence_does_not_bypass_original_contract_guard(self):
        flags = flags_for('promotional_display')
        evidence = unknown_checks('promotional_display', flags)
        check = next(c for c in evidence['checks'] if c['rule_id'] == 'display_brand')
        check.update(status='pass', source_ids=['photo'], reason='参半货品。')
        with self.assertRaisesRegex(AuditError, '须引用品牌方本次合同'):
            audit_decision('promotional_display', flags, evidence,
                           [doc('contract', '本次合同'), doc('photo', '照片货品')],
                           [mat('promotion_contract', 'contract')])


if __name__ == '__main__':
    unittest.main()
