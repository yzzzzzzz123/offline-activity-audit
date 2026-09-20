from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterable

from .card_evidence import EvidenceContext, attach_sheet_evidence, enrich_pass_item


SCENARIO_PRESENTATION: dict[str, tuple[str, str]] = {
    "personnel_incentive": ("人员激励", "人员激励核销"),
    "promotional_display": ("堆头/陈列", "堆头陈列核销"),
    "poster_material": ("海报/展示道具", "海报物料核销"),
    "other_expense": ("其他费用", "其他费用核销"),
    "maintenance_fee": ("维护费用", "维护费用核销"),
    "giveaway_promotion": ("额外搭赠", "额外搭赠核销"),
    "price_difference_support": ("价格补差", "价格补差核销"),
    "pos_target_incentive": ("POS达标激励", "POS达标激励核销"),
    "entry_fee": ("进场费", "进场费核销"),
    "self_procured_gift_material": ("客户自采赠品物料", "客户自采赠品物料核销"),
}

# Customer-facing wording only. Unknown control ids deliberately fall back to a
# neutral business label instead of exposing an engineering enum.
CONTROL_PRESENTATION: dict[str, tuple[str, str, str]] = {
    "required_materials": ("材料完整性", "必需材料核验通过", "material"),
    "fee_nature": ("费用性质", "费用类型核验通过", "material"),
    "promotional_contract": ("合同基准", "促销合同核验通过", "material"),
    "settlement": ("结算材料", "结算单核验通过", "material"),
    "sales_delivery_statement": ("销售与出货", "销售/出货明细核验通过", "material"),
    "party_alignment": ("签约主体", "业务主体核验通过", "material"),
    "period_alignment": ("活动期间", "活动期间核验通过", "material"),
    "shipment_reconciliation": ("销售与出货", "出货金额核验通过", "amount"),
    "product_correspondence": ("商品对应", "商品对应关系核验通过", "product"),
    "receipt_execution": ("门店小票", "门店小票执行核验通过", "store"),
    "activity_execution": ("活动现场", "活动现场执行核验通过", "store"),
    "duplicate_evidence": ("照片重复", "照片重复检查通过", "store"),
    "amount_recalculation": ("金额复算", "申报金额复算通过", "amount"),
    "pos_visual_seal": ("POS材料", "盖章POS核验通过", "material"),
    "pos_spreadsheet": ("POS材料", "POS电子表核验通过", "material"),
    "pos_correspondence": ("POS对应", "POS明细对应通过", "amount"),
    "contract_authority": ("合同基准", "合同权威性核验通过", "material"),
    "contract_amount": ("合同金额", "合同金额核验通过", "amount"),
    "source_integrity": ("来源完整性", "来源完整性核验通过", "material"),
    "shelf_photo_coverage": ("上架照片", "货架照片覆盖核验通过", "store"),
    "system_deduction_proof": ("扣款凭证", "系统扣款凭证核验通过", "material"),
    "dealer_recipient": ("激励对象", "激励对象核验通过", "material"),
    "activity_existence": ("活动证明", "活动存在性核验通过", "store"),
    "price_terms": ("补差条款", "价格补差条款核验通过", "amount"),
    "all_store_photo_coverage": ("门店照片", "全门店照片覆盖核验通过", "store"),
    "type_specific_support": ("专项材料", "专项支持材料核验通过", "material"),
    "contract_and_gift_rule": ("赠送规则", "合同与赠送规则核验通过", "material"),
    "purchase_document": ("采购与付款", "采购及付款材料核验通过", "material"),
    "activity_photo_coverage": ("活动返图", "活动返图覆盖核验通过", "store"),
    "gift_quantity_sufficiency": ("赠品数量", "赠品数量核验通过", "amount"),
}

PASS_VALUE_SET = {
    "pass",
    "passed",
    "match",
    "matched",
    "exact",
    "compatible",
    "complete",
    "valid",
    "visible",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _basename(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return re.split(r"[\\/]", text)[-1]


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        clean = _text(value)
        if not clean or clean in seen:
            continue
        seen.add(clean)
        result.append(clean)
    return result


def _case_subject(result: dict[str, Any], default: str) -> str:
    value = _basename(result.get("case_name") or result.get("case_id"))
    return value or default


def _item(
    *,
    category: str,
    title: str,
    subject: str,
    basis: str,
    source_files: Iterable[str] = (),
    source_file_count: int | None = None,
    confidence: str = "high",
    confidence_score: Any = None,
    scope: str = "material",
) -> dict[str, Any]:
    files = _unique(_basename(value) for value in source_files)
    total_files = max(len(files), int(source_file_count or 0))
    normalized_confidence = confidence if confidence in {"high", "medium", "low"} else "high"
    item = {
        "check_id": "",
        "category": category,
        "title": title,
        "subject": subject,
        "basis": _text(basis) or "结构化核销结果已确认该检查项通过。",
        "source_files": files,
        "source_file_count": total_files,
        "confidence": normalized_confidence,
        "scope": scope if scope in {"material", "product", "store", "amount"} else "material",
    }
    try:
        normalized_score = round(float(confidence_score), 2)
    except (TypeError, ValueError):
        normalized_score = None
    if normalized_score is not None and 0 <= normalized_score <= 1:
        item["confidence_score"] = normalized_score
    return item


def _generic_control_items(
    scenario: str, result: dict[str, Any], audit_type: str
) -> list[dict[str, Any]]:
    audit = result.get(f"{scenario}_audit")
    if not isinstance(audit, dict):
        return []
    controls = audit.get("controls")
    if not isinstance(controls, list):
        return []
    subject = _case_subject(result, f"本次{audit_type}材料")
    items: list[dict[str, Any]] = []
    context = EvidenceContext(scenario, result)
    for control in controls:
        if not isinstance(control, dict) or _text(control.get("status")).lower() != "pass":
            continue
        control_id = _text(control.get("control_id"))
        category, title, scope = CONTROL_PRESENTATION.get(
            control_id,
            ("业务规则", "业务规则检查通过", "material"),
        )
        files = context.control_files(control_id)
        items.append(
            _item(
                category=category,
                title=title,
                subject=subject,
                basis=_text(control.get("basis")),
                source_files=files,
                source_file_count=len(files),
                confidence=_text(control.get("confidence")) or "high",
                confidence_score=control.get("confidence_score"),
                scope=scope,
            )
        )
        items[-1]["control_id"] = control_id
    return items


def _display_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    contract = result.get("contract") if isinstance(result.get("contract"), dict) else {}
    summary = result.get("summary") if isinstance(result.get("summary"), dict) else {}
    contract_file = _basename(contract.get("source_file"))
    sales = result.get("sales") if isinstance(result.get("sales"), dict) else {}
    sales_file = _basename(sales.get("source_file"))

    core = result.get("contract_core_reconciliation")
    if isinstance(core, dict):
        core_file = _basename(core.get("source_file")) or contract_file
        for check in core.get("field_checks") or []:
            if not isinstance(check, dict) or _text(check.get("status")).lower() != "pass":
                continue
            label = _text(check.get("label")) or "合同核心字段"
            comparison = _text(check.get("comparison_value"))
            subject = _text(check.get("contract_value")) or contract_file or "合同主文件"
            if comparison and comparison != subject:
                subject = f"合同：{subject}；对照：{comparison}"
            items.append(
                _item(
                    category="合同核心字段",
                    title=f"{label}核验通过",
                    subject=subject,
                    basis=_text(check.get("basis")),
                    source_files=[core_file],
                    confidence_score=check.get("confidence_score"),
                    scope="material",
                )
            )

    if _text(summary.get("sales_internal_status")).lower() == "pass":
        details: list[str] = []
        if _text(summary.get("sales_line_amount_status")).lower() == "pass":
            details.append("逐行金额复算通过")
        if _text(summary.get("sales_printed_quantity_status")).lower() == "exact":
            details.append("打印数量与明细汇总一致")
        if _text(summary.get("sales_printed_amount_status")).lower() == "exact":
            details.append("打印金额与明细汇总一致")
        items.append(
            _item(
                category="销售明细",
                title="销售明细内部复核通过",
                subject=sales_file or "销售明细",
                basis="；".join(details) or "销售明细内部字段与汇总已核对通过。",
                source_files=[sales_file],
                scope="amount",
            )
        )

    attachment = result.get("contract_attachment_sales_reconciliation")
    if isinstance(attachment, dict):
        matched = int(attachment.get("matched_count") or 0)
        record_count = int(attachment.get("record_count") or 0)
        if matched:
            extra_count = len(attachment.get("unmatched_sales_rows") or [])
            basis = f"合同附件已有 {matched}/{record_count or matched} 行与销售明细完成严格字段对应。"
            if extra_count:
                basis += f"本项只记录已配对行；另有 {extra_count} 行销售记录无合同附件基准，仍在错误总览中处理。"
            items.append(
                _item(
                    category="合同附件与销售",
                    title="已配对明细逐行核验通过",
                    subject=f"{matched} 行合同附件明细",
                    basis=basis,
                    source_files=[contract_file, sales_file],
                    scope="product",
                )
            )

    stores = [item for item in result.get("store_reconciliation") or [] if isinstance(item, dict)]
    photo_count = sum(int(store.get("photo_count") or 0) for store in stores)
    if stores and photo_count and all(_text(store.get("duplicate_check")).lower() == "none" for store in stores):
        photo_files = _unique(
            _basename(source)
            for store in stores
            for source in (store.get("photo_files") or [])
        )
        items.append(
            _item(
                category="照片重复",
                title="现场照片重复检查通过",
                subject=f"{photo_count} 张现场照片",
                basis="未发现完全相同的现场照片内容。",
                source_files=photo_files,
                source_file_count=len(photo_files),
                scope="store",
            )
        )

    activity_start = _text(contract.get("activity_start"))
    activity_end = _text(contract.get("activity_end"))
    for store in stores:
        store_name = _text(store.get("contract_store_name")) or "未命名门店"
        photo_files = [_basename(source) for source in store.get("photo_files") or []]
        visible_date = _text(store.get("visible_date"))
        visible_location = _text(store.get("visible_location"))
        if _text(store.get("period_match")).lower() == "match":
            period = " 至 ".join(value for value in (activity_start, activity_end) if value)
            basis = f"照片水印日期 {visible_date or '已识别'}"
            basis += f" 位于合同执行期 {period} 内。" if period else " 已落在约定执行期内。"
            items.append(
                _item(
                    category="活动日期",
                    title="现场日期核验通过",
                    subject=store_name,
                    basis=basis,
                    source_files=photo_files,
                    scope="store",
                )
            )

        store_match = _text(store.get("store_match")).lower()
        if store_match in {"exact", "compatible"}:
            resolution = store.get("location_resolution")
            resolution = resolution if isinstance(resolution, dict) else {}
            confidence = _text(resolution.get("confidence")) or "high"
            if store_match == "exact":
                basis = f"合同门店“{store_name}”与照片水印地点“{visible_location or store_name}”可直接对应。"
            else:
                basis = _text(resolution.get("basis")) or _text(store.get("store_match_basis"))
            items.append(
                _item(
                    category="门店地点",
                    title="门店地点核验通过",
                    subject=store_name,
                    basis=basis,
                    source_files=photo_files,
                    confidence=confidence,
                    confidence_score=resolution.get("confidence_score")
                    if resolution.get("confidence_score") is not None
                    else store.get("confidence_score"),
                    scope="store",
                )
            )

        if _text(store.get("display_match")).lower() == "pass":
            items.append(
                _item(
                    category="陈列标准",
                    title="现场陈列核验通过",
                    subject=store_name,
                    basis=_text(store.get("display_description")),
                    source_files=photo_files,
                    scope="store",
                )
            )

        if _text(store.get("photo_knowledge_match")).lower() in PASS_VALUE_SET:
            items.append(
                _item(
                    category="现场商品",
                    title="现场商品识别通过",
                    subject=store_name,
                    basis=_text(store.get("photo_knowledge_match_basis"))
                    or "照片可见商品已与商品知识库完成核对。",
                    source_files=photo_files,
                    scope="product",
                )
            )

        if _text(store.get("photo_contract_product_status")).lower() in PASS_VALUE_SET:
            items.append(
                _item(
                    category="合同商品范围",
                    title="现场商品合同范围核验通过",
                    subject=store_name,
                    basis=_text(store.get("photo_contract_product_basis"))
                    or "照片商品已落在合同约定范围内。",
                    source_files=[*photo_files, contract_file],
                    scope="product",
                )
            )

        if _text(store.get("amount_rule_status")).lower() == "pass":
            items.append(
                _item(
                    category="金额规则",
                    title="单店金额规则核验通过",
                    subject=store_name,
                    basis=_text(store.get("amount_rule_basis")),
                    source_files=[contract_file, *photo_files],
                    scope="amount",
                )
            )
    return items


def _personnel_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    sales = result.get("sales") if isinstance(result.get("sales"), dict) else {}
    settlement = result.get("settlement") if isinstance(result.get("settlement"), dict) else {}
    sales_file = _basename(sales.get("source_file"))
    settlement_file = _basename(settlement.get("source_file"))
    for record in result.get("sku_reconciliation") or []:
        if not isinstance(record, dict):
            continue
        product = (
            _text(record.get("knowledge_product_name"))
            or _text(record.get("excel_product_name"))
            or _text(record.get("settlement_product_name"))
            or f"结算第{record.get('line_no') or '?'}行"
        )
        confidence = _text(record.get("mapping_confidence")) or "high"
        rows = "、".join(str(value) for value in record.get("excel_rows") or [])
        if (
            _text(record.get("knowledge_status")).lower() in {"exact", "matched", "fuzzy_matched"}
            and _text(record.get("knowledge_barcode_match")).lower() == "exact"
        ):
            barcode = _text(record.get("mapped_barcode")) or _text(record.get("knowledge_barcode_69"))
            basis = f"销售明细第{rows or '对应'}行的69码 {barcode} 与商品知识库一致；商品名称已完成辅助核对。"
            items.append(
                _item(
                    category="商品知识库",
                    title="销售商品知识库核验通过",
                    subject=product,
                    basis=basis,
                    source_files=[sales_file],
                    confidence=confidence,
                    confidence_score=record.get("confidence_score"),
                    scope="product",
                )
            )
        if _text(record.get("mapping_status")).lower() == "matched":
            items.append(
                _item(
                    category="结算商品对应",
                    title="结算商品对应通过",
                    subject=product,
                    basis=(
                        f"结算第{record.get('line_no') or '?'}行已对应销售明细第{rows or '对应'}行；"
                        "结算数量与尚未使用的销售商品形成唯一对应。"
                    ),
                    source_files=[sales_file, settlement_file],
                    confidence=confidence,
                    confidence_score=record.get("confidence_score"),
                    scope="product",
                )
            )
        if _text(record.get("quantity_status")).lower() == "match":
            quantity = record.get("settlement_quantity")
            items.append(
                _item(
                    category="销售数量",
                    title="销售数量核验通过",
                    subject=product,
                    basis=f"结算数量 {quantity} 与销售明细汇总数量 {record.get('excel_quantity')} 一致。",
                    source_files=[sales_file, settlement_file],
                    scope="amount",
                )
            )
        if _text(record.get("amount_status")).lower() == "match":
            items.append(
                _item(
                    category="激励金额",
                    title="单品激励金额复算通过",
                    subject=product,
                    basis=(
                        f"数量 {record.get('settlement_quantity')} × 单位奖励 {record.get('unit_reward')} 元"
                        f" = {record.get('calculated_settlement_reward')} 元，与结算金额一致。"
                    ),
                    source_files=[settlement_file],
                    scope="amount",
                )
            )

    evidence_by_id = {
        _text(item.get("transfer_id")): item
        for item in result.get("transfer_evidence") or []
        if isinstance(item, dict)
    }
    for transfer in result.get("store_transfer_reconciliation") or []:
        if not isinstance(transfer, dict):
            continue
        if _text(transfer.get("status")).lower() != "amount_match_identity_unverified":
            continue
        evidence = evidence_by_id.get(_text(transfer.get("transfer_id"))) or {}
        items.append(
            _item(
                category="转账金额",
                title="转账金额子项核验通过",
                subject=_text(transfer.get("store_name")) or "对应门店",
                basis=(
                    f"应付 {transfer.get('expected_reward_amount')} 元，截图可见转账 {transfer.get('transfer_amount')} 元，"
                    "金额一致；收款身份和完整日期未通过的部分仍在错误总览中处理。"
                ),
                source_files=evidence.get("source_files") or [],
                scope="amount",
            )
        )
    return items


def _poster_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    audit = result.get("poster_material_audit")
    if not isinstance(audit, dict):
        return []
    items: list[dict[str, Any]] = []
    contract = audit.get("contract") if isinstance(audit.get("contract"), dict) else {}
    settlement = audit.get("settlement") if isinstance(audit.get("settlement"), dict) else {}
    receipt = audit.get("invoice_receipt") if isinstance(audit.get("invoice_receipt"), dict) else {}
    if _text(contract.get("customer_seal_visible")).lower() == "visible":
        items.append(
            _item(
                category="合同签章",
                title="合同客户印章核验通过",
                subject=_text(contract.get("customer_name")) or "物料制作合同",
                basis="合同客户印章清晰可见。",
                source_files=[contract.get("source_file")],
                scope="material",
            )
        )
    if _text(settlement.get("customer_seal_visible")).lower() == "visible":
        items.append(
            _item(
                category="结算签章",
                title="结算单客户印章核验通过",
                subject=_text(settlement.get("customer_name")) or "物料制作结算单",
                basis="市场费用结算单模板及客户印章清晰可见。",
                source_files=[settlement.get("source_file")],
                scope="material",
            )
        )
    if _text(receipt.get("issuer_stamp_visible")).lower() == "visible":
        items.append(
            _item(
                category="票据签章",
                title="票据印章核验通过",
                subject=_text(receipt.get("issuer_name")) or "物料制作票据",
                basis="票据开具方印章清晰可见。",
                source_files=[receipt.get("source_file")],
                scope="material",
            )
        )

    start = _text(contract.get("activity_start"))
    end = _text(contract.get("activity_end"))
    for photo in audit.get("field_photos") or []:
        if not isinstance(photo, dict):
            continue
        source = _basename(photo.get("source_file"))
        date = _text(photo.get("watermark_date"))
        time = _text(photo.get("watermark_time"))
        location = _text(photo.get("watermark_location"))
        if (
            _text(photo.get("date_visibility")).lower() == "visible"
            and _text(photo.get("time_visibility")).lower() == "visible"
            and _text(photo.get("location_visibility")).lower() == "visible"
            and (not start or not end or start <= date <= end)
        ):
            items.append(
                _item(
                    category="现场水印",
                    title="现场照片水印核验通过",
                    subject=location or source,
                    basis=f"照片显示 {date} {time}、{location}，日期位于合同执行期内。",
                    source_files=[source],
                    scope="store",
                )
            )
        materials = [item for item in photo.get("observed_materials") or [] if isinstance(item, dict)]
        if _text(photo.get("finished_effect_visible")).lower() == "visible" and materials:
            material_basis = "；".join(_text(item.get("basis")) for item in materials if item.get("basis"))
            placement = _text(photo.get("display_position"))
            basis = "；".join(value for value in (material_basis, f"摆放位置：{placement}" if placement else "") if value)
            items.append(
                _item(
                    category="成品与摆放",
                    title="现场成品与摆放核验通过",
                    subject=location or source,
                    basis=basis,
                    source_files=[source],
                    scope="store",
                )
            )
    return items


def _other_expense_items(result: dict[str, Any]) -> list[dict[str, Any]]:
    audit = result.get("other_expense_audit")
    if not isinstance(audit, dict):
        return []
    items: list[dict[str, Any]] = []
    for document in audit.get("documents") or []:
        if not isinstance(document, dict):
            continue
        source = _basename(document.get("source_file"))
        title = _text(document.get("title")) or _text(document.get("document_type")) or source
        if _text(document.get("signed_visible")).lower() == "visible":
            items.append(
                _item(
                    category="签署信息",
                    title="文件签署信息核验通过",
                    subject=title,
                    basis="该文件的签署或签章信息清晰可见；文件的业务归类及其他阻断问题仍单独判断。",
                    source_files=[source],
                    scope="material",
                )
            )
        activity = document.get("activity_evidence")
        if not isinstance(activity, dict):
            continue
        date = _text(activity.get("watermark_date"))
        time = _text(activity.get("watermark_time"))
        location = _text(activity.get("watermark_location"))
        content = _text(activity.get("activity_content"))
        if date and time and location:
            items.append(
                _item(
                    category="活动现场",
                    title="活动照片水印信息核验通过",
                    subject=location,
                    basis=f"照片水印清晰显示 {date} {time}、{location}。",
                    source_files=[source],
                    scope="store",
                )
            )
        if content:
            items.append(
                _item(
                    category="活动现场",
                    title="活动内容可视性核验通过",
                    subject=location or title,
                    basis=content,
                    source_files=[source],
                    scope="store",
                )
            )
    return items


def _photo_reconciliation_items(
    scenario: str, result: dict[str, Any]
) -> list[dict[str, Any]]:
    audit = result.get(f"{scenario}_audit")
    if not isinstance(audit, dict):
        return []
    items: list[dict[str, Any]] = []
    for photo in audit.get("photo_reconciliation") or []:
        if not isinstance(photo, dict):
            continue
        passed = _text(photo.get("status")).lower() == "pass"
        if scenario == "entry_fee":
            passed = passed or (
                _text(photo.get("visual_status")).lower() == "pass"
                and bool(photo.get("matched_store"))
            )
        if not passed:
            continue
        source = _basename(photo.get("source_file"))
        store = _text(photo.get("matched_store")) or _text(photo.get("routed_store")) or source
        visible_store = _text(photo.get("visible_store"))
        visible_location = _text(photo.get("visible_location"))
        visible_date = _text(photo.get("visible_date"))
        facts = [
            f"照片水印门店：{visible_store}" if visible_store else "",
            f"照片水印地点：{visible_location}" if visible_location else "",
            f"照片日期：{visible_date}" if visible_date else "",
            _text(photo.get("store_match_basis")),
        ]
        items.append(
            _item(
                category="门店返图",
                title="门店照片对应核验通过",
                subject=store,
                basis="；".join(value for value in facts if value) or "现场照片已与对应门店完成核对。",
                source_files=[source],
                scope="store",
            )
        )
    return items


def _fallback_view_items(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for row in sheet.get("rows") or []:
        if not isinstance(row, dict) or _text(row.get("status")).lower() != "pass":
            continue
        values = [_text(value) for value in row.get("values") or []]
        basis = next((value for value in reversed(values) if value), "检查结果已通过。")
        items.append(
            _item(
                category="核销明细",
                title="明细检查项通过",
                subject=_text(row.get("heading")) or f"第{row.get('excel_row') or '?'}行",
                basis=basis,
                confidence=_text(row.get("confidence")) or "high",
                confidence_score=row.get("confidence_score"),
                scope="material" if _text(row.get("section")) != "detail" else "product",
            )
        )
    return items


def _deduplicate(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in items:
        key = (
            item.get("category"),
            item.get("title"),
            item.get("subject"),
            item.get("basis"),
            tuple(item.get("source_files") or []),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


def _scenario_items(
    scenario: str, result: dict[str, Any], sheet: dict[str, Any], audit_type: str
) -> list[dict[str, Any]]:
    if scenario == "promotional_display":
        items = _display_items(result)
    elif scenario == "personnel_incentive":
        items = _personnel_items(result)
    elif scenario == "poster_material":
        items = _poster_items(result)
    elif scenario == "other_expense":
        items = _other_expense_items(result)
    else:
        items = _generic_control_items(scenario, result, audit_type)
        if scenario in {"entry_fee", "self_procured_gift_material"}:
            items.extend(_photo_reconciliation_items(scenario, result))
    items = _deduplicate(items)
    return items or _fallback_view_items(sheet)


def _pdf_items(sheets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Use the saved PDF checks verbatim, including every packet of one type."""
    items = []
    for sheet in sheets:
        for row in sheet.get("rows") or []:
            if row.get("status") != "pass":
                continue
            evidence = deepcopy(row.get("card_evidence") or {})
            values = row.get("values") or []
            category = row.get("check_category") or "PDF审核要点"
            item = _item(category=category, title=category + "核验通过",
                         subject=sheet.get("source_archive") or sheet["audit_type_label"],
                         basis=values[1] if len(values) > 1 else row.get("heading"),
                         source_files=evidence.get("source_files") or [],
                         source_file_count=evidence.get("source_file_count"),
                         scope="amount" if row.get("numeric") else "material")
            item.update(evidence=evidence, control_id=row.get("rule_id"), archive_id=sheet.get("archive_id"))
            items.append(item)
    return items


def build_pass_check_log(
    view: dict[str, Any], results_by_scenario: dict[str, dict[str, Any]]
) -> dict[str, Any]:
    groups: list[dict[str, Any]] = []
    sequence = 0
    scope_counts = {"material": 0, "product": 0, "store": 0, "amount": 0}
    seen_scenarios: set[str] = set()
    for sheet in view.get("sheets") or []:
        if not isinstance(sheet, dict):
            continue
        if sheet.get("projection_kind") == "material_diagnostic":
            # Classifying submitted files does not establish a passed business
            # check, even when the hinted business scenario is known.
            continue
        scenario = _text(sheet.get("scenario"))
        if not scenario or scenario in seen_scenarios:
            continue
        seen_scenarios.add(scenario)
        if sheet.get("projection_kind") == "pdf_policy":
            audit_type, title = sheet["audit_type_label"], sheet["title"]
            items = _pdf_items([s for s in view["sheets"]
                                if s.get("scenario") == scenario and s.get("projection_kind") == "pdf_policy"])
        else:
            audit_type, title = SCENARIO_PRESENTATION.get(
                scenario,
                (_text(sheet.get("name")) or "其他核销", _text(sheet.get("name")) or "其他核销"),
            )
            result = results_by_scenario.get(scenario)
            result = result if isinstance(result, dict) else {}
            items = _scenario_items(scenario, result, sheet, audit_type)
            evidence_context = EvidenceContext(scenario, result)
            for item in items:
                enrich_pass_item(evidence_context, item)
        category_counts: dict[str, int] = {}
        for item in items:
            sequence += 1
            item["check_id"] = f"pass-{sequence:04d}"
            category = _text(item.get("category")) or "核销检查"
            category_counts[category] = category_counts.get(category, 0) + 1
            scope = _text(item.get("scope"))
            if scope in scope_counts:
                scope_counts[scope] += 1
        groups.append(
            {
                "scenario": scenario,
                "audit_type": audit_type,
                "title": title,
                "item_count": len(items),
                "category_counts": category_counts,
                "items": items,
            }
        )
    return {
        "schema_version": "1.1",
        "total": sequence,
        "audit_type_count": len(groups),
        "types_with_pass": sum(1 for group in groups if group["item_count"] > 0),
        "scope_counts": scope_counts,
        "groups": groups,
    }


def attach_pass_check_log(
    view: dict[str, Any] | None,
    results_by_scenario: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    if not isinstance(view, dict):
        return view
    enriched = deepcopy(view)
    enriched["pass_check_log"] = build_pass_check_log(enriched, results_by_scenario)
    for sheet in enriched.get("sheets") or []:
        if sheet.get("projection_kind") in {"material_diagnostic", "pdf_policy"} or sheet.get("decision_source") == "material_content":
            continue
        scenario = _text(sheet.get("scenario"))
        attach_sheet_evidence(sheet, EvidenceContext(scenario, results_by_scenario.get(scenario) or {}))
    return enriched


def load_workspace_results(workspace: Path) -> dict[str, dict[str, Any]]:
    result_root = workspace / "analysis" / "results"
    if not result_root.is_dir():
        return {}
    results: dict[str, dict[str, Any]] = {}
    try:
        cases = json.loads((workspace / "analysis" / "input-cases.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        cases = {}
    for path in sorted(result_root.glob("*.json")):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict):
            continue
        scenario = _text(value.get("scenario")) or path.stem
        if scenario in SCENARIO_PRESENTATION:
            # Presentation companions only: never write back into saved results.
            try:
                evidence = json.loads((workspace / "analysis" / "evidence" / path.name).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                evidence = {}
            value["_presentation_evidence"] = evidence if isinstance(evidence, dict) else {}
            value["_presentation_case"] = cases.get(scenario, {}) if isinstance(cases, dict) else {}
            results[scenario] = value
    return results
