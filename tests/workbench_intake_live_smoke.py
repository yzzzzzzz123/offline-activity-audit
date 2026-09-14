from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

COMPLETED_JOB_ID = "c" * 24
FAILED_JOB_ID = "f" * 24
ORPHAN_JOB_ID = "d" * 24
COMPLETED_WORKSPACE_ID = "20260909_0900_00-codex_high"
FAILED_WORKSPACE_ID = "20260909_0901_00-codex_high"
RUNNING_WORKSPACE_ID = "OSS运行资料-20260910_0902_00"
INPUT_WORKSPACE_ID = "input运行资料-20260910_0903_00"


def main() -> None:
    parser = argparse.ArgumentParser(description="验收 OSS/input 活动记录去重、来源标签及终态分区")
    parser.add_argument("--url", default="http://127.0.0.1:18082/")
    parser.add_argument("--screenshot", type=Path, default=Path(tempfile.gettempdir()) / "offline-audit-intake-operations.png")
    args = parser.parse_args()
    errors = []
    screenshots = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        response = page.goto(args.url)
        expect(page.locator("#as-intake-count")).to_have_text("3")
        assert response is not None and response.status == 200
        config = page.evaluate("async () => (await fetch('/api/config')).json()")
        assert config["api_version"] == "1.38" and config["system_version"] == "2.11.25"
        assert config["material_problem_policy"] == "analyze_and_report"
        assert config["oss_intake"]["operations_list_endpoint"] == "/api/intake/jobs?completed=0"
        operations = page.evaluate("async () => (await fetch('/api/intake/jobs?completed=0')).json()")
        assert operations["count"] == 4
        assert page.evaluate("async () => (await fetch('/api/intake/jobs')).json()")["count"] == 5
        expect(page.get_by_role("heading", name="运行中的记录", exact=True)).to_be_visible()
        active = page.locator("#as-intake-list .as-intake-card")
        assert active.count() == 3
        assert page.locator("#as-intake-list .running").count() == 2
        assert page.locator("#as-intake-list .queued").count() == 1
        assert page.locator("#as-intake-list .failed").count() == 0
        assert page.locator('#as-intake-list [data-input-source="oss"]').count() == 2
        assert page.locator('#as-intake-list [data-input-source="input"]').count() == 1
        assert page.locator(f'#as-intake-list [data-as-open="{RUNNING_WORKSPACE_ID}"]').count() == 1
        assert page.locator("#as-intake-list [data-as-delete-job]").count() == 2
        expect(page.locator("#as-intake-failed-panel")).to_be_visible()
        assert page.locator("#as-intake-failed-list .failed").count() == 1
        expect(page.locator("#as-intake-failed-list")).to_contain_text("ZIP 内没有可识别的核销材料")
        recent_ids = page.locator("#as-recent-list [data-run-card]").evaluate_all("nodes => nodes.map(node => node.dataset.runCard)")
        assert set(recent_ids) == {COMPLETED_WORKSPACE_ID, FAILED_WORKSPACE_ID}
        assert page.locator("#as-recent-list button").count() == 0
        for width in (1440, 1280):
            page.set_viewport_size({"width": width, "height": 960})
            assert page.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)") <= width + 2
            target = args.screenshot.with_name(args.screenshot.stem + f"-{width}.png")
            target.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(target), full_page=True, animations="disabled")
            screenshots.append(str(target))
        page.locator('[data-as-view="ledger"]').click()
        assert page.locator("#as-ledger-list [data-run-card]").count() == 4
        expect(page.locator(f'[data-as-delete="{INPUT_WORKSPACE_ID}"]')).to_be_disabled()
        assert page.locator(f'[data-as-delete-job="{FAILED_JOB_ID}"]').count() == 1

        def delete_job(job_id: str, *, terminal: bool) -> None:
            page.locator(f'[data-as-delete-job="{job_id}"]:visible').click()
            expect(page.locator("#as-delete-dialog")).to_be_visible()
            expect(page.locator("#as-delete-confirm")).to_have_text("永久删除" if terminal else "取消并删除")
            assert page.locator("#as-delete-cancel").evaluate("node => document.activeElement === node")
            page.locator("#as-delete-confirm").click()
            expect(page.locator("#as-delete-dialog")).to_be_hidden()

        delete_job(FAILED_JOB_ID, terminal=True)
        expect(page.locator("#as-nav-ledger")).to_have_text("3")
        page.locator('[data-as-view="overview"]').click()
        assert page.locator("#as-recent-list [data-run-card]").count() == 1
        delete_job(ORPHAN_JOB_ID, terminal=True)
        expect(page.locator("#as-intake-failed-panel")).to_be_hidden()
        queued_id = page.locator("#as-intake-list .queued").get_attribute("data-intake-job")
        delete_job(queued_id, terminal=False)
        running_id = page.locator("#as-intake-list .running[data-intake-job]").get_attribute("data-intake-job")
        delete_job(running_id, terminal=False)
        expect(page.locator("#as-intake-count")).to_have_text("1")
        expect(page.locator("#as-nav-ledger")).to_have_text("2")
        assert page.locator(f'[data-active-run="{INPUT_WORKSPACE_ID}"]').count() == 1
        assert page.evaluate("async () => (await fetch('/api/intake/jobs?completed=0')).json()") == {"jobs": [], "count": 0}
        remaining = page.evaluate("async () => (await fetch('/api/intake/jobs')).json()")
        assert remaining["count"] == 1 and remaining["jobs"][0]["job_id"] == COMPLETED_JOB_ID
        assert page.locator("#as-recent-list [data-run-card]").count() == 1
        browser.close()
    assert not errors, errors
    print(json.dumps({"status": "passed", "checks": ["OSS/input merge", "one card per active run", "two source tags", "running excluded from recent", "failed run in recent and ledger", "failed intake without run separate", "owned linked/queued/running/orphan OSS deletion", "completed OSS and direct input preserved", "Chinese workspace IDs", "1440/1280"], "screenshots": screenshots, "console_and_page_errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
