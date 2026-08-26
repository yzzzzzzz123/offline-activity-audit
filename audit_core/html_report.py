from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from .common import AuditError


SHEET_SCENARIOS = {
    "人员激励核销": "personnel_incentive",
    "堆头核销": "promotional_display",
}
SCENARIO_SHEETS = {value: key for key, value in SHEET_SCENARIOS.items()}
SUMMARY_PREFIXES = (
    "合计",
    "总计",
    "实际申报",
    "收款人与日期",
    "收款人/日期",
)
FORBIDDEN_VISIBLE_TERMS = (
    "详见审计JSON",
    "见内部结果",
    "候选命中",
    "商品ID",
    "视觉RAG",
    "唯一收敛",
)
DATA_OPEN = '<script id="audit-data" type="application/json">'
DATA_CLOSE = "</script>"


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\r\n", "\n").replace("\r", "\n").strip()


def _row_kind(values: list[str]) -> str:
    first = values[0].lstrip() if values else ""
    if first.startswith(SUMMARY_PREFIXES):
        return "summary"
    return "record"


def _row_status(values: list[str], kind: str) -> str:
    if kind == "summary":
        return "summary"
    conclusion = values[5] if len(values) > 5 else ""
    if (
        "要重新提交：不用" in conclusion
        or "要重新提交什么：不用" in conclusion
    ) and not any(
        token in conclusion for token in ("暂不能核销", "完全不匹配", "需补")
    ):
        return "pass"
    return "issue"


def _row_confidence(values: list[str]) -> str:
    text = "\n".join(values)
    if "置信度：低" in text:
        return "low"
    if "置信度：中" in text:
        return "medium"
    if "置信度：高" in text:
        return "high"
    return "not_applicable"


def _display_store_heading(values: list[str]) -> str | None:
    contract_text = values[0] if values else ""
    numbered_store = re.search(r"第\s*\d+\s*家[：:]\s*([^\n；]+)", contract_text)
    if numbered_store:
        return numbered_store.group(1).strip()
    current_store = re.search(
        r"本店(?!堆头)(?:[：:]\s*)?([^\n；]+)",
        contract_text,
    )
    if current_store:
        return current_store.group(1).strip()
    photo_text = values[1] if len(values) > 1 else ""
    visible_location = re.search(r"识别地点[：:]\s*([^\n]+)", photo_text)
    if visible_location:
        return visible_location.group(1).strip()
    return None


def _row_heading(
    values: list[str],
    row_number: int,
    kind: str,
    scenario: str,
) -> str:
    if kind == "summary":
        first = values[0] if values else ""
        for line in first.splitlines():
            label = line.strip()
            if label:
                return label[:90]
        return "汇总"
    if scenario == "promotional_display":
        store = _display_store_heading(values)
        if store:
            return store[:90]
    first = values[0] if values else ""
    if scenario == "personnel_incentive":
        title_lines = [line.strip() for line in first.splitlines() if line.strip()]
        if len(title_lines) >= 2 and title_lines[0].startswith("结算第"):
            return f"{title_lines[0]} · {title_lines[1]}"
    for line in first.splitlines():
        label = line.strip()
        if label:
            return label[:90]
    conclusion = values[5] if len(values) > 5 else ""
    for line in conclusion.splitlines():
        label = line.strip()
        if label:
            return label[:90]
    return f"核销记录 {row_number - 3}"


def _workbook_payload(workbook_path: str | Path) -> dict[str, Any]:
    source = Path(workbook_path)
    if not source.is_file():
        raise AuditError(f"找不到待生成HTML的工作簿：{source}")

    workbook = load_workbook(source, read_only=True, data_only=False)
    try:
        unknown = set(workbook.sheetnames) - set(SHEET_SCENARIOS)
        if unknown:
            raise AuditError("HTML不支持的工作表：" + "、".join(sorted(unknown)))
        if not workbook.sheetnames:
            raise AuditError("工作簿没有可展示的工作表")

        sheets: list[dict[str, Any]] = []
        for worksheet in workbook.worksheets:
            headers = [_cell_text(worksheet.cell(3, column).value) for column in range(1, 7)]
            if len(headers) != 6 or any(not header for header in headers):
                raise AuditError(f"{worksheet.title} 缺少完整六列表头，不能生成HTML")

            rows: list[dict[str, Any]] = []
            for row_number in range(4, worksheet.max_row + 1):
                values = [
                    _cell_text(worksheet.cell(row_number, column).value)
                    for column in range(1, 7)
                ]
                if not any(values):
                    continue
                kind = _row_kind(values)
                rows.append(
                    {
                        "excel_row": row_number,
                        "kind": kind,
                        "status": _row_status(values, kind),
                        "confidence": _row_confidence(values),
                        "heading": _row_heading(
                            values,
                            row_number,
                            kind,
                            SHEET_SCENARIOS[worksheet.title],
                        ),
                        "values": values,
                    }
                )

            sheets.append(
                {
                    "name": worksheet.title,
                    "scenario": SHEET_SCENARIOS[worksheet.title],
                    "title": _cell_text(worksheet["A1"].value) or worksheet.title,
                    "note": _cell_text(worksheet["A2"].value),
                    "headers": headers,
                    "rows": rows,
                }
            )
    finally:
        workbook.close()

    return {
        "schema_version": "1.0",
        "title": "线下活动核销结果",
        "sheets": sheets,
    }


def _json_for_script(payload: dict[str, Any]) -> str:
    return (
        json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


HTML_TEMPLATE = r'''<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <meta name="color-scheme" content="light">
  <title>线下活动核销结果 · 本地查看页</title>
  <style>
    :root {
      --ink: #17201b;
      --ink-soft: #4f5a53;
      --paper: #f4efe4;
      --paper-bright: #fffdf7;
      --line: #d8d0c0;
      --line-strong: #afa592;
      --accent: #c74f1c;
      --accent-dark: #92350f;
      --moss: #2e6a50;
      --moss-soft: #e0eee6;
      --warn: #a7472b;
      --warn-soft: #f7e3d9;
      --gold: #b47a20;
      --gold-soft: #f5ebcf;
      --shadow: 0 18px 60px rgba(45, 38, 26, .10);
      --radius: 20px;
    }

    * { box-sizing: border-box; }
    html { scroll-behavior: smooth; }
    body {
      margin: 0;
      min-width: 320px;
      color: var(--ink);
      background:
        radial-gradient(circle at 14% 8%, rgba(199, 79, 28, .09), transparent 26rem),
        radial-gradient(circle at 92% 18%, rgba(46, 106, 80, .09), transparent 30rem),
        repeating-linear-gradient(0deg, rgba(41, 35, 23, .018) 0, rgba(41, 35, 23, .018) 1px, transparent 1px, transparent 5px),
        var(--paper);
      font-family: "Microsoft YaHei UI", "Noto Sans CJK SC", "PingFang SC", sans-serif;
      line-height: 1.65;
    }

    button, input, a { font: inherit; }
    button, a { -webkit-tap-highlight-color: transparent; }
    button:focus-visible, input:focus-visible, a:focus-visible, summary:focus-visible {
      outline: 3px solid rgba(199, 79, 28, .28);
      outline-offset: 3px;
    }

    .masthead {
      position: relative;
      overflow: hidden;
      color: #fffaf0;
      background: #17201b;
      border-bottom: 1px solid rgba(255,255,255,.12);
    }
    .masthead::before {
      content: "核";
      position: absolute;
      right: clamp(12px, 8vw, 130px);
      top: -72px;
      color: rgba(255,255,255,.045);
      font-family: STKaiti, KaiTi, serif;
      font-size: clamp(220px, 30vw, 480px);
      line-height: 1;
      transform: rotate(-8deg);
      pointer-events: none;
    }
    .masthead-inner {
      position: relative;
      width: min(1480px, calc(100% - 40px));
      margin: 0 auto;
      padding: 42px 0 58px;
    }
    .eyebrow {
      display: flex;
      align-items: center;
      gap: 10px;
      margin-bottom: 18px;
      color: #e9c59d;
      font-size: 12px;
      font-weight: 800;
      letter-spacing: .18em;
      text-transform: uppercase;
    }
    .eyebrow::before { content: ""; width: 38px; height: 2px; background: var(--accent); }
    h1 {
      max-width: 900px;
      margin: 0;
      font-family: STKaiti, KaiTi, "Microsoft YaHei UI", sans-serif;
      font-size: clamp(40px, 6vw, 78px);
      font-weight: 700;
      line-height: 1.08;
      letter-spacing: -.04em;
    }
    .masthead-meta {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
      margin-top: 24px;
      color: #ced7d1;
      font-size: 14px;
    }
    .offline-badge {
      display: inline-flex;
      align-items: center;
      gap: 8px;
      padding: 7px 12px;
      color: #dff3e7;
      background: rgba(80, 165, 116, .16);
      border: 1px solid rgba(148, 214, 176, .3);
      border-radius: 999px;
      font-weight: 800;
    }
    .offline-badge::before { content: ""; width: 8px; height: 8px; background: #7fd39f; border-radius: 50%; box-shadow: 0 0 0 4px rgba(127, 211, 159, .12); }

    .shell {
      width: min(1480px, calc(100% - 40px));
      margin: -26px auto 64px;
      position: relative;
    }
    .scenario-tabs {
      display: flex;
      gap: 8px;
      padding: 8px;
      width: fit-content;
      max-width: 100%;
      overflow-x: auto;
      background: rgba(255,253,247,.96);
      border: 1px solid rgba(216,208,192,.9);
      border-radius: 16px;
      box-shadow: var(--shadow);
    }
    .tab-button {
      border: 0;
      padding: 12px 18px;
      color: var(--ink-soft);
      background: transparent;
      border-radius: 11px;
      cursor: pointer;
      font-weight: 800;
      white-space: nowrap;
      transition: background .18s ease, color .18s ease, transform .18s ease;
    }
    .tab-button:hover { transform: translateY(-1px); color: var(--ink); }
    .tab-button[aria-selected="true"] { color: white; background: var(--ink); box-shadow: 0 8px 24px rgba(23,32,27,.18); }

    .sheet-intro {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 24px;
      align-items: end;
      margin: 36px 0 22px;
    }
    .sheet-kicker { color: var(--accent); font-size: 12px; font-weight: 900; letter-spacing: .16em; }
    .sheet-intro h2 {
      margin: 3px 0 8px;
      font-family: STKaiti, KaiTi, "Microsoft YaHei UI", sans-serif;
      font-size: clamp(30px, 4vw, 48px);
      line-height: 1.18;
    }
    .sheet-note { max-width: 950px; margin: 0; color: var(--ink-soft); font-size: 14px; }
    .excel-link {
      display: inline-flex;
      align-items: center;
      gap: 9px;
      min-height: 44px;
      padding: 10px 16px;
      color: var(--paper-bright);
      text-decoration: none;
      background: var(--moss);
      border: 1px solid var(--moss);
      border-radius: 12px;
      font-weight: 850;
      box-shadow: 0 10px 24px rgba(46,106,80,.16);
      transition: transform .18s ease, box-shadow .18s ease;
    }
    .excel-link:hover { transform: translateY(-2px); box-shadow: 0 14px 28px rgba(46,106,80,.22); }

    .summary-grid {
      display: grid;
      grid-template-columns: repeat(4, minmax(150px, 1fr));
      gap: 12px;
      margin-bottom: 16px;
    }
    .metric {
      min-height: 126px;
      padding: 18px 20px;
      background: rgba(255,253,247,.9);
      border: 1px solid var(--line);
      border-radius: 16px;
      box-shadow: 0 8px 30px rgba(45,38,26,.05);
      animation: rise .42s both;
    }
    .metric:nth-child(2) { animation-delay: .05s; }
    .metric:nth-child(3) { animation-delay: .1s; }
    .metric:nth-child(4) { animation-delay: .15s; }
    .metric-label { display: block; color: var(--ink-soft); font-size: 13px; font-weight: 750; }
    .metric-value { display: block; margin-top: 8px; font-family: Georgia, "Times New Roman", serif; font-size: 42px; line-height: 1; font-weight: 700; }
    .metric-hint { display: block; margin-top: 9px; color: #7f776a; font-size: 12px; }
    .metric.pass .metric-value { color: var(--moss); }
    .metric.issue .metric-value { color: var(--warn); }

    .toolbar {
      position: sticky;
      top: 10px;
      z-index: 20;
      display: grid;
      grid-template-columns: minmax(240px, 1fr) auto auto;
      gap: 10px;
      align-items: center;
      padding: 12px;
      margin-bottom: 16px;
      background: rgba(255,253,247,.91);
      border: 1px solid rgba(216,208,192,.95);
      border-radius: 16px;
      box-shadow: 0 12px 38px rgba(45,38,26,.10);
      backdrop-filter: blur(14px);
    }
    .search-wrap { position: relative; min-width: 0; }
    .search-wrap::before { content: "⌕"; position: absolute; left: 14px; top: 50%; transform: translateY(-53%); color: #7a7265; font-size: 24px; }
    .search-input {
      width: 100%;
      height: 44px;
      padding: 0 42px 0 44px;
      color: var(--ink);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 11px;
      outline: none;
    }
    .search-input:focus { border-color: var(--accent); box-shadow: 0 0 0 3px rgba(199,79,28,.11); }
    .clear-search {
      position: absolute;
      right: 8px;
      top: 6px;
      width: 32px;
      height: 32px;
      padding: 0;
      border: 0;
      color: #756d61;
      background: transparent;
      border-radius: 8px;
      cursor: pointer;
      font-size: 18px;
    }
    .clear-search[hidden] { display: none; }
    .segmented, .action-group { display: flex; align-items: center; gap: 6px; }
    .filter-button, .tool-button {
      min-height: 40px;
      padding: 8px 12px;
      color: var(--ink-soft);
      background: #fff;
      border: 1px solid var(--line);
      border-radius: 10px;
      cursor: pointer;
      font-size: 13px;
      font-weight: 800;
      white-space: nowrap;
      transition: border-color .18s ease, color .18s ease, background .18s ease;
    }
    .filter-button:hover, .tool-button:hover { color: var(--ink); border-color: var(--line-strong); }
    .filter-button[aria-pressed="true"] { color: #fff; background: var(--accent); border-color: var(--accent); }
    .tool-button.primary { color: #fff; background: var(--ink); border-color: var(--ink); }

    .results-line {
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
      min-height: 34px;
      margin: 0 4px 10px;
      color: var(--ink-soft);
      font-size: 13px;
    }
    .results-line strong { color: var(--ink); }

    .records { display: grid; gap: 12px; }
    .record-card {
      background: rgba(255,253,247,.96);
      border: 1px solid var(--line);
      border-left: 5px solid var(--line-strong);
      border-radius: var(--radius);
      box-shadow: 0 10px 38px rgba(45,38,26,.055);
      overflow: clip;
      animation: rise .34s both;
    }
    .record-card.pass { border-left-color: var(--moss); }
    .record-card.issue { border-left-color: var(--warn); }
    .record-card.summary { border-left-color: var(--gold); background: #fff9e9; }
    .record-card details > summary {
      display: grid;
      grid-template-columns: 48px minmax(0, 1fr) auto 24px;
      gap: 14px;
      align-items: center;
      min-height: 104px;
      padding: 18px 22px;
      cursor: pointer;
      list-style: none;
    }
    .record-card summary::-webkit-details-marker { display: none; }
    .record-number {
      display: grid;
      place-items: center;
      width: 42px;
      height: 42px;
      color: #fff;
      background: var(--ink);
      border-radius: 50%;
      font-family: Georgia, serif;
      font-size: 14px;
      font-weight: 700;
    }
    .record-card.summary .record-number { background: var(--gold); }
    .record-title { min-width: 0; }
    .record-title h3 { margin: 0 0 5px; font-size: 17px; line-height: 1.4; }
    .record-title p {
      display: -webkit-box;
      margin: 0;
      overflow: hidden;
      color: var(--ink-soft);
      font-size: 13px;
      -webkit-line-clamp: 2;
      -webkit-box-orient: vertical;
    }
    .status-pill {
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-width: 76px;
      padding: 7px 11px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 900;
    }
    .status-pill.pass { color: #245840; background: var(--moss-soft); }
    .status-pill.issue { color: #86341e; background: var(--warn-soft); }
    .status-pill.summary { color: #80580f; background: var(--gold-soft); }
    .chevron { color: #796f61; font-size: 20px; transition: transform .2s ease; }
    details[open] .chevron { transform: rotate(180deg); }

    .record-body { padding: 0 22px 22px; border-top: 1px solid var(--line); }
    .evidence-grid {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
      padding-top: 18px;
    }
    .evidence-cell {
      min-width: 0;
      padding: 16px;
      background: #fff;
      border: 1px solid #e5dece;
      border-radius: 13px;
    }
    .evidence-label {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 11px;
      padding-bottom: 9px;
      border-bottom: 1px solid #eee7da;
    }
    .evidence-label strong { font-size: 13px; }
    .evidence-label small { overflow: hidden; color: #82786a; text-overflow: ellipsis; white-space: nowrap; }
    .cell-body { color: #374139; font-size: 13px; word-break: break-word; }
    .cell-line { margin: 0 0 6px; white-space: pre-wrap; }
    .cell-line:last-child { margin-bottom: 0; }
    .cell-line b { color: var(--ink); font-weight: 850; }
    .identity-card {
      margin: 10px 0;
      overflow: hidden;
      background: #fffdf8;
      border: 1px solid #ddd2bf;
      border-radius: 10px;
    }
    .identity-title {
      padding: 8px 10px;
      color: var(--ink);
      background: #f2eadc;
      border-bottom: 1px solid #ddd2bf;
      font-weight: 900;
    }
    .identity-table { width: 100%; border-collapse: collapse; table-layout: fixed; }
    .identity-table th,
    .identity-table td { padding: 7px 9px; vertical-align: top; text-align: left; }
    .identity-table tr + tr th,
    .identity-table tr + tr td { border-top: 1px solid #eee7da; }
    .identity-table th {
      width: 76px;
      color: #6f6558;
      background: rgba(246, 241, 232, .72);
      font-weight: 800;
      white-space: nowrap;
    }
    .identity-table td { color: #28342c; font-weight: 700; }
    .identity-spacer { height: 4px; }
    .empty-value { color: #948b7e; font-style: italic; }
    .record-actions { display: flex; justify-content: flex-end; gap: 8px; margin-top: 12px; }

    .empty-state {
      padding: 64px 24px;
      text-align: center;
      background: rgba(255,253,247,.82);
      border: 1px dashed var(--line-strong);
      border-radius: var(--radius);
    }
    .empty-state strong { display: block; margin-bottom: 6px; font-size: 20px; }
    .empty-state span { color: var(--ink-soft); }

    .compact .record-card details > summary { min-height: 76px; padding-block: 12px; }
    .compact .record-title p { -webkit-line-clamp: 1; }
    .compact .evidence-grid { grid-template-columns: repeat(3, minmax(0, 1fr)); }
    .compact .evidence-cell { padding: 12px; }

    .back-top {
      position: fixed;
      right: 22px;
      bottom: 22px;
      width: 46px;
      height: 46px;
      border: 1px solid rgba(255,255,255,.15);
      color: #fff;
      background: var(--ink);
      border-radius: 50%;
      box-shadow: 0 12px 30px rgba(23,32,27,.22);
      cursor: pointer;
      opacity: 0;
      pointer-events: none;
      transform: translateY(8px);
      transition: opacity .2s ease, transform .2s ease;
    }
    .back-top.visible { opacity: 1; pointer-events: auto; transform: translateY(0); }
    .toast {
      position: fixed;
      left: 50%;
      bottom: 26px;
      z-index: 50;
      max-width: min(420px, calc(100% - 32px));
      padding: 11px 16px;
      color: #fff;
      background: var(--ink);
      border-radius: 999px;
      box-shadow: 0 12px 34px rgba(23,32,27,.26);
      opacity: 0;
      pointer-events: none;
      transform: translate(-50%, 12px);
      transition: opacity .2s ease, transform .2s ease;
    }
    .toast.show { opacity: 1; transform: translate(-50%, 0); }
    .page-footer { margin: 34px 0 0; color: #766e63; font-size: 12px; text-align: center; }

    @keyframes rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: translateY(0); } }

    @media (max-width: 1040px) {
      .summary-grid { grid-template-columns: repeat(2, minmax(150px, 1fr)); }
      .toolbar { grid-template-columns: 1fr; position: relative; top: 0; }
      .segmented, .action-group { flex-wrap: wrap; }
      .evidence-grid, .compact .evidence-grid { grid-template-columns: 1fr; }
    }
    @media (max-width: 720px) {
      .masthead-inner, .shell { width: min(100% - 24px, 1480px); }
      .masthead-inner { padding: 32px 0 48px; }
      .shell { margin-top: -22px; }
      .sheet-intro { grid-template-columns: 1fr; align-items: start; }
      .summary-grid { grid-template-columns: 1fr 1fr; gap: 8px; }
      .metric { min-height: 104px; padding: 15px; }
      .metric-value { font-size: 34px; }
      .record-card details > summary { grid-template-columns: 38px minmax(0,1fr) 20px; padding: 14px; }
      .record-number { width: 34px; height: 34px; }
      .status-pill { grid-column: 2; justify-self: start; }
      .chevron { grid-column: 3; grid-row: 1 / span 2; }
      .record-body { padding: 0 14px 16px; }
      .results-line { align-items: flex-start; flex-direction: column; gap: 2px; }
    }

    @media print {
      body { background: #fff; }
      .masthead { color: #000; background: #fff; border-bottom: 2px solid #000; }
      .masthead::before, .eyebrow, .offline-badge, .scenario-tabs, .toolbar, .excel-link, .record-actions, .back-top, .toast { display: none !important; }
      .masthead-inner, .shell { width: 100%; margin: 0; padding: 12px 0; }
      h1 { color: #000; font-size: 28px; }
      .masthead-meta { color: #333; }
      .sheet-intro { margin: 14px 0; }
      .summary-grid { grid-template-columns: repeat(4, 1fr); }
      .metric, .record-card { box-shadow: none; break-inside: avoid; }
      .records { display: block; }
      .record-card { margin-bottom: 10px; }
      .record-card details > summary { min-height: auto; padding: 10px; }
      .record-card details[open] .record-body { display: block; }
      .evidence-grid, .compact .evidence-grid { grid-template-columns: 1fr 1fr; }
      .page-footer { margin-top: 14px; }
    }
  </style>
</head>
<body>
  <header class="masthead">
    <div class="masthead-inner">
      <div class="eyebrow">Offline activity verification</div>
      <h1>线下活动核销结果</h1>
      <div class="masthead-meta">
        <span class="offline-badge">离线可用</span>
        <span id="fileName">本地查看页</span>
        <span>·</span>
        <span>所有判断均来自同批 Excel 可见内容</span>
      </div>
    </div>
  </header>

  <main class="shell" id="top">
    <nav class="scenario-tabs" id="scenarioTabs" aria-label="核销场景"></nav>

    <section class="sheet-intro" aria-labelledby="sheetTitle">
      <div>
        <div class="sheet-kicker" id="sheetKicker">AUDIT VIEW</div>
        <h2 id="sheetTitle"></h2>
        <p class="sheet-note" id="sheetNote"></p>
      </div>
      <a class="excel-link" id="excelLink" href="#" title="打开同名 Excel 原文件">
        <span aria-hidden="true">▦</span>
        打开原始 Excel
      </a>
    </section>

    <section class="summary-grid" id="summaryGrid" aria-label="当前场景汇总"></section>

    <section class="toolbar" aria-label="查看工具">
      <div class="search-wrap">
        <input class="search-input" id="searchInput" type="search" placeholder="搜索门店、商品、69码、文件名或问题…" autocomplete="off">
        <button class="clear-search" id="clearSearch" type="button" aria-label="清空搜索" hidden>×</button>
      </div>
      <div class="segmented" id="statusFilters" aria-label="结论筛选">
        <button class="filter-button" type="button" data-status="all" aria-pressed="true">全部</button>
        <button class="filter-button" type="button" data-status="pass" aria-pressed="false">只看通过</button>
        <button class="filter-button" type="button" data-status="issue" aria-pressed="false">只看待补</button>
        <button class="filter-button" type="button" data-status="summary" aria-pressed="false">只看汇总</button>
      </div>
      <div class="action-group">
        <button class="tool-button" id="densityButton" type="button" aria-pressed="false">紧凑显示</button>
        <button class="tool-button" id="expandButton" type="button" aria-pressed="false">展开全部</button>
        <button class="tool-button primary" id="printButton" type="button">打印 / 存 PDF</button>
      </div>
    </section>

    <div class="results-line">
      <span id="resultsCount"></span>
      <span>点开任意记录即可查看六列完整内容</span>
    </div>

    <section class="records" id="records" aria-live="polite"></section>
    <p class="page-footer">这是便于阅读的本地页面；正式核销内容与同名 Excel 保持一致。</p>
  </main>

  <button class="back-top" id="backTop" type="button" aria-label="返回顶部">↑</button>
  <div class="toast" id="toast" role="status" aria-live="polite"></div>
  __AUDIT_DATA__
  <script>
    (() => {
      'use strict';
      const data = JSON.parse(document.getElementById('audit-data').textContent);
      const state = { sheetIndex: 0, status: 'all', query: '', compact: false, expanded: false };
      const tabs = document.getElementById('scenarioTabs');
      const records = document.getElementById('records');
      const searchInput = document.getElementById('searchInput');
      const clearSearch = document.getElementById('clearSearch');
      const densityButton = document.getElementById('densityButton');
      const expandButton = document.getElementById('expandButton');
      const backTop = document.getElementById('backTop');
      const toast = document.getElementById('toast');
      let toastTimer = null;

      const escapeHtml = (value) => String(value ?? '')
        .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;').replaceAll("'", '&#039;');

      const splitHeader = (header) => {
        const lines = String(header || '').split('\n').filter(Boolean);
        return { label: lines[0] || '未命名字段', source: lines.slice(1).join(' / ') };
      };

      const renderLine = (line) => {
        const raw = String(line || '');
        const colon = raw.indexOf('：');
        if (colon > 0 && colon <= 18) {
          return `<p class="cell-line"><b>${escapeHtml(raw.slice(0, colon + 1))}</b>${escapeHtml(raw.slice(colon + 1))}</p>`;
        }
        return `<p class="cell-line">${escapeHtml(raw) || '&nbsp;'}</p>`;
      };

      const renderCellBody = (value) => {
        const lines = String(value || '').split('\n');
        const identityHeading = /^(现场商品\d+（知识库）|对应销售Excel第\d+行)$/;
        const identityField = /^(商品编码|商品名称|69码|数量)：(.*)$/;
        const blocks = [];
        let index = 0;
        while (index < lines.length) {
          const heading = lines[index].match(identityHeading);
          if (heading) {
            const rows = [];
            index += 1;
            while (index < lines.length) {
              const field = lines[index].match(identityField);
              if (!field) break;
              rows.push(
                `<tr><th scope="row">${escapeHtml(field[1])}</th><td>${escapeHtml(field[2]) || '未提供'}</td></tr>`
              );
              index += 1;
            }
            if (rows.length) {
              blocks.push(
                `<section class="identity-card"><div class="identity-title">${escapeHtml(heading[1])}</div>`
                + `<table class="identity-table"><tbody>${rows.join('')}</tbody></table></section>`
              );
              continue;
            }
          }
          if (!lines[index]) {
            blocks.push('<div class="identity-spacer" aria-hidden="true"></div>');
          } else {
            blocks.push(renderLine(lines[index]));
          }
          index += 1;
        }
        return blocks.join('');
      };

      const renderCell = (header, value) => {
        const meta = splitHeader(header);
        const body = value
          ? renderCellBody(value)
          : '<span class="empty-value">本项没有可见内容</span>';
        return `<section class="evidence-cell">
          <div class="evidence-label"><strong>${escapeHtml(meta.label)}</strong>${meta.source ? `<small title="${escapeHtml(meta.source)}">${escapeHtml(meta.source)}</small>` : ''}</div>
          <div class="cell-body">${body}</div>
        </section>`;
      };

      const statusText = (status) => ({ pass: '通过', issue: '待补材料', summary: '汇总' }[status] || '查看');

      const conclusionExcerpt = (row) => {
        const conclusion = row.values[5] || row.values.find(Boolean) || '';
        return conclusion.replaceAll('\n', ' · ');
      };

      const activeSheet = () => data.sheets[state.sheetIndex];

      const currentRows = () => {
        const query = state.query.trim().toLocaleLowerCase('zh-CN');
        return activeSheet().rows.filter((row) => {
          if (state.status !== 'all' && row.status !== state.status) return false;
          if (!query) return true;
          return [row.heading, ...row.values].join('\n').toLocaleLowerCase('zh-CN').includes(query);
        });
      };

      const showToast = (message) => {
        window.clearTimeout(toastTimer);
        toast.textContent = message;
        toast.classList.add('show');
        toastTimer = window.setTimeout(() => toast.classList.remove('show'), 1800);
      };

      const legacyCopy = (text) => {
        const area = document.createElement('textarea');
        area.value = text;
        area.setAttribute('readonly', '');
        area.style.position = 'fixed';
        area.style.left = '-9999px';
        document.body.appendChild(area);
        area.select();
        area.setSelectionRange(0, area.value.length);
        let copied = false;
        try { copied = document.execCommand('copy'); } catch (_) { copied = false; }
        area.remove();
        return copied;
      };

      const copyText = async (text) => {
        let copied = false;
        if (navigator.clipboard && window.isSecureContext) {
          try {
            await navigator.clipboard.writeText(text);
            copied = true;
          } catch (_) {
            copied = legacyCopy(text);
          }
        } else {
          copied = legacyCopy(text);
        }
        showToast(copied ? '本条结论已复制' : '浏览器未允许复制，请手动选择文字');
      };

      const setExcelLink = () => {
        const pathParts = decodeURIComponent(window.location.pathname).split('/');
        const pageName = pathParts[pathParts.length - 1] || '核销结果.html';
        const excelName = pageName.replace(/\.html$/i, '.xlsx');
        document.getElementById('fileName').textContent = pageName;
        const excelUrl = new URL(window.location.href);
        excelUrl.hash = '';
        excelUrl.search = '';
        excelUrl.pathname = excelUrl.pathname.replace(/[^/]*$/, encodeURIComponent(excelName));
        document.getElementById('excelLink').href = excelUrl.href;
      };

      const renderTabs = () => {
        tabs.innerHTML = data.sheets.map((sheet, index) => `
          <button class="tab-button" type="button" role="tab" data-sheet="${index}" aria-selected="${index === state.sheetIndex}">
            ${escapeHtml(sheet.name)}
          </button>`).join('');
      };

      const renderIntro = () => {
        const sheet = activeSheet();
        document.getElementById('sheetKicker').textContent = sheet.scenario === 'personnel_incentive' ? 'PERSONNEL INCENTIVE' : 'PROMOTIONAL DISPLAY';
        document.getElementById('sheetTitle').textContent = sheet.title;
        document.getElementById('sheetNote').textContent = sheet.note;
      };

      const renderMetrics = () => {
        const rows = activeSheet().rows;
        const recordsOnly = rows.filter((row) => row.kind === 'record');
        const passed = recordsOnly.filter((row) => row.status === 'pass').length;
        const issues = recordsOnly.filter((row) => row.status === 'issue').length;
        const summaries = rows.filter((row) => row.kind === 'summary').length;
        const metrics = [
          ['核销记录', recordsOnly.length, '当前场景逐项记录', ''],
          ['可直接通过', passed, '无需重新提交材料', 'pass'],
          ['需重新提交', issues, '点开查看具体补交内容', 'issue'],
          ['汇总与说明', summaries, '金额、申报或身份汇总', ''],
        ];
        document.getElementById('summaryGrid').innerHTML = metrics.map(([label, value, hint, tone]) => `
          <article class="metric ${tone}"><span class="metric-label">${label}</span><strong class="metric-value">${value}</strong><span class="metric-hint">${hint}</span></article>`).join('');
      };

      const renderRecords = () => {
        const sheet = activeSheet();
        const rows = currentRows();
        document.getElementById('resultsCount').innerHTML = `当前显示 <strong>${rows.length}</strong> / ${sheet.rows.length} 条`;
        if (!rows.length) {
          records.innerHTML = '<div class="empty-state"><strong>没有找到符合条件的记录</strong><span>可以清空搜索或切换上方结论筛选。</span></div>';
          return;
        }
        records.innerHTML = rows.map((row, index) => {
          const displayIndex = String(row.excel_row - 3).padStart(2, '0');
          return `<article class="record-card ${row.status}">
            <details ${state.expanded ? 'open' : ''} data-row="${row.excel_row}">
              <summary>
                <span class="record-number">${displayIndex}</span>
                <div class="record-title"><h3>${escapeHtml(row.heading)}</h3><p>${escapeHtml(conclusionExcerpt(row))}</p></div>
                <span class="status-pill ${row.status}">${statusText(row.status)}</span>
                <span class="chevron" aria-hidden="true">⌄</span>
              </summary>
              <div class="record-body">
                <div class="evidence-grid">${row.values.map((value, cellIndex) => renderCell(sheet.headers[cellIndex], value)).join('')}</div>
                <div class="record-actions"><button class="tool-button copy-conclusion" type="button" data-copy-row="${row.excel_row}">复制本条结论</button></div>
              </div>
            </details>
          </article>`;
        }).join('');
      };

      const renderAll = () => {
        renderTabs();
        renderIntro();
        renderMetrics();
        renderRecords();
      };

      tabs.addEventListener('click', (event) => {
        const button = event.target.closest('[data-sheet]');
        if (!button) return;
        state.sheetIndex = Number(button.dataset.sheet);
        state.status = 'all';
        state.query = '';
        state.expanded = false;
        searchInput.value = '';
        clearSearch.hidden = true;
        document.querySelectorAll('[data-status]').forEach((item) => item.setAttribute('aria-pressed', String(item.dataset.status === 'all')));
        renderAll();
      });

      document.getElementById('statusFilters').addEventListener('click', (event) => {
        const button = event.target.closest('[data-status]');
        if (!button) return;
        state.status = button.dataset.status;
        document.querySelectorAll('[data-status]').forEach((item) => item.setAttribute('aria-pressed', String(item === button)));
        renderRecords();
      });

      searchInput.addEventListener('input', () => {
        state.query = searchInput.value;
        clearSearch.hidden = !state.query;
        renderRecords();
      });
      clearSearch.addEventListener('click', () => {
        state.query = '';
        searchInput.value = '';
        clearSearch.hidden = true;
        searchInput.focus();
        renderRecords();
      });

      densityButton.addEventListener('click', () => {
        state.compact = !state.compact;
        document.body.classList.toggle('compact', state.compact);
        densityButton.setAttribute('aria-pressed', String(state.compact));
        densityButton.textContent = state.compact ? '舒展显示' : '紧凑显示';
      });

      expandButton.addEventListener('click', () => {
        state.expanded = !state.expanded;
        records.querySelectorAll('details').forEach((detail) => { detail.open = state.expanded; });
        expandButton.setAttribute('aria-pressed', String(state.expanded));
        expandButton.textContent = state.expanded ? '收起全部' : '展开全部';
      });

      records.addEventListener('click', (event) => {
        const button = event.target.closest('[data-copy-row]');
        if (!button) return;
        const rowNumber = Number(button.dataset.copyRow);
        const row = activeSheet().rows.find((item) => item.excel_row === rowNumber);
        if (row) copyText(row.values[5] || row.values.join('\n'));
      });

      document.getElementById('printButton').addEventListener('click', () => {
        records.querySelectorAll('details').forEach((detail) => { detail.open = true; });
        window.setTimeout(() => window.print(), 40);
      });
      window.addEventListener('afterprint', () => renderRecords());
      window.addEventListener('scroll', () => backTop.classList.toggle('visible', window.scrollY > 500), { passive: true });
      backTop.addEventListener('click', () => window.scrollTo({ top: 0, behavior: 'smooth' }));

      setExcelLink();
      renderAll();
    })();
  </script>
  <noscript>请启用浏览器 JavaScript，以使用搜索、筛选和展开按钮。</noscript>
</body>
</html>
'''


def create_html_report_from_workbook(
    workbook_path: str | Path,
    output_path: str | Path,
) -> Path:
    payload = _workbook_payload(workbook_path)
    embedded = DATA_OPEN + _json_for_script(payload) + DATA_CLOSE
    html = HTML_TEMPLATE.replace("__AUDIT_DATA__", embedded)
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8", newline="\n")
    return target


def _embedded_payload(html: str) -> dict[str, Any]:
    start = html.find(DATA_OPEN)
    if start < 0:
        raise AuditError("HTML缺少内嵌核销数据")
    start += len(DATA_OPEN)
    end = html.find(DATA_CLOSE, start)
    if end < 0:
        raise AuditError("HTML内嵌核销数据没有正确结束")
    try:
        payload = json.loads(html[start:end])
    except json.JSONDecodeError as exc:
        raise AuditError("HTML内嵌核销数据不是有效JSON") from exc
    if not isinstance(payload, dict):
        raise AuditError("HTML内嵌核销数据结构错误")
    return payload


def verify_html_report(
    html_path: str | Path,
    scenarios: list[str],
    *,
    workbook_path: str | Path | None = None,
) -> dict[str, Any]:
    source = Path(html_path)
    if not source.is_file():
        raise AuditError(f"找不到HTML输出：{source}")
    html = source.read_text(encoding="utf-8")
    payload = _embedded_payload(html)
    expected_sheets = [SCENARIO_SHEETS[scenario] for scenario in scenarios]
    actual_sheets = [str(sheet.get("name") or "") for sheet in payload.get("sheets", [])]
    if actual_sheets != expected_sheets:
        raise AuditError(f"HTML场景名称或顺序错误：{actual_sheets}，期望{expected_sheets}")

    required_controls = (
        'id="scenarioTabs"',
        'id="searchInput"',
        'id="statusFilters"',
        'id="densityButton"',
        'id="expandButton"',
        'id="printButton"',
        'id="excelLink"',
        'id="backTop"',
    )
    missing_controls = [control for control in required_controls if control not in html]
    if missing_controls:
        raise AuditError("HTML缺少交互按钮：" + "、".join(missing_controls))
    if re.search(r"<(?:script|link)\b[^>]*(?:src|href)=[\"']https?://", html, re.I):
        raise AuditError("HTML不得依赖在线脚本或样式")
    for forbidden in FORBIDDEN_VISIBLE_TERMS:
        if forbidden in html:
            raise AuditError(f"HTML出现不应交给客户的工程表达：{forbidden}")

    record_counts = {
        str(sheet["name"]): len(sheet.get("rows") or [])
        for sheet in payload.get("sheets", [])
    }
    if workbook_path is not None:
        workbook_payload = _workbook_payload(workbook_path)
        for html_sheet, workbook_sheet in zip(
            payload.get("sheets", []), workbook_payload["sheets"], strict=True
        ):
            if html_sheet.get("headers") != workbook_sheet["headers"]:
                raise AuditError(f"{html_sheet.get('name')} 的HTML表头与Excel不一致")
            if html_sheet.get("rows") != workbook_sheet["rows"]:
                raise AuditError(f"{html_sheet.get('name')} 的HTML记录与Excel不一致")

    return {
        "path": str(source.resolve()),
        "sheet_names": actual_sheets,
        "record_counts": record_counts,
        "button_count": len(required_controls),
        "external_dependency_count": 0,
        "workbook_content_equal": workbook_path is not None,
    }
