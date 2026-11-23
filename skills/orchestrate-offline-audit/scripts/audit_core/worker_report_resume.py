"""Recover complete worker audits rejected by recognized report-merge defects.

This narrowly recognized failure replays intake requests under their original
source identities, then restores all three completed self-checks. It never
reruns a worker or accepts a partially finished audit as a completed report.
"""
from copy import deepcopy
import json
from uuid import uuid4

from .common import AuditError
from .pdf_materials import PdfEvidenceProvider


LEGACY_FAILURE = '子Agent不能覆盖其他角色的审核项或重复写入同一结果。'
ROLE_FAILURE_PREFIX = '子Agent不能覆盖其他角色的审核项：'


def _recover_omitted_photo_reviews(traces, requests, role, selected, review,
                                  review_sequence, done, value, failure_message):
    """Prove the stale merge from original model output; never invent a verdict."""
    from .agent_orchestration import check_report
    from .model_self_check import merge_self_check_result
    original = review.get('model_result')
    if (role not in {'documents', 'numbers'} or not isinstance(original, dict)
            or not original.get('photo_reviews')):
        raise AuditError('原自测输入没有可确认的跨角色照片字段继承故障')
    selected_set = set(selected)
    photos = original['photo_reviews']
    # Only this fully out-of-role container is recognized. Mixed/owned rows
    # need the responsible model, rather than silently discarding its work.
    if (not isinstance(photos, list) or any(not p.get('checks') for p in photos)
            or any(c.get('rule_id') in selected_set for p in photos for c in p['checks'])):
        raise AuditError('原照片字段包含本角色判断或结构不完整，不能按继承故障恢复')
    check_report({'checks': original.get('checks')}, selected)
    answers = [e for e in traces if e['type'] == 'ai.validated'
               and e.get('agent_id') == role and review_sequence < e['sequence'] < done['sequence']
               and e.get('call_id') in requests
               and {'ready', 'checks', 'notes'}.issubset(
                   requests[e['call_id']]['data']['output_schema'].get('required', []))]
    finals = [e for e in answers if e['data'].get('validated_result', {}).get('ready') is True]
    if len(finals) != 1:
        raise AuditError('缺少唯一原始已完成自测回答，禁止推测修正')
    event = finals[0]
    request = requests[event['call_id']]
    answer = event['data']['validated_result']
    if (request.get('agent_id') != role or not review_sequence < request['sequence'] < event['sequence']
            or request['data'].get('attempt') != event['data'].get('attempt')
            or any(answer.get(k) for k in ('tool_requests', 'image_requests', 'source_requests'))
            or 'photo_reviews' in answer or answer.get('notes') != done['data'].get('notes')):
        raise AuditError('原始自测尚有请求、已返回照片判断或身份不一致，不能代替模型恢复')
    try:
        visible = json.loads(event['data']['visible_response'])
    except (KeyError, TypeError, ValueError) as error:
        raise AuditError('原始自测可见回答缺失或不可核验') from error
    if visible != answer:
        raise AuditError('原始自测回答与已验证结果不一致')
    check_report({'checks': answer.get('checks')}, selected)
    if merge_self_check_result(original, answer) != value:
        raise AuditError('原完成结果不是已确认的旧字段合并，禁止改写模型判断')
    try:
        check_report(value, selected)
    except AuditError as error:
        if str(error) != failure_message:
            raise AuditError('原角色错误与中断记录不一致') from error
    else:
        raise AuditError('原完成结果没有跨角色错误，不能借此恢复其他故障')
    corrected = merge_self_check_result(original, answer, agent_id=role)
    expected = deepcopy(value)
    expected.pop('photo_reviews', None)
    if corrected != expected:
        raise AuditError('恢复超出旧照片字段清理范围，禁止改写业务结果')
    check_report(corrected, selected)
    return corrected, {'kind': 'omitted_foreign_photo_reviews', 'removed_field': 'photo_reviews',
        'removed_review_count': len(photos), 'answer_call_id': event['call_id'],
        'answer_sequence': event['sequence'], 'input_sequence': review_sequence,
        'business_answers_changed': False, 'original_trace_preserved': True}


def completed_worker_checkpoint(traces, manifest):
    failure = manifest.get('failure') or {}
    message = failure.get('message') or ''
    stale_merge = message.startswith(ROLE_FAILURE_PREFIX)
    if failure.get('type') != 'AuditError' or (message != LEGACY_FAILURE and not stale_merge):
        return None
    from .agent_orchestration import WORKERS, check_report
    if {e['data'].get('run_id') for e in traces if e['type'] == 'run.started'} != {manifest['run_id']}:
        raise AuditError('子任务恢复断点不属于唯一原run')
    if any(e['type'] in {'workflow.logic_review.started', 'workflow.pdf_materials.validated',
                         'workflow.report.verified'} for e in traces):
        raise AuditError('子任务合并已经完成，不能使用合并前恢复入口')
    starts = [e for e in traces if e['type'] == 'audit.agents.started']
    if len(starts) != 1:
        raise AuditError('缺少唯一原子任务分工，停止而不猜测恢复')
    start = starts[0]
    aid = start['data']['archive_id']
    workers = start['data']['workers']
    assignments = {w['agent_id']: w['rule_ids'] for w in workers}
    if (set(assignments) != set(WORKERS) or len(workers) != len(WORKERS)
            or len({r for ids in assignments.values() for r in ids}) != sum(map(len, assignments.values()))):
        raise AuditError('原子任务分工缺失、重复或跨角色重叠')
    failures = [e for e in traces if e['type'] == 'audit.agents.worker_failed']
    if (len(failures) != 1 or failures[0]['data'].get('archive_id') != aid
            or failures[0]['data'].get('error') != message):
        raise AuditError('子任务中断不是唯一已识别的报告合并故障')
    failed_role = failures[0]['data']['worker_agent_id']
    if failed_role not in assignments:
        raise AuditError('失败子任务不属于原分工')
    requests = {e['call_id']: e for e in traces if e['type'] == 'ai.request' and e['data'].get('attempt') == 1}
    inventories = [e for e in traces if e['type'] == 'ai.validated' and e.get('call_id') in requests
                   and 'candidate_scenarios' in requests[e['call_id']]['data']['output_schema'].get('properties', {})]
    if len(inventories) != 1:
        raise AuditError('资料盘点模型断点缺失或不唯一')

    def observation_record(role, prefix, before):
        records = [e for e in traces if e['type'] == 'analysis.observation'
                   and e.get('agent_id') == role and start['sequence'] < e['sequence'] < before
                   and e['data'].get('artifact', '').rsplit('/', 1)[-1].startswith(prefix)]
        if not records:
            raise AuditError('已完成子任务缺少原始输入或自测结果')
        return records[-1]

    def observation(role, prefix, before):
        return deepcopy(observation_record(role, prefix, before)['data']['observation'])

    saved = {}
    for role in WORKERS:
        completed = [e for e in traces if e['type'] == 'audit.model_self_check.completed'
                     and e.get('agent_id') == role and e['sequence'] > start['sequence']]
        if len(completed) != 1:
            raise AuditError('子任务自测未完整完成或存在多个版本，禁止推测复用')
        done = completed[0]
        value = deepcopy(done['data'].get('validated_result'))
        if (done['data'].get('model_is_authoritative') is not True
                or done['data'].get('rule_ids') != assignments[role]):
            raise AuditError('子任务自测身份或分工与原记录不一致')
        raw = observation(role, 'model-self-check-result-', done['sequence'])
        if raw.get('model_is_authoritative') is not True or raw.get('result') != value:
            raise AuditError('子任务完成事件与已保存自测结果不一致')
        original = observation(role, 'model-owned-audit-input-', done['sequence'])
        review_record = observation_record(role, 'model-self-check-input-', done['sequence'])
        review = deepcopy(review_record['data']['observation'])
        repair = None
        if stale_merge and role == failed_role:
            value, repair = _recover_omitted_photo_reviews(traces, requests, role, assignments[role],
                review, review_record['sequence'], done, value, message)
        else:
            check_report(value, assignments[role])
        reports = [e['data'] for e in traces if e['type'] == 'audit.agents.worker_completed'
                   and e.get('agent_id') == role and e['data'].get('archive_id') == aid]
        if role == failed_role:
            if reports:
                raise AuditError('失败子任务已有完成报告，停止而不重复合并')
            groups = [value['checks'], *[p['checks'] for p in value.get('photo_reviews', [])]]
            if not stale_merge and not any(len([c['rule_id'] for c in group]) != len({c['rule_id'] for c in group}) for group in groups):
                raise AuditError('原结果没有触发旧重复编号限制，不能借此恢复其他故障')
        elif (len(reports) != 1 or reports[0]['result'] != value
              or reports[0]['assigned_rule_ids'] != assignments[role]):
            raise AuditError('已完成子任务报告缺失、重复或与自测不一致')
        saved[role] = {'value': value, 'original': original, 'review': review,
                       'report': deepcopy(reports[0]) if reports else None,
                       'source_sequence': done['sequence'], 'structural_repair': repair}
    return {'archive_id': aid, 'assignments': deepcopy(assignments), 'workers': saved,
            'inventory_call_id': inventories[0]['call_id']}


class WorkerReportResumeProvider(PdfEvidenceProvider):
    def __init__(self, connection, traces, checkpoint):
        super().__init__(connection.model, connection.reasoning_effort, connection=connection)
        from .failed_upload_resume import ResumeEvidenceProvider
        self.reader = ResumeEvidenceProvider(connection, traces,
            revalidate_from_call_id=checkpoint['inventory_call_id'])
        self.checkpoint = deepcopy(checkpoint)
        self.reused = []
        self.reused_checkpoints = []
        self.pending_label = '最终逻辑核验'
        self._conversation_namespace = 'worker-report-' + uuid4().hex
        self._restored = False
        self._classified = False
        self.reader._conversation_namespace = self._conversation_namespace

    def classify(self, case, root):
        if self._classified:
            return super().classify(case, root)
        if case['archive_id'] != self.checkpoint['archive_id']:
            raise AuditError('出现原断点之外的资料包，禁止扩大核销')
        value = self.reader.classify(case, root)
        self.reused.extend(self.reader.reused)
        self.reused_checkpoints.extend(self.reader.reused_checkpoints)
        self._classified = True
        return value

    def audit(self, case, root):
        from .agent_orchestration import (WORKERS, scoped_case, expand_sources,
                                          source_catalog, report_for, owner)
        from .audit_rag import EXTRA_OWNER
        from .execution_trace import record_step
        from .model_metrics import save_model_observations
        if (self._restored or not self._classified
                or case['archive_id'] != self.checkpoint['archive_id']):
            raise AuditError('子任务恢复不能重复使用或跨资料包执行')
        catalog = source_catalog(case)
        reports = []
        for role in WORKERS:
            saved = self.checkpoint['workers'][role]
            original, review = saved['original'], saved['review']
            scoped = scoped_case(case, role)
            selected = [s['id'] for s in scoped['_agent_rule_specs']]
            if (selected != self.checkpoint['assignments'][role]
                    or original.get('rules') != scoped['_agent_rule_specs']
                    or original.get('scenario') != case['scenario']
                    or original.get('flags') != case['flags']
                    or original.get('materials') != case['materials']
                    or review.get('agent_id') != role or review.get('source_catalog') != catalog):
                raise AuditError('子任务规则、资料盘点或完整来源目录发生变化，禁止复用')
            documents = review.get('documents')
            if not isinstance(documents, list) or len({d['unit_id'] for d in documents}) != len(documents):
                raise AuditError('原子任务自测缺少完整可读取来源')
            expand_sources(scoped, case, [d['unit_id'] for d in documents])
            if scoped['documents'] != documents:
                raise AuditError('原子任务原始资料、事实或顺序发生变化，禁止重用判断')
            report = report_for(scoped, saved['value'], (saved['report'] or {}).get('stage_advice', []))
            if saved['report'] is not None and report != saved['report']:
                raise AuditError('已完成子任务的来源或报告内容改变')
            reports.append(report)
        aid = case['archive_id']
        self._audit_cases[aid] = deepcopy(case)
        self._agent_reports = {aid: reports}
        checks = [deepcopy(c) for r in reports for c in r['result']['checks']]
        value = {'checks': checks, '_agent_mode': 'main_three',
                 '_agent_owners': {c['rule_id']: owner(c['rule_id']) for c in checks},
                 '_agent_assignments': deepcopy(self.checkpoint['assignments'])}
        photos = [deepcopy(p) for r in reports for p in r['result'].get('photo_reviews', [])]
        if photos:
            value['photo_reviews'] = photos
        for report in reports:
            for key, ids in EXTRA_OWNER.items():
                if key in report['result'] and ids & set(report['assigned_rule_ids']):
                    value[key] = deepcopy(report['result'][key])
            self._soft_check_advice.extend(report['stage_advice'])
            label = aid + ':' + report['agent_id']
            self.reused.append(label)
            receipt = {'archive_id': aid, 'agent_id': report['agent_id'], 'model_calls': 0,
                       'source_sequence': self.checkpoint['workers'][report['agent_id']]['source_sequence'],
                       'same_run_only': True, 'source_identity_checked': True}
            repair = self.checkpoint['workers'][report['agent_id']].get('structural_repair')
            if repair:
                receipt['structural_repair'] = deepcopy(repair)
            self.reused_checkpoints.append(receipt)
            save_model_observations('agent-report-' + aid + '-' + report['agent_id'], report)
            record_step('audit.same_run_business_checkpoint_reused', '复用本单已完成子任务，继续汇总核验', receipt)
        self._restored = True
        return value
