"""Versioned completed-record repair, with synthetic files and no model/network."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.completed_upload_revalidation import revalidate_completed_upload
from audit_core.failed_upload_resume import resume_local_upload
from audit_core.workbench_store import WorkbenchRunStore, reserve_workspace, atomic_write_json
from tests.test_failed_upload_resume import CONNECTION, trace_prefix, request, result


class CompletedUploadRevalidationTests(TestCase):
    job_id='1'*24

    def setup_record(self,root):
        root.mkdir(exist_ok=True)
        workspace=root/'local-completed';workspace.mkdir()
        trace=workspace/'analysis/execution-trace';trace.mkdir(parents=True)
        events=trace_prefix()+[request(2,'target','商品','source A'),result(3,'target','商品',{'ok':True})]
        for event in events:atomic_write_json(trace/f'step-{event["sequence"]:08d}.json',event)
        (workspace/'snapshot.json').write_text('{"old":"complete"}',encoding='utf-8')
        (workspace/'ai-analysis-summary.md').write_text('old completed summary',encoding='utf-8')
        digest=hashlib.sha256((workspace/'snapshot.json').read_bytes()).hexdigest()
        manifest={'status':'completed','run_id':'same-run','snapshot_sha256':digest,
                  'producer_model':CONNECTION.provider,'audit_model':CONNECTION.model,
                  'workbench_url':'http://127.0.0.1:8080/','token_usage':{'total_tokens':500}}
        atomic_write_json(workspace/'manifest.json',manifest)
        archive=root/'original.zip';archive.write_bytes(b'original fixture archive')
        job={'status':'completed','input_source':'input','callback':{'required':False,'attempts':0},
             'run_id':'same-run','delivery':{'input_file':str(archive),'sha256':hashlib.sha256(archive.read_bytes()).hexdigest()},
             'verifyCode':'','bizType':'补差','largeVenueFee':None,'cvsOtcChannel':False,
             'result':{'status':'completed','old_result':'preserve'}}
        path=root/'.intake/jobs'/f'{self.job_id}.json';atomic_write_json(path,job)
        return workspace,path,digest

    def execute(self,root,digest,runner,**kwargs):
        with ExitStack() as stack:
            stack.enter_context(patch('audit_core.model_settings.ModelSettingsStore.snapshot',return_value=CONNECTION))
            stack.enter_context(patch('audit_core.workbench_runtime.run_persistent_audit',side_effect=lambda run_id,**kw:runner(**kw)))
            stack.enter_context(patch('audit_core.pdf_materials.PdfEvidenceProvider._call',side_effect=AssertionError('Real model forbidden')))
            return revalidate_completed_upload(self.job_id,root,from_call_id='target',expected_snapshot_sha256=digest,**kwargs)

    def candidate_completed(self,**kwargs):
        workspace=kwargs['resume_workspace'];provider=kwargs['evidence_provider']
        provider._call(workspace/'unused',Path('skill'),{'type':'object','required':['ok'],'properties':{'ok':{'const':True}}},
                       'source A',images=[],label='商品',validator=lambda _:None)
        self.assertEqual(kwargs['revalidation_snapshot_sha256'],hashlib.sha256((workspace/'snapshot.json').read_bytes()).hexdigest())
        (workspace/'snapshot.json').write_text('{"new":"complete"}',encoding='utf-8')
        (workspace/'ai-analysis-summary.md').write_text('corrected summary',encoding='utf-8')
        manifest=json.loads((workspace/'manifest.json').read_text(encoding='utf-8'))
        manifest['snapshot_sha256']=hashlib.sha256((workspace/'snapshot.json').read_bytes()).hexdigest()
        manifest['token_usage']={'total_tokens':500}
        atomic_write_json(workspace/'manifest.json',manifest)
        return {'status':'completed','worktree':str(workspace),'snapshot':str(workspace/'snapshot.json'),
                'analysis_summary':str(workspace/'ai-analysis-summary.md')}

    def test_success_keeps_full_previous_version_same_identity_and_no_callback(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);workspace,job_path,digest=self.setup_record(root)
            old_job=job_path.read_bytes();old_manifest=(workspace/'manifest.json').read_bytes()
            def runner(**kwargs):
                self.assertEqual(job_path.read_bytes(),old_job)
                self.assertEqual((workspace/'manifest.json').read_bytes(),old_manifest)
                self.assertEqual((workspace/'ai-analysis-summary.md').read_text(encoding='utf-8'),'old completed summary')
                return self.candidate_completed(**kwargs)
            output=self.execute(root,digest,runner)
            self.assertEqual(output['worktree'],str(workspace))
            self.assertEqual(Path(output['analysis_summary']).read_text(encoding='utf-8'),'corrected summary')
            job=json.loads(job_path.read_text(encoding='utf-8'))
            self.assertEqual(job['callback'],{'required':False,'attempts':0})
            version=root/job['revalidation_history'][0]['version']
            self.assertEqual((version/'previous/manifest.json').read_bytes(),old_manifest)
            self.assertEqual((version/'previous-job.json').read_bytes(),old_job)
            self.assertEqual(json.loads((version/'receipt.json').read_text(encoding='utf-8'))['state'],'published')
            self.assertFalse((workspace/'run-resume.lock').exists())

    def test_failed_candidate_preserves_published_result_and_job_byte_for_byte(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);workspace,job_path,digest=self.setup_record(root)
            old_job=job_path.read_bytes();old_snapshot=(workspace/'snapshot.json').read_bytes()
            def fail(**kwargs):
                (kwargs['resume_workspace']/'snapshot.json').write_text('private failed candidate',encoding='utf-8')
                raise AuditError('candidate failed')
            with self.assertRaisesRegex(AuditError,'candidate failed'):self.execute(root,digest,fail)
            self.assertEqual(job_path.read_bytes(),old_job)
            self.assertEqual((workspace/'snapshot.json').read_bytes(),old_snapshot)
            receipt=next((root/'.revalidation-history').rglob('receipt.json'))
            self.assertEqual(json.loads(receipt.read_text(encoding='utf-8'))['state'],'not_published')

    def test_stale_snapshot_running_remote_callback_or_unknown_target_never_calls_runner(self):
        for mode in ('hash','running','remote','callback','attempted','target'):
            with self.subTest(mode=mode),TemporaryDirectory() as temporary:
                root=Path(temporary);workspace,job_path,digest=self.setup_record(root)
                job=json.loads(job_path.read_text(encoding='utf-8'))
                if mode=='hash':digest='0'*64
                if mode=='running':job['status']='running'
                if mode=='remote':job['input_source']='oss'
                if mode=='callback':job['callback']['required']=True
                if mode=='attempted':job['callback']['attempts']=1
                if mode=='target':
                    path=workspace/'analysis/execution-trace/step-00000002.json'
                    event=json.loads(path.read_text(encoding='utf-8'));event['call_id']='other';atomic_write_json(path,event)
                atomic_write_json(job_path,job)
                with self.assertRaises(AuditError):self.execute(root,digest,lambda **_:self.fail('must not start'))

    def test_success_without_visiting_original_target_does_not_publish(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);workspace,job_path,digest=self.setup_record(root)
            original=job_path.read_bytes()
            with self.assertRaises(AuditError):self.execute(root,digest,lambda **_:{'status':'completed'})
            self.assertEqual(original,job_path.read_bytes())

    def test_publish_failure_restores_previous_directory_and_job(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);workspace,job_path,digest=self.setup_record(root)
            original=job_path.read_bytes();original_snapshot=(workspace/'snapshot.json').read_bytes()
            from audit_core.workbench_store import atomic_write_json as write
            def fail_publish(path,value):
                if Path(path)==job_path and 'revalidation_history' in value:raise OSError('publish denied')
                return write(path,value)
            with patch('audit_core.workbench_store.atomic_write_json',side_effect=fail_publish):
                with self.assertRaisesRegex(OSError,'publish denied'):self.execute(root,digest,self.candidate_completed)
            self.assertEqual(job_path.read_bytes(),original)
            self.assertEqual((workspace/'snapshot.json').read_bytes(),original_snapshot)

    def test_concurrent_original_changes_and_candidate_history_tampering_are_not_published(self):
        for mode in ('concurrent','history','candidate_snapshot'):
            with self.subTest(mode=mode),TemporaryDirectory() as temporary:
                root=Path(temporary);workspace,job_path,digest=self.setup_record(root)
                original_snapshot=(workspace/'snapshot.json').read_bytes()
                def runner(**kwargs):
                    output=self.candidate_completed(**kwargs)
                    if mode=='concurrent':
                        job=json.loads(job_path.read_text(encoding='utf-8'));job['other_update']=True;atomic_write_json(job_path,job)
                    elif mode=='history':
                        (kwargs['resume_workspace']/'analysis/execution-trace/step-00000003.json').write_text('tampered',encoding='utf-8')
                    else:
                        (kwargs['resume_workspace']/'snapshot.json').write_text('not the sealed snapshot',encoding='utf-8')
                    return output
                with self.assertRaises(AuditError):self.execute(root,digest,runner)
                self.assertEqual((workspace/'snapshot.json').read_bytes(),original_snapshot)
                if mode=='concurrent':self.assertTrue(json.loads(job_path.read_text(encoding='utf-8'))['other_update'])

    def test_ordinary_failed_resume_still_cannot_bypass_completed_protection(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);_,_,_=self.setup_record(root)
            with self.assertRaises(AuditError):resume_local_upload(self.job_id,root)

    def test_candidate_uses_actual_runtime_store_and_publication_with_a_model_free_audit_double(self):
        from tests.test_workbench import _view_payload
        with TemporaryDirectory() as temporary:
            root=Path(temporary);workspace,job_path,_=self.setup_record(root)
            run_id='20261109-synthetic'
            job=json.loads(job_path.read_text(encoding='utf-8'));job['run_id']=run_id;atomic_write_json(job_path,job)
            trace_path=workspace/'analysis/execution-trace/step-00000001.json'
            event=json.loads(trace_path.read_text(encoding='utf-8'));event['data']['run_id']=run_id;atomic_write_json(trace_path,event)
            store=WorkbenchRunStore(workspace,run_id=run_id,producer_model=CONNECTION.provider,
                audit_model=CONNECTION.model,root_html=Path('root.html'),workbench_url='http://127.0.0.1:8080/')
            store.complete(view_payload=_view_payload(),scenarios=['price_difference_support'],verification={})
            digest=json.loads((workspace/'manifest.json').read_text(encoding='utf-8'))['snapshot_sha256']
            original=job_path.read_bytes()
            def fake_audit(*args,**kwargs):
                self.assertEqual(job_path.read_bytes(),original)
                provider=kwargs['evidence_provider']
                provider._call(Path(kwargs['output_dir'])/'unused',Path('skill'),
                    {'type':'object','required':['ok'],'properties':{'ok':{'const':True}}},
                    'source A',images=[],label='商品',validator=lambda _:None)
                return {'view_payload':_view_payload(),'verification':{},'scenarios':['price_difference_support']}
            with patch('audit_core.model_settings.ModelSettingsStore.snapshot',return_value=CONNECTION), \
                 patch('audit_core.workbench_runtime.run_audit',side_effect=fake_audit), \
                 patch('audit_core.pdf_materials.PdfEvidenceProvider._call',side_effect=AssertionError('Real model forbidden')):
                output=revalidate_completed_upload(self.job_id,root,from_call_id='target',expected_snapshot_sha256=digest)
            self.assertEqual(output['status'],'completed')
            self.assertTrue(Path(output['static_html']).is_file())
            snapshot=json.loads(Path(output['snapshot']).read_text(encoding='utf-8'))
            self.assertEqual(snapshot['view']['title'],_view_payload()['title'])
            manifest=json.loads((workspace/'manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(manifest['resume_history'][-1]['kind'],'completed_revalidation')

    def test_store_requires_matching_explicit_completed_snapshot_and_preserves_usage(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);workspace=reserve_workspace('20261109-test','codex',root)
            store=WorkbenchRunStore(workspace,run_id='20261109-test',producer_model='codex',root_html=Path('root.html'),workbench_url='http://127.0.0.1:8080/')
            store.fail(AuditError('synthetic original failure'))
            manifest=json.loads(store.manifest_path.read_text(encoding='utf-8'))
            manifest.update(status='completed',token_usage={'total_tokens':123})
            digest=hashlib.sha256(store.snapshot_path.read_bytes()).hexdigest();manifest['snapshot_sha256']=digest
            atomic_write_json(store.manifest_path,manifest)
            with self.assertRaises(AuditError):WorkbenchRunStore.resume_failed(workspace,run_id='20261109-test')
            with self.assertRaises(AuditError):WorkbenchRunStore.resume_failed(workspace,run_id='20261109-test',revalidation_snapshot_sha256='0'*64)
            reopened=WorkbenchRunStore.resume_failed(workspace,run_id='20261109-test',revalidation_snapshot_sha256=digest)
            self.assertEqual(reopened.manifest['resume_history'][-1]['previous_token_usage'],{'total_tokens':123})
            self.assertEqual(reopened.manifest['resume_history'][-1]['kind'],'completed_revalidation')
