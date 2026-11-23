"""Bounded body disposal before rejecting a request and closing its connection."""
from __future__ import annotations

import time


MAX_REJECTED_BODY_BYTES = 64 * 1024
REJECTED_BODY_TIMEOUT = 0.5


def discard_rejected_body(handler) -> None:
    # Closing with unread body bytes can reset the connection before Windows
    # clients receive the rejection. Never parse or execute the rejected body.
    # A delegated handler may reach this helper twice for the same rejection.
    if getattr(handler, "_rejected_body_discarded", False):
        return
    handler._rejected_body_discarded = True
    lengths = handler.headers.get_all("Content-Length", [])
    if len(lengths) != 1 or handler.headers.get_all("Transfer-Encoding"):
        return
    digits = lengths[0].strip()
    if not digits.isascii() or not digits.isdigit():
        return
    digits = digits.lstrip("0") or "0"
    if len(digits) > len(str(MAX_REJECTED_BODY_BYTES)):
        return
    remaining = int(digits)
    if not 0 < remaining <= MAX_REJECTED_BODY_BYTES:
        return
    previous_timeout = handler.connection.gettimeout()
    deadline = time.monotonic() + REJECTED_BODY_TIMEOUT
    try:
        while remaining:
            budget = deadline - time.monotonic()
            if budget <= 0:
                break
            handler.connection.settimeout(budget if previous_timeout is None else min(budget, previous_timeout))
            chunk = handler.rfile.read1(min(8192, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
    except OSError:
        pass
    finally:
        handler.connection.settimeout(previous_timeout)
