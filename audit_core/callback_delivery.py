"""Explicit, receipt-backed delivery of an immutable completed local result."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import sys
from typing import Any, Callable, Iterator

from .common import AuditError
from .oss_intake import (
    ACTIVE_JOB_STATUSES, DEFAULT_CALLBACK_ATTEMPTS, DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    MAX_CALLBACK_RESULT_CHARS, OSSIntakeConfig, VERIFY_CODE_PATTERN,
    post_analysis_callback,
)
from .workbench_store import (
    ANALYSIS_SUMMARY_FILENAME, DEFAULT_WORKTREES_ROOT, WORKSPACE_ID_PATTERN,
    atomic_write_json, utc_now,
)


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _encoded(value: Any) -> bytes:
    # Exactly matches the existing callback sender's payload encoding.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _is_link(path: Path) -> bool:
    return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())


def _read_file(path: Path) -> bytes:
    if _is_link(path) or not path.is_file():
        raise AuditError(f"补发所需档案必须是普通文件：{path.name}")
    return path.read_bytes()


def _read_object(content: bytes, name: str) -> dict[str, Any]:
    try:
        value = json.loads(content)
    except (ValueError, UnicodeError):
        raise AuditError(f"补发所需 {name} 不是有效 JSON") from None
    if not isinstance(value, dict):
        raise AuditError(f"补发所需 {name} 必须是对象")
    return value


def prepare_callback_delivery(
    worktrees_root: Path, *, workspace_id: str, verify_code: str,
    analyze_id: int, config: OSSIntakeConfig,
) -> dict[str, Any]:
    """Validate identity supplied by the operator, then bind exact saved bytes."""
    if not isinstance(workspace_id, str) or WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None:
        raise AuditError("必须显式提供安全的 workspace_id")
    if not isinstance(verify_code, str) or VERIFY_CODE_PATTERN.fullmatch(verify_code) is None:
        raise AuditError("verifyCode 必须是 1～80 位安全业务编号")
    if isinstance(analyze_id, bool) or not isinstance(analyze_id, int) or not 1 <= analyze_id <= 9_223_372_036_854_775_807:
        raise AuditError("analyzeId 必须是 64 位正整数")
    if not config.callback_url:
        raise AuditError("未配置 OSS 分析结果回调地址")
    root = Path(worktrees_root).resolve()
    workspace = root / workspace_id
    if _is_link(workspace) or not workspace.is_dir() or workspace.resolve().parent != root:
        raise AuditError("指定的 worktree 不存在或不是直属真实目录")
    manifest_bytes = _read_file(workspace / "manifest.json")
    snapshot_bytes = _read_file(workspace / "snapshot.json")
    manifest = _read_object(manifest_bytes, "manifest.json")
    snapshot = _read_object(snapshot_bytes, "snapshot.json")
    run = snapshot.get("run")
    if not isinstance(run, dict) or not isinstance(snapshot.get("view"), dict):
        raise AuditError("snapshot.json 缺少有效运行信息或结果视图")
    if manifest.get("workspace_id") != workspace_id or run.get("workspace_id") != workspace_id:
        raise AuditError("指定 worktree 与 manifest/snapshot 的 workspace_id 不一致")
    if manifest.get("status") != "completed" or run.get("status") != "completed":
        raise AuditError("仅允许补发已完成的 worktree")
    if not isinstance(manifest.get("run_id"), str) or not manifest["run_id"]:
        raise AuditError("已完成档案缺少 run_id")
    if manifest.get("snapshot_sha256") != _digest(snapshot_bytes):
        raise AuditError("snapshot.json SHA-256 与 manifest 不一致")
    # Snapshot is written immediately before manifest. These two values are
    # updated by that final write and intentionally cannot be identical.
    final_write_fields = {"snapshot_sha256", "updated_at"}
    if ({key: value for key, value in manifest.items() if key not in final_write_fields}
            != {key: value for key, value in run.items() if key not in final_write_fields}):
        raise AuditError("manifest 与 snapshot 的运行信息不一致")
    for key, expected in (("verifyCode", verify_code), ("analyzeId", analyze_id)):
        if key in manifest and manifest[key] != expected:
            raise AuditError("操作员指定的业务身份与档案已保存身份不一致")
    metadata = manifest.get("analysis_summary")
    if not isinstance(metadata, dict) or metadata.get("path") != ANALYSIS_SUMMARY_FILENAME:
        raise AuditError("摘要必须指向本 worktree 的固定 AI 分析小结")
    content = _read_file(workspace / ANALYSIS_SUMMARY_FILENAME)
    if (type(metadata.get("size")) is not int or metadata["size"] != len(content)
            or metadata.get("sha256") != _digest(content)):
        raise AuditError("AI 分析小结的大小或 SHA-256 不一致")
    try:
        summary = content.decode("utf-8").strip()
    except UnicodeError:
        raise AuditError("AI 分析小结不是有效 UTF-8") from None
    if not summary:
        raise AuditError("AI 分析小结为空，不能补发")
    if len(summary) > MAX_CALLBACK_RESULT_CHARS or "回调内容已截断" in summary:
        raise AuditError("AI 分析小结超过回调上限或已截断，不能补发不完整结果")
    payload = {"verifyCode": verify_code, "analyzeId": analyze_id, "result": summary}
    payload_hash = _digest(_encoded(payload))
    target_hash = _digest(config.callback_url.encode("utf-8"))
    identity = {"verifyCode": verify_code, "analyzeId": analyze_id}
    receipt_id = _digest(_encoded({**identity, "payload_sha256": payload_hash, "target_sha256": target_hash}))
    return {
        "schema_version": "1.0", "delivery_id": receipt_id,
        "workspace_id": workspace_id, "run_id": manifest["run_id"],
        **identity, "identity_source": "operator_explicit",
        "manifest_sha256": _digest(manifest_bytes), "snapshot_sha256": _digest(snapshot_bytes),
        "summary_sha256": _digest(content), "summary_size": len(content),
        "payload_sha256": payload_hash, "target_sha256": target_hash,
        "idempotency_key": f"{verify_code}:{analyze_id}:{payload_hash[:16]}",
        "payload": payload,
    }


def _reject_active_oss_delivery(root: Path, verify_code: str, analyze_id: int) -> None:
    """Read existing receipts directly; never initialize or recover the OSS store."""
    jobs = root / ".intake" / "jobs"
    if _is_link(jobs) or jobs.resolve().parent != (root / ".intake").resolve():
        raise AuditError("OSS 任务回执目录不是直属真实目录")
    if not jobs.exists():
        return
    for path in jobs.glob("*.json"):
        job = _read_object(_read_file(path), "OSS 任务回执")
        if (job.get("verifyCode") == verify_code
                and job.get("analyzeId", job.get("fileId")) == analyze_id
                and job.get("status") in ACTIVE_JOB_STATUSES):
            raise AuditError("同一业务仍有 OSS 任务执行中，暂不能补发本地结果")


@contextmanager
def _delivery_lock(path: Path) -> Iterator[None]:
    """OS lock releases on process exit; a persistent lock file is harmless."""
    if _is_link(path):
        raise AuditError("补发锁文件不能是符号链接")
    with path.open("a+b") as stream:
        if stream.seek(0, os.SEEK_END) == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise AuditError("同一业务目标已有补发正在执行，请等待该投递结束") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def deliver_completed_callback(
    worktrees_root: Path = DEFAULT_WORKTREES_ROOT, *, workspace_id: str,
    verify_code: str, analyze_id: int, config: OSSIntakeConfig,
    apply: bool = False,
    sender: Callable[[dict[str, Any], OSSIntakeConfig], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    prepared = prepare_callback_delivery(worktrees_root, workspace_id=workspace_id,
                                         verify_code=verify_code, analyze_id=analyze_id, config=config)
    root = Path(worktrees_root).resolve()
    storage = root / ".intake" / "callback-deliveries"
    if (_is_link(storage.parent) or _is_link(storage)
            or storage.parent.resolve() != root / ".intake"
            or storage.resolve().parent != storage.parent.resolve()):
        raise AuditError("补发回执目录不能是符号链接")
    _reject_active_oss_delivery(root, verify_code, analyze_id)
    receipt_path = storage / f"{prepared['delivery_id']}.json"

    def existing_receipt() -> dict[str, Any] | None:
        if not receipt_path.exists():
            return None
        old = _read_object(_read_file(receipt_path), "补发回执")
        for key in ("delivery_id", "verifyCode", "analyzeId", "payload_sha256", "target_sha256"):
            if old.get(key) != prepared[key]:
                raise AuditError("既有补发回执与本次业务或正文不一致")
        return old

    old = existing_receipt()
    if not apply:
        return {**prepared, "mode": "dry_run", "status": "already_delivered" if old and old.get("status") == "delivered" else "ready"}
    storage.mkdir(parents=True, exist_ok=True)
    lock_id = _digest(_encoded({"verifyCode": verify_code, "analyzeId": analyze_id, "target_sha256": prepared["target_sha256"]}))
    with _delivery_lock(storage / f"{lock_id}.lock"):
        _reject_active_oss_delivery(root, verify_code, analyze_id)
        current = prepare_callback_delivery(worktrees_root, workspace_id=workspace_id,
                                            verify_code=verify_code, analyze_id=analyze_id, config=config)
        if current != prepared:
            raise AuditError("等待投递期间档案发生变化，请重新核对")
        old = existing_receipt()
        if old and old.get("status") == "delivered":
            return {**old, "mode": "apply", "status": "already_delivered"}
        receipt = {key: value for key, value in prepared.items() if key != "payload"}
        history = list((old or {}).get("deliveries") or [])
        receipt.update(status="sending", created_at=(old or {}).get("created_at") or utc_now(), updated_at=utc_now(), deliveries=history)
        transmission = {"number": len(history) + 1, "status": "sending", "started_at": utc_now()}
        history.append(transmission)
        atomic_write_json(receipt_path, receipt)
        try:
            outcome = (sender or post_analysis_callback)(prepared["payload"], config)
            http_status = int(outcome.get("http_status") or 0)
            if outcome.get("status") != "delivered" or not 200 <= http_status < 300:
                raise AuditError("发送函数未返回成功投递回执")
        except Exception as exc:
            transmission.update(status="failed", finished_at=utc_now(),
                                attempts=int(getattr(exc, "attempts", 0) or 0),
                                http_status=getattr(exc, "http_status", None), error_type=type(exc).__name__)
            receipt.update(status="failed", updated_at=utc_now())
            atomic_write_json(receipt_path, receipt)
            raise AuditError("回调投递失败；独立回执已保存，可用相同参数仅重试投递") from None
        transmission.update(status="delivered", finished_at=utc_now(),
                            attempts=int(outcome.get("attempts") or 1), http_status=http_status,
                            delivered_at=outcome.get("delivered_at") or utc_now())
        receipt.update(status="delivered", updated_at=utc_now(), delivered_at=transmission["delivered_at"])
        try:
            atomic_write_json(receipt_path, receipt)
        except OSError:
            raise AuditError("回调已返回成功，但投递回执保存失败；请用相同参数和幂等键重试确认") from None
        return {**receipt, "mode": "apply"}


def _environment_config() -> OSSIntakeConfig:
    try:
        timeout = int(os.environ.get("OFFLINE_AUDIT_OSS_CALLBACK_TIMEOUT") or DEFAULT_CALLBACK_TIMEOUT_SECONDS)
        attempts = int(os.environ.get("OFFLINE_AUDIT_OSS_CALLBACK_ATTEMPTS") or DEFAULT_CALLBACK_ATTEMPTS)
    except ValueError:
        raise AuditError("回调超时和重试次数环境变量必须是整数") from None
    return OSSIntakeConfig(
        # The shared config requires a download host. This tool never downloads.
        allowed_hosts=("callback-only.invalid",),
        callback_url=os.environ.get("OFFLINE_AUDIT_OSS_CALLBACK_URL"),
        callback_token=os.environ.get("OFFLINE_AUDIT_OSS_CALLBACK_TOKEN"),
        allow_http_callback=str(os.environ.get("OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK") or "").lower() in {"1", "true", "yes", "on"},
        callback_timeout_seconds=timeout, callback_attempts=attempts,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="校验已完成 worktree 并补发三字段回调；默认只预览")
    parser.add_argument("--worktrees-root", type=Path, default=DEFAULT_WORKTREES_ROOT)
    parser.add_argument("--workspace-id", required=True)
    parser.add_argument("--verify-code", required=True)
    parser.add_argument("--analyze-id", required=True, type=int)
    parser.add_argument("--apply", action="store_true", help="实际投递，并保存独立补发回执")
    args = parser.parse_args(argv)
    try:
        result = deliver_completed_callback(args.worktrees_root, workspace_id=args.workspace_id,
            verify_code=args.verify_code, analyze_id=args.analyze_id, config=_environment_config(), apply=args.apply)
    except AuditError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError):
        print("补发档案或回执读取失败；未输出环境凭据或目标地址", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
