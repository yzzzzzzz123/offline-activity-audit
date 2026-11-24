"""Build complete POS calculations from source spreadsheet cells, not samples.

Only explicit native rows and unambiguous columns/ranges are used. Unknown
layouts remain unknown; no formula execution or rounding supplies missing data.
"""
from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation, localcontext
import math
import re
from .spreadsheet_rows import ROW, CELL, parse_cells


NUMBER = re.compile(r"[-+]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?")
SUM = re.compile(r"公式=SUM\(\$?([A-Z]+)\$?(\d+):\$?([A-Z]+)\$?(\d+)\)；缓存值=", re.I)
PRODUCT = re.compile(r"公式=\$?([A-Z]+)\$?(\d+)\s*\*\s*\$?([A-Z]+)\$?(\d+)；缓存值=.+", re.I)
TOTAL = re.compile(r"(?:总计|小计|合计|总合计|本页合计|累计)[：:\s]*$")
INLINE_AMOUNT_TOTAL = re.compile(r"(?:合计|总计|总合计)\s*[：:]\s*(" + NUMBER.pattern + r")\s*元")
HEADERS = {
    "quantity": {"数量", "销量", "销售数量", "销售量", "销售总数量", "售出数量", "实销数量"},
    "price": {"单价", "销售单价", "销售价格", "零售单价", "零售价", "实销单价", "成交单价"},
    "amount": {"金额", "销售金额", "销售额", "销售总金额", "合计金额", "销售总计", "实销金额", "成交金额",
               "POS金额", "POS销售金额", "POS销售额"},
}
PERIOD_HEADER = re.compile(r"\d{4}年\d{1,2}月\d{1,2}日[-—~至]\d{4}年\d{1,2}月\d{1,2}日")


def _header_label(raw: str) -> str:
    return re.sub(r"\s|[（(][^）)]*[）)]", "", raw)


def _columns(cells: dict, *, amount_only: bool = False) -> dict | None:
    found = {}
    for col, cell in cells.items():
        label = _header_label(cell["raw"])
        for kind, names in HEADERS.items():
            if amount_only and kind != 'amount':
                continue
            # A pivot's explicit sum label preserves quantity/amount meaning.
            # Do not treat summed/averaged prices as a sale unit price.
            candidate = re.sub(r"^求和项[:：]", "", label) if kind in {"quantity", "amount"} else label
            if candidate in names:
                if kind in found:
                    return None
                found[kind] = col
    if set(found) == {"quantity", "price"}:
        labels = {col: _header_label(cell["raw"]) for col, cell in cells.items()}
        periods = [col for col, label in labels.items() if PERIOD_HEADER.fullmatch(label)]
        # Confirmed dealer layout: the activity-period column contains sales
        # amount. Only recognize it with explicit SKU/name/quantity/unit-price
        # columns and one period; never invent a price by dividing two columns.
        if (len(periods) == 1 and any(label in {"商品名称", "产品名称", "品名"} for label in labels.values())
                and any(label in {"条码", "69码", "商品条码"} for label in labels.values())):
            found["amount"] = periods[0]
    return found if len(found) == (1 if amount_only else 3) else None


def _formula_column_candidate(cells: dict) -> dict | None:
    """A narrow candidate only; no column meaning is accepted before proof."""
    labels = {col: _header_label(cell["raw"]) for col, cell in cells.items()}
    quantity = [col for col, label in labels.items() if label in HEADERS["quantity"]]
    amounts = [col for col, label in labels.items() if label in HEADERS["amount"]]
    factors = [col for col, label in labels.items() if label == "销售金额"]
    totals = [col for col, label in labels.items() if label == "销售总计"]
    if (len(quantity) != 1 or len(amounts) != 2 or len(factors) != 1 or len(totals) != 1
            or any(label in HEADERS["price"] for label in labels.values())):
        return None
    return {"quantity": quantity[0], "price": factors[0], "amount": totals[0]}


def merged_cell_bounds(layout):
    """Return verified Excel rectangle bounds or no layout evidence for old snapshots."""
    from openpyxl.utils.cell import range_boundaries
    addresses = layout.get('merged_ranges') if isinstance(layout, dict) else None
    if not isinstance(addresses, (list, tuple)):
        return []
    bounds = []
    for address in addresses:
        if not isinstance(address, str):
            return []
        try:
            left, top, right, bottom = range_boundaries(address)
        except ValueError:
            return []
        if (None in (left, top, right, bottom) or not 1 <= left <= right <= 16_384
                or not 1 <= top <= bottom <= 1_048_576):
            return []
        bounds.append((left, top, right, bottom))
    return bounds


def _merged_header_columns(row, rows, layout):
    """Resolve explicit two-row headers from native merge boundaries only.

    This supplies column meaning, never data values. A numeric cell is never
    filled down or duplicated, and column bindings cannot cross sheets/files.
    """
    if not isinstance(layout, dict) or not layout.get('merged_ranges'):
        return None
    index = row['index']
    previous = rows.get(index - 1)
    if previous is None:
        return None
    if any(NUMBER.fullmatch(c['raw'].strip()) or c['raw'].startswith('公式') for c in row['cells'].values()):
        return None
    labels = {col: cell['raw'] for col, cell in row['cells'].items()}
    for first_col, first_row, last_col, last_row in merged_cell_bounds(layout):
        parent = rows.get(first_row, {}).get('cells', {}).get(first_col, {}).get('raw', '')
        if not parent or NUMBER.fullmatch(parent.strip()) or parent.startswith('公式'):
            continue
        if first_row < index <= last_row and first_col == last_col:
            labels.setdefault(first_col, parent)
        elif first_row == last_row == index - 1 and last_col > first_col:
            for col in list(labels):
                if first_col <= col <= last_col and any(
                        _header_label(parent + labels[col]) in names for names in HEADERS.values()):
                    labels[col] = parent + labels[col]
    return _columns({col: {'raw': raw} for col, raw in labels.items()})


def _prove_formula_columns(details: list[dict], columns: dict) -> bool:
    """Require every native amount formula to prove the same exact mapping."""
    if not details:
        return False
    for row in details:
        formula = PRODUCT.fullmatch(row["cells"].get(columns["amount"], {}).get("raw", ""))
        if formula is None:
            return False
        first_col, first_row, second_col, second_row = formula.groups()
        if (int(first_row) != row["index"] or int(second_row) != row["index"]
                or {_column_number(first_col), _column_number(second_col)}
                != {columns["quantity"], columns["price"]}):
            return False
        operands = {kind: _cell_operand(row, column) for kind, column in columns.items()}
        if any(value is None for value in operands.values()):
            return False
        values = {kind: Decimal(value["number"].replace(",", "").replace("，", ""))
                  for kind, value in operands.items()}
        # Decimal's default precision must not round a mismatch into proof.
        with localcontext() as context:
            context.prec = max(28, sum(len(values[kind].as_tuple().digits)
                                       for kind in ("quantity", "price")))
            if values["quantity"] * values["price"] != values["amount"]:
                return False
    return True


def _cell_operand(row: dict, column: int) -> dict | None:
    cell = row["cells"].get(column)
    if cell is None:
        return None
    raw = cell["raw"]
    if raw.startswith("公式"):
        if "；缓存值=" not in raw:
            return None
        raw = raw.split("；缓存值=", 1)[1]
    raw = raw.strip()
    if not NUMBER.fullmatch(raw):
        return None
    try:
        if not Decimal(raw.replace(",", "").replace("，", "")).is_finite():
            return None
    except InvalidOperation:
        return None
    return {"unit_id": row["unit_id"], "quote": cell["quote"], "number": raw}


def is_binary_formula_cache_difference(item: dict, units: list[dict]) -> bool:
    """Explain a proven native formula representation; never relax comparison.

    Exact IEEE result equality, not a money epsilon. Literal amounts, unmatched
    formulas, wrong rows or materially wrong caches do not enter this category.
    Original Decimal values, matches/status and complete calculations stay intact.
    """
    if item.get('operation') != 'product' or len(item.get('right', [])) != 2:
        return False
    uid = item['left']['unit_id']
    unit = next((u for u in units if u['unit_id'] == uid), {})
    if unit.get('image') or any(t['unit_id'] != uid for t in item['right']):
        return False
    for fact in unit.get('native_facts', []):
        match = ROW.fullmatch(fact)
        if match is None:
            continue
        parsed = parse_cells(match[3])
        if parsed is None:
            continue
        cells = {col: c['raw'] for col, c in parsed.items()}
        left_columns = [col for col, c in parsed.items() if item['left']['quote'] == c['quote']]
        if len(left_columns) != 1:
            continue
        formula = PRODUCT.fullmatch(cells[left_columns[0]])
        if not formula:
            continue
        col1, row1, col2, row2 = formula.groups()
        if int(row1) != int(match[2]) or int(row2) != int(match[2]):
            continue
        columns = {_column_number(col1), _column_number(col2)}
        quoted_columns = {col for term in item['right'] for col, c in parsed.items()
                          if term['quote'] == c['quote']}
        if columns != quoted_columns:
            continue
        try:
            def number(raw):
                return Decimal(str(raw).replace(',', '').replace('，', ''))
            left = number(item['left_value'])
            factors = [number(t['number']) for t in item['right']]
            with localcontext() as context:
                context.prec = max(28, sum(len(v.as_tuple().digits) for v in factors))
                exact = factors[0] * factors[1]
            binary = float(factors[0]) * float(factors[1])
            return (math.isfinite(binary) and float(left) == binary and left != exact
                    and number(item['right_value']) == exact)
        except (ValueError, InvalidOperation, OverflowError):
            return False
    return False


def _proved_unlabelled_total(row: dict, details: list[dict], columns: dict, last_index: int) -> bool:
    """Recognize only a final two-number total proven by every native detail.

    A partial/incorrect or unidentified numeric row remains unknown; it is never
    silently dropped as a total. No rounding or tolerance supplies the proof.
    """
    populated = {col for col, cell in row["cells"].items() if cell["raw"].strip()}
    if (row["index"] != last_index or not details
            or populated != {columns["quantity"], columns["amount"]}):
        return False
    values = {key: [_cell_operand(detail, columns[key]) for detail in details]
              for key in ("quantity", "amount")}
    totals = {key: _cell_operand(row, columns[key]) for key in values}
    if any(v is None for group in values.values() for v in group) or any(v is None for v in totals.values()):
        return False
    text = [v["number"].replace(",", "").replace("，", "") for group in values.values() for v in group]
    text += [v["number"].replace(",", "").replace("，", "") for v in totals.values()]
    with localcontext() as context:
        context.prec = max(28, sum(len(v) for v in text) + 10)
        sums = {key: sum((Decimal(v["number"].replace(",", "").replace("，", "")) for v in group), Decimal(0))
                for key, group in values.items()}
        actual = {key: Decimal(v["number"].replace(",", "").replace("，", "")) for key, v in totals.items()}
        return actual["quantity"] == sums["quantity"] and actual["amount"] >= sums["amount"]


def _column_number(letters: str) -> int:
    number = 0
    for letter in letters.upper():
        number = number * 26 + ord(letter) - ord("A") + 1
    return number


def _inline_amount_total(row: dict) -> dict | None:
    """Recognize a whole, single-cell currency total, never prose or a subtotal."""
    populated = [cell for cell in row["cells"].values() if cell["raw"].strip()]
    if len(populated) != 1:
        return None
    cell = populated[0]
    matched = INLINE_AMOUNT_TOTAL.fullmatch(cell["raw"].strip())
    if matched is None:
        return None
    return {"unit_id": row["unit_id"], "quote": cell["quote"], "number": matched[1]}


def prepare_pos_arithmetic(documents: list[dict], materials: list[dict] | None,
                           units: list[dict] | None = None, *, amount_only: bool = False,
                           scenario: str | None = None, check_progress=None) -> dict | None:
    """Return a source-bound plan, or None for older non-native evidence."""
    if materials is None:
        return None
    check_quantity_total = not amount_only and scenario != 'pos_target_incentive'
    ids = {uid for material in materials if material["id"] == "pos_excel"
           and material["state"] == "present" for uid in material["source_ids"]}
    metadata = {u["unit_id"]: u for u in units or []}
    groups = defaultdict(dict)
    source_ids, limitations, unread_sources = [], [], []
    for document in documents:
        uid = document["unit_id"]
        if uid not in ids:
            continue
        meta = metadata.get(uid, {})
        unread = 0
        for fact in document["facts"]:
            if check_progress is not None:
                check_progress()
            matched = ROW.fullmatch(fact)
            if not matched:
                unread += 1
                continue
            sheet, index, content = matched.groups()
            key = (meta.get("file_id", uid), sheet)
            cells = parse_cells(content)
            if cells is None:
                unread += 1
                continue
            row = {"unit_id": uid, "index": int(index), "cells": cells, 'sheet_layout': meta.get('sheet_layout')}
            if int(index) in groups[key]:
                limitations.append(f"{sheet}第{index}行出现重复来源，无法确定完整明细范围。")
            groups[key][int(index)] = row
            if uid not in source_ids:
                source_ids.append(uid)
        if unread or not document["facts"]:
            unread_sources.append(meta.get("source_file") or uid)
    if not groups:
        return None
    for source in dict.fromkeys(unread_sources):
        limitations.append(f"{source}存在无法按原始工作表行列读取的记录，不能确认全部销售明细范围。")
    comparisons, tables = [], []
    for group_key, rows in groups.items():
        _, sheet = group_key
        last_index = max(rows)
        columns, details, totals, blocks = None, [], [], []
        formula_mapping = False
        for row in sorted(rows.values(), key=lambda r: r["index"]):
            if check_progress is not None:
                check_progress()
            header = _columns(row["cells"], amount_only=amount_only)
            if header is None and not amount_only:
                header = _merged_header_columns(row, rows, row.get('sheet_layout'))
            candidate = False
            if header is None and not amount_only:
                header = _formula_column_candidate(row["cells"])
                candidate = header is not None
            if header:
                # A repeated header within a paginated table does not discard
                # preceding detail rows. A new header after totals starts a block.
                if columns and (header != columns or candidate != formula_mapping or totals):
                    blocks.append((columns, details, totals, formula_mapping))
                    details, totals = [], []
                columns = header
                formula_mapping = candidate
                continue
            if columns is None:
                continue
            raw_values = [c["raw"].strip() for c in row["cells"].values()]
            is_total = _inline_amount_total(row) is not None or any(TOTAL.fullmatch(v) for v in raw_values) or any(
                SUM.match(row["cells"].get(columns[k], {}).get("raw", "")) for k in columns) or (
                not amount_only and not totals and _proved_unlabelled_total(row, details, columns, last_index))
            if is_total:
                totals.append(row)
            elif totals and [value for value in raw_values if value] == ["."]:
                # A lone trailing dot is not a product or a numeric operand.
                # Keep it in the native rows so a SUM including it stays unknown.
                continue
            elif any(col in row["cells"] for col in columns.values()):
                if totals:
                    limitations.append(f"{sheet}合计之后仍有销售明细，无法将合计视为完整范围。")
                details.append(row)
        if columns:
            blocks.append((columns, details, totals, formula_mapping))
        else:
            limitations.append(f"{sheet}未能确认销售金额列。" if amount_only else f"{sheet}未能确认数量、销售单价和金额列的完整对应关系。")
        for columns, details, totals, formula_mapping in blocks:
            if not details:
                limitations.append(f"{sheet}未读到可完整复算的销售明细。")
                continue
            if formula_mapping and not _prove_formula_columns(details, columns):
                limitations.append(f"{sheet}未能由全部明细的同一行原生乘法公式及精确缓存值确认销售数量、单价因子和销售总计列，不能按表头猜测单价。")
                continue
            table = {"sheet": sheet, "detail_rows": len(details), "first_row": details[0]["index"],
                     "last_row": details[-1]["index"], "total_rows": [r["index"] for r in totals]}
            if formula_mapping:
                table["column_basis"] = "all_native_row_products_exact"
            tables.append(table)
            for row in details:
                if amount_only:
                    continue
                values = {k: _cell_operand(row, col) for k, col in columns.items()}
                if any(v is None for v in values.values()):
                    limitations.append(f"{sheet}第{row['index']}行数量、单价或金额缺少可读取的原始数值或公式缓存。")
                    continue
                comparisons.append({"label": f"{sheet}第{row['index']}行销售金额", "value_kind": "amount",
                                    "left": values["amount"], "right": [values["quantity"], values["price"]],
                                    "operation": "product", "operator": "ge"})
            if not totals:
                limitations.append(f"{sheet}未找到销售数量及金额合计，无法完成两项合计复算。" if check_quantity_total else f"{sheet}未找到销售金额合计。")
            for total in totals:
                if check_progress is not None:
                    check_progress()
                inline_amount = _inline_amount_total(total)
                if inline_amount is not None:
                    # This alternate layout is accepted only as the sole final
                    # total of one complete, contiguous native detail block.
                    if (len(blocks) != 1 or len(totals) != 1
                            or total["index"] != last_index
                            or len(details) != total["index"] - details[0]["index"]
                            or any(row["index"] != details[0]["index"] + offset
                                   for offset, row in enumerate(details))):
                        limitations.append(f"{sheet}第{total['index']}行单元格金额合计不是唯一末行或明细范围不完整，无法确认全表合计。")
                        continue
                    terms = [_cell_operand(row, columns["amount"]) for row in details]
                    if any(term is None for term in terms):
                        limitations.append(f"{sheet}第{total['index']}行金额合计对应明细缺少可读取的原始数值。")
                    else:
                        comparisons.append({"label": f"{sheet}第{total['index']}行销售金额合计（全部{len(details)}行）",
                                            "value_kind": "amount", "left": inline_amount, "right": terms,
                                            "operation": "sum", "operator": "ge"})
                    if check_quantity_total:
                        limitations.append(f"{sheet}未找到销售数量合计，无法完成数量合计复算。")
                    continue
                for kind in (("quantity", "amount") if check_quantity_total else ("amount",)):
                    col = columns[kind]
                    left = _cell_operand(total, col)
                    raw = total["cells"].get(col, {}).get("raw", "")
                    formula = SUM.match(raw)
                    selected = details
                    if formula:
                        first_col, first, last_col, last = formula.groups()
                        first, last = int(first), int(last)
                        if _column_number(first_col) != col or _column_number(last_col) != col or first > last:
                            limitations.append(f"{sheet}第{total['index']}行合计公式并非对应销售列的连续明细范围。")
                            continue
                        selected = [r for r in details if first <= r["index"] <= last]
                        # Native facts omit empty rows, but never omit populated
                        # rows. A range containing another total is not a detail sum.
                        if (not selected or any(first <= r["index"] <= last for r in totals)
                                or {r["index"] for r in selected} != {i for i in rows if first <= i <= last}):
                            limitations.append(f"{sheet}第{total['index']}行合计范围含有非明细或未能读取的行。")
                            continue
                        if len(selected) != len(details):
                            limitations.append(f"{sheet}第{total['index']}行公式未覆盖本表全部销售明细，不能用局部合计替代总计。")
                            continue
                    elif len(totals) != 1 or raw.startswith("公式"):
                        limitations.append(f"{sheet}第{total['index']}行合计的对应明细范围无法完整确认。")
                        continue
                    terms = [_cell_operand(r, col) for r in selected]
                    if left is None or any(term is None for term in terms):
                        limitations.append(f"{sheet}第{total['index']}行合计或对应明细缺少可读取的原始数值。")
                        continue
                    comparisons.append({"label": f"{sheet}第{total['index']}行销售{'数量' if kind == 'quantity' else '金额'}合计（全部{len(selected)}行）",
                                        "value_kind": kind, "left": left, "right": terms,
                                        "operation": "sum", "operator": "eq" if kind == "quantity" else "ge"})
    if not tables and not limitations:
        limitations.append("POS Excel中数量、销售单价和金额列的对应关系未能完整确认，不能用部分行推算整表。")
    return {"complete": bool(tables) and not limitations, "tables": tables,
            "source_ids": source_ids, "comparisons": comparisons,
            "limitations": list(dict.fromkeys(limitations))}
