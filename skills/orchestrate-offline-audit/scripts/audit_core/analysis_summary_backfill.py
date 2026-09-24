from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from .common import AuditError
from .workbench_store import (
    ANALYSIS_SUMMARY_FILENAME,
    DEFAULT_WORKTREES_ROOT,
    WORKSPACE_ID_PATTERN,
    atomic_write_json,
    atomic_write_text,
    read_json_file,
    render_analysis_summary_markdown,
)


def _summary_metadata(content: str) -> dict[str, Any]:
    encoded = content.encode("utf-8")
    return {
        "path": ANALYSIS_SUMMARY_FILENAME,
        "size": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
    }


def backfill_analysis_summaries(
    worktrees_root: Path = DEFAULT_WORKTREES_ROOT,
    *,
    apply: bool = False,
    refresh_archives: bool = False,
    backup_dir: Path | None = None,
    root_html: Path | None = None,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    if refresh_archives:
        from .completed_worktree_refresh import refresh_completed_worktree_archives

        return refresh_completed_worktree_archives(
            worktrees_root, apply=apply, backup_dir=backup_dir, root_html=root_html, workspace_id=workspace_id,
        )
    if backup_dir is not None:
        raise AuditError("--backup-dir 仅与 --refresh-archives 一起使用")
    root = worktrees_root.resolve()
    if not root.is_dir():
        raise AuditError(f"worktrees 目录不存在：{root}")
    if workspace_id is not None and (
        WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None or not (root / workspace_id).is_dir()
    ):
        raise AuditError("指定的 worktree 不存在或名称不安全")

    prepared: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for workspace in sorted(root.iterdir(), key=lambda path: path.name):
        if workspace_id is not None and workspace.name != workspace_id:
            continue
        if (
            workspace.name.startswith(".")
            or not workspace.is_dir()
            or workspace.is_symlink()
        ):
            continue
        if WORKSPACE_ID_PATTERN.fullmatch(workspace.name) is None:
            skipped.append({"workspace_id": workspace.name, "reason": "invalid_name"})
            continue
        manifest_path = workspace / "manifest.json"
        snapshot_path = workspace / "snapshot.json"
        if not manifest_path.is_file() or not snapshot_path.is_file():
            skipped.append({"workspace_id": workspace.name, "reason": "missing_run_data"})
            continue

        manifest = read_json_file(manifest_path)
        snapshot = read_json_file(snapshot_path)
        classification_failed = (manifest.get("status") == "failed"
                                 and (manifest.get("failure") or {}).get("code") == "classification_failed")
        if manifest.get("status") != "completed" and not classification_failed:
            skipped.append({"workspace_id": workspace.name, "reason": "not_completed"})
            continue
        view_payload = snapshot.get("view")
        snapshot_run = snapshot.get("run")
        if not isinstance(view_payload, dict) or not isinstance(snapshot_run, dict):
            raise AuditError(f"{workspace.name} 的 snapshot.json 结构无效")

        summary = render_analysis_summary_markdown(view_payload, manifest)
        metadata = _summary_metadata(summary)
        summary_path = workspace / ANALYSIS_SUMMARY_FILENAME
        current_summary = None
        if summary_path.is_file():
            try:
                current_summary = summary_path.read_text(encoding="utf-8")
            except UnicodeError as exc:
                raise AuditError(f"{workspace.name} 的现有小结不是有效 UTF-8") from exc
        snapshot_digest = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        current = (
            current_summary == summary
            and manifest.get("analysis_summary") == metadata
            and snapshot_run.get("analysis_summary") == metadata
            and manifest.get("snapshot_sha256") == snapshot_digest
        )
        prepared.append(
            {
                "workspace": workspace,
                "manifest_path": manifest_path,
                "snapshot_path": snapshot_path,
                "summary_path": summary_path,
                "manifest": manifest,
                "snapshot": snapshot,
                "summary": summary,
                "metadata": metadata,
                "current": current,
            }
        )

    results: list[dict[str, Any]] = []
    for item in prepared:
        workspace = item["workspace"]
        if item["current"]:
            results.append({"workspace_id": workspace.name, "status": "unchanged"})
            continue
        if not apply:
            results.append({"workspace_id": workspace.name, "status": "would_update"})
            continue

        atomic_write_text(item["summary_path"], item["summary"])
        metadata = _summary_metadata(item["summary_path"].read_text(encoding="utf-8"))
        manifest = item["manifest"]
        snapshot = item["snapshot"]
        manifest["analysis_summary"] = metadata
        snapshot["run"]["analysis_summary"] = metadata
        atomic_write_json(item["snapshot_path"], snapshot)
        manifest["snapshot_sha256"] = hashlib.sha256(
            item["snapshot_path"].read_bytes()
        ).hexdigest()
        atomic_write_json(item["manifest_path"], manifest)
        results.append(
            {
                "workspace_id": workspace.name,
                "status": "updated",
                "summary_size": metadata["size"],
                "summary_sha256": metadata["sha256"],
            }
        )

    return {
        "mode": "apply" if apply else "dry_run",
        "updated": sum(item["status"] == "updated" for item in results),
        "would_update": sum(item["status"] == "would_update" for item in results),
        "unchanged": sum(item["status"] == "unchanged" for item in results),
        "results": results,
        "skipped": skipped,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="按当前错误原因规范刷新已完成 worktree 的摘要；可选备份后同步静态档案",
    )
    parser.add_argument(
        "--worktrees-root",
        type=Path,
        default=DEFAULT_WORKTREES_ROOT,
        help="worktrees 根目录；默认使用当前项目 worktrees",
    )
    parser.add_argument(
        "--workspace-id",
        help="仅刷新指定终态 worktree 的摘要，包含核销方式无法确认的失败记录",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="实际写入；省略时只显示将要更新的目录",
    )
    parser.add_argument(
        "--refresh-archives", action="store_true",
        help="同时刷新静态 HTML 和客户展示数量；默认先预览全部已完成目录",
    )
    parser.add_argument(
        "--backup-dir", type=Path,
        help="本项目 artifacts 下新的独立备份目录；--refresh-archives --apply 时必填",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = backfill_analysis_summaries(
        args.worktrees_root, apply=args.apply, refresh_archives=args.refresh_archives,
        backup_dir=args.backup_dir,
        workspace_id=args.workspace_id,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
