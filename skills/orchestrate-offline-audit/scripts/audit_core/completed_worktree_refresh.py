"""Backed-up refresh of completed customer artifacts without re-running audits."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
from typing import Any
import uuid

from .common import AuditError
from .error_reason import attach_error_reasons
from .workbench_html import (
    STATIC_ARCHIVE_FILENAME,
    SYSTEM_VERSION,
    projected_view_error_count,
    render_static_run_archive_html,
)
from .workbench_store import (
    ANALYSIS_SUMMARY_FILENAME,
    DEFAULT_WORKTREES_ROOT,
    PROJECT_ROOT,
    WORKSPACE_ID_PATTERN,
    atomic_write_text,
    render_analysis_summary_markdown,
)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _assert_plain_path(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink() or (hasattr(candidate, "is_junction") and candidate.is_junction()):
            raise AuditError(f"刷新路径不能经过符号链接或目录联接：{candidate}")


def _current_bytes(path: Path) -> bytes | None:
    _assert_plain_path(path)
    if not path.exists():
        return None
    if not path.is_file():
        raise AuditError(f"刷新目标不是普通文件：{path}")
    return path.read_bytes()


def _atomic_restore(path: Path, content: bytes | None) -> None:
    if content is None:
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _prepare_workspace(workspace: Path, root_html: Path) -> dict[str, Any]:
    manifest_path, snapshot_path = workspace / "manifest.json", workspace / "snapshot.json"
    original_manifest = _current_bytes(manifest_path)
    if original_manifest is None:
        return {"workspace_id": workspace.name, "skip": "missing_run_data"}
    try:
        manifest = json.loads(original_manifest)
    except (ValueError, UnicodeError) as exc:
        raise AuditError(f"{workspace.name} 的运行数据不是有效 JSON") from exc
    if not isinstance(manifest, dict):
        raise AuditError(f"{workspace.name} 的运行数据结构无效")
    if manifest.get("status") != "completed":
        return {"workspace_id": workspace.name, "skip": "not_completed"}
    original_snapshot = _current_bytes(snapshot_path)
    if original_snapshot is None:
        return {"workspace_id": workspace.name, "skip": "missing_run_data"}
    try:
        snapshot = json.loads(original_snapshot)
    except (ValueError, UnicodeError) as exc:
        raise AuditError(f"{workspace.name} 的运行数据不是有效 JSON") from exc
    if not isinstance(snapshot, dict):
        raise AuditError(f"{workspace.name} 的运行数据结构无效")
    if manifest.get("workspace_id") != workspace.name:
        raise AuditError(f"{workspace.name} 的运行 ID 与目录不一致")
    if not isinstance(snapshot.get("view"), dict) or not isinstance(snapshot.get("run"), dict):
        raise AuditError(f"{workspace.name} 的 snapshot.json 结构无效")
    if snapshot["run"].get("workspace_id") != workspace.name or snapshot["run"].get("status") != "completed":
        raise AuditError(f"{workspace.name} 的快照运行身份或完成状态不一致")
    if "snapshot_sha256" in manifest and manifest["snapshot_sha256"] != _digest(original_snapshot):
        raise AuditError(f"{workspace.name} 的原始 snapshot.json 摘要校验失败，未刷新")

    projected = attach_error_reasons(snapshot["view"])
    error_count = projected_view_error_count(projected)
    summary_manifest = dict(manifest, error_count=error_count)
    summary = render_analysis_summary_markdown(projected, summary_manifest).encode("utf-8")
    metadata = {"path": ANALYSIS_SUMMARY_FILENAME, "size": len(summary), "sha256": _digest(summary)}
    updated_snapshot = deepcopy(snapshot)
    updated_snapshot["run"]["analysis_summary"] = metadata
    # The manifest binds this derived metadata to the final snapshot digest.
    # Copying it inside the snapshot would make that digest self-referential.
    updated_snapshot["run"].pop("customer_projection", None)
    snapshot_bytes = original_snapshot if updated_snapshot == snapshot else _json_bytes(updated_snapshot)
    updated_manifest = deepcopy(manifest)
    updated_manifest["analysis_summary"] = metadata
    updated_manifest["snapshot_sha256"] = _digest(snapshot_bytes)
    updated_manifest["customer_projection"] = {
        "schema_version": "1.0", "policy_version": SYSTEM_VERSION,
        "source_snapshot_sha256": _digest(snapshot_bytes), "error_count": error_count,
    }
    manifest_bytes = original_manifest if updated_manifest == manifest else _json_bytes(updated_manifest)
    html = render_static_run_archive_html(
        workspace, root_html, manifest=updated_manifest, snapshot=updated_snapshot,
    ).encode("utf-8")
    expected = {
        ANALYSIS_SUMMARY_FILENAME: summary,
        "snapshot.json": snapshot_bytes,
        "manifest.json": manifest_bytes,
        STATIC_ARCHIVE_FILENAME: html,
    }
    originals = {
        "manifest.json": original_manifest, "snapshot.json": original_snapshot,
        ANALYSIS_SUMMARY_FILENAME: _current_bytes(workspace / ANALYSIS_SUMMARY_FILENAME),
        STATIC_ARCHIVE_FILENAME: _current_bytes(workspace / STATIC_ARCHIVE_FILENAME),
    }
    changes = [
        {"path": workspace / name, "relative_path": f"{workspace.name}/{name}",
         "before": originals[name], "after": content}
        for name, content in expected.items() if originals[name] != content
    ]
    return {
        "workspace_id": workspace.name, "changes": changes, "originals": originals,
        "workspace": workspace, "summary": summary.decode("utf-8"),
        "error_count_before": manifest.get("error_count", 0), "error_count_after": error_count,
    }


def refresh_completed_worktree_archives(
    worktrees_root: Path = DEFAULT_WORKTREES_ROOT,
    *,
    root_html: Path | None = None,
    backup_dir: Path | None = None,
    apply: bool = False,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    """Prepare the whole refresh before backing up and writing any worktree."""
    root = Path(worktrees_root).absolute()
    template = Path(root_html or PROJECT_ROOT / "skills/orchestrate-offline-audit/assets" / STATIC_ARCHIVE_FILENAME).absolute()
    _assert_plain_path(root)
    _assert_plain_path(template)
    root, template = root.resolve(), template.resolve()
    if not root.is_dir():
        raise AuditError(f"worktrees 目录不存在：{root}")
    if workspace_id is not None and (
        WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None or not (root / workspace_id).is_dir()
    ):
        raise AuditError("指定的 worktree 不存在或名称不安全")
    if not template.is_file():
        raise AuditError(f"固定工作台不存在：{template}")
    backup = Path(backup_dir).absolute() if backup_dir is not None else None
    if backup is not None:
        _assert_plain_path(backup)
        backup = backup.resolve()
    template_project = (
        template.parents[3]
        if template.parts[-4:-1] == ("skills", "orchestrate-offline-audit", "assets")
        else template.parent
    )
    artifacts = template_project / "artifacts"
    if backup is not None and (backup == artifacts or artifacts not in backup.parents):
        raise AuditError("--backup-dir 必须是本项目 artifacts 下的独立目录")
    if apply and backup is None:
        raise AuditError("刷新静态档案时必须显式提供 --backup-dir，以备份全部待修改文件")

    prepared, skipped = [], []
    for workspace in sorted(root.iterdir(), key=lambda path: path.name):
        if workspace_id is not None and workspace.name != workspace_id:
            continue
        if workspace.name.startswith("."):
            continue
        _assert_plain_path(workspace)
        if not workspace.is_dir():
            continue
        if WORKSPACE_ID_PATTERN.fullmatch(workspace.name) is None:
            skipped.append({"workspace_id": workspace.name, "reason": "invalid_name"})
            continue
        item = _prepare_workspace(workspace, template)
        if "skip" in item:
            skipped.append({"workspace_id": workspace.name, "reason": item["skip"]})
        else:
            prepared.append(item)

    changes = [change for item in prepared for change in item["changes"]]
    if apply and changes:
        # Reject concurrent changes before creating backups, then check again
        # after all backups exist and before the first target is replaced.
        def verify_originals() -> None:
            for item in prepared:
                for filename, content in item["originals"].items():
                    if _current_bytes(item["workspace"] / filename) != content:
                        raise AuditError(f"{item['workspace_id']}/{filename} 在预览后发生变化，未应用刷新")

        verify_originals()
        if backup.exists():
            raise AuditError("备份目录已存在；请为本次刷新指定新的独立目录")
        backup.mkdir(parents=True, exist_ok=False)
        backup_records = []
        for change in changes:
            target = backup / change["relative_path"]
            before = change["before"]
            if before is not None:
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as handle:
                    handle.write(before)
                if target.read_bytes() != before:
                    raise AuditError(f"备份校验失败：{change['relative_path']}")
            backup_records.append({
                "path": change["relative_path"], "existed": before is not None,
                "before_sha256": _digest(before) if before is not None else None,
                "after_sha256": _digest(change["after"]),
            })
        with (backup / "backup-index.json").open("xb") as handle:
            handle.write(_json_bytes({"schema_version": "1.0", "worktrees_root": str(root), "files": backup_records}))
        verify_originals()
        written = []
        try:
            for change in changes:
                written.append(change)
                atomic_write_text(change["path"], change["after"].decode("utf-8"))
            for change in changes:
                if _current_bytes(change["path"]) != change["after"]:
                    raise AuditError(f"刷新校验失败：{change['relative_path']}")
        except Exception:
            for change in reversed(written):
                _atomic_restore(change["path"], change["before"])
            raise

    results = []
    for item in prepared:
        results.append({
            "workspace_id": item["workspace_id"],
            "status": ("updated" if apply else "would_update") if item["changes"] else "unchanged",
            "error_count_before": item["error_count_before"], "error_count_after": item["error_count_after"],
            "summary": item["summary"],
            "files": [{"path": change["relative_path"], "existed": change["before"] is not None,
                       "before_sha256": _digest(change["before"]) if change["before"] is not None else None,
                       "after_sha256": _digest(change["after"])} for change in item["changes"]],
        })
    return {
        "mode": "apply" if apply else "dry_run", "refresh_archives": True,
        "backup_dir": str(backup) if backup is not None else None,
        "updated": sum(item["status"] == "updated" for item in results),
        "would_update": sum(item["status"] == "would_update" for item in results),
        "unchanged": sum(item["status"] == "unchanged" for item in results),
        "results": results, "skipped": skipped,
    }
