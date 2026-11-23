"""Bounded queue concurrency with isolated fake runners; no real AI or callback."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from unittest import mock

from audit_core.oss_intake import FORMAL_RUNNER, OSSIntakeRequestError, OSSIntakeService, OSSIntakeUnavailableError
from audit_core.model_settings import ModelSelection
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from tests.pdf_test_support import fixture_cli_command
from tests.test_oss_intake import (_config, _fake_downloader, _fake_callback_sender,
    _no_network_url_validator, _payload, _wait_for_job)


class IntakeConcurrencyTests(unittest.TestCase):
    def test_two_real_cli_subprocesses_publish_distinct_archives_with_model_doubles(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);callbacks=[];processes=[]
            native_popen=subprocess.Popen
            def launch(command, **kwargs):
                if len(command)>2 and command[2]==str(FORMAL_RUNNER):
                    command=fixture_cli_command(command[3:])
                    # Both child processes must exist before either starts the CLI.
                    barrier=("import os,time\nfrom pathlib import Path\n"
                        f"barrier=Path({str(root/'barrier')!r});barrier.mkdir(exist_ok=True)\n"
                        "(barrier/str(os.getpid())).touch()\n"
                        "deadline=time.monotonic()+15\n"
                        "while len(list(barrier.iterdir()))<2 and time.monotonic()<deadline:time.sleep(.02)\n"
                        "assert len(list(barrier.iterdir()))==2, 'Both formal children must overlap'\n")
                    command[3]=barrier+command[3]
                process=native_popen(command,**kwargs)
                processes.append(process)
                return process
            def callback(payload,config):
                callbacks.append(dict(payload));return _fake_callback_sender(payload,config)
            service=OSSIntakeService(worktrees_root=root/'worktrees',input_root=root/'input',
                config=replace(_config(),audit_workers=2),downloader=_fake_downloader,
                url_validator=_no_network_url_validator,callback_sender=callback)
            with mock.patch('audit_core.oss_intake.subprocess.Popen',side_effect=launch), \
                    mock.patch('audit_core.model_settings.ModelSettingsStore.snapshot',
                               return_value=ModelSelection(api_key='sk_test_fixture_only')), \
                    mock.patch.dict(os.environ, {
                        'OFFLINE_AUDIT_QINCE_OPENID':'1234567890123456789',
                        'OFFLINE_AUDIT_QINCE_APPKEY':'fixture-only-not-a-real-key',
                        'OFFLINE_AUDIT_QINCE_HOST':'https://qince-network-must-not-be-used.invalid'}):
                try:
                    jobs=[service.submit(_payload(verify_code=f'HX20261105000{i}',analyze_id=i))[0]
                          for i in (1,2)]
                    completed=[_wait_for_job(service,j['job_id'],'completed',timeout=30) for j in jobs]
                    self.assertEqual(len(processes),2)
                    self.assertEqual(len({p.pid for p in processes}),2)
                    self.assertTrue(all(p.returncode==0 for p in processes))
                    workspaces=[Path(j['result']['worktree']) for j in completed]
                    self.assertNotEqual(workspaces[0],workspaces[1])
                    for job,workspace in zip(completed,workspaces):
                        manifest=json.loads((workspace/'manifest.json').read_text(encoding='utf-8'))
                        self.assertEqual(manifest['run_id'],job['run_id'])
                        self.assertEqual(manifest['status'],'completed')
                        self.assertTrue((workspace/'snapshot.json').is_file())
                        self.assertTrue((workspace/'ai-analysis-summary.md').is_file())
                        events=[json.loads(p.read_text(encoding='utf-8')) for p in
                                (workspace/'analysis/execution-trace').glob('step-*.json')]
                        self.assertFalse(any(e.get('type')=='qince.request' for e in events))
                        self.assertTrue(any(e.get('type')=='qince.skipped' for e in events))
                        self.assertEqual(job['callback']['status'],'delivered')
                    self.assertEqual(sorted(p['analyzeId'] for p in callbacks),[1,2])
                finally:
                    service.close();service._download_worker.join(5)
                    for worker in service._audit_workers:worker.join(30)
                    self.assertTrue(all(not worker.is_alive() for worker in service._audit_workers))

    def test_invalid_worker_counts_are_rejected(self):
        for value in (0, 3, -1, True, '2', 1.5, None):
            with self.subTest(value=value), self.assertRaises(OSSIntakeRequestError):
                replace(_config(), audit_workers=value)

    def test_two_workers_keep_third_queued_coalesce_duplicate_and_drain_on_close(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            lock = threading.Lock()
            both = threading.Event()
            release = threading.Event()
            calls = []
            callbacks = []
            active = peak = 0

            def runner(**kwargs):
                nonlocal active, peak
                with lock:
                    calls.append(kwargs)
                    active += 1
                    peak = max(peak, active)
                    if active == 2: both.set()
                try:
                    if not release.wait(5): raise AssertionError('test release timed out')
                    return {'run_id':kwargs['run_id'], 'workspace_id':kwargs['run_id'],
                            'status':'completed', 'producer_model':'codex', 'error_count':0}
                finally:
                    with lock: active -= 1

            def callback(payload, config):
                with lock: callbacks.append(dict(payload))
                return _fake_callback_sender(payload, config)

            service = OSSIntakeService(worktrees_root=root, input_root=root/'input',
                config=replace(_config(),audit_workers=2), downloader=_fake_downloader,
                runner=runner,url_validator=_no_network_url_validator,callback_sender=callback)
            try:
                jobs = [service.submit(_payload(verify_code=f'HX20261105000{i}',analyze_id=i))[0]
                        for i in (1,2,3)]
                self.assertTrue(both.wait(5), 'second audit must start while first is blocked')
                _wait_for_job(service,jobs[2]['job_id'],'downloaded')
                with lock:
                    self.assertEqual(len(calls),2)
                    self.assertEqual(peak,2)
                    self.assertNotEqual(calls[0]['input_dir'],calls[1]['input_dir'])
                    self.assertNotEqual(calls[0]['run_id'],calls[1]['run_id'])
                    self.assertIsNot(calls[0]['cancel_event'],calls[1]['cancel_event'])
                duplicate, created = service.submit(_payload(verify_code='HX202611050001',analyze_id=1))
                self.assertFalse(created)
                self.assertEqual(duplicate['job_id'],jobs[0]['job_id'])
                service.close()
                with self.assertRaises(OSSIntakeUnavailableError):
                    service.submit(_payload(analyze_id=4))
                release.set()
                for job in jobs:
                    saved = _wait_for_job(service,job['job_id'],'completed')
                    self.assertEqual(saved['result']['run_id'],saved['run_id'])
                service._download_worker.join(5)
                for worker in service._audit_workers: worker.join(5)
                self.assertFalse(service._download_worker.is_alive())
                self.assertTrue(all(not worker.is_alive() for worker in service._audit_workers))
                self.assertEqual(len(calls),3)
                self.assertEqual(sorted(p['analyzeId'] for p in callbacks),[1,2,3])
                self.assertEqual(len(service._controls),0)
            finally:
                release.set();service.close()
                service._download_worker.join(5)
                for worker in service._audit_workers: worker.join(5)

    def test_failure_and_cancellation_do_not_stop_or_cancel_other_worker(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary); started=threading.Event();release=threading.Event();callbacks=[]
            def runner(**kwargs):
                if kwargs['verify_code']=='HX202611050001':
                    started.set()
                    if not kwargs['cancel_event'].wait(5):raise AssertionError('cancellation timed out')
                    return {}
                if kwargs['verify_code']=='HX202611050002':raise RuntimeError('isolated runner failure')
                if not release.wait(5):raise AssertionError('release timed out')
                return {'run_id':kwargs['run_id'],'status':'completed','producer_model':'codex'}
            def callback(payload,config):
                callbacks.append(dict(payload));return _fake_callback_sender(payload,config)
            service=OSSIntakeService(worktrees_root=root,input_root=root/'input',
                config=replace(_config(),audit_workers=2),downloader=_fake_downloader,
                runner=runner,url_validator=_no_network_url_validator,callback_sender=callback)
            try:
                first=service.submit(_payload(verify_code='HX202611050001',analyze_id=1))[0]
                self.assertTrue(started.wait(5))
                failed=service.submit(_payload(verify_code='HX202611050002',analyze_id=2))[0]
                _wait_for_job(service,failed['job_id'],'failed')
                third=service.submit(_payload(verify_code='HX202611050003',analyze_id=3))[0]
                _wait_for_job(service,third['job_id'],'running')
                service.delete(first['job_id'])
                self.assertEqual(service.get(third['job_id'])['status'],'running')
                release.set()
                _wait_for_job(service,third['job_id'],'completed')
                self.assertEqual(sorted(p['analyzeId'] for p in callbacks),[2,3])
            finally:
                release.set();service.close();service._download_worker.join(5)
                for worker in service._audit_workers:worker.join(5)

    def test_config_reports_actual_two_worker_pipeline(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            service=OSSIntakeService(worktrees_root=root,input_root=root/'input',
                config=replace(_config(),audit_workers=2))
            server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,
                catalog=WorkbenchCatalog(root),intake=service)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f'http://127.0.0.1:{server.server_port}/api/config') as response:
                    pipeline=json.load(response)['oss_intake']
                self.assertEqual(pipeline['audit_worker_count'],2)
                self.assertFalse(pipeline['single_audit_worker'])
                self.assertEqual(pipeline['pipeline'],'download_then_bounded_parallel_audit')
            finally:
                server.shutdown();server.server_close();thread.join(5)
                service.close();service._download_worker.join(5)
                for worker in service._audit_workers:worker.join(5)
