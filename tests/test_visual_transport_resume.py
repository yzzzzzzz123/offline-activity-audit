"""Source transcription checkpoints belong to the frozen image route."""
from copy import deepcopy
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from audit_core.common import AuditError
from audit_core.failed_upload_resume import ResumeEvidenceProvider
from audit_core.resume_request_identity import current_request
from tests.test_failed_upload_resume import SCHEMA, request, result, trace_prefix


class VisualTransportResumeTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name);self.image=self.root/'source.png'
        Image.new('RGB',(4,4),'white').save(self.image)
        self.visual=SimpleNamespace(provider='zimaocloud',model='qwen3.8-max',reasoning_effort='max')
        self.main=SimpleNamespace(provider='deepseek',model='deepseek-v4-pro',reasoning_effort='max',visual=self.visual)
        self.skill=self.root/'skill';self.skill.mkdir()
        self.saved_label='独立图片读取一';self.pending_label='独立图片读取二'

    def visual_request(self,sequence,call,label,text):
        event=request(sequence,call,label,text)
        event['agent_id']='visual'
        event['data']=current_request(self.visual,self.skill,SCHEMA,text,[self.image],transcription=True)|{'attempt':1}
        return event

    def traces(self):
        return trace_prefix()+[
            self.visual_request(2,'saved',self.saved_label,'saved source'),
            result(3,'saved',self.saved_label,{'ok':True}),
            self.visual_request(4,'failed',self.pending_label,'pending source'),
            {'sequence':5,'type':'ai.failed','call_id':'failed',
             'label':self.pending_label+' · 第1次请求失败',
             'data':{'attempt':1,'error_code':'invalid_output','retryable':True}}]

    def test_main_route_reuses_visual_result_and_only_calls_pending_image(self):
        provider=ResumeEvidenceProvider(self.main,self.traces(),run_id='same-run')
        self.assertEqual(provider.pending_label,self.pending_label)
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',return_value={'ok':True}) as model:
            value=provider._call(self.root,self.skill,SCHEMA,'saved source',images=[self.image],
                label=self.saved_label,validator=lambda _:None,transcription=True)
            self.assertEqual(value,{'ok':True});self.assertEqual(model.call_count,0)
            provider._call(self.root,self.skill,SCHEMA,'pending source',images=[self.image],
                label=self.pending_label,validator=lambda _:None,transcription=True)
            self.assertEqual(model.call_count,1)
        self.assertEqual(provider.reused,[self.saved_label])
        self.assertIs(provider._visual_provider.connection,self.visual)
        self.assertTrue(provider._visual_provider.pending_started)

    def test_model_or_effort_changes_do_not_reuse_old_visual_evidence(self):
        traces=self.traces()
        for field,value in [('model','qwen3.8-plus'),('reasoning_effort','low')]:
            with self.subTest(field=field):
                visual=deepcopy(self.visual);setattr(visual,field,value)
                main=deepcopy(self.main);main.visual=visual
                with self.assertRaisesRegex(AuditError,'断点模型'):
                    ResumeEvidenceProvider(main,traces)

    def test_visual_answer_cannot_be_reused_with_only_main_connection(self):
        main=deepcopy(self.main);main.visual=None
        with self.assertRaisesRegex(AuditError,'断点模型'):
            ResumeEvidenceProvider(main,self.traces())


if __name__=='__main__':unittest.main()
