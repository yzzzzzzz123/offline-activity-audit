from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sys
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops
from playwright.sync_api import Page, sync_playwright


DESKTOP_VIEWPORT = {"width": 1440, "height": 960}


def _path(value: str) -> Path:
    path = Path(value).expanduser().resolve()
    if not path.is_file():
        raise argparse.ArgumentTypeError(f"文件不存在：{path}")
    return path


def _sha256(value: bytes | str) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _pixel_difference(reference_png: bytes, candidate_png: bytes) -> dict[str, Any]:
    reference = Image.open(io.BytesIO(reference_png)).convert("RGBA")
    candidate = Image.open(io.BytesIO(candidate_png)).convert("RGBA")
    if reference.size != candidate.size:
        return {
            "equal": False,
            "reference_size": list(reference.size),
            "candidate_size": list(candidate.size),
            "different_pixels": None,
        }
    difference = ImageChops.difference(reference, candidate)
    pixels = difference.get_flattened_data()
    different_pixels = sum(1 for pixel in pixels if pixel != (0, 0, 0, 0))
    return {
        "equal": different_pixels == 0,
        "reference_size": list(reference.size),
        "candidate_size": list(candidate.size),
        "different_pixels": different_pixels,
    }


def _render(page: Page, path: Path) -> dict[str, Any]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    external_requests: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on(
        "request",
        lambda request: external_requests.append(request.url)
        if request.url.startswith(("http://", "https://"))
        else None,
    )
    page.goto(path.as_uri(), wait_until="networkidle")
    page.wait_for_selector(".eo-home-cockpit")
    page.wait_for_timeout(50)
    snapshot = page.evaluate(
        """() => ({
          title: document.title,
          bodyClass: document.body.className,
          bodyHtml: document.body.outerHTML,
          errorStyle: document.getElementById('error-only-preview-style')?.textContent || '',
          total: document.querySelector('.eo-total-gauge strong')?.textContent?.trim() || '',
          metrics: [...document.querySelectorAll('.eo-metric')].map(node => node.innerText.trim()),
          tabs: [...document.querySelectorAll('.eo-tab')].map(node => node.innerText.trim()),
          scenarios: [...document.querySelectorAll('[id^="scenario-"]')].map(node => ({
            title: node.querySelector('.eo-ledger-title h2')?.textContent?.trim() || '',
            impact: node.querySelector('.eo-impact strong')?.textContent?.trim() || '',
            cards: [...node.querySelectorAll('.eo-error-card')].map(card => ({
              title: card.querySelector('.eo-error-head h3')?.textContent?.trim() || '',
              scope: card.querySelector('.eo-scope')?.textContent?.trim() || '',
              text: card.innerText.trim(),
              tableRows: [...card.querySelectorAll('.eo-table tbody tr')].map(row => row.innerText.trim()),
              tableCells: [...card.querySelectorAll('.eo-table tbody tr')].map(row =>
                [...row.querySelectorAll('td')].map(cell => cell.innerText.trim())
              ),
              fields: Object.fromEntries([...card.querySelectorAll('.eo-field')].map(field => [
                field.querySelector(':scope > span')?.textContent?.trim() || '',
                field.querySelector(':scope > div')?.innerText?.trim() || '',
              ])),
              chips: [...card.querySelectorAll('.eo-chip')].map(node => node.textContent?.trim() || ''),
            })),
          })),
          width: {scroll: document.documentElement.scrollWidth, viewport: window.innerWidth},
        })"""
    )
    screenshot = page.screenshot(full_page=True, animations="disabled")

    tabs = page.locator(".eo-tab")
    if tabs.count() < 2:
        raise RuntimeError(f"{path.name} 缺少场景导航")
    tabs.nth(1).click()
    selected_view = tabs.nth(1).get_attribute("data-view")
    selected_visible = page.locator(f"#{selected_view}").is_visible()
    selected_state = tabs.nth(1).get_attribute("aria-selected")
    page.locator(f"#{selected_view} [data-open='home']").click()
    home_visible = page.locator("#home").is_visible()
    detail = page.locator(".eo-error-details").first
    detail_operable = True
    if detail.count():
        detail_view = detail.evaluate("node => node.closest('.eo-view')?.id")
        if not detail_view:
            raise RuntimeError(f"{path.name} 的错误明细不属于任何场景视图")
        page.locator(f".eo-tab[data-view='{detail_view}']").click()
        detail.locator("summary").click()
        detail_operable = bool(detail.get_attribute("open") is not None)
        page.locator(f"#{detail_view} [data-open='home']").click()

    return {
        "snapshot": snapshot,
        "screenshot": screenshot,
        "console_errors": console_errors,
        "page_errors": page_errors,
        "external_requests": external_requests,
        "interaction": {
            "scenario_visible": selected_visible,
            "selected_state": selected_state,
            "home_visible_after_return": home_visible,
            "detail_operable": detail_operable,
        },
    }


def _audit_payload(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8")
    match = re.search(
        r'<script id="audit-data" type="application/json">([\s\S]*?)</script>',
        html,
    )
    if not match:
        raise RuntimeError(f"{path.name} 缺少 audit-data")
    payload = json.loads(match.group(1))
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path.name} 的 audit-data 顶层不是对象")
    return payload


def _lines(value: Any) -> list[str]:
    return [line.strip() for line in str(value or "").splitlines() if line.strip()]


def _line_with(value: Any, prefix: str) -> str:
    return next((line for line in _lines(value) if line.startswith(prefix)), "")


def _after_prefix(value: Any, prefix: str) -> str:
    line = _line_with(value, prefix)
    return line[len(prefix) :].strip() if line else ""


def _labeled_value(value: Any, label: str) -> str:
    match = re.search(
        rf"(?:^|[\n；])\s*{re.escape(label)}：([^；\n]+)",
        str(value or ""),
    )
    return match.group(1).strip() if match else ""


def _header_files(sheet: dict[str, Any], index: int) -> str:
    values = _lines((sheet.get("headers") or [])[index])
    return "、".join(values[1:] if len(values) > 1 else values)


def _sheet(payload: dict[str, Any], scenario: str) -> dict[str, Any]:
    matches = [
        item
        for item in payload.get("sheets") or []
        if str(item.get("scenario") or "") == scenario
    ]
    if len(matches) != 1:
        raise RuntimeError(f"场景 {scenario} 数量错误：{len(matches)}")
    return matches[0]


def _first_number(value: Any, pattern: str) -> str | None:
    match = re.search(pattern, str(value or ""))
    return match.group(1) if match else None


def _personnel_logic(sheet: dict[str, Any]) -> dict[str, Any]:
    rows = list(sheet.get("rows") or [])
    products = []
    for row in rows:
        if row.get("section") != "detail" or row.get("kind") != "record":
            continue
        values = list(row.get("values") or [])
        knowledge_line = _line_with(values[1], "知识库商品：")
        knowledge_parts = [item.strip() for item in knowledge_line.split("/")]
        products.append(
            {
                "excel_row": int(row["excel_row"]),
                "status": row.get("status"),
                "knowledge_product_code": (
                    knowledge_parts[0].removeprefix("知识库商品：").strip()
                    if knowledge_parts
                    else ""
                ),
                "knowledge_barcode_69": knowledge_parts[-1] if knowledge_parts else "",
                "excel_barcode_69": _labeled_value(values[1], "条码"),
                "excel_quantity": _labeled_value(values[1], "数量"),
                "excel_reward": _labeled_value(values[1], "计算奖励"),
                "settlement_quantity": _labeled_value(values[2], "数量"),
                "settlement_reward": _labeled_value(values[2], "行奖励"),
                "strict_result": (
                    "all_correspond"
                    if "商品、数量、奖励金额全部对应" in str(values[4])
                    else "problem"
                ),
            }
        )

    issue_rows = [row for row in rows if row.get("status") == "issue"]
    amount = next((row for row in rows if row.get("heading") == "实际申请金额"), {})
    amount_values = list(amount.get("values") or [""] * 6)
    recipient = next((row for row in rows if row.get("heading") == "收款人与日期"), {})
    recipient_values = list(recipient.get("values") or [""] * 6)
    return {
        "source_files": {
            "excel": _header_files(sheet, 1),
            "settlement": _header_files(sheet, 2),
            "transfer": _header_files(sheet, 3),
        },
        "products": products,
        "issue_headings": [str(row.get("heading") or "") for row in issue_rows],
        "amount": {
            "claimed": _first_number(amount_values[2], r"视觉识别申请：(\d+(?:\.\d+)?)元"),
            "transferred": _first_number(amount_values[3], r"视觉识别转账：(\d+(?:\.\d+)?)元"),
        },
        "recipient_and_date": {
            "store_count": _first_number(recipient_values[1], r"Excel读取到(\d+)家门店"),
            "recipient_complete": "完整收款人" not in str(recipient_values[3]),
            "store_mapping_complete": "门店对应关系" not in str(recipient_values[3]),
            "date_complete": "完整交易日期未显示" not in str(recipient_values[3]),
        },
    }


def _comparison_statuses(segment: str) -> dict[str, str]:
    fields = (
        "客户名称",
        "业务日期",
        "商品编码",
        "条形码",
        "数量",
        "零售价",
        "合计金额",
        "合同附件行内金额",
        "配对说明",
    )
    result: dict[str, str] = {}
    for line in _lines(segment):
        for field in fields:
            if line.startswith(f"{field}："):
                result[field] = line.split("：", 1)[1].strip()
                break
    return result


def _display_contract_baseline(sheet: dict[str, Any]) -> dict[str, Any]:
    activity = next(
        row
        for row in sheet.get("rows") or []
        if row.get("heading") == "活动概况｜合同PDF主核销文件"
    )
    text = str((activity.get("values") or [""])[0])
    party_text = _after_prefix(text, "合同签订方：").split("｜", 1)[0]
    period_line = _line_with(text, "执行周期：")
    return {
        "parties": [item.strip() for item in party_text.split("、") if item.strip()],
        "budget": _first_number(text, r"活动预算金额：(\d+(?:\.\d+)?)元"),
        "period": re.findall(r"\d{4}-\d{2}-\d{2}", period_line),
        "store_count": _first_number(text, r"商家/门店：共(\d+)家"),
        "stack_count": _first_number(text, r"堆头数量：(\d+)"),
        "seal_pass": "盖章：可见｜核销：通过" in text,
        "display_requires_one_square_metre": "1平方米" in text,
        "display_requires_four_vertical": "4纵" in text,
        "settlement_requires_real_evidence": "真实有效的凭证" in text,
        "settlement_as_goods_credit": "货补下单" in text,
        "unused_fee_clears_on_termination": "剩余未使用费用将清零" in text,
    }


def _display_attachment_logic(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    records = []
    for row in sheet.get("rows") or []:
        heading = str(row.get("heading") or "")
        location = re.fullmatch(r"合同销售附件第(\d+)行｜PDF第(\d+)页", heading)
        if not location:
            continue
        values = list(row.get("values") or [])
        contract = str(values[0])
        sales = str(values[2])
        comparison = str(values[3])
        parts = comparison.split("\n\n合同附件 → 销售Excel\n", 1)
        knowledge = parts[0]
        sales_comparison = parts[1] if len(parts) == 2 else ""
        sales_row = _first_number(sales, r"销售Excel第(\d+)行")
        records.append(
            {
                "line_no": int(location.group(1)),
                "source_page": int(location.group(2)),
                "row_status": row.get("status"),
                "contract": {
                    "customer_name": _labeled_value(contract, "客户名称"),
                    "business_date": _labeled_value(contract, "业务日期"),
                    "product_code": _labeled_value(contract, "商品编码"),
                    # Product names are intentionally absent: names are fuzzy
                    # auxiliary evidence and never own identity or pass/fail.
                    "barcode_69": _labeled_value(contract, "条形码"),
                    "quantity": _labeled_value(contract, "数量"),
                    "retail_price": _labeled_value(contract, "零售价"),
                    "total_amount": _labeled_value(contract, "合计金额"),
                },
                "knowledge": {
                    "product_code": _labeled_value(knowledge, "知识库商品编码"),
                    "barcode_69": _labeled_value(knowledge, "知识库69码"),
                    "product_code_result": _line_with(knowledge, "商品编码："),
                    "barcode_result": _line_with(knowledge, "条形码："),
                    "existence_unmet": "本项结果：商品存在条件未满足" in knowledge,
                },
                "sales": {
                    "excel_row": sales_row,
                    "customer_name": _labeled_value(sales, "客户名称"),
                    "business_date": _labeled_value(sales, "业务日期"),
                    "product_code": _labeled_value(sales, "商品编码"),
                    "barcode_69": _labeled_value(sales, "条形码"),
                    "quantity": _labeled_value(sales, "数量"),
                    "retail_price": _labeled_value(sales, "零售价"),
                    "total_amount": _labeled_value(sales, "合计金额"),
                },
                "sales_field_results": _comparison_statuses(sales_comparison),
            }
        )
    return records


DISPLAY_GLOBAL_LABELS = frozenset(
    {
        "合同核心六项",
        "合同核心七项",
        "销售Excel对合同",
        "销售Excel对合同附件",
        "销售Excel文件内部",
        "合同商品知识库",
        "现场商品与合同",
    }
)


def _display_store_logic(sheet: dict[str, Any]) -> list[dict[str, Any]]:
    stores = []
    for row in sheet.get("rows") or []:
        values = list(row.get("values") or [])
        if row.get("section") != "detail" or not values or not str(values[0]).startswith("门店："):
            continue
        photo = str(values[1])
        comparison = str(values[3])
        labels_line = _line_with(values[5], "主要问题：")
        labels = [
            item.strip()
            for item in labels_line.removeprefix("主要问题：").split("、")
            if item.strip() and item.strip() not in DISPLAY_GLOBAL_LABELS
        ]
        normalized_labels = []
        for label in labels:
            if label == "合同门店":
                label = "门店水印错误" if "门店不一致" in comparison else "门店水印缺失或无法核对"
            normalized_labels.append(label)
        display_text = _labeled_value(photo, "陈列标准核验")
        display_status = (
            "unmet"
            if display_text.startswith("不符合")
            else "meets"
            if display_text.startswith("符合")
            else "unclear"
            if display_text.startswith("无法确认")
            else "unrecognized"
        )
        columns = _first_number(photo, r"可见纵列数：(\d+)")
        stores.append(
            {
                "store": str(row.get("heading") or ""),
                "photo_files": _labeled_value(photo, "文件"),
                "display_status": display_status,
                "column_threshold": (
                    ">=4" if columns is not None and int(columns) >= 4 else columns
                ),
                "local_error_labels": normalized_labels,
                "error_date": (
                    _labeled_value(photo, "识别日期")
                    if "活动日期" in normalized_labels
                    else None
                ),
                "error_location": (
                    _labeled_value(photo, "识别地点")
                    if any("门店水印" in label for label in normalized_labels)
                    else None
                ),
            }
        )
    return stores


def _display_logic(sheet: dict[str, Any]) -> dict[str, Any]:
    attachment_summary = next(
        row
        for row in sheet.get("rows") or []
        if row.get("heading") == "合同销售附件｜合同主基准"
    )
    summary_text = str((attachment_summary.get("values") or ["", "", "", ""])[3])
    summary = re.search(
        r"合同附件(\d+)行：通过(\d+)行、问题(\d+)行；销售Excel另有(\d+)行无合同附件基准",
        summary_text,
    )
    return {
        "source_files": {
            "contract": _header_files(sheet, 0),
            "photos": _header_files(sheet, 1),
            "sales": _header_files(sheet, 2),
        },
        "contract_baseline": _display_contract_baseline(sheet),
        "attachment_summary": list(summary.groups()) if summary else None,
        "attachment_records": _display_attachment_logic(sheet),
        "stores": _display_store_logic(sheet),
    }


def _poster_photo_facts(text: str) -> dict[str, Any]:
    observations = []
    for line in _lines(text):
        match = re.match(
            r"^(.+?\.jpg)：(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s+([^；]+)；",
            line,
        )
        if match:
            observations.append(
                {
                    "file": match.group(1),
                    "datetime": match.group(2),
                    "location": match.group(3),
                    "dimension_evidence_absent": "无尺寸依据" in line,
                }
            )
    return {
        "submitted_photos": _first_number(text, r"共提交(\d+)张返图"),
        "recognized_locations": _first_number(text, r"识别(\d+)个地点"),
        "contract_display_units": _first_number(text, r"合同(\d+)个"),
        "visible_display_units": _first_number(text, r"照片可明确计数(\d+)个"),
        "contract_stores": _first_number(text, r"合同涉及(\d+)家门店"),
        "photo_locations": _first_number(text, r"照片仅识别到(\d+)个不同地点"),
        "observations": observations,
    }


def _poster_logic(sheet: dict[str, Any]) -> dict[str, Any]:
    issues = []
    for row in sheet.get("rows") or []:
        if row.get("status") != "issue":
            continue
        heading = str(row.get("heading") or "").removeprefix("错误项：")
        values = list(row.get("values") or [])
        recognized = str(values[1])
        facts: dict[str, Any]
        if heading == "合同材料不完整":
            facts = {
                "attachment_referenced": "具体门店清单见附件" in recognized,
                "attachment_missing": "未提交该附件" in recognized,
            }
        elif heading == "票据费用明细不完整":
            facts = {
                "quantity_missing": "数量=未列明" in recognized,
                "unit_price_missing": "单价=未列明" in recognized,
                "subtotal_missing": "小计=未列明" in recognized,
                "contract_lampbox_count": _first_number(recognized, r"灯箱（合同数量(\d+)）"),
                "contract_counterstand_count": _first_number(recognized, r"台上架（合同数量(\d+)）"),
            }
        elif heading == "金额或项目不一致":
            facts = {
                label: _first_number(recognized, rf"{label}(\d+(?:\.\d+)?)元")
                for label in ("合同", "票据", "结算单")
            }
        elif heading == "现场照片执行证据不完整":
            facts = _poster_photo_facts(recognized)
        else:
            facts = {"recognized": recognized}
        issues.append(
            {
                "heading": heading,
                "source_files": _after_prefix(values[0], "来源："),
                "facts": facts,
            }
        )
    return {"issues": issues}


def _business_logic(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": payload.get("schema_version"),
        "personnel_incentive": _personnel_logic(
            _sheet(payload, "personnel_incentive")
        ),
        "promotional_display": _display_logic(
            _sheet(payload, "promotional_display")
        ),
        "poster_material": _poster_logic(_sheet(payload, "poster_material")),
    }


EXPECTED_PERSONNEL_PRODUCTS = (
    (4, "CP-KQ-YG-0205", "6970356166832", "238", "714元"),
    (5, "CP-KQ-YG-0229", "6970356162391", "275", "825元"),
    (6, "CP-KQ-YG-0260", "6970356162810", "284", "852元"),
    (7, "CP-KQ-YG-0080", "6970356164388", "226", "678元"),
    (8, "CP-KQ-YG-0076", "6970356164395", "225", "675元"),
    (9, "CP-KQ-YG-0419", "6970356169338", "252", "756元"),
    (10, "CP-KQ-YG-0418", "6970356169321", "239", "717元"),
    (11, "CP-KQ-YG-0324", "6970356162636", "278", "834元"),
    (12, "CP-KQ-YG-0363", "6970356164289", "190", "570元"),
    (13, "CP-KQ-YG-0460", "6970356164296", "244", "732元"),
)

EXPECTED_CONTRACT_IDENTITIES = (
    (1, "020260012", "6970356167334"),
    (2, "020260011", "6970356167341"),
    (3, "020260009", "6970356164241"),
    (4, "020260007", "6970356164203"),
    (5, "020260008", "6970356164258"),
    (6, "020260013", "6970356164296"),
    (7, "020260010", "6970356164289"),
    (8, "020260031", "6970356166979"),
    (9, "020260037", "6970356164388"),
    (10, "030160008", "6970356164159"),
    (11, "020260034", "6970356168454"),
    (12, "020260005", "6970356165774"),
    (13, "020260015", "6970356168904"),
    (14, "020260019", "6970356165910"),
    (15, "030160005", "6970356166450"),
    (16, "030160004", "6970356166931"),
    (17, "020260022", "6970356162391"),
    (18, "020260023", "6970356162810"),
    (19, "030160009", "6970356160304"),
    (20, "030160007", "6970356164500"),
    (21, "020260006", "6970356164395"),
    (22, "020260016", "6970356167853"),
    (23, "020260018", "6970356167839"),
    (24, "020260046", "6970356169338"),
    (25, "020260035", "6970356169321"),
    (26, "030160003", "6970356165095"),
    (27, "020260024", "6970356162919"),
    (28, "030160012", "6970356160496"),
    (29, "030160002", "6970356165156"),
    (30, "030160001", "6970356166429"),
    (31, "030160010", "6970356160366"),
    (32, "020260002", "6970356165453"),
    (33, "020260001", "6970356164548"),
    (34, "030160011", "6970356161721"),
    (35, "020260017", "6970356167846"),
    (36, "020260025", "6970356162636"),
)

EXPECTED_DISPLAY_LOCAL_ERRORS = {
    "百佳华百货（公明店）": (
        "unclear",
        "3",
        ("陈列标准",),
        None,
        None,
    ),
    "金大福（东浦金正和总店）": (
        "unclear",
        "3",
        ("活动日期", "门店水印缺失或无法核对", "陈列标准"),
        "未识别",
        "未识别",
    ),
    "挺拇指生活超市（横沥店）": (
        "meets",
        ">=4",
        ("门店水印错误",),
        None,
        "东莞市·南铭购物乐园",
    ),
    "人人购物广场荟和二店": (
        "meets",
        ">=4",
        ("门店水印错误",),
        None,
        "深圳市·荟和超市（人人购物同乐店）",
    ),
    "润家连锁超市（美联购物中心店）": (
        "meets",
        ">=4",
        ("门店水印错误",),
        None,
        "深圳市宝安区·深圳紫云快捷宾馆",
    ),
}


def _ean13_valid(value: str) -> bool:
    if not re.fullmatch(r"69\d{11}", value):
        return False
    digits = [int(character) for character in value]
    check = (10 - sum(
        digit * (1 if index % 2 == 0 else 3)
        for index, digit in enumerate(digits[:12])
    ) % 10) % 10
    return check == digits[-1]


def _scenario(snapshot: dict[str, Any], title: str) -> dict[str, Any]:
    matches = [item for item in snapshot["scenarios"] if item["title"] == title]
    if len(matches) != 1:
        raise RuntimeError(f"渲染场景 {title} 数量错误：{len(matches)}")
    return matches[0]


def _card(scenario: dict[str, Any], title: str) -> dict[str, Any]:
    matches = [item for item in scenario["cards"] if item["title"] == title]
    if len(matches) != 1:
        raise RuntimeError(f"渲染错误卡 {title} 数量错误：{len(matches)}")
    return matches[0]


def _material_contract_count(text: str, label: str) -> str | None:
    positions = [match.start() for match in re.finditer(re.escape(label), text)]
    for start in reversed(positions):
        segment = text[start : start + 90]
        for pattern in (r"合同数量\s*(\d+)", r"(\d+)\s*个"):
            value = _first_number(segment, pattern)
            if value is not None:
                return value
    return None


def _rendered_business_logic(snapshot: dict[str, Any]) -> dict[str, Any]:
    personnel = _scenario(snapshot, "人员激励")
    amount_card = _card(personnel, "实际申请金额")
    amount_reason = amount_card["fields"].get("错误原因", "")
    recipient_card = _card(personnel, "收款人与日期")
    recipient_reason = recipient_card["fields"].get("错误原因", "")

    display = _scenario(snapshot, "堆头陈列")
    contract_card = next(
        item for item in display["cards"] if item["scope"] == "合同商品"
    )
    contract_rows = []
    for cells in contract_card["tableCells"]:
        if len(cells) < 4:
            continue
        location = re.fullmatch(r"合同销售附件第(\d+)行｜PDF第(\d+)页", cells[0])
        cause = cells[3]
        if not location:
            continue
        contract_code = _first_number(cause, r"产品编码：合同\s+(\d{9})")
        knowledge_code_match = re.search(
            r"知识库主编码\s+([A-Z0-9-]+)",
            cause,
        )
        contract_rows.append(
            {
                "line_no": int(location.group(1)),
                "source_page": int(location.group(2)),
                "contract_product_code": contract_code,
                "knowledge_product_code": (
                    knowledge_code_match.group(1) if knowledge_code_match else None
                ),
                "alias_unregistered": "product_code_aliases 未登记合同编码" in cause,
            }
        )

    store_errors = {}
    for item in display["cards"]:
        if item["scope"] != "门店现场":
            continue
        reason = item["fields"].get("错误原因", "")
        location_match = re.search(
            r"照片水印地点：(.+?)(?=水印地点已|照片没有|；|文件：|$)",
            reason,
        )
        store_errors[item["title"]] = {
            "chips": item["chips"],
            "recognized_date": _first_number(
                reason,
                r"识别日期：(\d{4}-\d{2}-\d{2}|未识别)",
            ),
            "watermark_location": (
                location_match.group(1).strip() if location_match else None
            ),
        }

    poster = _scenario(snapshot, "展示道具")
    poster_cards = {item["title"]: item for item in poster["cards"]}
    contract_reason = poster_cards["合同材料不完整"]["fields"].get("错误原因", "")
    invoice_reason = poster_cards["票据费用明细不完整"]["fields"].get("错误原因", "")
    amount_reason_poster = poster_cards["金额或项目不一致"]["fields"].get("错误原因", "")
    photo_reason = poster_cards["现场照片执行证据不完整"]["fields"].get("错误原因", "")

    return {
        "personnel": {
            "claimed_amount": _first_number(amount_reason, r"申请金额为(\d+(?:\.\d+)?)元"),
            "transferred_amount": _first_number(amount_reason, r"转账(?:凭证)?识别金额为(\d+(?:\.\d+)?)元"),
            "difference": _first_number(amount_reason, r"相差(\d+(?:\.\d+)?)元"),
            "store_count": _first_number(recipient_reason, r"读取到(\d+)家门店"),
            "recipient_missing": "没有完整显示每笔收款人" in recipient_reason,
            "store_mapping_missing": "对应门店" in recipient_reason,
            "complete_date_missing": "完整交易日期" in recipient_reason,
        },
        "display": {
            "contract_error_count": _first_number(
                contract_card["fields"].get("错误原因", ""),
                r"(\d+)行",
            ),
            "contract_rows": contract_rows,
            "store_errors": store_errors,
        },
        "poster": {
            "contract_attachment_referenced": "具体门店清单见附件" in contract_reason,
            "contract_attachment_missing": "未提交该附件" in contract_reason,
            "invoice_quantity_missing": "数量=未列明" in invoice_reason,
            "invoice_unit_price_missing": "单价=未列明" in invoice_reason,
            "invoice_subtotal_missing": "小计=未列明" in invoice_reason,
            "contract_lampbox_count": _material_contract_count(invoice_reason, "灯箱"),
            "contract_counter_display_count": _material_contract_count(invoice_reason, "台上架"),
            "amounts": {
                label: _first_number(amount_reason_poster, rf"{label}(\d+(?:\.\d+)?)元")
                for label in ("合同", "票据", "结算单")
            },
            "submitted_photos": _first_number(photo_reason, r"共提交(\d+)张返图"),
            "recognized_locations": _first_number(photo_reason, r"识别(\d+)个地点"),
            "contract_display_units": _first_number(
                photo_reason,
                r"台上架/台面展示架合同(\d+)个",
            ),
            "visible_display_units": _first_number(
                photo_reason,
                r"照片可明确计数(\d+)个",
            ),
            "contributing_photos": _first_number(
                photo_reason,
                r"涉及(\d+)张照片",
            ),
            "contract_store_count": _first_number(photo_reason, r"合同涉及(\d+)家门店"),
            "photo_location_count": _first_number(
                photo_reason,
                r"照片仅识别到(\d+)个不同地点",
            ),
            "all_photos_lack_dimensions": "10张照片均未提供尺寸" in photo_reason,
        },
    }


def _candidate_internal_checks(
    logic: dict[str, Any],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    personnel = logic["personnel_incentive"]
    products = personnel["products"]
    product_facts = tuple(
        (
            item["excel_row"],
            item["knowledge_product_code"],
            item["knowledge_barcode_69"],
            item["excel_quantity"],
            item["excel_reward"],
        )
        for item in products
    )
    personnel_checks = {
        "ten_expected_products": product_facts == EXPECTED_PERSONNEL_PRODUCTS,
        "all_products_pass": all(item["status"] == "pass" for item in products),
        "strict_code_and_barcode_identity": all(
            re.fullmatch(r"CP-[A-Z0-9-]+", item["knowledge_product_code"] or "")
            and _ean13_valid(item["knowledge_barcode_69"])
            and item["knowledge_barcode_69"] == item["excel_barcode_69"]
            for item in products
        ),
        "quantity_and_reward_correspond": all(
            item["excel_quantity"] == item["settlement_quantity"]
            and item["excel_reward"] == item["settlement_reward"]
            and item["strict_result"] == "all_correspond"
            for item in products
        ),
        "only_two_real_issues": personnel["issue_headings"]
        == ["实际申请金额", "收款人与日期"],
        "amounts_are_7350_and_7353": personnel["amount"]
        == {"claimed": "7350", "transferred": "7353"},
        "seven_stores_lack_recipient_mapping_and_full_date": personnel[
            "recipient_and_date"
        ]
        == {
            "store_count": "7",
            "recipient_complete": False,
            "store_mapping_complete": False,
            "date_complete": False,
        },
    }

    display = logic["promotional_display"]
    expected_baseline = {
        "parties": ["东莞市诚成行供应链管理有限公司", "厦门参半商贸有限公司"],
        "budget": "20000",
        "period": ["2026-04-01", "2026-04-30"],
        "store_count": "20",
        "stack_count": "20",
        "seal_pass": True,
        "display_requires_one_square_metre": True,
        "display_requires_four_vertical": True,
        "settlement_requires_real_evidence": True,
        "settlement_as_goods_credit": True,
        "unused_fee_clears_on_termination": True,
    }
    records = display["attachment_records"]
    identities = tuple(
        (
            item["line_no"],
            item["contract"]["product_code"],
            item["contract"]["barcode_69"],
        )
        for item in records
    )
    expected_field_results = {
        "客户名称": "精确匹配",
        "业务日期": "一致",
        "商品编码": "精确匹配",
        "条形码": "精确匹配",
        "数量": "一致",
        "零售价": "一致",
        "合计金额": "一致",
        "合同附件行内金额": "一致",
    }

    def comparable(value: Any) -> str:
        return re.sub(r"\s+", "", str(value or ""))

    paired_fields = (
        "customer_name",
        "business_date",
        "product_code",
        "barcode_69",
        "quantity",
        "retail_price",
        "total_amount",
    )
    store_projection = {
        item["store"]: (
            item["display_status"],
            item["column_threshold"],
            tuple(item["local_error_labels"]),
            item["error_date"],
            item["error_location"],
        )
        for item in display["stores"]
        if item["local_error_labels"]
    }
    display_scenario = _scenario(snapshot, "堆头陈列")
    display_checks = {
        "contract_baseline_fully_recognized": display["contract_baseline"]
        == expected_baseline,
        "contract_attachment_summary_36_of_36": display["attachment_summary"]
        == ["36", "36", "0", "0"],
        "all_36_product_codes_and_barcodes_exact": identities
        == EXPECTED_CONTRACT_IDENTITIES,
        "all_36_barcodes_valid": all(
            _ean13_valid(item["contract"]["barcode_69"])
            for item in records
        ),
        "all_36_contract_rows_pair_to_sales": all(
            all(
                comparable(item["contract"][field])
                == comparable(item["sales"][field])
                for field in paired_fields
            )
            and item["sales_field_results"] == expected_field_results
            for item in records
        ),
        "all_36_use_code_plus_69_and_not_name_as_gate": all(
            item["row_status"] == "issue"
            and item["knowledge"]["barcode_69"] == item["contract"]["barcode_69"]
            and item["knowledge"]["barcode_result"] == "条形码：精确匹配"
            and "不匹配" in item["knowledge"]["product_code_result"]
            and "知识库未登记合同业务编码" in item["knowledge"]["product_code_result"]
            and item["knowledge"]["existence_unmet"]
            for item in records
        ),
        "twenty_contract_stores_present": len(display["stores"]) == 20,
        "only_five_local_store_errors": store_projection
        == EXPECTED_DISPLAY_LOCAL_ERRORS,
        "huadu_display_passes": any(
            item["store"] == "华都超市（东坑大道北店）"
            and item["display_status"] == "meets"
            and item["column_threshold"] == ">=4"
            and not item["local_error_labels"]
            for item in display["stores"]
        ),
        "passing_contract_baseline_hidden": all(
            "合同已识别" not in item["title"]
            and "活动概况" not in item["title"]
            for item in display_scenario["cards"]
        ),
        "no_product_name_mismatch_error": all(
            "商品名称" not in item["title"]
            and "商品名称" not in item["chips"]
            for item in display_scenario["cards"]
        ),
    }

    poster = logic["poster_material"]
    poster_issues = {item["heading"]: item for item in poster["issues"]}
    expected_poster_headings = {
        "合同材料不完整",
        "票据费用明细不完整",
        "金额或项目不一致",
        "现场照片执行证据不完整",
    }
    photo_facts = poster_issues.get("现场照片执行证据不完整", {}).get(
        "facts",
        {},
    )
    observations = photo_facts.get("observations") or []
    poster_checks = {
        "exact_four_blocking_issues": set(poster_issues) == expected_poster_headings
        and len(poster["issues"]) == 4,
        "referenced_contract_attachment_missing": poster_issues.get(
            "合同材料不完整",
            {},
        ).get("facts")
        == {"attachment_referenced": True, "attachment_missing": True},
        "invoice_item_detail_missing": all(
            poster_issues.get("票据费用明细不完整", {})
            .get("facts", {})
            .get(key)
            is True
            for key in ("quantity_missing", "unit_price_missing", "subtotal_missing")
        ),
        "amounts_are_8000_8024_8000": poster_issues.get(
            "金额或项目不一致",
            {},
        ).get("facts", {}).get("合同")
        == "8000.00"
        and poster_issues.get("金额或项目不一致", {})
        .get("facts", {})
        .get("票据")
        == "8024.00"
        and poster_issues.get("金额或项目不一致", {})
        .get("facts", {})
        .get("结算单")
        == "8000.00",
        "photo_quantity_facts_match_standard": {
            key: photo_facts.get(key)
            for key in (
                "submitted_photos",
                "recognized_locations",
                "contract_display_units",
                "visible_display_units",
                "contract_stores",
                "photo_locations",
            )
        }
        == {
            "submitted_photos": "10",
            "recognized_locations": "10",
            "contract_display_units": "90",
            "visible_display_units": "7",
            "contract_stores": "80",
            "photo_locations": "10",
        },
        "ten_independent_watermark_observations": len(observations) == 10
        and len({item["file"] for item in observations}) == 10
        and all(
            item["datetime"]
            and item["location"]
            and item["dimension_evidence_absent"]
            for item in observations
        ),
    }
    return {
        "schema_version_1_1": logic["schema_version"] == "1.1",
        "personnel_incentive": personnel_checks,
        "promotional_display": display_checks,
        "poster_material": poster_checks,
    }


def _all_checks_true(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(value) and all(_all_checks_true(item) for item in value.values())
    if isinstance(value, list):
        return bool(value) and all(_all_checks_true(item) for item in value)
    return value is True


def _rendered_structure(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "body_class": snapshot["bodyClass"],
        "total": snapshot["total"],
        "metrics": snapshot["metrics"],
        "tabs": snapshot["tabs"],
        "scenarios": [
            {
                "title": scenario["title"],
                "impact": scenario["impact"],
                "cards": [
                    {
                        "title": card["title"],
                        "scope": card["scope"],
                        "source": card["fields"].get("问题文件", ""),
                        "baseline": card["fields"].get("对照文件", ""),
                        "chips": card["chips"],
                        "table_locations": [
                            cells[0] for cells in card["tableCells"] if cells
                        ],
                    }
                    for card in scenario["cards"]
                ],
            }
            for scenario in snapshot["scenarios"]
        ],
    }


def verify(reference: Path, candidate: Path) -> dict[str, Any]:
    reference_payload = _audit_payload(reference)
    candidate_payload = _audit_payload(candidate)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        reference_context = browser.new_context(
            viewport=DESKTOP_VIEWPORT,
            locale="zh-CN",
            color_scheme="light",
            reduced_motion="reduce",
            device_scale_factor=1,
        )
        candidate_context = browser.new_context(
            viewport=DESKTOP_VIEWPORT,
            locale="zh-CN",
            color_scheme="light",
            reduced_motion="reduce",
            device_scale_factor=1,
        )
        reference_render = _render(reference_context.new_page(), reference)
        candidate_render = _render(candidate_context.new_page(), candidate)
        reference_context.close()
        candidate_context.close()
        browser.close()

    reference_snapshot = reference_render["snapshot"]
    candidate_snapshot = candidate_render["snapshot"]
    pixel = _pixel_difference(
        reference_render["screenshot"],
        candidate_render["screenshot"],
    )
    reference_structure = _rendered_structure(reference_snapshot)
    candidate_structure = _rendered_structure(candidate_snapshot)
    candidate_logic = _business_logic(candidate_payload)
    reference_rendered_logic = _rendered_business_logic(reference_snapshot)
    candidate_rendered_logic = _rendered_business_logic(candidate_snapshot)
    candidate_internal_checks = _candidate_internal_checks(
        candidate_logic,
        candidate_snapshot,
    )
    exact_checks = {
        "title_equal": reference_snapshot["title"] == candidate_snapshot["title"],
        "rendered_body_equal": reference_snapshot["bodyHtml"] == candidate_snapshot["bodyHtml"],
        "error_style_equal": reference_snapshot["errorStyle"] == candidate_snapshot["errorStyle"],
        "error_projection_equal": (
            reference_snapshot["total"],
            reference_snapshot["metrics"],
            reference_snapshot["tabs"],
            reference_snapshot["scenarios"],
        )
        == (
            candidate_snapshot["total"],
            candidate_snapshot["metrics"],
            candidate_snapshot["tabs"],
            candidate_snapshot["scenarios"],
        ),
        "desktop_pixels_equal": pixel["equal"],
    }
    delivery_standard_checks = {
        "title_equal": exact_checks["title_equal"],
        "body_class_equal": (
            reference_snapshot["bodyClass"] == candidate_snapshot["bodyClass"]
        ),
        "error_style_equal": exact_checks["error_style_equal"],
        "rendered_error_structure_equal": reference_structure == candidate_structure,
        "rendered_business_logic_equal": (
            reference_rendered_logic == candidate_rendered_logic
        ),
        "candidate_internal_business_valid": _all_checks_true(
            candidate_internal_checks
        ),
        "desktop_pixels_equal": exact_checks["desktop_pixels_equal"],
    }
    safety_checks = {
        "candidate_console_clean": not candidate_render["console_errors"],
        "candidate_page_errors_clean": not candidate_render["page_errors"],
        "candidate_external_requests_zero": not candidate_render["external_requests"],
        "candidate_navigation_operable": all(
            (
                candidate_render["interaction"]["scenario_visible"],
                candidate_render["interaction"]["selected_state"] == "true",
                candidate_render["interaction"]["home_visible_after_return"],
                candidate_render["interaction"]["detail_operable"],
            )
        ),
    }
    # Model-authored OCR descriptions may use different but equivalent wording.
    # Literal DOM/text hashes remain visible diagnostics, while acceptance is
    # gated by exact rendered structure, exact business facts/decisions, exact
    # primary-interface pixels, and all runtime safety checks.
    passed = all(delivery_standard_checks.values()) and all(safety_checks.values())
    def scenario_details(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
        return [
            {
                "title": scenario["title"],
                "cards": [
                    {
                        "title": card["title"],
                        "scope": card["scope"],
                        "text": card["text"],
                        "table_rows": card["tableRows"],
                    }
                    for card in scenario["cards"]
                ],
            }
            for scenario in snapshot["scenarios"]
        ]
    return {
        "passed": passed,
        "reference": str(reference),
        "candidate": str(candidate),
        "delivery_standard_checks": delivery_standard_checks,
        "exact_checks": exact_checks,
        "safety_checks": safety_checks,
        "literal_text_diagnostics": {
            "rendered_body_equal": exact_checks["rendered_body_equal"],
            "error_projection_text_equal": exact_checks["error_projection_equal"],
            "acceptance_note": (
                "逐字DOM和模型说明句仅作诊断；正式通过要求渲染结构、业务事实与判断、"
                "首页像素和运行安全全部精确一致。"
            ),
        },
        "reference_rendered_structure": reference_structure,
        "candidate_rendered_structure": candidate_structure,
        "reference_rendered_business_logic": reference_rendered_logic,
        "candidate_rendered_business_logic": candidate_rendered_logic,
        "candidate_internal_checks": candidate_internal_checks,
        "hidden_payload_policy": (
            "基准HTML的隐藏audit-data仅验证可解析，不参与相等判定；它属于旧版中间数据，"
            "正式验收以基准渲染出的错误语义、候选当前业务不变量和像素结果为准。"
        ),
        "reference_projection": {
            "total": reference_snapshot["total"],
            "metrics": reference_snapshot["metrics"],
            "tabs": reference_snapshot["tabs"],
            "scenario_card_counts": [
                len(item["cards"]) for item in reference_snapshot["scenarios"]
            ],
            "scenario_details": scenario_details(reference_snapshot),
        },
        "candidate_projection": {
            "total": candidate_snapshot["total"],
            "metrics": candidate_snapshot["metrics"],
            "tabs": candidate_snapshot["tabs"],
            "scenario_card_counts": [
                len(item["cards"]) for item in candidate_snapshot["scenarios"]
            ],
            "scenario_details": scenario_details(candidate_snapshot),
        },
        "hashes": {
            "reference_body_sha256": _sha256(reference_snapshot["bodyHtml"]),
            "candidate_body_sha256": _sha256(candidate_snapshot["bodyHtml"]),
            "reference_style_sha256": _sha256(reference_snapshot["errorStyle"]),
            "candidate_style_sha256": _sha256(candidate_snapshot["errorStyle"]),
            "reference_screenshot_sha256": _sha256(reference_render["screenshot"]),
            "candidate_screenshot_sha256": _sha256(candidate_render["screenshot"]),
        },
        "pixel_difference": pixel,
        "candidate_diagnostics": {
            "console_errors": candidate_render["console_errors"],
            "page_errors": candidate_render["page_errors"],
            "external_requests": candidate_render["external_requests"],
            "viewport": DESKTOP_VIEWPORT,
        },
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "用 Chromium 对比错误清单交付基准与正式生成 HTML 的渲染结构、"
            "业务逻辑、首页像素和运行安全"
        )
    )
    parser.add_argument("--reference", required=True, type=_path)
    parser.add_argument("--candidate", required=True, type=_path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = verify(args.reference, args.candidate)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
