"""Publish saved summary text for older running workbench servers, without reruns."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .common import AuditError
from .workbench_html import load_analysis_summary
from .workbench_store import atomic_write_json, atomic_write_text


def publish_summary_displays(root: Path, backup: Path, *, apply: bool = False) -> dict:
    root = root.resolve()
    results = []
    pending = []
    for workspace in sorted(root.iterdir()):
        if not workspace.is_dir() or workspace.name.startswith('.'):
            continue
        if workspace.is_symlink() or getattr(workspace, 'is_junction', lambda: False)():
            continue
        manifest_path = workspace / 'manifest.json'
        snapshot_path = workspace / 'snapshot.json'
        if not manifest_path.is_file():
            continue
        raw = manifest_path.read_bytes()
        manifest = json.loads(raw)
        if manifest.get('workspace_id') != workspace.name:
            raise AuditError(f'{workspace.name} 的运行身份不一致')
        if manifest.get('status') not in {'completed', 'failed'}:
            pending.append(workspace.name)
            continue
        display = load_analysis_summary(workspace, None, manifest)
        if not display.get('text') and not (workspace / 'ai-analysis-summary.md').is_file():
            snapshot = json.loads(snapshot_path.read_bytes()) if snapshot_path.is_file() else {}
            display = load_analysis_summary(workspace, snapshot.get('view'), manifest)
        if not display.get('text'):
            results.append({'workspace_id': workspace.name, 'status': 'unavailable'})
            continue
        if manifest.get('analysis_summary_display') == display:
            results.append({'workspace_id': workspace.name, 'status': 'unchanged'})
            continue
        status = 'would_update'
        if apply:
            target = backup / workspace.name / 'manifest.json'
            if target.exists() and target.read_bytes() != raw:
                raise AuditError(f'{workspace.name} 的备份已存在且内容不同')
            if not target.exists():
                atomic_write_text(target, raw.decode('utf-8'))
            if manifest_path.read_bytes() != raw:
                raise AuditError(f'{workspace.name} 的记录正在变化，未写入')
            manifest['analysis_summary_display'] = display
            atomic_write_json(manifest_path, manifest)
            status = 'updated'
        results.append({'workspace_id': workspace.name, 'status': status,
                        'source': display['source'],
                        'summary_sha256': hashlib.sha256(display['text'].encode('utf-8')).hexdigest()})
    return {'mode': 'apply' if apply else 'preview', 'results': results, 'pending': pending,
            'updated': sum(r['status'] == 'updated' for r in results),
            'unavailable': sum(r['status'] == 'unavailable' for r in results)}
