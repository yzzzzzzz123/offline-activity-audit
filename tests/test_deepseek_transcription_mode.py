"""No network: the selected effort reaches reading and every later stage."""
from tests.pdf_test_support import isolated_stage_summary
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import httpx
from PIL import Image

from audit_core.model_settings import api_selection
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.prompts import bound_prompt
from audit_core.qwen_model import complete_json,QwenRequestError


class DeepSeekTranscriptionModeTests(unittest.TestCase):
    def setUp(self):
        self.selection=api_selection({'provider':'deepseek','api_key':'sk-synthetic-only', 'reasoning_effort':'max'})

    def test_explicit_none_selection_disables_thinking_without_raising_output_cap(self):
        def reply(request):
            payload=json.loads(request.content)
            self.assertEqual(payload['thinking'],{'type':'disabled'})
            self.assertNotIn('reasoning_effort',payload)
            self.assertNotIn('max_tokens',payload)
            event={'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}],
                   'usage':{'prompt_tokens':12,'completion_tokens':5}}
            return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
        text,usage=complete_json(replace(self.selection,reasoning_effort='none'),[],system='fixture',
                                transport=httpx.MockTransport(reply))
        self.assertEqual(json.loads(text),{'ok':True})
        self.assertEqual(usage['output_tokens'],5)
        self.assertEqual(self.selection.reasoning_effort,'max')

    def test_transcription_and_audit_share_immutable_max_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);image=root/'visible.png';Image.new('RGB',(8,8),'white').save(image)
            provider=PdfEvidenceProvider(None,None,connection=self.selection, stage_summary_reviewer=isolated_stage_summary)
            with patch('audit_core.qwen_model.extract_qwen_json',return_value={'documents':[]}) as model:
                provider._call(root/'reading',root,{},bound_prompt('Read visible facts'),
                    images=[image],label='reading',validator=None,transcription=True)
                provider._call(root/'audit',root,{},bound_prompt('Check business rules'),
                    images=[image],label='audit',validator=None)
            reading,audit=model.call_args_list
            self.assertIs(reading.kwargs['connection'],self.selection)
            self.assertEqual(audit.kwargs['connection'],self.selection)
            self.assertEqual(provider.connection.reasoning_effort,'max')
            self.assertEqual(provider._reading_identity()[0]['effort'],'max')

    def test_provider_pipeline_sends_max_to_real_http_adapter_for_all_stages(self):
        # Run the real provider, parser and HTTP serializer; intercept the socket
        # boundary so no key or customer material leaves the isolated test.
        from audit_core.pdf_materials import ORCHESTRATOR_SKILL
        for name in ('deepseek', 'zimaocloud'):
            with self.subTest(provider=name), tempfile.TemporaryDirectory() as folder:
                root=Path(folder);image=root/'source.png';Image.new('RGB',(8,8),'white').save(image)
                selection=api_selection({'provider':name,'api_key':'sk-synthetic-only','reasoning_effort':'max'})
                provider=PdfEvidenceProvider(None,None,connection=selection,stage_summary_reviewer=isolated_stage_summary)
                seen=[]
                def remote(request):
                    payload=json.loads(request.content);seen.append(payload)
                    self.assertEqual(payload['reasoning_effort'],'max')
                    self.assertNotIn('max_tokens',payload)
                    self.assertNotIn('max_completion_tokens',payload)
                    if name=='deepseek':self.assertEqual(payload['thinking'],{'type':'enabled'})
                    event={'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
                    return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
                def complete(selected,content,**kwargs):
                    self.assertIs(selected,selection)
                    return complete_json(selected,content,transport=httpx.MockTransport(remote),**kwargs)
                schema={'type':'object','required':['ok'],'properties':{'ok':{'type':'boolean'}}}
                stages=('reading','product-identity','audit','stage-self-check','logic-review','summary')
                with patch('audit_core.qwen_model.complete_json',side_effect=complete):
                    for stage in stages:
                        answer=provider._call(root/stage,ORCHESTRATOR_SKILL,schema,bound_prompt(stage),
                            images=[image] if stage=='reading' else [],label=stage,
                            validator=lambda _:None,transcription=stage=='reading')
                        self.assertEqual(answer,{'ok':True})
                self.assertEqual(len(seen),len(stages))
                self.assertEqual(provider._reading_identity()[0]['effort'],'max')

    def test_read_documents_explicitly_selects_transcription_and_keeps_source_coverage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);image=root/'visible.png';Image.new('RGB',(8,8),'white').save(image)
            provider=PdfEvidenceProvider(None,None,connection=self.selection, stage_summary_reviewer=isolated_stage_summary)
            units=[{'unit_id':'source-1','image':image,'source_file':'original.png','locator':'照片1',
                    'native_facts':[],'limitations':[]}]
            def call(call_root,skill,schema,prompt,**kwargs):
                self.assertTrue(kwargs['transcription'])
                manifest=json.loads(prompt.partial_variables['manifest'])
                value={'documents':[{'unit_id':item['unit_id'],'roles':['photo'],
                    'facts':['本图可见商品'],'limitations':[]} for item in manifest]}
                kwargs['validator'](value)
                return value
            with patch.object(provider,'_call',side_effect=call) as model:
                documents=provider._read_documents({'archive_id':'a001','units':units},root,{})
            self.assertEqual(model.call_count,1)
            self.assertEqual(documents[0]['unit_id'],'source-1')
            self.assertEqual(documents[0]['facts'],['本图可见商品'])

    def test_output_limit_stays_failure_and_names_actual_provider(self):
        seen=[]
        event={'choices':[{'delta':{'content':'{"incomplete":'},'finish_reason':'length'}],
               'usage':{'prompt_tokens':24995,'completion_tokens':32768,
                        'completion_tokens_details':{'reasoning_tokens':22614}}}
        with self.assertRaises(QwenRequestError) as error:
            complete_json(self.selection,[],system='fixture',progress=seen.append,
                transport=httpx.MockTransport(lambda request:httpx.Response(200,
                    text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')))
        self.assertEqual(error.exception.error_code,'output_limit')
        self.assertFalse(error.exception.retryable)
        self.assertIn('DeepSeek',str(error.exception))
        self.assertNotIn('千问',str(error.exception))
        self.assertEqual(seen[-1]['output_tokens'],32768)
