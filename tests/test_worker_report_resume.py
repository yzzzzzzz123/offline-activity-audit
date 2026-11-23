"""Recovery admits only complete self-checks from one unchanged worker split."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.agent_orchestration import WORKERS, report_for, scoped_case, source_catalog
from audit_core.common import AuditError
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_settings import ModelSelection
from audit_core.worker_report_resume import (LEGACY_FAILURE, completed_worker_checkpoint,
                                            ROLE_FAILURE_PREFIX, WorkerReportResumeProvider)
from tests.test_agent_orchestration import fixture_case


def checkpoint_fixture(*, stale_photos=False):
    case = fixture_case('self_procured_gift_material')
    traces = []

    def event(kind, data, role='main', call_id=None):
        traces.append({'sequence': len(traces) + 1, 'type': kind, 'agent_id': role,
                       'data': deepcopy(data), 'call_id': call_id})

    def observation(role, name, value):
        event('analysis.observation', {'artifact': 'analysis/model-observations/' + name + '.json',
                                      'observation': value}, role)

    event('run.started', {'run_id': 'fixture-run'})
    event('ai.request', {'attempt': 1, 'output_schema': {'properties': {'candidate_scenarios': {}}}},
          'documents', 'inventory-call')
    event('ai.validated', {}, 'documents', 'inventory-call')
    scopes = {role: scoped_case(case, role) for role in WORKERS}
    failed_role = 'documents' if stale_photos else 'numbers'
    failure = ROLE_FAILURE_PREFIX + 'photo_authenticity' if stale_photos else LEGACY_FAILURE
    event('audit.agents.started', {'archive_id': 'a01', 'workers': [
        {'agent_id': role, 'rule_ids': [s['id'] for s in scopes[role]['_agent_rule_specs']]}
        for role in WORKERS]})
    for role, scoped in scopes.items():
        selected = [s['id'] for s in scoped['_agent_rule_specs']]
        value = {'checks': [{'rule_id': rule, 'status': 'pass', 'reason': '已核对本次来源。',
                             'source_ids': [scoped['documents'][0]['unit_id']]} for rule in selected]}
        if role == 'numbers' and not stale_photos:
            original = next(check for check in value['checks'] if check['rule_id'] == 'pos_fields')
            original.update(status='fail', reason='门店甲缺少字段。', source_ids=['pos_excel'])
            value['checks'].append({**deepcopy(original), 'status': 'pass',
                                   'reason': '门店乙字段齐全。', 'source_ids': ['settlement']})
        observation(role, 'model-owned-audit-input-' + role, {
            'scenario': case['scenario'], 'flags': case['flags'], 'materials': case['materials'],
            'rules': scoped['_agent_rule_specs']})
        original = deepcopy(value)
        if stale_photos and role in {'visual', 'documents'}:
            original['photo_reviews'] = [{'unit_id': 'photo', 'checks': [{
                'rule_id': 'photo_authenticity', 'status': 'unknown',
                'reason': '照片仍需人工核实。', 'source_ids': ['photo']}]}]
            value = deepcopy(original)
        observation(role, 'model-self-check-input-' + role, {
            'agent_id': role, 'documents': scoped['documents'], 'source_catalog': source_catalog(case),
            'model_result': original})
        if stale_photos:
            call_id = role + '-self-check'
            answer = {'ready': True, 'checks': value['checks'], 'notes': []}
            if role == 'visual':
                answer['photo_reviews'] = value['photo_reviews']
            event('ai.request', {'attempt': 1, 'output_schema': {
                'properties': {'ready': {}, 'checks': {}, 'notes': {}},
                'required': ['ready', 'checks', 'notes']}}, role, call_id)
            event('ai.validated', {'attempt': 1, 'validated_result': answer,
                'visible_response': json.dumps(answer, ensure_ascii=False)}, role, call_id)
        observation(role, 'model-self-check-result-' + role, {
            'result': value, 'notes': [], 'model_is_authoritative': True})
        event('audit.model_self_check.completed', {'validated_result': value,
              'model_is_authoritative': True, 'rule_ids': selected, 'notes': []}, role)
        if role != failed_role:
            event('audit.agents.worker_completed', report_for(scoped, value), role)
    event('audit.agents.worker_failed', {'archive_id': 'a01', 'worker_agent_id': failed_role,
                                       'error': failure})
    manifest = {'run_id': 'fixture-run', 'status': 'failed',
                'failure': {'type': 'AuditError', 'message': failure}}
    return case, traces, manifest


def self_check_validation(traces, role='documents'):
    """Return the role's self-check completion, not the inventory validation."""
    return next(
        event for event in traces
        if event['type'] == 'ai.validated'
        and event['agent_id'] == role
        and 'validated_result' in event['data']
    )


class WorkerCheckpointTests(TestCase):
    def test_stale_photo_merge_recovery_uses_original_model_answer_and_keeps_other_reports(self):
        _, traces, manifest = checkpoint_fixture(stale_photos=True)
        before = deepcopy(traces)
        checkpoint = completed_worker_checkpoint(traces, manifest)
        self.assertNotIn('photo_reviews', checkpoint['workers']['documents']['value'])
        for role in ('visual', 'numbers'):
            saved = checkpoint['workers'][role]
            self.assertEqual(saved['value'], saved['report']['result'])
        original = next(e['data']['validated_result'] for e in traces
            if e['type'] == 'audit.model_self_check.completed' and e['agent_id'] == 'documents')
        self.assertEqual(checkpoint['workers']['documents']['value']['checks'], original['checks'])
        repair = checkpoint['workers']['documents']['structural_repair']
        self.assertEqual(repair['answer_call_id'], 'documents-self-check')
        self.assertFalse(repair['business_answers_changed'])
        self.assertEqual(traces, before)

    def test_stale_merge_requires_original_raw_final_answer(self):
        _, traces, manifest = checkpoint_fixture(stale_photos=True)
        traces = [
            e for e in traces
            if not (
                e['type'] == 'ai.validated'
                and e['agent_id'] == 'documents'
                and 'validated_result' in e['data']
            )
        ]
        with self.assertRaisesRegex(AuditError, '唯一原始已完成自测回答'):
            completed_worker_checkpoint(traces, manifest)

    def test_new_foreign_photo_answer_cannot_be_cleaned_as_old_merge(self):
        _, traces, manifest = checkpoint_fixture(stale_photos=True)
        event = self_check_validation(traces)
        event['data']['validated_result']['photo_reviews'] = []
        with self.assertRaisesRegex(AuditError, '已返回照片判断'):
            completed_worker_checkpoint(traces, manifest)

    def test_raw_answer_and_old_merge_must_be_identical_to_recorded_outputs(self):
        for tamper in ('visible', 'merged'):
            with self.subTest(tamper=tamper):
                _, traces, manifest = checkpoint_fixture(stale_photos=True)
                event = self_check_validation(traces)
                if tamper == 'visible':
                    event['data']['visible_response'] = '{}'
                else:
                    for e in traces:
                        if e['agent_id'] == 'documents' and e['type'] == 'audit.model_self_check.completed':
                            e['data']['validated_result']['checks'][0]['reason'] = 'manually changed'
                        if e['agent_id'] == 'documents' and e['type'] == 'analysis.observation' and 'model-self-check-result-' in e['data']['artifact']:
                            e['data']['observation']['result']['checks'][0]['reason'] = 'manually changed'
                with self.assertRaisesRegex(AuditError, '原始自测回答|旧字段合并'):
                    completed_worker_checkpoint(traces, manifest)

    def test_pending_or_mixed_owned_results_are_not_silently_discarded(self):
        for tamper in ('pending', 'mixed'):
            with self.subTest(tamper=tamper):
                _, traces, manifest = checkpoint_fixture(stale_photos=True)
                event = self_check_validation(traces)
                if tamper == 'pending':
                    event['data']['validated_result']['image_requests'] = ['original.jpg']
                else:
                    event = next(e for e in traces if e['type'] == 'analysis.observation' and e['agent_id'] == 'documents'
                        and 'model-self-check-input-' in e['data']['artifact'])
                    source = event['data']['observation']['model_result']
                    source['photo_reviews'][0]['checks'].append(deepcopy(source['checks'][0]))
                with self.assertRaisesRegex(AuditError, '尚有请求|包含本角色判断'):
                    completed_worker_checkpoint(traces, manifest)

    def test_known_failure_reuses_all_reports_without_collapsing_source_results(self):
        case, traces, manifest = checkpoint_fixture()
        checkpoint = completed_worker_checkpoint(traces, manifest)
        connection = ModelSelection(provider='deepseek', model='deepseek-flash',
                                    api_key='synthetic-only-not-a-key')
        with patch('audit_core.failed_upload_resume.ResumeEvidenceProvider') as reader, \
                TemporaryDirectory() as folder, collect_model_artifacts(Path(folder) / 'analysis'):
            reader.return_value.reused = []
            reader.return_value.reused_checkpoints = []
            provider = WorkerReportResumeProvider(connection, traces, checkpoint)
            provider.classify(case, Path(folder))
            value = provider.audit(case, Path(folder))
            self.assertEqual([c['status'] for c in value['checks'] if c['rule_id'] == 'pos_fields'], ['fail', 'pass'])
            self.assertEqual(len(provider._agent_reports['a01']), 3)
            reader.return_value.classify.assert_called_once()
            with self.assertRaises(AuditError): provider.audit(case, Path(folder))

    def test_incomplete_worker_cannot_be_restored(self):
        _, traces, manifest = checkpoint_fixture()
        traces = [e for e in traces if not (e['type'] == 'audit.model_self_check.completed'
                                           and e['agent_id'] == 'numbers')]
        with self.assertRaisesRegex(AuditError, '自测未完整完成'):
            completed_worker_checkpoint(traces, manifest)

    def test_foreign_rule_cannot_be_smuggled_into_restored_report(self):
        _, traces, manifest = checkpoint_fixture()
        done = next(e for e in traces if e['type'] == 'audit.model_self_check.completed'
                    and e['agent_id'] == 'numbers')
        done['data']['validated_result']['checks'].append({'rule_id': 'contract_signed'})
        # The integrity comparison can reject the mutated completion event
        # before the later role-assignment check names the foreign rule.
        with self.assertRaisesRegex(AuditError, 'contract_signed|子任务完成事件与已保存自测结果不一致'):
            completed_worker_checkpoint(traces, manifest)

    def test_source_change_rejected_before_any_model_call(self):
        case, traces, manifest = checkpoint_fixture()
        checkpoint = completed_worker_checkpoint(traces, manifest)
        connection = ModelSelection(provider='deepseek', model='deepseek-flash',
                                    api_key='synthetic-only-not-a-key')
        with patch('audit_core.failed_upload_resume.ResumeEvidenceProvider'), TemporaryDirectory() as folder:
            provider = WorkerReportResumeProvider(connection, traces, checkpoint)
            provider.classify(case, Path(folder))
            changed = deepcopy(case)
            changed['documents'][0]['facts'].append('CHANGED SOURCE VALUE')
            with self.assertRaisesRegex(AuditError, '原始资料'):
                provider.audit(changed, Path(folder))

    def test_other_failure_never_enters_this_recovery_path(self):
        _, traces, manifest = checkpoint_fixture()
        manifest['failure']['message'] = 'some other failure'
        self.assertIsNone(completed_worker_checkpoint(traces, manifest))

    def test_completed_merge_and_ambiguous_run_rejected(self):
        _, traces, manifest = checkpoint_fixture()
        traces.append({'sequence': 999, 'type': 'workflow.pdf_materials.validated', 'data': {}})
        with self.assertRaisesRegex(AuditError, '合并已经完成'):
            completed_worker_checkpoint(traces, manifest)
        traces.pop()
        traces.append({'sequence': 999, 'type': 'run.started', 'data': {'run_id': 'other-run'}})
        with self.assertRaisesRegex(AuditError, '唯一原run'):
            completed_worker_checkpoint(traces, manifest)
