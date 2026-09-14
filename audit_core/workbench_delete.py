from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .workbench_store import WORKSPACE_ID_PATTERN, read_json_file


class RunDeletionConflict(ValueError):
    """The target cannot currently be deleted in its entirety."""


def _plain_path(path: Path, *, tree: bool = False) -> None:
    """Never traverse links, Windows junctions, or nested mount points."""
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(metadata.st_mode) or (
        getattr(metadata, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    ):
        raise RunDeletionConflict("删除范围包含链接或目录联接，请先人工处理")
    if stat.S_ISDIR(metadata.st_mode):
        if path.is_mount():
            raise RunDeletionConflict("不能删除挂载目录")
        if tree:
            for child in path.iterdir():
                _plain_path(child, tree=True)
    elif not stat.S_ISREG(metadata.st_mode):
        raise RunDeletionConflict("删除范围包含非常规文件，请先人工处理")


def _git(executable: str, cwd: Path, *args: str, check: bool = True):
    environment = os.environ.copy()
    for key in (
        "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    ):
        environment.pop(key, None)
    try:
        result = subprocess.run(
            [executable, "-c", "core.hooksPath=/dev/null", "-c", "gc.auto=0", "-C", str(cwd), *args],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RunDeletionConflict("Git 清理超时，未确认完整删除，请检查后重试") from exc
    if check and result.returncode:
        raise RunDeletionConflict("Git 清理失败：" + result.stderr.strip()[-500:])
    return result


def _git_records(value: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    current: dict[str, str] = {}
    for field in value.split("\0"):
        if not field:
            if current:
                records.append(current)
                current = {}
            continue
        key, _, item = field.partition(" ")
        current[key] = item
    if current:
        records.append(current)
    return records


@dataclass(frozen=True)
class GitWorktree:
    executable: str
    repository: Path
    workspace: Path
    branch: str | None
    head: str

    def remove(self) -> None:
        if self.branch:
            # Detach only the worktree being deleted. Remove its branch before
            # its registration so a branch-lock failure leaves a retryable run,
            # not an invisible orphan branch after a successful directory delete.
            _git(self.executable, self.workspace, "update-ref", "--no-deref", "HEAD", self.head, self.head)
            try:
                _git(self.executable, self.repository, "branch", "-D", "--", self.branch)
            except RunDeletionConflict:
                _git(self.executable, self.workspace, "symbolic-ref", "HEAD", f"refs/heads/{self.branch}")
                raise
        _git(self.executable, self.repository, "worktree", "remove", "--force", "--", str(self.workspace))
        records = _git_records(_git(self.executable, self.repository, "worktree", "list", "--porcelain", "-z").stdout)
        if any(Path(item["worktree"]).resolve() == self.workspace for item in records):
            raise RunDeletionConflict("Git worktree 登记尚未清理完成")


def _git_worktree(root: Path, workspace: Path) -> GitWorktree | None:
    repository = root.parent
    dotgit = workspace / ".git"
    executable = shutil.which("git")
    if not executable:
        if dotgit.is_file() or (repository / ".git" / "worktrees").exists():
            raise RunDeletionConflict("找不到 Git，无法确认并清理 worktree 登记")
        return None
    listing = _git(executable, repository, "worktree", "list", "--porcelain", "-z", check=False)
    if listing.returncode:
        if dotgit.is_file() or (repository / ".git").exists():
            raise RunDeletionConflict("无法确认当前项目的 Git worktree 登记，不能自动删除")
        return None
    records = _git_records(listing.stdout)
    matching = [item for item in records if Path(item["worktree"]).resolve() == workspace]
    if not matching:
        if dotgit.is_file():
            raise RunDeletionConflict("Git 登记与运行目录不一致，不能自动删除")
        return None
    item = matching[0]
    if len(matching) != 1 or item is records[0] or "locked" in item:
        raise RunDeletionConflict("不能删除 Git 主工作目录或已锁定的 worktree")
    branch = item.get("branch", "")
    if branch:
        if not branch.startswith("refs/heads/"):
            raise RunDeletionConflict("Git 分支不是本地独立分支")
        if branch in {"refs/heads/main", "refs/heads/master", "refs/heads/develop"} or any(
            other is not item and other.get("branch") == branch for other in records
        ):
            raise RunDeletionConflict("该记录使用共享或主干 Git 分支，不能完整自动删除")
    return GitWorktree(executable, repository, workspace, branch.removeprefix("refs/heads/") or None, item["HEAD"])


@dataclass(frozen=True)
class RunDeletionPlan:
    workspace_id: str
    workspace: Path
    legacy: Path
    review: Path
    jobs: tuple[tuple[Path, Path], ...]
    git_worktree: GitWorktree | None

    def execute(self) -> dict[str, Any]:
        # Keep the primary record until all auxiliary data can be removed. If an
        # OS file lock interrupts cleanup, the existing record remains retryable.
        for receipt, source in self.jobs:
            if source.exists():
                _remove_directory(source)
            _remove_file(receipt)
        _remove_file(self.review)
        _remove_file(self.legacy)
        if self.workspace.exists():
            # Keep a readable record for retries when Windows holds an open log
            # or another child cannot be removed. Delete manifest/snapshot last.
            for child in self.workspace.iterdir():
                if child.name in {"manifest.json", "snapshot.json"}:
                    continue
                if child.name == ".git" and self.git_worktree is not None:
                    continue
                if child.is_dir():
                    _remove_directory(child)
                else:
                    _remove_file(child)
            if self.git_worktree is not None:
                self.git_worktree.remove()
            else:
                _remove_file(self.workspace / "snapshot.json")
                _remove_file(self.workspace / "manifest.json")
                self.workspace.rmdir()
        remaining = [self.workspace, self.legacy, self.review]
        remaining.extend(path for pair in self.jobs for path in pair)
        if any(path.exists() for path in remaining):
            raise RunDeletionConflict("删除尚未全部完成，请检查文件占用后重试")
        return {"workspace_id": self.workspace_id, "deleted": True, "deleted_jobs": len(self.jobs)}


def _unlock_readonly(function, target, error) -> None:
    metadata = os.stat(target, follow_symlinks=False)
    if (
        os.name == "nt"
        and isinstance(error, PermissionError)
        and getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_READONLY
    ):
        os.chmod(target, stat.S_IREAD | stat.S_IWRITE)
        function(target)
    else:
        raise error


def _remove_file(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except PermissionError as exc:
        _unlock_readonly(os.unlink, path, exc)


def _remove_directory(path: Path) -> None:
    # onerror also supports the project's minimum Python 3.11 runtime.
    shutil.rmtree(path, onerror=lambda function, target, info: _unlock_readonly(function, target, info[1]))


def prepare_run_deletion(root: Path, workspace_id: str, *, input_root: Path) -> RunDeletionPlan:
    return _prepare_run_deletion(
        root,
        workspace_id,
        input_root=input_root,
        allow_active=False,
        associated_job_id=None,
    )


def _prepare_run_deletion(
    root: Path,
    workspace_id: str,
    *,
    input_root: Path,
    allow_active: bool,
    associated_job_id: str | None,
) -> RunDeletionPlan:
    if WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None:
        raise ValueError("运行 ID 不安全")
    root = root.resolve()
    workspace = root / workspace_id
    legacy = root / f"{workspace_id}.html"
    review_root = root / ".reviews"
    review = review_root / f"{workspace_id}.json"
    _plain_path(workspace, tree=True)
    _plain_path(legacy)
    _plain_path(review_root)
    _plain_path(review)
    if workspace.exists() and not workspace.is_dir():
        raise RunDeletionConflict("运行目录类型不正确")
    if any(path.exists() and not path.is_file() for path in (legacy, review)):
        raise RunDeletionConflict("运行档案或核验标记类型不正确")
    if not workspace.is_dir() and not legacy.is_file():
        raise FileNotFoundError(workspace_id)
    run_id = ""
    if workspace.is_dir():
        manifest = read_json_file(workspace / "manifest.json")
        if manifest.get("workspace_id") != workspace_id:
            raise RunDeletionConflict("运行标识与目录不一致")
        if not allow_active and manifest.get("status") not in {"completed", "failed"}:
            raise RunDeletionConflict("运行中的记录不能删除，请等待任务结束")
        run_id = str(manifest.get("run_id") or "")
    jobs_root = root / ".intake" / "jobs"
    _plain_path(jobs_root.parent)
    _plain_path(jobs_root)
    jobs: list[tuple[Path, Path]] = []
    input_root = input_root.absolute()
    if jobs_root.exists():
        for receipt in sorted(jobs_root.glob("*.json")):
            _plain_path(receipt)
            job = read_json_file(receipt)
            job_id = str(job.get("job_id") or "")
            if re.fullmatch(r"[a-f0-9]{24}", job_id) is None or receipt.name != f"{job_id}.json":
                raise RunDeletionConflict("关联投递任务标识不安全")
            if associated_job_id is not None and job_id != associated_job_id:
                continue
            if not isinstance(job.get("result") or {}, dict):
                raise RunDeletionConflict("投递任务结果记录格式不正确")
            result_id = str((job.get("result") or {}).get("workspace_id") or "")
            associated = result_id == workspace_id
            if not result_id and run_id and job.get("run_id") == run_id:
                # Failed early intake can lack a result receipt. Do not assign a
                # shared run-id receipt when a later retry owns another record.
                others = []
                for path in root.glob("*/manifest.json"):
                    if path.parent == workspace:
                        continue
                    _plain_path(path.parent)
                    _plain_path(path)
                    if read_json_file(path).get("run_id") == run_id:
                        others.append(path)
                if others:
                    raise RunDeletionConflict("投递任务对应多条运行，无法安全确定删除范围")
                associated = True
            if not associated:
                continue
            if not allow_active and job.get("status") not in {"completed", "failed"}:
                raise RunDeletionConflict("关联投递任务仍在运行或回调，暂不能删除")
            # Only the configured per-job directory is owned by this record;
            # never trust an absolute path found in a persisted receipt.
            _plain_path(input_root)
            source = input_root / job_id
            _plain_path(source, tree=True)
            if source.exists() and not source.is_dir():
                raise RunDeletionConflict("投递资料目录类型不正确")
            jobs.append((receipt, source))
    return RunDeletionPlan(workspace_id, workspace, legacy, review, tuple(jobs), _git_worktree(root, workspace))


@dataclass(frozen=True)
class IntakeJobDeletionPlan:
    job_id: str
    receipt: Path
    source: Path
    run_plan: RunDeletionPlan | None

    def execute(self) -> dict[str, Any]:
        if self.run_plan is not None:
            result = self.run_plan.execute()
            return {
                "job_id": self.job_id,
                "workspace_id": result["workspace_id"],
                "deleted": True,
            }
        if self.source.exists():
            _remove_directory(self.source)
        _remove_file(self.receipt)
        if self.source.exists() or self.receipt.exists():
            raise RunDeletionConflict("投递任务删除尚未全部完成，请检查文件占用后重试")
        return {"job_id": self.job_id, "workspace_id": None, "deleted": True}


def prepare_intake_job_deletion(
    root: Path,
    job_id: str,
    *,
    input_root: Path,
) -> IntakeJobDeletionPlan:
    """Resolve one intake attempt and only the run/input paths owned by it."""

    if re.fullmatch(r"[a-f0-9]{24}", job_id) is None:
        raise ValueError("投递任务 ID 不安全")
    root = root.resolve()
    receipt = root / ".intake" / "jobs" / f"{job_id}.json"
    _plain_path(receipt.parent.parent)
    _plain_path(receipt.parent)
    _plain_path(receipt)
    if not receipt.is_file():
        raise FileNotFoundError(job_id)
    job = read_json_file(receipt)
    if job.get("job_id") != job_id:
        raise RunDeletionConflict("投递任务标识与收据不一致")

    input_root = input_root.resolve()
    _plain_path(input_root)
    source = input_root / job_id
    _plain_path(source, tree=True)
    if source.exists() and not source.is_dir():
        raise RunDeletionConflict("投递资料目录类型不正确")

    result = job.get("result") or {}
    if not isinstance(result, dict):
        raise RunDeletionConflict("投递任务结果记录格式不正确")
    workspace_id = str(result.get("workspace_id") or "")
    if workspace_id:
        if WORKSPACE_ID_PATTERN.fullmatch(workspace_id) is None:
            raise RunDeletionConflict("投递任务关联的运行标识不安全")
    else:
        run_id = str(job.get("run_id") or "")
        job_created_at = str(job.get("created_at") or "")
        candidates: list[str] = []
        for manifest_path in root.glob("*/manifest.json"):
            workspace = manifest_path.parent
            _plain_path(workspace)
            _plain_path(manifest_path)
            manifest = read_json_file(manifest_path)
            if str(manifest.get("run_id") or "") != run_id:
                continue
            # A canceled runner may leave its manifest at an active stage. For a
            # completed manifest without a result receipt, only accept a workspace
            # created no earlier than this attempt so an old run-id collision can
            # never pull historical evidence into the deletion scope.
            manifest_created_at = str(manifest.get("created_at") or "")
            if manifest.get("status") in {"completed", "failed"} and (
                not job_created_at
                or not manifest_created_at
                or manifest_created_at < job_created_at
            ):
                continue
            candidate_id = workspace.name
            if WORKSPACE_ID_PATTERN.fullmatch(candidate_id) is None:
                raise RunDeletionConflict("投递任务候选运行目录名称不安全")
            candidates.append(candidate_id)
        if len(candidates) > 1:
            raise RunDeletionConflict("投递任务对应多条运行，无法安全确定删除范围")
        workspace_id = candidates[0] if candidates else ""

    run_plan = None
    if workspace_id:
        run_plan = _prepare_run_deletion(
            root,
            workspace_id,
            input_root=input_root,
            allow_active=True,
            associated_job_id=job_id,
        )
    return IntakeJobDeletionPlan(job_id, receipt, source, run_plan)
