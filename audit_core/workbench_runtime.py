from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import quote

from .common import AuditError
from .html_report import _embedded_payload
from .orchestrator import (
    DEFAULT_INPUT_DIR,
    SCENARIO_ORDER,
    EvidenceProvider,
    normalize_producer_model,
    normalize_run_id,
    run_audit,
)
from .workbench_store import (
    DEFAULT_WORKTREES_ROOT,
    PROJECT_ROOT,
    WorkbenchRunStore,
    reserve_workspace,
    sanitize_case,
)


ROOT_HTML = PROJECT_ROOT / "offline-activity-audit.html"
DEFAULT_WORKBENCH_URL = "http://192.0.0.108:8080/"


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
    scenario: str | None = None,
    workbench_url: str | None = None,
) -> dict[str, Any]:
    normalized_run_id = normalize_run_id(run_id)
    normalized_model = normalize_producer_model(producer_model)
    url = _normalized_workbench_url(
        workbench_url or os.environ.get("OFFLINE_AUDIT_WORKBENCH_URL")
    )
    if not ROOT_HTML.is_file():
        raise AuditError(f"找不到固定核销工作台：{ROOT_HTML}")

    workspace = reserve_workspace(
        normalized_run_id,
        normalized_model,
        worktrees_root,
    )
    store = WorkbenchRunStore(
        workspace,
        run_id=normalized_run_id,
        producer_model=normalized_model,
        root_html=ROOT_HTML,
        workbench_url=url,
    )
    stdout_tee = _Tee(sys.stdout, store.run_log_path)
    stderr_tee = _Tee(sys.stderr, store.run_log_path)

    try:
        with tempfile.TemporaryDirectory(prefix="offline-audit-render-") as temporary:
            staging_root = Path(temporary)
            with contextlib.redirect_stdout(stdout_tee), contextlib.redirect_stderr(stderr_tee):
                legacy = run_audit(
                    normalized_run_id,
                    producer_model=normalized_model,
                    input_dir=input_dir,
                    output_dir=staging_root,
                    evidence_provider=evidence_provider,
                    model=model,
                    scenario=scenario,
                    observer=store.observe,
                )
            generated_html = Path(str(legacy["output"]))
            view_payload = _embedded_payload(generated_html.read_text(encoding="utf-8"))
            verification = sanitize_case(legacy.get("verification") or {})
            scenarios = [str(value) for value in legacy.get("scenarios") or []]
            store.complete(
                view_payload=view_payload,
                scenarios=scenarios,
                verification=verification,
            )
    except BaseException as exc:
        store.fail(exc)
        raise

    run_url = url + "?run=" + quote(store.workspace_id, safe="")
    return {
        "run_id": normalized_run_id,
        "workspace_id": store.workspace_id,
        "producer_model": normalized_model,
        "status": "completed",
        "worktree": str(store.workspace),
        "output": str(ROOT_HTML),
        "workbench_url": run_url,
        "scenarios": list(store.manifest["scenarios"]),
        "error_count": int(store.manifest["error_count"]),
        "snapshot": str(store.snapshot_path),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从 input/ 的活动材料 ZIP 生成结构化核销档案，"
            "并发布到根目录固定局域网工作台"
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
        help="执行本次核销的模型来源标识，例如 codex、qwen3.7 或 glm-4.5",
    )
    parser.add_argument(
        "--scenario",
        choices=SCENARIO_ORDER,
        help="只运行指定核销类型；不传时运行 input/ 中全部已支持类型",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_persistent_audit(
            args.run_id,
            producer_model=args.producer_model,
            model=os.environ.get("OFFLINE_AUDIT_MODEL") or None,
            scenario=args.scenario,
        )
    except AuditError as exc:
        print(f"核销失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
