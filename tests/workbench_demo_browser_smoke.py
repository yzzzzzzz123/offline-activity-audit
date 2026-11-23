"""Exercise the actual canonical UI against synthetic records, at desktop size."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import urljoin, urlsplit

from playwright.sync_api import expect, sync_playwright

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "skills/orchestrate-offline-audit/scripts"))
from audit_core.workbench_demo import DemoCatalog, DemoHTTPServer


def verify(base: str, output: Path) -> None:
    errors, requests = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server"])
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        page.on("request", lambda request: requests.append(request.url))
        config = page.request.get(urljoin(base, "api/config")).json()
        assert config.get("demo") is True, "Browser test requires explicit demo mode"
        page.goto(base)
        page.wait_for_load_state("networkidle")
        expect(page.locator(".as-run-card")).to_have_count(6)
        expect(page.locator(".as-demo-notice")).to_have_count(0)
        expect(page.locator("body")).not_to_contain_text("全部为虚构样例，仅供展示；不执行真实核销，不作为业务结算依据。")
        expect(page.locator("[data-as-delete], [data-as-review]")).to_have_count(0)
        expect(page.locator("#as-tab-model")).to_be_visible()
        page.locator("#as-tab-model").click()
        expect(page.locator("#as-view-model")).to_be_visible()
        expect(page.locator("#as-model-current")).to_contain_text("当前：")
        page.locator("#as-model-provider").select_option('qwen')
        expect(page.locator("#as-model-choice")).to_be_enabled()
        expect(page.locator("#as-model-choice")).to_have_value('qwen3.8-max')
        page.locator("#as-model-provider").select_option('ppio')
        expect(page.locator("#as-model-choice")).to_have_value('qwen/qwen3.8-max')
        page.screenshot(path=str(output / "model-settings.png"), full_page=True)
        page.locator("#as-tab-overview").click()
        page.screenshot(path=str(output / "overview.png"), full_page=True)

        page.locator("#as-overview-scenario").select_option("personnel_incentive")
        expect(page.locator(".as-run-card")).to_have_count(1)
        page.locator("#as-view-overview [data-as-clear-filters]").click()
        page.locator("#as-overview-source").select_option("input-oss")
        expect(page.locator(".as-run-card")).to_have_count(3)
        page.locator("#as-overview-date-from").fill("2026-10-18")
        page.locator("#as-overview-date-to").fill("2026-10-19")
        expect(page.locator(".as-run-card")).to_have_count(1)
        page.locator("#as-tab-archive").click()
        expect(page.locator(".as-archive-card")).to_have_count(1)
        expect(page.locator("#as-archive-source")).to_have_value("input-oss")
        page.locator("#as-view-archive [data-as-clear-filters]").click()
        expect(page.locator(".as-archive-card")).to_have_count(6)
        page.locator("#as-tab-overview").click()
        page.locator('[data-as-open="demo-personnel"]').click()
        page.wait_for_load_state("networkidle")
        expect(page.locator("#eoErrorList .eo-error-card")).to_have_count(2)
        expect(page.locator("#eoErrorList")).to_contain_text("尚差 100 元")
        page.screenshot(path=str(output / "detail.png"), full_page=True)

        catalog = DemoCatalog()
        for key, record in catalog.records.items():
            if key != "demo-personnel":
                page.locator("#as-record-select").select_option(key)
                page.wait_for_load_state("networkidle")
            expect(page.locator(".as-demo-notice")).to_have_count(0)
            expect(page.locator("#eoErrorList .eo-error-card")).to_have_count(len(record["issues"]))
            page.locator('[data-eo-view="passed"]').click()
            expect(page.locator("#eoPassList .eo-pass-card:visible")).to_have_count(len(record["checks"]))
            expect(page.locator("#eoPassList")).to_contain_text("未经实际材料识别或核验")
            page.locator('[data-eo-view="home"]').click()

        page.locator("#as-record-technical").click()
        expect(page.locator("#as-drawer-backdrop")).to_be_visible()
        expect(page.locator('[data-as-tech-tab]')).to_have_count(0)
        page.locator('#as-chain-support > summary').click()
        analysis = page.locator('[data-as-analysis="demo/record.json"]')
        analysis.locator(':scope > summary').click()
        expect(analysis.locator('.as-code')).to_contain_text("演示数据")
        log = page.locator('[data-as-resource="log"]')
        log.locator(':scope > summary').click()
        expect(log.locator('.as-code')).to_contain_text("没有执行真实核销")
        page.locator("#as-drawer-close").click()
        page.locator("#as-back-ledger").click()
        page.wait_for_load_state("networkidle")
        expect(page.locator(".as-run-card")).to_have_count(6)
        page.set_viewport_size({"width": 1280, "height": 960})
        for view in ("overview", "archive", "model"):
            page.locator(f"#as-tab-{view}").click()
            expect(page.locator(f"#as-view-{view}")).to_be_visible()
            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), view
            page.screenshot(path=str(output / f"{view}-1280.png"), full_page=True)
        assert not errors, errors
        origin = urlsplit(base).netloc
        assert all(urlsplit(url).netloc == origin for url in requests), requests
        assert not any("/api/intake/oss" in url for url in requests), requests
        browser.close()
    print(json.dumps({"passed": True, "records": 6, "browser_errors": errors,
                      "checks": ["overview", "scenario/source/date filters", "shared archive filters",
                                 "all record details", "correct checks", "record switching",
                                 "technical files/log", "read-only audit controls", "no external requests",
                                 "no banner", "model settings/provider choices", "1280px primary views"],
                      "screenshots": str(output)}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", help="Existing demo URL; omit for an isolated local fixture")
    args = parser.parse_args()
    output = Path(tempfile.mkdtemp(prefix="offline-demo-browser-"))
    if args.url:
        verify(args.url.rstrip("/") + "/", output)
        return
    with DemoHTTPServer(("127.0.0.1", 0), settings_root=output / 'model-settings') as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            verify(f"http://127.0.0.1:{server.server_port}/", output)
        finally:
            server.shutdown()
            thread.join()


if __name__ == "__main__":
    main()
