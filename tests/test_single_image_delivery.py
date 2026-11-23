"""Every real request carries at most one original, in the same conversation."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx
from PIL import Image

from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_settings import api_selection
from audit_core.pdf_materials import PdfEvidenceProvider, ORCHESTRATOR_SKILL
from audit_core.prompts import bound_prompt


class SingleImageDeliveryTests(TestCase):
    def test_requested_original_is_reread_without_prior_wrong_ocr_and_then_returned_to_owner(self):
        selection=api_selection({'provider':'zimaocloud','api_key':'synthetic-never-send','reasoning_effort':'max'})
        provider=PdfEvidenceProvider(selection.model,selection.reasoning_effort,connection=selection)
        requests=[]
        outputs=[{'checks':[]}, {'facts':['本图水印2025.12.12 星期五，14:32'],'limitations':[]},
                 {'checks':[{'rule_id':'photo_activity_dates','status':'pass','reason':'重新核对原图为2025年12月12日。','source_ids':[]}]}]
        def remote(request):
            payload=json.loads(request.content);requests.append(payload)
            self.assertEqual(payload['reasoning_effort'],'max')
            event={'choices':[{'delta':{'content':json.dumps(outputs.pop(0),ensure_ascii=False)},'finish_reason':'stop'}]}
            return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
        original_client=httpx.AsyncClient
        def client(*args,**kwargs):
            kwargs['transport']=httpx.MockTransport(remote);return original_client(*args,**kwargs)
        schema={'type':'object','required':['checks'],'properties':{'checks':{'type':'array'}}}
        with TemporaryDirectory() as folder:
            root=Path(folder);picture=root/'original.png';Image.new('RGB',(8,8),'white').save(picture)
            original_bytes=picture.read_bytes()
            with collect_model_artifacts(root/'analysis'),patch('audit_core.qwen_model.httpx.AsyncClient',side_effect=client):
                provider._call(root/'seed',ORCHESTRATOR_SKILL,schema,
                    bound_prompt('INITIAL-OCR-2026: 初读的错误年份，尚待核验'),images=[],label='初步判断',validator=lambda _:None)
                result=provider._call(root/'recheck',ORCHESTRATOR_SKILL,schema,
                    bound_prompt('INITIAL-OCR-2026: 请核对这处日期与星期问题'),images=[picture],label='定向回看',
                    validator=lambda _:None,original_reinspection=True)
            self.assertEqual(result['checks'][0]['status'],'pass')
            self.assertEqual(picture.read_bytes(),original_bytes)
        counts=[sum(block.get('type')=='image_url' for message in payload['messages']
                    if isinstance(message['content'],list) for block in message['content']) for payload in requests]
        self.assertEqual(counts,[0,1,0])
        self.assertEqual([len(p['messages']) for p in requests],[2,2,4])
        independent=json.dumps(requests[1],ensure_ascii=False)
        self.assertNotIn('INITIAL-OCR-2026',independent)
        continued=json.dumps(requests[2],ensure_ascii=False)
        self.assertIn('2025.12.12 星期五',continued)
        self.assertIn('不要因旧转录在前文重复多次',continued)

    def test_multi_image_audit_sends_each_original_once_then_aggregates_without_images(self):
        selection = api_selection({'provider': 'deepseek', 'api_key': 'synthetic-never-send'})
        provider = PdfEvidenceProvider(selection.model, selection.reasoning_effort, connection=selection)
        requests = []
        outputs = [{'facts': ['单图实际可见内容' + str(i)], 'limitations': []} for i in range(3)] + [{'checks': []}]
        def handler(request):
            requests.append(json.loads(request.content))
            event = {'choices': [{'delta': {'content': json.dumps(outputs.pop(0), ensure_ascii=False)}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')
        transport = httpx.MockTransport(handler)
        original_client = httpx.AsyncClient
        def client(*args, **kwargs):
            kwargs['transport'] = transport
            return original_client(*args, **kwargs)
        with TemporaryDirectory() as folder:
            root = Path(folder)
            images = []
            for i in range(3):
                path = root / f'original-{i}.png'
                Image.new('RGB', (8, 8), (i * 40, 10, 20)).save(path)
                images.append(path)
            with collect_model_artifacts(root/'analysis'), patch('audit_core.qwen_model.httpx.AsyncClient', side_effect=client):
                result = provider._call(root/'audit', ORCHESTRATOR_SKILL,
                    {'type': 'object', 'required': ['checks'], 'properties': {'checks': {'type': 'array'}}},
                    bound_prompt('逐张核验本单原图，保留每个来源，不能借其他图补造事实。'),
                    images=images, label='隔离逐图审核', validator=lambda value: value)
            self.assertEqual(result, {'checks': []})
            counts = [sum(block.get('type') == 'image_url' for message in request['messages']
                          if isinstance(message['content'], list) for block in message['content']) for request in requests]
            self.assertEqual(counts, [1, 1, 1, 0])
            self.assertEqual([len(request['messages']) for request in requests], [2, 4, 6, 8])
            last = json.dumps(requests[-1], ensure_ascii=False)
            self.assertTrue(all(path.name in last for path in images))
            self.assertIn('单图实际可见内容0', last)
            self.assertIn('单图实际可见内容2', last)
            self.assertNotIn('data:image', last)

    def test_one_image_uses_original_call_without_extra_visual_stage(self):
        provider = PdfEvidenceProvider('fixture', 'medium')
        with TemporaryDirectory() as folder:
            root = Path(folder); image = root/'one.png'
            Image.new('RGB', (8, 8)).save(image)
            with patch('audit_core.codex_runner._find_codex', side_effect=AssertionError('no external process')), \
                 patch('audit_core.single_image_review.run', side_effect=AssertionError('single image must not split')):
                # Only exercise routing; this fixture is deliberately stopped
                # before it can create an external model process.
                with self.assertRaisesRegex(AssertionError, 'no external process'):
                    provider._call_stage(root/'stage', ORCHESTRATOR_SKILL, {'type': 'object'},
                        bound_prompt('只核验本图'), images=[image], label='单图', validator=lambda value: value)
