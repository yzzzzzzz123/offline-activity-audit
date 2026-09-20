"""任务临时空间：正常退出清理，进程中断后由下次正式运行补清理。"""
from __future__ import annotations

from .paths import PROJECT_ROOT

from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tempfile


_DEFAULT_SCOPE = PROJECT_ROOT / "worktrees"
_SCOPE: ContextVar[Path] = ContextVar("audit_temporary_scope", default=_DEFAULT_SCOPE)
_KINDS = {"render", "product-images"}
_OWNER = "owner.json"
_LEASE = "lease.lock"


def current_temporary_scope() -> Path:
    return _SCOPE.get()


def _scope_hash(scope: Path) -> str:
    return hashlib.sha256(os.path.normcase(str(scope.resolve())).encode("utf-8")).hexdigest()[:12]


def _plain(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or (not directory and info.st_nlink != 1)
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
        raise ValueError("临时目录含链接或非普通文件，保留待核查")
    return info


def _temp_root(value: Path | None) -> Path:
    root = Path(value if value is not None else tempfile.gettempdir()).absolute()
    # 检查全部祖先后才解析，不能让 junction 将清理目标导向业务目录。
    for parent in (root, *root.parents):
        _plain(parent, directory=True)
    return root.resolve(strict=True)


def _validate_container(container: Path, root: Path, scope: Path) -> dict:
    prefix = "oa-" + _scope_hash(scope) + "-"
    if not re.fullmatch(re.escape(prefix) + r"[a-z0-9_]{8}", container.name):
        raise ValueError("不是本项目登记的临时目录")
    _plain(container, directory=True)
    if container.resolve(strict=True).parent != root:
        raise ValueError("临时目录越界")
    marker = container / _OWNER
    if _plain(marker, directory=False).st_size > 2048:
        raise ValueError("临时目录登记信息过大")
    owner = json.loads(marker.read_text(encoding="utf-8"))
    if (not isinstance(owner, dict) or owner.get("version") != 1
            or owner.get("scope") != _scope_hash(scope)
            or owner.get("container") != container.name or owner.get("kind") not in _KINDS
            or type(owner.get("pid")) is not int or owner["pid"] <= 0):
        raise ValueError("临时目录归属无法确认")
    if set(child.name for child in container.iterdir()) - {_OWNER, _LEASE, "data"}:
        raise ValueError("临时目录含未登记的顶层文件")
    return owner


def _lock(path: Path):
    # 锁由操作系统持有，跨进程/线程有效；断电和强杀会释放。PID 仅用于登记，
    # 不凭 PID 或目录年龄删除，因此 PID 复用、长时任务都不会被误判为过期。
    try:
        _plain(path, directory=False)
    except FileNotFoundError:
        with path.open("xb") as output:
            output.write(b"0")
    stream = path.open("r+b")
    try:
        before = _plain(path, directory=False)
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("临时目录占用锁已变化")
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return stream
    except BaseException:
        stream.close()
        raise


def _process_uses(container: Path) -> bool:
    """强杀宿主后，仍引用该材料路径的模型子进程也要保护；检查失败时保留。"""
    if os.name == "nt":
        environment = os.environ.copy()
        environment["OFFLINE_AUDIT_TEMP_CHECK_PATH"] = str(container).lower()
        # 仅返回布尔值；命令行及其中可能存在的认证内容不离开此只读检查进程。
        script = (
            "$ErrorActionPreference='Stop';"
            "$needle=$env:OFFLINE_AUDIT_TEMP_CHECK_PATH;"
            "$busy=$false;"
            "foreach($item in (Get-CimInstance Win32_Process -Property ProcessId,CommandLine)){"
            "if($item.ProcessId -ne $PID -and $item.CommandLine -and "
            "$item.CommandLine.ToLowerInvariant().Replace('\\\\','\\').Contains($needle))"
            "{$busy=$true;break}};"
            "if($busy){'busy'}else{'free'}"
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
            env=environment, capture_output=True, text=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode != 0 or result.stdout.strip() != "free"
    proc = Path("/proc")
    if not proc.is_dir():
        return True
    for entry in proc.iterdir():
        if not entry.name.isdigit() or int(entry.name) == os.getpid():
            continue
        try:
            if entry.stat().st_uid != os.getuid():
                continue
            if str(container).encode() in (entry / "cmdline").read_bytes():
                return True
            if (entry / "cwd").resolve(strict=True).is_relative_to(container):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True
    return False


def _tree(path: Path, boundary: Path) -> list[tuple[Path, bool]]:
    _plain(path, directory=True)
    if path.resolve(strict=True) != boundary:
        raise ValueError("临时材料目录越界")
    entries: list[tuple[Path, bool]] = []

    def visit(directory: Path) -> None:
        for child in directory.iterdir():
            info = child.lstat()
            is_dir = stat.S_ISDIR(info.st_mode)
            _plain(child, directory=is_dir)
            if not child.resolve(strict=True).is_relative_to(boundary):
                raise ValueError("临时文件越界")
            if is_dir:
                visit(child)
            entries.append((child, is_dir))
        if len(entries) > 100000:
            raise ValueError("临时文件过多，保留待核查")

    visit(path)
    entries.append((path, True))
    return entries


def _remove_data(container: Path) -> None:
    data = container / "data"
    try:
        data.lstat()
    except FileNotFoundError:
        return
    # 全树先验证后删除；逐项再次检查祖先和类型，不跟随链接，不修改源文件 ACL。
    entries = _tree(data, container / "data")
    for path, directory in entries:
        for parent in path.parents:
            if parent == container:
                break
            _plain(parent, directory=True)
        _plain(path, directory=directory)
        if directory:
            path.rmdir()
        else:
            path.unlink()


def _remove_registration(container: Path) -> None:
    # 图片已删除后才释放锁/删除登记；如文件锁阻止删除数据，登记必须完整留存。
    if set(child.name for child in container.iterdir()) - {_OWNER, _LEASE}:
        raise ValueError("临时空间仍有未清理数据，保留登记")
    (container / _LEASE).unlink(missing_ok=True)
    (container / _OWNER).unlink(missing_ok=True)
    container.rmdir()


def cleanup_abandoned_temporaries(*, scope: Path | None = None, temp_root: Path | None = None) -> dict[str, int]:
    result = {"removed": 0, "busy": 0, "deferred": 0}
    selected = Path(scope) if scope is not None else current_temporary_scope()
    try:
        root = _temp_root(temp_root)
        candidates = list(root.glob("oa-" + _scope_hash(selected) + "-*"))
    except (OSError, ValueError):
        result["deferred"] += 1
        return result
    for container in candidates:
        stream = None
        try:
            _validate_container(container, root, selected)
            try:
                stream = _lock(container / _LEASE)
            except (PermissionError, BlockingIOError):
                result["busy"] += 1
                continue
            _validate_container(container, root, selected)
            if _process_uses(container):
                result["busy"] += 1
                continue
            _remove_data(container)
            stream.close()
            stream = None
            _remove_registration(container)
            result["removed"] += 1
        except FileNotFoundError:
            # 另一启动已完成补清理，无需重建该目录。
            pass
        except (OSError, ValueError, RecursionError, subprocess.SubprocessError):
            result["deferred"] += 1
        finally:
            if stream is not None:
                stream.close()
    return result


@contextmanager
def temporary_scope(scope: Path):
    token = _SCOPE.set(Path(scope).resolve())
    try:
        result = cleanup_abandoned_temporaries()
        if result["removed"] or result["deferred"]:
            print(f"临时文件补清理：已清理 {result['removed']} 个目录，"
                  f"保留占用中 {result['busy']} 个，待下次重试 {result['deferred']} 个。", file=sys.stderr)
        yield
    finally:
        _SCOPE.reset(token)


class ManagedTemporaryDirectory:
    def __init__(self, kind: str, *, scope: Path | None = None, temp_root: Path | None = None):
        if kind not in _KINDS:
            raise ValueError("未登记的临时空间用途")
        self.scope = Path(scope) if scope is not None else current_temporary_scope()
        self.root = _temp_root(temp_root)
        self.container = Path(tempfile.mkdtemp(prefix="oa-" + _scope_hash(self.scope) + "-", dir=self.root))
        self._lease = _lock(self.container / _LEASE)
        self._closed = False
        owner = {"version": 1, "scope": _scope_hash(self.scope), "container": self.container.name,
                 "kind": kind, "pid": os.getpid()}
        # 登记完成前不存图片；即使此处断电，也不会产生未登记的业务材料。
        try:
            (self.container / _OWNER).write_text(json.dumps(owner), encoding="utf-8")
            self.name = str(self.container / "data")
            Path(self.name).mkdir()
        except BaseException:
            self._lease.close()
            self._closed = True
            # 初始化尚未交给调用者，不含业务数据；登记写入失败也不泄漏句柄。
            try:
                _remove_registration(self.container)
            except (OSError, ValueError):
                pass
            raise

    def __enter__(self) -> str:
        return self.name

    def __exit__(self, *_args) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._closed:
            return
        try:
            _validate_container(self.container, self.root, self.scope)
            _remove_data(self.container)
            self._lease.close()
            _remove_registration(self.container)
        except (OSError, ValueError, RecursionError):
            print("本次临时文件暂未清理完毕，已保留登记，下次正式运行将安全重试。", file=sys.stderr)
        finally:
            self._lease.close()
            self._closed = True


def register_abandoned_legacy_temporary(source: Path, *, scope: Path | None = None,
                                       temp_root: Path | None = None) -> Path:
    """仅供已明确确认归属及中断状态的旧目录迁移；正式启动绝不自动猜测/调用。

    同卷移动到带登记的临时容器，不删除图片；下一次正式启动再按占用保护补清理。
    """
    root = _temp_root(temp_root)
    source = Path(source).absolute()
    match = re.fullmatch(r"offline-(audit-render|product-images)-[a-z0-9_]{8}", source.name)
    _plain(source, directory=True)
    if match is None or source.resolve(strict=True).parent != root:
        raise ValueError("只能登记已确认归属的系统临时目录，不能迁移业务目录")
    _tree(source, source)
    if _process_uses(source):
        raise ValueError("旧临时目录仍被引用或无法确认已停止，未迁移")
    value = ManagedTemporaryDirectory("render" if match[1] == "audit-render" else "product-images",
                                     scope=scope, temp_root=root)
    try:
        Path(value.name).rmdir()  # 仅删除刚创建且为空的 data 目录。
        source.rename(value.name)
    except BaseException:
        value.cleanup()
        raise
    value._lease.close()
    value._closed = True
    return Path(value.name)
