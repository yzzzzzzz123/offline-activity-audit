"""Give the read-only Windows sandbox access to this run's staged model files."""
from __future__ import annotations

import csv
from functools import lru_cache
import io
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import uuid

from .common import AuditError
from .model_metrics import save_model_observations


def sandbox_arguments() -> list[str]:
    # --ignore-user-config also removes native Windows sandbox selection. Without
    # an explicit backend, even Get-Content is rejected before it can run.
    arguments = ["-c", "allow_login_shell=false"]
    if sys.platform == "win32":
        arguments += ["-c", 'windows.sandbox="unelevated"']
    return arguments


@lru_cache(maxsize=1)
def _current_user_sid() -> str:
    result = subprocess.run(
        ["whoami.exe", "/user", "/fo", "csv", "/nh"], capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=10, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    rows = list(csv.reader(io.StringIO(result.stdout.strip())))
    sid = rows[0][-1] if len(rows) == 1 and rows[0] else ""
    if not re.fullmatch(r"S-1-\d+(?:-\d+)+", sid):
        raise AuditError("无法确认当前 Windows 用户 SID（configuration）")
    return sid


def prepare_model_directory(directory: Path) -> None:
    if sys.platform != "win32":
        return
    # Python 3.13+ mkdtemp uses OWNER RIGHTS instead of an explicit user ACE.
    # The restricted token cannot use that ACE. Grant only the SAME user's RX
    # on the staged model subtree, without opening the source/Excel parent.
    path = Path(directory)
    temporary = Path(tempfile.gettempdir()).resolve()
    resolved = path.resolve()
    if (not path.is_dir() or path.is_symlink() or path.is_junction()
            or resolved == temporary or not resolved.is_relative_to(temporary)):
        raise AuditError("模型读取权限只能配置在本次系统临时材料目录内（configuration）")
    try:
        subprocess.run(
            ["icacls.exe", str(resolved), "/grant", "*" + _current_user_sid() + ":(OI)(CI)(RX)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True, timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AuditError("当前用户的模型材料读取权限配置失败（configuration）") from exc


def verify_windows_sandbox(codex: str, temporary_root: Path) -> None:
    """No model call: prove staged reads work and writes remain denied."""
    if sys.platform != "win32":
        return
    with tempfile.TemporaryDirectory(prefix="sandbox-probe-", dir=temporary_root) as directory:
        root = Path(directory)
        canary = root / "canary.txt"
        nonce = uuid.uuid4().hex
        canary.write_text(nonce, encoding="utf-8")
        prepare_model_directory(root)
        command = [codex, "sandbox", "--permission-profile", ":read-only", "-C", str(root),
                   "--", "powershell.exe", "-NoProfile", "-NonInteractive", "-Command"]
        literal = "'" + str(canary).replace("'", "''") + "'"
        try:
            read = subprocess.run(
                [*command, f"Get-Content -LiteralPath {literal} -Raw -ErrorAction Stop"],
                cwd=root,
                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            write = subprocess.run(
                [*command, f"Set-Content -LiteralPath {literal} -Value changed -ErrorAction Stop"],
                cwd=root,
                capture_output=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise AuditError("Windows 只读沙箱预检无法执行（configuration）") from exc
        checks = {"version": 1, "platform": "windows", "sandbox": "read-only",
                  "native_backend": "unelevated", "login_shell": False,
                  "staged_file_read": read.returncode == 0 and read.stdout.strip() == nonce,
                  "staged_file_write_denied": write.returncode != 0 and canary.read_text(encoding="utf-8") == nonce}
        save_model_observations("execution-environment", checks)
        if not checks["staged_file_read"] or not checks["staged_file_write_denied"]:
            raise AuditError("Windows 沙箱预检失败：必须可读当前材料且拒绝写入（configuration）")


def required_read_was_blocked(stderr: str) -> bool:
    """Classify tool errors in memory; never preserve raw model diagnostics."""
    for line in stderr.casefold().splitlines():
        read_tool = "view_image" in line or "get-content" in line
        blocked = any(marker in line for marker in (
            "blocked by policy", "access is denied", "permission denied", "拒绝访问",
        ))
        if read_tool and blocked:
            return True
    return False


def reported_material_access_failure(value: object) -> bool:
    """An inaccessible source is an execution failure, not unclear business evidence."""
    if isinstance(value, dict):
        return any(reported_material_access_failure(item) for item in value.values())
    if isinstance(value, list):
        return any(reported_material_access_failure(item) for item in value)
    if not isinstance(value, str):
        return False
    text = value.casefold()
    return any(marker in text for marker in (
        "拒绝访问", "访问被拒绝", "被执行策略", "被策略拦截", "文件访问限制阻断",
        "access denied", "access is denied", "permission denied", "blocked by policy",
    ))
