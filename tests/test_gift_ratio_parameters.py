"""Equal buy/gift parameters are two positions in one proven ratio, not a sum."""
from copy import deepcopy
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_decision
from tests.test_brand_contract_source import gift_fixture, decide_gift, operand
from tests.pdf_test_support import flags_for, unknown_checks


def equal_ratio_fixture(operation='ratio', text='1:1'):
    fixture = gift_fixture(operation)
    fixture[1][1]['facts'].append('本次购买与赠送比例 ' + text)
    comparison = fixture[4]['comparisons'][0]
    comparison['right'][1:] = [operand('c', text, '1'), operand('c', text, '1')]
    return fixture


class GiftRatioParameterTests(unittest.TestCase):
    def test_original_equal_ratio_parameters_work_in_both_gift_scenarios(self):
        for operation in ('ratio', 'floor_ratio'):
            for text in ('1:1', '1 ： 1', '1.0:1.00'):
                with self.subTest(operation=operation, text=text):
                    fixture = equal_ratio_fixture(operation, text)
                    result = decide_gift(fixture)
                    self.assertEqual(result['status'], 'pass')
                    self.assertEqual(result['calculations'][0]['right_value'], '100')
                    _, docs, materials, _, check = fixture
                    flags = flags_for('giveaway_promotion')
                    evidence = unknown_checks('giveaway_promotion', flags)
                    target = next(c for c in evidence['checks'] if c['rule_id'] == 'gift_rule_quantity')
                    target.update(deepcopy(check))
                    result = audit_decision('giveaway_promotion', flags, evidence, docs, materials)
                    self.assertEqual(next(c for c in result['checks'] if c['rule_id'] == 'gift_rule_quantity')['status'], 'pass')

    def test_duplicate_sum_product_and_non_gift_parameters_still_reject(self):
        for operation in ('sum', 'product'):
            fixture = equal_ratio_fixture(operation)
            with self.assertRaises(AuditError):
                decide_gift(fixture)
        from audit_core.fee_policy_checks import validate_workbook_comparison
        fixture = equal_ratio_fixture()
        with self.assertRaises(AuditError):
            validate_workbook_comparison('giveaway_promotion', 'other_rule',
                                         fixture[4]['comparisons'][0], fixture[1], fixture[2])

    def test_one_number_dates_unequal_ratio_and_unrelated_numbers_still_reject(self):
        for text in ('1', '2026/1/1', '1:2', '1元，1支', '11:11'):
            with self.subTest(text=text):
                with self.assertRaises(AuditError):
                    decide_gift(equal_ratio_fixture(text=text))

    def test_wrong_source_and_invented_quote_still_reject(self):
        fixture = equal_ratio_fixture()
        fixture[1][1]['facts'].remove('本次购买与赠送比例 1:1')
        with self.assertRaises(AuditError):
            decide_gift(fixture)
        fixture = equal_ratio_fixture()
        fixture[1][0]['facts'].append('1:1')
        for term in fixture[4]['comparisons'][0]['right'][1:]:
            term['unit_id'] = 's'
        with self.assertRaises(AuditError):
            decide_gift(fixture)

    def test_non_equal_ratio_cannot_copy_only_one_parameter(self):
        fixture = gift_fixture()
        fixture[1][1]['facts'].append('买2赠1，比例2:1')
        fixture[4]['comparisons'][0]['right'][1:] = [operand('c', '2:1', '2')]*2
        with self.assertRaises(AuditError):
            decide_gift(fixture)


if __name__ == '__main__':
    unittest.main()
