from __future__ import annotations

from copy import deepcopy
import re
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import (
    AuditError,
    decimal_value,
    exception,
    json_number,
    money,
    normalize_text,
    now_utc,
    resolve_case_file,
    resolve_case_files,
    unique_by,
)
from .excel_sources import image_file_inventory, read_personnel_sales


def _period_matches(period_values: list[str], start_text: str, end_text: str) -> bool:
    start = date.fromisoformat(start_text)
    end = date.fromisoformat(end_text)
    for raw in period_values:
        numbers = [int(value) for value in re.findall(r"\d+", raw)]
        if len(numbers) >= 6:
            candidate = numbers[:6]
            if candidate == [start.year, start.month, start.day, end.year, end.month, end.day]:
                return True
        if len(numbers) >= 4:
            candidate = numbers[-4:]
            if candidate == [start.month, start.day, end.month, end.day]:
                return True
    return False


def _complete_residual_sales_matches(
    lines: list[dict[str, Any]],
    sales_skus: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Complete cautious model mappings with an auditable one-to-one rule.

    Settlement images sometimes use an internal alias such as ``SP-4`` while the
    sales workbook only carries the consumer-facing product name and barcode.
    A missing barcode is filled only when all three controls agree:

    * the remaining settlement product core is contained in the Excel name;
    * the settlement quantity equals the deterministic Excel SKU aggregate; and
    * the candidate barcode is unique across all unresolved settlement lines.

    The completed mapping remains medium-confidence because the source settlement
    still does not print the barcode.  This lets the report perform the requested
    line-by-line quantity comparison without hiding the identity limitation.
    """

    completed = deepcopy(lines)
    sku_by_barcode = {str(item["barcode"]): item for item in sales_skus}
    already_used = {
        str((line.get("sales_match") or {}).get("barcode"))
        for line in completed
        if (line.get("sales_match") or {}).get("barcode")
        and str((line.get("sales_match") or {}).get("barcode")) in sku_by_barcode
    }
    proposals: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    candidate_frequency: dict[str, int] = defaultdict(int)

    for line in completed:
        match = line.setdefault("sales_match", {})
        if str(match.get("barcode") or "").strip():
            continue
        normalized_name = normalize_text(line.get("product_name"))
        product_core = re.sub(r"^sp\d+", "", normalized_name)
        if len(product_core) < 4 or product_core in {"牙膏", "商品", "产品"}:
            continue
        settlement_quantity = decimal_value(
            line.get("quantity"), label=f"settlement line {line.get('line_no')} quantity"
        )
        candidates = [
            sku
            for barcode, sku in sku_by_barcode.items()
            if barcode not in already_used
            and product_core in normalize_text(sku.get("product_name"))
            and decimal_value(sku.get("quantity"), label=f"Excel SKU {barcode} quantity")
            == settlement_quantity
        ]
        if len(candidates) != 1:
            continue
        candidate = candidates[0]
        barcode = str(candidate["barcode"])
        candidate_frequency[barcode] += 1
        proposals.append((line, candidate, product_core))

    for line, candidate, product_core in proposals:
        barcode = str(candidate["barcode"])
        if candidate_frequency[barcode] != 1:
            continue
        match = line["sales_match"]
        quantity = candidate["quantity"]
        match.update(
            {
                "barcode": barcode,
                "excel_product_name": candidate["product_name"],
                "confidence": "medium",
                "basis": (
                    f"确定性补全：去除结算别名后商品核心词“{product_core}”包含于销售Excel商品名；"
                    f"结算数量与该条码Excel逐行汇总数量均为{quantity}；且在剩余结算行与剩余销售SKU中"
                    "满足一行一条码唯一约束。结算单未印条码，因此身份置信度保留为中等。"
                ),
            }
        )
        line.setdefault("notes", []).append(
            f"系统按商品核心词、数量相等和剩余一对一唯一性补全条码{barcode}；原始结算单未印条码。"
        )
        already_used.add(barcode)

    return completed


def _line_result(
    line: dict[str, Any],
    sku_map: dict[str, dict[str, Any]],
    used_barcodes: set[str],
    exceptions: list[dict[str, Any]],
) -> tuple[dict[str, Any], Decimal]:
    line_no = int(line["line_no"])
    quantity = decimal_value(line["quantity"], label=f"settlement line {line_no} quantity")
    unit_reward = money(line["unit_reward"], label=f"settlement line {line_no} unit reward")
    reward_amount = money(line["reward_amount"], label=f"settlement line {line_no} reward amount")
    calculated_reward = (quantity * unit_reward).quantize(Decimal("0.01"))
    line_amount_difference = reward_amount - calculated_reward
    match = line["sales_match"]
    barcode = str(match.get("barcode") or "").strip()
    result: dict[str, Any] = {
        "line_no": line_no,
        "settlement_product_name": line["product_name"],
        "mapped_barcode": barcode or None,
        "mapping_basis": match.get("basis"),
        "mapping_confidence": match.get("confidence"),
        "settlement_quantity": json_number(quantity),
        "unit_reward": json_number(unit_reward),
        "settlement_reward_amount": json_number(reward_amount),
        "calculated_settlement_reward": json_number(calculated_reward),
        "settlement_line_amount_difference": json_number(line_amount_difference),
        "excel_product_name": None,
        "excel_quantity": None,
        "quantity_difference": None,
        "excel_rows": [],
        "store_quantities": [],
        "quantity_status": "unmatched",
        "amount_status": "match" if line_amount_difference == 0 else "mismatch",
        "mapping_status": "unmatched",
        "supported_quantity": 0,
        "supported_reward_amount": 0,
        "notes": list(line.get("notes") or []),
    }
    if line_amount_difference != 0:
        exceptions.append(
            exception(
                "high",
                "SETTLEMENT_LINE_AMOUNT_MISMATCH",
                f"结算单第{line_no}行奖励金额与数量×单价不一致，差额{line_amount_difference}元。",
                "该行结算金额不能直接作为可靠核销依据。",
                "更正结算单行金额或提供书面计算口径。",
                source=f"settlement line {line_no}",
            )
        )
    if not barcode:
        exceptions.append(
            exception(
                "high",
                "SETTLEMENT_SKU_UNMAPPED",
                f"结算单第{line_no}行“{line['product_name']}”未映射到销售Excel条码。",
                "无法执行逐SKU销售数量核对。",
                "补充条码或由业务人员确认唯一商品映射。",
                source=f"settlement line {line_no}",
            )
        )
        return result, Decimal("0")
    if barcode in used_barcodes:
        result["mapping_status"] = "duplicate_barcode"
        exceptions.append(
            exception(
                "high",
                "DUPLICATE_SETTLEMENT_SKU_MAPPING",
                f"销售条码{barcode}被多个结算行重复使用。",
                "逐项1:1映射失效，可能重复核销。",
                "拆分并确认每个结算行唯一对应的销售SKU。",
                source=f"settlement line {line_no}",
            )
        )
        return result, Decimal("0")
    sku = sku_map.get(barcode)
    if sku is None:
        result["mapping_status"] = "barcode_not_found"
        exceptions.append(
            exception(
                "high",
                "MAPPED_BARCODE_NOT_IN_EXCEL",
                f"结算单第{line_no}行映射条码{barcode}在销售Excel中不存在。",
                "该结算数量没有销售明细支持。",
                "更正条码映射或补交完整销售明细。",
                source=f"settlement line {line_no}",
            )
        )
        return result, Decimal("0")

    used_barcodes.add(barcode)
    excel_quantity = decimal_value(sku["quantity"], label=f"Excel SKU {barcode} quantity")
    difference = excel_quantity - quantity
    supported_quantity = max(Decimal("0"), min(excel_quantity, quantity))
    supported_reward = (supported_quantity * unit_reward).quantize(Decimal("0.01"))
    result.update(
        {
            "excel_product_name": sku["product_name"],
            "excel_quantity": json_number(excel_quantity),
            "quantity_difference": json_number(difference),
            "excel_rows": sku["excel_rows"],
            "store_quantities": sku["store_quantities"],
            "quantity_status": "match" if difference == 0 else "mismatch",
            "mapping_status": "matched",
            "supported_quantity": json_number(supported_quantity),
            "supported_reward_amount": json_number(supported_reward),
        }
    )
    expected_name = match.get("excel_product_name")
    if expected_name and normalize_text(expected_name) != normalize_text(sku["product_name"]):
        exceptions.append(
            exception(
                "medium",
                "MAPPING_PRODUCT_NAME_CONFLICT",
                f"结算单第{line_no}行声明的Excel商品名与条码实际商品名不一致。",
                "商品映射需要人工确认。",
                "以条码主数据复核商品名称后重新出具映射。",
                source=f"settlement line {line_no}",
            )
        )
    if match.get("confidence") != "high":
        exceptions.append(
            exception(
                "medium",
                "NON_HIGH_CONFIDENCE_SKU_MAPPING",
                f"结算单第{line_no}行SKU映射置信度为{match.get('confidence')}。",
                "逐项数量虽然可计算，但商品身份仍有不确定性。",
                "补充结算单条码或商品主数据映射。",
                source=f"settlement line {line_no}",
            )
        )
    if difference != 0:
        exceptions.append(
            exception(
                "high",
                "SKU_QUANTITY_MISMATCH",
                f"结算单第{line_no}行“{line['product_name']}”销售数量与Excel相差{difference}。",
                "该SKU仅能按较低的已支持数量计算核销。",
                "核对Excel来源行与结算单数量并更正差异。",
                source=f"Excel rows {','.join(str(row) for row in sku['excel_rows'])}",
            )
        )
    return result, supported_reward


def _transfer_reconciliation(
    sales: dict[str, Any],
    sku_lines: list[dict[str, Any]],
    transfers: list[dict[str, Any]],
    exceptions: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], Decimal]:
    units_by_barcode = {
        item["mapped_barcode"]: money(item["unit_reward"])
        for item in sku_lines
        if item["mapping_status"] == "matched" and item["mapped_barcode"]
    }
    expected: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    for record in sales["records"]:
        unit = units_by_barcode.get(record["barcode"])
        if unit is None:
            continue
        expected[record["store_name"]] += decimal_value(record["quantity"], label="store quantity") * unit
    expected = {name: value.quantize(Decimal("0.01")) for name, value in expected.items()}
    transfer_map = unique_by(transfers, "transfer_id", "transfer_id")
    transfer_total = sum((money(item["amount"]) for item in transfer_map.values()), Decimal("0"))
    assigned_stores: set[str] = set()
    assigned_transfers: set[str] = set()
    rows: list[dict[str, Any]] = []
    row_by_store: dict[str, dict[str, Any]] = {}
    for store, amount in expected.items():
        row = {
            "store_name": store,
            "excel_quantity": next(item["quantity"] for item in sales["stores"] if item["store_name"] == store),
            "expected_reward_amount": json_number(amount),
            "transfer_id": None,
            "transfer_amount": None,
            "amount_difference": None,
            "match_basis": None,
            "identity_status": "unmatched",
            "status": "unmatched",
        }
        rows.append(row)
        row_by_store[normalize_text(store)] = row

    for transfer_id, item in transfer_map.items():
        store = str(item.get("store_name") or "").strip()
        if not item.get("identity_visible") or not store:
            continue
        row = row_by_store.get(normalize_text(store))
        if row is None or row["store_name"] in assigned_stores:
            continue
        amount = money(item["amount"])
        expected_amount = money(row["expected_reward_amount"])
        row.update(
            {
                "transfer_id": transfer_id,
                "transfer_amount": json_number(amount),
                "amount_difference": json_number(amount - expected_amount),
                "match_basis": "visible_store_identity",
                "identity_status": "verified",
                "status": "match" if amount == expected_amount else "mismatch",
            }
        )
        assigned_stores.add(row["store_name"])
        assigned_transfers.add(transfer_id)

    expected_groups: dict[Decimal, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        if row["store_name"] not in assigned_stores:
            expected_groups[money(row["expected_reward_amount"])].append(row)
    transfer_groups: dict[Decimal, list[tuple[str, dict[str, Any]]]] = defaultdict(list)
    for transfer_id, item in transfer_map.items():
        if transfer_id not in assigned_transfers:
            transfer_groups[money(item["amount"])].append((transfer_id, item))

    for amount, store_rows in expected_groups.items():
        vouchers = sorted(transfer_groups.get(amount, []), key=lambda pair: pair[0])
        sorted_rows = sorted(store_rows, key=lambda item: item["store_name"])
        for index, row in enumerate(sorted_rows):
            if index >= len(vouchers):
                continue
            transfer_id, _ = vouchers[index]
            ambiguous = len(sorted_rows) > 1 or len(vouchers) > 1
            row.update(
                {
                    "transfer_id": transfer_id,
                    "transfer_amount": json_number(amount),
                    "amount_difference": 0,
                    "match_basis": "amount_multiset",
                    "identity_status": "amount_only_ambiguous" if ambiguous else "amount_only_unverified",
                    "status": "amount_match_identity_unverified",
                }
            )
            assigned_transfers.add(transfer_id)

    hidden_identity = any(not item.get("identity_visible") for item in transfers)
    hidden_date = any(not item.get("event_date_visible") for item in transfers)
    if hidden_identity:
        exceptions.append(
            exception(
                "medium",
                "TRANSFER_STORE_IDENTITY_NOT_VISIBLE",
                "转账截图未显示可核验的门店/收款人身份，只能按金额集合勾稽。",
                "无法证明每笔款项实际支付给对应门店。",
                "补充显示收款人、门店映射和交易单号的转账详情。",
            )
        )
    if hidden_date:
        exceptions.append(
            exception(
                "medium",
                "TRANSFER_DATE_NOT_VISIBLE",
                "转账截图未显示完整交易日期。",
                "无法独立确认付款发生在活动结算周期内。",
                "补充带交易日期和交易单号的明细。",
            )
        )
    unmatched_stores = [row for row in rows if row["status"] == "unmatched"]
    unmatched_transfers = [item for key, item in transfer_map.items() if key not in assigned_transfers]
    if unmatched_stores or unmatched_transfers:
        exceptions.append(
            exception(
                "high",
                "TRANSFER_AMOUNT_MULTISET_MISMATCH",
                f"有{len(unmatched_stores)}家门店预期金额或{len(unmatched_transfers)}笔转账无法按金额集合匹配。",
                "付款证据不能完整覆盖门店应付金额。",
                "核对缺失、重复或金额错误的转账凭证。",
            )
        )
    rows.sort(key=lambda item: next(store["excel_rows"][0] for store in sales["stores"] if store["store_name"] == item["store_name"]))
    return rows, transfer_total


def audit_personnel_case(
    case_path: str | Path,
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    sales_path = resolve_case_file(case_path, case, "sales_excel")
    if sales_path is None:
        raise AuditError("Personnel case requires a sales Excel file")
    sales = read_personnel_sales(sales_path)
    source_images: list[Path] = []
    settlement_image = resolve_case_file(case_path, case, "settlement_image", required=False)
    if settlement_image is not None:
        source_images.append(settlement_image)
    source_images.extend(resolve_case_files(case_path, case, "transfer_images"))
    image_files = image_file_inventory(source_images) if source_images else None
    settlement = evidence["settlement"]
    lines = sorted(settlement["lines"], key=lambda item: int(item["line_no"]))
    unique_by(lines, "line_no", "settlement line number")
    lines = _complete_residual_sales_matches(lines, sales["skus"])
    sku_map = {item["barcode"]: item for item in sales["skus"]}
    exceptions: list[dict[str, Any]] = []
    used_barcodes: set[str] = set()
    sku_results: list[dict[str, Any]] = []
    quantity_supported_reward = Decimal("0")
    for line in lines:
        result, supported = _line_result(line, sku_map, used_barcodes, exceptions)
        sku_results.append(result)
        quantity_supported_reward += supported

    unmatched_sales_skus = [item for item in sales["skus"] if item["barcode"] not in used_barcodes]
    expect_all = bool((case.get("rules") or {}).get("expect_all_sales_skus_matched", True))
    if unmatched_sales_skus and expect_all:
        exceptions.append(
            exception(
                "high",
                "EXCEL_SKU_NOT_ON_SETTLEMENT",
                f"销售Excel有{len(unmatched_sales_skus)}个SKU未进入结算单逐项映射。",
                "Excel总销量与结算范围可能不一致。",
                "确认未结算SKU是否属于本活动；如属于，补充或更正结算单。",
                source=",".join(item["barcode"] for item in unmatched_sales_skus),
            )
        )

    line_quantity_total = sum((decimal_value(item["settlement_quantity"], label="settlement quantity") for item in sku_results), Decimal("0"))
    excel_mapped_total = sum((decimal_value(item["excel_quantity"], label="Excel quantity") for item in sku_results if item["excel_quantity"] is not None), Decimal("0"))
    line_reward_total = sum((money(item["settlement_reward_amount"]) for item in sku_results), Decimal("0"))
    calculated_line_reward_total = sum((money(item["calculated_settlement_reward"]) for item in sku_results), Decimal("0"))
    declared_quantity = decimal_value(settlement["declared_total_quantity"], label="declared total quantity")
    declared_reward = money(settlement["declared_total_reward"], label="declared total reward")
    claimed_amount = money(settlement["claimed_amount"], label="claimed amount")
    if declared_quantity != line_quantity_total:
        exceptions.append(
            exception(
                "high",
                "SETTLEMENT_QUANTITY_TOTAL_MISMATCH",
                f"结算单声明总数量{declared_quantity}与逐行合计{line_quantity_total}不一致。",
                "结算单内部数量不平。",
                "更正结算单总数量。",
            )
        )
    if declared_reward != line_reward_total:
        exceptions.append(
            exception(
                "high",
                "SETTLEMENT_REWARD_TOTAL_MISMATCH",
                f"结算单声明奖励合计{declared_reward}与逐行金额合计{line_reward_total}不一致。",
                "结算单内部金额不平。",
                "更正结算单奖励合计。",
            )
        )
    claim_difference = claimed_amount - line_reward_total
    if claim_difference != 0:
        exceptions.append(
            exception(
                "high" if claim_difference > 0 else "medium",
                "CLAIM_DIFFERS_FROM_SETTLEMENT_LINES",
                f"实际核销金额{claimed_amount}与结算单逐行合计{line_reward_total}相差{claim_difference}元。",
                "需要明确人工调整、舍尾或扣减口径。" if claim_difference < 0 else "实际申请超过结算明细支持金额。",
                "提供差额说明并在审批记录中保留调整依据。",
            )
        )
    if not _period_matches(sales["period_values"], settlement["activity_start"], settlement["activity_end"]):
        exceptions.append(
            exception(
                "high",
                "ACTIVITY_PERIOD_MISMATCH",
                "销售Excel期间与结算单活动期间无法确认一致。",
                "可能包含活动期外销售。",
                "补充标准日期字段或更正活动期间。",
            )
        )

    store_rows, transfer_total = _transfer_reconciliation(
        sales, sku_results, evidence.get("transfers") or [], exceptions
    )
    if transfer_total != line_reward_total:
        exceptions.append(
            exception(
                "high" if transfer_total < claimed_amount else "medium",
                "TRANSFER_TOTAL_DIFFERS_FROM_SETTLEMENT",
                f"去重转账合计{transfer_total}与结算单逐行合计{line_reward_total}相差{transfer_total - line_reward_total}元。",
                "付款证据与结算金额未完全勾稽。",
                "补充缺失凭证或解释多付/少付金额。",
            )
        )

    evidence_cap = quantity_supported_reward
    if evidence.get("transfers"):
        evidence_cap = min(evidence_cap, transfer_total)
    suggested = max(Decimal("0"), min(claimed_amount, evidence_cap))
    high_count = sum(item["severity"] == "high" for item in exceptions)
    medium_count = sum(item["severity"] == "medium" for item in exceptions)
    matched_count = sum(item["mapping_status"] == "matched" for item in sku_results)
    quantity_match_count = sum(item["quantity_status"] == "match" for item in sku_results)
    if matched_count == 0:
        conclusion = "human_review"
    elif suggested < claimed_amount:
        conclusion = "partial_pass"
    elif high_count:
        conclusion = "human_review"
    elif medium_count:
        conclusion = "conditional_pass"
    else:
        conclusion = "pass"

    line_by_barcode = {
        item["mapped_barcode"]: item for item in sku_results if item["mapping_status"] == "matched"
    }
    trace: list[dict[str, Any]] = []
    for record in sales["records"]:
        matched = line_by_barcode.get(record["barcode"])
        trace.append(
            {
                **record,
                "settlement_line_no": matched["line_no"] if matched else None,
                "settlement_product_name": matched["settlement_product_name"] if matched else None,
                "unit_reward": matched["unit_reward"] if matched else None,
                "expected_reward_amount": (
                    json_number(decimal_value(record["quantity"], label="quantity") * money(matched["unit_reward"]))
                    if matched
                    else None
                ),
            }
        )

    return {
        "schema_version": "1.0",
        "generated_at": now_utc(),
        "scenario": "personnel_incentive",
        "case_id": str(case.get("case_id") or "personnel-incentive"),
        "case_name": str(case.get("case_name") or settlement.get("customer_name") or "人员激励"),
        "summary": {
            "conclusion": conclusion,
            "claimed_amount": json_number(claimed_amount),
            "settlement_line_quantity": json_number(line_quantity_total),
            "excel_mapped_quantity": json_number(excel_mapped_total),
            "quantity_difference": json_number(excel_mapped_total - line_quantity_total),
            "settlement_line_reward": json_number(line_reward_total),
            "calculated_line_reward": json_number(calculated_line_reward_total),
            "transfer_total": json_number(transfer_total),
            "quantity_supported_reward": json_number(quantity_supported_reward),
            "suggested_approved_amount": json_number(suggested),
            "temporarily_held_amount": json_number(claimed_amount - suggested),
            "settlement_line_count": len(sku_results),
            "matched_sku_count": matched_count,
            "quantity_match_count": quantity_match_count,
            "excel_detail_row_count": sales["detail_row_count"],
            "store_count": sales["store_count"],
            "high_exception_count": high_count,
            "medium_exception_count": medium_count,
        },
        "settlement": settlement,
        "sales": sales,
        "sku_reconciliation": sku_results,
        "sales_trace": trace,
        "store_transfer_reconciliation": store_rows,
        "transfer_evidence": evidence.get("transfers") or [],
        "image_inventory": image_files,
        "unmatched_sales_skus": unmatched_sales_skus,
        "exceptions": exceptions,
    }
