from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from email.message import Message
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.parse import quote, unquote, urljoin, urlparse
from zoneinfo import ZoneInfo

from .common import AuditError
from .orchestrator import (
    SCENARIO_ORDER,
    normalize_producer_model,
    normalize_run_id,
    output_date_from_run_id,
)
from .workbench_store import atomic_write_json, read_json_file, utc_now


PROJECT_ROOT = Path(__file__).resolve().parents[1]
FORMAL_RUNNER = (
    PROJECT_ROOT / "skills" / "orchestrate-offline-audit" / "scripts" / "run.py"
)
DEFAULT_OSS_INPUT_ROOT = PROJECT_ROOT / "input-oss"
DEFAULT_MAX_DOWNLOAD_BYTES = 1024 * 1024 * 1024
DEFAULT_DOWNLOAD_TIMEOUT_SECONDS = 60
DEFAULT_CALLBACK_TIMEOUT_SECONDS = 30
DEFAULT_CALLBACK_ATTEMPTS = 3
CALLBACK_PATH = "/api/v1/ai/analyze/callback"
MAX_CALLBACK_RESULT_CHARS = 20_000
MAX_REQUEST_BYTES = 64 * 1024
JOB_ID_PATTERN = re.compile(r"[a-f0-9]{24}")
EVENT_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,199}")
VERIFY_CODE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}")
SHA256_PATTERN = re.compile(r"[a-fA-F0-9]{64}")
ACTIVE_JOB_STATUSES = {"accepted", "downloading", "running", "callback"}


class OSSIntakeError(AuditError):
    """Base error for the authenticated OSS transport adapter."""


class OSSIntakeRequestError(OSSIntakeError):
    """The submitted event envelope is invalid."""


class OSSIntakeConflictError(OSSIntakeError):
    """An event ID was reused for a different object."""


class OSSIntakeUnavailableError(OSSIntakeError):
    """The intake queue cannot accept another job."""


class OSSIntakeDownloadError(OSSIntakeError):
    """The remote object could not be downloaded and verified."""


class OSSIntakeExecutionError(OSSIntakeError):
    """The bundled formal runner failed."""


class OSSIntakeCallbackError(OSSIntakeError):
    """The completed audit result could not be delivered to the caller."""

    def __init__(
        self,
        message: str,
        *,
        attempts: int = 0,
        http_status: int | None = None,
    ) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.http_status = http_status


def _normalized_host(value: str) -> str:
    raw = str(value or "").strip().rstrip(".").lower()
    if not raw or "://" in raw or "/" in raw or "@" in raw:
        raise OSSIntakeRequestError(f"OSS 允许域名无效：{value!r}")
    try:
        return raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise OSSIntakeRequestError(f"OSS 允许域名无效：{value!r}") from exc


@dataclass(frozen=True)
class OSSIntakeConfig:
    webhook_secret: str
    allowed_hosts: tuple[str, ...]
    producer_model: str = "codex"
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES
    download_timeout_seconds: int = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    allow_private_hosts: bool = False
    workbench_url: str | None = None
    callback_url: str | None = None
    callback_token: str | None = None
    callback_timeout_seconds: int = DEFAULT_CALLBACK_TIMEOUT_SECONDS
    callback_attempts: int = DEFAULT_CALLBACK_ATTEMPTS

    def __post_init__(self) -> None:
        secret = str(self.webhook_secret or "")
        if len(secret) < 16:
            raise OSSIntakeRequestError("OSS webhook 密钥至少需要 16 个字符")
        hosts = tuple(sorted({_normalized_host(value) for value in self.allowed_hosts}))
        if not hosts:
            raise OSSIntakeRequestError("启用 OSS 接口时必须配置至少一个精确下载域名")
        try:
            max_download_bytes = int(self.max_download_bytes)
            timeout_seconds = int(self.download_timeout_seconds)
            callback_timeout_seconds = int(self.callback_timeout_seconds)
            callback_attempts = int(self.callback_attempts)
        except (TypeError, ValueError) as exc:
            raise OSSIntakeRequestError("OSS 下载与回调配置必须是整数") from exc
        if not 1 <= max_download_bytes <= DEFAULT_MAX_DOWNLOAD_BYTES:
            raise OSSIntakeRequestError(
                "OSS 单文件下载上限必须在 1 字节到 1 GiB 之间"
            )
        if not 1 <= timeout_seconds <= 600:
            raise OSSIntakeRequestError("OSS 下载超时必须在 1 到 600 秒之间")
        if not 1 <= callback_timeout_seconds <= 120:
            raise OSSIntakeRequestError("OSS 结果回调超时必须在 1 到 120 秒之间")
        if not 1 <= callback_attempts <= 5:
            raise OSSIntakeRequestError("OSS 结果回调尝试次数必须在 1 到 5 之间")
        callback_url = str(self.callback_url or "").strip() or None
        if callback_url is not None:
            parsed_callback = urlparse(callback_url)
            if (
                parsed_callback.scheme.lower() != "https"
                or not parsed_callback.hostname
                or parsed_callback.username is not None
                or parsed_callback.password is not None
                or parsed_callback.fragment
                or parsed_callback.path.rstrip("/") != CALLBACK_PATH
            ):
                raise OSSIntakeRequestError(
                    f"OSS 结果回调必须是路径为 {CALLBACK_PATH} 的完整 HTTPS 地址"
                )
        callback_token = str(self.callback_token or "").strip() or None
        try:
            producer_model = normalize_producer_model(self.producer_model)
        except AuditError as exc:
            raise OSSIntakeRequestError(str(exc)) from exc
        object.__setattr__(self, "webhook_secret", secret)
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "producer_model", producer_model)
        object.__setattr__(self, "max_download_bytes", max_download_bytes)
        object.__setattr__(self, "download_timeout_seconds", timeout_seconds)
        object.__setattr__(self, "callback_url", callback_url)
        object.__setattr__(self, "callback_token", callback_token)
        object.__setattr__(
            self,
            "callback_timeout_seconds",
            callback_timeout_seconds,
        )
        object.__setattr__(self, "callback_attempts", callback_attempts)


def _validate_download_url(
    value: str,
    config: OSSIntakeConfig,
    *,
    resolver: Callable[..., Any] = socket.getaddrinfo,
) -> str:
    raw = str(value or "").strip()
    if not raw or len(raw) > 8192:
        raise OSSIntakeRequestError("download_url 不能为空且不得超过 8192 个字符")
    parsed = urlparse(raw)
    if parsed.scheme.lower() != "https":
        raise OSSIntakeRequestError("OSS 下载地址必须使用 HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise OSSIntakeRequestError("OSS 下载地址不得在 authority 中携带用户名或密码")
    if parsed.fragment:
        raise OSSIntakeRequestError("OSS 下载地址不得包含 URL fragment")
    try:
        port = parsed.port
    except ValueError as exc:
        raise OSSIntakeRequestError("OSS 下载地址端口无效") from exc
    if port not in {None, 443}:
        raise OSSIntakeRequestError("OSS 下载地址只允许 HTTPS 443 端口")
    if not parsed.path or parsed.path == "/":
        raise OSSIntakeRequestError("OSS 下载地址缺少对象路径")
    host = _normalized_host(parsed.hostname or "")
    if host not in config.allowed_hosts:
        raise OSSIntakeRequestError(f"OSS 下载域名未在白名单中：{host}")

    try:
        resolved = resolver(host, 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise OSSIntakeRequestError(f"OSS 下载域名无法解析：{host}") from exc
    addresses = {str(item[4][0]) for item in resolved if item and item[4]}
    if not addresses:
        raise OSSIntakeRequestError(f"OSS 下载域名没有可用地址：{host}")
    if not config.allow_private_hosts:
        for address in addresses:
            try:
                public = ipaddress.ip_address(address).is_global
            except ValueError as exc:
                raise OSSIntakeRequestError(
                    f"OSS 下载域名解析结果无效：{host}"
                ) from exc
            if not public:
                raise OSSIntakeRequestError(
                    f"OSS 下载域名解析到非公网地址：{host}；"
                    "如确需使用专有网络端点，请显式启用私网 OSS"
                )
    return raw


def _safe_object_basename(object_key: str) -> str:
    raw = str(object_key or "").strip()
    if not raw or len(raw) > 1024 or "\x00" in raw:
        raise OSSIntakeRequestError("object_key 不能为空且不得超过 1024 个字符")
    normalized = raw.replace("\\", "/")
    name = PurePosixPath(normalized).name
    if not name or name in {".", ".."} or len(name) > 180:
        raise OSSIntakeRequestError("OSS 对象文件名无效或过长")
    if any(character in '<>:"|?*' for character in name):
        raise OSSIntakeRequestError("OSS 对象文件名包含 Windows 不支持的字符")
    if name.endswith((" ", ".")):
        raise OSSIntakeRequestError("OSS 对象文件名不能以空格或点结尾")
    reserved = name.split(".", 1)[0].upper()
    if reserved in {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{index}" for index in range(1, 10)),
        *(f"LPT{index}" for index in range(1, 10)),
    }:
        raise OSSIntakeRequestError("OSS 对象文件名是 Windows 保留名称")
    if Path(name).suffix.lower() != ".zip":
        raise OSSIntakeRequestError("OSS 入站对象必须是 .zip 文件")
    return name


def _business_date(value: Any) -> str:
    if value is None or value == "":
        return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    if not isinstance(value, str):
        raise OSSIntakeRequestError("business_date 必须是字符串")
    raw = value.strip()
    compact = raw.replace("-", "")
    try:
        parsed = datetime.strptime(compact, "%Y%m%d")
    except ValueError as exc:
        raise OSSIntakeRequestError("business_date 必须是 YYYYMMDD 或 YYYY-MM-DD") from exc
    return parsed.strftime("%Y%m%d")


def _clean_optional_text(value: Any, label: str, *, limit: int) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise OSSIntakeRequestError(f"{label} 必须是字符串")
    raw = value.strip()
    if not raw or len(raw) > limit or any(ord(character) < 32 for character in raw):
        raise OSSIntakeRequestError(f"{label} 无效或过长")
    return raw


def _normalized_etag(value: Any) -> str | None:
    raw = _clean_optional_text(value, "etag", limit=256)
    if raw is None:
        return None
    if raw.startswith("W/"):
        raw = raw[2:]
    return raw.strip('"').strip()


def _url_identity(download_url: str) -> str:
    parsed = urlparse(download_url)
    host = _normalized_host(parsed.hostname or "")
    return f"https://{host}{parsed.path}"


def _business_date_from_verify_code(verify_code: str) -> str:
    match = re.search(r"\d{8}", verify_code)
    if match is not None:
        try:
            return datetime.strptime(match.group(0), "%Y%m%d").strftime("%Y%m%d")
        except ValueError:
            pass
    return _business_date(None)


def _url_basename_or_fallback(
    download_url: str,
    *,
    verify_code: str,
    file_id: int,
) -> tuple[str, str]:
    candidate = unquote(PurePosixPath(urlparse(download_url).path).name)
    try:
        return _safe_object_basename(candidate), "url"
    except OSSIntakeRequestError:
        return _safe_object_basename(f"{verify_code}-{file_id}.zip"), "fallback"


def _normalize_compact_submission(
    payload: dict[str, Any],
    config: OSSIntakeConfig,
    *,
    url_validator: Callable[[str, OSSIntakeConfig], str],
) -> dict[str, Any]:
    raw_verify_code = payload.get("verifyCode")
    if not isinstance(raw_verify_code, str):
        raise OSSIntakeRequestError("verifyCode 必须是字符串")
    verify_code = raw_verify_code.strip()
    if VERIFY_CODE_PATTERN.fullmatch(verify_code) is None:
        raise OSSIntakeRequestError(
            "verifyCode 必须为 1～80 位 ASCII 字母、数字或 ._:-"
        )

    raw_file_id = payload.get("fileId")
    if isinstance(raw_file_id, bool) or not isinstance(raw_file_id, int):
        raise OSSIntakeRequestError("fileId 必须是正整数")
    file_id = int(raw_file_id)
    if not 1 <= file_id <= 9_223_372_036_854_775_807:
        raise OSSIntakeRequestError("fileId 必须是 64 位正整数")

    raw_download_url = payload.get("downloadUrl")
    if not isinstance(raw_download_url, str):
        raise OSSIntakeRequestError("downloadUrl 必须是字符串")
    download_url = url_validator(raw_download_url, config)
    basename, basename_source = _url_basename_or_fallback(
        download_url,
        verify_code=verify_code,
        file_id=file_id,
    )
    parsed = urlparse(download_url)
    host = _normalized_host(parsed.hostname or "")
    object_key = unquote(parsed.path.lstrip("/")) or basename
    event_id = f"{verify_code}:{file_id}"
    business_date = _business_date_from_verify_code(verify_code)
    canonical = {
        "contract": "oss_ai_v1",
        "verifyCode": verify_code,
        "fileId": file_id,
        "url_identity": _url_identity(download_url),
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    job_id = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
    return {
        **canonical,
        "event_id": event_id,
        "verify_code": verify_code,
        "file_id": file_id,
        "bucket": host,
        "object_key": object_key,
        "basename": basename,
        "basename_source": basename_source,
        "download_url": download_url,
        "etag": None,
        "size": None,
        "sha256": None,
        "business_date": business_date,
        "scenario": None,
        "callback_required": True,
        "fingerprint": fingerprint,
        "job_id": job_id,
        "run_id": f"{business_date}-oss-{job_id[:12]}",
    }


def _normalize_legacy_submission(
    payload: dict[str, Any],
    config: OSSIntakeConfig,
    *,
    url_validator: Callable[[str, OSSIntakeConfig], str],
) -> dict[str, Any]:
    raw_event_id = payload.get("event_id")
    if not isinstance(raw_event_id, str):
        raise OSSIntakeRequestError(
            "请求必须提供 verifyCode、fileId、downloadUrl"
        )
    event_id = raw_event_id.strip()
    if EVENT_ID_PATTERN.fullmatch(event_id) is None:
        raise OSSIntakeRequestError(
            "event_id 必须为 1～200 位 ASCII 字母、数字或 ._:@/-"
        )
    bucket = _clean_optional_text(payload.get("bucket"), "bucket", limit=255)
    if bucket is None:
        raise OSSIntakeRequestError("bucket 不能为空")
    object_key = _clean_optional_text(payload.get("object_key"), "object_key", limit=1024)
    if object_key is None:
        raise OSSIntakeRequestError("object_key 不能为空")
    basename = _safe_object_basename(object_key)
    raw_download_url = payload.get("download_url")
    if not isinstance(raw_download_url, str):
        raise OSSIntakeRequestError("download_url 必须是字符串")
    download_url = url_validator(raw_download_url, config)
    etag = _normalized_etag(payload.get("etag"))
    sha256 = _clean_optional_text(payload.get("sha256"), "sha256", limit=64)
    if sha256 is not None:
        if SHA256_PATTERN.fullmatch(sha256) is None:
            raise OSSIntakeRequestError("sha256 必须是 64 位十六进制摘要")
        sha256 = sha256.lower()
    if etag is None and sha256 is None:
        raise OSSIntakeRequestError("etag 与 sha256 至少需要提供一个用于对象一致性校验")

    expected_size: int | None
    raw_size = payload.get("size")
    if raw_size is None or raw_size == "":
        expected_size = None
    elif isinstance(raw_size, bool):
        raise OSSIntakeRequestError("size 必须是正整数")
    else:
        try:
            expected_size = int(raw_size)
        except (TypeError, ValueError) as exc:
            raise OSSIntakeRequestError("size 必须是正整数") from exc
        if not 1 <= expected_size <= config.max_download_bytes:
            raise OSSIntakeRequestError(
                f"size 必须在 1 到 {config.max_download_bytes} 字节之间"
            )

    scenario = _clean_optional_text(payload.get("scenario"), "scenario", limit=80)
    if scenario is not None and scenario not in SCENARIO_ORDER:
        raise OSSIntakeRequestError("scenario 不是已登记的核销场景")
    business_date = _business_date(payload.get("business_date"))
    requested_run_id = _clean_optional_text(payload.get("run_id"), "run_id", limit=80)
    if requested_run_id is not None:
        try:
            requested_run_id = normalize_run_id(requested_run_id)
            if output_date_from_run_id(requested_run_id) != business_date:
                raise OSSIntakeRequestError("run_id 日期必须与 business_date 一致")
        except AuditError as exc:
            if isinstance(exc, OSSIntakeRequestError):
                raise
            raise OSSIntakeRequestError(str(exc)) from exc

    canonical = {
        "event_id": event_id,
        "bucket": bucket,
        "object_key": object_key,
        "url_identity": _url_identity(download_url),
        "etag": etag,
        "size": expected_size,
        "sha256": sha256,
        "business_date": business_date,
        "run_id": requested_run_id,
        "scenario": scenario,
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    job_id = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
    run_id = requested_run_id or f"{business_date}-oss-{job_id[:12]}"
    return {
        **canonical,
        "basename": basename,
        "basename_source": "object_key",
        "download_url": download_url,
        "callback_required": False,
        "fingerprint": fingerprint,
        "job_id": job_id,
        "run_id": run_id,
    }


def normalize_submission(
    payload: dict[str, Any],
    config: OSSIntakeConfig,
    *,
    url_validator: Callable[[str, OSSIntakeConfig], str] = _validate_download_url,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OSSIntakeRequestError("请求 JSON 顶层必须是对象")
    compact_keys = {"verifyCode", "fileId", "downloadUrl"}
    if compact_keys.intersection(payload):
        if not compact_keys.issubset(payload):
            raise OSSIntakeRequestError(
                "请求必须同时提供 verifyCode、fileId、downloadUrl"
            )
        return _normalize_compact_submission(
            payload,
            config,
            url_validator=url_validator,
        )
    return _normalize_legacy_submission(
        payload,
        config,
        url_validator=url_validator,
    )


def _response_etag(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    return raw.strip('"').strip()


class _ValidatedRedirectHandler(urllib.request.HTTPRedirectHandler):
    def __init__(self, config: OSSIntakeConfig) -> None:
        super().__init__()
        self.config = config

    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        target = urljoin(req.full_url, newurl)
        _validate_download_url(target, self.config)
        return super().redirect_request(req, fp, code, msg, headers, target)


def _content_disposition_filename(headers: Any) -> str | None:
    raw = str(headers.get("Content-Disposition") or "").strip()
    if not raw:
        return None
    message = Message()
    message["Content-Disposition"] = raw
    value = message.get_filename()
    return unquote(str(value)).strip() if value else None


def _response_basename(
    response: Any,
    submission: dict[str, Any],
    *,
    fallback: str,
) -> str:
    candidates: list[str] = []
    if submission.get("contract") == "oss_ai_v1":
        disposition_name = _content_disposition_filename(response.headers)
        if disposition_name:
            candidates.append(disposition_name)
        candidates.append(unquote(PurePosixPath(urlparse(response.geturl()).path).name))
    if submission.get("basename"):
        candidates.append(str(submission["basename"]))
    candidates.append(fallback)
    for candidate in candidates:
        try:
            return _safe_object_basename(candidate)
        except OSSIntakeRequestError:
            continue
    raise OSSIntakeDownloadError("OSS 下载响应没有可用的 ZIP 文件名")


def download_oss_object(
    submission: dict[str, Any],
    destination: Path,
    config: OSSIntakeConfig,
) -> dict[str, Any]:
    url = _validate_download_url(str(submission["download_url"]), config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    opener = urllib.request.build_opener(_ValidatedRedirectHandler(config))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "offline-activity-audit-oss-intake/1.0"},
        method="GET",
    )
    expected_size = submission.get("size")
    expected_etag = _response_etag(submission.get("etag"))
    expected_sha256 = submission.get("sha256")
    digest = hashlib.sha256()
    total = 0
    verified = False
    actual_destination = destination
    try:
        with opener.open(request, timeout=config.download_timeout_seconds) as response:
            _validate_download_url(response.geturl(), config)
            status = int(response.getcode() or 0)
            if status != 200:
                raise OSSIntakeDownloadError(f"OSS 下载返回非 200 状态：{status}")
            raw_length = response.headers.get("Content-Length")
            if raw_length:
                try:
                    content_length = int(raw_length)
                except ValueError as exc:
                    raise OSSIntakeDownloadError("OSS 响应 Content-Length 无效") from exc
                if content_length < 0:
                    raise OSSIntakeDownloadError("OSS 响应 Content-Length 无效")
                if content_length > config.max_download_bytes:
                    raise OSSIntakeDownloadError("OSS 对象超过服务端下载上限")
                if expected_size is not None and content_length != int(expected_size):
                    raise OSSIntakeDownloadError("OSS 响应长度与事件 size 不一致")
            actual_etag = _response_etag(response.headers.get("ETag"))
            if expected_etag is not None:
                if actual_etag is None:
                    raise OSSIntakeDownloadError("OSS 响应缺少事件要求校验的 ETag")
                if actual_etag != expected_etag:
                    raise OSSIntakeDownloadError("OSS 响应 ETag 与事件不一致")

            actual_destination = destination.with_name(
                _response_basename(
                    response,
                    submission,
                    fallback=destination.name,
                )
            )
            with actual_destination.open("xb") as stream:
                while True:
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    total += len(block)
                    if total > config.max_download_bytes:
                        raise OSSIntakeDownloadError("OSS 对象超过服务端下载上限")
                    digest.update(block)
                    stream.write(block)
        if expected_size is not None and total != int(expected_size):
            raise OSSIntakeDownloadError("OSS 实际下载长度与事件 size 不一致")
        actual_sha256 = digest.hexdigest()
        if expected_sha256 is not None and actual_sha256 != expected_sha256:
            raise OSSIntakeDownloadError("OSS 对象 SHA-256 与事件不一致")
        if not zipfile.is_zipfile(actual_destination):
            raise OSSIntakeDownloadError("OSS 对象不是有效 ZIP")
        verified = True
        return {
            "bytes": total,
            "sha256": actual_sha256,
            "etag": actual_etag,
            "basename": actual_destination.name,
            "input_file": str(actual_destination),
            "verified_at": utc_now(),
        }
    except urllib.error.HTTPError as exc:
        raise OSSIntakeDownloadError(f"OSS 下载失败：HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise OSSIntakeDownloadError("OSS 下载连接失败") from exc
    except (OSError, zipfile.BadZipFile) as exc:
        raise OSSIntakeDownloadError("OSS 对象下载或 ZIP 校验失败") from exc
    finally:
        if actual_destination.exists() and not verified:
            actual_destination.unlink(missing_ok=True)


def run_formal_audit_subprocess(
    *,
    run_id: str,
    producer_model: str,
    input_dir: Path,
    worktrees_root: Path,
    scenario: str | None,
    workbench_url: str | None,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(
        prefix="offline-audit-oss-result-"
    ) as temporary:
        result_path = Path(temporary) / "formal-result.json"
        command = [
            sys.executable,
            "-B",
            str(FORMAL_RUNNER),
            "--run-id",
            run_id,
            "--producer-model",
            producer_model,
            "--input-dir",
            str(input_dir),
            "--worktrees",
            str(worktrees_root),
            "--result-json",
            str(result_path),
        ]
        if scenario:
            command.extend(["--scenario", scenario])
        if workbench_url:
            command.extend(["--workbench-url", workbench_url])
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        completed = subprocess.run(
            command,
            cwd=PROJECT_ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            **kwargs,
        )
        if completed.returncode != 0:
            detail = (completed.stdout or "").strip()[-2000:]
            raise OSSIntakeExecutionError(
                "正式核销运行失败" + (f"：{detail}" if detail else "")
            )
        if not result_path.is_file():
            raise OSSIntakeExecutionError("正式核销运行未返回结果收据")
        result = read_json_file(result_path)
        if str(result.get("status")) != "completed" or not result.get(
            "workspace_id"
        ):
            raise OSSIntakeExecutionError("正式核销运行返回的结果收据无效")
        return result


def build_callback_result(result: dict[str, Any]) -> str:
    error_count = int(result.get("error_count") or 0)
    scenarios = [str(value) for value in result.get("scenarios") or []]
    scenario_count = len(scenarios)
    if error_count:
        heading = (
            f"核销分析完成，共{scenario_count or 1}个场景，"
            f"发现{error_count}个错误项。"
        )
    else:
        heading = f"核销分析完成，共{scenario_count or 1}个场景，未发现阻断问题。"

    notes: list[str] = []
    snapshot_value = result.get("snapshot")
    if snapshot_value:
        try:
            snapshot = read_json_file(Path(str(snapshot_value)).resolve())
            view = snapshot.get("view") if isinstance(snapshot, dict) else None
            sheets = view.get("sheets") if isinstance(view, dict) else None
            if isinstance(sheets, list):
                for sheet in sheets:
                    if not isinstance(sheet, dict):
                        continue
                    name = str(
                        sheet.get("name")
                        or sheet.get("title")
                        or sheet.get("scenario")
                        or "核销结果"
                    ).strip()
                    note = str(sheet.get("note") or "").strip()
                    if note:
                        notes.append(f"{name}：{note}")
        except (OSError, ValueError, json.JSONDecodeError):
            notes = []

    callback_result = heading
    if notes:
        callback_result += " " + " ".join(notes)
    if len(callback_result) > MAX_CALLBACK_RESULT_CHARS:
        callback_result = callback_result[: MAX_CALLBACK_RESULT_CHARS - 8] + "……（截断）"
    return callback_result


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> urllib.request.Request | None:
        return None


def post_analysis_callback(
    payload: dict[str, Any],
    config: OSSIntakeConfig,
) -> dict[str, Any]:
    callback_url = config.callback_url
    if callback_url is None:
        raise OSSIntakeCallbackError("未配置 OSS 分析结果回调地址")
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=utf-8",
        "Idempotency-Key": f"{payload['verifyCode']}:{payload['fileId']}",
        "User-Agent": "offline-activity-audit-callback/1.0",
    }
    if config.callback_token:
        headers["Authorization"] = f"Bearer {config.callback_token}"
    opener = urllib.request.build_opener(_NoRedirectHandler())
    last_error = "未知错误"
    last_status: int | None = None
    for attempt in range(1, config.callback_attempts + 1):
        request = urllib.request.Request(
            callback_url,
            data=encoded,
            headers=headers,
            method="POST",
        )
        try:
            with opener.open(
                request,
                timeout=config.callback_timeout_seconds,
            ) as response:
                status = int(response.getcode() or 0)
                response.read(4096)
            if 200 <= status < 300:
                return {
                    "status": "delivered",
                    "http_status": status,
                    "attempts": attempt,
                    "delivered_at": utc_now(),
                }
            last_error = f"HTTP {status}"
            last_status = status
            retryable = status in {408, 429} or status >= 500
        except urllib.error.HTTPError as exc:
            status = int(exc.code)
            exc.close()
            last_error = f"HTTP {status}"
            last_status = status
            retryable = status in {408, 429} or status >= 500
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc.__class__.__name__
            retryable = True
        if not retryable or attempt >= config.callback_attempts:
            break
        time.sleep(min(2 ** (attempt - 1), 4))
    raise OSSIntakeCallbackError(
        f"分析结果回调失败：{last_error}",
        attempts=attempt,
        http_status=last_status,
    )


class OSSIntakeStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.jobs_root = self.root / "jobs"
        self.jobs_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._recover_interrupted_jobs()

    def _path(self, job_id: str) -> Path:
        if JOB_ID_PATTERN.fullmatch(job_id) is None:
            raise OSSIntakeRequestError("job_id 不安全")
        return self.jobs_root / f"{job_id}.json"

    def _recover_interrupted_jobs(self) -> None:
        for path in self.jobs_root.glob("*.json"):
            try:
                job = read_json_file(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            if str(job.get("status")) not in ACTIVE_JOB_STATUSES:
                continue
            job["status"] = "failed"
            job["updated_at"] = utc_now()
            job["finished_at"] = utc_now()
            job["failure"] = {
                "code": "service_restarted",
                "message": "服务在任务完成前重启；如需重新分析，请使用新的 fileId 和下载地址投递。",
            }
            atomic_write_json(path, job)

    def create_or_get(self, submission: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        job_id = str(submission["job_id"])
        path = self._path(job_id)
        with self._lock:
            if path.is_file():
                existing = read_json_file(path)
                if existing.get("idempotency_fingerprint") != submission["fingerprint"]:
                    identity = (
                        "同一 verifyCode 与 fileId"
                        if submission.get("callback_required")
                        else "同一 event_id"
                    )
                    raise OSSIntakeConflictError(
                        f"{identity}已用于另一个 OSS 对象"
                    )
                return existing, False
            now = utc_now()
            callback_required = bool(submission.get("callback_required"))
            job = {
                "schema_version": "1.1",
                "job_id": job_id,
                "event_id": submission["event_id"],
                "verifyCode": submission.get("verify_code"),
                "fileId": submission.get("file_id"),
                "idempotency_fingerprint": submission["fingerprint"],
                "status": "accepted",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "finished_at": None,
                "run_id": submission["run_id"],
                "producer_model": submission["producer_model"],
                "scenario": submission.get("scenario"),
                "object": {
                    "bucket": submission["bucket"],
                    "object_key": submission["object_key"],
                    "basename": submission["basename"],
                    "etag": submission.get("etag"),
                    "size": submission.get("size"),
                    "sha256": submission.get("sha256"),
                },
                "delivery": None,
                "result": None,
                "callback": {
                    "required": callback_required,
                    "status": "pending" if callback_required else "not_required",
                    "attempts": 0,
                    "http_status": None,
                    "delivered_at": None,
                },
                "failure": None,
            }
            atomic_write_json(path, job)
            return job, True

    def get(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            path = self._path(job_id)
            if not path.is_file():
                raise FileNotFoundError(job_id)
            return read_json_file(path)

    def update(self, job_id: str, **changes: Any) -> dict[str, Any]:
        with self._lock:
            job = self.get(job_id)
            job.update(changes)
            job["updated_at"] = utc_now()
            atomic_write_json(self._path(job_id), job)
            return job


Downloader = Callable[[dict[str, Any], Path, OSSIntakeConfig], dict[str, Any]]
AuditRunner = Callable[..., dict[str, Any]]
URLValidator = Callable[[str, OSSIntakeConfig], str]
CallbackSender = Callable[[dict[str, Any], OSSIntakeConfig], dict[str, Any]]


class OSSIntakeService:
    def __init__(
        self,
        *,
        worktrees_root: Path,
        input_root: Path,
        config: OSSIntakeConfig,
        downloader: Downloader = download_oss_object,
        runner: AuditRunner = run_formal_audit_subprocess,
        url_validator: URLValidator = _validate_download_url,
        callback_sender: CallbackSender = post_analysis_callback,
    ) -> None:
        self.worktrees_root = worktrees_root.resolve()
        self.worktrees_root.mkdir(parents=True, exist_ok=True)
        self.input_root = input_root.resolve()
        self.input_root.mkdir(parents=True, exist_ok=True)
        self.config = config
        self.store = OSSIntakeStore(self.worktrees_root / ".intake")
        self.downloader = downloader
        self.runner = runner
        self.url_validator = url_validator
        self.callback_sender = callback_sender
        self._queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._closed = threading.Event()
        self._worker = threading.Thread(
            target=self._worker_loop,
            name="offline-audit-oss-intake",
            daemon=True,
        )
        self._worker.start()

    @property
    def secret(self) -> str:
        return self.config.webhook_secret

    def submit(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if self._closed.is_set():
            raise OSSIntakeUnavailableError("OSS 入站服务正在关闭")
        submission = normalize_submission(
            payload,
            self.config,
            url_validator=self.url_validator,
        )
        if submission.get("callback_required") and not self.config.callback_url:
            raise OSSIntakeUnavailableError("OSS 分析结果回调地址尚未配置")
        submission["producer_model"] = self.config.producer_model
        job, created = self.store.create_or_get(submission)
        if created:
            self._queue.put(submission)
        return job, created

    def get(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    def close(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        self._queue.put(None)

    def _worker_loop(self) -> None:
        while True:
            submission = self._queue.get()
            try:
                if submission is None:
                    return
                self._process(submission)
            finally:
                self._queue.task_done()

    def _process(self, submission: dict[str, Any]) -> None:
        job_id = str(submission["job_id"])
        self.store.update(
            job_id,
            status="downloading",
            started_at=utc_now(),
            failure=None,
        )
        try:
            input_dir = self.input_root / job_id
            input_dir.mkdir(parents=True, exist_ok=True)
            destination = input_dir / str(submission["basename"])
            delivery = self.downloader(submission, destination, self.config)
            input_file = Path(str(delivery.get("input_file") or destination)).resolve()
            try:
                input_file.relative_to(input_dir.resolve())
            except ValueError as exc:
                raise OSSIntakeDownloadError("OSS 下载文件越过任务输入目录") from exc
            if not input_file.is_file():
                raise OSSIntakeDownloadError("OSS 下载完成后找不到本地 ZIP")
            delivery = {
                **delivery,
                "input_directory": str(input_dir),
                "input_file": str(input_file),
            }
            self.store.update(job_id, status="running", delivery=delivery)
            result = self.runner(
                run_id=submission["run_id"],
                producer_model=self.config.producer_model,
                input_dir=input_dir,
                worktrees_root=self.worktrees_root,
                scenario=submission.get("scenario"),
                workbench_url=self.config.workbench_url,
            )
            callback: dict[str, Any]
            if submission.get("callback_required"):
                callback_payload = {
                    "verifyCode": submission["verify_code"],
                    "fileId": submission["file_id"],
                    "result": build_callback_result(result),
                }
                self.store.update(
                    job_id,
                    status="callback",
                    result=result,
                    callback={
                        "required": True,
                        "status": "sending",
                        "attempts": 0,
                        "http_status": None,
                        "delivered_at": None,
                    },
                )
                callback = self.callback_sender(callback_payload, self.config)
                callback = {
                    "required": True,
                    "status": "delivered",
                    "attempts": int(callback.get("attempts") or 1),
                    "http_status": int(callback.get("http_status") or 200),
                    "delivered_at": callback.get("delivered_at") or utc_now(),
                }
            else:
                callback = {
                    "required": False,
                    "status": "not_required",
                    "attempts": 0,
                    "http_status": None,
                    "delivered_at": None,
                }
            self.store.update(
                job_id,
                status="completed",
                result=result,
                callback=callback,
                failure=None,
                finished_at=utc_now(),
            )
        except BaseException as exc:
            existing_job = self.store.get(job_id)
            result = existing_job.get("result") or self._find_run_receipt(
                str(submission["run_id"])
            )
            message = self._safe_failure_message(exc, submission)
            if isinstance(exc, OSSIntakeCallbackError):
                failure_code = "callback_failed"
                callback = {
                    "required": True,
                    "status": "failed",
                    "attempts": exc.attempts,
                    "http_status": exc.http_status,
                    "delivered_at": None,
                }
            else:
                failure_code = (
                    "audit_failed"
                    if isinstance(exc, OSSIntakeExecutionError)
                    else "intake_failed"
                )
                callback = existing_job.get("callback")
            self.store.update(
                job_id,
                status="failed",
                result=result,
                callback=callback,
                failure={
                    "code": failure_code,
                    "message": message,
                },
                finished_at=utc_now(),
            )

    def _find_run_receipt(self, run_id: str) -> dict[str, Any] | None:
        matches: list[tuple[float, Path, dict[str, Any]]] = []
        for path in self.worktrees_root.iterdir():
            manifest_path = path / "manifest.json"
            if path.name.startswith(".") or not manifest_path.is_file():
                continue
            try:
                manifest = read_json_file(manifest_path)
                if str(manifest.get("run_id")) != run_id:
                    continue
                matches.append((manifest_path.stat().st_mtime, path, manifest))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        if not matches:
            return None
        _, path, manifest = max(matches, key=lambda item: item[0])
        base = str(self.config.workbench_url or "").rstrip("/")
        return {
            "run_id": run_id,
            "workspace_id": manifest.get("workspace_id") or path.name,
            "status": manifest.get("status"),
            "worktree": str(path.resolve()),
            "workbench_url": (
                f"{base}/?run={quote(path.name, safe='')}" if base else None
            ),
        }

    def _safe_failure_message(
        self,
        exc: BaseException,
        submission: dict[str, Any],
    ) -> str:
        message = str(exc).strip() or exc.__class__.__name__
        secrets_to_hide = {
            str(submission.get("download_url") or ""),
            self.config.webhook_secret,
            str(self.config.callback_url or ""),
            str(self.config.callback_token or ""),
        }
        for secret in secrets_to_hide:
            if secret:
                message = message.replace(secret, "[已隐藏]")
        message = re.sub(r"https://[^\s]+", "HTTPS 地址（已隐藏）", message)
        return message[-2000:]


def public_job(job: dict[str, Any]) -> dict[str, Any]:
    """Return the authenticated API projection without internal fingerprints."""

    return {
        key: value
        for key, value in job.items()
        if key != "idempotency_fingerprint"
    }
