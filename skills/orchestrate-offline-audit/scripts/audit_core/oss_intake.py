from __future__ import annotations

from .paths import INPUT_ROOT, PROJECT_ROOT

import errno
import hashlib
import ipaddress
import json
import os
import queue
import re
import socket
import ssl
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

from .common import AuditError, BIZ_TYPE_FAILURE_MESSAGE, BIZ_TYPE_NOT_REQUIRED_MESSAGE, biz_type_skip_result
from .scenario_registry import scenario_for_biz_type
from .customer_language import SYSTEM_FAILURE_MESSAGE
from .codex_runner import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT
from .orchestrator import (
    SCENARIO_ORDER,
    normalize_producer_model,
    normalize_run_id,
    output_date_from_run_id,
)
from .workbench_store import (
    ANALYSIS_SUMMARY_FILENAME,
    atomic_write_json,
    read_json_file,
    render_analysis_summary_markdown,
    project_run_status,
    utc_now,
)
from .workbench_delete import RunDeletionConflict, prepare_intake_job_deletion


FORMAL_RUNNER = (
    PROJECT_ROOT / "skills" / "orchestrate-offline-audit" / "scripts" / "run.py"
)
DEFAULT_OSS_INPUT_ROOT = INPUT_ROOT
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
ACTIVE_JOB_STATUSES = {
    "accepted",
    "downloading",
    "downloaded",
    "running",
    "callback",
}
TERMINAL_JOB_STATUSES = {"completed", "failed"}
INTERNAL_JOB_FIELDS = {"idempotency_fingerprint", "identity_job_id"}


def _attempt_job_id(identity_job_id: str, attempt: int) -> str:
    if attempt <= 1:
        return identity_job_id
    value = f"{identity_job_id}:attempt:{attempt}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]


class OSSIntakeError(AuditError):
    """Base error for the OSS transport adapter."""


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
    """The persisted audit outcome could not be delivered to the caller."""

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


class OSSIntakeCancelledError(OSSIntakeError):
    """The user canceled an accepted intake attempt from the workbench."""


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
    allowed_hosts: tuple[str, ...]
    producer_model: str = "codex"
    max_download_bytes: int = DEFAULT_MAX_DOWNLOAD_BYTES
    download_timeout_seconds: int = DEFAULT_DOWNLOAD_TIMEOUT_SECONDS
    allow_private_hosts: bool = False
    workbench_url: str | None = None
    callback_url: str | None = None
    allow_http_callback: bool = False
    callback_required: bool = True
    receive_only: bool = False
    callback_token: str | None = None
    callback_timeout_seconds: int = DEFAULT_CALLBACK_TIMEOUT_SECONDS
    callback_attempts: int = DEFAULT_CALLBACK_ATTEMPTS

    def __post_init__(self) -> None:
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
        allow_http_callback = bool(self.allow_http_callback)
        if callback_url is not None:
            parsed_callback = urlparse(callback_url)
            callback_scheme = parsed_callback.scheme.lower()
            if (
                callback_scheme not in {"http", "https"}
                or (callback_scheme == "http" and not allow_http_callback)
                or not parsed_callback.hostname
                or parsed_callback.username is not None
                or parsed_callback.password is not None
                or parsed_callback.fragment
                or parsed_callback.path.rstrip("/") != CALLBACK_PATH
            ):
                raise OSSIntakeRequestError(
                    f"OSS 结果回调必须是路径为 {CALLBACK_PATH} 的完整 HTTPS 地址；"
                    "受信任内网 HTTP 必须显式启用"
                )
        callback_token = str(self.callback_token or "").strip() or None
        receive_only = bool(self.receive_only)
        callback_required = bool(self.callback_required) and not receive_only
        try:
            producer_model = normalize_producer_model(self.producer_model)
        except AuditError as exc:
            raise OSSIntakeRequestError(str(exc)) from exc
        object.__setattr__(self, "allowed_hosts", hosts)
        object.__setattr__(self, "producer_model", producer_model)
        object.__setattr__(self, "max_download_bytes", max_download_bytes)
        object.__setattr__(self, "download_timeout_seconds", timeout_seconds)
        object.__setattr__(self, "callback_url", callback_url)
        object.__setattr__(self, "allow_http_callback", allow_http_callback)
        object.__setattr__(self, "callback_required", callback_required)
        object.__setattr__(self, "receive_only", receive_only)
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
    analyze_id: int,
) -> tuple[str, str]:
    candidate = unquote(PurePosixPath(urlparse(download_url).path).name)
    try:
        return _safe_object_basename(candidate), "url"
    except OSSIntakeRequestError:
        return _safe_object_basename(f"{verify_code}-{analyze_id}.zip"), "fallback"


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

    raw_analyze_id = payload.get("analyzeId")
    if isinstance(raw_analyze_id, bool) or not isinstance(raw_analyze_id, int):
        raise OSSIntakeRequestError("analyzeId 必须是正整数")
    analyze_id = int(raw_analyze_id)
    if not 1 <= analyze_id <= 9_223_372_036_854_775_807:
        raise OSSIntakeRequestError("analyzeId 必须是 64 位正整数")

    biz_type = payload.get("bizType")
    if biz_type is not None and not isinstance(biz_type, str):
        raise OSSIntakeRequestError("bizType 必须是字符串或 null")

    large_venue_fee = payload.get("largeVenueFee")
    if "largeVenueFee" in payload and not isinstance(large_venue_fee, bool):
        raise OSSIntakeRequestError("largeVenueFee 必须是布尔值 true 或 false")

    raw_download_url = payload.get("downloadUrl")
    if not isinstance(raw_download_url, str):
        raise OSSIntakeRequestError("downloadUrl 必须是字符串")
    download_url = url_validator(raw_download_url, config)
    basename, basename_source = _url_basename_or_fallback(
        download_url,
        verify_code=verify_code,
        analyze_id=analyze_id,
    )
    parsed = urlparse(download_url)
    host = _normalized_host(parsed.hostname or "")
    object_key = unquote(parsed.path.lstrip("/")) or basename
    event_id = f"{verify_code}:{analyze_id}"
    business_date = _business_date_from_verify_code(verify_code)
    canonical = {
        "contract": "oss_ai_v1",
        "verifyCode": verify_code,
        "analyzeId": analyze_id,
        "url_identity": _url_identity(download_url),
    }
    # Keep the historical fingerprint encoding so a field rename does not
    # turn an existing business identity and object path into a conflict.
    fingerprint_fields = {**canonical}
    fingerprint_fields["fileId"] = fingerprint_fields.pop("analyzeId")
    fingerprint = hashlib.sha256(
        json.dumps(fingerprint_fields, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    job_id = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:24]
    return {
        **canonical,
        "event_id": event_id,
        "verify_code": verify_code,
        "analyze_id": analyze_id,
        # Keep the historical object fingerprint; this value selects the Skill.
        "biz_type": biz_type,
        "large_venue_fee": large_venue_fee,
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
        "callback_required": config.callback_required,
        "fingerprint": fingerprint,
        "job_id": job_id,
        "run_id": f"{business_date}-oss-{job_id[:12]}",
        "generated_run_id": True,
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
            "请求必须提供 verifyCode、analyzeId、downloadUrl"
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
        "generated_run_id": requested_run_id is None,
    }


def normalize_submission(
    payload: dict[str, Any],
    config: OSSIntakeConfig,
    *,
    url_validator: Callable[[str, OSSIntakeConfig], str] = _validate_download_url,
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise OSSIntakeRequestError("请求 JSON 顶层必须是对象")
    if "fileId" in payload:
        raise OSSIntakeRequestError("fileId 已更名为 analyzeId，请使用 analyzeId")
    compact_keys = {"verifyCode", "analyzeId", "downloadUrl"}
    if compact_keys.intersection(payload):
        if not compact_keys.issubset(payload):
            raise OSSIntakeRequestError(
                "请求必须同时提供 verifyCode、analyzeId、downloadUrl"
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


def _download_connection_failure(reason: Any) -> str:
    """Classify transport errors without exposing URLs or exception text."""
    error_numbers = {getattr(reason, "errno", None), getattr(reason, "winerror", None)}
    if isinstance(reason, ssl.SSLCertVerificationError):
        detail = "TLS 证书校验失败"
    elif isinstance(reason, ssl.SSLError):
        detail = "TLS 握手失败"
    elif isinstance(reason, socket.gaierror):
        detail = "域名无法解析"
    elif isinstance(reason, TimeoutError) or error_numbers & {errno.ETIMEDOUT, 10060}:
        detail = "连接超时"
    elif isinstance(reason, ConnectionRefusedError) or error_numbers & {
        errno.ECONNREFUSED, 10061
    }:
        detail = "目标服务器拒绝连接"
    elif isinstance(reason, ConnectionResetError) or error_numbers & {
        errno.ECONNRESET, 10054
    }:
        detail = "连接被重置"
    else:
        return "OSS 下载连接失败"
    return f"OSS 下载连接失败：{detail}"


def download_oss_object(
    submission: dict[str, Any],
    destination: Path,
    config: OSSIntakeConfig,
    *,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    if cancel_event is not None and cancel_event.is_set():
        raise OSSIntakeCancelledError("投递任务已取消")
    url = _validate_download_url(str(submission["download_url"]), config)
    destination.parent.mkdir(parents=True, exist_ok=True)
    # OSS has its own validated destination policy. Desktop/model proxies must
    # not route signed downloads or make them depend on a local proxy process.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _ValidatedRedirectHandler(config),
    )
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
                    if cancel_event is not None and cancel_event.is_set():
                        raise OSSIntakeCancelledError("投递任务已取消")
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
        raise OSSIntakeDownloadError(_download_connection_failure(exc.reason)) from exc
    except (OSError, zipfile.BadZipFile) as exc:
        message = _download_connection_failure(exc)
        if message == "OSS 下载连接失败":
            message = "OSS 对象下载或 ZIP 校验失败"
        raise OSSIntakeDownloadError(message) from exc
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
    cancel_event: threading.Event | None = None,
    biz_type: str | None = None,
    large_venue_fee: bool | None = None,
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
            "--model",
            DEFAULT_MODEL,
            "--reasoning-effort",
            DEFAULT_REASONING_EFFORT,
            "--input-dir",
            str(input_dir),
            "--input-source",
            "oss",
            "--worktrees",
            str(worktrees_root),
            "--result-json",
            str(result_path),
        ]
        if scenario:
            command.extend(["--scenario", scenario])
        if biz_type is not None:
            command.append("--biz-type=" + biz_type)
        if large_venue_fee is not None:
            command.append("--large-venue-fee=" + json.dumps(large_venue_fee))
        if workbench_url:
            command.extend(["--workbench-url", workbench_url])
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONIOENCODING"] = "utf-8"
        kwargs: dict[str, Any] = {}
        if os.name == "nt":
            kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if cancel_event is None:
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
            returncode = completed.returncode
            output = completed.stdout or ""
        else:
            if cancel_event.is_set():
                raise OSSIntakeCancelledError("投递任务已取消")
            if os.name == "nt":
                kwargs["creationflags"] = int(kwargs.get("creationflags") or 0) | getattr(
                    subprocess,
                    "CREATE_NEW_PROCESS_GROUP",
                    0,
                )
            else:
                kwargs["start_new_session"] = True
            process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                **kwargs,
            )
            output = ""
            while True:
                try:
                    output, _ = process.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    if not cancel_event.is_set():
                        continue
                    _terminate_formal_process(process)
                    try:
                        output, _ = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        output, _ = process.communicate()
                    raise OSSIntakeCancelledError("投递任务已取消")
            returncode = int(process.returncode or 0)
            if cancel_event.is_set():
                raise OSSIntakeCancelledError("投递任务已取消")
        result = read_json_file(result_path) if result_path.is_file() else None
        classification_failed = bool(
            isinstance(result, dict) and result.get("status") == "failed"
            and isinstance(result.get("failure"), dict)
            and result["failure"].get("code") == "classification_failed"
        )
        biz_type_failed = bool(
            isinstance(result, dict) and result.get("status") == "failed"
            and isinstance(result.get("failure"), dict)
            and result["failure"].get("code") == "biz_type_unrecognized"
        )
        if returncode != 0 and not (returncode == 2 and (classification_failed or biz_type_failed)):
            detail = output.strip()[-2000:]
            raise OSSIntakeExecutionError(
                "正式核销运行失败" + (f"：{detail}" if detail else "")
            )
        if result is None:
            raise OSSIntakeExecutionError("正式核销运行未返回结果收据")
        if (result.get("status") == "completed" and result.get("audit_required") is False
                and result.get("reason_code") == "biz_type_not_required"
                and isinstance(biz_type, str) and biz_type.strip()
                and scenario_for_biz_type(biz_type) is None):
            return result
        if (str(result.get("status")) != "completed" and not classification_failed and not biz_type_failed) or not result.get(
            "workspace_id"
        ):
            raise OSSIntakeExecutionError("正式核销运行返回的结果收据无效")
        return project_run_status(result)


def _terminate_formal_process(process: subprocess.Popen[str]) -> None:
    """Terminate only the formal runner process tree owned by this intake job."""

    if process.poll() is not None:
        return
    if os.name == "nt":
        completed = subprocess.run(
            ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if completed.returncode == 0 or process.poll() is not None:
            return
    process.kill()


def build_callback_result(result: dict[str, Any]) -> str:
    if (result.get("status") == "completed" and result.get("audit_required") is False
            and result.get("reason_code") == "biz_type_not_required"):
        return BIZ_TYPE_NOT_REQUIRED_MESSAGE
    failure = result.get("failure") or {}
    if result.get("status") == "failed" and failure.get("code") in {"biz_type_unrecognized", "plan_type_unrecognized"}:
        return BIZ_TYPE_FAILURE_MESSAGE
    if result.get("status") == "failed" and failure.get("code") != "classification_failed":
        if not str(failure.get("message") or "").strip():
            raise OSSIntakeExecutionError("核销失败结果缺少具体原因")
        return SYSTEM_FAILURE_MESSAGE
    summary_value = str(result.get("analysis_summary") or "").strip()
    worktree_value = str(result.get("worktree") or "").strip()
    if summary_value and worktree_value:
        try:
            summary_path = Path(summary_value).resolve()
            worktree_path = Path(worktree_value).resolve()
            relative = summary_path.relative_to(worktree_path)
            if (
                relative.as_posix() == ANALYSIS_SUMMARY_FILENAME
                and summary_path.is_file()
            ):
                callback_result = summary_path.read_text(encoding="utf-8").strip()
                if callback_result:
                    return _bounded_callback_result(callback_result)
        except (OSError, UnicodeError, ValueError):
            pass

    view_payload: dict[str, Any] = {"sheets": []}
    snapshot_value = result.get("snapshot")
    if snapshot_value:
        try:
            snapshot = read_json_file(Path(str(snapshot_value)).resolve())
            view = snapshot.get("view") if isinstance(snapshot, dict) else None
            if isinstance(view, dict):
                view_payload = view
        except (OSError, ValueError, json.JSONDecodeError):
            pass

    scenarios = [str(value) for value in result.get("scenarios") or []]
    if result.get("status") == "failed" and not view_payload.get("sheets"):
        if (result.get("failure") or {}).get("code") == "classification_failed":
            return "具体资料问题未记录，请人工核对。"
    callback_result = render_analysis_summary_markdown(
        view_payload,
        {
            "scenario_count": len(scenarios) or 1,
            "error_count": int(result.get("error_count") or 0),
        },
    )
    return _bounded_callback_result(callback_result)


def _bounded_callback_result(value: str) -> str:
    if len(value) <= MAX_CALLBACK_RESULT_CHARS:
        return value
    suffix = "\n\n> 问题较多，这里只显示部分。完整问题请在核销记录中查看。"
    return value[: MAX_CALLBACK_RESULT_CHARS - len(suffix)].rstrip() + suffix


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
        "Idempotency-Key": (
            f"{payload['verifyCode']}:{payload['analyzeId']}:"
            f"{hashlib.sha256(encoded).hexdigest()[:16]}"
        ),
        "User-Agent": "offline-activity-audit-callback/1.0",
    }
    if config.callback_token:
        headers["Authorization"] = f"Bearer {config.callback_token}"
    # Callback delivery uses its own direct connection. A stale process or
    # Windows proxy must not block the configured business callback endpoint.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _NoRedirectHandler(),
    )
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
                "message": "服务在任务完成前重启；如需重新分析，请使用同一业务字段和新的临时下载地址再次投递。",
            }
            atomic_write_json(path, job)

    def _jobs_for_identity(self, identity_job_id: str) -> list[dict[str, Any]]:
        matches: list[dict[str, Any]] = []
        for path in self.jobs_root.glob("*.json"):
            try:
                job = read_json_file(path)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            job_id = str(job.get("job_id") or "")
            stored_identity = str(job.get("identity_job_id") or job_id)
            if stored_identity != identity_job_id:
                continue
            if JOB_ID_PATTERN.fullmatch(job_id) is None or path.name != f"{job_id}.json":
                continue
            try:
                attempt = int(job.get("attempt") or 1)
            except (TypeError, ValueError):
                continue
            if attempt < 1:
                continue
            job["attempt"] = attempt
            matches.append(job)
        return matches

    def create_or_get(self, submission: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        identity_job_id = str(submission.get("identity_job_id") or submission["job_id"])
        self._path(identity_job_id)
        with self._lock:
            identity_jobs = self._jobs_for_identity(identity_job_id)
            supersedes_job_id: str | None = None
            if identity_jobs:
                existing = max(
                    identity_jobs,
                    key=lambda item: (
                        int(item.get("attempt") or 1),
                        str(item.get("created_at") or ""),
                        str(item.get("job_id") or ""),
                    ),
                )
                if existing.get("idempotency_fingerprint") != submission["fingerprint"]:
                    identity = (
                        "同一 verifyCode 与 analyzeId"
                        if submission.get("contract") == "oss_ai_v1"
                        else "同一 event_id"
                    )
                    raise OSSIntakeConflictError(
                        f"{identity}已用于另一个 OSS 对象"
                    )
                if str(existing.get("status")) not in TERMINAL_JOB_STATUSES:
                    existing_biz_type = existing.get("bizType", existing.get("planType"))
                    if submission.get("contract") == "oss_ai_v1" and (existing_biz_type or "") != (submission.get("biz_type") or ""):
                        raise OSSIntakeConflictError("活动任务的bizType不能更改，请等待本次处理结束后重新提交")
                    if existing.get("largeVenueFee") is not submission.get("large_venue_fee"):
                        raise OSSIntakeConflictError("活动任务的largeVenueFee不能更改，请等待本次处理结束后重新提交")
                    return existing, False
                attempt = max(int(item.get("attempt") or 1) for item in identity_jobs) + 1
                supersedes_job_id = str(existing["job_id"])
            else:
                attempt = 1

            job_id = _attempt_job_id(identity_job_id, attempt)
            path = self._path(job_id)
            if path.exists():
                raise OSSIntakeConflictError("无法为本次重复投递创建唯一任务")
            submission["identity_job_id"] = identity_job_id
            submission["job_id"] = job_id
            submission["attempt"] = attempt
            submission["supersedes_job_id"] = supersedes_job_id
            if attempt > 1 and submission.get("generated_run_id"):
                submission["run_id"] = (
                    f"{submission['business_date']}-oss-{job_id[:12]}"
                )
            now = utc_now()
            callback_required = bool(submission.get("callback_required"))
            job = {
                "schema_version": "1.7",
                "job_id": job_id,
                "identity_job_id": identity_job_id,
                "attempt": attempt,
                "supersedes_job_id": supersedes_job_id,
                "event_id": submission["event_id"],
                "verifyCode": submission.get("verify_code"),
                "analyzeId": submission.get("analyze_id"),
                "bizType": submission.get("biz_type"),
                "largeVenueFee": submission.get("large_venue_fee"),
                "idempotency_fingerprint": submission["fingerprint"],
                "status": "accepted",
                "created_at": now,
                "updated_at": now,
                "started_at": None,
                "downloaded_at": None,
                "audit_started_at": None,
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

    def list(
        self,
        *,
        active_only: bool = False,
        completed: bool | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            jobs: list[dict[str, Any]] = []
            for path in self.jobs_root.glob("*.json"):
                job = read_json_file(path)
                job_id = str(job.get("job_id") or "")
                if JOB_ID_PATTERN.fullmatch(job_id) is None or path.name != f"{job_id}.json":
                    raise OSSIntakeRequestError("OSS 投递任务收据标识不安全")
                status = str(job.get("status"))
                if active_only and status not in ACTIVE_JOB_STATUSES:
                    continue
                if completed is not None and (status == "completed") is not completed:
                    continue
                jobs.append(job)
            return sorted(
                jobs,
                key=lambda item: (
                    str(item.get("created_at") or ""),
                    str(item.get("job_id") or ""),
                ),
                reverse=True,
            )

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


class _JobControl:
    def __init__(self) -> None:
        self.cancel_event = threading.Event()
        self.idle_event = threading.Event()
        self.idle_event.set()
        self.lock = threading.Lock()
        self.active_stages = 0


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
        self._download_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._audit_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._closed = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._controls: dict[str, _JobControl] = {}
        self._download_worker = threading.Thread(
            target=self._download_worker_loop,
            name="offline-audit-oss-download",
            daemon=True,
        )
        self._audit_worker = threading.Thread(
            target=self._audit_worker_loop,
            name="offline-audit-oss-audit",
            daemon=True,
        )
        self._download_worker.start()
        self._audit_worker.start()

    def submit(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        submission = normalize_submission(
            payload,
            self.config,
            url_validator=self.url_validator,
        )
        if submission.get("callback_required") and not self.config.callback_url:
            raise OSSIntakeUnavailableError("OSS 分析结果回调地址尚未配置")
        submission["producer_model"] = self.config.producer_model
        with self._lifecycle_lock:
            if self._closed.is_set():
                raise OSSIntakeUnavailableError("OSS 入站服务正在关闭")
            job, created = self.store.create_or_get(submission)
            if created:
                self._controls[str(submission["job_id"])] = _JobControl()
                self._download_queue.put(submission)
        return job, created

    def get(self, job_id: str) -> dict[str, Any]:
        return self.store.get(job_id)

    def list(
        self,
        *,
        active_only: bool = False,
        completed: bool | None = None,
    ) -> list[dict[str, Any]]:
        return self.store.list(active_only=active_only, completed=completed)

    def delete(self, job_id: str) -> dict[str, Any]:
        with self._lifecycle_lock:
            job = self.store.get(job_id)
            active = str(job.get("status")) in ACTIVE_JOB_STATUSES
            control = self._controls.get(job_id)
            if active and control is None:
                raise RunDeletionConflict("该活动任务不属于当前服务进程，不能安全终止")
            if control is not None:
                with control.lock:
                    control.cancel_event.set()
                idle_event = control.idle_event
            else:
                idle_event = None
        if idle_event is not None and not idle_event.wait(timeout=75):
            raise RunDeletionConflict("任务终止超时，尚不能安全删除，请稍后重试")
        with self._lifecycle_lock:
            if active:
                try:
                    self.store.update(
                        job_id,
                        status="failed",
                        failure={
                            "code": "cancelled_by_user",
                            "message": "用户已从工作台取消并删除本次投递任务。",
                        },
                        finished_at=utc_now(),
                    )
                except FileNotFoundError:
                    pass
            plan = prepare_intake_job_deletion(
                self.worktrees_root,
                job_id,
                input_root=self.input_root,
            )
            result = plan.execute()
            self._controls.pop(job_id, None)
            result["canceled"] = active
            return result

    def close(self) -> None:
        with self._lifecycle_lock:
            if self._closed.is_set():
                return
            self._closed.set()
            self._download_queue.put(None)

    def _download_worker_loop(self) -> None:
        while True:
            submission = self._download_queue.get()
            try:
                if submission is None:
                    # The sentinel is queued after every accepted submission. Forward it
                    # only after all downloads have either failed or entered the audit
                    # queue, so close() cannot strand a verified ZIP between stages.
                    self._audit_queue.put(None)
                    return
                control = self._begin_job_stage(submission)
                if control is None:
                    continue
                try:
                    self._download(submission, control)
                finally:
                    self._end_job_stage(control)
                    self._release_terminal_control(submission, control)
            finally:
                self._download_queue.task_done()

    def _audit_worker_loop(self) -> None:
        while True:
            submission = self._audit_queue.get()
            try:
                if submission is None:
                    return
                control = self._begin_job_stage(submission)
                if control is None:
                    continue
                try:
                    self._audit(submission, control)
                finally:
                    self._end_job_stage(control)
                    self._release_terminal_control(submission, control)
            finally:
                self._audit_queue.task_done()

    def _begin_job_stage(self, submission: dict[str, Any]) -> _JobControl | None:
        control = self._controls.get(str(submission["job_id"]))
        if control is None:
            return None
        with control.lock:
            if control.cancel_event.is_set():
                return None
            control.active_stages += 1
            control.idle_event.clear()
            return control

    @staticmethod
    def _end_job_stage(control: _JobControl) -> None:
        with control.lock:
            control.active_stages = max(0, control.active_stages - 1)
            if control.active_stages == 0:
                control.idle_event.set()

    def _release_terminal_control(
        self,
        submission: dict[str, Any],
        control: _JobControl,
    ) -> None:
        job_id = str(submission["job_id"])
        try:
            terminal = str(self.store.get(job_id).get("status")) in TERMINAL_JOB_STATUSES
        except FileNotFoundError:
            terminal = True
        if terminal:
            with self._lifecycle_lock:
                if self._controls.get(job_id) is control:
                    self._controls.pop(job_id, None)

    @staticmethod
    def _raise_if_cancelled(control: _JobControl) -> None:
        if control.cancel_event.is_set():
            raise OSSIntakeCancelledError("投递任务已取消")

    def _download(self, submission: dict[str, Any], control: _JobControl) -> None:
        job_id = str(submission["job_id"])
        biz_type = submission.get("biz_type")
        if submission.get("contract") == "oss_ai_v1" and scenario_for_biz_type(biz_type) is None:
            # Empty input fails; other non-matching values complete without AI.
            # Neither case downloads materials or creates a formal worktree.
            submission.pop("download_url", None)
            if not (biz_type or "").strip():
                failure = {"code": "biz_type_unrecognized", "message": BIZ_TYPE_FAILURE_MESSAGE}
                result = {"run_id": submission["run_id"], "status": "failed", "failure": failure}
            else:
                failure = None
                result = biz_type_skip_result(submission["run_id"], biz_type)
            try:
                self._raise_if_cancelled(control)
                self.store.update(job_id, started_at=utc_now())
                self._finish_job(submission, control, result, failure=failure)
            except OSSIntakeCancelledError:
                pass
            except BaseException as exc:
                self._fail_job(submission, exc)
            return
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
            if self.downloader is download_oss_object:
                delivery = self.downloader(
                    submission,
                    destination,
                    self.config,
                    cancel_event=control.cancel_event,
                )
            else:
                # Existing test and integration downloaders keep their legacy
                # three-argument contract. Cancellation is still checked
                # immediately after they return; the bundled downloader also
                # checks it between streamed blocks.
                delivery = self.downloader(submission, destination, self.config)
            self._raise_if_cancelled(control)
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
            # Verification has made the local ZIP authoritative for the rest of
            # this attempt. Release the temporary credential before either
            # completing receive-only mode or waiting for the audit worker.
            submission.pop("download_url", None)
            if self.config.receive_only:
                self.store.update(
                    job_id,
                    status="completed",
                    delivery=delivery,
                    downloaded_at=utc_now(),
                    result=None,
                    callback={
                        "required": False,
                        "status": "not_required",
                        "attempts": 0,
                        "http_status": None,
                        "delivered_at": None,
                    },
                    failure=None,
                    finished_at=utc_now(),
                )
                return
            with control.lock:
                if control.cancel_event.is_set():
                    raise OSSIntakeCancelledError("投递任务已取消")
                self.store.update(
                    job_id,
                    status="downloaded",
                    delivery=delivery,
                    downloaded_at=utc_now(),
                    failure=None,
                )
                self._audit_queue.put(submission)
        except OSSIntakeCancelledError:
            return
        except BaseException as exc:
            self._fail_job(submission, exc)

    def _audit(self, submission: dict[str, Any], control: _JobControl) -> None:
        job_id = str(submission["job_id"])
        input_dir = self.input_root / job_id
        self.store.update(
            job_id,
            status="running",
            audit_started_at=utc_now(),
            failure=None,
        )
        try:
            result = self.runner(
                run_id=submission["run_id"],
                producer_model=self.config.producer_model,
                input_dir=input_dir,
                worktrees_root=self.worktrees_root,
                scenario=submission.get("scenario"),
                biz_type=submission.get("biz_type"),
                large_venue_fee=submission.get("large_venue_fee"),
                workbench_url=self.config.workbench_url,
                cancel_event=control.cancel_event,
            )
            self._raise_if_cancelled(control)
            result = project_run_status(result)
            biz_type_failed = (result.get("status") == "failed"
                                and (result.get("failure") or {}).get("code") == "biz_type_unrecognized")
            if result.get("status") != "completed" and not biz_type_failed:
                raise OSSIntakeExecutionError("正式核销运行返回的结果收据无效")
            self._finish_job(submission, control, result,
                             failure=result["failure"] if biz_type_failed else None)
        except OSSIntakeCancelledError:
            return
        except BaseException as exc:
            self._fail_job(submission, exc)

    def _finish_job(
        self, submission: dict[str, Any], control: _JobControl,
        result: dict[str, Any], *, failure: dict[str, Any] | None = None,
    ) -> None:
        job_id = str(submission["job_id"])
        callback: dict[str, Any]
        if submission.get("callback_required"):
            callback_payload = {
                "verifyCode": submission["verify_code"],
                "analyzeId": submission["analyze_id"],
                "result": build_callback_result(result),
            }
            self.store.update(
                job_id,
                status="callback",
                result=result,
                failure=failure,
                callback={
                    "required": True,
                    "status": "sending",
                    "attempts": 0,
                    "http_status": None,
                    "delivered_at": None,
                },
            )
            self._raise_if_cancelled(control)
            callback = self.callback_sender(callback_payload, self.config)
            self._raise_if_cancelled(control)
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
            status="failed" if failure else "completed",
            result=result,
            callback=callback,
            failure=failure,
            finished_at=utc_now(),
        )

    def _fail_job(
        self,
        submission: dict[str, Any],
        exc: BaseException,
    ) -> None:
        job_id = str(submission["job_id"])
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
            str(self.config.callback_url or ""),
            str(self.config.callback_token or ""),
        }
        for secret in secrets_to_hide:
            if secret:
                message = message.replace(secret, "[已隐藏]")
        message = re.sub(r"https://[^\s]+", "HTTPS 地址（已隐藏）", message)
        return message[-2000:]


def public_job(job: dict[str, Any], *, input_root: Path = DEFAULT_OSS_INPUT_ROOT) -> dict[str, Any]:
    """Return the public API projection without internal fingerprints."""

    result = {
        key: value
        for key, value in job.items()
        if key not in INTERNAL_JOB_FIELDS
    }
    # Project old receipts without rewriting immutable job history.
    if "fileId" in result:
        result.setdefault("analyzeId", result.pop("fileId"))
    if "planType" in result:
        result.setdefault("bizType", result.pop("planType"))
    result.setdefault("bizType", None)
    result.setdefault("largeVenueFee", None)
    delivery = result.get("delivery")
    job_id = str(result.get("job_id") or "")
    if isinstance(delivery, dict) and JOB_ID_PATTERN.fullmatch(job_id):
        old_dir = PurePosixPath(str(delivery.get("input_directory") or "").replace("\\", "/"))
        old_file = PurePosixPath(str(delivery.get("input_file") or "").replace("\\", "/"))
        if (old_dir.parent.name == "input-oss" and old_dir.name == job_id
                and old_file.parent == old_dir and old_file.suffix.lower() == ".zip"):
            directory = input_root.resolve() / job_id
            migrated = directory / old_file.name
            if migrated.is_file() and not directory.is_symlink() and not migrated.is_symlink():
                result["delivery"] = {**delivery, "input_directory": str(directory), "input_file": str(migrated)}
    return result
