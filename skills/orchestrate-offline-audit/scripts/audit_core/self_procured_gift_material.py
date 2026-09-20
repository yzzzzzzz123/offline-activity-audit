from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .common import optional_iso_date, normalized_name_score
from .common import AuditError, json_number, money, now_utc, sha256_file
from .excel_sources import read_self_procured_pos_summary


CENT = Decimal("0.01")


def _issue(
    code: str,
    title: str,
    source_files: list[str],
    observed: str,
    expected: str,
    impact: str,
    resubmission: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "source_files": list(dict.fromkeys(name for name in source_files if name)),
        "observed": observed,
        "expected": expected,
        "impact": impact,
        "resubmission": resubmission,
        "severity": "high",
        "confidence": "high",
    }


def _control(controls: list[dict[str, str]], control_id: str, passed: bool, basis: str) -> None:
    controls.append(
        {"control_id": control_id, "status": "pass" if passed else "fail", "basis": basis}
    )


def _documents_of_type(documents: list[dict[str, Any]], document_type: str) -> list[dict[str, Any]]:
    return [item for item in documents if str(item.get("document_type")) == document_type]


def _sources(documents: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("source_file") or "") for item in documents]


def _decimal(value: Any, *, label: str) -> Decimal | None:
    if value is None:
        return None
    return money(value, label=label)


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _equal(left: Any, right: Any) -> bool:
    a = _decimal(left, label="左侧金额/数量")
    b = _decimal(right, label="右侧金额/数量")
    return a is not None and b is not None and abs(a - b) <= CENT


def _same_material(left: Any, right: Any) -> bool:
    return normalized_name_score(left, right) >= 0.72


def _visual_pos_representations(
    documents: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    named = [
        item for item in documents
        if item.get("document_type") == "stamped_pos_data"
        and item.get("role") == "stamped_pos_data"
    ]
    classified = [
        item for item in documents
        if item.get("document_type") == "stamped_pos_data"
        and item.get("role") != "stamped_pos_data"
    ]
    representations: list[list[dict[str, Any]]] = []
    if named:
        representations.append(named)
    if classified:
        representations.append(classified)
    return representations


def _pos_representation_matches(
    documents: list[dict[str, Any]],
    spreadsheet: dict[str, Any],
) -> tuple[bool, str]:
    visual_rows = [row for item in documents for row in (item.get("pos_rows") or [])]
    if not visual_rows:
        return False, "视觉POS没有可读取逐门店明细"
    unused = set(range(len(visual_rows)))
    problems: list[str] = []
    for record in spreadsheet.get("records") or []:
        ranked = sorted(
            (
                (normalized_name_score(record.get("store_name"), visual_rows[index].get("store_name")), index)
                for index in unused
                if _equal(record.get("sales_quantity"), visual_rows[index].get("sales_quantity"))
                and _equal(record.get("sales_amount"), visual_rows[index].get("sales_amount"))
            ),
            reverse=True,
        )
        if not ranked or ranked[0][0] < 0.82 or (
            len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.03
        ):
            problems.append(
                f"Excel第{record.get('excel_row')}行{record.get('store_name')}="
                f"{record.get('sales_quantity')}/{record.get('sales_amount')}"
            )
            continue
        unused.remove(ranked[0][1])
    printed_quantities = [
        item.get("pos_total_quantity")
        for item in documents
        if item.get("pos_total_quantity") is not None
    ]
    printed_amounts = [
        item.get("pos_total_sales_amount")
        for item in documents
        if item.get("pos_total_sales_amount") is not None
    ]
    quantity_total_pass = bool(printed_quantities) and any(
        _equal(value, spreadsheet.get("total_sales_quantity")) for value in printed_quantities
    )
    amount_total_pass = bool(printed_amounts) and any(
        _equal(value, spreadsheet.get("total_sales_amount")) for value in printed_amounts
    )
    passed = not problems and not unused and quantity_total_pass and amount_total_pass
    return (
        passed,
        f"电子表{len(spreadsheet.get('records') or [])}行，视觉POS{len(visual_rows)}行，"
        f"未对应电子表行{len(problems)}，未使用视觉行{len(unused)}；"
        f"数量合计{'一致' if quantity_total_pass else '不一致/缺失'}，"
        f"金额合计{'一致' if amount_total_pass else '不一致/缺失'}",
    )


def audit_self_procured_gift_material_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "self_procured_gift_material":
        raise AuditError("自采赠品物料审核收到错误的场景类型")
    documents = list(evidence.get("documents") or [])
    contracts = _documents_of_type(documents, "signed_promotional_contract")
    settlements = _documents_of_type(documents, "settlement")
    receipts = _documents_of_type(documents, "purchase_invoice_or_receipt")
    payments = _documents_of_type(documents, "purchase_payment_record")
    pos_documents = _documents_of_type(documents, "stamped_pos_data")
    photos = _documents_of_type(documents, "activity_photo")
    contract = contracts[0] if len(contracts) == 1 else None
    settlement = settlements[0] if len(settlements) == 1 else None
    receipt = receipts[0] if len(receipts) == 1 else None
    controls: list[dict[str, str]] = []
    issues: list[dict[str, Any]] = []

    missing: list[str] = []
    if len(receipts) != 1:
        missing.append("费用明细和时间清楚的发票或收据")
    if len(contracts) != 1:
        missing.append("签章促销合同")
    if len(settlements) != 1:
        missing.append("公司统一模板客户盖章结算单")
    if not pos_documents:
        missing.append("经销商盖章POS数据")
    if case.get("pos_spreadsheet") is None:
        missing.append("POS数据电子表")
    if not photos:
        missing.append("全部活动门店现场水印照片")
    gift_rule_present = bool(contract) and all(
        contract.get(field) is not None
        for field in ("qualifying_product_or_set", "buy_quantity", "gift_quantity", "gift_material_name")
    )
    if not gift_rule_present:
        missing.append("清楚的活动赠送规则")
    materials_pass = not missing
    _control(
        controls,
        "required_materials",
        materials_pass,
        "六类资料及赠送规则齐全" if materials_pass else "缺少：" + "、".join(missing),
    )
    if missing:
        issues.append(
            _issue(
                "required_materials_missing",
                "自采赠品物料资料不完整",
                _sources(documents) + [Path(case["activity_return_workbook"]).name],
                "缺少：" + "、".join(missing),
                "发票/收据、赠送规则、经销商盖章POS及电子表、全部门店水印照片、统一模板客户盖章结算单和签章合同必须齐全。",
                "采购、活动执行、销售基数和申报金额未形成完整证据链。",
                "补交" + "、".join(missing) + "。",
            )
        )

    contract_pass = bool(contract) and all(
        (
            contract.get("signed_visible") == "visible",
            bool(contract.get("dealer_name")),
            contract.get("activity_start") is not None,
            contract.get("activity_end") is not None,
            contract.get("store_count") is not None,
            contract.get("activity_budget") is not None,
            contract.get("qualifying_product_or_set") is not None,
            contract.get("qualifying_purchase_amount") is not None,
            contract.get("gift_material_name") is not None,
            contract.get("buy_quantity") is not None,
            contract.get("gift_quantity") is not None,
            contract.get("material_quantity") is not None,
            contract.get("material_unit_price") is not None,
            contract.get("document_amount") is not None,
        )
    )
    settlement_pass = bool(settlement) and all(
        (
            settlement.get("company_template_visible") == "visible",
            settlement.get("customer_seal_visible") == "visible",
            bool(settlement.get("dealer_name")),
            settlement.get("activity_start") is not None,
            settlement.get("activity_end") is not None,
            settlement.get("gift_material_name") is not None,
            settlement.get("material_quantity") is not None,
            settlement.get("material_unit_price") is not None,
            settlement.get("document_amount") is not None,
        )
    )
    authority_match = bool(contract and settlement) and all(
        (
            normalized_name_score(contract.get("dealer_name"), settlement.get("dealer_name")) >= 0.82,
            contract.get("activity_start") == settlement.get("activity_start"),
            contract.get("activity_end") == settlement.get("activity_end"),
            _same_material(contract.get("gift_material_name"), settlement.get("gift_material_name")),
            _equal(contract.get("material_quantity"), settlement.get("material_quantity")),
            _equal(contract.get("material_unit_price"), settlement.get("material_unit_price")),
            _equal(contract.get("document_amount"), settlement.get("document_amount")),
        )
    )
    contract_settlement_pass = contract_pass and settlement_pass and authority_match
    _control(
        controls,
        "contract_and_gift_rule",
        contract_settlement_pass,
        (
            "合同与结算已共同明确经销商、活动期、赠送规则、自采物料、数量、单价和金额并签章"
            if contract_settlement_pass
            else "合同/结算缺失、执行标记或赠送规则不完整，或双方活动/物料/金额不一致"
        ),
    )
    if not contract_settlement_pass:
        issues.append(
            _issue(
                "contract_gift_rule_invalid",
                "促销合同与结算未共同建立清楚的赠送规则",
                _sources([item for item in (contract, settlement) if item]),
                controls[-1]["basis"],
                "签章合同和客户盖章统一模板结算须共同明确活动期、门店范围、购买条件、买赠比例、自采物料、数量、单价、预算和申报金额。",
                "无法确定自采物料是否属于本活动以及申报口径。",
                "补交或补正赠送规则和物料金额均完整一致的签章合同、盖章结算单。",
            )
        )

    payment_total = _round(
        sum(
            (_decimal(item.get("payment_amount"), label="付款金额") or Decimal("0") for item in payments),
            Decimal("0"),
        )
    )
    receipt_amount = _decimal(receipt.get("document_amount"), label="票据金额") if receipt else None
    receipt_quantity = _decimal(receipt.get("material_quantity"), label="票据数量") if receipt else None
    receipt_unit = _decimal(receipt.get("material_unit_price"), label="票据单价") if receipt else None
    receipt_calculated = (
        _round(receipt_quantity * receipt_unit)
        if receipt_quantity is not None and receipt_unit is not None
        else None
    )
    purchase_pass = bool(receipt) and all(
        (
            receipt.get("receipt_detail_visible") == "visible",
            receipt.get("document_date") is not None,
            receipt.get("gift_material_name") is not None,
            receipt_amount is not None,
            receipt_calculated is not None,
            receipt_amount is not None and receipt_calculated is not None and abs(receipt_amount - receipt_calculated) <= CENT,
            not payments or (receipt_amount is not None and abs(payment_total - receipt_amount) <= CENT),
            not contract or _same_material(receipt.get("gift_material_name"), contract.get("gift_material_name")),
        )
    )
    purchase_basis = (
        f"票据物料={receipt.get('gift_material_name') if receipt else '缺失'}，"
        f"数量={receipt_quantity}，单价={receipt_unit}，复算={receipt_calculated}，"
        f"票据金额={receipt_amount}；提交付款{len(payments)}笔，合计={payment_total}"
    )
    _control(controls, "purchase_document", purchase_pass, purchase_basis)
    if not purchase_pass:
        issues.append(
            _issue(
                "purchase_document_invalid",
                "自采物料票据明细或付款对应不完整",
                _sources(receipts + payments),
                purchase_basis,
                "发票/收据须清楚显示物料、数量、单价、金额和开具时间，数量×单价须等于金额；已提交付款记录时付款合计应对应票据。",
                "不能确认自采物料实际采购金额。",
                "补交明细清楚的发票/收据；如收款人与票据主体不同，另附关系和付款用途说明。",
            )
        )

    pos_sheet: dict[str, Any] | None = None
    pos_error: str | None = None
    if case.get("pos_spreadsheet") is not None:
        try:
            pos_sheet = read_self_procured_pos_summary(case["pos_spreadsheet"])
        except AuditError as exc:
            pos_error = str(exc)
    pos_sheet_pass = bool(pos_sheet) and not pos_sheet.get("external_formula_cells") and all(
        pos_sheet.get(field) == "exact"
        for field in ("printed_quantity_status", "printed_amount_status")
    )
    representations = _visual_pos_representations(documents)
    representation_results: list[str] = []
    correspondence_pass = False
    if pos_sheet_pass and pos_sheet is not None:
        for representation in representations:
            passed, basis = _pos_representation_matches(representation, pos_sheet)
            representation_results.append(basis)
            correspondence_pass = correspondence_pass or passed
    pos_pass = pos_sheet_pass and bool(pos_documents) and all(
        item.get("customer_seal_visible") == "visible" for item in pos_documents
    ) and correspondence_pass
    pos_basis = (
        ("；".join(representation_results) if representation_results else (pos_error or "缺少POS电子表"))
        + f"；视觉POS文件{len(pos_documents)}份，电子表={'有效' if pos_sheet_pass else '缺失或无效'}"
    )
    _control(controls, "pos_correspondence", pos_pass, pos_basis)
    if not pos_pass:
        issues.append(
            _issue(
                "pos_correspondence_failed",
                "盖章POS与POS电子表未形成逐门店对应",
                _sources(pos_documents) + ([Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else []),
                pos_basis,
                "经销商盖章POS与POS电子表须在活动周期、门店、销售数量、销售金额及合计上逐项唯一一致；活动返图.xls不能替代POS电子表。",
                "无法确定用于赠送规则复核的合格销售基数。",
                "补交可直接读取、无外部依赖且与盖章POS逐店及合计一致的POS电子表。",
            )
        )

    activity = case.get("activity_return") or {}
    activity_records = list(activity.get("records") or [])
    photo_by_source = {str(item.get("source_file") or ""): item for item in photos}
    start = optional_iso_date(contract.get("activity_start")) if contract else None
    end = optional_iso_date(contract.get("activity_end")) if contract else None
    photo_reconciliation: list[dict[str, Any]] = []
    invalid_photos: list[str] = []
    for record in activity_records:
        source = Path(record["photo_file"]).name
        photo = photo_by_source.get(source)
        photo_date = optional_iso_date(photo.get("photo_date")) if photo else None
        period_ok = bool(start and end and photo_date and start <= photo_date <= end)
        store_score = max(
            normalized_name_score(record.get("store_name"), photo.get("photo_store_name") if photo else None),
            normalized_name_score(record.get("store_name"), photo.get("photo_location") if photo else None),
        )
        visual_ok = bool(photo) and all(
            photo.get(field) == "visible"
            for field in (
                "watermark_date_visible",
                "watermark_time_visible",
                "watermark_address_visible",
                "promotion_content_visible",
                "self_procured_material_visible",
                "qualifying_product_visible",
            )
        ) and bool(photo.get("photo_time")) and bool(photo.get("photo_location"))
        material_ok = bool(photo and contract) and _same_material(
            photo.get("photo_material_name"), contract.get("gift_material_name")
        )
        passed = period_ok and store_score >= 0.72 and visual_ok and material_ok
        if not passed:
            invalid_photos.append(source)
        photo_reconciliation.append(
            {
                "excel_row": record.get("excel_row"),
                "source_file": source,
                "routed_store": record.get("store_name"),
                "visible_store": photo.get("photo_store_name") if photo else None,
                "visible_location": photo.get("photo_location") if photo else None,
                "visible_date": photo.get("photo_date") if photo else None,
                "store_score": round(store_score, 4),
                "status": "pass" if passed else "fail",
            }
        )
    photo_paths = [Path(record["photo_file"]) for record in activity_records]
    hash_groups: dict[str, list[str]] = {}
    for path in photo_paths:
        hash_groups.setdefault(sha256_file(path), []).append(path.name)
    duplicate_photo_groups = [names for names in hash_groups.values() if len(names) > 1]
    return_periods = sorted({str(item.get("period_text") or "") for item in activity_records})
    store_count = int(contract.get("store_count") or 0) if contract else 0
    photo_pass = (
        bool(activity_records)
        and len(activity_records) == store_count
        and len(photos) == len(activity_records)
        and not invalid_photos
        and not duplicate_photo_groups
    )
    photo_basis = (
        f"合同门店{store_count}家，活动返图{len(activity_records)}行/照片{len(photos)}张，"
        f"不合格照片{len(invalid_photos)}张，重复图片组{len(duplicate_photo_groups)}组；"
        f"工作簿期间={return_periods}"
    )
    _control(controls, "activity_photo_coverage", photo_pass, photo_basis)
    if not photo_pass:
        issues.append(
            _issue(
                "activity_photo_coverage_failed",
                "活动照片未完整覆盖全部门店或可见内容不合格",
                invalid_photos + [name for group in duplicate_photo_groups for name in group],
                photo_basis,
                "每个活动门店须有一张自身显示活动期日期、地址、拍摄时间、促销活动内容、合格商品和客户自采物料的原始水印照片；工作簿门店名不能替代照片水印。",
                "不能证明全部活动门店实际开展自采物料赠送。",
                "按错误清单逐店补交水印和活动/物料内容清楚、互不重复的原始照片。",
            )
        )

    pos_quantity = _decimal(pos_sheet.get("total_sales_quantity"), label="合格POS数量") if pos_sheet else None
    material_quantity = _decimal(contract.get("material_quantity"), label="合同物料数量") if contract else None
    buy_quantity = _decimal(contract.get("buy_quantity"), label="购买数量规则") if contract else None
    gift_quantity = _decimal(contract.get("gift_quantity"), label="赠送数量规则") if contract else None
    required_gifts: Decimal | None = None
    if pos_quantity is not None and buy_quantity is not None and gift_quantity is not None and buy_quantity > 0:
        required_gifts = _round(pos_quantity / buy_quantity * gift_quantity)
    gift_sufficiency_pass = bool(contract) and all(
        (
            buy_quantity is not None,
            gift_quantity is not None,
            bool(contract.get("limited_quantity_rule"))
            or (
                required_gifts is not None
                and material_quantity is not None
                and material_quantity + CENT >= required_gifts
            ),
        )
    )
    gift_basis = (
        f"赠送规则买{buy_quantity}赠{gift_quantity}，POS合格套包数量={pos_quantity}，"
        f"规则所需赠品={required_gifts}，自采物料={material_quantity}，"
        f"限量条款={contract.get('limited_quantity_rule') if contract else None}"
    )
    _control(controls, "gift_quantity_sufficiency", gift_sufficiency_pass, gift_basis)
    if not gift_sufficiency_pass:
        issues.append(
            _issue(
                "gift_quantity_sufficiency_failed",
                "赠送规则与POS销售及自采物料数量未闭环",
                _sources([item for item in (contract, settlement) if item])
                + ([Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else []),
                gift_basis,
                "按合同买赠比例复核POS合格套包数量；自采物料少于应赠数量时，合同/结算须明确限量、先到先得或具体分配上限。",
                "不能确认活动按备注的赠送规则完整执行。",
                "补交POS电子表并补充经签章确认的限量/分配规则，或补正赠品物料数量。",
            )
        )

    contract_amount = _decimal(contract.get("document_amount"), label="合同金额") if contract else None
    contract_budget = _decimal(contract.get("activity_budget"), label="合同预算") if contract else None
    contract_qty = _decimal(contract.get("material_quantity"), label="合同数量") if contract else None
    contract_unit = _decimal(contract.get("material_unit_price"), label="合同单价") if contract else None
    settlement_amount = _decimal(settlement.get("document_amount"), label="结算金额") if settlement else None
    settlement_qty = _decimal(settlement.get("material_quantity"), label="结算数量") if settlement else None
    settlement_unit = _decimal(settlement.get("material_unit_price"), label="结算单价") if settlement else None
    contract_calculated = _round(contract_qty * contract_unit) if contract_qty is not None and contract_unit is not None else None
    settlement_calculated = _round(settlement_qty * settlement_unit) if settlement_qty is not None and settlement_unit is not None else None
    amount_pass = all(
        (
            contract_calculated is not None,
            settlement_calculated is not None,
            contract_amount is not None,
            contract_budget is not None,
            settlement_amount is not None,
            receipt_amount is not None,
            contract_calculated is not None and contract_amount is not None and abs(contract_calculated - contract_amount) <= CENT,
            contract_amount is not None and contract_budget is not None and abs(contract_amount - contract_budget) <= CENT,
            settlement_calculated is not None and settlement_amount is not None and abs(settlement_calculated - settlement_amount) <= CENT,
            contract_amount is not None and settlement_amount is not None and abs(contract_amount - settlement_amount) <= CENT,
            receipt_amount is not None and settlement_amount is not None and abs(receipt_amount - settlement_amount) <= CENT,
            not payments or (settlement_amount is not None and abs(payment_total - settlement_amount) <= CENT),
        )
    )
    amount_basis = (
        f"合同数量×单价={contract_qty}×{contract_unit}={contract_calculated}，合同金额={contract_amount}，"
        f"预算={contract_budget}；结算数量×单价={settlement_qty}×{settlement_unit}={settlement_calculated}，"
        f"申报={settlement_amount}；票据={receipt_amount}；付款合计={payment_total}"
    )
    _control(controls, "amount_recalculation", amount_pass, amount_basis)
    if not amount_pass:
        issues.append(
            _issue(
                "amount_recalculation_failed",
                "自采赠品物料金额无法全链路复算",
                _sources(contracts + settlements + receipts + payments),
                amount_basis,
                "合同、结算和票据的物料数量×单价须等于金额，且申报不得超过合同预算、有效票据和实际付款支持。",
                "申报金额缺少一致、可复算的业务来源。",
                "补正合同、票据、付款或结算中的物料、数量、单价和金额，使其逐项一致。",
            )
        )

    claimed = settlement_amount or Decimal("0")
    blocked = any(item["status"] != "pass" for item in controls)
    supported = Decimal("0") if blocked else min(
        claimed,
        contract_budget or Decimal("0"),
        receipt_amount or Decimal("0"),
    )
    held = max(claimed - supported, Decimal("0"))
    exceptions = [
        {
            "severity": item["severity"],
            "code": item["code"],
            "message": item["title"],
            "impact": item["impact"],
            "suggestion": item["resubmission"],
            "source": "、".join(item.get("source_files") or []),
        }
        for item in issues
    ]
    return {
        "schema_version": "2.1",
        "generated_at": now_utc(),
        "scenario": "self_procured_gift_material",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": "客户自采赠品物料核销",
        "summary": {
            "conclusion": "human_review" if blocked else "pass",
            "decision_label": "资料需补正" if blocked else "可核销",
            "claimed_amount": json_number(claimed),
            "suggested_approved_amount": json_number(supported),
            "temporarily_held_amount": json_number(held),
            "high_exception_count": len(exceptions),
            "medium_exception_count": 0,
            "error_group_count": len(issues),
            "document_count": len(documents),
            "activity_store_count": len(activity_records),
            "activity_photo_count": len(photos),
            "pos_spreadsheet_status": "valid" if pos_sheet_pass else "missing_or_invalid",
            "recalculated_amount": json_number(contract_calculated) if contract_calculated is not None else None,
        },
        "exceptions": exceptions,
        "self_procured_gift_material_audit": {
            "controls": controls,
            "issues": issues,
            "documents": documents,
            "pos_spreadsheet": pos_sheet,
            "activity_return": {
                "source_file": activity.get("source_file"),
                "source_sha256": activity.get("source_sha256"),
                "sheet": activity.get("sheet"),
                "record_count": len(activity_records),
                "period_values": return_periods,
            },
            "photo_reconciliation": photo_reconciliation,
            "duplicate_photo_groups": duplicate_photo_groups,
            "calculation_basis": amount_basis,
        },
    }
