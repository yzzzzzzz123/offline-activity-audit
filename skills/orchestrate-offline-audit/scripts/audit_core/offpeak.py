"""Only start new audits inside DeepSeek's official off-peak window.

DeepSeek's pricing page defines peak hours as 01:00-04:00 and 06:00-10:00 UTC,
Monday to Friday, excluding mainland Chinese public holidays; every other hour
is off-peak, including weekends and holidays in full. In Asia/Shanghai that is
09:00-12:00 and 14:00-18:00 on workdays.

This module only answers "may a new audit start now". A job that already
started keeps running past the boundary until it finishes: nothing here
cancels, pauses or re-gates a run in flight.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone, time as clock_time
import json
import os
from pathlib import Path

from .paths import SKILL_ROOT
from .common import china_timezone

BEIJING = china_timezone()
# Official DeepSeek peak windows, always expressed in UTC.
PEAK_UTC = ((clock_time(1, 0), clock_time(4, 0)), (clock_time(6, 0), clock_time(10, 0)))
HOLIDAY_FILE = SKILL_ROOT / "references" / "cn-holidays.json"
WINDOW_LABEL = "工作日 09:00-12:00、14:00-18:00（北京时间）为高峰，其余时段为空闲"
SOURCE_LABEL = ("DeepSeek 官方错峰时段：UTC 01:00-04:00、06:00-10:00（周一至周五，"
                "中国法定节假日除外）为高峰，其余为空闲")


def beijing_now() -> datetime:
    return datetime.now(BEIJING)


def _as_beijing(moment: datetime | None) -> datetime:
    if moment is None:
        return beijing_now()
    if moment.tzinfo is None:
        return moment.replace(tzinfo=BEIJING)
    return moment.astimezone(BEIJING)


def public_holidays() -> frozenset[date]:
    """Dates treated as Chinese public holidays (off-peak for the whole day)."""
    values: set[date] = set()
    for item in str(os.environ.get("OFFLINE_AUDIT_CN_HOLIDAYS") or "").split(","):
        text = item.strip()
        if not text:
            continue
        try:
            values.add(date.fromisoformat(text))
        except ValueError:
            continue
    try:
        document = json.loads(HOLIDAY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return frozenset(values)
    items = document.get("holidays") if isinstance(document, dict) else document
    for item in items if isinstance(items, list) else []:
        try:
            values.add(date.fromisoformat(str(item).strip()))
        except ValueError:
            continue
    return frozenset(values)


def is_peak(moment: datetime | None = None, *, holidays: frozenset[date] | None = None) -> bool:
    """True when DeepSeek bills this moment at peak rate."""
    moment = _as_beijing(moment)
    days = public_holidays() if holidays is None else holidays
    if moment.date() in days or moment.weekday() >= 5:
        return False
    utc_clock = moment.astimezone(timezone.utc).timetz().replace(tzinfo=None)
    return any(start <= utc_clock < end for start, end in PEAK_UTC)


def next_offpeak_start(moment: datetime | None = None, *,
                       holidays: frozenset[date] | None = None,
                       limit_days: int = 14) -> datetime:
    """First Beijing-time minute at or after ``moment`` that is off-peak."""
    moment = _as_beijing(moment)
    days = public_holidays() if holidays is None else holidays
    cursor = moment.replace(second=0, microsecond=0)
    if cursor < moment:
        cursor += timedelta(minutes=1)
    for _ in range(max(1, limit_days) * 24 * 60):
        if not is_peak(cursor, holidays=days):
            return cursor
        cursor += timedelta(minutes=1)
    return cursor


def next_start_label(moment: datetime, start: datetime) -> str:
    delta = (start.date() - moment.date()).days
    day = {0: "今天", 1: "明天", 2: "后天"}.get(delta, start.strftime("%m-%d"))
    return f"{day} {start.strftime('%H:%M')}"


def describe(moment: datetime | None = None) -> dict[str, object]:
    moment = _as_beijing(moment)
    days = public_holidays()
    peak = is_peak(moment, holidays=days)
    start = next_offpeak_start(moment, holidays=days) if peak else None
    return {
        "peak_now": peak,
        "offpeak_now": not peak,
        "next_offpeak_start": start.isoformat() if start else None,
        "next_offpeak_label": next_start_label(moment, start) if start else None,
        # Server-side seconds remove browser/server clock skew from the page.
        "seconds_to_next_offpeak": (
            int(max(0.0, (start - moment).total_seconds())) if start else None
        ),
        "window": WINDOW_LABEL,
        "source": SOURCE_LABEL,
        "holiday_count": len(days),
        "checked_at": moment.isoformat(),
    }
