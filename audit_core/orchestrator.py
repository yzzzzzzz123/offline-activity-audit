from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .archive_input import prepare_cases
from .codex_runner import extract_with_codex
from .common import AuditError, clean_identifier, validate_json
from .display import audit_display_case
from .personnel import audit_personnel_case
from .report import create_combined_report, verify_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "input"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "worktrees"
RESULT_SCHEMA = PROJECT_ROOT / "contracts" / "audit-result.schema.json"
EVIDENCE_SCHEMA = {
    "personnel_incentive": PROJECT_ROOT
    / "skills"
    / "audit-personnel-incentive"
    / "references"
    / "evidence.schema.json",
    "promotional_display": PROJECT_ROOT
    / "skills"
    / "audit-promotional-display"
    / "references"
    / "evidence.schema.json",
}
SCENARIO_ORDER = ("personnel_incentive", "promotional_display")
EvidenceProvider = Callable[[dict[str, Any], Path], dict[str, Any]]


def normalize_run_id(value: str) -> str:
    raw = value.strip()
    if not raw or clean_identifier(raw) != raw or raw in {".", ".."}:
        raise AuditError(
            "run-id 只能包含中英文、数字、点、下划线和连字符，且不得超过80个字符"
        )
    return raw


def normalize_producer_model(value: str) -> str:
    raw = unicodedata.normalize("NFKC", value).strip().lower()
    if (
        not raw
        or len(raw) > 40
        or raw in {".", ".."}
        or re.fullmatch(r"[a-z0-9][a-z0-9._-]*", raw) is None
    ):
        raise AuditError(
            "producer-model 只能包含英文字母、数字、点、下划线和连字符，"
            "必须以字母或数字开头，且不得超过40个字符"
        )
    return raw


def output_date_from_run_id(value: str) -> str:
    run_id = normalize_run_id(value)
    match = re.match(r"^(\d{8})(?:$|[._-])", run_id)
    if match is None:
        raise AuditError("run-id 必须以有效的 YYYYMMDD 业务日期开头")
    output_date = match.group(1)
    try:
        datetime.strptime(output_date, "%Y%m%d")
    except ValueError as exc:
        raise AuditError("run-id 必须以有效的 YYYYMMDD 业务日期开头") from exc
    return output_date


def next_output_path(
    run_id: str,
    producer_model: str,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
) -> Path:
    root = Path(output_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    output_date = output_date_from_run_id(run_id)
    producer = normalize_producer_model(producer_model)
    base = root / f"{output_date}-{producer}.xlsx"
    if not base.exists():
        return base
    revision = 1
    while True:
        candidate = root / f"{output_date}-{producer}-1.{revision}.xlsx"
        if not candidate.exists():
            return candidate
        revision += 1


def _default_provider(model: str | None) -> EvidenceProvider:
    def provider(case: dict[str, Any], temporary_root: Path) -> dict[str, Any]:
        return extract_with_codex(case, temporary_root, model=model)

    return provider


def _publish_without_overwrite(
    temporary_file: Path,
    run_id: str,
    producer_model: str,
    output_dir: Path,
) -> Path:
    while True:
        target = next_output_path(run_id, producer_model, output_dir)
        try:
            os.link(temporary_file, target)
        except FileExistsError:
            continue
        except OSError:
            if target.exists():
                continue
            os.rename(temporary_file, target)
            return target
        temporary_file.unlink()
        return target


def run_audit(
    run_id: str,
    *,
    producer_model: str,
    input_dir: str | Path = DEFAULT_INPUT_DIR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    evidence_provider: EvidenceProvider | None = None,
    model: str | None = None,
) -> dict[str, Any]:
    normalized_run_id = normalize_run_id(run_id)
    normalized_producer_model = normalize_producer_model(producer_model)
    output_root = Path(output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    provider = evidence_provider or _default_provider(model)
    temporary_workbook: Path | None = None
    temporary_root = output_root / f".offline-audit-{normalized_run_id}-{uuid.uuid4().hex}"
    if temporary_root.exists():
        raise AuditError(f"临时运行目录冲突：{temporary_root}")
    temporary_root.mkdir()

    try:
        cases = prepare_cases(input_dir, temporary_root / "sources")
        results: list[dict[str, Any]] = []
        scenarios = [scenario for scenario in SCENARIO_ORDER if scenario in cases]
        for scenario in scenarios:
            case = cases[scenario]
            evidence = provider(case, temporary_root)
            validate_json(evidence, EVIDENCE_SCHEMA[scenario])
            if scenario == "personnel_incentive":
                result = audit_personnel_case(case, evidence)
            else:
                result = audit_display_case(case, evidence)
            validate_json(result, RESULT_SCHEMA)
            results.append(result)

        temporary_workbook = output_root / f".{normalized_run_id}-{uuid.uuid4().hex}.xlsx"
        create_combined_report(results, temporary_workbook)
        verification = verify_workbook(temporary_workbook, scenarios)
        published = _publish_without_overwrite(
            temporary_workbook,
            normalized_run_id,
            normalized_producer_model,
            output_root,
        )
        temporary_workbook = None
        verification["path"] = str(published)
        return {
            "run_id": normalized_run_id,
            "producer_model": normalized_producer_model,
            "output": str(published),
            "scenarios": scenarios,
            "verification": verification,
        }
    finally:
        if temporary_workbook and temporary_workbook.exists():
            temporary_workbook.unlink()
        if temporary_root.exists():
            shutil.rmtree(temporary_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 input/ 的1～2个活动材料ZIP生成线下活动核销Excel"
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = run_audit(
            args.run_id,
            producer_model=args.producer_model,
            model=os.environ.get("OFFLINE_AUDIT_MODEL") or None,
        )
    except AuditError as exc:
        print(f"核销失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
