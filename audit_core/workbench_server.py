from __future__ import annotations

import argparse
import hashlib
import json
import mimetypes
import re
import socket
import sys
import time
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .html_report import DATA_CLOSE, DATA_OPEN
from .workbench_runtime import ROOT_HTML
from .workbench_store import (
    DEFAULT_WORKTREES_ROOT,
    WORKSPACE_ID_PATTERN,
    main_flow_task_list,
    read_json_file,
)


API_VERSION = "1.1"
SYSTEM_VERSION = "2.2.0"
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
MAX_API_BYTES = 24 * 1024 * 1024
WORKBENCH_CONTEXT_ID = "audit-workbench-context"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_workspace_id(value: str) -> str:
    decoded = unquote(value).strip()
    if WORKSPACE_ID_PATTERN.fullmatch(decoded) is None:
        raise ValueError("运行 ID 不安全")
    return decoded


def _legacy_payload(path: Path) -> dict[str, Any]:
    html = path.read_text(encoding="utf-8")
    start = html.find(DATA_OPEN)
    if start < 0:
        raise ValueError("旧 HTML 缺少核销数据")
    start += len(DATA_OPEN)
    end = html.find(DATA_CLOSE, start)
    if end < 0:
        raise ValueError("旧 HTML 核销数据未闭合")
    value = json.loads(html[start:end])
    if not isinstance(value, dict):
        raise ValueError("旧 HTML 核销数据结构无效")
    return value


def _error_count(view: dict[str, Any] | None) -> int:
    if not view:
        return 0
    return sum(
        int((sheet.get("audit_counts") or {}).get("error_count") or 0)
        for sheet in view.get("sheets") or []
        if isinstance(sheet, dict)
    )


def _script_json(value: Any) -> str:
    """Serialize JSON safely for an inline application/json script element."""
    return (
        json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        .replace("<", "\\u003c")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def _replace_audit_payload(html: str, payload: dict[str, Any]) -> str:
    start = html.find(DATA_OPEN)
    if start < 0:
        raise ValueError("固定页面缺少 audit-data 数据槽")
    start += len(DATA_OPEN)
    end = html.find(DATA_CLOSE, start)
    if end < 0:
        raise ValueError("固定页面的 audit-data 数据槽未闭合")
    return html[:start] + _script_json(payload) + html[end:]


def _inject_workbench_context(html: str, context: dict[str, Any]) -> str:
    closing_head = html.lower().find("</head>")
    if closing_head < 0:
        raise ValueError("固定页面缺少 head 闭合标签")
    tag = (
        f'<script id="{WORKBENCH_CONTEXT_ID}" type="application/json">'
        f"{_script_json(context)}</script>\n"
    )
    return html[:closing_head] + tag + html[closing_head:]


def _local_ipv4_addresses() -> list[str]:
    values: set[str] = {"127.0.0.1"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            address = str(info[4][0])
            if address and not address.startswith("169.254."):
                values.add(address)
    except OSError:
        pass
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            probe.connect(("8.8.8.8", 80))
            values.add(str(probe.getsockname()[0]))
        finally:
            probe.close()
    except OSError:
        pass
    return sorted(values, key=lambda value: (value == "127.0.0.1", value))


class WorkbenchCatalog:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _workspace(self, workspace_id: str) -> Path:
        safe_id = _safe_workspace_id(workspace_id)
        candidate = (self.root / safe_id).resolve()
        if candidate.parent != self.root:
            raise ValueError("运行路径越出 worktrees")
        return candidate

    def list_runs(self) -> list[dict[str, Any]]:
        runs: list[dict[str, Any]] = []
        for path in self.root.iterdir():
            if path.name.startswith("."):
                continue
            try:
                if path.is_dir() and (path / "manifest.json").is_file():
                    manifest = read_json_file(path / "manifest.json")
                    runs.append(self._summary(manifest, storage_type="worktree"))
                elif path.is_file() and path.suffix.lower() == ".html":
                    payload = _legacy_payload(path)
                    modified = datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat(timespec="seconds")
                    scenarios = [
                        str(sheet.get("scenario") or "")
                        for sheet in payload.get("sheets") or []
                        if isinstance(sheet, dict) and sheet.get("scenario")
                    ]
                    runs.append(
                        {
                            "workspace_id": path.stem,
                            "run_id": path.stem,
                            "business_date": path.stem[:8],
                            "producer_model": "legacy",
                            "status": "completed",
                            "created_at": modified,
                            "updated_at": modified,
                            "completed_at": modified,
                            "scenarios": scenarios,
                            "scenario_count": len(scenarios),
                            "error_count": _error_count(payload),
                            "storage_type": "legacy_html",
                            "snapshot_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        }
                    )
            except (OSError, ValueError, json.JSONDecodeError):
                continue
        runs.sort(
            key=lambda item: (
                str(item.get("business_date") or ""),
                str(item.get("created_at") or ""),
                str(item.get("workspace_id") or ""),
            ),
            reverse=True,
        )
        return runs

    @staticmethod
    def _summary(manifest: dict[str, Any], *, storage_type: str) -> dict[str, Any]:
        return {
            "workspace_id": manifest.get("workspace_id"),
            "run_id": manifest.get("run_id"),
            "business_date": manifest.get("business_date"),
            "producer_model": manifest.get("producer_model"),
            "status": manifest.get("status"),
            "created_at": manifest.get("created_at"),
            "updated_at": manifest.get("updated_at"),
            "completed_at": manifest.get("completed_at"),
            "scenarios": list(manifest.get("scenarios") or []),
            "scenario_count": int(manifest.get("scenario_count") or 0),
            "error_count": int(manifest.get("error_count") or 0),
            "storage_type": storage_type,
            "snapshot_sha256": manifest.get("snapshot_sha256"),
            "failure": manifest.get("failure"),
            "main_flow_tasks": main_flow_task_list(),
        }

    def snapshot(self, workspace_id: str) -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        if workspace.is_dir():
            snapshot_path = workspace / "snapshot.json"
            manifest_path = workspace / "manifest.json"
            manifest = read_json_file(manifest_path)
            # Older or already-running archives predate the canonical checklist. Keep the
            # worktree immutable and normalize only the read-only API projection.
            manifest["main_flow_tasks"] = main_flow_task_list()
            if snapshot_path.is_file():
                snapshot = read_json_file(snapshot_path)
            else:
                snapshot = {
                    "schema_version": "1.0",
                    "run": manifest,
                    "view": None,
                    "verification": None,
                    "analysis_files": [],
                    "dom_checkpoints": [],
                }
            snapshot["run"] = manifest
            snapshot["recent_events"] = self.events(workspace_id)[-120:]
            return snapshot

        legacy = self.root / f"{workspace_id}.html"
        if not legacy.is_file():
            raise FileNotFoundError(workspace_id)
        view = _legacy_payload(legacy)
        modified = datetime.fromtimestamp(
            legacy.stat().st_mtime, timezone.utc
        ).isoformat(timespec="seconds")
        scenarios = [
            str(sheet.get("scenario") or "")
            for sheet in view.get("sheets") or []
            if isinstance(sheet, dict) and sheet.get("scenario")
        ]
        return {
            "schema_version": "1.0",
            "run": {
                "workspace_id": workspace_id,
                "run_id": workspace_id,
                "business_date": workspace_id[:8],
                "producer_model": "legacy",
                "status": "completed",
                "created_at": modified,
                "updated_at": modified,
                "completed_at": modified,
                "scenarios": scenarios,
                "scenario_count": len(scenarios),
                "error_count": _error_count(view),
                "storage_type": "legacy_html",
                "failure": None,
                "main_flow_tasks": main_flow_task_list(),
            },
            "view": view,
            "verification": {"legacy_import": True},
            "analysis_files": [],
            "dom_checkpoints": [],
            "recent_events": [],
        }

    def events(self, workspace_id: str) -> list[dict[str, Any]]:
        workspace = self._workspace(workspace_id)
        path = workspace / "logs" / "events.jsonl"
        if not path.is_file():
            return []
        events: list[dict[str, Any]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                events.append(event)
        return events

    def run_log(self, workspace_id: str) -> str:
        workspace = self._workspace(workspace_id)
        path = workspace / "logs" / "run.log"
        if not path.is_file():
            return "该历史结果没有持久化运行日志。\n"
        return path.read_text(encoding="utf-8")

    def analysis_file(self, workspace_id: str, relative_path: str) -> Path:
        snapshot = self.snapshot(workspace_id)
        allowed = {
            str(item.get("path") or "")
            for item in snapshot.get("analysis_files") or []
            if isinstance(item, dict)
        }
        if relative_path not in allowed:
            raise FileNotFoundError(relative_path)
        workspace = self._workspace(workspace_id)
        target = (workspace / relative_path).resolve()
        if workspace not in target.parents or not target.is_file():
            raise FileNotFoundError(relative_path)
        return target

    def checkpoint(self, workspace_id: str, checkpoint_id: str) -> Path:
        if re.fullmatch(r"\d{3}-[a-z0-9_-]+", checkpoint_id) is None:
            raise ValueError("断点 ID 不安全")
        workspace = self._workspace(workspace_id)
        target = (workspace / "dom" / "checkpoints" / f"{checkpoint_id}.json").resolve()
        if workspace not in target.parents or not target.is_file():
            raise FileNotFoundError(checkpoint_id)
        return target


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        catalog: WorkbenchCatalog,
    ) -> None:
        super().__init__(address, handler)
        self.catalog = catalog
        self.started_at = _utc_now()


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer

    def log_message(self, format: str, *args: Any) -> None:
        try:
            sys.stderr.write(
                f"[{self.log_date_time_string()}] {self.client_address[0]} "
                + (format % args)
                + "\n"
            )
            sys.stderr.flush()
        except Exception:
            # A detached Windows background process may not retain a console.
            # Access logging must never prevent the HTTP response itself.
            return

    def _headers(
        self,
        status: int,
        content_type: str,
        length: int | None = None,
        *,
        etag: str | None = None,
        cache: str = "no-store",
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        if etag:
            self.send_header("ETag", f'"{etag}"')
        if length is not None:
            self.send_header("Content-Length", str(length))
        self.end_headers()

    def _bytes(
        self,
        value: bytes,
        *,
        content_type: str,
        status: int = HTTPStatus.OK,
        cache: str = "no-store",
    ) -> None:
        self._headers(status, content_type, len(value), cache=cache)
        if self.command != "HEAD":
            self.wfile.write(value)

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_API_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "响应数据过大")
            return
        etag = hashlib.sha256(encoded).hexdigest()
        if self.headers.get("If-None-Match", "").strip('"') == etag:
            self._headers(HTTPStatus.NOT_MODIFIED, "application/json; charset=utf-8", 0)
            return
        self._headers(
            status,
            "application/json; charset=utf-8",
            len(encoded),
            etag=etag,
        )
        if self.command != "HEAD":
            self.wfile.write(encoded)

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": int(status)}, status)

    def _serve_html(self, query: dict[str, list[str]]) -> None:
        if not ROOT_HTML.is_file():
            self._error(HTTPStatus.NOT_FOUND, "固定工作台 HTML 不存在")
            return
        html = ROOT_HTML.read_text(encoding="utf-8")
        requested = str((query.get("run") or [""])[0]).strip()
        context: dict[str, Any] = {
            "api_version": API_VERSION,
            "system_version": SYSTEM_VERSION,
            "main_flow_tasks": main_flow_task_list(),
            "mode": "record" if requested else "system",
            "selected_run": None,
            "run": None,
            "view_available": False,
            "analysis_files": [],
            "dom_checkpoints": [],
            "recent_events": [],
        }
        if requested:
            workspace_id = _safe_workspace_id(requested)
            snapshot = self.server.catalog.snapshot(workspace_id)
            view = snapshot.get("view")
            if isinstance(view, dict) and isinstance(view.get("sheets"), list):
                html = _replace_audit_payload(html, view)
                context["view_available"] = True
            context.update(
                {
                    "selected_run": workspace_id,
                    "run": snapshot.get("run"),
                    "analysis_files": snapshot.get("analysis_files") or [],
                    "dom_checkpoints": snapshot.get("dom_checkpoints") or [],
                    "recent_events": snapshot.get("recent_events") or [],
                    "verification": snapshot.get("verification"),
                }
            )
        html = _inject_workbench_context(html, context)
        value = html.encode("utf-8")
        self._bytes(
            value,
            content_type="text/html; charset=utf-8",
            cache="no-cache, must-revalidate",
        )

    def _config(self) -> dict[str, Any]:
        port = int(self.server.server_address[1])
        addresses = _local_ipv4_addresses()
        return {
            "api_version": API_VERSION,
            "system_version": SYSTEM_VERSION,
            "main_flow_tasks": main_flow_task_list(),
            "refresh_policy": {
                "mode": "stage_boundary",
                "visible_update_rule": "workspace_status_or_stage_index_change",
                "running_probe_interval_ms": 2500,
                "idle_probe_interval_ms": 15000,
            },
            "service_started_at": self.server.started_at,
            "html": ROOT_HTML.name,
            "port": port,
            "listen_host": str(self.server.server_address[0]),
            "local_url": f"http://127.0.0.1:{port}/",
            "lan_urls": [
                f"http://{address}:{port}/"
                for address in addresses
                if address != "127.0.0.1"
            ],
            "worktrees": self.server.catalog.root.name,
            "read_only": True,
        }

    def _serve_events(self, workspace_id: str, query: dict[str, list[str]]) -> None:
        try:
            after = int((query.get("after") or ["0"])[0])
        except ValueError:
            after = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        deadline = time.monotonic() + 25
        sent = after
        try:
            while time.monotonic() < deadline:
                events = [
                    event
                    for event in self.server.catalog.events(workspace_id)
                    if int(event.get("seq") or 0) > sent
                ]
                for event in events:
                    seq = int(event.get("seq") or 0)
                    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
                    self.wfile.write(f"id: {seq}\ndata: {data}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    sent = max(sent, seq)
                if events:
                    snapshot = self.server.catalog.snapshot(workspace_id)
                    if str((snapshot.get("run") or {}).get("status")) in {
                        "completed",
                        "failed",
                    }:
                        return
                time.sleep(0.6)
            self.wfile.write(b": keepalive\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, FileNotFoundError):
            return

    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        route = parsed.path.rstrip("/") or "/"
        query = parse_qs(parsed.query)
        try:
            if route in {"/", f"/{ROOT_HTML.name}"}:
                self._serve_html(query)
                return
            if route == "/api/config":
                self._json(self._config())
                return
            if route == "/api/runs":
                runs = self.server.catalog.list_runs()
                self._json({"runs": runs, "count": len(runs)})
                return

            match = re.fullmatch(r"/api/runs/([^/]+)(?:/(.*))?", route)
            if not match:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            workspace_id = _safe_workspace_id(match.group(1))
            tail = match.group(2) or ""
            if not tail or tail == "snapshot":
                self._json(self.server.catalog.snapshot(workspace_id))
                return
            if tail == "log":
                value = self.server.catalog.run_log(workspace_id).encode("utf-8")
                self._bytes(value, content_type="text/plain; charset=utf-8")
                return
            if tail == "events":
                self._serve_events(workspace_id, query)
                return
            if tail == "analysis":
                relative = str((query.get("path") or [""])[0])
                target = self.server.catalog.analysis_file(workspace_id, relative)
                content_type = mimetypes.guess_type(target.name)[0] or "application/json"
                self._bytes(
                    target.read_bytes(),
                    content_type=f"{content_type}; charset=utf-8",
                )
                return
            checkpoint_match = re.fullmatch(r"checkpoints/([^/]+)", tail)
            if checkpoint_match:
                target = self.server.catalog.checkpoint(
                    workspace_id, checkpoint_match.group(1)
                )
                self._bytes(
                    target.read_bytes(),
                    content_type="application/json; charset=utf-8",
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "运行资源不存在")
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "运行或资源不存在")
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"读取失败：{exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动线下活动核销固定局域网工作台")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8080")
    parser.add_argument(
        "--worktrees",
        default=str(DEFAULT_WORKTREES_ROOT),
        help="持久化运行目录",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (1 <= args.port <= 65535):
        print("端口必须在 1 到 65535 之间", file=sys.stderr)
        return 2
    catalog = WorkbenchCatalog(Path(args.worktrees))
    server = WorkbenchHTTPServer(
        (args.host, args.port),
        Handler,
        catalog=catalog,
    )
    config = {
        "local": f"http://127.0.0.1:{args.port}/",
        "lan": [
            f"http://{value}:{args.port}/"
            for value in _local_ipv4_addresses()
            if value != "127.0.0.1"
        ],
        "html": str(ROOT_HTML),
        "worktrees": str(catalog.root),
    }
    print(json.dumps(config, ensure_ascii=False, indent=2), flush=True)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
