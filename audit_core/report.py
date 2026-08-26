from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

from .common import AuditError


NAVY = "17365D"
PALE_BLUE = "EAF3F8"
TOTAL_BLUE = "D9EAF7"
PASS_GREEN = "E2F0D9"
REVIEW_YELLOW = "FFF2CC"
BORDER_BLUE = "B7C9D6"
INPUT_BLUE = "0000FF"
FORMULA_ERRORS = {"#REF!", "#DIV/0!", "#VALUE!", "#N/A", "#NAME?"}


def _font(*, bold: bool = False, color: str = "000000", size: float = 10) -> Font:
    return Font(name="Microsoft YaHei", size=size, bold=bold, color=color)


def _border() -> Border:
    side = Side(style="thin", color=BORDER_BLUE)
    return Border(left=side, right=side, top=side, bottom=side)


def _number(value: Any) -> str:
    if value is None:
        return "未识别"
    if isinstance(value, bool):
        return "是" if value else "否"
    try:
        decimal = Decimal(str(value))
    except Exception:
        return str(value)
    if decimal == decimal.to_integral_value():
        return str(int(decimal))
    return format(decimal.normalize(), "f")


CONFIDENCE_PRESENTATION = {
    "exact": "高",
    "fuzzy": "中",
    "unmatched": "低",
}


def _match_confidence(level: str) -> str:
    return f"置信度：{CONFIDENCE_PRESENTATION[level]}"


def _style_title(ws: Any, title: str, note: str) -> None:
    ws.merge_cells("A1:F1")
    ws["A1"] = title
    ws["A1"].font = _font(bold=True, color="FFFFFF", size=16)
    ws["A1"].fill = PatternFill("solid", fgColor=NAVY)
    ws["A1"].alignment = Alignment(horizontal="left", vertical="center")
    ws.row_dimensions[1].height = 28

    ws.merge_cells("A2:F2")
    ws["A2"] = note
    ws["A2"].font = _font(color="666666", size=9)
    ws["A2"].fill = PatternFill("solid", fgColor=PALE_BLUE)
    ws["A2"].alignment = Alignment(vertical="center", wrap_text=True)
    ws.row_dimensions[2].height = 42


def _write_header(ws: Any, values: list[str], *, height: float) -> None:
    for column, value in enumerate(values, 1):
        cell = ws.cell(3, column, value)
        cell.font = _font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor=NAVY)
        cell.border = _border()
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.row_dimensions[3].height = height


def _status_fill(value: Any) -> PatternFill | None:
    text = str(value or "")
    if text in {
        "通过", "全部通过", "匹配", "三方金额匹配", "身份与日期已验证",
        "完全对应", "模糊对应", "精确匹配",
    }:
        return PatternFill("solid", fgColor=PASS_GREEN)
    if any(
        token in text
        for token in (
            "需", "未验证", "不一致", "部分", "人工", "未匹配",
            "模糊匹配", "完全不匹配",
        )
    ):
        return PatternFill("solid", fgColor=REVIEW_YELLOW)
    return None


def _write_row(
    ws: Any,
    row: int,
    values: list[Any],
    *,
    height: float,
    total: bool = False,
    font_size: float = 10,
) -> None:
    for column, value in enumerate(values, 1):
        cell = ws.cell(row, column, value)
        cell.border = _border()
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        cell.font = _font(
            bold=total,
            color="000000" if total else INPUT_BLUE,
            size=font_size,
        )
        if total:
            cell.fill = PatternFill("solid", fgColor=TOTAL_BLUE)
        elif column == 6:
            fill = _status_fill(value)
            if fill:
                cell.fill = fill
    ws.row_dimensions[row].height = height


def _sheet_base(ws: Any, *, zoom: int, tab_color: str) -> None:
    ws.sheet_view.showGridLines = False
    ws.sheet_view.zoomScale = zoom
    ws.sheet_properties.tabColor = tab_color
    ws.page_setup.orientation = "landscape"
    ws.page_setup.fitToHeight = 0
    ws.print_title_rows = "1:3"


PASS_PERSONNEL_KNOWLEDGE_MATCHES = {"matched", "fuzzy_matched"}


def _personnel_knowledge_match_text(item: dict[str, Any]) -> str:
    status = str(item.get("knowledge_status") or "unmatched")
    barcode = {
        "exact": "69码精确匹配",
        "invalid": "69码完全不匹配（格式无效）",
        "missing": "69码完全不匹配（缺失）",
        "not_found": "69码完全不匹配（知识库未登记）",
    }.get(str(item.get("knowledge_barcode_match") or "not_found"), "69码完全不匹配")
    name = {
        "exact": "产品名精确匹配",
        "fuzzy": "产品名模糊匹配",
        "unmatched": "产品名完全不匹配",
    }.get(str(item.get("knowledge_name_match_type") or "unmatched"), "产品名完全不匹配")
    similarity = item.get("knowledge_name_similarity")
    if name == "产品名模糊匹配" and similarity is not None:
        name += f"（相似度{float(similarity):.3f}）"
    level = "exact" if status == "matched" else "fuzzy" if status == "fuzzy_matched" else "unmatched"
    return f"{barcode}；{name}\n知识库结论：{_match_confidence(level)}"


def _personnel_sales_knowledge_text(item: dict[str, Any]) -> str:
    knowledge_identity = (
        f"{item.get('knowledge_product_code')} / {item.get('knowledge_product_name')} / "
        f"{item.get('knowledge_barcode_69')}"
        if item.get("knowledge_product_name")
        else "未唯一确定"
    )
    return (
        f"Excel商品：{item.get('excel_product_name') or '未匹配'}\n"
        f"条码：{item.get('mapped_barcode') or '未匹配'}；数量：{_number(item.get('excel_quantity'))}\n"
        f"知识库商品：{knowledge_identity}\n"
        f"{_personnel_knowledge_match_text(item)}\n"
        f"计算奖励：{_number(item['calculated_settlement_reward'])}元"
    )


def _personnel_row_product_name(item: dict[str, Any]) -> str:
    """Use the selected catalog name as the human-facing settlement-row title."""

    knowledge_name = str(item.get("knowledge_product_name") or "").strip()
    if knowledge_name:
        return knowledge_name
    return str(item.get("settlement_product_name") or "未识别商品").strip()


def _personnel_comparison_text(item: dict[str, Any]) -> str:
    mapping_level = "exact" if item.get("mapping_confidence") == "high" else (
        "fuzzy" if item.get("mapping_status") == "matched" else "unmatched"
    )
    return (
        "Excel ↔ 商品知识库\n"
        f"{_personnel_knowledge_match_text(item).splitlines()[0]}\n"
        "销售/知识库 ↔ 结算单\n"
        f"商品：{_match_confidence(mapping_level)}\n"
        f"数量差：{_number(item.get('quantity_difference'))}；"
        f"金额差：{_number(item['settlement_line_amount_difference'])}元"
    )


def _personnel_conclusion(item: dict[str, Any]) -> str:
    if item.get("mapping_status") != "matched":
        return (
            f"{_match_confidence('unmatched')}\n"
            "要重新提交：结算单商品这一行，拍清商品名称、条码和数量"
        )
    if item.get("knowledge_status") not in PASS_PERSONNEL_KNOWLEDGE_MATCHES:
        return (
            f"{_match_confidence('unmatched')}\n"
            f"要重新提交：{item.get('knowledge_resubmission') or '核对销售Excel与商品知识库后重跑'}"
        )
    if Decimal(str(item.get("quantity_difference") or 0)) != 0:
        return (
            f"{_match_confidence('unmatched')}\n"
            "要重新提交：更正后的销售Excel或结算单，让数量一致"
        )
    if Decimal(str(item.get("settlement_line_amount_difference") or 0)) != 0:
        return (
            f"{_match_confidence('unmatched')}\n"
            "要重新提交：更正后的结算单，让奖励金额和销售计算一致"
        )
    if item.get("mapping_confidence") == "low":
        return (
            f"{_match_confidence('unmatched')}\n"
            "要重新提交：补拍结算单商品这一行，让商品名称或条码更清楚"
        )
    if (
        item.get("mapping_confidence") == "medium"
        or item.get("knowledge_status") == "fuzzy_matched"
    ):
        return (
            f"{_match_confidence('fuzzy')}\n"
            "69码精确、商品名称唯一模糊匹配，数量和金额一致，已通过；"
            "要重新提交：不用"
        )
    return f"{_match_confidence('exact')}\n要重新提交：不用"


def _add_personnel_sheet(wb: Workbook, result: dict[str, Any]) -> Any:
    ws = wb.create_sheet("人员激励核销")
    summary = result["summary"]
    sales = result["sales"]
    settlement = result["settlement"]
    transfer_files = sorted(
        {
            Path(str(name)).name
            for item in result.get("transfer_evidence") or []
            for name in item.get("source_files") or []
        }
    )
    _style_title(
        ws,
        "人员激励核销（简明对比）",
        "销售Excel由代码读取并先核验商品知识库：69码必须精确匹配，产品名可在同码候选内唯一模糊匹配；结算单和转账截图由视觉AI识别。",
    )
    _write_header(
        ws,
        [
            "核验对象",
            f"销售Excel + 商品知识库（代码核验）\n{Path(str(sales['source_file'])).name}",
            f"结算单（视觉AI识别）\n{Path(str(settlement['source_file'])).name}",
            "转账凭证（视觉AI识别）\n" + "、".join(transfer_files),
            "具体对比结果",
            "结论 / 要重新提交什么",
        ],
        height=30,
    )

    row = 4
    for item in result["sku_reconciliation"]:
        _write_row(
            ws,
            row,
            [
                f"结算第{item['line_no']}行\n{_personnel_row_product_name(item)}",
                _personnel_sales_knowledge_text(item),
                f"视觉识别商品：{item['settlement_product_name']}\n数量：{_number(item['settlement_quantity'])}\n行奖励：{_number(item['settlement_reward_amount'])}元",
                "—（转账只核对总额）",
                _personnel_comparison_text(item),
                _personnel_conclusion(item),
            ],
            height=126,
            font_size=9,
        )
        row += 1

    values = [
        Decimal(str(summary["calculated_line_reward"])),
        Decimal(str(summary["settlement_line_reward"])),
        Decimal(str(summary["transfer_total"])),
    ]
    three_way_difference = max(values) - min(values)
    _write_row(
        ws,
        row,
        [
            "合计",
            (
                f"数量：{_number(summary['excel_mapped_quantity'])}\n"
                f"计算奖励：{_number(summary['calculated_line_reward'])}元\n"
                f"知识库：{summary.get('sales_knowledge_matched_count', 0)}个产品名精确，"
                f"{summary.get('sales_knowledge_fuzzy_count', 0)}个产品名模糊，"
                f"{summary.get('sales_knowledge_problem_count', 0)}个未通过"
            ),
            f"视觉识别数量：{_number(summary['settlement_line_quantity'])}\n行奖励合计：{_number(summary['settlement_line_reward'])}元",
            f"视觉识别并去重：{_number(summary['transfer_total'])}元",
            (
                f"三方金额差：{_number(three_way_difference)}元\n"
                f"知识库问题商品：{summary.get('sales_knowledge_problem_count', 0)}个"
            ),
            (
                f"{_match_confidence('exact')}\n三方金额一致；要重新提交：不用"
                if three_way_difference == 0
                and int(summary.get("sales_knowledge_problem_count") or 0) == 0
                else (
                    f"{_match_confidence('unmatched')}\n"
                    + (
                        "金额虽一致，但有商品未通过知识库；按对应商品行要求重新提交"
                        if three_way_difference == 0
                        else "要重新提交：核对并更正结算单或转账截图中的金额"
                    )
                )
            ),
        ],
        height=90,
        total=True,
    )
    row += 1
    claim_difference = Decimal(str(summary["claimed_amount"])) - Decimal(str(summary["transfer_total"]))
    _write_row(
        ws,
        row,
        [
            "实际申请金额",
            "—",
            f"视觉识别申请：{_number(summary['claimed_amount'])}元",
            f"视觉识别转账：{_number(summary['transfer_total'])}元",
            f"申请金额 - 转账金额：{_number(claim_difference)}元",
            (
                f"{_match_confidence('exact')}\n申请金额与转账金额一致；要重新提交：不用"
                if claim_difference == 0
                else (
                    f"{_match_confidence('unmatched')}\n"
                    "要重新提交：更正申请金额，或补一份金额差异说明"
                )
            ),
        ],
        height=60,
    )
    row += 1

    transfers = result.get("transfer_evidence") or []
    recipient_visible = bool(transfers) and all(
        item.get("identity_visible") and item.get("recipient_name") for item in transfers
    )
    store_visible = bool(transfers) and all(item.get("store_name") for item in transfers)
    dates_visible = bool(transfers) and all(
        item.get("event_date_visible") and item.get("transfer_date") for item in transfers
    )
    identity_verified = recipient_visible and store_visible and dates_visible
    if identity_verified:
        transfer_note = "收款人、门店对应关系和完整交易日期可识别"
        comparison = "Excel门店 ↔ 转账收款人可以逐一确认"
    else:
        missing: list[str] = []
        if not recipient_visible:
            missing.append("完整收款人")
        if not store_visible:
            missing.append("门店对应关系")
        if not dates_visible:
            missing.append("完整交易日期")
        transfer_note = "截图金额可识别；" + "、".join(missing) + "未显示"
        comparison = "Excel门店 ↔ 转账收款人无法逐一确认"
    identity_level = "exact" if identity_verified else (
        "fuzzy" if recipient_visible or store_visible or dates_visible else "unmatched"
    )
    amount_note = "金额一致" if three_way_difference == 0 else "金额不一致"
    _write_row(
        ws,
        row,
        [
            "收款人与日期",
            f"Excel读取到{summary['store_count']}家门店",
            f"视觉识别活动期：{settlement.get('activity_start')} 至 {settlement.get('activity_end')}",
            transfer_note,
            comparison,
            (
                f"{_match_confidence(identity_level)}\n{amount_note}；"
                + (
                    "身份与日期均可确认；要重新提交：不用"
                    if identity_verified
                    else "要重新提交：补拍转账截图，让收款人、门店和完整日期都看得见"
                )
            ),
        ],
        height=72,
    )

    # Freeze only the two title rows and the header. Freezing product rows makes
    # the lower reconciliation rows effectively unreachable on shorter screens.
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:F{row}"
    widths = {"A": 27, "B": 48, "C": 28, "D": 28, "E": 38, "F": 34}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    _sheet_base(ws, zoom=85, tab_color="70AD47")
    return ws


KNOWLEDGE_FIELD_LABELS = {
    "product_code": "商品编码",
    "product_name": "商品名称",
    "barcode_69": "69码",
}
def _sales_field_comparisons(item: dict[str, Any]) -> list[dict[str, Any]]:
    comparisons = list(item.get("field_comparisons") or [])
    if comparisons:
        return comparisons
    matched = set(item.get("matched_fields") or [])
    unmatched = set(item.get("unmatched_fields") or [])
    conflicting = set(item.get("conflicting_fields") or [])
    missing = set(item.get("missing_fields") or [])
    source_keys = {
        "product_code": "source_product_code",
        "product_name": "source_product_name",
        "barcode_69": "source_barcode_69",
    }
    knowledge_keys = {
        "product_code": "knowledge_product_code",
        "product_name": "knowledge_product_name",
        "barcode_69": "knowledge_barcode_69",
    }
    result: list[dict[str, Any]] = []
    for field in KNOWLEDGE_FIELD_LABELS:
        if field in matched:
            comparison = "matched"
        elif field in conflicting:
            comparison = "conflict"
        elif field in missing:
            comparison = "missing"
        elif field in unmatched:
            comparison = "not_found"
        else:
            comparison = "matched" if item.get(knowledge_keys[field]) else "not_found"
        result.append(
            {
                "field": field,
                "source_value": str(item.get(source_keys[field]) or ""),
                "comparison": comparison,
                "selected_knowledge_value": item.get(knowledge_keys[field]),
                "matching_products": [],
            }
        )
    return result


def _sales_difference_fields(item: dict[str, Any]) -> list[str]:
    return [
        KNOWLEDGE_FIELD_LABELS.get(str(comparison.get("field")), str(comparison.get("field")))
        for comparison in _sales_field_comparisons(item)
        if comparison.get("comparison") != "matched"
    ]
def _failed_display_controls(
    item: dict[str, Any],
    contract: dict[str, Any],
    contract_knowledge: dict[str, Any],
    contract_sales: dict[str, Any],
) -> list[str]:
    failed: list[str] = []
    if contract_sales.get("status") != "pass":
        failed.append("合同与销售")
    if not item.get("photo_files"):
        failed.append("现场照片")
    if item.get("period_match") != "match":
        failed.append("活动日期")
    if item.get("store_match") not in {"exact", "compatible"}:
        failed.append("合同门店")
    if item.get("display_match") != "pass":
        failed.append("陈列标准")
    if item.get("duplicate_check") != "none":
        failed.append("照片复用")
    if contract_knowledge.get("status") == "fail":
        failed.append("合同商品知识库")
    if item.get("photo_knowledge_match") != "exact":
        failed.append("现场商品知识库")
    if item.get("sales_product_match") != "exact":
        failed.append("现场与销售商品")
    if contract.get("requires_promotion") and not item.get("promotion_present"):
        failed.append("合同促销要求")
    if item.get("amount_rule_status") != "pass":
        failed.append("金额计费口径")
    return list(dict.fromkeys(failed))


def _wrapped_line_count(value: Any, characters_per_line: int) -> int:
    lines = str(value or "").splitlines() or [""]
    return sum(max(1, (len(line) + characters_per_line - 1) // characters_per_line) for line in lines)


def _display_row_height(values: list[Any]) -> float:
    characters = [34, 46, 44, 44, 24, 34]
    longest = max(
        _wrapped_line_count(value, width)
        for value, width in zip(values, characters, strict=True)
    )
    return min(409.0, max(132.0, 13.0 * longest + 18.0))


def _clip(value: Any, limit: int = 180) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _short_list(values: list[Any], *, limit: int = 5, empty: str = "未识别") -> str:
    clean = list(dict.fromkeys(str(value).strip() for value in values if str(value).strip()))
    if not clean:
        return empty
    visible = clean[:limit]
    suffix = f"（另{len(clean) - limit}项）" if len(clean) > limit else ""
    return "、".join(visible) + suffix


def _concise_sales_problems(sales: dict[str, Any]) -> str:
    rows: list[str] = []
    for item in sales.get("knowledge_reconciliation") or []:
        if item.get("knowledge_status") in {"matched", "fuzzy_matched"}:
            continue
        fields = _sales_difference_fields(item)
        excel_identity = " / ".join(
            [
                str(item.get("source_product_code") or "缺编码"),
                _clip(item.get("source_product_name") or "缺名称", 36),
                str(item.get("source_barcode_69") or "缺69码"),
            ]
        )
        knowledge_identity = (
            " / ".join(
                [
                    str(item.get("knowledge_product_code") or "缺编码"),
                    _clip(item.get("knowledge_product_name") or "缺名称", 36),
                    str(item.get("knowledge_barcode_69") or "缺69码"),
                ]
            )
            if item.get("knowledge_product_id")
            else "未找到同一知识库商品"
        )
        rows.append(
            f"第{item.get('excel_row')}行：{_match_confidence('unmatched')}；"
            f"Excel[{excel_identity}]；"
            f"知识库[{knowledge_identity}]；不一致["
            + ("、".join(fields) if fields else "商品身份")
            + "]"
        )
    if not rows:
        return "无"
    visible = rows[:3]
    return "\n".join(visible) + (
        f"\n其余{len(rows) - 3}行已全部纳入本表核验，匹配结果已计入上方汇总"
        if len(rows) > 3
        else ""
    )


def _sales_identity_lines(sales: dict[str, Any]) -> str:
    records = list(sales.get("records") or [])
    knowledge_by_row = {
        int(item["excel_row"]): item
        for item in sales.get("knowledge_reconciliation") or []
    }

    def confidence_for(item: dict[str, Any]) -> str:
        knowledge = knowledge_by_row.get(int(item.get("excel_row") or 0), {})
        level = {
            "matched": "exact",
            "fuzzy_matched": "fuzzy",
        }.get(str(knowledge.get("knowledge_status")), "unmatched")
        return _match_confidence(level)

    lines = [
        f"第{item.get('excel_row')}行｜{item.get('product_code') or '缺编码'}｜"
        f"{_clip(item.get('product_name') or '缺名称', 36)}｜{item.get('barcode') or '缺69码'}｜"
        f"数量{_number(item.get('quantity'))}｜{confidence_for(item)}"
        for item in records[:3]
    ]
    if len(records) > 3:
        lines.append(f"其余{len(records) - 3}行已全部纳入本表核验")
    return "\n".join(lines) or "未读取到销售商品明细"


def _contract_sales_text(contract_sales: dict[str, Any]) -> str:
    customer_labels = {
        "exact": "主体一致",
        "fuzzy": "主体模糊一致",
        "mismatch": "主体不一致",
        "unverifiable": "主体无法确认",
    }
    period_labels = {
        "covered": "日期在合同期内",
        "mismatch": "日期超出合同期",
        "unverifiable": "日期无法确认",
    }
    integrity_labels = {
        "pass": "水印/盖章可核验",
        "fail": "未见水印或盖章",
        "unverifiable": "水印/盖章无法确认",
    }
    status = str(contract_sales.get("status") or "unverifiable")
    if status == "pass":
        level = (
            "exact"
            if contract_sales.get("customer_status") == "exact"
            else "fuzzy"
        )
    elif status == "unverifiable":
        level = "fuzzy"
    else:
        level = "unmatched"
    details = "；".join(
        [
            customer_labels.get(
                str(contract_sales.get("customer_status")), "主体未核验"
            ),
            period_labels.get(str(contract_sales.get("period_status")), "日期未核验"),
            integrity_labels.get(
                str(contract_sales.get("integrity_status")), "合同完整性未核验"
            ),
        ]
    )
    return f"{_match_confidence(level)}（{details}）"


def _internal_product_text(item: dict[str, Any]) -> str:
    checks = list(item.get("sales_product_checks") or [])
    if not checks:
        return f"{_match_confidence('unmatched')}（没有找到与现场商品对应的销售行）"
    code_values = {str(check.get("product_code_match") or "unverifiable") for check in checks}
    name_values = {str(check.get("name_match") or "mismatch") for check in checks}
    barcode_values = {str(check.get("barcode_match") or "mismatch") for check in checks}
    if code_values == {"exact"}:
        code_text = "商品编码一致"
    elif "mismatch" in code_values:
        code_text = "商品编码不一致"
    else:
        code_text = "商品编码暂时不能判断"
    if "mismatch" in name_values:
        name_text = "商品名称对不上"
    elif "unverifiable" in name_values:
        name_text = "商品名称暂时不能判断"
    elif "fuzzy" in name_values:
        name_text = "商品名称模糊匹配"
    else:
        name_text = "商品名称精确匹配"
    if barcode_values == {"exact"}:
        barcode_text = "69码一致"
    elif "mismatch" in barcode_values:
        barcode_text = "69码不一致"
    else:
        barcode_text = "69码暂时不能判断"
    status = str(item.get("sales_product_match") or "unmatched")
    level = {
        "exact": "exact",
        "fuzzy": "fuzzy",
        "candidate": "fuzzy",
    }.get(status, "unmatched")
    return (
        f"{_match_confidence(level)}（{code_text}；{name_text}；{barcode_text}）"
    )


def _field_match_text(value: Any) -> str:
    return {
        "exact": "精确匹配",
        "fuzzy": "模糊匹配",
        "mismatch": "完全不匹配",
        "unverifiable": "暂时无法判断",
    }.get(str(value), "暂时无法判断")


def _sales_correspondence_text(
    item: dict[str, Any],
    sales: dict[str, Any],
    *,
    include_summary: bool,
) -> str:
    """Render every sales row related to this store's field-photo products."""

    lines: list[str] = []
    if include_summary:
        lines.extend(
            [
                f"客户：{_short_list(list(sales.get('customers') or []), limit=4)}",
                f"业务日期：{_short_list(list(sales.get('period_values') or []), limit=6)}",
                (
                    f"销售汇总：{len(sales.get('records') or [])}行；"
                    f"数量{_number(sales.get('total_quantity'))}；"
                    f"金额{_number(sales.get('retail_amount'))}元"
                ),
            ]
        )
    checks = list(item.get("sales_product_checks") or [])
    if not checks:
        lines.append("现场商品对应销售明细：未找到")
        return "\n".join(lines)
    for index, check in enumerate(checks, 1):
        if lines:
            lines.append("")
        lines.extend(
            [
                f"现场商品{index}（知识库）",
                f"商品编码：{check.get('knowledge_product_code') or '缺编码'}",
                f"商品名称：{check.get('knowledge_product_name') or '缺名称'}",
                f"69码：{check.get('knowledge_barcode_69') or '缺69码'}",
                "",
            ]
        )
        if check.get("excel_row") is None:
            lines.append(
                "对应销售Excel：未找到69码为"
                f"{check.get('knowledge_barcode_69') or '缺69码'}且商品名称能够对应"
                f"“{check.get('knowledge_product_name') or '该现场商品'}”的有效销售行"
            )
            lines.append(
                "逐项核对：没有对应销售行，商品编码、商品名称和69码均无法核对"
            )
        else:
            lines.extend(
                [
                    f"对应销售Excel第{check['excel_row']}行",
                    f"商品编码：{check.get('source_product_code') or '缺编码'}",
                    f"商品名称：{check.get('source_product_name') or '缺名称'}",
                    f"69码：{check.get('source_barcode_69') or '缺69码'}",
                    f"数量：{_number(check.get('source_quantity'))}",
                    "",
                ]
            )
            lines.append(
                "逐项核对："
                f"商品编码{_field_match_text(check.get('product_code_match'))}；"
                f"商品名称{_field_match_text(check.get('name_match'))}；"
                f"69码{_field_match_text(check.get('barcode_match'))}"
            )
        level = {
            "exact": "exact",
            "fuzzy": "fuzzy",
            "candidate": "fuzzy",
            "unmatched": "unmatched",
        }.get(str(check.get("status")), "unmatched")
        lines.append("本行结论：" + _match_confidence(level))
    return "\n".join(lines)


def _photo_product_text(item: dict[str, Any]) -> str:
    hits = list(item.get("product_reference_hits") or [])
    if not hits:
        return f"未对应知识库商品（{_match_confidence('unmatched')}）"
    values = [
        f"{hit.get('product_name') or '未命名'}（{hit.get('product_code') or '缺编码'} / "
        f"{hit.get('barcode_69') or '缺69码'}，"
        f"{_match_confidence('exact' if hit.get('confidence') == 'exact' else 'fuzzy')}）"
        for hit in hits
    ]
    return "、".join(values)


def _sales_knowledge_summary(sales: dict[str, Any]) -> str:
    exact_count = int(sales.get("knowledge_matched_count") or 0)
    fuzzy_count = int(sales.get("knowledge_fuzzy_count") or 0)
    unmatched_count = int(sales.get("knowledge_problem_count") or 0)
    if unmatched_count == 0 and fuzzy_count == 0 and exact_count:
        level = "exact"
    elif exact_count or fuzzy_count:
        level = "fuzzy"
    else:
        level = "unmatched"
    return (
        f"{_match_confidence(level)}；精确匹配{exact_count}行、"
        f"模糊匹配{fuzzy_count}行、完全不匹配{unmatched_count}行"
    )


def _contract_photo_text(
    item: dict[str, Any],
    contract: dict[str, Any],
    contract_knowledge: dict[str, Any],
    *,
    store: str,
    period: str,
    display: str,
    promotion_result: str,
    contract_product_result: str,
) -> str:
    hard_failure = (
        not item.get("photo_files")
        or item.get("period_match") == "mismatch"
        or item.get("store_match") == "mismatch"
        or item.get("display_match") == "fail"
        or (
            contract.get("requires_promotion")
            and not item.get("promotion_present")
        )
        or contract_knowledge.get("status") == "fail"
    )
    uncertain = (
        item.get("period_match") != "match"
        or item.get("store_match") != "exact"
        or item.get("display_match") != "pass"
    )
    level = "unmatched" if hard_failure else ("fuzzy" if uncertain else "exact")
    return (
        f"{_match_confidence(level)}（门店{store}；日期{period}；"
        f"陈列{display}；促销{promotion_result}；"
        f"合同商品{contract_product_result}）"
    )


def _plain_advice(value: Any) -> str:
    text = str(value)
    replacements = {
        "商品视觉RAG": "商品知识库",
        "视觉RAG": "商品知识库",
        "RAG": "商品知识库",
        "候选命中": "模糊匹配",
        "候选": "模糊匹配",
        "商品ID": "商品身份",
        "SKU": "具体商品",
        "唯一收敛": "唯一确认",
    }
    for source, target in replacements.items():
        text = text.replace(source, target)
    return text


def _row_ranges(values: list[int]) -> str:
    numbers = sorted(set(int(value) for value in values))
    if not numbers:
        return ""
    groups: list[tuple[int, int]] = []
    start = previous = numbers[0]
    for value in numbers[1:]:
        if value == previous + 1:
            previous = value
            continue
        groups.append((start, previous))
        start = previous = value
    groups.append((start, previous))
    return "、".join(
        str(start) if start == end else f"{start}—{end}"
        for start, end in groups
    )


def _display_resubmission_items(
    item: dict[str, Any],
    contract: dict[str, Any],
    contract_knowledge: dict[str, Any],
    contract_sales: dict[str, Any],
) -> list[str]:
    """Build at most one plain-language request per source file type."""

    sales_details = list(
        dict.fromkeys(
            str(check["resubmission"]).strip()
            for check in item.get("sales_product_checks") or []
            if check.get("resubmission")
        )
    )
    if contract_sales.get("customer_status") in {"mismatch", "unverifiable"}:
        sales_details.append("客户名称要能和合同签订方对应")
    if contract_sales.get("period_status") in {"mismatch", "unverifiable"}:
        sales_details.append("业务日期要落在合同活动期内")

    contract_details: list[str] = []
    if contract_sales.get("integrity_status") != "pass":
        contract_details.append("水印或盖章要拍清楚")
    if contract_knowledge.get("status") == "fail":
        contract_details.append("合同写到的具体商品要能对应知识库商品")
    if item.get("amount_rule_status") != "pass":
        contract_details.append("写清按店或按堆头的单价和数量")

    photo_details: list[str] = []
    if not item.get("photo_files"):
        photo_details.append(f"补交{item['contract_store_name']}的现场原图")
    if item.get("photo_knowledge_match") == "candidate":
        photo_details.append("把商品短码、规格、口味或正面包装再拍清楚一点")
    elif item.get("photo_knowledge_match") == "unmatched":
        photo_details.append("重新拍商品正面，至少让商品短码或明显名称看清")
    if item.get("period_match") != "match":
        photo_details.append("让完整拍摄日期看得见")
    if item.get("store_match") not in {"exact", "compatible"}:
        photo_details.append("让门店名称或地址看得见")
    if item.get("display_match") != "pass":
        photo_details.append("补一张完整堆头全景，能看清1平方米或数清4列")
    if item.get("duplicate_check") != "none":
        photo_details.append("提交这家门店自己拍的、没有在别店用过的原图")
    if contract.get("requires_promotion") and not item.get("promotion_present"):
        photo_details.append("把合同要求的促销牌或促销文字拍清楚")

    result: list[str] = []
    if sales_details:
        result.append("重新提交销售Excel：" + "；".join(dict.fromkeys(sales_details)))
    if contract_details:
        result.append("重新提交合同PDF或补充说明：" + "；".join(dict.fromkeys(contract_details)))
    if photo_details:
        result.append("重新提交现场照片：" + "；".join(dict.fromkeys(photo_details)))
    return result


def _contract_requirement_lines(contract: dict[str, Any]) -> list[str]:
    """Show only contract requirements that actually exist in the source."""

    lines = [f"陈列：{contract.get('display_standard') or '未识别'}"]
    if contract.get("requires_specific_products"):
        product_requirement = _short_list(
            list(contract.get("required_products") or []),
            limit=3,
            empty="合同限定了具体商品，但未识别到商品名称",
        )
        lines.append(f"商品：{product_requirement}")
    if contract.get("requires_promotion"):
        promotion_requirement = str(
            contract.get("required_promotion")
            or "合同要求促销，但未识别到具体形式"
        )
        lines.append(f"促销：{promotion_requirement}")
    return lines


def _add_display_sheet(wb: Workbook, result: dict[str, Any]) -> Any:
    ws = wb.create_sheet("堆头核销")
    summary = result["summary"]
    contract = result["contract"]
    sales = result["sales"]
    contract_sales = result.get("contract_sales_reconciliation") or {}
    contract_knowledge = result.get("contract_product_knowledge") or {}
    _style_title(
        ws,
        "堆头陈列核销（简明对比）",
        "按合同、现场照片、销售Excel的顺序核验；现场先确定知识库商品，再逐一核对相关销售行。",
    )
    _write_header(
        ws,
        [
            f"合同PDF（视觉AI识别）\n{Path(str(contract['source_file'])).name}",
            "现场照片（视觉AI识别）\n逐张显示文件名和识别内容",
            f"销售Excel（代码读取）\n{Path(str(sales['source_file'])).name}",
            "与合同具体对比结果",
            "核销金额",
            "结论 / 要重新提交什么",
        ],
        height=60,
    )

    period_labels = {"match": "一致", "mismatch": "不一致", "unverifiable": "无法确认"}
    store_labels = {
        "exact": "一致",
        "compatible": "基本一致",
        "mismatch": "不一致",
        "filename_only": "无法确认（仅见文件名）",
    }
    display_labels = {"pass": "符合", "fail": "不符合", "uncertain": "无法确认"}
    display_basis_labels = {
        "stack_1sqm": "达到1平米堆头",
        "four_vertical": "达到4纵陈列",
        "both": "同时达到1平米堆头和4纵陈列",
        "none": "未达到1平米堆头或4纵陈列",
        "unclear": "照片不足以判断1平米堆头或4纵陈列",
    }
    duplicate_labels = {
        "none": "未发现跨门店照片复用",
        "exact": "发现跨门店照片复用",
        "possible": "疑似跨门店照片复用",
        "unverifiable": "无法判断是否存在跨门店照片复用",
    }
    photo_knowledge_labels = {
        "exact": _match_confidence("exact"),
        "candidate": _match_confidence("fuzzy"),
        "unmatched": _match_confidence("unmatched"),
    }
    contract_stores = {
        int(store["line_no"]): store for store in contract.get("stores") or []
    }
    contract_requirement_text = "\n".join(_contract_requirement_lines(contract))
    all_failed_controls: list[str] = []

    row = 4
    for item in result["store_reconciliation"]:
        photo_names = "、".join(Path(str(value)).name for value in item["photo_files"]) or "未提交照片"
        period = period_labels.get(item["period_match"], str(item["period_match"]))
        store = store_labels.get(item["store_match"], str(item["store_match"]))
        display = display_labels.get(item["display_match"], str(item["display_match"]))
        display_basis = display_basis_labels.get(
            item.get("display_standard_basis"),
            str(item.get("display_standard_basis") or "未提供陈列标准依据"),
        )
        display_description = _clip(
            item.get("display_description") or "未提供可见陈列依据",
            130,
        )
        vertical_count = item.get("display_vertical_facing_count")
        vertical_count_text = "无法可靠计数" if vertical_count is None else str(vertical_count)
        duplicate = duplicate_labels.get(item["duplicate_check"], str(item["duplicate_check"]))
        photo_knowledge = photo_knowledge_labels.get(
            item.get("photo_knowledge_match"),
            str(item.get("photo_knowledge_match") or "未对应知识库"),
        )
        store_record = contract_stores.get(int(item["store_line_no"]), {})
        contract_address = str(store_record.get("address") or "合同未列地址")
        failed_controls = _failed_display_controls(
            item,
            contract,
            contract_knowledge,
            contract_sales,
        )
        all_failed_controls.extend(failed_controls)
        failed_text = "、".join(failed_controls) if failed_controls else "无"
        resubmission_items = _display_resubmission_items(
            item,
            contract,
            contract_knowledge,
            contract_sales,
        )
        resubmission_text = "\n".join(
            f"{index}. {value}"
            for index, value in enumerate(resubmission_items, 1)
        )
        promotion_result = (
            "符合"
            if not contract.get("requires_promotion") or item.get("promotion_present")
            else "不符合"
        )
        if row == 4:
            contract_text = (
                f"签订方：{_short_list(list(contract.get('contract_parties') or []), limit=4)}\n"
                f"活动预算：{_number(contract.get('activity_budget'))}元；申报：{_number(contract.get('claimed_amount'))}元\n"
                f"执行周期：{contract.get('activity_start') or '未识别'} 至 {contract.get('activity_end') or '未识别'}\n"
                f"活动内容：{_clip(contract.get('activity_content') or '未识别', 120)}\n"
                f"商家/门店：共{len(contract.get('stores') or [])}家；本店{item['contract_store_name']}\n"
                f"堆头数量：总计{_number(contract.get('contract_stack_count'))}；本店{_number(store_record.get('stack_count'))}\n"
                f"水印：{_number(contract.get('watermark_visible'))}\n"
                f"盖章：{_number(contract.get('seal_visible'))}\n"
                f"{contract_requirement_text}"
            )
        else:
            contract_text = (
                "合同总体见首行\n"
                f"第{item['store_line_no']}家：{item['contract_store_name']}\n"
                f"地址：{contract_address}\n本店堆头：{_number(store_record.get('stack_count'))}"
            )
        visible_text = _short_list(
            list(item.get("visible_text") or []),
            limit=6,
            empty="未识别到有效文字",
        )
        visual_basis = _clip(display_description, 120)
        photo_column_text = (
            f"文件：{photo_names}\n"
            f"可见文字：{visible_text}\n"
            f"识别日期：{item.get('visible_date') or '未识别'}\n"
            f"识别地点：{item.get('visible_location') or '未识别'}\n"
            f"陈列标准核验：{display}（{display_basis}）\n"
            f"视觉依据：{visual_basis}\n"
            f"可见纵列数：{vertical_count_text}\n"
            f"照片复用检查：{duplicate}\n"
            f"现场商品：{_photo_product_text(item)}\n"
            f"知识库结论：{photo_knowledge}\n"
            f"促销信息：{item.get('promotion_summary') or '未识别'}"
        )
        sales_column_text = _sales_correspondence_text(
            item,
            sales,
            include_summary=row == 4,
        )
        contract_product_result = {
            "not_applicable": "不适用",
            "pass": "具体商品已对上知识库",
            "fail": "具体商品未对上知识库",
        }.get(str(contract_knowledge.get("status")), "未核验")
        comparison_column_text = (
            "合同 ↔ 现场："
            + _contract_photo_text(
                item,
                contract,
                contract_knowledge,
                store=store,
                period=period,
                display=display,
                promotion_result=promotion_result,
                contract_product_result=contract_product_result,
            )
            + "\n"
            f"现场 ↔ 销售：{_internal_product_text(item)}\n"
            f"合同 ↔ 销售：{_contract_sales_text(contract_sales)}"
        )
        units_text = _number(item.get("claim_units"))
        calculation_text = (
            f"{units_text} × {_number(summary['fee_per_store'])}元"
            if item.get("amount_rule_status") == "pass"
            else "不自动分摊，转人工确认"
        )
        amount_column_text = (
            f"口径：{_clip(item.get('amount_rule_basis') or '未识别', 90)}\n"
            f"计算：{calculation_text}\n"
            f"本店支持：{_number(item['supported_amount'])}元"
        )
        conclusion_column_text = (
            "结论：通过\n要重新提交：不用"
            if item["status"] == "pass"
            else (
                f"结论：暂不能核销\n主要问题：{failed_text}\n"
                f"要重新提交：\n{resubmission_text or '1. 补交能看清问题项的原始材料'}"
            )
        )
        row_values = [
            contract_text,
            photo_column_text,
            sales_column_text,
            comparison_column_text,
            amount_column_text,
            conclusion_column_text,
        ]
        _write_row(
            ws,
            row,
            row_values,
            height=_display_row_height(row_values),
            font_size=9,
        )
        row += 1

    if summary["supplement_store_count"] == 0:
        conclusion = "通过"
    elif summary["passed_store_count"]:
        conclusion = "部分通过"
    else:
        conclusion = "暂不能核销"
    retail = _number(summary.get("sales_retail_amount"))
    failed_control_summary = "、".join(dict.fromkeys(all_failed_controls)) or "无"
    _write_row(
        ws,
        row,
        [
            f"合计：合同{summary['contract_store_count']}家\n预算{_number(summary.get('activity_budget'))}元；申报{_number(summary['claimed_amount'])}元",
            f"共识别{summary['photo_count']}张照片",
            f"Excel读取{summary['sales_sku_count']}行商品\n数量{_number(summary['sales_quantity'])}；零售额{retail}元\n与现场商品相关的销售行已在各门店逐项展示",
            f"{summary['passed_store_count']}家通过；{summary['supplement_store_count']}家需补证\n未通过字段汇总：{failed_control_summary}",
            f"建议核销{_number(summary['suggested_approved_amount'])}元\n暂缓{_number(summary['temporarily_held_amount'])}元",
            conclusion,
        ],
        height=108,
        total=True,
        font_size=9,
    )
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:F{row}"
    widths = {"A": 40, "B": 54, "C": 52, "D": 48, "E": 28, "F": 42}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    _sheet_base(ws, zoom=75, tab_color="ED7D31")
    return ws


def create_combined_report(results: list[dict[str, Any]], output_path: str | Path) -> Path:
    if not results:
        raise AuditError("没有可生成工作簿的核销结果")
    by_scenario: dict[str, dict[str, Any]] = {}
    for result in results:
        scenario = str(result.get("scenario") or "")
        if scenario in by_scenario:
            raise AuditError(f"同一工作簿不能包含两份同类结果：{scenario}")
        by_scenario[scenario] = result
    unknown = set(by_scenario) - {"personnel_incentive", "promotional_display"}
    if unknown:
        raise AuditError("不支持的核销结果类型：" + "、".join(sorted(unknown)))

    workbook = Workbook()
    workbook.remove(workbook.active)
    if "personnel_incentive" in by_scenario:
        _add_personnel_sheet(workbook, by_scenario["personnel_incentive"])
    if "promotional_display" in by_scenario:
        _add_display_sheet(workbook, by_scenario["promotional_display"])
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    workbook.close()
    return target


def verify_workbook(path: str | Path, scenarios: list[str]) -> dict[str, Any]:
    source = Path(path)
    workbook = load_workbook(source, data_only=False)
    expected = [
        name
        for scenario, name in (
            ("personnel_incentive", "人员激励核销"),
            ("promotional_display", "堆头核销"),
        )
        if scenario in scenarios
    ]
    if workbook.sheetnames != expected:
        raise AuditError(f"工作表名称或顺序错误：{workbook.sheetnames}，期望{expected}")
    formulas: list[str] = []
    errors: list[str] = []
    for ws in workbook.worksheets:
        if ws.max_column != 6:
            raise AuditError(f"{ws.title} 不是6列结构")
        merges = {str(value) for value in ws.merged_cells.ranges}
        if not {"A1:F1", "A2:F2"}.issubset(merges):
            raise AuditError(f"{ws.title} 缺少两行合并标题")
        expected_freeze = "A4"
        if str(ws.freeze_panes) != expected_freeze:
            raise AuditError(f"{ws.title} 冻结窗格错误：{ws.freeze_panes}")
        if ws.auto_filter.ref != f"A3:F{ws.max_row}":
            raise AuditError(f"{ws.title} 筛选范围错误：{ws.auto_filter.ref}")
        for row in ws.iter_rows():
            for cell in row:
                if cell.data_type == "f" or (isinstance(cell.value, str) and cell.value.startswith("=")):
                    formulas.append(f"{ws.title}!{cell.coordinate}")
                if isinstance(cell.value, str) and cell.value in FORMULA_ERRORS:
                    errors.append(f"{ws.title}!{cell.coordinate}")
    workbook.close()
    if formulas:
        raise AuditError("输出工作簿不允许公式：" + "、".join(formulas))
    if errors:
        raise AuditError("输出工作簿含公式错误值：" + "、".join(errors))
    return {
        "path": str(source.resolve()),
        "sheet_names": expected,
        "formula_count": 0,
        "formula_error_count": 0,
    }
