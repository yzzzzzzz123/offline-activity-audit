"""Output-shape-independent recovery, complete diagnostics and safe summary mapping."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.summary_review import _validate_answer, review_stage, stage_request
from audit_core.execution_repair_agent import recover_output
from audit_core.execution_diagnostics import report_failure
from audit_core.execution_trace import collect_execution_trace, record_step
from audit_core.failed_upload_resume import load_trace, ResumeEvidenceProvider
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_settings import api_selection
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.prompts import bound_prompt
from audit_core.resume_request_identity import request_identity


def response(*texts):
    return {'ready': True, 'issues': [], 'items': [
        {'source_problem_ids': ['photo'], 'text': text, 'uncertainty_preserved': True} for text in texts]}


def sample():
    texts = ['甲店照片 a001-f0018 上有2026-07-10和2026.03.24两个日期，无法确认拍摄日期。',
             '乙店照片 a001-f0041 模糊，无法看清价签。']
    value = {'checks': [{'rule_id': 'photo', 'status': 'unknown',
        'reason': '\n'.join(f'{i}. {t}' for i, t in enumerate(texts, 1)),
        'source_ids': ['a001-f0018', 'a001-f0041'], 'comparisons': []}]}
    return texts, value


class SummaryCompatibilityTests(TestCase):
    def test_multiple_paragraphs_map_back_to_one_unchanged_business_check(self):
        texts, value = sample(); original = deepcopy(value)
        provider = SimpleNamespace(_call=lambda *args, **kwargs: response(*texts))
        with TemporaryDirectory() as directory:
            result = review_stage(provider, {}, value, value, Path(directory), ['photo'], lambda v, s: v)
        self.assertEqual(result['checks'][0]['reason'], '\n'.join(texts))
        for key in ('rule_id', 'status', 'source_ids', 'comparisons'):
            self.assertEqual(result['checks'][0][key], value['checks'][0][key])
        self.assertEqual(value, original)




    def test_stage_checks_still_cannot_merge_into_one_item(self):
        _, value = sample(); value['checks'].append({**value['checks'][0], 'rule_id': 'other'})
        request = stage_request(value, value); candidate = response(value['checks'][0]['reason'])
        candidate['items'][0]['source_problem_ids'] = ['photo', 'other']
        with self.assertRaises(AuditError):_validate_answer(candidate, request, stage=True)


class StructuredRecoveryTests(TestCase):
    def test_non_checks_output_enters_recovery_and_records_a_reusable_checkpoint(self):
        selection = api_selection({'provider': 'zimaocloud', 'api_key': 'isolated-test'})
        provider = PdfEvidenceProvider(selection.model, selection.reasoning_effort, connection=selection)
        texts, value = sample(); request = stage_request(value, value)
        from audit_core.summary_review import _schema
        schema = _schema(['photo'], stage=True)
        invalid = response(texts[0], texts[1])
        invalid['items'] = 'not an array'
        corrected = response(*texts)
        recovery = {'diagnosis': '原候选改动了日期，按原输入恢复，保留两段对应关系。',
                    'developer_fix': None, 'answer': corrected}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with collect_model_artifacts(root/'run/analysis'), patch('httpx.Client', side_effect=AssertionError('no network')), \
                 patch('audit_core.qwen_model.complete_json', side_effect=[(json.dumps(invalid), {}), (json.dumps(recovery), {})]) as model:
                record_step('run.started', '开始本单', {'run_id': 'one'})
                result = provider._call(root/'call', Path('skill'), schema,
                    bound_prompt('待核验完整内容：{data}', data=json.dumps(request, ensure_ascii=False)),
                    images=[], label='总结核验', validator=lambda v: _validate_answer(v, request, stage=True), max_attempts=1)
            self.assertEqual(result, corrected); self.assertEqual(model.call_count, 2)
            traces = load_trace(root/'run')
            failures = [e for e in traces if e['type'] == 'ai.validation_failed']
            self.assertIn('items', failures[0]['data']['validation_error'])
            repaired = [e for e in traces if e['type'] == 'audit.execution_repair.completed']
            self.assertEqual(repaired[0]['data']['validated_result'], corrected)
            self.assertEqual(repaired[0]['data']['source_call_id'], failures[0]['call_id'])
            from tests.test_failed_upload_resume import pending
            later = pending(max(e['sequence'] for e in traces)+1)
            for event in later:
                if event['type'] == 'ai.request':
                    event['data'].update(provider=selection.provider, model=selection.model,
                                         reasoning_effort=selection.reasoning_effort)
            resumed = ResumeEvidenceProvider(selection, traces+later, run_id='one')
            self.assertTrue(any(v['value'] == corrected for records in resumed.saved.values() for v in records))

    def test_rejected_corrections_receive_feedback_and_are_bounded_without_recursion(self):
        calls = []
        def call(root, skill, schema, prompt, **kwargs):
            calls.append((prompt.format(), kwargs))
            return {'diagnosis': '尝试候选', 'developer_fix': None, 'answer': {'amount': 11}}
        provider = SimpleNamespace(_call=call)
        def validate(v):
            if v['amount'] != 10:raise AuditError('原金额10不能变11')
        with TemporaryDirectory() as directory:
            with self.assertRaises(AuditError) as raised:
                recover_output(provider, root=Path(directory)/'call', skill=Path('skill'),
                    schema={'type': 'object'}, prompt=bound_prompt('原金额10'), images=[], label='任意结构化阶段',
                    validator=validate, candidate={'amount': 12}, error=AuditError('原金额被改'), diagnostics={})
        self.assertEqual(len(calls), 1)
        self.assertIn('原金额10不能变11', raised.exception.validation_detail['last_feedback']['validation_error'])
        self.assertTrue(all(k['allow_repair'] is False and k['max_attempts'] == 1 for _, k in calls))
        self.assertIn('last_feedback', raised.exception.validation_detail)

    def test_diagnostic_gets_saved_response_exact_error_and_artifact_without_write_authority(self):
        seen = []
        class Provider:
            def _call(self, root, skill, schema, prompt, **kwargs):
                seen.append((prompt.format(), kwargs))
                return {'category': 'program', 'diagnosis': '阶段映射与段落数量冲突。',
                    'evidence': ['响应两条，对应同一原审核项。'], 'operator_action': '核对阶段映射', 'developer_fix': None}
        with TemporaryDirectory() as directory:
            root = Path(directory); analysis = root/'run/analysis'
            analysis.mkdir(parents=True)
            with collect_execution_trace(analysis):
                record_step('ai.validation_failed', '阶段失败', {'visible_response': '{"items":[1,2]}',
                    'validation_error': '一项不能返回两段', 'diagnostics': {'validation_code': 'evidence_validation'}},
                    status='failed', call_id='original-call')
            from audit_core.execution_diagnostics import DiagnosticState, _STATE
            token = _STATE.set(DiagnosticState(root, 'one', analysis, Provider()))
            try:report_failure(AuditError('校验失败'), stage='execution')
            finally:_STATE.reset(token)
        self.assertIn('一项不能返回两段', seen[0][0]); self.assertIn('visible_response', seen[0][0])
        self.assertIn('original-call', seen[0][0]); self.assertIn('artifact', seen[0][0])
        self.assertFalse(seen[0][1]['allow_repair'])

    def test_summary_source_identity_keeps_evidence_but_allows_presentation_rule_fix(self):
        texts, value = sample(); source = stage_request(value, value)
        def identity(payload, prefix):
            return request_identity({'provider': 'one', 'model': 'two', 'reasoning_effort': 'default',
                'skill': 'same', 'images': [], 'output_schema': {'type': 'object',
                    'properties': {'ready': {}, 'issues': {}, 'items': {}}},
                'input_text': prefix+'待核验完整内容：'+json.dumps(payload, ensure_ascii=False)}, '总结')
        changed = deepcopy(source); changed['instruction'] = '允许分段，审核记录保持'
        before, after = identity(source, '旧指令'), identity(changed, '修正指令')
        self.assertNotEqual(before['request'], after['request']); self.assertEqual(before['source'], after['source'])
        for field in ('text', 'status', 'source_ids', 'read_only'):
            other = deepcopy(changed); other['problems'][0][field] = 'changed'
            self.assertNotEqual(before['source'], identity(other, '')['source'])

    def test_model_outage_during_recovery_escapes_without_retrying_the_failed_model(self):
        from audit_core.qwen_model import QwenRequestError
        error = QwenRequestError('quota unavailable', code='quota'); calls = []
        def call(*args, **kwargs):calls.append(kwargs);raise error
        with TemporaryDirectory() as directory, self.assertRaises(QwenRequestError) as raised:
            recover_output(SimpleNamespace(_call=call), root=Path(directory)/'call', skill=Path('skill'),
                schema={'type': 'object'}, prompt=bound_prompt('original'), images=[], label='summary',
                validator=lambda _: None, candidate={}, error=AuditError('bad output'), diagnostics={})
        self.assertIs(raised.exception, error);self.assertEqual(len(calls), 1)
