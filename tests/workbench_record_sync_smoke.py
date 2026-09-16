"""Verify synchronized catalogs and active/terminal overview with disposable worktrees."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
from unittest.mock import patch

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from audit_core.workbench_html import render_static_run_archive
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import WorkbenchRunStore, reserve_workspace
from playwright.sync_api import expect, sync_playwright


def complete(store: WorkbenchRunStore) -> None:
    store.complete(
        view_payload={"schema_version": "1.1", "title": "同步测试", "sheets": []},
        scenarios=["promotional_display"], verification={"fixture": True},
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()
    args.artifacts.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    server_errors: list[int] = []
    screenshots: list[str] = []

    class QuietHandler(Handler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
            if isinstance(code, int) and code >= 400:
                server_errors.append(code)

    with tempfile.TemporaryDirectory(prefix="offline-record-sync-") as temporary:
        root = Path(temporary) / "worktrees"
        stores: list[WorkbenchRunStore] = []

        def create(index: int, *, oss: bool = False) -> WorkbenchRunStore:
            workspace = root / f"20260910_0000_00-sync{index:03d}"
            if index == 1:
                workspace = reserve_workspace("20260827-unicode", "codex", root, archive_names=["HX202606040013-核销资料-诚成26年4月【堆头20家】.zip"])
            else:
                workspace.mkdir(parents=True)
            run_id = f"20260827-oss-{index:012x}" if oss else f"20260827-{'showcase' if index < 3 else 'sync'}-{index:03d}"
            with patch("audit_core.workbench_store.utc_now", return_value="2026-09-09T15:55:00+00:00"):
                store = WorkbenchRunStore(
                    workspace, run_id=run_id, producer_model="codex",
                    root_html=PROJECT / "offline-activity-audit.html", workbench_url="http://127.0.0.1/",
                    audit_model="gpt-6-astra", reasoning_effort="ultra",
                )
            store.observe("cases.prepared", {"scenarios": ["promotional_display"], "cases": {"promotional_display": {"source_archive": "HX202606040013-核销资料-诚成26年4月【堆头20家】.zip"}}})
            with patch("audit_core.workbench_store.utc_now", return_value="2026-09-09T16:01:00+00:00"):
                store.observe("scenario.started", {"scenario": "promotional_display"})
            stores.append(store)
            return store

        for index in range(107):
            store = create(index, oss=index == 104)
            if index < 105:
                complete(store)
            elif index == 106:
                store.fail(RuntimeError("isolated failure fixture"))
        catalog = WorkbenchCatalog(root)
        server = WorkbenchHTTPServer(("127.0.0.1", 0), QuietHandler, catalog=catalog)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1440, "height": 960}, service_workers="block")
                page = context.new_page()
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
                page.goto(base)
                config = page.request.get(base + "/api/config").json()
                assert config["api_version"] == "1.39" and config["system_version"] == "2.11.26"
                assert config["material_problem_policy"] == "analyze_and_report"
                expect(page.locator("#as-nav-overview")).to_have_text("107")

                def counts(expected: int) -> None:
                    for selector in ("#as-hero-count", "#as-nav-overview", "#as-nav-archive"):
                        expect(page.locator(selector)).to_have_text(str(expected))
                    expect(page.locator("#as-rail-total")).to_have_text(f"{expected} 条记录")
                    expect(page.locator("#as-metrics .as-metric:first-child > strong")).to_have_text(str(expected))

                def select(view: str) -> None:
                    page.locator(f"#as-tab-{view}").click()
                    expect(page.locator(f"#as-view-{view}")).to_be_visible()

                def ids(view: str) -> list[str]:
                    selector, attribute = ("#as-ledger-list [data-run-card]", "data-run-card") if view == "overview" else (
                        "#as-archive-list [data-as-technical]", "data-as-technical",
                    )
                    return page.locator(selector).evaluate_all("(nodes, attr) => nodes.map(node => node.getAttribute(attr))", attribute)

                counts(107)
                assert page.locator("[data-as-view]").count() == 2
                assert page.locator("#as-tab-ledger, #as-view-ledger, #as-recent-list").count() == 0
                expect(page.get_by_role("heading", name="运行中的记录", exact=True)).to_be_visible()
                active = page.locator(f'[data-active-run="{stores[105].workspace.name}"]')
                expect(active).to_contain_text("input 导入")
                expect(page.locator("#as-intake-count")).to_have_text("1")
                assert len(ids("overview")) == 100
                page.locator('[data-as-load-more="overview"]').click()
                recent_ids = ids("overview")
                assert len(recent_ids) == 107 and stores[105].workspace.name in recent_ids
                assert stores[106].workspace.name in recent_ids
                expect(page.locator("#as-ledger-window")).to_contain_text("已显示全部")
                ledger_ids = ids("overview")
                assert ledger_ids == [run["workspace_id"] for run in catalog.list_runs()]
                assert len(ledger_ids) == 107
                expect(page.locator(f'[data-as-delete="{stores[105].workspace.name}"]')).to_be_disabled()
                expect(page.locator(f'[data-run-card="{stores[104].workspace.name}"]')).to_contain_text("OSS 上传")
                expect(page.locator('.as-run-main small').first).to_have_text("2026-09-10")
                expect(page.locator('.as-run-main h3').first).to_have_text("HX202606040013-核销资料-诚成26年4月【堆头20家】")
                expect(page.locator('.as-model-tag').first).to_have_text("gpt6astra_ultra")
                select("archive")
                assert ids("archive") == ledger_ids and not ids("overview")
                colors = {}
                for status, rgb in (("running", "rgb(215, 154, 32)"), ("failed", "rgb(216, 75, 62)"), ("completed", "rgb(24, 132, 94)")):
                    card = page.locator(f'.as-archive-card[data-run-status="{status}"]').first
                    assert card.evaluate("node => getComputedStyle(node).borderLeftColor") == rgb
                    colors[status] = card.locator(".as-status").evaluate("node => getComputedStyle(node).color")
                assert len(set(colors.values())) == 3
                page.locator(f'[data-as-technical="{stores[105].workspace.name}"]').click()
                expect(page.locator("#as-tech-panel-summary .as-status.running")).to_be_visible()
                expect(page.locator("#as-tech-panel-summary")).to_contain_text("2026-09-10")
                page.keyboard.press("Escape")
                page.locator(f'[data-as-technical="{stores[1].workspace.name}"]').click()
                expect(page.locator("#as-tech-panel-summary")).to_contain_text("HX202606040013-核销资料-诚成26年4月【堆头20家】")
                page.keyboard.press("Escape")
                target = stores[0].workspace.name
                page.locator("#as-archive-search").fill(target)
                assert ids("archive") == [target]
                select("overview")
                expect(page.locator("#as-search")).to_have_value(target)
                assert ids("overview") == [target]
                select("overview")
                expect(page.locator("#as-search")).to_have_value(target)
                assert ids("overview") == [target]
                before = {name: (stores[0].workspace / name).read_bytes() for name in ("manifest.json", "snapshot.json")}
                page.locator(f'[data-as-review="{target}"]').check()
                expect(page.locator(f'[data-run-card="{target}"]')).to_contain_text("已人工核验")
                select("archive")
                expect(page.locator("#as-archive-list")).to_contain_text("已人工核验")
                page.locator("#as-archive-refresh").click()
                expect(page.locator("#as-archive-list")).to_contain_text("已人工核验")
                assert all((stores[0].workspace / name).read_bytes() == value for name, value in before.items())
                page.locator("#as-archive-search").fill("SYNC-NO-MATCH")
                for view in ("overview", "archive"):
                    select(view)
                    assert ids(view) == []
                    counts(107)
                select("overview")
                page.locator("#as-search").fill("")
                assert len(ids("overview")) == 100
                select("archive")
                assert len(ids("archive")) == 100
                complete(stores[105])
                page.locator("#as-archive-refresh").click()
                expect(page.locator('#as-intake-panel')).to_be_hidden()
                counts(107)
                select("overview")
                expect(page.locator("#as-ledger-count")).to_have_text("107 / 107 条运行记录")
                create(107, oss=True)
                expect(page.locator("#as-nav-overview")).to_have_text("108", timeout=20000)
                expect(page.locator("#as-intake-list")).to_contain_text("OSS 上传")
                expect(page.locator("#as-ledger-count")).to_have_text("108 / 108 条运行记录")
                stores[107].fail(RuntimeError("transition from running to failed"))
                expect(page.locator("#as-ledger-count")).to_have_text("108 / 108 条运行记录", timeout=20000)
                expect(page.locator('#as-intake-panel')).to_be_hidden()
                select("archive")
                page.locator('[data-as-load-more="archive"]').click()
                archive_ids = ids("archive")
                assert len(archive_ids) == 108
                select("overview")
                assert ids("overview") == archive_ids
                page.locator("#as-search").fill(target)
                page.locator(f'[data-as-delete="{target}"]').click()
                page.locator("#as-delete-confirm").click()
                expect(page.locator("#as-delete-dialog")).not_to_be_visible()
                assert ids("overview") == [] and not stores[0].workspace.exists()
                for view in ("overview", "archive"):
                    select(view)
                    assert ids(view) == []
                counts(107)
                page.locator("#as-archive-search").fill("showcase")
                for width in (1440, 1280):
                    page.set_viewport_size({"width": width, "height": 960})
                    for view in ("overview", "archive"):
                        select(view)
                        assert len(ids(view)) == 2
                        counts(107)
                        assert page.evaluate("Math.max(document.documentElement.scrollWidth, document.body.scrollWidth)") <= width + 2
                        screenshot = args.artifacts / f"sync-{view}-{width}.png"
                        page.screenshot(path=str(screenshot), full_page=False, animations="disabled")
                        screenshots.append(str(screenshot))

                context.set_offline(True)
                for index in (1, 106):
                    archive = render_static_run_archive(stores[index].workspace, PROJECT / "offline-activity-audit.html")
                    page.goto(archive.as_uri())
                    counts(1)
                    for view in ("overview", "archive"):
                        select(view)
                        assert ids(view) == [stores[index].workspace.name]
                    assert page.locator("[data-as-delete]").count() == 0
                    if index == 106:
                        select("overview")
                        page.locator('[data-as-open]').click()
                        page.locator("#as-record-technical").click()
                        expect(page.locator("#as-tech-panel-summary .as-status.failed")).to_be_visible()
                context.close()
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert not thread.is_alive()
    assert not errors, errors
    assert not server_errors, server_errors
    report = {
        "status": "passed", "system_version": "2.11.26", "api_version": "1.39",
        "checks": ["all worktrees in ledger/archive with identical order/search/pagination", "overview contains the complete ledger and its actions",
                   "active input and OSS records with source tags", "creation changes total; completion keeps total and updates status", "failure preserves the run in the overview ledger",
                   "shared search across two lists", "pagination beyond 100", "inactive DOM released", "manual review/refresh/delete synchronized",
                   "business bytes unchanged by annotation", "three distinct status colors and text", "analysis date uses Shanghai timezone, not business date or creation when analysis time exists",
                   "static completed/failed list parity and reachable diagnosis", "1440/1280 desktop layout"],
        "screenshots": screenshots, "console_and_page_errors": errors, "http_errors": server_errors,
        "temporary_service_closed": True, "temporary_records_removed": not Path(temporary).exists(),
    }
    (args.artifacts / "record-sync-verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
