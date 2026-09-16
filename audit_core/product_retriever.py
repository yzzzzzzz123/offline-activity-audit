"""LangChain retriever over a run's authoritative product database snapshot."""
from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field

from .common import AuditError
from .product_rag import ean13_is_valid
from .workflow import invoke_local

MAX_PRODUCT_REFERENCE_CANDIDATES = 8
MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO = 4


def _normalize_product_lookup_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).casefold())


def _product_lookup_support_tokens(values: list[Any]) -> tuple[set[str], set[str], set[str]]:
    """Extract bundle, measurement, and packaging-phrase anchors for candidate retrieval."""

    raw_values = [str(value).casefold() for value in values if value]
    bundles = {
        re.sub(r"\s+", "", match.group(0))
        for value in raw_values
        for match in re.finditer(r"\d+\s*\+\s*\d+", value)
    }
    measurements = {
        f"{number}{unit}"
        for value in raw_values
        for number, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*(ml|毫升|g|克|支|条|片)",
            value,
        )
    }
    phrases: set[str] = set()
    for value in raw_values:
        for sequence in re.findall(r"[\u4e00-\u9fff]{3,}", value):
            phrases.update(
                sequence[index : index + 3]
                for index in range(len(sequence) - 2)
            )
    return bundles, measurements, phrases


def _product_candidate_score(product: dict[str, Any], query: dict[str, Any]) -> int:
    # 候选来自数据库身份文字；图片清单在选出候选之后才按需读取。
    # 此处不能以尚未加载的 views 判空，否则数据库商品会全部被提前排除。
    barcode = str(product["barcode_69"])
    visible_barcodes = {
        str(value)
        for value in query.get("visible_barcodes_69") or []
        if ean13_is_valid(str(value))
    }
    visible_names = [
        _normalize_product_lookup_text(value)
        for value in query.get("visible_product_names") or []
    ]
    visible_codes = {
        _normalize_product_lookup_text(value)
        for value in query.get("visible_product_codes") or []
    }
    raw_query_text = [
        *(query.get("visible_product_names") or []),
        *(query.get("visible_product_codes") or []),
        *(query.get("visible_text") or []),
        *(query.get("packaging_terms") or []),
    ]
    visible_text = _normalize_product_lookup_text(
        " ".join(str(value) for value in raw_query_text)
    )

    barcode_matched = barcode in visible_barcodes or barcode in visible_text
    score = 1000 if barcode_matched else 0

    product_codes = {
        _normalize_product_lookup_text(value)
        for value in (
            product.get("product_code"),
        )
        if value and _normalize_product_lookup_text(value) != _normalize_product_lookup_text("未标注")
    }
    code_matched = any(
        code in visible_codes or code in visible_text for code in product_codes
    )
    if code_matched:
        score = max(score, 700)
        if barcode_matched:
            score += 700

    catalog_text_anchors = [_normalize_product_lookup_text(product.get("product_name") or "")]
    raw_catalog_text = [product.get("product_name")]
    query_name_candidates = [*visible_names]
    if visible_text:
        query_name_candidates.append(visible_text)
    name_match_score = 0
    for catalog_name in catalog_text_anchors:
        for visible_name in query_name_candidates:
            if visible_name in {"参半", "牙膏", "参半牙膏", "商品", "参半产品", "口腔护理"}:
                continue
            if min(len(catalog_name), len(visible_name)) < 4:
                continue
            if catalog_name in visible_name or visible_name in catalog_name:
                name_match_score = max(
                    name_match_score,
                    400 + min(len(catalog_name), len(visible_name)),
                )

    if name_match_score:
        if score:
            score += name_match_score
        else:
            score = name_match_score

    query_bundles, query_measurements, query_phrases = _product_lookup_support_tokens(
        raw_query_text
    )
    catalog_bundles, catalog_measurements, catalog_phrases = (
        _product_lookup_support_tokens(raw_catalog_text)
    )
    bundle_overlap = query_bundles & catalog_bundles
    measurement_overlap = query_measurements & catalog_measurements
    phrase_overlap = query_phrases & catalog_phrases
    supporting_route = (
        bundle_overlap and (measurement_overlap or phrase_overlap)
    ) or (
        measurement_overlap and phrase_overlap
    )
    if supporting_route:
        support_score = (
            300
            + 90 * len(bundle_overlap)
            + 70 * len(measurement_overlap)
            + min(80, 10 * len(phrase_overlap))
        )
        score = max(score, support_score)

    if score == 0:
        return 0

    return score


class CatalogProductRetriever(BaseRetriever):
    """Rank only visible photo anchors; reference images are loaded after retrieval.

    Round-robin selection keeps photos from crowding each other out. Exact
    identifiers and the existing bounded text ranking retain their semantics;
    no remote embeddings or cross-run index is introduced.
    """

    catalog: dict[str, Any] = Field(exclude=True, repr=False)
    max_products: int = Field(default=MAX_PRODUCT_REFERENCE_CANDIDATES, ge=1)
    per_photo: int = Field(default=MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO, ge=1)

    def _get_relevant_documents(self, query: str, *, run_manager) -> list[Document]:
        try:
            value = json.loads(query)
        except (ValueError, TypeError):
            raise AuditError("商品检索需要当前照片的结构化可见锚点") from None
        if not isinstance(value, dict) or not isinstance(value.get("photo_queries", []), list):
            raise AuditError("商品检索的照片查询格式无效")
        products = list(self.catalog.get("products") or [])
        by_id = {str(p["product_id"]): p for p in products}
        ranked_per_photo = []
        for photo in value.get("photo_queries") or []:
            if not isinstance(photo, dict):
                raise AuditError("商品检索的照片锚点必须是对象")
            ranked = sorted(
                ((score, str(p["product_id"])) for p in products
                 if (score := _product_candidate_score(p, photo)) > 0),
                key=lambda item: (-item[0], item[1]),
            )
            ranked_per_photo.append(ranked[:self.per_photo])
        selected, seen = [], set()
        for rank in range(self.per_photo):
            for ranked in ranked_per_photo:
                if rank >= len(ranked) or ranked[rank][1] in seen:
                    continue
                score, product_id = ranked[rank]
                seen.add(product_id)
                product = by_id[product_id]
                selected.append(Document(
                    id=product_id, page_content=product["product_name"],
                    metadata={"product_id": product_id, "product_code": product.get("product_code"),
                              "barcode_69": product["barcode_69"], "retrieval_score": score},
                ))
                if len(selected) >= self.max_products:
                    return selected
        return selected


def select_product_candidates(catalog: dict[str, Any], query_result: dict[str, Any], *,
                              max_products: int = MAX_PRODUCT_REFERENCE_CANDIDATES) -> dict[str, Any]:
    retriever = CatalogProductRetriever(catalog=catalog, max_products=max_products)
    documents = invoke_local(retriever, json.dumps(query_result, ensure_ascii=False))
    by_id = {str(product["product_id"]): product for product in catalog.get("products") or []}
    return {**catalog, "products": [by_id[document.id] for document in documents]}
