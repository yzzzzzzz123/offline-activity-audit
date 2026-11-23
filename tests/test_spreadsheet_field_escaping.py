"""Native row round trips and real validators; no model calls or tolerance."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from openpyxl import Workbook

from audit_core.audit_rag import pos_chunks
from audit_core.common import AuditError
from audit_core.native_pos_identity import native_identity_extractions
from audit_core.pdf_evidence import calculate, document_map, _staff_stance, _staff_quoted_facts
from audit_core.pdf_materials import _spreadsheet_units, _gate_documents
from audit_core.pos_arithmetic import prepare_pos_arithmetic
from audit_core.pos_product_identity import validate_extraction, verify_identity
from audit_core.prompts import bound_prompt
from audit_core.spreadsheet_rows import ROW, encode_row, parse_cells


class SpreadsheetFieldEscapingTests(unittest.TestCase):
    def test_ordinary_rows_keep_checkpoint_spelling(self):
        fact = encode_row('销售', 3, [(1, '名称'), (5, '0000123'), (8, '1.0000000000000001')])
        self.assertEqual(fact, '销售!R3: C1=名称 | C5=0000123 | C8=1.0000000000000001')

    def test_out_of_order_xml_cells_keep_coordinates_and_duplicates_are_not_overwritten(self):
        self.assertEqual(encode_row('表', 1, [(3, '后列'), (1, '前列')]), '表!R1: C1=前列 | C3=后列')
        with self.assertRaisesRegex(AuditError, '坐标重复'):
            encode_row('表', 1, [(1, '第一个值'), (1, '不能覆盖它')])

    def test_literal_delimiters_quotes_backslashes_controls_and_row_marker_round_trip(self):
        values = ['原名 | C5=999', '引号"反斜杠\\目录\\t和\t制表\n换行\r回车',
                  '商品!R87: 不能变成行号', '{"unit_id":"另一来源"}', '0000690012345', '百分号%＆😀']
        fact = encode_row('销售\n备注', 2, list(enumerate(values, 1)))
        saved = json.loads(json.dumps({'facts': [fact]}, ensure_ascii=False))['facts'][0]
        matched = ROW.fullmatch(saved)
        self.assertEqual((matched[1], int(matched[2])), ('销售\n备注', 2))
        cells = parse_cells(matched[3])
        self.assertEqual([c['raw'] for c in cells.values()], values)
        self.assertEqual(len(cells), 6)
        self.assertTrue(all(c['quote'] in saved for c in cells.values()))
        prompt = bound_prompt('证据：{facts}', facts=json.dumps(saved, ensure_ascii=False)).format()
        self.assertEqual(json.loads(prompt.removeprefix('证据：')), saved)

    def test_malformed_duplicate_or_unclosed_cells_are_not_partially_accepted(self):
        for content in ['C1=1 | C1=2', 'C2=x | C1=y', 'junk | C1=1',
                        '{"C1":"a","C1":"b"}', '{"C1":"a"',
                        '{"C1":null}', '{"C1":{"nested":"not text"}}', '["C1","a"]']:
            with self.subTest(content=content):
                self.assertIsNone(parse_cells(content))

    def test_pos_barcode_json_whitespace_spelling_queries_the_source_digits(self):
        code='6970356160458'
        for source in (code+'\t', '\t'+code+'\r\n', code+'\t '):
            for value in (source, json.dumps(source,ensure_ascii=False)[1:-1], source.strip()):
                with self.subTest(source=source,value=value):
                    fact=encode_row('销售',3,[(3,source),(4,'渠道商品原名')])
                    cells=parse_cells(ROW.fullmatch(fact)[3])
                    extracted={'rows':[{'fact_indexes':[0],
                        'product_name':{'fact_index':0,'quote':cells[4]['quote'],'value':'渠道商品原名'},
                        'barcode_69':{'fact_index':0,'quote':cells[3]['quote'],'value':value}}],
                        'other_facts':[]}
                    document={'facts':[fact]}
                    before=deepcopy(document)
                    validate_extraction(extracted,document)
                    self.assertEqual(extracted['rows'][0]['barcode_69']['value'],code)
                    catalog={'products':[{'product_code':c,'product_name':'库内名称','barcode_69':code}
                                         for c in ('CP-A-001','CP-A-002')]}
                    result=verify_identity(extracted['rows'][0],catalog,None)
                    self.assertEqual(result['status'],'pass')
                    self.assertEqual(len(result['references']),2)
                    self.assertEqual(document,before)

    def test_pos_barcode_escaping_does_not_rewrite_literal_content_or_unknown_code(self):
        for source in ('6970356160458\\t','6970356\t160458','6978974202147\t'):
            with self.subTest(source=source):
                fact=encode_row('销售',3,[(3,source),(4,'原名')])
                cells=parse_cells(ROW.fullmatch(fact)[3])
                extracted={'rows':[{'fact_indexes':[0],
                    'product_name':{'fact_index':0,'quote':cells[4]['quote'],'value':'原名'},
                    'barcode_69':{'fact_index':0,'quote':cells[3]['quote'],'value':source.strip()}}],
                    'other_facts':[]}
                if '\t' in source.strip():
                    with self.assertRaises(AuditError):
                        validate_extraction(extracted,{'facts':[fact]})
                    self.assertEqual(extracted['rows'][0]['barcode_69']['value'],source.strip())
                    continue
                validate_extraction(extracted,{'facts':[fact]})
                result=verify_identity(extracted['rows'][0],{'products':[]},None)
                self.assertEqual(result['reason_code'],'catalog_product_missing')
                self.assertEqual(extracted['rows'][0]['barcode_69']['value'],source.strip())

    def test_pos_barcode_escaped_whitespace_cannot_borrow_a_different_cell_or_truncate(self):
        fact=encode_row('销售',3,[(3,'6970356160458\t'),(4,'原名')])
        cells=parse_cells(ROW.fullmatch(fact)[3])
        for value,quote in (('697035616045\\t',cells[3]['quote']),
                            ('6970356160459\\t',cells[3]['quote']),
                            ('6970356160458\\t','另一行条码')):
            extracted={'rows':[{'fact_indexes':[0],
                'product_name':{'fact_index':0,'quote':cells[4]['quote'],'value':'原名'},
                'barcode_69':{'fact_index':0,'quote':quote,'value':value}}],
                'other_facts':[]}
            with self.subTest(value=value,quote=quote),self.assertRaises(AuditError):
                validate_extraction(extracted,{'facts':[fact]})

    def test_escaped_native_staff_words_preserve_affirmation_and_denial(self):
        for raw, stance in [('是否临促：否\n备注', 'denied'), ('临时聘请促销员\n每日200元', 'affirmed')]:
            fact = encode_row('说明', 2, [(1, raw)])
            self.assertEqual(_staff_stance(fact), stance)
        facts = [encode_row('说明', 2, [(1, '是否临促\n本次安排')]), encode_row('说明', 3, [(1, '是')])]
        self.assertEqual(_staff_quoted_facts('\n'.join(facts), facts), ['\n'.join(facts)])
        unrelated = [facts[0], encode_row('说明', 3, [(2, '是')])]
        self.assertEqual(_staff_quoted_facts('\n'.join(unrelated), unrelated), [])

    def test_upload_parser_arithmetic_identity_and_rag_share_decoded_columns(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'含空格＆括号{资料}.xlsx'
            wb = Workbook()
            ws = wb.active
            ws.title = '销售\n明细'
            ws.append(['商品名称', '69码', '销售数量', '销售单价', '销售金额'])
            name = '原名"牙膏" | C5=999 \\包装{保持原文}'
            ws.append([name, '6970356164395', 2, '1.2500000000000001', '2.5000000000000002'])
            ws.append(['合计', None, 2, None, '2.5000000000000002'])
            wb.save(source)
            wb.close()
            output = root/'units'
            output.mkdir()
            units = _spreadsheet_units({'path': source, 'source_file': source.name, 'file_id': 'f1'}, output)
            documents = [{'unit_id': u['unit_id'], 'roles': ['spreadsheet'], 'facts': u['native_facts'],
                          'limitations': []} for u in units]
            materials = [{'id': 'pos_excel', 'state': 'present', 'source_ids': [u['unit_id'] for u in units]}]
            plan = prepare_pos_arithmetic(documents, materials, units)
            self.assertTrue(plan['complete'], plan['limitations'])
            self.assertEqual(len(plan['comparisons']), 3)
            for comparison in plan['comparisons']:
                result = calculate(comparison, document_map(documents))
                self.assertEqual(result['left_value'], result['right_value'])
            extracted = native_identity_extractions(documents, units)[units[0]['unit_id']]['extracted']
            self.assertEqual(extracted['rows'][0]['product_name']['value'], name)
            validate_extraction(extracted, documents[0])
            inventory = json.loads(_gate_documents(documents))[0]['native_table_inventory']
            self.assertIn(name, inventory['columns']['C1'])
            self.assertEqual(len(inventory['columns']), 5)
            bad = deepcopy(extracted)
            bad['rows'][0]['product_name']['value'] += '虚构'
            with self.assertRaises(AuditError): validate_extraction(bad, documents[0])
            chunks = pos_chunks(documents, units)
            self.assertTrue(all(any('销售数量' in fact for d in c['documents'] for fact in d['facts']) for c in chunks))


if __name__ == '__main__': unittest.main()
