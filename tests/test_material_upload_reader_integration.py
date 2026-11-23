"""Normal archive -> fixed readers -> real API wire with mocked HTTP only.

This verifies transport, schema and source coverage, never OCR model accuracy.
"""
from tests.pdf_test_support import isolated_stage_summary
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx
from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage
from PIL import Image
import pymupdf

from audit_core import qwen_model
from audit_core.material_intake import prepare_material_diagnosis
from audit_core.model_settings import api_selection
from audit_core.pdf_materials import PdfEvidenceProvider, prepare_units
from audit_core.pdf_policy import material_catalogue


class UploadReaderIntegrationTests(unittest.TestCase):
    def test_normal_upload_tools_feed_only_visual_units_to_actual_model_adapter(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            picture = root/'现场 {特殊}＆图.png'
            Image.new('RGB', (19, 23), (6, 7, 8)).save(picture)
            workbook = root/'明细 {原文}.xlsx'
            wb = Workbook()
            wb.active['A1'] = '首页不漏'
            sheet = wb.create_sheet('第二张表')
            raw = '不能拆列 | C9=999 "引号"\\目录\n下一行{schema}'
            sheet.append(['商品名称', '69码', '数量', '单价', '金额'])
            sheet.append([raw, '6970356164395', 2, 3, 6])
            sheet.add_image(SheetImage(str(picture)), 'F2')
            wb.save(workbook)
            wb.close()
            pdf = root/'合同两页.pdf'
            with pymupdf.open() as document:
                for n in range(2):
                    page = document.new_page(width=120, height=160)
                    page.insert_text((10, 30), f'PAGE_{n+1} {{literal}}')
                document.save(pdf)
            package = root/'资料.zip'
            with ZipFile(package, 'w') as archive:
                for source in [picture, workbook, pdf]:
                    archive.write(source, source.name)
            prepared = prepare_material_diagnosis(package, root/'prepared', original_error='', classify_by_content=True)
            units = prepare_units(prepared['archives'][0], root/'units')
            self.assertEqual(sum(bool(u.get('image')) for u in units), 4)
            wire = []
            def handler(request):
                payload = json.loads(request.content)
                wire.append(payload)
                self.assertNotIn('max_tokens', payload)
                self.assertNotIn('max_completion_tokens', payload)
                content = payload['messages'][-1]['content']
                text = '\n'.join(c['text'] for c in content if c['type'] == 'text')
                manifest, _ = json.JSONDecoder().raw_decode(text.split('本批资料：', 1)[1].lstrip())
                self.assertTrue(all(m['reader_skills'] for m in manifest))
                self.assertNotIn(raw, text, '原生Excel不应再次送OCR')
                docs = [{'unit_id': m['unit_id'], 'roles': ['photo'],
                         'facts': m['pdf_text'] or ['模拟可见事实'], 'limitations': []} for m in manifest]
                fields = [{'unit_id': m['unit_id'], **{key: {'state': 'not_applicable', 'fact_indexes': []}
                          for key in ['written_dealer_name', 'seal_name', 'activity_period']}} for m in manifest]
                answer = json.dumps({'documents': docs, 'field_inventory': fields}, ensure_ascii=False)
                event = {'choices': [{'delta': {'content': answer}, 'finish_reason': 'stop'}],
                         'usage': {'prompt_tokens': 0, 'completion_tokens': 0}}
                return httpx.Response(200, text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
            original = qwen_model.complete_json
            def complete(selection, content, **kwargs):
                return original(selection, content, transport=httpx.MockTransport(handler), **kwargs)
            # Both provider adapters use the same reader entry; fake keys cannot
            # leave this test because the actual HTTP transport is prohibited.
            for provider_name in ['ppio', 'deepseek']:
                selection = api_selection({'provider': provider_name,
                    'api_key': 'sk_synthetic_never_send' if provider_name == 'ppio' else 'sk-synthetic-never-send'})
                provider = PdfEvidenceProvider(None, None, connection=selection, stage_summary_reviewer=isolated_stage_summary)
                destination = root/provider_name
                destination.mkdir()
                with patch.object(qwen_model, 'complete_json', side_effect=complete), \
                     patch.object(httpx.AsyncHTTPTransport, 'handle_async_request', side_effect=AssertionError('real network forbidden')):
                    documents = provider._read_documents({'archive_id': 'a001', 'units': units}, destination, material_catalogue())
                self.assertEqual({d['unit_id'] for d in documents}, {u['unit_id'] for u in units})
                by_id = {d['unit_id']: d for d in documents}
                for unit in units:
                    if not unit.get('image'):
                        self.assertEqual(by_id[unit['unit_id']]['facts'], unit['native_facts'])
                text = '\n'.join(c['text'] for p in wire for c in p['messages'][-1]['content'] if c['type'] == 'text')
                self.assertIn('文书页面读取：', text)
                self.assertIn('表内图片读取：', text)
                self.assertIn('图片读取：逐张准确保留', text)
            self.assertGreaterEqual(len(wire), 2)


if __name__ == '__main__': unittest.main()
