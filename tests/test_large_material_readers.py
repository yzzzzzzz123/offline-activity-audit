"""Large-file boundary regressions; entirely local deterministic tools."""
import errno
import io
from pathlib import Path
import unittest
from unittest.mock import patch
from zipfile import ZipFile

from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image
import pymupdf

from audit_core.common import AuditError
from audit_core.excel_sources import read_pos_target_summary, read_self_procured_pos_summary
from audit_core.pdf_materials import prepare_units, _spreadsheet_units
from audit_core.workbook_reading import sequential_workbook
from audit_core.material_intake import _visual_limitations
from tests.reader_test_support import new_test_dir, remove_test_dir


class LargeMaterialReaderTests(unittest.TestCase):
    def setUp(self):
        self.root = new_test_dir("large-readers-")
        self.addCleanup(remove_test_dir, self.root)

    def save(self, wb):
        path = self.root/'source.xlsx'
        wb.save(path)
        wb.close()
        return path

    def test_row_cursor_keeps_sparse_cells_and_can_deliberately_seek_back(self):
        wb = Workbook()
        wb.active['A3'] = '原名'
        wb.active['C9000'] = '末行'
        path = self.save(wb)
        old = io.BytesIO(path.read_bytes())
        with ZipFile(old) as source, ZipFile(path, 'w') as target:
            for item in source.infolist():
                target.writestr(item, source.read(item).replace(b'ref="A3:C9000"', b'ref="A1:A1"'))
        book = sequential_workbook(path)
        try:
            sheet = book.worksheets[0]
            self.assertEqual(sheet.max_row, 9000)
            self.assertEqual(sheet.max_column, 3)
            self.assertIsNone(sheet.cell(1, 1).value)
            self.assertEqual(sheet.cell(3, 1).value, '原名')
            self.assertIsNone(sheet.cell(3, 2).value)
            self.assertEqual(sheet.cell(9000, 3).value, '末行')
            self.assertEqual(sheet.cell(3, 1).value, '原名')
        finally:
            book.close()

    def test_summary_tools_keep_rows_and_printed_total_beyond_ten_thousand(self):
        wb = Workbook(write_only=True)
        sheet = wb.create_sheet('完整汇总')
        sheet.append(['日期', '门店名称', '销售数量', '销售金额'])
        count = 10007
        for n in range(count): sheet.append(['2026-09-01', '门店'+str(n), 2, 5])
        sheet.append(['合计', None, count*2, count*5])
        path = self.save(wb)
        for reader in (read_pos_target_summary, read_self_procured_pos_summary):
            with self.subTest(reader=reader.__name__):
                value = reader(path)
                self.assertEqual(value['detail_row_count'], count)
                self.assertEqual(value['records'][-1]['store_name'], '门店10006')
                self.assertEqual(value['printed_amount_status'], 'exact')

    def test_photo_locator_does_not_read_entire_worksheet_xml(self):
        wb = Workbook()
        wb.active['A1'] = '原始表头'
        for n in range(2, 4000): wb.active.cell(n, 2, n)
        png = io.BytesIO()
        Image.new('RGB', (12, 13), 'red').save(png, format='PNG')
        wb.active.add_image(SheetImage(io.BytesIO(png.getvalue())), 'C3999')
        source = self.save(wb)
        output = self.root/'units'
        output.mkdir()
        original = ZipFile.read
        def read(archive, name, *args, **kwargs):
            key = name.filename if hasattr(name, 'filename') else str(name)
            if key.startswith('xl/worksheets/') and key.endswith('.xml'):
                raise AssertionError('whole worksheet XML materialization is not permitted')
            return original(archive, name, *args, **kwargs)
        with patch.object(ZipFile, 'read', read):
            units = _spreadsheet_units({'path': source, 'source_file': source.name, 'file_id': 'f1'}, output)
        photos = [u for u in units if u.get('photo_reference')]
        self.assertEqual(len(photos), 1)
        self.assertIn('R3999C3', photos[0]['locator'])
        self.assertTrue(photos[0]['image'].is_file())

    def pdf(self, count=3):
        source = self.root/'pages.pdf'
        with pymupdf.open() as doc:
            for n in range(count):
                page = doc.new_page(width=180, height=240)
                page.insert_text((10, 30), f'ORIGINAL-PAGE-{n+1}')
            doc.save(source)
        return source

    def prepare(self, source):
        return prepare_units({'inventory': [{'path': source, 'source_file': source.name,
            'kind': 'visual', 'suffix': '.pdf', 'file_id': 'f1'}]}, self.root/'units')

    def test_pdf_all_pages_text_and_full_dimensions_survive_atomic_rendering(self):
        units = self.prepare(self.pdf())
        self.assertEqual([u['locator'] for u in units], ['第1页', '第2页', '第3页'])
        for n, unit in enumerate(units, 1):
            self.assertIn(f'ORIGINAL-PAGE-{n}', unit['native_facts'][0])
            with Image.open(unit['image']) as image: self.assertEqual(image.size, (360, 480))
        self.assertFalse(list((self.root/'units').glob('*.pending.jpg')))

    def test_failed_page_publication_is_not_reported_as_read_or_missing_material(self):
        source = self.pdf(1)
        with patch('audit_core.document_pages.os.replace', side_effect=OSError(errno.ENOSPC, 'disk full')):
            units = self.prepare(source)
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]['read_status'], 'system_failure')
        self.assertEqual(units[0]['error_code'], 'storage_full')
        self.assertFalse(units[0]['customer_actionable'])
        self.assertFalse(list((self.root/'units').glob('*.jpg')))
        self.assertTrue(source.is_file())

    def test_disk_resource_guard_remains_execution_error_not_customer_missing_evidence(self):
        source = self.pdf(1)
        with patch('audit_core.document_pages.shutil.disk_usage') as usage:
            usage.return_value.free = 0
            units = self.prepare(source)
        self.assertEqual(units[0]['read_status'], 'system_failure')
        self.assertEqual(units[0]['error_code'], 'storage_full')
        self.assertFalse(units[0]['customer_actionable'])

    def test_image_pixel_resource_limit_is_not_mislabeled_as_missing_material(self):
        source = self.root/'picture.png'
        Image.new('RGB', (12, 13), 'red').save(source)
        with patch.object(Image, 'MAX_IMAGE_PIXELS', 50):
            with self.assertRaisesRegex(AuditError, '像素尺寸超出安全读取范围'):
                _visual_limitations(source)


if __name__ == '__main__': unittest.main()
