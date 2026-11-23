"""Actual claim wording must not change the distinct row/total requirements."""
from copy import deepcopy
from unittest import TestCase
import jsonschema

from audit_core.audit_rag import part_schema
from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_schema,audit_decision
from audit_core.comparison_policy import settlement_purpose,single_settlement_expense
from tests.pdf_test_support import flags_for
from tests.test_shared_failure_repairs import comparison,operand


def fixture(claim=6000,total=6000,operation='value',connector='为'):
    facts=[f'正文：门店费用实际核销金额{connector} {claim} 元。',
           f'表格项目：本次陈列；数量：10个；单价：600元；合计：{total}元。']
    docs=[{'unit_id':'s','facts':facts,'roles':['settlement'],'limitations':[]},
          {'unit_id':'c','facts':['合同计划数量10个；单价600元。'],'roles':['promotion_contract'],'limitations':[]}]
    mats=[{'id':'settlement','state':'present','source_ids':['s'],'reason':'原件'},
          {'id':'promotion_contract','state':'present','source_ids':['c'],'reason':'原件'}]
    row=comparison(operand(f'合计：{total}元',total),[operand('数量：10个',10),operand('单价：600元',600)],'product','eq')
    support=[operand(f'合计：{total}元',total)] if operation=='value' else deepcopy(row['right'])
    cap=comparison(operand(f'实际核销金额{connector} {claim} 元',claim),support,operation,'le')
    return docs,mats,{'checks':[{'rule_id':'settlement_calculation','status':'pass','reason':'按结算原值核对',
            'source_ids':['s'],'source_assertions':[],'comparisons':[row,cap]}],'reference_reads':[]}


class ClaimFieldRoleTests(TestCase):
    def decide(self,value):
        docs,mats,evidence=value
        schema=part_schema(audit_schema('promotional_display',materials=mats,documents=docs,units=[],include_photo_reviews=False),['settlement_calculation'],{})
        jsonschema.validate(evidence,schema)
        return audit_decision('promotional_display',flags_for('promotional_display'),evidence,docs,mats,units=[],selected_rule_ids=['settlement_calculation'])['checks'][0]

    def test_real_claim_value_and_product_caps_accept_all_common_field_connectors(self):
        for connector in ('为','是','：',':','='):
            for operation in ('value','product'):
                with self.subTest(connector=connector,operation=operation):
                    value=fixture(operation=operation,connector=connector);before=deepcopy(value)
                    result=self.decide(value)
                    self.assertEqual(result['status'],'pass')
                    self.assertEqual([c['operator'] for c in result['comparisons']],['eq','le'])
                    self.assertEqual(value,before)

    def test_lower_claim_passes_higher_claim_and_incorrect_table_amount_fail(self):
        for claim,total,status in [(5900,6000,'pass'),(6100,6000,'fail'),(5800,5800,'fail')]:
            with self.subTest(claim=claim,total=total):self.assertEqual(self.decide(fixture(claim,total))['status'],status)

    def test_fill_in_underlines_preserve_claim_roles_in_all_shared_scenarios(self):
        scenarios=('personnel_incentive','promotional_display','price_difference_support',
                   'giveaway_promotion','self_procured_gift_material')
        for scenario in scenarios:
            for marker in ('_','___','＿',' _ ＿ '):
                for operation in ('value','product'):
                    docs,mats,value=fixture(claim=5900,operation=operation)
                    quote=f'实际核销金额为{marker}5900{marker}元'
                    docs[0]['facts'][0]='正文：'+quote+'。'
                    value['checks'][0]['comparisons'][1]['left']['quote']=quote
                    if scenario!='promotional_display':value['checks'][0].pop('source_assertions')
                    before=deepcopy((docs,mats,value))
                    with self.subTest(scenario=scenario,marker=marker,operation=operation):
                        result=audit_decision(scenario,flags_for(scenario),value,docs,mats,units=[],
                            selected_rule_ids=['settlement_calculation'])['checks'][0]
                        self.assertEqual(result['status'],'pass')
                        self.assertEqual([c['operator'] for c in result['comparisons']],['eq','le'])
                        self.assertEqual((docs,mats,value),before)

    def test_underlined_claim_does_not_hide_excess_or_bind_another_equal_amount(self):
        docs,mats,value=fixture(claim=6100)
        quote='实际核销金额为_6100_元'
        docs[0]['facts'][0]='正文：'+quote+'。'
        cap=value['checks'][0]['comparisons'][1];cap['left']['quote']=quote
        self.assertEqual(self.decide((docs,mats,value))['status'],'fail')
        cap['left'].update(quote='6000',number='6000')
        self.assertEqual(settlement_purpose(cap,docs),'declared_total')
        cap['left'].update(quote='实际核销金额为_6100_元',number='600')
        self.assertEqual(settlement_purpose(cap,docs),'declared_total')

    def test_claim_verbs_are_not_inferred_from_a_model_label_or_same_number(self):
        docs,_,value=fixture();row=value['checks'][0]['comparisons'][0]
        row.update(label='独立核销金额',operator='le')
        self.assertEqual(settlement_purpose(row,docs),'line_amount')
        with self.assertRaises(AuditError):self.decide((docs,fixture()[1],value))

    def test_contract_factors_remain_forbidden_for_a_real_claim(self):
        docs,mats,value=fixture(operation='product')
        value['checks'][0]['comparisons'][1]['right']=[operand('计划数量10个',10,'c'),operand('单价600元',600,'c')]
        with self.assertRaises((AuditError,jsonschema.ValidationError)):self.decide((docs,mats,value))

    def test_standalone_vertical_claim_with_a_verb_is_not_hidden_as_a_single_amount(self):
        docs=[{'unit_id':'s','facts':['人数：2人。','天数：3天。','单日工资：100元。','合计：600元。',
                                    '实际核销金额为 590 元。'],'limitations':[]}]
        basis=single_settlement_expense(docs)
        self.assertIsNotNone(basis)
        self.assertEqual([c['number'] for c in basis['claims']],['590'])

    def test_labelled_table_fields_with_quotes_and_explicit_per_item_units_keep_their_roles(self):
        docs,mats,value=fixture(claim=3000,total=3000)
        docs[0]['facts']=['正文：实际执行5家门店，600元/店，陈列核销金额为 3000 元。',
                         '明细表堆头数量：“5（个）”。','明细表堆头金额：“600 元/堆头”。','明细表合计：“3000 元”。']
        value['checks'][0]['comparisons']=[comparison(operand('明细表合计：“3000 元”。',3000),
            [operand('明细表堆头数量：“5（个）”。',5),operand('明细表堆头金额：“600 元/堆头”。',600)],'product','eq'),
            comparison(operand(docs[0]['facts'][0],3000),
            [operand('明细表堆头数量：“5（个）”。',5),operand('明细表堆头金额：“600 元/堆头”。',600)],'product','le')]
        result=self.decide((docs,mats,value))
        self.assertEqual(result['status'],'pass')
        self.assertEqual(len(result['comparisons']),2)
        changed=deepcopy(docs);changed[0]['facts'][2]='明细表堆头金额：“600 元”。'
        self.assertIsNone(single_settlement_expense(changed[:1]))

    def test_generation_requires_unambiguous_complete_field_quotes_for_equal_amounts(self):
        docs,mats,value=fixture(claim=600,total=600)
        docs[0]['facts']=['人数：2人。','天数：3天。','单日工资：100元。','合计：600元。','本次核销金额为 600元。']
        value['checks'][0]['comparisons']=[comparison(operand('600元',600),
            [operand('人数：2人。',2),operand('天数：3天。',3),operand('单日工资：100元。',100)],'product','eq')]
        with self.assertRaises(jsonschema.ValidationError):self.decide((docs,mats,value))
