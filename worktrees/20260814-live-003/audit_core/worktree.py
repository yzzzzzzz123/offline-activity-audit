from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .common import AuditError, clean_identifier, now_utc, sha256_file, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKTREES_ROOT = PROJECT_ROOT / "worktrees"
BRANCH_PREFIX = "run/offline-audit/"
SUPERVISOR_ACTOR = "trusted_supervisor"
MAX_UNTRACKED_FILE_BYTES = 25 * 1024 * 1024

SENSITIVE_DIRECTORY_NAMES = {".aws", ".gnupg", ".ssh", "private-keys"}
SENSITIVE_EXACT_FILENAMES = {
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "service-account.json",
    "token.json",
    "tokens.json",
}
SENSITIVE_SUFFIXES = {".jks", ".key", ".p12", ".pem", ".pfx"}
SAFE_ENV_SUFFIXES = {".example", ".sample", ".template"}


class WorktreeError(AuditError):
    """Raised when a linked-worktree lifecycle gate fails."""


def _git_process(
    args: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise WorktreeError(f"git {' '.join(args)} failed: {detail}")
    return completed


def run_git(
    args: list[str],
    *,
    cwd: Path = PROJECT_ROOT,
    env: dict[str, str] | None = None,
    check: bool = True,
) -> str:
    return _git_process(args, cwd=cwd, env=env, check=check).stdout.strip()


def _canonical_sha256(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def ensure_repository() -> Path:
    if not (PROJECT_ROOT / ".git").is_dir():
        raise WorktreeError(
            "Worktree allocation must be started from the primary Git worktree"
        )
    completed = _git_process(
        ["rev-parse", "--show-toplevel"],
        check=False,
    )
    if completed.returncode != 0:
        raise WorktreeError(
            "The project root must be an initialized Git repository before a formal run"
        )
    root = Path(completed.stdout.strip()).resolve()
    if root != PROJECT_ROOT.resolve():
        raise WorktreeError(f"Git root mismatch: expected {PROJECT_ROOT}, got {root}")
    if not run_git(["rev-parse", "--verify", "HEAD"], check=False):
        raise WorktreeError("The Git repository must contain a baseline commit")
    return root


def _git_common_dir(*, cwd: Path = PROJECT_ROOT) -> Path:
    raw = Path(run_git(["rev-parse", "--git-common-dir"], cwd=cwd))
    if not raw.is_absolute():
        raw = cwd / raw
    return raw.resolve()


@contextmanager
def allocation_lock() -> Iterator[None]:
    common_dir = _git_common_dir()
    lock_path = common_dir / "offline-audit-worktree.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.05)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _registered_worktrees() -> dict[Path, dict[str, str]]:
    output = run_git(["worktree", "list", "--porcelain"])
    records: dict[Path, dict[str, str]] = {}
    current: dict[str, str] = {}
    for line in [*output.splitlines(), ""]:
        if not line:
            if current.get("worktree"):
                records[Path(current["worktree"]).resolve()] = dict(current)
            current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    return records


def _untracked_paths() -> list[str]:
    completed = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise WorktreeError(f"Unable to inspect untracked files: {detail}")
    return sorted(
        value.decode("utf-8", errors="replace")
        for value in completed.stdout.split(b"\0")
        if value
    )


def _looks_sensitive(relative_path: str) -> bool:
    parts = [part.casefold() for part in Path(relative_path).parts]
    if not parts:
        return False
    if any(part in SENSITIVE_DIRECTORY_NAMES for part in parts[:-1]):
        return True
    filename = parts[-1]
    if filename in SENSITIVE_EXACT_FILENAMES:
        return True
    if filename in {".env", ".envrc"}:
        return True
    if filename.startswith(".env.") and not any(
        filename.endswith(suffix) for suffix in SAFE_ENV_SUFFIXES
    ):
        return True
    if filename.startswith("client_secret"):
        return True
    if filename.endswith(("-credentials.json", "_credentials.json")):
        return True
    return any(filename.endswith(suffix) for suffix in SENSITIVE_SUFFIXES)


def _guard_untracked(paths: list[str]) -> dict[str, Any]:
    blocked: list[str] = []
    unsafe_links: list[str] = []
    oversized: list[str] = []
    for relative in paths:
        source = PROJECT_ROOT / relative
        if _looks_sensitive(relative):
            blocked.append(relative)
        if source.is_symlink():
            unsafe_links.append(relative)
        elif source.is_file() and source.stat().st_size > MAX_UNTRACKED_FILE_BYTES:
            oversized.append(relative)
    if blocked or unsafe_links or oversized:
        details = {
            "sensitive": blocked,
            "symlinks": unsafe_links,
            "oversized": oversized,
        }
        raise WorktreeError(
            "Refusing to snapshot unsafe untracked inputs: "
            + json.dumps(details, ensure_ascii=False)
        )
    return {
        "status": "passed",
        "untracked_count": len(paths),
        "sensitive_count": 0,
        "symlink_count": 0,
        "oversized_count": 0,
    }


def _build_snapshot_tree() -> str:
    with tempfile.TemporaryDirectory(prefix="offline-audit-index-") as directory:
        environment = dict(os.environ)
        environment["GIT_INDEX_FILE"] = str(Path(directory) / "index")
        run_git(["read-tree", "HEAD"], env=environment)
        run_git(["add", "-A", "--", "."], env=environment)
        return run_git(["write-tree"], env=environment)


def _workspace_state() -> dict[str, Any]:
    untracked = _untracked_paths()
    gate = _guard_untracked(untracked)
    status_text = run_git(["status", "--porcelain=v1", "--untracked-files=all"])
    branch = run_git(["symbolic-ref", "--quiet", "--short", "HEAD"], check=False)
    state: dict[str, Any] = {
        "head": run_git(["rev-parse", "HEAD"]),
        "branch": branch or "DETACHED",
        "index_tree": run_git(["write-tree"]),
        "snapshot_tree": _build_snapshot_tree(),
        "status_sha256": _sha256_text(status_text),
        "dirty": bool(status_text),
        "untracked_gate": gate,
    }
    state["state_sha256"] = _canonical_sha256(state)
    return state


def normalize_run_id(raw: str) -> str:
    value = clean_identifier(raw)
    if value != raw.strip():
        raise WorktreeError(
            "run-id may contain only letters, numbers, Chinese characters, dot, dash, or underscore"
        )
    if value in {".", ".."} or not value:
        raise WorktreeError("run-id is invalid")
    return value


def _identity(run_id: str) -> dict[str, Any]:
    normalized = normalize_run_id(run_id)
    root = WORKTREES_ROOT.resolve()
    target = (root / normalized).resolve()
    if target.parent != root or target == PROJECT_ROOT.resolve():
        raise WorktreeError("Managed worktree path escaped the worktrees root")
    return {
        "run_id": normalized,
        "branch": f"{BRANCH_PREFIX}{normalized}",
        "path": target,
    }


def _branch_exists(branch: str) -> bool:
    completed = _git_process(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"],
        check=False,
    )
    if completed.returncode not in {0, 1}:
        detail = (completed.stderr or completed.stdout).strip()
        raise WorktreeError(f"Unable to inspect branch {branch}: {detail}")
    return completed.returncode == 0


def _assert_available(run_id: str) -> dict[str, Any]:
    identity = _identity(run_id)
    target = identity["path"]
    branch = str(identity["branch"])
    if WORKTREES_ROOT.is_symlink():
        raise WorktreeError("worktrees root cannot be a symbolic link")
    if target.exists() or target.is_symlink():
        raise WorktreeError(f"Run worktree already exists: {target}")
    if target in _registered_worktrees():
        raise WorktreeError(f"Run worktree is still registered: {target}")
    if _branch_exists(branch):
        raise WorktreeError(f"Run branch already exists: {branch}")
    return identity


def _preflight_unlocked(run_id: str) -> dict[str, Any]:
    ensure_repository()
    identity = _assert_available(run_id)
    main_workspace = _workspace_state()
    value: dict[str, Any] = {
        "schema_version": "1.0",
        "action": "allocate_worktree",
        "required_actor": SUPERVISOR_ACTOR,
        "run_id": identity["run_id"],
        "branch": identity["branch"],
        "worktree": str(identity["path"]),
        "main_workspace": main_workspace,
        "gates": {
            "repository": "passed",
            "identity_available": "passed",
            "untracked_input_safety": "passed",
            "isolated_index_snapshot": "passed",
        },
    }
    value["preflight_sha256"] = _canonical_sha256(value)
    return value


def preflight(run_id: str) -> dict[str, Any]:
    with allocation_lock():
        return _preflight_unlocked(run_id)


def _create_snapshot_commit(run_id: str, snapshot_tree: str) -> str:
    parent = run_git(["rev-parse", "HEAD"])
    return run_git(
        [
            "-c",
            "user.name=Offline Audit Runtime",
            "-c",
            "user.email=offline-audit@local.invalid",
            "commit-tree",
            snapshot_tree,
            "-p",
            parent,
            "-m",
            f"snapshot offline audit {run_id}",
        ]
    )


def _cleanup_new_allocation(target: Path, branch: str) -> None:
    run_git(["worktree", "remove", "--force", str(target)], check=False)
    run_git(["branch", "-D", branch], check=False)


def create_worktree(
    *,
    run_id: str,
    expected_preflight_sha256: str,
    requested_by: str = SUPERVISOR_ACTOR,
) -> dict[str, Any]:
    if requested_by != SUPERVISOR_ACTOR:
        raise WorktreeError(f"Allocation is restricted to {SUPERVISOR_ACTOR}")
    if not re.fullmatch(r"[0-9a-f]{64}", expected_preflight_sha256):
        raise WorktreeError("A valid preflight SHA-256 is required")
    with allocation_lock():
        current = _preflight_unlocked(run_id)
        if current["preflight_sha256"] != expected_preflight_sha256:
            raise WorktreeError("Preflight is stale; the main workspace changed")
        target = Path(str(current["worktree"])).resolve()
        branch = str(current["branch"])
        before = dict(current["main_workspace"])
        snapshot_commit = _create_snapshot_commit(
            str(current["run_id"]),
            str(before["snapshot_tree"]),
        )
        WORKTREES_ROOT.mkdir(parents=True, exist_ok=True)
        run_git(["branch", branch, snapshot_commit])
        try:
            run_git(
                [
                    "worktree",
                    "add",
                    "--relative-paths",
                    str(target),
                    branch,
                ]
            )
            after = _workspace_state()
        except Exception:
            _cleanup_new_allocation(target, branch)
            raise
        if after["state_sha256"] != before["state_sha256"]:
            _cleanup_new_allocation(target, branch)
            raise WorktreeError(
                "Main workspace changed during allocation; the new worktree was rolled back"
            )
    return {
        "schema_version": "1.0",
        "action": "worktree_allocated",
        "requested_by": requested_by,
        "run_id": current["run_id"],
        "branch": branch,
        "path": str(target),
        "snapshot_commit": snapshot_commit,
        "snapshot_tree": before["snapshot_tree"],
        "preflight_sha256": expected_preflight_sha256,
        "main_workspace_state_sha256": before["state_sha256"],
    }


def verify_worktree(worktree: str | Path, run_id: str) -> dict[str, str]:
    identity = _identity(run_id)
    target = Path(worktree).resolve()
    if target != identity["path"] or target == PROJECT_ROOT.resolve():
        raise WorktreeError("Worktree identity does not match the managed run")
    if target.is_symlink() or not target.is_dir():
        raise WorktreeError(f"Managed worktree does not exist: {target}")
    registration = _registered_worktrees().get(target)
    if registration is None or "prunable" in registration:
        raise WorktreeError("Managed worktree is not one live Git registration")
    actual_branch = registration.get("branch", "").removeprefix("refs/heads/")
    if actual_branch != identity["branch"]:
        raise WorktreeError("Registered worktree branch differs from the run identity")
    head = run_git(["rev-parse", "HEAD"], cwd=target)
    branch_tip = run_git(["rev-parse", f"refs/heads/{identity['branch']}"])
    if head != branch_tip:
        raise WorktreeError("Worktree HEAD differs from its branch tip")
    return {
        "run_id": str(identity["run_id"]),
        "branch": str(identity["branch"]),
        "path": str(target),
        "head": head,
    }


def checkpoint(worktree: str | Path, run_id: str, message: str) -> dict[str, Any]:
    target = Path(worktree).resolve()
    verified = verify_worktree(target, run_id)
    output_root = target / "output"
    if not output_root.is_dir():
        raise WorktreeError("Formal run produced no output directory")
    run_git(["add", "-A", "--", "output"], cwd=target)
    diff = _git_process(["diff", "--cached", "--quiet"], cwd=target, check=False)
    if diff.returncode not in {0, 1}:
        detail = (diff.stderr or diff.stdout).strip()
        raise WorktreeError(f"Unable to inspect staged output: {detail}")
    if diff.returncode == 1:
        run_git(
            [
                "-c",
                "user.name=Offline Audit Runtime",
                "-c",
                "user.email=offline-audit@local.invalid",
                "commit",
                "-m",
                message,
            ],
            cwd=target,
        )
    head = run_git(["rev-parse", "HEAD"], cwd=target)
    status_text = run_git(["status", "--porcelain=v1", "--untracked-files=all"], cwd=target)
    if status_text:
        raise WorktreeError("Checkpoint completed but the owned worktree is not clean")
    return {
        **verified,
        "action": "checkpoint_completed",
        "checkpoint_commit": head,
        "worktree_clean": True,
    }


def worktree_status(run_id: str) -> dict[str, Any]:
    identity = _identity(run_id)
    verified = verify_worktree(identity["path"], run_id)
    status_text = run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=identity["path"],
    )
    output_root = identity["path"] / "output"
    delivery = output_root / "delivery.json"
    return {
        **verified,
        "status": "clean" if not status_text else "dirty",
        "output": str(output_root) if output_root.exists() else None,
        "delivery": str(delivery) if delivery.is_file() else None,
    }


def list_worktrees() -> list[dict[str, Any]]:
    ensure_repository()
    root = WORKTREES_ROOT.resolve()
    result: list[dict[str, Any]] = []
    for path, record in _registered_worktrees().items():
        if path == PROJECT_ROOT.resolve() or path.parent != root:
            continue
        run_id = path.name
        expected_branch = f"{BRANCH_PREFIX}{run_id}"
        branch = record.get("branch", "").removeprefix("refs/heads/")
        if branch != expected_branch:
            continue
        status_text = run_git(
            ["status", "--porcelain=v1", "--untracked-files=all"],
            cwd=path,
        )
        delivery = path / "output" / "delivery.json"
        result.append(
            {
                "run_id": run_id,
                "branch": branch,
                "path": str(path),
                "head": record.get("HEAD"),
                "status": "clean" if not status_text else "dirty",
                "delivery": str(delivery) if delivery.is_file() else None,
            }
        )
    return sorted(result, key=lambda item: str(item["run_id"]))


def remove_worktree(run_id: str, confirm_run_id: str) -> dict[str, Any]:
    if confirm_run_id != run_id:
        raise WorktreeError("Removal requires --confirm-run-id to equal --run-id")
    identity = _identity(run_id)
    target = identity["path"]
    verified = verify_worktree(target, run_id)
    status_text = run_git(
        ["status", "--porcelain=v1", "--untracked-files=all"],
        cwd=target,
    )
    if status_text:
        raise WorktreeError("Refusing to remove an unclean owned worktree")
    branch_tip = run_git(["rev-parse", f"refs/heads/{identity['branch']}"])
    if branch_tip != verified["head"]:
        raise WorktreeError("Refusing removal because the worktree and branch tips differ")
    run_git(["worktree", "remove", str(target)])
    if target in _registered_worktrees():
        raise WorktreeError("Git worktree registration remains after removal")
    run_git(["branch", "-D", str(identity["branch"])])
    if _branch_exists(str(identity["branch"])):
        raise WorktreeError("Run branch remains after removal")
    if target.exists() or target.is_symlink():
        raise WorktreeError("Managed worktree path remains after removal")
    return {
        "schema_version": "1.0",
        "action": "worktree_removed",
        "run_id": run_id,
        "branch": identity["branch"],
        "path": str(target),
        "removed_head": branch_tip,
        "recoverable_from_git_reflog_until_pruned": True,
    }


def _is_inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def export_output(
    *,
    worktree: str | Path,
    run_id: str,
    output_dir: str | Path,
    checkpoint_commit: str,
) -> dict[str, Any]:
    target_worktree = Path(worktree).resolve()
    verify_worktree(target_worktree, run_id)
    source = target_worktree / "output"
    if not source.is_dir():
        raise WorktreeError("Cannot export a run without an output directory")
    export_root = Path(output_dir).resolve()
    if _is_inside(export_root, PROJECT_ROOT.resolve()):
        raise WorktreeError("Export directory must be outside the project Git repository")
    destination = export_root / run_id
    if destination.exists() or destination.is_symlink():
        raise WorktreeError(f"Export target already exists: {destination}")
    export_root.mkdir(parents=True, exist_ok=True)
    workbooks = sorted(source.glob("*.xlsx"), key=lambda value: value.name.casefold())
    if len(workbooks) != 1:
        raise WorktreeError(
            f"External delivery requires exactly one top-level Excel workbook; found {len(workbooks)}"
        )
    destination.mkdir(parents=True)
    exported_workbook = destination / workbooks[0].name
    shutil.copy2(workbooks[0], exported_workbook)
    files = [
        {
            "path": exported_workbook.name,
            "bytes": exported_workbook.stat().st_size,
            "sha256": sha256_file(exported_workbook),
        }
    ]
    receipt = {
        "schema_version": "1.0",
        "action": "output_exported",
        "run_id": run_id,
        "source_worktree": str(target_worktree),
        "checkpoint_commit": checkpoint_commit,
        "exported_at": now_utc(),
        "export_dir": str(destination),
        "file_count": len(files),
        "files": files,
    }
    return receipt
