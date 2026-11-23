from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

from .common import AuditError, BizTypeRecognitionError, biz_type_skip_result
from .scenario_registry import SCENARIO_LABELS, scenario_for_biz_type
from .archive_input import ArchiveInputError, discover_zip_paths
from .model_metrics import collect_model_artifacts
from .execution_diagnostics import diagnose_run_errors, report_failure, set_run_context
from .codex_runner import (
    ALLOWED_REASONING_EFFORTS,
    DEFAULT_MODEL,
    DEFAULT_REASONING_EFFORT,
)
from .html_report import _embedded_payload
from .run_temporary import ManagedTemporaryDirectory, temporary_scope
from .orchestrator import (
    DEFAULT_INPUT_DIR,
    SCENARIO_ORDER,
    EvidenceProvider,
    normalize_producer_model,
    normalize_run_id,
    run_audit,
)
from .workbench_html import STATIC_ARCHIVE_FILENAME, render_static_run_archive
from .workbench_store import (
    DEFAULT_WORKTREES_ROOT,
    PROJECT_ROOT,
    WorkbenchRunStore,
    atomic_write_json,
    reserve_workspace,
    sanitize_case,
)


ROOT_HTML = PROJECT_ROOT / 'skills/orchestrate-offline-audit/assets/offline-activity-audit.html'
DEFAULT_WORKBENCH_URL = "http://192.0.0.148:8080/"


class LocalBizTypeRequiredError(AuditError):
    # This is an interactive prerequisite, before a task/run has been accepted.
    execution_not_started = True

    def __init__(self) -> None:
        super().__init__(
            "请先询问用户本次本地 ZIP 的核销方式，确认后通过 --biz-type 传入。可选："
            + "、".join(SCENARIO_LABELS.values())
            + "。多个 ZIP 的核销方式不同时，请逐包确认并分别运行。"
        )


def _ask_local_biz_type() -> str:
    """Ask on an interactive terminal before opening materials or creating a run."""
    if not sys.stdin.isatty():
        raise LocalBizTypeRequiredError()
    labels = tuple(SCENARIO_LABELS.values())
    print("本次本地 ZIP 使用哪种核销方式？", file=sys.stderr)
    print("本次选择适用于所选的全部 ZIP；如各包方式不同，请取消后分别指定 ZIP 运行。", file=sys.stderr)
    for index, label in enumerate(labels, 1):
        print(f"{index}. {label}", file=sys.stderr)
    while True:
        print("请输入序号或完整名称（输入 q 取消）：", file=sys.stderr, flush=True)
        try:
            answer = input()
        except (EOFError, KeyboardInterrupt):
            raise LocalBizTypeRequiredError() from None
        if answer == "q":
            raise LocalBizTypeRequiredError()
        if answer in labels:
            return answer
        if answer in {str(index) for index in range(1, len(labels) + 1)}:
            return labels[int(answer) - 1]
        print("尚未选择核销方式，请从上述八项中选择；不会自动判断。", file=sys.stderr)


def _input_archive_names(input_dir: str | Path, scenario: str | None) -> list[str]:
    try:
        paths = discover_zip_paths(input_dir)
    except ArchiveInputError:
        return []
    # Preserve all selected source names; the confirmed bizType alone selects a Skill.
    return [path.name for path in paths]


class _Tee(TextIO):
    def __init__(self, primary: TextIO, log_path: Path) -> None:
        self.primary = primary
        self.log_path = log_path
        self._lock = threading.Lock()

    @property
    def encoding(self) -> str:
        return "utf-8"

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        if not value:
            return 0
        with self._lock:
            self.primary.write(value)
            self.primary.flush()
            with self.log_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(value)
                stream.flush()
        return len(value)

    def flush(self) -> None:
        self.primary.flush()


def _normalized_workbench_url(value: str | None) -> str:
    raw = str(value or DEFAULT_WORKBENCH_URL).strip()
    if not raw.startswith(("http://", "https://")):
        raise AuditError("工作台地址必须使用 http:// 或 https://")
    return raw.rstrip("/") + "/"


@diagnose_run_errors
def run_persistent_audit(
    run_id: str,
    *,
    producer_model: str,
    input_dir: str | Path = DEFAULT_INPUT_DIR,
    worktrees_root: str | Path = DEFAULT_WORKTREES_ROOT,
    evidence_provider: EvidenceProvider | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    scenario: str | None = None,
    workbench_url: str | None = None,
    input_source: str = "input",
    biz_type: str | None = None,
    large_venue_fee: bool | None = None,
    cvs_otc_channel: bool = False,
    verify_code: str | None = None,
    qince_data_path: str | Path | None = None,
    resume_workspace: str | Path | None = None,
    revalidation_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    from .worker_handoff import wait_before_audit
    wait_before_audit(worktrees_root, run_id)
    injected_provider = evidence_provider is not None
    if input_source != "oss" and (not isinstance(biz_type, str) or not biz_type.strip()):
        raise LocalBizTypeRequiredError()
    if large_venue_fee is not None and not isinstance(large_venue_fee, bool):
        raise AuditError("largeVenueFee 必须是布尔值 true 或 false")
    if input_source == "oss" and biz_type is None:
        biz_type = ""
    normalized_run_id = normalize_run_id(run_id)
    normalized_model = normalize_producer_model(producer_model)
    if isinstance(biz_type, str) and biz_type.strip() and scenario_for_biz_type(biz_type) is None:
        return biz_type_skip_result(normalized_run_id, biz_type)
    from .model_settings import API_PROVIDERS, ModelSettingsStore, selection_from_environment
    from .session_model import selection_from_environment as session_selection
    connection = getattr(evidence_provider, 'connection', None) or session_selection() or selection_from_environment()
    if connection is None and model is None and evidence_provider is None:
        connection = ModelSettingsStore(Path(worktrees_root)).snapshot()
    if connection is not None and connection.provider in {*API_PROVIDERS, "codex-session"} and evidence_provider is None:
        from .pdf_materials import PdfEvidenceProvider
        model, reasoning_effort, normalized_model = connection.model, connection.reasoning_effort, connection.provider
        evidence_provider = PdfEvidenceProvider(model, reasoning_effort, connection=connection)
    set_run_context(provider=evidence_provider)
    selected_audit_model = str(
        model or (DEFAULT_MODEL if normalized_model == "codex" else normalized_model)
    ).strip()
    selected_reasoning_effort = str(
        reasoning_effort or DEFAULT_REASONING_EFFORT
    ).strip().lower()
    if (selected_reasoning_effort not in ALLOWED_REASONING_EFFORTS
            and not (connection is not None and connection.provider in {*API_PROVIDERS, "codex-session"}
                     and selected_reasoning_effort == connection.reasoning_effort)):
        raise AuditError(f"不支持的模型推理强度：{selected_reasoning_effort}")
    url = _normalized_workbench_url(
        workbench_url or os.environ.get("OFFLINE_AUDIT_WORKBENCH_URL")
    )
    if not ROOT_HTML.is_file():
        raise AuditError(f"找不到固定核销工作台：{ROOT_HTML}")

    source_archives = _input_archive_names(input_dir, scenario)
    workspace = Path(resume_workspace).resolve() if resume_workspace is not None else reserve_workspace(
        normalized_run_id,
        normalized_model,
        worktrees_root,
        audit_model=selected_audit_model,
        reasoning_effort=selected_reasoning_effort,
        archive_names=source_archives,
    )
    set_run_context(analysis=workspace / 'analysis')
    if resume_workspace is not None:
        if workspace.parent != Path(worktrees_root).resolve():
            raise AuditError('续跑目录必须是当前工作台已有运行目录')
        store = WorkbenchRunStore.resume_failed(workspace,run_id=normalized_run_id,
            revalidation_snapshot_sha256=revalidation_snapshot_sha256)
    else:
        store = WorkbenchRunStore(
        workspace,
        run_id=normalized_run_id,
        producer_model=normalized_model,
        root_html=ROOT_HTML,
        workbench_url=url,
        audit_model=selected_audit_model,
        reasoning_effort=selected_reasoning_effort,
        source_archives=source_archives,
            input_source=input_source,
        )
    from .run_liveness import claim_run, release_run
    claim_run(store.workspace)
    stdout_tee = _Tee(sys.stdout, store.run_log_path)
    stderr_tee = _Tee(sys.stderr, store.run_log_path)
    static_html_path = workspace / STATIC_ARCHIVE_FILENAME

    try:
        from .runtime_evidence_repair import repair_scope
        with temporary_scope(Path(worktrees_root)), ManagedTemporaryDirectory("render") as temporary, repair_scope():
            staging_root = Path(temporary)
            with contextlib.redirect_stdout(stdout_tee), contextlib.redirect_stderr(stderr_tee), collect_model_artifacts(store.workspace / "analysis", observer=store.observe):
                from .execution_trace import record_step
                record_step("run.started", "开始本单正式执行链", {"run_id": normalized_run_id, "workspace_id": store.workspace_id,
                    "verify_code": verify_code, "biz_type": biz_type, "source_archives": source_archives,
                    "producer_model": normalized_model, "audit_model": selected_audit_model,
                    "large_venue_fee": large_venue_fee, "cvs_otc_channel": cvs_otc_channel is True,
                    "model_context": {"scope": "this_run_only", "run_id": normalized_run_id,
                        "workspace_id": store.workspace_id,
                        "provider_instance": "injected_component" if injected_provider else "new_for_this_run",
                        "api_messages": "same_run_conversation", "cross_run_conversation": False}}, status="started")
                from .qince_context import load_context
                # An injected provider is an isolated/component execution. It
                # must not start real Qince queries just because this developer
                # machine now has working business credentials.
                qince_data = (load_context(verify_code, qince_data_path,
                                          scenario=scenario_for_biz_type(biz_type) or scenario)
                              if not injected_provider or qince_data_path is not None else None)
                legacy = run_audit(
                    normalized_run_id,
                    producer_model=normalized_model,
                    input_dir=input_dir,
                    output_dir=staging_root,
                    evidence_provider=evidence_provider,
                    model=selected_audit_model,
                    reasoning_effort=selected_reasoning_effort,
                    scenario=scenario,
                    biz_type=biz_type,
                    large_venue_fee=large_venue_fee,
                    cvs_otc_channel=cvs_otc_channel is True,
                    verify_code=verify_code, qince_data=qince_data,
                    worktrees_root=worktrees_root, workspace_id=store.workspace_id,
                    observer=store.observe,
                )
            if "view_payload" in legacy:
                view_payload = legacy["view_payload"]
            else:
                generated_html = Path(str(legacy["output"]))
                view_payload = _embedded_payload(generated_html.read_text(encoding="utf-8"))
            verification = sanitize_case(legacy.get("verification") or {})
            scenarios = [str(value) for value in legacy.get("scenarios") or []]
            store.complete(
                view_payload=view_payload,
                scenarios=scenarios,
                verification=verification,
                analysis_summary=legacy.get('reviewed_analysis_summary'),
            )
            with collect_model_artifacts(store.workspace / "analysis", observer=store.observe):
                record_step("run.completed", "本单结果、问题摘要及快照已保存", {"scenarios": scenarios,
                    "error_count": store.manifest["error_count"], "verification": verification,
                    "snapshot": "snapshot.json", "summary": "ai-analysis-summary.md"})
            static_html_path = render_static_run_archive(
                store.workspace,
                ROOT_HTML,
                workbench_url=url,
            )
    except BaseException as exc:
        from .execution_trace import record_step
        try:
            with collect_model_artifacts(store.workspace / "analysis", observer=store.observe):
                report_failure(exc, stage='execution')
        except BaseException:
            report_failure(exc, stage='execution')
        try:
            store.fail(exc)
            with collect_model_artifacts(store.workspace / "analysis", observer=store.observe):
                record_step("run.failed", "本单执行中断，失败档案已保存", {"error_type": type(exc).__name__,
                    "error_code": getattr(exc, "error_code", None), "snapshot": "snapshot.json"}, status="failed")
        except BaseException as archive_exc:
            report_failure(archive_exc, stage='failure_archive')
            raise exc
        try:
            static_html_path = render_static_run_archive(
                store.workspace,
                ROOT_HTML,
                workbench_url=url,
            )
        except Exception as archive_exc:
            report_failure(archive_exc, stage='static_archive')
            print(f"静态运行页面生成失败：{archive_exc}", file=sys.stderr)
        if not isinstance(exc, BizTypeRecognitionError):
            raise
    finally:
        release_run(store.workspace)

    run_url = url + "?run=" + quote(store.workspace_id, safe="")
    return {
        "run_id": normalized_run_id,
        "workspace_id": store.workspace_id,
        "producer_model": normalized_model,
        "audit_model": selected_audit_model,
        "reasoning_effort": selected_reasoning_effort,
        "status": store.manifest["status"],
        "failure": store.manifest.get("failure"),
        "worktree": str(store.workspace),
        "output": str(static_html_path),
        "static_html": str(static_html_path),
        "system_html": str(ROOT_HTML),
        "workbench_url": run_url,
        "scenarios": list(store.manifest["scenarios"]),
        "error_count": int(store.manifest["error_count"]),
        "snapshot": str(store.snapshot_path),
        "analysis_summary": str(store.analysis_summary_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从 input/ 的活动材料 ZIP 生成结构化核销档案，"
            "发布到固定局域网工作台并生成运行目录离线静态页面"
        )
    )
    parser.add_argument("--biz-type", help="用户已确认的本次核销方式；本地未传时先询问，非交互调用须先询问用户再传入")
    parser.add_argument("--large-venue-fee", choices=("true", "false"),
                        help="是否包含大型活动场地费；陈列堆头为true时须提供商场入场协议")
    parser.add_argument("--cvs-otc-channel", choices=("true", "false"), default="false",
                        help="是否属于CVS/OTC渠道；仅true按CVS/OTC进场费规则，默认false")
    parser.add_argument("--verify-code", help="本次核销单号；供已配置的勤策只读查询定位，不从文件名推断")
    parser.add_argument("--qince-data", help="本次勤策三个查询接口的返回JSON文件；用于本地验证或上游已有返回")
    parser.add_argument(
        "--run-id",
        required=True,
        help="本次运行标识，必须以 YYYYMMDD 业务日期开头",
    )
    parser.add_argument(
        "--producer-model",
        required=True,
        help="执行本次核销的生产来源标识，例如 codex、qwen3.8 或 glm-4.5",
    )
    parser.add_argument(
        "--model",
        help="本次实际使用的模型；未传时 codex 使用默认模型，其他来源沿用 producer-model",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=("default", "none", "enabled", "low", "medium", "high", "xhigh", "max", "ultra"),
        help=f"本次主要识别与判断的推理强度；默认 {DEFAULT_REASONING_EFFORT}",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIO_ORDER,
        help="仅作类型一致性检查，须与 --biz-type 对应；不能代替用户确认核销方式",
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help="ZIP 文件或输入目录；默认扫描项目 input/ 及其一级任务子目录中的 ZIP",
    )
    parser.add_argument(
        "--worktrees",
        default=str(DEFAULT_WORKTREES_ROOT),
        help="持久化运行目录；默认使用项目 worktrees/",
    )
    parser.add_argument("--input-source", choices=("input", "oss"), default="input", help="材料导入来源；OSS 适配器显式传入 oss")
    parser.add_argument(
        "--workbench-url",
        help="写入运行档案的工作台地址；默认读取 OFFLINE_AUDIT_WORKBENCH_URL",
    )
    parser.add_argument(
        "--result-json",
        help="将最终运行收据原子写入指定 JSON；供可信 OSS 入站适配器使用",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        biz_type = args.biz_type
        if args.input_source == "input" and (biz_type is None or not biz_type.strip()):
            biz_type = _ask_local_biz_type()
        result = run_persistent_audit(
            args.run_id,
            producer_model=args.producer_model,
            input_dir=args.input_dir,
            worktrees_root=args.worktrees,
            model=args.model or os.environ.get("OFFLINE_AUDIT_MODEL") or None,
            reasoning_effort=(
                args.reasoning_effort
                or os.environ.get("OFFLINE_AUDIT_REASONING_EFFORT")
                or None
            ),
            scenario=args.scenario,
            workbench_url=args.workbench_url,
            input_source=args.input_source,
            biz_type=biz_type if biz_type is not None else "",
            large_venue_fee=None if args.large_venue_fee is None else args.large_venue_fee == "true",
            cvs_otc_channel=args.cvs_otc_channel == "true",
            verify_code=args.verify_code, qince_data_path=args.qince_data,
        )
    except LocalBizTypeRequiredError as exc:
        print(f"尚未开始核销：{exc}", file=sys.stderr)
        return 2
    except AuditError as exc:
        print(f"核销失败：{exc}", file=sys.stderr)
        return 2
    if args.result_json:
        atomic_write_json(Path(args.result_json).resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "failed" else 0
