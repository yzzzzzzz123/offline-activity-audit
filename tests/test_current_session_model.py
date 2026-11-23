"""The model can be supplied by this session without changing the audit chain."""
from datetime import datetime,timedelta,timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

from PIL import Image

from audit_core.common import AuditError
from audit_core.model_settings import ModelSelection,SNAPSHOT_ENV
from audit_core.qwen_model import extract_qwen_json
from audit_core.session_model import ENV,MODEL,PROVIDER,upload_override,selection_from_environment


class CurrentSessionModelTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.worktrees=self.root/'worktrees'
        self.input=self.root/'input';self.input.mkdir()
        self.zip=self.input/'original.zip'
        with zipfile.ZipFile(self.zip,'w') as archive:archive.writestr('material.txt','Synthetic original fixture')
        self.code='HX202609170040'
        self.options=dict(input_source='input',verify_code=self.code,input_dir=self.input,run_id='20261108-upload-test')
        self.config={'enabled':True,'expires_at':(datetime.now(timezone.utc)+timedelta(hours=1)).isoformat(),
                     'exchange_root':str(self.worktrees/'.session-model/authorized-test'),
                     'uploads':[{'verifyCode':self.code,'sha256':hashlib.sha256(self.zip.read_bytes()).hexdigest()}]}
    def configure(self):
        path=self.worktrees/'.settings/current-session-upload.json';path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(self.config),encoding='utf-8')
    def bind(self):
        self.configure()
        return upload_override(self.worktrees,**self.options)

    def test_disabled_by_default_and_never_applies_to_oss_or_other_invoice(self):
        self.assertIsNone(upload_override(self.worktrees,**self.options))
        self.configure()
        self.assertIsNone(upload_override(self.worktrees,**{**self.options,'input_source':'oss'}))
        self.assertIsNone(upload_override(self.worktrees,**{**self.options,'verify_code':'HX202610110012'}))
        self.assertFalse((self.worktrees/'.session-model').exists())

    def test_zip_scope_expiry_and_run_binding_are_enforced(self):
        record=self.bind()
        self.assertEqual(upload_override(self.worktrees,**self.options),record)
        with self.assertRaisesRegex(AuditError,'其他任务'):
            upload_override(self.worktrees,**{**self.options,'run_id':'20261108-other'})
        self.zip.write_bytes(b'changed zip')
        with self.assertRaisesRegex(AuditError,'原件不一致'):
            upload_override(self.worktrees,**self.options)

    def test_expired_or_external_exchange_does_not_fall_back_to_a_paid_provider(self):
        for expiry,root,expected in (
            ((datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(),self.worktrees/'.session-model/test','已到期'),
            (self.config['expires_at'],self.root/'outside','临时范围'),
        ):
            self.config.update(expires_at=expiry,exchange_root=str(root));self.configure()
            with self.subTest(expected=expected),self.assertRaisesRegex(AuditError,expected):
                upload_override(self.worktrees,**self.options)

    def test_normal_formal_subprocess_uses_an_immutable_session_snapshot_without_loading_key(self):
        from audit_core.oss_intake import run_formal_audit_subprocess
        self.configure()
        def run(command,**kwargs):
            self.assertEqual(command[command.index('--producer-model')+1],PROVIDER)
            self.assertEqual(command[command.index('--model')+1],MODEL)
            self.assertEqual(command[command.index('--verify-code')+1],self.code)
            self.assertNotIn(SNAPSHOT_ENV,kwargs['env'])
            record=json.loads(kwargs['env'][ENV]);self.assertEqual(record['verifyCode'],self.code)
            target=Path(command[command.index('--result-json')+1]);target.write_text(json.dumps({'status':'completed','workspace_id':'isolated-current-session'}))
            from types import SimpleNamespace
            return SimpleNamespace(returncode=0,stdout='')
        with patch('audit_core.model_settings.ModelSettingsStore.snapshot',side_effect=AssertionError('Must not load API Key')), \
             patch('audit_core.oss_intake.subprocess.run',side_effect=run):
            result=run_formal_audit_subprocess(run_id=self.options['run_id'],producer_model='ppio',
                input_dir=self.input,worktrees_root=self.worktrees,scenario='promotional_display',
                workbench_url=None,biz_type='陈列堆头',input_source='input',verify_code=self.code)
        self.assertEqual(result['status'],'completed')

    def test_session_response_runs_through_real_schema_retry_and_preserves_actual_identity(self):
        record=self.bind();case=Path(record['exchange_root']);stage=self.root/'actual-model-stage';stage.mkdir()
        image=stage/'original.png';Image.new('RGB',(20,20),'white').save(image)
        schema=stage/'schema.json';schema.write_text(json.dumps({'type':'object','additionalProperties':False,
            'properties':{'ok':{'type':'boolean'}},'required':['ok']}),encoding='utf-8')
        skill=self.root/'skill';skill.mkdir();(skill/'SKILL.md').write_text('Existing business rules',encoding='utf-8')
        seen=[];errors=[];stop=threading.Event()
        def respond():
            try:
                deadline=time.monotonic()+15
                while not stop.is_set() and time.monotonic()<deadline:
                    for ready_path in sorted(case.glob('*/ready.json')):
                        folder=ready_path.parent
                        if (folder/'response.json').exists():continue
                        ready=json.loads(ready_path.read_text(encoding='utf-8'))
                        request=json.loads((folder/'request.json').read_text(encoding='utf-8'))
                        self.assertEqual(request['verifyCode'],self.code)
                        self.assertEqual(request['provider'],PROVIDER)
                        self.assertEqual(request['images'][0]['sha256'],hashlib.sha256(image.read_bytes()).hexdigest())
                        self.assertIn('Existing business rules',request['system_instructions'])
                        seen.append(request)
                        value={'ok':'wrong type'} if len(seen)==1 else {'ok':True}
                        payload={'request_sha256':ready['request_sha256'],'responder':'current_codex_session','value':value}
                        temporary=folder/'answer.tmp';temporary.write_text(json.dumps(payload),encoding='utf-8')
                        temporary.replace(folder/'response.json')
                    time.sleep(.02)
            except BaseException as error:errors.append(error)
        worker=threading.Thread(target=respond,daemon=True);worker.start()
        try:
            with patch.dict(os.environ,{ENV:json.dumps(record)},clear=False), \
                 patch('audit_core.qwen_model.complete_json',side_effect=AssertionError('No external inference')), \
                 patch('audit_core.codex_runner._run_codex_json',side_effect=AssertionError('No delegated agent')):
                selection=selection_from_environment()
                value=extract_qwen_json(connection=selection,root=stage,skill=skill,schema_path=schema,
                    prompt='Read this exact original material',images=[image],label='会话替代隔离样本',validator=lambda value:None)
            self.assertEqual(value,{'ok':True})
            self.assertEqual(len(seen),2)
            self.assertIn('上一次本批 JSON 未通过校验',seen[1]['input_text'])
            self.assertEqual(errors,[])
        finally:stop.set();worker.join(timeout=2)


if __name__=='__main__':unittest.main()
