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
from audit_core.common import AuditError
from audit_core.scenario_registry import SCENARIO_LABELS, SCENARIO_ORDER
from audit_core.workbench_runtime import run_persistent_audit
from audit_core.workbench_server import WorkbenchCatalog, WorkbenchHTTPServer, Handler
from tests.pdf_test_support import bundle, image_bytes, PolicyProvider


def main():
    root = Path(tempfile.mkdtemp(prefix="pdf-workbench-desktop-"))
    completed = {}
    for index, scenario in enumerate(SCENARIO_ORDER):
        source = bundle(root / "input", f"{index + 1:02d}.zip")
        completed[scenario] = run_persistent_audit("20260923-pdf-browser", producer_model="codex",
            input_dir=source, worktrees_root=root / "worktrees", biz_type=SCENARIO_LABELS[scenario],
            evidence_provider=PolicyProvider())
    complete = completed["entry_fee"]
    bundle(root / "incomplete", "需补资料.zip", files={"01-核销资料.png": image_bytes(), "02-运输单.png": image_bytes("blue")})
    material_issues = run_persistent_audit("20260917-pdf-browser-materials", producer_model="codex",
        input_dir=root / "incomplete", worktrees_root=root / "worktrees",
        biz_type="KT板等物料制作", input_source="oss",
        evidence_provider=PolicyProvider(missing={0: ["invoice"]}, extra={0: [{
            "description": "运输单", "reason": "物料制作资料清单不需要运输单", "source_ids": ["a001-f0002"]}]}))
    assert material_issues["status"] == "completed" and material_issues["failure"] is None
    bundle(root / "interrupted", "系统中断.zip")
    before = set((root / "worktrees").iterdir())

    def interrupted(*args):
        raise AuditError("模拟模型连接中断")

    try:
        run_persistent_audit("20260917-pdf-browser-interrupted", producer_model="codex",
            input_dir=root / "interrupted", worktrees_root=root / "worktrees", evidence_provider=interrupted,
            biz_type="KT板等物料制作")
        raise AssertionError("系统意外中断不得生成已完成收据")
    except AuditError:
        pass
    failed_workspace = next(path for path in set((root / "worktrees").iterdir()) - before
                            if (path / "manifest.json").is_file())
    failed_manifest = json.loads((failed_workspace / "manifest.json").read_text(encoding="utf-8"))
    assert failed_manifest["status"] == "failed"
    invalid_biz_type = run_persistent_audit("20260921-invalid-biz-type", producer_model="codex",
        input_dir=root / "interrupted", worktrees_root=root / "worktrees",
        biz_type="", input_source="oss", evidence_provider=PolicyProvider())
    assert invalid_biz_type["status"] == "failed"
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
                assert response.json()["scenario_classification_policy"] == "biz_type"
                assert response.json()["local_input_type_policy"] == "ask_user_before_run"
                assert response.json()["material_problem_policy"] == "require_exact_material_items_before_audit"
                page.goto(base, wait_until="networkidle")
                expect(page.locator("#as-ledger-list .as-run-card")).to_have_count(len(completed) + 3)
                expect(page.locator('[data-as-view="ledger"]')).to_have_count(0)
                for scenario, receipt in completed.items():
                    page.goto(base + "?run=" + quote(receipt["workspace_id"]), wait_until="networkidle")
                    expect(page.locator("#eoErrorList .eo-error-card")).to_have_count(receipt["error_count"])
                    assert page.locator(f'#eoErrorList [data-audit-type="{SCENARIO_LABELS[scenario]}"]').count() > 0
                for width in (1440, 1280):
                    page.set_viewport_size({"width": width, "height": 960})
                    views = []
                    for delivery, url in (("served", base + "?run=" + quote(complete["workspace_id"])),
                                          ("static", Path(complete["static_html"]).as_uri() + "?run=" + quote(complete["workspace_id"]))):
                        page.goto(url, wait_until="networkidle")
                        page.wait_for_selector("body.error-only-page")
                        expect(page.locator("#eoErrorList .eo-error-card")).to_have_count(expected_errors)
                        assert page.locator('#eoErrorList [data-audit-type="条码费"]').count() > 0
                        expect(page.locator('#eoErrorList [data-audit-type="进场费"]')).to_have_count(0)
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
                    material_views = []
                    for delivery, url in (("served", base + "?run=" + quote(material_issues["workspace_id"])),
                                          ("static", Path(material_issues["static_html"]).as_uri() + "?run=" + quote(material_issues["workspace_id"]))):
                        page.goto(url, wait_until="networkidle")
                        page.wait_for_selector("body.error-only-page")
                        expect(page.locator(".as-failure")).to_have_count(0)
                        expect(page.locator("#eoErrorList .eo-error-card")).to_have_count(material_issues["error_count"])
                        body = page.locator("body").inner_text()
                        assert "缺少发票或收据" in body
                        assert "KT板等物料制作" in body and "如果申报的是" not in body
                        assert "多交了运输单" in body
                        assert "02-运输单.png" in body
                        assert "核销失败" not in body and "不予核销" not in body and "0核销" not in body
                        assert "ZIP 文件名" not in body
                        material_views.append(page.locator("#eoErrorList .eo-error-card").all_text_contents())
                        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                        if host == "127.0.0.1":
                            shot = root / f"{delivery}-material-issues-{width}.png"
                            page.screenshot(path=str(shot))
                            shots.append(str(shot))
                    assert material_views[0] == material_views[1]
                page.goto(base + "?run=" + quote(failed_manifest["workspace_id"]), wait_until="networkidle")
                expect(page.locator(".as-failure")).to_have_text("系统处理没有完成，暂时不能提供完整核销结果。")
                assert "缺少资料" not in page.locator("body").inner_text()
                for url in (base + "?run=" + quote(invalid_biz_type["workspace_id"]),
                            Path(invalid_biz_type["static_html"]).as_uri() + "?run=" + quote(invalid_biz_type["workspace_id"])):
                    page.goto(url, wait_until="networkidle")
                    expect(page.locator(".as-failure")).to_have_text("无法识别核销类型")
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
