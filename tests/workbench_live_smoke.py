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
    page.wait_for_selector("#as-monitor-list .as-runtime-monitor", state="attached")


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        description="验证一级核销系统与多份二级核销记录页。"
    )
    parser.add_argument("--url", default="http://127.0.0.1:8080/")
    parser.add_argument(
        "--screenshot",
        type=Path,
        default=Path(tempfile.gettempdir()) / "offline-audit-system-primary.png",
    )
    args = parser.parse_args()
    secondary_screenshot = args.screenshot.with_name(
        args.screenshot.stem + "-secondary" + args.screenshot.suffix
    )
    failed_screenshot = args.screenshot.with_name(
        args.screenshot.stem + "-failed" + args.screenshot.suffix
    )

    console_errors: list[str] = []
    page_errors: list[str] = []
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
        completed_id = next(
            (run["workspace_id"] for run in listing["runs"] if run["status"] == "completed"),
            None,
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
        assert page.locator("#as-recent-list .as-run-card").count() == min(
            listing["count"], 5
        )
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
        assert page.locator("#as-monitor-list button").count() == 0
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
            assert page.get_by_role("button", name="← 返回核销台账").is_visible()
            page.screenshot(path=str(secondary_screenshot), full_page=True)
            secondary_screenshot_output = str(secondary_screenshot)

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
                "primary": str(args.screenshot),
                "secondary": secondary_screenshot_output,
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
