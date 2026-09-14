from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .common import AuditError, json_number, money, normalize_text, now_utc, sha256_file


CENT = Decimal("0.01")
GIVEAWAY_MARKERS = ("额外搭赠", "搭赠", "买赠", "赠品", "赠送")
CONFLICT_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("维护费用", ("维护费用", "渠道维护")),
    ("人员激励", ("人员激励", "人员奖励", "销售提成")),
    ("堆头陈列", ("堆头", "陈列费", "场地使用费")),
    ("展示道具", ("展示道具", "物料制作", "海报制作")),
    ("直营", ("直营费用", "直营维护")),
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
        "source_files": list(dict.fromkeys(name for name in source_files if name)),
        "observed": observed,
        "expected": expected,
        "impact": impact,
        "resubmission": resubmission,
        "severity": severity,
        "confidence": confidence,
    }


def _documents_by_type(
    documents: list[dict[str, Any]],
    document_type: str,
) -> list[dict[str, Any]]:
    return [
        document
        for document in documents
        if str(document.get("document_type") or "") == document_type
    ]


def _source_names(documents: list[dict[str, Any]]) -> list[str]:
    return [str(document.get("source_file") or "") for document in documents]


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


def _legal_name(value: Any) -> str:
    text = normalize_text(value)
    for marker in ("有限责任公司", "有限公司", "商贸", "贸易", "公司"):
        text = text.replace(normalize_text(marker), "")
    return text


def _name_similarity(left: Any, right: Any) -> float:
    left_text = _legal_name(left)
    right_text = _legal_name(right)
    if not left_text or not right_text:
        return 0.0
    if left_text == right_text:
        return 1.0
    if left_text in right_text or right_text in left_text:
        return min(len(left_text), len(right_text)) / max(len(left_text), len(right_text))
    return SequenceMatcher(None, left_text, right_text).ratio()


def _names_compatible(values: list[str], *, threshold: float = 0.78) -> bool:
    cleaned = [value for value in values if _legal_name(value)]
    if len(cleaned) < 2:
        return False
    baseline = cleaned[0]
    return all(_name_similarity(baseline, value) >= threshold for value in cleaned[1:])


def _dealer_name(document: dict[str, Any]) -> str:
    explicit = str(document.get("dealer_name") or "").strip()
    if explicit:
        return explicit
    parties = [str(value) for value in document.get("party_names") or []]
    company_names = [value for value in parties if "公司" in value]
    return max(company_names, key=len, default="")


def _document_text(document: dict[str, Any]) -> str:
    values: list[Any] = [
        document.get("title"),
        document.get("fee_type"),
        document.get("calculation_text"),
        document.get("gift_ratio_text"),
        document.get("activity_content"),
        document.get("visible_summary"),
    ]
    for line in document.get("product_lines") or []:
        values.extend(
            (
                line.get("product_name"),
                line.get("gift_ratio_text"),
            )
        )
    return normalize_text(" ".join(str(value or "") for value in values))


def _fee_nature(
    authoritative_documents: list[dict[str, Any]],
) -> tuple[bool, str, list[str]]:
    if not authoritative_documents:
        return False, "未提交可核对费用性质的合同或结算", []
    missing_markers: list[str] = []
    conflicts: list[str] = []
    sources: list[str] = []
    for document in authoritative_documents:
        source = str(document.get("source_file") or "")
        text = _document_text(document)
        sources.append(source)
        if not any(normalize_text(marker) in text for marker in GIVEAWAY_MARKERS):
            missing_markers.append(f"{source}未明确额外搭赠或赠品支持")
        for label, markers in CONFLICT_MARKERS:
            if any(normalize_text(marker) in text for marker in markers):
                conflicts.append(f"{source}明确显示{label}")
    if conflicts:
        return False, "；".join(conflicts), sources
    if missing_markers:
        return False, "；".join(missing_markers), sources
    return True, "合同和结算均明确显示额外搭赠或赠品支持", sources


def _gift_lines(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        line
        for line in document.get("product_lines") or []
        if str(line.get("line_type") or "") == "gift"
    ]


def _eligible_lines(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        line
        for line in document.get("product_lines") or []
        if str(line.get("line_type") or "") in {"eligible_sale", "shipment"}
    ]


def _gift_plan_amount(
    document: dict[str, Any],
) -> tuple[Decimal | None, str]:
    lines = _gift_lines(document)
    if lines and all(
        line.get("quantity") is not None and line.get("unit_price") is not None
        for line in lines
    ):
        amount = sum(
            Decimal(str(line["quantity"])) * Decimal(str(line["unit_price"]))
            for line in lines
        )
        return _round_money(amount), f"{len(lines)}项赠品数量×明确单价求和"
    if lines and all(line.get("amount") is not None for line in lines):
        amount = sum(Decimal(str(line["amount"])) for line in lines)
        return _round_money(amount), f"{len(lines)}项明确赠品小计求和"
    quantity = _decimal(document.get("total_gift_quantity"), label="赠品总数量")
    unit_price = _decimal(document.get("gift_unit_price"), label="赠品单价")
    if quantity is not None and unit_price is not None:
        return _round_money(quantity * unit_price), "赠品总数量×明确赠品单价"
    return None, "缺少可完整复算的赠品数量与明确单价或逐项小计"


def _ratio_value(document: dict[str, Any]) -> tuple[Decimal, Decimal] | None:
    values = [document.get("gift_ratio_text")]
    values.extend(line.get("gift_ratio_text") for line in document.get("product_lines") or [])
    parsed: set[tuple[Decimal, Decimal]] = set()
    for value in values:
        text = str(value or "")
        match = re.search(r"(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)", text)
        if match:
            buy = Decimal(match.group(1))
            gift = Decimal(match.group(2))
            if buy > 0 and gift >= 0:
                parsed.add((buy, gift))
    return next(iter(parsed)) if len(parsed) == 1 else None


def _product_text(value: Any) -> str:
    text = normalize_text(value)
    for marker in ("oralshark", "参半", "赠送", "赠", "送"):
        text = text.replace(normalize_text(marker), "")
    return text


def _product_score(source: dict[str, Any], baseline: dict[str, Any]) -> float:
    source_code = normalize_text(source.get("product_code"))
    baseline_code = normalize_text(baseline.get("product_code"))
    if source_code and baseline_code and source_code != baseline_code:
        return 0.0
    source_barcode = normalize_text(source.get("barcode_69"))
    baseline_barcode = normalize_text(baseline.get("barcode_69"))
    if source_barcode and baseline_barcode and source_barcode != baseline_barcode:
        return 0.0
    source_name = _product_text(source.get("product_name"))
    baseline_name = _product_text(baseline.get("product_name"))
    if not source_name or not baseline_name:
        return 0.0
    if source_name == baseline_name:
        return 1.0
    if source_name in baseline_name or baseline_name in source_name:
        return 0.92
    return SequenceMatcher(None, source_name, baseline_name).ratio()


def _unique_product_match(
    source: dict[str, Any],
    baselines: list[dict[str, Any]],
) -> bool:
    scores = sorted((_product_score(source, baseline) for baseline in baselines), reverse=True)
    if not scores or scores[0] < 0.55:
        return False
    return len(scores) == 1 or scores[0] - scores[1] >= 0.05


def _product_correspondence(
    contract: dict[str, Any] | None,
    settlement: dict[str, Any] | None,
    delivery: dict[str, Any] | None,
    receipts: list[dict[str, Any]],
) -> tuple[bool, str]:
    if contract is None:
        return False, "缺少合同商品基线"
    eligible = _eligible_lines(contract)
    gifts = _gift_lines(contract)
    if not eligible or not gifts:
        return False, "合同未同时识别合格购买商品和赠品商品"
    checks: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    if delivery is not None:
        for line in delivery.get("product_lines") or []:
            line_type = str(line.get("line_type") or "")
            if line_type in {"eligible_sale", "shipment"}:
                checks.append((f"{delivery.get('source_file')}第{line.get('line_no')}行", line, eligible))
            elif line_type == "gift":
                checks.append((f"{delivery.get('source_file')}第{line.get('line_no')}行", line, gifts))
    if settlement is not None:
        for line in _gift_lines(settlement):
            checks.append((f"{settlement.get('source_file')}第{line.get('line_no')}行", line, gifts))
    for receipt in receipts:
        for line in receipt.get("product_lines") or []:
            line_type = str(line.get("line_type") or "")
            if line_type == "eligible_sale":
                checks.append((f"{receipt.get('source_file')}第{line.get('line_no')}行", line, eligible))
            elif line_type == "gift":
                checks.append((f"{receipt.get('source_file')}第{line.get('line_no')}行", line, gifts))
    problems = [label for label, line, baselines in checks if not _unique_product_match(line, baselines)]
    if not checks:
        return False, "出货、结算和小票没有可与合同核对的商品行"
    if problems:
        return False, "以下商品行无法唯一对应合同范围：" + "、".join(problems)
    return True, f"{len(checks)}个出货、结算或小票商品行均唯一对应合同购买/赠品范围"


def _receipt_execution(
    receipts: list[dict[str, Any]],
    contract: dict[str, Any] | None,
) -> tuple[bool, str]:
    if not receipts:
        return False, "未提交门店销售小票"
    if contract is None:
        return False, "缺少合同，无法核对小票买赠比例"
    start = _date_value(contract.get("activity_start"))
    end = _date_value(contract.get("activity_end"))
    ratio = _ratio_value(contract)
    if start is None or end is None or ratio is None:
        return False, "合同活动期或唯一买赠比例缺失"
    buy_ratio, gift_ratio = ratio
    problems: list[str] = []
    for receipt in receipts:
        source = str(receipt.get("source_file") or "")
        receipt_date = _date_value(receipt.get("document_date"))
        sale_lines = [
            line for line in receipt.get("product_lines") or []
            if str(line.get("line_type") or "") == "eligible_sale"
        ]
        gift_lines = _gift_lines(receipt)
        if receipt_date is None or not start <= receipt_date <= end:
            problems.append(f"{source}日期缺失或不在合同期内")
        if not sale_lines or not gift_lines:
            problems.append(f"{source}未同时显示购买商品和赠品")
            continue
        if any(line.get("quantity") is None for line in [*sale_lines, *gift_lines]):
            problems.append(f"{source}购买或赠品数量不完整")
            continue
        if any(line.get("amount") is None or not _same_money(line.get("amount"), 0) for line in gift_lines):
            problems.append(f"{source}至少一项赠品未明确显示零金额")
        sale_quantity = sum(Decimal(str(line["quantity"])) for line in sale_lines)
        gift_quantity = sum(Decimal(str(line["quantity"])) for line in gift_lines)
        if gift_quantity * buy_ratio != sale_quantity * gift_ratio:
            problems.append(
                f"{source}购买数量{sale_quantity}、赠品数量{gift_quantity}不符合{buy_ratio}:{gift_ratio}"
            )
    if problems:
        return False, "；".join(problems)
    return True, f"{len(receipts)}张小票均在合同期内，包含零金额赠品并符合{buy_ratio}:{gift_ratio}"


def audit_giveaway_promotion_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "giveaway_promotion":
        raise AuditError("额外搭赠审核收到错误的场景类型")

    documents = list(evidence.get("documents") or [])
    contracts = _documents_by_type(documents, "signed_promotional_contract")
    settlements = _documents_by_type(documents, "settlement")
    deliveries = _documents_by_type(documents, "sales_delivery_statement")
    receipts = _documents_by_type(documents, "store_receipt")
    photos = _documents_by_type(documents, "activity_photo")
    issues: list[dict[str, Any]] = []
    controls: list[dict[str, str]] = []

    def control(control_id: str, passed: bool, basis: str) -> None:
        controls.append(
            {"control_id": control_id, "status": "pass" if passed else "fail", "basis": basis}
        )

    missing_roles: list[str] = []
    multiple_roles: list[str] = []
    for candidates, label in (
        (contracts, "签章额外搭赠促销合同"),
        (settlements, "经销商盖章结算单"),
        (deliveries, "系统销售或出货明细"),
    ):
        if not candidates:
            missing_roles.append(label)
        elif len(candidates) > 1:
            sources = _source_names(candidates)
            multiple_roles.append(label)
            issues.append(
                _issue(
                    "multiple_role_candidates",
                    f"{label}存在多份材料，主材料无法唯一确认",
                    sources,
                    f"识别到{len(candidates)}份{label}：" + "、".join(sources),
                    f"本次核销的{label}应能明确唯一适用的主材料；文件名称相近不能证明内容重复。",
                    "未选择其中任意一份作为核销依据，暂缓全部申报。",
                    "请说明所列各份材料的用途并明确本次适用版本；确认误交的重复文件后再移除。",
                )
            )
    if not receipts:
        missing_roles.append("活动期内门店销售小票")
    if not photos:
        missing_roles.append("活动期内门店现场照片")
    materials_pass = not missing_roles and not multiple_roles
    material_problems = []
    if missing_roles:
        material_problems.append("缺少：" + "、".join(missing_roles))
    if multiple_roles:
        material_problems.append("存在多份且未能唯一绑定：" + "、".join(multiple_roles))
    control("required_materials", materials_pass, "五类资料齐全" if materials_pass else "；".join(material_problems))
    if missing_roles:
        issues.append(
            _issue(
                "required_materials_missing",
                "额外搭赠核销资料不完整",
                _source_names(documents),
                "缺少：" + "、".join(missing_roles),
                "额外搭赠必须同时提供签章促销合同、盖章结算、系统销售/出货明细、门店小票和活动照片。",
                "证据链不完整，不能确认全部申报赠品已按合同执行。",
                "补交" + "、".join(missing_roles) + "。",
            )
        )

    contract = contracts[0] if len(contracts) == 1 else None
    settlement = settlements[0] if len(settlements) == 1 else None
    delivery = deliveries[0] if len(deliveries) == 1 else None

    fee_pass, fee_basis, fee_sources = _fee_nature([*contracts, *settlements])
    control("fee_nature", fee_pass, fee_basis)
    if not fee_pass:
        issues.append(
            _issue(
                "fee_nature_conflict",
                "费用性质不是明确的额外搭赠",
                fee_sources,
                fee_basis,
                "促销合同和结算正文均应明确额外搭赠、买赠或赠品支持性质。",
                "归档名不能证明费用性质，也不得把正常出货或其他费用改称搭赠。",
                "补交正文费用性质一致、经销商签章完整的额外搭赠合同和结算。",
            )
        )

    contract_problems: list[str] = []
    if contract is None:
        contract_problems.append("未识别到唯一促销合同")
    else:
        if contract.get("signed_visible") != "visible":
            contract_problems.append("经销商签章未清楚显示")
        if not _dealer_name(contract):
            contract_problems.append("经销商名称未识别")
        if not contract.get("activity_start") or not contract.get("activity_end"):
            contract_problems.append("活动周期不完整")
        if not _eligible_lines(contract) or not _gift_lines(contract):
            contract_problems.append("购买商品或赠品范围不完整")
        if _ratio_value(contract) is None:
            contract_problems.append("买赠比例缺失或不唯一")
        if contract.get("contract_budget") is None:
            contract_problems.append("合同预算未识别")
        contract_amount, _ = _gift_plan_amount(contract)
        if contract_amount is None:
            contract_problems.append("赠品数量和单价不能复算")
    contract_pass = not contract_problems
    control("promotional_contract", contract_pass, "合同基线完整" if contract_pass else "；".join(contract_problems))
    if not contract_pass:
        issues.append(
            _issue(
                "promotional_contract_invalid",
                "额外搭赠促销合同不完整",
                _source_names(contracts),
                "；".join(contract_problems),
                "合同应有经销商签章，并明确周期、购买商品、赠品、买赠比例、赠品数量、单价、预算和核销资料。",
                "缺少额外搭赠的业务和金额权威基线。",
                "补交经销商签章及额外搭赠条款完整的促销合同。",
            )
        )

    settlement_problems: list[str] = []
    if settlement is None:
        settlement_problems.append("未识别到唯一结算单")
    else:
        if settlement.get("company_template_visible") != "visible":
            settlement_problems.append("公司结算模板结构未确认")
        if settlement.get("dealer_seal_visible") != "visible":
            settlement_problems.append("经销商盖章未清楚显示")
        if not _dealer_name(settlement):
            settlement_problems.append("经销商名称未识别")
        if not settlement.get("activity_start") or not settlement.get("activity_end"):
            settlement_problems.append("结算活动周期不完整")
        if settlement.get("shipment_amount") is None:
            settlement_problems.append("正常出货金额未识别")
        if settlement.get("claimed_gift_amount") is None:
            settlement_problems.append("额外搭赠申报金额未识别")
        settlement_amount, _ = _gift_plan_amount(settlement)
        if settlement_amount is None:
            settlement_problems.append("赠品明细、数量或单价不能复算")
        if not settlement.get("calculation_text"):
            settlement_problems.append("结算计算方式未识别")
    settlement_pass = not settlement_problems
    control("settlement", settlement_pass, "结算模板、盖章、出货和赠品申报完整" if settlement_pass else "；".join(settlement_problems))
    if not settlement_pass:
        issues.append(
            _issue(
                "settlement_invalid",
                "额外搭赠结算单不完整",
                _source_names(settlements),
                "；".join(settlement_problems),
                "结算单应使用公司结构、经销商盖章，并分别列示正常出货、赠品明细、计算方式和额外搭赠申报金额。",
                "不能确认经销商申报金额及其计算依据。",
                "补交模板、盖章、周期、出货、赠品数量单价和申报金额完整的结算单。",
            )
        )

    delivery_problems: list[str] = []
    if delivery is None:
        delivery_problems.append("未识别到唯一系统销售或出货明细")
    else:
        if not (delivery.get("product_lines") or []):
            delivery_problems.append("出货商品明细缺失")
        if delivery.get("shipment_amount") is None:
            delivery_problems.append("正常出货合计未识别")
        if not (_dealer_name(delivery) or delivery.get("store_name")):
            delivery_problems.append("经销商或门店主体未识别")
        if delivery.get("dealer_seal_visible") not in {"visible", "not_applicable"}:
            delivery_problems.append("经销商盖章或独立系统凭据不清楚")
    delivery_pass = not delivery_problems
    control("sales_delivery_statement", delivery_pass, "出货主体、明细和合计完整" if delivery_pass else "；".join(delivery_problems))
    if not delivery_pass:
        issues.append(
            _issue(
                "sales_delivery_invalid",
                "系统销售或出货明细不完整",
                _source_names(deliveries),
                "；".join(delivery_problems),
                "系统销售或出货凭据应显示主体、逐项商品和正常出货合计，并保留可核验的系统或经销商确认。",
                "无法核对结算使用的正常出货金额和商品范围。",
                "补交完整、清晰且可核验的系统销售或出货明细。",
            )
        )

    dealer_values = [
        _dealer_name(item)
        for item in (contract, settlement, delivery)
        if item is not None
    ]
    dealer_pass = len(dealer_values) == 3 and _names_compatible(dealer_values)
    store_values = [
        str(value)
        for value in [
            delivery.get("store_name") if delivery else None,
            *(receipt.get("store_name") for receipt in receipts),
        ]
        if value
    ]
    store_pass = len(store_values) < 2 or _names_compatible(store_values, threshold=0.72)
    party_pass = dealer_pass and store_pass
    party_basis = "经销商：" + ("、".join(dealer_values) if dealer_values else "缺失")
    if store_values:
        party_basis += "；门店：" + "、".join(store_values)
    control("party_alignment", party_pass, party_basis)
    if not party_pass:
        issues.append(
            _issue(
                "party_alignment_failed",
                "额外搭赠经销商或门店主体无法闭合",
                _source_names([item for item in (contract, settlement, delivery) if item]) + _source_names(receipts),
                party_basis,
                "合同、结算和出货的经销商应唯一对应；出货门店与门店小票名称在可比时应唯一兼容。",
                "不能确认全部资料属于同一申报经销商和活动门店。",
                "补交或更正主体名称清楚且相互一致的合同、结算、出货和小票。",
            )
        )

    period_pass = False
    period_basis = "缺少合同活动周期"
    if contract is not None:
        start = _date_value(contract.get("activity_start"))
        end = _date_value(contract.get("activity_end"))
        dates: list[tuple[str, date | None]] = []
        if settlement is not None:
            dates.extend(
                [
                    ("结算开始", _date_value(settlement.get("activity_start"))),
                    ("结算结束", _date_value(settlement.get("activity_end"))),
                ]
            )
        if delivery is not None:
            dates.append(("出货日期", _date_value(delivery.get("document_date"))))
        dates.extend(
            (str(receipt.get("source_file")), _date_value(receipt.get("document_date")))
            for receipt in receipts
        )
        dates.extend(
            (str(photo.get("source_file")), _date_value(photo.get("activity_date")))
            for photo in photos
        )
        period_pass = bool(start and end and dates) and all(
            value is not None and start <= value <= end for _, value in dates
        )
        period_basis = (
            f"合同周期{start}至{end}；"
            + "、".join(f"{label}={value}" for label, value in dates)
        )
    control("period_alignment", period_pass, period_basis)
    if not period_pass:
        issues.append(
            _issue(
                "period_alignment_failed",
                "额外搭赠活动周期无法闭合",
                _source_names([item for item in (contract, settlement, delivery) if item]) + _source_names([*receipts, *photos]),
                period_basis,
                "结算、系统出货、门店小票和活动照片日期应位于签章合同活动期内。",
                "不能确认出货和买赠执行发生在合同授权期间。",
                "补交或更正日期清楚且处于同一合同活动期的材料。",
            )
        )

    shipment_pass = bool(
        settlement is not None
        and delivery is not None
        and _same_money(settlement.get("shipment_amount"), delivery.get("shipment_amount"))
    )
    shipment_basis = (
        f"结算正常出货{settlement.get('shipment_amount') if settlement else '缺失'}元；"
        f"系统销售/出货合计{delivery.get('shipment_amount') if delivery else '缺失'}元"
    )
    control("shipment_reconciliation", shipment_pass, shipment_basis)
    if not shipment_pass:
        issues.append(
            _issue(
                "shipment_reconciliation_failed",
                "正常出货金额与结算不一致",
                _source_names([item for item in (settlement, delivery) if item]),
                shipment_basis,
                "系统销售/出货合计应与结算单正常出货金额精确一致；该金额不等于额外搭赠申报。",
                "结算使用的正常出货基础无法确认。",
                "补正系统销售/出货明细或结算中的正常出货金额，使两者一致。",
            )
        )

    products_pass, products_basis = _product_correspondence(
        contract, settlement, delivery, receipts
    )
    control("product_correspondence", products_pass, products_basis)
    if not products_pass:
        issues.append(
            _issue(
                "product_correspondence_failed",
                "购买商品和赠品无法按合同唯一对应",
                _source_names([item for item in (contract, settlement, delivery) if item]) + _source_names(receipts),
                products_basis,
                "出货和小票购买商品应对应合同合格商品，赠品行应对应合同赠品；已有产品编码和69码必须精确一致。",
                "不能确认申报赠品用于合同约定的合格交易。",
                "补交或更正商品名称、规格、编码、69码和数量清楚的合同、出货或小票。",
            )
        )

    receipt_pass, receipt_basis = _receipt_execution(receipts, contract)
    control("receipt_execution", receipt_pass, receipt_basis)
    if not receipt_pass:
        issues.append(
            _issue(
                "receipt_execution_failed",
                "门店小票不能证明合同买赠执行",
                _source_names(receipts),
                receipt_basis,
                "每张有效小票应在合同期内，显示合格购买商品、零金额赠品及符合合同的买赠数量比例。",
                "不能确认消费者端实际按合同执行额外搭赠。",
                "补交日期、购买商品、赠品、数量和零金额均清楚且比例正确的门店小票。",
            )
        )

    photo_problems: list[str] = []
    if not photos:
        photo_problems.append("未提交活动现场照片")
    elif contract is None:
        photo_problems.append("缺少合同活动期，无法核对照片")
    else:
        start = _date_value(contract.get("activity_start"))
        end = _date_value(contract.get("activity_end"))
        for photo in photos:
            source = str(photo.get("source_file") or "")
            photo_date = _date_value(photo.get("activity_date"))
            if photo_date is None or start is None or end is None or not start <= photo_date <= end:
                photo_problems.append(f"{source}缺少活动期内可见日期")
            if not photo.get("activity_location"):
                photo_problems.append(f"{source}未识别活动地点")
            if not photo.get("activity_content") or photo.get("promotion_visible") != "visible":
                photo_problems.append(f"{source}未清楚显示参与商品和额外搭赠活动")
    photo_pass = not photo_problems
    control("activity_execution", photo_pass, f"活动照片{len(photos)}份" if photo_pass else "；".join(photo_problems))
    if not photo_pass:
        issues.append(
            _issue(
                "activity_execution_missing",
                "缺少可核验的额外搭赠活动照片",
                _source_names(photos),
                "；".join(photo_problems),
                "活动照片应在合同期内清楚显示门店或地点、参与商品和额外搭赠执行。",
                "门店小票不能替代现场活动照片，促销执行证据不完整。",
                "补交活动期内门店、商品和额外搭赠内容清楚的现场照片。",
            )
        )

    hash_sources: dict[str, list[str]] = {}
    for path_value in case.get("visual_files") or []:
        path = Path(path_value)
        hash_sources.setdefault(sha256_file(path), []).append(path.name)
    duplicate_groups = [names for names in hash_sources.values() if len(names) > 1]
    duplicate_pass = not duplicate_groups
    duplicate_basis = (
        "未发现完全相同图片"
        if duplicate_pass
        else "；".join("、".join(names) + "内容完全相同" for names in duplicate_groups)
    )
    control("duplicate_evidence", duplicate_pass, duplicate_basis)
    if not duplicate_pass:
        issues.append(
            _issue(
                "duplicate_evidence_found",
                "发现完全相同的重复证据图片",
                [name for group in duplicate_groups for name in group],
                duplicate_basis,
                "合同、结算、出货、小票和活动照片应分别由其原始证据证明，重复图片不能充当多个角色。",
                "证据独立性不足，不能重复计算或重复证明执行。",
                "删除重复副本并补交各业务角色对应的原始材料。",
            )
        )

    claim_amount = (
        _decimal(settlement.get("claimed_gift_amount"), label="额外搭赠申报金额")
        if settlement is not None
        else None
    ) or Decimal("0.00")
    contract_recalculated: Decimal | None = None
    settlement_recalculated: Decimal | None = None
    contract_basis = "合同缺失"
    settlement_basis = "结算缺失"
    contract_budget: Decimal | None = None
    if contract is not None:
        contract_recalculated, contract_basis = _gift_plan_amount(contract)
        contract_budget = _decimal(contract.get("contract_budget"), label="合同预算")
    if settlement is not None:
        settlement_recalculated, settlement_basis = _gift_plan_amount(settlement)
    amount_pass = bool(
        contract_recalculated is not None
        and settlement_recalculated is not None
        and contract_budget is not None
        and _same_money(contract_recalculated, settlement_recalculated)
        and _same_money(contract_recalculated, contract_budget)
        and _same_money(contract_recalculated, claim_amount)
    )
    amount_basis = (
        f"合同复算={contract_recalculated}（{contract_basis}）；"
        f"合同预算={contract_budget}；结算复算={settlement_recalculated}（{settlement_basis}）；"
        f"额外搭赠申报={claim_amount}；"
        f"正常出货={settlement.get('shipment_amount') if settlement else '缺失'}"
    )
    control("amount_recalculation", amount_pass, amount_basis)
    if not amount_pass:
        issues.append(
            _issue(
                "amount_recalculation_failed",
                "额外搭赠申报金额无法完整复算一致",
                _source_names([item for item in (contract, settlement) if item]),
                amount_basis,
                "赠品数量×明确单价或逐项小计应同时等于签章合同预算和盖章结算的额外搭赠申报金额。",
                "额外搭赠金额缺少可重复计算的权威链路，正常出货金额不能替代。",
                "补正合同或结算中的赠品商品、数量、单价、小计和申报金额，使全链路精确一致。",
            )
        )

    # An ambiguous principal document blocks comparisons that depend on it.
    # Their customer-facing cause is the already reported source ambiguity,
    # rather than fictitious missing documents or invented numeric mismatches.
    dependent_issues = {
        "promotional_contract": "promotional_contract_invalid",
        "settlement": "settlement_invalid",
        "sales_delivery_statement": "sales_delivery_invalid",
        "party_alignment": "party_alignment_failed",
        "period_alignment": "period_alignment_failed",
        "shipment_reconciliation": "shipment_reconciliation_failed",
        "product_correspondence": "product_correspondence_failed",
        "receipt_execution": "receipt_execution_failed",
        "activity_execution": "activity_execution_missing",
        "amount_recalculation": "amount_recalculation_failed",
    }
    ambiguous_dependencies: dict[str, list[str]] = {}
    for candidates, label, dependent_controls in (
        (contracts, "促销合同", (
            "promotional_contract", "party_alignment", "period_alignment",
            "product_correspondence", "receipt_execution", "amount_recalculation",
            *(("activity_execution",) if photos else ()),
        )),
        (settlements, "结算单", (
            "settlement", "party_alignment", "period_alignment",
            "shipment_reconciliation", "product_correspondence", "amount_recalculation",
        )),
        (deliveries, "系统销售或出货明细", (
            "sales_delivery_statement", "party_alignment", "period_alignment",
            "shipment_reconciliation", "product_correspondence",
        )),
    ):
        if len(candidates) > 1:
            for control_id in dependent_controls:
                ambiguous_dependencies.setdefault(control_id, []).append(
                    f"{label}{len(candidates)}份（{'、'.join(_source_names(candidates))}）"
                )
    for item in controls:
        reasons = ambiguous_dependencies.get(item["control_id"])
        if reasons:
            item["status"] = "fail"
            item["basis"] = "存在多份主材料候选，尚未选定核销依据：" + "；".join(reasons)
    suppressed_codes = {
        dependent_issues[control_id] for control_id in ambiguous_dependencies
    }
    issues = [issue for issue in issues if issue["code"] not in suppressed_codes]

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
        "scenario": "giveaway_promotion",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": "额外搭赠核销",
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
            "receipt_count": len(receipts),
            "activity_photo_count": len(photos),
            "decision_label": decision_label,
            "shipment_amount": json_number(
                _decimal(settlement.get("shipment_amount"), label="正常出货金额")
            ) if settlement is not None and settlement.get("shipment_amount") is not None else None,
            "recalculated_gift_amount": json_number(contract_recalculated)
            if contract_recalculated is not None else None,
        },
        "giveaway_promotion_audit": {
            "documents": documents,
            "missing_roles": missing_roles,
            "controls": controls,
            "excluded_files": [Path(path).name for path in case.get("excluded_files") or []],
            "issues": issues,
        },
        "exceptions": exceptions,
    }
