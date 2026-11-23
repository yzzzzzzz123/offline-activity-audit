"""Exercise per-run token displays using isolated archives and optional live GETs."""
from __future__ import annotations

import argparse
import json
import tempfile
import threading
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from audit_core.model_metrics import collect_model_artifacts, record_model_attempt
from audit_core.execution_trace import collect_execution_trace, record_step
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import WorkbenchRunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-url")
    parser.add_argument("--output", type=Path, default=Path("artifacts/token-usage-20261027"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    errors = []
    report = {}
    with tempfile.TemporaryDirectory(prefix="audit-token-ui-") as temporary:
        root = Path(temporary)
        stores = {}
        for name in ("complete", "partial", "missing", "zero", "running", "mixed"):
            store = WorkbenchRunStore(root / ("20261027-token-" + name),
                                      run_id="20261027-token-" + name, producer_model="ppio",
                                      root_html=ROOT_HTML, workbench_url="http://127.0.0.1:8080/",
                                      source_archives=["Token-" + name + ".zip"], input_source="input")
            stores[name] = store
            if name == 'mixed':
                with collect_model_artifacts(store.analysis_root):
                    record_model_attempt({'model': 'deepseek-v4-1-flash', 'status': 'success', 'input_tokens': 100, 'output_tokens': 20})
                    record_model_attempt({'model': 'qwen3.8-max', 'status': 'success', 'input_tokens': 200, 'output_tokens': 30})
            if name == 'running':
                store.observe('scenario.started',{'scenario':'personnel_incentive'})
                with collect_execution_trace(store.analysis_root,store.observe):
                    record_step('database.query','读取本次商品数据库只读快照',{'table':'fixture_catalog.products'},status='started')
            if name in ("complete", "partial", "zero"):
                with collect_model_artifacts(store.analysis_root):
                    record_model_attempt({"status": "success", "input_tokens": 0 if name == "zero" else 1500,
                                          "output_tokens": 0 if name == "zero" else 250})
                    if name == "partial":
                        record_model_attempt({"status": "timeout"})
            if name != "running":
                store.complete(view_payload={"schema_version": "1.1", "sheets": []},
                               scenarios=[], verification={"verified": True})
        server = WorkbenchHTTPServer(("127.0.0.1", 0), Handler, catalog=WorkbenchCatalog(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-proxy-server"])
                try:
                    page = browser.new_page(viewport={"width": 1440, "height": 960}, device_scale_factor=1)
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(f"http://127.0.0.1:{server.server_address[1]}/", wait_until="networkidle")
                    for name, expected in (("complete", "1,750"), ("partial", "至少 1,750"),
                                           ("missing", "未记录"), ("zero", "0"), ("running", "统计中")):
                        meter = page.locator(f'[data-run-card="{stores[name].workspace_id}"] .as-token-usage')
                        expect(meter).to_be_visible()
                        expect(meter.locator("strong")).to_have_text(expected)
                        expected_parts = '输入：0 / 输出：0' if name == 'zero' else '输入：1,500 / 输出：250' if name in ('complete','partial') else '输入：未返回 / 输出：未返回' if name == 'running' else '输入：未记录 / 输出：未记录'
                        expect(meter.locator('.as-token-breakdown')).to_have_text(expected_parts)
                        report[name] = meter.inner_text()
                    mixed = page.locator(f'[data-run-card="{stores["mixed"].workspace_id}"] .as-token-usage')
                    expect(mixed.locator('strong')).to_have_text('350')
                    deepseek = mixed.locator('[data-token-model="deepseek-v4-1-flash"]')
                    qwen = mixed.locator('[data-token-model="qwen3.8-max"]')
                    expect(deepseek).to_contain_text('DeepSeek：120 Token')
                    expect(deepseek.locator('small')).to_have_text('输入：100 / 输出：20')
                    expect(qwen).to_contain_text('Qwen：230 Token')
                    expect(qwen.locator('small')).to_have_text('输入：200 / 输出：30')
                    assert deepseek.evaluate("node => getComputedStyle(node).borderLeftWidth === '2px' && getComputedStyle(node).borderTopWidth === '0px' && node.querySelector('small').getBoundingClientRect().top >= node.querySelector('span').getBoundingClientRect().bottom")
                    expect(page.locator('#as-tab-model, #as-view-model, #as-current-models')).to_have_count(0)
                    report['per_model'] = mixed.inner_text()
                    current = page.locator(f'[data-run-card="{stores["running"].workspace_id}"]')
                    expect(current.locator('[data-as-current-execution]')).to_have_text('AI 正在读取本次商品数据库只读快照')
                    for width in (1440, 1280):
                        page.set_viewport_size({"width": width, "height": 960})
                        expect(page.locator('[data-run-card]').first).to_be_visible()
                        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "Page overflows"
                        page.screenshot(path=str(args.output / f"isolated-{width}.png"), full_page=True)
                    current.evaluate("node => node.dataset.stableCard = 'same-node'")
                    with collect_execution_trace(stores['running'].analysis_root,stores['running'].observe):
                        record_step('qince.request','查询勤策本单业务明细',{'endpoint':'/fixture/verification'},status='started')
                    expect(current.locator('[data-as-current-execution]')).to_have_text('AI 正在访问勤策，读取本单业务明细',timeout=15000)
                    assert current.get_attribute('data-stable-card') == 'same-node', 'Progress should update without rebuilding the card'
                    report['progress_text_updated_in_place'] = True
                    with collect_model_artifacts(stores["running"].analysis_root):
                        record_model_attempt({"status": "success", "input_tokens": 100, "output_tokens": 20})
                    running = page.locator(f'[data-run-card="{stores["running"].workspace_id}"] .as-token-usage')
                    expect(running.locator("strong")).to_have_text("120", timeout=20000)
                    expect(running.locator('.as-token-breakdown')).to_have_text('输入：100 / 输出：20')
                    report["automatic_refresh"] = running.inner_text()
                    if args.live_url:
                        page.goto(args.live_url, wait_until="networkidle")
                        meter = page.locator('[data-run-card*="SQ202604300025-HX202605260014"] .as-token-usage')
                        expect(meter).to_be_visible()
                        expect(meter.locator("strong")).to_have_text("71,263")
                        expect(meter).to_contain_text("输入：")
                        expect(meter).to_contain_text("输出：")
                        report["live"] = meter.inner_text()
                        for width in (1440, 1280):
                            page.set_viewport_size({"width": width, "height": 960})
                            assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
                            page.screenshot(path=str(args.output / f"live-{width}.png"), full_page=True)
                    assert not errors, errors
                finally:
                    browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    report["page_errors"] = errors
    (args.output / "browser-result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
