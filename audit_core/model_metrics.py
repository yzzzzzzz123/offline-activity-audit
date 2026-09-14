"""按运行隔离的模型计量；只保存计数、受控标签和已校验可见事实。"""

from __future__ import annotations

import json
import math
import re
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


@contextmanager
def collect_model_artifacts(analysis_dir: Path) -> Iterator[None]:
    """为当前上下文启用收集；嵌套、异常退出和并发任务不会串写其他运行。"""
    root = Path(analysis_dir).resolve()
    root.mkdir(parents=True, exist_ok=True)
    token = _SINK.set(_ArtifactSink(root))
    try:
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
    model = attempt.get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", model) and ".." not in model:
        result["model"] = model
    for key, allowed in (("reasoning_effort", _EFFORTS), ("status", _STATUSES), ("error_code", _ERROR_CODES)):
        value = attempt.get(key)
        if isinstance(value, str) and value in allowed:
            result[key] = value
    for key in ("attempt", "image_count", "prompt_bytes", "delegated_agents_count"):
        value = attempt.get(key)
        if _count(value):
            result[key] = value
    if "delegated_agents_count" in attempt and attempt["delegated_agents_count"] is None:
        result["delegated_agents_count"] = None
    elapsed = attempt.get("elapsed_seconds")
    if type(elapsed) in (int, float) and elapsed >= 0 and (type(elapsed) is int or math.isfinite(elapsed)):
        result["elapsed_seconds"] = elapsed
    if isinstance(attempt.get("usage"), Mapping):
        result["usage"] = _usage(attempt["usage"])
    return result


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
    payload = _safe_attempt(attempt)
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
    encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    with sink.lock:
        directory = _contained_path(sink.root, "model-observations")
        directory.mkdir(exist_ok=True)
        destination = _contained_path(directory, f"{name}.json")
        temporary: Path | None = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", newline="\n", dir=directory, suffix=".tmp", delete=False) as handle:
                temporary = Path(handle.name)
                handle.write(encoded)
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
