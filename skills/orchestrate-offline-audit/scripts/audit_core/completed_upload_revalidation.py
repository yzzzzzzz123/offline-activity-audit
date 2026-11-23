"""Explicit versioned correction of one local, callback-free completed upload.

The published version stays intact while a private candidate is validated. Only
an exact, explicitly selected original call and its known audit dependencies may
be reconsidered. This module never runs automatically or changes model settings.
"""
from copy import deepcopy
import hashlib
from itertools import chain
import json
from pathlib import Path
import re
import shutil
from uuid import uuid4

from .common import AuditError
from .failed_upload_resume import ResumeEvidenceProvider, load_trace, saved_qince


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as incoming:
        for block in iter(lambda: incoming.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def revalidate_completed_upload(job_id, worktrees_root, *, from_call_id=None, expected_snapshot_sha256,
                                finish_unreviewed=False):
    from .model_settings import ModelSettingsStore
    from .workbench_runtime import run_persistent_audit
    from .workbench_store import atomic_write_json, atomic_write_text, utc_now

    root = Path(worktrees_root).resolve()
    if (not isinstance(job_id, str) or not re.fullmatch(r'[a-f0-9]{24}', job_id)
            or (not finish_unreviewed and (not isinstance(from_call_id, str) or not from_call_id))
            or not isinstance(expected_snapshot_sha256, str)
            or not re.fullmatch(r'[a-f0-9]{64}', expected_snapshot_sha256)):
        raise AuditError('重新核验须明确原job、受影响调用及已完成快照SHA-256')
    job_path = root/'.intake/jobs'/f'{job_id}.json'
    job_text = job_path.read_text(encoding='utf-8')
    job = json.loads(job_text)
    if (job.get('status') != 'completed' or job.get('input_source') != 'input'
            or job.get('callback', {}).get('required') is not False
            or job.get('callback', {}).get('attempts') != 0):
        raise AuditError('只支持显式重新核验本地上传、从未回调的已完成任务')
    matches = []
    for path in root.glob('*/manifest.json'):
        value = json.loads(path.read_text(encoding='utf-8'))
        if value.get('run_id') == job['run_id']:
            matches.append((path.parent, value))
    if len(matches) != 1:
        raise AuditError('重新核验的原记录归属不唯一')
    workspace, manifest = matches[0]
    if (workspace.resolve().parent != root or workspace.is_symlink()
            or manifest.get('status') != 'completed'
            or manifest.get('snapshot_sha256') != expected_snapshot_sha256
            or _sha(workspace/'snapshot.json') != expected_snapshot_sha256):
        raise AuditError('已完成记录位置、状态或快照指纹不符')
    archive = Path(job['delivery']['input_file']).resolve()
    if not archive.is_file() or _sha(archive) != job['delivery']['sha256']:
        raise AuditError('原ZIP丢失或内容指纹变化')
    connection = ModelSettingsStore(root).snapshot()
    if connection.provider != manifest['producer_model'] or connection.model != manifest['audit_model']:
        raise AuditError('不能更换平台或模型重新核验原记录')
    traces = load_trace(workspace)
    if finish_unreviewed:
        snapshot=json.loads((workspace/'snapshot.json').read_text(encoding='utf-8'))
        review=snapshot.get('verification',{}).get('logic_review',{})
        if review.get('status')!='unavailable' or review.get('model_reviewed') is not False:
            raise AuditError('最终逻辑核验并非未完成状态，不能重复补跑')
        if connection.reasoning_effort!=manifest['reasoning_effort']:
            raise AuditError('本次模型推理强度与原记录不一致')
        from .final_review_resume import completed_business_packets, FinalReviewResumeProvider
        provider=FinalReviewResumeProvider(connection,completed_business_packets(traces,manifest,finish_unreviewed=True))
    else:
        provider = ResumeEvidenceProvider(connection, traces, run_id=job['run_id'],
                                           revalidate_from_call_id=from_call_id)
    qince = saved_qince(traces)
    if job['verifyCode'] and not qince.get('queryVerificationRegular'):
        raise AuditError('缺少原任务勤策快照，不能重新联网补造')

    # Keep an external per-job lock throughout publication; the ordinary resume
    # lock travels with the archived original during the two directory renames.
    locks = root/'.intake/revalidation-locks'
    locks.mkdir(exist_ok=True)
    outer_lock = locks/f'{job_id}.lock'
    with outer_lock.open('x', encoding='utf-8') as output:
        output.write(job_id)
    lock = workspace/'run-resume.lock'
    acquired = False
    version = None
    try:
        with lock.open('x', encoding='utf-8') as output:
            output.write(job_id)
        acquired = True
        # Never follow a link/junction out of the original record while staging.
        for item in chain((workspace,), workspace.rglob('*')):
            if item.is_symlink() or getattr(item.lstat(), 'st_file_attributes', 0) & 0x400:
                raise AuditError('重新核验目录存在符号链接或重解析点')
        version = root/'.revalidation-history'/job_id/uuid4().hex
        candidate_root = version/'candidate'
        candidate_root.mkdir(parents=True)
        candidate = candidate_root/workspace.name
        shutil.copytree(workspace, candidate, ignore=shutil.ignore_patterns('run-resume.lock'))
        before_traces = {p.name:_sha(p) for p in (workspace/'analysis/execution-trace').glob('step-*.json')}
        qince_path = version/'qince.json'
        atomic_write_json(qince_path, qince)
        atomic_write_text(version/'previous-job.json', job_text)
        receipt = {'job_id':job_id, 'run_id':job['run_id'], 'workspace_id':workspace.name,
                   'from_call_id':from_call_id, 'previous_snapshot_sha256':expected_snapshot_sha256,
                   'operation':'finish_unreviewed_summary' if finish_unreviewed else 'revalidate_original_call',
                   'state':'validating_candidate', 'callback_attempts':0}
        atomic_write_json(version/'receipt.json', receipt)
        result = run_persistent_audit(job['run_id'], producer_model=manifest['producer_model'],
            input_dir=archive.parent, worktrees_root=candidate_root, evidence_provider=provider,
            model=connection.model, reasoning_effort=connection.reasoning_effort, input_source='input',
            biz_type=job['bizType'], large_venue_fee=job['largeVenueFee'],
            cvs_otc_channel=job.get('cvsOtcChannel') is True, verify_code=job['verifyCode'],
            qince_data_path=qince_path if job['verifyCode'] else None, resume_workspace=candidate,
            revalidation_snapshot_sha256=expected_snapshot_sha256, workbench_url=manifest['workbench_url'])
        candidate_manifest = json.loads((candidate/'manifest.json').read_text(encoding='utf-8'))
        if (result.get('status') != 'completed' or candidate_manifest.get('status') != 'completed'
                or candidate_manifest.get('run_id') != job['run_id']
                or (not finish_unreviewed and not provider.revalidation_target_seen)
                or candidate_manifest.get('producer_model') != connection.provider
                or candidate_manifest.get('audit_model') != connection.model
                or candidate_manifest.get('snapshot_sha256') != _sha(candidate/'snapshot.json')
                or any(_sha(candidate/'analysis/execution-trace'/name) != digest
                       for name,digest in before_traces.items())):
            raise AuditError('候选版本未完成指定原调用核验或改变了历史执行链，保留原完成版本')
        if finish_unreviewed:
            final_snapshot=json.loads((candidate/'snapshot.json').read_text(encoding='utf-8'))
            final_review=final_snapshot.get('verification',{}).get('logic_review',{})
            if final_review.get('model_reviewed') is not True or final_review.get('status')!='passed':
                raise AuditError('候选版本的最终逻辑核验仍未完成，保留原版本')
        if (job_path.read_text(encoding='utf-8') != job_text
                or json.loads((workspace/'manifest.json').read_text(encoding='utf-8')) != manifest
                or _sha(workspace/'snapshot.json') != expected_snapshot_sha256):
            raise AuditError('原记录在核验期间变化，不覆盖并发结果')
        receipt.update(state='publishing', reused_checkpoints=provider.reused_checkpoints,
                       revalidated_stages=getattr(provider,'revalidated_stages',[]))
        atomic_write_json(version/'receipt.json', receipt)
        previous = version/'previous'
        # Both absolute destinations are under the explicitly selected root.
        if not previous.resolve().is_relative_to(root) or candidate.resolve().parent != candidate_root.resolve():
            raise AuditError('重新核验发布路径超出原工作台')
        workspace.rename(previous)
        try:
            candidate.rename(workspace)
            published_result = deepcopy(result)
            for key in ('worktree','output','static_html','snapshot','analysis_summary'):
                if key in published_result:
                    relative = Path(published_result[key]).relative_to(candidate)
                    published_result[key] = str(workspace/relative)
            updated = deepcopy(job)
            updated.update(result=published_result, status='completed', failure=None,
                           finished_at=utc_now(), updated_at=utc_now())
            updated.setdefault('revalidation_history', []).append({
                'version':str(version.relative_to(root)), 'from_call_id':from_call_id,
                'previous_snapshot_sha256':expected_snapshot_sha256,
                'snapshot_sha256':candidate_manifest['snapshot_sha256']})
            atomic_write_json(job_path, updated)
            receipt['state'] = 'published'
            atomic_write_json(version/'receipt.json', receipt)
        except BaseException:
            if workspace.exists():
                workspace.rename(version/'unpublished-candidate')
            previous.rename(workspace)
            atomic_write_text(job_path, job_text)
            raise
        (previous/'run-resume.lock').unlink(missing_ok=True)
        return published_result
    except BaseException as error:
        if version is not None and (version/'receipt.json').is_file():
            receipt.update(state='not_published', error_type=type(error).__name__)
            atomic_write_json(version/'receipt.json', receipt)
        raise
    finally:
        if acquired:
            lock.unlink(missing_ok=True)
        outer_lock.unlink(missing_ok=True)
