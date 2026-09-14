"""堆头视觉请求的有界切分与无损合并，不读取文件或作业务判断。"""

from __future__ import annotations

from collections.abc import Sequence
from copy import deepcopy
from pathlib import Path
from typing import Any, TypeVar

from .common import AuditError


T = TypeVar("T")
ImageSource = str | Path


def _positive_integer(value: Any, label: str) -> int:
    if type(value) is not int or value < 1:
        raise AuditError(f"{label}必须为正整数")
    return value


def chunk_sequence(items: Sequence[T], max_items: int) -> list[list[T]]:
    """按原顺序分组；不会丢弃尾部成员，也不会修改输入。"""
    _positive_integer(max_items, "每块最大成员数")
    return [list(items[start : start + max_items]) for start in range(0, len(items), max_items)]


def _attachment_keys(records: Sequence[dict[str, Any]]) -> list[tuple[int, int]]:
    keys: list[tuple[int, int]] = []
    previous_line = 0
    previous_page = 0
    for record in records:
        if not isinstance(record, dict):
            raise AuditError("合同附件记录必须为对象")
        page = _positive_integer(record.get("source_page"), "合同附件来源页码")
        line = _positive_integer(record.get("line_no"), "合同附件全局行号")
        if line <= previous_line or page < previous_page:
            raise AuditError("合同附件必须保持递增全局行号和原始页顺序，不能重复或重排")
        keys.append((page, line))
        previous_page, previous_line = page, line
    return keys


def chunk_attachment_records(
    records: Sequence[dict[str, Any]], max_rows: int = 8
) -> list[list[dict[str, Any]]]:
    """每块只含同一 PDF 页的有界行组，原页码和全局行号保持不变。"""
    _positive_integer(max_rows, "每块最大附件行数")
    _attachment_keys(records)
    chunks: list[list[dict[str, Any]]] = []
    for record in records:
        if (
            not chunks
            or len(chunks[-1]) >= max_rows
            or chunks[-1][-1]["source_page"] != record["source_page"]
        ):
            chunks.append([])
        chunks[-1].append(deepcopy(record))
    return chunks


def _expected_photo_names(expected_images: Sequence[ImageSource]) -> list[str]:
    names: list[str] = []
    normalized: set[str] = set()
    for source in expected_images:
        if not isinstance(source, (str, Path)):
            raise AuditError("预期照片来源必须为路径或文件名")
        name = str(source).replace("\\", "/").rsplit("/", 1)[-1]
        if not name or name in {".", ".."}:
            raise AuditError("预期照片必须具有有效文件名")
        if name.casefold() in normalized:
            raise AuditError(f"预期照片文件名重复或大小写冲突：{name}")
        names.append(name)
        normalized.add(name.casefold())
    return names


def _notes(results: Sequence[dict[str, Any]]) -> list[str]:
    notes: list[str] = []
    seen: set[str] = set()
    for result in results:
        if not isinstance(result, dict):
            raise AuditError("分块结果必须为对象")
        values = result.get("extraction_notes", [])
        if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
            raise AuditError("分块提取说明必须为字符串数组")
        for value in values:
            if value not in seen:
                notes.append(value)
                seen.add(value)
    return notes


def _rows(result: dict[str, Any], result_key: str) -> list[dict[str, Any]]:
    if not isinstance(result, dict):
        raise AuditError("分块结果必须为对象")
    rows = result.get(result_key)
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise AuditError(f"分块结果 {result_key} 必须为对象数组")
    return rows


def _check_envelope(result: dict[str, Any], *, version: str, scenario: str | None = None) -> None:
    if result.get("schema_version") != version:
        raise AuditError(f"分块证据版本不一致，要求 {version}")
    if scenario is not None and result.get("scenario") != scenario:
        raise AuditError(f"分块证据场景不一致，要求 {scenario}")


def _merge_photo_rows(
    expected_images: Sequence[ImageSource],
    results: Sequence[dict[str, Any]],
    result_key: str,
) -> list[dict[str, Any]]:
    expected = _expected_photo_names(expected_images)
    expected_set = set(expected)
    merged: dict[str, dict[str, Any]] = {}
    for result in results:
        for row in _rows(result, result_key):
            photo_file = row.get("photo_file")
            if not isinstance(photo_file, str) or photo_file not in expected_set:
                raise AuditError(f"分块结果包含未知照片：{photo_file}")
            if photo_file in merged:
                raise AuditError(f"分块结果重复返回照片：{photo_file}")
            merged[photo_file] = deepcopy(row)
    missing = [name for name in expected if name not in merged]
    if missing:
        raise AuditError("分块结果缺少照片：" + "、".join(missing))
    return [merged[name] for name in expected]


def merge_product_queries(
    expected_images: Sequence[ImageSource], results: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """按全部输入照片合并文字预检，拒绝漏图、重复图和非提交来源。"""
    notes = _notes(results)
    for result in results:
        _check_envelope(result, version="1.0")
    return {
        "schema_version": "1.0",
        "photo_queries": _merge_photo_rows(expected_images, results, "photo_queries"),
        "extraction_notes": notes,
    }


def merge_photo_assignments(
    expected_images: Sequence[ImageSource], results: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """逐图覆盖的可见地点路由；未知门店及地点相容性由调用方验证。"""
    notes = _notes(results)
    rows = _merge_photo_rows(expected_images, results, "photo_assignments")
    required = {"photo_file", "store_line_no", "visible_location", "location_basis"}
    for row in rows:
        if set(row) != required:
            raise AuditError("照片路由必须且只能包含照片、门店行号、可见地点和地点依据")
        if row["store_line_no"] is not None:
            _positive_integer(row["store_line_no"], "照片路由门店行号")
        if row["visible_location"] is not None and not isinstance(row["visible_location"], str):
            raise AuditError("照片路由可见地点必须为文字或 null")
        if not isinstance(row["location_basis"], str) or not row["location_basis"].strip():
            raise AuditError("照片路由必须保留非空地点依据")
    return {"photo_assignments": rows, "extraction_notes": notes}


def _route_key(review: dict[str, Any]) -> tuple[int, str, tuple[str, ...]]:
    if not isinstance(review, dict):
        raise AuditError("门店照片路由必须为对象")
    line = _positive_integer(review.get("store_line_no"), "照片路由门店行号")
    name = review.get("contract_store_name")
    if not isinstance(name, str) or not name.strip():
        raise AuditError("照片路由必须保留非空合同门店名称")
    photos = review.get("photo_files")
    if not isinstance(photos, list) or any(not isinstance(photo, str) or not photo for photo in photos):
        raise AuditError("照片路由必须保留有序照片文件名数组")
    if any("/" in photo or "\\" in photo or photo in {".", ".."} for photo in photos):
        raise AuditError("照片路由只能使用来源文件名，不能使用路径")
    if len({photo.casefold() for photo in photos}) != len(photos):
        raise AuditError("同一门店照片路由包含重复文件名")
    return line, name, tuple(photos)


def _expected_routes(
    reviews: Sequence[dict[str, Any]],
) -> dict[int, tuple[int, str, tuple[str, ...]]]:
    routes: dict[int, tuple[int, str, tuple[str, ...]]] = {}
    for review in reviews:
        key = _route_key(review)
        if key[0] in routes:
            raise AuditError(f"预期照片路由重复包含合同门店行：{key[0]}")
        routes[key[0]] = key
    return routes


def chunk_routed_reviews(
    photo_reviews: Sequence[dict[str, Any]], max_stores: int = 1
) -> list[list[dict[str, Any]]]:
    """按门店分块，单店的完整照片组始终作为一个不可切开的成员。"""
    _expected_routes(photo_reviews)
    return chunk_sequence(deepcopy(list(photo_reviews)), max_stores)


def merge_routed_results(
    expected_reviews: Sequence[dict[str, Any]],
    results: Sequence[dict[str, Any]],
    result_key: str,
) -> dict[str, Any]:
    """只拼接独立门店结果，不合成日期、地点或跨照片陈列计数。"""
    if result_key not in {"photo_reviews", "display_reviews"}:
        raise AuditError("只允许合并 photo_reviews 或 display_reviews")
    expected = _expected_routes(expected_reviews)
    notes = _notes(results)
    version = "2.5" if result_key == "photo_reviews" else "1.0"
    scenario = "promotional_display" if result_key == "photo_reviews" else None
    merged: dict[int, dict[str, Any]] = {}
    for result in results:
        _check_envelope(result, version=version, scenario=scenario)
        for row in _rows(result, result_key):
            key = _route_key(row)
            line = key[0]
            if line not in expected:
                raise AuditError(f"分块结果包含未知合同门店行：{line}")
            if line in merged:
                raise AuditError(f"分块结果重复返回合同门店行：{line}")
            if key != expected[line]:
                raise AuditError(f"分块结果改变了合同第 {line} 行门店名称或有序照片路由")
            merged[line] = deepcopy(row)
    missing = [line for line in expected if line not in merged]
    if missing:
        raise AuditError(f"分块结果缺少合同门店行：{missing}")
    combined: dict[str, Any] = {
        "schema_version": version,
        result_key: [merged[line] for line in expected],
        "extraction_notes": notes,
    }
    if scenario is not None:
        combined["scenario"] = scenario
    return combined


def merge_contract_product_cells(
    expected_records: Sequence[dict[str, Any]], results: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """按原 PDF 页和全局行号合并单元格复读，不填补或修改任何读数。"""
    expected = _attachment_keys(expected_records)
    expected_set = set(expected)
    notes = _notes(results)
    merged: dict[tuple[int, int], dict[str, Any]] = {}
    for result in results:
        for row in _rows(result, "records"):
            key = (
                _positive_integer(row.get("source_page"), "合同附件来源页码"),
                _positive_integer(row.get("line_no"), "合同附件全局行号"),
            )
            if key not in expected_set:
                raise AuditError(f"分块结果包含未知合同附件页/行：{key}")
            if key in merged:
                raise AuditError(f"分块结果重复返回合同附件页/行：{key}")
            merged[key] = deepcopy(row)
    missing = [key for key in expected if key not in merged]
    if missing:
        raise AuditError(f"分块结果缺少合同附件页/行：{missing}")
    return {"records": [merged[key] for key in expected], "extraction_notes": notes}
