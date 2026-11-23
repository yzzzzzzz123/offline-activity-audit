"""把已经没有核销进程的运行目录封存为失败，避免界面长期显示“正在核销”。

运行目录的 manifest 只有在进程优雅结束时才会写入终态。进程被强制结束、
机器断电或启动脚本异常退出时，manifest 会一直停在 running，工作台就会把
一单早就死掉的运行当成进行中并持续刷新“累计中”。

本模块只做两件事：运行开始时登记当前进程号，扫描时把确实没有进程接管的
运行按失败封存，并写明原因。
"""
from __future__ import annotations

import ctypes
import json
import os
import time
from pathlib import Path

from .workbench_store import WorkbenchRunStore, atomic_write_json

RUN_PID_FILENAME = "run.pid"
INTERRUPTED_CODE = "run_interrupted"
INTERRUPTED_REASON = (
    "核销进程已不在，本单运行未正常结束（被强制停止或异常退出），没有生成核销结论；"
    "原始资料包保留，请重新提交资料后整单重跑。"
)
# 旧运行目录没有进程登记文件，只能按空闲时间兜底判断。
LEGACY_IDLE_SECONDS = 900.0


def process_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def claim_run(workspace: Path) -> None:
    """运行目录登记当前核销进程，供工作台判断运行是否还活着。"""
    atomic_write_json(
        Path(workspace) / RUN_PID_FILENAME,
        {"pid": os.getpid(), "claimed_at": time.time()},
    )


def release_run(workspace: Path) -> None:
    try:
        (Path(workspace) / RUN_PID_FILENAME).unlink()
    except OSError:
        pass


def _recorded_pid(workspace: Path) -> int | None:
    try:
        value = json.loads((workspace / RUN_PID_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    pid = value.get("pid") if isinstance(value, dict) else None
    return pid if isinstance(pid, int) and pid > 0 else None


def _last_activity(workspace: Path) -> float | None:
    newest = None
    for relative in ("manifest.json", "snapshot.json", "logs/run.log", "analysis"):
        try:
            stamp = (workspace / relative).stat().st_mtime
        except OSError:
            continue
        newest = stamp if newest is None else max(newest, stamp)
    return newest


def seal_orphaned_runs(
    worktrees_root: Path,
    *,
    idle_seconds: float = LEGACY_IDLE_SECONDS,
    now: float | None = None,
) -> list[dict]:
    """把没有进程接管的 running 运行目录封存为失败，返回本次封存清单。"""
    root = Path(worktrees_root)
    if not root.is_dir():
        return []
    moment = time.time() if now is None else now
    sealed: list[dict] = []
    for manifest_path in sorted(root.glob("*/manifest.json")):
        workspace = manifest_path.parent
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if str(manifest.get("status")) not in {"running", "queued"}:
            continue
        pid = _recorded_pid(workspace)
        if pid is not None and process_alive(pid):
            continue
        if pid is None:
            last = _last_activity(workspace)
            if last is None or moment - last < idle_seconds:
                continue
        run_id = str(manifest.get("run_id") or "")
        if not run_id:
            continue
        try:
            store = WorkbenchRunStore.fail_interrupted(
                workspace,
                run_id=run_id,
                reason=INTERRUPTED_REASON,
                code=INTERRUPTED_CODE,
            )
        except Exception:
            continue
        release_run(workspace)
        sealed.append(
            {"workspace_id": store.workspace_id, "run_id": run_id, "pid": pid}
        )
    return sealed
