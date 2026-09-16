from __future__ import annotations

from .error_reason import attach_error_reasons

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from .common import AuditError
from .codex_runner import DEFAULT_REASONING_EFFORT
from .orchestrator import normalize_producer_model, output_date_from_run_id
from .pass_check_log import attach_pass_check_log, load_workspace_results


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKTREES_ROOT = PROJECT_ROOT / "worktrees"
ANALYSIS_SUMMARY_FILENAME = "ai-analysis-summary.md"
MAIN_FLOW_TASKS: tuple[tuple[str, str, str], ...] = (
    ("bootstrap", "运行初始化", "建立持久化运行目录"),
    ("intake", "材料分流", "识别场景与资料角色"),
    ("analysis", "AI 识别", "提取材料中的可见事实"),
    ("evidence", "证据校验", "核对 Schema 与来源边界"),
    ("decision", "核销决策", "执行确定性业务规则"),
    ("verification", "结果封存", "验证页面投影与技术档案"),
)


def main_flow_task_list() -> list[dict[str, Any]]:
    """Return the canonical six-stage main-flow checklist in display order."""

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
WORKSPACE_TIMEZONE = ZoneInfo("Asia/Shanghai")


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


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8", newline="\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, _json_text(value))


def _workspace_model_label(value: str) -> str:
    raw = str(value or "").strip().lower()
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


def render_analysis_summary_markdown(
    view_payload: dict[str, Any],
    manifest: dict[str, Any],
) -> str:
    """One concrete reason per bullet, without rewording business decisions."""
    def escape(value: Any) -> str:
        text = " ".join(str(value or "").split()).replace("\\", "\\\\")
        for character in ("`", "*", "[", "]", "<", ">", "#", "_"):
            text = text.replace(character, "\\" + character)
        return text

    projected = attach_error_reasons(view_payload)
    groups: dict[str, list[str]] = {}
    for sheet in projected.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        reasons = [reason for row in sheet.get("rows") or [] if isinstance(row, dict)
                   and row.get("status") == "issue" for reason in row.get("error_reasons") or []]
        if not reasons:
            continue
        name = str(sheet.get("audit_type_label") or sheet.get("name") or sheet.get("scenario") or "核销结果")
        if sheet.get("projection_kind") == "classification_rejection":
            name = "核销失败：核销方式无法确认"
        group = groups.setdefault(name, [])
        for reason in reasons:
            if reason not in group:
                group.append(reason)
    if not groups:
        return "具体错误原因未记录。" if int(manifest.get("error_count") or 0) else "未发现错误。"
    return "\n\n".join(f"## {escape(name)}\n\n" + "\n".join(f"- {escape(reason)}" for reason in reasons)
                        for name, reasons in groups.items())


def classification_failure(result: dict[str, Any] | None) -> dict[str, str]:
    reasons = [str(archive["reason"]) for archive in (result or {}).get("archives", [])
               if archive.get("reason")]
    return {
        "type": "ArchiveClassificationError",
        "code": "classification_failed",
        "message": "\n".join(reasons) or "核销方式无法确认，本次核销失败。",
    }


def project_run_status(manifest: dict[str, Any], view: dict[str, Any] | None = None) -> dict[str, Any]:
    """Correct older classification receipts in memory, preserving audit history."""
    if manifest.get("status") == "completed" and manifest.get("classification_rejection"):
        return {**manifest, "status": "failed",
                "failure": classification_failure((view or {}).get("classification_rejection"))}
    return dict(manifest)


class WorkbenchRunStore:
    """Append-only audit trace and atomic UI snapshot for one persisted run."""

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
            self.write_analysis("classification-rejection/result.json", result, label="ZIP 名称分类退回结果")
            names = [item["source_archive"] for item in result["archives"]]
            self.manifest["source_archives"] = list(dict.fromkeys([
                *(self.manifest.get("source_archives") or []), *names,
            ]))
            self.manifest["classification_rejection"] = True
            self._write_manifest()
            self.append_event(event_type, "核销方式无法确认，资料已退回", stage="decision")
            self.write_checkpoint("decision", "名称分类退回", {
                "conclusion": "rejected", "archive_count": len(names),
            })
            return

        if event_type == "material_diagnostic.started":
            case = sanitize_case(payload.get("case") or {})
            if case:
                self.write_analysis("material-diagnostic/input.json", case, label="材料诊断来源盘点")
                names = [str(name) for name in case.get("source_archives") or []]
                if not names:
                    names = [str(item.get("source_archive") or "") for item in case.get("archives") or []]
                self.manifest["source_archives"] = list(dict.fromkeys([
                    *(self.manifest.get("source_archives") or []), *(name for name in names if name),
                ]))
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
                f"已识别 {len(scenarios)} 个核销场景",
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
    ) -> None:
        with self._lock:
            rejection = view_payload.get("classification_rejection")
            if rejection is not None:
                from .common import validate_json
                from .material_diagnostic_output import CLASSIFICATION_REJECTION_SCHEMA

                validate_json(rejection, CLASSIFICATION_REJECTION_SCHEMA)
            failure = classification_failure(rejection) if rejection is not None else None
            self._view_payload = attach_error_reasons(attach_pass_check_log(
                view_payload,
                load_workspace_results(self.workspace),
            ))
            error_count = sum(
                int((sheet.get("audit_counts") or {}).get("error_count") or 0)
                for sheet in view_payload.get("sheets") or []
            )
            self.manifest.update(
                {
                    "status": "failed" if failure else "completed",
                    "completed_at": utc_now(),
                    "scenarios": scenarios,
                    "scenario_count": len(scenarios),
                    "error_count": error_count,
                    "failure": failure,
                }
            )
            summary = render_analysis_summary_markdown(
                self._view_payload,
                self.manifest,
            )
            atomic_write_text(self.analysis_summary_path, summary)
            self.manifest["analysis_summary"] = {
                "path": ANALYSIS_SUMMARY_FILENAME,
                "size": self.analysis_summary_path.stat().st_size,
                "sha256": hashlib.sha256(
                    self.analysis_summary_path.read_bytes()
                ).hexdigest(),
            }
            self._verification = verification
            self.write_checkpoint(
                "failed" if failure else "ready",
                "核销方式无法确认" if failure else "固定工作台数据已就绪",
                {
                    "scenarios": scenarios,
                    "error_count": error_count,
                    "view_schema_version": view_payload.get("schema_version"),
                },
            )
            self.append_event(
                "run.failed" if failure else "run.completed",
                failure["message"] if failure else f"核销完成，共 {len(scenarios)} 个场景、{error_count} 个错误项",
                stage="failed" if failure else "complete",
                details=failure or {"scenarios": scenarios, "error_count": error_count},
                level="error" if failure else "info",
            )
            self._write_snapshot()
            self._write_manifest()

    def fail(self, exc: BaseException) -> None:
        with self._lock:
            failure = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            self.manifest.update(
                {
                    "status": "failed",
                    "completed_at": utc_now(),
                    "failure": failure,
                }
            )
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

    def _write_snapshot(self) -> None:
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
        atomic_write_json(self.snapshot_path, snapshot)
        digest = hashlib.sha256(self.snapshot_path.read_bytes()).hexdigest()
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
