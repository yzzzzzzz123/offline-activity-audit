from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from .common import AuditError, load_json, sha256_file, validate_json


CATALOG_RELATIVE_PATH = Path("references") / "product-rag.json"
SCHEMA_RELATIVE_PATH = Path("references") / "product-rag.schema.json"
PENDING_RELATIVE_PATH = Path("canban-product-multimodal-knowledge-base") / "pending-barcode.json"
PENDING_SCHEMA_RELATIVE_PATH = Path("references") / "product-rag-pending.schema.json"


def ean13_is_valid(value: str) -> bool:
    """Validate a 13-digit EAN/GTIN without accepting formatted or partial values."""

    if re.fullmatch(r"[0-9]{13}", value) is None:
        return False
    digits = [int(item) for item in value]
    weighted = sum(digits[index] * (1 if index % 2 == 0 else 3) for index in range(12))
    return (10 - weighted % 10) % 10 == digits[-1]


def _unique_text(values: list[str], label: str) -> None:
    normalized = [value.strip().casefold() for value in values]
    if len(normalized) != len(set(normalized)):
        raise AuditError(f"商品视觉RAG中的{label}存在重复")


@lru_cache(maxsize=8)
def _load_product_rag_cached(skill_root_text: str) -> dict[str, Any]:
    skill_root = Path(skill_root_text)
    catalog_path = skill_root / CATALOG_RELATIVE_PATH
    schema_path = skill_root / SCHEMA_RELATIVE_PATH
    catalog = load_json(catalog_path)
    validate_json(catalog, schema_path)

    products = list(catalog.get("products") or [])
    _unique_text([str(item["product_id"]) for item in products], "product_id")
    _unique_text([str(item["product_name"]) for item in products], "产品名称")
    _unique_text(
        [
            str(item["product_code"])
            for item in products
            if str(item["product_code"]) != "未标注"
        ],
        "已确认产品编码",
    )
    _unique_text(
        [
            str(source["source_id"])
            for item in products
            for source in item.get("sources") or []
        ],
        "source_id",
    )
    _unique_text(
        [
            str(view["view_id"])
            for item in products
            for view in item.get("views") or []
        ],
        "view_id",
    )

    for product in products:
        product_id = str(product["product_id"])
        barcode = str(product["barcode_69"])
        if not ean13_is_valid(barcode):
            raise AuditError(f"商品视觉RAG的69码未通过EAN-13校验：{product_id}={barcode}")

        product_code = str(product["product_code"])
        product_code_aliases = [
            str(value) for value in product.get("product_code_aliases") or []
        ]
        _unique_text(product_code_aliases, f"{product_id} 产品编码别名")
        if product_code.strip().casefold() in {
            value.strip().casefold() for value in product_code_aliases
        }:
            raise AuditError(f"商品视觉RAG主产品编码不能重复出现在别名中：{product_id}")

        views = list(product.get("views") or [])
        if not views and product.get("match_policy") != "candidate_only":
            raise AuditError(
                "商品视觉RAG缺少参考图时必须限制为candidate_only："
                f"{product_id}"
            )
        _unique_text([str(item["view_id"]) for item in views], f"{product_id} view_id")
        sources = list(product.get("sources") or [])
        _unique_text([str(item["source_id"]) for item in sources], f"{product_id} source_id")

        for view in views:
            relative = Path(str(view["image_file"]))
            if relative.is_absolute():
                raise AuditError(f"商品视觉RAG图片必须使用Skill内相对路径：{relative}")
            image_path = (skill_root / relative).resolve()
            if not image_path.is_relative_to(skill_root):
                raise AuditError(f"商品视觉RAG图片越出Skill目录：{relative}")
            if not image_path.is_file():
                raise AuditError(f"商品视觉RAG图片不存在：{relative}")
            expected_suffix = "" if product_code == "未标注" else f"__{product_code}"
            if expected_suffix and not image_path.parent.name.endswith(expected_suffix):
                raise AuditError(
                    "商品视觉RAG正式目录未体现已确认产品编码："
                    f"{product_id}={relative}"
                )
            actual_hash = sha256_file(image_path)
            if actual_hash != str(view["sha256"]):
                raise AuditError(
                    "商品视觉RAG图片哈希不一致："
                    f"{relative}，目录={view['sha256']}，实际={actual_hash}"
                )
    return catalog


def load_product_rag(skill_dir: str | Path) -> dict[str, Any]:
    return _load_product_rag_cached(str(Path(skill_dir).resolve()))


def clear_product_rag_cache() -> None:
    """Drop cached catalog state after an explicit maintenance update."""

    _load_product_rag_cached.cache_clear()


def load_pending_product_rag(skill_dir: str | Path) -> dict[str, Any]:
    """Validate quarantined reference images without exposing them to runtime matching."""

    skill_root = Path(skill_dir).resolve()
    manifest_path = skill_root / PENDING_RELATIVE_PATH
    if not manifest_path.exists():
        return {"schema_version": "1.0", "pending_products": []}
    manifest = load_json(manifest_path)
    validate_json(manifest, skill_root / PENDING_SCHEMA_RELATIVE_PATH)
    products = list(manifest.get("pending_products") or [])
    _unique_text([str(item["pending_id"]) for item in products], "待补69码 pending_id")
    for product in products:
        pending_id = str(product["pending_id"])
        views = list(product.get("views") or [])
        _unique_text([str(item["view_id"]) for item in views], f"{pending_id} view_id")
        for view in views:
            relative = Path(str(view["image_file"]))
            if relative.is_absolute():
                raise AuditError(f"待补69码图片必须使用Skill内相对路径：{relative}")
            image_path = (skill_root / relative).resolve()
            if not image_path.is_relative_to(skill_root):
                raise AuditError(f"待补69码图片越出Skill目录：{relative}")
            if not image_path.is_file():
                raise AuditError(f"待补69码图片不存在：{relative}")
            actual_hash = sha256_file(image_path)
            if actual_hash != str(view["sha256"]):
                raise AuditError(
                    "待补69码图片哈希不一致："
                    f"{relative}，清单={view['sha256']}，实际={actual_hash}"
                )
    return manifest


def product_by_id(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        str(product["product_id"]): product
        for product in catalog.get("products") or []
    }


def product_reference_images(
    skill_dir: str | Path,
    catalog: dict[str, Any],
) -> list[tuple[dict[str, Any], dict[str, Any], Path]]:
    root = Path(skill_dir).resolve()
    return [
        (product, view, (root / str(view["image_file"])).resolve())
        for product in catalog.get("products") or []
        for view in product.get("views") or []
    ]


def resolve_product_reference_hits(
    raw_hits: list[dict[str, Any]],
    catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    """Replace model-returned IDs with immutable catalog identity fields."""

    products = product_by_id(catalog)
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
                "product_code_aliases": [
                    str(value) for value in product.get("product_code_aliases") or []
                ],
                "barcode_69": str(product["barcode_69"]),
                "specification": str(product["specification"]),
                "variant": product.get("variant"),
                "confidence": confidence,
                "matched_view_ids": matched_views,
                "visible_basis": [str(value) for value in raw.get("visible_basis") or []],
                "limitations": [str(value) for value in raw.get("limitations") or []],
            }
        )
    return resolved


def product_reference_label(hit: dict[str, Any]) -> str:
    confidence = "精确命中" if hit["confidence"] == "exact" else "候选命中"
    return (
        f"{hit['product_name']}（产品编码：{hit['product_code']}；"
        f"69码：{hit['barcode_69']}；视觉RAG：{confidence}）"
    )
