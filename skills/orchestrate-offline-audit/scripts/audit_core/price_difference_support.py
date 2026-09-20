from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .common import optional_iso_date, normalized_name_score
from .common import AuditError, json_number, money, normalize_text, now_utc
from .excel_sources import read_maintenance_pos


CENT = Decimal("0.01")


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
        "source_files": list(dict.fromkeys(source_files)),
        "observed": observed,
        "expected": expected,
        "impact": impact,
        "resubmission": resubmission,
        "severity": severity,
        "confidence": confidence,
    }


def _by_role(documents: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [item for item in documents if str(item.get("role")) == role]


def _sources(documents: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("source_file") or "") for item in documents]


def _decimal(value: Any, *, label: str) -> Decimal | None:
    if value is None:
        return None
    return money(value, label=label)


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _equal_money(left: Any, right: Any) -> bool:
    left_value = _decimal(left, label="左侧金额")
    right_value = _decimal(right, label="右侧金额")
    return (
        left_value is not None
        and right_value is not None
        and abs(left_value - right_value) <= CENT
    )


def _document_text(document: dict[str, Any]) -> str:
    return normalize_text(
        " ".join(
            str(document.get(field) or "")
            for field in ("title", "fee_type", "calculation_method", "visible_summary")
        )
    )


def _add_control(
    controls: list[dict[str, str]],
    control_id: str,
    passed: bool,
    basis: str,
) -> None:
    controls.append(
        {"control_id": control_id, "status": "pass" if passed else "fail", "basis": basis}
    )


def _visual_pos_totals(pos_documents: list[dict[str, Any]]) -> tuple[Decimal | None, Decimal | None]:
    printed_quantities = [
        _decimal(item.get("pos_total_quantity"), label="盖章POS总数量")
        for item in pos_documents
        if item.get("pos_total_quantity") is not None
    ]
    printed_amounts = [
        _decimal(item.get("pos_total_sales_amount"), label="盖章POS总金额")
        for item in pos_documents
        if item.get("pos_total_sales_amount") is not None
    ]
    lines = [line for item in pos_documents for line in (item.get("pos_lines") or [])]
    line_quantities = [
        _decimal(line.get("quantity"), label="盖章POS行数量")
        for line in lines
        if line.get("quantity") is not None
    ]
    line_amounts = [
        _decimal(line.get("sales_amount"), label="盖章POS行金额")
        for line in lines
        if line.get("sales_amount") is not None
    ]
    quantity = printed_quantities[0] if len(printed_quantities) == 1 else (
        sum((value for value in line_quantities if value is not None), Decimal("0"))
        if line_quantities else None
    )
    amount = printed_amounts[0] if len(printed_amounts) == 1 else (
        sum((value for value in line_amounts if value is not None), Decimal("0"))
        if line_amounts else None
    )
    return quantity, amount


def _party_aligned(documents: list[dict[str, Any]]) -> tuple[bool, str]:
    dealer_names = [
        str(item.get("dealer_name"))
        for item in documents
        if item.get("dealer_name")
    ]
    if len(dealer_names) < 2:
        return False, "至少两份权威资料未同时识别到经销商全称"
    anchor = dealer_names[0]
    problems = [name for name in dealer_names[1:] if normalized_name_score(anchor, name) < 0.82]
    if problems:
        return False, "经销商名称不一致：" + "、".join(dealer_names)
    return True, "合同、结算和盖章POS识别到同一经销商：" + anchor


def _photo_coverage(
    photos: list[dict[str, Any]],
    contract: dict[str, Any] | None,
    settlement: dict[str, Any] | None,
    pos_documents: list[dict[str, Any]],
) -> tuple[bool, str, list[str]]:
    expected_stores = list(
        dict.fromkeys(
            str(line.get("store_name") or "").strip()
            for document in pos_documents
            for line in (document.get("pos_lines") or [])
            if str(line.get("store_name") or "").strip()
        )
    )
    if not expected_stores and contract:
        expected_stores = list(contract.get("store_names") or [])
    expected_count_values = [
        int(value)
        for value in (
            contract.get("store_count") if contract else None,
            settlement.get("store_count") if settlement else None,
            len(expected_stores) or None,
        )
        if value is not None
    ]
    expected_count = max(expected_count_values, default=0)
    start = optional_iso_date(contract.get("activity_start")) if contract else None
    end = optional_iso_date(contract.get("activity_end")) if contract else None
    activity_price = contract.get("activity_price") if contract else None
    valid_photos: list[dict[str, Any]] = []
    invalid_sources: list[str] = []
    for photo in photos:
        photo_date = optional_iso_date(photo.get("photo_date"))
        valid = all(
            photo.get(field) == "visible"
            for field in (
                "watermark_date_visible",
                "watermark_time_visible",
                "watermark_address_visible",
                "activity_price_visible",
            )
        )
        valid = valid and bool(photo.get("photo_time")) and bool(photo.get("photo_location"))
        if start is not None and end is not None:
            valid = valid and photo_date is not None and start <= photo_date <= end
        else:
            valid = False
        if activity_price is not None:
            valid = valid and _equal_money(photo.get("photo_activity_price"), activity_price)
        else:
            valid = False
        if valid:
            valid_photos.append(photo)
        else:
            invalid_sources.append(str(photo.get("source_file") or ""))

    matched_stores: set[str] = set()
    unmatched_locations: list[str] = []
    for photo in valid_photos:
        location = str(photo.get("photo_location") or "")
        if expected_stores:
            scored = sorted(
                ((normalized_name_score(location, store), store) for store in expected_stores),
                reverse=True,
            )
            if scored and scored[0][0] >= 0.65 and (
                len(scored) == 1 or scored[0][0] - scored[1][0] >= 0.03
            ):
                matched_stores.add(scored[0][1])
            else:
                unmatched_locations.append(location)
        else:
            matched_stores.add(normalize_text(location))

    covered_count = len(matched_stores)
    passed = (
        expected_count > 0
        and covered_count >= expected_count
        and not invalid_sources
        and not unmatched_locations
    )
    basis = (
        f"应覆盖{expected_count}家，提交{len(photos)}张，"
        f"有效且唯一匹配{covered_count}家；无效{len(invalid_sources)}张，"
        f"地点无法唯一匹配{len(unmatched_locations)}张"
    )
    return passed, basis, [*invalid_sources, *unmatched_locations]


def audit_price_difference_support_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "price_difference_support":
        raise AuditError("价格补差审核收到错误的场景类型")

    documents = list(evidence.get("documents") or [])
    contracts = _by_role(documents, "signed_promotional_contract")
    settlements = _by_role(documents, "settlement")
    pos_documents = _by_role(documents, "stamped_pos_data")
    photos = _by_role(documents, "activity_photo")
    contract = contracts[0] if len(contracts) == 1 else None
    settlement = settlements[0] if len(settlements) == 1 else None
    issues: list[dict[str, Any]] = []
    controls: list[dict[str, str]] = []

    missing: list[str] = []
    if len(contracts) != 1:
        missing.append("签章后的促销合同")
    if len(settlements) != 1:
        missing.append("公司统一模板盖章结算单")
    if not pos_documents:
        missing.append("经销商盖章POS数据")
    if case.get("pos_spreadsheet") is None:
        missing.append("POS数据电子表")
    if not photos:
        missing.append("全部活动门店现场活动照片")
    material_pass = not missing
    _add_control(controls, "required_materials", material_pass, "五类必备资料齐全" if material_pass else "缺少：" + "、".join(missing))
    if missing:
        issues.append(
            _issue(
                "required_materials_missing",
                "价格补差资料不完整",
                _sources(documents),
                "缺少：" + "、".join(missing),
                "必须同时提供盖章POS、POS电子表、统一模板盖章结算单、全门店有效活动照片和签章促销合同。",
                "证据链不完整，申报金额不能核销。",
                "补交" + "、".join(missing) + "。",
            )
        )

    authority_documents = [item for item in (contract, settlement) if item]
    nature_pass = len(authority_documents) == 2 and all(
        any(marker in _document_text(item) for marker in ("补差", "价格差额", "差额补偿"))
        for item in authority_documents
    )
    _add_control(controls, "fee_nature", nature_pass, "合同和结算正文均明确价格补差" if nature_pass else "合同或结算正文未同时明确价格补差性质")
    if not nature_pass:
        issues.append(
            _issue(
                "fee_nature_unproven",
                "费用性质未形成价格补差闭环",
                _sources(authority_documents),
                "合同或结算正文未同时识别到补差、价格差额或差额补偿表述。",
                "归档名称不能替代合同和结算正文中的价格补差性质。",
                "无法确认按价格补差规则核销。",
                "补交正文费用性质明确且相互一致的签章合同与盖章结算单。",
            )
        )

    contract_pass = bool(contract) and all(
        (
            contract.get("signed_visible") == "visible",
            contract.get("activity_start") is not None,
            contract.get("activity_end") is not None,
            contract.get("original_price") is not None,
            contract.get("activity_price") is not None,
            contract.get("support_unit_amount") is not None,
            contract.get("planned_quantity") is not None,
            contract.get("amount_ceiling") is not None,
        )
    )
    _add_control(controls, "promotional_contract", contract_pass, "合同关键条款和签章完整" if contract_pass else "合同签章、周期、价格、补差单价、数量或预算不完整")
    if not contract_pass:
        issues.append(
            _issue(
                "promotional_contract_incomplete",
                "促销合同关键条款或签章不完整",
                _sources(contracts),
                "未完整识别签章、活动周期、原价、活动价、合同补差单价、数量上限和预算上限。",
                "上述字段必须由同一份签章促销合同明确建立。",
                "无法确定活动及金额权威基线。",
                "补交关键条款清楚且已签章的完整促销合同。",
            )
        )

    settlement_pass = bool(settlement) and all(
        (
            settlement.get("company_template_visible") == "visible",
            settlement.get("dealer_seal_visible") == "visible",
            settlement.get("settlement_quantity") is not None,
            settlement.get("support_unit_amount") is not None,
            settlement.get("claimed_amount") is not None,
            bool(settlement.get("calculation_method")),
        )
    )
    _add_control(controls, "settlement", settlement_pass, "统一模板、盖章、数量、公式和金额完整" if settlement_pass else "结算模板、盖章、数量、补差单价、公式或金额不完整")
    if not settlement_pass:
        issues.append(
            _issue(
                "settlement_incomplete",
                "结算单不满足统一模板盖章要求",
                _sources(settlements),
                "未完整识别公司模板、经销商印章、结算数量、补差单价、计算方式或申报金额。",
                "结算单须使用公司统一模板并由经销商盖章，详细记录费用明细、计算方式和金额。",
                "申报金额缺少有效确认。",
                "按公司统一模板补交明细完整且经销商盖章的结算单。",
            )
        )

    pos_seal_pass = bool(pos_documents) and all(
        item.get("dealer_seal_visible") == "visible" for item in pos_documents
    )
    _add_control(controls, "pos_visual_seal", pos_seal_pass, "全部POS可视页均见经销商印章" if pos_seal_pass else "至少一页POS未见经销商印章或POS缺失")
    if not pos_seal_pass:
        issues.append(
            _issue(
                "stamped_pos_invalid",
                "POS数据未完成经销商盖章确认",
                _sources(pos_documents),
                "缺少POS资料或至少一页经销商印章未清楚显示。",
                "全部提交的POS页须可读并由经销商盖章确认。",
                "不能确认POS数据由申报经销商认可。",
                "补交印章和明细清楚的完整POS数据。",
            )
        )

    pos_sheet: dict[str, Any] | None = None
    pos_sheet_error: str | None = None
    if case.get("pos_spreadsheet") is not None:
        try:
            pos_sheet = read_maintenance_pos(case["pos_spreadsheet"])
        except AuditError as exc:
            pos_sheet_error = str(exc)
    pos_sheet_pass = pos_sheet is not None and not pos_sheet.get("external_formula_cells")
    _add_control(controls, "pos_spreadsheet", pos_sheet_pass, "电子表可读且无外部公式依赖" if pos_sheet_pass else (pos_sheet_error or "未提交POS电子表"))
    if not pos_sheet_pass:
        issues.append(
            _issue(
                "pos_spreadsheet_invalid",
                "POS数据电子表缺失或不可核验",
                [Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else [],
                pos_sheet_error or "本包未提交POS数据电子表。",
                "须提供可直接读取、含门店/商品/数量/活动单价/金额明细且无外部依赖的POS电子表。",
                "无法确定性核对POS明细并计算合格数量。",
                "补交与盖章POS逐项一致的原始POS电子表。",
            )
        )

    visual_quantity, visual_amount = _visual_pos_totals(pos_documents)
    pos_correspondence_pass = bool(pos_sheet) and visual_quantity is not None and visual_amount is not None and _equal_money(visual_quantity, pos_sheet.get("total_quantity")) and _equal_money(visual_amount, pos_sheet.get("total_sales_amount"))
    _add_control(controls, "pos_correspondence", pos_correspondence_pass, "盖章POS与电子表合计数量、金额一致" if pos_correspondence_pass else f"盖章POS数量/金额={visual_quantity}/{visual_amount}；电子表={pos_sheet.get('total_quantity') if pos_sheet else '缺失'}/{pos_sheet.get('total_sales_amount') if pos_sheet else '缺失'}")
    if not pos_correspondence_pass:
        issues.append(
            _issue(
                "pos_correspondence_failed",
                "盖章POS与电子表未形成可核对对应",
                [*_sources(pos_documents), *([Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else [])],
                controls[-1]["basis"],
                "盖章POS与电子表的门店、商品、数量、活动单价、金额及合计须唯一对应。",
                "不能把电子表数量作为补差计算基数。",
                "更正并重交逐项及合计一致的盖章POS和POS电子表。",
            )
        )

    party_pass, party_basis = _party_aligned([*authority_documents, *pos_documents])
    _add_control(controls, "party_alignment", party_pass, party_basis)
    if not party_pass:
        issues.append(
            _issue(
                "party_alignment_failed",
                "经销商主体无法唯一对应",
                _sources([*authority_documents, *pos_documents]),
                party_basis,
                "合同、结算和盖章POS须指向同一经销商主体。",
                "不能确认申报主体和销售主体一致。",
                "补交主体全称清楚且一致的合同、结算和盖章POS。",
            )
        )

    period_pass = bool(contract and settlement) and contract.get("activity_start") == settlement.get("activity_start") and contract.get("activity_end") == settlement.get("activity_end")
    _add_control(controls, "period_alignment", period_pass, "合同与结算活动周期一致" if period_pass else "合同与结算周期缺失或不一致")
    if not period_pass:
        issues.append(
            _issue(
                "period_alignment_failed",
                "活动周期未保持一致",
                _sources(authority_documents),
                controls[-1]["basis"],
                "合同、结算、POS及照片均须位于同一合同活动周期。",
                "不能确认销售及现场执行属于本次活动。",
                "补交周期清楚且相互一致的资料。",
            )
        )

    price_pass = bool(contract) and contract.get("original_price") is not None and contract.get("activity_price") is not None and Decimal(str(contract["original_price"])) > Decimal(str(contract["activity_price"])) and contract.get("support_unit_amount") is not None
    _add_control(controls, "price_terms", price_pass, "合同原价高于活动价，且另行明确补差单价" if price_pass else "合同未同时明确有效原价、活动价和补差单价")
    if not price_pass:
        issues.append(
            _issue(
                "price_terms_invalid",
                "价格和补差单价依据不完整",
                _sources(contracts),
                controls[-1]["basis"],
                "合同应分别明确原价、活动价和公司补差单价；零售差额不能替代合同补差单价。",
                "无法确定每件支持金额。",
                "补交三项价格条款清楚的签章合同。",
            )
        )

    photo_pass, photo_basis, _ = _photo_coverage(photos, contract, settlement, pos_documents)
    _add_control(controls, "all_store_photo_coverage", photo_pass, photo_basis)
    if not photo_pass:
        issues.append(
            _issue(
                "activity_photo_coverage_failed",
                "现场照片未覆盖全部活动门店",
                _sources(photos),
                photo_basis,
                "每家活动门店均须至少一张活动期内、日期/地址/拍摄时间水印清楚且明确展示合同活动价的现场照片。",
                "现有照片只能支持其自身可见门店，不能外推到未拍摄门店。",
                "按未覆盖门店逐店补交符合水印和活动价要求的原始现场照片。",
            )
        )

    calculated_amount: Decimal | None = None
    calculation_basis = "金额复算条件不完整"
    amount_pass = False
    if contract and settlement and pos_sheet:
        unit_support = _decimal(contract.get("support_unit_amount"), label="合同补差单价")
        pos_quantity = _decimal(pos_sheet.get("total_quantity"), label="POS电子表总数量")
        planned_quantity = _decimal(contract.get("planned_quantity"), label="合同数量上限")
        settlement_quantity = _decimal(settlement.get("settlement_quantity"), label="结算数量")
        ceiling = _decimal(contract.get("amount_ceiling"), label="合同预算上限")
        claim_value = _decimal(settlement.get("claimed_amount"), label="结算申报金额")
        if all(value is not None for value in (unit_support, pos_quantity, planned_quantity, settlement_quantity, ceiling, claim_value)):
            assert unit_support is not None and pos_quantity is not None and planned_quantity is not None
            assert settlement_quantity is not None and ceiling is not None and claim_value is not None
            eligible_quantity = min(pos_quantity, planned_quantity)
            calculated_amount = min(_round(eligible_quantity * unit_support), ceiling)
            calculation_basis = (
                f"POS数量{pos_quantity}与合同数量上限{planned_quantity}取低值{eligible_quantity}，"
                f"乘合同补差单价{unit_support}，受预算{ceiling}约束，结果{calculated_amount}；"
                f"结算数量{settlement_quantity}、申报{claim_value}"
            )
            amount_pass = settlement_quantity == eligible_quantity and abs(calculated_amount - claim_value) <= CENT
    _add_control(controls, "amount_recalculation", amount_pass, calculation_basis)
    if not amount_pass:
        issues.append(
            _issue(
                "amount_recalculation_failed",
                "申报金额无法按合同补差单价闭环复算",
                _sources([*contracts, *settlements]) + ([Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else []),
                calculation_basis,
                "以合同范围内POS合格数量乘合同补差单价，并受合同数量和预算上限约束；结算数量及申报金额须精确一致。",
                "金额依据未闭环，不能核销申报金额。",
                "补正POS电子表、合同数量/补差单价/预算或结算数量和金额，使全链路一致。",
            )
        )

    claim = _decimal(settlement.get("claimed_amount"), label="结算申报金额") if settlement else None
    claimed_amount = claim or Decimal("0")
    blocking_failed = any(item["status"] != "pass" for item in controls)
    supported = Decimal("0") if blocking_failed else min(calculated_amount or Decimal("0"), claimed_amount)
    held = max(claimed_amount - supported, Decimal("0"))
    conclusion = "human_review" if blocking_failed else "pass"
    exceptions = [
        {
            "severity": str(item.get("severity") or "high"),
            "code": str(item["code"]),
            "message": str(item["title"]),
            "impact": str(item["impact"]),
            "suggestion": str(item["resubmission"]),
            "source": "、".join(item.get("source_files") or []),
        }
        for item in issues
    ]
    return {
        "schema_version": "2.1",
        "generated_at": now_utc(),
        "scenario": "price_difference_support",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": "价格补差核销",
        "summary": {
            "conclusion": conclusion,
            "decision_label": "资料需补正" if blocking_failed else "可核销",
            "claimed_amount": json_number(claimed_amount),
            "suggested_approved_amount": json_number(supported),
            "temporarily_held_amount": json_number(held),
            "high_exception_count": sum(item["severity"] == "high" for item in exceptions),
            "medium_exception_count": sum(item["severity"] == "medium" for item in exceptions),
            "calculated_amount": json_number(calculated_amount) if calculated_amount is not None else None,
        },
        "exceptions": exceptions,
        "price_difference_support_audit": {
            "controls": controls,
            "issues": issues,
            "pos_spreadsheet": pos_sheet,
            "calculation_basis": calculation_basis,
        },
    }
