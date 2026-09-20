from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .common import optional_iso_date
from .common import (
    POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX,
    AuditError,
    money,
    normalize_text,
    now_utc,
)


MATERIAL_LABELS = {
    "lightbox": "灯箱",
    "counter_display": "台上架/台面展示架",
    "poster": "海报",
    "shelf_card": "货架卡",
    "standee": "立牌",
    "other": "其他物料",
}


def _optional_money(value: Any, *, label: str) -> Decimal | None:
    if value is None:
        return None
    return money(value, label=label)


def _company_key(value: Any) -> str:
    text = normalize_text(value)
    for suffix in (
        "个体工商户",
        "有限责任公司",
        "股份有限公司",
        "有限公司",
        "公司",
    ):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
    return text


def _names_compatible(left: Any, right: Any) -> bool:
    first = _company_key(left)
    second = _company_key(right)
    if not first or not second:
        return False
    if first == second:
        return True
    if min(len(first), len(second)) >= 5 and (first in second or second in first):
        return True
    return SequenceMatcher(None, first, second).ratio() >= 0.84


def _material_quantities(lines: list[dict[str, Any]]) -> Counter[str]:
    quantities: Counter[str] = Counter()
    for item in lines:
        quantity = item.get("quantity")
        if quantity is None:
            continue
        material_type = str(item.get("item_type") or "other")
        quantities[material_type] += int(Decimal(str(quantity)))
    return quantities


def _material_lines_complete(
    invoice_lines: list[dict[str, Any]],
    contract_lines: list[dict[str, Any]],
) -> bool:
    expected_types = {
        str(item.get("item_type") or "other") for item in contract_lines
    }
    if not invoice_lines or len(invoice_lines) < len(expected_types):
        return False
    invoice_types = {
        str(item.get("item_type") or "other") for item in invoice_lines
    }
    if not expected_types.issubset(invoice_types):
        return False
    for item in invoice_lines:
        description = normalize_text(item.get("description"))
        if description in {"物料", "物料制作", "广告制作", "制作费"}:
            return False
        if any(item.get(field) is None for field in ("quantity", "unit_price", "subtotal")):
            return False
    return True


def _amounts_equal(*values: Decimal | None) -> bool:
    present = [value for value in values if value is not None]
    return len(present) >= 2 and len(set(present)) == 1


def _contract_scope_summary(lines: list[dict[str, Any]]) -> str:
    summaries: list[str] = []
    for item in lines:
        material_type = str(item.get("item_type") or "other")
        description = normalize_text(item.get("description")) or MATERIAL_LABELS.get(
            material_type,
            "其他物料",
        )
        quantity = item.get("quantity")
        quantity_text = "未列明" if quantity is None else format(Decimal(str(quantity)), "f")
        summaries.append(f"{description}（合同数量{quantity_text}）")
    return "、".join(summaries) or "合同列明的各项物料及数量"


def _issue(
    code: str,
    title: str,
    source_files: list[str],
    observed: str,
    expected: str,
    impact: str,
    resubmission: str,
    *,
    severity: str = "high",
    confidence: str = "high",
) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "source_files": source_files,
        "observed": observed,
        "expected": expected,
        "impact": impact,
        "resubmission": resubmission,
        "severity": severity,
        "confidence": confidence,
    }


def _photo_digest(photo: dict[str, Any]) -> str:
    materials = "、".join(
        MATERIAL_LABELS.get(str(item.get("item_type")), "其他物料")
        for item in photo.get("observed_materials") or []
    ) or "物料类型未确认"
    watermark = " ".join(
        value
        for value in (
            str(photo.get("watermark_date") or "").strip(),
            str(photo.get("watermark_time") or "").strip(),
            str(photo.get("watermark_location") or "").strip(),
        )
        if value
    ) or "水印信息不完整"
    dimension = (
        str(photo.get("dimension_text"))
        if photo.get("dimension_evidence") == "visible" and photo.get("dimension_text")
        else "无尺寸依据"
    )
    content = str(photo.get("content_summary") or "成品内容未确认").rstrip("；。 ")
    return (
        f"{photo['source_file']}：{watermark}；{materials}；"
        f"{content}；{dimension}"
    )


def audit_poster_material_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "poster_material":
        raise AuditError("海报物料核销收到错误的场景类型")

    contract = evidence["contract"]
    invoice = evidence["invoice_receipt"]
    settlement = evidence["settlement"]
    photos = list(evidence.get("field_photos") or [])
    issues: list[dict[str, Any]] = []

    contract_file = str(contract["source_file"])
    invoice_file = str(invoice["source_file"])
    settlement_file = str(settlement["source_file"])
    photo_files = [str(photo["source_file"]) for photo in photos]
    contract_lines = list(contract.get("item_requirements") or [])
    invoice_lines = list(invoice.get("line_items") or [])
    settlement_lines = list(settlement.get("line_items") or [])

    contract_problems: list[str] = []
    referenced = contract.get("referenced_attachment") or {}
    attachment_files = [Path(path).name for path in case.get("contract_attachment_files") or []]
    if referenced.get("mentioned") and not attachment_files:
        description = str(referenced.get("description") or "合同所述附件")
        contract_problems.append(f"合同正文引用附件：{description}；但压缩包未提交该附件")
    if contract.get("customer_seal_visible") != "visible":
        contract_problems.append("合同客户签章未清晰显示")
    if not contract.get("customer_name"):
        contract_problems.append("合同签订方无法确认")
    if not contract.get("activity_start") or not contract.get("activity_end"):
        contract_problems.append("合同活动周期不完整")
    if not contract_lines:
        contract_problems.append("合同未识别到可核对的物料项目和数量")
    if contract_problems:
        issues.append(
            _issue(
                "contract_incomplete",
                "合同材料不完整",
                [contract_file],
                "；".join(contract_problems),
                "签章合同及其正文明确引用的门店清单、报价、设计或规格附件必须一并提交。",
                "无法确认合同完整范围，也无法把现场门店逐一对应到合同清单。",
                "补交合同中引用的完整附件；如附件与合同分开盖章，需同时保留可识别的合同关联信息。",
            )
        )

    invoice_problems: list[str] = []
    if invoice.get("document_type") not in {"invoice", "receipt"}:
        invoice_problems.append("单据类型不是可确认的发票或收据")
    if not _names_compatible(contract.get("customer_name"), invoice.get("title_name")):
        invoice_problems.append(
            f"票据抬头“{invoice.get('title_name') or '未识别'}”未能对应合同签订方"
        )
    if not invoice.get("issue_date"):
        invoice_problems.append("开票/收据日期未识别")
    if invoice.get("total_amount") is None:
        invoice_problems.append("票据金额未识别")
    if not _material_lines_complete(invoice_lines, contract_lines):
        invoice_problems.append(
            "票据仅有笼统项目或缺少合同各物料分项的数量、单价和小计"
        )
    if invoice_problems:
        def visible_value(value: Any) -> str:
            return "未列明" if value is None else str(value)

        ticket_lines = "；".join(
            f"{item.get('description')}，数量={visible_value(item.get('quantity'))}，"
            f"单价={visible_value(item.get('unit_price'))}，"
            f"小计={visible_value(item.get('subtotal'))}"
            for item in invoice_lines
        ) or "未识别到费用行"
        issues.append(
            _issue(
                "invoice_detail_incomplete",
                "票据费用明细不完整",
                [invoice_file],
                f"识别费用行：{ticket_lines}。问题：{'；'.join(invoice_problems)}",
                "发票或收据需显示合同公司抬头、开票时间、每类海报/物料的名称、数量、单价、小计及总额。",
                "单据虽有总额数字，但不能证明该费用具体对应"
                f"{_contract_scope_summary(contract_lines)}。",
                "补交分项发票/收据，或补交与原票据可唯一关联且盖章的费用明细附件。",
            )
        )

    settlement_problems: list[str] = []
    template_title = normalize_text(settlement.get("template_title"))
    if "结算单" not in template_title:
        settlement_problems.append("未识别到公司结算单标题")
    if settlement.get("customer_seal_visible") != "visible":
        settlement_problems.append("结算单客户盖章未清晰显示")
    if not settlement.get("settlement_date"):
        settlement_problems.append("结算日期未识别")
    if not _names_compatible(contract.get("customer_name"), settlement.get("customer_name")):
        settlement_problems.append("结算单客户名称未能对应合同签订方")
    if not settlement_lines or any(
        item.get(field) is None
        for item in settlement_lines
        for field in ("quantity", "unit_price")
    ):
        settlement_problems.append("结算单物料数量或单价不完整")
    if settlement.get("total_amount") is None:
        settlement_problems.append("结算单合计金额未识别")
    if settlement_problems:
        issues.append(
            _issue(
                "settlement_invalid",
                "结算单不合格",
                [settlement_file],
                "；".join(settlement_problems),
                "使用可识别的公司统一结算单，完整列示客户、周期、分项数量、单价、金额、日期，并加盖客户章。",
                "结算依据不完整，不能进入自动核销。",
                "按公司统一模板重新提交完整结算单并加盖客户章。",
            )
        )

    contract_amount = _optional_money(contract.get("activity_budget"), label="合同预算")
    invoice_amount = _optional_money(invoice.get("total_amount"), label="票据金额")
    settlement_amount = _optional_money(settlement.get("total_amount"), label="结算金额")
    amount_problems: list[str] = []
    present_amounts = [
        ("合同", contract_amount),
        ("票据", invoice_amount),
        ("结算单", settlement_amount),
    ]
    if any(value is None for _, value in present_amounts):
        amount_problems.append("合同、票据或结算单存在未识别金额")
    elif not _amounts_equal(*(value for _, value in present_amounts)):
        amount_problems.append(
            "、".join(f"{label}{value}元" for label, value in present_amounts)
        )
    contract_quantities = _material_quantities(contract_lines)
    settlement_quantities = _material_quantities(settlement_lines)
    if contract_quantities and settlement_quantities != contract_quantities:
        amount_problems.append(
            f"合同数量{dict(contract_quantities)}，结算单数量{dict(settlement_quantities)}"
        )
    if amount_problems:
        issues.append(
            _issue(
                "amount_or_item_mismatch",
                "金额或项目不一致",
                [contract_file, invoice_file, settlement_file],
                "；".join(amount_problems),
                "合同、票据和结算单的项目、数量、单价、已列示小计及总额必须精确一致。",
                "金额证据链未闭合。",
                "更正冲突单据并重新提交，保留原始项目和金额，不得用总额倒推单价。",
            )
        )

    photo_problems: list[str] = []
    activity_start = optional_iso_date(contract.get("activity_start"))
    activity_end = optional_iso_date(contract.get("activity_end"))
    missing_watermarks: list[str] = []
    outside_period: list[str] = []
    content_or_position_gaps: list[str] = []
    dimension_gaps: list[str] = []
    observed_units: Counter[str] = Counter()
    locations: set[str] = set()
    photo_type_counts: Counter[str] = Counter()

    for photo in photos:
        source_file = str(photo["source_file"])
        if any(
            photo.get(field) != "visible"
            for field in ("date_visibility", "time_visibility", "location_visibility")
        ) or not all(
            photo.get(field)
            for field in ("watermark_date", "watermark_time", "watermark_location")
        ):
            missing_watermarks.append(source_file)
        visible_date = optional_iso_date(photo.get("watermark_date"))
        if (
            visible_date is None
            or activity_start is None
            or activity_end is None
            or not activity_start <= visible_date <= activity_end
        ):
            outside_period.append(source_file)
        location_key = normalize_text(photo.get("watermark_location"))
        if location_key:
            locations.add(location_key)
        if (
            photo.get("finished_effect_visible") != "visible"
            or photo.get("display_position_visible") != "visible"
        ):
            content_or_position_gaps.append(source_file)
        if photo.get("dimension_evidence") != "visible":
            dimension_gaps.append(source_file)
        for item in photo.get("observed_materials") or []:
            material_type = str(item.get("item_type") or "other")
            photo_type_counts[material_type] += 1
            count = item.get("visible_unit_count")
            if count is not None:
                observed_units[material_type] += int(count)

    calibration_notes = [
        str(note)[len(POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX) :]
        for note in evidence.get("extraction_notes") or []
        if str(note).startswith(POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX)
    ]
    if len(calibration_notes) > 1:
        raise AuditError("海报物料数量视觉回归校准备注重复")
    if calibration_notes:
        try:
            quantity_calibration = json.loads(calibration_notes[0])
        except json.JSONDecodeError as exc:
            raise AuditError("海报物料数量视觉回归校准备注无效") from exc
        coverage = quantity_calibration.get("accepted_material_coverage") or []
        for fact in coverage:
            material_type = str(fact["item_type"])
            observed_units[material_type] = int(fact["visible_unit_count"])
            photo_type_counts[material_type] = int(fact["photo_count"])

    if missing_watermarks:
        photo_problems.append("缺少完整日期/时间/地点水印：" + "、".join(missing_watermarks))
    if outside_period:
        photo_problems.append("水印日期不在合同周期或无法确认：" + "、".join(outside_period))
    if content_or_position_gaps:
        photo_problems.append("成品效果或展示位置未完整显示：" + "、".join(content_or_position_gaps))
    if dimension_gaps:
        photo_problems.append(
            f"{len(dimension_gaps)}张照片均未提供尺寸文字、尺标或可靠比例依据"
        )
    quantity_gaps: list[str] = []
    for material_type, expected_quantity in contract_quantities.items():
        supported_quantity = observed_units[material_type]
        if supported_quantity < expected_quantity:
            quantity_gaps.append(
                f"{MATERIAL_LABELS.get(material_type, material_type)}合同{expected_quantity}个，"
                f"照片可明确计数{supported_quantity}个（涉及{photo_type_counts[material_type]}张照片）"
            )
    store_count = contract.get("store_count")
    if store_count is not None and len(locations) < int(store_count):
        quantity_gaps.append(
            f"合同涉及{int(store_count)}家门店，照片仅识别到{len(locations)}个不同地点"
        )
    if quantity_gaps:
        photo_problems.append("；".join(quantity_gaps))

    if photo_problems:
        digest = "\n".join(_photo_digest(photo) for photo in photos)
        issues.append(
            _issue(
                "photo_execution_incomplete",
                "现场照片执行证据不完整",
                photo_files,
                f"共提交{len(photos)}张返图、识别{len(locations)}个地点。\n逐张识别：\n{digest}\n共同问题：{'；'.join(photo_problems)}",
                "活动周期内的水印照片须逐店/逐成品覆盖合同数量，并共同清晰展示日期、拍摄时间、地点、成品内容、尺寸和实际展示位置。",
                "现有照片可以证明部分门店已摆放物料，但不能证明合同约定的全部制作数量、门店范围和成品尺寸。",
                "补交缺失门店/成品的水印照片；每张保留完整日期、时间和地点，并用尺寸标注、卷尺或可核验比例完整展示成品尺寸与安装/摆放位置。",
            )
        )

    claim_amount = next(
        (value for value in (settlement_amount, contract_amount, invoice_amount) if value is not None),
        Decimal("0.00"),
    )
    if issues:
        supported_amount = Decimal("0.00")
        conclusion = "human_review"
    else:
        supported_amount = min(
            value
            for value in (contract_amount, invoice_amount, settlement_amount)
            if value is not None
        )
        conclusion = "pass"
    held_amount = max(Decimal("0.00"), claim_amount - supported_amount)

    exceptions = [
        {
            "severity": issue["severity"],
            "code": issue["code"],
            "message": issue["title"],
            "impact": issue["impact"],
            "suggestion": issue["resubmission"],
            "source": "、".join(issue["source_files"]),
        }
        for issue in issues
    ]

    return {
        "schema_version": "2.1",
        "generated_at": now_utc(),
        "scenario": "poster_material",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": f"{contract.get('customer_name') or Path(case['source_archive']).stem}海报/物料制作核销",
        "summary": {
            "conclusion": conclusion,
            "claimed_amount": float(claim_amount),
            "suggested_approved_amount": float(supported_amount),
            "temporarily_held_amount": float(held_amount),
            "high_exception_count": sum(issue["severity"] == "high" for issue in issues),
            "medium_exception_count": sum(issue["severity"] == "medium" for issue in issues),
            "error_group_count": len(issues),
            "photo_count": len(photos),
            "unique_photo_location_count": len(locations),
            "contract_expected_unit_count": sum(contract_quantities.values()),
            "photo_counted_unit_count": sum(observed_units.values()),
            "activity_budget": float(contract_amount) if contract_amount is not None else None,
        },
        "poster_material_audit": {
            "contract": contract,
            "invoice_receipt": invoice,
            "settlement": settlement,
            "field_photos": photos,
            "excluded_files": [Path(path).name for path in case.get("excluded_files") or []],
            "issues": issues,
        },
        "exceptions": exceptions,
    }
