"""Blocking failures must reach diagnostics without replaying business work."""
import io
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.execution_diagnostics import diagnose_run_errors, list_reports, report_failure, set_run_context
from audit_core.execution_trace import technical_inventory
from audit_core.model_metrics import collect_model_artifacts
from audit_core.workbench_runtime import run_persistent_audit


ANSWER = {'category': 'environment', 'diagnosis': '商品查询命令失败，底层服务状态需要核实。',
          'evidence': ['数据库调用抛出异常，调用位置已记录。'],
          'operator_action': '检查Docker及商品库容器，确认只读查询正常。', 'developer_fix': None}


class Provider:
    def __init__(self, answer=None, error=None):
        self.answer = answer or ANSWER
        self.error = error
        self.calls = []

    def _call(self, root, skill, schema, prompt, **kwargs):
        self.calls.append((root, prompt.format(), kwargs))
        if self.error:
            raise self.error
        kwargs['validator'](self.answer)
        return self.answer


class ExecutionDiagnosticsTests(TestCase):
    def test_corrupt_trace_or_report_cannot_prevent_reporting_next_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            trace = root/'run/analysis/execution-trace'
            trace.mkdir(parents=True)
            (trace/'step-00000001.json').write_text('[]', encoding='utf-8')
            (trace/'step-00000002.json').write_text('{"type":"tool.failed","data":[]}', encoding='utf-8')
            fallback = root/'.intake/diagnostics'
            fallback.mkdir(parents=True)
            (fallback/'broken.json').write_text('[]', encoding='utf-8')
            report = report_failure(AuditError('工具失败'), stage='tool', worktrees=root,
                                    run_id='one', analysis=root/'run/analysis')
            self.assertIsNotNone(report)
            self.assertEqual(len(list_reports(root)), 1)

    def test_formal_model_adapter_records_one_diagnostic_call_and_usage(self):
        from audit_core.model_settings import api_selection
        from audit_core.pdf_materials import PdfEvidenceProvider
        connection = api_selection({'provider': 'zimaocloud', 'api_key': 'test-isolated-local'})
        provider = PdfEvidenceProvider(connection.model, connection.reasoning_effort, connection=connection)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            @diagnose_run_errors
            def run(run_id, **kwargs):
                set_run_context(analysis=root/'run/analysis', provider=provider)
                raise AuditError('商品数据库读取失败')
            with patch('audit_core.qwen_model.complete_json', return_value=(json.dumps(ANSWER),
                       {'usage': {'input_tokens': 42, 'output_tokens': 10, 'total_tokens': 52}})) as model:
                with self.assertRaises(AuditError):run('one', worktrees_root=root)
            model.assert_called_once()
            report = list_reports(root)[0]
            self.assertEqual(report['method'], 'execution_agent')
            metrics = [json.loads(line) for line in (root/'run/analysis/model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual(len(metrics), 1)
            self.assertEqual(metrics[0]['usage']['input_tokens'], 42)
            self.assertEqual(metrics[0]['usage']['output_tokens'], 10)

    def test_concurrent_runs_keep_diagnostic_provider_and_facts_isolated(self):
        from concurrent.futures import ThreadPoolExecutor
        with TemporaryDirectory() as directory:
            root = Path(directory)
            @diagnose_run_errors
            def run(run_id, **kwargs):
                provider = Provider({**ANSWER, 'diagnosis': '本单故障：' + run_id})
                set_run_context(analysis=root/run_id/'analysis', provider=provider)
                raise AuditError('仅属于' + run_id)
            def invoke(run_id):
                with self.assertRaises(AuditError):run(run_id, worktrees_root=root)
            with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(invoke, ['first', 'second']))
            reports = list_reports(root)
            self.assertEqual(len(reports), 2)
            for report in reports:
                self.assertIn(report['run_id'], report['diagnosis'])
                self.assertEqual(report['facts'][0]['message'], '仅属于' + report['run_id'])

    def test_all_runtime_failures_preserve_original_and_register_once(self):
        errors = [AuditError('商品数据库读取失败'), TimeoutError('勤策查询超时'),
                  FileNotFoundError('材料读取组件缺失'), PermissionError('结果不可写'),
                  MemoryError('内存不足'), TypeError('程序意外类型错误'),
                  subprocess.CalledProcessError(1, ['docker', 'password=do-not-record'])]
        for error in errors:
            with self.subTest(error=type(error).__name__), TemporaryDirectory() as directory:
                root = Path(directory)
                provider = Provider()
                with patch('audit_core.workbench_runtime.run_audit', side_effect=error) as business:
                    with self.assertRaises(type(error)) as raised:
                        run_persistent_audit('20261110-test', producer_model='codex', input_dir=root/'input',
                            worktrees_root=root, evidence_provider=provider, biz_type='条码费')
                self.assertIs(raised.exception, error)
                business.assert_called_once()
                reports = list_reports(root)
                self.assertEqual(len(reports), 1)
                report = reports[0]
                self.assertEqual(report['facts'][0]['error_type'], type(error).__name__)
                self.assertEqual(report['model_status'], 'unavailable_for_this_failure' if isinstance(error, MemoryError) else 'completed')
                self.assertEqual(len(provider.calls), 0 if isinstance(error, MemoryError) else 1)
                self.assertFalse(report['business_retried'])
                self.assertFalse(report['callback_sent'])
                self.assertNotIn('do-not-record', json.dumps(report))
                self.assertFalse(list(root.rglob('repair-items')))
                workspace = next(p.parent for p in root.glob('*/manifest.json'))
                self.assertEqual(json.loads((workspace/'manifest.json').read_text(encoding='utf-8'))['status'], 'failed')
                _, timeline = technical_inventory(workspace)
                diagnostic = next(e for e in timeline['steps'] if e['type'] == 'audit.execution_diagnostic.reported')
                self.assertIn('diagnosis', diagnostic['display_data'])

    def test_initialization_errors_are_reported_without_model_or_workspace(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            error = OSError('模型配置读取失败')
            with patch('audit_core.worker_handoff.wait_before_audit', side_effect=error):
                with self.assertRaises(OSError) as raised:
                    run_persistent_audit('bootstrap-test', producer_model='codex', worktrees_root=root)
            self.assertIs(raised.exception, error)
            report = list_reports(root)[0]
            self.assertEqual(report['stage'], 'initialization')
            self.assertEqual(report['model_status'], 'not_available')

    def test_agent_failure_is_not_recursively_retried_or_masked(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            error = AuditError('查库失败')
            provider = Provider(error=TimeoutError('诊断模型超时'))
            @diagnose_run_errors
            def run(run_id, **kwargs):
                set_run_context(analysis=root/'run/analysis', provider=provider)
                raise error
            with self.assertRaises(AuditError) as raised:
                run('one', worktrees_root=root)
            self.assertIs(raised.exception, error)
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(provider.calls[0][2]['max_attempts'], 1)
            report = list_reports(root)[0]
            self.assertEqual(report['model_status'], 'failed')
            self.assertEqual(report['diagnostic_error_type'], 'TimeoutError')

    def test_code_defect_stays_in_original_run_diagnostic(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = Provider({**ANSWER, 'category': 'program', 'diagnosis': '工具异常未进入通用诊断入口。',
                'developer_fix': '在运行边界覆盖工具异常，并用数据库命令失败做回归。'})
            @diagnose_run_errors
            def run(run_id, **kwargs):
                set_run_context(analysis=root/'run/analysis', provider=provider)
                raise AuditError('工具执行失败')
            with self.assertRaises(AuditError):run('one', worktrees_root=root)
            reports = list_reports(root)
            self.assertEqual(len(reports), 1)
            self.assertIn('developer_fix', reports[0])
            self.assertNotIn('repair_id', reports[0])
            self.assertFalse(list(root.rglob('repair-items')))

    def test_model_auth_or_quota_failure_does_not_call_same_broken_model(self):
        from audit_core.qwen_model import QwenRequestError
        with TemporaryDirectory() as directory:
            root = Path(directory)
            provider = Provider()
            @diagnose_run_errors
            def run(run_id, **kwargs):
                set_run_context(analysis=root/'run/analysis', provider=provider)
                raise QwenRequestError('余额不足', code='quota')
            with self.assertRaises(QwenRequestError):run('one', worktrees_root=root)
            self.assertFalse(provider.calls)
            self.assertEqual(list_reports(root)[0]['model_status'], 'unavailable_for_this_failure')

    def test_storage_failure_and_observer_failure_still_leave_original_diagnostic(self):
        for mode in ('primary_disk', 'observer'):
            with self.subTest(mode=mode), TemporaryDirectory() as directory:
                root = Path(directory)
                @diagnose_run_errors
                def run(run_id, **kwargs):
                    set_run_context(analysis=root/'run/analysis')
                    error = AuditError('原始故障')
                    if mode == 'primary_disk':
                        from audit_core.workbench_store import atomic_write_json
                        def write(path, value):
                            if 'model-observations' in str(path):raise OSError('只读')
                            atomic_write_json(path, value)
                        with patch('audit_core.workbench_store.atomic_write_json', side_effect=write):
                            report_failure(error, stage='execution')
                    else:
                        def observer(*args):raise MemoryError('刷新失败')
                        with collect_model_artifacts(root/'run/analysis', observer=observer):
                            report_failure(error, stage='execution')
                    raise error
                with self.assertRaisesRegex(AuditError, '原始故障'):run('one', worktrees_root=root)
                self.assertEqual(len(list_reports(root)), 1)

    def test_error_text_and_model_report_redact_credentials(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            error = AuditError('password=plain-secret api_key="key-secret" token=token-secret https://user:pass@host/a?sig=signed-secret Bearer bearer-secret')
            report = report_failure(error, stage='download', worktrees=root, run_id='one')
            text = json.dumps(report)
            for secret in ('plain-secret', 'key-secret', 'token-secret', 'signed-secret', 'bearer-secret', 'user:pass'):
                self.assertNotIn(secret, text)

    def test_failure_archive_error_cannot_replace_original_business_failure(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            original = AuditError('原始业务执行故障')
            with patch('audit_core.workbench_runtime.run_audit', side_effect=original), \
                 patch('audit_core.workbench_store.WorkbenchRunStore.fail', side_effect=OSError('磁盘不可写')):
                with self.assertRaises(AuditError) as raised:
                    run_persistent_audit('20261110-test', producer_model='codex', input_dir=root/'input',
                        worktrees_root=root, evidence_provider=object(), biz_type='条码费')
            self.assertIs(raised.exception, original)
            reports = list_reports(root)
            self.assertEqual({r['stage'] for r in reports}, {'execution', 'failure_archive'})

    def test_http_report_endpoint_filters_and_rejects_traversal(self):
        import urllib.error
        from tests.test_model_switching import ModelSettingsAPITests
        server = ModelSettingsAPITests()
        server.setUp()
        try:
            report_failure(TimeoutError('网络故障'), stage='callback', worktrees=server.root, run_id='one')
            with server.opener.open(server.base+'/api/execution-diagnostics') as response:
                self.assertEqual(json.load(response)['count'], 1)
            with server.opener.open(server.base+'/api/execution-diagnostics?workspace_id=test-run') as response:
                self.assertEqual(json.load(response)['count'], 0)
            with self.assertRaises(urllib.error.HTTPError) as error:
                server.opener.open(server.base+'/api/execution-diagnostics?workspace_id=..')
            error.exception.close()
        finally:
            server.doCleanups()


class IntakeDiagnosticTests(TestCase):
    def test_repeated_boundaries_for_one_failure_do_not_duplicate_reports(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            error = RuntimeError('worker故障')
            first = report_failure(error, stage='worker', worktrees=root, run_id='one')
            second = report_failure(error, stage='archive', worktrees=root, run_id='one')
            self.assertEqual(first['id'], second['id'])
            self.assertEqual(len(list_reports(root)), 1)

    def test_service_restart_reports_interrupted_jobs_without_replay(self):
        from audit_core.oss_intake import OSSIntakeStore
        from audit_core.workbench_store import atomic_write_json
        with TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root/'.intake/jobs'
            jobs.mkdir(parents=True)
            job_id = 'a'*24
            atomic_write_json(jobs/(job_id+'.json'), {'job_id':job_id, 'run_id':'interrupted', 'status':'running'})
            store = OSSIntakeStore(root/'.intake')
            job = store.get(job_id)
            self.assertEqual(job['status'], 'failed')
            self.assertEqual(job['execution_diagnostic']['stage'], 'service_recovery')
            self.assertEqual(len(list_reports(root)), 1)

    def test_download_runner_and_callback_failures_all_register(self):
        from audit_core.oss_intake import OSSIntakeService, OSSIntakeDownloadError, OSSIntakeCallbackError
        from tests.test_oss_intake import (_config, _payload, _fake_downloader,
            _fake_callback_sender, _no_network_url_validator, _wait_for_job)
        for stage in ('download', 'audit', 'callback'):
            with self.subTest(stage=stage), TemporaryDirectory() as directory:
                root = Path(directory)
                def downloader(*args):
                    if stage == 'download':raise OSSIntakeDownloadError('下载失败')
                    return _fake_downloader(*args)
                def runner(**kwargs):
                    if stage == 'audit':raise RuntimeError('runner崩溃')
                    return {'run_id': kwargs['run_id'], 'status': 'completed', 'scenarios': []}
                def callback(*args):
                    if stage == 'callback':raise OSSIntakeCallbackError('回调失败', attempts=1)
                    return _fake_callback_sender(*args)
                service = OSSIntakeService(worktrees_root=root, input_root=root/'input', config=_config(),
                    downloader=downloader, runner=runner, callback_sender=callback,
                    url_validator=_no_network_url_validator)
                try:
                    job, _ = service.submit(_payload())
                    failed = _wait_for_job(service, job['job_id'], 'failed')
                    self.assertIn('execution_diagnostic', failed)
                    self.assertEqual(failed['execution_diagnostic']['run_id'], job['run_id'])
                    self.assertGreaterEqual(len(list_reports(root)), 1)
                finally:
                    service.close()
