"""只读验收正式单文件静态档案；不启动服务、重跑模型或修补页面。"""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import sys
from typing import Any
from urllib.parse import unquote, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "skills/orchestrate-offline-audit/scripts"))
from audit_core.workbench_html import SYSTEM_VERSION


class ArchiveVerificationError(RuntimeError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ArchiveVerificationError(message)


def _json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    _require(isinstance(value, dict), f"必须为 JSON 对象：{path.name}")
    return value


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class _EmbeddedDocuments(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.documents: dict[str, str] = {}
        self.meta: dict[str, str] = {}
        self.active: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "meta" and attributes.get("name"):
            self.meta[str(attributes["name"])] = str(attributes.get("content") or "")
        if tag == "script" and attributes.get("id") in {
            "audit-data", "audit-workbench-context", "audit-static-archive",
        }:
            name = str(attributes["id"])
            _require(name not in self.documents, f"静态 HTML 包含重复数据槽：{name}")
            self.active = name
            self.documents[name] = ""

    def handle_data(self, data: str) -> None:
        if self.active:
            self.documents[self.active] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self.active = None


def _inventory(workspace: Path) -> dict[str, str]:
    inventory: dict[str, str] = {}
    for path in sorted(workspace.rglob("*")):
        _require(not path.is_symlink() and not path.is_junction(), f"档案包含链接或 junction：{path.name}")
        _require(path.resolve().is_relative_to(workspace), f"档案成员越出运行目录：{path.name}")
        if path.is_file():
            inventory[path.relative_to(workspace).as_posix()] = _sha256(path)
    return inventory


def _indexed_resource(workspace: Path, item: dict[str, Any], *, require_hash: bool) -> Path:
    relative = item.get("path")
    _require(isinstance(relative, str) and bool(relative), "索引资源必须有路径")
    normalized = Path(relative)
    _require(not normalized.is_absolute() and ".." not in normalized.parts, "索引资源路径不安全")
    path = (workspace / normalized).resolve()
    _require(path.is_relative_to(workspace) and path.is_file(), f"索引资源不存在或越界：{relative}")
    size = item.get("size")
    _require(type(size) is int and path.stat().st_size == size, f"索引资源大小不匹配：{relative}")
    expected_hash = item.get("sha256")
    if require_hash or expected_hash is not None:
        _require(isinstance(expected_hash, str) and _sha256(path) == expected_hash, f"索引资源 SHA-256 不匹配：{relative}")
    return path


def verify_filesystem(workspace: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    _require(workspace.is_dir(), "指定的正式运行目录不存在")
    _require(not workspace.is_symlink() and not workspace.is_junction(), "正式运行目录不能是链接或 junction")
    workspace = workspace.resolve()
    inventory = _inventory(workspace)
    manifest = _json(workspace / "manifest.json")
    snapshot = _json(workspace / "snapshot.json")
    _require(manifest.get("status") == "completed", "正式运行尚未完成，不能验收为完成档案")
    _require(manifest.get("failure") is None, "已完成 manifest 不应保留失败对象")
    _require(manifest.get("workspace_id") == workspace.name, "manifest 运行 ID 与目录名不一致")
    _require(manifest.get("snapshot_sha256") == inventory.get("snapshot.json"), "snapshot 字节与 manifest SHA-256 不一致")
    run = snapshot.get("run")
    _require(isinstance(run, dict), "snapshot 缺少运行信息")
    for field in ("workspace_id", "run_id", "status", "producer_model", "audit_model", "reasoning_effort", "scenarios", "scenario_count", "error_count"):
        _require(run.get(field) == manifest.get(field), f"snapshot 与 manifest 的 {field} 不一致")
    _require(type(manifest.get("error_count")) is int and manifest["error_count"] >= 0, "错误数必须为非负整数")
    scenarios = manifest.get("scenarios")
    _require(isinstance(scenarios, list) and bool(scenarios), "正式运行缺少场景清单")
    _require(len(scenarios) == len(set(scenarios)) == manifest.get("scenario_count"), "正式运行场景数量不一致")
    view = snapshot.get("view")
    _require(isinstance(view, dict) and isinstance(view.get("sheets"), list), "完成快照缺少正式业务视图")
    _require([sheet.get("scenario") for sheet in view["sheets"]] == scenarios, "业务视图没有按顺序覆盖正式场景")
    issue_rows = sum(row.get("status") == "issue" for sheet in view["sheets"] for row in sheet.get("rows", []))
    _require(issue_rows == manifest["error_count"], "业务视图中的错误行数与 manifest 不一致")

    html_files = [name for name in inventory if Path(name).suffix.lower() in {".html", ".htm"}]
    _require(html_files == ["offline-activity-audit.html"], f"运行目录必须仅有唯一正式 HTML，实际：{html_files}")
    forbidden_extensions = {
        ".xlsx", ".xls", ".xlsm", ".xlsb", ".zip", ".rar", ".7z", ".pdf",
        ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tmp", ".pyc",
    }
    forbidden_names = {
        "result.json", "evidence.json", "contract-evidence.json", "contract-product-cells.json",
        "photo-evidence.json", "product-query.json", "display-standard-review.json",
        "codex-output.schema.json", "model-catalog.json", "stdout.log", "stderr.log",
        "stdout.jsonl", "stderr.jsonl", "prompt.txt", "response.json", "raw-output.json",
    }
    forbidden_directories = {"input", "sources", "extracted", "tmp", "temp", ".prepared", ".audit-tmp", "__pycache__"}
    leftovers = []
    for name in inventory:
        path = Path(name)
        if (
            path.suffix.lower() in forbidden_extensions
            or path.name.lower() in forbidden_names
            or any(part.casefold() in forbidden_directories or part.casefold().startswith("model-promotional_display") for part in path.parts[:-1])
        ):
            leftovers.append(name)
    _require(not leftovers, f"档案含工作簿、原始模型输出或解压临时残留：{leftovers}")

    summary = manifest.get("analysis_summary")
    _require(isinstance(summary, dict), "manifest 缺少分析摘要索引")
    _indexed_resource(workspace, summary, require_hash=True)
    analysis_files = snapshot.get("analysis_files")
    checkpoints = snapshot.get("dom_checkpoints")
    _require(isinstance(analysis_files, list) and bool(analysis_files), "snapshot 缺少分析文件索引")
    _require(isinstance(checkpoints, list) and bool(checkpoints), "snapshot 缺少 DOM 检查点索引")
    resource_paths = []
    for item in analysis_files:
        _require(isinstance(item, dict), "分析文件索引必须为对象")
        resource_paths.append(_indexed_resource(workspace, item, require_hash=True))
    checkpoint_ids = []
    for item in checkpoints:
        _require(isinstance(item, dict), "检查点索引必须为对象")
        path = _indexed_resource(workspace, item, require_hash=False)
        checkpoint = _json(path)
        _require(checkpoint.get("checkpoint_id") == item.get("checkpoint_id"), "检查点 ID 与其索引不一致")
        resource_paths.append(path)
        checkpoint_ids.append(item.get("checkpoint_id"))
    _require(len(resource_paths) == len(set(resource_paths)), "分析或检查点索引重复引用资源")
    _require(len(checkpoint_ids) == len(set(checkpoint_ids)), "检查点 ID 重复")
    allowed_files = {
        "manifest.json", "snapshot.json", "offline-activity-audit.html",
        "logs/run.log", "logs/events.jsonl", "analysis/model-metrics.jsonl",
        Path(summary["path"]).as_posix(),
        *(path.relative_to(workspace).as_posix() for path in resource_paths),
    }
    for name in inventory:
        path = Path(name)
        # 正式计量收集器另存的首轮/聚焦/校准可见事实不属于原始模型输出。
        if path.parts[:2] == ("analysis", "model-observations") and len(path.parts) == 3 and path.suffix == ".json":
            _json(workspace / path)
            allowed_files.add(name)
    unindexed = sorted(set(inventory) - allowed_files)
    _require(not unindexed, f"档案含未批准文件，无法证明没有原始输出或临时残留：{unindexed}")

    html = workspace / html_files[0]
    parser = _EmbeddedDocuments()
    parser.feed(html.read_text(encoding="utf-8"))
    documents: dict[str, dict[str, Any]] = {}
    for name in ("audit-data", "audit-workbench-context", "audit-static-archive"):
        _require(name in parser.documents, f"正式 HTML 缺少自包含数据槽：{name}")
        value = json.loads(parser.documents[name])
        _require(isinstance(value, dict), f"HTML 数据槽必须为 JSON 对象：{name}")
        documents[name] = value
    context = documents["audit-workbench-context"]
    archive = documents["audit-static-archive"]
    _require(parser.meta.get("offline-audit-delivery-mode") == "static_archive", "HTML 未标记为静态档案")
    _require(context.get("delivery_mode") == "static_archive" and context.get("system_version") == SYSTEM_VERSION,
             f"HTML 静态模式或当前系统版本不正确，期望 {SYSTEM_VERSION}")
    _require(context.get("selected_run") == archive.get("workspace_id") == workspace.name, "HTML 内嵌运行 ID 不一致")
    for catalog in (context.get("static_catalog"), archive.get("catalog")):
        _require(isinstance(catalog, dict) and catalog.get("count") == 1, "HTML 目录必须只包含当前运行")
        runs = catalog.get("runs")
        _require(isinstance(runs, list) and len(runs) == 1 and runs[0].get("workspace_id") == workspace.name, "HTML 目录包含其他运行")
        _require(runs[0].get("status") == "completed", "HTML 目录运行尚未完成")
    _require(archive.get("snapshot", {}).get("run", {}).get("workspace_id") == workspace.name, "HTML 内嵌快照 ID 不一致")
    _require(documents["audit-data"].get("sheets") == view["sheets"], "HTML 中的业务行与已完成快照不同")
    for item in analysis_files:
        embedded = archive.get("analysis", {}).get(item["path"])
        _require(embedded == _json(workspace / item["path"]), f"HTML 缺少或改写批准分析：{item['path']}")
    for item in checkpoints:
        embedded = archive.get("checkpoints", {}).get(item["checkpoint_id"])
        _require(embedded == _json(workspace / item["path"]), f"HTML 缺少或改写检查点：{item['checkpoint_id']}")
    _require(archive.get("log") == (workspace / "logs" / "run.log").read_text(encoding="utf-8"), "HTML 日志与原档案不同")
    checks = {
        "workspace_id": workspace.name, "run_id": manifest["run_id"],
        "scenarios": scenarios, "model": manifest.get("audit_model"),
        "reasoning_effort": manifest.get("reasoning_effort"), "error_count": manifest["error_count"],
        "snapshot_sha256": manifest["snapshot_sha256"], "html_path": str(html),
        "html_sha256": inventory[html_files[0]], "file_count": len(inventory),
        "analysis_files_verified": len(analysis_files), "checkpoints_verified": len(checkpoints),
        "temporary_residue_count": 0, "system_version": context["system_version"],
    }
    return checks, snapshot, inventory


def verify_browser(workspace: Path, expected_errors: int, *, timeout_ms: int, screenshot: Path | None) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    html = workspace / "offline-activity-audit.html"
    remote_requests: list[dict[str, str]] = []
    other_local_requests: list[str] = []
    request_failures: list[dict[str, str]] = []
    console_errors: list[str] = []
    page_errors: list[str] = []
    widths: dict[str, dict[str, int]] = {}

    def safe_request(request: Any) -> dict[str, str]:
        parsed = urlsplit(request.url)
        return {"scheme": parsed.scheme, "host": parsed.hostname or "", "resource_type": request.resource_type}

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1440, "height": 960}, service_workers="block")
            context.set_offline(True)
            page = context.new_page()
            page.set_default_timeout(timeout_ms)

            def route_request(route: Any) -> None:
                parsed = urlsplit(route.request.url)
                if parsed.scheme in {"data", "blob", "about"}:
                    route.continue_()
                elif parsed.scheme == "file" and unquote(parsed.path).casefold() == unquote(urlsplit(html.as_uri()).path).casefold():
                    route.continue_()
                else:
                    if parsed.scheme == "file":
                        other_local_requests.append(Path(unquote(parsed.path)).name)
                    else:
                        remote_requests.append(safe_request(route.request))
                    route.abort()

            context.route("**/*", route_request)
            page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
            page.on("pageerror", lambda error: page_errors.append(str(error)))
            page.on("requestfailed", lambda request: request_failures.append({**safe_request(request), "failure": str(request.failure or "") }))

            def same_file() -> None:
                parsed = urlsplit(page.url)
                _require(parsed.scheme == "file" and unquote(parsed.path).casefold() == unquote(urlsplit(html.as_uri()).path).casefold(), "浏览过程中离开了唯一正式本地 HTML")

            def no_overflow(label: str) -> None:
                values = page.evaluate("""() => ({viewport: innerWidth, document: document.documentElement.scrollWidth, body: document.body.scrollWidth})""")
                widths[label] = values
                _require(max(values["document"], values["body"]) <= values["viewport"] + 2, f"{label} 在 1440px 下横向溢出：{values}")

            def primary_ready() -> None:
                page.wait_for_load_state("networkidle")
                page.wait_for_selector("body.audit-system-page")
                page.wait_for_function("""() => document.querySelector('#as-recent-list') && !document.querySelector('#as-recent-list').textContent.includes('正在读取')""")
                same_file()

            page.goto(html.as_uri(), wait_until="networkidle", timeout=timeout_ms)
            primary_ready()
            no_overflow("overview")
            page.locator("#as-tab-ledger").click()
            page.locator("#as-view-ledger").wait_for(state="visible")
            open_record = page.locator("#as-ledger-list [data-as-open]")
            _require(open_record.count() == 1, "离线台账必须恰好存在一条可打开的完成记录")
            _require(open_record.get_attribute("data-as-open") == workspace.name, "台账按钮指向其他运行")
            _require(page.locator("[data-as-delete]").count() == 0, "离线档案不应提供删除控件")
            no_overflow("ledger")
            with page.expect_navigation(wait_until="networkidle", timeout=timeout_ms):
                open_record.click()
            page.wait_for_selector("body.error-only-page")
            page.locator("#eoErrorList").wait_for(state="visible")
            same_file()
            _require(page.locator("#eo-tab-home").get_attribute("aria-selected") == "true", "记录默认页面不是错误总览")
            cards = page.locator("#eoErrorList .eo-error-card")
            error_card_count = cards.count()
            # The frozen UI groups contract/knowledge/sales failures, while the
            # manifest counts failed source rows. Those are different units.
            _require((0 < error_card_count <= expected_errors) if expected_errors else error_card_count == 0,
                     "错误分组数量与已校验业务错误行的存在性或数量范围不一致")
            _require(page.locator(".eo-pass-filter-head, #eoErrorVisible, #eoPassVisible").count() == 0, "筛选区不应显示标题或命中统计")
            if expected_errors:
                card_summaries = cards.evaluate_all("""nodes => nodes.map(node => ({name: node.getAttribute('aria-label'), title: node.querySelector('h3')?.textContent.trim(), action: node.querySelector('.eo-field.action')?.textContent.trim(), type: node.dataset.auditType}))""")
                _require(all(item["name"] and item["title"] and item["action"] and item["type"] for item in card_summaries), "错误卡缺少标题、可访问名称、核销类型或处理动作")
                categories = page.locator("#eoErrorCategory")
                if categories.locator("option").count() > 1:
                    categories.select_option(index=1)
                    _require(0 < page.locator("#eoErrorList .eo-error-card:visible").count() <= error_card_count, "错误原因分类筛选数量不正确")
                    categories.select_option(value="")
                    _require(page.locator("#eoErrorList .eo-error-card:visible").count() == error_card_count, "清除错误原因分类后未恢复全部卡片")
            else:
                _require(page.locator("#eoErrorList .eo-empty").is_visible(), "无错误结果没有显示明确空状态")
            no_overflow("errors")
            if screenshot is not None:
                screenshot.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(screenshot), full_page=True)
            page.locator("#eo-tab-passed").click()
            page.locator("#passed").wait_for(state="visible")
            _require(page.locator("#eo-tab-passed").get_attribute("aria-selected") == "true", "正确检查项页签不能切换")
            pass_count = page.locator(".eo-pass-card").count()
            no_overflow("passed")
            with page.expect_navigation(wait_until="networkidle", timeout=timeout_ms):
                page.locator("#as-back-ledger").click()
            primary_ready()
            no_overflow("return_to_primary")
            _require(not urlsplit(page.url).query, "返回一级页面后仍有其他运行查询")
        finally:
            browser.close()
    failures = {
        "remote_requests": remote_requests, "other_local_dependencies": other_local_requests,
        "request_failures": request_failures, "console_errors": console_errors, "page_errors": page_errors,
    }
    _require(not any(failures.values()), "浏览器或自包含验收失败：" + json.dumps(failures, ensure_ascii=False))
    return {
        "viewport": {"width": 1440, "height": 960}, "headless": True,
        "external_network_blocked": True, "remote_dependency_count": 0,
        "local_dependency_count": 0, "console_error_count": 0, "page_error_count": 0,
        "request_failure_count": 0, "error_rows": expected_errors, "error_cards": error_card_count, "pass_cards": pass_count,
        "error_filter_verified": expected_errors > 0, "local_roundtrip_verified": True,
        "horizontal_widths": widths, "screenshot": str(screenshot) if screenshot else None,
    }


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="只读验证正式工作区的唯一自包含 HTML、快照哈希、来源索引及 PC 页面；不调用模型、不启动服务。")
    parser.add_argument("workspace", type=Path, help="已完成正式 worktree 的绝对或相对路径")
    parser.add_argument("--screenshot", type=Path, help="可选：将错误总览截图写到工作区以外的新 PNG 文件")
    parser.add_argument("--timeout-ms", type=int, default=30000, help="浏览器单步等待毫秒数，默认 30000")
    args = parser.parse_args(argv)
    try:
        _require(args.timeout_ms > 0, "timeout-ms 必须为正整数")
        _require(not args.workspace.is_symlink() and not args.workspace.is_junction(), "正式工作区不能是链接或 junction")
        workspace = args.workspace.resolve()
        screenshot = args.screenshot.resolve() if args.screenshot else None
        if screenshot:
            _require(screenshot.suffix.lower() == ".png", "截图必须是 PNG 文件")
            _require(not screenshot.is_relative_to(workspace), "截图不能写入不可变正式工作区")
            _require(not screenshot.exists(), "指定截图已经存在，验证器不会覆盖")
        checks, _snapshot, before = verify_filesystem(workspace)
        browser = verify_browser(workspace, checks["error_count"], timeout_ms=args.timeout_ms, screenshot=screenshot)
        _require(_inventory(workspace) == before, "浏览验证前后正式档案的文件或字节发生变化")
        print(json.dumps({"status": "passed", "filesystem": checks, "browser": browser, "archive_bytes_unchanged": True}, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "failed", "error_type": type(exc).__name__, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
