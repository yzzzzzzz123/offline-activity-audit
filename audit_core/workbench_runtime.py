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

from .common import AuditError
from .archive_input import discover_archives
from .model_metrics import collect_model_artifacts
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


ROOT_HTML = PROJECT_ROOT / "offline-activity-audit.html"
DEFAULT_WORKBENCH_URL = "http://192.0.0.148:8080/"


def _input_archive_names(input_dir: str | Path, scenario: str | None) -> list[str]:
    root = Path(input_dir)
    if not root.is_dir() or root.is_symlink():
        return []
    paths = sorted((path for path in root.iterdir() if path.is_file() and path.suffix.lower() == ".zip"), key=lambda path: path.name.casefold())
    # Every archive must be inspected before a content-derived type is known.
    # --scenario filters confirmed types afterwards; names never select a Skill.
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
) -> dict[str, Any]:
    normalized_run_id = normalize_run_id(run_id)
    normalized_model = normalize_producer_model(producer_model)
    selected_audit_model = str(
        model or (DEFAULT_MODEL if normalized_model == "codex" else normalized_model)
    ).strip()
    selected_reasoning_effort = str(
        reasoning_effort or DEFAULT_REASONING_EFFORT
    ).strip().lower()
    if selected_reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise AuditError(f"不支持的模型推理强度：{selected_reasoning_effort}")
    url = _normalized_workbench_url(
        workbench_url or os.environ.get("OFFLINE_AUDIT_WORKBENCH_URL")
    )
    if not ROOT_HTML.is_file():
        raise AuditError(f"找不到固定核销工作台：{ROOT_HTML}")

    source_archives = _input_archive_names(input_dir, scenario)
    workspace = reserve_workspace(
        normalized_run_id,
        normalized_model,
        worktrees_root,
        audit_model=selected_audit_model,
        reasoning_effort=selected_reasoning_effort,
        archive_names=source_archives,
    )
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
    stdout_tee = _Tee(sys.stdout, store.run_log_path)
    stderr_tee = _Tee(sys.stderr, store.run_log_path)
    static_html_path = workspace / STATIC_ARCHIVE_FILENAME

    try:
        with temporary_scope(Path(worktrees_root)), ManagedTemporaryDirectory("render") as temporary:
            staging_root = Path(temporary)
            with contextlib.redirect_stdout(stdout_tee), contextlib.redirect_stderr(stderr_tee), collect_model_artifacts(store.workspace / "analysis"):
                legacy = run_audit(
                    normalized_run_id,
                    producer_model=normalized_model,
                    input_dir=input_dir,
                    output_dir=staging_root,
                    evidence_provider=evidence_provider,
                    model=selected_audit_model,
                    reasoning_effort=selected_reasoning_effort,
                    scenario=scenario,
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
            )
            static_html_path = render_static_run_archive(
                store.workspace,
                ROOT_HTML,
                workbench_url=url,
            )
    except BaseException as exc:
        store.fail(exc)
        try:
            static_html_path = render_static_run_archive(
                store.workspace,
                ROOT_HTML,
                workbench_url=url,
            )
        except Exception as archive_exc:
            print(f"静态运行页面生成失败：{archive_exc}", file=sys.stderr)
        raise

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
        choices=("low", "medium", "high", "xhigh", "max", "ultra"),
        help=f"本次主要识别与判断的推理强度；默认 {DEFAULT_REASONING_EFFORT}",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIO_ORDER,
        help="按资料内容识别后，只审核指定类型；不能覆盖内容分类结果",
    )
    parser.add_argument(
        "--input-dir",
        default=str(DEFAULT_INPUT_DIR),
        help="本次运行的 ZIP 输入目录；默认使用项目 input/",
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
        )
    except AuditError as exc:
        print(f"核销失败：{exc}", file=sys.stderr)
        return 2
    if args.result_json:
        atomic_write_json(Path(args.result_json).resolve(), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 2 if result["status"] == "failed" else 0
