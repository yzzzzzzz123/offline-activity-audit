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
    workspace = reserve_workspace("20260828-browser", "codex", ROOT)
    store = WorkbenchRunStore(
        workspace,
        run_id="20260828-browser",
        producer_model="codex",
        root_html=Path("offline-activity-audit.html"),
        workbench_url=f"http://127.0.0.1:{PORT}/",
    )
    store.observe(
        "cases.prepared",
        {
            "scenarios": ["price_difference_support"],
            "cases": {
                "price_difference_support": {
                    "scenario": "price_difference_support",
                    "source_archive": "价格补差样例.zip",
                }
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
        "result.validated",
        {
            "scenario": "price_difference_support",
            "result": {
                "scenario": "price_difference_support",
                "summary": {"conclusion": "supplement", "suggested_approved_amount": 0},
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
                }
            ],
        },
        scenarios=["price_difference_support"],
        verification={"html": {"verified": True}},
    )
    completed_workspace_id = workspace.name
    running_workspace = reserve_workspace("20260829-browser-running", "codex", ROOT)
    running_store = WorkbenchRunStore(
        running_workspace,
        run_id="20260829-browser-running",
        producer_model="codex",
        root_html=Path("offline-activity-audit.html"),
        workbench_url=f"http://127.0.0.1:{PORT}/",
    )
    running_store.observe(
        "cases.prepared",
        {
            "scenarios": ["maintenance_fee"],
            "cases": {"maintenance_fee": {"scenario": "maintenance_fee"}},
        },
    )
    running_store.observe("scenario.started", {"scenario": "maintenance_fee"})
    return completed_workspace_id, running_store, running_workspace.name


def main() -> None:
    workspace_id, running_store, running_workspace_id = seed()
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
        page.wait_for_selector("#as-monitor-list .as-runtime-monitor")
        assert page.get_by_role("heading", name="核销管理中心").first.is_visible()
        assert page.locator("[data-as-view]").count() == 3
        assert page.locator('[data-as-view="monitor"]').count() == 0
        assert page.locator("#as-view-overview #as-monitor-list").is_visible()
        assert page.get_by_role("heading", name="核销运行链路").is_visible()
        assert page.locator("#as-monitor-list [data-runtime-stage]").count() == 6
        assert page.locator("#as-monitor-list .as-runtime-orb").count() == 6
        assert page.locator("#as-monitor-list .as-runtime-route").get_attribute(
            "viewBox"
        ) == "0 0 1200 74"
        assert page.locator("#as-monitor-list [data-runtime-event]").count() >= 1
        assert "阶段完成时更新" in page.locator("#as-monitor-count").inner_text()
        assert (
            page.locator("#as-monitor-list .as-runtime-monitor").get_attribute(
                "data-runtime-workspace"
            )
            == running_workspace_id
        )
        assert page.locator("#as-monitor-list button").count() == 0
        assert page.locator("#as-recent-list button").count() == 0
        assert page.locator("#as-archive-list [data-as-open]").count() == 0
        assert page.locator("#as-archive-list button").count() == 2

        page.locator("#as-monitor-list .as-runtime-monitor").evaluate(
            "node => node.dataset.sameStageSentinel = 'keep'"
        )
        running_store.observe("scenario.started", {"scenario": "maintenance_fee"})
        page.wait_for_timeout(3200)
        assert (
            page.locator("#as-monitor-list .as-runtime-monitor").get_attribute(
                "data-same-stage-sentinel"
            )
            == "keep"
        )

        running_store.observe(
            "evidence.validated",
            {"scenario": "maintenance_fee", "evidence": {"documents": []}},
        )
        page.wait_for_function(
            """() => {
              const monitor = document.querySelector('#as-monitor-list .as-runtime-monitor');
              return monitor
                && !monitor.dataset.sameStageSentinel
                && document.querySelector('[data-runtime-event="evidence.validated"]');
            }"""
        )
        page.screenshot(path=str(screenshot_root / "desktop.png"), full_page=True)

        page.locator('[data-as-view="ledger"]').click()
        page.wait_for_selector(f'#as-ledger-list [data-as-open="{workspace_id}"]')
        assert page.locator("#as-ledger-list [data-as-technical]").count() == 0
        assert page.locator("#as-ledger-list button").count() == 2
        page.locator(f'#as-ledger-list [data-as-open="{workspace_id}"]').click()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("body.error-only-page")
        assert f"run={workspace_id}" in page.url
        assert page.get_by_role("heading", name="核销错误处置总览").is_visible()

        page.get_by_role("button", name="技术档案").click()
        page.get_by_role("button", name="分析文件").click()
        page.locator('[data-as-analysis^="analysis/results/"]').first.click()
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
