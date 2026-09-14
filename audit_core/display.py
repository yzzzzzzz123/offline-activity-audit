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
from .location_resolution import (
    LocationResolver,
    city_hint_from_values,
    default_location_resolver,
    deterministic_location_resolution,
)
from .product_rag import (
    FIELD_SHORT_CODE_PATTERN,
    PRODUCT_EXISTENCE_NAME_THRESHOLD,
    apply_visible_catalog_text_exact_hits,
    catalog_product_name_values,
    ean13_is_valid,
    product_name_similarity,
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
    if seal is True:
        integrity_status = "pass"
        integrity_basis = "合同盖章清晰可见；水印状态仅记录，不参与核销。"
    elif seal is False:
        integrity_status = "fail"
        integrity_basis = "合同明确未见盖章，完整性门禁不通过；水印不参与核销。"
    else:
        integrity_status = "unverifiable"
        integrity_basis = "合同盖章无法确认，完整性门禁待核验；水印不参与核销。"

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


def _contract_core_reconciliation(
    contract: dict[str, Any],
    contract_sales: dict[str, Any],
) -> dict[str, Any]:
    """Audit every mandatory core-contract field as its own visible control."""

    def text_is_explicit(value: Any) -> bool:
        text = str(value or "").strip()
        return bool(text) and not any(
            marker in text
            for marker in ("未识别", "无法识别", "不清楚", "不明确")
        )

    party_status = str(contract_sales.get("customer_status") or "unverifiable")
    party_control = (
        "pass"
        if party_status in {"exact", "fuzzy"}
        else "fail"
        if party_status == "mismatch"
        else "unverifiable"
    )
    parties = [
        str(value).strip()
        for value in contract.get("contract_parties") or []
        if str(value).strip()
    ]
    sales_customers = [
        str(value).strip()
        for value in contract_sales.get("sales_customers") or []
        if str(value).strip()
    ]

    budget = contract.get("activity_budget")
    claimed = contract.get("claimed_amount")
    if budget is None:
        budget_control = "unverifiable"
        budget_basis = "合同未识别到明确活动预算金额。"
    elif claimed is not None and _decimal_value(claimed) is not None and _decimal_value(budget) is not None:
        if _decimal_value(claimed) <= _decimal_value(budget):
            budget_control = "pass"
            budget_basis = "活动预算金额明确，且申报金额未超过预算。"
        else:
            budget_control = "fail"
            budget_basis = "申报金额超过合同活动预算金额。"
    else:
        budget_control = "unverifiable"
        budget_basis = "活动预算金额或申报金额无法完整核对。"

    period_status = str(contract_sales.get("period_status") or "unverifiable")
    period_control = (
        "pass"
        if period_status == "covered"
        else "fail"
        if period_status == "mismatch"
        else "unverifiable"
    )

    activity_content = str(contract.get("activity_content") or "").strip()
    content_control = "pass" if text_is_explicit(activity_content) else "unverifiable"
    content_basis = (
        "合同已明确写出活动内容。"
        if content_control == "pass"
        else "合同活动内容缺失、模糊或无法识别。"
    )

    settlement_method = str(contract.get("settlement_method") or "").strip()
    fee_basis = str(contract.get("fee_basis") or "unclear")
    method_control = (
        "pass"
        if text_is_explicit(settlement_method) and fee_basis != "unclear"
        else "unverifiable"
    )
    method_basis = (
        "合同核销方式原文清楚，且确定性规则已识别计费口径。"
        if method_control == "pass"
        else "合同核销方式缺失、模糊或无法形成明确计费口径。"
    )

    seal = contract.get("seal_visible")
    seal_control = "pass" if seal is True else "fail" if seal is False else "unverifiable"

    checks = [
        {
            "field": "contracting_party",
            "label": "合同签订方",
            "contract_value": "、".join(parties) or "未识别",
            "comparison_value": "、".join(sales_customers) or None,
            "status": party_control,
            "basis": str(contract_sales.get("customer_basis") or "合同签订方未核验。"),
        },
        {
            "field": "activity_budget",
            "label": "活动预算金额",
            "contract_value": (
                f"{json_number(budget)}元" if budget is not None else "未识别"
            ),
            "comparison_value": (
                f"申报{json_number(claimed)}元" if claimed is not None else None
            ),
            "status": budget_control,
            "basis": budget_basis,
        },
        {
            "field": "execution_period",
            "label": "执行周期",
            "contract_value": (
                f"{contract.get('activity_start') or '未识别'} 至 "
                f"{contract.get('activity_end') or '未识别'}"
            ),
            "comparison_value": "、".join(
                str(value) for value in contract_sales.get("sales_period_values") or []
            ) or None,
            "status": period_control,
            "basis": str(contract_sales.get("period_basis") or "合同执行周期未核验。"),
        },
        {
            "field": "activity_content",
            "label": "活动内容",
            "contract_value": activity_content or "未识别",
            "comparison_value": None,
            "status": content_control,
            "basis": content_basis,
        },
        {
            "field": "settlement_method",
            "label": "核销方式",
            "contract_value": settlement_method or "未识别",
            "comparison_value": fee_basis,
            "status": method_control,
            "basis": method_basis,
        },
        {
            "field": "seal",
            "label": "盖章",
            "contract_value": "可见" if seal is True else "未见" if seal is False else "无法确认",
            "comparison_value": None,
            "status": seal_control,
            "basis": (
                "合同盖章清晰可见。"
                if seal is True
                else "合同未见盖章。"
                if seal is False
                else "合同盖章状态无法确认。"
            ),
        },
    ]
    problem_fields = [
        str(item["field"]) for item in checks if item["status"] != "pass"
    ]
    statuses = {str(item["status"]) for item in checks}
    status = (
        "fail"
        if "fail" in statuses
        else "unverifiable"
        if "unverifiable" in statuses
        else "pass"
    )
    return {
        "status": status,
        "source_file": str(contract.get("source_file") or ""),
        "field_count": len(checks),
        "passed_count": sum(item["status"] == "pass" for item in checks),
        "problem_count": len(problem_fields),
        "problem_fields": problem_fields,
        "field_checks": checks,
        "basis": (
            "合同核心六项全部核清。"
            if status == "pass"
            else "合同核心六项存在不通过或无法核对字段："
            + "、".join(
                str(item["label"])
                for item in checks
                if item["status"] != "pass"
            )
            + "。"
        ),
    }


def _determine_store_match(
    contract_name: str,
    contract_address: str | None,
    visible_location: str | None,
    resolver: LocationResolver,
) -> tuple[str, str, dict[str, Any]]:
    if not visible_location:
        basis = "照片水印未识别到独立地点，文件名不能单独证明门店"
        return (
            "filename_only",
            basis,
            deterministic_location_resolution(
                status="not_applicable",
                contract_name=contract_name,
                visible_location=visible_location,
                basis=basis,
            ),
        )

    contract_text = _canonical_location(contract_name)
    visible_text = _canonical_location(visible_location)
    if (
        contract_text
        and (contract_text == visible_text or contract_text in visible_text)
        and not _numbered_store_conflict(contract_name, visible_location)
    ):
        basis = "照片水印地点完整包含合同门店名称，无需调用地图MCP"
        return (
            "exact",
            basis,
            deterministic_location_resolution(
                status="not_needed",
                contract_name=contract_name,
                visible_location=visible_location,
                basis=basis,
            ),
        )

    location_resolution = resolver.resolve(
        contract_name=contract_name,
        contract_address=contract_address,
        visible_location=visible_location,
        city_hint=city_hint_from_values(
            visible_location,
            contract_address,
            contract_name,
        ),
    )
    resolution_status = str(location_resolution.get("status") or "unavailable")
    basis = str(location_resolution.get("basis") or "地点解析没有返回可审计依据")
    if resolution_status in {"same_place", "parent_child", "nearby"}:
        return "compatible", basis, location_resolution
    if resolution_status == "unrelated":
        return "location_unverified", basis, location_resolution
    return "location_unverified", basis, location_resolution


def _canonical_product(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    text = text.replace("參半", "参半")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff+%]+", "", text)


def _strict_identity_key(value: Any) -> str:
    """Preserve a formal business identifier for exact source comparison."""

    return str(value or "").strip()


def _product_grams(value: Any) -> set[str]:
    text = _canonical_product(value)
    for token in ("参半", "oralshark", "牙膏", "组合装", "超值装", "特享装", "量贩装"):
        text = text.replace(token, "")
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _similarity(left: Any, right: Any) -> float:
    return product_name_similarity(left, right)


def _catalog_name_values(product: dict[str, Any]) -> list[str]:
    return catalog_product_name_values(product)


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


def _best_fuzzy_product(
    value: Any,
    products: list[dict[str, Any]],
    *,
    threshold: float = PRODUCT_EXISTENCE_NAME_THRESHOLD,
) -> tuple[dict[str, Any] | None, float]:
    """同码多候选只按数据库完整名称消歧，不使用历史编码别名或图片描述。"""

    ranked = sorted(
        (
            (
                _similarity(value, str(product.get("product_name") or "")),
                product,
            )
            for product in products
        ),
        key=lambda item: (-item[0], str(item[1].get("product_id") or "")),
    )
    if not ranked or ranked[0][0] <= threshold:
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


def _strict_catalog_product_codes(product: dict[str, Any]) -> set[str]:
    """Return formal business codes, excluding packaging-only short codes."""

    values = {str(product.get("product_code") or "").strip()}
    return {value for value in values if value}


def _product_records_catalog_reconciliation(
    source_records: list[dict[str, Any]],
    catalog: dict[str, Any],
    *,
    source_label: str = "合同附件",
) -> dict[str, Any]:
    """将合同附件商品与本次数据库快照对账。

    69码和商品编码严格对应数据库三字段；正式数据源没有别名。
    来源编码原样保留用于合同到Excel比较，名称仅作模糊辅助证据。
    """

    products = list(catalog.get("products") or [])
    by_id = {str(product["product_id"]): product for product in products}
    reconciled: list[dict[str, Any]] = []

    for record in source_records:
        source_values = {
            "product_code": str(record.get("product_code") or "").strip(),
            "product_name": str(record.get("product_name") or "").strip(),
            "barcode_69": str(record.get("barcode") or "").strip(),
        }
        missing_fields = [
            field
            for field in ("product_code", "barcode_69")
            if not source_values[field]
        ]
        invalid_barcode = bool(source_values["barcode_69"]) and not ean13_is_valid(
            source_values["barcode_69"]
        )
        field_candidates: dict[str, set[str]] = {
            "product_code": {
                str(product["product_id"])
                for product in products
                if source_values["product_code"]
                and source_values["product_code"] in _strict_catalog_product_codes(product)
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
        selected: dict[str, Any] | None = None
        diagnostic_selected: dict[str, Any] | None = None
        name_match_type = "unmatched"
        fuzzy_score = 0.0

        strict_identity_ids = code_ids & barcode_ids
        if not missing_fields and not invalid_barcode and strict_identity_ids:
            exact_barcode_names = strict_identity_ids & exact_name_ids
            if exact_barcode_names:
                selected = by_id[sorted(exact_barcode_names)[0]]
                name_match_type = "exact"
                fuzzy_score = 1.0
            elif len(strict_identity_ids) == 1:
                selected = by_id[next(iter(strict_identity_ids))]
                name_match_type = "fuzzy"
                fuzzy_score = _catalog_name_score(
                    source_values["product_name"], selected
                ) if source_values["product_name"] else 0.0
            else:
                selected, fuzzy_score = _best_fuzzy_product(
                    source_values["product_name"],
                    [
                        by_id[product_id]
                        for product_id in sorted(strict_identity_ids)
                    ],
                    threshold=PRODUCT_EXISTENCE_NAME_THRESHOLD,
                )
                if selected is not None:
                    name_match_type = "fuzzy"

        diagnostic_selected = selected
        if diagnostic_selected is None and barcode_ids:
            exact_barcode_names = barcode_ids & exact_name_ids
            if len(exact_barcode_names) == 1:
                diagnostic_selected = by_id[next(iter(exact_barcode_names))]
                name_match_type = "exact"
                fuzzy_score = 1.0
            elif source_values["product_name"]:
                diagnostic_selected, fuzzy_score = _best_fuzzy_product(
                    source_values["product_name"],
                    [by_id[product_id] for product_id in sorted(barcode_ids)],
                    threshold=PRODUCT_EXISTENCE_NAME_THRESHOLD,
                )
                if diagnostic_selected is not None:
                    name_match_type = "fuzzy"
        if diagnostic_selected is None and len(barcode_ids) == 1:
            diagnostic_selected = by_id[next(iter(barcode_ids))]
        elif diagnostic_selected is None and len(code_ids) == 1:
            diagnostic_selected = by_id[next(iter(code_ids))]
        elif diagnostic_selected is None and len(exact_name_ids) == 1:
            diagnostic_selected = by_id[next(iter(exact_name_ids))]

        selected_id = (
            str(diagnostic_selected["product_id"])
            if diagnostic_selected is not None
            else None
        )
        if diagnostic_selected is not None and name_match_type == "fuzzy":
            field_candidates["product_name"].add(
                str(diagnostic_selected["product_id"])
            )

        matched_fields: list[str] = []
        unmatched_fields: list[str] = []
        conflicting_fields: list[str] = []
        field_comparisons: list[dict[str, Any]] = []
        for field, source_value in source_values.items():
            candidates = field_candidates[field]
            if not source_value:
                comparison = "missing"
            elif field == "product_code" and code_ids:
                comparison = "matched"
            elif field == "product_code":
                comparison = "conflict" if diagnostic_selected is not None else "not_found"
            elif field == "barcode_69" and invalid_barcode:
                comparison = "invalid"
            elif field == "barcode_69" and barcode_ids:
                comparison = "matched"
            elif (
                field == "product_name"
                and diagnostic_selected is not None
                and name_match_type != "exact"
            ):
                comparison = "fuzzy"
            elif (
                field == "product_name"
                and diagnostic_selected is not None
                and name_match_type == "exact"
            ):
                comparison = "matched"
            elif field == "product_name" and not candidates:
                comparison = "not_found"
            elif field == "product_name":
                comparison = "conflict"
            else:
                comparison = "not_found"

            if comparison in {"matched", "fuzzy"}:
                matched_fields.append(field)
            elif comparison == "not_found":
                unmatched_fields.append(field)
            elif comparison in {"conflict", "invalid"}:
                conflicting_fields.append(field)

            selected_knowledge_value = (
                str(diagnostic_selected[field])
                if diagnostic_selected is not None
                else None
            )
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
        elif not barcode_ids:
            status = "unmatched"
        elif not code_ids or not strict_identity_ids:
            status = "conflict"
        elif selected is not None and name_match_type == "exact":
            status = "matched"
        elif selected is not None and name_match_type == "fuzzy":
            status = "fuzzy_matched"
        else:
            status = "conflict"

        if status == "unmatched":
            basis = (
                f"{source_label}第{record['excel_row']}行的有效69码"
                "未命中正式商品知识库。"
            )
        elif status == "conflict":
            details: list[str] = []
            if invalid_barcode:
                details.append("69码未通过EAN-13校验")
            if missing_fields:
                details.append(
                    "缺少"
                    + "、".join(
                        KNOWLEDGE_FIELD_LABELS[field] for field in missing_fields
                    )
                )
            if source_values["product_code"] and not code_ids:
                details.append(
                    "产品编码未在本次数据库商品资料中登记"
                )
            if code_ids and barcode_ids and not strict_identity_ids:
                details.append("产品编码和69码没有共同命中同一个知识库商品")
            if (
                strict_identity_ids
                and selected is None
                and not source_values["product_name"]
            ):
                details.append(
                    "合同PDF商品名称未识别，无法在同69码商品中完成模糊确认"
                )
            if (
                strict_identity_ids
                and selected is None
                and source_values["product_name"]
            ):
                details.append("商品名称模糊相似度未严格高于0.5")
            basis = (
                f"{source_label}第{record['excel_row']}行未通过商品存在核验；"
                + "；".join(details or ["产品编码与69码未共同满足知识库核验条件"])
            )
        else:
            assert selected is not None
            name_basis = (
                "商品名称精确匹配"
                if status == "matched"
                else "商品名称模糊相似度严格高于0.5"
            )
            basis = (
                f"{source_label}第{record['excel_row']}行产品编码与69码精确匹配，{name_basis}。"
            )

        nonempty_sets = [values for values in field_candidates.values() if values]
        candidate_id_set = set().union(*nonempty_sets) if nonempty_sets else set()
        if selected_id:
            candidate_id_set.add(selected_id)
        reconciled.append(
            {
                "excel_row": int(record["excel_row"]),
                "source_product_code": source_values["product_code"],
                "source_product_name": source_values["product_name"],
                "source_barcode_69": source_values["barcode_69"],
                "source_quantity": record.get("quantity"),
                "source_unit": record.get("unit"),
                "source_retail_price": record.get("retail_price"),
                "source_total_amount": record.get("total_amount"),
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
                "candidate_product_ids": sorted(candidate_id_set),
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
            f"{matched_count + fuzzy_count}/{len(reconciled)}行通过商品存在核验"
            f"（69码精确，名称精确{matched_count}行、模糊>0.5共{fuzzy_count}行）"
            + (f"；问题行：{problem_rows}" if problem_rows else "；全部通过")
        ),
    }

def _contract_attachment_knowledge_reconciliation(
    contract: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Resolve a stamped contract sales attachment against the product catalog.

    The attachment is evidence supplied inside the signed contract package, but
    it is not itself a narrow contractual SKU requirement.  It therefore has a
    separate, conditional gate from ``required_product_identities``.
    """

    attachment = contract.get("sales_attachment") or {}
    if not attachment.get("present"):
        return {
            "status": "not_applicable",
            "record_count": 0,
            "matched_count": 0,
            "fuzzy_count": 0,
            "problem_count": 0,
            "problem_rows": [],
            "records": [],
            "basis": "合同包未附商品销售明细，本项不核验。",
        }
    source_records = list(attachment.get("records") or [])
    if not source_records:
        return {
            "status": "fail",
            "record_count": 0,
            "matched_count": 0,
            "fuzzy_count": 0,
            "problem_count": 1,
            "problem_rows": [],
            "records": [],
            "basis": "合同标记为含销售明细附件，但没有识别到任何商品行。",
        }
    projected = [
        {
            "excel_row": int(item["line_no"]),
            "product_code": item.get("product_code"),
            "product_name": item.get("product_name"),
            "barcode": item.get("barcode_69"),
            "quantity": item.get("quantity"),
        }
        for item in source_records
    ]
    reconciled = _product_records_catalog_reconciliation(
        projected,
        catalog,
        source_label="合同附件",
    )
    source_by_line = {int(item["line_no"]): item for item in source_records}
    enriched: list[dict[str, Any]] = []
    for record in reconciled["records"]:
        line_no = int(record["excel_row"])
        source = source_by_line[line_no]
        enriched.append(
            {
                **record,
                "contract_line_no": line_no,
                "source_page": int(source["source_page"]),
                "source_customer_name": source.get("customer_name"),
                "source_business_date": source.get("business_date"),
                "source_unit": source.get("unit"),
                "source_retail_price": source.get("retail_price"),
                "source_total_amount": source.get("total_amount"),
            }
        )
    return {
        **reconciled,
        "status": "pass" if reconciled["status"] == "pass" else "fail",
        "records": enriched,
    }


def _photo_contract_product_reconciliation(
    contract_knowledge: dict[str, Any],
    attachment_knowledge: dict[str, Any],
    product_reference_hits: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compare independently resolved field identities with the contract product scope."""

    contract_sources: dict[str, list[str]] = {}
    required_core_ids = {
        str(value)
        for value in contract_knowledge.get("knowledge_product_ids") or []
        if str(value)
    }
    for record in contract_knowledge.get("records") or []:
        product_id = str(record.get("knowledge_product_id") or "")
        if not product_id or record.get("knowledge_status") not in PASS_KNOWLEDGE_MATCHES:
            continue
        contract_sources.setdefault(product_id, []).append("合同核心条款")
    for record in attachment_knowledge.get("records") or []:
        product_id = str(record.get("knowledge_product_id") or "")
        if not product_id or record.get("knowledge_status") not in PASS_KNOWLEDGE_MATCHES:
            continue
        source_page = record.get("source_page")
        line_no = record.get("contract_line_no") or record.get("excel_row")
        source = f"合同附件第{line_no}行"
        if source_page:
            source += f"（PDF第{source_page}页）"
        contract_sources.setdefault(product_id, []).append(source)

    if contract_knowledge.get("status") == "fail" or attachment_knowledge.get("status") == "fail":
        return {
            "status": "unmatched",
            "contract_product_ids": sorted(contract_sources),
            "photo_product_ids": [],
            "checks": [],
            "basis": "合同商品尚未全部通过知识库，不能继续确认现场商品是否属于合同。",
        }
    if not contract_sources:
        return {
            "status": "not_applicable",
            "contract_product_ids": [],
            "photo_product_ids": [
                str(item.get("reference_product_id") or "")
                for item in product_reference_hits
                if str(item.get("reference_product_id") or "")
            ],
            "checks": [],
            "basis": "合同核心条款和附件均未形成可核对的具体商品身份，本项不适用。",
        }

    checks: list[dict[str, Any]] = []
    exact_photo_ids: set[str] = set()
    for hit in product_reference_hits:
        product_id = str(hit.get("reference_product_id") or "")
        confidence = str(hit.get("confidence") or "candidate")
        sources = list(dict.fromkeys(contract_sources.get(product_id, [])))
        if confidence == "exact" and sources:
            status = "exact"
            exact_photo_ids.add(product_id)
            basis = "现场包装已视觉对应知识库图片，且该知识库商品已由合同确认。"
        elif sources:
            status = "candidate"
            basis = "现场包装与合同已确认商品的知识库图片仅能模糊对应。"
        else:
            status = "unmatched"
            basis = "现场商品知识库身份已确认，但该商品不在适用的合同商品范围内。"
        checks.append(
            {
                "reference_product_id": product_id,
                "product_code": str(hit.get("product_code") or ""),
                "product_name": str(hit.get("product_name") or ""),
                "barcode_69": str(hit.get("barcode_69") or ""),
                "visual_confidence": confidence,
                "contract_sources": sources,
                "status": status,
                "basis": basis,
            }
        )

    missing_core_ids = sorted(required_core_ids - exact_photo_ids)
    if not checks or any(item["status"] == "unmatched" for item in checks) or missing_core_ids:
        status = "unmatched"
    elif any(item["status"] == "candidate" for item in checks):
        status = "candidate"
    else:
        status = "exact"
    detail = ""
    if missing_core_ids:
        detail = "；合同核心条款要求的商品未全部在现场照片中清晰确认"
    return {
        "status": status,
        "contract_product_ids": sorted(contract_sources),
        "photo_product_ids": sorted(
            {
                str(item.get("reference_product_id") or "")
                for item in product_reference_hits
                if str(item.get("reference_product_id") or "")
            }
        ),
        "checks": checks,
        "basis": (
            "现场商品均通过知识库图片视觉确认，并属于合同已确认商品。"
            if status == "exact"
            else "现场商品未能全部视觉对应合同已确认的知识库商品" + detail + "。"
        ),
    }


def _decimal_value(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _number_pair_status(
    left: Any,
    right: Any,
    *,
    tolerance: Decimal = Decimal("0"),
) -> str:
    left_value = _decimal_value(left)
    right_value = _decimal_value(right)
    if left_value is None or right_value is None:
        return "unverifiable"
    return "exact" if abs(left_value - right_value) <= tolerance else "mismatch"


def _text_pair_status(left: Any, right: Any) -> str:
    left_value = _strict_identity_key(left)
    right_value = _strict_identity_key(right)
    if not left_value or not right_value:
        return "unverifiable"
    return "exact" if left_value == right_value else "mismatch"


def _barcode_pair_status(left: Any, right: Any) -> str:
    left_value = str(left or "").strip()
    right_value = str(right or "").strip()
    if not left_value or not right_value:
        return "unverifiable"
    if not ean13_is_valid(left_value) or not ean13_is_valid(right_value):
        return "mismatch"
    return "exact" if left_value == right_value else "mismatch"


def _period_pair_status(left: Any, right: Any) -> str:
    left_bounds = _parse_period_bounds(left)
    right_bounds = _parse_period_bounds(right)
    if left_bounds is None or right_bounds is None:
        return "unverifiable"
    return "exact" if left_bounds == right_bounds else "mismatch"


def _product_name_pair_status(left: Any, right: Any) -> tuple[str, float]:
    left_value = str(left or "").strip()
    right_value = str(right or "").strip()
    if not left_value or not right_value:
        return "unverifiable", 0.0
    if _canonical_product(left_value) == _canonical_product(right_value):
        return "exact", 1.0
    score = _similarity(left_value, right_value)
    if (
        _name_measurements_compatible(left_value, right_value)
        and score > PRODUCT_EXISTENCE_NAME_THRESHOLD
    ):
        return "fuzzy", score
    return "mismatch", score


def _contract_attachment_sales_reconciliation(
    contract: dict[str, Any],
    sales: dict[str, Any],
) -> dict[str, Any]:
    """Compare every stamped PDF attachment row with the code-read sales row."""

    attachment = contract.get("sales_attachment") or {}
    if not attachment.get("present"):
        sales_records = sorted(
            list(sales.get("records") or []),
            key=lambda item: int(item["excel_row"]),
        )
        return {
            "status": "unverifiable",
            "record_count": 0,
            "matched_count": 0,
            "problem_count": 0,
            "records": [],
            "unmatched_contract_rows": [],
            "unmatched_sales_rows": [
                int(item["excel_row"]) for item in sales_records
            ],
            "unmatched_sales_records": sales_records,
            "ambiguous_sales_rows": [],
            "attachment_line_amount_status": "not_applicable",
            "attachment_line_amount_problem_rows": [],
            "quantity_status": "unverifiable",
            "amount_status": "unverifiable",
            "basis": "合同PDF未附销售明细，属于文档级无法核验；销售Excel的八个核验字段没有合同基准，无法完成逐行核对。",
        }

    attachment_records = sorted(
        list(attachment.get("records") or []),
        key=lambda item: int(item["line_no"]),
    )
    sales_records = sorted(
        list(sales.get("records") or []),
        key=lambda item: int(item["excel_row"]),
    )
    unused_sales_rows = {int(item["excel_row"]): item for item in sales_records}
    checks: list[dict[str, Any]] = []

    def candidate_score(contract_row: dict[str, Any], sales_row: dict[str, Any]) -> tuple[float, int]:
        code_exact = _text_pair_status(
            contract_row.get("product_code"), sales_row.get("product_code")
        ) == "exact"
        barcode_exact = _barcode_pair_status(
            contract_row.get("barcode_69"), sales_row.get("barcode")
        ) == "exact"
        name_status, name_score = _product_name_pair_status(
            contract_row.get("product_name"), sales_row.get("product_name")
        )
        if code_exact and barcode_exact and name_status in {"mismatch", "unverifiable"}:
            # The two strict identifiers already establish the product.  Keep
            # differing or unreadable source names as non-blocking auxiliary text.
            name_status = "fuzzy"
        strict_count = int(code_exact) + int(barcode_exact)
        customer_score = _entity_similarity(
            contract_row.get("customer_name"), sales_row.get("customer_name")
        )
        period_exact = _period_pair_status(
            contract_row.get("business_date"), sales_row.get("period_text")
        ) == "exact"
        quantity_exact = _number_pair_status(
            contract_row.get("quantity"), sales_row.get("quantity")
        ) == "exact"
        price_exact = _number_pair_status(
            contract_row.get("retail_price"),
            sales_row.get("retail_price"),
            tolerance=Decimal("0.01"),
        ) == "exact"
        amount_exact = _number_pair_status(
            contract_row.get("total_amount"),
            sales_row.get("total_amount"),
            tolerance=Decimal("0.01"),
        ) == "exact"
        name_compatible = name_status in {"exact", "fuzzy"}
        transaction_identity_exact = all(
            (
                customer_score == 1.0,
                period_exact,
                quantity_exact,
                price_exact,
                amount_exact,
            )
        )
        # Two exact formal identifiers locate a row without depending on its
        # auxiliary name.  With only one exact identifier, a differing or
        # unreadable name may still locate the row when every independent
        # transaction fact is exact.  Equal candidates remain ambiguous below;
        # strict identifier problems are still reported after pairing.
        relaxed_transaction_locator = (
            strict_count == 1
            and name_status in {"mismatch", "unverifiable"}
            and transaction_identity_exact
        )
        if strict_count == 0 or (
            strict_count == 1
            and not name_compatible
            and not relaxed_transaction_locator
        ):
            return -1.0, int(sales_row["excel_row"])

        score = 100.0 * strict_count + customer_score
        if name_compatible:
            # A compatible product name must always outrank the relaxed
            # transaction-only locator when both compete for one strict ID.
            score += 10.0 + name_score
        score += float(period_exact)
        score += float(quantity_exact)
        score += float(price_exact)
        score += float(amount_exact)
        return score, int(sales_row["excel_row"])

    for contract_row in attachment_records:
        ranked = sorted(
            (
                (*candidate_score(contract_row, sales_row), sales_row)
                for sales_row in unused_sales_rows.values()
            ),
            key=lambda item: (-item[0], item[1]),
        )
        valid_ranked = [item for item in ranked if item[0] >= 0]
        ambiguous_rows: list[int] = []
        if valid_ranked:
            top_score = valid_ranked[0][0]
            ambiguous_rows = [
                int(item[1])
                for item in valid_ranked
                if abs(item[0] - top_score) <= 1e-9
            ]
        selected = (
            valid_ranked[0][2]
            if valid_ranked and len(ambiguous_rows) == 1
            else None
        )
        if selected is None:
            missing_identity = any(
                not str(contract_row.get(field) or "").strip()
                for field in ("product_code", "product_name", "barcode_69")
            )
            ambiguous = len(ambiguous_rows) > 1
            contract_line_amount_status = _number_pair_status(
                _decimal_value(contract_row.get("quantity"))
                * _decimal_value(contract_row.get("retail_price"))
                if _decimal_value(contract_row.get("quantity")) is not None
                and _decimal_value(contract_row.get("retail_price")) is not None
                else None,
                contract_row.get("total_amount"),
                tolerance=Decimal("0.01"),
            )
            unavailable_comparisons = [
                {
                    "field": field,
                    "contract_value": contract_row.get(contract_field),
                    "sales_value": None,
                    "status": "unverifiable",
                }
                for field, contract_field in (
                    ("customer_name", "customer_name"),
                    ("business_date", "business_date"),
                    ("product_code", "product_code"),
                    ("product_name", "product_name"),
                    ("barcode_69", "barcode_69"),
                    ("quantity", "quantity"),
                    ("retail_price", "retail_price"),
                    ("total_amount", "total_amount"),
                )
            ]
            checks.append(
                {
                    "contract_line_no": int(contract_row["line_no"]),
                    "contract_source_page": int(contract_row["source_page"]),
                    "sales_excel_row": None,
                    "status": (
                        "unverifiable"
                        if missing_identity or ambiguous
                        else "fail"
                    ),
                    "confidence": "low",
                    "field_comparisons": unavailable_comparisons,
                    "ambiguous_sales_rows": ambiguous_rows,
                    "contract_calculated_total_amount": (
                        json_number(
                            _decimal_value(contract_row.get("quantity"))
                            * _decimal_value(contract_row.get("retail_price"))
                        )
                        if _decimal_value(contract_row.get("quantity")) is not None
                        and _decimal_value(contract_row.get("retail_price")) is not None
                        else None
                    ),
                    "contract_line_amount_status": contract_line_amount_status,
                    "basis": (
                        "合同附件商品身份不完整，无法定位销售Excel行。"
                        if missing_identity
                        else (
                            "合同附件行同时命中多个证据评分相同的销售Excel行，"
                            f"无法唯一配对：{ambiguous_rows}。"
                            if ambiguous
                            else "销售Excel没有找到与合同附件69码或商品编码一致、且名称能够对应的商品行。"
                        )
                    ),
                }
            )
            continue

        sales_row_no = int(selected["excel_row"])
        unused_sales_rows.pop(sales_row_no, None)
        customer_score = _entity_similarity(
            contract_row.get("customer_name"), selected.get("customer_name")
        )
        customer_status = (
            "unverifiable"
            if not str(contract_row.get("customer_name") or "").strip()
            or not str(selected.get("customer_name") or "").strip()
            else "exact"
            if customer_score == 1.0
            else "fuzzy"
            if customer_score >= 0.6
            else "mismatch"
        )
        name_status, name_score = _product_name_pair_status(
            contract_row.get("product_name"), selected.get("product_name")
        )
        if (
            _text_pair_status(
                contract_row.get("product_code"), selected.get("product_code")
            )
            == "exact"
            and _barcode_pair_status(
                contract_row.get("barcode_69"), selected.get("barcode")
            )
            == "exact"
            and name_status in {"mismatch", "unverifiable"}
        ):
            name_status = "fuzzy"
        contract_quantity = _decimal_value(contract_row.get("quantity"))
        contract_price = _decimal_value(contract_row.get("retail_price"))
        contract_calculated_amount = (
            contract_quantity * contract_price
            if contract_quantity is not None and contract_price is not None
            else None
        )
        contract_line_amount_status = _number_pair_status(
            contract_calculated_amount,
            contract_row.get("total_amount"),
            tolerance=Decimal("0.01"),
        )
        comparisons = [
            {
                "field": "customer_name",
                "contract_value": contract_row.get("customer_name"),
                "sales_value": selected.get("customer_name"),
                "status": customer_status,
            },
            {
                "field": "business_date",
                "contract_value": contract_row.get("business_date"),
                "sales_value": selected.get("period_text"),
                "status": _period_pair_status(
                    contract_row.get("business_date"), selected.get("period_text")
                ),
            },
            {
                "field": "product_code",
                "contract_value": contract_row.get("product_code"),
                "sales_value": selected.get("product_code"),
                "status": _text_pair_status(
                    contract_row.get("product_code"), selected.get("product_code")
                ),
            },
            {
                "field": "product_name",
                "contract_value": contract_row.get("product_name"),
                "sales_value": selected.get("product_name"),
                "status": name_status,
                "similarity": round(name_score, 3) if name_score else None,
            },
            {
                "field": "barcode_69",
                "contract_value": contract_row.get("barcode_69"),
                "sales_value": selected.get("barcode"),
                "status": _barcode_pair_status(
                    contract_row.get("barcode_69"), selected.get("barcode")
                ),
            },
            {
                "field": "quantity",
                "contract_value": contract_row.get("quantity"),
                "sales_value": selected.get("quantity"),
                "status": _number_pair_status(
                    contract_row.get("quantity"), selected.get("quantity")
                ),
            },
            {
                "field": "retail_price",
                "contract_value": contract_row.get("retail_price"),
                "sales_value": selected.get("retail_price"),
                "status": _number_pair_status(
                    contract_row.get("retail_price"),
                    selected.get("retail_price"),
                    tolerance=Decimal("0.01"),
                ),
            },
            {
                "field": "total_amount",
                "contract_value": contract_row.get("total_amount"),
                "sales_value": selected.get("total_amount"),
                "status": _number_pair_status(
                    contract_row.get("total_amount"),
                    selected.get("total_amount"),
                    tolerance=Decimal("0.01"),
                ),
            },
        ]
        # Product name is displayed as fuzzy auxiliary evidence but never owns
        # pass/fail.  Comparable code, 69 code, and transaction fields do.
        decision_comparisons = [
            item for item in comparisons if item["field"] != "product_name"
        ]
        statuses = {str(item["status"]) for item in decision_comparisons}
        if "mismatch" in statuses or contract_line_amount_status == "mismatch":
            row_status = "fail"
        elif "unverifiable" in statuses or contract_line_amount_status == "unverifiable":
            row_status = "unverifiable"
        else:
            row_status = "pass"
        confidence = (
            "high"
            if row_status == "pass" and "fuzzy" not in statuses
            else "medium"
            if row_status == "pass"
            else "low"
        )
        problem_fields = [
            str(item["field"])
            for item in decision_comparisons
            if item["status"] in {"mismatch", "unverifiable"}
        ]
        if contract_line_amount_status != "exact":
            problem_fields.append("合同附件数量×零售价")
        checks.append(
            {
                "contract_line_no": int(contract_row["line_no"]),
                "contract_source_page": int(contract_row["source_page"]),
                "sales_excel_row": sales_row_no,
                "status": row_status,
                "confidence": confidence,
                "field_comparisons": comparisons,
                "ambiguous_sales_rows": [],
                "contract_calculated_total_amount": (
                    json_number(contract_calculated_amount)
                    if contract_calculated_amount is not None
                    else None
                ),
                "contract_line_amount_status": contract_line_amount_status,
                "basis": (
                    "客户、业务日期、商品身份、数量、零售价和合计金额均一致。"
                    if not problem_fields
                    else "不一致或无法核对字段：" + "、".join(problem_fields) + "。"
                ),
            }
        )

    unmatched_contract_rows = [
        int(item["contract_line_no"])
        for item in checks
        if item["sales_excel_row"] is None
    ]
    ambiguous_sales_rows = sorted(
        {
            int(row)
            for item in checks
            for row in item.get("ambiguous_sales_rows") or []
        }
    )
    unmatched_sales_rows = sorted(
        set(unused_sales_rows) - set(ambiguous_sales_rows)
    )
    unmatched_sales_records = [
        dict(unused_sales_rows[row]) for row in unmatched_sales_rows
    ]
    attachment_line_amount_problem_rows = [
        int(item["contract_line_no"])
        for item in checks
        if item.get("contract_line_amount_status") != "exact"
    ]
    attachment_line_amount_status = (
        "fail"
        if any(
            item.get("contract_line_amount_status") == "mismatch"
            for item in checks
        )
        else "unverifiable"
        if attachment_line_amount_problem_rows
        else "pass"
    )
    attachment_quantity = attachment.get("total_quantity")
    attachment_amount = attachment.get("total_amount")
    quantity_source_status = _number_pair_status(
        attachment_quantity,
        sales.get("total_quantity"),
    )
    amount_source_status = _number_pair_status(
        attachment_amount,
        sales.get("retail_amount"),
        tolerance=Decimal("0.01"),
    )
    row_quantities = [_decimal_value(item.get("quantity")) for item in attachment_records]
    calculated_attachment_quantity = (
        sum((value for value in row_quantities if value is not None), Decimal("0"))
        if row_quantities and all(value is not None for value in row_quantities)
        else None
    )
    row_amounts = [_decimal_value(item.get("total_amount")) for item in attachment_records]
    calculated_attachment_amount = (
        sum((value for value in row_amounts if value is not None), Decimal("0"))
        if row_amounts and all(value is not None for value in row_amounts)
        else None
    )
    quantity_arithmetic_status = _number_pair_status(
        attachment_quantity,
        calculated_attachment_quantity,
    )
    amount_arithmetic_status = _number_pair_status(
        attachment_amount,
        calculated_attachment_amount,
        tolerance=Decimal("0.01"),
    )

    def combined_total_status(*statuses: str) -> str:
        if "mismatch" in statuses:
            return "mismatch"
        if "unverifiable" in statuses:
            return "unverifiable"
        return "exact"

    quantity_status = combined_total_status(
        quantity_source_status,
        quantity_arithmetic_status,
    )
    amount_status = combined_total_status(
        amount_source_status,
        amount_arithmetic_status,
    )
    problem_checks = [item for item in checks if item["status"] != "pass"]
    if (
        any(item["status"] == "fail" for item in problem_checks)
        or unmatched_sales_rows
        or quantity_status == "mismatch"
        or amount_status == "mismatch"
    ):
        status = "fail"
    elif problem_checks or quantity_status == "unverifiable" or amount_status == "unverifiable":
        status = "unverifiable"
    else:
        status = "pass"
    matched_count = sum(item["status"] == "pass" for item in checks)
    return {
        "status": status,
        "record_count": len(attachment_records),
        "matched_count": matched_count,
        "problem_count": len(problem_checks),
        "records": checks,
        "unmatched_contract_rows": unmatched_contract_rows,
        "unmatched_sales_rows": unmatched_sales_rows,
        "unmatched_sales_records": unmatched_sales_records,
        "ambiguous_sales_rows": ambiguous_sales_rows,
        "attachment_line_amount_status": attachment_line_amount_status,
        "attachment_line_amount_problem_rows": attachment_line_amount_problem_rows,
        "attachment_total_quantity": attachment_quantity,
        "calculated_attachment_quantity": (
            json_number(calculated_attachment_quantity)
            if calculated_attachment_quantity is not None
            else None
        ),
        "sales_total_quantity": sales.get("total_quantity"),
        "quantity_source_status": quantity_source_status,
        "quantity_arithmetic_status": quantity_arithmetic_status,
        "quantity_status": quantity_status,
        "attachment_total_amount": attachment_amount,
        "calculated_attachment_amount": (
            json_number(calculated_attachment_amount)
            if calculated_attachment_amount is not None
            else None
        ),
        "sales_total_amount": sales.get("retail_amount"),
        "amount_source_status": amount_source_status,
        "amount_arithmetic_status": amount_arithmetic_status,
        "amount_status": amount_status,
        "basis": (
            f"合同附件{len(attachment_records)}行：通过{matched_count}行、问题{len(problem_checks)}行；"
            f"销售Excel另有{len(unmatched_sales_rows)}行无合同附件基准；"
            f"附件行金额{'全部一致' if attachment_line_amount_status == 'pass' else '有问题或无法核对'}；"
            f"数量{'一致' if quantity_status == 'exact' else '不一致或无法核对'}，"
            f"合计金额{'一致' if amount_status == 'exact' else '不一致或无法核对'}。"
        ),
    }


def _contract_product_knowledge_reconciliation(
    contract: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    required_products = [
        str(value).strip() for value in contract.get("required_products") or []
    ]
    if not contract.get("requires_specific_products"):
        return {
            "status": "not_applicable",
            "records": [],
            "knowledge_product_ids": [],
            "basis": "合同未出现可核验的具体商品条件，商品知识库不适用。",
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
        code_ids = {
            str(product["product_id"])
            for product in products
            if source_code
            and _strict_identity_key(source_code)
            == _strict_identity_key(product.get("product_code"))
        }
        barcode_ids = {
            str(product["product_id"])
            for product in products
            if source_barcode
            and not invalid_barcode
            and str(product.get("barcode_69") or "") == source_barcode
        }
        exact_name_ids = {
            str(product["product_id"])
            for product in products
            if source_name
            and _canonical_product(source_name)
            in {
                _canonical_product(candidate)
                for candidate in _catalog_name_values(product)
            }
        }

        supplied_identity_count = sum(
            bool(value) for value in (source_code, source_name, source_barcode)
        )
        strict_sets: list[set[str]] = []
        if source_code:
            strict_sets.append(code_ids)
        if source_barcode and not invalid_barcode:
            strict_sets.append(barcode_ids)
        strict_candidates = (
            set.intersection(*strict_sets)
            if strict_sets
            else set(by_id)
        )
        strict_identifier_missing = (
            bool(source_code) and not code_ids
        ) or (
            bool(source_barcode) and not invalid_barcode and not barcode_ids
        )
        strict_identifier_conflict = bool(strict_sets) and not strict_candidates

        selected: dict[str, Any] | None = None
        score = 0.0
        fuzzy = False
        if not invalid_barcode and not strict_identifier_missing and not strict_identifier_conflict:
            if source_name:
                exact_pool = strict_candidates & exact_name_ids
                if len(exact_pool) == 1:
                    selected = by_id[next(iter(exact_pool))]
                    score = 1.0
                elif not exact_pool:
                    selected, score = _best_fuzzy_product(
                        source_name,
                        [
                            by_id[product_id]
                            for product_id in sorted(strict_candidates)
                        ],
                        threshold=PRODUCT_EXISTENCE_NAME_THRESHOLD,
                    )
                    fuzzy = selected is not None
            elif len(strict_candidates) == 1:
                selected = by_id[next(iter(strict_candidates))]
                score = 1.0

        if not supplied_identity_count or invalid_barcode:
            knowledge_status = "conflict"
        elif strict_identifier_missing:
            knowledge_status = "unmatched"
        elif strict_identifier_conflict:
            knowledge_status = "conflict"
        elif selected is not None:
            knowledge_status = "fuzzy_matched" if fuzzy else "matched"
        else:
            knowledge_status = "conflict"

        diagnostic_selected = selected
        if diagnostic_selected is None and len(barcode_ids) == 1:
            diagnostic_selected = by_id[next(iter(barcode_ids))]
        elif diagnostic_selected is None and len(code_ids) == 1:
            diagnostic_selected = by_id[next(iter(code_ids))]
        elif diagnostic_selected is None and len(exact_name_ids) == 1:
            diagnostic_selected = by_id[next(iter(exact_name_ids))]

        if knowledge_status in PASS_KNOWLEDGE_MATCHES:
            passed_fields: list[str] = []
            if source_code:
                passed_fields.append("商品编码精确匹配")
            if source_barcode:
                passed_fields.append("69码精确匹配")
            if source_name:
                passed_fields.append(
                    "商品名称模糊且唯一匹配"
                    if fuzzy
                    else "商品名称精确匹配"
                )
            basis = "合同具体商品" + "、".join(passed_fields) + "。"
        else:
            if invalid_barcode:
                reason = "69码未通过EAN-13校验"
            elif not supplied_identity_count:
                reason = "缺少商品编码、商品名称或69码等可核验身份"
            elif source_code and not code_ids:
                reason = "商品编码未登记"
            elif source_barcode and not barcode_ids:
                reason = "69码未登记"
            elif strict_identifier_conflict:
                reason = "商品编码与69码指向不同知识库商品"
            elif source_name:
                reason = "商品名称未与严格标识指向的商品唯一对应"
            else:
                reason = "现有身份字段不能唯一确定商品"
            basis = (
                "合同具体商品未通过商品存在核验："
                + reason
                + "；已提供的商品编码和69码必须精确一致，商品名称只需唯一模糊兼容，不要求逐字一致。"
            )

        records.append(
            {
                "required_product": value,
                "source_product_code": source_code or None,
                "source_product_name": source_name or None,
                "source_barcode_69": source_barcode or None,
                "knowledge_status": knowledge_status,
                "knowledge_product_id": (
                    str(diagnostic_selected["product_id"])
                    if diagnostic_selected is not None
                    else None
                ),
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
                "name_match_type": (
                    "fuzzy"
                    if fuzzy
                    else "exact"
                    if selected is not None and source_name
                    else None
                ),
                "name_similarity": (
                    round(score, 3)
                    if source_name and score
                    else None
                ),
                "basis": basis,
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
            f"合同出现的{len(records)}个具体商品条件全部通过共享商品知识库核验"
            if not problem
            else f"合同具体商品条件中有{len(problem)}个未通过共享商品知识库核验"
        ),
    }

def _photo_knowledge_control(product_reference_hits: list[dict[str, Any]]) -> tuple[str, str]:
    if not product_reference_hits:
        return "unmatched", "现场照片可见文字未能对应到知识库商品，或包装视觉比对不一致，置信度低"
    if all(str(hit.get("confidence")) == "exact" for hit in product_reference_hits):
        return "exact", "现场照片可见文字已对应知识库文字，且包装关键特征与知识库多方位参考图相容，置信度高"
    return "candidate", "现场照片文字或包装仅能对应多个知识库候选，尚未唯一确认，置信度中"



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


def audit_display_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
    *,
    location_resolver: LocationResolver | None = None,
) -> dict[str, Any]:
    sales_path = Path(case["sales_excel"]).resolve()
    contract_path = Path(case["contract_pdf"]).resolve()
    photo_paths = [Path(value).resolve() for value in case["photo_files"]]
    contract = evidence["contract"]
    from .product_database import load_product_catalog
    product_rag = load_product_catalog()
    contract_knowledge = _contract_product_knowledge_reconciliation(contract, product_rag)
    contract_attachment_knowledge = _contract_attachment_knowledge_reconciliation(
        contract,
        product_rag,
    )
    sales = read_display_sales(sales_path)
    contract_sales = _contract_sales_reconciliation(contract, sales)
    contract_core = _contract_core_reconciliation(contract, contract_sales)
    contract_attachment_sales = _contract_attachment_sales_reconciliation(contract, sales)
    photos = image_file_inventory(photo_paths, directory=case.get("source_root"))
    pdf = pdf_inventory(contract_path)
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
    if sales.get("internal_status") != "pass":
        exceptions.append(
            exception(
                "high",
                "SALES_EXCEL_INTERNAL_RECONCILIATION_FAILED",
                str(sales.get("internal_basis") or "销售Excel内部核对未通过。"),
                "销售Excel的明细行金额或打印合计不能自我闭环。",
                "按问题行更正数量、零售价或合计金额，并保留唯一的合计/总计行。",
                source=Path(str(sales.get("source_file") or sales_path)).name,
            )
        )
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
    if contract_attachment_knowledge["status"] == "fail":
        exceptions.append(
            exception(
                "high",
                "CONTRACT_ATTACHMENT_PRODUCT_KNOWLEDGE_MISMATCH",
                contract_attachment_knowledge["basis"],
                "合同销售附件中的商品身份没有完整通过正式商品知识库。",
                "更正合同附件中的商品编码、商品名称或69码后重新核销。",
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
                "核对合同签订方与销售客户、合同执行周期与业务日期，并补充盖章清楚的合同。",
                source=(
                    f"{Path(str(contract.get('source_file') or contract_path)).name} / "
                    f"{Path(str(sales.get('source_file') or sales_path)).name}"
                ),
            )
        )
    if contract_core["status"] != "pass":
        exceptions.append(
            exception(
                "high",
                "CONTRACT_CORE_FIELDS_INCOMPLETE",
                contract_core["basis"],
                "合同签订方、活动预算金额、执行周期、活动内容、核销方式或盖章未全部核清。",
                "按合同核心字段问题清单重新提交清晰、完整且已签章的合同PDF。",
                source=Path(str(contract.get("source_file") or contract_path)).name,
            )
        )
    if contract_attachment_sales["status"] != "pass":
        exceptions.append(
            exception(
                "high",
                "CONTRACT_ATTACHMENT_SALES_RECONCILIATION_FAILED",
                contract_attachment_sales["basis"],
                "盖章合同附件与独立销售Excel的商品明细不能逐行闭环。",
                "按问题行重新提交一致且清晰的合同销售附件或销售Excel。",
                source=(
                    f"{Path(str(contract.get('source_file') or contract_path)).name} / "
                    f"{Path(str(sales.get('source_file') or sales_path)).name}"
                ),
            )
        )

    contract_attachment_sales_pass = contract_attachment_sales["status"] == "pass"
    sales_internal_pass = sales.get("internal_status") == "pass"
    resolved_location_resolver = location_resolver or default_location_resolver()

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
        store_match, deterministic_location_basis, location_resolution = _determine_store_match(
            str(store["store_name"]),
            str(store.get("address") or "").strip() or None,
            review.get("visible_location"),
            resolved_location_resolver,
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
        reference_hits = apply_visible_catalog_text_exact_hits(
            reference_hits,
            [str(value) for value in review.get("visible_text") or []],
            product_rag,
        )
        reference_labels = [product_reference_label(item) for item in reference_hits]
        recognized = reference_labels or recognized_visible
        photo_knowledge_match, photo_knowledge_basis = _photo_knowledge_control(reference_hits)
        photo_contract_product = _photo_contract_product_reconciliation(
            contract_knowledge,
            contract_attachment_knowledge,
            reference_hits,
        )
        promotion_summary, promotion_present = build_promotion_summary(review)
        contract_sales_pass = contract_sales["status"] == "pass"
        contract_core_pass = contract_core["status"] == "pass"
        contract_knowledge_pass = contract_knowledge["status"] in {"pass", "not_applicable"}
        contract_attachment_knowledge_pass = contract_attachment_knowledge["status"] in {
            "pass",
            "not_applicable",
        }
        photo_knowledge_pass = photo_knowledge_match == "exact"
        photo_contract_product_pass = photo_contract_product["status"] in {
            "not_applicable",
            "exact",
        }
        product_pass = all(
            [
                contract_knowledge_pass,
                contract_attachment_knowledge_pass,
                photo_knowledge_pass,
                photo_contract_product_pass,
            ]
        )
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
                contract_core_pass,
                file_pass,
                period_pass,
                store_pass,
                display_pass,
                duplicate_pass,
                product_pass,
                promotion_pass,
                amount_rule_pass,
                contract_attachment_sales_pass,
                sales_internal_pass,
            ]
        )

        risks = list(review.get("risk_notes") or [])
        advice = list(review.get("supplement_advice") or [])
        if not contract_sales_pass:
            risks.append("合同与销售Excel的签订方、业务日期或合同完整性未闭环")
            advice.append("核对合同签订方/销售客户、执行周期/业务日期，并补充盖章清楚的合同。")
        if not contract_core_pass:
            risks.append(contract_core["basis"])
            advice.append("补齐合同签订方、活动预算金额、执行周期、活动内容、核销方式和盖章的问题项。")
        if not contract_knowledge_pass:
            risks.append(contract_knowledge["basis"])
            advice.append("先让合同限定商品唯一对应正式知识库，再重新核销。")
        if not contract_attachment_knowledge_pass:
            risks.append(contract_attachment_knowledge["basis"])
            advice.append("先更正合同附件中的商品编码、商品名称或69码，使其通过知识库。")
        if not contract_attachment_sales_pass:
            risks.append(contract_attachment_sales["basis"])
            advice.append("让销售Excel的八个核验字段逐行对齐合同附件，并补齐缺失的合同附件行；单位无需对齐。")
        if not sales_internal_pass:
            risks.append(str(sales.get("internal_basis") or "销售Excel内部金额无法闭环。"))
            advice.append("更正销售Excel行内金额及打印合计后重新提交。")
        if photo_knowledge_match == "candidate":
            risks.append("现场照片与商品知识库只有模糊匹配，置信度中")
            advice.append("补拍清晰69码、唯一商品短码或足以确认具体商品的包装照片。")
        elif photo_knowledge_match == "unmatched":
            risks.append("现场照片与商品知识库完全不匹配，置信度低")
            advice.append("补拍能与商品知识库对应的商品正面、侧面或69码。")
        if not photo_contract_product_pass:
            risks.append(photo_contract_product["basis"])
            advice.append("补拍合同已确认商品的清晰正面、侧面或条码面，并保留完整现场环境。")
        if missing:
            risks.append("核验引用了不存在的照片：" + "、".join(missing))
            advice.append("补齐缺失的原始现场照片。")
        if not period_pass:
            advice.append("补充活动期内且完整日期可见的原始现场照片。")
        if not store_pass:
            location_status = str(location_resolution.get("status") or "unavailable")
            location_confidence = str(location_resolution.get("confidence") or "low")
            if location_status == "unrelated":
                distance = location_resolution.get("distance_meters")
                distance_text = (
                    f"，两个唯一地点直线距离约{distance:g}米"
                    if isinstance(distance, (int, float))
                    else ""
                )
                advice.append(
                    f"地图地点核验置信度{('低' if location_confidence == 'low' else location_confidence)}"
                    f"{distance_text}；补充能证明两处同址或临近的权威地址材料，"
                    "否则重新提交水印地点正确的现场照片。"
                )
            elif store_match == "location_unverified":
                advice.append(
                    "配置百度地图 MCP 后重新核验，或补充能唯一证明两处同址、商场与店铺关系或实际距离的权威地址材料；"
                    "当前地点置信度低，转人工核验。"
                )
            else:
                advice.append("补充水印中可见合同门店名称/地址的照片。")
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
                "location_resolution": location_resolution,
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
                "photo_contract_product_status": photo_contract_product["status"],
                "photo_contract_product_basis": photo_contract_product["basis"],
                "photo_contract_product_checks": photo_contract_product["checks"],
                "contract_sales_status": contract_sales["status"],
                "contract_core_status": contract_core["status"],
                "contract_attachment_sales_status": contract_attachment_sales["status"],
                "sales_internal_status": sales.get("internal_status"),
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
    attachment_global_pass = (
        contract_attachment_knowledge["status"] in {"pass", "not_applicable"}
        and contract_attachment_sales["status"] == "pass"
    )
    activity_global_pass = (
        contract_core["status"] == "pass"
        and contract_sales["status"] == "pass"
        and attachment_global_pass
        and sales_internal_pass
    )
    if not activity_global_pass:
        # Preserve each store's independently supported amount for diagnosis,
        # but a broken stamped-attachment or sales-file internal chain blocks
        # settlement for the activity until that global evidence is corrected.
        suggested = Decimal("0")
    conclusion = (
        "pass"
        if results and not supplement_count and activity_global_pass
        else "partial_pass"
        if activity_global_pass and passed_count and supplement_count
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
            "sales_internal_status": sales.get("internal_status"),
            "sales_internal_problem_rows": list(
                sales.get("internal_problem_rows") or []
            ),
            "sales_line_amount_status": sales.get("line_amount_status"),
            "sales_printed_quantity_status": sales.get(
                "printed_quantity_status"
            ),
            "sales_printed_amount_status": sales.get("printed_amount_status"),
            "contract_attachment_present": bool(
                (contract.get("sales_attachment") or {}).get("present")
            ),
            "contract_attachment_row_count": contract_attachment_sales["record_count"],
            "contract_attachment_quantity": contract_attachment_sales.get(
                "attachment_total_quantity"
            ),
            "contract_attachment_amount": contract_attachment_sales.get(
                "attachment_total_amount"
            ),
            "contract_attachment_knowledge_problem_count": (
                contract_attachment_knowledge["problem_count"]
            ),
            "contract_attachment_sales_problem_count": contract_attachment_sales[
                "problem_count"
            ],
            "contract_core_status": contract_core["status"],
            "contract_core_problem_count": contract_core["problem_count"],
            "high_exception_count": sum(item["severity"] == "high" for item in exceptions),
            "medium_exception_count": sum(item["severity"] == "medium" for item in exceptions),
        },
        "contract": contract,
        "contract_product_knowledge": contract_knowledge,
        "contract_attachment_product_knowledge": contract_attachment_knowledge,
        "contract_core_reconciliation": contract_core,
        "contract_sales_reconciliation": contract_sales,
        "contract_attachment_sales_reconciliation": contract_attachment_sales,
        "contract_pdf": pdf,
        "sales": sales,
        "photo_inventory": photos,
        "store_reconciliation": results,
        "unreferenced_photos": unreferenced,
        "exceptions": exceptions,
    }
