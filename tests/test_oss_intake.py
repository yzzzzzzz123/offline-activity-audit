from __future__ import annotations

import contextlib
import hashlib
import io
import json
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from unittest import mock
from urllib.parse import quote

from audit_core.oss_intake import (
    FORMAL_RUNNER,
    OSSIntakeConfig,
    OSSIntakeCallbackError,
    OSSIntakeConflictError,
    OSSIntakeDownloadError,
    OSSIntakeRequestError,
    OSSIntakeService,
    OSSIntakeUnavailableError,
    _validate_download_url,
    build_callback_result,
    download_oss_object,
    post_analysis_callback,
    run_formal_audit_subprocess,
)
from audit_core.workbench_runtime import main as runtime_main
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import atomic_write_json


SECRET = "test-webhook-secret-32-characters"
OSS_HOST = "audit-materials.oss-cn-hangzhou.aliyuncs.com"
CALLBACK_URL = "https://business.example/api/v1/ai/analyze/callback"


def _zip_bytes() -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("结算单.txt", "test")
    return stream.getvalue()


def _payload(
    *,
    verify_code: str = "HX202608310001",
    file_id: int = 123,
) -> dict:
    return {
        "verifyCode": verify_code,
        "fileId": file_id,
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
        webhook_secret=SECRET,
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
    if set(payload) != {"verifyCode", "fileId", "result"}:
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
            self.assertIn("--worktrees", captured)
            self.assertIn("--scenario", captured)
            self.assertFalse(Path(captured[-1]).exists())

    def test_callback_result_uses_formal_snapshot_note(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "snapshot.json"
            atomic_write_json(
                snapshot,
                {
                    "view": {
                        "sheets": [
                            {
                                "name": "维护费用核销",
                                "note": "当前结论：资料需补正；建议核销0元。",
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
            "核销分析完成，共1个场景，发现2个错误项。 "
            "维护费用核销：当前结论：资料需补正；建议核销0元。",
        )

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
            webhook_secret=SECRET,
            allowed_hosts=(OSS_HOST,),
            callback_url=CALLBACK_URL,
            callback_token="callback-test-token",
        )
        payload = {
            "verifyCode": "HX202603250014",
            "fileId": 123,
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
        self.assertEqual(captured["idempotency"], "HX202603250014:123")
        self.assertEqual(captured["timeout"], 30)
        self.assertEqual(receipt["status"], "delivered")
        self.assertEqual(receipt["http_status"], 204)


class OSSIntakeServiceTests(unittest.TestCase):
    def test_compact_contract_requires_callback_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = OSSIntakeConfig(
                webhook_secret=SECRET,
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

    def test_event_is_idempotent_and_signed_url_is_never_persisted(self) -> None:
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
                receipt_text = next((root / ".intake" / "jobs").glob("*.json")).read_text(
                    encoding="utf-8"
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
                        file_id=124,
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
                self.assertEqual(payload["fileId"], 125)
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
                        file_id=125,
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

    def _authorized(self, path: str) -> urllib.request.Request:
        return urllib.request.Request(
            self.base + path,
            headers={"Authorization": f"Bearer {SECRET}"},
        )

    def test_authenticated_post_runs_job_and_status_is_queryable(self) -> None:
        body = json.dumps(_payload(), ensure_ascii=False).encode("utf-8")
        unauthorized = urllib.request.Request(
            self.base + "/api/intake/oss",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.opener.open(unauthorized, timeout=5)
        self.assertEqual(raised.exception.code, 401)
        raised.exception.close()

        request = urllib.request.Request(
            self.base + "/api/intake/oss",
            data=body,
            headers={
                "Authorization": f"Bearer {SECRET}",
                "Content-Type": "application/json; charset=utf-8",
            },
            method="POST",
        )
        with self.opener.open(request, timeout=5) as response:
            self.assertEqual(response.status, 202)
            accepted = json.load(response)
        job_id = str(accepted["job"]["job_id"])
        deadline = time.monotonic() + 5
        job: dict = {}
        while time.monotonic() < deadline:
            with self.opener.open(
                self._authorized(f"/api/intake/jobs/{job_id}"),
                timeout=5,
            ) as response:
                job = json.load(response)["job"]
            if job.get("status") == "completed":
                break
            time.sleep(0.02)
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["result"]["workspace_id"], "20260831-codex")
        self.assertEqual(job["verifyCode"], "HX202608310001")
        self.assertEqual(job["fileId"], 123)
        self.assertEqual(job["callback"]["status"], "delivered")
        self.assertEqual(job["callback"]["http_status"], 204)
        self.assertEqual(
            self.callbacks,
            [
                {
                    "verifyCode": "HX202608310001",
                    "fileId": 123,
                    "result": "核销分析完成，共1个场景，未发现阻断问题。",
                }
            ],
        )
        self.assertNotIn("idempotency_fingerprint", job)

        with self.opener.open(self.base + "/api/config", timeout=5) as response:
            config = json.load(response)
        self.assertEqual(config["api_version"], "1.2")
        self.assertTrue(config["oss_intake"]["enabled"])
        self.assertTrue(config["oss_intake"]["single_worker"])
        self.assertTrue(config["oss_intake"]["callback_configured"])
        self.assertEqual(
            config["oss_intake"]["request_fields"],
            ["verifyCode", "fileId", "downloadUrl"],
        )
        self.assertEqual(config["oss_intake"]["input_directory"], "input-oss")


if __name__ == "__main__":
    unittest.main()
