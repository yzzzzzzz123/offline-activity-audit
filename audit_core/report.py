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


def _font(*, bold: bool = False, color: str = "000000", size: int = 10) -> Font:
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
    if text in {"通过", "全部通过", "匹配", "三方金额匹配", "身份与日期已验证"}:
        return PatternFill("solid", fgColor=PASS_GREEN)
    if any(token in text for token in ("需", "未验证", "不一致", "部分", "人工", "未匹配")):
        return PatternFill("solid", fgColor=REVIEW_YELLOW)
    return None


def _write_row(
    ws: Any,
    row: int,
    values: list[Any],
    *,
    height: float,
    total: bool = False,
) -> None:
    for column, value in enumerate(values, 1):
        cell = ws.cell(row, column, value)
        cell.border = _border()
        cell.alignment = Alignment(vertical="top", wrap_text=True)
        cell.font = _font(bold=total, color="000000" if total else INPUT_BLUE)
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


def _personnel_conclusion(item: dict[str, Any]) -> str:
    if item.get("mapping_status") != "matched":
        return "需补证：商品未匹配"
    if Decimal(str(item.get("quantity_difference") or 0)) != 0:
        return "需补证：销售数量不一致"
    if Decimal(str(item.get("settlement_line_amount_difference") or 0)) != 0:
        return "需补证：奖励金额不一致"
    if item.get("mapping_confidence") != "high":
        return "金额匹配，商品身份未验证"
    return "通过"


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
        "销售Excel由代码读取；结算单和转账截图由视觉AI识别。逐商品比较Excel与结算单，最后比较三方金额。",
    )
    _write_header(
        ws,
        [
            "核验对象",
            f"销售Excel（代码读取）\n{Path(str(sales['source_file'])).name}",
            f"结算单（视觉AI识别）\n{Path(str(settlement['source_file'])).name}",
            "转账凭证（视觉AI识别）\n" + "、".join(transfer_files),
            "具体对比结果",
            "结论",
        ],
        height=30,
    )

    row = 4
    for item in result["sku_reconciliation"]:
        _write_row(
            ws,
            row,
            [
                f"结算第{item['line_no']}行\n{item['settlement_product_name']}",
                f"条码：{item.get('mapped_barcode') or '未匹配'}\n数量：{_number(item.get('excel_quantity'))}\n计算奖励：{_number(item['calculated_settlement_reward'])}元",
                f"视觉识别商品：{item['settlement_product_name']}\n数量：{_number(item['settlement_quantity'])}\n行奖励：{_number(item['settlement_reward_amount'])}元",
                "—（转账只核对总额）",
                f"Excel ↔ 结算单\n数量差：{_number(item.get('quantity_difference'))}\n金额差：{_number(item['settlement_line_amount_difference'])}元",
                _personnel_conclusion(item),
            ],
            height=72,
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
            f"数量：{_number(summary['excel_mapped_quantity'])}\n计算奖励：{_number(summary['calculated_line_reward'])}元",
            f"视觉识别数量：{_number(summary['settlement_line_quantity'])}\n行奖励合计：{_number(summary['settlement_line_reward'])}元",
            f"视觉识别并去重：{_number(summary['transfer_total'])}元",
            f"三方金额差：{_number(three_way_difference)}元",
            "三方金额匹配" if three_way_difference == 0 else "需补证：三方金额不一致",
        ],
        height=72,
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
            "匹配" if claim_difference == 0 else "需补证：说明金额差异",
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
    amount_prefix = "金额匹配" if three_way_difference == 0 else "金额不匹配"
    _write_row(
        ws,
        row,
        [
            "收款人与日期",
            f"Excel读取到{summary['store_count']}家门店",
            f"视觉识别活动期：{settlement.get('activity_start')} 至 {settlement.get('activity_end')}",
            transfer_note,
            comparison,
            "身份与日期已验证" if identity_verified else f"{amount_prefix}，身份与日期未验证",
        ],
        height=72,
    )

    # Freeze only the two title rows and the header. Freezing product rows makes
    # the lower reconciliation rows effectively unreachable on shorter screens.
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:F{row}"
    widths = {"A": 28, "B": 38, "C": 28, "D": 28, "E": 30, "F": 30}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    _sheet_base(ws, zoom=85, tab_color="70AD47")
    return ws


def _display_row_height(product_names: list[str], display_description: str) -> float:
    length = len("；".join(product_names))
    description_extra = 18 if len(display_description) > 50 else 0
    if length > 140:
        return 186 + description_extra
    if length > 100:
        return 171 + description_extra
    return 164 + description_extra


def _add_display_sheet(wb: Workbook, result: dict[str, Any]) -> Any:
    ws = wb.create_sheet("堆头核销")
    summary = result["summary"]
    contract = result["contract"]
    sales = result["sales"]
    customers = "、".join(str(value) for value in sales.get("customers") or [])
    periods = "、".join(str(value) for value in sales.get("period_values") or [])
    _style_title(
        ws,
        "堆头陈列核销（简明对比）",
        "合同PDF和现场照片由视觉AI识别，销售Excel由代码读取；每行直接显示三类材料怎样对比。",
    )
    _write_header(
        ws,
        [
            f"合同PDF（视觉AI识别）\n{Path(str(contract['source_file'])).name}",
            f"销售Excel（代码读取）\n{Path(str(sales['source_file'])).name}\n对应现场识别产品",
            "现场照片（视觉AI识别）\n逐张显示文件名和识别内容",
            "与合同具体对比结果",
            "核销金额",
            "结论 / 需补材料",
        ],
        height=60,
    )

    period_labels = {"match": "日期符合", "mismatch": "日期不符", "unverifiable": "日期未识别"}
    store_labels = {
        "exact": "地点符合",
        "compatible": "地点基本符合",
        "mismatch": "地点不符",
        "filename_only": "地点仅见文件名",
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
    product_match_labels = {"exact": "明确对应", "candidate": "候选对应", "unmatched": "未匹配"}

    row = 4
    for item in result["store_reconciliation"]:
        photo_names = "、".join(Path(str(value)).name for value in item["photo_files"]) or "未提交照片"
        products = "；".join(str(value) for value in item.get("recognized_products") or []) or "未能可靠识别具体产品"
        sales_names = [str(value) for value in item.get("sales_product_names") or []]
        sales_text = "；".join(sales_names) or "未找到可验证的对应商品名"
        period = period_labels.get(item["period_match"], str(item["period_match"]))
        store = store_labels.get(item["store_match"], str(item["store_match"]))
        display = display_labels.get(item["display_match"], str(item["display_match"]))
        display_basis = display_basis_labels.get(
            item.get("display_standard_basis"),
            str(item.get("display_standard_basis") or "未提供陈列标准依据"),
        )
        display_description = str(item.get("display_description") or "未提供可见陈列依据").strip()
        vertical_count = item.get("display_vertical_facing_count")
        vertical_count_text = "无法可靠计数" if vertical_count is None else str(vertical_count)
        vertical_basis = "；".join(
            str(value).strip()
            for value in item.get("display_vertical_facing_basis") or []
            if str(value).strip()
        ) or "未形成可靠逐列依据"
        stack_basis = str(item.get("display_stack_1sqm_basis") or "").strip()
        stack_basis = stack_basis or "未形成可验证的1平米依据"
        structured_display_description = (
            f"{display_description}\n"
            f"可见纵列数：{vertical_count_text}\n"
            f"逐列依据（左到右）：{vertical_basis}\n"
            f"1平米依据：{stack_basis}"
        )
        duplicate = duplicate_labels.get(item["duplicate_check"], str(item["duplicate_check"]))
        match = product_match_labels.get(item["sales_product_match"], str(item["sales_product_match"]))
        advice = "；".join(item.get("supplement_advice") or [])
        warning = "\n无门店明细，不能单独证明该店" if not sales.get("store_level_available") else ""
        _write_row(
            ws,
            row,
            [
                f"第{item['store_line_no']}家：{item['contract_store_name']}\n合同要求：{_number(summary['fee_per_store'])}元；{contract['display_standard']}",
                f"读取客户：{customers}\n读取期间：{periods}\nExcel对应产品：{sales_text}\n对应判断：{match}{warning}",
                f"文件：{photo_names}\n识别日期：{item.get('visible_date') or '未识别'}\n识别地点：{item.get('visible_location') or '未识别'}\n陈列标准核验：{display}（{display_basis}）\n视觉依据：{structured_display_description}\n照片复用检查：{duplicate}\n识别产品：{products}\n促销信息：{item['promotion_summary']}",
                f"照片 ↔ 合同日期/门店/陈列标准/照片复用\n{period}；{store}\n陈列标准：{display}（{display_basis}）\n照片复用：{duplicate}",
                f"{_number(item['supported_amount'])}元",
                "通过" if item["status"] == "pass" else f"需补证：{advice or '补充可核验的现场照片。'}",
            ],
            height=_display_row_height(sales_names, structured_display_description),
        )
        row += 1

    if summary["supplement_store_count"] == 0:
        conclusion = "通过"
    elif summary["passed_store_count"]:
        conclusion = "部分通过"
    else:
        conclusion = "需人工复核"
    retail = _number(summary.get("sales_retail_amount"))
    _write_row(
        ws,
        row,
        [
            f"合计：合同{summary['contract_store_count']}家\n申报{_number(summary['claimed_amount'])}元",
            f"Excel读取{summary['sales_sku_count']}个SKU\n数量{_number(summary['sales_quantity'])}；零售额{retail}元",
            f"共识别{summary['photo_count']}张照片",
            f"{summary['passed_store_count']}家通过；{summary['supplement_store_count']}家需补证",
            f"建议核销{_number(summary['suggested_approved_amount'])}元\n暂缓{_number(summary['temporarily_held_amount'])}元",
            conclusion,
        ],
        height=66,
        total=True,
    )
    ws.freeze_panes = "A4"
    ws.auto_filter.ref = f"A3:F{row}"
    widths = {"A": 42, "B": 38, "C": 66.3333333333333, "D": 42, "E": 22, "F": 48}
    for column, width in widths.items():
        ws.column_dimensions[column].width = width
    _sheet_base(ws, zoom=80, tab_color="ED7D31")
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
