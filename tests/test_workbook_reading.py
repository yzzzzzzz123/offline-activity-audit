"""Real workbook fixtures: coverage, exact operands, layout and legacy tools.

No live models or private files are used. These are reader/contract checks,
not a claim that a customer's original audit has been rerun.
"""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image

from audit_core.common import AuditError
from audit_core.excel_sources import read_display_sales, read_maintenance_pos, read_personnel_sales
from audit_core.pdf_materials import _spreadsheet_units, PdfEvidenceProvider
from audit_core.pdf_evidence import validate_documents
from audit_core.pos_arithmetic import prepare_pos_arithmetic
from audit_core.workbook_reading import numeric_text


class WorkbookReadingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def source(self, wb, name='source.xlsx', rewrite=None):
        path = self.root/name
        wb.save(path)
        wb.close()
        if rewrite:
            original = io.BytesIO(path.read_bytes())
            with ZipFile(original) as old, ZipFile(path, 'w') as new:
                for item in old.infolist():
                    new.writestr(item, rewrite(item.filename, old.read(item)))
        return path

    def read(self, source):
        output = self.root/('units-'+str(len(list(self.root.glob('units-*')))))
        output.mkdir()
        before = source.read_bytes()
        units = _spreadsheet_units({'path': source, 'file_id': 'f1', 'source_file': source.name}, output)
        self.assertEqual(source.read_bytes(), before)
        receipt = json.loads((output/'f1-workbook-reading.json').read_text(encoding='utf-8'))
        return units, receipt

    def test_all_sheets_hidden_cells_and_rows_beyond_each_chunk_are_preserved(self):
        wb = Workbook()
        wb.active.title = '说明'
        wb.active['A1'] = '不是销售主表'
        for index, state in enumerate(('visible', 'hidden', 'veryHidden')):
            sheet = wb.create_sheet('明细'+str(index))
            sheet.sheet_state = state
            sheet.append(['商品名称', '商品条码', '数量', '单价', '金额'])
            for n in range(1, 304):
                sheet.append(['原名'+str(n), '0697000000001', n, 2, n*2])
            sheet.row_dimensions[7].hidden = True
            sheet.column_dimensions['D'].hidden = True
        units, receipt = self.read(self.source(wb))
        self.assertEqual(len(receipt['sheets']), 4)
        self.assertEqual(receipt['model_calls'], 0)
        for index in range(3):
            own = [u for u in units if u['locator'] == '明细'+str(index)]
            self.assertEqual(sum(len(u['native_facts']) for u in own), 304)
            self.assertIn('C3=303', own[-1]['native_facts'][-1])
            self.assertEqual(own[0]['sheet_layout']['hidden_rows'], [7])
            self.assertEqual(own[0]['sheet_layout']['hidden_columns'], [{'min': 4, 'max': 4}])
            self.assertEqual(receipt['sheets'][index+1]['row_ranges'], [[1, 150], [151, 300], [301, 304]])

    def test_wrong_dimensions_do_not_truncate_data_or_expand_format_only_area(self):
        wb = Workbook()
        wb.active['A1'] = '头'
        wb.active['C9000'] = '最后一项'
        wb.active['XFD1048576'].number_format = '0.00'
        def rewrite(name, payload):
            if name == 'xl/worksheets/sheet1.xml':
                payload = payload.replace(b'ref="A1:XFD1048576"', b'ref="A1:A1"')
            return payload
        units, receipt = self.read(self.source(wb, rewrite=rewrite))
        self.assertEqual(receipt['stored_cell_count'], 3)
        self.assertEqual([f for u in units for f in u['native_facts']], ['Sheet!R1: C1=头', 'Sheet!R9000: C3=最后一项'])

    def test_numeric_xml_precision_formula_zero_and_missing_cache_stay_distinct(self):
        wb = Workbook()
        wb.active.append([1.25, '=A1*2', '=A1*3', '0000123', '=A1*0'])
        def rewrite(name, payload):
            if name != 'xl/worksheets/sheet1.xml': return payload
            root = ET.fromstring(payload)
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            for c in root.findall('.//s:c', ns):
                if c.get('r') in {'A1', 'B1', 'E1'}:
                    c.find('s:v', ns).text = {'A1': '1.234567890123456789', 'B1': '2.469135780246913578', 'E1': '0'}[c.get('r')]
            return ET.tostring(root)
        units, _ = self.read(self.source(wb, rewrite=rewrite))
        fact = units[0]['native_facts'][0]
        self.assertIn('C1=1.234567890123456789', fact)
        self.assertIn('C2=公式=A1*2；缓存值=2.469135780246913578', fact)
        self.assertIn('C3=公式=A1*3；缓存值=无缓存结果，无法确认数值', fact)
        self.assertIn('C4=0000123', fact)
        self.assertIn('C5=公式=A1*0；缓存值=0', fact)

    def test_array_formula_is_read_as_formula_not_python_object_repr(self):
        wb = Workbook()
        wb.active.append([2, 3, '=A1*B1'])
        def rewrite(name, payload):
            if name != 'xl/worksheets/sheet1.xml': return payload
            root = ET.fromstring(payload)
            ns = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
            cell = root.find(".//s:c[@r='C1']", ns)
            cell.find('s:f', ns).attrib.update(t='array', ref='C1:C1')
            cell.find('s:v', ns).text = '6'
            return ET.tostring(root)
        units, _ = self.read(self.source(wb, rewrite=rewrite))
        fact = units[0]['native_facts'][0]
        self.assertIn('C3=公式=A1*B1；缓存值=6', fact)
        self.assertNotIn('object at', fact)

    def test_merged_header_guides_math_without_filling_blank_numeric_cells(self):
        wb = Workbook()
        sheet = wb.active
        sheet.append(['商品名称', '数量', '销售'])
        sheet.append([None, None, '单价', '金额'])
        for address in ('A1:A2', 'B1:B2', 'C1:D1'): sheet.merge_cells(address)
        sheet.append(['甲', 2, 3, 6])
        sheet.append(['乙', 4, 3, 12])
        sheet.append(['合计', 6, None, 18])
        units, _ = self.read(self.source(wb))
        case = {'units': units, 'archive_id': 'a1'}
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        with patch.object(provider, '_call', side_effect=AssertionError('native cells must not call a model')):
            docs = provider._read_documents(case, self.root, {})
        self.assertCountEqual(docs[0]['sheet_layout']['merged_ranges'], ['A1:A2', 'B1:B2', 'C1:D1'])
        self.assertEqual(docs[0]['facts'][1], 'Sheet!R2: C3=单价 | C4=金额')
        materials = [{'id': 'pos_excel', 'state': 'present', 'source_ids': [u['unit_id'] for u in units]}]
        plan = prepare_pos_arithmetic(docs, materials, units)
        self.assertTrue(plan['complete'], plan['limitations'])
        self.assertEqual(plan['tables'][0]['detail_rows'], 2)
        changed = deepcopy(docs)
        changed[0]['sheet_layout']['sheet'] = '别的表'
        with self.assertRaises(AuditError): validate_documents({'documents': changed}, units)

    def test_all_picture_placements_survive_on_multiple_sheets(self):
        wb = Workbook()
        png = io.BytesIO()
        Image.new('RGB', (12, 14), 'red').save(png, format='PNG')
        for sheet in (wb.active, wb.create_sheet('隐藏照片')):
            for _ in range(2): sheet.add_image(SheetImage(io.BytesIO(png.getvalue())), 'C4')
        wb['隐藏照片'].sheet_state = 'hidden'
        units, receipt = self.read(self.source(wb))
        photos = [u for u in units if u.get('photo_reference')]
        self.assertEqual(len(photos), 4)
        self.assertEqual(receipt['photo_unit_count'], 4)
        self.assertEqual([len(s['photo_unit_ids']) for s in receipt['sheets']], [2, 2])
        self.assertTrue(all(p['image'].read_bytes() == png.getvalue() for p in photos))

    def test_large_layout_is_not_repeated_in_every_table_packet(self):
        wb = Workbook()
        for row in range(1, 602):
            wb.active.cell(row, 1, '原文'+str(row))
            wb.active.merge_cells(start_row=row, start_column=1, end_row=row, end_column=2)
        units, receipt = self.read(self.source(wb))
        self.assertEqual(len(receipt['sheets'][0]['merged_ranges']), 601)
        self.assertEqual(len(units), 5)
        self.assertLessEqual(sum(len(u['sheet_layout']['merged_ranges']) for u in units), 605)
        self.assertEqual(numeric_text({'raw_number': '1E+999999999'}, ''), '1E+999999999')

    def test_legacy_sales_tools_read_other_tabs_and_check_totals_per_sheet(self):
        variants = [
            (read_personnel_sales, ['日期', '门店', '条码', '商品名称', '数量', '单价', '金额'],
             lambda qty, name: ['9月', name, '6970000000001', '商品甲', qty, 3, qty*3]),
            (read_display_sales, ['客户名称', '业务日期', '商品编码', '商品名称', '条码', '数量', '零售价', '合计金额'],
             lambda qty, name: [name, '9月', 'P1', '商品甲', '6970000000001', qty, 3, qty*3]),
            (read_maintenance_pos, ['商品名称', '数量', '销售单价', '销售金额'],
             lambda qty, name: ['商品甲', qty, 3, qty*3]),
        ]
        for index, (reader, headers, row) in enumerate(variants):
            wb = Workbook()
            wb.active['A1'] = '说明页'
            for name, qty in [('甲店', 2), ('乙店', 3)]:
                sheet = wb.create_sheet(name)
                sheet.append(headers)
                sheet.append(row(qty, name))
                total = [None]*len(headers)
                total[0] = '合计'
                total[headers.index('数量')] = qty
                total[-1] = qty*3
                sheet.append(total)
                if name == '乙店': sheet.sheet_state = 'hidden'
            with self.subTest(reader=reader.__name__):
                value = reader(self.source(wb, name=f'legacy-{index}.xlsx'))
                self.assertEqual(len(value['records']), 2)
                self.assertEqual(value['total_quantity'], 5)
                self.assertEqual({r['sheet'] for r in value['records']}, {'甲店', '乙店'})
                self.assertEqual(len(value['sheet_inventory']), 3)
                if 'printed_amount_status' in value:
                    self.assertEqual(value['printed_amount_status'], 'exact')


if __name__ == '__main__': unittest.main()
