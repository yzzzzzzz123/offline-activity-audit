"""Uploaded Word and unified reader tests; no live model or customer key."""
from io import BytesIO
from pathlib import Path
import json
import subprocess
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import pymupdf
from PIL import Image
from openpyxl import Workbook

from audit_core.common import AuditError, sha256_file
from audit_core.material_intake import prepare_material_diagnosis
from audit_core.material_reader import read_material
from audit_core.pdf_materials import prepare_units
from audit_core.word_materials import inspect_word, render_word, WordMaterialError
from tests.reader_test_support import new_test_dir, remove_test_dir, test_dir


def docx_bytes(*, external=False):
    output=BytesIO()
    with ZipFile(output,'w') as z:
        z.writestr('[Content_Types].xml','<Types/>')
        z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>完整合同5000元</w:t></w:r></w:p></w:body></w:document>')
        if external:
            z.writestr('word/_rels/document.xml.rels','<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship TargetMode="External" Type="test/image" Target="https://example.invalid/secret"/></Relationships>')
    return output.getvalue()


def pdf_bytes(pages=2):
    with pymupdf.open() as doc:
        for index in range(pages):
            page=doc.new_page(width=240,height=320)
            page.insert_text((20,40),f'FULL PAGE {index+1} AMOUNT 5000')
        return doc.tobytes()


class WordMaterialTests(unittest.TestCase):
    def setUp(self):
        self.root=new_test_dir("word-material-")
        self.addCleanup(remove_test_dir,self.root)
        self.source=self.root/'合同..docx';self.source.write_bytes(docx_bytes())

    def fake_command(self, command, **kwargs):
        output=Path(command[command.index('--output')+1]);output.write_bytes(pdf_bytes())
        self.assertTrue(any(str(part).endswith('word_render_worker.py') for part in command))
        self.assertIn('timeout',kwargs)
        return subprocess.CompletedProcess(command,0,'{"tool":"synthetic-renderer"}','')

    def test_docx_validates_package_without_model(self):
        value=inspect_word(self.source)
        self.assertEqual(value['format'],'docx');self.assertEqual(value['member_count'],2)

    def test_corrupt_docx_is_material_error_before_converter(self):
        self.source.write_bytes(b'not a word file')
        with patch('audit_core.word_materials.run_command') as command:
            with self.assertRaises(WordMaterialError): render_word(self.source,self.root/'render')
        command.assert_not_called()

    def test_broken_internal_xml_is_reported(self):
        with ZipFile(self.source,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>');z.writestr('word/document.xml','<broken')
        with self.assertRaises(WordMaterialError):inspect_word(self.source)

    def test_duplicate_package_members_are_not_accepted(self):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            with ZipFile(self.source,'a') as z:z.writestr('word/document.xml','<document/>')
        with self.assertRaises(WordMaterialError):inspect_word(self.source)

    def test_external_linked_images_are_not_fetched(self):
        self.source.write_bytes(docx_bytes(external=True))
        with patch('audit_core.word_materials.run_command') as command:
            with self.assertRaisesRegex(WordMaterialError,'外部内容'):inspect_word(self.source)
        command.assert_not_called()

    def test_word_xml_checks_stream_large_text_and_do_not_create_document_tree(self):
        import tracemalloc
        with ZipFile(self.source,'w') as z:
            z.writestr('[Content_Types].xml','<Types/>')
            z.writestr('word/document.xml','<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>'+('完整原文'*150000)+'</w:t></w:r></w:p></w:body></w:document>')
        tracemalloc.start()
        try:
            inspect_word(self.source)
            _,peak=tracemalloc.get_traced_memory()
        finally:tracemalloc.stop()
        self.assertLess(peak,2*1024*1024)

    def test_permission_and_io_are_not_material_errors(self):
        error=PermissionError('denied')
        with patch.object(Path,'open',side_effect=error):
            with self.assertRaises(PermissionError):inspect_word(self.source)

    def test_missing_linux_converter_is_system_error(self):
        with patch('audit_core.word_materials.os.name','posix'),patch('audit_core.word_materials.libreoffice_path',return_value=None):
            with self.assertRaisesRegex(AuditError,'缺少Word读取工具') as caught:
                render_word(self.source,self.root/'render')
        self.assertNotIsInstance(caught.exception,WordMaterialError)

    def test_conversion_timeout_is_not_customer_missing_material(self):
        with patch('audit_core.word_materials.run_command',side_effect=subprocess.TimeoutExpired('renderer',200)):
            with self.assertRaisesRegex(AuditError,'超时') as caught:
                render_word(self.source,self.root/'render')
        self.assertNotIsInstance(caught.exception,WordMaterialError)

    def test_worker_failure_and_invalid_pdf_are_system_errors(self):
        for result in [subprocess.CompletedProcess([],1,'','error'),subprocess.CompletedProcess([],0,'{}','')]:
            with self.subTest(result=result.returncode),test_dir("word-material-") as d:
                with patch('audit_core.word_materials.run_command',return_value=result):
                    with self.assertRaises(AuditError) as caught:render_word(self.source,Path(d)/'render')
                self.assertNotIsInstance(caught.exception,WordMaterialError)

    def test_full_pdf_original_hash_and_bounded_worker_are_preserved(self):
        before=sha256_file(self.source)
        with patch('audit_core.word_materials.run_command',side_effect=self.fake_command):
            value=render_word(self.source,self.root/'render')
        self.assertEqual(value['pages'],2);self.assertEqual(sha256_file(self.source),before)
        self.assertEqual(value['source_sha256'],before)

    def test_normal_zip_intake_word_reuses_pdf_pages_and_original_identity(self):
        bundle=self.root/'正常资料.zip'
        with ZipFile(bundle,'w') as z:z.writestr('附件/合同..docx',self.source.read_bytes())
        before=sha256_file(bundle)
        with patch('audit_core.word_materials.run_command',side_effect=self.fake_command):
            case=prepare_material_diagnosis(bundle,self.root/'prepared',original_error='',classify_by_content=True)['archives'][0]
            units=prepare_units(case,self.root/'images')
        item=case['inventory'][0]
        self.assertEqual(item['kind'],'visual');self.assertEqual(item['suffix'],'.docx')
        self.assertEqual(item['limitations'],[])
        self.assertEqual([u['source_file'] for u in units],['附件/合同..docx']*2)
        self.assertEqual([u['locator'] for u in units],['第1页','第2页'])
        self.assertEqual(len({u['unit_id'] for u in units}),2)
        self.assertTrue(all(Path(u['image']).is_file() for u in units))
        self.assertIn('FULL PAGE 2',units[1]['native_facts'][0])
        self.assertEqual(sha256_file(bundle),before)

    def test_native_doc_is_detected_without_guessing_from_name(self):
        p=self.root/'unknown.doc';p.write_bytes(bytes.fromhex('d0cf11e0a1b11ae1')+b'dummy')
        self.assertEqual(inspect_word(p)['format'],'doc')
        p.write_bytes(b'bad');self.assertRaises(WordMaterialError,inspect_word,p)

    def test_unified_single_word_reader_records_sources_not_a_verdict(self):
        with patch('audit_core.word_materials.run_command',side_effect=self.fake_command):
            result=read_material(self.source,self.root/'result')
        saved=json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
        self.assertEqual(saved['model_calls'],0)
        self.assertNotIn('decision',saved)
        self.assertEqual(result['unit_count'],2)
        self.assertEqual(saved['archives'][0]['units'][0]['source_file'],self.source.name)

    def test_unified_pdf_png_xlsx_zip_and_jfif_adapters(self):
        pdf=self.root/'scan.pdf';pdf.write_bytes(pdf_bytes())
        png=self.root/'photo.png';Image.new('RGB',(20,30),'red').save(png)
        jfif=self.root/'photo.jfif';Image.new('RGB',(20,30),'blue').save(jfif,format='JPEG')
        xlsx=self.root/'sales.xlsx';wb=Workbook();wb.active.append(['69码','销量','单价','金额']);wb.active.append(['0697123',2,3,6]);wb.save(xlsx);wb.close()
        bundle=self.root/'mixed.zip'
        with ZipFile(bundle,'w') as z:
            for p in (pdf,png,xlsx):z.write(p,p.name)
        for index,source in enumerate((pdf,png,xlsx,bundle,jfif)):
            with self.subTest(source=source.name):
                result=read_material(source,self.root/f'out-{index}')
                saved=json.loads(Path(result['manifest']).read_text(encoding='utf-8'))
                self.assertEqual(result['limitations'],[]);self.assertGreater(result['unit_count'],0)
                self.assertTrue(all(u['source_file'] for a in saved['archives'] for u in a['units']))

    def test_existing_output_is_not_overwritten(self):
        target=self.root/'existing';target.mkdir();(target/'keep').write_text('original')
        with self.assertRaises(AuditError):read_material(self.source,target)
        self.assertEqual((target/'keep').read_text(),'original')


if __name__=='__main__':unittest.main()
