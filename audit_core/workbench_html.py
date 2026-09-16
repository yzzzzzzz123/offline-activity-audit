from __future__ import annotations

import json
from copy import deepcopy
from html import escape as escape_html_attribute
from pathlib import Path
from typing import Any

from .common import AuditError
from .error_reason import attach_error_reasons
from .html_report import DATA_CLOSE, DATA_OPEN
from .pass_check_log import attach_pass_check_log, load_workspace_results
from .workbench_store import read_event_records, atomic_write_text, main_flow_task_list, project_run_status, read_json_file


API_VERSION = "1.40"
SYSTEM_VERSION = "2.11.27"
STATIC_ARCHIVE_FILENAME = "offline-activity-audit.html"
WORKBENCH_CONTEXT_ID = "audit-workbench-context"
STATIC_ARCHIVE_ID = "audit-static-archive"
PAGE_MODE_META_NAME = "offline-audit-page-mode"
DELIVERY_MODE_META_NAME = "offline-audit-delivery-mode"
VIEW_AVAILABLE_META_NAME = "offline-audit-view-available"


def customer_projection_error_count(manifest: dict[str, Any]) -> int:
    """Read a versioned derived count without opening a completed snapshot."""
    original = int(manifest.get("error_count") or 0)
    projection = manifest.get("customer_projection")
    if manifest.get("status") != "completed" or not isinstance(projection, dict):
        return original
    digest = manifest.get("snapshot_sha256")
    count = projection.get("error_count")
    if (
        projection.get("schema_version") != "1.0"
        or projection.get("policy_version") != SYSTEM_VERSION
        or not isinstance(digest, str) or len(digest) != 64
        or projection.get("source_snapshot_sha256") != digest
        or type(count) is not int or count < 0
    ):
        return original
    return count


def projected_view_error_count(view: dict[str, Any]) -> int:
    return sum(int((sheet.get("audit_counts") or {}).get("error_count") or 0)
               for sheet in view.get("sheets") or [] if isinstance(sheet, dict))


def script_json(value: Any) -> str:
    """Serialize JSON safely for an inline application/json script element."""

    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def replace_audit_payload(html: str, payload: dict[str, Any]) -> str:
    start = html.find(DATA_OPEN)
    if start < 0:
        raise ValueError("固定页面缺少 audit-data 数据槽")
    start += len(DATA_OPEN)
    end = html.find(DATA_CLOSE, start)
    if end < 0:
        raise ValueError("固定页面的 audit-data 数据槽未闭合")
    return html[:start] + script_json(payload) + html[end:]


def inject_workbench_context(html: str, context: dict[str, Any]) -> str:
    closing_head = html.lower().find("</head>")
    if closing_head < 0:
        raise ValueError("固定页面缺少 head 闭合标签")
    context_payload = dict(context)
    static_archive = context_payload.pop("static_archive", None)
    page_mode = escape_html_attribute(str(context_payload.get("mode") or ""), quote=True)
    delivery_mode = escape_html_attribute(
        str(context_payload.get("delivery_mode") or ""), quote=True
    )
    view_available = "true" if context_payload.get("view_available") else "false"
    tags = (
        f'<meta name="{PAGE_MODE_META_NAME}" content="{page_mode}">\n'
        f'<meta name="{DELIVERY_MODE_META_NAME}" content="{delivery_mode}">\n'
        f'<meta name="{VIEW_AVAILABLE_META_NAME}" content="{view_available}">\n'
        f'<script id="{WORKBENCH_CONTEXT_ID}" type="application/json">'
        f"{script_json(context_payload)}</script>\n"
    )
    if static_archive is not None:
        tags += (
            f'<script id="{STATIC_ARCHIVE_ID}" type="application/json">'
            f"{script_json(static_archive)}</script>\n"
        )
    return html[:closing_head] + tags + html[closing_head:]


def _workspace_resource(workspace: Path, relative_path: str) -> Path:
    relative = Path(relative_path)
    if not relative_path or relative.is_absolute() or ".." in relative.parts:
        raise AuditError("静态档案资源路径越出运行目录")
    target = (workspace / relative).resolve()
    if workspace not in target.parents or not target.is_file():
        raise AuditError(f"静态档案资源不存在：{relative_path}")
    return target


def _static_run_summary(manifest: dict[str, Any], workspace: Path | None = None) -> dict[str, Any]:
    from .workbench_labels import record_labels

    summary = project_run_status(manifest)
    summary.update(record_labels(manifest, workspace))
    summary["storage_type"] = "worktree"
    summary["main_flow_tasks"] = main_flow_task_list()
    summary["error_count"] = customer_projection_error_count(manifest)
    summary.setdefault("manual_reviewed", False)
    summary.setdefault("manual_reviewed_at", None)
    return summary


def render_static_run_archive_html(
    workspace: str | Path,
    root_html: str | Path,
    *,
    workbench_url: str | None = None,
    manifest: dict[str, Any] | None = None,
    snapshot: dict[str, Any] | None = None,
) -> str:
    """Prepare a self-contained archive without writing files or mutating input."""

    workspace_path = Path(workspace).resolve()
    root_html_path = Path(root_html).resolve()
    snapshot = deepcopy(snapshot) if snapshot is not None else read_json_file(workspace_path / "snapshot.json")
    manifest = deepcopy(manifest) if manifest is not None else read_json_file(workspace_path / "manifest.json")
    workspace_id = str(manifest.get("workspace_id") or workspace_path.name)
    if workspace_id != workspace_path.name:
        raise AuditError("静态档案运行 ID 与目录名称不一致")

    manifest = project_run_status(manifest, snapshot.get("view"))
    run = _static_run_summary(manifest, workspace_path)
    review_path = workspace_path.parent / ".reviews" / f"{workspace_id}.json"
    if review_path.is_file():
        review = read_json_file(review_path)
        if review.get("workspace_id") == workspace_id and isinstance(review.get("reviewed"), bool):
            run["manual_reviewed"] = review["reviewed"]
            run["manual_reviewed_at"] = review.get("reviewed_at") if review["reviewed"] else None
    snapshot["run"] = run
    snapshot["view"] = attach_pass_check_log(
        snapshot.get("view"),
        load_workspace_results(workspace_path),
    )
    snapshot["view"] = attach_error_reasons(snapshot["view"])
    if run.get("status") == "completed" and isinstance(snapshot.get("view"), dict):
        run["error_count"] = projected_view_error_count(snapshot["view"])
    snapshot["recent_events"] = read_event_records(
        workspace_path / "logs" / "events.jsonl"
    )[-120:]

    analysis_payloads: dict[str, dict[str, Any]] = {}
    for item in snapshot.get("analysis_files") or []:
        if not isinstance(item, dict):
            continue
        relative_path = str(item.get("path") or "")
        target = _workspace_resource(workspace_path, relative_path)
        analysis_payloads[relative_path] = read_json_file(target)

    checkpoint_payloads: dict[str, dict[str, Any]] = {}
    for item in snapshot.get("dom_checkpoints") or []:
        if not isinstance(item, dict):
            continue
        checkpoint_id = str(item.get("checkpoint_id") or "")
        relative_path = str(item.get("path") or "")
        if not checkpoint_id:
            raise AuditError("静态档案包含无效 DOM 断点")
        target = _workspace_resource(workspace_path, relative_path)
        checkpoint_payloads[checkpoint_id] = read_json_file(target)

    log_path = workspace_path / "logs" / "run.log"
    run_log = (
        log_path.read_text(encoding="utf-8")
        if log_path.is_file()
        else "该运行没有持久化运行日志。\n"
    )
    archived_snapshot = dict(snapshot)
    # The verified view already occupies the canonical audit-data slot. Keep one
    # copy in the standalone file and let its local API adapter restore the field.
    archived_snapshot["view"] = None
    archive = {
        "schema_version": "1.0",
        "workspace_id": workspace_id,
        "catalog": {"runs": [run], "count": 1},
        "snapshot": archived_snapshot,
        "analysis": analysis_payloads,
        "checkpoints": checkpoint_payloads,
        "log": run_log,
    }
    context: dict[str, Any] = {
        "api_version": API_VERSION,
        "system_version": SYSTEM_VERSION,
        "main_flow_tasks": main_flow_task_list(),
        "delivery_mode": "static_archive",
        "workbench_url": str(
            workbench_url or manifest.get("workbench_url") or ""
        ).strip(),
        "mode": "system",
        "selected_run": workspace_id,
        "static_catalog": {"runs": [run], "count": 1},
        "run": run,
        "view_available": run.get("status") == "completed" and isinstance(snapshot.get("view"), dict)
        and isinstance(snapshot["view"].get("sheets"), list),
        "analysis_files": snapshot.get("analysis_files") or [],
        "dom_checkpoints": snapshot.get("dom_checkpoints") or [],
        "recent_events": snapshot.get("recent_events") or [],
        "verification": snapshot.get("verification"),
        "static_archive": archive,
    }

    html = root_html_path.read_text(encoding="utf-8")
    if context["view_available"]:
        html = replace_audit_payload(html, snapshot["view"])
    else:
        # Failed archives must not retain the template's historical business rows.
        html = replace_audit_payload(html, {})
    html = inject_workbench_context(html, context)
    return html


def render_static_run_archive(
    workspace: str | Path,
    root_html: str | Path,
    *,
    workbench_url: str | None = None,
) -> Path:
    """Publish the canonical two-level, self-contained read-only workbench."""
    workspace_path = Path(workspace).resolve()
    root_html_path = Path(root_html).resolve()
    target = workspace_path / STATIC_ARCHIVE_FILENAME
    if target == root_html_path:
        raise AuditError("静态运行页面不得覆盖根目录固定工作台")
    html = render_static_run_archive_html(workspace_path, root_html_path, workbench_url=workbench_url)
    atomic_write_text(target, html)
    return target
