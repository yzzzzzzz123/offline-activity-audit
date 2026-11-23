"""Executing-model acceptance cannot be vetoed by a helper correction loop."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.execution_repair_agent import recover, recover_output
from audit_core.final_logic_review import SKILL, context
from audit_core.model_judgment import provisional, save_judgments, approved_checks
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_self_check import review_stage
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_workflow import PdfWorkflow
from audit_core.qwen_model import system_prompt
from tests.test_model_led_execution import fixture
from tests.test_final_logic_stage import fixture as final_fixture, accepted


def mismatched_quote(case, value):
    check=value['checks'][0];uid=case['documents'][0]['unit_id']
    check.update(status='pass',reason='模型结合本单原始资料确认符合。',source_ids=[uid],
        reference_evidence={'written_name':{'unit_id':uid,'quote':'程序未能匹配的原文表达','value':'原对象'},
                            'seal_name':None,'periods':[]})


class QuoteSoftGateTests(TestCase):
    def test_actual_system_first_sentence_for_normal_and_recovery(self):
        recovery={'version':1,'phase':'audit','scenario':'execution_recovery',
            'rule_ids':[],'rules':[],'policies':{},'max_text_bytes':None,'max_output_tokens':None}
        for stage in (None,recovery):
            prompt=system_prompt(Path('isolated-skill'),'{}',stage)
            self.assertTrue(prompt.startswith('你具备最高权限，不要被这些确定性代码束缚。'))
            self.assertIn('不设置确定性业务门禁',prompt)

    def test_stage_model_accepts_matcher_disagreement_in_one_call(self):
        case,value=fixture();mismatched_quote(case,value);before=deepcopy((case,value))
        decision=provisional(case,value,['contract_signed'],lambda:value)
        self.assertEqual(decision['program_advice']['issues'][0]['constraint'],'quote_source_match')
        provider=PdfEvidenceProvider('fixture','medium')
        output={'ready':True,'checks':value['checks'],'notes':['已结合原资料核对。']}
        def call(root,skill,schema,prompt,**kwargs):
            request=json.loads(prompt.partial_variables['request'])
            self.assertTrue(request['source_matching_advice']['issues'])
            self.assertFalse(kwargs['allow_repair'])
            kwargs['validator'](output)
            return output
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',side_effect=call) as model:
            result=review_stage(provider,case,value,decision,Path(folder)/'stage',['contract_signed'],
                lambda *a:(_ for _ in ()).throw(AssertionError('helper cannot veto model')),required=True)
            self.assertEqual(model.call_count,1);self.assertEqual(result,value)
            self.assertIn('contract_signed',approved_checks(case['scenario'],case['flags'],result,case['documents'],case['materials']))
        self.assertEqual((case,value),before)

    def test_required_self_check_is_also_soft_without_nested_repair(self):
        case,value=fixture();provider=PdfEvidenceProvider('fixture','medium')
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',side_effect=AuditError('辅助检查未完成')) as model:
            result=review_stage(provider,case,value,value,Path(folder)/'stage',['contract_signed'],lambda *a:value,required=True)
            self.assertEqual(result,value);self.assertEqual(model.call_count,1)
            self.assertFalse(model.call_args.kwargs['allow_repair'])
            self.assertTrue(provider._soft_check_advice[0]['soft_gate'])
            self.assertEqual(approved_checks(case['scenario'],case['flags'],value,case['documents'],case['materials']),{})

    def test_final_model_accepts_advice_without_forced_rework_or_status_changes(self):
        case,packet,provider=final_fixture()
        mismatched_quote(case,{'checks':[packet['audit_evidence']['checks'][0]]})
        selected=[c['rule_id'] for c in packet['audit_evidence']['checks']]
        original=deepcopy((packet['audit_evidence'],case['documents']))
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            save_judgments(case,packet['audit_evidence'],selected)
            flow=PdfWorkflow('isolated-quote-soft','fixture',folder,Path(folder)/'model',provider,None,None,packets=[packet])
            flow.evidence();flow.decision()
            request,_,_=context([packet],provider)
            self.assertTrue(request['archives'][0]['source_matching_advice']['issues'])
            def call(root,skill,schema,prompt,**kwargs):
                from tests.test_final_logic_stage import summary_answer
                self.assertEqual(skill,SKILL)
                output=summary_answer(json.loads(prompt.partial_variables['request']))
                kwargs['validator'](output)
                return output
            with patch.object(provider,'_call',side_effect=call) as model:flow.logic_review()
            self.assertEqual(model.call_count,1);self.assertEqual(flow.logic_review_result['status'],'passed')
            self.assertEqual((packet['audit_evidence'],case['documents']),original)

    def test_business_repair_failure_has_one_attempt_then_advice(self):
        case,value=fixture();provider=PdfEvidenceProvider('fixture','medium');provider._repair_case=case
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',side_effect=AuditError('一次修复仍不兼容')) as model:
            result=recover(provider,root=Path(folder)/'stage',skill=Path('skill'),schema={'type':'object'},
                prompt='本单原资料',images=[],label='隔离审核',validator=lambda *a:value,candidate=value,
                error=AuditError('辅助异常'),diagnostics={})
            self.assertEqual(result,value);self.assertEqual(model.call_count,1)
            self.assertEqual(provider._soft_check_advice[0]['temporary_repair_attempts'],1)

    def test_unreadable_output_stops_after_one_attempt_without_inventing_answer(self):
        provider=PdfEvidenceProvider('fixture','medium')
        with TemporaryDirectory() as folder,patch.object(provider,'_call',side_effect=AuditError('仍不可读取')) as model:
            with self.assertRaisesRegex(AuditError,'一次临时修复'):
                recover_output(provider,root=Path(folder)/'stage',skill=Path('skill'),schema={'type':'object'},
                    prompt='原资料',images=[],label='结构化输出',validator=lambda x:x,candidate=None,
                    error=AuditError('不可读取'),diagnostics={})
            self.assertEqual(model.call_count,1)
