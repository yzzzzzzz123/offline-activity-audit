"""Explicit, bounded local-upload handoff to the current interactive AI session.

Disabled without a private, expiring allowlist. No HTTP inference, subprocess
agent, alternate renderer or business-rule changes are involved.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .common import AuditError
from .model_settings import ModelSelection

ENV = 'OFFLINE_AUDIT_CURRENT_SESSION'
PROVIDER = 'codex-session'
MODEL = 'current-session'


def _read(path: Path, limit=16*1024*1024):
    if path.is_symlink() or (hasattr(path,'is_junction') and path.is_junction()):
        raise AuditError('当前会话资料路径不能是链接')
    with path.open('rb') as stream:
        raw=stream.read(limit+1)
    if len(raw)>limit:
        raise AuditError('当前会话本批数据超过读取上限')
    return json.loads(raw)


def upload_override(worktrees_root, *, input_source, verify_code, input_dir, run_id):
    """Claim exactly one run for each explicitly authorized local ZIP identity."""
    root=Path(worktrees_root).resolve()
    path=root/'.settings/current-session-upload.json'
    if input_source!='input' or not path.is_file():
        return None
    config=_read(path,65536)
    if config.get('enabled') is not True:
        return None
    uploads=config.get('uploads',[])
    if not isinstance(uploads,list) or not 1<=len(uploads)<=32:
        raise AuditError('当前会话本地测试范围无效')
    match=[row for row in uploads if row.get('verifyCode')==verify_code]
    if not match:
        return None
    if len(match)!=1 or not re.fullmatch(r'HX\d{12}',str(verify_code)):
        raise AuditError('当前会话本地测试身份无效')
    expiry=datetime.fromisoformat(config['expires_at'])
    if expiry.tzinfo is None or expiry<=datetime.now(timezone.utc):
        raise AuditError('当前会话本地测试授权已到期，未回退外部模型')
    files=list(Path(input_dir).glob('*.zip'))
    if len(files)!=1:
        raise AuditError('当前会话本地测试须对应一份原始ZIP')
    with files[0].open('rb') as stream:
        digest=hashlib.file_digest(stream,'sha256').hexdigest()
    if digest!=match[0].get('sha256'):
        raise AuditError('当前会话本地测试ZIP与授权原件不一致')
    exchange=Path(config['exchange_root']).resolve()
    if not exchange.is_relative_to(root/'.session-model'):
        raise AuditError('当前会话交换目录必须位于本机核销临时范围内')
    exchange.mkdir(parents=True,exist_ok=True)
    case=exchange/verify_code
    case.mkdir(exist_ok=True)
    binding=case/'binding.json'
    record={'run_id':run_id,'verifyCode':verify_code,'sha256':digest,
            'expires_at':config['expires_at'],'exchange_root':str(case),
            'provider':PROVIDER,'model':MODEL}
    try:
        with binding.open('x',encoding='utf-8') as stream:
            json.dump(record,stream,ensure_ascii=False)
    except FileExistsError:
        if _read(binding,65536)!=record:
            raise AuditError('本单当前会话测试已绑定其他任务，未启动重复模型调用') from None
    return record


def selection_from_environment():
    raw=os.environ.get(ENV)
    if not raw:
        return None
    record=json.loads(raw)
    case=Path(record['exchange_root']).resolve(strict=True)
    if _read(case/'binding.json',65536)!=record:
        raise AuditError('当前会话本次模型快照与原上传任务不一致')
    return ModelSelection(PROVIDER,MODEL,'default','', '')


def exchange(model, instructions):
    """Wait for the current session's final JSON; the normal parser validates it."""
    from .execution_trace import record_step
    from .model_metrics import save_model_observations
    record=json.loads(os.environ[ENV])
    case=Path(record['exchange_root']).resolve(strict=True)
    if _read(case/'binding.json',65536)!=record:
        raise AuditError('当前会话模型响应身份不一致')
    call=hashlib.sha256(str(model.model_root).encode()).hexdigest()[:20]
    folder=case/(call+'-'+str(model.attempt))
    if not folder.exists() and len(list(case.glob('*/request.json')))>=64:
        raise AuditError('当前会话本单请求已达有界上限')
    folder.mkdir(exist_ok=True)
    request={'provider':PROVIDER,'model':MODEL,'run_id':record['run_id'],
             'verifyCode':record['verifyCode'],'attempt':model.attempt,
             'call_id':model.trace_call_id,'system_instructions':model.system_prompt,
             'input_text':instructions,'output_schema':_read(model.model_root/'schema.json'),
             'images':[{'path':str(p.resolve()),'name':p.name,
                        'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in model.images]}
    payload=json.dumps(request,ensure_ascii=False,separators=(',',':')).encode('utf-8')
    if len(payload)>16*1024*1024:
        raise AuditError('当前会话本批输入超过读取上限')
    checksum=hashlib.sha256(payload).hexdigest()
    target=folder/'request.json'
    temporary=folder/'request.tmp'
    temporary.write_bytes(payload);temporary.replace(target)
    (folder/'ready.json').write_text(json.dumps({'request_sha256':checksum,
        'response_path':str(folder/'response.json'),'verifyCode':record['verifyCode'],
        'stage':model.model_root.name},ensure_ascii=False),encoding='utf-8')
    record_step('ai.transport','本批原资料交由当前Codex会话分析',{
        'provider':PROVIDER,'model':MODEL,'request_sha256':checksum,
        'image_count':len(model.images),'api_request':False,'delegated_agents_count':0,
        'usage_available':False},status='started',call_id=model.trace_call_id)
    progress_name='call-progress-'+hashlib.sha256(str(model.model_root).encode()).hexdigest()[:16]
    started=time.monotonic();last=started-10
    while time.monotonic()-started<model.timeout_seconds:
        now=time.monotonic()
        model.last_metrics={'phase':'awaiting_current_session','response_received':False,
                            'model_event_count':0,'usage_available':False,'api_request':False}
        if now-last>=5:
            save_model_observations(progress_name,{'attempt':model.attempt,**model.last_metrics})
            last=now
        response=folder/'response.json'
        if response.is_file():
            answer=_read(response,8*1024*1024)
            if (answer.get('request_sha256')!=checksum
                    or answer.get('responder')!='current_codex_session'
                    or not isinstance(answer.get('value'),dict)):
                raise AuditError('当前会话响应未对应本批请求')
            raw=json.dumps(answer['value'],ensure_ascii=False,separators=(',',':'))
            model.last_metrics.update(phase='model_output',response_received=True,model_event_count=1)
            model.last_output.append(raw)
            (folder/'consumed.json').write_text(json.dumps({'request_sha256':checksum}),encoding='utf-8')
            return raw
        time.sleep(0.5)
    raise AuditError('当前会话未在本批时限内返回分析，未生成完整核销结论')
