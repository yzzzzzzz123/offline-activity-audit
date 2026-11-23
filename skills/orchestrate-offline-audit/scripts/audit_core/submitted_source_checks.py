"""Limited checks on received sources, including when another material is missing.

No network, inferred prices, guessed dates, or gate bypass. Qince rows must have
already been linked by context_sources; only personnel incentive uses the new
application-period comparison requested for HX202610230029.
"""
from __future__ import annotations

from collections import defaultdict
from .spreadsheet_rows import parse_cells
from datetime import date
import json
from pathlib import PurePosixPath
import re

from .pos_arithmetic import (ROW, CELL, HEADERS, NUMBER, TOTAL, _header_label,
                             _columns, _formula_column_candidate, _prove_formula_columns)
from .price_support_dates import _ACTIVITY_LABEL, _period, _uncertain_or_other_context
from .pdf_policy import SUBMITTED_SOURCE_CHECKS
from .activity_periods import case_activity_periods, period_problem


def _check(rule_id, reason, sources, *, status="fail"):
    return {"rule_id": rule_id, "status": status, "reason": reason,
            "source_ids": sorted(set(sources)), "comparisons": [], "calculations": [],
            "effect": "", "requirement": SUBMITTED_SOURCE_CHECKS[rule_id]["requirement"]}


def _roles(materials, role):
    return {uid for item in materials if item["id"] == role
            and item["state"] in {"present", "unclear"} for uid in item["source_ids"]}


def _name(unit):
    return PurePosixPath(str(unit.get("source_file", unit["unit_id"])).replace("\\", "/")).name


def _json_fields(document):
    result = {}
    for fact in document["facts"]:
        key, separator, raw = fact.partition("=")
        if not separator:
            continue
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if key in result and result[key] != value:
            return {}  # Conflicting native facts cannot establish an application.
        result[key] = value
    return result


def _iso_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[ T].*)?", value):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def application_period_issues(scenario, documents, materials, units):
    if scenario != "personnel_incentive":
        return []
    by_id = {unit["unit_id"]: unit for unit in units}
    facts = {document["unit_id"]: document["facts"] for document in documents}
    applications = []
    for document in documents:
        unit = by_id.get(document["unit_id"], {})
        if unit.get("source_kind") != "qince" or unit.get("source_file") != "勤策申请明细":
            continue
        fields = _json_fields(document)
        if not fields.get("applyCode") or not fields.get("subId"):
            continue
        start, end = _iso_date(fields.get("startDate")), _iso_date(fields.get("endDate"))
        if start is None or end is None or end < start:
            continue
        applications.append((document["unit_id"], (start, end)))
    if not applications:
        return []  # An unavailable optional source is not a new missing material.
    application_ids = {uid for uid, _ in applications}
    periods = {period for _, period in applications}
    if len(periods) != 1:
        return [_check("application_activity_dates",
                       "本单关联的勤策申请明细存在多个活动周期，暂时不能确认与本次合同、结算单的对应关系。",
                       application_ids | _roles(materials, "promotion_contract") | _roles(materials, "settlement"),
                       status="unknown")]
    application = next(iter(periods))
    readings = case_activity_periods(facts, materials)
    issues = []
    for role, label in (("promotion_contract", "促销合同"), ("settlement", "结算单")):
        ids = _roles(materials, role) & facts.keys()
        if not ids:
            continue  # Existing gate reports missing documents.
        reading = readings[role]
        sources = ids | application_ids | {s['unit_id'] for p in reading['periods'] for s in p['sources']}
        observed = [tuple(date.fromisoformat(v) for v in period) for period in reading['coverage']]
        if reading['state'] != 'available':
            issues.append(_check("application_activity_dates",
                period_problem(label, reading),
                sources, status="unknown"))
        elif observed != [application]:
            issues.append(_check("application_activity_dates",
                f"{label}执行周期" + '、'.join(f'{a}至{b}' for a,b in observed)
                + f"与本单勤策活动申请的{application[0]}至{application[1]}不一致。",
                sources))
    return issues


_PRODUCT_HEADERS = {"商品名称", "产品名称", "品名", "名称", "SKU名称"}
_SKU_HEADERS = {"商品SKU", "SKU", "商品编码", "商品条码", "条码", "69码", "商品SKU/编码", "SKU/编码"}
_PRICES = HEADERS["price"] | {"SKU单价", "商品单价", "售价"}
_EMPTY = {"", "-", "—", "无", "未填"}


def _formula_price(rows, index, columns):
    """Reuse existing all-row native formula proof; never divide amount by quantity."""
    details = []
    for number, (uid, cells, fact) in sorted(rows.items()):
        if number <= index:
            continue
        match = ROW.fullmatch(fact)
        native = parse_cells(match[3]) if match else None
        if native is None:
            return None
        if _columns(native) or _formula_column_candidate(native) or any(TOTAL.fullmatch(raw.strip()) for raw in cells.values()):
            break
        if any(col in cells for col in columns.values()):
            details.append({"unit_id": uid, "index": number, "cells": native, "fact": fact})
    return columns["price"] if _prove_formula_columns(details, columns) else None


def pos_unit_price_issues(flags, documents, materials, units):
    if flags.get("uses_pos") is not True:
        return []
    ids = _roles(materials, "pos_excel")
    groups = defaultdict(dict)
    for unit in units:
        if unit["unit_id"] not in ids or unit.get("image") or unit.get("limitations"):
            continue
        # Native cell facts, never model prose or a stamped/PDF substitute.
        for fact in unit.get("native_facts", []):
            match = ROW.fullmatch(fact)
            if not match:
                continue
            parsed = parse_cells(match[3])
            if parsed is None:
                continue
            cells = {col: cell['raw'] for col, cell in parsed.items()}
            row = (unit["unit_id"], cells, fact)
            key = (unit["source_file"], match[1])
            previous = groups[key].get(int(match[2]))
            if previous and previous[1] != cells:
                groups[key][int(match[2])] = (None, {}, "")
            elif previous is None:
                groups[key][int(match[2])] = row
    issues = []
    for (filename, sheet), rows in groups.items():
        if any(uid is None for uid, _, _ in rows.values()):
            continue
        header = None
        for row_index, (uid, cells, _) in sorted(rows.items()):
            labels = {col: _header_label(value) for col, value in cells.items()}
            names = [col for col, value in labels.items() if value in _PRODUCT_HEADERS]
            skus = [col for col, value in labels.items() if value.upper() in _SKU_HEADERS]
            quantities = [col for col, value in labels.items()
                          if re.sub(r"^求和项[:：]", "", value) in HEADERS["quantity"]]
            amounts = [col for col, value in labels.items()
                       if re.sub(r"^求和项[:：]", "", value) in HEADERS["amount"]]
            if names and skus and quantities and amounts:
                prices = [col for col, value in labels.items() if value in _PRICES]
                candidate = _formula_column_candidate({col: {"raw": raw} for col, raw in cells.items()})
                if not prices and candidate:
                    proven = _formula_price(rows, row_index, candidate)
                    if proven is not None:
                        prices = [proven]
                header = (names[0], skus[0], quantities[0], amounts[0], prices)
                if not prices:
                    ambiguous = candidate is not None or any("价" in value and not re.search("补差|奖励|进货|采购", value)
                                    for value in labels.values())
                    issues.append(_check("pos_fields",
                        f"《{_name({'source_file': filename, 'unit_id': uid})}》{sheet}第{row_index}行表头"
                        + ("的价格字段含义不能确认，暂时不能确认是否提供SKU销售单价。" if ambiguous else
                           "缺少SKU销售单价列；销售数量、销售金额不能替代单价。"),
                        [uid], status="unknown" if ambiguous else "fail"))
                continue
            if header is None:
                continue
            name_col, sku_col, quantity_col, amount_col, prices = header
            if (not prices or not cells.get(name_col, "").strip() or not cells.get(sku_col, "").strip()
                    or not NUMBER.fullmatch(cells.get(quantity_col, "").strip())
                    or not NUMBER.fullmatch(cells.get(amount_col, "").strip())):
                continue
            if all(cells.get(col, "").strip() in _EMPTY for col in prices):
                issues.append(_check("pos_fields",
                    f"《{_name({'source_file': filename, 'unit_id': uid})}》{sheet}第{row_index}行"
                    f"商品{cells[name_col]}（{cells[sku_col]}）未填写销售单价。", [uid]))
    return issues


def submitted_source_checks(scenario, flags, documents, materials, units):
    """Only observable issues; never treat absence of a finding as approval."""
    return (application_period_issues(scenario, documents, materials, units)
            + pos_unit_price_issues(flags, documents, materials, units))
