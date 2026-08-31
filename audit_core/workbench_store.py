from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .common import AuditError
from .orchestrator import normalize_producer_model, output_date_from_run_id


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKTREES_ROOT = PROJECT_ROOT / "worktrees"
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


WORKSPACE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


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


def _reserved_workspace_ids(root: Path, base: str) -> set[str]:
    reserved: set[str] = set()
    if not root.exists():
        return reserved
    pattern = re.compile(rf"^{re.escape(base)}(?:-1\.\d+)?$")
    for path in root.iterdir():
        stem = path.stem if path.is_file() else path.name
        if pattern.fullmatch(stem):
            reserved.add(stem)
    return reserved


def reserve_workspace(
    run_id: str,
    producer_model: str,
    root: str | Path = DEFAULT_WORKTREES_ROOT,
) -> Path:
    worktrees_root = Path(root).resolve()
    worktrees_root.mkdir(parents=True, exist_ok=True)
    output_date = output_date_from_run_id(run_id)
    producer = normalize_producer_model(producer_model)
    base = f"{output_date}-{producer}"

    for _ in range(1000):
        reserved = _reserved_workspace_ids(worktrees_root, base)
        if base not in reserved:
            candidate_id = base
        else:
            revisions = [
                int(match.group(1))
                for value in reserved
                if (match := re.fullmatch(rf"{re.escape(base)}-1\.(\d+)", value))
            ]
            candidate_id = f"{base}-1.{max(revisions, default=0) + 1}"
        candidate = worktrees_root / candidate_id
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise AuditError("无法为本次核销保留唯一 worktree 运行目录")


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
        self._lock = threading.RLock()
        self._event_seq = 0
        self._checkpoint_seq = 0
        self._view_payload: dict[str, Any] | None = None
        self._verification: dict[str, Any] | None = None
        self._analysis_files: list[dict[str, Any]] = []
        self._checkpoints: list[dict[str, Any]] = []
        now = utc_now()
        self.manifest: dict[str, Any] = {
            "schema_version": "1.1",
            "workspace_id": self.workspace_id,
            "run_id": run_id,
            "business_date": output_date_from_run_id(run_id),
            "producer_model": normalize_producer_model(producer_model),
            "status": "running",
            "created_at": now,
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
        if event_type == "cases.prepared":
            cases = sanitize_case(payload.get("cases") or {})
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
            self.append_event(
                "scenario.started",
                "开始结构化识别",
                stage="analysis",
                scenario=scenario,
            )
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
            self._view_payload = view_payload
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
                }
            )
            self._verification = verification
            self.write_checkpoint(
                "ready",
                "固定工作台数据已就绪",
                {
                    "scenarios": scenarios,
                    "error_count": error_count,
                    "view_schema_version": view_payload.get("schema_version"),
                },
            )
            self.append_event(
                "run.completed",
                f"核销完成，共 {len(scenarios)} 个场景、{error_count} 个错误项",
                stage="complete",
                details={"scenarios": scenarios, "error_count": error_count},
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
