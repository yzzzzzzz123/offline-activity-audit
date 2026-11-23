"""A single vertical fee keeps original fields and a separate claim ceiling."""
from copy import deepcopy
from decimal import Decimal
from unittest import TestCase
import jsonschema

from audit_core.audit_rag import part_schema
from audit_core.common import AuditError
from audit_core.comparison_policy import single_settlement_expense, settlement_purpose
from audit_core.pdf_evidence import audit_decision, audit_schema
from tests.pdf_test_support import flags_for
from tests.test_shared_failure_repairs import comparison, operand


def fixture(quantity='7', price='82.50', total='577.50', claim='577.50', wrapper='表格行“', closing='”'):
    field = lambda label, value: wrapper + label + closing + '：' + value + '。'
    facts = ['页面标题为市场费用结算单。',
             '正文：本次执行门店与原表所列陈列费用对应。',
             f'正文：陈列核销金额合计为 {claim} 元，',
             '表格标题：“项目：本次陈列费用”。',
             field('活动时间', '2026年08月01日-2026年08月10日'),
             field('堆头数量', quantity+'（个）'),
             field('堆头金额', price+'元/堆头'), field('合计', total+'元')]
    uid='fee-source'
    docs=[{'unit_id':uid, 'roles':['settlement'], 'facts':facts, 'limitations':[]}]
    mats=[{'id':'settlement','state':'present','source_ids':[uid],'reason':'原件已读取'}]
    op=lambda fact, n: operand(fact,n,uid)
    row=comparison(op(facts[-1],total),[op(facts[-3],quantity),op(facts[-2],price)],'product','eq')
    cap=comparison(op(facts[2],claim),deepcopy(row['right']),'product','le')
    value={'checks':[{'rule_id':'settlement_calculation','status':'pass','reason':'核对原始费用与实际核销',
        'source_ids':[uid],'source_assertions':[],'comparisons':[row,cap]}],'reference_reads':[]}
    return docs,mats,value


def decide(data, scenario='promotional_display', *, generation=True):
    docs,mats,value=data
    if generation:
        schema=part_schema(audit_schema(scenario,documents=docs,materials=mats,units=[],include_photo_reviews=False),
                           ['settlement_calculation'],{})
        jsonschema.Draft202012Validator.check_schema(schema)
        jsonschema.validate(value,schema)
    return audit_decision(scenario,flags_for(scenario),value,docs,mats,units=[],
                          selected_rule_ids=['settlement_calculation'])['checks'][0]


class VerticalSettlementFieldGroupTests(TestCase):
    def numbered_fixture(self,claim='350',total='360'):
        data=fixture(quantity='4',price='90',total=total,claim=claim)
        data[0][0]['facts']=['文件标题：市场费用结算单。',
            f'正文：本次核销金额为_{claim}_元，核销明细如下：',
            '表格表头：项目：市场费用（堆头）。',
            '表格第1行：活动时间：2026年08月01日-2026年08月10日。',
            '表格第2行：堆头数量：4（个）。',
            '表格第3行：堆头金额：90元/堆头。',
            f'表格第4行：合计：{total}元。']
        row,cap=data[2]['checks'][0]['comparisons']
        row['left']['quote']=f'合计：{total}元'
        for operand,quote in zip(row['right'],['堆头数量：4（个）','堆头金额：90元/堆头']):operand['quote']=quote
        cap['left']['quote']=f'本次核销金额为_{claim}_元'
        cap['right']=deepcopy(row['right'])
        return data

    def test_numbered_vertical_fields_are_one_fee_with_original_quotes_preserved(self):
        data=self.numbered_fixture();before=deepcopy(data)
        basis=single_settlement_expense(data[0]);self.assertIsNotNone(basis)
        self.assertEqual(basis['factors'][0]['quote'],data[0][0]['facts'][4])
        self.assertEqual(basis['claims'][0]['number'],'350')
        result=decide(data,generation=False)
        self.assertEqual(result['status'],'pass')
        self.assertEqual([c['operator'] for c in result['comparisons']],['eq','le'])
        self.assertEqual(data,before)
        data[2]['checks'][0]['comparisons']=deepcopy([
            {**result['comparisons'][0],'left':basis['total'],'right':basis['factors']},
            {**result['comparisons'][1],'left':basis['claims'][0],'right':basis['factors']}])
        self.assertEqual(decide(data)['status'],'pass')

    def test_numbered_rows_keep_amount_precision_and_reject_other_fees_or_layouts(self):
        for claim,total in [('360.0001','360'),('350','359.99'),('350','360.01')]:
            with self.subTest(claim=claim,total=total):
                self.assertEqual(decide(self.numbered_fixture(claim,total),generation=False)['status'],'fail')
        for extra in ['表格第5行：其他费用：30元。','表格第5行：另一笔费用。',
                      '表格第2行：活动时间：2026年08月01日。',
                      '表格表头：另一张费用表。','表格第5行：活动时间：另有制作费用30元。',
                      '表格第5行：数量2个；单价30元；金额60元。']:
            data=self.numbered_fixture();data[0][0]['facts'].append(extra)
            with self.subTest(extra=extra):
                self.assertIsNone(single_settlement_expense(data[0]))
                with self.assertRaises(AuditError):decide(data,generation=False)

    def unnumbered_fixture(self):
        data=self.numbered_fixture()
        data[0][0]['facts']=[fact.replace('表格表头：','表格项目：')
            .replace('表格第1行：','表格行：').replace('表格第2行：','表格行：')
            .replace('表格第3行：','表格行：').replace('表格第4行：','表格行：')
            for fact in data[0][0]['facts']]
        return data

    def test_unnumbered_field_rows_preserve_original_quotes_in_all_shared_scenarios(self):
        for scenario in ('personnel_incentive','promotional_display','price_difference_support',
                         'giveaway_promotion','self_procured_gift_material'):
            data=self.unnumbered_fixture()
            if scenario!='promotional_display':data[2]['checks'][0].pop('source_assertions')
            before=deepcopy(data)
            with self.subTest(scenario=scenario):
                basis=single_settlement_expense(data[0]);self.assertIsNotNone(basis)
                self.assertEqual(basis['total']['quote'],'表格行：合计：360元。')
                self.assertEqual(decide(data,scenario,generation=False)['status'],'pass')
                self.assertEqual(data,before)
                row,cap=data[2]['checks'][0]['comparisons']
                row['left']=deepcopy(basis['total']);row['right']=deepcopy(basis['factors'])
                cap['left']=deepcopy(basis['claims'][0]);cap['right']=deepcopy(basis['factors'])
                self.assertEqual(decide(data,scenario)['status'],'pass')

    def test_unnumbered_rows_reject_unknown_fields_extra_fees_and_second_tables(self):
        for extra in ['表格行：其他费用：30元。','表格行：另一笔费用。',
                      '表格行：活动时间：另有制作费用30元。','表格行：堆头数量：2（个）。',
                      '表格行：数量2个；单价30元；金额60元。','表格项目：项目：另一笔费用。']:
            data=self.unnumbered_fixture();data[0][0]['facts'].append(extra)
            with self.subTest(extra=extra):
                self.assertIsNone(single_settlement_expense(data[0]))
                with self.assertRaises(AuditError):decide(data,generation=False)

    def test_equivalent_numeric_row_indices_are_still_duplicates(self):
        data=self.numbered_fixture()
        data[0][0]['facts'].append('表格第02行：活动时间：2026年08月01日。')
        self.assertIsNone(single_settlement_expense(data[0]))
        with self.assertRaises(AuditError):decide(data,generation=False)

    def test_quoted_vertical_fields_keep_one_original_group_in_all_shared_scenarios(self):
        for scenario in ('personnel_incentive','promotional_display','price_difference_support',
                         'giveaway_promotion','self_procured_gift_material'):
            data=fixture()
            if scenario!='promotional_display':data[2]['checks'][0].pop('source_assertions')
            before=deepcopy(data)
            with self.subTest(scenario=scenario):
                basis=single_settlement_expense(data[0])
                self.assertIsNotNone(basis)
                self.assertEqual([o['quote'] for o in basis['factors']],data[0][0]['facts'][-3:-1])
                result=decide(data,scenario)
                self.assertEqual(result['status'],'pass')
                self.assertEqual([c['operator'] for c in result['comparisons']],['eq','le'])
                self.assertEqual(data,before)

    def test_layout_wording_and_quotes_do_not_change_field_roles(self):
        for wrapper,closing in [('表格行“','”'),('表格行"','"'),('表格行‘','’'),
                                 ('表格行',''),('明细表“','”'),('表内“','”'),('','')]:
            with self.subTest(wrapper=wrapper): self.assertEqual(decide(fixture(wrapper=wrapper,closing=closing))['status'],'pass')

    def test_claim_total_qualifier_is_bound_to_its_own_original_value(self):
        for label in ('陈列核销金额合计','堆头及陈列核销金额总计','实际结算金额合计','本次核销总金额'):
            data=fixture()
            fact=data[0][0]['facts'][2].replace('陈列核销金额合计',label)
            data[0][0]['facts'][2]=fact
            cap=data[2]['checks'][0]['comparisons'][1]
            cap['left']['quote']=fact
            with self.subTest(label=label):
                self.assertEqual(settlement_purpose(cap,data[0]),'claim_ceiling')
                self.assertEqual(decide(data)['status'],'pass')

    def test_missing_claim_comparison_is_added_from_proven_group_without_mutating_answer(self):
        data=fixture(claim='560')
        data[2]['checks'][0]['comparisons'].pop()
        before=deepcopy(data)
        result=decide(data)
        self.assertEqual([c['operator'] for c in result['comparisons']],['eq','le'])
        self.assertEqual(result['comparisons'][1]['left']['number'],'560')
        self.assertIn('settlement_basis_completion',result)
        self.assertEqual(data,before)

    def test_real_arithmetic_difference_and_claim_excess_are_business_failures(self):
        for total,claim in [('577.49','570'),('577.51','570'),('577.50','577.5001'),('577.50','600')]:
            with self.subTest(total=total,claim=claim):
                result=decide(fixture(total=total,claim=claim))
                self.assertEqual(result['status'],'fail')
                self.assertTrue(any(not c['matches'] for c in result['calculations']))

    def test_non_fixture_values_and_zero_use_original_decimal_precision(self):
        for quantity,price in [('3','17.1234'),('0','92.80'),('11','5.03')]:
            total=str(Decimal(quantity)*Decimal(price))
            with self.subTest(quantity=quantity):self.assertEqual(decide(fixture(quantity,price,total,total))['status'],'pass')

    def test_extra_quoted_fee_or_conflicting_factor_cannot_be_ignored(self):
        for extra in ('表格行“培训费用”：30元。','表格行“堆头金额”：30元/堆头。',
                      '表格行“堆头数量”：2（个）。','表格行“合计”：30元。'):
            data=fixture();data[0][0]['facts'].append(extra)
            with self.subTest(extra=extra):
                self.assertIsNone(single_settlement_expense(data[0]))
                with self.assertRaises(AuditError):decide(data,generation=False)

    def test_multiple_tables_partial_reads_and_other_pages_never_gain_single_group_proof(self):
        for mode in ('titles','partial','second_page','indexed_rows'):
            data=fixture()
            if mode=='titles':data[0][0]['facts'].append('表格标题：“另一组费用”。')
            elif mode=='partial':data[0][0]['limitations']=['表格缺行，未完整读取。']
            elif mode=='indexed_rows':data[0][0]['facts'].append('表格行2：另一笔费用。')
            else:
                data[0].append({'unit_id':'other','roles':['settlement'],'facts':['另一笔费用。'],'limitations':[]})
                data[1][0]['source_ids'].append('other')
            with self.subTest(mode=mode):
                self.assertIsNone(single_settlement_expense(data[0]))
                with self.assertRaises(AuditError):decide(data,generation=False)

    def test_cross_fee_factor_and_aggregated_quantity_do_not_become_one_vertical_group(self):
        for combined in (False,True):
            data=fixture(quantity='4',price='90',total='360',claim='350')
            data[0][0]['facts'] += ['表格行“制作数量”：3（个）。','表格行“制作单价”：20元/个。',
                                   '表格行“制作金额”：60元。']
            row=data[2]['checks'][0]['comparisons'][0]
            if combined:
                data[0][0]['facts'].append('本次数量合计：7（个）。')
                row['right'][0]=operand('本次数量合计：7（个）。','7','fee-source')
            else:row['right'][1]=operand('表格行“制作单价”：20元/个。','20','fee-source')
            with self.subTest(combined=combined):
                self.assertIsNone(single_settlement_expense(data[0]))
                with self.assertRaises(AuditError):decide(data,generation=False)

    def test_original_generation_still_requires_complete_field_quotes(self):
        data=fixture()
        data[2]['checks'][0]['comparisons'][0]['left']['quote']='577.50元'
        with self.assertRaises(jsonschema.ValidationError):decide(data)
