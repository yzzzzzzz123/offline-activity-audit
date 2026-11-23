"""Upload execution cannot turn deterministic helper behavior into verdicts."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.model_authority import execution, transport_schema, validate_transport
from audit_core.pdf_evidence import audit_decision, audit_schema, classification_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import rules
from audit_core.scenario_registry import SCENARIO_ORDER
from tests.pdf_test_support import classification, flags_for
from tests.test_audit_rag_contract import case_for


class ModelOwnedBusinessFlowTests(TestCase):
    def test_all_eight_scenarios_publish_model_state_without_program_verdict(self):
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario):
                case,_=case_for(1);flags=flags_for(scenario)
                check={'rule_id':rules(scenario)[0].id,'status':'pass','reason':'模型已依据完整原文核对。',
                    'source_ids':[case['documents'][0]['unit_id']], 'comparisons':[{
                        'left':{'number':'1','quote':'不同表述'},'right':[{'number':'2'}],
                        'operator':'eq','operation':'value','value_kind':'amount'}]}
                evidence={'checks':[check]};before=deepcopy((evidence,flags,case['documents']))
                with execution(), patch('audit_core.pdf_evidence._program_audit_decision',side_effect=AssertionError('program veto')), \
                     patch('audit_core.pdf_evidence.calculate',side_effect=AssertionError('automatic calculation')):
                    result=audit_decision(scenario,flags,evidence,case['documents'],[])
                self.assertEqual(result['checks'][0]['status'],'pass')
                self.assertEqual(result['checks'][0]['reason'],check['reason'])
                self.assertEqual(result['checks'][0]['comparisons'],check['comparisons'])
                self.assertEqual(result['summary']['error_count'],0)
                self.assertEqual((evidence,flags,case['documents']),before)

    def test_missing_material_does_not_close_selected_business_audit(self):
        scenario='price_difference_support';case,_=case_for(1)
        value=classification(scenario,[d['unit_id'] for d in case['documents']],missing={'pos'})
        value['materials']=deepcopy(next(m['materials'] for m in value['material_matches'] if m['scenario']==scenario))
        with execution(),patch('audit_core.pdf_evidence.validate_classification',side_effect=AssertionError('material gate')):
            result=classification_decision(value,case['documents'],scenario)
        self.assertTrue(result['matched']);self.assertEqual(result['scenario'],scenario)
        self.assertFalse(result['deterministic_material_gate'])
        self.assertTrue(any(m['state']=='missing' for m in value['materials']))

    def test_transport_does_not_invoke_business_validator_or_repair_agent(self):
        from audit_core.model_settings import ModelSelection
        from audit_core.prompts import bound_prompt
        from audit_core.pdf_materials import ORCHESTRATOR_SKILL
        provider=PdfEvidenceProvider('fixture','medium',connection=ModelSelection(provider='qwen',api_key='fixture-only'))
        schema={'type':'object','required':['checks'],'properties':{'checks':{'type':'array',
            'minItems':5,'items':{'type':'object','additionalProperties':False,
                'required':['rule_id','status','reason','source_ids','comparisons'],
                'properties':{'rule_id':{'type':'string','const':'pos_arithmetic'},'status':{'enum':['unknown']},
                    'reason':{'type':'string','maxLength':4},'source_ids':{'type':'array','uniqueItems':True},
                    'comparisons':{'type':'array','minItems':1}}}}}}
        value={'checks':[{'rule_id':'pos_arithmetic','status':'pass','reason':'执行模型确认原始表的结果有效。',
                         'source_ids':[],'new_model_evidence':{'quote_end':'兼容的新字段'}}]}
        def extract(**kwargs):
            actual=json.loads(kwargs['schema_path'].read_text('utf-8'))
            kwargs['validator'](value)
            self.assertEqual(kwargs['max_attempts'],1)
            self.assertNotIn('const',json.dumps(actual));self.assertNotIn('minItems',json.dumps(actual))
            return value
        with TemporaryDirectory() as folder,execution(),patch('audit_core.qwen_model.extract_qwen_json',side_effect=extract):
            actual=provider._call_stage(Path(folder)/'stage',ORCHESTRATOR_SKILL,schema,bound_prompt('核对本单。'),
                images=[],label='隔离业务审核',validator=lambda v:(_ for _ in ()).throw(AssertionError('business validator')))
        self.assertEqual(actual,value)

    def test_unreadable_json_shape_is_not_published_as_a_decision(self):
        schema=transport_schema({'type':'object','required':['checks'],
            'properties':{'checks':{'type':'array'}}})
        with self.assertRaises(AuditError):validate_transport({'checks':'not an array'},schema)

    def test_failed_tool_returns_control_without_rejecting_the_stage(self):
        from audit_core.audit_tools import dispatch_requests
        case,_=case_for(1)
        requests=[{'tool':'unsupported-helper','rule_ids':[],'fields':[]},
                  {'tool':'reference_fields','fields':[]}]
        with execution():
            results=dispatch_requests(case,requests,[])
        self.assertFalse(results[0]['result']['complete'])
        self.assertEqual(results[0]['result']['status'],'model_takeover')
        self.assertEqual(results[1]['result']['tool'],'reference_fields')

    def test_material_rework_preserves_completed_pos_judgment(self):
        case,_=case_for(1);scenario='price_difference_support';case['scenario']=scenario
        case['flags']=flags_for(scenario)
        old=classification(scenario,[d['unit_id'] for d in case['documents']],missing={'pos'})
        revised=classification(scenario,[d['unit_id'] for d in case['documents']])
        proof={'checks':[{'rule_id':'pos_arithmetic','status':'pass','reason':'150行及两项合计复算完成。',
                          'source_ids':[],'comparisons':[]}]}
        packet={'archive_id':case['archive_id'],'classification':old,'route':{'scenario':scenario,'matched':True},
                'documents':case['documents'],'units':case['units'],'audit_evidence':deepcopy(proof)}
        provider=PdfEvidenceProvider('fixture','medium');provider._audit_cases[case['archive_id']]=deepcopy(case)
        with TemporaryDirectory() as folder,execution(),patch.object(provider,'_call',return_value=revised), \
             patch.object(provider,'audit',side_effect=AssertionError('unrelated full audit')):
            provider.rework_materials(packet,Path(folder),{'findings':['申请和批复实际已取得。']})
        self.assertEqual(packet['audit_evidence'],proof)
        self.assertEqual(packet['classification'],revised)

    def test_auxiliary_source_findings_cannot_overwrite_model_judgment(self):
        from audit_core.pdf_workflow import _with_submitted_checks
        result={'checks':[{'rule_id':'pos_fields','status':'pass','reason':'模型确认。'}]}
        helper=[{'rule_id':'pos_fields','status':'fail','reason':'旧解析路径未识别。','source_ids':[]}]
        with execution():actual=_with_submitted_checks(result,helper)
        self.assertEqual(actual,result)

    def test_actual_provider_audit_uses_model_after_failed_helpers(self):
        case,_=case_for(1);provider=PdfEvidenceProvider('fixture','medium',agent_mode='single')
        value={'checks':[{'rule_id':'contract_signed','status':'unknown','reason':'原图章面部分被遮挡。',
            'source_ids':['contract'],'comparisons':[]}]}
        tool={'tool':'case_python','rule_ids':['contract_signed'],'fields':[],
              'source_ids':['contract'],'code':'result = 1 / 0'}
        outputs=[{**deepcopy(value),'tool_requests':[tool]},deepcopy(value)]
        seen=[]
        def model(root,skill,schema,prompt,**kwargs):
            request=json.loads(prompt.partial_variables['request']);seen.append(request)
            return outputs.pop(0)
        def self_check(owner,current,candidate,*args,**kwargs):
            self.assertEqual(candidate,value)
            self.assertEqual(seen[-1]['tools'][0]['result']['status'],'model_takeover')
            return candidate
        with TemporaryDirectory() as folder, \
             patch('audit_core.pdf_materials.prepare_product_references',side_effect=AuditError('工具未支持布局')), \
             patch('audit_core.pdf_materials.prepare_pos_identities',return_value={}), \
             patch('audit_core.pdf_materials.template_comparison',return_value=({},[])), \
             patch('audit_core.model_self_check.review_stage',side_effect=self_check), \
             patch('audit_core.audit_tools.execute_tool',return_value={'complete':False,'status':'model_takeover','error':'除零'}), \
             patch.object(provider,'_call',side_effect=model) as calls, \
             patch('audit_core.pdf_evidence._program_audit_decision',side_effect=AssertionError('business gate')):
            result=provider.audit(case,Path(folder))
        self.assertEqual(result,value);self.assertEqual(calls.call_count,2)
        self.assertEqual(seen[0]['product_references']['status'],'model_takeover')

    def test_all_eight_upload_workflows_continue_from_missing_material_to_model_final_review(self):
        from audit_core.pdf_workflow import PdfWorkflow
        from audit_core.scenario_registry import SCENARIO_LABELS
        from audit_core.pdf_policy import requirements
        from tests.pdf_test_support import bundle
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario),TemporaryDirectory() as folder:
                root=Path(folder);bundle(root/'input');calls=[]
                provider=PdfEvidenceProvider('fixture','medium',agent_mode='single')
                missing=next(r.id for r in requirements(scenario) if r.when=='always')
                checks=[]
                def read(case,*args):
                    return [{'unit_id':u['unit_id'],'roles':['submitted'],
                        'facts':['本次原始凭证可见；部分章面文字模糊。'],'limitations':[]} for u in case['units']]
                def model(stage,skill,schema,prompt,**kwargs):
                    calls.append(kwargs['label'])
                    if 'candidate_scenarios' in schema['properties']:
                        # The prepared source ID is supplied in the real prompt.
                        docs=json.loads(prompt.partial_variables['documents'])
                        ids=[d['unit_id'] for d in docs]
                        value=classification(scenario,ids,missing={missing})
                        match=next(m for m in value['material_matches'] if m['scenario']==scenario)
                        value.update(candidate_scenarios=[scenario],materials=deepcopy(match['materials']),
                            flags=deepcopy(match['flags']),material_matches=[deepcopy(match)])
                    elif kwargs['label'].endswith('模型自主审核'):
                        request=json.loads(prompt.partial_variables['request'])
                        uid=request['documents'][0]['unit_id']
                        checks[:]=[{'rule_id':r.id,'status':'unknown' if i==0 else 'pass',
                            'reason':'原始章面文字模糊，暂不能确认对应内容。' if i==0 else '执行模型已结合本单资料完成核对。',
                            'source_ids':[uid], 'model_evidence':{'原始内容':'保留模型自行组织的证据。'}}
                            for i,r in enumerate(rules(scenario))]
                        value={'checks':deepcopy(checks)}
                    elif kwargs['label']=='模型自主阶段自测':
                        value={'ready':True,'checks':deepcopy(checks),'notes':[]}
                    elif 'findings' in schema['properties'] and 'items' not in schema['properties']:
                        value={'ready':True,'findings':[],'notes':['已结合原始资料完成本单逻辑自查。']}
                    else:
                        request=json.loads(prompt.partial_variables['request'])
                        value={'ready':True,'issues':[],'items':[{'source_problem_ids':[p['id']],
                            'text':p['text'],'uncertainty_preserved':True} for p in request['problems']]}
                    kwargs['validator'](value)
                    return value
                flow=PdfWorkflow('isolated-model-owned-'+scenario,'fixture',root/'input',root/'model',
                    provider,None,None,biz_type=SCENARIO_LABELS[scenario])
                with patch.object(provider,'_read_documents',side_effect=read),patch.object(provider,'_call',side_effect=model), \
                     patch('audit_core.pdf_materials.prepare_product_references',side_effect=AuditError('辅助工具布局不支持')), \
                     patch('audit_core.pdf_materials.prepare_pos_identities',side_effect=AuditError('辅助查询不可用')), \
                     patch('audit_core.pdf_materials.template_comparison',return_value=({},[])), \
                     patch('audit_core.pdf_workflow.submitted_source_checks',side_effect=AuditError('辅助引用识别失败')), \
                     patch('httpx.Client',side_effect=AssertionError('external network forbidden')), \
                     patch('httpx.AsyncClient',side_effect=AssertionError('external network forbidden')):
                    receipt=flow.run()
                packet=flow.packets[0]
                self.assertTrue(packet['route']['matched'])
                self.assertTrue(any(m['state']=='missing' for m in packet['classification']['materials']))
                self.assertEqual(packet['audit_evidence']['checks'],checks)
                self.assertIn('模型自主阶段自测',calls)
                self.assertEqual(calls.count('最终AI总结逻辑自校验'), 1)
                self.assertEqual(receipt['verification']['logic_review']['status'],'passed')
                self.assertFalse(receipt['verification']['pdf_policy']['deterministic_business_gates'])
                self.assertIn('暂不能确认',receipt['reviewed_analysis_summary'])
