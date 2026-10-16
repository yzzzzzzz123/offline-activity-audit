"""Display only the submitted files relevant to each saved material-gate issue."""
from __future__ import annotations

from copy import deepcopy

from .customer_language import concise_reason, plain_text
from .pdf_evidence import classification_issues
from .scenario_registry import SCENARIO_LABELS


def attach_material_issue_files(view: dict | None, packets: list[dict]) -> dict | None:
    """Project file identities from material assignments, preserving raw evidence.

    Historical reports are matched by packet identity (or an unambiguous archive
    name). Never infer a material's role from its filename or use a package list.
    """
    if not isinstance(view, dict) or not packets:
        return view
    projected = deepcopy(view)
    for sheet in projected.get("sheets") or []:
        if sheet.get("projection_kind") != "classification_rejection":
            continue
        matches = [packet for packet in packets
                   if (packet.get("archive_id") == sheet["archive_id"] if sheet.get("archive_id")
                       else packet.get("source_archive") == sheet.get("source_archive"))]
        if len(matches) != 1:
            continue
        packet = matches[0]
        classification = packet.get("classification")
        if not isinstance(classification, dict):
            continue
        units = packet.get("units") or []
        by_id = {unit["unit_id"]: unit["source_file"] for unit in units}
        selected = sheet.get("scenario") if sheet.get("confirmed_scenario") else None
        label = SCENARIO_LABELS.get(sheet.get("scenario"), "")
        files_by_reason: dict[str, list[str]] = {}
        for issue in classification_issues(classification, selected):
            reason = concise_reason(plain_text(issue["reason"], units),
                                    archive=sheet.get("source_archive", ""), label=label,
                                    files=list(by_id.values()))
            files = files_by_reason.setdefault(reason, [])
            for uid in issue["source_ids"]:
                source = by_id.get(uid)
                if source and source not in files:
                    files.append(source)
        for row in sheet.get("rows") or []:
            reasons = row.get("error_reasons") or [row.get("error_reason", "")]
            if not reasons or any(reason not in files_by_reason for reason in reasons):
                continue
            row["business_source_files"] = list(dict.fromkeys(
                source for reason in reasons for source in files_by_reason[reason]))
    return projected
