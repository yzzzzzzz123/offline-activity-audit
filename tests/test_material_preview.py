"""Original previews are scoped to saved inventory and never invoke a model."""
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import urlopen
from unittest.mock import patch
import zipfile

from audit_core.material_preview import original_material, render_material, MaterialPreviewUnavailable
from audit_core.workbench_server import WorkbenchCatalog,WorkbenchHTTPServer,Handler


def digest(value):return hashlib.sha256(value).hexdigest()
def zipped(files):
    out=io.BytesIO()
    with zipfile.ZipFile(out,'w') as archive:
        for name,value in files.items():archive.writestr(name,value)
    return out.getvalue()


class MaterialPreviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name);self.workspace=self.root/'worktrees'/'preview-fixture';self.workspace.mkdir(parents=True)
        self.inputs=self.root/'input';self.inputs.mkdir();(self.workspace/'analysis').mkdir()
        (self.workspace/'manifest.json').write_text(json.dumps({'workspace_id':'preview-fixture','run_id':'20261112-preview','status':'completed','scenarios':[]}),encoding='utf-8')

    def fixture(self,files,items):
        raw=zipped(files);(self.inputs/'资料.zip').write_bytes(raw)
        self.packet={'archive_id':'a001','source_archive':'D:/former-location/资料.zip','archive_sha256':digest(raw),'inventory':items}
        self.save();return raw

    def save(self):
        (self.workspace/'analysis/input-cases.json').write_text(json.dumps({'a001':self.packet},ensure_ascii=False),encoding='utf-8')

    def test_original_bytes_and_hash_mismatch_and_cross_run_identity(self):
        value=b'original text'
        self.fixture({'目录/文件.txt':value},[{'file_id':'a001-f1','source_file':'目录/文件.txt','sha256':digest(value)}])
        before={p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        with patch('audit_core.qwen_model.complete_json',side_effect=AssertionError('Preview cannot call models')):
            self.assertEqual(original_material(self.workspace,self.inputs,'a001-f1'),('目录/文件.txt',value))
        self.assertEqual(before,{p:p.read_bytes() for p in self.root.rglob('*') if p.is_file()})
        with self.assertRaises(MaterialPreviewUnavailable):original_material(self.workspace,self.inputs,'other-run-f1')
        self.packet['inventory'][0]['sha256']='0'*64;self.save()
        with self.assertRaises(MaterialPreviewUnavailable):original_material(self.workspace,self.inputs,'a001-f1')

    def test_nested_and_embedded_files_use_recorded_parent_and_fingerprint(self):
        image=b'image content';inner=zipped({'photo.png':image});doc=zipped({'word/media/one.png':image})
        items=[{'file_id':'a001-f1','source_file':'inner.zip','sha256':digest(inner)},
               {'file_id':'a001-f2','source_file':'inner.zip/photo.png','parent_source':'inner.zip','sha256':digest(image)},
               {'file_id':'a001-f3','source_file':'合同.docx','sha256':digest(doc)},
               {'file_id':'a001-f4','source_file':'合同.docx/提取图.png','parent_source':'合同.docx','sha256':digest(image)}]
        self.fixture({'inner.zip':inner,'合同.docx':doc},items)
        for file_id in ['a001-f2','a001-f4']:self.assertEqual(original_material(self.workspace,self.inputs,file_id)[1],image)

    def test_missing_fingerprints_and_path_traversal_do_not_open_arbitrary_files(self):
        self.fixture({'file.txt':b'value'},[{'file_id':'a001-f1','source_file':'../private.txt'}])
        with self.assertRaises(MaterialPreviewUnavailable):original_material(self.workspace,self.inputs,'a001-f1')
        self.packet.pop('archive_sha256');self.packet['inventory'][0]['source_file']='file.txt';self.save()
        with self.assertRaises(MaterialPreviewUnavailable):original_material(self.workspace,self.inputs,'a001-f1')
        with self.assertRaises(ValueError):original_material(self.workspace,self.inputs,'../../private')

    def test_spreadsheet_paging_and_word_text_are_local_and_escaped(self):
        import openpyxl
        workbook=openpyxl.Workbook();sheet=workbook.active;sheet.title='销售明细'
        sheet.append(['<script>alert(1)</script>','=1+1'])
        for number in range(103):sheet.append([number,str(number)])
        output=io.BytesIO();workbook.save(output);workbook.close()
        mime,body=render_material('明细.xlsx',output.getvalue(),page=1,route='/api/runs/fixture/materials/preview',file_id='a001-f1')
        self.assertIn('text/html',mime);text=body.decode('utf-8')
        self.assertIn('&lt;script&gt;',text);self.assertNotIn('<script>',text)
        self.assertIn('=1+1',text);self.assertIn('page=2',text);self.assertIn('104 行',text)
        _,second=render_material('明细.xls',output.getvalue(),page=2)
        self.assertIn('103',second.decode('utf-8'))
        doc=zipped({'word/document.xml':b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Original contract</w:t></w:r></w:p></w:body></w:document>'})
        self.assertIn(b'Original contract',render_material('合同.docx',doc)[1])

    def test_http_preview_and_download_are_get_only_and_bound_to_inventory(self):
        value=b'original text'
        self.fixture({'原件.txt':value},[{'file_id':'a001-f1','source_file':'原件.txt','sha256':digest(value)}])
        server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,catalog=WorkbenchCatalog(self.root/'worktrees'))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close);self.addCleanup(server.shutdown)
        base=f'http://127.0.0.1:{server.server_address[1]}/api/runs/preview-fixture/materials/preview?file_id=a001-f1'
        with urlopen(base) as response:
            self.assertIn(b'original text',response.read());self.assertIn('sandbox',response.headers['Content-Security-Policy'])
        with urlopen(base+'&download=1') as response:
            self.assertEqual(response.read(),value);self.assertIn('attachment',response.headers['Content-Disposition'])
        with self.assertRaises(HTTPError) as caught:urlopen(base+'&path=/private.txt')
        self.assertEqual(caught.exception.code,400);caught.exception.close()

    def test_pdf_pages_render_locally_without_active_document_content(self):
        import fitz
        with fitz.open() as document:
            document.new_page().insert_text((20,30),'Original page one')
            document.new_page().insert_text((20,30),'Original page two')
            value=document.tobytes()
        mime,body=render_material('原件.pdf',value,page=1,route='/preview',file_id='f1')
        self.assertIn('text/html',mime);text=body.decode('utf-8')
        self.assertIn('第 1 / 2 页',text);self.assertIn('data:image/png;base64,',text);self.assertIn('page=2',text)
        self.assertNotIn('<script',text)


if __name__=='__main__':unittest.main()
