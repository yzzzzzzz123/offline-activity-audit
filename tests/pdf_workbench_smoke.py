"""Desktop validation of PDF-policy reports using isolated injected model facts."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile
import threading
from urllib.parse import quote

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "skills/orchestrate-offline-audit/scripts"))

from playwright.sync_api import sync_playwright, expect
from audit_core.scenario_registry import SCENARIO_ORDER
from audit_core.workbench_runtime import run_persistent_audit
from audit_core.workbench_server import WorkbenchCatalog, WorkbenchHTTPServer, Handler
from tests.pdf_test_support import bundle, PolicyProvider


def main():
    root = Path(tempfile.mkdtemp(prefix="pdf-workbench-desktop-"))
    for index in range(len(SCENARIO_ORDER)):
        bundle(root / "input", f"{index + 1:02d}.zip")
    complete = run_persistent_audit("20260917-pdf-browser", producer_model="codex",
        input_dir=root / "input", worktrees_root=root / "worktrees", evidence_provider=PolicyProvider(SCENARIO_ORDER))
    bundle(root / "incomplete", "需补资料.zip")
    failed = run_persistent_audit("20260917-pdf-browser-failure", producer_model="codex",
        input_dir=root / "incomplete", worktrees_root=root / "worktrees",
        evidence_provider=PolicyProvider(candidates={0: []}))
    snapshot = json.loads(Path(complete["snapshot"]).read_text(encoding="utf-8"))
    expected_errors = complete["error_count"]
    expected_passes = snapshot["view"]["pass_check_log"]["total"]
    issues = []
    signatures = []
    shots = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 960})
        page.on("pageerror", lambda error: issues.append(str(error)))
        page.on("console", lambda message: issues.append(message.text) if message.type == "error" else None)
        for host in ("127.0.0.1", "0.0.0.0"):
            server = WorkbenchHTTPServer((host, 0), Handler, catalog=WorkbenchCatalog(root / "worktrees"))
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = f"http://127.0.0.1:{server.server_address[1]}/"
            try:
                response = page.request.get(base + "api/config")
                assert response.json()["scenario_classification_policy"] == "material_content"
                assert response.json()["material_problem_policy"] == "require_exact_material_items_before_audit"
                page.goto(base, wait_until="networkidle")
                expect(page.locator("#as-ledger-list .as-run-card")).to_have_count(2)
                expect(page.locator('[data-as-view="ledger"]')).to_have_count(0)
                for width in (1440, 1280):
                    page.set_viewport_size({"width": width, "height": 960})
                    views = []
                    for delivery, url in (("served", base + "?run=" + quote(complete["workspace_id"])),
                                          ("static", Path(complete["static_html"]).as_uri() + "?run=" + quote(complete["workspace_id"]))):
                        page.goto(url, wait_until="networkidle")
                        page.wait_for_selector("body.error-only-page")
                        expect(page.locator("#eoErrorList .eo-error-card")).to_have_count(expected_errors)
                        errors = page.locator("#eoErrorList .eo-error-card").all_text_contents()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (delivery, width)
                        if host == "127.0.0.1":
                            shot = root / f"{delivery}-errors-{width}.png"
                            page.screenshot(path=str(shot))
                            shots.append(str(shot))
                        page.get_by_role("tab", name="正确检查项").click()
                        expect(page.locator("#eoPassList .eo-pass-card")).to_have_count(expected_passes)
                        passes = page.locator("#eoPassList .eo-pass-card").all_text_contents()
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        views.append((errors, passes))
                        if delivery == "static":
                            expect(page.locator("[data-as-delete]")).to_have_count(0)
                            page.locator("#as-back-ledger").click()
                            expect(page.locator("#as-view-overview")).to_be_visible()
                            assert page.url.startswith("file:")
                    assert views[0] == views[1], "服务与静态档案的错误和通过项不一致"
                    signatures.append((host, width, len(views[0][0]), len(views[0][1])))
                for width in (1440, 1280):
                    page.set_viewport_size({"width": width, "height": 960})
                    for delivery, url in (("served", base + "?run=" + quote(failed["workspace_id"])),
                                          ("static", Path(failed["static_html"]).as_uri() + "?run=" + quote(failed["workspace_id"]))):
                        page.goto(url, wait_until="networkidle")
                        expect(page.locator(".as-failure")).to_have_text("核销方式无法确认")
                        body = page.locator("body").inner_text()
                        assert "资料清单不匹配" not in body
                        assert "ZIP 文件名" not in body
                        assert "具体原因如下" not in body
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        if host == "127.0.0.1":
                            shot = root / f"{delivery}-classification-failure-{width}.png"
                            page.screenshot(path=str(shot))
                            shots.append(str(shot))
            finally:
                server.shutdown()
                thread.join(timeout=5)
                server.server_close()
        browser.close()
    assert not issues, issues
    report = {"fixtures": str(root), "checks": signatures, "screenshots": shots, "console_errors": issues}
    (root / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
