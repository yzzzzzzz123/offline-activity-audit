"""Continue an import failure before final review without repeating paid audits."""
from copy import deepcopy

from .common import AuditError
from .pdf_materials import PdfEvidenceProvider


def completed_business_packets(traces, manifest, *, finish_unreviewed=False):
    failure = manifest.get('failure') or {}
    if finish_unreviewed:
        previous = [item.get('previous_failure') or {} for item in manifest.get('resume_history', [])]
        if manifest.get('status') != 'completed' or not any(
                item.get('type') == 'ImportError' and 'restore_reports' in item.get('message', '') for item in previous):
            raise AuditError('仅支持补完已恢复本次导入故障但最终核验仍不可用的记录')
        start = max(i for i,e in enumerate(traces) if e['type'] == 'run.started')
        end = next((i for i in range(start,len(traces)) if traces[i]['type']=='workflow.logic_review.started'),len(traces))
        traces=traces[start:end]
    elif failure.get('type') != 'ImportError' or 'restore_reports' not in failure.get('message', ''):
        return None
    if any(e['type'] in {'workflow.logic_review.started', 'workflow.report.verified'} for e in traces):
        raise AuditError('最终核验已经开始，不能采用核验前恢复断点')
    runs = {e['data'].get('run_id') for e in traces if e['type'] == 'run.started'}
    if runs != {manifest['run_id']}:
        raise AuditError('已完成审核断点不属于当前唯一run')
    checkpoints = [e for e in traces if e['type'] == 'workflow.pdf_materials.validated']
    if len(checkpoints) != 1:
        raise AuditError('已完成审核断点缺失或不唯一，停止避免重做模型审核')
    checkpoint = checkpoints[0]
    from .pdf_policy import POLICY_VERSION
    if checkpoint['data'].get('policy_version') != POLICY_VERSION:
        raise AuditError('审核规则版本已改变，不能直接复用原业务结果')
    packets = deepcopy(checkpoint['data'].get('packets', []))
    expected = {p['route']['scenario'] for p in packets if p.get('audit_evidence')}
    decisions = {e['data']['scenario'] for e in traces
                 if e['type'] == 'workflow.result.validated' and e['sequence'] > checkpoint['sequence']}
    if not packets or not expected or expected != decisions:
        raise AuditError('缺少完整业务审核及结果断点，禁止从不完整结果继续封存')
    if len({p['archive_id'] for p in packets}) != len(packets):
        raise AuditError('审核断点资料包身份重复')
    for packet in packets:
        evidence = packet.get('audit_evidence', {})
        if evidence.get('_agent_mode') != 'main_three' or '_agent_assignments' in evidence:
            continue
        # Earlier child results saved owners in the evidence and complete
        # assignments in the run trace. Restore that original assignment,
        # rather than inferring it from today's rule routing or redoing work.
        starts = [e for e in traces if e['type'] == 'audit.agents.started'
                  and e['sequence'] < checkpoint['sequence']
                  and e['data'].get('archive_id') == packet['archive_id']]
        if len(starts) != 1:
            raise AuditError('旧断点缺少唯一的原子任务分工，不能推测恢复')
        workers = starts[0]['data']['workers']
        assignments = {worker['agent_id']: deepcopy(worker['rule_ids']) for worker in workers}
        owners = {rule: role for role, rules in assignments.items() for rule in rules}
        if (len(assignments) != len(workers) or set(assignments) != {'visual', 'documents', 'numbers'}
                or len(owners) != sum(len(rules) for rules in assignments.values())
                or any(owners.get(rule) != role for rule, role in evidence.get('_agent_owners', {}).items())):
            raise AuditError('旧断点子任务分工与已保存审核归属不一致')
        evidence['_agent_assignments'] = assignments
    return packets


class FinalReviewResumeProvider(PdfEvidenceProvider):
    def __init__(self, connection, packets):
        super().__init__(connection.model, connection.reasoning_effort, connection=connection)
        self.packets = {p['archive_id']: deepcopy(p) for p in packets}
        self.reused = []
        self.reused_checkpoints = []
        self.pending_label = '最终逻辑核验'
        self._restored_classifications = set()
        from uuid import uuid4
        self._conversation_namespace = 'final-review-' + uuid4().hex
        # Resolve both sides of the repaired import before any paid request.
        from .agent_orchestration import restore_reports
        from . import final_logic_review
        if any(p.get('audit_evidence', {}).get('_agent_mode') == 'main_three' for p in packets):
            restore_reports(self, packets)

    @staticmethod
    def _identity(unit):
        # The same ZIP is independently hash-checked by the resume entry.
        # Images are rendered again from that ZIP into this run's new temporary
        # directory; only their ephemeral filesystem paths may differ.
        return {k: v for k, v in unit.items() if k != 'image'}

    def _packet(self, case):
        packet = self.packets.get(case['archive_id'])
        if packet is None:
            raise AuditError('恢复出现原断点之外的资料包，禁止新增审核')
        saved = {u['unit_id']: u for u in packet['units']}
        for unit in case['units']:
            original = saved.get(unit['unit_id'])
            if original is None or self._identity(original) != self._identity(unit):
                raise AuditError('原始资料来源或事实发生变化，不能复用已完成审核')
        return packet

    def _record(self, aid, stage):
        from .execution_trace import record_step
        label = aid + ':' + stage
        self.reused.append(label)
        self.reused_checkpoints.append({'archive_id': aid, 'stage': stage, 'model_calls': 0})
        record_step('audit.same_run_business_checkpoint_reused', '复用本单已完成审核，继续最终核验',
                    {'archive_id': aid, 'stage': stage, 'model_calls': 0,
                     'same_run_only': True, 'source_identity_checked': True})

    def classify(self, case, temporary_root):
        if case['archive_id'] in self._restored_classifications:
            # A later classification call belongs to explicitly targeted final
            # review rework; it must not silently return the old inventory.
            return super().classify(case, temporary_root)
        packet = self._packet(case)
        if case.get('selected_scenario') != packet['route'].get('scenario'):
            raise AuditError('本次核销方式与原断点不同')
        ids = {u['unit_id'] for u in case['units']}
        documents = [deepcopy(d) for d in packet['documents'] if d['unit_id'] in ids]
        if {d['unit_id'] for d in documents} != ids:
            raise AuditError('资料读取断点覆盖不完整')
        self._record(case['archive_id'], 'classification')
        self._restored_classifications.add(case['archive_id'])
        return {'documents': documents, 'classification': deepcopy(packet['classification'])}

    def audit(self, case, temporary_root):
        packet = self._packet(case)
        if ({u['unit_id'] for u in case['units']} != {u['unit_id'] for u in packet['units']}
                or case['documents'] != packet['documents']
                or case['materials'] != packet['classification']['materials']
                or case['scenario'] != packet['route']['scenario']):
            raise AuditError('本单业务来源、资料盘点或类型与已完成审核不一致')
        self._audit_cases[case['archive_id']] = deepcopy(case)
        self._record(case['archive_id'], 'business_audit')
        return deepcopy(packet['audit_evidence'])
