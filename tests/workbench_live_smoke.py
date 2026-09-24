from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Page, sync_playwright


PRIMARY_LIST_BATCH_SIZE = 100


def wait_for_primary(page: Page) -> None:
    page.wait_for_load_state("networkidle")
    page.wait_for_selector("body.audit-system-page")
    page.wait_for_function(
        """() => {
          const total = document.querySelector('#as-rail-total');
          return total && !total.textContent.includes('读取中');
        }"""
    )


def reveal_ledger_record(page: Page, workspace_id: str) -> None:
    target = page.locator(f'#as-ledger-list [data-as-open="{workspace_id}"]')
    while target.count() == 0:
        more = page.locator('[data-as-load-more="overview"]')
        assert more.count() == 1
        more.click()


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
        "--pass-category",
        help="可选：在正确检查项中执行一次正向检查分类筛选。",
    )
    parser.add_argument(
        "--expected-pass-count",
        type=int,
        help="与 --pass-category 配合，断言检查分类筛选后的命中数量。",
    )
    parser.add_argument(
        "--expected-audit-type-count",
        type=int,
        help="可选：断言错误与正确视图中固定核销方式选项的数量。",
    )
    args = parser.parse_args()
    if args.expected_pass_count is not None and not args.pass_category:
        parser.error("--expected-pass-count 必须与 --pass-category 同时使用")
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
        assert page.locator(".as-rail").evaluate(
            "node => getComputedStyle(node).backgroundColor"
        ) == "rgb(16, 39, 49)"
        listing = page.evaluate("async () => (await fetch('/api/runs')).json()")
        config = page.evaluate("async () => (await fetch('/api/config')).json()")
        api_major, api_minor = (
            int(value) for value in str(config.get("api_version") or "0.0").split(".")[:2]
        )
        intake_path = (
            "/api/intake/jobs?completed=0"
            if api_major > 1 or (api_major == 1 and api_minor >= 20)
            else "/api/intake/jobs"
        )
        intake_jobs = [
            job
            for job in (
                page.evaluate(
                    "async path => (await fetch(path)).json()", intake_path
                )["jobs"]
                if config.get("oss_intake", {}).get("enabled")
                else []
            )
            if job.get("status") != "completed"
        ]
        active_jobs = [job for job in intake_jobs if job["status"] in ("accepted", "downloading", "downloaded", "running", "callback")]
        job_run_ids = {job.get("run_id") for job in active_jobs}
        active_runs = [run for run in listing["runs"] if run["status"] not in ("completed", "failed") and run["run_id"] not in job_run_ids]
        assert page.locator("#as-intake-list .as-intake-card").count() == len(active_jobs) + len(active_runs)
        assert page.locator("#as-intake-panel").is_visible() == bool(active_jobs or active_runs)
        assert page.locator("#as-intake-list [data-as-delete-job]").count() == len(active_jobs)
        for run in active_runs:
            assert page.locator(f'[data-active-run="{run["workspace_id"]}"]').count() == 1
        source_counts = page.locator("#as-intake-list [data-input-source]").evaluate_all("nodes => nodes.map(node => node.dataset.inputSource)")
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
        assert overview_completed_metric.locator("small").inner_text() == "最近一次已完成核销的类型数"
        assert overview_completed_metric.locator(":scope > strong").inner_text() == str(
            expected_completed_zip_count
        )
        expected_pending = sum(
            int(run.get("error_count") or 0)
            for run in listing["runs"]
            if run.get("status") == "completed" and not run.get("manual_reviewed")
        )
        pending_metric = page.locator("#as-metrics .as-metric").filter(
            has_text="待人工核验"
        )
        assert pending_metric.locator(":scope > strong").inner_text() == str(
            expected_pending
        )
        all_runs = listing["runs"]
        completed_runs = [run for run in all_runs if run["status"] == "completed"]
        for selector in ("#as-hero-count", "#as-nav-overview", "#as-nav-archive"):
            assert page.locator(selector).inner_text() == str(len(all_runs))
        assert page.locator("#as-rail-total").inner_text() == f"{len(all_runs)} 条记录"
        assert page.locator("#as-metrics .as-metric:first-child > strong").inner_text() == str(len(all_runs))
        overview_ids = page.locator("#as-ledger-list [data-run-card]").evaluate_all("nodes => nodes.map(node => node.dataset.runCard)")
        assert overview_ids == [run["workspace_id"] for run in all_runs[:PRIMARY_LIST_BATCH_SIZE]]
        assert page.locator("[data-as-view]").count() == 2
        assert page.locator('.as-nav[role="tablist"]').count() == 1
        assert page.locator('.as-nav [role="tab"]').count() == 2
        assert page.locator('.as-view[role="tabpanel"]').count() == 2
        assert page.locator('.as-nav [role="tab"]').evaluate_all(
            "nodes => nodes.map(node => node.tabIndex)"
        ) == [0, -1]
        assert page.locator("#as-ledger-list .as-run-card").count() == min(len(all_runs), PRIMARY_LIST_BATCH_SIZE)
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
        assert page.locator("#as-ledger-list .as-run-card").count() == min(len(all_runs), PRIMARY_LIST_BATCH_SIZE)
        assert page.locator("#as-archive-list button").count() == 0
        page.locator('[data-as-view="archive"]').click()
        assert page.locator("#as-archive-list button").count() == min(
            len(all_runs), PRIMARY_LIST_BATCH_SIZE
        )
        assert page.locator('#as-archive-list[role="list"]').count() == 1
        page.locator('[data-as-view="overview"]').click()
        assert page.locator("#as-archive-list .as-archive-card").count() == 0
        ledger_ids = page.locator("#as-ledger-list [data-run-card]").evaluate_all(
            "nodes => nodes.map(node => node.dataset.runCard)"
        )
        assert ledger_ids == [
            run["workspace_id"] for run in all_runs[:PRIMARY_LIST_BATCH_SIZE]
        ]
        assert page.locator("#as-nav-overview").inner_text() == str(len(all_runs))
        assert page.get_by_role("heading", name="核销台账").is_visible()
        assert page.locator("#as-status-filter").count() == 0
        assert page.locator("#as-ledger-list .as-status").evaluate_all("nodes => nodes.map(node => node.className.split(' ').pop())") == [run["status"] for run in all_runs[:PRIMARY_LIST_BATCH_SIZE]]
        page.locator('[data-as-view="archive"]').click()
        archive_ids = page.locator('#as-archive-list [data-as-technical]').evaluate_all("nodes => nodes.map(node => node.dataset.asTechnical)")
        assert archive_ids == ledger_ids
        archive_screenshot = args.screenshot.with_name(args.screenshot.stem + "-archive.png")
        page.screenshot(path=str(archive_screenshot), full_page=True, animations="disabled")
        page.locator('[data-as-view="overview"]').click()
        page.screenshot(path=str(args.screenshot), full_page=True, animations="disabled")

        secondary_screenshot_output = None
        if completed_id is not None:
            page.locator('[data-as-view="overview"]').click()
            reveal_ledger_record(page, completed_id)
            visible_ids = page.locator("#as-ledger-list [data-run-card]").evaluate_all(
                "nodes => nodes.map(node => node.dataset.runCard)"
            )
            assert visible_ids == [
                run["workspace_id"] for run in all_runs[: len(visible_ids)]
            ]
            assert page.locator("#as-ledger-list [data-as-technical]").count() == 0
            assert page.locator("#as-ledger-list [data-as-open]").count() == len(visible_ids)
            assert page.locator("#as-ledger-list [data-as-delete], #as-ledger-list [data-as-delete-job]").count() == len(visible_ids)
            assert page.locator("#as-ledger-list [data-as-review]").count() == sum(run["status"] == "completed" for run in all_runs[:len(visible_ids)])
            for run in all_runs[:len(visible_ids)]:
                control = page.locator(f'[data-as-delete="{run["workspace_id"]}"]')
                if control.count():
                    assert control.is_enabled() == (run["status"] in ("completed", "failed"))
            page.locator(f'#as-ledger-list [data-as-open="{completed_id}"]').click()
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("body.error-only-page")
            page.wait_for_selector("#as-back-ledger")
            assert page.locator("#as-record-select option").evaluate_all("nodes => nodes.map(node => node.value)") == [run["workspace_id"] for run in all_runs]
            assert parse_qs(urlsplit(page.url).query).get("run") == [completed_id]
            assert page.get_by_role("region", name="筛选错误检查项").is_visible()
            assert page.locator(".eo-queue-head").count() == 0
            assert page.locator(".eo-pass-filter-head, #eoErrorVisible, #eoPassVisible").count() == 0
            assert page.locator(".eo-pass-filter-panel [data-error-reset], .eo-pass-filter-panel [data-pass-reset]").count() == 0
            assert page.locator("#home .eo-metric").count() == 0
            assert page.locator("#home .eo-cockpit-hero, #home .eo-gauge-stat").count() == 0
            error_cards = page.locator("#eoErrorList .eo-error-card")
            assert error_cards.count() > 0
            assert error_cards.evaluate_all(
                "nodes => nodes.every(node => /^错误项 \\d+；核销类型：/.test(node.getAttribute('aria-label') || ''))"
            )
            error_total = error_cards.count()
            assert page.locator("#eoErrorList .eo-scope").count() == error_cards.count()
            assert page.locator("#eoErrorList .eo-error-reason").count() >= error_cards.count()
            assert page.locator("#eoErrorList .eo-error-head .eo-error-reason").count() == 0
            assert page.locator("#eoErrorList .eo-field .eo-error-reason").count() >= error_cards.count()
            assert error_cards.evaluate_all(
                "nodes => nodes.every(node => Boolean(node.dataset.errorCategories))"
            )
            assert page.locator("#eoErrorList .eo-error-confidence").count() == 0
            assert error_cards.evaluate_all(
                "nodes => nodes.every(node => Boolean(node.dataset.errorConfidenceScore))"
            )
            assert page.locator("#eoErrorConfidence").count() == 0
            assert error_cards.evaluate_all(
                "nodes => nodes.every(node => !node.hidden && node.offsetParent !== null)"
            )
            fixed_error_types = page.locator("#eoErrorType option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            )
            assert error_cards.evaluate_all(
                "(nodes, types) => nodes.every(node => types.includes(node.dataset.auditType))",
                fixed_error_types,
            )
            assert len(fixed_error_types) >= 1
            if args.expected_audit_type_count is not None:
                expected_options = (
                    args.expected_audit_type_count
                    if args.expected_audit_type_count == 1
                    else args.expected_audit_type_count + 1
                )
                assert len(fixed_error_types) == expected_options
                if args.expected_audit_type_count == 1:
                    assert fixed_error_types[0]
                    assert "" not in fixed_error_types
                    assert not page.locator("#eoErrorType").is_disabled()
                else:
                    assert fixed_error_types[0] == ""
            assert page.locator("#eoErrorCategory option").count() > 1
            page.locator("#eoErrorCategory").select_option(index=1)
            category_total = page.locator("#eoErrorList .eo-error-card:visible").count()
            assert 0 < category_total <= error_total
            assert page.locator("#eoErrorList .eo-error-card:visible").count() == category_total
            assert page.locator("#eoErrorType option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            ) == fixed_error_types
            page.locator("#eoErrorCategory").select_option(value="")
            assert page.locator("#eoErrorList .eo-error-card:visible").count() == error_total
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
            assert page.get_by_role("button", name="← 返回系统总览").is_visible()
            page.screenshot(path=str(secondary_screenshot), full_page=True, animations="disabled")
            secondary_screenshot_output = str(secondary_screenshot)

            page.get_by_role("tab", name="正确检查项").click()
            assert page.get_by_role("region", name="筛选正确检查项").is_visible()
            assert page.locator("#home").is_hidden()
            assert page.locator("#passed").is_visible()
            assert page.locator("#passed .eo-metric").count() == 0
            assert page.locator("#passed .eo-cockpit-hero, #passed .eo-gauge-stat").count() == 0
            assert page.locator("#eoPassList .eo-pass-group").count() >= 1
            pass_total = int(page.locator('[data-eo-view="passed"] em').inner_text())
            assert page.locator("#eoPassList .eo-pass-card").count() == pass_total
            assert page.locator("#eoPassList .eo-pass-card").evaluate_all(
                "nodes => nodes.every(node => node.getAttribute('aria-label')?.startsWith('正确检查项 '))"
            )
            assert page.locator("#eoPassList .eo-pass-card").first.evaluate(
                "node => getComputedStyle(node).contentVisibility"
            ) == "auto"
            assert page.locator("#eoPassList .eo-pass-card").evaluate_all(
                "nodes => nodes.every(node => Boolean(node.dataset.passConfidenceScore))"
            )
            assert page.locator("#eoPassList .eo-pass-confidence").count() == 0
            assert page.locator("#eoPassConfidence").count() == 0
            assert page.locator(".eo-pass-warning").count() == 0
            assert page.locator("#eoPassList .eo-pass-card:visible").count() == pass_total
            fixed_pass_types = page.locator("#eoPassType option").evaluate_all(
                "nodes => nodes.map(node => node.value)"
            )
            assert len(fixed_pass_types) >= 1
            if args.expected_audit_type_count is not None:
                expected_options = (
                    args.expected_audit_type_count
                    if args.expected_audit_type_count == 1
                    else args.expected_audit_type_count + 1
                )
                assert len(fixed_pass_types) == expected_options
                if args.expected_audit_type_count == 1:
                    assert fixed_pass_types[0]
                    assert "" not in fixed_pass_types
                    assert not page.locator("#eoPassType").is_disabled()
                else:
                    assert fixed_pass_types[0] == ""
            if page.locator("#eoPassCategory option").count() > 1:
                page.locator("#eoPassCategory").select_option(index=1)
                filtered_total = page.locator("#eoPassList .eo-pass-card:visible").count()
                assert 0 < filtered_total <= pass_total
                assert (
                    page.locator("#eoPassList .eo-pass-card:visible").count()
                    == filtered_total
                )
                assert page.locator("#eoPassType option").evaluate_all(
                    "nodes => nodes.map(node => node.value)"
                ) == fixed_pass_types
            if not page.locator("#eoPassCategory").is_disabled():
                page.locator("#eoPassCategory").select_option(value="")
            assert page.locator("#eoPassList .eo-pass-card:visible").count() == pass_total
            if args.pass_category:
                page.locator("#eoPassCategory").select_option(value=args.pass_category)
                positive_pass_count = page.locator("#eoPassList .eo-pass-card:visible").count()
                assert positive_pass_count > 0
                if args.expected_pass_count is not None:
                    assert positive_pass_count == args.expected_pass_count
                assert (
                    page.locator("#eoPassList .eo-pass-card:visible").count()
                    == positive_pass_count
                )
                page.screenshot(path=str(pass_filtered_screenshot), full_page=True, animations="disabled")
                page.locator("#eoPassCategory").select_option(value="")
                assert page.locator("#eoPassList .eo-pass-card:visible").count() == pass_total
            page.wait_for_timeout(350)
            page.screenshot(path=str(pass_screenshot), full_page=True, animations="disabled")

            page.get_by_role("tab", name="错误总览").click()
            assert page.get_by_role("region", name="筛选错误检查项").is_visible()

            page.get_by_role("button", name="技术档案").click()
            page.wait_for_selector("#as-drawer-backdrop:not([hidden])")
            assert page.get_by_role("heading", name=completed_id).is_visible()
            page.get_by_role("button", name="关闭").click()
            page.get_by_role("button", name="← 返回系统总览").click()
            wait_for_primary(page)
            assert page.locator("#as-view-overview").is_visible()

        page.reload()
        wait_for_primary(page)
        assert page.locator("#as-view-overview").is_visible()
        assert page.locator('[data-as-view="overview"]').get_attribute(
            "aria-selected"
        ) == "true"
        assert page.locator("#as-view-archive").is_hidden()

        failed_screenshot_output = None
        if failed_id is not None:
            page.goto(f"{args.url.rstrip('/')}?run={failed_id}")
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("body.audit-system-page")
            assert f"run={failed_id}" in page.url
            assert page.get_by_role("heading", name="运行失败").is_visible()
            assert page.locator(".as-failure").is_visible()
            page.screenshot(path=str(failed_screenshot), full_page=True, animations="disabled")
            failed_screenshot_output = str(failed_screenshot)

            page.get_by_role("button", name="打开技术档案").click()
            page.wait_for_selector("#as-drawer-backdrop:not([hidden])")
            page.get_by_role("tab", name="DOM 断点").click()
            if page.locator("[data-as-checkpoint]").count():
                page.locator("[data-as-checkpoint]").first.click()
                page.wait_for_function(
                    "document.querySelector('#as-checkpoint-view').textContent.includes('checkpoint_id')"
                )
            page.get_by_role("button", name="关闭").click()
            page.get_by_role("button", name="← 返回系统总览").click()
            wait_for_primary(page)

        running_screenshot_output = None
        if running_id is not None:
            page.goto(f"{args.url.rstrip('/')}?run={running_id}")
            page.wait_for_load_state("networkidle")
            page.wait_for_selector("body.audit-system-page")
            assert f"run={running_id}" in page.url
            assert page.get_by_role("heading", name="正在核销").is_visible()
            assert page.get_by_role("button", name="打开技术档案").is_visible()
            running_screenshot = args.screenshot.with_name(
                args.screenshot.stem + "-running" + args.screenshot.suffix
            )
            page.screenshot(path=str(running_screenshot), full_page=True, animations="disabled")
            running_screenshot_output = str(running_screenshot)

        browser.close()

    assert not console_errors, console_errors
    assert not page_errors, page_errors
    print(
        json.dumps(
            {
                "status": response.status,
                "completed_run": completed_id,
                "catalog_count": len(all_runs), "ledger_ids": ledger_ids, "archive_ids": archive_ids,
                "overview_ids": overview_ids, "active_run_ids": [run["workspace_id"] for run in active_runs],
                "active_sources": source_counts, "archive": str(archive_screenshot),
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
