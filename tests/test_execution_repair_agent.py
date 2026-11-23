from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json

from audit_core.common import AuditError
from audit_core.execution_repair_agent import recover
from audit_core.runtime_evidence_repair import repair_scope, bind_settlement, basis_for
from audit_core.comparison_policy import single_settlement_expense
from tests.test_vertical_settlement_field_groups import fixture, decide


def repair_fixture():
    data=fixture(quantity='4',price='90',total='360',claim='350',wrapper='',closing='')
    facts=data[0][0]['facts']
    # This arbitrary metadata wording is intentionally unknown to the fixed
    # parser. The agent binds original fields instead of adding source branches.
    facts[-4:]=['读取位置[活动日期]：'+facts[-4],
                '读取位置[数量字段]：'+facts[-3],
                '读取位置[单位费用字段]：'+facts[-2],
                '读取位置[总费用字段]：'+facts[-1]]
    row,cap=data[2]['checks'][0]['comparisons']
    row['left']['quote']=facts[-1];row['right'][0]['quote']=facts[-3];row['right'][1]['quote']=facts[-2]
    cap['right']=deepcopy(row['right'])
    binding={'unit_id':'fee-source','explanation':'同一陈列费用的纵向字段；原事实及金额保持',
        'fields':[{'role':'quantity','fact_index':5,'number':'4','label':'堆头数量'},
                  {'role':'unit_price','fact_index':6,'number':'90','label':'堆头金额'},
                  {'role':'total','fact_index':7,'number':'360','label':'合计'},
                  {'role':'claim','fact_index':2,'number':'350','label':'陈列核销金额合计'}]}
    return data,binding


class RuntimeEvidenceRepairTests(TestCase):
    def test_binding_restores_full_original_validation_without_editing_sources(self):
        data,binding=repair_fixture();before=deepcopy(data)
        self.assertIsNone(single_settlement_expense(data[0]))
        with self.assertRaises(AuditError):decide(data,generation=False)
        with repair_scope():
            bind_settlement(binding,data[0],data[1])
            self.assertEqual(decide(data,generation=False)['status'],'pass')
            changed=deepcopy(data[0]);changed[0]['facts'].append('另一笔费用：20元')
            self.assertIsNone(basis_for(changed))
        self.assertIsNone(basis_for(data[0]));self.assertEqual(data,before)

    def test_same_unit_id_different_run_never_reuses_a_binding(self):
        data,binding=repair_fixture()
        with repair_scope():
            bind_settlement(binding,*data[:2]);self.assertIsNotNone(basis_for(data[0]))
            with repair_scope():self.assertIsNone(basis_for(data[0]))
            self.assertIsNotNone(basis_for(data[0]))

    def test_real_arithmetic_difference_and_excess_still_fail(self):
        for mode in ('total','claim'):
            data,binding=repair_fixture()
            role=next(f for f in binding['fields'] if f['role']==mode)
            old=role['number'];role['number']='361'
            data[0][0]['facts'][role['fact_index']]=data[0][0]['facts'][role['fact_index']].replace(old,'361')
            for c in data[2]['checks'][0]['comparisons']:
                if mode=='total' and c['operator']=='eq' or mode=='claim' and c['operator']=='le':
                    c['left']['quote']=data[0][0]['facts'][role['fact_index']];c['left']['number']='361'
            with repair_scope():
                bind_settlement(binding,*data[:2])
                self.assertEqual(decide(data,generation=False)['status'],'fail')

    def test_extra_fees_unknown_rows_missing_sources_and_wrong_roles_rejected(self):
        for mode in ('extra_fee','unknown_row','wrong_role','missing','duplicate','number','partial','second_table','duplicate_row','code','cross_fee_in_used_rows'):
            data,binding=repair_fixture()
            if mode=='extra_fee':data[0][0]['facts'].append('其他费用：20元。')
            elif mode=='unknown_row':data[0][0]['facts'].append('表格行：另一笔制作费用。')
            elif mode=='wrong_role':binding['fields'][0]['role']='headcount'
            elif mode=='missing':data[1][0]['state']='missing'
            elif mode=='duplicate':binding['fields'].append(deepcopy(binding['fields'][0]))
            elif mode=='number':binding['fields'][0]['number']='3'
            elif mode=='partial':data[0][0]['limitations']=['未完整读取，缺行']
            elif mode=='second_table':data[0][0]['facts'].append('表格项目：另一笔费用。')
            elif mode=='duplicate_row':data[0][0]['facts']+=['表格第2行：活动时间：2026年08月01日。','表格第02行：活动时间：2026年08月02日。']
            elif mode=='code':binding['code']='修改源码'
            elif mode=='cross_fee_in_used_rows':
                data[0][0]['facts'][5]+='另一笔制作数量：3个；制作单价：20元/个；制作金额：60元。'
            from jsonschema import ValidationError
            with self.subTest(mode=mode),repair_scope(),self.assertRaises((AuditError,ValidationError)):bind_settlement(binding,*data[:2])

    def test_agent_tool_request_defers_after_one_attempt_without_a_second_model_call(self):
        data,binding=repair_fixture();original=deepcopy(data)
        class Provider:
            _repair_case={'scenario':'promotional_display','archive_id':'a001','documents':data[0],'materials':data[1],'units':[]}
            calls=[]
            def _call(self,*args,**kwargs):
                self.calls.append(kwargs['label'])
                answer={'action':'answer','tool':None,'fields':[],
                    'diagnosis':'读取位置前缀导致单笔纵向字段未识别','developer_fix':'统一字段位置识别并添加额外费用反例',
                    'settlement_binding':binding,'answer':data[2]}
                if len(self.calls)==1:answer.update(action='read_tool',tool='source_inventory',settlement_binding=None,answer=None)
                kwargs['validator'](answer)
                return answer
        with TemporaryDirectory() as directory,repair_scope():
            root=Path(directory);provider=Provider()
            with patch('audit_core.model_metrics.current_analysis_root',return_value=root/'worktree/analysis'):
                answer=recover(provider,root=root/'stage',skill=Path('skill'),schema={'type':'object'},prompt='本次原始资料',
                    images=[],label='结算审核',validator=lambda v:decide((data[0],data[1],v),generation=False),
                    candidate=data[2],error=AuditError('跨行混乘'),diagnostics={'validation_code':'comparison_scope'})
            self.assertFalse(list(root.rglob('repair-items')))
            self.assertEqual(answer,data[2])
            self.assertEqual(len(provider.calls),1)
            self.assertEqual(provider._soft_check_advice[0]['temporary_repair_attempts'],1)
            self.assertEqual(data,original)

    def test_answer_correction_without_project_change_creates_no_todo(self):
        data=fixture();original=deepcopy(data)
        class Provider:
            _repair_case={'scenario':'promotional_display','documents':data[0],'materials':data[1],'units':[]}
            def _call(self,*args,**kwargs):
                value={'action':'answer','tool':None,'fields':[],
                    'diagnosis':'本次回答遗漏字段，原程序无需修改','developer_fix':None,
                    'settlement_binding':None,'answer':data[2]}
                kwargs['validator'](value)
                return value
        with TemporaryDirectory() as directory,repair_scope():
            root=Path(directory)
            with patch('audit_core.model_metrics.current_analysis_root',return_value=root/'run/analysis'):
                answer=recover(Provider(),root=root/'stage',skill=Path('skill'),schema={'type':'object'},prompt='本单原文',
                    images=[],label='结算审核',validator=lambda v:decide((data[0],data[1],v),generation=False),
                    candidate=data[2],error=AuditError('响应缺字段'),diagnostics={})
            self.assertEqual(answer,data[2]);self.assertEqual(data,original)
            self.assertFalse((root/'run/analysis/repair-items').exists())

    def test_checkpoint_binding_survives_without_a_maintenance_todo(self):
        from audit_core.model_metrics import collect_model_artifacts
        from audit_core.execution_trace import record_step
        data,binding=repair_fixture()
        with TemporaryDirectory() as directory:
            analysis=Path(directory)/'run/analysis'
            with collect_model_artifacts(analysis),repair_scope():
                bind_settlement(binding,*data[:2])
                self.assertEqual(decide(data,generation=False)['status'],'pass')
                import hashlib
                signature=hashlib.sha256(json.dumps(data[0],ensure_ascii=False,sort_keys=True).encode()).hexdigest()
                record_step('audit.execution_repair.completed','本单校验通过',{
                    'original_validation_passed':True,'source_fingerprint':signature,'runtime_binding':binding})
            with collect_model_artifacts(analysis),repair_scope():
                self.assertEqual(decide(data,generation=False)['status'],'pass')
            self.assertFalse(list(Path(directory).rglob('repair-items')))




    def test_model_business_answer_is_checkpointed_without_entering_legacy_repair(self):
        from audit_core.model_settings import api_selection
        from audit_core.pdf_materials import PdfEvidenceProvider
        from audit_core.model_metrics import collect_model_artifacts
        from audit_core.execution_trace import record_step
        from audit_core.failed_upload_resume import ResumeEvidenceProvider,load_trace
        from audit_core.scenario_registry import SKILL_BY_SCENARIO
        from tests.test_failed_upload_resume import pending
        data,binding=repair_fixture()
        selection=api_selection({'provider':'zimaocloud','api_key':'test-isolated-local'})
        provider=PdfEvidenceProvider(selection.model,selection.reasoning_effort,connection=selection, stage_summary_reviewer=isolated_stage_summary)
        provider._repair_case={'scenario':'promotional_display','archive_id':'a001','documents':data[0],'materials':data[1],'units':[]}
        response={'action':'answer','tool':None,'fields':[],'diagnosis':'原位置前缀未被识别',
            'developer_fix':'统一字段位置及唯一费用验证','settlement_binding':binding,'answer':data[2]}
        outputs=[(json.dumps(data[2],ensure_ascii=False),{}),(json.dumps(response,ensure_ascii=False),{})]
        validator=lambda value:decide((data[0],data[1],value),generation=False)
        with TemporaryDirectory() as directory,repair_scope():
            root=Path(directory);analysis=root/'run/analysis'
            with collect_model_artifacts(analysis),patch('audit_core.qwen_model.complete_json',side_effect=outputs) as remote:
                record_step('run.started','开始本单',{'run_id':'same-run'})
                answer=provider._call(root/'call',SKILL_BY_SCENARIO['promotional_display'],{'type':'object','properties':{'checks':{'type':'array'}}},
                    '本批原始结算单：'+json.dumps(data[0],ensure_ascii=False),images=[],label='结算审核',validator=validator)
                self.assertEqual(answer,data[2]);self.assertEqual(remote.call_count,1)
                self.assertFalse(list(root.rglob('repair-items')))
            traces=load_trace(root/'run')
            # A later stage is unfinished. The repaired prior stage must be a
            # real checkpoint, not replayed as a failed original model answer.
            later=pending(max(e['sequence'] for e in traces)+1)
            for event in later:
                if event['type']=='ai.request':event['data'].update(provider=selection.provider,model=selection.model,reasoning_effort=selection.reasoning_effort)
            resumed=ResumeEvidenceProvider(selection,traces+later)
            self.assertEqual(resumed.pending_label,'日期')
            self.assertTrue(any(v['value']==answer for records in resumed.saved.values() for v in records))
            # An exact-source repaired field basis is revalidated from the
            # ordinary same-run trace after the original context is gone.
            with repair_scope(),collect_model_artifacts(analysis):
                self.assertEqual(answer,data[2])

    def test_extra_business_evidence_field_does_not_invoke_repair(self):
        from audit_core.model_settings import api_selection
        from audit_core.pdf_materials import PdfEvidenceProvider
        from audit_core.scenario_registry import SKILL_BY_SCENARIO
        data=fixture();bad={**data[2],'unexpected':'错误字段'}
        connection=api_selection({'provider':'zimaocloud','api_key':'test-isolated'})
        provider=PdfEvidenceProvider(connection.model,connection.reasoning_effort,connection=connection, stage_summary_reviewer=isolated_stage_summary)
        provider._repair_case={'scenario':'promotional_display','documents':data[0],'materials':data[1],'units':[]}
        response={'action':'answer','tool':None,'fields':[],'diagnosis':'响应字段与原合同不一致',
            'developer_fix':None,'settlement_binding':None,'answer':data[2]}
        schema={'type':'object','additionalProperties':False,'properties':{'checks':{'type':'array'},'reference_reads':{'type':'array'}}}
        outputs=[(json.dumps(bad),{}),(json.dumps(response),{})]
        with TemporaryDirectory() as directory,repair_scope(),patch('audit_core.qwen_model.complete_json',side_effect=outputs) as remote:
            result=provider._call(Path(directory)/'stage',SKILL_BY_SCENARIO['promotional_display'],schema,
                '本次原文：'+json.dumps(data[0]),images=[],label='结算审核',
                validator=lambda value:decide((data[0],data[1],value),generation=False))
            self.assertEqual(result,bad);self.assertEqual(remote.call_count,1)
            self.assertFalse(list(Path(directory).rglob('repair-items')))
