"""General settlement generation/source contract, no bill-specific runtime code."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

import jsonschema

from audit_core.audit_rag import part_schema
from audit_core.common import CheckEvidenceError
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.pdf_evidence import AUDIT_SCHEMA,audit_schema,audit_decision
from tests.pdf_test_support import flags_for
from tests.test_shared_failure_repairs import operand,comparison,settlement_fixture


def single_fee(total=3600,claim=None):
    fact=f'本次费用：人数2人，天数10天，单日工资180元，合计{total}元。'
    documents=[{'unit_id':'s','roles':['settlement'],'facts':[fact], 'limitations':[]},
               {'unit_id':'c','roles':['promotion_contract'],'facts':['计划预算3600元；人数2人，天数10天，单日工资180元。'], 'limitations':[]}]
    if claim is not None:documents[0]['facts'].append(f'本次核销金额：{claim}元。')
    materials=[{'id':'settlement','state':'present','source_ids':['s'],'reason':'原件'},
               {'id':'promotion_contract','state':'present','source_ids':['c'],'reason':'原件'}]
    c=comparison(operand(f'合计{total}元',total),[operand('人数2人',2),operand('天数10天',10),operand('单日工资180元',180)],'product','eq')
    checks=[{'rule_id':'settlement_calculation','status':'pass','reason':'按真实费用核对','source_ids':['s'],'comparisons':[c]}]
    return documents,materials,{'checks':checks,'reference_reads':[]}


class SettlementGenerationScopeTests(TestCase):
    def schema(self,scenario,docs,mats):
        return part_schema(audit_schema(scenario,materials=mats,documents=docs,include_photo_reviews=False),['settlement_calculation'],{})

    def decide(self,scenario,docs,mats,value):
        return audit_decision(scenario,flags_for(scenario),value,docs,mats,units=[],selected_rule_ids=['settlement_calculation'])['checks'][0]

    def test_single_fee_needs_only_the_actual_row_calculation_in_all_shared_scenarios(self):
        for scenario in ('personnel_incentive','promotional_display','price_difference_support','giveaway_promotion','self_procured_gift_material'):
            with self.subTest(scenario=scenario):
                docs,mats,value=single_fee();before=deepcopy(value)
                if scenario=='promotional_display':
                    value['checks'][0]['source_assertions']=[]
                    before=deepcopy(value)
                jsonschema.validate(value,self.schema(scenario,docs,mats))
                result=self.decide(scenario,docs,mats,value)
                self.assertEqual(result['status'],'pass')
                self.assertEqual(len(result['comparisons']),1)
                self.assertEqual(value,before)

    def test_contract_or_pos_cannot_be_generated_as_settlement_operands(self):
        for side in ('left','right'):
            docs,mats,value=single_fee();extra=deepcopy(value['checks'][0]['comparisons'][0])
            if side=='left':extra['left']=operand('计划预算3600元',3600,'c')
            else:extra['right'][0]=operand('人数2人',2,'c')
            value['checks'][0]['comparisons'].append(extra)
            with self.subTest(side=side),self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(value,self.schema('personnel_incentive',docs,mats))

    def test_same_total_is_not_a_second_claim_just_because_the_model_renames_it(self):
        for operation,operator in [('product','le'),('value','ge')]:
            docs,mats,value=single_fee();extra=deepcopy(value['checks'][0]['comparisons'][0])
            extra.update(label='实际核销金额上限',operation=operation,operator=operator)
            if operation=='value':extra['right']=[operand('合计3600元',3600)]
            value['checks'][0]['comparisons'].append(extra)
            with self.subTest(operation=operation),self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(value,self.schema('personnel_incentive',docs,mats))

    def test_true_under_and_over_stated_row_amounts_remain_business_failures(self):
        for total in (3500,3700):
            docs,mats,value=single_fee(total)
            jsonschema.validate(value,self.schema('personnel_incentive',docs,mats))
            self.assertEqual(self.decide('personnel_incentive',docs,mats,value)['status'],'fail')

    def test_independent_real_claim_is_still_checked_at_its_own_ceiling(self):
        for claim,status in [(3600,'pass'),(3800,'fail')]:
            docs,mats,value=single_fee(claim=claim)
            value['checks'][0]['comparisons'].append(comparison(operand(f'本次核销金额：{claim}元',claim),[operand('合计3600元',3600)],'value','le'))
            jsonschema.validate(value,self.schema('personnel_incentive',docs,mats))
            self.assertEqual(self.decide('personnel_incentive',docs,mats,value)['status'],status)

    def test_existing_multiple_rows_written_total_and_claim_keep_their_separate_rules(self):
        docs,mats,value=settlement_fixture();value['reference_reads']=[]
        jsonschema.validate(value,self.schema('personnel_incentive',docs,mats))
        self.assertEqual(self.decide('personnel_incentive',docs,mats,value)['status'],'pass')

    def test_source_error_precedes_operation_error_and_reports_the_actual_operand(self):
        docs,mats,value=single_fee();extra=deepcopy(value['checks'][0]['comparisons'][0])
        extra.update(operation='value',operator='ge');extra['left']=operand('计划预算3600元',3600,'c')
        extra['right']=[operand('合计3600元',3600)]
        value['checks'][0]['comparisons'].append(extra)
        with TemporaryDirectory() as temporary:
            schema=Path(temporary)/'schema.json';schema.write_text(json.dumps(AUDIT_SCHEMA),encoding='utf-8')
            parser=EvidenceOutputParser(schema_path=schema,post_validate=lambda v:self.decide('personnel_incentive',docs,mats,v))
            with self.assertRaises(CheckEvidenceError):parser.parse(json.dumps(value))
            self.assertEqual(parser.last_diagnostics['validation_code'],'comparison_source')
            self.assertEqual(parser.last_diagnostics['validation_paths'],['checks/0/comparisons/1/left/unit_id'])
            self.assertEqual(parser.last_retry_context,'')
