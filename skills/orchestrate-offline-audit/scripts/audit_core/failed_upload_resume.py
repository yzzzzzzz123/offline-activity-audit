"""Explicit local recovery of a failed upload in its original run directory.

Only validated answers from this same ZIP/run can be replayed, through current
validators. Invalid answers are never replayed. The normal audit runner handles
all pending model work, publication, metrics and temporary-file cleanup.
"""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .common import AuditError
from .pdf_materials import PdfEvidenceProvider
from .resume_request_identity import (current_request, request_identity,
                                      reading_manifest_identities, reading_manifest_identity)


def load_trace(workspace):
    return [json.loads(p.read_text(encoding='utf-8')) for p in
            sorted((Path(workspace)/'analysis/execution-trace').glob('step-*.json'))]


class ResumeEvidenceProvider(PdfEvidenceProvider):
    def __init__(self, connection, traces, *, run_id=None, revalidate_from_call_id=None,
                 interrupted_run=False):
        super().__init__(connection.model, connection.reasoning_effort, connection=connection)
        self.saved={}
        self.reused=[]
        self.reused_checkpoints=[]
        self._consumed=set()
        self.pending_started=False
        self.revalidation = revalidate_from_call_id is not None
        # A killed process leaves a request without any terminal event. That is
        # still a known stopping point when the run archive itself says it was
        # interrupted; otherwise a dangling request proves nothing.
        self.interrupted_run = bool(interrupted_run)
        self.revalidation_target_seen = False
        self.revalidated_stages = []
        self._reading_checkpoint_members = {}
        self._reading_checkpoint_groups = {}
        runs={e['data'].get('run_id') for e in traces if e['type']=='run.started'}
        if len(runs)!=1 or not next(iter(runs), None):
            raise AuditError('断点必须唯一归属同一原核销run，禁止跨单复用')
        self.run_id=next(iter(runs))
        if run_id is not None and run_id!=self.run_id:
            raise AuditError('断点run与原任务归属不一致')
        requests={}
        failures=[]
        self._reference_source_budget = next((e['data']['max_sources_per_request'] for e in traces
            if e['type']=='image.reference_facts.partition'
            and type(e['data'].get('max_sources_per_request')) is int
            and 1 < e['data']['max_sources_per_request'] <= 128), None)
        for event in traces:
            if event['type']!='ai.request':continue
            call_id=event.get('call_id');attempt=event['data'].get('attempt')
            if not isinstance(call_id,str) or not call_id or type(attempt) is not int or attempt<1:
                raise AuditError('断点请求缺少调用及尝试身份')
            key=(call_id,attempt)
            if key in requests:raise AuditError('同一调用尝试存在多个请求，断点身份有歧义')
            requests[key]=event
            if attempt==1:
                members=reading_manifest_identities(event['data']['input_text'])
                if members is not None:
                    label=re.sub(r' · 第\d+次.*$', '', event['label'])
                    cache_label=re.sub(r' · 单图 \d+/\d+$', '', label)
                    self._reading_checkpoint_members.setdefault(cache_label,set()).update(members)
                    self._reading_checkpoint_groups.setdefault(label,[]).append(members)
        def paired(event):
            call_id=event.get('call_id');attempt=event['data'].get('attempt')
            request=requests.get((call_id,attempt))
            base=requests.get((call_id,1))
            if request is None or base is None or request['sequence']>=event['sequence']:
                raise AuditError('断点结果未与本次调用的原始请求配对，禁止按阶段名称复用')
            first=base['data'];actual=request['data']
            selected = (getattr(connection, 'visual', None)
                        if first.get('images') or base.get('agent_id') == 'visual' else None) or connection
            label=re.sub(r' · 第\d+次.*$', '', base['label'])
            identity=request_identity(first,label)
            if (first['provider']!=selected.provider or first['model']!=selected.model
                    or first.get('reasoning_effort')!=selected.reasoning_effort
                    or any(actual.get(k)!=first.get(k) for k in
                           ('provider','model','reasoning_effort','skill','output_schema','images'))
                    or not isinstance(actual.get('input_text'),str)
                    or not actual['input_text'].startswith(first['input_text'])):
                raise AuditError('断点模型、Schema、图片或重试来源发生变化')
            if re.sub(r' · 第\d+次.*$', '', event['label'])!=label:
                raise AuditError('断点结果与请求的调用用途不一致')
            return label,identity,call_id
        for event in traces:
            if event['type']=='audit.execution_repair.completed':
                data=event['data'];call_id=data.get('source_call_id')
                request=requests.get((call_id,1))
                if request is None or data.get('original_validation_passed') is not True:
                    raise AuditError('现场修复结果缺少唯一原始请求身份')
                first=request['data']
                if first['provider']!=connection.provider or first['model']!=connection.model:
                    raise AuditError('现场修复断点模型发生变化')
                label=re.sub(r' · 第\d+次.*$', '', request['label'])
                identity=request_identity(first,label)
                value=data.get('validated_result')
                if not isinstance(value,dict):raise AuditError('现场修复没有完整有效答案')
                self.saved.setdefault(identity['request'],[]).append(
                    {'value':value,'sequence':event['sequence'],'call_id':call_id,'identity':identity})
            if event['type'] in {'ai.validation_failed','ai.failed'}:
                failures.append((event,*paired(event)))
            if event['type']=='ai.validated':
                label,identity,call_id=paired(event)
                value=event['data'].get('validated_result')
                if not isinstance(value,dict):raise AuditError('断点没有可校验的原始结果')
                self.saved.setdefault(identity['request'],[]).append(
                    {'value':value,'sequence':event['sequence'],'call_id':call_id,'identity':identity})
        if self.revalidation:
            target = requests.get((revalidate_from_call_id, 1))
            valid = [v for records in self.saved.values() for v in records
                     if v['call_id'] == revalidate_from_call_id]
            if target is None or len(valid) != 1:
                raise AuditError('重新核验必须明确指定本run内唯一已验证原调用，不能猜测受影响位置')
            self.revalidation_from = target['sequence']
            self.revalidation_from_call_id = revalidate_from_call_id
            self.pending_label = re.sub(r' · 第\d+次.*$', '', target['label'])
            self.pending_identity = valid[0]['identity']
            self._lineages = [request_identity(e['data'], re.sub(r' · 第\d+次.*$', '', e['label']))
                              for e in requests.values()
                              if e['data']['attempt'] == 1 and e['sequence'] >= self.revalidation_from]
        else:
            valid_calls={v['call_id'] for records in self.saved.values() for v in records}
            unresolved=[e for (call_id,attempt),e in requests.items() if attempt==1 and call_id not in valid_calls]
            # A user-stopped transport has a real request and interruption event,
            # but no validation failure. Never mistake an earlier recovered
            # retry for the current stopping point.
            interrupted={e.get('call_id') for e in traces if e['type']=='ai.interrupted'}
            recovery_state = next((e for e in reversed(traces) if e['type'] in {
                'audit.execution_repair.started', 'audit.execution_repair.completed',
                'audit.execution_repair.deferred'}), None)
            recovery_parent = (recovery_state['data'].get('validation', {}).get('source_call_id')
                if recovery_state and recovery_state['type']=='audit.execution_repair.started' else None)
            recovery_target = requests.get((recovery_parent, 1)) if recovery_parent not in valid_calls else None
            if recovery_target is not None:
                # An interrupted helper loop is not a normal workflow stage.
                # Resume its recorded parent request, not a failed operation27.
                # The normal path still binds the exact original source below.
                self.pending_label=re.sub(r' · 第\d+次.*$', '', recovery_target['label'])
                self.pending_identity=request_identity(recovery_target['data'],self.pending_label)
            elif unresolved and (unresolved[-1]['call_id'] in interrupted or self.interrupted_run):
                target=unresolved[-1]
                self.pending_label=re.sub(r' · 第\d+次.*$', '', target['label'])
                self.pending_identity=request_identity(target['data'],self.pending_label)
            else:
                if not failures:raise AuditError('失败记录没有可定位的未完成模型阶段')
                _,self.pending_label,self.pending_identity,pending_call=failures[-1]
                if pending_call in valid_calls:
                    raise AuditError('最后失败阶段已有有效结果，不能推测其他失败位置')

    @property
    def reference_source_budget(self):
        from .product_reference_batches import JOINT_SOURCE_BUDGET
        return (self._reference_source_budget or JOINT_SOURCE_BUDGET
                if not self.pending_started else JOINT_SOURCE_BUDGET)

    def _reading_cache_lookup(self, cache, key, manifest, *, label):
        # New deployment-cache hits must not remove sources from an existing
        # same-run reader request. Rebuild that original complete batch, then
        # let _call enforce its model/source/image identity and current schema.
        # Other images and normal work after the pending stage keep the cache.
        if ((self.revalidation or not self.pending_started)
                and reading_manifest_identity(manifest) in self._reading_checkpoint_members.get(label,())):
            return None,'same_run_checkpoint_layout'
        return super()._reading_cache_lookup(cache,key,manifest,label=label)

    def _reading_requests(self, entries, label):
        entries = list(entries)
        members = {reading_manifest_identity(entry['manifest']) for entry in entries}
        if self.revalidation or not self.pending_started:
            # An old *saved* multi-image answer can still be replayed intact
            # with full current source validation and zero image transmission.
            # An actual pending multi-image call is split by the normal provider.
            groups = self._reading_checkpoint_groups.get(label, [])
            if len(entries) > 1 and any(set(group) == members for group in groups):
                yield entries, label
                return
        yield from super()._reading_requests(entries, label)

    def _call(self, root, skill, schema, prompt, *, images, label, validator, **kwargs):
        if kwargs.get('transcription') and images:
            routed = self._request_provider(images=images)
            if routed is not self:
                return routed._call(root,skill,schema,prompt,images=images,label=label,validator=validator,**kwargs)
        from .model_authority import enabled, transport_schema, validate_transport
        model_owned = enabled() and not kwargs.get('transcription',False)
        if model_owned and not self.revalidation:
            # Source ZIP, run, model and conversation ownership were checked by
            # the resume entry. A business-stage answer is continued by this AI,
            # not forced through an old acceptance predicate or display label.
            self.pending_started = True
            return super()._call(root,skill,schema,prompt,images=images,label=label,validator=validator,**kwargs)
        if self.pending_started and not self.revalidation:
            return super()._call(root,skill,schema,prompt,images=images,label=label,validator=validator,**kwargs)
        request=current_request(self.connection,skill,schema,prompt,images,
                                transcription=kwargs.get('transcription',False))
        identity=request_identity(request,label)
        matches=[v for v in self.saved.get(identity['request'],[]) if v['sequence'] not in self._consumed]
        compatible=False
        if not matches:
            # Generation contracts may gain closed field choices after repair.
            # This is safe only for an identical typed source/purpose/model;
            # the old answer still has to pass the entire current contract.
            matches=[v for records in self.saved.values() for v in records
                     if v['sequence'] not in self._consumed and v['identity']['source']==identity['source']]
            compatible=bool(matches)
        if len(matches)>1:raise AuditError('同一来源请求匹配多个已验证断点，停止而不猜测选择')
        if matches:
            saved=matches[0]
            answer=deepcopy(saved['value'])
            from .pdf_evidence import validate
            if model_owned:
                validate_transport(answer,transport_schema(schema))
            else:
                validate(answer,schema)
                validator(answer)
            from .execution_trace import record_step
            record_step('audit.same_run_stage_reused','复用本单已校验阶段，不再次调用模型',
                {'label':label,'source_sequence':saved['sequence'],'source_call_id':saved['call_id'],
                 'request_identity':identity['request'],'model_calls':0,
                 'same_run_only':True,'current_validation_passed':True,
                 'same_source_current_contract':compatible,
                 'original_request_identity':saved['identity']['request']})
            self._consumed.add(saved['sequence'])
            self.reused.append(label)
            self.reused_checkpoints.append({'label':label,'source_sequence':saved['sequence'],
                                            'request_identity':identity['request'],
                                            'same_source_current_contract':compatible})
            if self.revalidation and saved['call_id'] == self.revalidation_from_call_id:
                self.revalidation_target_seen = True
            return answer
        if self.revalidation:
            if (not self.revalidation_target_seen or identity['lineage'] is None
                    or not any(v['lineage'] == identity['lineage'] for v in self._lineages)):
                raise AuditError('重新核验未找到受影响原调用或同来源审核依赖，停止而不整单重跑：'+label)
            # Each subsequent stage is matched independently. An affected
            # reference cannot turn this into unrestricted model execution.
            value = super()._call(root,skill,schema,prompt,images=images,label=label,validator=validator,**kwargs)
            self.revalidated_stages.append({'label':label,'request_identity':identity['request']})
            return value
        if label!=self.pending_label or identity['source']!=self.pending_identity['source']:
            raise AuditError('续跑前序阶段没有匹配的已验证断点；来源、输入或Schema不同，停止避免重做整单模型分析：'+label)
        self.pending_started=True
        return super()._call(root,skill,schema,prompt,images=images,label=label,validator=validator,**kwargs)


def saved_qince(traces):
    """Rebuild the exact already-queried tables, no live Qince calls."""
    from .qince_context import ENDPOINTS
    methods={endpoint:method for method,endpoint in ENDPOINTS.items()}
    result={}
    for event in traces:
        if event['type']!='qince.response':continue
        data=event['data']
        endpoint=str(data['endpoint']).rstrip('/')
        method=methods.get(endpoint,endpoint.rsplit('/',1)[-1])
        result.setdefault(method,[]).extend(data.get('rows',[]))
    for method, rows in result.items():
        unique={json.dumps(row,ensure_ascii=False,sort_keys=True):row for row in rows}
        result[method]=list(unique.values())
    result.setdefault('queryRegularSale',[])
    return result


def resume_local_upload(job_id, worktrees_root):
    """恢复本地上传、无回调的失败任务（工作台既有入口）。"""
    return resume_failed_job(job_id, worktrees_root, require_local=True)


def resume_failed_job(job_id, worktrees_root, *, require_local=False,
                      callback_sender=None, callback_config=None):
    """在原 run 目录继续失败任务，保留原 run_id、资料包和已校验读取结果。

    `require_local` 只为本地上传入口保留原有范围；勤策/资料上传投递的任务用默认
    False 续跑同一工作目录。`callback_sender`/`callback_config` 给出时按投递时
    的回调要求发送结果回调，载荷与正式服务同一构造函数，不改写核销结论。
    """
    from .workbench_store import atomic_write_json, project_run_status, utc_now
    from .workbench_runtime import run_persistent_audit
    from .model_settings import ModelSettingsStore
    root=Path(worktrees_root).resolve()
    if not re.fullmatch(r'[a-f0-9]{24}',job_id):raise AuditError('续跑job_id不安全')
    job_path=root/'.intake/jobs'/f'{job_id}.json'
    job=json.loads(job_path.read_text(encoding='utf-8'))
    if job.get('status')!='failed':
        raise AuditError('只有失败任务可以续跑')
    callback_required=job.get('callback',{}).get('required') is True
    if require_local and (job.get('input_source')!='input' or callback_required):
        raise AuditError('仅支持显式恢复本地上传、无回调的失败任务')
    if callback_required and (callback_sender is None or callback_config is None):
        raise AuditError('该任务投递时要求回调，续跑必须提供回调发送器与回调配置')
    if not job.get('input_source') and not job.get('object'):
        raise AuditError('续跑任务缺少投递来源，不能确认原资料包归属')
    matches=[]
    for path in root.glob('*/manifest.json'):
        manifest=json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('run_id')==job['run_id']:matches.append((path.parent,manifest))
    if len(matches)!=1:raise AuditError('续跑记录归属不唯一')
    workspace,manifest=matches[0]
    if manifest.get('status')!='failed':raise AuditError('续跑档案不是失败状态')
    archive=Path(job['delivery']['input_file']).resolve()
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest()!=job['delivery']['sha256']:
        raise AuditError('原ZIP丢失或内容指纹变化，禁止复用断点')
    connection=ModelSettingsStore(root).snapshot()
    if (connection.provider!=manifest['producer_model'] or connection.model!=manifest['audit_model']
            or connection.reasoning_effort!=manifest['reasoning_effort']):
        raise AuditError('本地工作台模型已改变，不能静默用其他模型继续原记录')
    traces=load_trace(workspace)
    interrupted=any(str((value or {}).get('code'))=='run_interrupted'
                    for value in (manifest.get('failure'),job.get('failure'))) or \
                str((job.get('failure') or {}).get('code'))=='service_restarted'
    from .final_review_resume import completed_business_packets, FinalReviewResumeProvider
    from .worker_report_resume import completed_worker_checkpoint, WorkerReportResumeProvider
    packets=completed_business_packets(traces,manifest)
    workers=completed_worker_checkpoint(traces,manifest) if packets is None else None
    provider=(FinalReviewResumeProvider(connection,packets) if packets is not None else
              WorkerReportResumeProvider(connection,traces,workers) if workers is not None else
              ResumeEvidenceProvider(connection,traces,run_id=job['run_id'],interrupted_run=interrupted))
    qince=saved_qince(traces)
    if job['verifyCode'] and not qince.get('queryVerificationRegular'):
        raise AuditError('原任务缺少勤策快照，不能猜测补齐查询身份')
    history=workspace/'analysis/resume-history'
    history.mkdir(exist_ok=True)
    index=len(list(history.glob('before-*.json')))+1
    lock=workspace/'run-resume.lock'
    with lock.open('x',encoding='utf-8') as f:f.write(job_id)
    try:
        atomic_write_json(history/f'before-{index:03d}.json',{'job':job,'manifest':manifest})
        qince_path=history/f'qince-{index:03d}.json'
        atomic_write_json(qince_path,qince)
        job.update(status='running',failure=None,finished_at=None,updated_at=utc_now())
        atomic_write_json(job_path,job)
        try:
            try:
                result=run_persistent_audit(job['run_id'],producer_model=manifest['producer_model'],
                    input_dir=archive.parent,worktrees_root=root,evidence_provider=provider,
                    model=connection.model,reasoning_effort=connection.reasoning_effort,
                    input_source=job.get('input_source') or 'oss',biz_type=job['bizType'],large_venue_fee=job['largeVenueFee'],
                    cvs_otc_channel=job.get('cvsOtcChannel') is True,verify_code=job['verifyCode'],
                    qince_data_path=qince_path if job['verifyCode'] else None,resume_workspace=workspace,
                    workbench_url=manifest['workbench_url'])
            except BaseException as error:
                job.update(status='failed',failure={'code':'audit_failed','message':str(error)},finished_at=utc_now())
                raise
            # The audit itself is done; a callback problem must not turn a
            # finished claim into an audit failure again.
            result=project_run_status(result)
            job.update(status='completed',failure=None,result=result,finished_at=utc_now())
            callback_error=None
            if callback_required:
                from .oss_intake import build_callback_result
                job['callback']={'required':True,'status':'sending','attempts':0,
                                 'http_status':None,'delivered_at':None}
                atomic_write_json(job_path,{**job,'status':'callback','updated_at':utc_now()})
                try:
                    delivered=callback_sender({'verifyCode':job['verifyCode'],'analyzeId':job['analyzeId'],
                        'result':build_callback_result(result)},callback_config)
                except BaseException as error:
                    callback_error=error
                    job['callback']={'required':True,'status':'failed','attempts':0,'http_status':None,
                                     'delivered_at':None,'error':str(error)[:500]}
                else:
                    job['callback']={'required':True,'status':'delivered',
                        'attempts':int(delivered.get('attempts') or 1),
                        'http_status':int(delivered.get('http_status') or 200),
                        'delivered_at':delivered.get('delivered_at') or utc_now()}
            if callback_error is not None:
                raise AuditError('核销已完成，但结果回调发送失败：%s' % callback_error)
        finally:
            job['updated_at']=utc_now()
            atomic_write_json(job_path,job)
            atomic_write_json(history/f'after-{index:03d}.json',{'job_id':job_id,'status':job['status'],
                'reused_stages':provider.reused,'pending_stage':provider.pending_label,
                'reused_checkpoints':provider.reused_checkpoints,
                'callback_attempts':job['callback']['attempts']})
        return result
    finally:
        lock.unlink(missing_ok=True)
