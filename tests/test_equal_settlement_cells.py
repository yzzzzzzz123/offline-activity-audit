"""Equal numbers in independently headed cells are not the same source cell."""
from copy import deepcopy
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_decision
from audit_core.fee_policy_checks import separate_settlement_product_cells
from tests.pdf_test_support import flags_for, unknown_checks


def fixture(quantity='24000', price='1', amount='24000'):
    header = '表格行3表头：补差单品｜销售数量｜补差单价｜补差金额'
    row = f'表格行4：原始商品180g｜{quantity}｜{price}｜{amount}'
    operand = lambda number: {'unit_id': 'settlement', 'quote': row, 'number': number}
    comparison = {'label': '结算行金额与数量乘单价', 'value_kind': 'amount',
        'left': operand(amount), 'right': [operand(quantity), operand(price)], 'operation': 'product', 'operator': 'eq'}
    docs = [{'unit_id': 'settlement', 'facts': [header, row]}]
    materials = [{'id': 'settlement', 'state': 'present', 'source_ids': ['settlement'], 'reason': '原始结算单'}]
    flags = flags_for('price_difference_support')
    evidence = unknown_checks('price_difference_support', flags)
    check = next(c for c in evidence['checks'] if c['rule_id'] == 'settlement_calculation')
    check.update(status='pass', reason='原始结算行复算', source_ids=['settlement'], comparisons=[comparison])
    return flags, evidence, docs, materials, comparison


def decide(data):
    flags, evidence, docs, materials, _ = data
    result = audit_decision('price_difference_support', flags, evidence, docs, materials)
    return next(c for c in result['checks'] if c['rule_id'] == 'settlement_calculation')


class EqualSettlementCellTests(unittest.TestCase):
    def test_original_quantity_times_one_equals_same_numeric_amount(self):
        data = fixture()
        before = deepcopy(data)
        result = decide(data)
        self.assertEqual(result['status'], 'pass')
        self.assertEqual(result['calculations'][0]['right_value'], '24000')
        self.assertEqual(data, before)

    def test_duplicate_product_values_occupy_separate_original_columns(self):
        for quantity, price, amount in [('1', '1', '1'), ('2', '2', '4')]:
            with self.subTest(quantity=quantity):
                self.assertEqual(decide(fixture(quantity, price, amount))['status'], 'pass')

    def test_real_amount_overage_is_still_failure(self):
        result = decide(fixture('24000', '1', '24001'))
        self.assertEqual(result['status'], 'fail')
        self.assertFalse(result['calculations'][0]['matches'])

    def test_no_header_wrong_nearest_header_or_partial_row_cannot_establish_separate_cells(self):
        for mode in ('missing', 'reversed', 'partial'):
            data = fixture()
            if mode == 'missing': data[2][0]['facts'].pop(0)
            if mode == 'reversed': data[2][0]['facts'].insert(1, '新表头：商品｜金额｜单价｜数量')
            if mode == 'partial':
                for operand in [data[4]['left'], *data[4]['right']]:
                    operand['quote'] = operand['quote'].split('：', 1)[1]
            with self.subTest(mode=mode), self.assertRaises(AuditError):
                decide(data)

    def test_another_material_cannot_certify_settlement_arithmetic(self):
        data = fixture()
        data[3][0]['id'] = 'promotion_contract'
        with self.assertRaises(AuditError): decide(data)

    def test_value_or_sum_self_reference_and_unrelated_rules_remain_rejected(self):
        for operation in ('value', 'sum'):
            data = fixture()
            data[4].update(operation=operation, right=[deepcopy(data[4]['left'])])
            with self.subTest(operation=operation), self.assertRaises(AuditError): decide(data)
        data = fixture()
        self.assertFalse(separate_settlement_product_cells('unrelated', data[4], data[2]))

    def test_changed_source_number_is_not_accepted(self):
        data = fixture()
        data[4]['right'][0]['number'] = '24001'
        with self.assertRaises(AuditError): decide(data)


if __name__ == '__main__':
    unittest.main()
