from __future__ import annotations

import json
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "skills/orchestrate-offline-audit/scripts"))

from audit_core.workbench_html import render_static_run_archive
from audit_core.workbench_store import WorkbenchRunStore, atomic_write_json, reserve_workspace


PORT = int(os.environ.get("WORKBENCH_TEST_PORT", "18080"))
ROOT = Path(os.environ["WORKBENCH_TEST_ROOT"])


def seed() -> tuple[str, WorkbenchRunStore, str]:
    workspace = reserve_workspace(
        "20260828-browser",
        "codex",
        ROOT,
        audit_model="gpt-5.6-sol",
        reasoning_effort="xhigh",
    )
    store = WorkbenchRunStore(
        workspace,
        run_id="20260828-browser",
        producer_model="codex",
        root_html=PROJECT_ROOT / "skills/orchestrate-offline-audit/assets/offline-activity-audit.html",
        workbench_url=f"http://127.0.0.1:{PORT}/",
        audit_model="gpt-5.6-sol",
        reasoning_effort="xhigh",
    )
    store.observe(
        "cases.prepared",
        {
            "scenarios": ["price_difference_support", "maintenance_fee"],
            "cases": {
                "price_difference_support": {
                    "scenario": "price_difference_support",
                    "source_archive": "价格补差样例.zip",
                },
                "maintenance_fee": {
                    "scenario": "maintenance_fee",
                    "source_archive": "维护费用样例.zip",
                },
            },
        },
    )
    store.observe(
        "evidence.validated",
        {
            "scenario": "price_difference_support",
            "evidence": {
                "documents": [
                    {"source_file": "促销合同.pdf", "document_type": "signed_contract"}
                ]
            },
        },
    )
    store.observe(
        "evidence.validated",
        {
            "scenario": "maintenance_fee",
            "evidence": {
                "documents": [
                    {"source_file": "维护合同.pdf", "document_type": "signed_contract"},
                    {"source_file": "POS销售.xlsx", "document_type": "pos_statement"},
                ]
            },
        },
    )
    store.observe(
        "result.validated",
        {
            "scenario": "price_difference_support",
            "result": {
                "scenario": "price_difference_support",
                "summary": {"conclusion": "supplement", "suggested_approved_amount": 0},
                "price_difference_support_audit": {
                    "controls": [
                        {
                            "control_id": "fee_nature",
                            "status": "pass",
                            "basis": "合同和结算正文均明确价格补差",
                            "confidence_score": 1,
                        },
                        {
                            "control_id": "product_correspondence",
                            "status": "pass",
                            "basis": "合同与结算商品范围一致",
                        },
                        {
                            "control_id": "all_store_photo_coverage",
                            "status": "pass",
                            "basis": "合同门店均有活动期内现场照片",
                        },
                        {
                            "control_id": "price_terms",
                            "status": "pass",
                            "basis": "合同原价、活动价和补差单价均明确",
                        },
                        {
                            "control_id": "amount_recalculation",
                            "status": "fail",
                            "basis": "金额复算条件不完整",
                        },
                    ]
                },
            },
        },
    )
    store.observe(
        "result.validated",
        {
            "scenario": "maintenance_fee",
            "result": {
                "scenario": "maintenance_fee",
                "summary": {"conclusion": "supplement", "suggested_approved_amount": 0},
                "maintenance_fee_audit": {
                    "controls": [
                        {
                            "control_id": "promotional_contract",
                            "status": "pass",
                            "basis": "维护合同主体、期间与签章完整",
                        },
                        {
                            "control_id": "pos_spreadsheet",
                            "status": "pass",
                            "basis": "POS销售明细可识别且期间一致",
                            "confidence": "medium",
                        },
                    ]
                },
            },
        },
    )
    store.complete(
        view_payload={
            "schema_version": "1.1",
            "title": "线下活动核销结果",
            "sheets": [
                {
                    "name": "补差核销",
                    "scenario": "price_difference_support",
                    "title": "价格补差核销｜只显示错误",
                    "note": "浏览器验收数据",
                    "headers": [
                        "问题文件",
                        "对照文件",
                        "错误原因",
                        "处理方式",
                        "核销影响",
                        "结论",
                    ],
                    "rows": [
                        {
                            "excel_row": 4,
                            "kind": "record",
                            "section": "detail",
                            "status": "issue",
                            "confidence": "high",
                            "confidence_score": 1,
                            "heading": "补差金额无法复算",
                            "values": [
                                "价格补差结算单.jpg",
                                "促销合同.pdf",
                                "结算申报金额与合同补差单价乘以有效数量不一致",
                                "补交金额计算正确并完成盖章的结算单",
                                "本次申报暂不能核销",
                                "资料需补正",
                            ],
                        }
                    ],
                    "audit_counts": {
                        "source_row_count": 1,
                        "error_count": 1,
                        "detail_error_count": 1,
                        "context_error_count": 0,
                    },
                },
                {
                    "name": "维护费用核销",
                    "scenario": "maintenance_fee",
                    "title": "维护费用核销｜只显示错误",
                    "note": "联动筛选浏览器验收数据",
                    "headers": [
                        "问题文件",
                        "对照文件",
                        "错误原因",
                        "处理方式",
                        "核销影响",
                        "结论",
                    ],
                    "rows": [
                        {
                            "excel_row": 5,
                            "kind": "record",
                            "section": "detail",
                            "status": "issue",
                            "confidence": "medium",
                            "heading": "POS销售明细缺少盖章页",
                            "values": [
                                "POS销售.xlsx",
                                "维护合同.pdf",
                                "现有POS销售明细没有可核验的盖章页",
                                "补交包含完整期间并盖章的POS销售明细",
                                "本次申报暂不能核销",
                                "资料需补正",
                            ],
                        }
                    ],
                    "audit_counts": {
                        "source_row_count": 1,
                        "error_count": 1,
                        "detail_error_count": 1,
                        "context_error_count": 0,
                    },
                },
            ],
        },
        scenarios=["price_difference_support", "maintenance_fee"],
        verification={"html": {"verified": True}},
    )
    completed_workspace_id = workspace.name
    running_workspace = reserve_workspace(
        "20260829-browser-running",
        "qwen3.8",
        ROOT,
        audit_model="qwen3.8",
        reasoning_effort="max",
    )
    running_store = WorkbenchRunStore(
        running_workspace,
        run_id="20260829-browser-running",
        producer_model="qwen3.8",
        root_html=PROJECT_ROOT / "skills/orchestrate-offline-audit/assets/offline-activity-audit.html",
        workbench_url=f"http://127.0.0.1:{PORT}/",
        audit_model="qwen3.8",
        reasoning_effort="max",
    )
    running_store.observe(
        "cases.prepared",
        {
            "scenarios": ["maintenance_fee"],
            "cases": {"maintenance_fee": {"scenario": "maintenance_fee"}},
        },
    )
    running_store.observe("scenario.started", {"scenario": "maintenance_fee"})
    running_store.observe(
        "evidence.validated",
        {"scenario": "maintenance_fee", "evidence": {"documents": []}},
    )
    return completed_workspace_id, running_store, running_workspace.name


def main() -> None:
    if not ROOT.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("Destructive browser smoke fixtures must be inside the system temporary directory")
    local_date_before = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    workspace_id, running_store, running_workspace_id = seed()
    local_date_after = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d")
    allowed_dates = {local_date_before, local_date_after}
    assert workspace_id[:8] in allowed_dates
    assert workspace_id.endswith("-gpt5.6sol_xhigh")
    assert running_workspace_id[:8] in allowed_dates
    assert running_workspace_id.endswith("-qwen3.8_max")
    console_errors: list[str] = []
    page_errors: list[str] = []
    screenshot_root = Path(tempfile.gettempdir()) / "offline-audit-workbench-smoke"
    screenshot_root.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        page.on("pageerror", lambda error: page_errors.append(str(error)))
        page.goto(f"http://127.0.0.1:{PORT}/")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("body.audit-system-page")
        assert page.locator(".as-rail").evaluate(
            "node => getComputedStyle(node).backgroundColor"
        ) == "rgb(16, 39, 49)"
        page.wait_for_selector("#as-ledger-list .as-run-card")
        assert page.get_by_role("heading", name="核销管理中心").first.is_visible()
        overview_completed_metric = page.locator("#as-metrics .as-metric").filter(
            has_text="核销完成"
        )
        assert overview_completed_metric.count() == 1
        assert overview_completed_metric.locator("small").inner_text() == "最近一次 input 的 ZIP 总数"
        assert overview_completed_metric.locator(":scope > strong").inner_text() == "2"
        assert page.locator("[data-as-view]").count() == 2
        assert page.locator('.as-nav[role="tablist"]').count() == 1
        assert page.locator('.as-nav [role="tab"]').count() == 2
        assert page.locator('.as-view[role="tabpanel"]').count() == 2
        assert page.locator('.as-nav [role="tab"]').evaluate_all(
            "nodes => nodes.map(node => node.tabIndex)"
        ) == [0, -1]
        assert page.locator("#as-ledger-list .as-run-card").count() == 2
        assert page.locator("#as-archive-list .as-archive-card").count() == 0
        overview_tab = page.locator('[data-as-view="overview"]')
        overview_tab.focus()
        overview_tab.press("ArrowDown")
        assert page.locator('[data-as-view="archive"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#as-view-archive").is_visible()
        assert page.locator("#as-ledger-list .as-run-card").count() == 0
        page.locator('[data-as-view="archive"]').press("Home")
        assert page.locator('[data-as-view="overview"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#as-view-overview").is_visible()
        assert page.locator('[data-as-view="monitor"]').count() == 0
        assert page.locator("#as-monitor-list").count() == 0
        assert page.locator("[data-runtime-stage]").count() == 0
        assert page.get_by_role("heading", name="核销运行链路").count() == 0
        assert page.get_by_role("heading", name="核销台账", exact=True).is_visible()
        assert page.get_by_role("heading", name="最近核销记录").count() == 0
        assert page.locator("#as-tab-ledger, #as-view-ledger, #as-recent-list").count() == 0
        assert page.locator("#as-archive-list [data-as-open]").count() == 0
        assert page.locator("#as-ledger-list .as-run-card").count() == 2
        assert page.locator("#as-archive-list button").count() == 0
        page.locator('[data-as-view="archive"]').click()
        assert page.locator("#as-archive-list button").count() == 2
        assert page.locator("#as-nav-archive").inner_text() == page.locator("#as-nav-overview").inner_text() == "2"
        assert page.locator('#as-archive-list[role="list"]').count() == 1
        page.locator('[data-as-view="overview"]').click()
        assert page.locator("#as-archive-list .as-archive-card").count() == 0
        pending_metric = page.locator("#as-metrics .as-metric").filter(
            has_text="待人工核验"
        )
        assert pending_metric.locator(":scope > strong").inner_text() == "2"

        page.locator("#as-view-overview").evaluate(
            "node => node.dataset.sameStageSentinel = 'keep'"
        )
        running_store.observe("scenario.started", {"scenario": "maintenance_fee"})
        page.wait_for_timeout(3200)
        assert (
            page.locator("#as-view-overview").get_attribute("data-same-stage-sentinel")
            == "keep"
        )

        running_store.observe(
            "evidence.validated",
            {"scenario": "maintenance_fee", "evidence": {"documents": []}},
        )
        page.wait_for_timeout(3200)
        assert (
            page.locator("#as-view-overview").get_attribute("data-same-stage-sentinel")
            == "keep"
        )
        page.screenshot(path=str(screenshot_root / "desktop.png"), full_page=True)

        page.locator('[data-as-view="overview"]').click()
        page.wait_for_selector(f'#as-ledger-list [data-as-open="{workspace_id}"]')
        assert page.locator('#as-ledger-list[role="list"]').count() == 1
        assert page.locator("#as-ledger-list [data-as-technical]").count() == 0
        assert page.get_by_role("heading", name="核销台账").is_visible()
        assert page.locator("#as-status-filter").count() == 0
        assert page.locator("#as-ledger-list [data-as-open]").count() == 2
        assert page.locator("#as-ledger-list [data-as-delete]").count() == 2
        assert page.locator(
            f'#as-ledger-list [data-run-card="{running_workspace_id}"]'
        ).count() == 1
        delete_trigger = page.locator(f'#as-ledger-list [data-as-delete="{workspace_id}"]')
        assert delete_trigger.is_enabled()
        delete_trigger.click()
        assert page.get_by_role("dialog", name="永久删除这条核销记录？").is_visible()
        assert page.locator("#as-delete-workspace").inner_text() == workspace_id
        assert page.locator("#as-delete-cancel").evaluate("node => node === document.activeElement")
        page.screenshot(path=str(screenshot_root / "desktop-delete-confirm.png"), full_page=True)
        page.keyboard.press("Escape")
        assert page.locator("#as-delete-dialog").is_hidden()
        assert delete_trigger.evaluate("node => node === document.activeElement")
        assert (ROOT / workspace_id).is_dir()
        review = page.locator(f'#as-ledger-list [data-as-review="{workspace_id}"]')
        assert review.count() == 1
        assert not review.is_checked()
        review.check()
        page.wait_for_function(
            "() => document.querySelector('#as-metrics .as-metric:last-child > strong')?.textContent === '0'"
        )
        assert review.is_checked()
        assert "已人工核验" in page.locator(
            f'#as-ledger-list [data-run-card="{workspace_id}"]'
        ).inner_text()
        assert page.locator(
            f'#as-ledger-list [data-run-card="{workspace_id}"] .as-run-count strong'
        ).inner_text() == "✓"
        review.uncheck()
        page.wait_for_function(
            "() => document.querySelector('#as-metrics .as-metric:last-child > strong')?.textContent === '2'"
        )
        assert not review.is_checked()
        page.screenshot(path=str(screenshot_root / "desktop-ledger.png"), full_page=True)
        page.locator(f'#as-ledger-list [data-as-open="{workspace_id}"]').click()
        page.wait_for_load_state("networkidle")
        assert page.locator("#as-record-select option").evaluate_all("nodes => nodes.map(node => node.value)") == [running_workspace_id, workspace_id]
        page.wait_for_selector("body.error-only-page")
        assert f"run={workspace_id}" in page.url
        assert page.get_by_role("region", name="筛选错误检查项").is_visible()
        assert page.locator(".eo-error-filter-panel select").count() == 2
        assert page.locator(".eo-error-filter-panel input").count() == 0
        assert page.locator(".eo-queue-head").count() == 0
        assert page.locator(".eo-pass-filter-head, #eoErrorVisible, #eoPassVisible").count() == 0
        assert page.locator(".eo-pass-filter-panel [data-error-reset], .eo-pass-filter-panel [data-pass-reset]").count() == 0
        assert page.locator("#home .eo-metric").count() == 0
        assert page.locator("#home .eo-cockpit-hero, #home .eo-gauge-stat").count() == 0
        assert page.locator("#eoErrorList .eo-error-card").count() == 2
        assert page.locator("#eoErrorList .eo-error-card").evaluate_all(
            "nodes => nodes.every(node => /^错误项 \\d+；核销类型：/.test(node.getAttribute('aria-label') || ''))"
        )
        assert page.locator("#eoErrorList .eo-scope").count() == 2
        assert page.locator("#eoErrorList .eo-error-reason").count() == 2
        assert page.locator("#eoErrorList .eo-error-head .eo-error-reason").count() == 0
        assert page.locator("#eoErrorList .eo-field .eo-error-reason").count() == 2
        assert page.locator("#eoErrorList .eo-error-confidence").count() == 0
        assert page.locator("#eoErrorList .eo-error-card").evaluate_all(
            "nodes => nodes.map(node => node.dataset.errorConfidenceScore)"
        ) == ["1", "0.69"]
        assert "核销类型 · 价格补差" in page.locator("#eoErrorList .eo-scope").first.inner_text()
        assert page.locator("#eoErrorList .eo-error-reason").first.inner_text() == "金额复算"
        assert page.locator("#eoErrorList .eo-error-reason").first.evaluate(
            "node => node.classList.contains('eo-chip')"
        )
        fixed_error_types = ["", "价格补差", "维护费用"]
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        assert page.locator("#eoErrorList .eo-error-card").evaluate_all(
            "(nodes, types) => nodes.every(node => types.includes(node.dataset.auditType))",
            fixed_error_types,
        )
        assert page.locator("#eoErrorCategory option").evaluate_all(
            "(options, cards) => options.filter(option => option.value).every(option => cards.some(card => card.categories.split('|').includes(option.value) && card.type))",
            page.locator("#eoErrorList .eo-error-card").evaluate_all(
                "nodes => nodes.map(node => ({ categories: node.dataset.errorCategories, type: node.dataset.auditType }))"
            ),
        )
        assert page.locator("#eoErrorCategory option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == ["", "POS销售明细", "金额复算"]
        assert page.locator("#eoErrorFacetSummary").count() == 0
        assert page.locator("#eoErrorList .eo-error-card:visible").count() == 2
        page.locator("#eoErrorCategory").select_option(value="金额复算")
        assert page.locator("#eoErrorList .eo-error-card:visible").count() == 1
        assert "价格补差" in page.locator("#eoErrorList .eo-error-card:visible").inner_text()
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        page.locator("#eoErrorCategory").select_option(value="")
        assert page.locator("#eoErrorConfidence").count() == 0
        page.locator("#eoErrorCategory").select_option(value="POS销售明细")
        assert page.locator("#eoErrorList .eo-error-card:visible").count() == 1
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        assert page.locator("#eoErrorCategory option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == ["", "POS销售明细", "金额复算"]
        assert "维护费用" in page.locator("#eoErrorList .eo-error-card:visible").inner_text()
        page.locator("#eoErrorType").select_option(value="价格补差")
        assert page.locator("#eoErrorCategory").input_value() == ""
        assert page.locator("#eoErrorCategory option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == ["", "金额复算"]
        assert page.locator("#eoErrorList .eo-error-card:visible").count() == 1
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        page.locator("#eoErrorType").select_option(value="")
        page.locator("#eoErrorCategory").select_option(value="")
        assert page.locator("#eoErrorList .eo-error-card:visible").count() == 2
        page.wait_for_timeout(200)
        page.screenshot(path=str(screenshot_root / "desktop-error-log.png"), full_page=True)
        assert page.locator(".eo-scenario-card").count() == 0
        assert page.locator("button.eo-tab").count() == 2
        assert page.locator('.eo-rail-nav[role="tablist"]').count() == 1
        assert page.locator('.eo-rail-nav [role="tab"]').count() == 2
        assert page.locator('.eo-view[role="tabpanel"]').count() == 2
        assert page.locator('.eo-rail-nav [role="tab"]').evaluate_all(
            "nodes => nodes.map(node => node.tabIndex)"
        ) == [0, -1]
        assert page.locator("#eoPassList .eo-pass-card").count() == 0
        error_tab = page.locator('[data-eo-view="home"]')
        error_tab.focus()
        error_tab.press("ArrowRight")
        assert page.locator('[data-eo-view="passed"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#passed").is_visible()
        page.locator('[data-eo-view="passed"]').press("Home")
        assert page.locator('[data-eo-view="home"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#home").is_visible()
        assert page.locator(".eo-enter").count() == 0
        assert page.locator('[data-open^="scenario-"]').count() == 0

        page.get_by_role("tab", name="正确检查项").click()
        assert page.get_by_role("region", name="筛选正确检查项").is_visible()
        assert page.locator("#passed .eo-pass-filter-panel select").count() == 2
        assert page.locator("#passed .eo-pass-filter-panel input").count() == 0
        assert page.locator("#home").is_hidden()
        assert page.locator("#passed").is_visible()
        assert page.locator("#passed .eo-metric").count() == 0
        assert page.locator("#passed .eo-cockpit-hero, #passed .eo-gauge-stat").count() == 0
        assert page.locator("#eoPassList .eo-pass-group").count() == 2
        assert page.locator("#eoPassList .eo-pass-card").count() == 6
        assert page.locator("#eoPassList .eo-pass-card").evaluate_all(
            "nodes => nodes.every(node => node.getAttribute('aria-label')?.startsWith('正确检查项 '))"
        )
        assert page.locator("#eoPassList .eo-pass-card").first.evaluate(
            "node => getComputedStyle(node).contentVisibility"
        ) == "auto"
        assert page.locator("#eoPassList .eo-pass-card").evaluate_all(
            "nodes => nodes.map(node => node.dataset.passConfidenceScore)"
        ).count("1") == 1
        assert page.locator("#eoPassList .eo-pass-confidence").count() == 0
        medium_card = page.locator('#eoPassList .eo-pass-card[data-pass-confidence="medium"]')
        expect(medium_card).to_have_attribute("data-pass-confidence-score", "0.77")
        assert page.locator("#eoPassConfidence").count() == 0
        assert "费用类型核验通过" in "\n".join(
            page.locator("#eoPassList .eo-pass-card").all_inner_texts()
        )
        assert page.locator(".eo-pass-warning").count() == 0
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 6
        assert page.locator("#eoPassType option").count() == 3
        assert page.locator("#eoPassCategory option").count() == 7
        assert "2 种核销方式 · 6 类可选检查" in page.locator(
            "#eoPassFacetSummary"
        ).inner_text()
        fixed_pass_types = ["", "价格补差", "维护费用"]
        page.locator("#eoPassType").select_option(value="价格补差")
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 4
        assert page.locator("#eoPassCategory option").count() == 5
        assert page.locator("#eoPassCategory option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == ["", "费用性质", "商品对应", "门店照片", "补差条款"]
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 4
        page.locator("#eoPassCategory").select_option(value="商品对应")
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 1
        assert page.locator("#eoPassType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_pass_types
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 1
        assert "商品对应关系核验通过" in page.locator(
            "#eoPassList .eo-pass-card:visible"
        ).inner_text()
        page.locator("#eoPassCategory").select_option(value="")
        page.locator("#eoPassType").select_option(value="")
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 6
        page.locator("#eoPassCategory").select_option(value="合同基准")
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 1
        assert page.locator("#eoPassType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_pass_types
        page.locator("#eoPassType").select_option(value="价格补差")
        assert page.locator("#eoPassCategory").input_value() == ""
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 4
        assert page.locator("#eoPassType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_pass_types
        page.locator("#eoPassType").select_option(value="")
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 6
        page.wait_for_timeout(350)
        page.screenshot(path=str(screenshot_root / "desktop-pass-log.png"), full_page=True)

        page.get_by_role("tab", name="错误总览").click()
        assert page.get_by_role("region", name="筛选错误检查项").is_visible()
        assert page.locator(".eo-error-filter-panel select").count() == 2
        assert page.locator(".eo-error-filter-panel input").count() == 0
        assert page.locator("#passed").is_hidden()

        technical_trigger = page.get_by_role("button", name="技术档案")
        technical_trigger.click()
        assert page.locator("#as-drawer-close").evaluate("node => node === document.activeElement")
        assert page.locator('#as-drawer-backdrop [role="tablist"] [role="tab"]').count() == 4
        page.get_by_role("tab", name="分析文件").click()
        page.locator(
            '[data-as-analysis="analysis/results/price_difference_support.json"]'
        ).click()
        page.wait_for_function(
            "document.querySelector('#as-analysis-view').textContent.includes('price_difference_support')"
        )
        assert page.locator("#as-analysis-view").get_attribute("role") == "region"
        assert page.locator("#as-analysis-view").get_attribute("tabindex") == "0"
        assert page.locator(".as-drawer-body").evaluate(
            "node => node.scrollHeight === node.clientHeight"
        )

        page.get_by_role("tab", name="DOM 断点").click()
        page.locator("[data-as-checkpoint]").first.click()
        page.wait_for_function(
            "document.querySelector('#as-checkpoint-view').textContent.includes('checkpoint_id')"
        )

        page.get_by_role("tab", name="完整日志").click()
        page.wait_for_function(
            "document.querySelector('[data-as-tech-panel=\"log\"] .as-code').textContent.includes('核销完成')"
        )
        page.get_by_role("button", name="关闭").click()
        assert technical_trigger.evaluate("node => node === document.activeElement")

        page.get_by_role("button", name="← 返回系统总览").click()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("body.audit-system-page")
        assert page.locator("#as-view-overview").is_visible()

        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#as-view-overview:not([hidden])")
        assert page.locator('[data-as-view="overview"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#as-view-archive").is_hidden()

        page.goto(f"http://127.0.0.1:{PORT}/?run={running_workspace_id}")
        page.wait_for_load_state("networkidle")
        page.wait_for_selector(".as-record-page")
        page.locator(".as-record-page").evaluate(
            "node => node.dataset.sameStageSentinel = 'keep'"
        )
        running_store.observe(
            "evidence.validated",
            {"scenario": "maintenance_fee", "evidence": {"documents": []}},
        )
        page.wait_for_timeout(3200)
        assert (
            page.locator(".as-record-page").get_attribute("data-same-stage-sentinel")
            == "keep"
        )
        running_store.observe(
            "result.validated",
            {
                "scenario": "maintenance_fee",
                "result": {"scenario": "maintenance_fee", "summary": {}},
            },
        )
        page.wait_for_function(
            """() => {
              const page = document.querySelector('.as-record-page');
              return page && !page.dataset.sameStageSentinel;
            }"""
        )

        # Derive the fixture's only static page from the canonical source and
        # compare the same served/standalone business views before deleting it.
        archive = render_static_run_archive(ROOT / workspace_id, PROJECT_ROOT / 'skills/orchestrate-offline-audit/assets/offline-activity-audit.html')
        assert list((ROOT / workspace_id).rglob("*.html")) == [archive]
        assert not list((ROOT / workspace_id).rglob("*.xlsx"))
        page.goto(archive.as_uri())
        page.wait_for_selector("#as-view-overview:not([hidden])")
        assert page.locator("body.error-only-page").count() == 0
        assert page.locator('[data-as-view]').count() == 2
        page.screenshot(path=str(screenshot_root / "static-level-one.png"), full_page=True, animations="disabled")
        page.locator('[data-as-view="overview"]').click()
        assert page.locator('[data-as-delete]').count() == 0
        assert page.locator('[data-as-review]').is_disabled()
        page.locator(f'[data-as-open="{workspace_id}"]').click()
        page.wait_for_selector("body.error-only-page")
        assert page.url.startswith("file:")
        page.locator('#as-back-ledger').click()
        page.wait_for_selector("#as-view-overview:not([hidden])")
        assert page.url == archive.as_uri()
        representations = []
        for label, url in (
            ("served", f"http://127.0.0.1:{PORT}/?run={workspace_id}"),
            ("static", archive.as_uri() + f"?run={workspace_id}"),
        ):
            page.goto(url)
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("body.error-only-page")
            assert page.locator("[data-as-delete]").count() == 0
            errors = page.locator("#eoErrorList .eo-error-card").all_text_contents()
            page.screenshot(path=str(screenshot_root / f"delete-{label}-error.png"), full_page=True, animations="disabled")
            page.get_by_role("tab", name="正确检查项").click()
            passes = page.locator("#eoPassList .eo-pass-card").all_text_contents()
            page.screenshot(path=str(screenshot_root / f"delete-{label}-pass.png"), full_page=True, animations="disabled")
            representations.append((errors, passes))
        assert representations[0] == representations[1]

        page.goto(f"http://127.0.0.1:{PORT}/")
        page.wait_for_load_state("networkidle")
        page.locator('[data-as-view="overview"]').click()
        page.locator(f'[data-as-review="{workspace_id}"]').check()
        review_path = ROOT / ".reviews" / f"{workspace_id}.json"
        page.wait_for_function("() => document.querySelector('#as-metrics .as-metric:last-child > strong')?.textContent === '0'")
        assert review_path.is_file()
        stale_catalog = page.request.get(f"http://127.0.0.1:{PORT}/api/runs").text()
        held_probes = []
        page.route("**/api/runs", lambda route: held_probes.append(route))
        with page.expect_request("**/api/runs", timeout=20000):
            pass
        page.wait_for_timeout(100)
        assert held_probes, "A pre-deletion catalog response must be held for the race check"
        assert any(run["workspace_id"] == workspace_id for run in json.loads(stale_catalog)["runs"])
        page.locator(f'[data-as-delete="{workspace_id}"]').click()
        job_id = "b" * 24
        receipt = ROOT / ".intake" / "jobs" / f"{job_id}.json"
        source = ROOT.parent / "input-oss" / job_id
        source.mkdir(parents=True)
        (source / "fixture.zip").write_bytes(b"owned browser fixture")
        job = {"job_id": job_id, "status": "callback", "result": {"workspace_id": workspace_id}}
        atomic_write_json(receipt, job)
        before_errors = len(console_errors)
        page.locator("#as-delete-confirm").click()
        page.locator("#as-delete-error").filter(has_text="仍在运行或回调").wait_for()
        assert page.locator("#as-delete-dialog").is_visible()
        assert (ROOT / workspace_id / "manifest.json").is_file()
        expected_errors = console_errors[before_errors:]
        assert len(expected_errors) == 1 and "409" in expected_errors[0], expected_errors
        del console_errors[before_errors:]
        atomic_write_json(receipt, dict(job, status="completed"))
        page.locator("#as-delete-confirm").click()
        page.wait_for_selector("#as-delete-dialog", state="hidden")
        assert page.locator(f'#as-ledger-list [data-run-card="{workspace_id}"]').count() == 0
        assert page.locator("#as-overview-date-from").evaluate("node => node === document.activeElement")
        assert all(not path.exists() for path in (ROOT / workspace_id, review_path, receipt, source))
        for route in held_probes:
            route.fulfill(status=200, content_type="application/json", body=stale_catalog)
        page.unroute("**/api/runs")
        page.wait_for_timeout(3200)
        assert page.locator(f'#as-ledger-list [data-run-card="{workspace_id}"]').count() == 0
        assert (ROOT / running_workspace_id).is_dir()

        running_store.fail(RuntimeError("terminal fixture for deletion"))
        page.wait_for_timeout(3200)
        assert page.locator(
            f'#as-ledger-list [data-run-card="{running_workspace_id}"]'
        ).count() == 1
        page.locator('[data-as-view="archive"]').click()
        assert page.locator("#as-archive-list").get_by_text(
            running_workspace_id, exact=True
        ).count() == 1
        deletion_token = page.evaluate(
            "async () => (await fetch('/api/config')).json()"
        )["run_deletion"]["confirmation_token"]
        deleted = page.evaluate(
            """async ({ workspaceId, token }) => {
              const response = await fetch(`/api/runs/${encodeURIComponent(workspaceId)}`, {
                method: 'DELETE',
                headers: {
                  'Content-Type': 'application/json',
                  'X-Offline-Audit-Delete-Token': token,
                },
                body: JSON.stringify({ confirm_workspace_id: workspaceId }),
              });
              return { status: response.status, body: await response.json() };
            }""",
            {"workspaceId": running_workspace_id, "token": deletion_token},
        )
        assert deleted["status"] == 200, deleted
        assert not (ROOT / running_workspace_id).exists()
        page.reload()
        page.wait_for_load_state("networkidle")
        assert page.locator("#as-ledger-list .as-run-card").count() == 0
        assert page.locator("#as-metrics .as-metric").first.locator("strong").inner_text() == "0"
        page.screenshot(path=str(screenshot_root / "desktop-after-delete.png"), full_page=True)
        browser.close()

    assert not console_errors, console_errors
    assert not page_errors, page_errors
    print(
        {
            "workspace_id": workspace_id,
            "desktop": str(screenshot_root / "desktop.png"),
            "refresh_default": "overview",
            "viewport": "1440x960",
            "permanent_delete": "completed via ledger + failed via run API; owned OSS data; empty catalog",
            "expected_rejection": 409,
            "static_matches_served": representations[0] == representations[1],
            "console_errors": console_errors,
            "page_errors": page_errors,
        }
    )


if __name__ == "__main__":
    main()
