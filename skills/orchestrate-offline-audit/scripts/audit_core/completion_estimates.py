"""Read-only completion forecasts from comparable, uninterrupted local runs.

Only small manifests, material inventories and event metadata are read. Neither
snapshots nor model inputs are loaded, and predictions never change a job.
"""
from datetime import datetime, timezone, timedelta
from functools import lru_cache
from math import log
from statistics import mean, median
from pathlib import Path
import json
import re

from .run_timing import parse_time as _time, active_seconds as _active_seconds

MIN_SAMPLES = 3
MAX_NEIGHBORS = 12
MILESTONES = {'intake.completed', 'biz_type.resolved', 'pdf_materials.validated',
              'result.validated', 'report.verified'}


def _signature(workspace):
    result = []
    for name in ('manifest.json', 'logs/events.jsonl', 'analysis/input-cases.json'):
        path = workspace / name
        if path.is_symlink() or any(p.is_symlink() for p in path.parents if p != workspace.parent):
            return None
        try:
            stat = path.stat()
            result.append((stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:result.append(None)
    return tuple(result)


@lru_cache(maxsize=256)
def _profile(workspace_name, signature):
    del signature
    workspace = Path(workspace_name)
    manifest = json.loads((workspace / 'manifest.json').read_text(encoding='utf-8'))
    if not isinstance(manifest, dict):return None
    points = []
    interrupted = False
    occurrences = {}
    events_path = workspace / 'logs/events.jsonl'
    if events_path.is_file():
        with events_path.open(encoding='utf-8') as stream:
            for line in stream:
                try:event = json.loads(line)
                except ValueError:continue  # A live final line may still be being appended.
                if not isinstance(event, dict):continue
                stamp = _time(event.get('timestamp'))
                if stamp is None:continue
                kind = event.get('type')
                if kind in {'run.failed', 'run.resumed', 'run.revalidation_started'}:interrupted = True
                if kind == 'execution.step':
                    details = event.get('details') or {}
                    if not isinstance(details, dict):continue
                    if details.get('type') != 'ai.validated':continue
                    label = re.sub(r' · 第\d+次.*$', '', str(details.get('label') or ''))
                    key = 'ai.validated:' + label
                elif kind in MILESTONES:key = kind
                else:continue
                occurrences[key] = occurrences.get(key, 0) + 1
                points.append((key, occurrences[key], stamp))
    files = None
    inventory_path = workspace / 'analysis/input-cases.json'
    if inventory_path.is_file():
        cases = json.loads(inventory_path.read_text(encoding='utf-8'))
        if isinstance(cases, dict):
            inventories = [v['inventory'] for v in cases.values()
                           if isinstance(v, dict) and isinstance(v.get('inventory'), list)]
            if inventories:files = sum(len(items) for items in inventories)
    start = _time(manifest.get('analysis_started_at')) or _time(manifest.get('created_at'))
    end = _time(manifest.get('completed_at'))
    return {'workspace_id':workspace.name, 'run_id':manifest.get('run_id'), 'manifest':manifest,
            'start':start, 'end':end, 'files':files, 'points':points,
            'interrupted':interrupted,
            'model':(manifest.get('producer_model'), manifest.get('audit_model'), manifest.get('reasoning_effort')),
            'scenarios':tuple(sorted(manifest.get('scenarios') or []))}


def filtered_mean(values):
    """Discard extremes beyond three median absolute deviations, then average.

    Median/MAD locate outliers only; the forecast itself is an arithmetic mean.
    With zero spread, retain the repeated central duration. No learned model,
    random fit, or single surviving slow sample is involved.
    """
    center = median(values)
    deviation = median(abs(value-center) for value in values)
    kept = [value for value in values if abs(value-center) <= 3*deviation]
    return mean(kept), len(kept)


def estimate(current, history, now):
    """Use stage-relative historical remaining times, then elapsed total times."""
    base = {'workspace_id':current['workspace_id'], 'run_id':current['run_id'],
            'generated_at':now.isoformat(), 'sample_count':0}
    candidates = [p for p in history if p['model'] == current['model']]
    exact = [p for p in candidates if p['scenarios'] == current['scenarios'] and current['scenarios']]
    same_scenario = len(exact) >= MIN_SAMPLES
    candidates = exact if same_scenario else candidates
    if len(candidates) < MIN_SAMPLES:
        return {**base, 'status':'insufficient_history', 'message':'同模型历史样本不足，暂无法估算'}
    workload_matched = False
    if current['files']:
        close = [p for p in candidates if p['files'] and 1/3 <= p['files']/current['files'] <= 3]
        if len(close) >= MIN_SAMPLES:
            candidates = close
            workload_matched = True
        candidates = sorted(candidates, key=lambda p: (
            abs(log(p['files']/current['files'])) if p['files'] else float('inf'),
            -(p['end'].timestamp())))
    else:candidates = sorted(candidates, key=lambda p:p['end'], reverse=True)
    candidates = candidates[:MAX_NEIGHBORS]
    # Try the latest completed comparable step. Occurrence numbers prevent a
    # repeated stage in another packet from becoming the same progress point.
    times = []
    progress_matched = False
    for key, occurrence, stamp in reversed(current['points']):
        elapsed = _active_seconds(current['manifest'], stamp, now)
        if elapsed is None:continue
        matches = []
        for sample in candidates:
            anchor = next((t for k, n, t in sample['points'] if k == key and n == occurrence), None)
            if anchor is not None and sample['end'] > anchor:
                matches.append((sample['end'] - anchor).total_seconds())
        if len(matches) >= MIN_SAMPLES:
            times = matches
            progress_matched = True
            break
    if not times:
        if current['start'] is None:
            return {**base, 'status':'preparing', 'message':'资料准备中，开始核销后更新预估'}
        elapsed = _active_seconds(current['manifest'], current['start'], now)
        if elapsed is None:
            return {**base, 'status':'insufficient_history', 'message':'正在根据续跑进度重新估算'}
        times = [(p['end'] - p['start']).total_seconds() for p in candidates]
    average, kept_count = filtered_mean(times)
    base.update(sample_count=kept_count, raw_sample_count=len(times), excluded_count=len(times)-kept_count,
                calculation='outlier_filtered_mean',
                match='model_and_scenario' if same_scenario else 'model',
                workload_matched=workload_matched, progress_matched=progress_matched)
    # Average all normal comparable durations before subtracting elapsed time.
    # Dropping every faster sample as time passes would bias the estimate toward
    # the slowest historical run and invent a continuously extending deadline.
    remaining = average-elapsed
    if remaining <= 0:
        return {**base, 'status':'beyond_history', 'message':'本单耗时已超出历史参考范围，等待新进度更新预估'}
    return {**base, 'status':'estimated', 'remaining_seconds':round(remaining),
            'estimated_completion_at':(now + timedelta(seconds=remaining)).isoformat(),
            'confidence':'normal' if same_scenario and kept_count >= 5 else 'limited'}


class CompletionEstimator:
    def __init__(self, root):self.root = Path(root).resolve()

    def document(self, jobs=(), *, now=None):
        now = now or datetime.now(timezone.utc)
        profiles = []
        if self.root.is_dir():
            for workspace in self.root.iterdir():
                if not workspace.is_dir() or workspace.is_symlink() or workspace.name.startswith('.'):continue
                if workspace.resolve().parent != self.root:continue
                try:
                    signature = _signature(workspace)
                    if signature is not None:
                        profile = _profile(str(workspace), signature)
                        if profile:profiles.append(profile)
                except (OSError, ValueError, TypeError):continue
        # Failed, demo, resumed, revalidated, or timestamp-incomplete records
        # cannot teach a cold run's duration. Business issues can still finish.
        history = [p for p in profiles if p['manifest'].get('status') == 'completed'
                   and not p['manifest'].get('demo') and not p['manifest'].get('resume_history')
                   and not p['interrupted']
                   and p['manifest'].get('analysis_started_at') and p['start'] and p['end']
                   and now >= p['end'] > p['start'] and p['model'][1]]
        results = [estimate(p, history, now) for p in profiles
                   if p['manifest'].get('status') not in {'completed', 'failed'} and not p['manifest'].get('demo')]
        by_run = {item['run_id']:item for item in results}
        for job in jobs:
            if job.get('status') not in {'accepted','downloading','downloaded','waiting_offpeak',
                                         'running','callback'}:continue
            run_id = job.get('run_id')
            if job.get('status') == 'callback':
                results = [item for item in results if item.get('run_id') != run_id]
                results.append({'job_id':job.get('job_id'), 'run_id':run_id, 'status':'sending_result',
                    'message':'正在向业务系统发送结果', 'sample_count':0, 'generated_at':now.isoformat()})
            elif run_id in by_run:
                by_run[run_id]['job_id'] = job.get('job_id')
            else:
                results.append({'job_id':job.get('job_id'), 'run_id':run_id,
                    'status':'queued' if job.get('status') != 'running' else 'preparing',
                    'message':'开始核销后更新预估',
                    'sample_count':0, 'generated_at':now.isoformat()})
        return {'generated_at':now.isoformat(), 'history_count':len(history), 'estimates':results}
