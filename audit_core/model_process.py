"""Stream model subprocesses while exposing only bounded, safe progress metadata."""
from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from typing import Any, Callable


PROGRESS_INTERVAL_SECONDS = 30
TRANSPORT_FAILURE_TIMEOUT_SECONDS = 120
_TRANSPORT_MARKERS = (
    "tls handshake", "tls handshake eof", "error sending request", "connection refused",
    "connection reset", "connect error", "dns error", "failed to lookup address",
    "stream disconnected", "network is unreachable", "failed to connect",
)
_PHASE_LABELS = {
    "starting": "模型进程启动中",
    "awaiting_model": "等待模型响应，尚未观察到模型输出",
    "model_output": "已收到模型输出",
    "tool_running": "模型工具执行中",
    "transport_retry": "模型连接异常，等待重连",
    "finished": "模型调用已结束",
}


class _ProcessTree:
    """Own descendants, including those that retain pipes after the CLI exits."""
    def __init__(self, process: subprocess.Popen[str]):
        self.process = process
        self.job = None
        if os.name != "nt":
            return
        import ctypes as c
        from ctypes import wintypes as w

        class Limits(c.Structure):
            _fields_ = [("process_time", c.c_int64), ("job_time", c.c_int64),
                        ("flags", w.DWORD), ("min_working_set", c.c_size_t),
                        ("max_working_set", c.c_size_t), ("active_processes", w.DWORD),
                        ("affinity", c.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]

        class ExtendedLimits(c.Structure):
            _fields_ = [("basic", Limits), ("io", c.c_uint64 * 6),
                        ("process_memory", c.c_size_t), ("job_memory", c.c_size_t),
                        ("peak_process_memory", c.c_size_t), ("peak_job_memory", c.c_size_t)]

        self.kernel = c.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [c.c_void_p, w.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = w.HANDLE
        self.kernel.SetInformationJobObject.argtypes = [w.HANDLE, c.c_int, c.c_void_p, w.DWORD]
        self.kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        self.kernel.TerminateJobObject.argtypes = [w.HANDLE, w.UINT]
        self.kernel.CloseHandle.argtypes = [w.HANDLE]
        job = self.kernel.CreateJobObjectW(None, None)
        if not job:
            raise OSError("无法建立模型进程隔离")
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if (not self.kernel.SetInformationJobObject(job, 9, c.byref(limits), c.sizeof(limits))
                or not self.kernel.AssignProcessToJobObject(job, int(process._handle))):
            self.kernel.CloseHandle(job)
            raise OSError("无法登记模型子进程的退出清理")
        self.job = job

    def terminate(self) -> None:
        if self.job is not None:
            if not self.kernel.TerminateJobObject(self.job, 1):
                raise OSError("无法终止本次模型进程树")
        elif os.name != "nt":
            try:
                os.killpg(self.process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self) -> None:
        if self.job is not None:
            self.kernel.CloseHandle(self.job)
            self.job = None
        elif os.name != "nt":
            self.terminate()


class ModelProcessTimeout(subprocess.TimeoutExpired):
    def __init__(self, command: list[str], timeout: float, *, kind: str,
                 stdout: str, stderr: str, progress: dict[str, Any]):
        super().__init__(command, timeout, output=stdout, stderr=stderr)
        self.timeout_kind = kind
        self.progress = progress


def configured_timeout(default: int, variable: str) -> int:
    value = os.environ.get(variable, "").strip()
    if not value:
        return default
    try:
        result = int(value)
    except ValueError:
        raise ValueError(f"{variable} 必须是 1 至 86400 的整数") from None
    if not 1 <= result <= 86400:
        raise ValueError(f"{variable} 必须是 1 至 86400 的整数")
    return result


class ModelProgress:
    """Never retains or publishes message, reasoning, command, URL, or error text."""
    def __init__(self, started: float):
        self.started = started
        self.phase = "starting"
        self.event_count = 0
        self.model_event_count = 0
        self.tool_completed_count = 0
        self.transport_error_count = 0
        self.transport_failure_since: float | None = None
        self.last_model_event: float | None = None

    def observe(self, stream: str, line: str, now: float) -> None:
        event: dict[str, Any] = {}
        if stream == "stdout":
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    event = parsed
            except (ValueError, RecursionError):
                pass
        kind = event.get("type")
        if not isinstance(kind, str):
            return
        self.event_count += 1
        item = event.get("item")
        if not isinstance(item, dict):
            item = {}
        item_kind = item.get("type")
        if not isinstance(item_kind, str):
            item_kind = None
        # Local thread/turn creation does not establish a successful model response.
        if kind in {"thread.started", "turn.started"} and self.phase == "starting":
            self.phase = "awaiting_model"
        if kind in {"item.started", "item.updated", "item.completed"} and item_kind in {
            "reasoning", "agent_message", "command_execution", "mcp_tool_call",
            "tool_call", "function_call", "web_search", "plan", "file_change",
            "collab_tool_call", "collab_agent_tool_call",
        }:
            self.model_event_count += 1
            self.last_model_event = now
            self.transport_failure_since = None
            self.phase = "model_output"
            if item_kind in {"command_execution", "mcp_tool_call", "tool_call", "function_call"}:
                if kind == "item.completed":
                    self.tool_completed_count += 1
                else:
                    self.phase = "tool_running"
        # Inspect only errors, never ordinary agent text or tool output, for transport failures.
        error = event if kind in {"error", "turn.failed"} or item_kind == "error" else None
        # Stderr may also contain failed telemetry exports or copied tool output.
        # Only an authoritative model error event can shorten an active analysis.
        diagnostic = line.casefold() if error is not None else ""
        if any(marker in diagnostic for marker in _TRANSPORT_MARKERS):
            self.transport_error_count += 1
            if self.transport_failure_since is None:
                self.transport_failure_since = now
            self.phase = "transport_retry"
        if kind in {"turn.completed", "turn.failed"}:
            self.phase = "finished"

    def snapshot(self, now: float) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phase": self.phase, "event_count": self.event_count,
            "model_event_count": self.model_event_count,
            "tool_completed_count": self.tool_completed_count,
            "transport_error_count": self.transport_error_count,
            "elapsed_seconds": round(now - self.started, 3),
        }
        if self.last_model_event is not None:
            result["last_model_event_seconds"] = round(self.last_model_event - self.started, 3)
        return result


def run_model_process(command: list[str], *, input: str, timeout: float,
                      label: str, progress_callback: Callable[[dict[str, Any]], None] | None = None,
                      transport_timeout: float = TRANSPORT_FAILURE_TIMEOUT_SECONDS,
                      **kwargs: Any) -> subprocess.CompletedProcess[str]:
    """Drain both pipes immediately; a live process alone is never reported as analysis."""
    kwargs.pop("check", None)
    if os.name != "nt":
        kwargs["start_new_session"] = True
    process = subprocess.Popen(command, stdin=subprocess.PIPE, **kwargs)
    try:
        tree = _ProcessTree(process)
    except BaseException:
        process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        raise
    started = time.monotonic()
    progress = ModelProgress(started)
    messages: queue.Queue[tuple[str, str | None]] = queue.Queue(maxsize=256)
    # Keep complete output in memory, as subprocess.run did: truncating early
    # events could hide a sandbox denial or turn an unknown usage count into zero.
    # Only controlled metadata is published or persisted by the caller.
    captures: dict[str, list[str]] = {"stdout": [], "stderr": []}

    def reader(stream: Any, name: str) -> None:
        try:
            for line in stream:
                messages.put((name, line))
        finally:
            messages.put((name, None))
            stream.close()

    def writer() -> None:
        try:
            process.stdin.write(input)
            process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass
        finally:
            process.stdin.close()

    readers = [threading.Thread(target=reader, args=(getattr(process, name), name), daemon=True)
               for name in ("stdout", "stderr")]
    for worker in readers:
        worker.start()
    input_worker = threading.Thread(target=writer, daemon=True)
    input_worker.start()
    closed: set[str] = set()
    next_progress = started
    timeout_kind: str | None = None
    try:
        while len(closed) < 2 or process.poll() is None:
            now = time.monotonic()
            if timeout_kind is None:
                if now - started >= timeout:
                    timeout_kind = "total"
                elif (progress.transport_failure_since is not None
                      and now - progress.transport_failure_since >= transport_timeout):
                    timeout_kind = "transport"
                if timeout_kind is not None:
                    tree.terminate()
            if process.poll() is not None and len(closed) < 2:
                # A completed root must not wait forever for a forgotten descendant's stdout.
                tree.terminate()
            if now >= next_progress:
                snapshot = progress.snapshot(now)
                print(f"AI 进度 {label}：{_PHASE_LABELS[progress.phase]}；"
                      f"已用 {now - started:.0f} 秒，模型事件 {progress.model_event_count}，"
                      f"工具完成 {progress.tool_completed_count}，连接异常 {progress.transport_error_count}", flush=True)
                if progress_callback is not None:
                    progress_callback(snapshot)
                next_progress = now + PROGRESS_INTERVAL_SECONDS
            try:
                name, line = messages.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                closed.add(name)
                continue
            captures[name].append(line)
            progress.observe(name, line, time.monotonic())
        process.wait()
        snapshot = progress.snapshot(time.monotonic())
        if progress_callback is not None:
            progress_callback(snapshot)
        stdout = "".join(captures["stdout"])
        stderr = "".join(captures["stderr"])
        if timeout_kind is not None:
            raise ModelProcessTimeout(command, timeout, kind=timeout_kind,
                                      stdout=stdout, stderr=stderr, progress=snapshot)
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        tree.close()
        process.wait(timeout=10)
        for worker in [*readers, input_worker]:
            worker.join(timeout=2)
