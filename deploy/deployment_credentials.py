"""Explicitly authorized product OSS credentials for a private deployment bundle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys

from configuration import ROOT

sys.path.insert(0, str(ROOT / "skills/orchestrate-offline-audit/scripts"))
from audit_core.private_credentials import read_private_file

PRODUCT_OSS_MEMBER = "deploy/private/product-oss.json"


def checked_product_oss_bytes(data: bytes | str) -> bytes:
    try:
        if len(data) > 16384:
            raise ValueError()
        value = json.loads(data)
        if not isinstance(value, dict) or set(value) != {"accessKeyId", "accessKeySecret"}:
            raise ValueError()
        if any(not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9]{16,128}", item)
               for item in value.values()):
            raise ValueError()
        return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    except (ValueError, TypeError, UnicodeError):
        raise RuntimeError("Invalid product OSS credentials; no values were displayed.") from None


def bundled_product_oss_bytes(root: Path = ROOT) -> bytes | None:
    source = root / PRODUCT_OSS_MEMBER
    if source.is_symlink():
        raise RuntimeError("Bundled product OSS credentials must not be a link.")
    if not source.exists():
        return None
    return checked_product_oss_bytes(read_private_file(source))


def current_product_oss_bytes() -> bytes:
    """Reuse the current service's credential source; never request or print a key."""
    from audit_core.product_oss import _crypt, credential_path
    source = credential_path()
    if os.name == "nt":
        if source.is_symlink() or not source.is_file() or source.stat().st_size > 16384:
            raise RuntimeError("Current product OSS credentials are unavailable.")
        data = _crypt(source.read_bytes(), decrypt=True)
    else:
        data = read_private_file(source)
    return checked_product_oss_bytes(data)
