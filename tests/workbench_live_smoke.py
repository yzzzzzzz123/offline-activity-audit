from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import Page, sync_playwright


def wait_for_primary(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("body.audit-system-page")
    page.wait_for_selector("#as-recent-list .as-run-card", state="attached")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="验证一级核销系统与多份二级核销记录页。"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080/")
    parser.add_argument(
        "--run-id",
        help="指定要验收的已完成运行；默认选择目录中的最新已完成记录。",
    )
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=Path(tempfile.gettempdir()) / "offline-audit-system-primary.png",
    )
    parser.add_argument(
        "--pass-keyword",
        help="可选：在正确检查项中执行一次正向关键词筛选。",
    )
    parser.add_argument(
        "--expected-pass-count",
        type=int,
        help="与 --pass-keyword 配合，断言关键词筛选后的命中数量。",
    )
    parser.add_argument(
        "--expected-audit-type-count",
        type=int,
        help="可选：断言错误与正确视图中固定核销方式选项的数量。",
    )
    args = parser.parse_args()
    if args.expected_pass_count is not None and not args.pass_keyword:
        parser.error("--expected-pass-count 必须与 --pass-keyword 同时使用")
    secondary_screenshot = args.screenshot.with_name(
        args.screenshot.stem + "-secondary" + args.screenshot.suffix
    )
    pass_screenshot = args.screenshot.with_name(
        args.screenshot.stem + "-secondary-pass" + args.screenshot.suffix
    )
    pass_filtered_screenshot = args.screenshot.with_name(
        args.screenshot.stem + "-secondary-pass-filtered" + args.screenshot.suffix
    )
    failed_screenshot = args.screenshot.with_name(
        args.screenshot.stem + "-failed" + args.screenshot.suffix
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
    positive_pass_count: int | None = None
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

        response = page.goto(args.url)
        wait_for_primary(page)
        assert response is not None and response.status == 200
        listing = page.evaluate("async () => (await fetch('/api/runs')).json()")
        latest_completed_id = next(
            (run["workspace_id"] for run in listing["runs"] if run["status"] == "completed"),
            None,
        )
        completed_id = args.run_id or latest_completed_id
        if args.run_id:
            requested = next(
                (run for run in listing["runs"] if run["workspace_id"] == args.run_id),
                None,
            )
            assert requested is not None and requested["status"] == "completed"
        expected_completed_zip_count = 0
        if latest_completed_id is not None:
            completed_snapshot = page.evaluate(
                "async workspaceId => (await fetch(`/api/runs/${encodeURIComponent(workspaceId)}/snapshot`)).json()",
                latest_completed_id,
            )
            expected_completed_zip_count = len(
                (completed_snapshot.get("view") or {}).get("sheets") or []
            )
        failed_id = next(
            (run["workspace_id"] for run in listing["runs"] if run["status"] == "failed"),
            None,
        )
        running_id = next(
            (run["workspace_id"] for run in listing["runs"] if run["status"] == "running"),
            None,
        )
        assert completed_id is not None or failed_id is not None or running_id is not None
        assert page.get_by_role("heading", name="核销管理中心").first.is_visible()
        overview_completed_metric = page.locator("#as-metrics .as-metric").filter(
            has_text="核销完成"
        )
        assert overview_completed_metric.count() == 1
        assert overview_completed_metric.locator("small").inner_text() == "最近一次 input 的 ZIP 总数"
        assert overview_completed_metric.locator(":scope > strong").inner_text() == str(
            expected_completed_zip_count
        )
        assert page.locator("#as-recent-list .as-run-card").count() == min(
            listing["count"], 5
        )
        assert page.locator("[data-as-view]").count() == 3
        assert page.locator('[data-as-view="monitor"]').count() == 0
        assert page.locator("#as-monitor-list").count() == 0
        assert page.locator("[data-runtime-stage]").count() == 0
        assert page.get_by_role("heading", name="核销运行链路").count() == 0
        assert page.get_by_role("heading", name="最近核销记录").is_visible()
        assert page.locator("#as-recent-list button").count() == 0
        assert page.locator("#as-archive-list [data-as-open]").count() == 0
        assert page.locator("#as-archive-list button").count() == listing["count"]
        page.screenshot(path=str(args.screenshot), full_page=True)

        secondary_screenshot_output = None
        if completed_id is not None:
            page.locator('[data-as-view="ledger"]').click()
            page.wait_for_selector(f'#as-ledger-list [data-as-open="{completed_id}"]')
            assert page.locator("#as-ledger-list [data-as-technical]").count() == 0
            assert page.locator("#as-ledger-list button").count() == listing["count"]
            page.locator(f'#as-ledger-list [data-as-open="{completed_id}"]').click()
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("body.error-only-page")
            page.wait_for_selector("#as-back-ledger")
            assert f"run={completed_id}" in page.url
            assert page.get_by_role("heading", name="核销错误处置总览").is_visible()
            assert page.get_by_role("heading", name="核销错误结果").is_visible()
            assert page.locator(".eo-metric").first.locator(".eo-metric-copy > span").inner_text() == "核销类型"
            error_cards = page.locator("#eoErrorList .eo-error-card")
            assert error_cards.count() > 0
            error_total = error_cards.count()
            assert page.locator("#eoErrorList .eo-scope").count() == error_cards.count()
            assert page.locator("#eoErrorList .eo-error-confidence").count() == error_cards.count()
            assert error_cards.evaluate_all(
                "nodes => nodes.every(node => !node.hidden && node.offsetParent !== null)"
            )
            fixed_error_types = page.locator("#eoErrorType option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            )
            assert len(fixed_error_types) > 1
            if args.expected_audit_type_count is not None:
                assert len(fixed_error_types) - 1 == args.expected_audit_type_count
            page.locator("#eoErrorKeyword").fill("__NO_MATCHING_ERROR_CHECK__")
            assert page.locator("#eoErrorVisible").inner_text() == "0"
            assert page.locator("#eoErrorFilterEmpty").is_visible()
            assert page.locator("#eoErrorType option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ) == fixed_error_types
            page.locator("#eoErrorFilterEmpty [data-error-reset]").click()
            assert int(page.locator("#eoErrorVisible").inner_text()) == error_total
            assert page.locator(".eo-scenario-card").count() == 0
            assert page.locator("button.eo-tab").count() == 2
            assert page.locator(".eo-enter").count() == 0
            assert page.locator('[data-open^="scenario-"]').count() == 0
            assert page.get_by_role("button", name="← 返回核销台账").is_visible()
            page.screenshot(path=str(secondary_screenshot), full_page=True)
            secondary_screenshot_output = str(secondary_screenshot)

            page.get_by_role("button", name="正确检查项").click()
            assert page.get_by_role("heading", name="正确检查项日志").is_visible()
            assert page.locator("#home").is_hidden()
            assert page.locator("#passed").is_visible()
            assert page.locator("#eoPassList .eo-pass-group").count() >= 1
            pass_total = int(page.locator('[data-eo-view="passed"] em').inner_text())
            assert page.locator("#eoPassList .eo-pass-card").count() == pass_total
            assert page.locator(".eo-pass-warning").count() == 0
            assert int(page.locator("#eoPassVisible").inner_text()) == pass_total
            fixed_pass_types = page.locator("#eoPassType option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            )
            assert len(fixed_pass_types) > 1
            if args.expected_audit_type_count is not None:
                assert len(fixed_pass_types) - 1 == args.expected_audit_type_count
            if page.locator("#eoPassCategory option").count() > 1:
                page.locator("#eoPassCategory").select_option(index=1)
                filtered_total = int(page.locator("#eoPassVisible").inner_text())
                assert 0 < filtered_total <= pass_total
                assert (
                    page.locator("#eoPassList .eo-pass-card:visible").count()
                    == filtered_total
                )
                assert page.locator("#eoPassType option").evaluate_all(
                    "nodes => nodes.map(node => node.value)"
                ) == fixed_pass_types
            page.locator("#eoPassKeyword").fill("__NO_MATCHING_PASS_CHECK__")
            assert page.locator("#eoPassVisible").inner_text() == "0"
            assert page.locator("#eoPassFilterEmpty").is_visible()
            assert page.locator("#eoPassType option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ) == fixed_pass_types
            page.get_by_role("button", name="清除全部筛选").click()
            assert int(page.locator("#eoPassVisible").inner_text()) == pass_total
            assert page.locator("#eoPassList .eo-pass-card:visible").count() == pass_total
            if args.pass_keyword:
                page.locator("#eoPassKeyword").fill(args.pass_keyword)
                positive_pass_count = int(page.locator("#eoPassVisible").inner_text())
                assert positive_pass_count > 0
                if args.expected_pass_count is not None:
                    assert positive_pass_count == args.expected_pass_count
                assert (
                    page.locator("#eoPassList .eo-pass-card:visible").count()
                    == positive_pass_count
                )
                page.screenshot(path=str(pass_filtered_screenshot), full_page=True)
                page.locator(".eo-pass-filter-result [data-pass-reset]").click()
                assert int(page.locator("#eoPassVisible").inner_text()) == pass_total
            page.wait_for_timeout(350)
            page.screenshot(path=str(pass_screenshot), full_page=True)

            page.get_by_role("button", name="错误总览").click()
            assert page.get_by_role("heading", name="核销错误处置总览").is_visible()

            page.get_by_role("button", name="技术档案").click()
            page.wait_for_selector("#as-drawer-backdrop:not([hidden])")
            assert page.get_by_role("heading", name=completed_id).is_visible()
            page.get_by_role("button", name="关闭").click()
            page.get_by_role("button", name="← 返回核销台账").click()
            wait_for_primary(page)
            assert page.locator("#as-view-ledger").is_visible()

        page.reload()
        wait_for_primary(page)
        assert page.locator("#as-view-overview").is_visible()
        assert page.locator('[data-as-view="overview"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#as-view-ledger").is_hidden()

        failed_screenshot_output = None
        if failed_id is not None:
            page.locator('[data-as-view="ledger"]').click()
            page.locator(f'[data-as-open="{failed_id}"]').first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("body.audit-system-page")
            assert f"run={failed_id}" in page.url
            assert page.get_by_role("heading", name="运行失败").is_visible()
            assert page.locator(".as-failure").is_visible()
            page.screenshot(path=str(failed_screenshot), full_page=True)
            failed_screenshot_output = str(failed_screenshot)

            page.get_by_role("button", name="打开技术档案").click()
            page.wait_for_selector("#as-drawer-backdrop:not([hidden])")
            page.get_by_role("button", name="DOM 断点").click()
            if page.locator("[data-as-checkpoint]").count():
                page.locator("[data-as-checkpoint]").first.click()
                page.wait_for_function(
                    "document.querySelector('#as-checkpoint-view').textContent.includes('checkpoint_id')"
                )
            page.get_by_role("button", name="关闭").click()
            page.get_by_role("button", name="← 返回核销台账").click()
            wait_for_primary(page)

        running_screenshot_output = None
        if running_id is not None:
            page.locator('[data-as-view="ledger"]').click()
            page.locator(f'[data-as-open="{running_id}"]').first.click()
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("body.audit-system-page")
            assert f"run={running_id}" in page.url
            assert page.get_by_role("heading", name="运行中").is_visible()
            assert page.get_by_role("button", name="打开技术档案").is_visible()
            running_screenshot = args.screenshot.with_name(
                args.screenshot.stem + "-running" + args.screenshot.suffix
            )
            page.screenshot(path=str(running_screenshot), full_page=True)
            running_screenshot_output = str(running_screenshot)

        browser.close()

    assert not console_errors, console_errors
    assert not page_errors, page_errors
    print(
        json.dumps(
            {
                "status": response.status,
                "completed_run": completed_id,
                "primary": str(args.screenshot),
                "secondary": secondary_screenshot_output,
                "secondary_pass": str(pass_screenshot) if secondary_screenshot_output else None,
                "secondary_pass_filtered": (
                    str(pass_filtered_screenshot) if positive_pass_count is not None else None
                ),
                "positive_pass_count": positive_pass_count,
                "failed_record": failed_screenshot_output,
                "running_record": running_screenshot_output,
                "refresh_default": "overview",
                "viewport": "1440x960",
                "console_errors": console_errors,
                "page_errors": page_errors,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
