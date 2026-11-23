"""按运行隔离的模型计量；只保存计数、受控标签和已校验可见事实。"""

from __future__ import annotations

import json
import math
import re
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import Lock
from typing import Any


_TOKEN_FIELDS = ("input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens")
_ERROR_CODES = frozenset({
    "context_limit", "output_limit", "timeout", "rate_limit", "auth",
    "configuration", "invalid_output", "model_error",
})
GATE_VALIDATION_CODES = frozenset({
    "gate_source_reference", "gate_source_coverage", "gate_material_list",
    "gate_material_state", "gate_staff_condition", "gate_consistency",
})
_EFFORTS = frozenset({"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"})
_STATUSES = frozenset({"success", "failed", "timeout", "error", "completed"})
_OBSERVATION_NAME = re.compile(r"[a-z][a-z0-9]*(?:[-_][a-z0-9]+)*\Z")
_WINDOWS_RESERVED = frozenset({"con", "prn", "aux", "nul", *(
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in range(1, 10)
)})


@dataclass
class _ArtifactSink:
    root: Path
    lock: Any = field(default_factory=Lock)


_SINK: ContextVar[_ArtifactSink | None] = ContextVar("audit_model_artifact_sink", default=None)


def current_analysis_root():
    sink = _SINK.get()
    return sink.root if sink is not None else None


@contextmanager
def collect_model_artifacts(analysis_dir: Path, observer=None) -> Iterator[None]:
    """为当前上下文启用收集；嵌套、异常退出和并发任务不会串写其他运行。"""
    root = Path(analysis_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = _SINK.set(_ArtifactSink(root))
    try:
        from .execution_trace import collect_execution_trace
        with collect_execution_trace(root, observer):
            yield
    finally:
        _SINK.reset(token)


def _count(value: Any) -> bool:
    return type(value) is int and value >= 0


def _usage(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result = {key: value[key] for key in _TOKEN_FIELDS if _count(value.get(key))}
    for details_key, field_name, target in (
        ("input_tokens_details", "cached_tokens", "cached_input_tokens"),
        ("output_tokens_details", "reasoning_tokens", "reasoning_tokens"),
    ):
        details = value.get(details_key)
        if target not in result and isinstance(details, Mapping) and _count(details.get(field_name)):
            result[target] = details[field_name]
    return result


def _classify_error(value: Any, depth: int = 0) -> str:
    """错误正文只在内存中分类，返回值始终属于固定枚举。"""
    if isinstance(value, Mapping):
        explicit_code = value.get("code")
        if isinstance(explicit_code, str) and explicit_code.strip().casefold() in _ERROR_CODES:
            return explicit_code.strip().casefold()
        parts = [value.get(key) for key in ("code", "type", "message", "reason")]
        nested = value.get("error")
        if depth < 4 and isinstance(nested, (str, Mapping)):
            return _classify_error(nested, depth + 1)
    else:
        parts = [value]
    message = " ".join(part for part in parts if isinstance(part, str)).casefold()
    if message in _ERROR_CODES:
        return message
    groups = (
        ("context_limit", ("context_limit", "context_length", "context window", "maximum context", "上下文长度", "上下文窗口")),
        ("output_limit", ("output_limit", "max_output_tokens", "max_tokens", "output token limit", "输出长度", "输出上限")),
        ("timeout", ("timeout", "timed out", "超时")),
        ("rate_limit", ("rate_limit", "rate limit", "too_many_requests", "too many requests")),
        ("auth", ("authentication", "invalid_api_key", "unauthorized", "permission_error", "permission denied", "认证失败")),
        ("configuration", ("configuration", "invalid_json_schema", "invalid_request_error", "model_not_found", "unsupported_value", "不支持的模型")),
        ("invalid_output", ("invalid_output", "json decode", "jsondecode", "schema validation", "invalid json", "json解析", "证据校验失败")),
    )
    for code, markers in groups:
        if any(marker in message for marker in markers):
            return code
    return "model_error"


def summarize_codex_events(stdout: str) -> dict[str, Any]:
    """读取 JSONL 中明确的终态用量和工具事件，不扫描消息或推理内容。

    缺失用量字段省略。仅完整且以终态闭合的 JSONL 可以证明零次委托；
    不完整流且未见明确成功 spawn_agent 事件时，委托数量为 None。
    """
    usage: dict[str, int] = {}
    completed_usage: dict[str, dict[str, int]] = {}
    agent_calls: set[str] = set()
    terminal = False
    valid_stream = True
    seen_event = False
    error_code: str | None = None
    for line_number, line in enumerate(stdout.splitlines()):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except (json.JSONDecodeError, ValueError, RecursionError):
            valid_stream = False
            continue
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            valid_stream = False
            continue
        seen_event = True
        event_type = event["type"]
        terminal = event_type in {"turn.completed", "turn.failed", "response.completed", "response.failed"}
        if terminal:
            container = event.get("response") if event_type.startswith("response.") else event
            if not isinstance(container, Mapping):
                container = event
            event_usage = _usage(container.get("usage"))
            event_id = container.get("id") or event.get("turn_id") or event.get("turnId")
            key = str(event_id) if isinstance(event_id, (str, int)) else f"line-{line_number}"
            completed_usage[key] = event_usage
            if event_type.endswith("failed"):
                error_code = _classify_error(container.get("error"))
        if event_type == "error":
            error_code = _classify_error(event.get("error", event))
        # 只认结构化工具调用的完成事件，排除普通文本、命令输出及失败调用。
        item = event.get("item")
        if event_type != "item.completed" or not isinstance(item, Mapping):
            continue
        if item.get("type") not in ("collab_tool_call", "collab_agent_tool_call", "function_call", "tool_call"):
            continue
        tool = item.get("tool") or item.get("name")
        function = item.get("function")
        if tool is None and isinstance(function, Mapping):
            tool = function.get("name")
        if tool not in ("spawn_agent", "functions.spawn_agent", "collaboration.spawn_agent"):
            continue
        if item.get("status") in ("failed", "error", "cancelled", "canceled") or item.get("error"):
            continue
        call_id = item.get("id") or item.get("call_id")
        agent_calls.add(str(call_id) if isinstance(call_id, (str, int)) else f"line-{line_number}")
    if completed_usage:
        known_fields = set(_TOKEN_FIELDS).intersection(*(set(counters) for counters in completed_usage.values()))
        for key in _TOKEN_FIELDS:
            if key in known_fields:
                usage[key] = sum(counters[key] for counters in completed_usage.values())
    result: dict[str, Any] = {
        "usage": usage,
        "delegated_agents_count": len(agent_calls) if agent_calls or (seen_event and terminal and valid_stream) else None,
    }
    if error_code is not None:
        result["error_code"] = error_code
    return result


def _safe_label(value: Any) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 96:
        return False
    if not re.fullmatch(r"[A-Za-z0-9_\u4e00-\u9fff ()（）/\-]+", value):
        return False
    # 允许“海报/物料”等代码自有业务标签，不接受目录、URL 或文件路径。
    return "/" not in value or all(
        0 < index < len(value) - 1 and "\u4e00" <= value[index - 1] <= "\u9fff"
        and "\u4e00" <= value[index + 1] <= "\u9fff"
        for index, character in enumerate(value) if character == "/"
    )


def _safe_attempt(attempt: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    if _safe_label(attempt.get("label")):
        result["label"] = attempt["label"]
    from .run_conversation import AGENT_IDS
    if attempt.get('agent_id') in AGENT_IDS:
        result['agent_id']=attempt['agent_id']
    model = attempt.get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", model) and ".." not in model:
        result["model"] = model
    for key, allowed in (("reasoning_effort", _EFFORTS), ("configured_reasoning_effort", _EFFORTS | {"default"}),
                         ("request_reasoning_effort", _EFFORTS | {"default"}), ("status", _STATUSES), ("error_code", _ERROR_CODES)):
        value = attempt.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    for key in ("attempt", "image_count", "prompt_bytes", "delegated_agents_count",
                "event_count", "model_event_count", "tool_completed_count", "transport_error_count",
                "request_message_count", "conversation_turn_count", "request_text_bytes",
                "history_text_bytes", "content_event_count", "reasoning_event_count", "visible_output_bytes"):
        value = attempt.get(key)
        if _count(value):
            result[key] = value
    if "delegated_agents_count" in attempt and attempt["delegated_agents_count"] is None:
        result["delegated_agents_count"] = None
    if attempt.get("timeout_kind") in {"total", "transport"}:
        result["timeout_kind"] = attempt["timeout_kind"]
    stage = attempt.get("timeout_stage")
    if isinstance(stage, str) and stage in {"connect", "read", "write", "pool", "total", "upstream", "transport"}:
        result["timeout_stage"] = stage
    for key in ("read_wait_seconds", "write_wait_seconds"):
        value = attempt.get(key)
        if type(value) in (int, float) and math.isfinite(value) and 0 < value <= 86400:
            result[key] = value
    if type(attempt.get("response_received")) is bool:
        result["response_received"] = attempt["response_received"]
    for key in ('stream_done_seen','response_body_ended','response_complete'):
        if type(attempt.get(key)) is bool:
            result[key] = attempt[key]
    if attempt.get('response_mode') in {'sse','json'}:
        result['response_mode'] = attempt['response_mode']
    if attempt.get('response_finish_reason') in {'stop','length','content_filter','tool_calls','function_call','missing','other'}:
        result['response_finish_reason'] = attempt['response_finish_reason']
    if attempt.get('context_scope') in {'current_stage_only','same_run_conversation'}:
        result['context_scope']=attempt['context_scope']
    if type(attempt.get('conversation_history_reused')) is bool:
        result['conversation_history_reused']=attempt['conversation_history_reused']
    conversation_id=attempt.get('conversation_id')
    if isinstance(conversation_id,str) and re.fullmatch(r'[a-f0-9]{32}',conversation_id):
        result['conversation_id']=conversation_id
    validation_stage = attempt.get("validation_stage")
    if isinstance(validation_stage, str) and validation_stage in {"json", "schema", "evidence"}:
        result["validation_stage"] = validation_stage
    validation_code = attempt.get("validation_code")
    if isinstance(validation_code, str) and validation_code in {"json_syntax", "json_object", "schema_validation",
            "contract_reference", "unknown_source", "quote_not_in_source", "number_quote_mismatch",
            "missing_numeric_evidence", "missing_comparison_source", "incomplete_checklist",
            "missing_photo_source", "evidence_validation", "reading_source_coverage", "display_source_claim"} | GATE_VALIDATION_CODES:
        result["validation_code"] = validation_code
    root_type = attempt.get("validation_root_type")
    if isinstance(root_type, str) and root_type in {"object", "array", "string", "number", "boolean", "null", "invalid_json"}:
        result["validation_root_type"] = root_type
    kinds = attempt.get("validation_schema_kinds")
    if isinstance(kinds, list):
        allowed_kinds = {"type", "required", "additionalProperties", "enum", "minItems", "maxItems", "minLength"}
        safe_kinds = list(dict.fromkeys(kind for kind in kinds[:6] if isinstance(kind, str) and kind in allowed_kinds))
        if safe_kinds:
            result["validation_schema_kinds"] = safe_kinds
    paths = attempt.get("validation_paths")
    if isinstance(paths, list):
        safe_paths = [path for path in paths[:8] if isinstance(path, str) and len(path) <= 192
                      and re.fullmatch(r"\$|(?:[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,6}|\?)(?:/(?:[A-Za-z_][A-Za-z0-9_]{0,63}|[0-9]{1,6}|\?))*", path)]
        if safe_paths:
            result["validation_paths"] = safe_paths
    status = attempt.get("http_status")
    if type(status) is int and 100 <= status <= 599:
        result["http_status"] = status
    request_id = attempt.get("request_id")
    if (isinstance(request_id, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", request_id)
            and not request_id.startswith("sk-")):
        result["request_id"] = request_id
    if attempt.get("phase") in {"starting", "awaiting_model", "model_output", "tool_running", "transport_retry", "finished"}:
        result["phase"] = attempt["phase"]
    elapsed = attempt.get("elapsed_seconds")
    if type(elapsed) in (int, float) and elapsed >= 0 and (type(elapsed) is int or math.isfinite(elapsed)):
        result["elapsed_seconds"] = elapsed
    if isinstance(attempt.get("usage"), Mapping):
        result["usage"] = _usage(attempt["usage"])
    elif any(key in attempt for key in _TOKEN_FIELDS):
        # PPIO/Bailian streaming counters are returned at the top level.
        result["usage"] = _usage(attempt)
    return result


def summarize_model_usage(analysis_dir: Path | None) -> dict[str, Any]:
    """Sum provider counters for every attempt, including failed/retried calls.

    Cached input and reasoning are subsets, never added to input + output.
    Missing usage remains unknown. Older Qwen runs can retain their final
    counters in a progress file even when the journal omitted its usage field.
    Only an unambiguous completed-call match may recover those counters.
    """
    totals: dict[str, int | None] = dict.fromkeys(_TOKEN_FIELDS)
    recorded = missing = 0
    records: list[dict[str, Any]] = []
    progress: dict[tuple[int, int], list[dict[str, Any]]] = {}
    if analysis_dir is not None:
        root = Path(analysis_dir)
        path = root / "model-metrics.jsonl"
        try:
            if path.is_symlink() or root.is_symlink():
                raise OSError("Unsafe metrics path")
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line)
                    except (ValueError, RecursionError):
                        missing += 1
                        continue
                    if isinstance(value, dict):
                        records.append(value)
                    else:
                        missing += 1
        except (OSError, UnicodeError):
            missing += bool(records)
        if any(not _usage(row.get("usage")) for row in records):
            directory = root / "model-observations"
            if not directory.is_symlink():
                for candidate in directory.glob("call-progress-*.json"):
                    try:
                        if candidate.is_symlink() or candidate.stat().st_size > 65536:
                            continue
                        value = json.loads(candidate.read_text(encoding="utf-8"))
                    except (OSError, ValueError, RecursionError):
                        continue
                    if not isinstance(value, dict):
                        continue
                    elapsed = value.get("elapsed_seconds")
                    if not (type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0):
                        continue
                    if _count(value.get("attempt")) and _count(value.get("model_event_count")):
                        key = (value["attempt"], value["model_event_count"])
                        progress.setdefault(key, []).append(value)
    by_model: dict[str, dict[str, Any]] = {}
    if missing:
        by_model["unknown"] = {**dict.fromkeys(_TOKEN_FIELDS), "recorded_calls": 0, "unrecorded_calls": missing}
    keys = [(row.get("attempt"), row.get("model_event_count")) for row in records]
    for row, key in zip(records, keys):
        usage = _usage(row.get("usage"))
        if not usage and all(_count(part) for part in key) and keys.count(key) == 1:
            candidates = progress.get(key, [])
            elapsed = row.get("elapsed_seconds")
            if (len(candidates) == 1 and type(elapsed) in (int, float)
                    and math.isfinite(elapsed)
                    and 0 <= elapsed - candidates[0]["elapsed_seconds"] < 1):
                usage = _usage(candidates[0])
        # Attribute only the model recorded on this actual call. In particular,
        # current settings and the run's main model cannot identify old calls.
        model = _safe_attempt({"model": row.get("model")}).get("model", "unknown")
        bucket = by_model.setdefault(model, {**dict.fromkeys(_TOKEN_FIELDS), "recorded_calls": 0, "unrecorded_calls": 0})
        if "input_tokens" in usage and "output_tokens" in usage:
            recorded += 1
            bucket["recorded_calls"] += 1
        else:
            missing += 1
            bucket["unrecorded_calls"] += 1
        for field, count in usage.items():
            totals[field] = (totals[field] or 0) + count
            bucket[field] = (bucket[field] or 0) + count

    def summary(counters: dict[str, Any]) -> dict[str, Any]:
        has_usage = counters["input_tokens"] is not None or counters["output_tokens"] is not None
        total = (counters["input_tokens"] + counters["output_tokens"]
                 if counters["input_tokens"] is not None and counters["output_tokens"] is not None else None)
        return {**counters, "total_tokens": total,
                "status": "partial" if has_usage and counters["unrecorded_calls"] else "recorded" if has_usage else "unrecorded"}

    return {**summary({**totals, "recorded_calls": recorded, "unrecorded_calls": missing}),
            "by_model": [{"model": model, **summary(counters)} for model, counters in sorted(by_model.items())]}


def _contained_path(root: Path, name: str) -> Path:
    candidate = root / name
    if candidate.is_symlink() or candidate.resolve().parent != root.resolve():
        raise ValueError("模型分析文件路径必须位于当前运行目录内")
    return candidate


def record_model_attempt(attempt: Mapping[str, Any]) -> None:
    """只追加白名单计量；label/model 必须由调用代码提供，不能来自模型正文。"""
    sink = _SINK.get()
    if sink is None:
        return
    from .run_conversation import current_agent_id, agent_scope_active
    payload = _safe_attempt({**({'agent_id':current_agent_id()} if agent_scope_active() else {}), **attempt})
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n"
    with sink.lock:
        path = _contained_path(sink.root, "model-metrics.jsonl")
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)


def save_model_observations(name: str, payload: Any) -> None:
    """保存调用端已通过场景 schema 的可见事实；本函数不接收原始模型日志。"""
    sink = _SINK.get()
    if sink is None:
        return
    if not isinstance(name, str) or len(name) > 64 or not _OBSERVATION_NAME.fullmatch(name) or name in _WINDOWS_RESERVED:
        raise ValueError("模型观察文件名只能使用安全场景标识")
    with sink.lock:
        directory = _contained_path(sink.root, "model-observations")
        directory.mkdir(exist_ok=True)
        destination = _contained_path(directory, f"{name}.json")
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=directory, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                json.dump(payload, handle, ensure_ascii=False, allow_nan=False, indent=2)
                handle.write('\n')
            for attempt in range(5):
                try:
                    temporary.replace(destination)
                    break
                except PermissionError:
                    # Windows readers can briefly hold a handle without delete
                    # sharing. Retry only this local atomic publication.
                    if attempt == 4:
                        raise
                    time.sleep(.01 * (attempt + 1))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    if not name.startswith("call-progress-"):
        from .execution_trace import record_step
        record_step("analysis.observation", "保存已校验分析记录：" + name, {"artifact": "analysis/model-observations/" + name + ".json", "observation": payload})
