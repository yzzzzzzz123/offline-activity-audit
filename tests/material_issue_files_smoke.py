"""Check targeted material filenames on served and offline desktop reports."""
from __future__ import annotations

import io
import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/orchestrate-offline-audit/scripts"))

import openpyxl
from playwright.sync_api import sync_playwright, expect

from audit_core.workbench_runtime import run_persistent_audit
from audit_core.workbench_server import WorkbenchCatalog, WorkbenchHTTPServer, Handler
from tests.pdf_test_support import bundle, image_bytes, PolicyProvider
from tests.test_material_issue_files import SALES_FILE


def main():
    root = Path(tempfile.mkdtemp(prefix="material-issue-files-desktop-"))
    workbook = openpyxl.Workbook()
    workbook.active.append(["本次销售明细测试资料"])
    output = io.BytesIO()
    workbook.save(output)
    workbook.close()
    source = bundle(root / "input", "资料问题测试.zip", files={
        "1.厚街汉邦66广场家家欣.jpg": image_bytes(), "2.松岗百佳华.jpg": image_bytes(),
        SALES_FILE: output.getvalue(), "3.公明百佳华.jpg": image_bytes(),
        "郑州雨瑶5.1-5.20人员激励活动结算单.jpg": image_bytes(),
        "雨瑶人员激励参半销售数据2.xlsx": output.getvalue(),
        "雨瑶人员激励红包图片.jpg": image_bytes(), "雨瑶人员激励红包图片2.jpg": image_bytes()})

    def precise_pos(case, value):
        if case["kind"] != "pdf_material_classification":
            return
        ids = [u["unit_id"] for u in case["units"] if u["source_file"] == SALES_FILE]
        assert ids
        match = value["classification"]["material_matches"][0]
        next(m for m in match["materials"] if m["id"] == "pos_excel")["source_ids"] = ids

    receipt = run_persistent_audit("20261015-material-files-browser", producer_model="codex",
        input_dir=source, worktrees_root=root / "worktrees", biz_type="人员激励",
        evidence_provider=PolicyProvider(missing={0: ["pos", "promotion_contract"]}, mutate=precise_pos))
    assert receipt["status"] == "completed"
    assert receipt["error_count"] == 2
    errors = []
    shots = []
    signatures = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
        for host in ("127.0.0.1", "0.0.0.0"):
            server = WorkbenchHTTPServer((host, 0), Handler, catalog=WorkbenchCatalog(root / "worktrees"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}/"
            try:
                config = page.request.get(base + "api/config").json()
                assert config["api_version"] == "1.56"
                assert config["material_issue_file_policy"] == "related_files_only_hide_archive_gaps"
                for width in (1440, 1280):
                    page.set_viewport_size({"width": width, "height": 960})
                    deliveries = []
                    for kind, url in (("served", base + "?run=" + quote(receipt["workspace_id"])),
                                      ("static", Path(receipt["static_html"]).as_uri() + "?run=" + quote(receipt["workspace_id"]))):
                        page.goto(url, wait_until="networkidle")
                        cards = page.locator("#eoErrorList .eo-error-card")
                        expect(cards).to_have_count(2)
                        stamp = cards.filter(has_text="缺少盖章版销售明细。")
                        contract = cards.filter(has_text="缺少促销合同。")
                        expect(stamp.locator(".eo-evidence-source")).to_have_text([SALES_FILE])
                        expect(contract.locator(".eo-evidence")).to_have_count(0)
                        assert all(".jpg" not in text for text in cards.all_text_contents())
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        deliveries.append(cards.all_text_contents())
                        if host == "127.0.0.1":
                            shot = root / f"{kind}-materials-{width}.png"
                            page.screenshot(path=str(shot))
                            shots.append(str(shot))
                        if kind == "static":
                            page.locator("#as-back-ledger").click()
                            expect(page.locator("#as-view-overview")).to_be_visible()
                            assert page.url.startswith("file:")
                    assert deliveries[0] == deliveries[1]
                    signatures.append((host, width, 2))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
        browser.close()
    assert not errors, errors
    report = {"fixtures": str(root), "checks": signatures, "screenshots": shots, "console_errors": errors}
    (root / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
