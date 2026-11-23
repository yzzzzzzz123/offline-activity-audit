"""Source name whitespace must not reject an otherwise complete POS extraction."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit_core.common import AuditError
from audit_core.pos_product_identity import prepare_pos_identities, validate_extraction
from tests.test_pos_product_identity import row, document, extraction, case_for, catalog


class PosNameWhitespaceTests(unittest.TestCase):
    def test_original_trailing_name_spaces_are_kept_without_changing_evidence(self):
        data = row(name='D22-01参半口腔鲨鱼臻白酵素1+2特享装 160g+100g*2  ')
        source, value = document(data=data), extraction(data)
        before = deepcopy((source, value))
        validate_extraction(value, source)
        self.assertEqual((source, value), before)
        self.assertTrue(value['rows'][0]['product_name']['value'].endswith('  '))

    def test_leading_or_unicode_whitespace_requires_the_same_actual_source_text(self):
        for name in ('  参半喷雾20ml', '\t参半喷雾20ml\u3000'):
            with self.subTest(name=name):
                data = row(name=name)
                source, value = document(data=data), extraction(data)
                validate_extraction(value, source)
                value['rows'][0]['product_name']['value'] = ' ' + name
                with self.assertRaises(AuditError):
                    validate_extraction(value, source)

    def test_empty_names_and_whitespace_changed_barcode_remain_rejected(self):
        for data in (row(name='  '), row(barcode=' 6970356168997'), row(barcode='6970356168997 ')):
            with self.subTest(data=data), self.assertRaises(AuditError):
                validate_extraction(extraction(data), document(data=data))

    def test_cross_row_quotes_and_shortened_barcode_still_fail(self):
        data = row(name='参半喷雾20ml  ')
        source = document(data=data)
        for change in ('quote', 'barcode', 'row'):
            value = extraction(data)
            if change == 'quote':
                value['rows'][0]['product_name']['quote'] = '另一行参半喷雾20ml  '
            elif change == 'barcode':
                value['rows'][0]['barcode_69']['value'] = '697035616899'
            else:
                value['rows'][0]['fact_indexes'] = [0]
            with self.subTest(change=change), self.assertRaises(AuditError):
                validate_extraction(value, source)

    def test_normal_product_lookup_keeps_original_name_and_exact_barcode(self):
        data = row(name='参半渠道商品名称  ')
        case = case_for(data=data)
        value = extraction(data)
        before = deepcopy((case, value))
        call = Mock(return_value=value)
        with tempfile.TemporaryDirectory() as temporary, \
                patch('audit_core.pos_product_identity.load_product_catalog', return_value=catalog()), \
                patch('audit_core.pos_product_identity.save_model_observations'):
            result = prepare_pos_identities(case, Path(temporary), call)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['rows'][0]['status'], 'pass')
        self.assertEqual(result['rows'][0]['submitted']['product_name'], '参半渠道商品名称  ')
        self.assertEqual(result['rows'][0]['submitted']['barcode_69'], '6970356168997')
        self.assertEqual((case, value), before)


if __name__ == '__main__':
    unittest.main()
