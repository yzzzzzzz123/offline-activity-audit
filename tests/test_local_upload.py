"""Local ZIP intake: real HTTP and queue, isolated files and no external service."""
from __future__ import annotations

import io
import json
from pathlib import Path
import stat
import sys
import tempfile
import threading
import time
import unittest
from urllib.parse import urlencode
import urllib.error
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'skills/orchestrate-offline-audit/scripts'))
from audit_core.oss_intake import OSSIntakeService, OSSIntakeConfig, OSSIntakeRequestError, OSSIntakeStore
from audit_core.workbench_server import WorkbenchHTTPServer, WorkbenchCatalog, Handler


def archive(name='合同.txt', content=b'isolated current materials'):
    output=io.BytesIO()
    with zipfile.ZipFile(output,'w') as pack:pack.writestr(zipfile.ZipInfo(name,date_time=(2026,1,1,0,0,0)),content)
    return output.getvalue()


class LocalUploadTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.calls=[];self.entered=threading.Event();self.release=threading.Event();self.release.set()
        self.downloads=[];self.callbacks=[]
        def downloader(*args,**kwargs):self.downloads.append(args);raise AssertionError('manual upload must not download OSS')
        def callback(*args,**kwargs):self.callbacks.append(args);raise AssertionError('manual upload must not callback')
        def runner(**kwargs):
            self.calls.append(kwargs);self.entered.set();self.release.wait(8)
            return {'run_id':kwargs['run_id'],'workspace_id':'isolated-result','status':'completed',
                    'worktree':str(self.root/'worktrees/isolated-result')}
        self.intake=OSSIntakeService(worktrees_root=self.root/'worktrees',input_root=self.root/'input',
            config=OSSIntakeConfig(allowed_hosts=('example.test',),callback_url='https://example.test/api/v1/ai/analyze/callback',
                                   offpeak_only=False),
             downloader=downloader,callback_sender=callback,runner=runner)
        self.server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,catalog=WorkbenchCatalog(self.root/'worktrees'),intake=self.intake)
        threading.Thread(target=self.server.serve_forever,daemon=True).start()
        self.base=f'http://127.0.0.1:{self.server.server_port}'
        self.client=urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self):
        self.release.set();self.server.shutdown();self.server.server_close()
        self.intake._download_worker.join(10)
        for worker in self.intake._audit_workers:worker.join(10)
        self.temp.cleanup()

    def upload(self,raw=None,*,identity='a'*24,kind='条码费',filename='资料.zip',token=True,origin=None,**extra):
        query={'requestId':identity,'bizType':kind,'filename':filename,**extra}
        request=urllib.request.Request(self.base+'/api/intake/upload?'+urlencode(query),data=archive() if raw is None else raw,
             headers={'Content-Type':'application/zip','X-Offline-Audit-Upload-Token':self.server.upload_token if token else '',
                      'Origin':origin or self.base},method='POST')
        try:response=self.client.open(request,timeout=15)
        except urllib.error.HTTPError as error:response=error
        with response:return response.status,json.loads(response.read())

    def wait(self,jid):
        for _ in range(150):
            job=self.intake.get(jid)
            if job['status'] in ('completed','failed'):return job
            time.sleep(.02)
        self.fail('isolated upload task did not finish')

    def test_upload_uses_explicit_type_official_runner_and_local_provenance_without_callback(self):
        code,result=self.upload();self.assertEqual(code,202,result)
        job=self.wait(result['job']['job_id']);self.assertEqual(job['status'],'completed',job)
        self.assertEqual(job['input_source'],'input');self.assertIsNone(job['verifyCode']);self.assertIsNone(job['analyzeId'])
        self.assertEqual(self.calls[0]['biz_type'],'条码费');self.assertEqual(self.calls[0]['scenario'],'entry_fee')
        self.assertEqual(self.calls[0]['input_source'],'input');self.assertIsNone(self.calls[0]['large_venue_fee'])
        self.assertFalse(job['callback']['required']);self.assertEqual(self.callbacks,[]);self.assertEqual(self.downloads,[])
        path=Path(job['delivery']['input_file']);self.assertEqual(path.read_bytes(),archive())
        self.assertTrue(path.is_relative_to(self.root/'input'/job['job_id']))

    def test_same_request_is_idempotent_even_after_completion_and_changed_body_is_rejected(self):
        _,first=self.upload();self.wait(first['job']['job_id'])
        code,again=self.upload();self.assertEqual(code,202);self.assertTrue(again['duplicate'])
        self.assertEqual(first['job']['job_id'],again['job']['job_id']);self.assertEqual(len(self.calls),1)
        self.assertEqual(self.upload(raw=archive(content=b'changed'))[0],409)

    def test_explicit_original_identity_reaches_formal_runner_without_enabling_callback(self):
        code, result = self.upload(verifyCode='HX-SYNTHETIC', analyzeId='8')
        self.assertEqual(code, 202, result)
        job = self.wait(result['job']['job_id'])
        self.assertEqual(job['verifyCode'], 'HX-SYNTHETIC')
        self.assertEqual(job['analyzeId'], 8)
        self.assertEqual(self.calls[0]['verify_code'], 'HX-SYNTHETIC')
        self.assertFalse(job['callback']['required'])
        self.assertEqual(job['callback']['attempts'], 0)
        self.assertEqual(self.callbacks, [])
        code, again = self.upload(verifyCode='HX-SYNTHETIC', analyzeId='8')
        self.assertEqual(code, 202, again)
        self.assertTrue(again['duplicate'])
        self.assertEqual(self.upload(verifyCode='HX-OTHER', analyzeId='8')[0], 409)
        self.assertEqual(self.upload(verifyCode='HX-SYNTHETIC', analyzeId='9')[0], 409)
        _, new = self.upload(identity='b'*24, verifyCode='HX-SYNTHETIC', analyzeId='8')
        self.assertNotEqual(new['job']['job_id'], job['job_id'])
        self.wait(new['job']['job_id'])
        self.assertEqual(len(self.calls), 2)

    def test_identity_is_explicit_never_inferred_from_archive_name(self):
        code, result = self.upload(filename='HX-SYNTHETIC.zip')
        self.assertEqual(code, 202, result)
        job = self.wait(result['job']['job_id'])
        self.assertIsNone(job['verifyCode'])
        self.assertIsNone(job['analyzeId'])
        self.assertNotIn('verify_code', self.calls[0])

    def test_invalid_optional_identity_is_rejected_before_any_queue_entry(self):
        for fields in ({'verifyCode': ''}, {'verifyCode': '中文'}, {'verifyCode': '../other'},
                       {'verifyCode': 'x'*81}, {'analyzeId': '0'}, {'analyzeId': '-1'},
                       {'analyzeId': '1.2'}, {'analyzeId': 'true'}, {'analyzeId': '9223372036854775808'},
                       {'callbackRequired': 'true'}):
            with self.subTest(fields=fields):
                self.assertEqual(self.upload(**fields)[0], 400)
        self.assertEqual(self.intake.list(), [])
        self.assertEqual(self.callbacks, [])

    def test_channel_selection_reaches_the_queue_and_formal_runner(self):
        for index, value in enumerate(('true', 'false', '1', 'True')):
            code, result = self.upload(identity=f'{index:024x}', cvsOtcChannel=value)
            self.assertEqual(code, 202, result)
            job = self.wait(result['job']['job_id'])
            expected = value == 'true'
            self.assertIs(job['cvsOtcChannel'], expected)
            self.assertIs(self.calls[-1]['cvs_otc_channel'], expected)

    def test_type_must_be_selected_before_reading_any_material(self):
        class Unreadable:
            def read(self,*args):raise AssertionError('must reject missing type before reading materials')
        with self.assertRaises(OSSIntakeRequestError):
            self.intake.submit_upload(Unreadable(),length=10,filename='资料.zip',biz_type='',request_id='a'*24)
        self.assertEqual(list((self.root/'input').iterdir()),[])

    def test_interrupted_local_upload_keeps_original_and_has_actionable_recovery(self):
        root=self.root/'isolated-recovery';root.mkdir()
        raw=root/'original.zip';raw.write_bytes(archive())
        receipts=root/'receipts';store=OSSIntakeStore(receipts)
        jid='c'*24
        (store.jobs_root/(jid+'.json')).write_text(json.dumps({'job_id':jid,'status':'running',
                    'input_source':'input','delivery':{'input_file':str(raw)}}),encoding='utf-8')
        recovered=OSSIntakeStore(receipts).get(jid)
        self.assertEqual(recovered['status'],'failed')
        self.assertIn('资料上传模块',recovered['failure']['message'])
        self.assertEqual(raw.read_bytes(),archive())

    def test_bad_archive_names_content_and_paths_never_create_a_job(self):
        invalid=[(b'not zip','资料.zip'),(archive(),'../资料.zip'),(archive(),'CON.zip'),
                 (archive('../outside.txt'),'资料.zip'),(archive('C:/outside.txt'),'资料.zip')]
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as pack:
            info=zipfile.ZipInfo('linked');info.create_system=3;info.external_attr=(stat.S_IFLNK|0o777)<<16;pack.writestr(info,'target')
        invalid.append((stream.getvalue(),'资料.zip'))
        for raw,name in invalid:
            with self.subTest(name=name):self.assertEqual(self.upload(raw=raw,filename=name)[0],400)
        self.assertEqual(self.intake.list(),[]);self.assertEqual(list((self.root/'input').iterdir()),[])

    def test_size_limit_and_same_origin_token_are_enforced(self):
        self.assertEqual(self.upload(token=False)[0],403)
        self.assertEqual(self.upload(origin='http://untrusted.test')[0],403)
        from dataclasses import replace
        self.intake.config=replace(self.intake.config,max_download_bytes=10)
        self.assertEqual(self.upload()[0],413)
        self.assertEqual(self.intake.list(),[])

    def test_optional_venue_flag_is_only_used_for_display_and_is_never_defaulted(self):
        self.assertEqual(self.upload(largeVenueFee='true')[0],400)
        code,result=self.upload(kind='陈列堆头',largeVenueFee='true')
        self.assertEqual(code,202,result);self.wait(result['job']['job_id'])
        self.assertIs(self.calls[0]['large_venue_fee'],True)

    def test_manual_uploads_share_one_serial_audit_worker_and_queued_deletion(self):
        self.release.clear();_,first=self.upload();self.assertTrue(self.entered.wait(3))
        _,second=self.upload(identity='b'*24)
        self.assertEqual(second['job']['status'],'downloaded');self.assertEqual(len(self.calls),1)
        # Cancel only this owned queued attempt; the first continues and its ZIP remains.
        self.intake.delete(second['job']['job_id'])
        self.release.set();self.wait(first['job']['job_id'])
        self.assertEqual(len(self.calls),1)
        self.assertTrue(Path(self.intake.get(first['job']['job_id'])['delivery']['input_file']).is_file())
        self.assertFalse((self.root/'input'/second['job']['job_id']).exists())


if __name__=='__main__':unittest.main()
