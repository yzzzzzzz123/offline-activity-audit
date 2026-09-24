from __future__ import annotations

from .paths import INPUT_ROOT, PROJECT_ROOT

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

from .common import AuditError, clean_identifier
from .scenario_registry import EVIDENCE_SCHEMA_BY_SCENARIO as EVIDENCE_SCHEMA, SCENARIO_ORDER


DEFAULT_INPUT_DIR = INPUT_ROOT
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "worktrees"
RESULT_SCHEMA = PROJECT_ROOT / "skills/orchestrate-offline-audit/references/contracts" / "audit-result.schema.json"
EvidenceProvider = Callable[[dict[str, Any], Path], dict[str, Any]]
RunObserver = Callable[[str, dict[str, Any]], None]


def _create_temporary_root(output_root: Path) -> Path:
    """Create a short-lived run directory without inheriting the long run label.

    Reference-image source paths are already necessarily long because their
    authoritative product folders contain barcode, product name, and product
    code. Keeping the temporary component bounded leaves enough path-length
    headroom on Windows while the UUID still makes concurrent runs exclusive.
    """

    for _ in range(10):
        candidate = output_root / f".oa-{uuid.uuid4().hex}"
        try:
            candidate.mkdir()
        except FileExistsError:
            continue
        return candidate
    raise AuditError("无法创建唯一的临时运行目录")


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
    base = root / f"{output_date}-{producer}.html"
    base_xlsx = base.with_suffix(".xlsx")
    revision_pattern = re.compile(
        rf"^{re.escape(output_date)}-{re.escape(producer)}-1\.(\d+)\.(?:xlsx|html)$"
    )
    revisions = [
        int(match.group(1))
        for path in root.iterdir()
        if path.is_file() and (match := revision_pattern.fullmatch(path.name)) is not None
    ]
    if not base.exists() and not base_xlsx.exists() and not revisions:
        return base
    revision = max(revisions, default=0) + 1
    while True:
        candidate = root / f"{output_date}-{producer}-1.{revision}.html"
        if not candidate.exists() and not candidate.with_suffix(".xlsx").exists():
            return candidate
        revision += 1


def _default_provider(
    model: str | None,
    reasoning_effort: str | None,
) -> EvidenceProvider:
    from .pdf_materials import PdfEvidenceProvider
    return PdfEvidenceProvider(model, reasoning_effort)






def _link_or_copy_exclusive(source: Path, target: Path) -> None:
    try:
        os.link(source, target)
        return
    except FileExistsError:
        raise
    except OSError:
        if target.exists():
            raise FileExistsError(target)

    try:
        with source.open("rb") as source_file, target.open("xb") as target_file:
            shutil.copyfileobj(source_file, target_file)
    except Exception:
        target.unlink(missing_ok=True)
        raise


def _publish_html_without_overwrite(
    temporary_workbook: Path,
    temporary_html: Path,
    run_id: str,
    producer_model: str,
    output_dir: Path,
) -> Path:
    """Publish the verified HTML exclusively and discard the internal workbook.

    Both temporary artifacts remain private until the HTML has been linked or
    copied to an unused final name.  Cleanup failures roll back the published
    HTML so a failed run cannot leave a result that appears complete.
    Historical XLSX files continue to reserve their old revision labels, but a
    new run never creates a final XLSX artifact.
    """

    while True:
        target_html = next_output_path(run_id, producer_model, output_dir)
        try:
            _link_or_copy_exclusive(temporary_html, target_html)
        except FileExistsError:
            continue
        try:
            temporary_html.unlink()
            temporary_workbook.unlink()
        except Exception:
            target_html.unlink(missing_ok=True)
            raise
        return target_html




def run_audit(
    run_id: str,
    *,
    producer_model: str,
    input_dir: str | Path = DEFAULT_INPUT_DIR,
    output_dir: str | Path = DEFAULT_OUTPUT_DIR,
    evidence_provider: EvidenceProvider | None = None,
    model: str | None = None,
    reasoning_effort: str | None = None,
    scenario: str | None = None,
    biz_type: str | None = None,
    large_venue_fee: bool | None = None,
    observer: RunObserver | None = None,
) -> dict[str, Any]:
    normalized_run_id = normalize_run_id(run_id)
    normalized_producer_model = normalize_producer_model(producer_model)
    output_root = Path(output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    provider = evidence_provider or _default_provider(model, reasoning_effort)
    temporary_root = _create_temporary_root(output_root)
    from .pdf_workflow import PdfWorkflow
    workflow = PdfWorkflow(
        run_id=normalized_run_id, producer_model=normalized_producer_model,
        input_dir=input_dir, temporary_root=temporary_root,
        provider=provider, scenario=scenario, observer=observer, biz_type=biz_type,
        large_venue_fee=large_venue_fee,
    )
    try:
        return workflow.run()
    finally:
        if temporary_root.exists():
            shutil.rmtree(temporary_root)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "从 input/ 的活动材料ZIP生成并校验线下活动核销结果，"
            "正式发布一个可离线打开的HTML文件"
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
        result = run_audit(
            args.run_id,
            producer_model=args.producer_model,
            model=os.environ.get("OFFLINE_AUDIT_MODEL") or None,
            reasoning_effort=os.environ.get("OFFLINE_AUDIT_REASONING_EFFORT") or None,
            scenario=args.scenario,
        )
    except AuditError as exc:
        print(f"核销失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
