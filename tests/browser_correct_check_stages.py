"""Exercise the canonical renderer with synthetic data, without a server or network."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from playwright.sync_api import sync_playwright
from tests.test_error_only_frontend import ASSET, renderer_script
from tests.test_pass_check_frontend import fixture


def document(view: dict) -> str:
    source = ASSET.read_text(encoding="utf-8")
    styles = "\n".join(re.findall(r"<style\b[^>]*>[\s\S]*?</style>", source))
    data = json.dumps(view, ensure_ascii=False).replace("<", "\\u003c")
    return f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">{styles}</head><body><script id="audit-data" type="application/json">{data}</script><script>{renderer_script()}</script></body></html>'


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    results = []
    errors = []
    requests = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="chrome", headless=True, args=["--no-proxy-server"])
        context = browser.new_context(viewport={"width": 1440, "height": 960})
        context.route("**/*", lambda route: route.abort())
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: requests.append(request.url))
        for width in (1440, 1280):
            page.set_viewport_size({"width": width, "height": 960})
            page.set_content(document(fixture(business=True)), wait_until="networkidle")
            assert page.locator("#eoPassList .eo-pass-card").count() == 0, "Ledger must remain lazy"
            page.locator("#eo-tab-passed").click()
            assert page.locator("#passed").is_visible()
            assert page.locator("#eoPassStageCounts").inner_text() == "资料已提供 1 项 · 业务检查通过 1 项"
            assert page.locator(".eo-pass-card .eo-pass-index span").all_text_contents() == ["已提供", "通过"]
            assert page.locator(".eo-pass-stage-notice").first.inner_text().find("不代表") >= 0
            page.screenshot(path=str(args.output / f"mixed-{width}.png"), full_page=True, animations="disabled")
            page.locator("#eoPassCategory").select_option("资料完整性")
            assert page.locator(".eo-pass-card:visible").count() == 1
            assert page.locator(".eo-pass-card:visible").get_attribute("data-pass-stage") == "material_gate"
            page.locator("#eoPassCategory").select_option("金额核对")
            assert page.locator(".eo-pass-card:visible").get_attribute("data-pass-stage") == "business"
            page.locator("#eoPassCategory").select_option("")
            page.locator("#eo-tab-home").click()
            page.locator("#eo-tab-passed").click()
            assert page.locator(".eo-pass-card").count() == 2, "Reopening must not duplicate cards"
            dimensions = page.evaluate("({width:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth})")
            assert dimensions["scrollWidth"] <= dimensions["width"]
            results.append({"case": "mixed/filter/reopen", "width": width, "passed": True, "dimensions": dimensions})

        for kind in ("gate-empty-no-groups", "gate-empty-group", "business-empty"):
            view = fixture(material=False, empty_groups=kind != "gate-empty-no-groups")
            if kind == "business-empty":
                view["sheets"][0]["projection_kind"] = "pdf_policy"
            page.set_content(document(view), wait_until="networkidle")
            page.locator("#eo-tab-passed").click()
            assert page.locator("#eoPassList .eo-pass-empty").is_visible()
            assert page.locator("#eoPassList .eo-pass-group").count() == 0
            expected = "暂无明确通过的业务检查项" if kind == "business-empty" else "尚未进入业务审核"
            assert expected in page.locator("#eoPassList").inner_text()
            page.screenshot(path=str(args.output / f"{kind}.png"), full_page=True, animations="disabled")
            results.append({"case": kind, "passed": True, "text": page.locator("#eoPassList").inner_text()})

        view = fixture()
        view["pass_check_log"]["groups"][0]["notice"] = '<img src=x onerror="alert(1)">'
        page.set_content(document(view), wait_until="networkidle")
        page.locator("#eo-tab-passed").click()
        assert page.locator("#eoPassList img").count() == 0
        assert '<img src=x' in page.locator("#eoPassList").inner_text()
        results.append({"case": "escaped-notice", "passed": True})
        browser.close()
    receipt = {"source": str(ASSET), "network_requests": requests, "page_errors": errors, "cases": results}
    (args.output / "fixture-browser.json").write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding="utf-8")
    assert not errors, errors
    assert not requests, requests
    print(json.dumps({"passed": len(results), "page_errors": len(errors), "network_requests": len(requests), "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
