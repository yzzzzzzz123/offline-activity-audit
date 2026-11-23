"""One explicit local worker-count handoff at the next model-free task boundary."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import os
import re
import time

from .common import AuditError

NAME='worker-handoff.json'


def wait_before_audit(root, run_id):
    marker=Path(root)/'.intake'/NAME
    if not marker.is_file():return
    value=json.loads(marker.read_text(encoding='utf-8'))
    if value.get('old_service_pid')!=os.getppid():return
    if value.get('phase') not in {'waiting','ready'}:return
    if not re.fullmatch(r'[a-zA-Z0-9_-]+',run_id):raise AuditError('切换等待的run_id不安全')
    from .workbench_store import atomic_write_json
    held=marker.parent/'worker-handoff-held'
    held.mkdir(exist_ok=True)
    atomic_write_json(held/(run_id+'.json'),{'run_id':run_id,'runner_pid':os.getpid(),
        'parent_pid':os.getppid(),'handoff_id':value['handoff_id'],'model_calls':0})
    while marker.is_file():
        current=json.loads(marker.read_text(encoding='utf-8'))
        if current.get('handoff_id')!=value['handoff_id'] or current.get('phase') not in {'waiting','ready'}:return
        if time.time()>current['expires_at']:
            raise AuditError('并发切换等待超时；未调用模型，原资料已保留')
        time.sleep(0.25)


def restore_handoff(intake_root):
    """Restore only the journal explicitly prepared with no model in flight."""
    root=Path(intake_root)
    marker=root/NAME
    if not marker.is_file():return []
    value=json.loads(marker.read_text(encoding='utf-8'))
    if value.get('phase')!='ready' or value.get('old_service_pid')==os.getpid():return []
    if time.time()>value['expires_at']:raise AuditError('并发切换回执已过期，不自动恢复队列')
    jobs=value.get('jobs',[])
    if not jobs or len(jobs)>200:raise AuditError('并发切换队列范围无效')
    recovered=[]
    for saved in jobs:
        job_id=saved.get('job_id','')
        if not re.fullmatch(r'[a-f0-9]{24}',job_id):raise AuditError('切换队列job_id无效')
        current=json.loads((root/'jobs'/f'{job_id}.json').read_text(encoding='utf-8'))
        if current['run_id']!=saved['run_id'] or current['idempotency_fingerprint']!=saved['idempotency_fingerprint']:
            raise AuditError('切换队列身份改变')
        if current.get('status')=='completed':continue
        if saved.get('input_source')!='input' or saved.get('callback',{}).get('required') is not False:
            raise AuditError('本次切换只支持本地无回调待处理任务')
        archive=Path(saved['delivery']['input_file']).resolve()
        if not archive.is_relative_to(root.parent.parent/'input') or not archive.is_file():
            raise AuditError('切换原ZIP路径或文件无效')
        if hashlib.sha256(archive.read_bytes()).hexdigest()!=saved['delivery']['sha256']:
            raise AuditError('切换原ZIP指纹改变')
        recovered.append({**saved,'status':'downloaded','failure':None,'finished_at':None})
    from .workbench_store import atomic_write_json
    for job in recovered:atomic_write_json(root/'jobs'/f'{job["job_id"]}.json',job)
    value.update(phase='restored',restored_by_pid=os.getpid(),restored_job_ids=[j['job_id'] for j in recovered])
    atomic_write_json(marker,value)
    return recovered


def submission_from_job(job):
    return {'job_id':job['job_id'],'run_id':job['run_id'],'producer_model':job['producer_model'],
        'verify_code':job['verifyCode'],'analyze_id':job['analyzeId'],'biz_type':job['bizType'],
        'large_venue_fee':job.get('largeVenueFee'),'cvs_otc_channel':job.get('cvsOtcChannel') is True,
        'scenario':job.get('scenario'),'input_source':job.get('input_source'),
        'callback_required':job['callback']['required']}
