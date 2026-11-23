"""Native POS extraction saves calls while retaining coverage and DB checks."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit_core.model_metrics import collect_model_artifacts
from audit_core.native_pos_identity import native_identity_extractions
from audit_core.pdf_policy import MATERIALS
from audit_core.pos_product_identity import prepare_pos_identities, validate_extraction, verify_identity
from tests.test_pos_product_identity import case_for, catalog


HEADER = '销售!R1: C1=商品名称 | C2=国际条码 | C3=销售数量 | C4=销售单价 | C5=销售金额'


def native_case(scenario='pos_target_incentive', count=300, chunk=150):
    case = case_for(scenario)
    facts = [HEADER] + [f'销售!R{i+2}: C1=原始商品{i} | C2=6970356168997 | C3=1 | C4=1.005 | C5=1.005'
                        for i in range(count)] + [f'销售!R{count+2}: C1=合计 | C3={count} | C5={count}.0']
    documents, units = [], []
    for offset in range(0, len(facts), chunk):
        uid = f'excel-{offset // chunk}'
        part = facts[offset:offset+chunk]
        documents.append({'unit_id': uid, 'roles': ['spreadsheet'], 'facts': part, 'limitations': []})
        units.append({'unit_id': uid, 'file_id': 'original-pos', 'source_file': '原始POS.xlsx',
                      'image': None, 'native_facts': list(part), 'limitations': []})
    case['documents'], case['units'] = documents, units
    case['materials'][1]['source_ids'] = [d['unit_id'] for d in documents]
    return case


class NativePosIdentityTests(unittest.TestCase):
    def extract(self, case):
        return native_identity_extractions(case['documents'], case['units'])

    def sync(self, case):
        for document, unit in zip(case['documents'], case['units']):
            unit['native_facts'] = list(document['facts'])

    def test_every_row_and_quote_remain_bound_across_many_chunks(self):
        case = native_case(count=10013); before = deepcopy(case)
        direct = self.extract(case)
        self.assertEqual(case, before)
        self.assertEqual(len(direct), len(case['documents']))
        names = []
        for document in case['documents']:
            extracted = direct[document['unit_id']]['extracted']
            validate_extraction(extracted, document)
            names.extend(row['product_name']['value'] for row in extracted['rows'])
            for row in extracted['rows']:
                index = row['fact_indexes'][0]
                self.assertIn(row['product_name']['quote'], document['facts'][index])
                self.assertEqual(row['barcode_69']['value'], '6970356168997')
        self.assertEqual(names, [f'原始商品{i}' for i in range(10013)])

    def test_all_eight_types_keep_full_db_verification_without_model_extraction(self):
        for scenario in MATERIALS:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                case = native_case(scenario, count=12, chunk=5)
                call = Mock(side_effect=AssertionError('明确原生列不重复调用模型'))
                with patch('audit_core.pos_product_identity.load_product_catalog', return_value=catalog()), \
                     patch('audit_core.pos_product_identity.verify_identity', wraps=verify_identity) as verify:
                    result = prepare_pos_identities(case, Path(directory), call)
                call.assert_not_called()
                self.assertEqual(verify.call_count, 12)
                self.assertEqual(len(result['rows']), 12)
                self.assertEqual([r['submitted']['product_name'] for r in result['rows']],
                                 [f'原始商品{i}' for i in range(12)])
                self.assertTrue(all(r['status'] == 'pass' for r in result['rows']))

    def test_missing_fields_and_malformed_barcodes_are_not_repaired(self):
        case = native_case(count=3)
        case['documents'][0]['facts'][1:4] = [
            '销售!R2: C1=原名 | C2=06970356168997.0 | C3=1',
            '销售!R3: C1=只填名称 | C3=1',
            '销售!R4: C2=6970356168997 | C3=1']
        self.sync(case)
        with tempfile.TemporaryDirectory() as directory, \
             patch('audit_core.pos_product_identity.load_product_catalog', return_value=catalog()):
            result = prepare_pos_identities(case, Path(directory), Mock(side_effect=AssertionError('无需模型')))
        self.assertEqual([r['reason_code'] for r in result['rows']],
                         ['catalog_product_missing', 'barcode_missing', 'product_name_missing'])
        self.assertEqual(result['rows'][0]['submitted']['barcode_69'], '06970356168997.0')

    def test_header_context_is_bound_to_file_and_sheet(self):
        for field in ('file_id', 'sheet'):
            case = native_case(count=5, chunk=3)
            if field == 'file_id':
                case['units'][1]['file_id'] = 'different-file'
            else:
                case['documents'][1]['facts'] = [f.replace('销售!', '另一工作表!') for f in case['documents'][1]['facts']]
                self.sync(case)
            direct = self.extract(case)
            self.assertIn('excel-0', direct)
            self.assertNotIn('excel-1', direct)

    def test_unknown_prefix_only_keeps_affected_unit_on_existing_model_path(self):
        case = native_case(count=6, chunk=4)
        case['documents'][0]['facts'].insert(0, '销售!R0: 非原生行')
        self.sync(case)
        self.assertEqual(self.extract(case), {})  # malformed syntax rejects file
        case['documents'][0]['facts'][0] = '销售!R1000: C1=原始导出说明'
        # Put the title before the header in actual worksheet row order.
        case['documents'][0]['facts'] = [f.replace('!R1:', '!R2:').replace('!R1000:', '!R1:')
                                       for f in case['documents'][0]['facts']]
        # Avoid creating a duplicated physical row in this independent fixture.
        case['documents'][0]['facts'][2:] = []
        self.sync(case)
        direct = self.extract(case)
        self.assertNotIn('excel-0', direct)
        self.assertIn('excel-1', direct)

    def test_unknown_row_after_header_clears_binding_until_another_header(self):
        case = native_case(count=8, chunk=3)
        case['documents'][0]['facts'][2] = '销售!R3: C8=含义不清的另一张表'
        self.sync(case)
        direct = self.extract(case)
        self.assertNotIn('excel-0', direct)
        self.assertNotIn('excel-1', direct)

    def test_unclear_or_untrusted_native_sources_fall_back_without_guessing(self):
        changes = [
            lambda c: c['units'][0].update(image=Path('rendered-pos.png')),
            lambda c: c['units'][0].update(source_kind='qince'),
            lambda c: c['units'][0].update(native_facts=['内容发生变化']),
            lambda c: c['documents'][0].update(roles=['pos_image']),
            lambda c: c['documents'][0].update(limitations=['原始资料存在读取限制']),
            lambda c: c['documents'][0]['facts'].append(c['documents'][0]['facts'][1]),
        ]
        for change in changes:
            case = native_case(count=2); change(case)
            if change is changes[-1]: self.sync(case)
            self.assertEqual(self.extract(case), {})

    def test_ambiguous_headers_formulas_and_multiline_identity_do_not_use_fast_path(self):
        for replacement in (
                HEADER + ' | C6=条码',
                '销售!R1: C1=SKU | C2=内部货号',
                '销售!R1: C1=商品名称 | C2=商品名称 | C3=条码'):
            case = native_case(count=1);case['documents'][0]['facts'][0] = replacement;self.sync(case)
            self.assertEqual(self.extract(case), {})
        for raw in ('公式=A2；缓存值=6970356168997', '6970356168997\n6970356167341',
                    '6970356168997、6970356167341', '6970356168997 6970356167341'):
            case = native_case(count=1)
            case['documents'][0]['facts'][1] = '销售!R2: C1=原名 | C2=' + raw
            self.sync(case); self.assertEqual(self.extract(case), {})

    def test_cell_delimiters_and_column_order_are_not_silently_reinterpreted(self):
        for row in ('销售!R2: C2=6970356168997 | C1=原名',
                    '销售!R2: C1=原名 | C1=另一名 | C2=6970356168997',
                    '销售!R2: 前缀 C1=原名 | C2=6970356168997'):
            case = native_case(count=1);case['documents'][0]['facts'][1] = row;self.sync(case)
            self.assertEqual(self.extract(case), {})

    def test_actual_trace_and_observation_distinguish_native_extraction_from_ai_usage(self):
        case = native_case(count=2)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); analysis = root/'analysis'
            with collect_model_artifacts(analysis), \
                 patch('audit_core.pos_product_identity.load_product_catalog', return_value=catalog()):
                result = prepare_pos_identities(case, root, Mock(side_effect=AssertionError('无需AI')))
            events = [json.loads(p.read_text(encoding='utf-8')) for p in (analysis/'execution-trace').glob('*.json')]
            event = next(e for e in events if e['type'] == 'pos.identity_extracted')
            self.assertEqual(event['data']['extraction']['rows'], [r['source'] for r in result['rows']])
            self.assertEqual(event['data']['facts'], case['documents'][0]['facts'])
            observation = json.loads(next((analysis/'model-observations').glob('pos-product-identity-*.json')).read_text(encoding='utf-8'))
            self.assertEqual(observation['extraction_modes'][0]['method'], 'native_cells')
            self.assertFalse((analysis/'model-metrics.jsonl').exists())

    def test_original_validator_still_rejects_bad_native_candidate_and_uses_model(self):
        case = native_case(count=1)
        model = self.extract(case)['excel-0']['extracted']
        bad = deepcopy(model);bad['rows'][0]['barcode_69']['value'] = '697035616899'
        with tempfile.TemporaryDirectory() as directory, \
             patch('audit_core.pos_product_identity.native_identity_extractions',
                   return_value={'excel-0': {'extracted': bad, 'headers': []}}), \
             patch('audit_core.pos_product_identity.load_product_catalog', return_value=catalog()):
            call = Mock(return_value=model)
            result = prepare_pos_identities(case, Path(directory), call)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(result['rows'][0]['submitted']['barcode_69'], '6970356168997')


if __name__ == '__main__':
    unittest.main()
