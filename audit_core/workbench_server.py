from __future__ import annotations

import argparse
import contextlib
import functools
import gzip
import hashlib
import json
import mimetypes
import os
import re
import secrets
import socket
import stat
import sys
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from .html_report import DATA_CLOSE, DATA_OPEN
from .error_reason import attach_error_reasons
from .pass_check_log import attach_pass_check_log, load_workspace_results
from .oss_intake import (
    CALLBACK_PATH,
    DEFAULT_CALLBACK_ATTEMPTS,
    DEFAULT_CALLBACK_TIMEOUT_SECONDS,
    DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
    DEFAULT_MAX_DOWNLOAD_BYTES,
    DEFAULT_OSS_INPUT_ROOT,
    MAX_REQUEST_BYTES,
    OSSIntakeConfig,
    OSSIntakeConflictError,
    OSSIntakeRequestError,
    OSSIntakeService,
    OSSIntakeUnavailableError,
    public_job,
)
from .workbench_runtime import ROOT_HTML
from .common import AuditError
from .workbench_delete import RunDeletionConflict, prepare_run_deletion
from .workbench_labels import record_labels
from .workbench_html import (
    API_VERSION,
    SYSTEM_VERSION,
    WORKBENCH_CONTEXT_ID,
    customer_projection_error_count,
    inject_workbench_context as _inject_workbench_context,
    replace_audit_payload as _replace_audit_payload,
)
from .workbench_store import (
    DEFAULT_WORKTREES_ROOT,
    WORKSPACE_ID_PATTERN,
    atomic_write_json,
    main_flow_task_list,
    project_run_status,
    read_json_file,
)


DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8080
MAX_API_BYTES = 24 * 1024 * 1024
MAX_CACHED_GZIP_BYTES = 2 * 1024 * 1024
MAX_CACHED_HTML_DOCUMENTS = 8
MAX_CACHED_MANIFESTS = 64
MAX_CACHED_SNAPSHOT_DOCUMENTS = 16
MAX_CACHED_SNAPSHOT_BYTES = 24 * 1024 * 1024
MAX_CACHED_RESOURCE_DOCUMENTS = 64
MAX_CACHED_RESOURCE_BYTES = 16 * 1024 * 1024
CATALOG_MAIN_FLOW_TASKS = tuple(main_flow_task_list())
_REVIEW_METADATA_UNSET = object()
SYSTEM_AUDIT_PAYLOAD = {
    "schema_version": "1.1",
    "title": "线下活动核销结果",
    "sheets": [],
}


@functools.lru_cache(maxsize=4)
def _read_root_html_template(
    path: str,
    modified_ns: int,
    changed_ns: int,
    size: int,
) -> str:
    """Reuse the fixed shell until its on-disk signature changes."""
    del modified_ns, changed_ns, size
    return Path(path).read_text(encoding="utf-8")


def _read_event_records(path: Path) -> list[dict[str, Any]]:
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


def _completed_projection_signature(
    workspace: Path,
) -> tuple[tuple[str, int, int, int], ...]:
    paths = [workspace / "snapshot.json", workspace / "logs" / "events.jsonl", workspace / "analysis" / "input-cases.json"]
    for directory in ("results", "evidence"):
        result_root = workspace / "analysis" / directory
        if result_root.is_dir():
            paths.extend(sorted(result_root.glob("*.json")))
    signature: list[tuple[str, int, int, int]] = []
    for path in paths:
        if not path.is_file():
            continue
        stat = path.stat()
        signature.append(
            (
                path.relative_to(workspace).as_posix(),
                stat.st_mtime_ns,
                stat.st_ctime_ns,
                stat.st_size,
            )
        )
    return tuple(signature)


@functools.lru_cache(maxsize=16)
def _read_completed_snapshot_projection(
    workspace_path: str,
    signature: tuple[tuple[str, int, int, int], ...],
) -> dict[str, Any]:
    """Build an immutable completed-run projection once per file signature."""
    del signature
    workspace = Path(workspace_path)
    snapshot = read_json_file(workspace / "snapshot.json")
    snapshot["view"] = attach_pass_check_log(
        snapshot.get("view"),
        load_workspace_results(workspace),
    )
    snapshot["view"] = attach_error_reasons(snapshot["view"])
    snapshot["recent_events"] = _read_event_records(
        workspace / "logs" / "events.jsonl"
    )[-120:]
    return snapshot


@functools.lru_cache(maxsize=32)
def _read_analysis_allowlist(
    snapshot_path: str,
    modified_ns: int,
    changed_ns: int,
    size: int,
) -> frozenset[str]:
    """Cache only approved analysis paths; never build the business projection."""
    del modified_ns, changed_ns, size
    snapshot = read_json_file(Path(snapshot_path))
    return frozenset(
        str(item.get("path") or "")
        for item in snapshot.get("analysis_files") or []
        if isinstance(item, dict)
    )


@functools.lru_cache(maxsize=16)
def _gzip_payload(value: bytes) -> bytes:
    return gzip.compress(value, compresslevel=6, mtime=0)


def _accepts_gzip(value: str) -> bool:
    wildcard_quality = 0.0
    for entry in value.split(","):
        parts = [part.strip() for part in entry.split(";")]
        encoding = parts[0].lower()
        quality = 1.0
        for parameter in parts[1:]:
            if parameter.lower().startswith("q="):
                try:
                    quality = float(parameter[2:])
                except ValueError:
                    quality = 0.0
        if encoding == "gzip":
            return quality > 0
        if encoding == "*":
            wildcard_quality = quality
    return wildcard_quality > 0


def _etag_matches(value: str, current: str) -> bool:
    """Apply If-None-Match weak comparison to one negotiated representation."""

    for candidate in value.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate[:2].lower() == "w/":
            candidate = candidate[2:].strip()
        if len(candidate) >= 2 and candidate[0] == candidate[-1] == '"':
            candidate = candidate[1:-1]
        if candidate == current:
            return True
    return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_workspace_id(value: str) -> str:
    decoded = unquote(value).strip()
    if WORKSPACE_ID_PATTERN.fullmatch(decoded) is None:
        raise ValueError("运行 ID 不安全")
    return decoded


def _legacy_payload_from_html(html: str) -> dict[str, Any]:
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


def _legacy_payload(path: Path) -> dict[str, Any]:
    return _legacy_payload_from_html(path.read_text(encoding="utf-8"))


def _error_count(view: dict[str, Any] | None) -> int:
    if not view:
        return 0
    return sum(
        int((sheet.get("audit_counts") or {}).get("error_count") or 0)
        for sheet in view.get("sheets") or []
        if isinstance(sheet, dict)
    )


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
        self.review_root = self.root / ".reviews"
        self.review_root.mkdir(parents=True, exist_ok=True)
        self._review_lock = threading.RLock()
        self._cache_lock = threading.RLock()
        self._catalog_lock = threading.RLock()
        self._catalog_batch_condition = threading.Condition()
        self._catalog_active_generation: int | None = None
        self._catalog_completed_generation = 0
        self._catalog_generation_outcomes: dict[
            int,
            tuple[bool, object],
        ] = {}
        self._catalog_generation_participants: dict[int, int] = {}
        self._manifest_cache: OrderedDict[
            str,
            tuple[tuple[int, int, int], dict[str, Any]],
        ] = OrderedDict()
        self._review_cache: dict[
            str,
            tuple[tuple[int, int, int], dict[str, Any]],
        ] = {}
        self._legacy_cache: dict[
            str,
            tuple[tuple[int, int, int], dict[str, Any]],
        ] = {}
        self._run_summary_cache: dict[
            tuple[str, str],
            tuple[tuple[object, ...], dict[str, Any]],
        ] = {}
        self._catalog_document_cache: tuple[
            tuple[tuple[object, ...], ...],
            tuple[dict[str, Any], ...],
            bytes,
            str,
        ] | None = None
        self._snapshot_document_lock = threading.RLock()
        self._snapshot_document_cache: OrderedDict[
            str,
            tuple[tuple[object, ...], bytes, str],
        ] = OrderedDict()
        self._snapshot_document_cache_bytes = 0
        self._resource_document_lock = threading.RLock()
        self._resource_document_cache: OrderedDict[
            tuple[str, str],
            tuple[tuple[int, int, int] | None, bytes, str],
        ] = OrderedDict()
        self._resource_document_cache_bytes = 0

    @staticmethod
    def _stat_signature(value: os.stat_result) -> tuple[int, int, int]:
        return (value.st_mtime_ns, value.st_ctime_ns, value.st_size)

    def _manifest(
        self,
        workspace_id: str,
        path: Path,
        metadata: os.stat_result | None = None,
    ) -> dict[str, Any]:
        signature = self._stat_signature(metadata or path.stat())
        with self._cache_lock:
            cached = self._manifest_cache.get(workspace_id)
            if cached is not None and cached[0] == signature:
                self._manifest_cache.move_to_end(workspace_id)
                return cached[1]
        value = read_json_file(path)
        if MAX_CACHED_MANIFESTS > 0:
            with self._cache_lock:
                self._manifest_cache[workspace_id] = (signature, value)
                self._manifest_cache.move_to_end(workspace_id)
                while len(self._manifest_cache) > MAX_CACHED_MANIFESTS:
                    self._manifest_cache.popitem(last=False)
        return value

    def _legacy_summary(
        self,
        path: Path,
        metadata: os.stat_result,
    ) -> dict[str, Any]:
        signature = self._stat_signature(metadata)
        with self._cache_lock:
            cached = self._legacy_cache.get(path.name)
            if cached is not None and cached[0] == signature:
                result = dict(cached[1])
                result["scenarios"] = list(result.get("scenarios") or [])
                return result

        raw = path.read_bytes()
        payload = _legacy_payload_from_html(raw.decode("utf-8"))
        modified = datetime.fromtimestamp(
            metadata.st_mtime,
            timezone.utc,
        ).isoformat(timespec="seconds")
        scenarios = [
            str(sheet.get("scenario") or "")
            for sheet in payload.get("sheets") or []
            if isinstance(sheet, dict) and sheet.get("scenario")
        ]
        result = {
            "workspace_id": path.stem,
            "run_id": path.stem,
            "business_date": path.stem[:8],
            "producer_model": "legacy",
            "audit_model": "legacy",
            "reasoning_effort": None,
            "status": "completed",
            "created_at": modified,
            "updated_at": modified,
            "completed_at": modified,
            "scenarios": scenarios,
            "scenario_count": len(scenarios),
            "error_count": _error_count(payload),
            "storage_type": "legacy_html",
            "snapshot_sha256": hashlib.sha256(raw).hexdigest(),
        }
        with self._cache_lock:
            self._legacy_cache[path.name] = (signature, result)
        copy = dict(result)
        copy["scenarios"] = list(scenarios)
        return copy

    def _workspace(self, workspace_id: str) -> Path:
        safe_id = _safe_workspace_id(workspace_id)
        candidate = (self.root / safe_id).resolve()
        if candidate.parent != self.root:
            raise ValueError("运行路径越出 worktrees")
        return candidate

    def _review_path(self, workspace_id: str) -> Path:
        safe_id = _safe_workspace_id(workspace_id)
        # The validated ID cannot contain a separator or parent segment. Avoid
        # resolving this path for every row in the frequently-polled catalog.
        candidate = self.review_root / f"{safe_id}.json"
        if candidate.parent != self.review_root:
            raise ValueError("人工核验状态路径越出 worktrees/.reviews")
        return candidate

    def _review_state(
        self,
        workspace_id: str,
        *,
        metadata: os.stat_result | None | object = _REVIEW_METADATA_UNSET,
    ) -> tuple[dict[str, Any], tuple[int, int, int]]:
        _safe_workspace_id(workspace_id)
        path: Path | None = None
        if metadata is _REVIEW_METADATA_UNSET:
            path = self._review_path(workspace_id)
            try:
                metadata = path.stat()
            except FileNotFoundError:
                metadata = None
        if metadata is None:
            with self._cache_lock:
                self._review_cache.pop(workspace_id, None)
            return {
                "manual_reviewed": False,
                "manual_reviewed_at": None,
            }, (0, 0, 0)
        assert isinstance(metadata, os.stat_result)
        signature = self._stat_signature(metadata)
        with self._cache_lock:
            cached = self._review_cache.get(workspace_id)
            if cached is not None and cached[0] == signature:
                return dict(cached[1]), signature
        if path is None:
            path = self._review_path(workspace_id)
        value = read_json_file(path)
        result = {
            "manual_reviewed": bool(value.get("reviewed")),
            "manual_reviewed_at": value.get("reviewed_at")
            if value.get("reviewed")
            else None,
        }
        with self._cache_lock:
            self._review_cache[workspace_id] = (signature, result)
        return dict(result), signature

    def review_state(self, workspace_id: str) -> dict[str, Any]:
        return self._review_state(workspace_id)[0]

    def _catalog_review_metadata(self) -> dict[str, os.stat_result] | None:
        """Enumerate review markers once; return None to use safe point lookups."""

        metadata_by_workspace: dict[str, os.stat_result] = {}
        try:
            with os.scandir(self.review_root) as entries:
                for entry in entries:
                    if entry.name.startswith(".") or not entry.name.endswith(".json"):
                        continue
                    workspace_id = entry.name[:-5]
                    if WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None:
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        return None
                    metadata = entry.stat(follow_symlinks=False)
                    if not stat.S_ISREG(metadata.st_mode):
                        return None
                    metadata_by_workspace[workspace_id] = metadata
        except OSError:
            return None
        return metadata_by_workspace

    def html_signature(self, workspace_id: str) -> tuple[object, ...]:
        """Return the filesystem state that can change one rendered record."""

        workspace = self._workspace(workspace_id)
        review_path = self._review_path(workspace_id)

        def token(label: str, path: Path) -> tuple[object, ...]:
            try:
                metadata = path.stat()
            except FileNotFoundError:
                return (label, 0, 0, 0)
            return (label, *self._stat_signature(metadata))

        if workspace.is_dir():
            paths = [
                ("manifest.json", workspace / "manifest.json"),
                ("snapshot.json", workspace / "snapshot.json"),
                ("logs/events.jsonl", workspace / "logs" / "events.jsonl"),
                ("analysis/input-cases.json", workspace / "analysis" / "input-cases.json"),
            ]
            for directory in ("results", "evidence"):
                result_root = workspace / "analysis" / directory
                if result_root.is_dir():
                    paths.extend(
                        (path.relative_to(workspace).as_posix(), path)
                        for path in sorted(result_root.glob("*.json"))
                    )
            return (
                "worktree",
                *(token(label, path) for label, path in paths),
                token(".review", review_path),
            )

        legacy = self.root / f"{workspace_id}.html"
        if not legacy.is_file():
            raise FileNotFoundError(workspace_id)
        return (
            "legacy_html",
            token(legacy.name, legacy),
            token(".review", review_path),
        )

    def set_manual_review(self, workspace_id: str, reviewed: bool) -> dict[str, Any]:
        with self._review_lock:
            return self._set_manual_review_locked(workspace_id, reviewed)

    def _set_manual_review_locked(self, workspace_id: str, reviewed: bool) -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        legacy = self.root / f"{workspace_id}.html"
        if not workspace.is_dir() and not legacy.is_file():
            raise FileNotFoundError(workspace_id)
        if workspace.is_dir():
            manifest = read_json_file(workspace / "manifest.json")
            if project_run_status(manifest).get("status") != "completed":
                raise ValueError("只有已完成的核销记录可以标记人工核验")
        now = _utc_now()
        payload = {
            "schema_version": "1.0",
            "workspace_id": workspace_id,
            "reviewed": reviewed,
            "reviewed_at": now if reviewed else None,
            "updated_at": now,
        }
        with self._review_lock:
            atomic_write_json(self._review_path(workspace_id), payload)
        with self._cache_lock:
            self._review_cache.pop(workspace_id, None)
            self._catalog_document_cache = None
        return {
            "manual_reviewed": reviewed,
            "manual_reviewed_at": payload["reviewed_at"],
        }

    def delete_run(self, workspace_id: str, *, input_root: Path) -> dict[str, Any]:
        # Match the existing read lock order. Review mutations cannot recreate a
        # marker after deletion, and readers cannot refill a removed run's cache.
        with (
            self._review_lock,
            self._snapshot_document_lock,
            self._resource_document_lock,
            self._catalog_lock,
            self._cache_lock,
        ):
            plan = prepare_run_deletion(self.root, workspace_id, input_root=input_root)
            try:
                return plan.execute()
            finally:
                self._manifest_cache.clear()
                self._review_cache.clear()
                self._legacy_cache.clear()
                self._run_summary_cache.clear()
                self._catalog_document_cache = None
                self._snapshot_document_cache.clear()
                self._snapshot_document_cache_bytes = 0
                self._resource_document_cache.clear()
                self._resource_document_cache_bytes = 0
                _read_completed_snapshot_projection.cache_clear()
                _read_analysis_allowlist.cache_clear()
                _gzip_payload.cache_clear()

    @staticmethod
    def _encode_catalog(runs: tuple[dict[str, Any], ...]) -> bytes:
        return json.dumps(
            {"runs": runs, "count": len(runs)},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _catalog_state(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], bytes, str]:
        """Cover each read with a validation generation that starts after arrival."""

        condition = self._catalog_batch_condition
        leader = False
        outcome: tuple[bool, object]
        with condition:
            if self._catalog_active_generation is None:
                generation = self._catalog_completed_generation + 1
                self._catalog_active_generation = generation
                leader = True
            else:
                # A request arriving after the active scan began cannot safely
                # reuse it. Queue it for the next scan; all such requests share
                # that later generation once the current leader completes.
                generation = self._catalog_active_generation + 1
            self._catalog_generation_participants[generation] = (
                self._catalog_generation_participants.get(generation, 0) + 1
            )
            while not leader:
                if self._catalog_completed_generation >= generation:
                    outcome = self._catalog_generation_outcomes[generation]
                    break
                if (
                    self._catalog_active_generation is None
                    and generation == self._catalog_completed_generation + 1
                ):
                    self._catalog_active_generation = generation
                    leader = True
                    break
                condition.wait()

        try:
            if leader:
                try:
                    result = self._catalog_state_once()
                    outcome = (True, result)
                except BaseException as error:
                    outcome = (False, error)
                with condition:
                    self._catalog_generation_outcomes[generation] = outcome
                    self._catalog_completed_generation = generation
                    self._catalog_active_generation = None
                    condition.notify_all()

            success, payload = outcome
            if success:
                return payload  # type: ignore[return-value]
            raise payload  # type: ignore[misc]
        finally:
            with condition:
                participants = self._catalog_generation_participants[generation] - 1
                if participants:
                    self._catalog_generation_participants[generation] = participants
                else:
                    self._catalog_generation_participants.pop(generation, None)
                    self._catalog_generation_outcomes.pop(generation, None)

    def _catalog_state_once(
        self,
    ) -> tuple[tuple[dict[str, Any], ...], bytes, str]:
        """Check filesystem freshness every time and rebuild catalog JSON on change."""

        with self._catalog_lock:
            entries_with_signatures: list[
                tuple[dict[str, Any], tuple[object, ...]]
            ] = []
            live_workspaces: set[str] = set()
            live_legacy_files: set[str] = set()
            live_review_ids: set[str] = set()
            cacheable = True
            review_metadata = self._catalog_review_metadata()

            def load_review(
                workspace_id: str,
            ) -> tuple[dict[str, Any], tuple[int, int, int]]:
                safe_id = _safe_workspace_id(workspace_id)
                if review_metadata is None:
                    return self._review_state(safe_id)
                return self._review_state(
                    safe_id,
                    metadata=review_metadata.get(safe_id),
                )

            with os.scandir(self.root) as entries:
                for entry in entries:
                    if entry.name.startswith("."):
                        continue
                    path = Path(entry.path)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            manifest_path = path / "manifest.json"
                            metadata = os.stat(
                                manifest_path,
                                follow_symlinks=False,
                            )
                            if not stat.S_ISREG(metadata.st_mode):
                                continue
                            live_workspaces.add(entry.name)
                            manifest_signature = self._stat_signature(metadata)
                            cache_key = ("worktree", entry.name)
                            with self._cache_lock:
                                cached = self._run_summary_cache.get(cache_key)
                            manifest_unchanged = (
                                cached is not None
                                and len(cached[0]) == 5
                                and cached[0][0] == "worktree"
                                and cached[0][1] == entry.name
                                and cached[0][2] == manifest_signature
                            )
                            if manifest_unchanged:
                                workspace_id = str(cached[0][3])
                            else:
                                manifest = self._manifest(
                                    entry.name,
                                    manifest_path,
                                    metadata,
                                )
                                workspace_id = str(
                                    manifest.get("workspace_id") or entry.name
                                )
                            review, review_signature = load_review(workspace_id)
                            live_review_ids.add(_safe_workspace_id(workspace_id))
                            item_signature: tuple[object, ...] = (
                                "worktree",
                                entry.name,
                                manifest_signature,
                                workspace_id,
                                review_signature,
                            )
                            if cached is not None and cached[0] == item_signature:
                                run = cached[1]
                            elif manifest_unchanged:
                                run = dict(cached[1])
                                run.update(review)
                                with self._cache_lock:
                                    self._run_summary_cache[cache_key] = (
                                        item_signature,
                                        run,
                                    )
                            else:
                                run = self._summary(
                                    manifest,
                                    storage_type="worktree",
                                    workspace=path,
                                )
                                run.update(review)
                                with self._cache_lock:
                                    self._run_summary_cache[cache_key] = (
                                        item_signature,
                                        run,
                                    )
                            entries_with_signatures.append((run, item_signature))
                        elif (
                            entry.is_file(follow_symlinks=False)
                            and path.suffix.lower() == ".html"
                        ):
                            metadata = entry.stat(follow_symlinks=False)
                            live_legacy_files.add(path.name)
                            legacy_signature = self._stat_signature(metadata)
                            review, review_signature = load_review(path.stem)
                            live_review_ids.add(path.stem)
                            item_signature = (
                                "legacy_html",
                                path.name,
                                legacy_signature,
                                review_signature,
                            )
                            cache_key = ("legacy_html", path.name)
                            with self._cache_lock:
                                cached = self._run_summary_cache.get(cache_key)
                            legacy_unchanged = (
                                cached is not None
                                and len(cached[0]) == 4
                                and cached[0][0] == "legacy_html"
                                and cached[0][1] == path.name
                                and cached[0][2] == legacy_signature
                            )
                            if cached is not None and cached[0] == item_signature:
                                run = cached[1]
                            elif legacy_unchanged:
                                run = dict(cached[1])
                                run.update(review)
                                with self._cache_lock:
                                    self._run_summary_cache[cache_key] = (
                                        item_signature,
                                        run,
                                    )
                            else:
                                run = self._legacy_summary(path, metadata)
                                run.update(review)
                                with self._cache_lock:
                                    self._run_summary_cache[cache_key] = (
                                        item_signature,
                                        run,
                                    )
                            entries_with_signatures.append((run, item_signature))
                    except (OSError, ValueError, json.JSONDecodeError):
                        # A transiently unreadable entry must be retried next time,
                        # rather than freezing an incomplete directory projection.
                        cacheable = False

            entries_with_signatures.sort(
                key=lambda item: (
                    str(item[0].get("analysis_started_at") or item[0].get("created_at") or ""),
                    str(item[0].get("created_at") or ""),
                    str(item[0].get("workspace_id") or ""),
                ),
                reverse=True,
            )
            catalog_signature = tuple(
                item_signature
                for _, item_signature in entries_with_signatures
            )
            with self._cache_lock:
                self._manifest_cache = OrderedDict(
                    (workspace_id, cached)
                    for workspace_id, cached in self._manifest_cache.items()
                    if workspace_id in live_workspaces
                )
                self._legacy_cache = {
                    filename: cached
                    for filename, cached in self._legacy_cache.items()
                    if filename in live_legacy_files
                }
                live_summary_keys = {
                    ("worktree", name) for name in live_workspaces
                }
                live_summary_keys.update(
                    ("legacy_html", name) for name in live_legacy_files
                )
                self._run_summary_cache = {
                    key: cached
                    for key, cached in self._run_summary_cache.items()
                    if key in live_summary_keys
                }
                self._review_cache = {
                    workspace_id: cached
                    for workspace_id, cached in self._review_cache.items()
                    if workspace_id in live_review_ids
                }
                cached_document = self._catalog_document_cache
                if (
                    cacheable
                    and cached_document is not None
                    and cached_document[0] == catalog_signature
                ):
                    return (
                        cached_document[1],
                        cached_document[2],
                        cached_document[3],
                    )

            runs = tuple(run for run, _ in entries_with_signatures)
            encoded = self._encode_catalog(runs)
            etag = hashlib.sha256(encoded).hexdigest()
            if cacheable and len(encoded) <= MAX_API_BYTES:
                with self._cache_lock:
                    self._catalog_document_cache = (
                        catalog_signature,
                        runs,
                        encoded,
                        etag,
                    )
            return runs, encoded, etag

    def catalog_document(self) -> tuple[bytes, str]:
        _, encoded, etag = self._catalog_state()
        return encoded, etag

    @staticmethod
    def _encode_snapshot(snapshot: dict[str, Any]) -> bytes:
        return json.dumps(
            snapshot,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def snapshot_document(self, workspace_id: str) -> tuple[bytes, str]:
        """Reuse one immutable JSON representation until any source changes."""

        # Keep the signature check, snapshot assembly, and serialization under one
        # lock so a request burst cannot repeat the expensive JSON build. Source
        # signatures are still inspected on every call, including completed runs.
        with self._snapshot_document_lock:
            signature = self.html_signature(workspace_id)
            cached = self._snapshot_document_cache.get(workspace_id)
            if cached is not None and cached[0] == signature:
                self._snapshot_document_cache.move_to_end(workspace_id)
                return cached[1], cached[2]
            if cached is not None:
                self._snapshot_document_cache.pop(workspace_id)
                self._snapshot_document_cache_bytes -= len(cached[1])

            encoded = self._encode_snapshot(self.snapshot(workspace_id))
            etag = hashlib.sha256(encoded).hexdigest()
            if (
                MAX_CACHED_SNAPSHOT_DOCUMENTS > 0
                and len(encoded) <= MAX_API_BYTES
                and len(encoded) <= MAX_CACHED_SNAPSHOT_BYTES
            ):
                self._snapshot_document_cache[workspace_id] = (
                    signature,
                    encoded,
                    etag,
                )
                self._snapshot_document_cache_bytes += len(encoded)
                self._snapshot_document_cache.move_to_end(workspace_id)
                while (
                    len(self._snapshot_document_cache)
                    > MAX_CACHED_SNAPSHOT_DOCUMENTS
                    or self._snapshot_document_cache_bytes
                    > MAX_CACHED_SNAPSHOT_BYTES
                ):
                    _, evicted = self._snapshot_document_cache.popitem(last=False)
                    self._snapshot_document_cache_bytes -= len(evicted[1])
            return encoded, etag

    def list_runs(self) -> list[dict[str, Any]]:
        runs, _, _ = self._catalog_state()
        copies: list[dict[str, Any]] = []
        for run in runs:
            item = dict(run)
            item["scenarios"] = list(item.get("scenarios") or [])
            item["main_flow_tasks"] = [
                dict(task) for task in item.get("main_flow_tasks") or []
            ]
            copies.append(item)
        return copies

    @staticmethod
    def _summary(manifest: dict[str, Any], *, storage_type: str, workspace: Path | None = None) -> dict[str, Any]:
        manifest = project_run_status(manifest)
        return {
            "workspace_id": manifest.get("workspace_id"),
            "run_id": manifest.get("run_id"),
            "business_date": manifest.get("business_date"),
            "producer_model": manifest.get("producer_model"),
            "audit_model": manifest.get("audit_model") or manifest.get("producer_model"),
            "reasoning_effort": manifest.get("reasoning_effort"),
            "status": manifest.get("status"),
            "created_at": manifest.get("created_at"),
            "analysis_started_at": manifest.get("analysis_started_at"),
            "updated_at": manifest.get("updated_at"),
            "completed_at": manifest.get("completed_at"),
            "scenarios": list(manifest.get("scenarios") or []),
            "scenario_count": int(manifest.get("scenario_count") or 0),
            "error_count": customer_projection_error_count(manifest),
            "storage_type": storage_type,
            "snapshot_sha256": manifest.get("snapshot_sha256"),
            "failure": manifest.get("failure"),
            "main_flow_tasks": CATALOG_MAIN_FLOW_TASKS,
            **record_labels(manifest, workspace),
        }

    def snapshot(self, workspace_id: str) -> dict[str, Any]:
        workspace = self._workspace(workspace_id)
        if workspace.is_dir():
            snapshot_path = workspace / "snapshot.json"
            manifest_path = workspace / "manifest.json"
            manifest = dict(self._manifest(workspace_id, manifest_path))
            # Older or already-running archives predate the canonical checklist. Keep the
            # worktree immutable and normalize only the read-only API projection.
            manifest["main_flow_tasks"] = main_flow_task_list()
            manifest.update(record_labels(manifest, workspace))
            manifest.update(self.review_state(workspace_id))
            used_completed_cache = False
            if snapshot_path.is_file():
                if manifest.get("status") == "completed":
                    snapshot = dict(
                        _read_completed_snapshot_projection(
                            str(workspace),
                            _completed_projection_signature(workspace),
                        )
                    )
                    used_completed_cache = True
                else:
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
            manifest = project_run_status(manifest, snapshot.get("view"))
            snapshot["run"] = manifest
            if not used_completed_cache:
                snapshot["view"] = attach_pass_check_log(
                    snapshot.get("view"),
                    load_workspace_results(workspace),
                )
                snapshot["view"] = attach_error_reasons(snapshot["view"])
                snapshot["recent_events"] = self.events(workspace_id)[-120:]
            if manifest.get("status") == "completed" and isinstance(snapshot.get("view"), dict):
                manifest["error_count"] = _error_count(snapshot["view"])
            return snapshot

        legacy = self.root / f"{workspace_id}.html"
        if not legacy.is_file():
            raise FileNotFoundError(workspace_id)
        view = attach_pass_check_log(_legacy_payload(legacy), {})
        view = attach_error_reasons(view)
        modified = datetime.fromtimestamp(
            legacy.stat().st_mtime, timezone.utc
        ).isoformat(timespec="seconds")
        scenarios = [
            str(sheet.get("scenario") or "")
            for sheet in view.get("sheets") or []
            if isinstance(sheet, dict) and sheet.get("scenario")
        ]
        review = self.review_state(workspace_id)
        return {
            "schema_version": "1.0",
            "run": {
                "workspace_id": workspace_id,
                "run_id": workspace_id,
                "business_date": workspace_id[:8],
                "producer_model": "legacy",
                "audit_model": "legacy",
                "reasoning_effort": None,
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
                **review,
            },
            "view": view,
            "verification": {"legacy_import": True},
            "analysis_files": [],
            "dom_checkpoints": [],
            "recent_events": [],
        }

    def events(self, workspace_id: str) -> list[dict[str, Any]]:
        workspace = self._workspace(workspace_id)
        return _read_event_records(workspace / "logs" / "events.jsonl")

    def _resource_signature(self, path: Path) -> tuple[int, int, int] | None:
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            return None
        return self._stat_signature(metadata)

    @staticmethod
    def _load_resource(path: Path, mode: str) -> bytes:
        if mode == "utf8_text":
            return path.read_text(encoding="utf-8").encode("utf-8")
        return path.read_bytes()

    def resource_document(
        self,
        path: Path,
        *,
        mode: str = "bytes",
        missing: bytes | None = None,
    ) -> tuple[bytes, str]:
        """Read one file representation once per signature with bounded reuse."""

        resolved = path.resolve()
        cache_key = (mode, str(resolved))
        with self._resource_document_lock:
            signature = self._resource_signature(resolved)
            cached = self._resource_document_cache.get(cache_key)
            if cached is not None and cached[0] == signature:
                self._resource_document_cache.move_to_end(cache_key)
                return cached[1], cached[2]
            if cached is not None:
                self._resource_document_cache.pop(cache_key)
                self._resource_document_cache_bytes -= len(cached[1])

            cacheable = False
            if signature is None:
                if missing is None:
                    raise FileNotFoundError(resolved)
                value = missing
                cacheable = True
            else:
                # A running log may grow while it is read. Cache only a stable
                # representation; a continuously changing read is still returned
                # with an exact ETag and will be retried on the next request.
                value = b""
                for _ in range(3):
                    before = signature
                    try:
                        value = self._load_resource(resolved, mode)
                    except FileNotFoundError:
                        signature = None
                        if missing is None:
                            raise
                        value = missing
                        cacheable = True
                        break
                    signature = self._resource_signature(resolved)
                    if signature == before:
                        cacheable = True
                        break
                    if signature is None:
                        if missing is None:
                            raise FileNotFoundError(resolved)
                        value = missing
                        cacheable = True
                        break

            etag = hashlib.sha256(value).hexdigest()
            if (
                cacheable
                and MAX_CACHED_RESOURCE_DOCUMENTS > 0
                and len(value) <= MAX_CACHED_RESOURCE_BYTES
            ):
                self._resource_document_cache[cache_key] = (
                    signature,
                    value,
                    etag,
                )
                self._resource_document_cache_bytes += len(value)
                self._resource_document_cache.move_to_end(cache_key)
                while (
                    len(self._resource_document_cache)
                    > MAX_CACHED_RESOURCE_DOCUMENTS
                    or self._resource_document_cache_bytes
                    > MAX_CACHED_RESOURCE_BYTES
                ):
                    _, evicted = self._resource_document_cache.popitem(last=False)
                    self._resource_document_cache_bytes -= len(evicted[1])
            return value, etag

    def run_log_document(self, workspace_id: str) -> tuple[bytes, str]:
        workspace = self._workspace(workspace_id)
        if not workspace.is_dir() and not (self.root / f"{workspace_id}.html").is_file():
            raise FileNotFoundError(workspace_id)
        return self.resource_document(
            workspace / "logs" / "run.log",
            mode="utf8_text",
            missing="该历史结果没有持久化运行日志。\n".encode("utf-8"),
        )

    def run_log(self, workspace_id: str) -> str:
        value, _ = self.run_log_document(workspace_id)
        return value.decode("utf-8")

    def analysis_file(self, workspace_id: str, relative_path: str) -> Path:
        workspace = self._workspace(workspace_id)
        snapshot_path = workspace / "snapshot.json"
        if not snapshot_path.is_file():
            raise FileNotFoundError(relative_path)
        snapshot_stat = snapshot_path.stat()
        allowed = _read_analysis_allowlist(
            str(snapshot_path),
            snapshot_stat.st_mtime_ns,
            snapshot_stat.st_ctime_ns,
            snapshot_stat.st_size,
        )
        if relative_path not in allowed:
            raise FileNotFoundError(relative_path)
        target = (workspace / relative_path).resolve()
        if workspace not in target.parents or not target.is_file():
            raise FileNotFoundError(relative_path)
        return target

    def analysis_document(
        self,
        workspace_id: str,
        relative_path: str,
    ) -> tuple[Path, bytes, str]:
        target = self.analysis_file(workspace_id, relative_path)
        value, etag = self.resource_document(target)
        return target, value, etag

    def checkpoint(self, workspace_id: str, checkpoint_id: str) -> Path:
        if re.fullmatch(r"\d{3}-[a-z0-9_-]+", checkpoint_id) is None:
            raise ValueError("断点 ID 不安全")
        workspace = self._workspace(workspace_id)
        target = (workspace / "dom" / "checkpoints" / f"{checkpoint_id}.json").resolve()
        if workspace not in target.parents or not target.is_file():
            raise FileNotFoundError(checkpoint_id)
        return target

    def checkpoint_document(
        self,
        workspace_id: str,
        checkpoint_id: str,
    ) -> tuple[Path, bytes, str]:
        target = self.checkpoint(workspace_id, checkpoint_id)
        value, etag = self.resource_document(target)
        return target, value, etag


class WorkbenchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    # Browsers and LAN clients can open several sockets at once. The stdlib
    # default backlog is deliberately small and rejects burst traffic before
    # worker threads have a chance to accept it.
    request_queue_size = 128
    # On Windows, SO_REUSEADDR can let two unrelated workbench processes bind
    # the same port and make requests reach the older process unpredictably.
    allow_reuse_address = os.name != "nt"

    def server_bind(self) -> None:
        if os.name == "nt" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(
                socket.SOL_SOCKET,
                socket.SO_EXCLUSIVEADDRUSE,
                1,
            )
        super().server_bind()

    def handle_error(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: tuple[str, int],
    ) -> None:
        # Closing a browser or a sleeping LAN client can reset an HTTP/1.1
        # keep-alive socket while the request thread waits for its next line.
        # This is normal transport churn, not a workbench failure; keep real
        # handler exceptions on the stdlib traceback path.
        error = sys.exc_info()[1]
        if isinstance(
            error,
            (BrokenPipeError, ConnectionAbortedError, ConnectionResetError),
        ):
            return
        super().handle_error(request, client_address)

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        catalog: WorkbenchCatalog,
        intake: OSSIntakeService | None = None,
    ) -> None:
        # socketserver calls server_close() when binding fails, so fields used
        # by our override must exist before the base constructor starts.
        self.catalog = catalog
        self.intake = intake
        self.deletion_token = secrets.token_urlsafe(32)
        self.deletion_hosts = {"localhost", "127.0.0.1", socket.gethostname().lower(), *_local_ipv4_addresses()}
        self._html_cache_lock = threading.RLock()
        self._html_cache: OrderedDict[
            tuple[object, ...],
            tuple[bytes, str],
        ] = OrderedDict()
        super().__init__(address, handler)
        self.started_at = _utc_now()

    def html_document(
        self,
        signature: tuple[object, ...],
        builder: Callable[[], bytes],
    ) -> tuple[bytes, str]:
        """Build a signature-stable HTML document once across request threads."""

        with self._html_cache_lock:
            cached = self._html_cache.get(signature)
            if cached is not None:
                self._html_cache.move_to_end(signature)
                return cached
            value = builder()
            result = (value, hashlib.sha256(value).hexdigest())
            self._html_cache[signature] = result
            self._html_cache.move_to_end(signature)
            while len(self._html_cache) > MAX_CACHED_HTML_DOCUMENTS:
                self._html_cache.popitem(last=False)
            return result

    def server_close(self) -> None:
        if self.intake is not None:
            self.intake.close()
        super().server_close()

    def delete_run(self, workspace_id: str) -> dict[str, Any]:
        intake_lock = self.intake.store._lock if self.intake is not None else contextlib.nullcontext()
        input_root = self.intake.input_root if self.intake is not None else self.catalog.root.parent / "input-oss"
        with intake_lock, self._html_cache_lock:
            try:
                return self.catalog.delete_run(workspace_id, input_root=input_root)
            finally:
                self._html_cache.clear()

    def delete_intake_job(self, job_id: str) -> dict[str, Any]:
        if self.intake is None:
            raise FileNotFoundError(job_id)
        with self._html_cache_lock:
            try:
                return self.intake.delete(job_id)
            finally:
                self._html_cache.clear()


class Handler(BaseHTTPRequestHandler):
    server: WorkbenchHTTPServer
    protocol_version = "HTTP/1.1"

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
        content_encoding: str | None = None,
        vary_accept_encoding: bool = False,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "SAMEORIGIN")
        self.send_header("Referrer-Policy", "no-referrer")
        if content_encoding:
            self.send_header("Content-Encoding", content_encoding)
        if vary_accept_encoding:
            self.send_header("Vary", "Accept-Encoding")
        if etag:
            self.send_header("ETag", f'"{etag}"')
        if length is not None:
            self.send_header("Content-Length", str(length))
        if self.close_connection:
            self.send_header("Connection", "close")
        self.end_headers()

    def _response_encoding(
        self,
        value: bytes,
        content_type: str,
    ) -> tuple[str | None, bool]:
        compressible = len(value) >= 1024 and (
            content_type.startswith("text/")
            or content_type.startswith("application/json")
            or content_type.startswith("application/javascript")
            or content_type.startswith("application/xml")
        )
        if not compressible:
            return None, False
        if _accepts_gzip(str(self.headers.get("Accept-Encoding") or "")):
            return "gzip", True
        return None, True

    def _bytes(
        self,
        value: bytes,
        *,
        content_type: str,
        status: int = HTTPStatus.OK,
        cache: str = "no-store",
        etag: str | None = None,
    ) -> None:
        content_encoding, vary = self._response_encoding(value, content_type)
        representation_etag = f"{etag}-gzip" if etag and content_encoding else etag
        if (
            status == HTTPStatus.OK
            and representation_etag
            and self.command in {"GET", "HEAD"}
            and _etag_matches(
                str(self.headers.get("If-None-Match") or ""),
                representation_etag,
            )
        ):
            self._headers(
                HTTPStatus.NOT_MODIFIED,
                content_type,
                0,
                etag=representation_etag,
                cache=cache,
                content_encoding=content_encoding,
                vary_accept_encoding=vary,
            )
            return
        body = value
        if content_encoding:
            body = (
                _gzip_payload(value)
                if len(value) <= MAX_CACHED_GZIP_BYTES
                else gzip.compress(value, compresslevel=6, mtime=0)
            )
        self._headers(
            status,
            content_type,
            len(body),
            etag=representation_etag,
            cache=cache,
            content_encoding=content_encoding,
            vary_accept_encoding=vary,
        )
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, value: Any, status: int = HTTPStatus.OK) -> None:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        if len(encoded) > MAX_API_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "响应数据过大")
            return
        cache = (
            "private, no-cache, must-revalidate"
            if self.command in {"GET", "HEAD"} and status == HTTPStatus.OK
            else "no-store"
        )
        self._bytes(
            encoded,
            content_type="application/json; charset=utf-8",
            status=status,
            cache=cache,
            etag=(
                hashlib.sha256(encoded).hexdigest()
                if self.command in {"GET", "HEAD"} and status == HTTPStatus.OK
                else None
            ),
        )

    def _json_document(self, encoded: bytes, etag: str) -> None:
        if len(encoded) > MAX_API_BYTES:
            self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "响应数据过大")
            return
        self._bytes(
            encoded,
            content_type="application/json; charset=utf-8",
            cache="private, no-cache, must-revalidate",
            etag=etag,
        )

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message, "status": int(status)}, status)

    def _require_intake(self) -> OSSIntakeService | None:
        if self.server.intake is None:
            self.close_connection = True
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "OSS 入站接口未启用")
            return None
        return self.server.intake

    def _read_request_json(self) -> dict[str, Any]:
        media_type = str(self.headers.get("Content-Type") or "").split(";", 1)[0]
        if media_type.strip().lower() != "application/json":
            raise OSSIntakeRequestError("Content-Type 必须是 application/json")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise OSSIntakeRequestError("请求必须提供 Content-Length")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise OSSIntakeRequestError("Content-Length 无效") from exc
        if not 1 <= length <= MAX_REQUEST_BYTES:
            raise OSSIntakeRequestError(
                f"请求体必须在 1 到 {MAX_REQUEST_BYTES} 字节之间"
            )
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise OSSIntakeRequestError("请求体未完整接收")
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OSSIntakeRequestError("请求体不是有效 UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise OSSIntakeRequestError("请求 JSON 顶层必须是对象")
        return value

    def _serve_html(self, query: dict[str, list[str]]) -> None:
        if not ROOT_HTML.is_file():
            self._error(HTTPStatus.NOT_FOUND, "固定工作台 HTML 不存在")
            return
        root_html_stat = ROOT_HTML.stat()
        requested = str((query.get("run") or [""])[0]).strip()
        workspace_id = _safe_workspace_id(requested) if requested else None
        root_signature: tuple[object, ...] = (
            str(ROOT_HTML),
            root_html_stat.st_mtime_ns,
            root_html_stat.st_ctime_ns,
            root_html_stat.st_size,
            API_VERSION,
            SYSTEM_VERSION,
        )
        signature = (
            "record",
            *root_signature,
            workspace_id,
            self.server.catalog.html_signature(workspace_id),
        ) if workspace_id is not None else ("system", *root_signature)

        def build() -> bytes:
            html = _read_root_html_template(
                str(ROOT_HTML),
                root_html_stat.st_mtime_ns,
                root_html_stat.st_ctime_ns,
                root_html_stat.st_size,
            )
            context: dict[str, Any] = {
                "api_version": API_VERSION,
                "system_version": SYSTEM_VERSION,
                "main_flow_tasks": main_flow_task_list(),
                "delivery_mode": "server",
                "mode": "record" if workspace_id is not None else "system",
                "selected_run": None,
                "run": None,
                "view_available": False,
                "analysis_files": [],
                "dom_checkpoints": [],
                "recent_events": [],
                "oss_intake_enabled": self.server.intake is not None,
            }
            if workspace_id is not None:
                snapshot = self.server.catalog.snapshot(workspace_id)
                view = snapshot.get("view")
                if snapshot["run"].get("status") == "completed" and isinstance(view, dict) and isinstance(view.get("sheets"), list):
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
            else:
                # The level-one system never reads run business rows. Avoid parsing,
                # rendering, compressing, and transferring the historical payload
                # that remains in the canonical source as its direct-file fallback.
                html = _replace_audit_payload(html, SYSTEM_AUDIT_PAYLOAD)
            return _inject_workbench_context(html, context).encode("utf-8")

        value, etag = self.server.html_document(signature, build)
        self._bytes(
            value,
            content_type="text/html; charset=utf-8",
            cache="private, no-cache, must-revalidate",
            etag=etag,
        )

    def _config(self) -> dict[str, Any]:
        port = int(self.server.server_address[1])
        addresses = _local_ipv4_addresses()
        return {
            "api_version": API_VERSION,
            "system_version": SYSTEM_VERSION,
            "material_problem_policy": "analyze_and_report",
            "scenario_classification_policy": "zip_name",
            "unclassified_archive_policy": "fail_before_ai_and_callback_reason",
            "confidence_badge_display": "hidden",
            "result_summary_display": "hidden",
            "result_list_heading_display": "hidden",
            "filter_header_display": "hidden",
            "result_filters": ["audit_type", "category"],
            "error_text_policy": "reason_and_action_separate",
            "business_file_display": "filenames_only",
            "main_flow_tasks": main_flow_task_list(),
            "refresh_policy": {
                "record_lists": {
                    "source": "/api/runs",
                    "views": ["ledger", "archive"],
                    "statuses": "all",
                    "shared_search": True,
                    "shared_visible_count": True,
                    "record_selector_uses_same_statuses": True,
                    "visible_update_rule": "run_catalog_signature_change",
                    "date_field": "analysis_started_at",
                    "date_fallback": "created_at",
                    "date_timezone": "Asia/Shanghai",
                    "title_field": "display_name",
                    "title_source": "source_archives",
                    "model_display": "tag",
                    "workspace_naming": "archive_stem_timestamp_unique_suffix",
                    "status_colors": {"running": "amber", "failed": "red", "completed": "green"},
                },
                "overview": {
                    "mode": "catalog_signature",
                    "record_count_statuses": "all",
                    "record_list_scope": "terminal_runs",
                    "record_list_statuses": ["completed", "failed"],
                    "record_list_page_size": 100,
                    "active_records": {
                        "title": "运行中的记录",
                        "sources": ["input", "oss"],
                        "deduplicate_by": "run_id",
                        "failed_intake_without_run": "separate_list",
                    },
                    "visible_update_rule": "run_catalog_signature_change",
                    "running_probe_interval_ms": 2500,
                    "idle_probe_interval_ms": 15000,
                },
                "record": {
                    "mode": "stage_boundary",
                    "visible_update_rule": "workspace_status_or_stage_index_change",
                    "running_probe_interval_ms": 2500,
                },
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
            "read_only": False,
            "workbench_read_only": False,
            "business_results_read_only": True,
            "manual_review": {
                "writable": True,
                "endpoint": "/api/runs/{workspace_id}/manual-review",
                "request_fields": ["reviewed"],
            },
            "run_deletion": {
                "writable": True,
                "endpoint": "/api/runs/{workspace_id}",
                "method": "DELETE",
                "request_fields": ["confirm_workspace_id"],
                "confirmation_token": self.server.deletion_token,
                "terminal_only": True,
                "permanent": True,
            },
            "oss_intake": {
                "enabled": self.server.intake is not None,
                "endpoint": "/api/intake/oss",
                "list_endpoint": "/api/intake/jobs",
                "operations_list_endpoint": "/api/intake/jobs?completed=0",
                "status_endpoint": "/api/intake/jobs/{job_id}",
                "deletion": {
                    "writable": self.server.intake is not None,
                    "endpoint": "/api/intake/jobs/{job_id}",
                    "method": "DELETE",
                    "request_fields": ["confirm_job_id"],
                    "active_allowed": True,
                    "permanent": True,
                },
                "authenticated": False,
                "authentication_required": False,
                "single_worker": False,
                "pipeline": "download_then_serial_audit",
                "download_before_audit": True,
                "download_worker_count": 1,
                "audit_worker_count": 1,
                "single_audit_worker": True,
                "terminal_replay_policy": "new_attempt",
                "callback_result_format": "scenario_error_facts_markdown",
                "callback_transport": "direct",
                "request_fields": ["verifyCode", "analyzeId", "downloadUrl"],
                "callback_path": CALLBACK_PATH,
                "callback_configured": (
                    bool(self.server.intake.config.callback_url)
                    if self.server.intake is not None
                    else False
                ),
                "callback_required": (
                    self.server.intake.config.callback_required
                    if self.server.intake is not None
                    else None
                ),
                "allow_http_callback": (
                    self.server.intake.config.allow_http_callback
                    if self.server.intake is not None
                    else None
                ),
                "receive_only": (
                    self.server.intake.config.receive_only
                    if self.server.intake is not None
                    else None
                ),
                "input_directory": (
                    self.server.intake.input_root.name
                    if self.server.intake is not None
                    else None
                ),
                "max_download_bytes": (
                    self.server.intake.config.max_download_bytes
                    if self.server.intake is not None
                    else None
                ),
            },
        }

    def _serve_events(self, workspace_id: str, query: dict[str, list[str]]) -> None:
        self.server.catalog.snapshot(workspace_id)
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
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, FileNotFoundError):
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
                encoded, etag = self.server.catalog.catalog_document()
                self._json_document(encoded, etag)
                return

            if route == "/api/intake/jobs":
                intake = self._require_intake()
                if intake is None:
                    return
                if set(query) - {"active", "completed"}:
                    raise ValueError("OSS 任务列表只支持 active 或 completed 查询参数")
                if "active" in query and "completed" in query:
                    raise ValueError("active 与 completed 不能同时使用")
                active_value = str((query.get("active") or [""])[0]).strip().lower()
                if active_value not in {"", "0", "1", "false", "true"}:
                    raise ValueError("active 必须是 0、1、false 或 true")
                completed_value = str((query.get("completed") or [""])[0]).strip().lower()
                if completed_value not in {"", "0", "1", "false", "true"}:
                    raise ValueError("completed 必须是 0、1、false 或 true")
                active_only = active_value in {"1", "true"}
                completed = (
                    completed_value in {"1", "true"}
                    if completed_value
                    else None
                )
                jobs = [
                    public_job(job)
                    for job in intake.list(
                        active_only=active_only,
                        completed=completed,
                    )
                ]
                self._json({"jobs": jobs, "count": len(jobs)})
                return

            intake_match = re.fullmatch(r"/api/intake/jobs/([a-f0-9]{24})", route)
            if intake_match:
                intake = self._require_intake()
                if intake is None:
                    return
                job = intake.get(intake_match.group(1))
                self._json({"job": public_job(job)})
                return

            match = re.fullmatch(r"/api/runs/([^/]+)(?:/(.*))?", route)
            if not match:
                self._error(HTTPStatus.NOT_FOUND, "接口不存在")
                return
            workspace_id = _safe_workspace_id(match.group(1))
            tail = match.group(2) or ""
            if not tail or tail == "snapshot":
                encoded, etag = self.server.catalog.snapshot_document(workspace_id)
                self._json_document(encoded, etag)
                return
            if tail == "log":
                value, etag = self.server.catalog.run_log_document(workspace_id)
                self._bytes(
                    value,
                    content_type="text/plain; charset=utf-8",
                    cache="private, no-cache, must-revalidate",
                    etag=etag,
                )
                return
            if tail == "events":
                self._serve_events(workspace_id, query)
                return
            if tail == "analysis":
                relative = str((query.get("path") or [""])[0])
                target, value, etag = self.server.catalog.analysis_document(
                    workspace_id,
                    relative,
                )
                content_type = mimetypes.guess_type(target.name)[0] or "application/json"
                self._bytes(
                    value,
                    content_type=f"{content_type}; charset=utf-8",
                    cache="private, no-cache, must-revalidate",
                    etag=etag,
                )
                return
            checkpoint_match = re.fullmatch(r"checkpoints/([^/]+)", tail)
            if checkpoint_match:
                _, value, etag = self.server.catalog.checkpoint_document(
                    workspace_id,
                    checkpoint_match.group(1),
                )
                self._bytes(
                    value,
                    content_type="application/json; charset=utf-8",
                    cache="private, no-cache, must-revalidate",
                    etag=etag,
                )
                return
            self._error(HTTPStatus.NOT_FOUND, "运行资源不存在")
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "运行或资源不存在")
        except (ValueError, json.JSONDecodeError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"读取失败：{exc}")

    def do_DELETE(self) -> None:  # noqa: N802
        # This service is for a trusted LAN. The origin and per-process nonce
        # prevent browser requests from other sites from turning a visit into a
        # destructive action; JSON binds confirmation to one displayed record.
        self.close_connection = True
        try:
            parsed = urlparse(self.path)
            route = parsed.path.rstrip("/")
            run_match = re.fullmatch(r"/api/runs/([^/]+)", route)
            intake_match = re.fullmatch(r"/api/intake/jobs/([a-f0-9]{24})", route)
            if (run_match is None and intake_match is None) or parsed.query:
                self._error(HTTPStatus.NOT_FOUND, "删除接口不存在")
                return
            origin = urlparse(str(self.headers.get("Origin") or ""))
            host = str(self.headers.get("Host") or "")
            if (
                origin.scheme != "http"
                or origin.netloc.lower() != host.lower()
                or origin.hostname not in self.server.deletion_hosts
                or origin.path or origin.params or origin.query or origin.fragment
                or self.headers.get("Sec-Fetch-Site", "same-origin") != "same-origin"
                or not secrets.compare_digest(
                    str(self.headers.get("X-Offline-Audit-Delete-Token") or "").encode("utf-8"),
                    self.server.deletion_token.encode("utf-8"),
                )
            ):
                self._error(HTTPStatus.FORBIDDEN, "请从当前局域网工作台确认删除")
                return
            if self.headers.get("Transfer-Encoding"):
                raise ValueError("删除请求不支持分块传输")
            payload = self._read_request_json()
            if intake_match is not None:
                job_id = intake_match.group(1)
                if payload != {"confirm_job_id": job_id}:
                    raise ValueError("必须明确确认同一条投递任务")
                result = self.server.delete_intake_job(job_id)
            else:
                workspace_id = _safe_workspace_id(run_match.group(1))
                if payload != {"confirm_workspace_id": workspace_id}:
                    raise ValueError("必须明确确认同一条运行记录")
                result = self.server.delete_run(workspace_id)
            self._json(result)
        except FileNotFoundError:
            self._error(HTTPStatus.NOT_FOUND, "运行或投递任务不存在")
        except RunDeletionConflict as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except (ValueError, OSSIntakeRequestError) as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except AuditError:
            self._error(HTTPStatus.CONFLICT, "运行或投递档案元数据损坏，无法确认完整删除范围")
        except OSError:
            self._error(HTTPStatus.CONFLICT, "删除未完成：文件可能被占用或权限不足，请解除占用后重试")

    def do_POST(self) -> None:  # noqa: N802
        route = urlparse(self.path).path.rstrip("/") or "/"
        review_match = re.fullmatch(r"/api/runs/([^/]+)/manual-review", route)
        if review_match:
            try:
                workspace_id = _safe_workspace_id(review_match.group(1))
                payload = self._read_request_json()
                if set(payload) != {"reviewed"} or not isinstance(
                    payload.get("reviewed"), bool
                ):
                    raise OSSIntakeRequestError(
                        "人工核验请求必须且只能包含布尔字段 reviewed"
                    )
                review = self.server.catalog.set_manual_review(
                    workspace_id,
                    payload["reviewed"],
                )
                self._json({"workspace_id": workspace_id, **review})
            except FileNotFoundError:
                self._error(HTTPStatus.NOT_FOUND, "运行不存在")
            except OSSIntakeRequestError as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except (ValueError, json.JSONDecodeError) as exc:
                self._error(HTTPStatus.BAD_REQUEST, str(exc))
            except OSError as exc:
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"人工核验状态写入失败：{exc}",
                )
            return
        if route != "/api/intake/oss":
            self.close_connection = True
            self._error(HTTPStatus.NOT_FOUND, "接口不存在")
            return
        intake = self._require_intake()
        if intake is None:
            return
        try:
            payload = self._read_request_json()
            job, created = intake.submit(payload)
            active = str(job.get("status")) not in {"completed", "failed"}
            self._json(
                {
                    "job": public_job(job),
                    "duplicate": not created,
                    "rerun": created and int(job.get("attempt") or 1) > 1,
                    "status_url": f"/api/intake/jobs/{job['job_id']}",
                },
                HTTPStatus.ACCEPTED if active else HTTPStatus.OK,
            )
        except OSSIntakeConflictError as exc:
            self._error(HTTPStatus.CONFLICT, str(exc))
        except OSSIntakeRequestError as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except OSSIntakeUnavailableError as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except OSError as exc:
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, f"OSS 入站写入失败：{exc}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="启动线下活动核销固定局域网工作台")
    parser.add_argument("--host", default=DEFAULT_HOST, help="监听地址，默认 0.0.0.0")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="监听端口，默认 8080")
    parser.add_argument(
        "--worktrees",
        default=str(DEFAULT_WORKTREES_ROOT),
        help="持久化运行目录",
    )
    parser.add_argument(
        "--enable-oss-intake",
        action="store_true",
        help="启用 OSS 自动投递接口",
    )
    parser.add_argument(
        "--oss-allowed-host",
        action="append",
        default=[],
        help="允许下载 OSS 对象的精确 HTTPS 域名；可重复传入",
    )
    parser.add_argument(
        "--oss-producer-model",
        default=os.environ.get("OFFLINE_AUDIT_OSS_PRODUCER_MODEL", "codex"),
        help="OSS 自动任务的 producer-model，默认 codex",
    )
    parser.add_argument(
        "--oss-max-bytes",
        type=int,
        help="单个 OSS ZIP 最大下载字节数；默认 1 GiB",
    )
    parser.add_argument(
        "--oss-download-timeout",
        type=int,
        help="OSS 单次网络操作超时秒数；默认 60",
    )
    parser.add_argument(
        "--oss-allow-private-hosts",
        action="store_true",
        help="允许白名单 OSS 域名解析到私网地址；仅专有网络场景使用",
    )
    parser.add_argument(
        "--oss-callback-url",
        help=f"分析完成后的完整回调地址；默认要求 HTTPS，路径必须为 {CALLBACK_PATH}",
    )
    parser.add_argument(
        "--oss-allow-http-callback",
        action="store_true",
        help="显式允许受信任内网使用 HTTP 结果回调；不得用于公网",
    )
    parser.add_argument(
        "--oss-no-callback",
        action="store_true",
        help="接收并处理 OSS 任务但不回调结果；调用方通过任务状态接口查询",
    )
    parser.add_argument(
        "--oss-receive-only",
        action="store_true",
        help="只下载、校验并保存 OSS ZIP，不启动核销且不回调结果",
    )
    parser.add_argument(
        "--oss-callback-timeout",
        type=int,
        help="结果回调单次超时秒数；默认 30",
    )
    parser.add_argument(
        "--oss-callback-attempts",
        type=int,
        help="结果回调最多尝试次数；默认 3",
    )
    return parser


def _environment_flag(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _environment_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise OSSIntakeRequestError(f"环境变量 {name} 必须是整数") from exc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not (1 <= args.port <= 65535):
        print("端口必须在 1 到 65535 之间", file=sys.stderr)
        return 2
    catalog = WorkbenchCatalog(Path(args.worktrees))
    try:
        server = WorkbenchHTTPServer(
            (args.host, args.port),
            Handler,
            catalog=catalog,
        )
    except OSError as exc:
        print(
            f"工作台启动失败：无法监听 {args.host}:{args.port}；{exc}",
            file=sys.stderr,
        )
        return 2
    intake_enabled = args.enable_oss_intake or _environment_flag(
        "OFFLINE_AUDIT_OSS_ENABLED"
    )
    if intake_enabled:
        allowed_hosts = list(args.oss_allowed_host)
        if not allowed_hosts:
            allowed_hosts = [
                value.strip()
                for value in str(
                    os.environ.get("OFFLINE_AUDIT_OSS_ALLOWED_HOSTS") or ""
                ).split(",")
                if value.strip()
            ]
        receive_only = args.oss_receive_only or _environment_flag(
            "OFFLINE_AUDIT_OSS_RECEIVE_ONLY"
        )
        try:
            config = OSSIntakeConfig(
                allowed_hosts=tuple(allowed_hosts),
                producer_model=args.oss_producer_model,
                max_download_bytes=(
                    args.oss_max_bytes
                    if args.oss_max_bytes is not None
                    else _environment_int(
                        "OFFLINE_AUDIT_OSS_MAX_BYTES",
                        DEFAULT_MAX_DOWNLOAD_BYTES,
                    )
                ),
                download_timeout_seconds=(
                    args.oss_download_timeout
                    if args.oss_download_timeout is not None
                    else _environment_int(
                        "OFFLINE_AUDIT_OSS_DOWNLOAD_TIMEOUT",
                        DEFAULT_DOWNLOAD_TIMEOUT_SECONDS,
                    )
                ),
                allow_private_hosts=(
                    args.oss_allow_private_hosts
                    or _environment_flag("OFFLINE_AUDIT_OSS_ALLOW_PRIVATE_HOSTS")
                ),
                workbench_url=(
                    os.environ.get("OFFLINE_AUDIT_WORKBENCH_URL")
                    or f"http://127.0.0.1:{server.server_address[1]}/"
                ),
                callback_url=(
                    args.oss_callback_url
                    or os.environ.get("OFFLINE_AUDIT_OSS_CALLBACK_URL")
                ),
                allow_http_callback=(
                    args.oss_allow_http_callback
                    or _environment_flag("OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK")
                ),
                callback_required=not (
                    args.oss_no_callback
                    or receive_only
                    or _environment_flag("OFFLINE_AUDIT_OSS_NO_CALLBACK")
                ),
                receive_only=receive_only,
                callback_token=os.environ.get("OFFLINE_AUDIT_OSS_CALLBACK_TOKEN"),
                callback_timeout_seconds=(
                    args.oss_callback_timeout
                    if args.oss_callback_timeout is not None
                    else _environment_int(
                        "OFFLINE_AUDIT_OSS_CALLBACK_TIMEOUT",
                        DEFAULT_CALLBACK_TIMEOUT_SECONDS,
                    )
                ),
                callback_attempts=(
                    args.oss_callback_attempts
                    if args.oss_callback_attempts is not None
                    else _environment_int(
                        "OFFLINE_AUDIT_OSS_CALLBACK_ATTEMPTS",
                        DEFAULT_CALLBACK_ATTEMPTS,
                    )
                ),
            )
            server.intake = OSSIntakeService(
                worktrees_root=catalog.root,
                input_root=DEFAULT_OSS_INPUT_ROOT,
                config=config,
            )
        except OSSIntakeRequestError as exc:
            server.server_close()
            print(f"OSS 入站接口配置失败：{exc}", file=sys.stderr)
            return 2
    config = {
        "local": f"http://127.0.0.1:{args.port}/",
        "lan": [
            f"http://{value}:{args.port}/"
            for value in _local_ipv4_addresses()
            if value != "127.0.0.1"
        ],
        "html": str(ROOT_HTML),
        "worktrees": str(catalog.root),
        "oss_input": (
            str(server.intake.input_root) if server.intake is not None else None
        ),
        "oss_intake": server.intake is not None,
        "oss_callback": (
            bool(server.intake.config.callback_url)
            if server.intake is not None
            else False
        ),
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
