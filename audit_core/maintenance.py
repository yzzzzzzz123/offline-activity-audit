"""Bounded housekeeping for reproducible Python caches, never business storage."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

from .run_temporary import _plain, _tree, cleanup_abandoned_temporaries


PROJECT_ROOT = Path(__file__).resolve().parents[1]
_CODE_DIRS = ("audit_core", "skills", "scripts", "tests")
_ROOT_CACHES = ("__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache")


def _cache_candidates(root: Path):
    for name in _ROOT_CACHES:
        candidate = root / name
        if candidate.exists() or candidate.is_symlink():
            yield candidate
    for name in _CODE_DIRS:
        base = root / name
        if not base.exists():
            continue
        _plain(base, directory=True)
        for directory, children, _files in os.walk(base, followlinks=False):
            parent = Path(directory)
            for child in list(children):
                candidate = parent / child
                try:
                    _plain(candidate, directory=True)
                except (OSError, ValueError):
                    children.remove(child)
                    continue
                if child == "__pycache__":
                    children.remove(child)
                    yield candidate


def clean_project_caches(root: Path = PROJECT_ROOT, *, apply: bool = False) -> dict:
    root = Path(root).absolute()
    for ancestor in (root, *root.parents):
        _plain(ancestor, directory=True)
    result = {"apply": apply, "directories": [], "bytes": 0, "removed": 0, "deferred": []}
    for candidate in _cache_candidates(root):
        relative = candidate.relative_to(root).as_posix()
        try:
            _plain(candidate, directory=True)
            entries = _tree(candidate, candidate)
            files = [path for path, is_directory in entries if not is_directory]
            if candidate.name == "__pycache__" and any(path.suffix not in {".pyc", ".pyo"} for path in files):
                raise ValueError("缓存目录含非字节码文件，保留")
            size = sum(path.stat().st_size for path in files)
            if apply:
                shutil.rmtree(candidate)
                result["removed"] += 1
            result["directories"].append(relative)
            result["bytes"] += size
        except (OSError, ValueError, RecursionError):
            result["deferred"].append(relative)
    return result


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="预览或清理代码缓存及已登记的失效运行临时目录；保留业务材料、档案和数据库。")
    parser.add_argument("--apply", action="store_true", help="执行清理；默认只预览代码缓存")
    args = parser.parse_args(argv)
    result = clean_project_caches(apply=args.apply)
    if args.apply:
        result["abandoned_runs"] = cleanup_abandoned_temporaries(scope=PROJECT_ROOT / "worktrees")
    else:
        result["abandoned_runs"] = "执行时按归属登记和占用锁检查，不按目录年龄猜测"
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
