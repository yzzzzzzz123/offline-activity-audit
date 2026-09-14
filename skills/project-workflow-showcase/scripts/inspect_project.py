#!/usr/bin/env python3
"""Emit a bounded, read-only inventory for a project workflow showcase."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".idea",
    ".vscode",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".runtime",
    ".audit-tmp",
    "__pycache__",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "vendor",
    "coverage",
    "dist",
    "build",
    "target",
    ".next",
    ".nuxt",
    "input",
    "inputs",
    "input-oss",
    "output",
    "outputs",
    "result",
    "results",
    "logs",
    "worktrees",
}

SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
SENSITIVE_TOKENS = {"credential", "secret", "token", "private-key", "private_key"}

TEXT_SUFFIXES = {
    ".md", ".txt", ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go",
    ".rs", ".java", ".kt", ".rb", ".php", ".cs", ".swift", ".html", ".css",
    ".scss", ".sql", ".sh", ".ps1",
}

LANGUAGE_BY_SUFFIX = {
    ".py": "Python", ".js": "JavaScript", ".mjs": "JavaScript",
    ".cjs": "JavaScript", ".ts": "TypeScript", ".tsx": "TypeScript/React",
    ".jsx": "JavaScript/React", ".go": "Go", ".rs": "Rust", ".java": "Java",
    ".kt": "Kotlin", ".rb": "Ruby", ".php": "PHP", ".cs": "C#",
    ".swift": "Swift", ".html": "HTML", ".css": "CSS", ".scss": "SCSS",
    ".sql": "SQL", ".sh": "Shell", ".ps1": "PowerShell",
}

MANIFEST_NAMES = {
    "pyproject.toml", "requirements.txt", "package.json", "pnpm-workspace.yaml",
    "poetry.lock", "uv.lock", "go.mod", "Cargo.toml", "Gemfile", "composer.json",
    "pom.xml", "build.gradle", "docker-compose.yml", "docker-compose.yaml",
    "Dockerfile", "Makefile", "justfile",
}

ENTRYPOINT_NAMES = {
    "main.py", "app.py", "cli.py", "server.py", "worker.py", "runner.py",
    "index.js", "index.ts", "main.js", "main.ts", "manage.py",
}

SIGNAL_PATTERNS = {
    "agent_orchestration": re.compile(r"\b(orchestrator|multi[-_ ]agent|agent runner|broker)\b|总控|编排", re.I),
    "api_or_webhook": re.compile(r"\b(api|webhook|callback|route|endpoint|http server)\b|接口|回调", re.I),
    "contracts_or_schemas": re.compile(r"\b(schema|contract|validator|validation)\b|合同|校验", re.I),
    "state_or_queue": re.compile(r"\b(state machine|sqlite|queue|job|checkpoint|resume)\b|状态|队列|恢复", re.I),
    "human_gate": re.compile(r"\b(human|manual)[-_ ]?(approval|review|takeover)\b|人工(审批|复核|接管)", re.I),
    "security_boundary": re.compile(r"\b(secret|credential|sandbox|allowlist|denylist|read[- ]only)\b|只读|白名单|凭据", re.I),
    "browser_ui": re.compile(r"\b(playwright|frontend|browser|html|css|javascript)\b|前端|页面", re.I),
}


def is_sensitive(path: Path) -> bool:
    name = path.name.lower()
    return (
        name == ".env"
        or name.startswith(".env.")
        or path.suffix.lower() in SENSITIVE_SUFFIXES
        or any(token in name for token in SENSITIVE_TOKENS)
    )


def relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def path_is_skipped(path: Path, root: Path) -> bool:
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        return True
    return any(part in SKIP_DIRS for part in parts[:-1]) or is_sensitive(path)


def run_git(root: Path, args: list[str], timeout: int = 10) -> tuple[int, bytes]:
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), *args],
            check=False,
            capture_output=True,
            timeout=timeout,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return 127, b""
    return completed.returncode, completed.stdout


def decode_git(raw: bytes) -> str:
    return raw.decode("utf-8", errors="replace").strip()


def git_files(root: Path, max_files: int) -> tuple[list[Path], bool] | None:
    code, raw = run_git(root, ["ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "."])
    if code != 0:
        return None
    files: list[Path] = []
    for raw_rel in raw.split(b"\0"):
        if not raw_rel:
            continue
        rel = raw_rel.decode("utf-8", errors="replace")
        candidate = (root / rel).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if path_is_skipped(candidate, root) or candidate.is_symlink() or not candidate.is_file():
            continue
        files.append(candidate)
        if len(files) >= max_files:
            return sorted(files), True
    return sorted(files), False


def walk_files(root: Path, max_files: int) -> tuple[list[Path], bool]:
    files: list[Path] = []
    for current, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(
            name for name in dirs
            if name not in SKIP_DIRS and not Path(current, name).is_symlink()
        )
        for name in sorted(names):
            path = Path(current, name)
            if path.is_symlink() or not path.is_file() or is_sensitive(path):
                continue
            files.append(path.resolve())
            if len(files) >= max_files:
                return files, True
    return files, False


def list_files(root: Path, max_files: int) -> tuple[list[Path], bool, str]:
    from_git = git_files(root, max_files)
    if from_git is not None:
        files, truncated = from_git
        return files, truncated, "git tracked + non-ignored untracked files"
    files, truncated = walk_files(root, max_files)
    return files, truncated, "filesystem walk"


def safe_read(path: Path, byte_limit: int) -> str:
    if is_sensitive(path):
        return ""
    try:
        raw = path.read_bytes()[:byte_limit]
    except OSError:
        return ""
    if b"\x00" in raw:
        return ""
    return raw.decode("utf-8", errors="ignore")


def is_test(rel: str, name: str) -> bool:
    parts = rel.lower().split("/")
    lower = name.lower()
    return (
        any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts)
        or lower.startswith("test_")
        or lower.endswith(("_test.py", ".test.ts", ".test.js", ".spec.ts", ".spec.js"))
    )


def candidate_reason(rel: str, path: Path) -> tuple[int, str] | None:
    name = path.name
    lower = name.lower()
    parts = rel.lower().split("/")
    if name == "AGENTS.md":
        return 1000, "applicable instructions"
    if lower.startswith("readme"):
        return 950, "project orientation"
    if name == "SKILL.md":
        return 925, "agent workflow entrypoint"
    if name in MANIFEST_NAMES:
        return 900, "build or dependency manifest"
    if any(token in lower for token in ("architecture", "workflow", "orchestrat", "contract", "policy")):
        return 850, "architecture, workflow, or control contract"
    if name in ENTRYPOINT_NAMES:
        return 825, "likely runtime entrypoint"
    if any(part in {"contracts", "schemas", "policies"} for part in parts):
        return 775, "machine-readable boundary or policy"
    if lower.endswith((".schema.json", ".contract.json")):
        return 760, "machine-readable contract"
    if any(token in lower for token in ("validator", "promoter", "gateway", "router", "intake")):
        return 725, "gate or integration boundary"
    if is_test(rel, name):
        return 650, "behavioral verification"
    if any(part in {"docs", "documentation"} for part in parts) and path.suffix.lower() in TEXT_SUFFIXES:
        return 600, "focused documentation"
    return None


def top_level(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    try:
        children: Iterable[Path] = sorted(root.iterdir(), key=lambda item: item.name.lower())
    except OSError:
        return entries
    for child in children:
        if child.name in SKIP_DIRS or is_sensitive(child):
            continue
        entries.append({"name": child.name, "kind": "directory" if child.is_dir() else "file"})
        if len(entries) >= 80:
            break
    return entries


def git_summary(root: Path) -> dict[str, object]:
    code, repo_root_raw = run_git(root, ["rev-parse", "--show-toplevel"])
    if code != 0:
        return {"is_repository": False}
    _, branch_raw = run_git(root, ["branch", "--show-current"])
    _, head_raw = run_git(root, ["rev-parse", "--short=12", "HEAD"])
    status_code, status_raw = run_git(root, ["status", "--short", "--untracked-files=normal", "--", "."])
    status_lines = decode_git(status_raw).splitlines() if status_code == 0 and status_raw else []
    safe_status = []
    for line in status_lines:
        rel = line[3:].strip().strip('"') if len(line) > 3 else ""
        if rel and not is_sensitive(root / rel):
            safe_status.append(line)
    return {
        "is_repository": True,
        "repository_root": decode_git(repo_root_raw),
        "branch": decode_git(branch_raw) or "(detached or unborn)",
        "head": decode_git(head_raw) or None,
        "dirty_entry_count": len(safe_status),
        "status_sample": safe_status[:24],
        "status_sample_truncated": len(safe_status) > 24,
    }


def inspect(root: Path, max_files: int) -> dict[str, object]:
    files, truncated, source = list_files(root, max_files)
    suffix_counts: Counter[str] = Counter()
    language_counts: Counter[str] = Counter()
    module_counts: Counter[str] = Counter()
    categories: dict[str, list[str]] = defaultdict(list)
    candidates: list[tuple[int, str, str, int]] = []
    signal_evidence: dict[str, list[str]] = defaultdict(list)
    content_budget = 3_000_000

    for path in files:
        rel = relative(path, root)
        suffix = path.suffix.lower() or "[no extension]"
        suffix_counts[suffix] += 1
        if suffix in LANGUAGE_BY_SUFFIX:
            language_counts[LANGUAGE_BY_SUFFIX[suffix]] += 1
        first_part = rel.split("/", 1)[0]
        module_counts[first_part] += 1

        name = path.name
        lower = name.lower()
        if name == "AGENTS.md":
            categories["instructions"].append(rel)
        if lower.startswith("readme"):
            categories["readmes"].append(rel)
        if name == "SKILL.md":
            categories["skills"].append(rel)
        if name in MANIFEST_NAMES:
            categories["manifests"].append(rel)
        if name in ENTRYPOINT_NAMES:
            categories["entrypoints"].append(rel)
        if is_test(rel, name):
            categories["tests"].append(rel)
        if lower.endswith((".schema.json", ".contract.json")) or "schema" in rel.lower().split("/"):
            categories["schemas_and_contracts"].append(rel)

        reason = candidate_reason(rel, path)
        if reason is not None:
            score, label = reason
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            candidates.append((score, rel, label, size))

    for _, rel, _, _ in sorted(candidates, key=lambda item: (-item[0], item[1])):
        if content_budget <= 0:
            break
        path = root / rel
        read_limit = min(96_000, content_budget)
        content = safe_read(path, read_limit)
        if not content:
            continue
        content_budget -= len(content.encode("utf-8", errors="ignore"))
        for signal, pattern in SIGNAL_PATTERNS.items():
            if pattern.search(content) and len(signal_evidence[signal]) < 12:
                signal_evidence[signal].append(rel)

    return {
        "root": str(root),
        "git": git_summary(root),
        "inventory_source": source,
        "top_level_entries": top_level(root),
        "module_file_counts": dict(module_counts.most_common(20)),
        "languages_by_file_count": dict(language_counts.most_common(12)),
        "source_candidates": [
            {"path": rel, "reason": reason, "bytes": size}
            for _, rel, reason, size in sorted(candidates, key=lambda item: (-item[0], item[1]))[:160]
        ],
        "candidate_groups": {
            key: values[:50] for key, values in sorted(categories.items()) if values
        },
        "content_signals": dict(sorted(signal_evidence.items())),
        "scan": {
            "files_seen": len(files),
            "max_files": max_files,
            "truncated": truncated,
            "content_budget_exhausted": content_budget <= 0,
            "excluded_runtime_or_business_data_roots": sorted(
                {
                    ".audit-tmp",
                    ".runtime",
                    "input",
                    "input-oss",
                    "inputs",
                    "logs",
                    "output",
                    "outputs",
                    "result",
                    "results",
                    "worktrees",
                }
            ),
            "top_suffixes": dict(suffix_counts.most_common(15)),
        },
        "interpretation_note": (
            "This inventory is navigation evidence only. Read each cited file before "
            "turning a path, count, or content signal into a showcase claim."
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect a project read-only and emit bounded showcase navigation evidence."
    )
    parser.add_argument("--root", required=True, type=Path, help="Project directory")
    parser.add_argument("--max-files", type=int, default=8000, help="Maximum files to inventory")
    parser.add_argument("--pretty", action="store_true", help="Indent JSON output")
    args = parser.parse_args()

    root = args.root.expanduser().resolve()
    if not root.is_dir():
        parser.error(f"project root is not a directory: {root}")
    if args.max_files < 1:
        parser.error("--max-files must be positive")

    json.dump(inspect(root, args.max_files), sys.stdout, ensure_ascii=False, indent=2 if args.pretty else None)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
