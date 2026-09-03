from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.workbench_store import WorkbenchRunStore, reserve_workspace


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
        root_html=Path("offline-activity-audit.html"),
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
        root_html=Path("offline-activity-audit.html"),
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
    workspace_id, running_store, running_workspace_id = seed()
    assert workspace_id.startswith("20260828_")
    assert workspace_id.endswith("-gpt5.6sol_xhigh")
    assert running_workspace_id.startswith("20260829_")
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
        page.wait_for_selector("#as-recent-list .as-run-card")
        assert page.get_by_role("heading", name="核销管理中心").first.is_visible()
        overview_completed_metric = page.locator("#as-metrics .as-metric").filter(
            has_text="核销完成"
        )
        assert overview_completed_metric.count() == 1
        assert overview_completed_metric.locator("small").inner_text() == "最近一次 input 的 ZIP 总数"
        assert overview_completed_metric.locator(":scope > strong").inner_text() == "2"
        assert page.locator("[data-as-view]").count() == 3
        assert page.locator('[data-as-view="monitor"]').count() == 0
        assert page.locator("#as-monitor-list").count() == 0
        assert page.locator("[data-runtime-stage]").count() == 0
        assert page.get_by_role("heading", name="核销运行链路").count() == 0
        assert page.get_by_role("heading", name="最近核销记录").is_visible()
        assert page.locator("#as-recent-list button").count() == 0
        assert page.locator("#as-recent-list [data-as-review]").count() == 0
        assert page.locator("#as-archive-list [data-as-open]").count() == 0
        assert page.locator("#as-archive-list button").count() == 2
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

        page.locator('[data-as-view="ledger"]').click()
        page.wait_for_selector(f'#as-ledger-list [data-as-open="{workspace_id}"]')
        assert page.locator("#as-ledger-list [data-as-technical]").count() == 0
        assert page.locator("#as-ledger-list button").count() == 2
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
        page.wait_for_selector("body.error-only-page")
        assert f"run={workspace_id}" in page.url
        assert page.get_by_role("heading", name="核销错误处置总览").is_visible()
        assert page.get_by_role("heading", name="核销错误结果").is_visible()
        assert page.locator(".eo-metric").first.locator(".eo-metric-copy > span").inner_text() == "核销类型"
        assert page.locator("#eoErrorList .eo-error-card").count() == 2
        assert page.locator("#eoErrorList .eo-scope").count() == 2
        assert page.locator("#eoErrorList .eo-error-reason").count() == 2
        assert page.locator("#eoErrorList .eo-error-head .eo-error-reason").count() == 0
        assert page.locator("#eoErrorList .eo-field .eo-error-reason").count() == 2
        assert page.locator("#eoErrorList .eo-error-confidence").count() == 2
        assert page.locator("#eoErrorList .eo-error-confidence").all_inner_texts() == [
            "置信度 高：1",
            "置信度 中：0.69",
        ]
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
        assert "2 种核销方式 · 2 类可选错误原因 · 2 档可选置信度" in page.locator(
            "#eoErrorFacetSummary"
        ).inner_text()
        assert page.locator("#eoErrorVisible").inner_text() == "2"
        page.locator("#eoErrorCategory").select_option(value="金额复算")
        assert page.locator("#eoErrorVisible").inner_text() == "1"
        assert "价格补差" in page.locator("#eoErrorList .eo-error-card:visible").inner_text()
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        page.locator(
            ".eo-error-filter-panel .eo-pass-filter-result [data-error-reset]"
        ).click()
        page.locator("#eoErrorConfidence").select_option(value="medium")
        assert page.locator("#eoErrorVisible").inner_text() == "1"
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        assert page.locator("#eoErrorCategory option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == ["", "POS销售明细"]
        assert "维护费用" in page.locator("#eoErrorList .eo-error-card:visible").inner_text()
        page.locator("#eoErrorType").select_option(value="价格补差")
        assert page.locator("#eoErrorConfidence").input_value() == ""
        assert page.locator("#eoErrorCategory option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == ["", "金额复算"]
        assert page.locator("#eoErrorVisible").inner_text() == "1"
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        page.locator("#eoErrorKeyword").fill("不会命中的错误检查项")
        assert page.locator("#eoErrorVisible").inner_text() == "0"
        assert page.locator("#eoErrorFilterEmpty").is_visible()
        assert page.locator("#eoErrorType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_error_types
        page.locator("#eoErrorFilterEmpty [data-error-reset]").click()
        assert page.locator("#eoErrorVisible").inner_text() == "2"
        page.wait_for_timeout(200)
        page.screenshot(path=str(screenshot_root / "desktop-error-log.png"), full_page=True)
        assert page.locator(".eo-scenario-card").count() == 0
        assert page.locator("button.eo-tab").count() == 2
        assert page.locator(".eo-enter").count() == 0
        assert page.locator('[data-open^="scenario-"]').count() == 0

        page.get_by_role("button", name="正确检查项").click()
        assert page.get_by_role("heading", name="正确检查项日志").is_visible()
        assert page.locator("#home").is_hidden()
        assert page.locator("#passed").is_visible()
        assert page.locator("#eoPassList .eo-pass-group").count() == 2
        assert page.locator("#eoPassList .eo-pass-card").count() == 6
        assert page.locator("#eoPassList .eo-pass-card").evaluate_all(
            "nodes => nodes.map(node => node.dataset.passConfidenceScore)"
        ).count("1") == 1
        assert page.locator("#eoPassList .eo-pass-confidence").evaluate_all(
            "nodes => nodes.every(node => /^置信度 [高中低]：(?:0(?:\\.\\d+)?|1)$/.test(node.textContent.trim()))"
        )
        assert page.locator('#eoPassList .eo-pass-card[data-pass-confidence="medium"] .eo-pass-confidence').inner_text() == "置信度 中：0.77"
        assert page.locator("#eoPassConfidence option").evaluate_all(
            r"nodes => nodes.every(node => !/[：:]\s*0\./.test(node.textContent))"
        )
        assert "费用类型核验通过" in "\n".join(
            page.locator("#eoPassList .eo-pass-card").all_inner_texts()
        )
        assert page.locator(".eo-pass-warning").count() == 0
        assert page.locator("#eoPassVisible").inner_text() == "6"
        assert page.locator("#eoPassType option").count() == 3
        assert page.locator("#eoPassCategory option").count() == 7
        assert "2 种核销方式 · 6 类可选检查 · 2 档可选置信度" in page.locator(
            "#eoPassFacetSummary"
        ).inner_text()
        fixed_pass_types = ["", "价格补差", "维护费用"]
        page.locator("#eoPassType").select_option(value="价格补差")
        assert page.locator("#eoPassVisible").inner_text() == "4"
        assert page.locator("#eoPassCategory option").count() == 5
        assert page.locator("#eoPassCategory option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == ["", "费用性质", "商品对应", "门店照片", "补差条款"]
        page.locator("#eoPassConfidence").select_option(value="high")
        assert page.locator("#eoPassVisible").inner_text() == "4"
        page.locator("#eoPassCategory").select_option(value="商品对应")
        assert page.locator("#eoPassVisible").inner_text() == "1"
        assert page.locator("#eoPassType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_pass_types
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 1
        assert "商品对应关系核验通过" in page.locator(
            "#eoPassList .eo-pass-card:visible"
        ).inner_text()
        page.locator("#eoPassKeyword").fill("不会命中的检查项")
        assert page.locator("#eoPassVisible").inner_text() == "0"
        assert page.locator("#eoPassFilterEmpty").is_visible()
        page.get_by_role("button", name="清除全部筛选").click()
        assert page.locator("#eoPassVisible").inner_text() == "6"
        assert page.locator("#eoPassList .eo-pass-card:visible").count() == 6
        page.locator("#eoPassCategory").select_option(value="合同基准")
        assert page.locator("#eoPassVisible").inner_text() == "1"
        assert page.locator("#eoPassType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_pass_types
        page.locator("#eoPassType").select_option(value="价格补差")
        assert page.locator("#eoPassCategory").input_value() == ""
        assert page.locator("#eoPassVisible").inner_text() == "4"
        assert page.locator("#eoPassType option").evaluate_all(
            "nodes => nodes.map(node => node.value)"
        ) == fixed_pass_types
        page.locator(".eo-pass-filter-result [data-pass-reset]").click()
        assert page.locator("#eoPassVisible").inner_text() == "6"
        page.wait_for_timeout(350)
        page.screenshot(path=str(screenshot_root / "desktop-pass-log.png"), full_page=True)

        page.get_by_role("button", name="错误总览").click()
        assert page.get_by_role("heading", name="核销错误处置总览").is_visible()
        assert page.locator("#passed").is_hidden()

        page.get_by_role("button", name="技术档案").click()
        page.get_by_role("button", name="分析文件").click()
        page.locator(
            '[data-as-analysis="analysis/results/price_difference_support.json"]'
        ).click()
        page.wait_for_function(
            "document.querySelector('#as-analysis-view').textContent.includes('price_difference_support')"
        )

        page.get_by_role("button", name="DOM 断点").click()
        page.locator("[data-as-checkpoint]").first.click()
        page.wait_for_function(
            "document.querySelector('#as-checkpoint-view').textContent.includes('checkpoint_id')"
        )

        page.get_by_role("button", name="完整日志").click()
        page.wait_for_function(
            "document.querySelector('[data-as-tech-panel=\"log\"] .as-code').textContent.includes('核销完成')"
        )
        page.get_by_role("button", name="关闭").click()

        page.get_by_role("button", name="← 返回核销台账").click()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("body.audit-system-page")
        assert page.locator("#as-view-ledger").is_visible()

        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#as-view-overview:not([hidden])")
        assert page.locator('[data-as-view="overview"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#as-view-ledger").is_hidden()

        page.locator('[data-as-view="ledger"]').click()
        page.locator(f'[data-as-open="{running_workspace_id}"]').click()
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
        browser.close()

    assert not console_errors, console_errors
    assert not page_errors, page_errors
    print(
        {
            "workspace_id": workspace_id,
            "desktop": str(screenshot_root / "desktop.png"),
            "refresh_default": "overview",
            "viewport": "1440x960",
            "console_errors": console_errors,
            "page_errors": page_errors,
        }
    )


if __name__ == "__main__":
    main()
