"""Bind a workbook photo to its exact authored row, without inferring identity."""
from __future__ import annotations

from collections import Counter
from pathlib import Path
import re


_PLACEMENT = re.compile(r"(?P<sheet>.+)!R(?P<row>[1-9]\d*)C[1-9]\d*（(?:浮动图片[1-9]\d*|单元格图片)）")


def photo_row_context(photo: dict, documents: list[dict], units: list[dict],
                      table_ids: set[str]) -> list[dict]:
    """Return one unambiguous native row already available in this prompt.

    File IDs, original filenames, sheet names and row numbers are exact.
    Missing/duplicate ownership or conflicting rows return no binding; the
    caller keeps the complete original context independently.
    """
    file_id, source_file = photo.get("file_id"), photo.get("source_file")
    if not file_id or not source_file or Path(source_file).suffix.lower() not in {".xls", ".xlsx", ".xlsm"}:
        return []
    placement = _PLACEMENT.fullmatch(str(photo.get("locator", "")))
    if placement is None:
        return []
    sheet, row = placement.group("sheet", "row")
    locator = f"{sheet}!R{row}"
    prefix = locator + ": "
    unit_counts = Counter(unit.get("unit_id") for unit in units)
    document_counts = Counter(document.get("unit_id") for document in documents)
    document_by_id = {document.get("unit_id"): document for document in documents}
    matches = []
    for unit in units:
        uid = unit.get("unit_id")
        if (uid not in table_ids or unit_counts[uid] != 1 or document_counts[uid] != 1
                or unit.get("file_id") != file_id or unit.get("source_file") != source_file
                or unit.get("locator") != sheet or unit.get("image")
                or unit.get("photo_reference") or unit.get("source_kind") == "qince"):
            continue
        document_facts = document_by_id[uid].get("facts", [])
        for fact in unit.get("native_facts", []):
            if isinstance(fact, str) and fact.startswith(prefix) and fact in document_facts:
                matches.append({"unit_id": uid, "source_file": source_file,
                                "locator": locator, "fact": fact,
                                "meaning": "表格作者标注，不是照片水印或照片可见内容"})
    return matches if len(matches) == 1 else []
