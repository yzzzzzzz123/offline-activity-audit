"""Quoted table fields remain separate without relaxing source or decimal checks."""
from copy import deepcopy
from decimal import Decimal
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import _operand, _payment_operand, _quote_number_tokens, calculate


class NumericQuoteTokensTests(unittest.TestCase):
    def test_actual_reward_row_does_not_join_three_comma_separated_fields(self):
        quote = '第1行：小箭头 ABOUT FOCUS 修护柔顺洗发水隐匿玫瑰(500ml)-线下KA，155，5，775'
        documents = {'source': {'facts': [quote]}}
        before = deepcopy(documents)
        for number in ('155', '5', '775'):
            self.assertEqual(_operand({'unit_id': 'source', 'quote': quote, 'number': number}, documents), Decimal(number))
        for invented in ('1555775', '1555', '5005'):
            with self.assertRaises(AuditError):
                _operand({'unit_id': 'source', 'quote': quote, 'number': invented}, documents)
        self.assertEqual(documents, before)

    def test_valid_thousands_groups_keep_sign_precision_and_percentage(self):
        for quoted, number in (('奖励金额1,234.50元', '1234.50'), ('奖励金额1，234，567.89元', '1234567.89'),
                               ('付款-1,234.50元，', '-1234.50'), ('比例12,345.67％', '123.4567'),
                               ('金额1,000，', '1000')):
            with self.subTest(quoted=quoted):
                self.assertEqual(_operand({'unit_id': 's', 'quote': quoted, 'number': number},
                                           {'s': {'facts': [quoted]}}), Decimal(number))
        self.assertNotIn('5', _quote_number_tokens('数量1,005'))

    def test_invalid_grouping_is_not_repaired_into_an_invented_amount(self):
        for quote in ('数量155,5,775', '数量155，5，775', '金额12,34.50'):
            for number in ('1555775', '1234.50'):
                with self.subTest(quote=quote, number=number), self.assertRaises(AuditError):
                    _operand({'unit_id': 's', 'quote': quote, 'number': number}, {'s': {'facts': [quote]}})
        self.assertEqual(_quote_number_tokens('比例5，10，25%'), ['5', '10', '25%'])

    def test_strict_continuous_quote_same_source_and_decimal_comparison_remain(self):
        quote = '数量155，单价5，金额775'
        sources = {'s': {'facts': [quote]}, 'other': {'facts': ['金额999']}}
        for unit, text, number in (('s', quote.replace('，', ','), '5'), ('other', quote, '5'), ('s', quote, '5.01')):
            with self.subTest(unit=unit, text=text, number=number), self.assertRaises(AuditError):
                _operand({'unit_id': unit, 'quote': text, 'number': number}, sources)
        result = calculate({'label': '精确原值比较', 'value_kind': 'unit_price', 'operation': 'value', 'operator': 'eq',
            'left': {'unit_id': 's', 'quote': quote, 'number': '5'},
            'right': [{'unit_id': 'contract', 'quote': '合同单价5.0000000000001', 'number': '5.0000000000001'}]},
            {**sources, 'contract': {'facts': ['合同单价5.0000000000001']}})
        self.assertFalse(result['matches'])

    def test_payment_direction_guard_keeps_distinct_outgoing_and_incoming_rules(self):
        for prefix, expected in (('支出', True), ('收入', False)):
            quote = prefix + '-155，5，775'
            value, outflow = _payment_operand({'unit_id': 's', 'quote': quote,
                'number': '155' if expected else '-155'}, {'s': {'facts': [quote]}})
            self.assertEqual(value, Decimal('155' if expected else '-155'))
            self.assertEqual(outflow is not None, expected)


if __name__ == '__main__':
    unittest.main()
