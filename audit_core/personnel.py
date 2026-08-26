from __future__ import annotations

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
    unique_by,
)
from .excel_sources import image_file_inventory, read_personnel_sales
from .product_rag import (
    canonical_product_name,
    catalog_product_name_values,
    ean13_is_valid,
    load_product_rag,
    unique_fuzzy_catalog_product,
)


PRODUCT_KNOWLEDGE_SKILL_DIR = (
    Path(__file__).resolve().parents[1] / "skills" / "audit-promotional-display"
)
PASS_PERSONNEL_KNOWLEDGE_MATCHES = {"matched", "fuzzy_matched"}


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


def _product_identity(value: Any) -> str:
    text = normalize_text(value)
    for token in ("参半", "oralshark", "牙膏", "套盒", "特享装", "超值装", "量贩装"):
        text = text.replace(token, "")
    return re.sub(r"\d+(?:g|ml)", "", text)


def _product_similarity(left: Any, right: Any) -> float:
    left_text = _product_identity(left)
    right_text = _product_identity(right)
    if not left_text or not right_text:
        return 0.0
    if left_text in right_text or right_text in left_text:
        return 1.0

    def grams(value: str) -> set[str]:
        if len(value) == 1:
            return {value}
        return {value[index : index + 2] for index in range(len(value) - 1)}

    left_grams = grams(left_text)
    right_grams = grams(right_text)
    overlap = len(left_grams & right_grams)
    return overlap / max(1, len(left_grams | right_grams))


def _personnel_sales_knowledge_reconciliation(
    sales_skus: list[dict[str, Any]],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    """Resolve personnel Excel SKUs by exact 69 code and unique compatible name."""

    products = list(catalog.get("products") or [])
    reconciled: list[dict[str, Any]] = []
    for sku in sales_skus:
        source_barcode = str(sku.get("barcode") or "").strip()
        source_name = str(sku.get("product_name") or "").strip()
        excel_rows = [int(value) for value in sku.get("excel_rows") or []]
        row_text = "、".join(str(value) for value in excel_rows) or "未识别"
        barcode_valid = bool(source_barcode) and ean13_is_valid(source_barcode)
        barcode_candidates = (
            [
                product
                for product in products
                if str(product.get("barcode_69") or "") == source_barcode
            ]
            if barcode_valid
            else []
        )
        source_canonical = canonical_product_name(source_name)
        exact_name_candidates = [
            product
            for product in barcode_candidates
            if source_canonical
            and source_canonical
            in {
                canonical_product_name(value)
                for value in catalog_product_name_values(product)
            }
        ]

        selected: dict[str, Any] | None = None
        name_match_type = "unmatched"
        name_similarity: float | None = None
        if not source_barcode or not source_name:
            status = "conflict"
        elif not barcode_valid:
            status = "conflict"
        elif not barcode_candidates:
            status = "unmatched"
        elif len(exact_name_candidates) == 1:
            selected = exact_name_candidates[0]
            name_match_type = "exact"
            name_similarity = 1.0
            status = "matched"
        elif len(exact_name_candidates) > 1:
            status = "ambiguous"
        else:
            selected, score = unique_fuzzy_catalog_product(
                source_name,
                barcode_candidates,
                minimum_score=0.45 if len(barcode_candidates) > 1 else 0.55,
            )
            name_similarity = round(score, 3) if score else None
            if selected is not None:
                name_match_type = "fuzzy"
                status = "fuzzy_matched"
            elif len(barcode_candidates) > 1:
                status = "ambiguous"
            else:
                status = "conflict"

        diagnostic = selected
        if diagnostic is None and len(barcode_candidates) == 1:
            diagnostic = barcode_candidates[0]
        candidate_ids = sorted(
            str(product["product_id"]) for product in barcode_candidates
        )
        if status == "matched":
            assert selected is not None
            basis = (
                f"销售Excel第{row_text}行的69码{source_barcode}与商品知识库精确一致；"
                f"产品名与知识库商品{selected['product_code']}精确一致。"
            )
            resubmission = None
        elif status == "fuzzy_matched":
            assert selected is not None
            basis = (
                f"销售Excel第{row_text}行的69码{source_barcode}与商品知识库精确一致；"
                f"产品名在同码候选内唯一模糊对应知识库商品{selected['product_code']}"
                f"（相似度{name_similarity:.3f}）。"
            )
            resubmission = None
        elif not source_barcode or not source_name:
            missing = []
            if not source_barcode:
                missing.append("69码")
            if not source_name:
                missing.append("产品名")
            basis = f"销售Excel第{row_text}行缺少{'、'.join(missing)}，无法核对商品知识库。"
            resubmission = (
                f"重新提交销售Excel：补全第{row_text}行的{'、'.join(missing)}。"
            )
        elif not barcode_valid:
            basis = (
                f"销售Excel第{row_text}行的69码{source_barcode}未通过EAN-13校验，"
                "不能与商品知识库精确匹配。"
            )
            resubmission = (
                f"重新提交销售Excel：更正第{row_text}行69码为有效的13位商品码。"
            )
        elif not barcode_candidates:
            basis = (
                f"销售Excel第{row_text}行的69码{source_barcode}在正式商品知识库中不存在。"
            )
            resubmission = (
                f"核对销售Excel第{row_text}行69码；若69码正确，"
                "先补齐该商品的正式知识库资料后重跑。"
            )
        elif status == "ambiguous":
            basis = (
                f"销售Excel第{row_text}行的69码{source_barcode}精确命中多个知识库商品，"
                "产品名未能在同码候选内唯一消歧。"
            )
            resubmission = (
                f"重新提交销售Excel：补全第{row_text}行产品名的规格、香型或版本，"
                "使其能唯一对应商品知识库。"
            )
        else:
            expected = (
                f"{diagnostic['product_code']} / {diagnostic['product_name']}"
                if diagnostic is not None
                else "同码知识库商品"
            )
            basis = (
                f"销售Excel第{row_text}行的69码{source_barcode}与知识库精确一致，"
                f"但产品名不能模糊对应{expected}。"
            )
            resubmission = (
                f"重新提交销售Excel：核对并更正第{row_text}行产品名；"
                "若Excel名称正确，则先更正商品知识库名称后重跑。"
            )

        reconciled.append(
            {
                "source_barcode_69": source_barcode,
                "source_product_name": source_name,
                "source_quantity": sku.get("quantity"),
                "excel_rows": excel_rows,
                "knowledge_status": status,
                "knowledge_product_id": (
                    str(diagnostic["product_id"]) if diagnostic is not None else None
                ),
                "knowledge_product_code": (
                    str(diagnostic["product_code"]) if diagnostic is not None else None
                ),
                "knowledge_product_name": (
                    str(diagnostic["product_name"]) if diagnostic is not None else None
                ),
                "knowledge_barcode_69": (
                    str(diagnostic["barcode_69"]) if diagnostic is not None else None
                ),
                "barcode_match": (
                    "missing"
                    if not source_barcode
                    else "invalid"
                    if not barcode_valid
                    else "exact"
                    if barcode_candidates
                    else "not_found"
                ),
                "name_match_type": name_match_type,
                "name_similarity": name_similarity,
                "candidate_product_ids": candidate_ids,
                "basis": basis,
                "resubmission": resubmission,
            }
        )

    matched_count = sum(item["knowledge_status"] == "matched" for item in reconciled)
    fuzzy_count = sum(
        item["knowledge_status"] == "fuzzy_matched" for item in reconciled
    )
    problem_records = [
        item
        for item in reconciled
        if item["knowledge_status"] not in PASS_PERSONNEL_KNOWLEDGE_MATCHES
    ]
    return {
        "status": "pass" if not problem_records else "fail",
        "sku_count": len(reconciled),
        "matched_count": matched_count,
        "fuzzy_count": fuzzy_count,
        "problem_count": len(problem_records),
        "problem_barcodes": [item["source_barcode_69"] for item in problem_records],
        "records": reconciled,
        "basis": (
            f"{matched_count + fuzzy_count}/{len(reconciled)}个销售商品通过知识库核验"
            f"（产品名精确{matched_count}个、模糊{fuzzy_count}个）"
            + (
                "；全部69码均精确匹配"
                if not problem_records
                else f"；问题69码：{[item['source_barcode_69'] for item in problem_records]}"
            )
        ),
    }


def _sales_sku_product_similarity(settlement_name: Any, sku: dict[str, Any]) -> float:
    names = [sku.get("product_name")]
    knowledge = sku.get("knowledge_match") or {}
    if knowledge.get("knowledge_status") in PASS_PERSONNEL_KNOWLEDGE_MATCHES:
        names.append(knowledge.get("knowledge_product_name"))
    return max(
        (_product_similarity(settlement_name, name) for name in names if name),
        default=0.0,
    )


def _map_settlement_lines(
    lines: list[dict[str, Any]],
    sales_skus: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Map visual settlement lines to code-read sales SKUs without AI Excel access."""

    completed = [
        {
            **line,
            "notes": list(line.get("notes") or []),
        }
        for line in lines
    ]
    sku_by_barcode = {str(item["barcode"]): item for item in sales_skus}
    used: set[str] = set()
    for line in completed:
        visible_barcode = str(line.get("barcode_visible") or "").strip()
        if visible_barcode:
            sku = sku_by_barcode.get(visible_barcode)
            line["sales_match"] = {
                "barcode": visible_barcode,
                "excel_product_name": sku["product_name"] if sku else None,
                "confidence": "high" if sku else "ambiguous",
                "basis": "结算单可见条码与代码读取的销售Excel条码直接对应。",
            }
            if sku:
                used.add(visible_barcode)
            continue

        quantity = decimal_value(
            line["quantity"], label=f"settlement line {line.get('line_no')} quantity"
        )
        available = [sku for sku in sales_skus if str(sku["barcode"]) not in used]
        scored = sorted(
            (
                (
                    _sales_sku_product_similarity(line["product_name"], sku),
                    decimal_value(sku["quantity"], label="Excel SKU quantity") == quantity,
                    sku,
                )
                for sku in available
            ),
            key=lambda item: (item[1], item[0]),
            reverse=True,
        )
        quantity_candidates = [item for item in scored if item[1] and item[0] >= 0.08]
        candidate: dict[str, Any] | None = None
        score = 0.0
        if len(quantity_candidates) == 1:
            score, _, candidate = quantity_candidates[0]
        elif scored:
            best = scored[0]
            runner_up = scored[1][0] if len(scored) > 1 else 0.0
            if best[0] >= 0.35 and best[0] - runner_up >= 0.08:
                score, _, candidate = best

        if candidate is None:
            line["sales_match"] = {
                "barcode": None,
                "excel_product_name": None,
                "confidence": "ambiguous",
                "basis": "代码未找到同时满足商品文字相似、一行一条码唯一性的可靠销售SKU。",
            }
            continue
        barcode = str(candidate["barcode"])
        used.add(barcode)
        line["sales_match"] = {
            "barcode": barcode,
            "excel_product_name": candidate["product_name"],
            "confidence": "medium",
            "basis": (
                "确定性映射：结算商品文字与代码读取的Excel/知识库商品文字相似，"
                f"相似度={score:.3f}；结算数量与Excel汇总数量"
                f"{'一致' if decimal_value(candidate['quantity'], label='Excel quantity') == quantity else '不一致'}；"
                "且满足剩余一行一条码唯一约束。因结算单未显示条码，商品身份不标记为已验证。"
            ),
        }
        line["notes"].append(
            f"代码映射到Excel条码{barcode}；原始结算单未显示该条码。"
        )

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
        "knowledge_status": "unmatched",
        "knowledge_product_id": None,
        "knowledge_product_code": None,
        "knowledge_product_name": None,
        "knowledge_barcode_69": None,
        "knowledge_barcode_match": "not_found",
        "knowledge_name_match_type": "unmatched",
        "knowledge_name_similarity": None,
        "knowledge_basis": "尚未找到可核验的销售Excel商品。",
        "knowledge_resubmission": None,
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
    knowledge = dict(sku.get("knowledge_match") or {})
    knowledge_status = str(knowledge.get("knowledge_status") or "unmatched")
    knowledge_pass = knowledge_status in PASS_PERSONNEL_KNOWLEDGE_MATCHES
    excel_quantity = decimal_value(sku["quantity"], label=f"Excel SKU {barcode} quantity")
    difference = excel_quantity - quantity
    supported_quantity = max(Decimal("0"), min(excel_quantity, quantity))
    supported_reward = (supported_quantity * unit_reward).quantize(Decimal("0.01"))
    if not knowledge_pass:
        supported_reward = Decimal("0")
    result.update(
        {
            "excel_product_name": sku["product_name"],
            "excel_quantity": json_number(excel_quantity),
            "quantity_difference": json_number(difference),
            "excel_rows": sku["excel_rows"],
            "store_quantities": sku["store_quantities"],
            "knowledge_status": knowledge_status,
            "knowledge_product_id": knowledge.get("knowledge_product_id"),
            "knowledge_product_code": knowledge.get("knowledge_product_code"),
            "knowledge_product_name": knowledge.get("knowledge_product_name"),
            "knowledge_barcode_69": knowledge.get("knowledge_barcode_69"),
            "knowledge_barcode_match": knowledge.get("barcode_match") or "not_found",
            "knowledge_name_match_type": knowledge.get("name_match_type") or "unmatched",
            "knowledge_name_similarity": knowledge.get("name_similarity"),
            "knowledge_basis": knowledge.get("basis") or "销售商品未通过知识库核验。",
            "knowledge_resubmission": knowledge.get("resubmission"),
            "quantity_status": "match" if difference == 0 else "mismatch",
            "mapping_status": "matched",
            "supported_quantity": json_number(supported_quantity),
            "supported_reward_amount": json_number(supported_reward),
        }
    )
    if not knowledge_pass:
        exceptions.append(
            exception(
                "high",
                "SALES_PRODUCT_KNOWLEDGE_MISMATCH",
                f"销售Excel条码{barcode}未通过商品知识库核验：{result['knowledge_basis']}",
                "该商品的销售数量不能进入人员激励支持金额。",
                str(result.get("knowledge_resubmission") or "核对销售Excel与商品知识库后重跑。"),
                source=f"Excel rows {','.join(str(row) for row in sku['excel_rows'])}",
            )
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
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    sales_path = Path(case["sales_excel"]).resolve()
    sales = read_personnel_sales(sales_path)
    product_catalog = load_product_rag(PRODUCT_KNOWLEDGE_SKILL_DIR)
    sales_knowledge = _personnel_sales_knowledge_reconciliation(
        sales["skus"], product_catalog
    )
    knowledge_by_barcode = {
        str(item["source_barcode_69"]): item
        for item in sales_knowledge["records"]
    }
    for sku in sales["skus"]:
        sku["knowledge_match"] = knowledge_by_barcode[str(sku["barcode"])]
    sales.update(
        {
            "knowledge_status": sales_knowledge["status"],
            "knowledge_matched_count": sales_knowledge["matched_count"],
            "knowledge_fuzzy_count": sales_knowledge["fuzzy_count"],
            "knowledge_problem_count": sales_knowledge["problem_count"],
            "knowledge_problem_barcodes": sales_knowledge["problem_barcodes"],
            "knowledge_basis": sales_knowledge["basis"],
            "knowledge_reconciliation": sales_knowledge["records"],
        }
    )
    source_images = [
        Path(case["settlement_image"]).resolve(),
        *[Path(value).resolve() for value in case["transfer_images"]],
    ]
    image_files = image_file_inventory(source_images) if source_images else None
    settlement = evidence["settlement"]
    lines = sorted(settlement["lines"], key=lambda item: int(item["line_no"]))
    unique_by(lines, "line_no", "settlement line number")
    lines = _map_settlement_lines(lines, sales["skus"])
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
        knowledge = knowledge_by_barcode.get(str(record["barcode"])) or {}
        trace.append(
            {
                **record,
                "knowledge_status": knowledge.get("knowledge_status"),
                "knowledge_product_id": knowledge.get("knowledge_product_id"),
                "knowledge_product_code": knowledge.get("knowledge_product_code"),
                "knowledge_product_name": knowledge.get("knowledge_product_name"),
                "knowledge_barcode_69": knowledge.get("knowledge_barcode_69"),
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
        "schema_version": "2.1",
        "generated_at": now_utc(),
        "scenario": "personnel_incentive",
        "case_id": str(case.get("case_id") or Path(case["source_archive"]).stem),
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
            "sales_knowledge_matched_count": sales_knowledge["matched_count"],
            "sales_knowledge_fuzzy_count": sales_knowledge["fuzzy_count"],
            "sales_knowledge_problem_count": sales_knowledge["problem_count"],
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
