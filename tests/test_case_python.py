"""The model can replace an unavailable helper with a case-owned script."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.audit_tools import execute_tool, dispatch_requests
from audit_core.common import AuditError
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_self_check import review_stage
from tests.test_final_logic_stage import fixture


class CasePythonTests(TestCase):
    def case(self):
        return {'documents':[{'unit_id':'one','roles':['原始明细'],
            'facts':['甲店销量4支','乙店销量4支'],'limitations':[]}], 'materials':[], 'flags':{}}

    def test_generated_parser_runs_with_exact_original_rows_and_persists_its_code(self):
        case=self.case();original=deepcopy(case)
        code="values = [Decimal(re.search(r'[0-9]+', f).group()) for f in source('one')['facts']]\nresult = {'quantity': str(sum(values)), 'row_count':len(values)}"
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            result=execute_tool(case,'case_python',source_ids=['one'],code=code)
            self.assertTrue(result['complete'],result)
            self.assertEqual(result['result'],{'quantity':'8','row_count':2})
            scripts=list((Path(folder)/'analysis/generated-tools').glob('*.py'))
            self.assertEqual(len(scripts),1)
            self.assertEqual(scripts[0].read_text('utf-8'),code)
        self.assertEqual(case,original)

    def test_host_import_file_access_and_private_attributes_are_unavailable(self):
        for code in ["import os\nresult=os.getcwd()", "result=open('outside.txt').read()", "result=documents.__class__"]:
            with self.subTest(code=code):
                result=execute_tool(self.case(),'case_python',source_ids=['one'],code=code)
                self.assertFalse(result['complete'],result)
                self.assertEqual(result['status'],'model_takeover')
                self.assertNotIn('business_conclusion',result)

    def test_only_current_source_ids_are_opened(self):
        with self.assertRaises(AuditError):
            execute_tool(self.case(),'case_python',source_ids=['another-case'],code='result=1')

    def test_physical_tool_timeout_returns_control_to_the_model(self):
        with patch('audit_core.case_python.subprocess.run',side_effect=subprocess.TimeoutExpired('fixture',30)):
            result=execute_tool(self.case(),'case_python',source_ids=['one'],code='result=1')
        self.assertFalse(result['complete'])
        self.assertEqual(result['status'],'model_takeover')

    def test_dynamic_tool_is_available_through_the_normal_stage_protocol(self):
        result=dispatch_requests(self.case(),[{'tool':'case_python','fields':[],
            'rule_ids':['contract_signed'],'source_ids':['one'],'code':"result={'count':len(source('one')['facts'])}"}],['contract_signed'])
        self.assertEqual(result[0]['result']['result'],{'count':2})

    def test_stage_model_can_request_a_new_tool_then_complete_its_original_check(self):
        case,packet,provider=fixture()
        check=next(c for c in packet['audit_evidence']['checks'] if c['rule_id']=='contract_signed')
        uid=case['documents'][0]['unit_id'];value={'checks':[deepcopy(check)]};calls=[]
        def call(root,skill,schema,prompt,**kwargs):
            request=json.loads(prompt.partial_variables['request']);calls.append(request)
            if len(calls)==1:
                return {'ready':False,'checks':value['checks'],'notes':[], 'tool_requests':[
                    {'tool':'case_python','rule_ids':['contract_signed'],'fields':[],'source_ids':[uid],
                     'code':f"result={{'facts':len(source('{uid}')['facts'])}}"}]}
            self.assertTrue(request['tools'][-1]['result']['complete'])
            return {'ready':True,'checks':value['checks'],'notes':['已读取本单临时工具结果，保留实际证据限制。']}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',side_effect=call):
            result=review_stage(provider,case,value,value,Path(folder)/'stage',['contract_signed'],lambda v,ids:v)
        self.assertEqual(result,value)
        self.assertEqual(len(calls),2)

    def test_format_recovery_returns_after_its_single_temporary_tool_attempt(self):
        from audit_core.execution_repair_agent import recover_output
        from audit_core.audit_tools import execute_tool
        from audit_core.pdf_evidence import validate
        schema={'type':'object','required':['result'],'properties':{'result':{'type':'integer'}}}
        class Provider:
            def _call(self,*args,**kwargs):
                return {'diagnosis':'按原输入计算。','developer_fix':None,'answer':{},'tool_requests':[
                    {'tool':'case_python','rule_ids':['current_output'],'fields':[],'source_ids':['current-stage-input'],
                     'code':"data=json.loads(source('current-stage-input')['facts'][0]);result={'sum':sum(data['values'])}"}]}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch('audit_core.audit_tools.execute_tool',wraps=execute_tool) as tool:
            with self.assertRaises(AuditError):
                recover_output(Provider(),root=Path(folder)/'call',skill=Path(folder),schema=schema,
                    prompt='{"values":[4,4]}',images=[],label='可读结果恢复',
                    validator=lambda value:validate(value,schema),candidate={'result':'bad'},
                    error=AuditError('原值类型错误'),diagnostics={})
        self.assertEqual(tool.call_count,1)
