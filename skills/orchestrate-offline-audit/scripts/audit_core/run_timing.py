"""Actual per-run timing shared by persistence and read-only statistics."""
from datetime import datetime, timezone


def parse_time(value):
    if not isinstance(value, str):return None
    try:
        stamp = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return stamp.astimezone(timezone.utc) if stamp.tzinfo else None
    except ValueError:return None


def active_seconds(manifest, start, end):
    if start > end:return None
    pauses = []
    for resume in manifest.get('resume_history') or []:
        stopped, resumed = parse_time(resume.get('previous_completed_at')), parse_time(resume.get('resumed_at'))
        if stopped is None or resumed is None or resumed < stopped:return None
        left, right = max(start, stopped), min(end, resumed)
        if right > left:pauses.append((left, right))
    merged = []
    for left, right in sorted(pauses):
        if merged and left <= merged[-1][1]:merged[-1] = (merged[-1][0], max(right, merged[-1][1]))
        else:merged.append((left, right))
    return max(0, (end-start).total_seconds()-sum((right-left).total_seconds() for left,right in merged))


def finish_timing(manifest):
    start = parse_time(manifest.get('analysis_started_at')) or parse_time(manifest.get('created_at'))
    end = parse_time(manifest.get('completed_at'))
    if start is None or end is None or end < start:return None
    elapsed = (end-start).total_seconds()
    active = active_seconds(manifest, start, end)
    return {'started_at':start.isoformat(), 'finished_at':end.isoformat(),
            'elapsed_seconds':round(elapsed,3),
            'active_seconds':round(active,3) if active is not None else None,
            'paused_seconds':round(elapsed-active,3) if active is not None else None}


def completed_timing(manifest):
    """Read historical timestamps without rewriting records or inventing duration."""
    if manifest.get('status') != 'completed':return None
    timing = finish_timing(manifest)
    return timing if timing is not None and timing['active_seconds'] is not None else None
