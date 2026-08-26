from __future__ import annotations

import re
import unicodedata
from calendar import monthrange
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import (
    AuditError,
    exception,
    json_number,
    money,
    normalize_text,
    now_utc,
    unique_by,
)
from .excel_sources import image_file_inventory, pdf_inventory, read_display_sales
from .product_rag import (
    apply_visible_short_code_exact_hits,
    ean13_is_valid,
    load_product_rag,
    product_reference_label,
    resolve_product_reference_hits,
)


PASS_STORE_MATCHES = {"exact", "compatible"}
PASS_DISPLAY_STANDARDS = {"stack_1sqm", "four_vertical", "both"}
PASS_KNOWLEDGE_MATCHES = {"matched", "fuzzy_matched"}
PASS_SALES_PRODUCT_MATCHES = {"exact", "fuzzy"}
KNOWLEDGE_FIELD_LABELS = {
    "product_code": "商品编码",
    "product_name": "商品名称",
    "barcode_69": "69码",
}
NUMBERED_STORE_PATTERN = re.compile(r"(?P<number>\d{1,3}|[一二三四五六七八九十]{1,3})店")
GENERIC_LOCATION_BIGRAMS = {
    "超市", "商场", "购物", "广场", "生活", "连锁", "百货", "中心",
    "广东", "东莞", "深圳", "门店", "精选",
}
DISPLAY_SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "audit-promotional-display"
ENTITY_SUFFIXES = ("有限责任公司", "股份有限公司", "有限公司", "公司")


def _display_control(observation: dict[str, Any]) -> tuple[str, str]:
    evidence_level = str(observation.get("standard_evidence") or "unclear")
    matched_standard = str(observation.get("matched_standard") or "unclear")
    expected = {
        "meets": PASS_DISPLAY_STANDARDS,
        "does_not_meet": {"none"},
        "unclear": {"unclear"},
    }
    if evidence_level not in expected or matched_standard not in expected[evidence_level]:
        raise AuditError(
            "现场照片的陈列结论与命中标准不一致："
            f"standard_evidence={evidence_level}, matched_standard={matched_standard}"
        )
    return {
        "meets": "pass",
        "does_not_meet": "fail",
        "unclear": "uncertain",
    }[evidence_level], matched_standard


def _store_number(value: str) -> int | None:
    if value.isdigit():
        return int(value)
    digits = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "十" in value:
        tens, ones = value.split("十", 1)
        tens_value = digits.get(tens, 1) if tens else 1
        ones_value = digits.get(ones, 0) if ones else 0
        return tens_value * 10 + ones_value
    return digits.get(value)


def _numbered_store_conflict(contract_name: str, visible_location: str | None) -> bool:
    expected = {
        number
        for match in NUMBERED_STORE_PATTERN.finditer(unicodedata.normalize("NFKC", contract_name))
        if (number := _store_number(match.group("number"))) is not None
    }
    if not expected:
        return False
    observed = {
        number
        for match in NUMBERED_STORE_PATTERN.finditer(
            unicodedata.normalize("NFKC", str(visible_location or ""))
        )
        if (number := _store_number(match.group("number"))) is not None
    }
    return not expected.issubset(observed)


def _location_bigrams(value: str) -> set[str]:
    text = _canonical_location(value)
    return {
        text[index : index + 2]
        for index in range(max(0, len(text) - 1))
        if text[index : index + 2] not in GENERIC_LOCATION_BIGRAMS
    }


def _corroborated_location_bridge(
    contract_name: str,
    visible_location: str | None,
    photo_files: list[str],
) -> bool:
    """Use a filename only as a bridge between two independently present names."""
    if not visible_location:
        return False
    contract_grams = _location_bigrams(contract_name)
    visible_grams = _location_bigrams(visible_location)
    if not contract_grams or not visible_grams:
        return False
    for name in photo_files:
        filename_grams = _location_bigrams(Path(name).stem)
        contract_coverage = len(contract_grams & filename_grams) / len(contract_grams)
        visible_overlap = len(visible_grams & filename_grams)
        if contract_coverage >= 0.6 and visible_overlap >= 2:
            return True
    return False


def _canonical_location(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def _canonical_entity(value: Any) -> str:
    text = _canonical_location(str(value or ""))
    changed = True
    while changed and text:
        changed = False
        for suffix in ENTITY_SUFFIXES:
            normalized_suffix = _canonical_location(suffix)
            if text.endswith(normalized_suffix) and len(text) > len(normalized_suffix):
                text = text[: -len(normalized_suffix)]
                changed = True
                break
    return text


def _entity_similarity(left: Any, right: Any) -> float:
    left_text = _canonical_entity(left)
    right_text = _canonical_entity(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    if min(len(left_text), len(right_text)) >= 4 and (
        left_text in right_text or right_text in left_text
    ):
        return 0.9
    left_grams = {
        left_text[index : index + 2]
        for index in range(max(0, len(left_text) - 1))
    }
    right_grams = {
        right_text[index : index + 2]
        for index in range(max(0, len(right_text) - 1))
    }
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _parse_period_bounds(value: Any) -> tuple[date, date] | None:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not text:
        return None
    matches = re.findall(
        r"(?<!\d)(20\d{2})\s*[年./-]\s*(\d{1,2})(?:\s*[月./-]\s*(\d{1,2}))?\s*日?",
        text,
    )
    if not matches:
        compact = re.fullmatch(r"(20\d{2})(\d{2})(\d{2})", re.sub(r"\D", "", text))
        if compact:
            try:
                parsed = date(int(compact.group(1)), int(compact.group(2)), int(compact.group(3)))
            except ValueError:
                return None
            return parsed, parsed
        return None
    parsed_bounds: list[tuple[date, date]] = []
    for year_text, month_text, day_text in matches:
        year = int(year_text)
        month = int(month_text)
        try:
            if day_text:
                parsed = date(year, month, int(day_text))
                parsed_bounds.append((parsed, parsed))
            else:
                parsed_bounds.append(
                    (date(year, month, 1), date(year, month, monthrange(year, month)[1]))
                )
        except ValueError:
            return None
    return parsed_bounds[0][0], parsed_bounds[-1][1]


def _contract_sales_reconciliation(
    contract: dict[str, Any],
    sales: dict[str, Any],
) -> dict[str, Any]:
    """Check contract parties, dates and document integrity against sales evidence."""

    sales_customers = [str(value).strip() for value in sales.get("customers") or [] if str(value).strip()]
    contract_parties = list(
        dict.fromkeys(
            str(value).strip()
            for value in [
                contract.get("customer_name"),
                *(contract.get("contract_parties") or []),
            ]
            if str(value or "").strip()
        )
    )
    customer_matches: list[dict[str, Any]] = []
    for customer in sales_customers:
        ranked = sorted(
            (
                (_entity_similarity(customer, party), index, party)
                for index, party in enumerate(contract_parties)
            ),
            key=lambda item: (-item[0], item[1]),
        )
        score, _, party = ranked[0] if ranked else (0.0, 0, None)
        if score == 1.0:
            status = "exact"
        elif score >= 0.6:
            status = "fuzzy"
        else:
            status = "mismatch"
        customer_matches.append(
            {
                "sales_customer": customer,
                "contract_party": party,
                "status": status,
                "similarity": round(score, 3),
            }
        )
    if not sales_customers or not contract_parties:
        customer_status = "unverifiable"
    elif any(item["status"] == "mismatch" for item in customer_matches):
        customer_status = "mismatch"
    elif any(item["status"] == "fuzzy" for item in customer_matches):
        customer_status = "fuzzy"
    else:
        customer_status = "exact"
    if customer_status == "unverifiable":
        customer_basis = "合同签订方或销售客户缺失，无法核对主体。"
    elif customer_status == "mismatch":
        customer_basis = "销售客户中存在无法对应合同签订方的主体。"
    elif customer_status == "fuzzy":
        customer_basis = "销售客户与合同签订方名称存在简称或文字差异，模糊一致。"
    else:
        customer_basis = "销售客户与合同签订方名称一致。"

    period_values = [
        str(value).strip()
        for value in sales.get("period_values") or []
        if str(value).strip()
    ]
    period_checks: list[dict[str, Any]] = []
    activity_start = date.fromisoformat(str(contract["activity_start"]))
    activity_end = date.fromisoformat(str(contract["activity_end"]))
    for value in period_values:
        bounds = _parse_period_bounds(value)
        if bounds is None:
            period_checks.append(
                {"sales_period": value, "start": None, "end": None, "status": "unverifiable"}
            )
            continue
        value_start, value_end = bounds
        status = "covered" if activity_start <= value_start and value_end <= activity_end else "mismatch"
        period_checks.append(
            {
                "sales_period": value,
                "start": value_start.isoformat(),
                "end": value_end.isoformat(),
                "status": status,
            }
        )
    if not period_checks or any(item["status"] == "unverifiable" for item in period_checks):
        period_status = "unverifiable"
    elif any(item["status"] == "mismatch" for item in period_checks):
        period_status = "mismatch"
    else:
        period_status = "covered"
    period_basis = {
        "covered": "销售业务日期全部落在合同执行周期内。",
        "mismatch": "销售业务日期存在超出合同执行周期的记录。",
        "unverifiable": "销售业务日期缺失或无法完整解析，不能确认执行周期一致。",
    }[period_status]

    watermark = contract.get("watermark_visible")
    seal = contract.get("seal_visible")
    watermark_status = "present" if watermark is True else "absent" if watermark is False else "unverifiable"
    seal_status = "present" if seal is True else "absent" if seal is False else "unverifiable"
    if watermark is True or seal is True:
        integrity_status = "pass"
        integrity_basis = "合同可见水印或盖章，具备可核验的签署/来源标记。"
    elif watermark is False and seal is False:
        integrity_status = "fail"
        integrity_basis = "合同未见水印且未见盖章，完整性门禁不通过。"
    else:
        integrity_status = "unverifiable"
        integrity_basis = "合同水印和盖章状态未能确认，完整性门禁待核验。"

    if customer_status == "mismatch" or period_status == "mismatch" or integrity_status == "fail":
        status = "fail"
    elif (
        customer_status == "unverifiable"
        or period_status == "unverifiable"
        or integrity_status == "unverifiable"
    ):
        status = "unverifiable"
    else:
        status = "pass"
    return {
        "status": status,
        "customer_status": customer_status,
        "customer_basis": customer_basis,
        "customer_matches": customer_matches,
        "sales_customers": sales_customers,
        "contract_parties": contract_parties,
        "period_status": period_status,
        "period_basis": period_basis,
        "sales_period_values": period_values,
        "period_checks": period_checks,
        "watermark_status": watermark_status,
        "seal_status": seal_status,
        "integrity_status": integrity_status,
        "integrity_basis": integrity_basis,
    }


def _determine_store_match(
    contract_name: str,
    visible_location: str | None,
    photo_files: list[str],
) -> tuple[str, str]:
    if not visible_location:
        return "filename_only", "现场未识别到独立地点，文件名不能单独证明门店"
    if _numbered_store_conflict(contract_name, visible_location):
        return "mismatch", "合同为编号门店，现场地点未显示相同门店编号"

    contract_text = _canonical_location(contract_name)
    visible_text = _canonical_location(visible_location)
    if contract_text in visible_text or visible_text in contract_text:
        return "exact", "可见地点包含合同门店名称"

    contract_grams = _location_bigrams(contract_name)
    visible_grams = _location_bigrams(visible_location)
    direct_similarity = (
        len(contract_grams & visible_grams) / min(len(contract_grams), len(visible_grams))
        if contract_grams and visible_grams
        else 0.0
    )
    if direct_similarity >= 0.4:
        return "compatible", "可见地点与合同门店的有效名称片段一致"
    if _corroborated_location_bridge(contract_name, visible_location, photo_files):
        return "compatible", "可见地点与合同门店名称由同一原始文件名双向印证"
    return "mismatch", "可见地点与合同门店缺少可验证的名称对应关系"


def _canonical_product(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("參半", "参半")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff+%]+", "", text)


def _product_grams(value: Any) -> set[str]:
    text = _canonical_product(value)
    for token in ("参半", "oralshark", "牙膏", "组合装", "超值装", "特享装", "量贩装"):
        text = text.replace(token, "")
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _similarity(left: Any, right: Any) -> float:
    left_grams = _product_grams(left)
    right_grams = _product_grams(right)
    if not left_grams or not right_grams:
        return 0.0
    return len(left_grams & right_grams) / len(left_grams | right_grams)


def _catalog_name_values(product: dict[str, Any]) -> list[str]:
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


def _measurement_tokens(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return {
        f"{number}{unit}"
        for number, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*(ml|毫升|g|克|支|条|片)",
            text,
        )
    }


def _name_measurements_compatible(left: Any, right: Any) -> bool:
    left_tokens = _measurement_tokens(left)
    right_tokens = _measurement_tokens(right)
    return not left_tokens or not right_tokens or bool(left_tokens & right_tokens)


def _catalog_name_score(value: Any, product: dict[str, Any]) -> float:
    return max(
        (
            _similarity(value, candidate)
            for candidate in _catalog_name_values(product)
            if _name_measurements_compatible(value, candidate)
        ),
        default=0.0,
    )


def _unique_fuzzy_product(
    value: Any,
    products: list[dict[str, Any]],
    *,
    minimum_score: float,
) -> tuple[dict[str, Any] | None, float]:
    ranked = sorted(
        ((_catalog_name_score(value, product), product) for product in products),
        key=lambda item: (-item[0], str(item[1].get("product_id") or "")),
    )
    if not ranked or ranked[0][0] < minimum_score:
        return None, ranked[0][0] if ranked else 0.0
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None, ranked[0][0]
    return ranked[0][1], ranked[0][0]


def _catalog_identity_projection(product: dict[str, Any]) -> dict[str, str]:
    return {
        "product_id": str(product["product_id"]),
        "product_code": str(product["product_code"]),
        "product_name": str(product["product_name"]),
        "barcode_69": str(product["barcode_69"]),
    }


def _sales_catalog_reconciliation(
    sales_records: list[dict[str, Any]],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Resolve every sales row with strict code/barcode and exact-or-fuzzy name checks."""

    products = list(catalog.get("products") or [])
    by_id = {str(product["product_id"]): product for product in products}
    reconciled: list[dict[str, Any]] = []

    for record in sales_records:
        source_values = {
            "product_code": str(record.get("product_code") or "").strip(),
            "product_name": str(record.get("product_name") or "").strip(),
            "barcode_69": str(record.get("barcode") or "").strip(),
        }
        missing_fields = [field for field, value in source_values.items() if not value]
        invalid_barcode = bool(source_values["barcode_69"]) and not ean13_is_valid(
            source_values["barcode_69"]
        )
        field_candidates: dict[str, set[str]] = {
            "product_code": {
                str(product["product_id"])
                for product in products
                if source_values["product_code"]
                and _canonical_product(source_values["product_code"])
                in {
                    _canonical_product(product.get("product_code")),
                    *(
                        _canonical_product(value)
                        for value in product.get("product_code_aliases") or []
                    ),
                }
            },
            "product_name": {
                str(product["product_id"])
                for product in products
                if source_values["product_name"]
                and _canonical_product(source_values["product_name"])
                in {
                    _canonical_product(value)
                    for value in _catalog_name_values(product)
                }
            },
            "barcode_69": {
                str(product["product_id"])
                for product in products
                if not invalid_barcode
                and source_values["barcode_69"]
                and str(product.get("barcode_69") or "") == source_values["barcode_69"]
            },
        }
        code_ids = field_candidates["product_code"]
        exact_name_ids = field_candidates["product_name"]
        barcode_ids = field_candidates["barcode_69"]
        strict_ids = code_ids & barcode_ids if code_ids and barcode_ids else set()
        selected: dict[str, Any] | None = None
        diagnostic_selected: dict[str, Any] | None = None
        name_match_type = "unmatched"
        fuzzy_score = 0.0

        if not missing_fields and not invalid_barcode and strict_ids:
            exact_strict_names = strict_ids & exact_name_ids
            if len(exact_strict_names) == 1:
                selected = by_id[next(iter(exact_strict_names))]
                name_match_type = "exact"
            elif len(exact_strict_names) > 1:
                selected = None
            else:
                selected, fuzzy_score = _unique_fuzzy_product(
                    source_values["product_name"],
                    [by_id[product_id] for product_id in sorted(strict_ids)],
                    minimum_score=0.45 if len(strict_ids) > 1 else 0.55,
                )
                if selected is not None:
                    name_match_type = "fuzzy"

        nonempty_sets = [values for values in field_candidates.values() if values]
        diagnostic_intersection = (
            set.intersection(*nonempty_sets) if nonempty_sets else set()
        )
        if selected is not None:
            diagnostic_selected = selected
        elif len(diagnostic_intersection) == 1:
            diagnostic_selected = by_id[next(iter(diagnostic_intersection))]
        else:
            name_barcode = exact_name_ids & barcode_ids
            if len(name_barcode) == 1:
                diagnostic_selected = by_id[next(iter(name_barcode))]
            elif len(strict_ids) == 1:
                diagnostic_selected = by_id[next(iter(strict_ids))]

        selected_id = (
            str(diagnostic_selected["product_id"])
            if diagnostic_selected is not None
            else None
        )
        if selected is not None and name_match_type == "fuzzy":
            field_candidates["product_name"].add(str(selected["product_id"]))

        matched_fields: list[str] = []
        unmatched_fields: list[str] = []
        conflicting_fields: list[str] = []

        field_comparisons: list[dict[str, Any]] = []
        for field, source_value in source_values.items():
            candidates = field_candidates[field]
            if not source_value:
                comparison = "missing"
            elif field == "barcode_69" and invalid_barcode:
                comparison = "invalid"
            elif (
                field == "product_name"
                and selected is not None
                and name_match_type == "fuzzy"
            ):
                comparison = "fuzzy"
            elif selected_id and selected_id in candidates:
                comparison = "matched"
            elif not candidates:
                comparison = "not_found"
            elif selected_id is None and len(candidates) > 1:
                comparison = "ambiguous"
            elif candidates:
                comparison = "conflict"
            else:
                comparison = "not_found"
            selected_knowledge_value = None
            if diagnostic_selected is not None:
                selected_knowledge_value = str(diagnostic_selected[field])
            if comparison in {"matched", "fuzzy"}:
                matched_fields.append(field)
            elif comparison == "not_found":
                unmatched_fields.append(field)
            elif comparison in {"conflict", "ambiguous", "invalid"}:
                conflicting_fields.append(field)
            field_comparisons.append(
                {
                    "field": field,
                    "source_value": source_value,
                    "comparison": comparison,
                    "selected_knowledge_value": selected_knowledge_value,
                    "matching_products": [
                        _catalog_identity_projection(by_id[product_id])
                        for product_id in sorted(candidates)
                    ],
                }
            )

        if invalid_barcode or missing_fields:
            status = "conflict"
        elif not code_ids and not exact_name_ids and not barcode_ids:
            status = "unmatched"
        elif not code_ids or not barcode_ids or not strict_ids:
            status = "conflict"
        elif selected is not None and name_match_type == "exact":
            status = "matched"
        elif selected is not None and name_match_type == "fuzzy":
            status = "fuzzy_matched"
        elif len(strict_ids) > 1:
            status = "ambiguous"
        else:
            status = "conflict"

        if status == "unmatched":
            basis = (
                f"销售Excel第{record['excel_row']}行的商品编码、商品名称、69码"
                "均未命中正式商品知识库。"
            )
        elif status == "ambiguous":
            basis = (
                f"销售Excel第{record['excel_row']}行命中多个知识库商品，"
                "商品名称不能在同码/同编码候选中唯一消歧。"
            )
        elif status == "conflict":
            details: list[str] = []
            if invalid_barcode:
                details.append("69码未通过EAN-13校验")
            if missing_fields:
                details.append(
                    "缺少" + "、".join(KNOWLEDGE_FIELD_LABELS[field] for field in missing_fields)
                )
            if unmatched_fields:
                details.append(
                    "知识库无同值字段："
                    + "、".join(KNOWLEDGE_FIELD_LABELS[field] for field in unmatched_fields)
                )
            if conflicting_fields:
                details.append(
                    "字段指向不同知识库商品："
                    + "、".join(KNOWLEDGE_FIELD_LABELS[field] for field in conflicting_fields)
                )
            basis = (
                f"销售Excel第{record['excel_row']}行未通过知识库核验；"
                + "；".join(details or ["商品名称不能与严格编码和69码唯一对应"])
            )
        else:
            assert selected is not None
            if status == "matched":
                basis = (
                    f"销售Excel第{record['excel_row']}行商品编码、商品名称和69码"
                    f"均与知识库商品{selected['product_code']}严格一致。"
                )
            else:
                basis = (
                    f"销售Excel第{record['excel_row']}行商品编码和69码严格一致；"
                    f"商品名称与知识库商品{selected['product_code']}模糊一致"
                    f"（{fuzzy_score:.3f}）。"
                )
        candidate_id_set = set().union(*nonempty_sets) if nonempty_sets else set()
        if selected_id:
            candidate_id_set.add(selected_id)
        candidate_ids = sorted(candidate_id_set)
        reconciled.append(
            {
                "excel_row": int(record["excel_row"]),
                "source_product_code": source_values["product_code"],
                "source_product_name": source_values["product_name"],
                "source_barcode_69": source_values["barcode_69"],
                "source_quantity": record.get("quantity"),
                "knowledge_status": status,
                "knowledge_product_id": selected_id,
                "knowledge_product_name": (
                    str(diagnostic_selected["product_name"])
                    if diagnostic_selected is not None
                    else None
                ),
                "knowledge_product_code": (
                    str(diagnostic_selected["product_code"])
                    if diagnostic_selected is not None
                    else None
                ),
                "knowledge_barcode_69": (
                    str(diagnostic_selected["barcode_69"])
                    if diagnostic_selected is not None
                    else None
                ),
                "name_match_type": name_match_type,
                "name_similarity": round(fuzzy_score, 3) if fuzzy_score else None,
                "matched_fields": matched_fields,
                "unmatched_fields": unmatched_fields,
                "conflicting_fields": conflicting_fields,
                "missing_fields": missing_fields,
                "candidate_product_ids": candidate_ids,
                "field_comparisons": field_comparisons,
                "basis": basis,
            }
        )

    problem_rows = [
        int(item["excel_row"])
        for item in reconciled
        if item["knowledge_status"] not in PASS_KNOWLEDGE_MATCHES
    ]
    matched_count = sum(item["knowledge_status"] == "matched" for item in reconciled)
    fuzzy_count = sum(item["knowledge_status"] == "fuzzy_matched" for item in reconciled)
    status = "pass" if not problem_rows else "fail"
    return {
        "status": status,
        "record_count": len(reconciled),
        "matched_count": matched_count,
        "fuzzy_count": fuzzy_count,
        "problem_count": len(problem_rows),
        "problem_rows": problem_rows,
        "records": reconciled,
        "basis": (
            f"{matched_count + fuzzy_count}/{len(reconciled)}行通过商品知识库"
            f"（精确{matched_count}行、模糊{fuzzy_count}行）"
            + (f"；问题行：{problem_rows}" if problem_rows else "；全部通过")
        ),
    }


def _contract_product_knowledge_reconciliation(
    contract: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    required_products = [str(value).strip() for value in contract.get("required_products") or []]
    if not contract.get("requires_specific_products"):
        return {
            "status": "not_applicable",
            "records": [],
            "knowledge_product_ids": [],
            "basis": "合同未出现可核验的具体商品编码、明确商品名或69码，商品知识库不适用。",
        }
    products = list(catalog.get("products") or [])
    by_id = {str(product["product_id"]): product for product in products}
    identities = list(contract.get("required_product_identities") or [])
    if not identities:
        identities = [
            {
                "visible_text": value,
                "product_code": None,
                "product_name": value,
                "barcode_69": None,
            }
            for value in required_products
        ]
    records: list[dict[str, Any]] = []
    for identity in identities:
        value = str(identity.get("visible_text") or "").strip()
        source_code = str(identity.get("product_code") or "").strip()
        source_name = str(identity.get("product_name") or "").strip()
        source_barcode = str(identity.get("barcode_69") or "").strip()
        invalid_barcode = bool(source_barcode) and not ean13_is_valid(source_barcode)
        candidate_sets: list[set[str]] = []
        if source_code:
            candidate_sets.append(
                {
                    str(product["product_id"])
                    for product in products
                    if _canonical_product(source_code)
                    in {
                        _canonical_product(product.get("product_code")),
                        *(
                            _canonical_product(alias)
                            for alias in product.get("product_code_aliases") or []
                        ),
                    }
                }
            )
        exact_name_ids = {
            str(product["product_id"])
            for product in products
            if source_name
            and _canonical_product(source_name)
            in {_canonical_product(candidate) for candidate in _catalog_name_values(product)}
        }
        if source_name and exact_name_ids:
            candidate_sets.append(exact_name_ids)
        if source_barcode and not invalid_barcode:
            candidate_sets.append(
                {
                    str(product["product_id"])
                    for product in products
                    if str(product.get("barcode_69") or "") == source_barcode
                }
            )
        intersection = set.intersection(*candidate_sets) if candidate_sets else set()
        selected: dict[str, Any] | None = None
        score = 0.0
        fuzzy = False
        if len(intersection) == 1:
            selected = by_id[next(iter(intersection))]
            score = 1.0
        elif len(intersection) > 1 and source_name:
            selected, score = _unique_fuzzy_product(
                source_name,
                [by_id[product_id] for product_id in sorted(intersection)],
                minimum_score=0.45,
            )
            fuzzy = selected is not None and str(selected["product_id"]) not in exact_name_ids
        elif source_name and not candidate_sets:
            selected, score = _unique_fuzzy_product(
                source_name,
                products,
                minimum_score=0.72,
            )
            fuzzy = selected is not None

        if invalid_barcode or not any((source_code, source_name, source_barcode)):
            knowledge_status = "conflict"
        elif selected is not None:
            knowledge_status = "fuzzy_matched" if fuzzy else "matched"
        elif len(intersection) > 1:
            knowledge_status = "ambiguous"
        elif candidate_sets:
            knowledge_status = "conflict"
        else:
            knowledge_status = "unmatched"
        records.append(
            {
                "required_product": value,
                "source_product_code": source_code or None,
                "source_product_name": source_name or None,
                "source_barcode_69": source_barcode or None,
                "knowledge_status": knowledge_status,
                "knowledge_product_id": str(selected["product_id"]) if selected is not None else None,
                "knowledge_product_name": str(selected["product_name"]) if selected is not None else None,
                "knowledge_product_code": str(selected["product_code"]) if selected is not None else None,
                "knowledge_barcode_69": str(selected["barcode_69"]) if selected is not None else None,
                "basis": (
                    f"合同具体商品{'模糊' if fuzzy else '精确'}对应知识库"
                    + (f"（名称相似度{score:.3f}）" if fuzzy else "")
                    if knowledge_status in PASS_KNOWLEDGE_MATCHES
                    else "合同具体商品无法唯一且一致地对应正式商品知识库"
                ),
            }
        )
    problem = [
        item
        for item in records
        if item["knowledge_status"] not in PASS_KNOWLEDGE_MATCHES
    ]
    return {
        "status": "pass" if not problem else "fail",
        "records": records,
        "knowledge_product_ids": [
            str(item["knowledge_product_id"])
            for item in records
            if item["knowledge_product_id"]
            and item["knowledge_status"] in PASS_KNOWLEDGE_MATCHES
        ],
        "basis": (
            f"合同出现的{len(records)}个具体商品条件全部对应知识库"
            if not problem
            else f"合同具体商品条件中有{len(problem)}个无法唯一对应知识库"
        ),
    }


def _photo_knowledge_control(product_reference_hits: list[dict[str, Any]]) -> tuple[str, str]:
    if not product_reference_hits:
        return "unmatched", "现场照片与商品知识库完全不匹配，置信度低"
    if all(str(hit.get("confidence")) == "exact" for hit in product_reference_hits):
        return "exact", "现场照片与商品知识库精确匹配，置信度高"
    return "candidate", "现场照片与商品知识库模糊匹配，置信度中"


def sales_product_correspondence(
    sales_knowledge_records: list[dict[str, Any]],
    product_reference_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Use each field-photo product to locate and verify only relevant sales rows.

    A field photo first establishes the knowledge-base product.  The registered
    product identity is then used to locate likely Excel rows.  Product code and
    69 code are strict fields; only product name may be fuzzy.  An unrelated bad
    Excel row therefore cannot make every contract store fail.
    """

    def strict_key(value: Any) -> str:
        return unicodedata.normalize("NFKC", str(value or "")).strip().casefold()

    def name_comparison(source_name: str, knowledge_name: str) -> tuple[str, float]:
        if not source_name:
            return "unverifiable", 0.0
        if _canonical_product(source_name) == _canonical_product(knowledge_name):
            return "exact", 1.0
        score = _similarity(source_name, knowledge_name)
        if _name_measurements_compatible(source_name, knowledge_name) and score >= 0.45:
            return "fuzzy", score
        return "mismatch", score

    def code_matches(item: dict[str, Any], hit: dict[str, Any]) -> bool:
        source = strict_key(item.get("source_product_code"))
        allowed = {
            strict_key(hit.get("product_code")),
            *(strict_key(value) for value in hit.get("product_code_aliases") or []),
        }
        allowed.discard("")
        return bool(source and source in allowed)

    def relevant_rows(hit: dict[str, Any]) -> list[dict[str, Any]]:
        """Return rows independently resolved to the photo's catalog product.

        Photo short codes stop at the photo-to-catalog boundary. Sales rows
        start a fresh identity check: the registered 69 code must be exact and
        the product name must be exact or fuzzy-compatible. The catalog
        reconciliation must also resolve the row to this same product; this
        fails closed for shared-barcode ambiguity. Product code is deliberately
        not a row locator because it is a strict field verified afterwards.
        """

        product_id = str(hit.get("reference_product_id") or "")
        knowledge_name = str(hit.get("product_name") or "")
        knowledge_barcode = str(hit.get("barcode_69") or "")
        rows: list[dict[str, Any]] = []
        for item in sales_knowledge_records:
            if (
                not knowledge_barcode
                or str(item.get("source_barcode_69") or "").strip()
                != knowledge_barcode
            ):
                continue
            if str(item.get("knowledge_product_id") or "") != product_id:
                continue
            name_match, _name_score = name_comparison(
                str(item.get("source_product_name") or ""),
                knowledge_name,
            )
            if name_match in {"exact", "fuzzy"}:
                rows.append(item)
        return sorted(rows, key=lambda item: int(item["excel_row"]))

    product_checks: list[dict[str, Any]] = []
    matched_product_ids: set[str] = set()
    for hit in product_reference_hits:
        product_id = str(hit["reference_product_id"])
        knowledge_code = str(hit.get("product_code") or "")
        knowledge_name = str(hit.get("product_name") or "")
        knowledge_barcode = str(hit.get("barcode_69") or "")
        knowledge_aliases = [
            str(value) for value in hit.get("product_code_aliases") or []
        ]
        photo_identity_exact = str(hit.get("confidence") or "") == "exact"
        rows = relevant_rows(hit)
        if not rows:
            product_checks.append(
                {
                    "knowledge_product_id": product_id,
                    "knowledge_product_code": knowledge_code,
                    "knowledge_product_code_aliases": knowledge_aliases,
                    "knowledge_product_name": knowledge_name,
                    "knowledge_barcode_69": knowledge_barcode,
                    "photo_match": "exact" if photo_identity_exact else "fuzzy",
                    "excel_row": None,
                    "source_product_code": "",
                    "source_product_name": "",
                    "source_barcode_69": "",
                    "source_quantity": None,
                    "product_code_match": "unverifiable",
                    "name_match": "unverifiable",
                    "name_similarity": None,
                    "barcode_match": "unverifiable",
                    "status": "unmatched" if photo_identity_exact else "candidate",
                    "confidence": "low" if photo_identity_exact else "medium",
                    "barcode_comparison_basis": (
                        "现场商品已确定，但销售Excel没有找到69码精确一致且商品名称能够对应的有效销售行。"
                        if photo_identity_exact
                        else "现场商品还未精确确定，商品编码和69码暂不比较。"
                    ),
                    "basis": (
                        f"销售Excel没有找到69码为{knowledge_barcode}且商品名称能够对应“{knowledge_name}”的有效销售行。"
                        if photo_identity_exact
                        else "现场商品只能模糊判断，销售行暂不能最终确认。"
                    ),
                    "resubmission": (
                        f"重新导出包含“{knowledge_name}”的销售明细："
                        f"商品编码应为{knowledge_code}，69码应为{knowledge_barcode}；"
                        "不要把其他商品行改成这一商品。"
                        if photo_identity_exact
                        else None
                    ),
                }
            )
            continue

        hit_row_statuses: list[str] = []
        for item in rows:
            source_code = str(item.get("source_product_code") or "").strip()
            source_name = str(item.get("source_product_name") or "").strip()
            source_barcode = str(item.get("source_barcode_69") or "").strip()
            name_match, name_score = name_comparison(source_name, knowledge_name)
            if not photo_identity_exact:
                product_code_match = "unverifiable"
                barcode_match = "unverifiable"
                status = "candidate"
                confidence = "medium"
                basis = "现场商品只能模糊判断，这一销售行仅作为可能对应行。"
                resubmission = None
                barcode_basis = "现场商品还未精确确定，69码暂不比较。"
            else:
                product_code_match = "exact" if code_matches(item, hit) else "mismatch"
                barcode_match = (
                    "exact"
                    if source_barcode and source_barcode == knowledge_barcode
                    else "mismatch"
                )
                row_pass = (
                    product_code_match == "exact"
                    and barcode_match == "exact"
                    and name_match in {"exact", "fuzzy"}
                )
                status = name_match if row_pass else "unmatched"
                confidence = (
                    "high"
                    if status == "exact"
                    else "medium"
                    if status == "fuzzy"
                    else "low"
                )
                if row_pass:
                    basis = (
                        "商品编码、商品名称和69码均与知识库严格一致。"
                        if status == "exact"
                        else "商品编码和69码严格一致，商品名称表述略有差异但可以对应。"
                    )
                    resubmission = None
                else:
                    differences: list[str] = []
                    corrections: list[str] = []
                    if product_code_match != "exact":
                        differences.append("商品编码不一致")
                        corrections.append(f"商品编码改为{knowledge_code}")
                    if barcode_match != "exact":
                        differences.append("69码不一致")
                        corrections.append(f"69码改为{knowledge_barcode}")
                    if name_match not in {"exact", "fuzzy"}:
                        differences.append("商品名称对不上")
                        corrections.append(f"商品名称改为能明确对应“{knowledge_name}”")
                    basis = "；".join(differences) + "。"
                    unchanged: list[str] = []
                    if product_code_match == "exact":
                        unchanged.append("商品编码")
                    if name_match in {"exact", "fuzzy"}:
                        unchanged.append("商品名称")
                    if barcode_match == "exact":
                        unchanged.append("69码")
                    resubmission = (
                        f"第{item['excel_row']}行重新导出时："
                        + "；".join(corrections)
                        + (f"；{'、'.join(unchanged)}不用改。" if unchanged else "。")
                    )
                barcode_basis = (
                    "现场照片先确定知识库商品，再将该商品登记的69码与销售Excel比较；"
                    + ("两边一致。" if barcode_match == "exact" else "两边不一致。")
                )
            product_checks.append(
                {
                    "knowledge_product_id": product_id,
                    "knowledge_product_code": knowledge_code,
                    "knowledge_product_code_aliases": knowledge_aliases,
                    "knowledge_product_name": knowledge_name,
                    "knowledge_barcode_69": knowledge_barcode,
                    "photo_match": "exact" if photo_identity_exact else "fuzzy",
                    "excel_row": int(item["excel_row"]),
                    "source_product_code": source_code,
                    "source_product_name": source_name,
                    "source_barcode_69": source_barcode,
                    "source_quantity": item.get("source_quantity"),
                    "product_code_match": product_code_match,
                    "name_match": name_match,
                    "name_similarity": round(name_score, 3) if name_score else None,
                    "barcode_match": barcode_match,
                    "status": status,
                    "confidence": confidence,
                    "barcode_comparison_basis": barcode_basis,
                    "basis": basis,
                    "resubmission": resubmission,
                }
            )
            hit_row_statuses.append(status)
        if hit_row_statuses and all(
            value in PASS_SALES_PRODUCT_MATCHES for value in hit_row_statuses
        ):
            matched_product_ids.add(product_id)

    ordered_names = list(
        dict.fromkeys(
            str(item.get("source_product_name") or "").strip()
            for item in product_checks
            if str(item.get("source_product_name") or "").strip()
        )
    )
    all_photo_exact = bool(product_reference_hits) and all(
        str(hit.get("confidence")) == "exact" for hit in product_reference_hits
    )
    all_products_have_valid_row = bool(product_reference_hits) and all(
        str(hit["reference_product_id"]) in matched_product_ids
        for hit in product_reference_hits
    )
    all_rows_exact = bool(product_checks) and all(
        str(check.get("status")) == "exact" for check in product_checks
    )
    if all_photo_exact and all_products_have_valid_row:
        status = "exact" if all_rows_exact else "fuzzy"
        basis = (
            "现场商品均已找到对应销售行；商品编码、商品名称和69码均严格一致。"
            if all_rows_exact
            else "现场商品均已找到对应销售行；商品编码和69码严格一致，商品名称模糊对应。"
        )
    elif product_reference_hits and not all_photo_exact:
        status = "candidate"
        basis = "现场商品只能模糊判断，相关销售行暂不能最终确认。"
    else:
        status = "unmatched"
        failed_rows = [
            str(item["excel_row"])
            for item in product_checks
            if item.get("excel_row") is not None
            and item.get("status") not in PASS_SALES_PRODUCT_MATCHES
        ]
        basis = "现场商品没有通过销售Excel核对"
        if failed_rows:
            basis += "；需更正Excel第" + "、".join(failed_rows) + "行"
        basis += "。"
    return {
        "names": ordered_names,
        "knowledge_product_ids": sorted(matched_product_ids),
        "product_checks": product_checks,
        "status": status,
        "basis": basis,
    }


def build_promotion_summary(review: dict[str, Any]) -> tuple[str, bool]:
    promotion = review.get("promotion_evidence") or {}
    signals = [
        str(item.get("text") or "").strip()
        for item in promotion.get("signals") or []
        if str(item.get("text") or "").strip()
    ]
    product_text = "；".join(str(value) for value in review.get("recognized_products") or [])
    canonical = _canonical_product(product_text)
    if "3+2" in canonical and not any("3+2" in value for value in signals):
        signals.append("3+2组合装")
    if "1+1" in canonical and not any("1+1" in value for value in signals):
        signals.append("1+1组合装")
    if any(token in canonical for token in ("超值装", "特享装", "量贩装")) and not signals:
        signals.append("包装标示超值/特享/量贩装")
    signals = list(dict.fromkeys(signals))
    prices = [str(value).strip() for value in promotion.get("visible_prices") or [] if str(value).strip()]
    limitations = [str(value).strip() for value in promotion.get("limitations") or [] if str(value).strip()]
    if signals:
        summary = "有促销：" + "；".join(signals)
        if prices:
            summary += "；可见" + "、".join(prices) + "价签"
        return summary, True
    if prices:
        return "未识别到明确促销词或组合装；仅见商品陈列及" + "、".join(prices) + "售价", False
    if limitations:
        return "无法判断：" + "；".join(limitations), False
    return "未识别到明确促销词或组合装", False


def _period_label(visible_date: Any, start: date, end: date) -> tuple[str, bool]:
    if not visible_date:
        return "unverifiable", False
    try:
        value = date.fromisoformat(str(visible_date))
    except ValueError:
        return "unverifiable", False
    return ("match", True) if start <= value <= end else ("mismatch", False)


def _photo_path(images_by_name: dict[str, Path], value: Any) -> Path | None:
    return images_by_name.get(Path(str(value)).name.casefold())


def _duplicate_maps(
    photo_inventory: dict[str, Any],
    review_lines_by_file: dict[str, set[int]],
) -> tuple[set[str], set[str], list[dict[str, Any]]]:
    exact = {
        name.casefold()
        for group in photo_inventory.get("exact_duplicate_groups") or []
        for name in group
    }
    possible: set[str] = set()
    cross_store_pairs: list[dict[str, Any]] = []
    for pair in photo_inventory.get("phash_candidate_pairs") or []:
        names = [str(value) for value in pair.get("files") or []]
        lines: set[int] = set()
        for name in names:
            lines.update(review_lines_by_file.get(name.casefold(), set()))
        if len(lines) > 1:
            possible.update(name.casefold() for name in names)
            cross_store_pairs.append(pair)
    return exact, possible, cross_store_pairs


def _default_review(line_no: int, store_name: str) -> dict[str, Any]:
    return {
        "store_line_no": line_no,
        "contract_store_name": store_name,
        "photo_files": [],
        "visible_date": None,
        "visible_location": None,
        "location_basis": "未提交该合同门店的结构化照片核验结果",
        "display_observation": {
            "standard_evidence": "unclear",
            "matched_standard": "unclear",
            "vertical_facing_count": None,
            "vertical_facing_basis": [],
            "stack_1sqm_basis": None,
            "description": "未提交照片",
            "limitations": ["未提交照片"],
        },
        "visible_text": [],
        "recognized_products": ["未能可靠识别具体产品"],
        "product_reference_hits": [],
        "promotion_evidence": {"signals": [], "visible_prices": [], "limitations": ["未提交照片"]},
        "risk_notes": ["未提交该合同门店的照片"],
        "supplement_advice": ["补充带门头、完整日期地点水印和完整陈列的原始照片。"],
    }


def audit_display_case(case: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    sales_path = Path(case["sales_excel"]).resolve()
    contract_path = Path(case["contract_pdf"]).resolve()
    photo_paths = [Path(value).resolve() for value in case["photo_files"]]
    sales = read_display_sales(sales_path)
    product_rag = load_product_rag(DISPLAY_SKILL_DIR)
    sales_knowledge = _sales_catalog_reconciliation(sales["records"], product_rag)
    sales["knowledge_status"] = sales_knowledge["status"]
    sales["knowledge_matched_count"] = sales_knowledge["matched_count"]
    sales["knowledge_fuzzy_count"] = sales_knowledge["fuzzy_count"]
    sales["knowledge_problem_count"] = sales_knowledge["problem_count"]
    sales["knowledge_problem_rows"] = sales_knowledge["problem_rows"]
    sales["knowledge_basis"] = sales_knowledge["basis"]
    sales["knowledge_reconciliation"] = sales_knowledge["records"]
    photos = image_file_inventory(photo_paths, directory=case.get("source_root"))
    pdf = pdf_inventory(contract_path)
    contract = evidence["contract"]
    contract_knowledge = _contract_product_knowledge_reconciliation(contract, product_rag)
    contract_sales = _contract_sales_reconciliation(contract, sales)
    stores = sorted(contract["stores"], key=lambda item: int(item["line_no"]))
    store_map = unique_by(stores, "line_no", "contract store line number")
    review_map = unique_by(evidence.get("photo_reviews") or [], "store_line_no", "photo review store line number")
    images_by_name = {path.name.casefold(): path for path in photo_paths}
    review_lines_by_file: dict[str, set[int]] = {}
    for review in evidence.get("photo_reviews") or []:
        for name in review.get("photo_files") or []:
            review_lines_by_file.setdefault(Path(name).name.casefold(), set()).add(int(review["store_line_no"]))
    exact_names, possible_names, cross_store_pairs = _duplicate_maps(photos, review_lines_by_file)

    fee = money(contract["fee_per_store"], label="contract unit fee")
    claimed = money(contract["claimed_amount"], label="claimed amount")
    activity_budget = (
        money(contract["activity_budget"], label="activity budget")
        if contract.get("activity_budget") is not None
        else None
    )
    fee_basis = str(contract.get("fee_basis") or "unclear")
    start = date.fromisoformat(contract["activity_start"])
    end = date.fromisoformat(contract["activity_end"])
    exceptions: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    referenced: set[str] = set()
    if contract_knowledge["status"] == "fail":
        exceptions.append(
            exception(
                "high",
                "CONTRACT_PRODUCT_KNOWLEDGE_MISMATCH",
                contract_knowledge["basis"],
                "合同限定商品没有权威知识库身份，无法继续产品核销。",
                "修正合同商品描述或补齐知识库后重新核销。",
                source=Path(str(contract.get("source_file") or contract_path)).name,
            )
        )
    if contract_sales["status"] != "pass":
        exceptions.append(
            exception(
                "high",
                "CONTRACT_SALES_RECONCILIATION_FAILED",
                "合同与销售Excel未通过签订方、业务日期和合同完整性核验。",
                "合同与销售明细之间的主体或期间链路不能闭环。",
                "核对合同签订方与销售客户、合同执行周期与业务日期，并补充带水印或盖章的合同。",
                source=(
                    f"{Path(str(contract.get('source_file') or contract_path)).name} / "
                    f"{Path(str(sales.get('source_file') or sales_path)).name}"
                ),
            )
        )

    for line_no, store in store_map.items():
        review = review_map.get(line_no) or _default_review(int(line_no), str(store["store_name"]))
        if normalize_text(review["contract_store_name"]) != normalize_text(store["store_name"]):
            exceptions.append(
                exception(
                    "high",
                    "PHOTO_REVIEW_CONTRACT_STORE_CONFLICT",
                    f"合同第{line_no}家门店与照片核验行名称不一致。",
                    "照片可能绑定到错误门店。",
                    "按合同序号重新绑定照片。",
                )
            )
        photo_files = [Path(str(value)).name for value in review.get("photo_files") or []]
        referenced.update(name.casefold() for name in photo_files)
        missing = [name for name in photo_files if _photo_path(images_by_name, name) is None]
        file_pass = bool(photo_files) and not missing
        period_match, period_pass = _period_label(review.get("visible_date"), start, end)
        store_match, deterministic_location_basis = _determine_store_match(
            str(store["store_name"]),
            review.get("visible_location"),
            photo_files,
        )
        store_match_basis = (
            str(review.get("location_basis") or "") + "；" + deterministic_location_basis
        ).lstrip("；")
        store_pass = store_match in PASS_STORE_MATCHES
        observation = review.get("display_observation") or {}
        display_match, display_standard_basis = _display_control(observation)
        display_pass = display_match == "pass"

        bound = {name.casefold() for name in photo_files}
        if not photo_files:
            duplicate_check = "unverifiable"
        elif bound & exact_names:
            duplicate_check = "exact"
        elif bound & possible_names:
            duplicate_check = "possible"
        else:
            duplicate_check = "none"
        duplicate_pass = duplicate_check == "none"

        recognized_visible = [str(value) for value in review.get("recognized_products") or []]
        raw_reference_hits = list(review.get("product_reference_hits") or [])
        reference_hits = resolve_product_reference_hits(
            raw_reference_hits,
            product_rag,
        )
        reference_hits = apply_visible_short_code_exact_hits(
            reference_hits,
            [str(value) for value in review.get("visible_text") or []],
            product_rag,
        )
        reference_labels = [product_reference_label(item) for item in reference_hits]
        recognized = reference_labels or recognized_visible
        photo_knowledge_match, photo_knowledge_basis = _photo_knowledge_control(reference_hits)
        correspondence = sales_product_correspondence(
            sales_knowledge["records"],
            reference_hits,
        )
        promotion_summary, promotion_present = build_promotion_summary(review)
        contract_sales_pass = contract_sales["status"] == "pass"
        contract_knowledge_pass = contract_knowledge["status"] in {"pass", "not_applicable"}
        photo_knowledge_pass = photo_knowledge_match == "exact"
        internal_product_pass = correspondence["status"] in PASS_SALES_PRODUCT_MATCHES
        product_pass = all(
            [
                contract_knowledge_pass,
                photo_knowledge_pass,
                internal_product_pass,
            ]
        )
        if contract.get("requires_specific_products"):
            required_ids = set(contract_knowledge["knowledge_product_ids"])
            exact_photo_ids = {
                str(item["reference_product_id"])
                for item in reference_hits
                if str(item.get("confidence")) == "exact"
            }
            sales_ids = set(correspondence["knowledge_product_ids"])
            product_pass = product_pass and required_ids.issubset(exact_photo_ids & sales_ids)
        promotion_pass = not contract.get("requires_promotion") or promotion_present
        store_stack_count = store.get("stack_count")
        if fee_basis == "per_store":
            claim_units: int | None = 1
            amount_rule_status = "pass"
            amount_rule_basis = f"合同明确按店核销：1店×{json_number(fee)}元。"
        elif fee_basis == "per_stack" and store_stack_count is not None:
            claim_units = int(store_stack_count)
            amount_rule_status = "pass"
            amount_rule_basis = (
                f"合同明确按堆头核销：{claim_units}个×{json_number(fee)}元。"
            )
        elif fee_basis == "per_stack":
            claim_units = None
            amount_rule_status = "manual"
            amount_rule_basis = "合同按堆头计费，但该门店堆头数量未明确，不能自动分摊。"
        else:
            claim_units = None
            amount_rule_status = "manual"
            amount_rule_basis = "合同仅有总额或计费口径不清，系统不按门店自动分摊。"
        amount_rule_pass = amount_rule_status == "pass"
        passed = all(
            [
                contract_sales_pass,
                file_pass,
                period_pass,
                store_pass,
                display_pass,
                duplicate_pass,
                product_pass,
                promotion_pass,
                amount_rule_pass,
            ]
        )

        risks = list(review.get("risk_notes") or [])
        advice = list(review.get("supplement_advice") or [])
        if not contract_sales_pass:
            risks.append("合同与销售Excel的签订方、业务日期或合同完整性未闭环")
            advice.append("核对合同签订方/销售客户、执行周期/业务日期，并补充带水印或盖章的合同。")
        if not contract_knowledge_pass:
            risks.append(contract_knowledge["basis"])
            advice.append("先让合同限定商品唯一对应正式知识库，再重新核销。")
        if photo_knowledge_match == "candidate":
            risks.append("现场照片与商品知识库只有模糊匹配，置信度中")
            advice.append("补拍清晰69码、唯一商品短码或足以确认具体商品的包装照片。")
        elif photo_knowledge_match == "unmatched":
            risks.append("现场照片与商品知识库完全不匹配，置信度低")
            advice.append("补拍能与商品知识库对应的商品正面、侧面或69码。")
        if correspondence["status"] not in PASS_SALES_PRODUCT_MATCHES:
            risks.append(correspondence["basis"])
            advice.extend(
                str(check["resubmission"])
                for check in correspondence["product_checks"]
                if check.get("resubmission")
            )
        if missing:
            risks.append("核验引用了不存在的照片：" + "、".join(missing))
            advice.append("补齐缺失的原始现场照片。")
        if not period_pass:
            advice.append("补充活动期内且完整日期可见的原始现场照片。")
        if not store_pass:
            advice.append("补充可见合同门店名称/地址的照片或权威门店映射。")
        if not display_pass:
            advice.append("补充能看清完整堆头面积或纵向陈列数量的全景照片。")
        if duplicate_check in {"exact", "possible"}:
            advice.append("提供该门店独立原始照片并说明跨门店复用或近似照片的原因。")
        if contract.get("requires_specific_products") and not product_pass:
            advice.append("补充合同指定产品清晰可见的现场照片。")
        if not promotion_pass:
            advice.append("补充合同指定促销形式清晰可见的现场照片。")
        if not amount_rule_pass:
            risks.append(amount_rule_basis)
            advice.append("补充明确的按店/按堆头单价及对应数量后再计算核销金额。")
        advice = list(dict.fromkeys(advice))
        supported = fee * int(claim_units) if passed and claim_units is not None else Decimal("0")
        results.append(
            {
                "store_line_no": int(line_no),
                "contract_store_name": store["store_name"],
                "photo_files": photo_files,
                "photo_count": len(photo_files),
                "visible_date": review.get("visible_date"),
                "visible_location": review.get("visible_location"),
                "visible_text": [str(value) for value in review.get("visible_text") or []],
                "period_match": period_match,
                "store_match": store_match,
                "store_match_basis": store_match_basis,
                "display_match": display_match,
                "display_standard_basis": display_standard_basis,
                "display_description": observation.get("description"),
                "display_vertical_facing_count": observation.get("vertical_facing_count"),
                "display_vertical_facing_basis": list(
                    observation.get("vertical_facing_basis") or []
                ),
                "display_stack_1sqm_basis": observation.get("stack_1sqm_basis"),
                "duplicate_check": duplicate_check,
                "visible_recognized_products": (
                    recognized_visible or ["未能可靠识别具体产品"]
                ),
                "recognized_products": recognized or ["未能可靠识别具体产品"],
                "product_reference_hits": reference_hits,
                "photo_knowledge_match": photo_knowledge_match,
                "photo_knowledge_match_basis": photo_knowledge_basis,
                "promotion_summary": promotion_summary,
                "promotion_present": promotion_present,
                "sales_product_names": correspondence["names"],
                "sales_product_knowledge_ids": correspondence["knowledge_product_ids"],
                "sales_product_match": correspondence["status"],
                "sales_product_match_basis": correspondence["basis"],
                "sales_product_checks": correspondence["product_checks"],
                "contract_sales_status": contract_sales["status"],
                "contract_stack_count": (
                    int(store_stack_count) if store_stack_count is not None else None
                ),
                "claim_units": claim_units,
                "amount_rule_status": amount_rule_status,
                "amount_rule_basis": amount_rule_basis,
                "status": "pass" if passed else "supplement",
                "supported_amount": json_number(supported),
                "risk_notes": risks,
                "supplement_advice": advice,
            }
        )
        if not passed:
            potential_amount = (
                fee * int(claim_units) if claim_units is not None else Decimal("0")
            )
            exceptions.append(
                exception(
                    "high",
                    "STORE_DISPLAY_EVIDENCE_INCOMPLETE",
                    f"合同第{line_no}家“{store['store_name']}”存在未通过的强制条件。",
                    f"暂不支持该店{json_number(potential_amount)}元。",
                    "；".join(advice),
                    source="、".join(photo_files) or f"contract store line {line_no}",
                )
            )

    unknown_lines = sorted(set(review_map) - set(store_map))
    if unknown_lines:
        exceptions.append(
            exception(
                "high", "PHOTO_REVIEW_UNKNOWN_CONTRACT_LINE",
                f"照片核验包含合同不存在的门店序号：{unknown_lines}。",
                "可能存在错绑或多报。", "按合同原始门店序号重新提交。"
            )
        )
    unreferenced = sorted(path.name for path in photo_paths if path.name.casefold() not in referenced)
    if unreferenced:
        exceptions.append(
            exception(
                "medium", "UNREFERENCED_PHOTO_FILES",
                f"有{len(unreferenced)}张照片未绑定合同门店。",
                "材料可能遗漏核验。", "确认照片用途并绑定或移出材料包。",
                source="、".join(unreferenced),
            )
        )
    if photos.get("exact_duplicate_groups"):
        exceptions.append(
            exception(
                "high", "EXACT_DUPLICATE_PHOTOS", "发现SHA-256完全重复照片。",
                "可能存在重复核销。", "补交对应门店的独立原始照片。"
            )
        )
    if cross_store_pairs:
        exceptions.append(
            exception(
                "high", "POSSIBLE_CROSS_STORE_PHOTO_REUSE", "发现跨门店pHash近似重复候选。",
                "可能存在跨店复用。", "人工并排复核并补交独立原图。"
            )
        )
    if photos["files_with_exif_datetime"] == 0 or photos["files_with_gps"] == 0:
        exceptions.append(
            exception(
                "medium", "PHOTO_ORIGINAL_METADATA_ABSENT",
                "照片缺少可用EXIF拍摄时间或GPS。", "无法用原始元数据独立验证时空。",
                "保留原始拍摄文件或补充平台定位记录。"
            )
        )
    if not sales["store_level_available"]:
        exceptions.append(
            exception(
                "medium", "SALES_DATA_NOT_STORE_LEVEL",
                "销售Excel为客户汇总，不能拆分到合同门店。",
                "只能佐证期间总体销售，不能单独证明某店。",
                "如政策要求，补交门店级POS或出库明细。",
            )
        )

    supported = sum((money(item["supported_amount"]) for item in results), Decimal("0"))
    approval_caps = [claimed, supported]
    if activity_budget is not None:
        approval_caps.append(activity_budget)
    suggested = min(approval_caps)
    passed_count = sum(item["status"] == "pass" for item in results)
    supplement_count = len(results) - passed_count
    expected_units: int | None
    if fee_basis == "per_store":
        expected_units = len(stores)
    elif fee_basis == "per_stack":
        if contract.get("contract_stack_count") is not None:
            expected_units = int(contract["contract_stack_count"])
        elif all(store.get("stack_count") is not None for store in stores):
            expected_units = sum(int(store["stack_count"]) for store in stores)
        else:
            expected_units = None
    else:
        expected_units = None
    expected_claim = fee * expected_units if expected_units is not None else None
    if expected_claim is not None and claimed != expected_claim:
        exceptions.append(
            exception(
                "high", "CONTRACT_CLAIM_CALCULATION_MISMATCH",
                f"申报金额{claimed}不等于{expected_units}个计费单位×{fee}元={expected_claim}元。",
                "合同计费单位数、单价或申报金额不一致。", "更正合同或申报明细。"
            )
        )
    conclusion = (
        "pass" if results and not supplement_count
        else "partial_pass" if passed_count
        else "human_review"
    )
    return {
        "schema_version": "2.1",
        "generated_at": now_utc(),
        "scenario": "promotional_display",
        "case_id": str(case.get("case_id") or Path(case["source_archive"]).stem),
        "case_name": str(case.get("case_name") or contract.get("customer_name") or "堆头核销"),
        "summary": {
            "conclusion": conclusion,
            "claimed_amount": json_number(claimed),
            "activity_budget": (
                json_number(activity_budget) if activity_budget is not None else None
            ),
            "contract_store_count": len(stores),
            "fee_per_store": json_number(fee),
            "fee_basis": fee_basis,
            "contract_stack_count": contract.get("contract_stack_count"),
            "expected_contract_amount": (
                json_number(expected_claim) if expected_claim is not None else None
            ),
            "passed_store_count": passed_count,
            "supplement_store_count": supplement_count,
            "supported_amount": json_number(supported),
            "suggested_approved_amount": json_number(suggested),
            "temporarily_held_amount": json_number(claimed - suggested),
            "photo_count": photos["file_count"],
            "sales_sku_count": sales["sku_count"],
            "sales_quantity": sales["total_quantity"],
            "sales_retail_amount": sales["retail_amount"],
            "sales_knowledge_matched_count": sales_knowledge["matched_count"],
            "sales_knowledge_fuzzy_count": sales_knowledge["fuzzy_count"],
            "sales_knowledge_problem_count": sales_knowledge["problem_count"],
            "high_exception_count": sum(item["severity"] == "high" for item in exceptions),
            "medium_exception_count": sum(item["severity"] == "medium" for item in exceptions),
        },
        "contract": contract,
        "contract_product_knowledge": contract_knowledge,
        "contract_sales_reconciliation": contract_sales,
        "contract_pdf": pdf,
        "sales": sales,
        "photo_inventory": photos,
        "store_reconciliation": results,
        "unreferenced_photos": unreferenced,
        "exceptions": exceptions,
    }
