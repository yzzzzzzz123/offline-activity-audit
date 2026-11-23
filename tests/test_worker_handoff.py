from pathlib import Path
from tempfile import TemporaryDirectory
from dataclasses import replace
import hashlib
import json
import threading
import time
import unittest

from audit_core.common import AuditError
from audit_core.worker_handoff import restore_handoff, submission_from_job
from audit_core.oss_intake import OSSIntakeStore, OSSIntakeService
from tests.test_oss_intake import _config


class WorkerHandoffTests(unittest.TestCase):
    def prepare(self, base):
        root=base/'worktrees/.intake';(root/'jobs').mkdir(parents=True)
        jobs=[]
        for index in (1,2):
            uid=f'{index:024x}';archive=base/'input'/uid/'original.zip'
            archive.parent.mkdir(parents=True);archive.write_bytes(b'original')
            job={'job_id':uid,'run_id':f'20261109-upload-{uid[:12]}-{index}',
                'idempotency_fingerprint':uid,'status':'downloaded','input_source':'input',
                'producer_model':'codex','verifyCode':f'HX{index}','analyzeId':index,
                'bizType':'陈列堆头','largeVenueFee':False,'cvsOtcChannel':False,
                'created_at':str(index),'callback':{'required':False,'attempts':0},
                'delivery':{'input_file':str(archive),'sha256':hashlib.sha256(b'original').hexdigest()}}
            (root/'jobs'/f'{uid}.json').write_text(json.dumps(job),encoding='utf-8');jobs.append(job)
        marker={'phase':'ready','old_service_pid':-1,'handoff_id':'fixture',
                'target_workers':2,'expires_at':time.time()+300,'jobs':jobs}
        (root/'worker-handoff.json').write_text(json.dumps(marker),encoding='utf-8')
        return root,jobs

    def test_handoff_restores_same_job_ids_and_does_not_reset_callback(self):
        with TemporaryDirectory() as temporary:
            root,jobs=self.prepare(Path(temporary))
            store=OSSIntakeStore(root)
            self.assertEqual([j['job_id'] for j in store._handoff_jobs],[j['job_id'] for j in jobs])
            for job in jobs:
                self.assertEqual(store.get(job['job_id'])['status'],'downloaded')
                self.assertFalse(submission_from_job(job)['callback_required'])
            self.assertEqual(restore_handoff(root),[])

    def test_changed_zip_or_identity_stops_before_writing_any_job(self):
        for field in ('zip','identity'):
            with self.subTest(field=field),TemporaryDirectory() as temporary:
                root,jobs=self.prepare(Path(temporary))
                if field=='zip':Path(jobs[-1]['delivery']['input_file']).write_bytes(b'changed')
                else:
                    p=root/'jobs'/f'{jobs[-1]["job_id"]}.json';v=json.loads(p.read_text());v['run_id']='other';p.write_text(json.dumps(v))
                before={p.name:p.read_bytes() for p in (root/'jobs').glob('*.json')}
                with self.assertRaises(AuditError):OSSIntakeStore(root)
                self.assertEqual(before,{p.name:p.read_bytes() for p in (root/'jobs').glob('*.json')})

    def test_recovered_queue_runs_two_workers_without_download_or_callback(self):
        with TemporaryDirectory() as temporary:
            base=Path(temporary);root,jobs=self.prepare(base)
            entered=[];both=threading.Event();release=threading.Event()
            def runner(**kw):
                entered.append(kw['run_id'])
                if len(entered)==2:both.set()
                release.wait(5)
                return {'status':'completed','run_id':kw['run_id'],'error_count':0,'scenarios':[]}
            def forbidden(*a,**kw):raise AssertionError('No download, callback or live model in handoff test')
            service=OSSIntakeService(worktrees_root=base/'worktrees',input_root=base/'input',
                config=replace(_config(),audit_workers=2),runner=runner,downloader=forbidden,callback_sender=forbidden)
            try:
                self.assertTrue(both.wait(3));self.assertEqual(len(entered),2)
            finally:
                release.set();service.close();service._download_worker.join(5)
                for worker in service._audit_workers:worker.join(5)
            self.assertTrue(all(service.store.get(j['job_id'])['status']=='completed' for j in jobs))
