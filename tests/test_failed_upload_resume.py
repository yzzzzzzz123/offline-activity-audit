from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
import json
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.failed_upload_resume import ResumeEvidenceProvider, saved_qince
from audit_core.workbench_store import WorkbenchRunStore, reserve_workspace


SCHEMA={'type':'object','required':['ok'],'properties':{'ok':{'const':True}}}
CONNECTION=SimpleNamespace(provider='deepseek',model='deepseek-flash',reasoning_effort='default')


def request(sequence, call_id, label, text, schema=SCHEMA, attempt=1, images=None):
    return {'type':'ai.request','sequence':sequence,'call_id':call_id,'label':f'{label} · 第{attempt}次输入',
        'data':{'provider':CONNECTION.provider,'model':CONNECTION.model,'reasoning_effort':CONNECTION.reasoning_effort,
            'skill':'skill','attempt':attempt,'input_text':text,'output_schema':schema,'images':images or []}}


def result(sequence, call_id, label, value, attempt=1):
    return {'type':'ai.validated','sequence':sequence,'call_id':call_id,'label':f'{label} · 第{attempt}次返回及校验通过',
            'data':{'attempt':attempt,'validated_result':value}}


def trace_prefix():
    return [{'type':'run.started','sequence':1,'data':{'run_id':'same-run'}}]


def pending(sequence=100, label='日期', text='pending'):
    return [request(sequence,'pending-call',label,text),
            {'type':'ai.validation_failed','sequence':sequence+1,'call_id':'pending-call',
             'label':f'{label} · 第1次结果未通过校验','data':{'attempt':1}}]


class FailedUploadResumeTests(unittest.TestCase):
    def provider(self):
        return ResumeEvidenceProvider(CONNECTION,trace_prefix()+[
            request(2,'first-call','前序','original'),result(3,'first-call','前序',{'ok':True})]+pending())

    def test_only_same_run_validated_stage_replayed_through_current_validator(self):
        provider=self.provider(); seen=[]
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',side_effect=AssertionError('Model forbidden')):
            value=provider._call(Path('unused'),Path('skill'),SCHEMA,'original',images=[],label='前序',
                validator=lambda value:seen.append(value['ok']))
        self.assertEqual(value,{'ok':True});self.assertEqual(seen,[True])
        self.assertEqual(provider.reused,['前序'])
        value['ok']=False
        self.assertTrue(next(iter(provider.saved.values()))[0]['value']['ok'])

    def test_missing_prior_checkpoint_does_not_restart_whole_case(self):
        with self.assertRaisesRegex(AuditError,'停止'):
            self.provider()._call(Path('unused'),Path('skill'),SCHEMA,'different',images=[],label='其他前序',validator=lambda _:None)

    def test_only_pending_stage_uses_normal_model_path(self):
        provider=self.provider()
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',return_value={'new':True}) as call:
            self.assertEqual(provider._call(Path('unused'),Path('skill'),SCHEMA,'pending',images=[],label='日期',validator=lambda _:None),{'new':True})
            self.assertEqual(call.call_count,1)
        self.assertTrue(provider.pending_started)

    def test_replayed_answer_must_still_pass_current_schema(self):
        with self.assertRaises(AuditError):
            self.provider()._call(Path('unused'),Path('skill'),{'type':'object','required':['missing']},'original',
                images=[],label='前序',validator=lambda _:None)

    def test_interruption_after_a_successful_retry_uses_actual_unfinished_request(self):
        traces=trace_prefix()+pending(2,'前序','original')+[
            result(4,'pending-call','前序',{'ok':True}),
            request(5,'stopped-call','图片','unfinished'),
            {'type':'ai.interrupted','sequence':6,'call_id':'stopped-call','data':{'attempt':1}}]
        provider=ResumeEvidenceProvider(CONNECTION,traces)
        self.assertEqual(provider.pending_label,'图片')
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',return_value={'ok':True}) as call:
            provider._call(Path('unused'),Path('skill'),SCHEMA,'original',images=[],label='前序',validator=lambda _:None)
            self.assertEqual(call.call_count,0)
            provider._call(Path('unused'),Path('skill'),SCHEMA,'unfinished',images=[],label='图片',validator=lambda _:None)
            self.assertEqual(call.call_count,1)

    def test_unmarked_unfinished_request_does_not_infer_permission_to_resume(self):
        with self.assertRaises(AuditError):
            ResumeEvidenceProvider(CONNECTION,trace_prefix()+[
                request(2,'stopped','图片','unfinished')])

    def test_interrupted_helper_recovery_returns_to_its_recorded_original_stage(self):
        traces=trace_prefix()+pending(2,'业务审核','original-audit')+[
            {'type':'audit.execution_repair.started','sequence':4,'data':{
                'validation':{'source_call_id':'pending-call'}}},
            request(5,'repair-call','临时恢复操作','repair-input'),
            {'type':'ai.validation_failed','sequence':6,'call_id':'repair-call',
             'label':'临时恢复操作 · 第1次结果未通过校验','data':{'attempt':1}}]
        provider=ResumeEvidenceProvider(CONNECTION,traces)
        self.assertEqual(provider.pending_label,'业务审核')
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',return_value={'ok':True}) as model:
            provider._call(Path('unused'),Path('skill'),SCHEMA,'original-audit',images=[],
                label='业务审核',validator=lambda _:None)
        self.assertEqual(model.call_count,1)

    def test_completed_recovery_does_not_reopen_its_parent_when_a_later_stage_fails(self):
        traces=trace_prefix()+pending(2,'业务审核','original-audit')+[
            {'type':'audit.execution_repair.started','sequence':4,'data':{
                'validation':{'source_call_id':'pending-call'}}},
            {'type':'audit.execution_repair.completed','sequence':5,'data':{
                'source_call_id':'pending-call','original_validation_passed':True,
                'validated_result':{'ok':True}}}]+[
            request(6,'later-call','后续审核','later'),
            {'type':'ai.validation_failed','sequence':7,'call_id':'later-call',
             'label':'后续审核 · 第1次结果未通过校验','data':{'attempt':1}}]
        self.assertEqual(ResumeEvidenceProvider(CONNECTION,traces).pending_label,'后续审核')

    def test_reference_partition_keeps_original_budget_until_pending_then_new_budget(self):
        traces=trace_prefix()+[{'type':'image.reference_facts.partition','sequence':2,
            'data':{'max_sources_per_request':12}}]+pending()
        provider=ResumeEvidenceProvider(CONNECTION,traces)
        self.assertEqual(provider.reference_source_budget,12)
        provider.pending_started=True
        from audit_core.product_reference_batches import JOINT_SOURCE_BUDGET
        self.assertEqual(provider.reference_source_budget,JOINT_SOURCE_BUDGET)

    def test_stopped_store_keeps_all_original_events_and_usage(self):
        with TemporaryDirectory() as temporary:
            workspace=reserve_workspace('20261109-stop','codex',Path(temporary))
            store=WorkbenchRunStore(workspace,run_id='20261109-stop',producer_model='codex',
                root_html=Path('root.html'),workbench_url='http://127.0.0.1:8080/')
            before=store.events_path.read_bytes()
            stopped=WorkbenchRunStore.fail_interrupted(workspace,run_id='20261109-stop',reason='用户要求停止')
            self.assertEqual(stopped.manifest['status'],'failed')
            self.assertTrue(stopped.events_path.read_bytes().startswith(before))
            self.assertIn('用户要求停止',stopped.manifest['failure']['message'])
            with self.assertRaises(AuditError):
                WorkbenchRunStore.fail_interrupted(workspace,run_id='different',reason='stop')

    def test_saved_qince_preserves_distinct_original_rows_and_deduplicates_response_repeats(self):
        trace=[{'type':'qince.response','data':{'endpoint':'/api/queryVerificationRegular','rows':[{'verify_code':'HX1'}]}},
               {'type':'qince.response','data':{'endpoint':'/api/queryVerificationRegular','rows':[{'verify_code':'HX1'},{'verify_code':'HX2'}]}}]
        value=saved_qince(trace)
        self.assertEqual(value['queryVerificationRegular'],[{'verify_code':'HX1'},{'verify_code':'HX2'}])

    def test_versioned_reference_endpoint_maps_to_original_method_name(self):
        trace=[{'type':'qince.response','data':{'endpoint':'/api/pd/v2/queryProduct','rows':[{'prd_code':'P1'}]}},
               {'type':'qince.response','data':{'endpoint':'/api/v2/customer/query','rows':[{'cm_code':'C1'}]}}]
        value=saved_qince(trace)
        self.assertEqual(value['queryProductV2'],[{'prd_code':'P1'}])
        self.assertEqual(value['queryCustomerV2'],[{'cm_code':'C1'}])

    def test_resume_retains_creation_time_failure_and_usage_and_appends_events(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary)
            workspace=reserve_workspace('20261109-test','codex',root)
            store=WorkbenchRunStore(workspace,run_id='20261109-test',producer_model='codex',
                root_html=Path('root.html'),workbench_url='http://127.0.0.1:8080/')
            created=store.manifest['created_at']
            store.fail(AuditError('原失败'))
            before_events=store.events_path.read_text(encoding='utf-8')
            resumed=WorkbenchRunStore.resume_failed(workspace,run_id='20261109-test')
            self.assertEqual(resumed.manifest['created_at'],created)
            self.assertEqual(resumed.manifest['status'],'running')
            self.assertIn('原失败',resumed.manifest['resume_history'][0]['previous_failure']['message'])
            self.assertTrue(resumed.events_path.read_text(encoding='utf-8').startswith(before_events))
            with self.assertRaises(AuditError):WorkbenchRunStore.resume_failed(workspace,run_id='20261109-test')
