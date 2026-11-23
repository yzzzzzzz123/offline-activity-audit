"""Fault injection uses temporary archives and blocked external transports only."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
from unittest import mock

import httpx

from audit_core.common import AuditError
from audit_core.customer_language import SYSTEM_FAILURE_MESSAGE
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_settings import ModelSettingsStore, api_selection
from audit_core.oss_intake import (
    FORMAL_RUNNER, OSSIntakeCancelledError,
    OSSIntakeService, post_analysis_callback,
)
from audit_core.qwen_model import QwenRequestError, complete_json, extract_qwen_json
from audit_core.run_temporary import _scope_hash
from tests.pdf_test_support import archive_bytes
from tests.test_oss_intake import (
    _config, _fake_downloader, _no_network_url_validator, _payload, _wait_for_job,
)


class ModelFailureCallbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="offline-audit-retry-test-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selection = api_selection({"provider": "ppio", "api_key": "sk_synthetic-never-send"})
        self.schema = self.root / "schema.json"
        self.schema.write_text(json.dumps({
            "type": "object", "properties": {"ok": {"const": True}},
            "required": ["ok"], "additionalProperties": False,
        }), encoding="utf-8")
        # Any accidental remote model access fails the test immediately.
        blocker = mock.patch("audit_core.qwen_model.httpx.AsyncClient",
                             side_effect=AssertionError("External model access forbidden"))
        blocker.start()
        self.addCleanup(blocker.stop)

    def extract(self):
        return extract_qwen_json(connection=self.selection, root=self.root,
            skill=self.root, schema_path=self.schema, prompt="Synthetic retry fixture",
            images=[], label="隔离重试样本", validator=None)

    def test_transient_failure_retries_same_model_then_succeeds(self):
        for code in ("timeout", "rate_limit", "model_error", "invalid_output"):
            with self.subTest(code=code), mock.patch("audit_core.qwen_model.complete_json", side_effect=[
                QwenRequestError("模拟暂时错误", code, retryable=True),
                ('{"ok":true}', {"input_tokens": 12, "output_tokens": 5}),
            ]) as remote, mock.patch("audit_core.qwen_model.time.sleep") as sleep:
                self.assertEqual(self.extract(), {"ok": True})
                self.assertEqual(remote.call_count, 2)
                self.assertTrue(all(c.args[0] == self.selection for c in remote.call_args_list))
                sleep.assert_called_once_with(1)

    def test_invalid_json_and_schema_use_bounded_retries(self):
        with mock.patch("audit_core.qwen_model.complete_json", side_effect=[
            ("not-json", {}), ('{"ok":false}', {}), ('{"ok":true}', {}),
        ]) as remote, mock.patch("audit_core.qwen_model.time.sleep") as sleep:
            self.assertEqual(self.extract(), {"ok": True})
            self.assertEqual(remote.call_count, 3)
            self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
        with mock.patch("audit_core.qwen_model.complete_json", return_value=("not-json", {})) as remote, \
                mock.patch("audit_core.qwen_model.time.sleep") as sleep, \
                self.assertRaisesRegex(AuditError, "未生成核销结论"):
            try:
                self.extract()
            finally:
                self.assertEqual(remote.call_count, 3)
                self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])

    def test_exhaustion_stops_at_three_and_retains_attempt_metrics(self):
        with collect_model_artifacts(self.root / "analysis"), \
                mock.patch("audit_core.qwen_model.complete_json", side_effect=QwenRequestError(
                    "模拟超时", "timeout", retryable=True)) as remote, \
                mock.patch("audit_core.qwen_model.time.sleep") as sleep:
            with self.assertRaises(QwenRequestError):
                self.extract()
        self.assertEqual(remote.call_count, 3)
        self.assertEqual(sleep.call_args_list, [mock.call(1), mock.call(2)])
        metrics = [json.loads(line) for line in (self.root / "analysis/model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([v["attempt"] for v in metrics], [1, 2, 3])
        self.assertTrue(all(v["status"] == "failed" and v["error_code"] == "timeout" for v in metrics))

    def test_auth_quota_and_configuration_fail_without_repeating_unrepairable_request(self):
        for code in ("auth", "quota", "configuration"):
            with self.subTest(code=code), mock.patch("audit_core.qwen_model.complete_json", side_effect=
                    QwenRequestError("模拟不可重试错误", code)) as remote, \
                    mock.patch("audit_core.qwen_model.time.sleep") as sleep:
                with self.assertRaises(QwenRequestError):
                    self.extract()
                remote.assert_called_once()
                sleep.assert_not_called()

    def test_http_408_is_retryable_without_exposing_provider_body(self):
        # Override the blocker with a client constrained to an in-memory transport.
        # The original client is saved at module import, before the blocker.
        with mock.patch("audit_core.qwen_model.httpx.AsyncClient", _ASYNC_CLIENT):
            with self.assertRaises(QwenRequestError) as raised:
                complete_json(self.selection, [], system="fixture", transport=httpx.MockTransport(
                    lambda request: httpx.Response(408, text="private-provider-body")))
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.error_code, "timeout")
        self.assertNotIn("private-provider-body", str(raised.exception))

    def test_real_formal_runner_exhaustion_persists_and_callbacks_even_when_delivery_retries(self):
        for callback_succeeds in (True, False):
            with self.subTest(callback_succeeds=callback_succeeds):
                self._formal_failure_case(callback_succeeds)

    def _formal_failure_case(self, callback_succeeds):
        base = self.root / ("delivered" if callback_succeeds else "callback-failed")
        worktrees = base / "worktrees"
        ModelSettingsStore(worktrees).save(self.selection, expected_revision=0)
        # Exercise the actual run.py, runtime, PDF pipeline and model retry loop.
        # Only the remote completion and delay are replaced inside the child.
        child = (
            "import runpy,sys\n"
            f"sys.path.insert(0, {str(FORMAL_RUNNER.parent)!r})\n"
            "from unittest.mock import patch\n"
            "from audit_core.qwen_model import QwenRequestError\n"
            "with patch('audit_core.qwen_model.complete_json', side_effect=QwenRequestError('模拟模型超时', 'timeout', retryable=True)), patch('audit_core.qwen_model.time.sleep'), patch('audit_core.qince_context.QinceClient.from_environment', return_value=None), patch('httpx.AsyncClient', side_effect=AssertionError('External access forbidden')):\n"
            f"    runpy.run_path({str(FORMAL_RUNNER)!r}, run_name='__main__')\n"
        )
        original_popen = subprocess.Popen
        runner_calls, requests = [], []
        def launch(command, **kwargs):
            self.assertEqual(command[2], str(FORMAL_RUNNER))
            runner_calls.append(command)
            return original_popen([sys.executable, "-B", "-c", child, *command[3:]], **kwargs)

        def download(submission, destination, config):
            content = archive_bytes()
            destination.write_bytes(content)
            return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def getcode(self): return 200
            def read(self, *args): return b""

        service = None
        def send(payload, config):
            # The failure receipt and exact summary must exist before delivery.
            job = service.list()[0]
            self.assertEqual(job["status"], "callback")
            self.assertEqual(job["result"]["status"], "failed")
            saved = Path(job["result"]["analysis_summary"]).read_text(encoding="utf-8")
            self.assertEqual(payload, {"verifyCode": "HX202608310001", "analyzeId": 123, "result": saved})
            def open_request(request, **kwargs):
                requests.append(request)
                if callback_succeeds and len(requests) == 2:
                    return Response()
                raise urllib.error.HTTPError(config.callback_url, 503, "unavailable", {}, None)
            opener = mock.Mock()
            opener.open.side_effect = open_request
            with mock.patch("audit_core.oss_intake.urllib.request.build_opener", return_value=opener):
                return post_analysis_callback(payload, config)

        with mock.patch("audit_core.oss_intake.subprocess.Popen", side_effect=launch):
            service = OSSIntakeService(worktrees_root=worktrees, input_root=base / "input",
                config=_config(), downloader=download, url_validator=_no_network_url_validator,
                callback_sender=send)
            try:
                accepted, _ = service.submit(_payload())
                failed = _wait_for_job(service, accepted["job_id"], "failed", timeout=30)
            finally:
                self.close_service(service)
        self.assertEqual(len(runner_calls), 1)
        self.assertEqual(len(requests), 2 if callback_succeeds else 3, failed.get("failure"))
        self.assertTrue(all(r.data == requests[0].data for r in requests))
        self.assertTrue(all(r.get_header("Idempotency-key") == requests[0].get_header("Idempotency-key") for r in requests))
        self.assertEqual(json.loads(requests[0].data)["result"], SYSTEM_FAILURE_MESSAGE)
        self.assertEqual(failed["failure"]["code"], "audit_failed" if callback_succeeds else "callback_failed")
        self.assertEqual(failed["callback"]["status"], "delivered" if callback_succeeds else "failed")
        self.assertEqual(failed["result"]["status"], "failed")
        workspace = Path(failed["result"]["worktree"])
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(snapshot["run"]["status"], "failed")
        self.assertEqual(manifest["snapshot_sha256"], hashlib.sha256((workspace / "snapshot.json").read_bytes()).hexdigest())
        self.assertEqual(manifest["analysis_summary"]["sha256"], hashlib.sha256((workspace / "ai-analysis-summary.md").read_bytes()).hexdigest())
        self.assertTrue((workspace / "offline-activity-audit.html").is_file())
        self.assertIn('"run.failed"', (workspace / "logs/events.jsonl").read_text(encoding="utf-8"))
        metrics = [json.loads(line) for line in (workspace / "analysis/model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual([v["attempt"] for v in metrics], [1, 2, 3])
        self.assertTrue(Path(failed["delivery"]["input_file"]).is_file())
        self.assertFalse(list(Path(tempfile.gettempdir()).glob("oa-" + _scope_hash(worktrees) + "-*")))

    def test_failure_before_workspace_still_callbacks_and_retains_failed_status(self):
        callback = mock.Mock(return_value={"http_status": 200, "attempts": 1})
        runner = mock.Mock(side_effect=RuntimeError("synthetic startup failure"))
        service = OSSIntakeService(worktrees_root=self.root / "worktrees", input_root=self.root / "input",
            config=_config(), downloader=_fake_downloader, runner=runner,
            url_validator=_no_network_url_validator, callback_sender=callback)
        try:
            accepted, _ = service.submit(_payload())
            failed = _wait_for_job(service, accepted["job_id"], "failed")
            self.assertEqual(failed["callback"]["status"], "delivered")
            self.assertEqual(callback.call_args.args[0]["result"], SYSTEM_FAILURE_MESSAGE)
            self.assertEqual(failed["result"]["failure"]["code"], "audit_failed")
            runner.assert_called_once()
        finally:
            self.close_service(service)

    def test_cancellation_suppresses_failure_callback(self):
        callback = mock.Mock()
        ended = threading.Event()
        def cancel(**kwargs):
            kwargs["cancel_event"].set()
            ended.set()
            raise OSSIntakeCancelledError("模拟用户取消")
        service = OSSIntakeService(worktrees_root=self.root / "worktrees", input_root=self.root / "input",
            config=_config(), downloader=_fake_downloader, runner=cancel,
            url_validator=_no_network_url_validator, callback_sender=callback)
        try:
            service.submit(_payload())
            self.assertTrue(ended.wait(5))
        finally:
            self.close_service(service)
        callback.assert_not_called()

    def close_service(self, service):
        service.close()
        for worker in (service._download_worker, *service._audit_workers):
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


_ASYNC_CLIENT = httpx.AsyncClient
