from __future__ import annotations

import re
import unicodedata
from datetime import date
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
    load_product_rag,
    product_reference_label,
    resolve_product_reference_hits,
)


PASS_STORE_MATCHES = {"exact", "compatible"}
PASS_DISPLAY_STANDARDS = {"stack_1sqm", "four_vertical", "both"}
NUMBERED_STORE_PATTERN = re.compile(r"(?P<number>\d{1,3}|[一二三四五六七八九十]{1,3})店")
GENERIC_LOCATION_BIGRAMS = {
    "超市", "商场", "购物", "广场", "生活", "连锁", "百货", "中心",
    "广东", "东莞", "深圳", "门店", "精选",
}
DISPLAY_SKILL_DIR = Path(__file__).resolve().parents[1] / "skills" / "audit-promotional-display"


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


def _select_names(
    records: list[dict[str, Any]],
    predicate: Any,
) -> list[str]:
    return [str(item["product_name"]) for item in records if predicate(_canonical_product(item["product_name"]))]


def sales_product_correspondence(
    recognized_products: list[str],
    sales_records: list[dict[str, Any]],
    product_reference_hits: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return only source-cell names, never model-provided Excel wording."""

    selected: set[str] = set()
    exact_identifier_names: set[str] = set()
    exact_reference_products: list[str] = []
    bases: list[str] = []
    literal_exact = False
    candidate_only = False
    for hit in product_reference_hits or []:
        barcode = str(hit.get("barcode_69") or "").strip()
        primary_code = _canonical_product(hit.get("product_code"))
        if primary_code == _canonical_product("未标注"):
            primary_code = ""
        alias_codes = {
            _canonical_product(value)
            for value in hit.get("product_code_aliases") or []
            if _canonical_product(value)
        }
        primary_code_matches = [
            str(item["product_name"])
            for item in sales_records
            if primary_code
            and _canonical_product(item.get("product_code")) == primary_code
        ]
        exact_name_matches = [
            str(item["product_name"])
            for item in sales_records
            if _canonical_product(item.get("product_name"))
            == _canonical_product(hit.get("product_name"))
        ]
        barcode_matches = [
            str(item["product_name"])
            for item in sales_records
            if str(item.get("barcode") or "").strip() == barcode
        ]
        alias_code_matches = [
            str(item["product_name"])
            for item in sales_records
            if _canonical_product(item.get("product_code")) in alias_codes
        ]
        matches: list[str] = []
        if len(primary_code_matches) == 1:
            matches = primary_code_matches
            matched_record = next(
                item
                for item in sales_records
                if str(item["product_name"]) == matches[0]
            )
            excel_barcode = str(matched_record.get("barcode") or "").strip()
            basis = f"视觉RAG命中唯一产品编码{hit.get('product_code')}，与Excel原始产品编码比对"
            if barcode and excel_barcode and barcode != excel_barcode:
                basis += (
                    f"；现场69码{barcode}与Excel记录{excel_barcode}不同，"
                    "保留现场照片条码并记录差异"
                )
            bases.append(basis)
        elif len(exact_name_matches) == 1:
            matches = exact_name_matches
            bases.append("视觉RAG唯一产品名称与Excel原始商品名称精确比对")
        elif len(barcode_matches) == 1:
            matches = barcode_matches
            bases.append(f"视觉RAG命中69码{barcode}，且Excel仅有一条同码记录")
        elif len(alias_code_matches) == 1:
            matches = alias_code_matches
            matched_code = next(
                str(item.get("product_code"))
                for item in sales_records
                if str(item["product_name"]) == matches[0]
            )
            bases.append(
                f"视觉RAG命中产品编码别名{matched_code}，与Excel原始产品编码比对"
            )
        elif barcode_matches:
            matches = barcode_matches
            candidate_only = True
            bases.append(
                f"视觉RAG命中69码{barcode}，但Excel存在{len(barcode_matches)}条同码商品；"
                "69码不能单独确定唯一产品"
            )
        elif len(primary_code_matches) > 1 or len(exact_name_matches) > 1 or len(alias_code_matches) > 1:
            candidate_only = True
            bases.append("视觉RAG标识在Excel中命中多行，无法收敛为唯一商品")
        selected.update(matches)
        if str(hit.get("confidence")) == "exact" and matches:
            exact_identifier_names.update(matches)
            exact_reference_products.append(str(hit.get("product_name") or ""))
        elif matches:
            candidate_only = True

    expanded_products = [
        part.strip()
        for value in recognized_products
        for part in re.split(r"[；;、]", str(value))
        if part.strip()
    ]
    for product in expanded_products:
        text = _canonical_product(product)
        if any(
            reference
            and (
                _canonical_product(reference) in text
                or _similarity(product, reference) >= 0.8
            )
            for reference in exact_reference_products
        ):
            continue
        matches: list[str] = []
        reasons: list[str] = []
        is_3_plus_2_bundle = "3+2" in text
        is_1_plus_1_alias = "1+1" in text or "减少软垢" in text or "双重因子" in text

        if is_3_plus_2_bundle:
            values = _select_names(sales_records, lambda name: "3+2" in name)
            matches.extend(values)
            if len(values) == 1:
                literal_exact = True
            reasons.append("可见包装含3+2，与Excel中的同组合标识比对")
        if is_1_plus_1_alias:
            values = _select_names(
                sales_records,
                lambda name: "倍养护牙膏100g" in name or "专研清新美白牙膏100g" in name,
            )
            matches.extend(values)
            candidate_only = True
            reasons.append("可见SP-1/SE-1或1+1包装，按维护的产品别名规则列为候选")
        if any(token in text for token in ("绿茶清泡", "焕亮美白", "极光白", "光白酵素")):
            matches.extend(_select_names(sales_records, lambda name: "极光白酵素" in name))
            candidate_only = True
            reasons.append("可见极光白/绿茶组合包装，按产品别名规则列为候选")

        if not is_3_plus_2_bundle and not is_1_plus_1_alias and re.search(r"sp0?1", text):
            if any(token in text for token in ("科研", "洁白", "3重")):
                values = _select_names(sales_records, lambda name: "sp1" in name and "科研白" in name)
            elif "5重" in text:
                values = _select_names(sales_records, lambda name: "sp1" in name)
            else:
                values = _select_names(sales_records, lambda name: "sp1" in name)
            matches.extend(values)
            candidate_only = True
            reasons.append("可见SP-1系列标识，按系列与可见功效词筛选候选")
        if not is_3_plus_2_bundle and re.search(r"sp0?2", text):
            values: list[str] = []
            if "植萃" in text:
                values.extend(_select_names(sales_records, lambda name: "sp2" in name))
            if any(token in text for token in ("氨基酸", "温和护龈")):
                values.extend(_select_names(sales_records, lambda name: "sp2" in name and "氨基酸" in name))
            if any(token in text for token in ("益清新", "沁爽", "十里晚香")):
                values.extend(_select_names(sales_records, lambda name: "sp2" in name and "益清新" in name))
            if not values:
                values = _select_names(sales_records, lambda name: "sp2" in name)
            matches.extend(values)
            candidate_only = True
            reasons.append("可见SP-2系列标识，按系列与可见功效词筛选候选")
        if not is_3_plus_2_bundle and re.search(r"sp0?3", text):
            matches.extend(_select_names(sales_records, lambda name: "sp3" in name))
            candidate_only = True
            reasons.append("可见SP-3系列标识")
        if not is_3_plus_2_bundle and re.search(r"sp0?4", text):
            if any(token in text for token in ("清新", "薄荷", "全天")):
                values = _select_names(sales_records, lambda name: "sp4" in name and "清新" in name)
            elif any(token in text for token in ("美白", "皓齿")):
                values = _select_names(sales_records, lambda name: "sp4" in name and "美白" in name)
            else:
                values = _select_names(sales_records, lambda name: "sp4" in name)
            matches.extend(values)
            candidate_only = True
            reasons.append("可见SP-4系列标识，按可见功效词筛选候选")

        if not matches:
            scored = sorted(
                (
                    (_similarity(product, item["product_name"]), str(item["product_name"]))
                    for item in sales_records
                ),
                reverse=True,
            )
            if scored and scored[0][0] >= 0.28:
                top = scored[0][0]
                matches.extend(name for score, name in scored if score >= max(0.28, top - 0.06))
                candidate_only = True
                reasons.append(f"可见包装文字与Excel原始商品名字符相似度最高为{top:.3f}")

        selected.update(matches)
        bases.extend(reasons)

    ordered = [
        str(item["product_name"])
        for item in sales_records
        if str(item["product_name"]) in selected
    ]
    if not ordered:
        status = "unmatched"
        basis = "代码未从现场可见包装中找到可支持的Excel原始商品名。"
    elif (
        len(ordered) == 1
        and ordered[0] in exact_identifier_names
        and not any(
            str(hit.get("confidence")) == "candidate"
            for hit in product_reference_hits or []
        )
    ):
        status = "exact"
        basis = "；".join(dict.fromkeys(bases)) + "；唯一支持1个Excel原始商品名。"
    elif literal_exact and not candidate_only and len(ordered) == 1:
        status = "exact"
        basis = "；".join(dict.fromkeys(bases)) + "；唯一支持1个Excel原始商品名。"
    else:
        status = "candidate"
        basis = "；".join(dict.fromkeys(bases)) + "；可见信息不足以把所有候选收敛为唯一SKU。"
    return {"names": ordered, "status": status, "basis": basis}


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
            "description": "未提交照片",
            "limitations": ["未提交照片"],
        },
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
    photos = image_file_inventory(photo_paths, directory=case.get("source_root"))
    pdf = pdf_inventory(contract_path)
    contract = evidence["contract"]
    stores = sorted(contract["stores"], key=lambda item: int(item["line_no"]))
    store_map = unique_by(stores, "line_no", "contract store line number")
    review_map = unique_by(evidence.get("photo_reviews") or [], "store_line_no", "photo review store line number")
    images_by_name = {path.name.casefold(): path for path in photo_paths}
    review_lines_by_file: dict[str, set[int]] = {}
    for review in evidence.get("photo_reviews") or []:
        for name in review.get("photo_files") or []:
            review_lines_by_file.setdefault(Path(name).name.casefold(), set()).add(int(review["store_line_no"]))
    exact_names, possible_names, cross_store_pairs = _duplicate_maps(photos, review_lines_by_file)

    fee = money(contract["fee_per_store"], label="fee per store")
    claimed = money(contract["claimed_amount"], label="claimed amount")
    start = date.fromisoformat(contract["activity_start"])
    end = date.fromisoformat(contract["activity_end"])
    exceptions: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    referenced: set[str] = set()
    product_rag: dict[str, Any] | None = None

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
        if raw_reference_hits and product_rag is None:
            product_rag = load_product_rag(DISPLAY_SKILL_DIR)
        reference_hits = resolve_product_reference_hits(
            raw_reference_hits,
            product_rag or {"products": []},
        )
        reference_labels = [product_reference_label(item) for item in reference_hits]
        recognized = reference_labels or recognized_visible
        correspondence = sales_product_correspondence(
            recognized_visible,
            sales["records"],
            reference_hits,
        )
        promotion_summary, promotion_present = build_promotion_summary(review)
        product_pass = True
        if contract.get("requires_specific_products"):
            visible_text = normalize_text(
                " ".join(
                    [
                        *recognized_visible,
                        *(
                            f"{item['product_name']} {item['product_code']} "
                            f"{item['barcode_69']} {item['specification']}"
                            for item in reference_hits
                        ),
                    ]
                )
            )
            product_pass = all(
                normalize_text(required) in visible_text
                for required in contract.get("required_products") or []
            )
        promotion_pass = not contract.get("requires_promotion") or promotion_present
        passed = all(
            [file_pass, period_pass, store_pass, display_pass, duplicate_pass, product_pass, promotion_pass]
        )

        risks = list(review.get("risk_notes") or [])
        advice = list(review.get("supplement_advice") or [])
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
        if not product_pass:
            advice.append("补充合同指定产品清晰可见的现场照片。")
        if not promotion_pass:
            advice.append("补充合同指定促销形式清晰可见的现场照片。")
        advice = list(dict.fromkeys(advice))
        supported = fee if passed else Decimal("0")
        results.append(
            {
                "store_line_no": int(line_no),
                "contract_store_name": store["store_name"],
                "photo_files": photo_files,
                "photo_count": len(photo_files),
                "visible_date": review.get("visible_date"),
                "visible_location": review.get("visible_location"),
                "period_match": period_match,
                "store_match": store_match,
                "store_match_basis": store_match_basis,
                "display_match": display_match,
                "display_standard_basis": display_standard_basis,
                "display_description": observation.get("description"),
                "duplicate_check": duplicate_check,
                "recognized_products": recognized or ["未能可靠识别具体产品"],
                "product_reference_hits": reference_hits,
                "promotion_summary": promotion_summary,
                "promotion_present": promotion_present,
                "sales_product_names": correspondence["names"],
                "sales_product_match": correspondence["status"],
                "sales_product_match_basis": correspondence["basis"],
                "status": "pass" if passed else "supplement",
                "supported_amount": json_number(supported),
                "risk_notes": risks,
                "supplement_advice": advice,
            }
        )
        if not passed:
            exceptions.append(
                exception(
                    "high",
                    "STORE_DISPLAY_EVIDENCE_INCOMPLETE",
                    f"合同第{line_no}家“{store['store_name']}”存在未通过的强制条件。",
                    f"暂不支持该店{json_number(fee)}元。",
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
    suggested = min(claimed, supported)
    passed_count = sum(item["status"] == "pass" for item in results)
    supplement_count = len(results) - passed_count
    expected_claim = fee * len(stores)
    if claimed != expected_claim:
        exceptions.append(
            exception(
                "high", "CONTRACT_CLAIM_CALCULATION_MISMATCH",
                f"申报金额{claimed}不等于{len(stores)}家×{fee}元={expected_claim}元。",
                "合同门店数、单店金额或申报金额不一致。", "更正合同或申报明细。"
            )
        )
    conclusion = (
        "pass" if results and not supplement_count
        else "partial_pass" if passed_count
        else "human_review"
    )
    return {
        "schema_version": "2.0",
        "generated_at": now_utc(),
        "scenario": "promotional_display",
        "case_id": str(case.get("case_id") or Path(case["source_archive"]).stem),
        "case_name": str(case.get("case_name") or contract.get("customer_name") or "堆头核销"),
        "summary": {
            "conclusion": conclusion,
            "claimed_amount": json_number(claimed),
            "contract_store_count": len(stores),
            "fee_per_store": json_number(fee),
            "expected_contract_amount": json_number(expected_claim),
            "passed_store_count": passed_count,
            "supplement_store_count": supplement_count,
            "supported_amount": json_number(supported),
            "suggested_approved_amount": json_number(suggested),
            "temporarily_held_amount": json_number(claimed - suggested),
            "photo_count": photos["file_count"],
            "sales_sku_count": sales["sku_count"],
            "sales_quantity": sales["total_quantity"],
            "sales_retail_amount": sales["retail_amount"],
            "high_exception_count": sum(item["severity"] == "high" for item in exceptions),
            "medium_exception_count": sum(item["severity"] == "medium" for item in exceptions),
        },
        "contract": contract,
        "contract_pdf": pdf,
        "sales": sales,
        "photo_inventory": photos,
        "store_reconciliation": results,
        "unreferenced_photos": unreferenced,
        "exceptions": exceptions,
    }
