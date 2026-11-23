"""Read-only, synthetic showroom. Never creates runs, intake jobs or callbacks."""
from __future__ import annotations

import argparse
import hashlib
import json
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from .workbench_html import API_VERSION, SYSTEM_VERSION, inject_workbench_context, replace_audit_payload
from .workbench_store import main_flow_task_list
from .http_rejection import MAX_REJECTED_BODY_BYTES, REJECTED_BODY_TIMEOUT, discard_rejected_body


ASSETS = Path(__file__).resolve().parents[2] / "assets"
DEMO_NOTICE = "演示数据 · 仅供展示，不执行真实核销"


class DemoCatalog:
    def __init__(self) -> None:
        document = json.loads((ASSETS / "demo/records.json").read_text(encoding="utf-8"))
        self.records = {item["id"]: item for item in document["records"]}
        self.runs = [self.snapshot(key)["run"] for key in self.records]

    def snapshot(self, key: str) -> dict:
        record = self.records[key]
        count = len(record["issues"])
        run = {
            "workspace_id": key, "run_id": key, "display_name": record["title"],
            "source_archives": [record["title"] + ".zip"], "demo": True,
            "status": "completed", "created_at": record["date"],
            "analysis_started_at": record["date"], "updated_at": record["date"],
            "completed_at": record["date"], "input_source": "oss" if record["source"] == "input-oss" else "input",
            "scenarios": [record["scenario"]], "scenario_count": 1,
            "error_count": count, "manual_reviewed": False,
            "producer_model": "演示数据", "audit_model": "演示数据",
        }
        rows = [{
            "excel_row": index + 1, "kind": "record", "section": "detail",
            "status": "issue", "heading": issue["title"],
            "error_reason": issue["reason"], "check_category": issue["category"],
            "business_source_files": [issue["file"]],
            "values": [issue["file"], issue["reason"], "处理方式：" + issue["action"]],
        } for index, issue in enumerate(record["issues"])]
        checks = [{
            "check_id": f"demo-check-{index + 1}", "category": "演示检查项",
            "title": title, "subject": record["label"],
            "basis": "预设的虚构展示内容，未经实际材料识别或核验。",
            "source_files": [], "scope": "material",
        } for index, title in enumerate(record["checks"])]
        view = {
            "schema_version": "1.1", "title": DEMO_NOTICE, "demo": True,
            "sheets": [{
                "name": record["label"], "scenario": record["scenario"],
                "title": record["label"] + "展示样例", "note": DEMO_NOTICE,
                "projection_kind": "pdf_policy", "audit_type_label": record["label"],
                "headers": ["问题文件", "错误原因", "处理方式"], "rows": rows,
                "audit_counts": {"source_row_count": count, "error_count": count,
                                 "detail_error_count": count, "context_error_count": 0},
            }],
            "pass_check_log": {"groups": [{
                "scenario": record["scenario"], "audit_type": record["label"],
                "title": "演示检查项", "item_count": len(checks), "items": checks,
                "category_counts": {"演示检查项": len(checks)},
            }]},
        }
        return {
            "run": run, "view": view, "demo": True,
            "analysis_files": [{"path": "demo/record.json", "title": "虚构样例说明"}],
            "dom_checkpoints": [], "recent_events": [],
            "verification": {"demo": True, "notice": DEMO_NOTICE},
        }

    def html(self, key: str = "") -> bytes:
        snapshot = self.snapshot(key) if key else None
        context = {
            "api_version": API_VERSION, "system_version": SYSTEM_VERSION,
            "main_flow_tasks": main_flow_task_list(), "delivery_mode": "server",
            "mode": "record" if key else "system", "selected_run": key or None,
            "run": snapshot["run"] if snapshot else None, "view_available": bool(key),
            "analysis_files": snapshot["analysis_files"] if snapshot else [],
            "dom_checkpoints": [], "recent_events": [], "oss_intake_enabled": False,
            "demo": True, "read_only": True, "model_settings_writable": True,
        }
        html = (ASSETS / "offline-activity-audit.html").read_text(encoding="utf-8")
        payload = snapshot["view"] if snapshot else {"schema_version": "1.1", "sheets": []}
        return inject_workbench_context(replace_audit_payload(html, payload), context).encode("utf-8")


class DemoHTTPServer(ThreadingHTTPServer):
    def __init__(self, address, handler=None, *, settings_root=None):
        from .model_settings import ModelSettingsStore
        from .workbench_store import DEFAULT_WORKTREES_ROOT

        self.demo_catalog = DemoCatalog()
        self.model_settings = ModelSettingsStore(settings_root or DEFAULT_WORKTREES_ROOT)
        self.model_settings_token = secrets.token_urlsafe(32)
        self.model_probe_lock = threading.Lock()
        self.deletion_hosts = {"localhost", "127.0.0.1", socket.gethostname().lower()}
        super().__init__(address, handler or DemoHandler)


class DemoHandler(BaseHTTPRequestHandler):
    server_version = "OfflineAuditDemo"

    def _reply(self, value, status=200, content_type="application/json; charset=utf-8"):
        raw = value if isinstance(value, bytes) else json.dumps(value, ensure_ascii=False).encode("utf-8")
        etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
        unchanged = status == 200 and self.headers.get("If-None-Match") == etag
        self.send_response(304 if unchanged else status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "private, no-cache")
        self.send_header("ETag", etag)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Length", "0" if unchanged else str(len(raw)))
        self.end_headers()
        if not unchanged and self.command != "HEAD":
            self.wfile.write(raw)

    def do_GET(self):  # noqa: N802
        request = urlsplit(self.path)
        route = unquote(request.path)
        query = parse_qs(request.query)
        catalog = self.server.demo_catalog
        try:
            if route in {"/", "/offline-activity-audit.html"}:
                self._reply(catalog.html((query.get("run") or [""])[0]),
                            content_type="text/html; charset=utf-8")
            elif route == "/api/config":
                self._reply({
                    "api_version": API_VERSION, "system_version": SYSTEM_VERSION,
                    "demo": True, "notice": DEMO_NOTICE, "read_only": True,
                    "workbench_read_only": True, "business_results_read_only": True,
                    "oss_intake_enabled": False, "mysql_enabled": False,
                    "model_enabled": False, "callback_enabled": False,
                    "model_settings": {"endpoint": "/api/model-settings", "writable": True,
                                       "effective_scope": "new_runs_only", "manual_probe_only": True},
                    "manual_review": {"writable": False}, "run_deletion": {"writable": False},
                })
            elif route == "/api/model-settings":
                self._reply({**self.server.model_settings.public(),
                             "confirmation_token": self.server.model_settings_token})
            elif route == "/api/runs":
                self._reply({"runs": catalog.runs, "count": len(catalog.runs), "demo": True})
            elif route == "/api/intake/jobs":
                self._reply({"jobs": [], "count": 0, "demo": True})
            elif route.startswith("/api/runs/"):
                key, _, tail = route[len("/api/runs/"):].partition("/")
                snapshot = catalog.snapshot(key)
                if tail in {"", "snapshot"}:
                    self._reply(snapshot)
                elif tail == "analysis" and query.get("path") == ["demo/record.json"]:
                    self._reply({"notice": DEMO_NOTICE, "sample": catalog.records[key]})
                elif tail == "log":
                    self._reply((DEMO_NOTICE + "\n没有执行真实核销、模型调用、数据库查询或业务回调。\n").encode("utf-8"),
                                content_type="text/plain; charset=utf-8")
                else:
                    self._reply({"error": "演示资源不存在"}, 404)
            elif route == "/favicon.ico":
                self._reply(b"", 204, "image/x-icon")
            else:
                self._reply({"error": "演示资源不存在"}, 404)
        except KeyError:
            self._reply({"error": "演示记录不存在"}, 404)

    do_HEAD = do_GET

    def _json(self, value):
        self._reply(value)

    def _error(self, status, message):
        if status == 403:
            self._discard_rejected_body()
        self._reply({"error": message, "demo": True}, status)

    def _discard_rejected_body(self):
        discard_rejected_body(self)

    def _read_request_json(self):
        from .workbench_server import Handler
        return Handler._read_request_json(self)

    def do_POST(self):  # noqa: N802
        if urlsplit(self.path).path.rstrip("/") == "/api/model-settings":
            from .workbench_server import Handler
            return Handler._update_model_settings(self)
        self._read_only()

    def _read_only(self):
        self.close_connection = True
        self._error(403, "演示模式只允许查看，不执行真实核销或修改数据。")

    do_PUT = do_PATCH = do_DELETE = _read_only


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=DEMO_NOTICE)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    with DemoHTTPServer((args.host, args.port)) as server:
        print(json.dumps({"demo": True, "port": args.port, "notice": DEMO_NOTICE}, ensure_ascii=False), flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
    return 0
