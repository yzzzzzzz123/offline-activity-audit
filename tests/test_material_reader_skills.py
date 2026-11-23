"""On-demand reader selection, real CLI entrypoints, no model invocation."""
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from audit_core.common import AuditError
from audit_core.material_skills import reading_skills, runtime_instructions
from audit_core.paths import PROJECT_ROOT
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.reader_test_support import test_dir


class MaterialReaderSkillTests(unittest.TestCase):
    def test_selection_uses_format_and_placement_not_bill_names(self):
        for filename, expected in [('arbitrary.docx', ['read-audit-document']),
                                   ('contract.pdf', ['read-audit-document']),
                                   ('photo.png', ['read-audit-image']),
                                   ('any.xlsx', ['read-audit-spreadsheet', 'read-audit-image'])]:
            with self.subTest(filename=filename):
                self.assertEqual(reading_skills({'image': Path('page.jpg'), 'source_file': filename}), expected)
        self.assertEqual(reading_skills({'image': None, 'source_file': 'native.xlsx'}), [])

    def test_only_requested_reference_is_opened_and_no_arbitrary_path_is_allowed(self):
        original = Path.read_text
        opened = []
        def read(path, *args, **kwargs):
            opened.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'read_text', read):
            text = runtime_instructions(['read-audit-document', 'read-audit-document'])
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0].parent.parent.name, 'read-audit-document')
        self.assertIn('文书页面读取', text)
        self.assertNotIn('表内图片读取', text)
        with patch.object(Path, 'read_text', side_effect=AssertionError('must not open unregistered paths')):
            with self.assertRaises(AuditError): runtime_instructions(['../../private'])
            self.assertEqual(runtime_instructions([]), '')

    def test_actual_transcription_prompt_gets_selected_instructions_only(self):
        manifest = [{'unit_id': 'one', 'image': 'one.jpg', 'pdf_text': [], 'reading_limitations': [],
                     'reader_skills': ['read-audit-document']}]
        prompt = PdfEvidenceProvider._reading_prompt(manifest, {}).format()
        self.assertIn('文书页面读取：', prompt)
        self.assertNotIn('表内图片读取：', prompt)
        self.assertNotIn('图片读取：逐张准确保留', prompt)
        manifest[0]['reader_skills'] = ['read-audit-spreadsheet', 'read-audit-image']
        prompt = PdfEvidenceProvider._reading_prompt(manifest, {}).format()
        self.assertIn('表内图片读取：', prompt)
        self.assertIn('图片读取：逐张准确保留', prompt)
        self.assertNotIn('文书页面读取：', prompt)

    def test_standalone_entrypoints_read_actual_files_and_reject_wrong_family(self):
        with test_dir("reader-skills-") as directory:
            root = Path(directory)
            source = root/'source.xlsx'
            wb = Workbook()
            wb.active['A1'] = '首页'
            sheet = wb.create_sheet('明细')
            sheet['A1'] = '第二张表'
            wb.save(source)
            wb.close()
            image = root/'picture.png'
            Image.new('RGB', (12, 13), 'red').save(image)
            for family, filename in [('spreadsheet', source), ('image', image)]:
                target = root/family
                command = [sys.executable, '-B', str(PROJECT_ROOT/f'skills/read-audit-{family}/scripts/read.py'),
                           '--input', str(filename), '--output', str(target)]
                result = subprocess.run(command, capture_output=True, text=True, encoding='utf-8',
                                        cwd=PROJECT_ROOT, timeout=60)
                self.assertEqual(result.returncode, 0, result.stderr)
                value = json.loads(result.stdout)
                self.assertEqual(value['model_calls'], 0)
                saved = json.loads(Path(value['manifest']).read_text(encoding='utf-8'))
                if family == 'spreadsheet':
                    archive = saved['archives'][0]
                    self.assertEqual({u['locator'] for u in archive['units']}, {'Sheet', '明细'})
                    self.assertEqual(len(archive['workbook_readings']), 1)
            target = root/'must-not-exist'
            bad = subprocess.run([sys.executable, '-B', str(PROJECT_ROOT/'skills/read-audit-document/scripts/read.py'),
                                  '--input', str(source), '--output', str(target)],
                                 capture_output=True, cwd=PROJECT_ROOT, timeout=30)
            self.assertNotEqual(bad.returncode, 0)
            self.assertFalse(target.exists())


if __name__ == '__main__': unittest.main()
