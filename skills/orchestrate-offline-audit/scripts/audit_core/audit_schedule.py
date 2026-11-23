"""Persisted start windows shared by the workbench and both intake paths.

This policy gates new runners only. It never changes an in-flight audit.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import threading

from . import offpeak
from .workbench_store import atomic_write_json

DAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
FIELDS = {"mode", "weekly", "holiday_all_day", "holidays"}


class ScheduleSettingsError(ValueError):
    pass


class ScheduleSettingsConflict(ScheduleSettingsError):
    pass


def default_settings() -> dict:
    weekday = [["00:00", "09:00"], ["12:00", "14:00"], ["18:00", "24:00"]]
    return {"mode": "official", "weekly": [deepcopy(weekday) for _ in range(5)]
            + [[["00:00", "24:00"]] for _ in range(2)],
            "holiday_all_day": True, "holidays": []}


def _minute(value: object, *, end: bool = False) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        if end and value == "24:00":
            return 1440
        raise ScheduleSettingsError("时间请填写 HH:MM；结束时间可填写 24:00。")
    hour, minute = map(int, value.split(":"))
    return hour * 60 + minute


def _clock(value: int) -> str:
    return f"{value // 60:02d}:{value % 60:02d}"


def weekly_windows(settings: dict) -> list[list[list[int]]]:
    """Expand overnight windows into the following weekday, then merge touching ones."""
    mode = settings["mode"]
    if mode == "always":
        return [[[0, 1440]] for _ in range(7)]
    raw = default_settings()["weekly"] if mode == "official" else settings["weekly"]
    days: list[list[list[int]]] = [[] for _ in range(7)]
    for day, windows in enumerate(raw):
        for start_text, end_text in windows:
            start, end = _minute(start_text), _minute(end_text, end=True)
            if end > start:
                days[day].append([start, end])
            else:
                days[day].append([start, 1440])
                if end:
                    days[(day + 1) % 7].append([0, end])
    for day, windows in enumerate(days):
        merged: list[list[int]] = []
        for start, end in sorted(windows):
            if merged and start < merged[-1][1]:
                raise ScheduleSettingsError(f"{DAY_NAMES[day]}存在重叠时段，请检查本日及前一天的跨夜设置。")
            if merged and start == merged[-1][1]:
                merged[-1][1] = end
            else:
                merged.append([start, end])
        days[day] = merged
    return days


def validate_settings(value: object) -> dict:
    if not isinstance(value, dict) or set(value) != FIELDS:
        raise ScheduleSettingsError("核销时间配置字段不完整或包含未知字段。")
    if not isinstance(value["mode"], str) or value["mode"] not in {"official", "custom", "always"}:
        raise ScheduleSettingsError("请选择官方错峰、自定义时段或全天核销。")
    if type(value["holiday_all_day"]) is not bool:
        raise ScheduleSettingsError("节假日全天核销必须为布尔值。")
    weekly = value["weekly"]
    if not isinstance(weekly, list) or len(weekly) != 7:
        raise ScheduleSettingsError("请分别设置周一至周日的核销时段。")
    for day, windows in enumerate(weekly):
        if not isinstance(windows, list) or len(windows) > 8:
            raise ScheduleSettingsError(f"{DAY_NAMES[day]}最多可设置 8 个时段。")
        for window in windows:
            if not isinstance(window, list) or len(window) != 2:
                raise ScheduleSettingsError("每个时段必须包含开始和结束时间。")
            if _minute(window[0]) == _minute(window[1], end=True):
                raise ScheduleSettingsError("开始和结束时间不能相同；全天请填写 00:00–24:00。")
    if not any(weekly):
        raise ScheduleSettingsError("每周至少保留一个可核销时段。")
    holidays = value["holidays"]
    if not isinstance(holidays, list) or len(holidays) > 366:
        raise ScheduleSettingsError("节假日补录最多可填写 366 个日期。")
    for item in holidays:
        if not isinstance(item, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", item):
            raise ScheduleSettingsError("节假日请使用 YYYY-MM-DD 日期格式。")
        try:
            date.fromisoformat(item)
        except ValueError:
            raise ScheduleSettingsError(f"节假日日期无效：{item}") from None
    result = deepcopy(value)
    result["holidays"] = sorted(set(holidays))
    # Validate the custom draft even while another mode is selected.
    weekly_windows({**result, "mode": "custom"})
    return result


class AuditScheduleStore:
    def __init__(self, worktrees_root: Path):
        self.path = Path(worktrees_root) / ".settings" / "audit-schedule.json"
        self._lock = threading.RLock()

    def public(self) -> dict:
        with self._lock:
            if not self.path.exists():
                return {"revision": 0, **default_settings()}
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(value, dict):
                    raise ValueError()
                revision = value.pop("revision")
                if type(revision) is not int or revision < 1:
                    raise ValueError()
                return {"revision": revision, **validate_settings(value)}
            except (OSError, ValueError, KeyError, TypeError):
                raise ScheduleSettingsError("核销时间配置无法读取，请检查已保存的配置文件。") from None

    def save(self, payload: dict) -> dict:
        if not isinstance(payload, dict) or set(payload) != FIELDS | {"revision"}:
            raise ScheduleSettingsError("核销时间配置字段不完整或包含未知字段。")
        if type(payload["revision"]) is not int:
            raise ScheduleSettingsError("请刷新核销时间配置后重试。")
        candidate = validate_settings({key: payload[key] for key in FIELDS})
        with self._lock:
            current = self.public()
            if current["revision"] != payload["revision"]:
                raise ScheduleSettingsConflict("核销时间已在其他页面修改，请重新载入后再保存。")
            value = {"revision": current["revision"] + 1, **candidate}
            atomic_write_json(self.path, value)
            return value

    def describe(self, moment: datetime | None = None, *, forced_always: bool = False) -> dict:
        moment = offpeak._as_beijing(moment)
        settings = self.public()
        effective = {**settings, "mode": "always"} if forced_always else settings
        days = offpeak.public_holidays() | frozenset(date.fromisoformat(x) for x in settings["holidays"])
        windows = weekly_windows(effective)

        def allowed(at: datetime) -> bool:
            if effective["mode"] == "always":
                return True
            if at.date() in days and (effective["mode"] == "official" or settings["holiday_all_day"]):
                return True
            minute = at.hour * 60 + at.minute
            return any(start <= minute < end for start, end in windows[at.weekday()])

        can_start = allowed(moment)
        next_start = None
        if not can_start:
            # A nonempty weekly schedule guarantees an opening within eight days.
            for offset in range(8):
                day = (moment + timedelta(days=offset)).replace(hour=0, minute=0, second=0, microsecond=0)
                starts = [0] if day.date() in days and (effective["mode"] == "official" or settings["holiday_all_day"]) else [x[0] for x in windows[day.weekday()]]
                candidates = [day + timedelta(minutes=minute) for minute in starts]
                next_start = next((at for at in candidates if at > moment), None)
                if next_start is not None:
                    break
        official = offpeak.describe(moment)
        label = {"official": "官方错峰", "custom": "自定义时段", "always": "全天核销"}[effective["mode"]]
        return {**official, "mode": settings["mode"], "effective_mode": effective["mode"],
                "mode_label": label, "revision": settings["revision"],
                "enabled": effective["mode"] != "always", "allowed_now": can_start,
                "startup_override": forced_always,
                "next_offpeak_start": next_start.isoformat() if next_start else None,
                "next_offpeak_label": offpeak.next_start_label(moment, next_start) if next_start else None,
                "seconds_to_next_offpeak": int((next_start - moment).total_seconds()) if next_start else None,
                "window": offpeak.WINDOW_LABEL if effective["mode"] == "official" else label + "（北京时间）",
                "source": offpeak.SOURCE_LABEL if effective["mode"] == "official" else "工作台核销时间配置",
                "holiday_all_day": effective["mode"] != "custom" or settings["holiday_all_day"],
                "holiday_count": len(days), "holiday_dates": sorted(x.isoformat() for x in days),
                "weekly": [{"day": DAY_NAMES[i], "windows": [[_clock(a), _clock(b)] for a, b in day]}
                           for i, day in enumerate(windows)]}
