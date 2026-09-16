from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from .common import optional_iso_date, normalized_name_score
from .common import AuditError, json_number, money, now_utc
from .excel_sources import read_pos_target_summary


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
        "source_files": list(dict.fromkeys(source_files)),
        "observed": observed,
        "expected": expected,
        "impact": impact,
        "resubmission": resubmission,
        "severity": "high",
        "confidence": "high",
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


def _equal(left: Any, right: Any) -> bool:
    a = _decimal(left, label="左侧金额")
    b = _decimal(right, label="右侧金额")
    return a is not None and b is not None and abs(a - b) <= CENT


def _control(controls: list[dict[str, str]], control_id: str, passed: bool, basis: str) -> None:
    controls.append({"control_id": control_id, "status": "pass" if passed else "fail", "basis": basis})


def _pos_correspondence(
    visual_documents: list[dict[str, Any]],
    spreadsheet: dict[str, Any] | None,
) -> tuple[bool, str]:
    if spreadsheet is None or not visual_documents:
        return False, "盖章POS或POS电子表缺失"
    visual_rows = [row for item in visual_documents for row in (item.get("pos_rows") or [])]
    unused = set(range(len(visual_rows)))
    problems: list[str] = []
    for record in spreadsheet.get("records") or []:
        candidates = sorted(
            (
                (normalized_name_score(record.get("store_name"), visual_rows[index].get("store_name")), index)
                for index in unused
                if _equal(record.get("sales_amount"), visual_rows[index].get("sales_amount"))
            ),
            reverse=True,
        )
        if not candidates or candidates[0][0] < 0.82 or (
            len(candidates) > 1 and candidates[0][0] - candidates[1][0] < 0.03
        ):
            problems.append(
                f"Excel第{record.get('excel_row')}行{record.get('store_name')}={record.get('sales_amount')}"
            )
            continue
        unused.remove(candidates[0][1])
    printed_totals = [
        item.get("pos_total_sales_amount")
        for item in visual_documents
        if item.get("pos_total_sales_amount") is not None
    ]
    total_pass = len(printed_totals) == 1 and _equal(
        printed_totals[0], spreadsheet.get("total_sales_amount")
    )
    passed = not problems and not unused and total_pass
    return (
        passed,
        f"Excel{len(spreadsheet.get('records') or [])}行，盖章POS{len(visual_rows)}行，"
        f"未对应Excel行{len(problems)}，未使用盖章行{len(unused)}；"
        f"盖章合计{printed_totals[0] if len(printed_totals) == 1 else '不唯一或缺失'}，"
        f"电子表合计{spreadsheet.get('total_sales_amount')}"
    )


def _activity_proof_valid(
    proofs: list[dict[str, Any]],
    start: date | None,
    end: date | None,
) -> tuple[bool, str]:
    valid: list[str] = []
    invalid: list[str] = []
    for item in proofs:
        source = str(item.get("source_file") or "")
        activity_date = optional_iso_date(item.get("activity_date"))
        period_ok = start is not None and end is not None and activity_date is not None and start <= activity_date <= end
        role = str(item.get("role") or "")
        if role == "activity_photo":
            content_ok = all(
                item.get(field) == "visible"
                for field in (
                    "watermark_date_visible",
                    "watermark_time_visible",
                    "watermark_address_visible",
                    "activity_exists_visible",
                )
            ) and bool(item.get("activity_time")) and bool(item.get("activity_location"))
        elif role == "store_receipt":
            content_ok = (
                item.get("activity_exists_visible") == "visible"
                and bool(item.get("receipt_number"))
                and item.get("receipt_amount") is not None
            )
        else:
            content_ok = item.get("activity_exists_visible") == "visible"
        (valid if period_ok and content_ok else invalid).append(source)
    return bool(valid), f"提交{len(proofs)}份活动证明，有效{len(valid)}份，无效{len(invalid)}份"


def audit_pos_target_incentive_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "pos_target_incentive":
        raise AuditError("POS达标激励审核收到错误的场景类型")
    documents = list(evidence.get("documents") or [])
    contracts = _by_role(documents, "signed_promotional_contract")
    settlements = _by_role(documents, "settlement")
    pos_documents = _by_role(documents, "stamped_pos_data")
    proofs = [
        item for item in documents
        if str(item.get("role")) in {"activity_photo", "store_receipt", "other_activity_proof"}
    ]
    contract = contracts[0] if len(contracts) == 1 else None
    settlement = settlements[0] if len(settlements) == 1 else None
    issues: list[dict[str, Any]] = []
    controls: list[dict[str, str]] = []

    missing: list[str] = []
    if not pos_documents:
        missing.append("经销商盖章POS数据")
    if case.get("pos_spreadsheet") is None:
        missing.append("POS数据电子表")
    if len(contracts) != 1:
        missing.append("签章后的促销合同")
    if len(settlements) != 1:
        missing.append("公司统一模板盖章结算单")
    if not proofs:
        missing.append("满减活动水印照片、小票或其他存在证明")
    materials_pass = not missing
    _control(controls, "required_materials", materials_pass, "五类资料齐全" if materials_pass else "缺少：" + "、".join(missing))
    if missing:
        issues.append(
            _issue(
                "required_materials_missing",
                "POS达标激励资料不完整",
                _sources(documents),
                "缺少：" + "、".join(missing),
                "盖章POS、POS电子表、活动存在证明、统一模板盖章结算单和签章促销合同必须同时提交。",
                "证据链不完整，申报金额不能核销。",
                "补交" + "、".join(missing) + "。",
            )
        )

    contract_pass = bool(contract) and all(
        (
            contract.get("signed_visible") == "visible",
            contract.get("recipient_type") == "dealer",
            contract.get("channel_eligibility_visible") == "visible",
            contract.get("activity_start") is not None,
            contract.get("activity_end") is not None,
            bool(contract.get("pos_scope")),
            bool(contract.get("target_tiers")),
            contract.get("amount_ceiling") is not None,
        )
    )
    _control(controls, "contract_authority", contract_pass, "合同已签章并明确经销商、渠道、周期、POS范围、档位、比例和上限" if contract_pass else "缺少合同或合同权威条款不完整")
    if not contract_pass:
        issues.append(
            _issue(
                "contract_authority_missing",
                "促销合同未建立POS达标激励权威规则",
                _sources(contracts),
                controls[-1]["basis"],
                "签章合同须明确激励对象为经销商、战略/批准特殊渠道资格、活动周期和内容、POS范围、目标档位、比例及上限。",
                "结算单中的比例和上限不能替代合同授权。",
                "补交上述条款完整并已签章的促销合同。",
            )
        )

    settlement_pass = bool(settlement) and all(
        (
            settlement.get("company_template_visible") == "visible",
            settlement.get("dealer_seal_visible") == "visible",
            settlement.get("recipient_type") == "dealer",
            settlement.get("pos_sales_amount") is not None,
            bool(settlement.get("target_tiers")),
            settlement.get("amount_ceiling") is not None,
            settlement.get("claimed_amount") is not None,
        )
    )
    _control(controls, "settlement", settlement_pass, "结算模板、经销商盖章、POS基数、档位、比例、上限和申报完整" if settlement_pass else "结算模板、盖章或计算明细不完整")
    if not settlement_pass:
        issues.append(
            _issue(
                "settlement_invalid",
                "结算单未满足统一模板盖章要求",
                _sources(settlements),
                controls[-1]["basis"],
                "结算单须使用公司统一模板、由经销商盖章并列明POS基数、档位、比例、上限和申报金额。",
                "申报金额缺少有效确认或计算明细。",
                "按公司统一模板补交完整盖章结算单。",
            )
        )

    pos_seal_pass = bool(pos_documents) and all(
        item.get("dealer_seal_visible") == "visible" for item in pos_documents
    )
    _control(controls, "pos_visual_seal", pos_seal_pass, "盖章POS印章清楚" if pos_seal_pass else "POS缺失或至少一页未见经销商印章")
    if not pos_seal_pass:
        issues.append(
            _issue(
                "stamped_pos_invalid",
                "POS数据未完成经销商盖章确认",
                _sources(pos_documents),
                controls[-1]["basis"],
                "经销商盖章POS须清晰列明期间、门店、销售金额和合计。",
                "不能确认销售基数由经销商认可。",
                "补交明细及印章清楚的完整POS数据。",
            )
        )

    pos_sheet: dict[str, Any] | None = None
    pos_error: str | None = None
    if case.get("pos_spreadsheet") is not None:
        try:
            pos_sheet = read_pos_target_summary(case["pos_spreadsheet"])
        except AuditError as exc:
            pos_error = str(exc)
    pos_sheet_pass = pos_sheet is not None and not pos_sheet.get("external_formula_cells") and pos_sheet.get("printed_amount_status") == "exact"
    _control(controls, "pos_spreadsheet", pos_sheet_pass, "电子表20家门店明细可读、打印合计一致且无外部依赖" if pos_sheet_pass else (pos_error or "电子表缺失、合计不一致或存在外部依赖"))
    if not pos_sheet_pass:
        issues.append(
            _issue(
                "pos_spreadsheet_invalid",
                "POS电子表缺失或不可核验",
                [Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else [],
                controls[-1]["basis"],
                "电子表须可直接读取逐门店期间和销售金额，打印合计正确且无外部公式依赖。",
                "无法确定POS达标基数。",
                "补交完整、可读取且无外部依赖的POS电子表。",
            )
        )

    correspondence_pass, correspondence_basis = _pos_correspondence(pos_documents, pos_sheet)
    _control(controls, "pos_correspondence", correspondence_pass, correspondence_basis)
    if not correspondence_pass:
        issues.append(
            _issue(
                "pos_correspondence_failed",
                "盖章POS与电子表未逐门店一致",
                [*_sources(pos_documents), *([Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else [])],
                correspondence_basis,
                "两种POS载体的活动期间、门店和销售金额须逐项唯一对应，合计金额精确一致。",
                "电子表销售额不能直接作为激励计算基数。",
                "更正并重交逐门店及合计一致的盖章POS和电子表。",
            )
        )

    recipient_pass = bool(contract and settlement) and contract.get("recipient_type") == "dealer" and settlement.get("recipient_type") == "dealer" and normalized_name_score(contract.get("dealer_name"), settlement.get("dealer_name")) >= 0.82
    _control(controls, "dealer_recipient", recipient_pass, "合同与结算均明确同一经销商为激励对象" if recipient_pass else "合同缺失、激励对象不是经销商或经销商名称不一致")
    if not recipient_pass:
        issues.append(
            _issue(
                "dealer_recipient_unproven",
                "经销商激励对象未由合同和结算共同确认",
                _sources([item for item in (contract, settlement) if item]),
                controls[-1]["basis"],
                "本场景激励对象必须是合同约定且结算确认的同一经销商。",
                "不能确认费用应支付给本申报经销商。",
                "补交明确经销商激励对象和法定全称的签章合同及盖章结算单。",
            )
        )

    start = optional_iso_date(contract.get("activity_start")) if contract else None
    end = optional_iso_date(contract.get("activity_end")) if contract else None
    activity_pass, activity_basis = _activity_proof_valid(proofs, start, end)
    _control(controls, "activity_existence", activity_pass, activity_basis)
    if not activity_pass:
        issues.append(
            _issue(
                "activity_existence_unproven",
                "满减活动缺少有效存在证明",
                _sources(proofs),
                activity_basis,
                "至少提交一份合同活动期内的水印现场照片、小票或其他能独立证明满减活动存在的资料。",
                "POS销售达标本身不能证明满减活动实际执行。",
                "补交带日期、地址、拍摄时间水印的活动照片、小票或其他独立活动证明。",
            )
        )

    calculated: Decimal | None = None
    calculation_basis = "缺少签章合同档位/比例或可核验POS基数"
    amount_pass = False
    if contract and settlement and pos_sheet:
        sales = _decimal(pos_sheet.get("total_sales_amount"), label="合格POS销售额")
        tiers = sorted(
            (
                (_decimal(item.get("threshold_amount"), label="目标门槛"), _decimal(item.get("rate"), label="激励比例"))
                for item in (contract.get("target_tiers") or [])
            ),
            key=lambda item: item[0] or Decimal("0"),
        )
        eligible_tiers = [item for item in tiers if sales is not None and item[0] is not None and item[1] is not None and sales >= item[0]]
        ceiling = _decimal(contract.get("amount_ceiling"), label="合同上限")
        claim = _decimal(settlement.get("claimed_amount"), label="结算申报")
        settlement_sales = _decimal(settlement.get("pos_sales_amount"), label="结算POS基数")
        if sales is not None and eligible_tiers and ceiling is not None and claim is not None and settlement_sales is not None:
            threshold, rate = eligible_tiers[-1]
            assert threshold is not None and rate is not None
            calculated = min(_round(sales * rate), ceiling)
            calculation_basis = (
                f"POS销售{sales}达到最高合同门槛{threshold}，比例{rate}，"
                f"比例金额{_round(sales * rate)}，受上限{ceiling}约束后{calculated}；"
                f"结算POS基数{settlement_sales}、申报{claim}"
            )
            amount_pass = abs(sales - settlement_sales) <= CENT and abs(calculated - claim) <= CENT
    _control(controls, "amount_recalculation", amount_pass, calculation_basis)
    if not amount_pass:
        issues.append(
            _issue(
                "amount_recalculation_failed",
                "POS达标激励金额无法按签章合同闭环复算",
                _sources([item for item in (contract, settlement) if item]) + ([Path(case["pos_spreadsheet"]).name] if case.get("pos_spreadsheet") else []),
                calculation_basis,
                "按合同合格POS选择最高已达目标档位，POS销售额×该档比例并应用合同上限后，须与盖章结算申报一致。",
                "缺少合同权威或金额不一致时不能核销。",
                "补交合同或补正POS、档位、比例、上限和结算金额，使全链路可复算且一致。",
            )
        )

    claim_value = _decimal(settlement.get("claimed_amount"), label="结算申报金额") if settlement else None
    claimed = claim_value or Decimal("0")
    blocked = any(item["status"] != "pass" for item in controls)
    supported = Decimal("0") if blocked else min(calculated or Decimal("0"), claimed)
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
        "scenario": "pos_target_incentive",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": "POS达标激励核销（经销商）",
        "summary": {
            "conclusion": "human_review" if blocked else "pass",
            "decision_label": "资料需补正" if blocked else "可核销",
            "claimed_amount": json_number(claimed),
            "suggested_approved_amount": json_number(supported),
            "temporarily_held_amount": json_number(held),
            "high_exception_count": len(exceptions),
            "medium_exception_count": 0,
            "calculated_amount": json_number(calculated) if calculated is not None else None,
        },
        "exceptions": exceptions,
        "pos_target_incentive_audit": {
            "controls": controls,
            "issues": issues,
            "pos_spreadsheet": pos_sheet,
            "calculation_basis": calculation_basis,
        },
    }
