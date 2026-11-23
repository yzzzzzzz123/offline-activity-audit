"""Writable test directories without relying on the host TEMP ACL.

The managed Windows environment used for development creates
``tempfile.mkdtemp`` directories with an ACL that prevents the same process
from writing into them.  That is a host restriction, not a production reader
failure.  Reader tests therefore use an explicitly created directory under the
project runtime tree.  Production code keeps its normal 0700 tempfile
semantics, which are correct on Linux.
"""
from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TEST_PARENT = PROJECT_ROOT / "runtime"


def new_test_dir(prefix: str = "reader-test") -> Path:
    """Create one isolated directory using the platform's normal mkdir ACL."""
    TEST_PARENT.mkdir(parents=True, exist_ok=True)
    root = TEST_PARENT / f"{prefix}-{os.getpid()}-{uuid.uuid4().hex}"
    root.mkdir(parents=True)
    return root


def remove_test_dir(root: Path) -> None:
    """Remove only a directory created by :func:`new_test_dir`."""
    target = Path(root).resolve()
    parent = TEST_PARENT.resolve()
    if target.parent != parent or not target.name.startswith("reader-test") and not any(
            target.name.startswith(prefix) for prefix in (
                "word-material-", "material-intake-", "large-readers-",
                "reader-skills-", "pos-shape-", "reader-contract-",
                "reader-stability-", "material-preflight-",
            )):
        raise RuntimeError(f"拒绝清理非测试目录: {target}")
    shutil.rmtree(target, ignore_errors=True)


@contextmanager
def test_dir(prefix: str = "reader-test"):
    root = new_test_dir(prefix)
    try:
        yield root
    finally:
        remove_test_dir(root)
