"""Exact-byte visible image facts in one deployment; never audit decisions."""
from __future__ import annotations

from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
from threading import RLock
import time


CACHE_VERSION = 1
MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_ENTRY_BYTES = 1024 * 1024
MAX_ENTRIES = 10_000
MAX_MEMORY_BYTES = 8 * 1024 * 1024
MAX_MEMORY_ENTRIES = 128
MAX_CONTEXT_BYTES = 2 * 1024 * 1024
_NAME = re.compile(r"[0-9a-f]{64}\.json")
_LOCK = RLock()
_FIELDS = {"roles", "facts", "limitations"}


def _json(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def fact_key(image_sha256: str, context: dict) -> str | None:
    if not re.fullmatch(r"[0-9a-f]{64}", image_sha256):
        raise ValueError("Image fact cache requires an exact SHA-256")
    digest, length = hashlib.sha256(), 0
    encoder = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    for part in encoder.iterencode({"version": CACHE_VERSION, "image_sha256": image_sha256, "context": context}):
        if len(part) > MAX_CONTEXT_BYTES:
            return None
        data = part.encode("utf-8")
        length += len(data)
        if length > MAX_CONTEXT_BYTES:
            return None
        digest.update(data)
    return digest.hexdigest()


def _valid(value) -> bool:
    return (isinstance(value, dict) and set(value) == _FIELDS
            and all(isinstance(value[k], list) and all(isinstance(v, str) and v for v in value[k])
                    for k in _FIELDS))


def _memory_size(key, value):
    # Fixed three-field shape. Count repeated strings conservatively, including
    # key, tuple/int bookkeeping and an allowance for each OrderedDict node.
    return (sys.getsizeof(key) + sys.getsizeof(value) + sys.getsizeof((None, 0)) + sys.getsizeof(0) + 128
            + sum(sys.getsizeof(name) + sys.getsizeof(items) + sum(sys.getsizeof(text) for text in items)
                  for name, items in value.items()))


def _owner(value):
    return isinstance(value, str) and 0 < len(value) <= 512 and not any(ord(c) < 32 for c in value)


def _record(key, value, owners):
    payload = {"value": value, "owners": sorted(set(owners))}
    return {"version": CACHE_VERSION, "key": key,
            "payload_sha256": hashlib.sha256(_json(payload)).hexdigest(), **payload}


def _read_record(path, key):
    info = _plain(path)
    if info.st_size > MAX_ENTRY_BYTES:
        return None
    path.chmod(0o600)
    with path.open("rb") as incoming:
        opened = os.fstat(incoming.fileno())
        if (info.st_dev, info.st_ino) != (opened.st_dev, opened.st_ino):
            raise OSError("Image fact cache entry changed")
        raw = incoming.read(MAX_ENTRY_BYTES + 1)
    try:
        record = json.loads(raw)
        value, owners = record["value"], record["owners"]
        if (set(record) == {"version", "key", "payload_sha256", "value", "owners"}
                and record["version"] == CACHE_VERSION and record["key"] == key
                and _valid(value) and isinstance(owners, list) and owners and all(_owner(o) for o in owners)
                and record == _record(key, value, owners)):
            return record
    except (ValueError, TypeError, KeyError, RecursionError):
        pass
    return None


def _write_record(root, destination, record):
    raw = _json(record)
    if len(raw) > MAX_ENTRY_BYTES:
        raise OSError("Image fact cache record too large")
    fd, name = tempfile.mkstemp(prefix="entry-", suffix=".tmp", dir=root)
    pending = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            os.chmod(pending, 0o600)
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
        pending.replace(destination)
    finally:
        pending.unlink(missing_ok=True)


def _entries(root):
    # Bound scanning itself, not merely the resulting slice.
    entries = []
    with os.scandir(root) as scan:
        for scanned, item in enumerate(scan):
            if scanned > MAX_ENTRIES * 2:
                raise OSError("Image fact cache directory over limit")
            if _NAME.fullmatch(item.name):
                entries.append(root / item.name)
                if len(entries) > MAX_ENTRIES:
                    raise OSError("Image fact cache entry limit exceeded")
            elif re.fullmatch(r"entry-[a-zA-Z0-9_]+\.tmp", item.name):
                pending = root / item.name
                _plain(pending)
                pending.unlink()  # no active writer can exist while the cache lock is held
    return entries


def _store(root, path, record, max_bytes, max_entries):
    size = len(_json(record))
    if size > min(MAX_ENTRY_BYTES, max_bytes) or not max_entries:
        return False
    entries = []
    for candidate in _entries(root):
        info = _plain(candidate)
        if candidate != path:
            entries.append((info.st_mtime_ns, candidate.name, info.st_size))
    entries.sort()
    total = sum(length for _, _, length in entries)
    count = len(entries)
    for _, name, length in entries:
        if total + size <= max_bytes and count < max_entries:
            break
        target = root / name
        _plain(target)
        target.unlink()
        total -= length
        count -= 1
    _write_record(root, path, record)
    return True


def _plain(path: Path, *, directory=False):
    info = path.lstat()
    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
            or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))
            or (not directory and info.st_nlink != 1)):
        raise OSError("Unsafe image fact cache path")
    return info


def _prepare(root: Path):
    for path in reversed((root, *root.parents)):
        try:
            _plain(path, directory=True)
        except FileNotFoundError:
            try:
                path.mkdir(mode=0o700)
            except FileExistsError:
                _plain(path, directory=True)
    # Only cache-owned directories; never change deployment/worktree permissions.
    for path in (root.parent, root):
        path.chmod(0o700)


@contextmanager
def _locked(root: Path):
    # Only filesystem IO holds this lock; paid/model calls never do.
    with _LOCK:
        _prepare(root)
        path = root / "cache.lock"
        try:
            _plain(path)
        except FileNotFoundError:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as output:
                    output.write(b"0")
            except FileExistsError:
                pass
        _plain(path)
        path.chmod(0o600)
        with path.open("r+b") as stream:
            before, opened = _plain(path), os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise OSError("Image fact cache lock changed")
            deadline = time.monotonic() + 0.5
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
                        raise OSError("Image fact cache busy") from None
                    time.sleep(0.02)
            try:
                yield
            finally:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class ImageFactCache:
    """get -> (facts, hit source); put accepts only validated source-free facts.

    No root means this instance's bounded memory only. Corrupt/unavailable cache
    entries are misses, never extraction failures. Values contain only
    roles/facts/limitations, without source-binding, credentials, usage or
    decision fields. Joint reference observations bind their entire current
    image group and source IDs in the cache-key context before reuse.
    """
    def __init__(self, worktrees_root: Path | None = None, *, owner: str | None = None,
                 max_bytes=MAX_CACHE_BYTES, max_entries=MAX_ENTRIES):
        self.owner = owner if _owner(owner) else None
        self.root = (Path(worktrees_root).absolute() / ".cache" / "image-facts-v1"
                     if worktrees_root is not None and self.owner else None)
        self.max_bytes = max(0, min(max_bytes, MAX_CACHE_BYTES))
        self.max_entries = max(0, min(max_entries, MAX_ENTRIES))
        self.max_memory_bytes = min(self.max_bytes, MAX_MEMORY_BYTES)
        self.max_memory_entries = min(self.max_entries, MAX_MEMORY_ENTRIES)
        self.memory = OrderedDict()
        self.memory_bytes = 0

    def _remember(self, key, value):
        length = _memory_size(key, value)
        if length > self.max_memory_bytes or len(_json(value)) > MAX_ENTRY_BYTES or not self.max_memory_entries:
            return
        stored = deepcopy(value)
        length = _memory_size(key, stored)
        if length > self.max_memory_bytes:
            return
        old = self.memory.pop(key, None)
        if old:
            self.memory_bytes -= old[1]
        while self.memory and (self.memory_bytes + length > self.max_memory_bytes or len(self.memory) >= self.max_memory_entries):
            _, (_, removed) = self.memory.popitem(last=False)
            self.memory_bytes -= removed
        self.memory[key] = (stored, length)
        self.memory_bytes += length

    def get(self, key: str):
        if not _NAME.fullmatch(key + ".json"):
            raise ValueError("Invalid image fact cache key")
        if key in self.memory:
            value, _ = self.memory[key]
            self.memory.move_to_end(key)
            return deepcopy(value), "memory_hit"
        if self.root is None:
            return None, None
        try:
            with _locked(self.root):
                path = self.root / (key + ".json")
                record = _read_record(path, key)
                if record is None:
                    _plain(path)
                    path.unlink()  # discard only a verified ordinary cache-owned record
                    return None, None
                value = record["value"]
                if self.owner not in record["owners"]:
                    # A hit is not usable until deletion ownership is durable.
                    if not _store(self.root, path, _record(key, value, [*record["owners"], self.owner]),
                                  self.max_bytes, self.max_entries):
                        return None, None
                os.utime(path, None)
                self._remember(key, value)
                return deepcopy(value), "persistent_hit"
        except OSError:
            return None, None

    def put(self, key: str, value: dict) -> bool:
        if not _NAME.fullmatch(key + ".json") or not _valid(value):
            raise ValueError("Only validated roles/facts/limitations may be cached")
        if sum(len(text) for values in value.values() for text in values) > MAX_ENTRY_BYTES:
            return False
        raw = _json(_record(key, value, [self.owner] if self.owner else []))
        if len(raw) > min(MAX_ENTRY_BYTES, self.max_bytes) or not self.max_entries:
            return False
        self._remember(key, value)
        if self.root is None:
            return False
        try:
            with _locked(self.root):
                destination = self.root / (key + ".json")
                owners = [self.owner]
                try:
                    existing = _read_record(destination, key)
                    if existing:
                        owners.extend(existing["owners"])
                except FileNotFoundError:
                    pass
                return _store(self.root, destination, _record(key, value, owners), self.max_bytes, self.max_entries)
        except OSError:
            return False


def remove_run_facts(worktrees_root: Path, run_id: str) -> dict:
    """Authorized run deletion only: retain records still owned by another run."""
    result = {"removed_entries": 0, "remaining_entries": 0, "deferred": 0}
    if not _owner(run_id):
        return {**result, "deferred": 1}
    root = Path(worktrees_root).absolute() / ".cache" / "image-facts-v1"
    if not root.exists():
        return result
    try:
        with _locked(root):
            for path in _entries(root):
                try:
                    key = path.stem
                    record = _read_record(path, key)
                    owners = [o for o in record["owners"] if o != run_id] if record else []
                    if not owners:
                        _plain(path)
                        path.unlink()
                        result["removed_entries"] += 1
                    else:
                        if run_id in record["owners"]:
                            _write_record(root, path, _record(key, record["value"], owners))
                        result["remaining_entries"] += 1
                except OSError:
                    result["deferred"] += 1
    except OSError:
        result["deferred"] += 1
    return result
