from __future__ import annotations

import re
import unicodedata
from functools import lru_cache
from pathlib import Path
from typing import Any

from .common import AuditError, load_json, sha256_file, validate_json


CATALOG_RELATIVE_PATH = Path("references") / "product-rag.json"
SCHEMA_RELATIVE_PATH = Path("references") / "product-rag.schema.json"
PENDING_RELATIVE_PATH = Path("canban-product-multimodal-knowledge-base") / "pending-barcode.json"
PENDING_SCHEMA_RELATIVE_PATH = Path("references") / "product-rag-pending.schema.json"
FIELD_SHORT_CODE_PATTERN = re.compile(
    r"(?<![0-9A-Za-z])([A-Za-z]{1,4})[\s\-－_]*([0-9]{1,4})(?![0-9A-Za-z])"
)


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
    """Return the authoritative name and catalog-controlled aliases for matching."""

    values = [str(product.get("product_name") or "")]
    values.extend(str(value) for value in product.get("aliases") or [])
    for source in product.get("sources") or []:
        observed_name = str(source.get("observed_product_name") or "").strip()
        observed_specification = str(source.get("observed_specification") or "").strip()
        observed_variant = str(source.get("observed_variant") or "").strip()
        values.extend(
            [
                observed_name,
                f"{observed_name}{observed_variant}{observed_specification}",
                f"{observed_name}{observed_specification}{observed_variant}",
            ]
        )
    return list(dict.fromkeys(value for value in values if value))


def _product_name_grams(value: Any) -> set[str]:
    text = canonical_product_name(value)
    for token in ("参半", "oralshark", "牙膏", "组合装", "超值装", "特享装", "量贩装"):
        text = text.replace(token, "")
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


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
        left_grams = _product_name_grams(value)
        right_grams = _product_name_grams(candidate)
        if not left_grams or not right_grams:
            score = 0.0
        else:
            score = len(left_grams & right_grams) / len(left_grams | right_grams)
        scores.append(score)
    return max(scores, default=0.0)


def unique_fuzzy_catalog_product(
    value: Any,
    products: list[dict[str, Any]],
    *,
    minimum_score: float,
    minimum_margin: float = 0.08,
) -> tuple[dict[str, Any] | None, float]:
    """Select one catalog product only when fuzzy name evidence is strong and unique."""

    ranked = sorted(
        ((catalog_product_name_score(value, product), product) for product in products),
        key=lambda item: (-item[0], str(item[1].get("product_id") or "")),
    )
    if not ranked or ranked[0][0] < minimum_score:
        return None, ranked[0][0] if ranked else 0.0
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < minimum_margin:
        return None, ranked[0][0]
    return ranked[0][1], ranked[0][0]


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


def apply_visible_short_code_exact_hits(
    resolved_hits: list[dict[str, Any]],
    visible_text: list[str],
    catalog: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply the field-photo rule for a visible, catalog-unique short product code.

    Short packaging codes such as ``SP-1`` are deliberately tolerant of spaces,
    case, and hyphen style.  A code can establish an exact field identity only
    when it is a registered catalog alias for exactly one product.  Shared codes
    such as the current ``SP-4`` group remain fuzzy until another visible package
    feature disambiguates them.
    """

    visible_codes = {
        f"{match.group(1).upper()}{match.group(2)}"
        for value in visible_text
        for match in FIELD_SHORT_CODE_PATTERN.finditer(str(value))
    }
    if not visible_codes:
        return resolved_hits

    alias_index: dict[str, dict[str, tuple[dict[str, Any], str]]] = {}
    for product in catalog.get("products") or []:
        for alias in product.get("product_code_aliases") or []:
            match = FIELD_SHORT_CODE_PATTERN.fullmatch(str(alias).strip())
            if match is None:
                continue
            canonical = f"{match.group(1).upper()}{match.group(2)}"
            alias_index.setdefault(canonical, {})[str(product["product_id"])] = (
                product,
                str(alias),
            )

    result = [
        {
            **hit,
            "matched_view_ids": list(hit.get("matched_view_ids") or []),
            "visible_basis": list(hit.get("visible_basis") or []),
            "limitations": list(hit.get("limitations") or []),
        }
        for hit in resolved_hits
    ]
    by_id = {str(hit["reference_product_id"]): hit for hit in result}
    for canonical in sorted(visible_codes):
        matches = alias_index.get(canonical) or {}
        if len(matches) != 1:
            continue
        product, display_alias = next(iter(matches.values()))
        if product.get("match_policy") == "candidate_only":
            continue
        product_id = str(product["product_id"])
        basis = (
            f"现场照片可见知识库登记短码 {display_alias}，"
            "且该短码只对应这一种商品"
        )
        hit = by_id.get(product_id)
        if hit is None:
            hit = {
                "reference_product_id": product_id,
                "product_name": str(product["product_name"]),
                "product_code": str(product["product_code"]),
                "product_code_aliases": [
                    str(value) for value in product.get("product_code_aliases") or []
                ],
                "barcode_69": str(product["barcode_69"]),
                "specification": str(product["specification"]),
                "variant": product.get("variant"),
                "confidence": "exact",
                "matched_view_ids": [],
                "visible_basis": [basis],
                "limitations": [],
            }
            result.append(hit)
            by_id[product_id] = hit
            continue
        hit["confidence"] = "exact"
        if basis not in hit["visible_basis"]:
            hit["visible_basis"].append(basis)
    return result


def product_reference_label(hit: dict[str, Any]) -> str:
    confidence = "精确匹配（高置信度）" if hit["confidence"] == "exact" else "模糊匹配（中置信度）"
    return (
        f"{hit['product_name']}（产品编码：{hit['product_code']}；"
        f"69码：{hit['barcode_69']}；商品知识库：{confidence}）"
    )
