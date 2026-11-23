"""Keep material-gate cards small while retaining their source locators.

The caller owns a deep-copied business view. Original snapshots and technical
evidence are never passed here or changed by this projection helper.
"""
from __future__ import annotations

from typing import Any


def strip_material_source_facts(view: dict[str, Any] | None) -> dict[str, Any] | None:
    """Remove only repeated facts from classification-rejection card sources.

    Mutate and return the caller's copied view. All other fields, source entries,
    rows, and non-classification sheets retain their existing values and order.
    """
    if not isinstance(view, dict) or not isinstance(view.get("sheets"), list):
        return view
    for sheet in view["sheets"]:
        if not isinstance(sheet, dict) or sheet.get("projection_kind") != "classification_rejection":
            continue
        rows = sheet.get("rows")
        if not isinstance(rows, list):
            continue
        for row in rows:
            evidence = row.get("card_evidence") if isinstance(row, dict) else None
            sources = evidence.get("sources") if isinstance(evidence, dict) else None
            if not isinstance(sources, list):
                continue
            for source in sources:
                if isinstance(source, dict):
                    source.pop("facts", None)
    return view
