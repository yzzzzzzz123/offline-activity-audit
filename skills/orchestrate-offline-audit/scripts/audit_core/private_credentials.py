"""Read explicitly configured secret files without logging their contents."""
from __future__ import annotations

import os
from pathlib import Path
import stat

from .common import AuditError


def read_private_file(path: str | Path, *, limit: int = 16384) -> str:
    source = Path(path)
    try:
        if source.is_symlink():
            raise ValueError()
        descriptor = os.open(source, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
                raise ValueError()
            if os.name != 'nt' and (info.st_mode & 0o077 or info.st_uid not in (0, os.geteuid())):
                raise ValueError()
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise ValueError()
        return data.decode('utf-8').rstrip('\r\n')
    except (OSError, ValueError, UnicodeError):
        raise AuditError('私有配置无法读取：请检查文件归属、0600 权限和有效内容；未输出凭据。') from None


def environment_secret(name: str) -> str:
    path = os.environ.get(name + '_FILE')
    return read_private_file(path) if path else os.environ.get(name, '').strip()
