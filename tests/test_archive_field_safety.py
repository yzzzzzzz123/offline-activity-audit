"""Original names survive JSON; filesystem aliases cannot overwrite evidence."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile, ZipInfo

from PIL import Image

from audit_core.archive_input import _zip_member_parts, ArchiveInputError
from audit_core.material_corpus import inspect_corpus
from audit_core.material_intake import prepare_material_diagnosis


class ArchiveFieldSafetyTests(unittest.TestCase):
    def test_names_with_control_device_or_normalized_endings_fail_before_reading(self):
        for name in ['NUL.jpg', 'con.xlsx', 'folder/COM1.doc', 'LPT².pdf',
                     'photo.jpg.', 'name /x.png', 'line\nname.pdf', 'tab\tname.xls', 'orig\x00hidden.jpg']:
            with self.subTest(name=name), self.assertRaises(ArchiveInputError):
                _zip_member_parts(ZipInfo(name))
        for name in ["目录/中文＆{原件}'(1).png", 'a\\b\\合同.pdf', 'COM10.xlsx']:
            with self.subTest(name=name):
                self.assertTrue(_zip_member_parts(ZipInfo(name)))

    def test_inventory_mode_does_not_parse_and_corpus_keeps_deduplicated_associations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root/'input'
            source.mkdir()
            image = io.BytesIO()
            Image.new('RGB', (8, 9), 'red').save(image, format='PNG')
            name = "中文＆{原件}'(1).png"
            path = source/'one.zip'
            with ZipFile(path, 'w') as archive:
                archive.writestr(name, image.getvalue())
            (source/'copy.zip').write_bytes(path.read_bytes())
            with patch('audit_core.material_intake._visual_limitations', side_effect=AssertionError('inventory must not decode')):
                inventory = prepare_material_diagnosis(path, root/'inventory', original_error='', read_content=False)
            self.assertEqual(inventory['archives'][0]['inventory'][0]['source_file'], name)
            result = inspect_corpus(source, root/'result')
            saved = json.loads(Path(result['report']).read_text(encoding='utf-8'))
            self.assertEqual(saved['archive_path_count'], 2)
            self.assertEqual(saved['unique_archives'], 1)
            self.assertEqual(len(saved['archives'][0]['paths']), 2)
            self.assertEqual(saved['files'][0]['sources'][0]['source_file'], name)
            self.assertEqual(saved['file_status_counts'], {'read': 1})
            self.assertFalse(list((root/'result').glob('corpus-*')))


if __name__ == '__main__': unittest.main()
