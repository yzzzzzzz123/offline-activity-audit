from __future__ import annotations

import contextlib
import errno
import hashlib
import io
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from audit_core.oss_intake import (
    FORMAL_RUNNER,
    OSSIntakeConfig,
    OSSIntakeCallbackError,
    OSSIntakeCancelledError,
    OSSIntakeConflictError,
    OSSIntakeDownloadError,
    OSSIntakeExecutionError,
    OSSIntakeRequestError,
    OSSIntakeService,
    OSSIntakeUnavailableError,
    _validate_download_url,
    build_callback_result,
    download_oss_object,
    normalize_submission,
    post_analysis_callback,
    public_job,
    run_formal_audit_subprocess,
)
from audit_core.workbench_runtime import main as runtime_main
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import atomic_write_json
from tests.pdf_test_support import fixture_cli_command


OSS_HOST = "audit-materials.oss-cn-hangzhou.aliyuncs.com"
CALLBACK_URL = "https://business.example/api/v1/ai/analyze/callback"
INTERNAL_CALLBACK_URL = "http://192.0.0.225:8699/api/v1/ai/analyze/callback"


def _zip_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("结算单.txt", "test")
    return stream.getvalue()


def _payload(
    *,
    verify_code: str = "HX202608310001",
    analyze_id: int = 123,
) -> dict:
    return {
        "verifyCode": verify_code,
        "analyzeId": analyze_id,
        "downloadUrl": (
            f"https://{OSS_HOST}/incoming/{quote('维护费用-20260831.zip')}"
            "?X-Oss-Signature=must-never-be-persisted"
        ),
    }


def _legacy_payload(*, event_id: str = "evt-20260831-001") -> dict:
    return {
        "event_id": event_id,
        "bucket": "audit-materials",
        "object_key": "incoming/维护费用-20260831.zip",
        "download_url": (
            f"https://{OSS_HOST}/incoming/%E7%BB%B4%E6%8A%A4%E8%B4%B9%E7%94%A8.zip"
            "?X-Oss-Signature=must-never-be-persisted"
        ),
        "etag": "test-etag",
        "business_date": "2026-08-31",
        "scenario": "maintenance_fee",
    }


def _config() -> OSSIntakeConfig:
    return OSSIntakeConfig(
        allowed_hosts=(OSS_HOST,),
        producer_model="codex",
        workbench_url="http://127.0.0.1:8080/",
        callback_url=CALLBACK_URL,
    )


def _no_network_url_validator(value: str, _config: OSSIntakeConfig) -> str:
    return value


def _fake_downloader(
    submission: dict,
    destination: Path,
    _config: OSSIntakeConfig,
) -> dict:
    value = _zip_bytes()
    destination.write_bytes(value)
    return {
        "bytes": len(value),
        "sha256": hashlib.sha256(value).hexdigest(),
        "etag": submission.get("etag"),
        "verified_at": "2026-08-31T00:00:00+00:00",
    }


def _fake_callback_sender(
    payload: dict,
    _config: OSSIntakeConfig,
) -> dict:
    if set(payload) != {"verifyCode", "analyzeId", "result"}:
        raise AssertionError(payload)
    return {
        "status": "delivered",
        "http_status": 200,
        "attempts": 1,
        "delivered_at": "2026-08-31T00:01:00+00:00",
    }


def _wait_for_job(
    service: OSSIntakeService,
    job_id: str,
    status: str,
    *,
    timeout: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = service.get(job_id)
        if job.get("status") == status:
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not reach {status}: {service.get(job_id)}")


class OSSIntakeValidationTests(unittest.TestCase):
    def test_analyze_id_requires_a_positive_64_bit_integer(self) -> None:
        for invalid in (None, True, False, "123", 1.5, 0, -1, 2**63):
            with self.subTest(analyze_id=invalid):
                with self.assertRaisesRegex(OSSIntakeRequestError, "analyzeId"):
                    normalize_submission(
                        {**_payload(), "analyzeId": invalid},
                        _config(),
                        url_validator=_no_network_url_validator,
                    )
        for valid in (1, 123, 2**63 - 1):
            with self.subTest(analyze_id=valid):
                submission = normalize_submission(
                    _payload(analyze_id=valid),
                    _config(),
                    url_validator=_no_network_url_validator,
                )
                self.assertEqual(submission["analyze_id"], valid)
                self.assertEqual(submission["analyzeId"], valid)
                self.assertNotIn("fileId", submission)
                self.assertEqual(submission["event_id"], f"HX202608310001:{valid}")

    def test_http_callback_requires_explicit_trusted_lan_opt_in(self) -> None:
        with self.assertRaisesRegex(OSSIntakeRequestError, "显式启用"):
            OSSIntakeConfig(
                allowed_hosts=(OSS_HOST,),
                callback_url=INTERNAL_CALLBACK_URL,
            )
        config = OSSIntakeConfig(
            allowed_hosts=(OSS_HOST,),
            callback_url=INTERNAL_CALLBACK_URL,
            allow_http_callback=True,
        )
        self.assertEqual(config.callback_url, INTERNAL_CALLBACK_URL)
        self.assertTrue(config.allow_http_callback)
        with self.assertRaises(OSSIntakeRequestError):
            OSSIntakeConfig(
                allowed_hosts=(OSS_HOST,),
                callback_url="http://192.0.0.225:8699/wrong-path",
                allow_http_callback=True,
            )

    def test_download_url_requires_https_exact_host_and_public_dns(self) -> None:
        config = _config()

        def public_resolver(*args, **kwargs):  # type: ignore[no-untyped-def]
            return [(2, 1, 6, "", ("8.8.8.8", 443))]

        accepted = _validate_download_url(
            f"https://{OSS_HOST}/incoming/material.zip?signature=secret",
            config,
            resolver=public_resolver,
        )
        self.assertIn("signature=secret", accepted)
        with self.assertRaises(OSSIntakeRequestError):
            _validate_download_url(
                f"http://{OSS_HOST}/incoming/material.zip",
                config,
                resolver=public_resolver,
            )
        with self.assertRaises(OSSIntakeRequestError):
            _validate_download_url(
                "https://evil.example/incoming/material.zip",
                config,
                resolver=public_resolver,
            )

        def private_resolver(*args, **kwargs):  # type: ignore[no-untyped-def]
            return [(2, 1, 6, "", ("127.0.0.1", 443))]

        with self.assertRaises(OSSIntakeRequestError):
            _validate_download_url(
                f"https://{OSS_HOST}/incoming/material.zip",
                config,
                resolver=private_resolver,
            )

        invalid_run = _legacy_payload(event_id="invalid-run")
        invalid_run["run_id"] = "not-a-dated-run"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=config,
                downloader=_fake_downloader,
                runner=lambda **kwargs: {},
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                with self.assertRaises(OSSIntakeRequestError):
                    service.submit(invalid_run)
            finally:
                service.close()

    def test_downloader_verifies_size_etag_sha_and_removes_failed_partial(self) -> None:
        value = _zip_bytes()
        url = f"https://{OSS_HOST}/incoming/material.zip?signature=secret"

        class Response:
            def __init__(self) -> None:
                self.headers = {
                    "Content-Length": str(len(value)),
                    "ETag": '"test-etag"',
                }
                self.position = 0

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return False

            def geturl(self) -> str:
                return url

            def getcode(self) -> int:
                return 200

            def read(self, size: int) -> bytes:
                if self.position:
                    return b""
                self.position = len(value)
                return value

        class Opener:
            def __init__(self) -> None:
                self.responses = 0

            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                self.responses += 1
                return Response()

        submission = {
            "download_url": url,
            "size": len(value),
            "etag": "test-etag",
            "sha256": hashlib.sha256(value).hexdigest(),
        }
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "material.zip"
            with (
                mock.patch(
                    "audit_core.oss_intake._validate_download_url",
                    return_value=url,
                ),
                mock.patch(
                    "audit_core.oss_intake.urllib.request.build_opener",
                    return_value=Opener(),
                ),
            ):
                receipt = download_oss_object(submission, destination, _config())
            self.assertEqual(receipt["bytes"], len(value))
            self.assertEqual(receipt["sha256"], submission["sha256"])
            self.assertTrue(destination.is_file())

            failed_destination = Path(temporary) / "failed.zip"
            invalid = {**submission, "sha256": "0" * 64}
            with (
                mock.patch(
                    "audit_core.oss_intake._validate_download_url",
                    return_value=url,
                ),
                mock.patch(
                    "audit_core.oss_intake.urllib.request.build_opener",
                    return_value=Opener(),
                ),
                self.assertRaises(OSSIntakeDownloadError),
            ):
                download_oss_object(invalid, failed_destination, _config())
            self.assertFalse(failed_destination.exists())

    def test_download_bypasses_proxy_without_changing_global_proxy_or_redirect_rules(self) -> None:
        value = _zip_bytes()
        requests: list[str] = []

        class FixtureHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.path)
                if self.path == "/redirect.zip":
                    self.send_response(302)
                    self.send_header("Location", "https://unlisted.example/material.zip")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(value)))
                self.end_headers()
                self.wfile.write(value)

            def log_message(self, _format: str, *args: object) -> None:
                pass

        with HTTPServer(("127.0.0.1", 0), FixtureHandler) as server, socket.socket() as unused_proxy:
            unused_proxy.bind(("127.0.0.1", 0))
            proxy = f"http://127.0.0.1:{unused_proxy.getsockname()[1]}"
            poisoned_proxies = {"http": proxy, "https": proxy}
            base = f"http://127.0.0.1:{server.server_port}"
            url = base + "/material.zip"
            redirect_url = base + "/redirect.zip"
            config = OSSIntakeConfig(
                allowed_hosts=("127.0.0.1",),
                allow_private_hosts=True,
                download_timeout_seconds=1,
            )

            def fixture_validator(candidate: str, candidate_config: OSSIntakeConfig) -> str:
                # Only these local transport fixtures bypass HTTPS/443; redirected
                # URLs still pass through the production whitelist validator.
                if candidate in {url, redirect_url}:
                    return candidate
                return _validate_download_url(candidate, candidate_config)

            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                with tempfile.TemporaryDirectory() as temporary, mock.patch.dict(
                    os.environ,
                    {
                        "http_proxy": proxy,
                        "https_proxy": proxy,
                        "HTTP_PROXY": proxy,
                        "HTTPS_PROXY": proxy,
                        "no_proxy": "",
                        "NO_PROXY": "",
                    },
                ), mock.patch(
                    "urllib.request.getproxies", return_value=poisoned_proxies
                ), mock.patch(
                    "audit_core.oss_intake._validate_download_url",
                    side_effect=fixture_validator,
                ):
                    environment_before = dict(os.environ)
                    destination = Path(temporary) / "material.zip"
                    receipt = download_oss_object(
                        {"download_url": url}, destination, config
                    )
                    self.assertEqual(destination.read_bytes(), value)
                    self.assertEqual(receipt["sha256"], hashlib.sha256(value).hexdigest())
                    self.assertEqual(dict(os.environ), environment_before)
                    self.assertEqual(urllib.request.getproxies(), poisoned_proxies)
                    with self.assertRaises(urllib.error.URLError):
                        urllib.request.build_opener().open(url, timeout=1)
                    redirected_destination = Path(temporary) / "redirect.zip"
                    with self.assertRaisesRegex(OSSIntakeRequestError, "未在白名单"):
                        download_oss_object(
                            {"download_url": redirect_url}, redirected_destination, config
                        )
                    self.assertFalse(redirected_destination.exists())
                    self.assertEqual(requests, ["/material.zip", "/redirect.zip"])
                    self.assertEqual(dict(os.environ), environment_before)
            finally:
                server.shutdown()
                thread.join(timeout=5)

    def test_download_connection_errors_have_safe_specific_messages(self) -> None:
        url = f"https://{OSS_HOST}/material.zip?signature=must-stay-secret"
        sensitive_detail = f"private-proxy-password {url}"
        windows_refused = OSError(sensitive_detail)
        windows_refused.winerror = 10061  # type: ignore[attr-defined]
        cases = [
            (ConnectionRefusedError(sensitive_detail), "目标服务器拒绝连接"),
            (OSError(errno.ECONNREFUSED, sensitive_detail), "目标服务器拒绝连接"),
            (windows_refused, "目标服务器拒绝连接"),
            (TimeoutError(sensitive_detail), "连接超时"),
            (socket.timeout(sensitive_detail), "连接超时"),
            (ssl.SSLCertVerificationError(1, sensitive_detail), "TLS 证书校验失败"),
            (ssl.SSLError(1, sensitive_detail), "TLS 握手失败"),
            (socket.gaierror(socket.EAI_NONAME, sensitive_detail), "域名无法解析"),
            (ConnectionResetError(sensitive_detail), "连接被重置"),
        ]
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "material.zip"
            for reason, detail in cases:
                for wrapped in (True, False):
                    with self.subTest(reason=type(reason).__name__, wrapped=wrapped):
                        failure = urllib.error.URLError(reason) if wrapped else reason
                        opener = mock.Mock()
                        opener.open.side_effect = failure
                        with mock.patch(
                            "audit_core.oss_intake._validate_download_url", return_value=url
                        ), mock.patch(
                            "audit_core.oss_intake.urllib.request.build_opener", return_value=opener
                        ), self.assertRaises(OSSIntakeDownloadError) as caught:
                            download_oss_object({"download_url": url}, destination, _config())
                        self.assertEqual(str(caught.exception), f"OSS 下载连接失败：{detail}")
                        self.assertNotIn("must-stay-secret", str(caught.exception))
                        self.assertNotIn("private-proxy-password", str(caught.exception))
                        self.assertFalse(destination.exists())
                        opener.open.assert_called_once()

    def test_download_unknown_connection_error_keeps_safe_generic_message(self) -> None:
        url = f"https://{OSS_HOST}/material.zip?signature=must-stay-secret"
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(f"private-proxy-password {url}")
        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "audit_core.oss_intake._validate_download_url", return_value=url
        ), mock.patch(
            "audit_core.oss_intake.urllib.request.build_opener", return_value=opener
        ), self.assertRaises(OSSIntakeDownloadError) as caught:
            download_oss_object(
                {"download_url": url}, Path(temporary) / "material.zip", _config()
            )
        self.assertEqual(str(caught.exception), "OSS 下载连接失败")
        opener.open.assert_called_once()

    def test_compact_download_uses_content_disposition_zip_name(self) -> None:
        value = _zip_bytes()
        url = f"https://{OSS_HOST}/download/123?signature=secret"

        class Response:
            def __init__(self) -> None:
                self.headers = {
                    "Content-Length": str(len(value)),
                    "Content-Disposition": (
                        "attachment; filename*=UTF-8''"
                        + quote("维护费用-原始文件名.zip")
                    ),
                }
                self.position = 0

            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return False

            def geturl(self) -> str:
                return url

            def getcode(self) -> int:
                return 200

            def read(self, size: int) -> bytes:
                if self.position:
                    return b""
                self.position = len(value)
                return value

        class Opener:
            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                return Response()

        submission = {
            "contract": "oss_ai_v1",
            "download_url": url,
            "basename": "HX202603250014-123.zip",
            "size": None,
            "etag": None,
            "sha256": None,
        }
        with tempfile.TemporaryDirectory() as temporary:
            fallback = Path(temporary) / submission["basename"]
            with (
                mock.patch(
                    "audit_core.oss_intake._validate_download_url",
                    return_value=url,
                ),
                mock.patch(
                    "audit_core.oss_intake.urllib.request.build_opener",
                    return_value=Opener(),
                ),
            ):
                receipt = download_oss_object(submission, fallback, _config())
            actual = Path(receipt["input_file"])
            self.assertEqual(actual.name, "维护费用-原始文件名.zip")
            self.assertTrue(actual.is_file())
            self.assertFalse(fallback.exists())

    def test_formal_subprocess_invokes_bundled_run_script_and_reads_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            worktrees = root / "worktrees"
            input_dir.mkdir()
            captured: list[str] = []

            def fake_subprocess_run(command, **kwargs):  # type: ignore[no-untyped-def]
                captured.extend(str(value) for value in command)
                result_path = Path(command[command.index("--result-json") + 1])
                captured.append(str(result_path))
                atomic_write_json(
                    result_path,
                    {
                        "run_id": "20260831-oss-test",
                        "workspace_id": "20260831-codex",
                        "status": "completed",
                    },
                )
                return subprocess.CompletedProcess(command, 0, stdout="")

            with mock.patch(
                "audit_core.oss_intake.subprocess.run",
                side_effect=fake_subprocess_run,
            ):
                result = run_formal_audit_subprocess(
                    run_id="20260831-oss-test",
                    producer_model="codex",
                    input_dir=input_dir,
                    worktrees_root=worktrees,
                    scenario="maintenance_fee",
                    workbench_url="http://127.0.0.1:8080/",
                )
            self.assertEqual(result["workspace_id"], "20260831-codex")
            self.assertIn(str(FORMAL_RUNNER), captured)
            self.assertIn("--input-dir", captured)
            self.assertEqual(captured[captured.index("--model") + 1], "gpt-6-astra")
            self.assertEqual(captured[captured.index("--reasoning-effort") + 1], "medium")
            self.assertEqual(captured[captured.index("--input-source") + 1], "oss")
            self.assertIn("--worktrees", captured)
            self.assertIn("--scenario", captured)
            self.assertFalse(Path(captured[-1]).exists())

    def test_formal_subprocess_returns_failed_classification_with_persisted_reason(self) -> None:
        native_run = subprocess.run

        def fixture_run(command, **kwargs):
            if len(command) > 2 and command[2] == str(FORMAL_RUNNER):
                command = fixture_cli_command(command[3:], candidates={0: []})
            return native_run(command, **kwargs)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "unknown.zip").write_bytes(_zip_bytes())
            with mock.patch("audit_core.oss_intake.subprocess.run", side_effect=fixture_run):
                result = run_formal_audit_subprocess(
                    run_id="20260911-classification-failed", producer_model="codex",
                    input_dir=inputs, worktrees_root=root / "worktrees",
                    scenario=None, workbench_url=None,
                )
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["failure"]["code"], "classification_failed")
            summary = build_callback_result(result)
            self.assertEqual(summary, "核销方式无法确认")

    def test_formal_subprocess_does_not_accept_unrelated_failure_receipt(self) -> None:
        def failed_run(command, **kwargs):
            atomic_write_json(Path(command[command.index("--result-json") + 1]), {
                "status": "failed", "workspace_id": "fixture",
                "failure": {"code": "model_failed", "message": "测试失败"},
            })
            return subprocess.CompletedProcess(command, 2, stdout="model failure")

        with tempfile.TemporaryDirectory() as temporary, mock.patch(
            "audit_core.oss_intake.subprocess.run", side_effect=failed_run,
        ), self.assertRaises(OSSIntakeExecutionError):
            run_formal_audit_subprocess(run_id="20260911-failed", producer_model="codex",
                                       input_dir=Path(temporary), worktrees_root=Path(temporary),
                                       scenario=None, workbench_url=None)

    def test_formal_subprocess_cancellation_terminates_its_process_tree(self) -> None:
        cancel_event = threading.Event()

        class Process:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self.communicate_calls = 0

            def communicate(self, timeout=None):  # type: ignore[no-untyped-def]
                self.communicate_calls += 1
                if self.communicate_calls == 1:
                    cancel_event.set()
                    raise subprocess.TimeoutExpired(["fixture"], timeout)
                return "", None

            def poll(self):  # type: ignore[no-untyped-def]
                return None

            def kill(self) -> None:
                self.returncode = -1

        process = Process()
        with tempfile.TemporaryDirectory() as temporary, (
            mock.patch("audit_core.oss_intake.subprocess.Popen", return_value=process)
        ), mock.patch("audit_core.oss_intake._terminate_formal_process") as terminate:
            root = Path(temporary)
            with self.assertRaises(OSSIntakeCancelledError):
                run_formal_audit_subprocess(
                    run_id="20260909-oss-cancel",
                    producer_model="codex",
                    input_dir=root,
                    worktrees_root=root / "worktrees",
                    scenario=None,
                    workbench_url=None,
                    cancel_event=cancel_event,
                )
        terminate.assert_called_once_with(process)

    def test_callback_result_uses_formal_snapshot_error_causes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "snapshot.json"
            atomic_write_json(
                snapshot,
                {
                    "view": {
                        "sheets": [
                            {
                                "name": "维护费用核销",
                                "headers": ["错误原因"],
                                "rows": [
                                    {
                                        "status": "issue",
                                        "heading": "POS电子表缺失",
                                        "values": ["未提交可读取的POS电子表"],
                                    },
                                    {
                                        "status": "issue",
                                        "heading": "合同盖章缺失",
                                        "values": ["合同未显示有效盖章"],
                                    },
                                ],
                            }
                        ]
                    }
                },
            )
            result = build_callback_result(
                {
                    "scenarios": ["maintenance_fee"],
                    "error_count": 2,
                    "snapshot": str(snapshot),
                }
            )
        self.assertEqual(
            result,
            "## 维护费用核销\n\n"
            "- 未提交可读取的POS电子表。\n"
            "- 合同未显示有效盖章。",
        )

    def test_callback_result_uses_markdown_from_same_formal_worktree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            worktree = Path(temporary) / "20260908_1200_00-gpt5.6sol_high"
            worktree.mkdir()
            summary = worktree / "ai-analysis-summary.md"
            expected = (
                "# AI 小结\n\n"
                "## 维护费用核销\n\n"
                "- **POS电子表缺失**：未提交可读取的POS电子表。\n"
                "- **合同盖章缺失**：合同未显示有效盖章。\n"
            )
            summary.write_text(expected, encoding="utf-8")
            result = build_callback_result(
                {
                    "worktree": str(worktree),
                    "analysis_summary": str(summary),
                    "scenarios": ["maintenance_fee"],
                    "error_count": 2,
                }
            )
        self.assertEqual(result, expected.strip())

    def test_callback_posts_exact_three_field_utf8_json(self) -> None:
        captured: dict = {}

        class Response:
            def __enter__(self):  # type: ignore[no-untyped-def]
                return self

            def __exit__(self, *args):  # type: ignore[no-untyped-def]
                return False

            def getcode(self) -> int:
                return 204

            def read(self, size: int) -> bytes:
                return b""

        class Opener:
            def open(self, request, timeout):  # type: ignore[no-untyped-def]
                captured["url"] = request.full_url
                captured["method"] = request.get_method()
                captured["body"] = json.loads(request.data.decode("utf-8"))
                captured["content_type"] = request.get_header("Content-type")
                captured["authorization"] = request.get_header("Authorization")
                captured["idempotency"] = request.get_header("Idempotency-key")
                captured["timeout"] = timeout
                return Response()

        config = OSSIntakeConfig(
            allowed_hosts=(OSS_HOST,),
            callback_url=CALLBACK_URL,
            callback_token="callback-test-token",
        )
        payload = {
            "verifyCode": "HX202603250014",
            "analyzeId": 123,
            "result": "分析结论：资料需补正",
        }
        with mock.patch(
            "audit_core.oss_intake.urllib.request.build_opener",
            return_value=Opener(),
        ):
            receipt = post_analysis_callback(payload, config)
        self.assertEqual(captured["url"], CALLBACK_URL)
        self.assertEqual(captured["method"], "POST")
        self.assertEqual(captured["body"], payload)
        self.assertEqual(captured["content_type"], "application/json; charset=utf-8")
        self.assertEqual(captured["authorization"], "Bearer callback-test-token")
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.assertEqual(
            captured["idempotency"],
            f"HX202603250014:123:{hashlib.sha256(encoded).hexdigest()[:16]}",
        )
        self.assertEqual(captured["timeout"], 30)
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["http_status"], 204)

    def test_callback_uses_direct_opener_without_reading_or_changing_proxy_settings(self) -> None:
        original_factory = urllib.request.build_opener
        created: list = []
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 204
        response.read.return_value = b""

        def create_opener(*handlers):  # type: ignore[no-untyped-def]
            opener = original_factory(*handlers)
            opener.open = mock.Mock(return_value=response)
            created.append((handlers, opener))
            return opener

        proxies = {
            "HTTP_PROXY": "http://127.0.0.1:10808",
            "HTTPS_PROXY": "http://127.0.0.1:10808",
            "http_proxy": "http://127.0.0.1:10808",
            "https_proxy": "http://127.0.0.1:10808",
            "ALL_PROXY": "http://127.0.0.1:10808",
            "OFFLINE_AUDIT_MODEL_PROXY": "http://127.0.0.1:7897",
        }
        payload = {"verifyCode": "HX202603250014", "analyzeId": 123, "result": "资料需补正"}
        with (
            mock.patch.dict(os.environ, proxies),
            mock.patch("audit_core.oss_intake.urllib.request.getproxies", side_effect=AssertionError("callback must not read proxy settings")),
            mock.patch("audit_core.oss_intake.urllib.request.build_opener", side_effect=create_opener),
        ):
            before = dict(os.environ)
            receipt = post_analysis_callback(payload, _config())
            self.assertEqual(dict(os.environ), before)
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(len(created), 1)
        handlers, opener = created[0]
        proxy = next(handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler))
        self.assertEqual(proxy.proxies, {})
        redirect = next(handler for handler in handlers if isinstance(handler, urllib.request.HTTPRedirectHandler))
        self.assertIsNone(redirect.redirect_request(urllib.request.Request(CALLBACK_URL), None, 302, "Found", {}, "https://unrelated.example/"))
        # The default HTTPS handler keeps the standard TLS verification path.
        self.assertTrue(any(isinstance(handler, urllib.request.HTTPSHandler) for handler in opener.handlers))
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data.decode("utf-8")), payload)
        self.assertEqual(request.get_method(), "POST")

    def test_direct_callback_retry_keeps_identical_payload_and_idempotency(self) -> None:
        requests = []
        response = mock.MagicMock()
        response.__enter__.return_value = response
        response.getcode.return_value = 204
        response.read.return_value = b""
        outcomes = iter([
            urllib.error.URLError("private-network-detail"),
            urllib.error.HTTPError(CALLBACK_URL, 503, "Unavailable", {}, None),
            response,
        ])

        def open_callback(request, timeout):  # type: ignore[no-untyped-def]
            requests.append(request)
            outcome = next(outcomes)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        opener = mock.Mock()
        opener.open.side_effect = open_callback
        payload = {"verifyCode": "HX202603250014", "analyzeId": 123, "result": "完整中文核销摘要"}
        with (
            mock.patch("audit_core.oss_intake.urllib.request.build_opener", return_value=opener),
            mock.patch("audit_core.oss_intake.time.sleep") as sleep,
        ):
            receipt = post_analysis_callback(payload, _config())
        self.assertEqual(receipt["attempts"], 3)
        self.assertEqual(receipt["http_status"], 204)
        self.assertEqual(len(requests), 3)
        self.assertEqual(len({request.data for request in requests}), 1)
        self.assertEqual(len({request.get_header("Idempotency-key") for request in requests}), 1)
        self.assertEqual(json.loads(requests[0].data.decode("utf-8")), payload)
        self.assertEqual(sleep.call_count, 2)

    def test_direct_callback_does_not_follow_redirect_or_retry_it(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(CALLBACK_URL, 302, "Found", {}, None)
        with (
            mock.patch("audit_core.oss_intake.urllib.request.build_opener", return_value=opener),
            mock.patch("audit_core.oss_intake.time.sleep") as sleep,
        ):
            with self.assertRaises(OSSIntakeCallbackError) as caught:
                post_analysis_callback({"verifyCode": "HX202603250014", "analyzeId": 123, "result": "资料需补正"}, _config())
        self.assertEqual(caught.exception.http_status, 302)
        self.assertEqual(caught.exception.attempts, 1)
        self.assertEqual(opener.open.call_count, 1)
        sleep.assert_not_called()

    def test_direct_callback_network_failure_retains_safe_error_and_attempts(self) -> None:
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError("https://callback-secret:token@private.example/")
        with (
            mock.patch("audit_core.oss_intake.urllib.request.build_opener", return_value=opener),
            mock.patch("audit_core.oss_intake.time.sleep"),
        ):
            with self.assertRaises(OSSIntakeCallbackError) as caught:
                post_analysis_callback({"verifyCode": "HX202603250014", "analyzeId": 123, "result": "资料需补正"}, _config())
        self.assertEqual(caught.exception.attempts, 3)
        self.assertIsNone(caught.exception.http_status)
        self.assertEqual(str(caught.exception), "分析结果回调失败：URLError")
        self.assertNotIn("callback-secret", str(caught.exception))


class OSSIntakeServiceTests(unittest.TestCase):
    def test_compact_contract_requires_callback_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = OSSIntakeConfig(
                allowed_hosts=(OSS_HOST,),
            )
            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=config,
                downloader=_fake_downloader,
                runner=lambda **kwargs: {},
                url_validator=_no_network_url_validator,
            )
            try:
                with self.assertRaises(OSSIntakeUnavailableError):
                    service.submit(_payload())
            finally:
                service.close()

    def test_compact_contract_allows_explicit_no_callback_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = OSSIntakeConfig(
                allowed_hosts=(OSS_HOST,),
                callback_required=False,
            )
            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=config,
                downloader=_fake_downloader,
                runner=lambda **kwargs: {
                    "run_id": kwargs["run_id"],
                    "workspace_id": "20260831-codex",
                    "producer_model": "codex",
                    "status": "completed",
                    "worktree": str(root / "20260831-codex"),
                    "workbench_url": "http://127.0.0.1:8080/?run=20260831-codex",
                },
                url_validator=_no_network_url_validator,
                callback_sender=lambda *_args, **_kwargs: self.fail(
                    "receive-only mode must not send a callback"
                ),
            )
            try:
                accepted, created = service.submit(_payload())
                self.assertTrue(created)
                completed = _wait_for_job(
                    service,
                    str(accepted["job_id"]),
                    "completed",
                )
                self.assertEqual(completed["callback"]["status"], "not_required")
                self.assertTrue(Path(completed["delivery"]["input_file"]).is_file())
            finally:
                service.close()

    def test_compact_contract_receive_only_skips_runner_and_callback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = OSSIntakeConfig(
                allowed_hosts=(OSS_HOST,),
                receive_only=True,
            )
            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=config,
                downloader=_fake_downloader,
                runner=lambda **_kwargs: self.fail(
                    "receive-only mode must not start an audit"
                ),
                url_validator=_no_network_url_validator,
                callback_sender=lambda *_args, **_kwargs: self.fail(
                    "receive-only mode must not send a callback"
                ),
            )
            try:
                accepted, created = service.submit(_payload())
                self.assertTrue(created)
                completed = _wait_for_job(
                    service,
                    str(accepted["job_id"]),
                    "completed",
                )
                self.assertIsNone(completed["result"])
                self.assertEqual(completed["callback"]["status"], "not_required")
                self.assertTrue(Path(completed["delivery"]["input_file"]).is_file())
            finally:
                service.close()

    def test_legacy_terminal_receipt_replays_as_second_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = OSSIntakeConfig(
                allowed_hosts=(OSS_HOST,),
                receive_only=True,
            )
            first_service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=config,
                downloader=_fake_downloader,
                runner=lambda **_kwargs: self.fail(
                    "receive-only mode must not start an audit"
                ),
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                first, created = first_service.submit(_payload())
                self.assertTrue(created)
                _wait_for_job(first_service, str(first["job_id"]), "completed")
            finally:
                first_service.close()

            receipt_path = root / ".intake" / "jobs" / f"{first['job_id']}.json"
            legacy_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            legacy_receipt["schema_version"] = "1.1"
            legacy_receipt["fileId"] = legacy_receipt.pop("analyzeId")
            old_identity = {
                "contract": "oss_ai_v1",
                "verifyCode": "HX202608310001",
                "fileId": 123,
                "url_identity": _payload()["downloadUrl"].split("?", 1)[0],
            }
            legacy_receipt["idempotency_fingerprint"] = hashlib.sha256(
                json.dumps(old_identity, ensure_ascii=False, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            legacy_receipt.pop("identity_job_id")
            legacy_receipt.pop("attempt")
            legacy_receipt.pop("supersedes_job_id")
            atomic_write_json(receipt_path, legacy_receipt)
            original_receipt = receipt_path.read_bytes()

            replay_service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=config,
                downloader=_fake_downloader,
                runner=lambda **_kwargs: self.fail(
                    "receive-only mode must not start an audit"
                ),
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                historical = public_job(replay_service.get(str(first["job_id"])))
                self.assertEqual(historical["analyzeId"], 123)
                self.assertNotIn("fileId", historical)
                replay, replay_created = replay_service.submit(_payload())
                self.assertTrue(replay_created)
                self.assertEqual(replay["analyzeId"], 123)
                self.assertNotIn("fileId", replay)
                self.assertEqual(replay["attempt"], 2)
                self.assertEqual(replay["supersedes_job_id"], first["job_id"])
                self.assertNotEqual(replay["job_id"], first["job_id"])
                _wait_for_job(replay_service, str(replay["job_id"]), "completed")
                self.assertEqual(
                    json.loads(receipt_path.read_text(encoding="utf-8"))[
                        "schema_version"
                    ],
                    "1.1",
                )
                self.assertEqual(receipt_path.read_bytes(), original_receipt)
                changed_object = _payload()
                changed_object["downloadUrl"] = f"https://{OSS_HOST}/another.zip"
                with self.assertRaises(OSSIntakeConflictError):
                    replay_service.submit(changed_object)
            finally:
                replay_service.close()

    def test_active_duplicate_is_coalesced_and_terminal_duplicate_reruns(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_started = threading.Event()
            release_runner = threading.Event()
            calls: list[dict] = []

            def runner(**kwargs):  # type: ignore[no-untyped-def]
                calls.append(kwargs)
                self.assertEqual(
                    [path.name for path in kwargs["input_dir"].iterdir()],
                    ["维护费用-20260831.zip"],
                )
                runner_started.set()
                self.assertTrue(release_runner.wait(timeout=5))
                return {
                    "run_id": kwargs["run_id"],
                    "workspace_id": "20260831-codex",
                    "producer_model": "codex",
                    "status": "completed",
                    "worktree": str(root / "20260831-codex"),
                    "workbench_url": "http://127.0.0.1:8080/?run=20260831-codex",
                }

            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=_config(),
                downloader=_fake_downloader,
                runner=runner,
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                first, created = service.submit(_payload())
                self.assertTrue(created)
                self.assertEqual(first["attempt"], 1)
                self.assertIsNone(first["supersedes_job_id"])
                self.assertTrue(runner_started.wait(timeout=5))
                duplicate, duplicate_created = service.submit(_payload())
                self.assertFalse(duplicate_created)
                self.assertEqual(duplicate["job_id"], first["job_id"])
                release_runner.set()
                completed = _wait_for_job(
                    service,
                    str(first["job_id"]),
                    "completed",
                )
                self.assertEqual(len(calls), 1)
                expected_input_dir = root / "input-oss" / str(first["job_id"])
                expected_input_file = expected_input_dir / "维护费用-20260831.zip"
                self.assertEqual(calls[0]["input_dir"], expected_input_dir)
                self.assertTrue(expected_input_file.is_file())
                self.assertEqual(
                    completed["delivery"]["input_directory"],
                    str(expected_input_dir),
                )
                self.assertEqual(
                    completed["delivery"]["input_file"],
                    str(expected_input_file),
                )
                self.assertEqual(completed["result"]["workspace_id"], "20260831-codex")
                rerun, rerun_created = service.submit(_payload())
                self.assertTrue(rerun_created)
                self.assertEqual(rerun["attempt"], 2)
                self.assertEqual(rerun["supersedes_job_id"], first["job_id"])
                self.assertNotEqual(rerun["job_id"], first["job_id"])
                self.assertNotEqual(rerun["run_id"], first["run_id"])
                rerun_completed = _wait_for_job(
                    service,
                    str(rerun["job_id"]),
                    "completed",
                )
                self.assertEqual(len(calls), 2)
                rerun_input_dir = root / "input-oss" / str(rerun["job_id"])
                rerun_input_file = rerun_input_dir / "维护费用-20260831.zip"
                self.assertEqual(calls[1]["input_dir"], rerun_input_dir)
                self.assertTrue(rerun_input_file.is_file())
                self.assertTrue(expected_input_file.is_file())
                self.assertEqual(
                    rerun_completed["delivery"]["input_file"],
                    str(rerun_input_file),
                )
                receipts = list((root / ".intake" / "jobs").glob("*.json"))
                self.assertEqual(len(receipts), 2)
                receipt_text = "\n".join(
                    path.read_text(encoding="utf-8") for path in receipts
                )
                self.assertNotIn("must-never-be-persisted", receipt_text)
                self.assertNotIn("download_url", receipt_text)

                conflict = _payload()
                conflict["downloadUrl"] = (
                    f"https://{OSS_HOST}/incoming/{quote('另一份维护费用.zip')}"
                )
                with self.assertRaises(OSSIntakeConflictError):
                    service.submit(conflict)
            finally:
                release_runner.set()
                service.close()

    def test_later_zip_downloads_while_first_audit_runs_and_audits_stay_serial(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_runner_started = threading.Event()
            second_downloaded = threading.Event()
            release_first_runner = threading.Event()
            call_lock = threading.Lock()
            download_order: list[int] = []
            audit_order: list[str] = []
            active_audits = 0
            maximum_active_audits = 0

            def downloader(
                submission: dict,
                destination: Path,
                config: OSSIntakeConfig,
            ) -> dict:
                download_order.append(int(submission["analyze_id"]))
                delivery = _fake_downloader(submission, destination, config)
                if int(submission["analyze_id"]) == 202:
                    second_downloaded.set()
                return delivery

            def runner(**kwargs):  # type: ignore[no-untyped-def]
                nonlocal active_audits, maximum_active_audits
                with call_lock:
                    active_audits += 1
                    maximum_active_audits = max(maximum_active_audits, active_audits)
                    audit_order.append(str(kwargs["run_id"]))
                    call_number = len(audit_order)
                try:
                    if call_number == 1:
                        first_runner_started.set()
                        self.assertTrue(release_first_runner.wait(timeout=5))
                    return {
                        "run_id": kwargs["run_id"],
                        "workspace_id": f"workspace-{call_number}",
                        "producer_model": "codex",
                        "status": "completed",
                        "scenarios": ["maintenance_fee"],
                        "error_count": 0,
                    }
                finally:
                    with call_lock:
                        active_audits -= 1

            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=_config(),
                downloader=downloader,
                runner=runner,
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                first, first_created = service.submit(
                    _payload(verify_code="HX202608310011", analyze_id=201)
                )
                self.assertTrue(first_created)
                self.assertTrue(first_runner_started.wait(timeout=5))

                second, second_created = service.submit(
                    _payload(verify_code="HX202608310012", analyze_id=202)
                )
                self.assertTrue(second_created)
                self.assertTrue(
                    second_downloaded.wait(timeout=5),
                    "第二个 ZIP 不应等待第一个正式核销结束才下载",
                )
                waiting = _wait_for_job(
                    service,
                    str(second["job_id"]),
                    "downloaded",
                )
                self.assertTrue(Path(waiting["delivery"]["input_file"]).is_file())
                self.assertIsNotNone(waiting["downloaded_at"])
                self.assertIsNone(waiting["audit_started_at"])
                self.assertEqual(len(audit_order), 1)
                self.assertEqual(download_order, [201, 202])

                service.close()
                with self.assertRaises(OSSIntakeUnavailableError):
                    service.submit(
                        _payload(verify_code="HX202608310013", analyze_id=203)
                    )
                release_first_runner.set()
                _wait_for_job(service, str(first["job_id"]), "completed")
                _wait_for_job(service, str(second["job_id"]), "completed")
                self.assertEqual(len(audit_order), 2)
                self.assertEqual(maximum_active_audits, 1)
            finally:
                release_first_runner.set()
                service.close()

    def test_download_failure_does_not_block_the_next_zip(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            audited_run_ids: list[str] = []

            def downloader(
                submission: dict,
                destination: Path,
                config: OSSIntakeConfig,
            ) -> dict:
                if int(submission["analyze_id"]) == 301:
                    raise OSSIntakeDownloadError("OSS 下载失败：HTTP 403")
                return _fake_downloader(submission, destination, config)

            def runner(**kwargs):  # type: ignore[no-untyped-def]
                audited_run_ids.append(str(kwargs["run_id"]))
                return {
                    "run_id": kwargs["run_id"],
                    "workspace_id": "workspace-after-download-failure",
                    "producer_model": "codex",
                    "status": "completed",
                    "scenarios": ["maintenance_fee"],
                    "error_count": 0,
                }

            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=_config(),
                downloader=downloader,
                runner=runner,
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                failed_job, _ = service.submit(
                    _payload(verify_code="HX202608310021", analyze_id=301)
                )
                next_job, _ = service.submit(
                    _payload(verify_code="HX202608310022", analyze_id=302)
                )
                failed = _wait_for_job(
                    service,
                    str(failed_job["job_id"]),
                    "failed",
                )
                completed = _wait_for_job(
                    service,
                    str(next_job["job_id"]),
                    "completed",
                )
                self.assertEqual(failed["failure"]["code"], "intake_failed")
                self.assertEqual(audited_run_ids, [completed["run_id"]])
            finally:
                service.close()

    def test_downloaded_queued_job_can_be_deleted_without_running_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first_started = threading.Event()
            release_first = threading.Event()
            audited: list[str] = []

            def runner(**kwargs):  # type: ignore[no-untyped-def]
                audited.append(str(kwargs["run_id"]))
                if len(audited) == 1:
                    first_started.set()
                    self.assertTrue(release_first.wait(timeout=5))
                return {
                    "run_id": kwargs["run_id"],
                    "workspace_id": f"workspace-{len(audited)}",
                    "producer_model": "codex",
                    "status": "completed",
                }

            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=_config(),
                downloader=_fake_downloader,
                runner=runner,
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                first, _ = service.submit(
                    _payload(verify_code="HX202608310031", analyze_id=401)
                )
                self.assertTrue(first_started.wait(timeout=5))
                queued, _ = service.submit(
                    _payload(verify_code="HX202608310032", analyze_id=402)
                )
                _wait_for_job(service, str(queued["job_id"]), "downloaded")
                self.assertEqual(
                    {job["job_id"] for job in service.list(active_only=True)},
                    {first["job_id"], queued["job_id"]},
                )

                deleted = service.delete(str(queued["job_id"]))

                self.assertTrue(deleted["deleted"])
                self.assertTrue(deleted["canceled"])
                self.assertIsNone(deleted["workspace_id"])
                self.assertFalse((root / ".intake" / "jobs" / f"{queued['job_id']}.json").exists())
                self.assertFalse((root / "input-oss" / str(queued["job_id"])).exists())
                release_first.set()
                _wait_for_job(service, str(first["job_id"]), "completed")
                time.sleep(0.05)
                self.assertEqual(audited, [first["run_id"]])
            finally:
                release_first.set()
                service.close()

    def test_running_job_delete_cancels_runner_and_removes_owned_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            runner_started = threading.Event()
            callback_called = threading.Event()

            def runner(**kwargs):  # type: ignore[no-untyped-def]
                workspace_id = "20260909_1000_00-codex_high"
                workspace = root / workspace_id
                workspace.mkdir()
                atomic_write_json(
                    workspace / "manifest.json",
                    {
                        "workspace_id": workspace_id,
                        "run_id": kwargs["run_id"],
                        "status": "running",
                        "created_at": "2026-09-09T02:00:00+00:00",
                    },
                )
                runner_started.set()
                self.assertTrue(kwargs["cancel_event"].wait(timeout=5))
                raise OSSIntakeCancelledError("投递任务已取消")

            def callback_sender(*_args, **_kwargs):  # type: ignore[no-untyped-def]
                callback_called.set()
                return {}

            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=_config(),
                downloader=_fake_downloader,
                runner=runner,
                url_validator=_no_network_url_validator,
                callback_sender=callback_sender,
            )
            try:
                job, _ = service.submit(
                    _payload(verify_code="HX202609090001", analyze_id=501)
                )
                self.assertTrue(runner_started.wait(timeout=5))

                deleted = service.delete(str(job["job_id"]))

                self.assertTrue(deleted["deleted"])
                self.assertTrue(deleted["canceled"])
                self.assertEqual(
                    deleted["workspace_id"],
                    "20260909_1000_00-codex_high",
                )
                self.assertFalse((root / "20260909_1000_00-codex_high").exists())
                self.assertFalse((root / "input-oss" / str(job["job_id"])).exists())
                with self.assertRaises(FileNotFoundError):
                    service.get(str(job["job_id"]))
                self.assertFalse(callback_called.is_set())
            finally:
                service.close()

    def test_verified_oss_zip_is_retained_when_audit_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def failing_runner(**kwargs):  # type: ignore[no-untyped-def]
                raise RuntimeError("deterministic test failure")

            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=_config(),
                downloader=_fake_downloader,
                runner=failing_runner,
                url_validator=_no_network_url_validator,
                callback_sender=_fake_callback_sender,
            )
            try:
                accepted, created = service.submit(
                    _payload(
                        verify_code="HX202608310002",
                        analyze_id=124,
                    )
                )
                self.assertTrue(created)
                failed = _wait_for_job(service, str(accepted["job_id"]), "failed")
                source = (
                    root
                    / "input-oss"
                    / str(accepted["job_id"])
                    / "维护费用-20260831.zip"
                )
                self.assertTrue(source.is_file())
                self.assertEqual(failed["delivery"]["input_file"], str(source))
                self.assertEqual(failed["failure"]["code"], "intake_failed")
                rerun, rerun_created = service.submit(
                    _payload(
                        verify_code="HX202608310002",
                        analyze_id=124,
                    )
                )
                self.assertTrue(rerun_created)
                self.assertEqual(rerun["attempt"], 2)
                self.assertEqual(rerun["supersedes_job_id"], accepted["job_id"])
                self.assertNotEqual(rerun["job_id"], accepted["job_id"])
                _wait_for_job(service, str(rerun["job_id"]), "failed")
            finally:
                service.close()

    def test_completed_analysis_is_retained_when_callback_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def runner(**kwargs):  # type: ignore[no-untyped-def]
                return {
                    "run_id": kwargs["run_id"],
                    "workspace_id": "20260831-codex",
                    "producer_model": "codex",
                    "status": "completed",
                    "worktree": str(root / "20260831-codex"),
                    "scenarios": ["maintenance_fee"],
                    "error_count": 1,
                }

            def failing_callback(
                payload: dict,
                config: OSSIntakeConfig,
            ) -> dict:
                self.assertEqual(payload["verifyCode"], "HX202608310003")
                self.assertEqual(payload["analyzeId"], 125)
                self.assertEqual(config.callback_url, CALLBACK_URL)
                raise OSSIntakeCallbackError(
                    "分析结果回调失败：HTTP 500",
                    attempts=3,
                    http_status=500,
                )

            service = OSSIntakeService(
                worktrees_root=root,
                input_root=root / "input-oss",
                config=_config(),
                downloader=_fake_downloader,
                runner=runner,
                url_validator=_no_network_url_validator,
                callback_sender=failing_callback,
            )
            try:
                accepted, created = service.submit(
                    _payload(
                        verify_code="HX202608310003",
                        analyze_id=125,
                    )
                )
                self.assertTrue(created)
                failed = _wait_for_job(service, str(accepted["job_id"]), "failed")
                self.assertEqual(failed["failure"]["code"], "callback_failed")
                self.assertEqual(failed["result"]["status"], "completed")
                self.assertEqual(failed["callback"]["status"], "failed")
                self.assertEqual(failed["callback"]["attempts"], 3)
                self.assertEqual(failed["callback"]["http_status"], 500)
            finally:
                service.close()

    def test_runtime_cli_accepts_isolated_input_and_writes_result_receipt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            input_dir = root / "input"
            worktrees = root / "worktrees"
            result_path = root / "result.json"
            input_dir.mkdir()
            expected = {
                "run_id": "20260831-cli-adapter",
                "workspace_id": "20260831-codex",
                "status": "completed",
            }
            with (
                mock.patch(
                    "audit_core.workbench_runtime.run_persistent_audit",
                    return_value=expected,
                ) as run,
                contextlib.redirect_stdout(io.StringIO()),
            ):
                code = runtime_main(
                    [
                        "--run-id",
                        "20260831-cli-adapter",
                        "--producer-model",
                        "codex",
                        "--input-dir",
                        str(input_dir),
                        "--worktrees",
                        str(worktrees),
                        "--workbench-url",
                        "http://127.0.0.1:8080/",
                        "--result-json",
                        str(result_path),
                    ]
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(result_path.read_text(encoding="utf-8")), expected)
            self.assertEqual(Path(run.call_args.kwargs["input_dir"]), input_dir)
            self.assertEqual(Path(run.call_args.kwargs["worktrees_root"]), worktrees)


class OSSIntakeHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.callbacks: list[dict] = []

        def callback_sender(
            payload: dict,
            config: OSSIntakeConfig,
        ) -> dict:
            self.assertEqual(config.callback_url, CALLBACK_URL)
            self.callbacks.append(payload)
            return {
                "status": "delivered",
                "http_status": 204,
                "attempts": 1,
                "delivered_at": "2026-08-31T00:01:00+00:00",
            }

        self._callback_sender = callback_sender

        def runner(**kwargs):  # type: ignore[no-untyped-def]
            return {
                "run_id": kwargs["run_id"],
                "workspace_id": "20260831-codex",
                "producer_model": "codex",
                "status": "completed",
                "worktree": str(self.root / "20260831-codex"),
                "workbench_url": "http://127.0.0.1:8080/?run=20260831-codex",
            }

        self.intake = OSSIntakeService(
            worktrees_root=self.root,
            input_root=self.root / "input-oss",
            config=_config(),
            downloader=_fake_downloader,
            runner=runner,
            url_validator=_no_network_url_validator,
            callback_sender=self._callback_sender,
        )
        self.server = WorkbenchHTTPServer(
            ("127.0.0.1", 0),
            Handler,
            catalog=WorkbenchCatalog(self.root),
            intake=self.intake,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _request(self, path: str) -> urllib.request.Request:
        return urllib.request.Request(self.base + path)

    def test_http_rejects_file_id_and_missing_analyze_id(self) -> None:
        missing_id = _payload()
        missing_id.pop("analyzeId")
        for payload in (
            missing_id,
            {**missing_id, "fileId": 123},
            {**_payload(), "fileId": 999},
        ):
            with self.subTest(fields=sorted(payload)):
                request = urllib.request.Request(
                    self.base + "/api/intake/oss",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    self.opener.open(request, timeout=5)
                with caught.exception as response:
                    self.assertEqual(response.code, 400)
                    self.assertIn("analyzeId", response.read().decode("utf-8"))
        self.assertEqual(self.intake.list(), [])
        self.assertEqual(self.callbacks, [])

    def test_unauthenticated_post_runs_job_and_status_is_queryable(self) -> None:
        body = json.dumps(_payload(), ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(
            self.base + "/api/intake/oss",
            data=body,
            headers={
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 202)
            accepted = json.load(response)
        self.assertFalse(accepted["duplicate"])
        self.assertFalse(accepted["rerun"])
        self.assertEqual(accepted["job"]["attempt"], 1)
        self.assertNotIn("identity_job_id", accepted["job"])
        job_id = str(accepted["job"]["job_id"])
        deadline = time.monotonic() + 5
        job: dict = {}
        while time.monotonic() < deadline:
            with self.opener.open(
                self._request(f"/api/intake/jobs/{job_id}"),
                timeout=5,
            ) as response:
                job = json.load(response)["job"]
            if job.get("status") == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["workspace_id"], "20260831-codex")
        self.assertEqual(job["verifyCode"], "HX202608310001")
        self.assertEqual(job["analyzeId"], 123)
        self.assertNotIn("fileId", job)
        self.assertEqual(job["callback"]["status"], "delivered")
        self.assertEqual(job["callback"]["http_status"], 204)
        self.assertEqual(
            self.callbacks,
            [
                {
                    "verifyCode": "HX202608310001",
                    "analyzeId": 123,
                    "result": "未发现错误。",
                }
            ],
        )
        self.assertNotIn("idempotency_fingerprint", job)
        self.assertNotIn("identity_job_id", job)

        replay_request = urllib.request.Request(
            self.base + "/api/intake/oss",
            data=body,
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with self.opener.open(replay_request, timeout=5) as response:
            self.assertEqual(response.status, 202)
            replay = json.load(response)
        self.assertFalse(replay["duplicate"])
        self.assertTrue(replay["rerun"])
        self.assertEqual(replay["job"]["attempt"], 2)
        self.assertEqual(replay["job"]["supersedes_job_id"], job_id)
        self.assertNotEqual(replay["job"]["job_id"], job_id)
        replay_job = _wait_for_job(
            self.intake,
            str(replay["job"]["job_id"]),
            "completed",
        )
        self.assertEqual(replay_job["attempt"], 2)
        self.assertEqual(len(self.callbacks), 2)

        with self.opener.open(self.base + "/api/config", timeout=5) as response:
            config = json.load(response)
        self.assertEqual(config["api_version"], "1.44")
        self.assertEqual(config["material_problem_policy"], "require_exact_material_items_before_audit")
        self.assertTrue(config["oss_intake"]["enabled"])
        self.assertEqual(config["oss_intake"]["list_endpoint"], "/api/intake/jobs")
        self.assertEqual(
            config["oss_intake"]["operations_list_endpoint"],
            "/api/intake/jobs?completed=0",
        )
        self.assertTrue(config["oss_intake"]["deletion"]["writable"])
        self.assertTrue(config["oss_intake"]["deletion"]["active_allowed"])
        self.assertFalse(config["oss_intake"]["authenticated"])
        self.assertFalse(config["oss_intake"]["authentication_required"])
        self.assertFalse(config["oss_intake"]["single_worker"])
        self.assertEqual(
            config["oss_intake"]["pipeline"],
            "download_then_serial_audit",
        )
        self.assertTrue(config["oss_intake"]["download_before_audit"])
        self.assertEqual(config["oss_intake"]["download_worker_count"], 1)
        self.assertEqual(config["oss_intake"]["audit_worker_count"], 1)
        self.assertTrue(config["oss_intake"]["single_audit_worker"])
        self.assertEqual(
            config["oss_intake"]["terminal_replay_policy"],
            "new_attempt",
        )
        self.assertEqual(
            config["oss_intake"]["callback_result_format"],
            "scenario_error_facts_markdown",
        )
        self.assertEqual(config["oss_intake"]["callback_transport"], "direct")
        self.assertTrue(config["oss_intake"]["callback_configured"])
        self.assertTrue(config["oss_intake"]["callback_required"])
        self.assertFalse(config["oss_intake"]["allow_http_callback"])
        self.assertFalse(config["oss_intake"]["receive_only"])
        self.assertEqual(
            config["oss_intake"]["request_fields"],
            ["verifyCode", "analyzeId", "downloadUrl"],
        )
        self.assertEqual(config["oss_intake"]["input_directory"], "input-oss")

        with self.opener.open(self.base + "/api/intake/jobs", timeout=5) as response:
            listed = json.load(response)
        self.assertEqual(listed["count"], 2)
        self.assertEqual(
            {job["job_id"] for job in listed["jobs"]},
            {job_id, replay_job["job_id"]},
        )
        with self.opener.open(self.base + "/api/intake/jobs?active=1", timeout=5) as response:
            self.assertEqual(json.load(response), {"jobs": [], "count": 0})
        with self.opener.open(self.base + "/api/intake/jobs?completed=0", timeout=5) as response:
            self.assertEqual(json.load(response), {"jobs": [], "count": 0})

        self.intake.store.update(
            str(replay_job["job_id"]),
            status="failed",
            failure={"code": "fixture", "message": "测试失败记录"},
        )
        with self.opener.open(self.base + "/api/intake/jobs?completed=0", timeout=5) as response:
            incomplete = json.load(response)
        self.assertEqual(incomplete["count"], 1)
        self.assertEqual(incomplete["jobs"][0]["status"], "failed")
        self.assertEqual(incomplete["jobs"][0]["job_id"], replay_job["job_id"])
        with self.opener.open(self.base + "/api/intake/jobs?completed=1", timeout=5) as response:
            completed = json.load(response)
        self.assertEqual(completed["count"], 1)
        self.assertEqual(completed["jobs"][0]["job_id"], job_id)

        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.opener.open(
                self.base + "/api/intake/jobs?active=1&completed=0",
                timeout=5,
            )
        self.assertEqual(caught.exception.code, 400)
        caught.exception.close()

    def test_active_job_delete_endpoint_cancels_and_removes_the_attempt(self) -> None:
        runner_started = threading.Event()

        def runner(**kwargs):  # type: ignore[no-untyped-def]
            runner_started.set()
            self.assertTrue(kwargs["cancel_event"].wait(timeout=5))
            raise OSSIntakeCancelledError("投递任务已取消")

        self.intake.runner = runner
        job, _ = self.intake.submit(
            _payload(verify_code="HX202609090002", analyze_id=601)
        )
        job_id = str(job["job_id"])
        self.assertTrue(runner_started.wait(timeout=5))
        request = urllib.request.Request(
            self.base + f"/api/intake/jobs/{job_id}",
            data=json.dumps({"confirm_job_id": job_id}).encode("utf-8"),
            headers={
                "Content-Type": "application/json; charset=utf-8",
                "Origin": self.base,
                "X-Offline-Audit-Delete-Token": self.server.deletion_token,
            },
            method="DELETE",
        )
        with self.opener.open(request, timeout=10) as response:
            result = json.load(response)
        self.assertEqual(result["job_id"], job_id)
        self.assertTrue(result["deleted"])
        self.assertTrue(result["canceled"])
        with self.assertRaises(FileNotFoundError):
            self.intake.get(job_id)


if __name__ == "__main__":
    unittest.main()
