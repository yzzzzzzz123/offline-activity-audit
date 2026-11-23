"""Run-local, append-only records of actual operations and visible model output."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from threading import RLock
import uuid
import subprocess
import time
from functools import lru_cache


_SECRET_KEYS = frozenset({"authorization", "api_key", "apikey", "appkey", "password", "secret", "access_key_id", "access_key_secret", "token", "digest", "nonce", "reasoning_content", "reasoning", "thinking"})
_SINK = ContextVar("audit_execution_trace", default=None)


def safe_value(value):
    if isinstance(value, dict):
        return {str(k): "[已隐藏]" if str(k).casefold() in _SECRET_KEYS else safe_value(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [safe_value(v) for v in value]
    if isinstance(value, Path):
        return value.name
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}\b", "[已隐藏密钥]", value)
        return re.sub(r"(https?://[^\s?<>\"']+)\?[^\s<>\"']+", r"\1?[查询参数已隐藏]", value)
    return value


@dataclass
class TraceSink:
    root: Path
    observer: object = None
    sequence: int = 0
    lock: RLock = field(default_factory=RLock)


@contextmanager
def collect_execution_trace(analysis_root: Path, observer=None):
    root = Path(analysis_root).resolve() / "execution-trace"
    sink = TraceSink(root, observer)
    if root.is_dir():
        sink.sequence = max((int(p.stem.split("-")[1]) for p in root.glob("step-*.json") if re.fullmatch(r"step-\d{8}", p.stem)), default=0)
    token = _SINK.set(sink)
    try:
        yield
    finally:
        _SINK.reset(token)


def record_step(kind: str, label: str, data=None, *, status="completed", call_id=None):
    sink = _SINK.get()
    if sink is None:
        return None
    with sink.lock:
        sink.root.mkdir(exist_ok=True)
        if sink.root.is_symlink():
            raise OSError("执行记录目录不允许链接")
        sink.sequence += 1
        from .run_conversation import current_agent_id
        event = {"schema_version": "1.0", "sequence": sink.sequence, "type": kind, "label": label,
                 "agent_id": current_agent_id(),
                 "timestamp": datetime.now(timezone.utc).isoformat(), "status": status,
                 "call_id": call_id, "data": safe_value(data or {})}
        path = sink.root / f"step-{sink.sequence:08d}.json"
        # Exclusive creation preserves every attempt and never rewrites a prior step.
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(event, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
        meta = {k: event[k] for k in ("sequence", "type", "label", "timestamp", "status", "call_id", "agent_id")}
        meta["path"] = "analysis/execution-trace/" + path.name
        if sink.observer:
            sink.observer("execution.step", meta)
        return meta


def new_call_id(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:12] + "-" + uuid.uuid4().hex[:12]


def display_data(event):
    """Small, factual step descriptions; full input/output stays in its original file."""
    data = event.get("data") or {}
    if not isinstance(data, dict):
        return {}
    keys = ("source", "data_source", "table", "fields", "condition", "statement", "sql", "transport",
            "endpoint", "api_endpoint", "http_method", "business_parameters", "purpose", "verify_code",
            "provider", "model", "agent_id", "worker_agent_id", "attempt", "next_attempt", "delay_seconds", "reason", "validation",
            "error_code", "error_type", "timeout_stage", "elapsed_seconds", "return_code", "row_count",
            "verification_count", "plan_count", "join_fields", "object_key", "bucket", "product_code",
            "view_id", "size_bytes", "artifact", "path", "file")
    if event.get('type', '').startswith('audit.execution_diagnostic.'):
        keys += ('diagnosis', 'category', 'method', 'model_status', 'operator_action',
                 'developer_fix', 'repair_id', 'facts', 'evidence', 'diagnostic_error_type')
    def bounded(value):
        if isinstance(value, str):
            return value[:1600] + ("…（完整内容见本步原文）" if len(value) > 1600 else "")
        if isinstance(value, dict):
            return {k: bounded(v) for k, v in list(value.items())[:20]}
        if isinstance(value, list):
            return [bounded(v) for v in value[:20]]
        return value
    result = {key: bounded(safe_value(data[key])) for key in keys if key in data}
    for key, target in (("rows", "row_count"), ("images", "image_count"), ("linked_application_rows", "linked_row_count")):
        if isinstance(data.get(key), list):
            result.setdefault(target, len(data[key]))
    if isinstance(data.get("input_text"), str):
        result["input_characters"] = len(data["input_text"])
    metrics = data.get("metrics") or {}
    usage = metrics.get("usage", metrics) if isinstance(metrics, dict) else {}
    if isinstance(usage, dict):
        result["usage"] = {key: usage[key] for key in ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_tokens")
                           if type(usage.get(key)) is int and usage[key] >= 0}
    return result


@lru_cache(maxsize=2048)
def _resource_metadata(filename: str, modified: int, changed: int, size: int):
    del modified, changed, size
    raw = Path(filename).read_bytes()
    value = json.loads(raw)
    if not isinstance(value, dict):
        return None
    meta = {k: value.get(k) for k in ("schema_version", "sequence", "type", "label", "timestamp", "status", "call_id", "agent_id")}
    meta["display_data"] = display_data(value)
    return len(raw), hashlib.sha256(raw).hexdigest(), meta


def technical_inventory(workspace: Path, existing=()):
    """Discover only explicit internal trace/observation JSON, never arbitrary files."""
    records = {r["path"]: r for r in existing if isinstance(r, dict) and r.get("path")}
    steps = []
    analysis = workspace / "analysis"
    if analysis.is_symlink():
        return list(records.values()), {"schema_version":"1.0","count":0,"steps":[],"notice":"分析目录不符合本运行来源边界。"}
    for folder, pattern in (("execution-trace", r"step-\d{8}\.json"), ("model-observations", r"[a-z][a-z0-9_-]{0,63}\.json")):
        directory = workspace / "analysis" / folder
        if directory.is_symlink() or not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            if path.is_symlink() or not re.fullmatch(pattern, path.name):
                continue
            try:
                stat = path.stat()
                metadata = _resource_metadata(str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
            except (OSError, ValueError):
                continue
            if metadata is None:
                continue
            byte_count, digest, value = metadata
            relative = path.relative_to(workspace).as_posix()
            if folder == "execution-trace":
                if not isinstance(value, dict) or value.get("schema_version") != "1.0" or type(value.get("sequence")) is not int:
                    continue
                meta = {k: value.get(k) for k in ("sequence", "type", "label", "timestamp", "status", "call_id", "agent_id", "display_data")}
                steps.append({**meta, "path": relative})
                label = f"执行 {value['sequence']:04d} · {value.get('label') or value.get('type')}"
            else:
                label = "已保存分析记录 · " + path.stem
            records[relative] = {"path": relative, "label": label, "size": byte_count, "sha256": digest}
    steps.sort(key=lambda item: item["sequence"])
    return sorted(records.values(), key=lambda item: item["path"]), {"schema_version": "1.0", "count": len(steps), "steps": steps,
        "notice": "按实际操作顺序记录输入、返回、校验和异常。" if steps else "本历史记录未保存逐步执行轨迹；现有分析文件、日志和阶段断点仍可查看，不能据此补造未记录步骤。"}


def execution_route(steps):
    """Project actual external-system visits, retaining later return visits."""
    labels = {"database": "商品数据库", "qince": "勤策", "oss": "商品 OSS", "ai": "AI 识别"}
    route = []
    ordered = sorted((step for step in steps if isinstance(step, dict)
                      and type(step.get("sequence")) is int), key=lambda step: step["sequence"])
    for step in ordered:
        kind = str(step.get("type") or "")
        if step.get("status") == "skipped" or kind.endswith(".skipped"):
            continue
        stage = labels.get(kind.split(".")[0])
        if stage is not None and (not route or route[-1] != stage):
            route.append(stage)
    return route


def legacy_execution_route(events):
    """Recover explicit call receipts, never substitute workflow progress."""
    steps = []
    for event in events:
        if not isinstance(event, dict):
            continue
        kind = str(event.get('type') or event.get('event') or '').removeprefix('workflow.')
        if kind == 'execution.step':
            detail = event.get('details')
            if isinstance(detail, dict):
                steps.append({**detail, 'sequence': len(steps) + 1})
        elif kind == 'product_database.loaded':
            steps.append({'sequence':len(steps)+1, 'type':'database.result'})
        elif kind.split('.')[0] in {'ai', 'qince', 'database', 'oss'}:
            steps.append({**event, 'type':kind, 'sequence':len(steps)+1})
    return execution_route(steps)


@lru_cache(maxsize=256)
def _event_route(filename, modified, changed, size):
    del modified, changed, size
    def events():
        with Path(filename).open('r', encoding='utf-8') as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                except (ValueError, RecursionError):
                    continue
                if isinstance(value, dict):
                    yield value
    return tuple(legacy_execution_route(events()))


def _event_route_signature(workspace):
    directory = workspace / 'logs'
    if directory.is_symlink():
        return ()
    signature = []
    for name in ('events.jsonl', 'run.log'):
        path = directory / name
        try:
            if path.is_symlink():continue
            info = path.stat()
            if stat.S_ISREG(info.st_mode):
                signature.append(('logs/'+name, info.st_mtime_ns, info.st_ctime_ns, info.st_size))
        except OSError:
            continue
    return tuple(signature)


@lru_cache(maxsize=256)
def _log_route(filename, modified, changed, size):
    """Old runner output proves only the calls that it actually logged."""
    del modified, changed, size
    steps = []
    with Path(filename).open('r', encoding='utf-8') as stream:
        for line in stream:
            if re.match(r'^AI 正在识别 .+（第 \d+/\d+ 次，', line):
                steps.append({'sequence':len(steps)+1, 'type':'ai.request'})
            elif re.match(r'^\d{4}-\d{2}-\d{2}T\S+ INFO\s+intake 已读取商品数据库 \d+ 条，供本次核销使用\s*$', line):
                steps.append({'sequence':len(steps)+1, 'type':'database.result'})
    return tuple(execution_route(steps))


def read_legacy_execution_route(workspace, signature=None):
    if signature is None:
        signature = _event_route_signature(workspace)
    clock_route = _legacy_clock_route(str(workspace), tuple(signature))
    if clock_route:
        return list(clock_route)
    route = []
    for name, modified, changed, size in signature:
        if name != 'logs/events.jsonl':
            continue
        path = workspace / name
        if (workspace / 'logs').is_symlink() or path.is_symlink():
            return []
        try:
            route = list(_event_route(str(path), modified, changed, size))
        except (OSError, UnicodeError):
            continue
    for name, modified, changed, size in signature:
        if name != 'logs/run.log':continue
        path = workspace / name
        if (workspace / 'logs').is_symlink() or path.is_symlink():return []
        try:
            logged = list(_log_route(str(path), modified, changed, size))
            if logged and (not route or (len(logged) >= len(route) and set(route) <= set(logged))):
                return logged
        except (OSError, UnicodeError):continue
    return route


@lru_cache(maxsize=256)
def _legacy_clock_route(workspace_name, signature):
    """Recover old call order only from clocks still inside the sealed run.

    Model progress contains measured elapsed time; its original last-write
    clock gives the request start. Copied/retouched progress outside the run
    cannot prove order and is ignored. Database read_at_utc is the actual
    saved snapshot clock. No business phase is translated into a call.
    """
    from .run_timing import parse_time
    workspace = Path(workspace_name)
    bounds = []
    sealed = False
    try:
        if (workspace/'logs').is_symlink() or (workspace/'logs/events.jsonl').is_symlink():return ()
        with (workspace/'logs/events.jsonl').open(encoding='utf-8') as stream:
            for line in stream:
                try:event=json.loads(line)
                except (ValueError,RecursionError):continue
                if not isinstance(event,dict):continue
                stamp=parse_time(event.get('timestamp'))
                if stamp is not None:bounds.append(stamp.timestamp())
                sealed = event.get('type') in {'run.completed','run.failed'}
    except (OSError,UnicodeError):return ()
    if not sealed or len(bounds)<2:return ()
    start,end=min(bounds),max(bounds)
    calls=[]
    for name,modified,changed,size in signature:
        del changed
        if not name.startswith('analysis/'):continue
        path=workspace/name
        if (workspace/'analysis').is_symlink() or path.parent.is_symlink() or path.is_symlink():continue
        progress=path.name.startswith('call-progress-')
        # Progress contains counters only. Database catalogs can be larger;
        # only their existing data_source clock participates in this route.
        if size > (65536 if progress else 16*1024*1024):continue
        try:value=json.loads(path.read_text(encoding='utf-8'))
        except (OSError,ValueError,UnicodeError,RecursionError):continue
        if not isinstance(value,dict):continue
        if progress:
            elapsed=value.get('elapsed_seconds');saved=modified/1_000_000_000
            if (value.get('phase') not in {'awaiting_model','model_output','finished'}
                    or type(value.get('attempt')) is not int or value['attempt']<1
                    or type(elapsed) not in (int,float) or not 0<=elapsed<=(end-start)
                    or not start<=saved<=end or not start<=saved-elapsed<=end):continue
            calls.append((saved-elapsed,'ai.request'))
        else:
            source=value.get('data_source')
            if not isinstance(source,dict) or source.get('type')!='mysql':continue
            stamp=parse_time(source.get('read_at_utc'))
            if stamp is not None and start<=stamp.timestamp()<=end:
                calls.append((stamp.timestamp(),'database.result'))
    # An unpaired database snapshot cannot establish the AI call order.
    if not any(kind=='ai.request' for _,kind in calls):return ()
    return tuple(execution_route([{'sequence':i,'type':kind} for i,(_,kind) in enumerate(sorted(calls),1)]))


def execution_route_signature(workspace: Path):
    """Stat trace and phase journals; catalog polling never loads full snapshots."""
    signature = list(_event_route_signature(workspace))
    analysis = workspace / "analysis"
    observations = analysis/'model-observations'
    if not analysis.is_symlink() and not observations.is_symlink() and observations.is_dir():
        try:
            with os.scandir(observations) as entries:
                for entry in entries:
                    if not re.fullmatch(r'(?:call-progress-[a-f0-9]{16}|(?:photo|pos)-product-database-[a-zA-Z0-9_-]+)\.json',entry.name):continue
                    try:
                        if entry.is_symlink() or not entry.is_file(follow_symlinks=False):continue
                        info=entry.stat(follow_symlinks=False)
                        signature.append(('analysis/model-observations/'+entry.name,info.st_mtime_ns,info.st_ctime_ns,info.st_size))
                    except OSError:continue
        except OSError:pass
    database = analysis/'product-database.json'
    try:
        if not analysis.is_symlink() and not database.is_symlink():
            info=database.stat()
            if stat.S_ISREG(info.st_mode):signature.append(('analysis/product-database.json',info.st_mtime_ns,info.st_ctime_ns,info.st_size))
    except OSError:pass
    directory = analysis / "execution-trace"
    if analysis.is_symlink() or directory.is_symlink() or not directory.is_dir():
        return tuple(signature)
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                if not re.fullmatch(r"step-\d{8}\.json", entry.name):
                    continue
                try:
                    if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                        continue
                    metadata = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                signature.append((entry.name, metadata.st_mtime_ns, metadata.st_ctime_ns, metadata.st_size))
    except OSError:
        return tuple(signature)
    return tuple(sorted(signature))


@lru_cache(maxsize=32768)
def _route_metadata(filename, modified, changed, size):
    """Read the trace header only; full evidence remains available on demand."""
    del modified, changed, size
    path = Path(filename)
    prefix = []
    with path.open('r', encoding='utf-8') as stream:
        # record_step writes its small metadata before the potentially very large
        # data object. Catalog reads must not load/hash every model conversation.
        for _ in range(32):
            line = stream.readline(4096)
            if not line:
                break
            if line.startswith('  "data":'):
                value = json.loads(''.join(prefix).rstrip().removesuffix(',') + '}')
                break
            prefix.append(line)
        else:
            value = None
        if 'value' not in locals() or value is None:
            # Preserve compatibility with older compact/reordered trace files.
            stream.seek(0)
            value = json.load(stream)
    if not isinstance(value, dict):
        return None
    return {key: value.get(key) for key in ('schema_version', 'sequence', 'type', 'status')}


@lru_cache(maxsize=256)
def _read_execution_route(workspace_name, signature):
    workspace = Path(workspace_name)
    steps = []
    for filename, modified, changed, size in signature:
        if not re.fullmatch(r'step-\d{8}\.json',filename):
            continue
        path = workspace / "analysis" / "execution-trace" / filename
        try:
            metadata = _route_metadata(str(path), modified, changed, size)
        except (OSError, ValueError):
            continue
        if metadata is not None and metadata.get("schema_version") == "1.0":
            steps.append(metadata)
    return tuple(execution_route(steps) or read_legacy_execution_route(workspace, signature))


def read_execution_route(workspace: Path, signature=None):
    """Reuse metadata by current file signatures, without loading trace bodies."""
    if signature is None:
        signature = execution_route_signature(workspace)
    return list(_read_execution_route(str(workspace), tuple(signature)))


def enrich_technical_snapshot(snapshot: dict, workspace: Path):
    snapshot["analysis_files"], snapshot["execution_trace"] = technical_inventory(workspace, snapshot.get("analysis_files") or [])
    if isinstance(snapshot.get("run"), dict):
        snapshot["run"]["execution_route"] = read_execution_route(workspace)
        if snapshot['run'].get('status') == 'completed':
            from .run_timing import completed_timing
            snapshot["run"]["audit_timing"] = completed_timing(snapshot['run'])
    return snapshot


def run_command(command, *args, trace_output=True, **kwargs):
    """Preserve the real subprocess behavior and its complete safe text outputs."""
    if _SINK.get() is None:
        return subprocess.run(command, *args, **kwargs)
    call_id = uuid.uuid4().hex
    started = time.monotonic()
    parts = [str(v) for v in command] if isinstance(command, (list, tuple)) else [str(command)]
    for index, part in enumerate(parts[:-1]):
        if part.casefold() in {"--password", "--api-key", "--token", "--secret"}:
            parts[index + 1] = "[已隐藏]"
    record_step("command.started", "执行命令：" + Path(parts[0]).name, {"arguments": parts,
        "working_directory": str(kwargs.get("cwd") or "由当前进程继承"), "timeout_seconds": kwargs.get("timeout"),
        "stdin": kwargs.get("input") if isinstance(kwargs.get("input"), str) else None}, status="started", call_id=call_id)
    def output(value):
        if isinstance(value, bytes):
            try:
                return value.decode("utf-8")
            except UnicodeError:
                return {"size_bytes": len(value), "sha256": hashlib.sha256(value).hexdigest(), "binary_output": True}
        return value
    try:
        result = subprocess.run(command, *args, **kwargs)
    except (OSError, subprocess.SubprocessError) as exc:
        record_step("command.failed", "命令执行失败：" + Path(parts[0]).name, {"error_type": type(exc).__name__,
            "return_code": getattr(exc, "returncode", None), "elapsed_seconds": round(time.monotonic() - started, 3),
            **({"stdout": output(getattr(exc, "stdout", None)), "stderr": output(getattr(exc, "stderr", None))} if trace_output else {})}, status="failed", call_id=call_id)
        raise
    record_step("command.completed", "命令返回：" + Path(parts[0]).name, {"return_code": result.returncode,
        "elapsed_seconds": round(time.monotonic() - started, 3),
        **({"stdout": output(result.stdout), "stderr": output(result.stderr)} if trace_output else {"output_policy": "认证通道原始回包不展示，校验后的商品结果另有记录"})},
        status="completed" if result.returncode == 0 else "failed", call_id=call_id)
    return result
