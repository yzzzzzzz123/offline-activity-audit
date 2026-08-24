from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from .common import AuditError, validate_json
from .product_rag import (
    ean13_is_valid,
    load_product_rag,
    product_reference_images,
    resolve_product_reference_hits,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SKILL_BY_SCENARIO = {
    "personnel_incentive": PROJECT_ROOT / "skills" / "audit-personnel-incentive",
    "promotional_display": PROJECT_ROOT / "skills" / "audit-promotional-display",
}
DEFAULT_MAX_ATTEMPTS = 3
MAX_PRODUCT_REFERENCE_CANDIDATES = 8
MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO = 2
MAX_PRODUCT_REFERENCE_VIEWS = 4
DEFAULT_MODEL = "gpt-5.6-sol"
DEFAULT_ATTEMPT_TIMEOUT_SECONDS = 1200
DEFAULT_REASONING_EFFORT = "high"
PRODUCT_QUERY_REASONING_EFFORT = "medium"
ALLOWED_REASONING_EFFORTS = frozenset({"low", "medium", "high", "xhigh"})


class CodexExtractionError(AuditError):
    """Raised when visual extraction fails after all attempts."""


class CodexRequestConfigurationError(CodexExtractionError):
    """Raised when retrying the same Codex request cannot repair its configuration."""


NON_RETRYABLE_CODEX_ERROR_MARKERS = (
    "invalid_json_schema",
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    "model_not_found",
)


def _is_non_retryable_codex_error(detail: str) -> bool:
    normalized = detail.casefold()
    return any(marker in normalized for marker in NON_RETRYABLE_CODEX_ERROR_MARKERS)


def _find_codex() -> str:
    override = os.environ.get("OFFLINE_AUDIT_CODEX", "").strip()
    if override:
        candidate = Path(override).expanduser().resolve()
        if not candidate.is_file():
            raise AuditError(f"OFFLINE_AUDIT_CODEX 指向的文件不存在：{candidate}")
        return str(candidate)

    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if os.name == "nt" and local_app_data:
        bundled_root = Path(local_app_data) / "OpenAI" / "Codex" / "bin"
        bundled = sorted(
            bundled_root.glob("*/codex.exe"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        if bundled:
            return str(bundled[0].resolve())

    executable = shutil.which("codex")
    if not executable:
        raise AuditError("未找到 codex 命令，无法执行视觉证据提取")
    return executable


def _write_bundled_model_catalog(
    codex: str,
    destination: Path,
    selected_model: str,
) -> None:
    completed = subprocess.run(
        [codex, "debug", "models", "--bundled"],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()[-600:]
        raise AuditError(f"无法读取 Codex 内置模型目录：{detail}")
    try:
        catalog = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise AuditError(f"Codex 内置模型目录不是有效 JSON：{exc}") from exc
    models = catalog.get("models") if isinstance(catalog, dict) else None
    slugs = {
        str(item.get("slug"))
        for item in models or []
        if isinstance(item, dict) and item.get("slug")
    }
    if selected_model not in slugs:
        raise AuditError(
            f"模型 {selected_model} 不在当前 Codex CLI 的内置模型目录中"
        )
    destination.write_text(completed.stdout, encoding="utf-8")


def _strip_json_fence(text: str) -> str:
    value = text.strip()
    if value.startswith("```"):
        lines = value.splitlines()[1:]
        if lines and lines[-1].strip() == "```":
            lines.pop()
        value = "\n".join(lines).strip()
    return value


def _copy_images(sources: list[Path], destination: Path) -> list[Path]:
    copied = [destination / source.name for source in sources]
    for source, target in zip(sources, copied, strict=True):
        shutil.copy2(source, target)
    return copied


def _copy_product_reference_images(
    skill_dir: Path,
    destination: Path,
    catalog: dict[str, Any],
    *,
    max_views_per_product: int = MAX_PRODUCT_REFERENCE_VIEWS,
) -> list[dict[str, Any]]:
    references = product_reference_images(skill_dir, catalog)
    copied: list[dict[str, Any]] = []
    strength_order = {"strong": 0, "supporting": 1, "unreviewed": 2, "weak": 3}
    for product in catalog.get("products") or []:
        product_id = str(product["product_id"])
        available = [
            (view, source)
            for item_product, view, source in references
            if str(item_product["product_id"]) == product_id
        ]
        available.sort(
            key=lambda item: (
                strength_order.get(str(item[0].get("identity_strength")), 9),
                str(item[0].get("view_id")),
            )
        )
        selected: list[tuple[dict[str, Any], Path]] = []
        selected_ids: set[str] = set()
        seen_sources: set[str] = set()
        for view, source in available:
            source_id = str(view.get("source_id") or view.get("view_id"))
            if source_id in seen_sources:
                continue
            selected.append((view, source))
            selected_ids.add(str(view["view_id"]))
            seen_sources.add(source_id)
            if len(selected) >= max_views_per_product:
                break
        if len(selected) < max_views_per_product:
            for view, source in available:
                view_id = str(view["view_id"])
                if view_id in selected_ids:
                    continue
                selected.append((view, source))
                selected_ids.add(view_id)
                if len(selected) >= max_views_per_product:
                    break

        for view, source in selected:
            suffix = source.suffix.lower() or ".img"
            attached_name = f"rag-reference--{product_id}--{view['view_id']}{suffix}"
            target = destination / attached_name
            shutil.copy2(source, target)
            copied.append(
                {
                    "reference_product_id": product_id,
                    "product_name": str(product["product_name"]),
                    "product_code": str(product["product_code"]),
                    "product_code_aliases": [
                        str(value) for value in product.get("product_code_aliases") or []
                    ],
                    "barcode_69": str(product["barcode_69"]),
                    "view_id": str(view["view_id"]),
                    "face": str(view["face"]),
                    "identity_strength": str(view["identity_strength"]),
                    "visible_anchors": list(view.get("visible_anchors") or []),
                    "attached_file": attached_name,
                    "path": target,
                }
            )
    return copied


def _normalize_product_lookup_text(value: Any) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value).casefold())


def _product_candidate_score(product: dict[str, Any], query: dict[str, Any]) -> int:
    if not product.get("views"):
        return 0
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
    visible_text = _normalize_product_lookup_text(
        " ".join(str(value) for value in query.get("visible_text") or [])
    )

    barcode_matched = barcode in visible_barcodes or barcode in visible_text
    score = 1000 if barcode_matched else 0

    product_codes = {
        _normalize_product_lookup_text(value)
        for value in (
            product.get("product_code"),
            *(product.get("product_code_aliases") or []),
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

    catalog_names = [
        _normalize_product_lookup_text(product.get("product_name") or ""),
        *(
            _normalize_product_lookup_text(value)
            for value in product.get("aliases") or []
        ),
    ]
    query_name_candidates = [*visible_names]
    if visible_text:
        query_name_candidates.append(visible_text)
    name_match_score = 0
    for catalog_name in catalog_names:
        for visible_name in query_name_candidates:
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

    if score == 0:
        return 0

    supporting = [
        product.get("specification"),
        product.get("variant"),
        *(product.get("specification_aliases") or []),
        *(product.get("variant_aliases") or []),
    ]
    for value in supporting:
        normalized = _normalize_product_lookup_text(value or "")
        if len(normalized) >= 2 and normalized in visible_text:
            score += 25
    return score


def _select_product_rag_candidates(
    catalog: dict[str, Any],
    query_result: dict[str, Any],
    *,
    max_products: int = MAX_PRODUCT_REFERENCE_CANDIDATES,
) -> dict[str, Any]:
    products = list(catalog.get("products") or [])
    ranked_per_photo: list[list[tuple[int, str]]] = []
    by_id = {str(product["product_id"]): product for product in products}
    for query in query_result.get("photo_queries") or []:
        ranked = sorted(
            (
                (score, str(product["product_id"]))
                for product in products
                if (score := _product_candidate_score(product, query)) > 0
            ),
            key=lambda item: (-item[0], item[1]),
        )
        ranked_per_photo.append(ranked[:MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO])

    selected_ids: list[str] = []
    selected_set: set[str] = set()
    for rank in range(MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO):
        for ranked in ranked_per_photo:
            if rank >= len(ranked):
                continue
            product_id = ranked[rank][1]
            if product_id in selected_set:
                continue
            selected_set.add(product_id)
            selected_ids.append(product_id)
            if len(selected_ids) >= max_products:
                return {
                    "schema_version": catalog["schema_version"],
                    "products": [by_id[item] for item in selected_ids],
                }
    return {
        "schema_version": catalog["schema_version"],
        "products": [by_id[item] for item in selected_ids],
    }


def _extract_scanned_pdf_pages(source: Path, destination: Path) -> list[Path]:
    """Losslessly expose one full-page scan per PDF page to the vision model."""
    try:
        reader = PdfReader(source)
    except Exception as exc:  # pypdf exposes several parser-specific exceptions
        raise AuditError(f"合同 PDF 无法读取：{source.name}：{exc}") from exc
    if not reader.pages:
        raise AuditError(f"合同 PDF 没有页面：{source.name}")

    rendered: list[Path] = []
    for page_no, page in enumerate(reader.pages, start=1):
        try:
            embedded = list(page.images)
        except Exception as exc:
            raise AuditError(f"合同 PDF 第 {page_no} 页图像读取失败：{exc}") from exc
        if len(embedded) != 1:
            raise AuditError(
                f"合同 PDF 第 {page_no} 页不是单张完整扫描页（检测到 {len(embedded)} 张图），"
                "为避免漏读或拼错内容，已停止视觉识别"
            )
        image = embedded[0].image
        target = destination / f"contract-page-{page_no:02d}.png"
        if image.mode not in {"1", "L", "LA", "P", "RGB", "RGBA"}:
            image = image.convert("RGB")
        image.save(target, format="PNG", optimize=False)
        rendered.append(target)
    return rendered


def _write_subset_schema(
    full_schema: Path,
    destination: Path,
    payload_property: str,
    title: str,
) -> Path:
    source = json.loads(full_schema.read_text(encoding="utf-8"))
    names = ["schema_version", "scenario", payload_property, "extraction_notes"]
    schema = {
        "$schema": source.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "title": title,
        "type": "object",
        "additionalProperties": False,
        "required": names,
        "properties": {name: source["properties"][name] for name in names},
    }
    destination.write_text(
        json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return destination


def _personnel_prompt(skill_dir: Path, images: list[Path], schema: Path) -> str:
    names = "\n".join(f"- `{path.name}`" for path in images)
    return f"""Read `{skill_dir / 'SKILL.md'}` completely, then read its directly linked audit rules.

Inspect every attached image below at original resolution and return exactly one JSON object conforming to `{schema}`.

{names}

This is visual extraction only. Do not calculate an approval conclusion, Excel quantity, or Excel product correspondence. The sales Excel is deliberately absent from this AI workspace. Do not search the repository, `input/`, `worktrees/`, prior outputs, caches, or a gold-standard workbook for missing facts. Preserve source basenames exactly. Use null or a limitation note instead of guessing.

Read the settlement image and every transfer screenshot. Extract all settlement lines in printed order. Set `barcode_visible` only when the barcode is actually legible on the settlement; never infer it from product identity or quantity. Represent each distinct business transfer once, retain its visible occurrence count, and explain any sender/receiver-view deduplication. A chat title is not a store mapping. A weekday or clock time is not a complete transfer date.
"""


def _contract_prompt(
    skill_dir: Path,
    original_pdf: Path,
    page_images: list[Path],
    schema: Path,
) -> str:
    pages = "\n".join(f"- `{path.name}`" for path in page_images)
    return f"""Read `{skill_dir / 'SKILL.md'}` completely and then its linked audit rules. This is a focused contract pass; use the focused output schema `{schema}` instead of the full evidence schema.

The original contract is `{original_pdf.name}`. Its {len(page_images)} scanned pages were extracted losslessly and attached in page order:

{pages}

Inspect every page at original resolution and return exactly one JSON object conforming to the focused schema. `contract.source_file` must be exactly `{original_pdf.name}`, never a rendered page filename.

Use only explicit core contract terms for dates, stores, display standard, fee, claim, product scope, and promotion requirements. Do not convert a product or sales table appended after the contract into a contractual promotion condition. Generic scope such as `参半所有系列` covers the whole brand range and is not a narrow mandatory-product subset: set `requires_specific_products` to false and `required_products` to an empty array. Set `requires_promotion` to true only when the contract explicitly requires a discount, gift, multi-buy, special price, or other named promotion mechanic. Read the per-store fee from the explicit fee/calculation wording; never infer it from the claim alone. Preserve all contract stores in printed order. Use null or a limitation note instead of guessing.
"""


def _product_query_prompt(skill_dir: Path, images: list[Path], schema: Path) -> str:
    names = "\n".join(f"- `{path.name}`" for path in images)
    return f"""Read `{skill_dir / 'SKILL.md'}` completely and then its linked audit rules. This is a bounded product-query pass, not a reimbursement or display decision. Use `{schema}`.

Inspect each attached field photo at original resolution and return exactly one `photo_queries` item for every file below, preserving each basename exactly:

{names}

Record only identifiers truly visible in that same photo: a product name or distinctive name fragment, an explicit product code such as SP-1/CB-3, a complete 13-digit 69 barcode, and short supporting text. A barcode must start with 69, contain exactly 13 digits, and be fully legible; otherwise omit it. A QR code, anti-counterfeit code, batch/date printing, color, box shape, generic words such as `牙膏` or the brand alone are not product identity. Do not infer hidden text, do not combine separate photos into a stronger observation, do not consult Excel, and do not decide store, date, display, promotion, amount, duplicate-photo status, or catalog match. Use empty arrays and a limitation instead of guessing.
"""


def _validate_product_query_result(images: list[Path], result: dict[str, Any]) -> None:
    expected = [path.name for path in images]
    returned = [str(item["photo_file"]) for item in result.get("photo_queries") or []]
    if len(returned) != len(set(returned)):
        raise AuditError("商品候选预检重复返回同一现场照片")
    if len(returned) != len(expected) or set(returned) != set(expected):
        missing = sorted(set(expected) - set(returned))
        unknown = sorted(set(returned) - set(expected))
        raise AuditError(
            "商品候选预检必须逐张覆盖现场照片："
            f"missing={missing}，unknown={unknown}"
        )
    for item in result.get("photo_queries") or []:
        for barcode in item.get("visible_barcodes_69") or []:
            if not ean13_is_valid(str(barcode)):
                raise AuditError(
                    f"商品候选预检返回了无效EAN-13：{item['photo_file']}={barcode}"
                )


def _photo_prompt(
    skill_dir: Path,
    images: list[Path],
    schema: Path,
    contract_result: dict[str, Any],
    product_rag: dict[str, Any],
    product_reference_files: list[dict[str, Any]],
) -> str:
    names = "\n".join(f"- `{path.name}`" for path in images)
    contract_json = json.dumps(contract_result["contract"], ensure_ascii=False, indent=2)
    product_lines: list[str] = []
    products = {
        str(item["product_id"]): item for item in product_rag.get("products") or []
    }
    for product_id, product in products.items():
        policy = (
            "仅候选，禁止 exact"
            if product.get("match_policy") == "candidate_only"
            else "可按证据返回 exact 或 candidate"
        )
        aliases = "、".join(str(value) for value in product.get("aliases") or []) or "无"
        code_aliases = (
            "、".join(str(value) for value in product.get("product_code_aliases") or [])
            or "无"
        )
        product_lines.append(
            f"- `{product_id}`：产品名称 `{product['product_name']}`；"
            f"产品编码 `{product['product_code']}`；69码 `{product['barcode_69']}`；"
            f"规格 `{product['specification']}`；款式/香型 `{product.get('variant')}`；"
            f"名称别名 `{aliases}`；产品编码别名 `{code_aliases}`；命中策略 `{policy}`"
        )
        for item in product_reference_files:
            if item["reference_product_id"] != product_id:
                continue
            anchors = "、".join(str(value) for value in item["visible_anchors"])
            product_lines.append(
                f"  - `{item['attached_file']}` → view_id `{item['view_id']}`，"
                f"物理面 `{item['face']}`，参考强度 `{item['identity_strength']}`，"
                f"可见锚点：{anchors}"
            )
    product_context = "\n".join(product_lines) or "本次预检没有形成可靠候选；不得返回 product_reference_hits。"
    return f"""Read `{skill_dir / 'SKILL.md'}` completely and then its linked audit rules and `{skill_dir / 'references' / 'product-rag.md'}`. This is a focused field-photo pass; use the focused output schema `{schema}` instead of the full evidence schema.

Inspect every attached field photo at original resolution:

{names}

The following separately attached images are repository-owned product-reference views, not field evidence. Use them only to retrieve and compare product identity. Never put a `rag-reference--...` filename in `photo_files`, and never use a reference image to infer a store, date, display, promotion, price, or photo uniqueness:

{product_context}

The following already-validated contract JSON is authoritative only for contract store order, activity dates, display standard, product scope, and promotion requirements. Do not rewrite it and do not use it to invent facts that are not visible in a photo:

```json
{contract_json}
```

Return exactly one photo-review row for every contract store line, in contract order, including an empty `photo_files` list when no field photo can be assigned. Preserve field-photo basenames exactly. `recognized_products` may contain only products supported by visible field packaging or a grounded product-reference comparison; never use an Excel-derived name. For `product_reference_hits`, return only the listed `reference_product_id` and `view_id` values. Use `exact` only when the field photo shows a complete valid 69 code, or a product code/name plus an independent compatible anchor, or at least two independent identity anchors with a uniquely compatible reference view. Brand, red/silver color, box shape, generic whitening text, a QR code, batch/date printing, or background alone cannot produce `exact`. Use `candidate` when the field packaging is compatible but not unique, and use an empty array when there is no reliable catalog match. Every `visible_basis` item must describe something actually visible in a field photo; reference-only content is not a field observation. A row with no field photo must have an empty `product_reference_hits` array.

Extract ordinary visible prices separately from explicit promotion signals. A normal price tag alone is not a promotion. An explicit promotion signal requires visible special-price wording, old/new price, discount, gift, multi-buy, 1+1, 3+2, or value-pack wording. Field filenames are routing leads only and cannot independently prove date, location, product, promotion, or display compliance. Use null, `unclear`, an empty reference-hit array, or a limitation note instead of guessing.

The mandatory display standard has two independent ways to pass: a clearly supported `1平米堆头`, or a clearly countable `4纵陈列`. For every `display_observation`, set `matched_standard` to exactly one of `stack_1sqm`, `four_vertical`, `both`, `none`, or `unclear`. Use `standard_evidence=meets` only with `stack_1sqm`, `four_vertical`, or `both`; use `does_not_meet` only with `none`; and use `unclear` only with `unclear`.

Count vertical facings conservatively from left to right. Count only simultaneously visible, distinct vertical product columns on the same display plane; do not add boxes stacked vertically, columns from different shelf levels or viewing angles, or hidden/inferred columns. Put the exact integer in `vertical_facing_count` and one short left-to-right description per counted column in `vertical_facing_basis`; the integer and array length must match. Use null plus an empty array when a reliable count is impossible. `four_vertical` or `both` requires at least four listed columns. Set `stack_1sqm_basis` only when visible scale, dimensions, or a complete footprint proves at least one square metre; otherwise use null. The `description` must summarize these structured facts and the matched alternative, never only a generic phrase such as `陈列符合`. If neither branch is proved, return `unclear`. Do not decide whether photos are duplicated or reused across stores; deterministic code performs that separate anti-fraud check.
"""


def _validate_personnel_sources(case: dict[str, Any], evidence: dict[str, Any]) -> None:
    expected_settlement = Path(case["settlement_image"]).name
    if Path(str(evidence["settlement"]["source_file"])).name != expected_settlement:
        raise AuditError("AI 返回的结算单文件名不属于本次材料")
    allowed = {Path(path).name for path in case["transfer_images"]}
    used = {
        Path(name).name
        for transfer in evidence.get("transfers") or []
        for name in transfer.get("source_files") or []
    }
    unknown = sorted(used - allowed)
    if unknown:
        raise AuditError("AI 返回了不存在的转账截图文件名：" + "、".join(unknown))


def _validate_contract_result(case: dict[str, Any], evidence: dict[str, Any]) -> None:
    contract = evidence["contract"]
    expected_contract = Path(case["contract_pdf"]).name
    if Path(str(contract["source_file"])).name != expected_contract:
        raise AuditError("AI 返回的合同文件名不属于本次材料")
    line_numbers = [int(store["line_no"]) for store in contract["stores"]]
    if len(line_numbers) != len(set(line_numbers)):
        raise AuditError("AI 返回的合同门店序号存在重复")
    if not contract["requires_specific_products"] and contract["required_products"]:
        raise AuditError("合同未要求限定产品时，required_products 必须为空")
    if contract["requires_specific_products"] and not contract["required_products"]:
        raise AuditError("合同要求限定产品时，required_products 不能为空")
    if not contract["requires_promotion"] and contract["required_promotion"] not in {None, ""}:
        raise AuditError("合同未要求促销形式时，required_promotion 必须为空")


def _validate_photo_result(
    case: dict[str, Any],
    contract_result: dict[str, Any],
    evidence: dict[str, Any],
    product_rag: dict[str, Any] | None = None,
) -> None:
    allowed = {Path(path).name for path in case["photo_files"]}
    reviews = evidence.get("photo_reviews") or []
    used = {
        Path(name).name
        for review in reviews
        for name in review.get("photo_files") or []
    }
    unknown = sorted(used - allowed)
    if unknown:
        raise AuditError("AI 返回了不存在的现场照片文件名：" + "、".join(unknown))

    stores = contract_result["contract"]["stores"]
    expected_lines = [int(store["line_no"]) for store in stores]
    actual_lines = [int(review["store_line_no"]) for review in reviews]
    if actual_lines != expected_lines:
        raise AuditError(
            "现场照片核对必须按合同顺序逐店返回；"
            f"期望 {expected_lines}，实际 {actual_lines}"
        )
    expected_names = {
        int(store["line_no"]): str(store["store_name"]).strip()
        for store in stores
    }
    for review in reviews:
        line_no = int(review["store_line_no"])
        if str(review["contract_store_name"]).strip() != expected_names[line_no]:
            raise AuditError(f"合同第 {line_no} 家门店名称被现场照片核对改写")
        raw_hits = list(review.get("product_reference_hits") or [])
        if not review.get("photo_files") and raw_hits:
            raise AuditError(f"合同第 {line_no} 家门店没有现场照片却返回了商品视觉RAG命中")
        if product_rag is not None:
            resolve_product_reference_hits(raw_hits, product_rag)
        observation = review.get("display_observation") or {}
        evidence_level = str(observation.get("standard_evidence") or "")
        matched_standard = str(observation.get("matched_standard") or "")
        expected_standards = {
            "meets": {"stack_1sqm", "four_vertical", "both"},
            "does_not_meet": {"none"},
            "unclear": {"unclear"},
        }
        if (
            evidence_level not in expected_standards
            or matched_standard not in expected_standards[evidence_level]
        ):
            raise AuditError(
                f"合同第 {line_no} 家门店的陈列结论与命中标准不一致："
                f"standard_evidence={evidence_level}, matched_standard={matched_standard}"
            )
        description = str(observation.get("description") or "").strip()
        if evidence_level == "meets" and description in {"陈列符合", "符合", "达标"}:
            raise AuditError(
                f"合同第 {line_no} 家门店的陈列依据过于笼统，必须说明1平米堆头或4纵陈列的可见依据"
            )
        vertical_count = observation.get("vertical_facing_count")
        vertical_basis = list(observation.get("vertical_facing_basis") or [])
        normalized_vertical_basis = [str(value).strip().casefold() for value in vertical_basis]
        if any(not value for value in normalized_vertical_basis):
            raise AuditError(
                f"合同第 {line_no} 家门店的逐列依据包含空白项"
            )
        if len(normalized_vertical_basis) != len(set(normalized_vertical_basis)):
            raise AuditError(
                f"合同第 {line_no} 家门店的逐列依据存在重复，不能把同一纵列重复计数"
            )
        if vertical_count is None:
            if vertical_basis:
                raise AuditError(
                    f"合同第 {line_no} 家门店未给出可计数纵列，却返回了纵列依据"
                )
        elif int(vertical_count) != len(vertical_basis):
            raise AuditError(
                f"合同第 {line_no} 家门店的可见纵列数与逐列依据数量不一致："
                f"count={vertical_count}, basis={len(vertical_basis)}"
            )
        proves_four_vertical = matched_standard in {"four_vertical", "both"}
        if proves_four_vertical and (
            vertical_count is None or int(vertical_count) < 4 or len(vertical_basis) < 4
        ):
            raise AuditError(
                f"合同第 {line_no} 家门店声明达到4纵陈列，但没有列出至少4个可见纵列"
            )
        if not proves_four_vertical and vertical_count is not None and int(vertical_count) >= 4:
            raise AuditError(
                f"合同第 {line_no} 家门店已列出至少4个可见纵列，却未命中four_vertical"
            )
        stack_basis = str(observation.get("stack_1sqm_basis") or "").strip()
        proves_stack = matched_standard in {"stack_1sqm", "both"}
        if proves_stack and not stack_basis:
            raise AuditError(
                f"合同第 {line_no} 家门店声明达到1平米堆头，但没有面积可见依据"
            )
        if not proves_stack and stack_basis:
            raise AuditError(
                f"合同第 {line_no} 家门店给出了1平米面积依据，却未命中stack_1sqm"
            )


def _validate_source_names(
    case: dict[str, Any],
    evidence: dict[str, Any],
    product_rag: dict[str, Any] | None = None,
) -> None:
    if str(case["scenario"]) == "personnel_incentive":
        _validate_personnel_sources(case, evidence)
        return
    _validate_contract_result(case, evidence)
    _validate_photo_result(
        case,
        {"contract": evidence["contract"]},
        {"photo_reviews": evidence.get("photo_reviews") or []},
        product_rag,
    )


def _run_codex_json(
    *,
    codex: str,
    model_root: Path,
    skill_dir: Path,
    schema: Path,
    raw_output: Path,
    prompt: str,
    images: list[Path],
    selected_model: str,
    model_catalog: Path,
    label: str,
    max_attempts: int,
    attempt_timeout_seconds: int,
    reasoning_effort: str,
    post_validate: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    if reasoning_effort not in ALLOWED_REASONING_EFFORTS:
        raise AuditError(f"不支持的模型推理强度：{reasoning_effort}")
    command = [
        codex,
        "exec",
        "--ephemeral",
        "--ignore-user-config",
        "--json",
        "--color",
        "never",
        "--skip-git-repo-check",
        "--sandbox",
        "read-only",
        "--cd",
        str(model_root),
        "--add-dir",
        str(skill_dir),
        "--output-schema",
        str(schema),
        "--output-last-message",
        str(raw_output),
        "--model",
        selected_model,
        "-c",
        f"model_reasoning_effort={json.dumps(reasoning_effort)}",
        "-c",
        f"model_catalog_json={json.dumps(str(model_catalog), ensure_ascii=False)}",
    ]
    for image in images:
        command.extend(["--image", str(image)])

    last_error = "AI 提取未启动"
    for attempt in range(1, max_attempts + 1):
        attempt_started = time.monotonic()
        print(
            f"AI 正在识别 {label}（第 {attempt}/{max_attempts} 次，"
            f"模型 {selected_model}，推理 {reasoning_effort}）...",
            flush=True,
        )
        raw_output.unlink(missing_ok=True)
        try:
            completed = subprocess.run(
                command,
                cwd=model_root,
                input=prompt,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
                timeout=attempt_timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            last_error = f"单次视觉识别超过 {attempt_timeout_seconds} 秒"
            print(
                f"AI 识别 {label} 第 {attempt}/{max_attempts} 次失败"
                f"（{time.monotonic() - attempt_started:.1f} 秒）：{last_error}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
            continue
        try:
            if completed.returncode != 0:
                detail = "\n".join(
                    value.strip()
                    for value in (completed.stderr, completed.stdout)
                    if value.strip()
                )[-4000:]
                if _is_non_retryable_codex_error(detail):
                    raise CodexRequestConfigurationError(
                        f"codex 请求配置错误，重复执行无法修复：{detail}"
                    )
                raise AuditError(f"codex 退出码 {completed.returncode}：{detail}")
            if not raw_output.is_file():
                raise AuditError("codex 未生成结构化证据")
            value = json.loads(_strip_json_fence(raw_output.read_text(encoding="utf-8")))
            if not isinstance(value, dict):
                raise AuditError("AI 证据顶层必须是 JSON 对象")
            validate_json(value, schema)
            if post_validate is not None:
                post_validate(value)
            print(
                f"AI 完成 {label}（第 {attempt}/{max_attempts} 次，"
                f"{time.monotonic() - attempt_started:.1f} 秒）",
                flush=True,
            )
            return value
        except CodexRequestConfigurationError as exc:
            print(
                f"AI 识别 {label} 配置失败，停止重试"
                f"（{time.monotonic() - attempt_started:.1f} 秒）：{exc}",
                flush=True,
            )
            raise
        except (AuditError, json.JSONDecodeError) as exc:
            last_error = str(exc)
            print(
                f"AI 识别 {label} 第 {attempt}/{max_attempts} 次失败"
                f"（{time.monotonic() - attempt_started:.1f} 秒）：{last_error}",
                flush=True,
            )
            if attempt < max_attempts:
                time.sleep(min(2 ** (attempt - 1), 4))
    raise CodexExtractionError(
        f"AI 证据提取连续失败 {max_attempts} 次：{last_error}"
    )


def extract_with_codex(
    case: dict[str, Any],
    temporary_root: str | Path,
    *,
    model: str | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    attempt_timeout_seconds: int = DEFAULT_ATTEMPT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    scenario = str(case.get("scenario") or "")
    skill_dir = SKILL_BY_SCENARIO.get(scenario)
    if skill_dir is None:
        raise AuditError(f"不支持的 AI 提取场景：{scenario}")
    codex = _find_codex()
    if max_attempts < 1:
        raise AuditError("max_attempts 必须大于等于 1")
    if attempt_timeout_seconds < 1:
        raise AuditError("attempt_timeout_seconds 必须大于等于 1")

    root = Path(temporary_root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    full_schema = skill_dir / "references" / "evidence.schema.json"
    selected_model = model or DEFAULT_MODEL
    model_catalog = root / ".codex-model-catalog.json"
    if not model_catalog.is_file():
        _write_bundled_model_catalog(codex, model_catalog, selected_model)

    if scenario == "personnel_incentive":
        model_root = root / "model-personnel_incentive"
        model_root.mkdir(parents=True, exist_ok=False)
        sources = [Path(case["settlement_image"]), *map(Path, case["transfer_images"])]
        images = _copy_images(sources, model_root)
        return _run_codex_json(
            codex=codex,
            model_root=model_root,
            skill_dir=skill_dir,
            schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=_personnel_prompt(skill_dir, images, full_schema),
            images=images,
            selected_model=selected_model,
            model_catalog=model_catalog,
            label="人员激励材料",
            max_attempts=max_attempts,
            attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=DEFAULT_REASONING_EFFORT,
            post_validate=lambda value: _validate_personnel_sources(case, value),
        )

    contract_root = root / "model-promotional_display-contract"
    contract_root.mkdir(parents=True, exist_ok=False)
    contract_source = Path(case["contract_pdf"])
    copied_contract = contract_root / contract_source.name
    shutil.copy2(contract_source, copied_contract)
    page_images = _extract_scanned_pdf_pages(copied_contract, contract_root)
    contract_schema = _write_subset_schema(
        full_schema,
        contract_root / "contract-evidence.schema.json",
        "contract",
        "Promotional display contract visual evidence",
    )
    contract_result = _run_codex_json(
        codex=codex,
        model_root=contract_root,
        skill_dir=skill_dir,
        schema=contract_schema,
        raw_output=contract_root / "contract-evidence.json",
        prompt=_contract_prompt(skill_dir, copied_contract, page_images, contract_schema),
        images=page_images,
        selected_model=selected_model,
        model_catalog=model_catalog,
        label="堆头合同",
        max_attempts=max_attempts,
        attempt_timeout_seconds=attempt_timeout_seconds,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        post_validate=lambda value: _validate_contract_result(case, value),
    )

    photo_root = root / "model-promotional_display-photos"
    photo_root.mkdir(parents=True, exist_ok=False)
    photo_sources = [Path(value) for value in case["photo_files"]]
    photo_images = _copy_images(photo_sources, photo_root)
    full_product_rag = load_product_rag(skill_dir)
    query_schema = skill_dir / "references" / "product-query.schema.json"
    query_result = _run_codex_json(
        codex=codex,
        model_root=photo_root,
        skill_dir=skill_dir,
        schema=query_schema,
        raw_output=photo_root / "product-query.json",
        prompt=_product_query_prompt(skill_dir, photo_images, query_schema),
        images=photo_images,
        selected_model=selected_model,
        model_catalog=model_catalog,
        label="堆头商品候选预检",
        max_attempts=max_attempts,
        attempt_timeout_seconds=attempt_timeout_seconds,
        reasoning_effort=PRODUCT_QUERY_REASONING_EFFORT,
        post_validate=lambda value: _validate_product_query_result(photo_images, value),
    )
    product_rag = _select_product_rag_candidates(full_product_rag, query_result)
    product_reference_files = _copy_product_reference_images(
        skill_dir,
        photo_root,
        product_rag,
    )
    photo_schema = _write_subset_schema(
        full_schema,
        photo_root / "photo-evidence.schema.json",
        "photo_reviews",
        "Promotional display field-photo visual evidence",
    )
    photo_result = _run_codex_json(
        codex=codex,
        model_root=photo_root,
        skill_dir=skill_dir,
        schema=photo_schema,
        raw_output=photo_root / "photo-evidence.json",
        prompt=_photo_prompt(
            skill_dir,
            photo_images,
            photo_schema,
            contract_result,
            product_rag,
            product_reference_files,
        ),
        images=[
            *photo_images,
            *(item["path"] for item in product_reference_files),
        ],
        selected_model=selected_model,
        model_catalog=model_catalog,
        label="堆头现场照片",
        max_attempts=max_attempts,
        attempt_timeout_seconds=attempt_timeout_seconds,
        reasoning_effort=DEFAULT_REASONING_EFFORT,
        post_validate=lambda value: _validate_photo_result(
            case,
            contract_result,
            value,
            product_rag,
        ),
    )

    merged = {
        "schema_version": "2.2",
        "scenario": "promotional_display",
        "contract": contract_result["contract"],
        "photo_reviews": photo_result["photo_reviews"],
        "extraction_notes": [
            *contract_result.get("extraction_notes", []),
            *query_result.get("extraction_notes", []),
            *photo_result.get("extraction_notes", []),
        ],
    }
    validate_json(merged, full_schema)
    _validate_source_names(case, merged, product_rag)
    return merged
