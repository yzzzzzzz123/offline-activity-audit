from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher
from typing import Any

from .common import AuditError


FIELD_SHORT_CODE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])([A-Za-z]{1,4})[\s\-－_]*([0-9]{1,4})(?![0-9A-Za-z])"
)
PRODUCT_EXISTENCE_NAME_THRESHOLD = 0.5


def ean13_is_valid(value: str) -> bool:
    """Validate a 13-digit EAN/GTIN without accepting formatted or partial values."""

    if re.fullmatch(r"[0-9]{13}", value) is None:
        return False
    digits = [int(item) for item in value]
    weighted = sum(digits[index] * (1 if index % 2 == 0 else 3) for index in range(12))
    return (10 - weighted % 10) % 10 == digits[-1]


def canonical_product_name(value: Any) -> str:
    """Normalize a source/catalog product name without weakening identifiers."""

    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("參半", "参半")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff+%]+", "", text)


def catalog_product_name_values(product: dict[str, Any]) -> list[str]:
    """名称比较只使用数据库商品名称；规格/款式可从该名称解析。"""
    name = str(product.get("product_name") or "").strip()
    return [name] if name else []


def _fuzzy_product_name_text(value: Any) -> str:
    text = canonical_product_name(value)
    for token in (
        "参半",
        "oralshark",
        "牙膏",
        "套盒",
        "组合装",
        "超值装",
        "特享装",
        "量贩装",
    ):
        text = text.replace(token, "")
    return re.sub(r"\d+(?:g|ml)", "", text)


def _product_name_grams(value: Any) -> set[str]:
    text = _fuzzy_product_name_text(value)
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def product_name_similarity(left: Any, right: Any) -> float:
    """Return OCR-tolerant fuzzy compatibility for product names.

    Exact equality is only a score-1 special case, never a prerequisite.  The
    strict identity boundary remains the product code and 69 code wherever
    those comparable identifiers are supplied.
    """

    left_text = _fuzzy_product_name_text(left)
    right_text = _fuzzy_product_name_text(right)
    if not left_text or not right_text:
        return 0.0
    if left_text in right_text or right_text in left_text:
        return 1.0
    left_grams = _product_name_grams(left_text)
    right_grams = _product_name_grams(right_text)
    gram_score = (
        len(left_grams & right_grams) / len(left_grams | right_grams)
        if left_grams and right_grams
        else 0.0
    )
    sequence_score = SequenceMatcher(None, left_text, right_text).ratio()
    return max(gram_score, sequence_score)


def _measurement_tokens(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return {
        f"{number}{unit}"
        for number, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*(ml|毫升|g|克|支|条|片)",
            text,
        )
    }


def catalog_product_name_score(value: Any, product: dict[str, Any]) -> float:
    """Score one source name against the names controlled by one catalog product."""

    source_measurements = _measurement_tokens(value)
    scores: list[float] = []
    for candidate in catalog_product_name_values(product):
        candidate_measurements = _measurement_tokens(candidate)
        if (
            source_measurements
            and candidate_measurements
            and not source_measurements.intersection(candidate_measurements)
        ):
            continue
        scores.append(product_name_similarity(value, candidate))
    return max(scores, default=0.0)


def _catalog_full_identity_name_score(value: Any, product: dict[str, Any]) -> float:
    """保持完整数据库名称的模糊相容度，不借用历史别名或观察文本。"""
    return product_name_similarity(value, product.get("product_name") or "")


def _rank_fuzzy_catalog_products(
    value: Any,
    products: list[dict[str, Any]],
    *,
    minimum_margin: float,
) -> list[tuple[float, float, dict[str, Any]]]:
    scored = [
        (
            catalog_product_name_score(value, product),
            _catalog_full_identity_name_score(value, product),
            product,
        )
        for product in products
    ]
    if not scored:
        return []
    best_primary = max(item[0] for item in scored)
    contenders = [
        item for item in scored if best_primary - item[0] < minimum_margin
    ]
    return sorted(
        contenders,
        key=lambda item: (
            -item[1],
            -item[0],
            str(item[2].get("product_id") or ""),
        ),
    )


def best_fuzzy_catalog_product(
    value: Any,
    products: list[dict[str, Any]],
    *,
    threshold: float = PRODUCT_EXISTENCE_NAME_THRESHOLD,
) -> tuple[dict[str, Any] | None, float]:
    """Select one uniquely fuzzy-compatible name inside the strict ID set."""

    ranked = _rank_fuzzy_catalog_products(value, products, minimum_margin=0.08)
    if not ranked or ranked[0][0] <= threshold:
        return None, ranked[0][0] if ranked else 0.0
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < 0.08:
        return None, ranked[0][0]
    return ranked[0][2], ranked[0][0]


def unique_fuzzy_catalog_product(
    value: Any,
    products: list[dict[str, Any]],
    *,
    minimum_score: float,
    minimum_margin: float = 0.08,
) -> tuple[dict[str, Any] | None, float]:
    """Select one catalog product only when fuzzy name evidence is strong and unique."""

    ranked = _rank_fuzzy_catalog_products(
        value,
        products,
        minimum_margin=minimum_margin,
    )
    if not ranked or ranked[0][0] < minimum_score:
        return None, ranked[0][0] if ranked else 0.0
    if len(ranked) > 1 and ranked[0][1] - ranked[1][1] < minimum_margin:
        return None, ranked[0][0]
    return ranked[0][2], ranked[0][0]


def load_product_rag() -> dict[str, Any]:
    """兼容旧函数名称；内容始终来自本次数据库查询。"""
    from .product_database import load_product_catalog
    return load_product_catalog()


def product_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(product["product_id"]): product
        for product in catalog.get("products") or []
    }


def resolve_product_reference_hits(
    raw_hits: list[dict[str, Any]],
    catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replace model-returned IDs with immutable catalog identity fields."""

    products = product_by_id(catalog)
    if raw_hits and (catalog.get("data_source") or {}).get("type") == "mysql":
        from .product_images import attach_product_reference_images
        requested_ids = {str(hit.get("reference_product_id") or "") for hit in raw_hits}
        selected = {**catalog, "products": [p for p in products.values() if p["product_id"] in requested_ids]}
        products = product_by_id(attach_product_reference_images(selected))
    seen: set[str] = set()
    resolved: list[dict[str, Any]] = []
    for raw in raw_hits:
        product_id = str(raw.get("reference_product_id") or "")
        if product_id in seen:
            raise AuditError(f"现场照片重复返回商品视觉RAG命中：{product_id}")
        seen.add(product_id)
        product = products.get(product_id)
        if product is None:
            raise AuditError(f"现场照片返回了商品视觉RAG中不存在的产品ID：{product_id}")
        allowed_views = {str(item["view_id"]) for item in product.get("views") or []}
        if not allowed_views:
            raise AuditError(
                f"商品视觉RAG条目缺少独立现场参考图，不能作为视觉命中：{product_id}"
            )
        matched_views = [str(value) for value in raw.get("matched_view_ids") or []]
        if len(matched_views) != len(set(matched_views)):
            raise AuditError(f"商品视觉RAG命中 {product_id} 重复引用同一视图")
        unknown_views = sorted(set(matched_views) - allowed_views)
        if unknown_views:
            raise AuditError(
                f"商品视觉RAG命中 {product_id} 引用了不存在的视图："
                + "、".join(unknown_views)
            )
        confidence = str(raw.get("confidence") or "")
        if confidence not in {"exact", "candidate"}:
            raise AuditError(f"商品视觉RAG命中置信度无效：{product_id}={confidence}")
        if confidence == "exact" and product.get("match_policy") == "candidate_only":
            raise AuditError(f"商品视觉RAG中的冲突商品只允许候选命中：{product_id}")
        resolved.append(
            {
                "reference_product_id": product_id,
                "product_name": str(product["product_name"]),
                "product_code": str(product["product_code"]),
                "product_code_aliases": [],
                "barcode_69": str(product["barcode_69"]),
                "specification": "",
                "variant": None,
                "confidence": confidence,
                "matched_view_ids": matched_views,
                "visible_basis": [str(value) for value in raw.get("visible_basis") or []],
                "limitations": [str(value) for value in raw.get("limitations") or []],
            }
        )
    return resolved


def apply_visible_catalog_text_exact_hits(
    resolved_hits: list[dict[str, Any]],
    visible_text: list[str],
    catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    """Promote one visually supported hit when visible catalog text is unique.

    This covers general combinations such as bundle notation, specification,
    and package wording.  Text alone never creates a hit: the selected product
    must already have a model-returned match to at least one registered
    reference view, and the same visible text must uniquely select it from the
    complete validated catalog.
    """

    visible = " ".join(str(value).strip() for value in visible_text if str(value).strip())
    if not visible or not resolved_hits:
        return resolved_hits
    selected, score = best_fuzzy_catalog_product(
        visible,
        list(catalog.get("products") or []),
        threshold=PRODUCT_EXISTENCE_NAME_THRESHOLD,
    )
    if selected is None:
        return resolved_hits

    result = [
        {
            **hit,
            "matched_view_ids": list(hit.get("matched_view_ids") or []),
            "visible_basis": list(hit.get("visible_basis") or []),
            "limitations": list(hit.get("limitations") or []),
        }
        for hit in resolved_hits
    ]
    selected_id = str(selected["product_id"])
    matching_hits = [
        hit
        for hit in result
        if str(hit.get("reference_product_id") or "") == selected_id
        and hit.get("matched_view_ids")
    ]
    if len(matching_hits) != 1 or selected.get("match_policy") == "candidate_only":
        return result
    hit = matching_hits[0]
    hit["confidence"] = "exact"
    basis = (
        "现场照片可见名称片段、规格、款式或组合装文字已从完整知识库唯一收敛到"
        f"{selected['product_name']}，且包装与登记参考图相容"
    )
    if basis not in hit["visible_basis"]:
        hit["visible_basis"].append(basis)
    return result


def product_reference_label(hit: dict[str, Any]) -> str:
    confidence = "精确匹配（高置信度）" if hit["confidence"] == "exact" else "模糊匹配（中置信度）"
    return (
        f"{hit['product_name']}（产品编码：{hit['product_code']}；"
        f"69码：{hit['barcode_69']}；商品知识库：{confidence}）"
    )
