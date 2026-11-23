"""只缓存当前 OSS 清单已授权的商品参考图字节，不缓存身份、清单或审核结论。"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import os
from pathlib import Path
import re
import shutil
import stat
from threading import RLock
import time
from typing import Callable

from PIL import Image

from .common import AuditError, sha256_file
from .product_oss import BUCKET, ENDPOINT, PREFIX


MAX_CACHE_BYTES = 2 * 1024 * 1024 * 1024
_THREAD_LOCK = RLock()
_IMAGE_NAME = re.compile(r"[0-9a-f]{64}\.(?:png|jpg|jpeg|webp)")
_LOCK_WAIT_SECONDS = 180


def _plain(path: Path, *, directory: bool = False) -> os.stat_result:
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise AuditError("商品参考图缓存含链接或非普通文件，未使用或覆盖")
    return info


def cache_directory(scope: Path) -> Path:
    # 同一部署/worktrees 与 OSS 来源共同隔离；服务沙箱已有 worktrees 写权限。
    origin = hashlib.sha256(f"{ENDPOINT}\n{BUCKET}\n{PREFIX}".encode()).hexdigest()[:16]
    return Path(scope).absolute() / ".cache" / "product-images-v1" / origin


def _prepare(root: Path) -> None:
    # 逐级检查后才创建，拒绝 junction/symlink 将缓存写到其他位置。
    for path in reversed((root, *root.parents)):
        try:
            _plain(path, directory=True)
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                _plain(path, directory=True)


@contextmanager
def _locked(root: Path):
    with _THREAD_LOCK:
        _prepare(root)
        path = root / "cache.lock"
        try:
            _plain(path)
        except FileNotFoundError:
            try:
                with path.open("xb") as output:
                    output.write(b"0")
            except FileExistsError:
                pass
        _plain(path)
        with path.open("r+b") as stream:
            before, opened = _plain(path), os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise AuditError("商品参考图缓存锁已变化")
            deadline = time.monotonic() + _LOCK_WAIT_SECONDS
            while True:
                try:
                    stream.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError("商品参考图缓存锁等待超时") from None
                    time.sleep(0.05)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def verify_image(path: Path, view: dict) -> None:
    if path.stat().st_size != view["size_bytes"] or sha256_file(path) != view["sha256"]:
        raise AuditError("商品参考图片字节与本次 OSS 清单不一致")
    try:
        with Image.open(path) as image:
            if image.size != (view["width"], view["height"]):
                raise AuditError("商品参考图片尺寸与本次 OSS 清单不一致")
            if Image.MIME.get(image.format) != view["content_type"]:
                raise AuditError("商品参考图片类型与本次 OSS 清单不一致")
            image.verify()
    except AuditError:
        raise
    except Exception:
        raise AuditError("商品参考图片无法完整解码") from None


def _copy(source: Path, target: Path) -> None:
    # 不用硬链接；模型只能修改自己的副本，不能污染共享缓存。
    with target.open("xb") as output, source.open("rb") as incoming:
        shutil.copyfileobj(incoming, output)


def _make_room(root: Path, size: int, keep: Path) -> None:
    entries = []
    for path in root.iterdir():
        if _IMAGE_NAME.fullmatch(path.name):
            info = _plain(path)
            entries.append((info.st_mtime_ns, path, info.st_size))
    total = sum(item[2] for item in entries)
    for _, path, length in sorted(entries):
        if total + size <= MAX_CACHE_BYTES:
            break
        if path != keep:
            _plain(path)
            path.unlink()
            total -= length


def copy_or_download(view: dict, target: Path, *, scope: Path,
                     download: Callable[[dict, Path], None]) -> str:
    """调用方先读取并核验本次清单；只以该清单的准确哈希查缓存。"""
    if target.exists() or target.is_symlink():
        raise AuditError("商品参考图片目标已存在，未覆盖")
    root = cache_directory(scope)
    name = view["sha256"] + Path(view["object_key"]).suffix.lower()
    if not _IMAGE_NAME.fullmatch(name):
        raise AuditError("商品参考图片缓存标识无效")
    cached, pending = root / name, root / "download.part"
    try:
        with _locked(root):
            # 全局缓存锁保护唯一半成品；崩溃后下一次访问即可安全回收。
            try:
                _plain(pending)
            except FileNotFoundError:
                pass
            else:
                pending.unlink()
            try:
                _plain(cached)
            except FileNotFoundError:
                pass
            else:
                try:
                    verify_image(cached, view)
                except AuditError:
                    cached.unlink()  # 仅删除本缓存的损坏文件，随后重新从 OSS 获取。
                else:
                    _copy(cached, target)
                    verify_image(target, view)
                    try:
                        os.utime(cached, None)
                    except OSError:
                        pass  # LRU 时间戳写失败不影响已经验证的缓存命中。
                    return "persistent_hit"
            download(view, target)
            verify_image(target, view)
            if view["size_bytes"] > MAX_CACHE_BYTES:
                return "downloaded_uncached"
            _make_room(root, view["size_bytes"], cached)
            try:
                _copy(target, pending)
                pending.chmod(0o600)
                verify_image(pending, view)
                pending.replace(cached)
            finally:
                pending.unlink(missing_ok=True)
            return "downloaded"
    except OSError:
        # 缓存目录不可写/磁盘满不改变取图依据；仍须完成本次 OSS 下载与完整校验。
        if not target.exists():
            download(view, target)
        verify_image(target, view)
        return "downloaded_uncached"
