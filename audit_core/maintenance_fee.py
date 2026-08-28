from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .common import AuditError, json_number, money, normalize_text, now_utc
from .excel_sources import read_maintenance_pos


CENT = Decimal("0.01")
MAINTENANCE_MARKERS = ("维护费用", "维护费", "渠道维护", "经销商维护")
CONFLICT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("人员激励", ("人员激励", "人员奖励", "销售提成", "促销员工资")),
    ("直营", ("直营费用", "直营维护")),
    ("陈列堆头", ("堆头", "陈列费", "场地使用费")),
    ("海报/物料制作", ("物料制作", "海报制作", "展示道具")),
    ("其他费用", ("其他费用", "新增费用类型")),
)


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


def _documents_by_role(
    documents: list[dict[str, Any]],
    role: str,
) -> list[dict[str, Any]]:
    return [item for item in documents if str(item.get("role")) == role]


def _source_names(documents: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("source_file") or "") for item in documents]


def _document_text(document: dict[str, Any]) -> str:
    values: list[Any] = [
        document.get("title"),
        document.get("fee_type"),
        document.get("calculation_method"),
        document.get("visible_summary"),
    ]
    for line in document.get("expense_lines") or []:
        values.extend((line.get("description"), line.get("calculation_basis")))
    return normalize_text(" ".join(str(value or "") for value in values))


def _fee_nature(
    documents: list[dict[str, Any]],
) -> tuple[bool, str, list[str]]:
    visible_maintenance: list[str] = []
    conflicts: list[str] = []
    sources: list[str] = []
    for document in documents:
        text = _document_text(document)
        if not text:
            continue
        source = str(document.get("source_file") or "")
        if any(normalize_text(marker) in text for marker in MAINTENANCE_MARKERS):
            visible_maintenance.append(source)
            sources.append(source)
        for label, markers in CONFLICT_MARKERS:
            if any(normalize_text(marker) in text for marker in markers):
                conflicts.append(f"{source}明确显示{label}")
                sources.append(source)
    if conflicts:
        return False, "；".join(conflicts), list(dict.fromkeys(sources))
    if visible_maintenance:
        return True, "维护费用性质可见于" + "、".join(visible_maintenance), visible_maintenance
    return False, "合同、结算或专项资料中未识别到维护费用性质", _source_names(documents)


def _decimal(value: Any, *, label: str) -> Decimal | None:
    if value is None:
        return None
    return money(value, label=label)


def _round_money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _same_money(left: Any, right: Any) -> bool:
    left_value = _decimal(left, label="左侧金额")
    right_value = _decimal(right, label="右侧金额")
    return (
        left_value is not None
        and right_value is not None
        and abs(left_value - right_value) <= CENT
    )


def _same_number(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return False
    try:
        return abs(Decimal(str(left)) - Decimal(str(right))) <= Decimal("0.000001")
    except Exception:
        return False


def _date_value(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip().replace("/", "-").replace(".", "-")
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _name_similarity(left: Any, right: Any) -> float:
    left_text = normalize_text(left)
    right_text = normalize_text(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    return SequenceMatcher(None, left_text, right_text).ratio()


def _names_compatible(values: list[str]) -> bool:
    cleaned = [value for value in values if normalize_text(value)]
    if len(cleaned) < 2:
        return False
    baseline = cleaned[0]
    return all(_name_similarity(baseline, value) >= 0.82 for value in cleaned[1:])


def _match_pos_rows(
    visual_rows: list[dict[str, Any]],
    spreadsheet_rows: list[dict[str, Any]],
) -> tuple[bool, str]:
    if not visual_rows or not spreadsheet_rows:
        return False, "POS图片或电子表缺少可逐项对应的商品明细"
    if len(visual_rows) != len(spreadsheet_rows):
        return False, f"POS图片明细{len(visual_rows)}行，电子表明细{len(spreadsheet_rows)}行"

    used: set[int] = set()
    problems: list[str] = []
    for visual in visual_rows:
        scores = sorted(
            (
                (_name_similarity(visual.get("product_name"), row.get("product_name")), index)
                for index, row in enumerate(spreadsheet_rows)
                if index not in used
            ),
            reverse=True,
        )
        if not scores or scores[0][0] < 0.82:
            problems.append(f"商品“{visual.get('product_name') or '未识别'}”无唯一电子表对应行")
            continue
        if len(scores) > 1 and scores[0][0] - scores[1][0] < 0.05:
            problems.append(f"商品“{visual.get('product_name') or '未识别'}”存在多个相近候选")
            continue
        index = scores[0][1]
        used.add(index)
        row = spreadsheet_rows[index]
        if not _same_number(visual.get("quantity"), row.get("quantity")):
            problems.append(f"商品“{visual.get('product_name')}”数量不一致")
        if not _same_money(visual.get("sales_amount"), row.get("sales_amount")):
            problems.append(f"商品“{visual.get('product_name')}”销售金额不一致")
    if len(used) != len(spreadsheet_rows):
        problems.append("电子表存在POS图片未覆盖的商品行")
    return not problems, "；".join(problems) if problems else "逐项商品、数量和销售金额均唯一对应"


def _visible_pos_summary(
    pos_documents: list[dict[str, Any]],
) -> tuple[Decimal | None, Decimal | None, list[dict[str, Any]], str]:
    if not pos_documents:
        return None, None, [], "未提交盖章POS可视数据"
    rows = [line for document in pos_documents for line in document.get("pos_lines") or []]
    quantity_values = [
        _decimal(document.get("pos_total_quantity"), label="POS总数量")
        for document in pos_documents
        if document.get("pos_total_quantity") is not None
    ]
    amount_values = [
        _decimal(document.get("pos_total_sales_amount"), label="POS总销售额")
        for document in pos_documents
        if document.get("pos_total_sales_amount") is not None
    ]
    quantity = sum(quantity_values, Decimal("0")) if len(quantity_values) == len(pos_documents) else None
    amount = sum(amount_values, Decimal("0")) if len(amount_values) == len(pos_documents) else None
    return quantity, amount, rows, f"POS可视文件{len(pos_documents)}份，明细{len(rows)}行"


def _eligible_sales_base(
    contract: dict[str, Any],
    pos_sheet: dict[str, Any],
) -> tuple[Decimal | None, str]:
    scope = normalize_text(contract.get("eligible_pos_scope"))
    records = list(pos_sheet.get("records") or [])
    if not scope:
        return None, "合同未识别到明确的合格POS范围"
    if any(marker in scope for marker in ("全部", "所有", "全品", "pos总", "总销售")):
        return _decimal(pos_sheet.get("total_sales_amount"), label="POS销售总额"), "合同范围覆盖全部POS明细"
    selected = [
        row
        for row in records
        if normalize_text(row.get("product_name")) in scope
        or scope in normalize_text(row.get("product_name"))
    ]
    if not selected:
        return None, "合同合格POS范围无法唯一映射到电子表商品"
    return sum(
        (_decimal(row.get("sales_amount"), label="POS行销售额") or Decimal("0"))
        for row in selected
    ), f"合同范围映射电子表{len(selected)}行"


def _recalculate_amount(
    contract: dict[str, Any],
    pos_sheet: dict[str, Any],
) -> tuple[Decimal | None, str]:
    rate = _decimal(contract.get("rate"), label="合同费率")
    if rate is not None:
        if rate > Decimal("1"):
            return None, "合同费率大于1，无法确认是否按百分比表达"
        base, basis = _eligible_sales_base(contract, pos_sheet)
        if base is None:
            return None, basis
        amount = base * rate
        method = f"{basis}：{base}×{rate}"
    else:
        lines = list(contract.get("expense_lines") or [])
        calculable = [
            line
            for line in lines
            if line.get("quantity") is not None and line.get("unit_price") is not None
        ]
        if calculable and len(calculable) == len(lines):
            amount = sum(
                Decimal(str(line["quantity"])) * Decimal(str(line["unit_price"]))
                for line in calculable
            )
            method = f"合同{len(calculable)}项数量×单价求和"
        elif "固定" in normalize_text(contract.get("calculation_method")):
            fixed = [
                _decimal(line.get("amount"), label="合同固定费用")
                for line in lines
                if line.get("amount") is not None
            ]
            if not fixed:
                return None, "合同写明固定费用但未识别到明确金额"
            amount = sum((value for value in fixed if value is not None), Decimal("0"))
            method = "合同明确固定费用求和"
        else:
            return None, "合同计算方法不能由费率或数量×单价确定性复算"
    ceiling = _decimal(contract.get("amount_ceiling"), label="合同金额上限")
    if ceiling is not None and amount > ceiling:
        amount = ceiling
        method += f"，并受合同上限{ceiling}约束"
    return _round_money(amount), method


def audit_maintenance_fee_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "maintenance_fee":
        raise AuditError("维护费用审核收到错误的场景类型")

    documents = list(evidence.get("documents") or [])
    pos_documents = _documents_by_role(documents, "stamped_pos_data")
    contracts = _documents_by_role(documents, "signed_promotional_contract")
    settlements = _documents_by_role(documents, "settlement")
    supports = _documents_by_role(documents, "supporting_document")
    activity_photos = _documents_by_role(documents, "activity_photo")
    issues: list[dict[str, Any]] = []
    controls: list[dict[str, str]] = []

    pos_spreadsheet_path = case.get("pos_spreadsheet")
    missing_roles: list[str] = []
    if not pos_documents:
        missing_roles.append("经销商盖章POS数据")
    if pos_spreadsheet_path is None:
        missing_roles.append("POS数据电子表")
    if len(contracts) != 1:
        missing_roles.append("签章后的促销合同")
    if len(settlements) != 1:
        missing_roles.append("公司统一模板盖章结算单")
    if not supports and not activity_photos:
        missing_roles.append("具体费用对应的协议、相关文件或活动照片")
    if missing_roles:
        issues.append(
            _issue(
                "required_materials_missing",
                "核销资料不完整",
                _source_names(documents),
                "缺少：" + "、".join(missing_roles),
                "维护费用必须同时提供盖章POS、POS电子表、专项资料、统一模板盖章结算单和签章促销合同。",
                "证据链不完整，无法建立维护费用的业务、执行和金额依据。",
                "补交" + "、".join(missing_roles) + "。",
            )
        )
        controls.append({"control_id": "required_materials", "status": "fail", "basis": "、".join(missing_roles)})
    else:
        controls.append({"control_id": "required_materials", "status": "pass", "basis": "五类必需资料均已提交"})

    authoritative = [*contracts, *settlements, *supports]
    fee_pass, fee_basis, fee_sources = _fee_nature(authoritative)
    controls.append({"control_id": "fee_nature", "status": "pass" if fee_pass else "fail", "basis": fee_basis})
    if not fee_pass:
        issues.append(
            _issue(
                "fee_nature_conflict",
                "费用性质与维护费用不一致",
                fee_sources,
                fee_basis,
                "合同、结算或专项资料正文应明确属于维护费用；归档名称不能替代正文。",
                "无法确认本申报属于维护费用，且不得静默改投其他核销方式。",
                "提交正文费用性质为维护费用的签章合同和结算资料；如实际属于其他类型，请按实际类型另行申报。",
            )
        )

    pos_seal_pass = bool(pos_documents) and all(
        item.get("dealer_seal_visible") == "visible" for item in pos_documents
    )
    controls.append({"control_id": "pos_visual_seal", "status": "pass" if pos_seal_pass else "fail", "basis": "全部POS可视资料盖章清楚" if pos_seal_pass else "缺少POS资料或经销商印章未清楚显示"})
    if not pos_seal_pass:
        issues.append(
            _issue(
                "stamped_pos_invalid",
                "POS数据未完成经销商盖章确认",
                _source_names(pos_documents),
                "缺少POS资料或至少一份POS资料的经销商印章未清楚显示。",
                "POS可视数据应完整、清晰并由经销商盖章确认。",
                "不能确认POS数据由申报经销商认可。",
                "补交经销商印章清楚、商品明细和合计完整的POS数据。",
            )
        )

    pos_sheet: dict[str, Any] | None = None
    pos_sheet_error: str | None = None
    if pos_spreadsheet_path is not None:
        try:
            pos_sheet = read_maintenance_pos(pos_spreadsheet_path)
        except AuditError as exc:
            pos_sheet_error = str(exc)
    pos_sheet_pass = pos_sheet is not None and not pos_sheet.get("external_formula_cells")
    controls.append({"control_id": "pos_spreadsheet", "status": "pass" if pos_sheet_pass else "fail", "basis": "电子表可读且无外部公式依赖" if pos_sheet_pass else (pos_sheet_error or "POS电子表缺失或存在外部公式依赖")})
    if not pos_sheet_pass:
        issues.append(
            _issue(
                "pos_spreadsheet_invalid",
                "POS数据电子表缺失或不可核验",
                [Path(pos_spreadsheet_path).name] if pos_spreadsheet_path else [],
                pos_sheet_error or ("电子表存在外部公式依赖" if pos_sheet else "未提交POS数据电子表"),
                "提供一份可直接读取、含商品数量和销售金额明细且无外部依赖的POS电子表。",
                "无法确定性计算POS明细和销售基数。",
                "补交可读取且数据完整的POS电子表；将外部链接公式转换为本表内可核验值。",
            )
        )

    visual_quantity, visual_amount, visual_rows, visual_basis = _visible_pos_summary(pos_documents)
    correspondence_pass = False
    correspondence_basis = "盖章POS或电子表缺失，无法对应"
    if pos_sheet is not None and pos_documents:
        totals_match = _same_number(visual_quantity, pos_sheet.get("total_quantity")) and _same_money(
            visual_amount,
            pos_sheet.get("total_sales_amount"),
        )
        rows_match, row_basis = _match_pos_rows(visual_rows, list(pos_sheet.get("records") or []))
        correspondence_pass = totals_match and rows_match
        correspondence_basis = (
            f"{visual_basis}；POS图片总数量{visual_quantity}、总销售额{visual_amount}；"
            f"电子表总数量{pos_sheet.get('total_quantity')}、总销售额{pos_sheet.get('total_sales_amount')}；{row_basis}"
        )
    controls.append({"control_id": "pos_correspondence", "status": "pass" if correspondence_pass else "fail", "basis": correspondence_basis})
    if not correspondence_pass:
        issues.append(
            _issue(
                "pos_correspondence_failed",
                "盖章POS与电子表无法完整对应",
                [*_source_names(pos_documents), *([Path(pos_spreadsheet_path).name] if pos_spreadsheet_path else [])],
                correspondence_basis,
                "盖章POS与电子表的商品、数量、销售金额及合计应完整且唯一对应。",
                "不能确认结算使用的POS基础数据真实一致。",
                "补正盖章POS或电子表，使逐项商品、数量、销售金额和合计完全一致。",
            )
        )

    contract = contracts[0] if len(contracts) == 1 else None
    contract_problems: list[str] = []
    if contract is None:
        contract_problems.append("未提交唯一签章促销合同")
    else:
        if contract.get("signed_visible") != "visible":
            contract_problems.append("合同签章未清楚显示")
        if len(contract.get("party_names") or []) < 2:
            contract_problems.append("合同双方不完整")
        if not contract.get("activity_start") or not contract.get("activity_end"):
            contract_problems.append("活动周期不完整")
        if not contract.get("eligible_pos_scope"):
            contract_problems.append("合格POS范围未明确")
        if not contract.get("calculation_method"):
            contract_problems.append("计算方式未明确")
        if contract.get("rate") is None and not contract.get("expense_lines"):
            contract_problems.append("费率或可计算费用明细未明确")
    contract_pass = not contract_problems
    controls.append({"control_id": "promotional_contract", "status": "pass" if contract_pass else "fail", "basis": "合同基线完整" if contract_pass else "；".join(contract_problems)})
    if not contract_pass:
        issues.append(
            _issue(
                "promotional_contract_invalid",
                "签章促销合同不完整",
                _source_names(contracts),
                "；".join(contract_problems),
                "促销合同应已签章，并完整列示双方、周期、维护范围、合格POS范围、计算方式、费率或明细及金额上限。",
                "缺少维护费用的权威业务与计算基线。",
                "补交双方签章、活动和金额规则完整的促销合同。",
            )
        )

    settlement = settlements[0] if len(settlements) == 1 else None
    settlement_problems: list[str] = []
    if settlement is None:
        settlement_problems.append("未提交唯一结算单")
    else:
        if settlement.get("company_template_visible") != "visible":
            settlement_problems.append("未确认使用公司统一模板")
        if settlement.get("dealer_seal_visible") != "visible":
            settlement_problems.append("经销商盖章未清楚显示")
        if not settlement.get("customer_name"):
            settlement_problems.append("经销商名称未识别")
        if not settlement.get("activity_start") or not settlement.get("activity_end"):
            settlement_problems.append("结算活动周期不完整")
        if not settlement.get("expense_lines"):
            settlement_problems.append("费用明细未识别")
        if not settlement.get("calculation_method"):
            settlement_problems.append("计算方式未识别")
        if settlement.get("claimed_amount") is None:
            settlement_problems.append("申报金额未识别")
    settlement_pass = not settlement_problems
    controls.append({"control_id": "settlement", "status": "pass" if settlement_pass else "fail", "basis": "结算单模板、盖章、明细、计算和金额完整" if settlement_pass else "；".join(settlement_problems)})
    if not settlement_pass:
        issues.append(
            _issue(
                "settlement_invalid",
                "结算单不符合维护费用要求",
                _source_names(settlements),
                "；".join(settlement_problems),
                "使用公司统一模板，由经销商盖章，并详细记录费用明细、计算方式、活动周期和金额。",
                "申报金额及经销商确认依据不完整。",
                "按公司统一模板补交明细、计算、周期和金额完整且经销商盖章的结算单。",
            )
        )

    party_values = [
        str(item.get("customer_name") or "")
        for item in [*pos_documents, *contracts, *settlements]
        if item.get("customer_name")
    ]
    if pos_sheet is not None:
        party_values.extend(str(value) for value in pos_sheet.get("store_values") or [])
    party_pass = _names_compatible(party_values)
    party_basis = "、".join(party_values) if party_values else "未识别到可比主体"
    controls.append({"control_id": "party_alignment", "status": "pass" if party_pass else "fail", "basis": party_basis})
    if not party_pass:
        issues.append(
            _issue(
                "party_alignment_failed",
                "经销商主体无法闭合",
                _source_names([*pos_documents, *contracts, *settlements]),
                party_basis,
                "POS、合同、结算和电子表中的经销商主体应唯一对应。",
                "不能确认各份资料属于同一申报经销商。",
                "补交或更正主体名称完整且相互一致的POS、合同、结算和电子表。",
            )
        )

    period_pass = False
    period_basis = "缺少合同活动周期，无法执行期间核对"
    if contract is not None:
        contract_start = _date_value(contract.get("activity_start"))
        contract_end = _date_value(contract.get("activity_end"))
        comparable_dates: list[date] = []
        for document in [*pos_documents, *settlements, *activity_photos]:
            for field in ("activity_start", "activity_end", "activity_date"):
                parsed = _date_value(document.get(field))
                if parsed is not None:
                    comparable_dates.append(parsed)
        if pos_sheet is not None:
            comparable_dates.extend(
                parsed
                for parsed in (_date_value(value) for value in pos_sheet.get("period_values") or [])
                if parsed is not None
            )
        period_pass = bool(contract_start and contract_end and comparable_dates) and all(
            contract_start <= value <= contract_end for value in comparable_dates
        )
        period_basis = (
            f"合同周期{contract_start}至{contract_end}；可比日期"
            + ("、".join(str(value) for value in comparable_dates) if comparable_dates else "缺失")
        )
    controls.append({"control_id": "period_alignment", "status": "pass" if period_pass else "fail", "basis": period_basis})
    if not period_pass:
        issues.append(
            _issue(
                "period_alignment_failed",
                "活动周期无法一致核对",
                _source_names([*pos_documents, *contracts, *settlements, *activity_photos]),
                period_basis,
                "POS、结算及现场执行日期应位于签章促销合同的活动周期内。",
                "不能确认POS和维护活动发生在合同授权期间。",
                "补交日期清楚且处于同一合同活动期内的POS、结算和活动资料。",
            )
        )

    support_pass = bool(supports or activity_photos)
    support_basis = (
        f"专项文件{len(supports)}份，活动照片{len(activity_photos)}份"
        if support_pass
        else "未提交独立专项文件或活动照片"
    )
    if support_pass and activity_photos and contract is not None:
        start = _date_value(contract.get("activity_start"))
        end = _date_value(contract.get("activity_end"))
        photo_dates = [_date_value(item.get("activity_date")) for item in activity_photos]
        support_pass = bool(start and end) and all(
            value is not None and start <= value <= end for value in photo_dates
        )
        if not support_pass:
            support_basis += "；至少一张活动照片缺少可见日期或日期不在合同期内"
    controls.append({"control_id": "type_specific_support", "status": "pass" if support_pass else "fail", "basis": support_basis})
    if not support_pass:
        issues.append(
            _issue(
                "type_specific_support_missing",
                "维护费用专项资料不完整",
                _source_names([*supports, *activity_photos]),
                support_basis,
                "按具体维护内容提供协议、相关文件及活动周期内的现场照片等执行资料。",
                "只有POS或结算数据不能证明具体维护活动已经发生。",
                "按具体费用内容补交协议、相关文件及活动周期内照片。",
            )
        )

    claim_amount = (
        _decimal(settlement.get("claimed_amount"), label="结算申报金额")
        if settlement is not None
        else None
    ) or Decimal("0.00")
    recalculated: Decimal | None = None
    amount_basis = "缺少合同或POS电子表，无法复算"
    if contract is not None and pos_sheet is not None:
        recalculated, amount_basis = _recalculate_amount(contract, pos_sheet)
    amount_pass = recalculated is not None and _same_money(recalculated, claim_amount)
    if settlement is not None:
        settlement_rate = _decimal(settlement.get("rate"), label="结算费率")
        settlement_sales = _decimal(settlement.get("sales_amount"), label="结算销售额")
        if settlement_rate is not None and settlement_sales is not None:
            printed_calculation = _round_money(settlement_rate * settlement_sales)
            amount_basis += (
                f"；结算单自身列示{settlement_sales}×{settlement_rate}={printed_calculation}，"
                f"申报{claim_amount}"
            )
            amount_pass = amount_pass and _same_money(printed_calculation, claim_amount)
    controls.append({"control_id": "amount_recalculation", "status": "pass" if amount_pass else "fail", "basis": amount_basis})
    if not amount_pass:
        issues.append(
            _issue(
                "amount_recalculation_failed",
                "维护费用金额无法按合同和POS复算一致",
                _source_names([*contracts, *settlements]) + ([Path(pos_spreadsheet_path).name] if pos_spreadsheet_path else []),
                amount_basis,
                "按签章促销合同的明确范围和计算方式复算，结果应与经销商盖章结算单申报金额精确一致。",
                "申报金额缺少可复核计算链，不能自动支持。",
                "补正合同计算规则、POS电子表范围或结算金额，使三者可复算并一致。",
            )
        )

    supported_amount = claim_amount if not issues else Decimal("0.00")
    held_amount = claim_amount - supported_amount
    decision_label = "可核销" if not issues else "资料需补正"
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
        "scenario": "maintenance_fee",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": "维护费用核销",
        "summary": {
            "conclusion": "pass" if not issues else "human_review",
            "claimed_amount": json_number(claim_amount),
            "suggested_approved_amount": json_number(supported_amount),
            "temporarily_held_amount": json_number(held_amount),
            "high_exception_count": sum(issue["severity"] == "high" for issue in issues),
            "medium_exception_count": sum(issue["severity"] == "medium" for issue in issues),
            "error_group_count": len(issues),
            "document_count": len(documents),
            "missing_role_count": len(missing_roles),
            "pos_spreadsheet_status": "valid" if pos_sheet_pass else "missing_or_invalid",
            "decision_label": decision_label,
            "recalculated_amount": json_number(recalculated) if recalculated is not None else None,
        },
        "maintenance_fee_audit": {
            "documents": documents,
            "pos_spreadsheet": pos_sheet,
            "missing_roles": missing_roles,
            "controls": controls,
            "excluded_files": [Path(path).name for path in case.get("excluded_files") or []],
            "issues": issues,
        },
        "exceptions": exceptions,
    }
