from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import unicodedata
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .archive_input import MaterialInputError, prepare_cases
from .codex_runner import extract_with_codex
from .common import AuditError, clean_identifier, validate_json
from .display import audit_display_case
from .product_database import with_product_catalog
from .entry_fee import audit_entry_fee_case
from .giveaway_promotion import audit_giveaway_promotion_case
from .html_report import _embedded_payload, create_html_report_from_workbook, verify_html_report
from .maintenance_fee import audit_maintenance_fee_case
from .other_expense import audit_other_expense_case
from .personnel import audit_personnel_case
from .poster_material import audit_poster_material_case
from .pos_target_incentive import audit_pos_target_incentive_case
from .price_difference_support import audit_price_difference_support_case
from .report import create_combined_report, verify_workbook
from .scenario_registry import EVIDENCE_SCHEMA_BY_SCENARIO as EVIDENCE_SCHEMA, SCENARIO_ORDER
from .self_procured_gift_material import audit_self_procured_gift_material_case
from .workflow import build_audit_chain, invoke_audit_chain


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "input"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "worktrees"
RESULT_SCHEMA = PROJECT_ROOT / "contracts" / "audit-result.schema.json"
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
    def provider(case: dict[str, Any], temporary_root: Path) -> dict[str, Any]:
        if case.get("kind") == "material_diagnostic":
            from .material_diagnostic_ai import extract_material_diagnosis
            return extract_material_diagnosis(
                case, temporary_root, model=model, reasoning_effort=reasoning_effort,
            )
        return extract_with_codex(
            case,
            temporary_root,
            model=model,
            reasoning_effort=reasoning_effort,
        )

    return provider


def _prepare_audit_inputs(
    input_dir: str | Path, temporary_root: Path, selected_scenarios: set[str] | None,
) -> tuple[dict[str, Any], dict[str, Any] | None, list[dict[str, Any]]]:
    """Reject unknown ZIP types; diagnose material blockers for known types only."""
    try:
        return prepare_cases(
            input_dir, temporary_root / "sources", selected_scenarios=selected_scenarios,
        ), None, []
    except MaterialInputError as original_error:
        from .archive_input import _classify_archive
        from .material_intake import (
            _scenario_hint, prepare_classification_rejection, prepare_material_diagnosis,
        )

        paths = sorted((p for p in Path(input_dir).iterdir()
                        if p.is_file() and p.suffix.lower() == ".zip"),
                       key=lambda p: p.name.casefold())
        cases: dict[str, Any] = {}
        source_by_scenario: dict[str, Path] = {}
        review_paths: set[Path] = set()
        duplicate_scenarios: set[str] = set()
        rejections: list[dict[str, Any]] = []
        for index, path in enumerate(paths):
            if _scenario_hint(path.name) is None:
                rejections.append(prepare_classification_rejection(
                    path, temporary_root / f"classification-sources-{index:02d}",
                    archive_id=f"r{index + 1:03d}",
                ))
                continue
            if selected_scenarios is not None:
                try:
                    identified = _classify_archive(path)["scenario"]
                except MaterialInputError:
                    identified = _scenario_hint(path.name)
                if identified is not None and identified not in selected_scenarios:
                    continue
            single_input = temporary_root / f"single-input-{index:02d}"
            single_input.mkdir()
            _link_or_copy_exclusive(path, single_input / path.name)
            try:
                prepared = prepare_cases(
                    single_input, temporary_root / f"single-sources-{index:02d}",
                )
            except MaterialInputError:
                review_paths.add(path)
                try:
                    identified = _classify_archive(path)["scenario"]
                except MaterialInputError:
                    identified = _scenario_hint(path.name)
                if identified is not None:
                    if identified in source_by_scenario:
                        review_paths.add(source_by_scenario[identified])
                        cases.pop(identified, None)
                    source_by_scenario.setdefault(identified, path)
                    duplicate_scenarios.add(identified)
                continue
            for name, case in prepared.items():
                if selected_scenarios is not None and name not in selected_scenarios:
                    continue
                case["source_archive"] = path.resolve()
                if name in cases or name in duplicate_scenarios:
                    review_paths.update((source_by_scenario[name], path))
                    cases.pop(name, None)
                    duplicate_scenarios.add(name)
                else:
                    cases[name] = case
                    source_by_scenario[name] = path
        if not review_paths:
            if cases or rejections:
                return cases, None, rejections
            raise original_error
        diagnostic_input = temporary_root / "material-input"
        diagnostic_input.mkdir()
        for path in sorted(review_paths):
            _link_or_copy_exclusive(path, diagnostic_input / path.name)
        review = prepare_material_diagnosis(
            diagnostic_input, temporary_root / "material-sources",
            original_error=str(original_error), selected_scenarios=selected_scenarios,
        )
        return cases, review, rejections


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


@dataclass
class _AuditWorkflow:
    """One run's state, passed through each typed LCEL stage in order."""

    run_id: str
    producer_model: str
    input_dir: str | Path
    output_root: Path
    temporary_root: Path
    provider: EvidenceProvider
    scenario: str | None
    observer: RunObserver | None
    temporary_workbook: Path | None = None
    temporary_html: Path | None = None
    receipt: dict[str, Any] = field(default_factory=dict)
    cases: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    scenarios: list[str] = field(default_factory=list, init=False)
    diagnostic_case: dict[str, Any] | None = field(default=None, init=False, repr=False)
    classification_rejections: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    evidence_by_scenario: dict[str, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)
    diagnostic_evidence: dict[str, Any] | None = field(default=None, init=False, repr=False)
    results: list[dict[str, Any]] = field(default_factory=list, init=False, repr=False)
    diagnostic_result: dict[str, Any] | None = field(default=None, init=False, repr=False)
    diagnostic_view: dict[str, Any] | None = field(default=None, init=False, repr=False)
    supplemental_view: dict[str, Any] | None = field(default=None, init=False, repr=False)
    supplemental_verification: dict[str, Any] = field(default_factory=dict, init=False, repr=False)
    rejection_result: dict[str, Any] | None = field(default=None, init=False, repr=False)

    def intake(self) -> _AuditWorkflow:
        selected_scenarios = {self.scenario} if self.scenario else None
        self.cases, self.diagnostic_case, self.classification_rejections = _prepare_audit_inputs(
            self.input_dir, self.temporary_root, selected_scenarios,
        )
        self.scenarios = [name for name in SCENARIO_ORDER if name in self.cases]
        if self.observer is not None:
            self.observer(
                "cases.prepared",
                {
                    "cases": self.cases,
                    "scenarios": self.scenarios,
                    "temporary_root": str(self.temporary_root),
                },
            )
        if {"personnel_incentive", "promotional_display"}.intersection(self.scenarios):
            from .product_database import load_product_catalog
            catalog = load_product_catalog()
            if self.observer is not None:
                self.observer("product_database.loaded", {"catalog": catalog})
        return self

    def analysis(self) -> _AuditWorkflow:
        self.evidence_by_scenario = {}
        for scenario_name in self.scenarios:
            case = self.cases[scenario_name]
            if self.observer is not None:
                self.observer("scenario.started", {"scenario": scenario_name})
            self.evidence_by_scenario[scenario_name] = self.provider(case, self.temporary_root)

        self.diagnostic_evidence = None
        if self.diagnostic_case is not None:
            if self.observer is not None:
                self.observer("material_diagnostic.started", {"case": self.diagnostic_case})
            self.diagnostic_evidence = self.provider(self.diagnostic_case, self.temporary_root)
        return self

    def evidence(self) -> _AuditWorkflow:
        for scenario_name in self.scenarios:
            evidence = self.evidence_by_scenario[scenario_name]
            validate_json(evidence, EVIDENCE_SCHEMA[scenario_name])
            if self.observer is not None:
                self.observer(
                    "evidence.validated",
                    {"scenario": scenario_name, "evidence": evidence},
                )

        if self.diagnostic_case is not None:
            from .material_diagnostic_ai import validate_material_observations
            validate_material_observations(self.diagnostic_case, self.diagnostic_evidence)
            if self.observer is not None:
                self.observer("material_diagnostic.evidence_validated", {"evidence": self.diagnostic_evidence})
        return self

    def decision(self) -> _AuditWorkflow:
        self.results = []
        for scenario_name in self.scenarios:
            case = self.cases[scenario_name]
            evidence = self.evidence_by_scenario[scenario_name]
            if scenario_name == "personnel_incentive":
                result = audit_personnel_case(case, evidence)
            elif scenario_name == "promotional_display":
                result = audit_display_case(case, evidence)
            elif scenario_name == "poster_material":
                result = audit_poster_material_case(case, evidence)
            elif scenario_name == "other_expense":
                result = audit_other_expense_case(case, evidence)
            elif scenario_name == "maintenance_fee":
                result = audit_maintenance_fee_case(case, evidence)
            elif scenario_name == "giveaway_promotion":
                result = audit_giveaway_promotion_case(case, evidence)
            elif scenario_name == "price_difference_support":
                result = audit_price_difference_support_case(case, evidence)
            elif scenario_name == "pos_target_incentive":
                result = audit_pos_target_incentive_case(case, evidence)
            elif scenario_name == "entry_fee":
                result = audit_entry_fee_case(case, evidence)
            elif scenario_name == "self_procured_gift_material":
                result = audit_self_procured_gift_material_case(case, evidence)
            else:
                raise AuditError(f"未登记的核销处理器：{scenario_name}")
            validate_json(result, RESULT_SCHEMA)
            self.results.append(result)
            if self.observer is not None:
                self.observer(
                    "result.validated",
                    {"scenario": scenario_name, "result": result},
                )

        self.diagnostic_result = None
        self.diagnostic_view = None
        if self.diagnostic_case is not None:
            from .material_diagnostic_output import (
                build_material_diagnostic_result, build_material_diagnostic_view,
            )
            self.diagnostic_result = build_material_diagnostic_result(self.diagnostic_case, self.diagnostic_evidence)
            self.diagnostic_view = build_material_diagnostic_view(self.diagnostic_result)
            if self.observer is not None:
                self.observer("material_diagnostic.result_validated", {"result": self.diagnostic_result})

        self.supplemental_view = self.diagnostic_view
        self.supplemental_verification = {}
        if self.diagnostic_result is not None:
            self.supplemental_verification["material_diagnostic"] = {
                "schema_validated": True, "source_coverage_validated": True,
            }
        self.rejection_result = None
        if self.classification_rejections:
            from .material_diagnostic_output import (
                build_classification_rejection_result, build_classification_rejection_view,
            )
            self.rejection_result = build_classification_rejection_result(self.classification_rejections)
            rejection_view = build_classification_rejection_view(self.rejection_result)
            if self.supplemental_view is None:
                self.supplemental_view = rejection_view
            else:
                self.supplemental_view["sheets"].extend(rejection_view["sheets"])
            self.supplemental_view["classification_rejection"] = self.rejection_result
            self.supplemental_verification["classification_rejection"] = {
                "schema_validated": True, "archive_safety_validated": True,
                "ai_called": False, "conclusion": "rejected",
            }
            if self.observer is not None:
                self.observer("classification_rejection.result_validated", {"result": self.rejection_result})
        return self

    def _render_verified_report(self) -> dict[str, Any]:
        if not self.results and self.supplemental_view is not None:
            self.scenarios = [name for name in SCENARIO_ORDER if any(
                a.get("scenario") == name for a in (self.diagnostic_result or {}).get("archives", [])
            )]
            verification = self.supplemental_verification
            if self.observer is not None:
                self.observer("report.verified", {"verification": verification})
            return {"run_id": self.run_id, "producer_model": self.producer_model,
                    "view_payload": self.supplemental_view, "scenarios": self.scenarios, "verification": verification}

        self.temporary_workbook = self.temporary_root / "internal-report.xlsx"
        self.temporary_html = self.temporary_workbook.with_suffix(".html")
        create_combined_report(self.results, self.temporary_workbook)
        verification = verify_workbook(self.temporary_workbook, self.scenarios)
        create_html_report_from_workbook(self.temporary_workbook, self.temporary_html)
        html_verification = verify_html_report(
            self.temporary_html,
            self.scenarios,
            workbook_path=self.temporary_workbook,
        )
        if self.supplemental_view is not None:
            combined_view = _embedded_payload(self.temporary_html.read_text(encoding="utf-8"))
            combined_view["sheets"].extend(self.supplemental_view["sheets"])
            if self.diagnostic_result is not None:
                combined_view["material_diagnostic"] = self.diagnostic_result
            if self.rejection_result is not None:
                combined_view["classification_rejection"] = self.rejection_result
            self.scenarios = [name for name in SCENARIO_ORDER if name in self.cases or any(
                a.get("scenario") == name for a in (self.diagnostic_result or {}).get("archives", [])
            )]
            combined_verification = {"workbook": verification, "html": html_verification,
                                     **self.supplemental_verification}
            if self.observer is not None:
                self.observer("report.verified", {"verification": combined_verification})
            return {"run_id": self.run_id, "producer_model": self.producer_model,
                    "view_payload": combined_view, "scenarios": self.scenarios, "verification": combined_verification}
        if self.observer is not None:
            self.observer(
                "report.verified",
                {
                    "verification": {
                        "workbook": verification,
                        "html": html_verification,
                    }
                },
            )
        published_html = _publish_html_without_overwrite(
            self.temporary_workbook,
            self.temporary_html,
            self.run_id,
            self.producer_model,
            self.output_root,
        )
        self.temporary_workbook = None
        self.temporary_html = None
        verification.pop("path", None)
        html_verification["path"] = str(published_html)
        verification["html"] = html_verification
        return {
            "run_id": self.run_id,
            "producer_model": self.producer_model,
            "output": str(published_html),
            "outputs": {
                "html": str(published_html),
            },
            "scenarios": self.scenarios,
            "verification": verification,
        }

    def verification(self) -> _AuditWorkflow:
        self.receipt = self._render_verified_report()
        return self

    def chain(self):
        return build_audit_chain([
            ("intake", _AuditWorkflow.intake),
            ("analysis", _AuditWorkflow.analysis),
            ("evidence", _AuditWorkflow.evidence),
            ("decision", _AuditWorkflow.decision),
            ("verification", _AuditWorkflow.verification),
        ])


@with_product_catalog
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
    observer: RunObserver | None = None,
) -> dict[str, Any]:
    normalized_run_id = normalize_run_id(run_id)
    normalized_producer_model = normalize_producer_model(producer_model)
    output_root = Path(output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    provider = evidence_provider or _default_provider(model, reasoning_effort)
    temporary_root = _create_temporary_root(output_root)
    workflow = _AuditWorkflow(
        run_id=normalized_run_id, producer_model=normalized_producer_model,
        input_dir=input_dir, output_root=output_root, temporary_root=temporary_root,
        provider=provider, scenario=scenario, observer=observer,
    )
    try:
        completed = invoke_audit_chain(workflow.chain(), workflow)
        return completed.receipt
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
