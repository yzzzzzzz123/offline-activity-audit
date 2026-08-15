from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from .common import AuditError, load_json, validate_json, write_json
from .display import audit_display_case
from .personnel import audit_personnel_case


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RESULT_SCHEMA = PROJECT_ROOT / "contracts" / "audit-result.schema.json"
SCENARIOS: dict[str, dict[str, Any]] = {
    "personnel_incentive": {
        "schema": PROJECT_ROOT
        / "skills"
        / "audit-personnel-incentive"
        / "references"
        / "evidence.schema.json",
        "audit": audit_personnel_case,
    },
    "promotional_display": {
        "schema": PROJECT_ROOT
        / "skills"
        / "audit-promotional-display"
        / "references"
        / "evidence.schema.json",
        "audit": audit_display_case,
    },
}


def run_skill(
    scenario: str,
    case_path: str | Path,
    evidence_path: str | Path,
    output_path: str | Path,
) -> dict[str, Any]:
    if scenario not in SCENARIOS:
        raise AuditError(f"Unsupported scenario: {scenario}")
    case = load_json(case_path)
    if case.get("scenario") != scenario:
        raise AuditError(
            f"Case scenario {case.get('scenario')!r} does not match skill scenario {scenario!r}"
        )
    evidence = load_json(evidence_path)
    validate_json(evidence, SCENARIOS[scenario]["schema"])
    result = SCENARIOS[scenario]["audit"](case_path, case, evidence)
    validate_json(result, RESULT_SCHEMA)
    write_json(output_path, result)
    return result


def skill_main(scenario: str, argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=f"Run deterministic {scenario} audit")
    parser.add_argument("--case", required=True, help="Path to case JSON")
    parser.add_argument("--evidence", required=True, help="Path to extracted evidence JSON")
    parser.add_argument("--output", required=True, help="Path for audit result JSON")
    args = parser.parse_args(argv)
    try:
        result = run_skill(scenario, args.case, args.evidence, args.output)
    except AuditError as exc:
        parser.exit(2, f"audit failed: {exc}\n")
    summary = result["summary"]
    print(
        f"{result['case_id']}: {summary['conclusion']}, "
        f"suggested={summary['suggested_approved_amount']}"
    )
    return 0
