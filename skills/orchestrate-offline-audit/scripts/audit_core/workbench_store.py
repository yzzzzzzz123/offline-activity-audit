from __future__ import annotations

from .paths import PROJECT_ROOT

from .error_reason import attach_error_reasons

import hashlib
import json
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from .common import (AuditError, BizTypeRecognitionError,
                     BIZ_TYPE_FAILURE_MESSAGE, china_timezone, sha256_file)
from .customer_language import SYSTEM_FAILURE_MESSAGE
from .summary_layout import group_summary_issues, issue_lines
from .codex_runner import DEFAULT_REASONING_EFFORT
from .orchestrator import normalize_producer_model, output_date_from_run_id
from .pass_check_log import attach_pass_check_log, load_workspace_results, load_workspace_material_packets
from .model_metrics import summarize_model_usage


DEFAULT_WORKTREES_ROOT = PROJECT_ROOT / "worktrees"
ANALYSIS_SUMMARY_FILENAME = "ai-analysis-summary.md"
MAIN_FLOW_TASKS: tuple[tuple[str, str, str], ...] = (
    ("bootstrap", "运行初始化", "建立持久化运行目录"),
    ("intake", "材料分流", "识别场景与资料角色"),
    ("analysis", "AI 识别", "提取材料中的可见事实"),
    ("evidence", "证据校验", "核对 Schema 与来源边界"),
    ("decision", "核销判断", "汇总模型判断与程序辅助结果"),
    ("logic_review", "总结逻辑自校验", "核对总结；矛盾项定向复核，其他结果保留"),
    ("verification", "结果封存", "验证页面投影与技术档案"),
)


def main_flow_task_list() -> list[dict[str, Any]]:
    """Return the canonical main-flow checklist in display order."""

    return [
        {
            "order": index,
            "stage": stage,
            "label": label,
            "note": note,
        }
        for index, (stage, label, note) in enumerate(MAIN_FLOW_TASKS, 1)
    ]


WORKSPACE_ID_PATTERN = re.compile(r'(?![. ])(?!CON(?:\.|$)|PRN(?:\.|$)|AUX(?:\.|$)|NUL(?:\.|$)|(?:COM|LPT)[1-9](?:\.|$))[^<>:"/\\|?*%\x00-\x1f\x7f]{1,120}(?<![. ])', re.IGNORECASE)
WORKSPACE_TIMEZONE = china_timezone()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _json_text(value: Any, *, pretty: bool = True) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        indent=2 if pretty else None,
        sort_keys=False,
        default=str,
    ) + "\n"


def _replace_temporary(temporary: Path, path: Path) -> None:
    # Windows readers/scanners may briefly hold the destination. Preserve the
    # old file and retry only those sharing errors, with the existing bound.
    delays = (0.02, 0.04, 0.08, 0.16, 0.32)
    for attempt in range(len(delays) + 1):
        try:
            os.replace(temporary, path)
            return
        except PermissionError as exc:
            if getattr(exc, "winerror", None) not in (5, 32, 33) or attempt == len(delays):
                raise
            time.sleep(delays[attempt])


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        _replace_temporary(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    # Stream the same complete JSON into an isolated file. Do not materialize
    # a second full snapshot string while the live view/trace is still held.
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=False, default=str)
            stream.write('\n')
        _replace_temporary(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _workspace_model_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}/[a-z0-9][a-z0-9._-]{0,79}", raw):
        raw = raw.replace("/", "-")
    compact = re.sub(r"[-_\s]+", "", raw)
    if not compact or re.fullmatch(r"[a-z0-9][a-z0-9.]{0,39}", compact) is None:
        raise AuditError("核销模型名称无法安全写入 worktree 目录名")
    return compact


def _workspace_reasoning_label(value: str) -> str:
    raw = str(value or "").strip().lower()
    if not raw or re.fullmatch(r"[a-z0-9][a-z0-9]{0,19}", raw) is None:
        raise AuditError("推理强度无法安全写入 worktree 目录名")
    return raw


def reserve_workspace(
    run_id: str,
    producer_model: str,
    root: str | Path = DEFAULT_WORKTREES_ROOT,
    *,
    audit_model: str | None = None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    started_at: datetime | None = None,
    archive_names: list[str] | None = None,
) -> Path:
    worktrees_root = Path(root).resolve()
    worktrees_root.mkdir(parents=True, exist_ok=True)
    # Keep validating the business-date-prefixed run ID, but do not reuse that
    # business date as the filesystem chronology. A workspace is named for the
    # local instant at which the serial audit worker claims/reserves the run.
    output_date_from_run_id(run_id)
    producer = normalize_producer_model(producer_model)
    timestamp = started_at or datetime.now(WORKSPACE_TIMEZONE)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=WORKSPACE_TIMEZONE)
    else:
        timestamp = timestamp.astimezone(WORKSPACE_TIMEZONE)
    model_label = _workspace_model_label(audit_model or producer)
    reasoning_label = _workspace_reasoning_label(reasoning_effort)
    candidate_id = (
        f"{timestamp.strftime('%Y%m%d_%H%M_%S')}-"
        f"{model_label}_{reasoning_label}"
    )
    if archive_names:
        from .workbench_labels import archive_display_name

        title = archive_display_name(archive_names)
        # Keep full source names in the manifest; only the filesystem component
        # is shortened/sanitized for Windows and UTF-8 filesystem limits.
        label = re.sub(r'[<>:"/\\|?*%\x00-\x1f\x7f]', '_', title).strip('. ')
        label = label[:90]
        while len(label.encode("utf-8")) > 180:
            label = label[:-1]
        label = label.rstrip('. ') or "核销资料"
        candidate_id = f"{label}-{timestamp.strftime('%Y%m%d_%H%M_%S')}"
        if WORKSPACE_ID_PATTERN.fullmatch(candidate_id) is None:
            raise AuditError("压缩包名称无法安全用于 worktree 目录")
    candidate = worktrees_root / candidate_id
    if archive_names:
        for serial in range(1, 1000):
            candidate = worktrees_root / (candidate_id if serial == 1 else f"{candidate_id}-{serial:02d}")
            try:
                candidate.mkdir()
                return candidate
            except FileExistsError:
                continue
        raise AuditError("同名压缩包的 worktree 保留次数超出单秒上限")
    try:
        candidate.mkdir()
    except FileExistsError as exc:
        raise AuditError(f"同一秒的 worktree 运行目录已存在：{candidate_id}") from exc
    return candidate


def sanitize_case(value: Any) -> Any:
    if isinstance(value, Path):
        return value.name
    if isinstance(value, dict):
        sanitized: dict[str, Any] = {}
        for key, item in value.items():
            if key in {"prepared_root", "source_root"}:
                continue
            sanitized[str(key)] = sanitize_case(item)
        return sanitized
    if isinstance(value, (list, tuple)):
        return [sanitize_case(item) for item in value]
    if isinstance(value, str):
        candidate = Path(value)
        if candidate.is_absolute():
            return candidate.name
    return value


def _summary_problem_reason(value: str) -> str:
    """Drop only standalone, fact-free compliance phrases from a known issue.

    This never decides pass/fail from words. Ambiguous phrases, numbers, dates
    and object descriptions remain unless the whole clause is safely redundant.
    """
    parts = [part.strip() for part in re.split(r"[；;。\n，]", value) if part.strip()]
    problem = re.compile(r"无法|不能|未能|不清|不明确|待确认|不确定|遮挡|缺少|未提供|未提交|不足|不一致|不符|超出|未完整|未显示")
    if not any(problem.search(part) for part in parts):
        return value
    # A single rule may describe one unreadable price photo and then list the
    # other photos' passed checks. Remove only these complete, fact-free
    # compliance clauses; do not infer a decision or drop another problem.
    photo_compliance = re.compile(
        r"(?:其余|其他)照片(?:的)?水印(?:均)?(?:含|包含)(?:拍摄时间|拍摄日期|日期)"
        r"(?:和|及|、)门店地址(?:、|，|,)(?:活动)?价格(?:均)?(?:可读|清晰)"
        r"|(?:其余|其他)照片(?:的)?(?:水印)?(?:拍摄时间|拍摄日期|日期)"
        r"(?:、|和|及)门店地址(?:、|和|及)(?:活动)?价格(?:均)?(?:可读|清晰)"
    )
    clauses = [part.strip() for part in re.split(r"[；;。\n]", value) if part.strip()]
    photo_kept = [part for part in clauses if not photo_compliance.fullmatch(part)]
    removed_photo_compliance = len(photo_kept) < len(clauses)
    if removed_photo_compliance:
        value = "；".join(photo_kept) + "。"
        parts = [part.strip() for part in re.split(r"[；;。\n，]", value) if part.strip()]
    compliant = re.compile(
        r"(?:水印)?日期(?:处于|在)(?:本次)?(?:合同)?(?:约定的)?(?:活动|执行)?(?:周期|期间|日期范围)内"
        r"|(?:与)?合同(?:约定的)?(?:期间|门店|地址|要求)(?:及门店)?(?:一致|相符)"
        r"|(?:未见|未发现|没有)其他品牌(?:物料)?"
        r"|(?:其余|其他)(?:项目|检查项|部分)(?:均)?(?:符合要求|通过)"
    )
    kept = [part for part in parts if not compliant.fullmatch(part)]
    return "；".join(kept) + "。" if kept and len(kept) < len(parts) else value


def render_analysis_summary_markdown(
    view_payload: dict[str, Any],
    manifest: dict[str, Any],
) -> str:
    """Report business and material problems for human review, including gate issues."""
    if manifest.get("status") == "failed":
        code = (manifest.get("failure") or {}).get("code")
        if code in {"biz_type_unrecognized", "plan_type_unrecognized"}:
            return BIZ_TYPE_FAILURE_MESSAGE
        if code != "classification_failed":
            return SYSTEM_FAILURE_MESSAGE

    def escape(value: Any) -> str:
        text = " ".join(str(value or "").split()).replace("\\", "\\\\")
        for character in ("`", "*", "[", "]", "<", ">", "#", "_"):
            text = text.replace(character, "\\" + character)
        return text

    def row_problems(row: dict[str, Any]) -> list[dict[str, Any]]:
        items = row.get("summary_items")
        if not isinstance(items, list):
            # Old sealed snapshots do not retain per-check statuses. Use their
            # saved issue reasons, never the complete findings or passed photos.
            reasons = list(row.get("error_reasons") or []) if row.get("status") == "issue" else []
            values = row.get("values") or []
            location = str(values[0]).strip() if row.get("photo_unit_id") and values else ""
            return [{"reason": reason, "location": location} for reason in reasons]
        problems = []
        values = row.get("values") or []
        location = str(values[0]).strip() if row.get("photo_unit_id") and values else ""
        for item in items:
            if not isinstance(item, dict) or item.get("status") not in {"fail", "unknown"}:
                continue
            category = str(item.get("check_category") or "该项")
            # effect is static rule metadata (often defaulting to missing_material),
            # not a material-state decision. Preserve the actual missing-material
            # explanation or observed violation without inventing either meaning.
            for line in issue_lines(str(item.get("reason") or "").strip()) or [""]:
                reason = _summary_problem_reason(line)
                if item.get("status") == "unknown":
                    if not re.search(r"无法|不能|未能|不清|不明确|待确认|不确定", reason):
                        reason = f"暂时不能确认{category}" + (f"：{reason}" if reason else "。")
                elif not reason:
                    reason = f"{category}的具体错误原因未记录。"
                if reason:
                    problems.append({"reason": reason, "location": location,
                                     "source_file": next(iter(row.get("business_source_files") or []), "")
                                         if row.get("photo_unit_id") else "",
                                     "rule_id": item.get("rule_id"), "status": item["status"],
                                     "scope": "" if row.get("photo_unit_id") else
                                         tuple(item.get("source_ids") or [])})
        return problems

    projected = attach_error_reasons(view_payload)
    groups: dict[str, dict[str, list[dict[str, Any]]]] = {}
    seen: set[tuple] = set()
    classification_rejected = bool(
        projected.get("classification_rejection") or manifest.get("classification_rejection")
        or (manifest.get("failure") or {}).get("code") == "classification_failed"
    )
    for sheet in projected.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        if sheet.get("projection_kind") == "classification_rejection":
            classification_rejected = True
        rows = [row for row in sheet.get("rows") or [] if isinstance(row, dict)]
        name = ("核销资料问题" if sheet.get("projection_kind") == "classification_rejection" and not sheet.get("scenario") else
                str(sheet.get("audit_type_label") or sheet.get("name") or sheet.get("scenario") or "核销结果"))
        archive = str(sheet.get("source_archive") or "")
        for row in rows:
            for issue in row_problems(row):
                # Keep source identities intact; only the final human summary
                # groups identical issues, never the per-photo audit records.
                identity = str(row.get("photo_unit_id") or "")
                if not identity and isinstance(row.get("summary_items"), list):
                    identity = json.dumps([item.get("source_ids") for item in row["summary_items"]
                                           if isinstance(item, dict)], ensure_ascii=False)
                key = (name, archive, identity, issue.get("rule_id"), issue.get("status"), issue["reason"])
                if key not in seen:
                    groups.setdefault(name, {}).setdefault(archive, []).append(issue)
                    seen.add(key)
    if not groups:
        return ("具体资料问题未记录，请人工核对。" if classification_rejected else
                "具体错误原因未记录。" if int(manifest.get("error_count") or 0) else "未发现错误。")
    sections = []
    problem_number = 0
    multiple_archives = len({str(sheet.get("source_archive") or "")
                            for sheet in projected.get("sheets") or [] if isinstance(sheet, dict)}) > 1
    for name, archives in groups.items():
        parts = [f"## {escape(name)}"]
        for index, (archive, issues) in enumerate(archives.items(), 1):
            reasons = group_summary_issues(issues)
            if multiple_archives:
                # Name each bundle once; never repeat it before every problem.
                parts.append(f"### {escape(archive or f'第{index}份资料')}")
            if reasons:
                parts.append("\n".join(
                    f"{number}. {escape(reason)}"
                    for number, reason in enumerate(reasons, problem_number + 1)
                ))
            problem_number += len(reasons)
        sections.append("\n\n".join(parts))
    return "\n\n".join(sections)


def project_run_status(manifest: dict[str, Any], view: dict[str, Any] | None = None) -> dict[str, Any]:
    """Display completed material checks without rewriting historical records."""
    if (manifest.get("failure") or {}).get("code") == "classification_failed":
        return {**manifest, "status": "completed", "failure": None}
    if manifest.get("status") in {"running", "failed"} and not manifest.get("scenarios"):
        from .scenario_registry import scenario_for_biz_type
        selected = scenario_for_biz_type(manifest.get("bizType"))
        if selected and selected == manifest.get("selected_scenario"):
            return {**manifest, "scenarios": [selected], "scenario_count": 1}
    return dict(manifest)


class WorkbenchRunStore:
    """Append-only audit trace and atomic UI snapshot for one persisted run."""

    @classmethod
    def _open_existing(cls, workspace: Path, *, run_id: str, expected_status: str):
        """Open append-only state without replacing a previous event or result."""
        self = cls.__new__(cls)
        self.workspace = workspace.resolve()
        self.workspace_id = self.workspace.name
        if WORKSPACE_ID_PATTERN.fullmatch(self.workspace_id) is None:
            raise AuditError('续跑目录名称不安全')
        self.analysis_root = self.workspace/'analysis'
        self.dom_root = self.workspace/'dom/checkpoints'
        self.logs_root = self.workspace/'logs'
        self.events_path = self.logs_root/'events.jsonl'
        self.run_log_path = self.logs_root/'run.log'
        self.snapshot_path = self.workspace/'snapshot.json'
        self.manifest_path = self.workspace/'manifest.json'
        self.analysis_summary_path = self.workspace/ANALYSIS_SUMMARY_FILENAME
        self.manifest = json.loads(self.manifest_path.read_text(encoding='utf-8'))
        if self.manifest.get('run_id') != run_id or self.manifest.get('status') != expected_status:
            raise AuditError('仅允许同一run的失败续跑或显式指定快照的已完成记录重新核验')
        snapshot=(json.loads(self.snapshot_path.read_text(encoding='utf-8'))
                  if self.snapshot_path.is_file() else {})
        if not snapshot and expected_status != 'running':
            raise AuditError('终态断点缺少快照，禁止猜测恢复')
        self._lock=threading.RLock()
        events=[json.loads(line) for line in self.events_path.read_text(encoding='utf-8').splitlines() if line.strip()]
        self._event_seq=max((e.get('seq',0) for e in events),default=0)
        self._checkpoints=list(snapshot.get('dom_checkpoints') or [])
        self._checkpoint_seq=max((int(p.stem.split('-')[0]) for p in self.dom_root.glob('*.json')),default=0)
        self._analysis_files=list(snapshot.get('analysis_files') or [])
        self._view_payload=None
        self._verification=None
        return self

    @classmethod
    def fail_interrupted(cls, workspace: Path, *, run_id: str, reason: str,
                         code: str | None = None):
        """Seal an explicitly stopped runner after its owner verified it exited."""
        self = cls._open_existing(workspace, run_id=run_id, expected_status='running')
        self.fail(AuditError(reason))
        if code:
            self.manifest.setdefault('failure', {})['code'] = str(code)
            self._write_snapshot()
            self._write_manifest()
        return self

    @classmethod
    def resume_failed(cls, workspace: Path, *, run_id: str, revalidation_snapshot_sha256: str | None = None):
        """Reopen one explicitly authorized failed record, preserving its ledger."""
        revalidation = revalidation_snapshot_sha256 is not None
        self = cls._open_existing(workspace, run_id=run_id,
                                  expected_status='completed' if revalidation else 'failed')
        if revalidation and (hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest() != revalidation_snapshot_sha256
                             or self.manifest.get('snapshot_sha256') != revalidation_snapshot_sha256):
            raise AuditError('重新核验的已完成快照指纹不一致')
        self.manifest.setdefault('resume_history',[]).append({'resumed_at':utc_now(),
            'previous_failure':self.manifest.get('failure'),'previous_completed_at':self.manifest.get('completed_at'),
            'previous_audit_timing':self.manifest.get('audit_timing'),
            'previous_token_usage':self.manifest.get('token_usage'),
            **({'kind':'completed_revalidation','previous_snapshot_sha256':revalidation_snapshot_sha256}
               if revalidation else {})})
        self.manifest.update(status='running',failure=None,completed_at=None)
        self.manifest.pop('audit_timing', None)
        self._write_manifest()
        self.append_event('run.revalidation_started' if revalidation else 'run.resumed',
            '显式重新核验已完成记录的候选版本，保留原完成版本与全部用量' if revalidation
            else '在原记录继续未完成核销，保留此前失败记录和全部用量',stage='resume')
        return self

    def __init__(
        self,
        workspace: Path,
        *,
        run_id: str,
        producer_model: str,
        root_html: Path,
        workbench_url: str,
        audit_model: str | None = None,
        reasoning_effort: str | None = None,
        source_archives: list[str] | None = None,
        input_source: str | None = None,
    ) -> None:
        self.workspace = workspace.resolve()
        self.workspace_id = self.workspace.name
        if WORKSPACE_ID_PATTERN.fullmatch(self.workspace_id) is None:
            raise AuditError("worktree 运行目录名称不安全")
        self.analysis_root = self.workspace / "analysis"
        self.dom_root = self.workspace / "dom" / "checkpoints"
        self.logs_root = self.workspace / "logs"
        self.events_path = self.logs_root / "events.jsonl"
        self.run_log_path = self.logs_root / "run.log"
        self.snapshot_path = self.workspace / "snapshot.json"
        self.manifest_path = self.workspace / "manifest.json"
        self.analysis_summary_path = self.workspace / ANALYSIS_SUMMARY_FILENAME
        self._lock = threading.RLock()
        self._event_seq = 0
        self._checkpoint_seq = 0
        self._view_payload: dict[str, Any] | None = None
        self._verification: dict[str, Any] | None = None
        self._analysis_files: list[dict[str, Any]] = []
        self._checkpoints: list[dict[str, Any]] = []
        now = utc_now()
        normalized_producer = normalize_producer_model(producer_model)
        self.manifest: dict[str, Any] = {
            "schema_version": "1.2",
            "workspace_id": self.workspace_id,
            "run_id": run_id,
            "business_date": output_date_from_run_id(run_id),
            "producer_model": normalized_producer,
            "audit_model": str(audit_model or normalized_producer),
            "reasoning_effort": str(reasoning_effort or DEFAULT_REASONING_EFFORT),
            "source_archives": list(source_archives or []),
            "input_source": input_source,
            "status": "running",
            "created_at": now,
            "analysis_started_at": None,
            "updated_at": now,
            "completed_at": None,
            "scenarios": [],
            "scenario_count": 0,
            "error_count": 0,
            "root_html": root_html.name,
            "workbench_url": workbench_url,
            "snapshot_sha256": None,
            "failure": None,
            "main_flow_tasks": main_flow_task_list(),
        }
        self.analysis_root.mkdir(parents=True, exist_ok=True)
        self.dom_root.mkdir(parents=True, exist_ok=True)
        self.logs_root.mkdir(parents=True, exist_ok=True)
        self._write_manifest()
        self.append_event(
            "run.created",
            "运行目录已建立，等待材料分类",
            stage="bootstrap",
            details={"workspace_id": self.workspace_id},
        )
        self.write_checkpoint(
            "bootstrap",
            "运行初始化",
            {"status": "running", "workspace_id": self.workspace_id},
        )

    def _write_manifest(self) -> None:
        self.manifest["token_usage"] = summarize_model_usage(self.analysis_root)
        self.manifest["updated_at"] = utc_now()
        atomic_write_json(self.manifest_path, self.manifest)

    def append_event(
        self,
        event_type: str,
        message: str,
        *,
        stage: str,
        scenario: str | None = None,
        details: dict[str, Any] | None = None,
        level: str = "info",
    ) -> dict[str, Any]:
        with self._lock:
            self._event_seq += 1
            event = {
                "seq": self._event_seq,
                "timestamp": utc_now(),
                "type": event_type,
                "stage": stage,
                "scenario": scenario,
                "level": level,
                "message": message,
                "details": details or {},
            }
            line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
            with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(line + "\n")
                stream.flush()
            with self.run_log_path.open("a", encoding="utf-8", newline="\n") as stream:
                scenario_label = f" [{scenario}]" if scenario else ""
                stream.write(
                    f"{event['timestamp']} {level.upper():5s} "
                    f"{stage}{scenario_label} {message}\n"
                )
                stream.flush()
            return event

    def write_analysis(self, relative_path: str, value: Any, *, label: str) -> Path:
        clean = Path(relative_path)
        if clean.is_absolute() or ".." in clean.parts:
            raise AuditError("分析文件路径越出运行目录")
        target = (self.analysis_root / clean).resolve()
        if self.analysis_root not in target.parents:
            raise AuditError("分析文件路径越出运行目录")
        atomic_write_json(target, value)
        relative = target.relative_to(self.workspace).as_posix()
        record = {
            "path": relative,
            "label": label,
            "size": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
        self._analysis_files = [
            item for item in self._analysis_files if item["path"] != relative
        ]
        self._analysis_files.append(record)
        return target

    def write_checkpoint(
        self,
        stage: str,
        label: str,
        payload: dict[str, Any],
        *,
        scenario: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            self._checkpoint_seq += 1
            checkpoint_id = f"{self._checkpoint_seq:03d}-{stage}"
            value = {
                "schema_version": "1.0",
                "checkpoint_id": checkpoint_id,
                "timestamp": utc_now(),
                "stage": stage,
                "label": label,
                "scenario": scenario,
                "payload": payload,
            }
            target = self.dom_root / f"{checkpoint_id}.json"
            atomic_write_json(target, value)
            record = {
                "checkpoint_id": checkpoint_id,
                "timestamp": value["timestamp"],
                "stage": stage,
                "label": label,
                "scenario": scenario,
                "path": target.relative_to(self.workspace).as_posix(),
                "size": target.stat().st_size,
            }
            self._checkpoints.append(record)
            return record

    def observe(self, event_type: str, payload: dict[str, Any]) -> None:
        if event_type == "execution.step":
            self.append_event(event_type, payload["label"], stage="analysis", details=payload,
                              level="error" if payload.get("status") == "failed" else "info")
            self.write_checkpoint("execution", payload["label"], payload)
            try:
                self._write_snapshot()
            except MemoryError:
                # The append-only step, event and checkpoint are already on
                # disk. A transient intermediate UI refresh is not a failed
                # evidence check. Later refreshes retry; final sealing below
                # remains mandatory and does not pass through this handler.
                self.manifest['snapshot_refresh_warning'] = 'intermediate_memory_pressure'
                return
            self._write_manifest()
            return
        if event_type == "material_classification.started":
            self.manifest["largeVenueFee"] = payload.get("large_venue_fee")
            self.manifest["cvsOtcChannel"] = payload.get("cvs_otc_channel") is True
            policy = payload.get("classification_policy") or "material_content"
            event = self.append_event(event_type, "开始根据业务类型选择核销Skill" if policy == "biz_type" else "开始按资料内容识别八类核销", stage="analysis")
            if not self.manifest.get("analysis_started_at"):
                self.manifest["analysis_started_at"] = event["timestamp"]
            self.manifest["scenario_classification_policy"] = policy
            self._write_manifest()
            return
        if event_type == "biz_type.resolved":
            self.write_analysis("pdf-policy/biz-type.json", payload, label="业务类型与核销Skill对应结果")
            self.manifest["bizType"] = payload["biz_type"]
            self.manifest["selected_scenario"] = payload["scenario"]
            self.manifest["scenarios"] = [payload["scenario"]]
            self.manifest["scenario_count"] = 1
            self._write_manifest()
            self.append_event(event_type, "已完成业务类型识别", stage="analysis")
            return
        if event_type == "pdf_materials.validated":
            packets = sanitize_case(payload["packets"])
            self.write_analysis("pdf-policy/evidence.json", {"policy_version": payload["policy_version"], "packets": packets},
                                label="PDF八类资料识别与审核证据")
            self.manifest["audit_policy_version"] = payload["policy_version"]
            self._write_manifest()
            self.append_event(event_type, "资料分类及PDF要点证据已完整校验", stage="evidence")
            self.write_checkpoint("evidence", "八类内容证据已确认", {"archive_count": len(packets)})
            return
        if event_type == "product_database.loaded":
            catalog = payload["catalog"]
            self.write_analysis("product-database.json", catalog, label="本次商品数据库只读快照")
            self.manifest["product_database"] = catalog["data_source"]
            self._write_manifest()
            self.append_event(event_type, f"已读取商品数据库 {len(catalog['products'])} 条，供本次核销使用", stage="intake")
            return
        if event_type == "classification_rejection.result_validated":
            from .common import validate_json
            from .material_diagnostic_output import CLASSIFICATION_REJECTION_SCHEMA

            result = payload.get("result") or {}
            validate_json(result, CLASSIFICATION_REJECTION_SCHEMA)
            content_route = result.get("decision_source") in {"material_content", "biz_type", "plan_type"}
            self.write_analysis("classification-rejection/result.json", result,
                                label="资料清单核对结果" if content_route else "ZIP 名称分类退回结果")
            names = [item["source_archive"] for item in result["archives"]]
            from .workbench_labels import merge_archive_names
            self.manifest["source_archives"] = merge_archive_names(
                self.manifest.get("source_archives") or [], names,
            )
            self.manifest["classification_rejection"] = True
            self._write_manifest()
            self.append_event(event_type, "资料检查已完成，列出具体资料问题" if content_route else "核销方式无法确认，保留历史原因", stage="decision")
            self.write_checkpoint("decision", "资料清单核对完成" if content_route else "历史分类记录", {
                "conclusion": result["summary"]["conclusion"], "archive_count": len(names),
            })
            return

        if event_type == "material_diagnostic.started":
            case = sanitize_case(payload.get("case") or {})
            if case:
                self.write_analysis("material-diagnostic/input.json", case, label="材料诊断来源盘点")
                names = [str(name) for name in case.get("source_archives") or []]
                if not names:
                    names = [str(item.get("source_archive") or "") for item in case.get("archives") or []]
                from .workbench_labels import merge_archive_names
                self.manifest["source_archives"] = merge_archive_names(
                    self.manifest.get("source_archives") or [], [name for name in names if name],
                )
            event = self.append_event("material_diagnostic.started", "开始 AI 材料用途识别", stage="analysis")
            if not self.manifest.get("analysis_started_at"):
                self.manifest["analysis_started_at"] = event["timestamp"]
            self._write_manifest()
            return

        if event_type == "material_diagnostic.evidence_validated":
            from .common import validate_json
            from .material_diagnostic_output import EVIDENCE_SCHEMA

            evidence = payload.get("evidence") or {}
            validate_json(evidence, EVIDENCE_SCHEMA)
            self.write_analysis("material-diagnostic/evidence.json", evidence, label="AI 材料用途与可见事实")
            self.append_event(event_type, "AI 材料用途已通过证据 Schema 校验", stage="evidence")
            self.write_checkpoint("evidence", "材料诊断证据已确认", {"archive_count": len(evidence.get("archives") or [])})
            return

        if event_type == "material_diagnostic.result_validated":
            from .common import validate_json
            from .material_diagnostic_output import RESULT_SCHEMA

            result = payload.get("result") or {}
            validate_json(result, RESULT_SCHEMA)
            self.write_analysis("material-diagnostic/result.json", result, label="材料缺失与重复确定性诊断")
            diagnosed = [item.get("scenario") for item in result["archives"] if item.get("scenario")]
            scenarios = list(dict.fromkeys([*(self.manifest.get("scenarios") or []), *diagnosed]))
            self.manifest["scenarios"] = scenarios
            self.manifest["scenario_count"] = len(scenarios)
            self.manifest["material_diagnostic"] = True
            self._write_manifest()
            self.append_event(event_type, "材料诊断已验证；业务核销待确认", stage="decision")
            self.write_checkpoint("decision", "材料问题已确认", {"conclusion": "human_review", "archive_count": len(result["archives"])})
            return

        if event_type == "cases.prepared":
            cases = sanitize_case(payload.get("cases") or {})
            from .workbench_labels import source_archive_names

            self.manifest["source_archives"] = source_archive_names(cases)
            scenarios = list(payload.get("scenarios") or [])
            self.manifest["scenarios"] = scenarios
            self.manifest["scenario_count"] = len(scenarios)
            self._write_manifest()
            self.write_analysis("input-cases.json", cases, label="材料分类与角色绑定")
            self.append_event(
                "intake.completed",
                (f"已安全盘点 {len(cases)} 个资料包，等待内容分类" if payload.get("classification_policy") == "material_content"
                 else f"已识别 {len(scenarios)} 个核销场景"),
                stage="intake",
                details={"scenarios": scenarios},
            )
            self.write_checkpoint(
                "intake",
                "材料分类完成",
                {"scenarios": scenarios, "cases": cases},
            )
            return

        scenario = str(payload.get("scenario") or "") or None
        if event_type == "scenario.started":
            with self._lock:
                event = self.append_event(
                    "scenario.started",
                    "开始结构化识别",
                    stage="analysis",
                    scenario=scenario,
                )
                if not self.manifest.get("analysis_started_at"):
                    self.manifest["analysis_started_at"] = event["timestamp"]
                    self._write_manifest()
            return

        if event_type == "evidence.validated":
            evidence = payload.get("evidence") or {}
            self.write_analysis(
                f"evidence/{scenario}.json",
                evidence,
                label=f"{scenario} AI可见事实",
            )
            self.append_event(
                "evidence.validated",
                "AI可见事实已通过场景 Schema 校验",
                stage="evidence",
                scenario=scenario,
            )
            self.write_checkpoint(
                "evidence",
                "结构化证据已确认",
                {
                    "scenario": scenario,
                    "top_level_fields": sorted(evidence) if isinstance(evidence, dict) else [],
                },
                scenario=scenario,
            )
            return

        if event_type == "result.validated":
            result = payload.get("result") or {}
            self.write_analysis(
                f"results/{scenario}.json",
                result,
                label=f"{scenario} 确定性核销结果",
            )
            summary = result.get("summary") if isinstance(result, dict) else {}
            self.append_event(
                "result.validated",
                "确定性核销结果已通过统一结果契约",
                stage="decision",
                scenario=scenario,
            )
            self.write_checkpoint(
                "decision",
                "核销判断已闭合",
                {"scenario": scenario, "summary": summary or {}},
                scenario=scenario,
            )
            return

        if event_type in {"logic_review.started", "logic_review.rework", "logic_review.completed"}:
            review = payload.get('review') or {}
            review_label = {'passed': '总结逻辑自校验已通过',
                            'not_passed': '总结逻辑自校验有未解决事项',
                            'unavailable': '总结逻辑自校验未完成',
                            'external_evidence_provider': '本次未执行模型总结自校验'}.get(
                                review.get('status'), '总结逻辑自校验已结束')
            labels = {"logic_review.started": "开始总结逻辑自校验",
                      "logic_review.rework": "总结发现矛盾，定向复核相关审核项",
                      "logic_review.completed": review_label}
            self.append_event(event_type, labels[event_type], stage="logic_review", details=payload)
            if event_type == "logic_review.completed":
                self.write_analysis("final-logic-review.json", payload.get('review') or {}, label="最终逻辑自校验结果")
                self.write_checkpoint("logic_review", labels[event_type], payload.get('review') or {})
            return

        if event_type == "report.verified":
            self._verification = sanitize_case(payload.get("verification") or {})
            self.write_analysis(
                "verification.json",
                self._verification,
                label="中间结果与页面投影校验",
            )
            self.append_event(
                "report.verified",
                "六列中间结果和固定页面投影均已验证",
                stage="verification",
            )
            return

    def complete(
        self,
        *,
        view_payload: dict[str, Any],
        scenarios: list[str],
        verification: dict[str, Any],
        analysis_summary: str | None = None,
    ) -> None:
        with self._lock:
            rejection = view_payload.get("classification_rejection")
            if rejection is not None:
                from .common import validate_json
                from .material_diagnostic_output import CLASSIFICATION_REJECTION_SCHEMA

                validate_json(rejection, CLASSIFICATION_REJECTION_SCHEMA)
            self._view_payload = attach_error_reasons(attach_pass_check_log(
                view_payload,
                load_workspace_results(self.workspace),
                material_packets=load_workspace_material_packets(self.workspace),
            ))
            error_count = sum(
                int((sheet.get("audit_counts") or {}).get("error_count") or 0)
                for sheet in view_payload.get("sheets") or []
            )
            self.manifest.update(
                {
                    "status": "completed",
                    "completed_at": utc_now(),
                    "scenarios": scenarios,
                    "scenario_count": len(scenarios),
                    "error_count": error_count,
                    "failure": None,
                    "business_result_policy": "issues_for_human_review",
                    "run_failure_policy": "execution_errors_only",
                }
            )
            self._write_analysis_summary(analysis_summary)
            from .run_timing import finish_timing
            self.manifest['audit_timing'] = finish_timing(self.manifest)
            self._verification = verification
            self.write_checkpoint(
                "ready",
                "资料检查及问题清单已就绪",
                {
                    "scenarios": scenarios,
                    "error_count": error_count,
                    "view_schema_version": view_payload.get("schema_version"),
                },
            )
            self.append_event(
                "run.completed",
                f"资料检查完成，共 {len(scenarios)} 个类型、{error_count} 个问题",
                stage="complete",
                details={"scenarios": scenarios, "error_count": error_count},
                level="info",
            )
            self._write_snapshot()
            self._write_manifest()

    def fail(self, exc: BaseException) -> None:
        with self._lock:
            failure = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            if isinstance(exc, BizTypeRecognitionError):
                failure["code"] = "biz_type_unrecognized"
            self.manifest.update(
                {
                    "status": "failed",
                    "completed_at": utc_now(),
                    "failure": failure,
                }
            )
            # A failed execution must never expose a partial result as a pass.
            from .run_timing import finish_timing
            self.manifest['audit_timing'] = finish_timing(self.manifest)
            # Persist the exact safe callback text before sealing the snapshot.
            self._write_analysis_summary()
            self.write_checkpoint("failed", "运行失败", failure)
            self.append_event(
                "run.failed",
                str(exc),
                stage="failed",
                details=failure,
                level="error",
            )
            self._write_snapshot()
            self._write_manifest()

    def _write_analysis_summary(self, reviewed_summary: str | None = None) -> None:
        summary = (reviewed_summary if reviewed_summary is not None else
                   render_analysis_summary_markdown(self._view_payload or {}, self.manifest))
        if not isinstance(summary, str) or not summary.strip():
            raise AuditError('AI总结核验后未提供完整文稿，不能封存空总结。')
        atomic_write_text(self.analysis_summary_path, summary)
        self.manifest["analysis_summary"] = {
            "path": ANALYSIS_SUMMARY_FILENAME,
            "size": self.analysis_summary_path.stat().st_size,
            "sha256": hashlib.sha256(self.analysis_summary_path.read_bytes()).hexdigest(),
        }
        # Let already-running workbench servers expose new summaries without
        # interrupting their intake workers for a Python module reload.
        self.manifest["analysis_summary_display"] = {"text": summary.strip(), "source": "saved"}

    def _write_snapshot(self) -> None:
        self.manifest["token_usage"] = summarize_model_usage(self.analysis_root)
        snapshot = {
            "schema_version": "1.0",
            "run": self.manifest,
            "view": self._view_payload,
            "verification": self._verification,
            "analysis_files": sorted(
                self._analysis_files, key=lambda item: item["path"]
            ),
            "dom_checkpoints": list(self._checkpoints),
        }
        from .execution_trace import enrich_technical_snapshot
        enrich_technical_snapshot(snapshot, self.workspace)
        atomic_write_json(self.snapshot_path, snapshot)
        digest = sha256_file(self.snapshot_path)
        self.manifest["snapshot_sha256"] = digest


def read_json_file(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"无法读取运行数据 {path.name}：{exc}") from exc
    if not isinstance(value, dict):
        raise AuditError(f"运行数据 {path.name} 顶层必须是对象")
    return value


def read_event_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events
