"""Source-bound offline regressions for the adopted 11/17 workbook.

These checks exercise applicability, actual tools and prompt inputs. No model,
Qince, callback or server operation is used.
"""
from copy import deepcopy
from datetime import date
from pathlib import Path
import hashlib
import json
import unittest
from unittest.mock import patch

from audit_core import pdf_policy as policy
from audit_core.agent_orchestration import owner, scoped_case
from audit_core.audit_tools import _execute_tool
from audit_core.fee_policy_checks import payment_date_check, validate_workbook_comparison
from audit_core.pdf_evidence import calculate, document_map
from audit_core.model_authority import business_guidance
from audit_core.submitted_source_checks import submitted_source_checks
from audit_core.native_pos_fields import native_pos_field_check
from tests.pdf_test_support import flags_for

ROOT = Path(__file__).resolve().parents[1]

def doc(uid, *facts):
    return {'unit_id':uid,'roles':[],'facts':list(facts),'limitations':[]}

def material(role, *ids):
    return {'id':role,'state':'present','source_ids':list(ids),'reason':'本次实际原件'}

def operand(uid, quote, value):
    return {'unit_id':uid,'quote':quote,'number':str(value)}

def by_id(scenario):
    return {r.id:r for r in policy.rules(scenario)}

class Nov17PolicyTests(unittest.TestCase):
    def test_canonical_source_hash_rows_and_confirmed_interpretations(self):
        cat=policy.catalogue()
        source=ROOT/'skills/orchestrate-offline-audit/references/fee-policy'/cat['source']
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),cat['source_sha256'])
        self.assertEqual(cat['source'],'附件：费用分类及申请核销资料表-2026.11.17.xlsx')
        self.assertEqual(cat['source_rows']['entry_fee'],'5:6')
        self.assertEqual(cat['source_rows']['personnel_incentive'],'11:13')
        self.assertEqual(cat['interpretations']['commission_quantity'],'settlement_le_pos')
        self.assertEqual(cat['interpretations']['training_address'],'not_required')
        self.assertEqual(len(cat['types']),8)

    def test_public_seal_required_for_each_stamped_business_document(self):
        for s in policy.MATERIALS:
            for id in {'settlement_seal','settlement_template_seal','contract_signed','pos_seal','payment_statement_seal'} & by_id(s).keys():
                with self.subTest(scenario=s,rule=id):
                    text=by_id(s)[id].text
                    self.assertIn('公章',text)
                    self.assertNotIn('不区分章种',text)
                    self.assertNotIn('公章或财务专用章',text)

    def test_receipt_seal_uses_payee_not_advance_dealer_and_is_document_owned(self):
        self.assertEqual(owner('receipt_finance_seal'),'documents')
        for s in ('poster_material','self_procured_gift_material'):
            r=by_id(s)['receipt_finance_seal']
            self.assertIn('收款方公司财务专用章',r.text)
            self.assertTrue(policy.applicability(r.when,s,{'uses_receipt':True}))
            self.assertFalse(policy.applicability(r.when,s,{'uses_receipt':False}))
            case={'archive_id':'offline','scenario':s,'flags':{},'materials':[material('invoice','receipt')],
                  'documents':[doc('receipt','收据：收款方甲公司财务专用章')],'units':[{'unit_id':'receipt','source_file':'收据.pdf'}]}
            self.assertIn('receipt',{d['unit_id'] for d in scoped_case(case,'documents')['documents']})

    def test_procured_gifts_require_both_pos_versions_and_both_photo_roles(self):
        s='self_procured_gift_material'
        requirements={m.id:m for m in policy.requirements(s)}
        for flags in ({'uses_pos':True,'photo_evidence':False},{'uses_pos':False,'photo_evidence':True}):
            for role in ('invoice','pos','pos_excel','gift_finished_photos','gift_store_photos'):
                with self.subTest(flags=flags,role=role):
                    self.assertIs(policy.applicability(requirements[role].when,s,flags),True)
        self.assertTrue(policy.applicability('required_pos',s,{'uses_pos':False}))
        self.assertIn('不能相互替代',by_id(s)['gift_activity_evidence'].text)

    def test_display_target_and_wage_all_have_independent_approval_equality(self):
        for s in ('promotional_display','pos_target_incentive','personnel_incentive'):
            flags=flags_for(s,temporary_staff=s=='personnel_incentive',sales_commission=False)
            r=by_id(s)['claim_ceiling']
            self.assertIs(policy.applicability(r.when,s,flags),True)
            self.assertEqual(policy.comparison_operators(s,r.id),{'amount':'eq'})
            flags['_recorded_approval']=False
            self.assertIs(policy.applicability(r.when,s,flags),False)

    def test_commission_quantity_boundary_changes_only_commission_not_price_support(self):
        for claim,pos,commission_ok,price_ok in [(100,100,True,True),(99,100,True,False),(101,100,False,False)]:
            documents=[doc('s',f'结算销量{claim}'),doc('p',f'POS销量{pos}')]
            for scenario,wanted in [('personnel_incentive',commission_ok),('price_difference_support',price_ok)]:
                c={'label':'销量','value_kind':'quantity','left':operand('s',f'结算销量{claim}',claim),
                   'right':[operand('p',f'POS销量{pos}',pos)],'operation':'value',
                   'operator':policy.comparison_operators(scenario,'settlement_pos_quantity')['quantity']}
                self.assertIs(calculate(c,document_map(documents))['matches'],wanted)

    def test_contract_reward_price_needs_contract_and_application_independent_sources(self):
        docs=[doc('c','合同奖励单价2'),doc('a','申请单价2'),doc('s','结算单价2')]
        mats=[material('promotion_contract','c'),material('activity_application','a'),material('settlement','s')]
        c={'label':'合同单价','value_kind':'unit_price','left':operand('c','合同奖励单价2',2),
           'right':[operand('a','申请单价2',2)],'operation':'value','operator':'eq'}
        validate_workbook_comparison('personnel_incentive','contract_reward_unit_price',c,docs,mats)
        c['left']=operand('s','结算单价2',2)
        with self.assertRaisesRegex(Exception,'独立系统申请'):
            validate_workbook_comparison('personnel_incentive','contract_reward_unit_price',c,docs,mats)
        self.assertEqual(owner('contract_reward_unit_price'),'numbers')

    def test_bank_paid_temporary_wages_do_not_skip_amount_or_dates(self):
        s='personnel_incentive'
        flags=flags_for(s,temporary_staff=True,sales_commission=False,red_packet=False)
        for id in ('payment_claim_amount','payment_activity_dates','claim_ceiling'):
            self.assertIs(policy.applicability(by_id(s)[id].when,s,flags),True)
        self.assertIs(policy.applicability(by_id(s)['payment_company'].when,s,flags),False)

    def test_wage_payment_three_calendar_months_includes_last_day(self):
        docs=[doc('s','活动开始2026-01-01','活动结束2026-01-31'),doc('p','付款日期2026-04-30')]
        mats=[material('settlement','s'),material('payment','p')]
        def ref(uid,quote,day):return {'unit_id':uid,'quote':quote,'date':day}
        evidence={'start':ref('s','活动开始2026-01-01','2026-01-01'),
            'end':ref('s','活动结束2026-01-31','2026-01-31'),
            'payments':[ref('p','付款日期2026-04-30','2026-04-30')],'complete':True}
        self.assertEqual(payment_date_check(evidence,docs,mats)['status'],'pass')
        docs[-1]=doc('p','付款日期2026-05-01')
        evidence['payments']=[ref('p','付款日期2026-05-01','2026-05-01')]
        self.assertEqual(payment_date_check(evidence,docs,mats)['status'],'fail')

    def test_payment_period_does_not_mix_settlement_start_with_application_end(self):
        docs=[doc('s','开始2026-01-01'),doc('a','结束2026-01-31'),doc('p','付款2026-04-30')]
        mats=[material('settlement','s'),material('activity_application','a'),material('payment','p')]
        evidence={'start':{'unit_id':'s','quote':'开始2026-01-01','date':'2026-01-01'},
            'end':{'unit_id':'a','quote':'结束2026-01-31','date':'2026-01-31'},
            'payments':[{'unit_id':'p','quote':'付款2026-04-30','date':'2026-04-30'}],'complete':True}
        with self.assertRaisesRegex(Exception,'不能混用结算单'):
            payment_date_check(evidence,docs,mats)

    def test_pure_training_payment_does_not_inherit_commission_red_packet_brand(self):
        s='personnel_incentive'
        flags=flags_for(s,training=True,temporary_staff=False,sales_commission=False,red_packet=True)
        self.assertIs(policy.applicability(by_id(s)['payment_company'].when,s,flags),False)
        self.assertIs(policy.applicability(by_id(s)['payment_claim_amount'].when,s,flags),True)

    def test_commission_shortfall_message_describes_quantity_not_application_money(self):
        from tests.test_customer_language import calculated_check
        from audit_core.customer_language import numeric_problem
        check, units, _=calculated_check('settlement_pos_quantity','12',['10'])
        reason=numeric_problem(check,units)
        self.assertIn('结算申报销量超过POS实际销量',reason)
        self.assertNotIn('核销金额不能超过申请金额',reason)

    def test_training_address_exception_does_not_waive_date_time_or_field_staff(self):
        text=by_id('personnel_incentive')['training_photo_content'].text
        for wanted in ('不要求地址水印','不进行门店地址匹配','活动日期','拍摄时间','培训现场','每场至少一张'):
            self.assertIn(wanted,text)
        staff=by_id('personnel_incentive')['staff_photo_content'].text
        self.assertIn('地址匹配',staff)
        self.assertIn('每一天',staff)

    def test_pos_dates_rule_has_both_complete_pos_and_settlement_sources(self):
        for s in ('personnel_incentive','self_procured_gift_material','price_difference_support','giveaway_promotion','pos_target_incentive'):
            self.assertIn('全部明细',by_id(s)['pos_activity_dates'].text)
        self.assertEqual(owner('pos_activity_dates'),'numbers')
        s='price_difference_support'
        case={'archive_id':'offline','scenario':s,'flags':{},'materials':[material('pos_excel','p'),material('settlement','s')],
              'documents':[doc('p','POS原始日期及全部行'),doc('s','结算活动周期')],
              'units':[{'unit_id':'p','source_file':'POS.xlsx'},{'unit_id':'s','source_file':'结算.pdf'}]}
        self.assertEqual({d['unit_id'] for d in scoped_case(case,'numbers')['documents']},{'p','s'})

    def test_poster_dates_and_inventory_excel_are_explicit(self):
        self.assertEqual(owner('poster_payment_dates'),'numbers')
        r=by_id('poster_material')['poster_payment_dates']
        self.assertTrue(policy.applicability(r.when,'poster_material',{'poster_posting':True}))
        self.assertFalse(policy.applicability(r.when,'poster_material',{'poster_posting':False}))
        entry=next(m for m in policy.requirements('entry_fee') if m.id=='entry_system')
        self.assertIn('对应原生Excel',entry.text)
        self.assertIn('二选一',entry.text)
        self.assertEqual(policy.entry_photo_requirement(50,True),('stores',40))

    def test_target_actual_tool_cannot_silently_use_amount_only_plan(self):
        case={'scenario':'pos_target_incentive','archive_id':'offline','documents':[],'materials':[],'units':[]}
        with patch('audit_core.pos_arithmetic.prepare_pos_arithmetic',return_value=None) as calc:
            _execute_tool(case,'pos_calculation')
        self.assertNotIn('amount_only',calc.call_args.kwargs)
        _, proof=native_pos_field_check({'scenario':'pos_target_incentive','flags':{'uses_pos':False}})
        self.assertNotEqual(proof.get('fallback_reason'),'amount_only_policy_uses_original_sources')

    def test_target_missing_unit_price_advice_matches_other_pos_types(self):
        facts=['Sheet1!R1: C1=商品名称 | C2=69码 | C3=销售单价 | C4=销售数量 | C5=销售金额',
               'Sheet1!R2: C1=牙膏 | C2=6970356164289 | C3= | C4=2 | C5=20']
        docs=[doc('p',*facts)]
        mats=[material('pos_excel','p')]
        units=[{'unit_id':'p','source_file':'POS.xlsx','native_facts':facts}]
        results=[submitted_source_checks(s,{'uses_pos':True},docs,mats,units)
                 for s in ('pos_target_incentive','price_difference_support')]
        self.assertEqual(results[0],results[1])
        self.assertTrue(any('单价' in r['reason'] for r in results[0]))

    def test_actual_business_guidance_and_generated_guides_no_longer_mix_old_exemptions(self):
        forbidden=('不区分章种','公章或财务专用章','还允许合同章','不强制数量、销售单价或数量合计',
                   '外采赠品执行证明为POS数据与活动照片二选一','不恢复逐店逐日','单笔红包超过1000元须有经销商公司名称')
        for s in policy.MATERIALS:
            text=business_guidance(s)
            self.assertIn('2026.11.17',text)
            for token in forbidden:self.assertNotIn(token,text)
        background=(ROOT/'skills/orchestrate-offline-audit/references/business-background.md').read_text('utf-8')
        for token in forbidden:self.assertNotIn(token,background)

if __name__=='__main__':unittest.main()
