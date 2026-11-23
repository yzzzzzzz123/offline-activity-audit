"""Local source maintenance: isolated candidate tests, publication and denial."""
from contextlib import ExitStack
import hashlib
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import types
from unittest import TestCase
from unittest.mock import patch

from audit_core import local_source_repair as repair
from audit_core.common import AuditError
from audit_core.model_metrics import collect_model_artifacts
from audit_core.execution_repair_agent import _tool, recover, recover_output
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.prompts import bound_prompt


ORIGINAL = 'def normalize(value):\n    return value.strip()\n'
UPDATED = 'def normalize(value):\n    return value.strip().replace("，", ",")\n'


class LocalSourceRepairTests(TestCase):
    def setup_project(self, stack):
        folder = Path(stack.enter_context(TemporaryDirectory()))
        core = folder/repair.CORE;core.mkdir(parents=True)
        (core/'__init__.py').write_text('',encoding='utf-8')
        source = core/'reference_matching.py';source.write_text(ORIGINAL,encoding='utf-8')
        tests=folder/'tests';tests.mkdir();(tests/'__init__.py').write_text('',encoding='utf-8')
        (tests/'test_probe.py').write_text('from unittest import TestCase\nfrom audit_core.reference_matching import normalize\n'
            'class ExistingBehavior(TestCase):\n def test_existing(self):self.assertEqual(normalize(" raw "),"raw")\n',encoding='utf-8')
        stack.enter_context(patch.object(repair,'PROJECT_ROOT',folder))
        stack.enter_context(patch.object(repair,'policy_path',return_value=folder/'permission.json'))
        stack.enter_context(patch.object(repair,'REGRESSION_TESTS',('tests.test_probe',)))
        stack.enter_context(patch.object(repair,'authorized',return_value=True))
        stack.enter_context(collect_model_artifacts(folder/'worktrees/test/analysis'))
        module=types.ModuleType('audit_core.reference_matching');module.__file__=str(source)
        exec(compile(ORIGINAL,str(source),'exec'),module.__dict__)
        stack.enter_context(patch.dict(sys.modules,{'audit_core.reference_matching':module}))
        return folder,source,module

    def request(self,source,replacement=UPDATED):
        return {'action':'patch','module':source.name,'expected_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
            'reason':'兼容中文逗号，保留已有空格处理。','changes':[{'function':'normalize','source':replacement}],
            'probes':[{'function':'normalize','args':[' a，b '],'expected':'a,b'}]}

    def test_validated_patch_updates_real_source_and_already_imported_function_alias(self):
        with ExitStack() as stack:
            folder,source,module=self.setup_project(stack);alias=module.normalize
            result=repair.run({},json.dumps(self.request(source)))
            self.assertEqual(result['status'],'applied');self.assertTrue(result['source_code_modified'])
            self.assertIn('.replace(',source.read_text('utf-8'))
            self.assertEqual(alias(' a，b '),'a,b');self.assertIs(module.normalize,alias)
            self.assertEqual((Path(result['receipt']).parent/'before.py').read_text('utf-8'),ORIGINAL)
            archived=(Path(result['receipt']).parent/'candidate.py').read_bytes()
            self.assertEqual(archived,source.read_bytes())
            self.assertEqual(hashlib.sha256(archived).hexdigest(),result['after_sha256'])
            self.assertNotIn(b'\r\r\n',archived)
            self.assertTrue(Path(result['validation']['log']).is_file())

    def test_candidate_regression_failure_never_publishes_or_changes_live_code(self):
        with ExitStack() as stack:
            _,source,module=self.setup_project(stack)
            request=self.request(source,'def normalize(value):\n    return "wrong"\n')
            result=repair.run({},json.dumps(request))
            self.assertEqual(result['status'],'validation_failed');self.assertFalse(result['source_code_modified'])
            self.assertEqual(source.read_text('utf-8'),ORIGINAL);self.assertEqual(module.normalize(' raw '),'raw')

    def test_baseline_failure_is_explicit_and_does_not_publish(self):
        with ExitStack() as stack:
            _,source,_=self.setup_project(stack)
            stack.enter_context(patch.object(repair,'REGRESSION_TESTS',('tests.no_such_test',)))
            result=repair.run({},json.dumps(self.request(source)))
            self.assertEqual(result['status'],'baseline_failed');self.assertEqual(source.read_text('utf-8'),ORIGINAL)

    def test_concurrent_human_edit_is_preserved(self):
        with ExitStack() as stack:
            _,source,_=self.setup_project(stack)
            def verify(*args):
                source.write_text(ORIGINAL+'# concurrent human edit\n',encoding='utf-8')
                return {'passed':True}
            stack.enter_context(patch.object(repair,'_test',side_effect=verify))
            result=repair.run({},json.dumps(self.request(source)))
            self.assertEqual(result['status'],'model_takeover');self.assertIn('concurrent human edit',source.read_text('utf-8'))

    def test_publish_error_rolls_back_file_and_live_function(self):
        with ExitStack() as stack:
            _,source,module=self.setup_project(stack);write=repair._write;calls=[]
            def fail_once(path,data):
                write(path,data);calls.append(path)
                if len(calls)==1:raise OSError('simulated publish failure')
            stack.enter_context(patch.object(repair,'_write',side_effect=fail_once))
            result=repair.run({},json.dumps(self.request(source)))
            self.assertFalse(result['source_code_modified']);self.assertEqual(source.read_text('utf-8'),ORIGINAL)
            self.assertEqual(module.normalize(' a，b '),'a，b')

    def test_only_existing_pure_function_bodies_can_change(self):
        invalid=['def normalize(value):\n    import os\n    return os.environ\n',
            'def normalize(value):\n    return open("secret").read()\n',
            'def normalize(value):\n    return value.__class__\n',
            'def normalize(value):\n    global other\n    other=1\n    return value\n',
            'def normalize(value, extra=True):\n    return value\n',
            'def normalize(value):\n    return value\nopen("secret")\n']
        for code in invalid:
            with self.subTest(code=code),self.assertRaises((AuditError,SyntaxError)):
                repair._changed_source(ORIGINAL,[{'function':'normalize','source':code}])

    def test_server_requests_are_denied_even_with_write_request_and_environment_flags(self):
        with patch.object(repair,'authorized',return_value=False),patch.dict('os.environ',{'OFFLINE_AUDIT_LOCAL_SOURCE_REPAIR':'true'}):
            for request in [{'action':'read'}, {'action':'patch','module':'reference_matching.py'}]:
                with self.assertRaises(AuditError):repair.run({},json.dumps(request))
            self.assertIn('无项目源码写权限',repair.protocol())

    def test_path_and_control_modules_cannot_be_read_or_written(self):
        for name in ['../model_settings.py','model_settings.py','local_source_repair.py','oss_intake.py','../../config/application.env']:
            with self.subTest(name=name),self.assertRaises(AuditError):repair._path(name)
        with self.assertRaises(AuditError):
            repair._changed_source('def expected_operator(value):\n    return value\n',
                [{'function':'expected_operator','source':'def expected_operator(value):\n    return "pass"\n'}])

    def test_real_machine_project_runner_and_parent_are_required_not_just_a_flag(self):
        with ExitStack() as stack:
            folder=Path(stack.enter_context(TemporaryDirectory()));config=folder/'policy.json'
            config.write_text(json.dumps({'enabled':True,'machine_sha256':'this-machine','project_root':str(folder),'port':8080}),encoding='utf-8')
            stack.enter_context(patch.object(repair,'PROJECT_ROOT',folder));stack.enter_context(patch.object(repair,'LOCAL_PROJECT',folder))
            stack.enter_context(patch.object(repair,'policy_path',return_value=config));stack.enter_context(patch.object(repair,'machine_fingerprint',return_value='this-machine'))
            parent=stack.enter_context(patch.object(repair,'_parent_is_local_service',return_value=True))
            stack.enter_context(patch.object(sys,'argv',[str(folder/'skills/orchestrate-offline-audit/scripts/run.py')]))
            stack.enter_context(collect_model_artifacts(folder/'worktrees/test/analysis'))
            self.assertTrue(repair.authorized())
            parent.return_value=False;self.assertFalse(repair.authorized());parent.return_value=True
            with patch.object(repair,'machine_fingerprint',return_value='server-machine'):self.assertFalse(repair.authorized())
            with patch.object(repair,'LOCAL_PROJECT',folder/'server-installation'):self.assertFalse(repair.authorized())
            with patch.object(sys,'argv',['ordinary-cli.py']):self.assertFalse(repair.authorized())
            with patch.object(repair.os,'name','posix'):self.assertFalse(repair.authorized())

    def test_successful_case_tool_is_reused_and_source_change_invalidates_cache(self):
        results={}
        with patch('audit_core.audit_tools.execute_tool',return_value={'status':'completed','result':{'qty':4}}) as call:
            first=_tool({},'case_python',[],'result=4',[],results)
            second=_tool({},'case_python',[],'result=4',[],results)
            self.assertEqual(call.call_count,1);self.assertTrue(second['reused_tool_result'])
        with patch('audit_core.audit_tools.execute_tool',return_value={'status':'applied','source_code_modified':True,'receipt':'receipt.json'}):
            _tool({},'project_source',[],'{}',[],results)
            self.assertEqual(len(results),1)
            self.assertTrue(all(v.get('source_code_modified') for v in results.values()))

    def test_source_repair_can_revalidate_the_original_answer_without_another_model_call(self):
        case={'scenario':'price_difference_support','archive_id':'a001','documents':[],'materials':[],'flags':{}}
        candidate={'checks':[],'tag':'original'};ready=[]
        provider=PdfEvidenceProvider('fixture','medium');provider._repair_case=case
        answer={'action':'read_tool','tool':'project_source','fields':[],'code':'{}','diagnosis':'修复既有解析函数。',
                'developer_fix':None,'settlement_binding':None,'answer':None}
        def validate(value):
            if not ready:raise AuditError('原解析器不能处理该格式')
        def tool(*args,**kwargs):
            ready.append(True);return {'tool':'project_source','source_code_modified':True,'receipt':'isolated-receipt.json','status':'applied'}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',return_value=answer) as model, \
             patch('audit_core.audit_tools.execute_tool',side_effect=tool),patch('audit_core.execution_trace.record_step') as trace:
            value=recover(provider,root=Path(folder)/'stage',skill=Path('skill'),schema={'type':'object'},
                prompt=bound_prompt('原资料'),images=[],label='既有解析',validator=validate,candidate=candidate,
                error=AuditError('格式不支持'),diagnostics={})
            self.assertEqual(value,candidate);self.assertEqual(model.call_count,1)
            completed=[call.args[2] for call in trace.call_args_list if call.args[0]=='audit.execution_repair.completed']
            self.assertTrue(completed[0]['source_code_modified'])

    def test_rejected_answer_becomes_the_next_candidate_and_tool_results_are_kept(self):
        provider=PdfEvidenceProvider('fixture','medium')
        provider._repair_case={'scenario':'price_difference_support','archive_id':'a001','documents':[],'materials':[],'flags':{}}
        candidate={'checks':[],'tag':'original'};calls=[];ready=[]
        def validate(value):
            if not ready:raise AuditError('引用字段仍须修正')
        def model(root,skill,schema,prompt,**kwargs):
            calls.append(prompt.format())
            if len(calls)>1:self.assertIn('"tag": "latest"',calls[-1])
            if len(calls)==2:
                return {'action':'read_tool','tool':'source_inventory','fields':[],'diagnosis':'补查原始索引',
                    'developer_fix':None,'settlement_binding':None,'answer':None}
            if len(calls)==3:
                self.assertIn('kept-proof',calls[-1]);ready.append(True)
            return {'action':'answer','tool':None,'fields':[],'diagnosis':'保留当前修正后的回答',
                'developer_fix':None,'settlement_binding':None,'answer':{'checks':[],'tag':'latest'}}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',side_effect=model), \
             patch('audit_core.audit_tools.execute_tool',return_value={'status':'completed','proof':'kept-proof'}):
            value=recover(provider,root=Path(folder)/'stage',skill=Path('skill'),schema={'type':'object'},
                prompt=bound_prompt('原资料'),images=[],label='引用修复',validator=validate,candidate=candidate,
                error=AuditError('原引用不支持'),diagnostics={})
        self.assertEqual(value['tag'],'latest');self.assertEqual(len(calls),1)
        self.assertTrue(provider._soft_check_advice)

    def test_structured_output_reuses_original_after_helper_repair(self):
        provider=PdfEvidenceProvider('fixture','medium');candidate={'rows':[1]};ready=[]
        response={'diagnosis':'兼容既有读取函数','developer_fix':None,'answer':candidate,
            'tool_requests':[{'tool':'project_source','rule_ids':['pos_fields'],'fields':[],'code':'{}'}]}
        def validate(value):
            if not ready:raise AuditError('旧函数不支持该表格')
        def tool(*args,**kwargs):
            ready.append(True);return {'source_code_modified':True,'receipt':'isolated.json'}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',return_value=response) as model, \
             patch('audit_core.audit_tools.execute_tool',side_effect=tool):
            value=recover_output(provider,root=Path(folder)/'stage',skill=Path('skill'),schema={'type':'object'},
                prompt=bound_prompt('原表格'),images=[],label='原生表格',validator=validate,candidate=candidate,
                error=AuditError('不支持'),diagnostics={})
            self.assertEqual(value,candidate);self.assertEqual(model.call_count,1)

    def test_legacy_repair_defers_business_predicate_after_one_attempt(self):
        provider=PdfEvidenceProvider('fixture','medium')
        provider._repair_case={'scenario':'price_difference_support','documents':[],'materials':[],'flags':{}}
        response={'action':'answer','tool':None,'fields':[],'diagnosis':'同一回答',
            'developer_fix':None,'settlement_binding':None,'answer':{'checks':[]}}
        def rejected(value):raise AuditError('同一业务匹配提示')
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call',return_value=response) as model:
            result=recover(provider,root=Path(folder)/'stage',skill=Path('skill'),schema={'type':'object'},
                prompt=bound_prompt('原资料'),images=[],label='无进展',validator=rejected,candidate=response['answer'],
                error=AuditError('原提示'),diagnostics={})
        self.assertEqual(result,{'checks':[]});self.assertEqual(model.call_count,1)
        self.assertTrue(provider._soft_check_advice)

    def test_invalid_patch_returns_actionable_error_without_writing(self):
        with ExitStack() as stack:
            _,source,_=self.setup_project(stack)
            result=repair.run({},json.dumps(self.request(source,'def normalize(value):\n broken (')))
            self.assertEqual(result['status'],'model_takeover');self.assertEqual(result['error_type'],'SyntaxError')
            self.assertEqual(source.read_text('utf-8'),ORIGINAL)

    def test_server_tool_protocol_hides_local_source_capability(self):
        from audit_core.audit_tools import tool_protocol
        with patch.object(repair,'authorized',return_value=False):
            protocol=tool_protocol()
            self.assertNotIn('project_source',protocol)
            self.assertIn('case_python',protocol)
        with patch.object(repair,'authorized',return_value=True):
            self.assertIn('project_source',tool_protocol())

    def test_all_saved_business_contracts_match_the_runtime_tool_contract(self):
        from audit_core.audit_tools import TOOL_REQUEST_SCHEMA
        from audit_core.scenario_registry import SKILL_BY_SCENARIO
        contract_root=repair.PROJECT_ROOT/'skills/orchestrate-offline-audit/references/contracts'
        saved=json.loads((contract_root/'audit-tool-requests.schema.json').read_text('utf-8'))
        self.assertEqual(saved,TOOL_REQUEST_SCHEMA)
        schemas=[contract_root/'pdf-policy-audit.schema.json']
        schemas.extend(Path(skill)/'references/evidence.schema.json' for skill in SKILL_BY_SCENARIO.values())
        for path in schemas:
            with self.subTest(path=path):
                schema=json.loads(path.read_text('utf-8'))
                self.assertEqual(schema['properties']['tool_requests'],TOOL_REQUEST_SCHEMA)
