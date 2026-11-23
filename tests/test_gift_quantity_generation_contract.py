"""No free-standing POS value can masquerade as the contractual gift allowance."""
from copy import deepcopy
from unittest import TestCase
import jsonschema
from audit_core.audit_rag import part_schema
from audit_core.pdf_evidence import audit_schema,audit_decision
from tests.pdf_test_support import flags_for
from tests.test_shared_failure_repairs import operand,comparison


class GiftQuantityGenerationContractTests(TestCase):
    def fixture(self,claim=20):
        docs=[{'unit_id':'c','roles':['contract'],'facts':['活动规则：购买10件赠1件；本次直接约定赠送20件。'],'limitations':[]},
              {'unit_id':'s','roles':['settlement'],'facts':[f'申报赠品数量：{claim}件。'],'limitations':[]},
              {'unit_id':'x','roles':['spreadsheet'],'facts':['实际购买商品销量：200件；赠品实际数量：20件。'],'limitations':[]}]
        mats=[{'id':role,'state':'present','source_ids':[uid],'reason':'原件'} for role,uid in
              [('promotion_contract','c'),('settlement','s'),('pos_excel','x')]]
        c=comparison(operand(f'申报赠品数量：{claim}件',claim,'s'),
            [operand('实际购买商品销量：200件',200,'x'),operand('购买10件赠1件',10,'c'),operand('购买10件赠1件',1,'c')],'ratio','le')
        c['value_kind']='quantity'
        value={'checks':[{'rule_id':'gift_rule_quantity','status':'pass','reason':'按合同规则核对购买依据',
                'source_ids':['s','c','x'],'comparisons':[c]}],'reference_reads':[]}
        return docs,mats,value

    def schema(self,scenario,docs,mats):return part_schema(audit_schema(scenario,materials=mats,documents=docs,units=[],include_photo_reviews=False),['gift_rule_quantity'],{})

    def test_raw_pos_gift_value_rejected_while_contract_direct_value_remains_available(self):
        for scenario in ('giveaway_promotion','self_procured_gift_material'):
            docs,mats,value=self.fixture();c=value['checks'][0]['comparisons'][0]
            c.update(operation='value',right=[operand('赠品实际数量：20件',20,'x')])
            with self.subTest(scenario=scenario),self.assertRaises(jsonschema.ValidationError):jsonschema.validate(value,self.schema(scenario,docs,mats))
            c['right']=[operand('本次直接约定赠送20件',20,'c')]
            jsonschema.validate(value,self.schema(scenario,docs,mats))

    def test_ratio_uses_pos_purchases_and_contract_parameters_and_preserves_real_excess(self):
        for claim,status in [(19,'pass'),(20,'pass'),(21,'fail')]:
            docs,mats,value=self.fixture(claim);before=deepcopy(value)
            jsonschema.validate(value,self.schema('giveaway_promotion',docs,mats))
            result=audit_decision('giveaway_promotion',flags_for('giveaway_promotion'),value,docs,mats,units=[],selected_rule_ids=['gift_rule_quantity'])['checks'][0]
            self.assertEqual(result['status'],status)
            self.assertEqual(value,before)

    def test_contract_and_pos_positions_cannot_be_interchanged_or_self_filled(self):
        for position in (0,1,2):
            docs,mats,value=self.fixture();value['checks'][0]['comparisons'][0]['right'][position]['unit_id']='s'
            with self.subTest(position=position),self.assertRaises(jsonschema.ValidationError):jsonschema.validate(value,self.schema('giveaway_promotion',docs,mats))

    def test_one_to_one_keeps_both_parameters_and_does_not_multiply_packaging_count(self):
        for buy,expected in [(1,'pass'),(2,'fail')]:
            docs,mats,value=self.fixture(claim=200)
            docs[0]['facts']=[f'本次实际搭赠比例{buy}:1；赠品10条装一盒。']
            c=value['checks'][0]['comparisons'][0]
            c['right']=[operand('实际购买商品销量：200件',200,'x'),
                        operand(f'{buy}:1',buy,'c'),operand(f'{buy}:1',1,'c')]
            jsonschema.validate(value,self.schema('giveaway_promotion',docs,mats))
            result=audit_decision('giveaway_promotion',flags_for('giveaway_promotion'),value,docs,mats,units=[],selected_rule_ids=['gift_rule_quantity'])['checks'][0]
            self.assertEqual(result['status'],expected)
            self.assertEqual(result['calculations'][0]['right_value'],'200' if buy==1 else '100')
