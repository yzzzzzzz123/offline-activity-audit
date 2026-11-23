"""Select archive converters by capability, not by a generic executable name."""
from __future__ import annotations

import shutil
import subprocess

from .execution_trace import run_command


def _version_text(executable: str) -> str:
    try:
        result = run_command(
            [executable, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    output = (result.stdout or b"") + (result.stderr or b"")
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return str(output)


def rar_tool() -> str | None:
    """Return a libarchive-backed extractor that can actually handle RAR.

    Linux commonly ships GNU tar as ``tar`` and it cannot read RAR. The mere
    presence of ``tar`` is therefore not evidence of capability. A fallback
    named ``tar`` is accepted only when its own version output identifies
    bsdtar/libarchive.
    """
    bsdtar = shutil.which("bsdtar")
    if bsdtar:
        # The executable's own name is sufficient even when a minimal build
        # does not print a version string. An actual RAR listing remains the
        # final capability check in the extractor.
        return bsdtar

    generic = shutil.which("tar")
    if not generic:
        return None
    version = _version_text(generic).casefold()
    if "bsdtar" in version or "libarchive" in version:
        return generic
    return None
