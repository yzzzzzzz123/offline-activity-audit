"""Completed helper evidence survives recovery and reaches model review."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.audit_tools import execute_tool
from audit_core.model_metrics import collect_model_artifacts
from audit_core.tool_observations import observations
from audit_core.final_logic_review import context,SKILL
from audit_core.model_self_check import review_stage
from audit_core.pdf_evidence import calculate,document_map
from audit_core.pos_arithmetic import prepare_pos_arithmetic
from tests.test_pos_arithmetic_coverage import source
from tests.test_final_logic_stage import fixture,accepted


def case_with_pos():
    documents,materials,units=source(150,split=True)
    return {'archive_id':'a001','scenario':'price_difference_support','flags':{},
            'documents':documents,'materials':materials,'units':units}


def legacy_proof(root,case):
    plan=prepare_pos_arithmetic(case['documents'],case['materials'],case['units'])
    proof={'plan':plan,'calculations':[calculate(c,document_map(case['documents'])) for c in plan['comparisons']]}
    folder=root/'model-observations';folder.mkdir(parents=True,exist_ok=True)
    path=folder/'tool-pos-calculation-a001.json'
    path.write_text(json.dumps(proof,ensure_ascii=False),encoding='utf-8')
    return path


class ToolObservationTests(TestCase):
    def test_normal_tool_result_survives_a_new_case_object_without_changing_inputs(self):
        case=case_with_pos();original=deepcopy(case)
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            result=execute_tool(case,'pos_calculation')
            self.assertEqual(result['calculation_count'],152)
            replayed=observations(deepcopy(original))
            self.assertEqual(replayed[0]['result'],result)
            self.assertFalse(replayed[0]['authoritative'])
            self.assertEqual(case,original)

    def test_observations_do_not_cross_archives_sources_or_runs(self):
        case=case_with_pos()
        with TemporaryDirectory() as folder:
            with collect_model_artifacts(Path(folder)/'first/analysis'):
                execute_tool(case,'pos_calculation')
                foreign=deepcopy(case);foreign['archive_id']='a002'
                self.assertEqual(observations(foreign),[])
                changed=deepcopy(case);changed['documents'][0]['facts'][1]+=' source changed'
                self.assertEqual(observations(changed),[])
            with collect_model_artifacts(Path(folder)/'second/analysis'):
                self.assertEqual(observations(case),[])

    def test_old_pos_proof_is_recalculated_before_it_can_inform_a_review(self):
        case=case_with_pos();original=deepcopy(case)
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            path=legacy_proof(Path(folder)/'analysis',case);before=path.read_bytes()
            result=observations(case)[0]
            self.assertTrue(result['revalidated_against_current_sources'])
            self.assertTrue(result['result']['complete'])
            self.assertEqual(result['result']['calculation_count'],152)
            self.assertEqual(result['result']['difference_count'],0)
            self.assertEqual(path.read_bytes(),before);self.assertEqual(case,original)

    def test_old_proof_with_missing_rows_or_changed_calculations_is_not_accepted(self):
        for corrupted in ('plan','calculations'):
            with self.subTest(corrupted=corrupted),TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
                case=case_with_pos();path=legacy_proof(Path(folder)/'analysis',case)
                proof=json.loads(path.read_text('utf-8'))
                if corrupted=='plan':proof['plan']['comparisons'].pop()
                else:proof['calculations'][0]['matches']=False
                path.write_text(json.dumps(proof),encoding='utf-8')
                self.assertEqual(observations(case),[])

    def test_failed_tool_is_advice_without_becoming_a_customer_finding(self):
        case=case_with_pos();original=deepcopy(case)
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch('audit_core.audit_tools._execute_tool',side_effect=ValueError('unsupported layout')):
            result=execute_tool(case,'pos_calculation')
            self.assertEqual(result['status'],'model_takeover')
            self.assertEqual(observations(case)[0]['result'],result)
            self.assertEqual(case,original)

    def test_changed_helper_implementation_requires_revalidation_of_old_results(self):
        case=case_with_pos()
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            execute_tool(case,'pos_calculation')
            with patch('audit_core.tool_observations._implementation',return_value='new-helper-version'):
                records=observations(case)
                self.assertEqual(len(records),1)
                self.assertTrue(records[0]['revalidated_against_current_sources'])
                self.assertEqual(records[0]['implementation_fingerprint'],'new-helper-version')

    def test_final_review_and_targeted_rework_receive_the_same_completed_proof(self):
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            case,packet,provider=fixture('price_difference_support')
            pos=case_with_pos();case.update(documents=pos['documents'],materials=pos['materials'],units=pos['units'])
            packet.update(documents=case['documents'],units=case['units'])
            packet['classification']['materials']=case['materials']
            for c in packet['audit_evidence']['checks']:c['source_ids']=[]
            check=next(c for c in packet['audit_evidence']['checks'] if c['rule_id']=='pos_arithmetic')
            check.update(status='unknown',reason='原始复核尚未返回',source_ids=[d['unit_id'] for d in case['documents']])
            legacy_proof(Path(folder)/'analysis',case)
            packet['result']={'checks':packet['audit_evidence']['checks']}
            request,_,_=context([packet],provider)
            proof=request['archives'][0]['tool_observations'][0]['result']
            self.assertEqual(proof['difference_count'],0)
            self.assertEqual(proof['calculation_count'],152)
            candidate={'checks':[deepcopy(check)]}
            def model(root,skill,schema,prompt,**kwargs):
                payload=json.loads(prompt.partial_variables['request'])
                self.assertEqual(payload['tool_observations'][0]['result'],proof)
                updated={**deepcopy(check),'status':'pass','reason':'本单原值复算符合规定比较方向。'}
                return {'ready':True,'notes':[],'checks':[updated]}
            with patch.object(provider,'_call',side_effect=model) as call:
                value=review_stage(provider,case,candidate,{'checks':candidate['checks']},Path(folder)/'target',
                    ['pos_arithmetic'],lambda value,ids:{'checks':value['checks']})
            self.assertEqual(value['checks'][0]['status'],'pass');self.assertEqual(call.call_count,1)
            self.assertEqual(check['status'],'unknown','Only the model response may revise the check.')
