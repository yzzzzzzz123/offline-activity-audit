"""A delivered claim is continued in its original run and answers by callback."""
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.failed_upload_resume import (ResumeEvidenceProvider, resume_failed_job,
                                              resume_local_upload)
from audit_core.qince_context import ENDPOINTS


JOB_ID = 'abcdef0123456789abcdef01'
CONNECTION = SimpleNamespace(provider='deepseek', model='deepseek-flash', reasoning_effort='high')


class FailedJobResumeCallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / '.intake/jobs').mkdir(parents=True)
        self.workspace = self.root / 'HX202610180011-搭赠-72-20261112_1708_01'
        trace = self.workspace / 'analysis/execution-trace'
        trace.mkdir(parents=True)
        (self.workspace / 'manifest.json').write_text(json.dumps({
            'run_id': '20261018-oss-700ebb069375', 'status': 'failed',
            'producer_model': 'deepseek', 'audit_model': 'deepseek-flash',
            'reasoning_effort': CONNECTION.reasoning_effort,
            'workbench_url': 'http://127.0.0.1:8080/'}), encoding='utf-8')
        # One pending stage: the interruption happened inside this request.
        events = [
            {'type': 'run.started', 'sequence': 1,
             'data': {'run_id': '20261018-oss-700ebb069375'}},
            {'type': 'ai.request', 'sequence': 2, 'call_id': 'pending-call',
             'label': '核销资料盘点 a001 · 第1次输入',
             'data': {'provider': 'deepseek', 'model': 'deepseek-flash',
                      'reasoning_effort': 'high', 'skill': 'skill', 'attempt': 1,
                      'input_text': 'pending', 'output_schema': {}, 'images': []}},
            {'type': 'ai.validation_failed', 'sequence': 3, 'call_id': 'pending-call',
             'label': '核销资料盘点 a001 · 第1次结果未通过校验',
             'data': {'attempt': 1}},
        ]
        for index, event in enumerate(events, 1):
            (trace / ('step-%04d.json' % index)).write_text(json.dumps(event), encoding='utf-8')
        self.trace = trace
        self.archive = self.root / 'input' / JOB_ID / 'claim.zip'
        self.archive.parent.mkdir(parents=True)
        self.archive.write_bytes(b'PK\x03\x04fixture')
        self.job_path = self.root / '.intake/jobs' / (JOB_ID + '.json')

    def add_qince_snapshot(self):
        (self.trace / 'step-0009.json').write_text(json.dumps({
            'type': 'qince.response', 'sequence': 9,
            'data': {'endpoint': ENDPOINTS['queryVerificationRegular'],
                     'rows': [{'verifyCode': 'HX202610180011'}]}}), encoding='utf-8')

    def write_job(self, **overrides):
        job = {'job_id': JOB_ID, 'run_id': '20261018-oss-700ebb069375', 'status': 'failed',
               'verifyCode': 'HX202610180011', 'analyzeId': 72, 'bizType': '搭赠',
               'largeVenueFee': None, 'cvsOtcChannel': False,
               'object': {'bucket': 'oss', 'object_key': 'offline-verify/x.zip'},
               'delivery': {'input_file': str(self.archive),
                            'sha256': hashlib.sha256(self.archive.read_bytes()).hexdigest()},
               'callback': {'required': True, 'status': 'pending', 'attempts': 0,
                            'http_status': None, 'delivered_at': None}}
        job.update(overrides)
        self.job_path.write_text(json.dumps(job), encoding='utf-8')
        return job

    def resume(self, *, sender=None, config=None):
        with patch('audit_core.model_settings.ModelSettingsStore.snapshot', return_value=CONNECTION), \
             patch('audit_core.workbench_runtime.run_persistent_audit',
                   return_value={'status': 'completed', 'worktree': str(self.workspace),
                                 'scenarios': ['giveaway_promotion'], 'error_count': 0}) as audit:
            result = resume_failed_job(JOB_ID, self.root, callback_sender=sender, callback_config=config)
        return result, audit

    def test_delivered_job_is_continued_and_answers_by_callback(self):
        self.write_job()
        self.add_qince_snapshot()
        sent = []
        result, audit = self.resume(sender=lambda payload, config: sent.append((payload, config))
                                    or {'attempts': 1, 'http_status': 200,
                                        'delivered_at': '2026-11-12T10:00:00+00:00'},
                                    config='callback-config')
        self.assertEqual(audit.call_count, 1)
        job = json.loads(self.job_path.read_text(encoding='utf-8'))
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(job['callback']['status'], 'delivered')
        self.assertEqual(job['callback']['http_status'], 200)
        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0][0]['verifyCode'], 'HX202610180011')
        self.assertEqual(sent[0][0]['analyzeId'], 72)
        self.assertTrue(sent[0][0]['result'].strip())
        self.assertEqual(sent[0][1], 'callback-config')

    def test_callback_is_not_sent_when_the_audit_fails_again(self):
        self.write_job()
        self.add_qince_snapshot()
        sent = []
        with patch('audit_core.model_settings.ModelSettingsStore.snapshot', return_value=CONNECTION), \
             patch('audit_core.workbench_runtime.run_persistent_audit',
                   side_effect=AuditError('模型未通过校验')):
            with self.assertRaises(AuditError):
                resume_failed_job(JOB_ID, self.root,
                                  callback_sender=lambda payload, config: sent.append(payload),
                                  callback_config='callback-config')
        job = json.loads(self.job_path.read_text(encoding='utf-8'))
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(job['callback']['status'], 'pending')
        self.assertEqual(sent, [])

    def test_callback_requirement_without_configuration_is_refused(self):
        self.write_job()
        self.add_qince_snapshot()
        with self.assertRaisesRegex(AuditError, '回调'):
            resume_failed_job(JOB_ID, self.root)

    def test_local_upload_entry_keeps_its_original_scope(self):
        self.write_job(input_source='input', object=None)
        with self.assertRaisesRegex(AuditError, '仅支持显式恢复本地上传'):
            resume_local_upload(JOB_ID, self.root)

    def test_changed_zip_is_never_reused(self):
        self.write_job(delivery={'input_file': str(self.archive), 'sha256': '0' * 64})
        self.add_qince_snapshot()
        with self.assertRaisesRegex(AuditError, '原ZIP'):
            self.resume(sender=lambda payload, config: {'attempts': 1}, config='callback-config')

    def test_missing_call_QC_snapshot_is_refused(self):
        self.write_job()
        with self.assertRaisesRegex(AuditError, '勤策快照'):
            self.resume(sender=lambda payload, config: {'attempts': 1}, config='callback-config')

    def test_killed_run_continues_at_its_unfinished_request(self):
        """A SIGKILL leaves no terminal event; the archive's interruption is the permission."""
        # Only the live request, no validation result: exactly the 69/70/71 trace.
        trace = [json.loads(path.read_text(encoding='utf-8'))
                 for path in sorted(self.trace.glob('step-*.json'))]
        trace = [event for event in trace if event['type'] != 'ai.validation_failed']
        for index, event in enumerate(trace, 1):
            (self.trace / ('step-%04d.json' % index)).write_text(json.dumps(event), encoding='utf-8')
        with self.assertRaisesRegex(AuditError, '未完成模型阶段'):
            ResumeEvidenceProvider(CONNECTION, trace)
        provider = ResumeEvidenceProvider(CONNECTION, trace, interrupted_run=True)
        self.assertEqual(provider.pending_label, '核销资料盘点 a001')
        (self.workspace / 'manifest.json').write_text(json.dumps({
            'run_id': '20261018-oss-700ebb069375', 'status': 'failed',
            'producer_model': 'deepseek', 'audit_model': 'deepseek-flash',
            'workbench_url': 'http://127.0.0.1:8080/',
            'reasoning_effort': CONNECTION.reasoning_effort,
            'failure': {'code': 'run_interrupted'}}), encoding='utf-8')
        self.write_job(failure={'code': 'service_restarted'})
        self.add_qince_snapshot()
        _, audit = self.resume(sender=lambda payload, config: {'attempts': 1}, config='callback-config')
        self.assertEqual(audit.call_count, 1)
        job = json.loads(self.job_path.read_text(encoding='utf-8'))
        self.assertEqual(job['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
