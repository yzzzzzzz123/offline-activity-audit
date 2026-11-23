"""Current workbook policy, isolated sources and deterministic amount/date proofs."""
from copy import deepcopy
from datetime import date
from pathlib import Path
import hashlib,json,unittest
from audit_core.common import AuditError
from audit_core.pdf_policy import (POLICY_VERSION,SOURCE_TITLE,SOURCE_SHA256,FLAGS,applicability,requirements,rules,comparison_operators,catalogue)
from audit_core.pdf_evidence import (audit_decision,audit_schema,classification_schema,validate,validate_classification)
from audit_core.fee_policy_checks import (application_sources,approval_sources,constrain_application_check,
    validate_workbook_comparison,add_months,payment_date_check,target_tier_comparison)
from audit_core.qince_context import context_sources,VERIFICATION,APPLICATIONS
from tests.pdf_test_support import flags_for,classification,unknown_checks

def doc(uid,*facts):return {'unit_id':uid,'roles':[],'facts':list(facts),'limitations':[]}
def mat(id,*ids):return {'id':id,'state':'present','source_ids':list(ids),'reason':'本次实际资料'}
def operand(uid,quote,number):return {'unit_id':uid,'quote':quote,'number':str(number)}
def ref(uid,day):return {'unit_id':uid,'quote':day,'date':day}
def comparison(left,right,kind='amount',operation='value',operator='eq'):
    return {'label':'本次原值比较','value_kind':kind,'left':left,'right':right,'operation':operation,'operator':operator}

class WorkbookPolicyTests(unittest.TestCase):
    def test_workbook_source_and_correction_are_traceable_and_unchanged(self):
        from audit_core.paths import PROJECT_ROOT
        source=PROJECT_ROOT/'skills/orchestrate-offline-audit/references/fee-policy'/SOURCE_TITLE
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),SOURCE_SHA256)
        snapshot=json.loads(source.with_name('source-workbook.json').read_text(encoding='utf-8'))
        self.assertEqual(snapshot['user_corrections'][0]['cell'],'Sheet1!H12')
        self.assertEqual(snapshot['user_corrections'][0]['execution'],'结算销量≤POS全量对应销量')
        self.assertEqual(snapshot['user_corrections'][1]['cell'],'Sheet1!H13')
        self.assertEqual(POLICY_VERSION,'2026-11-17.xlsx.53')

    def test_all_eight_types_have_current_sources_closed_schemas_and_unique_checks(self):
        policy=catalogue();self.assertEqual(len(policy['types']),8)
        for scenario in policy['types']:
            rs=rules(scenario);self.assertEqual(len(rs),len({r.id for r in rs}))
            self.assertTrue(all(r.source for r in rs));self.assertIn('activity_application',{m.id for m in requirements(scenario)})
            self.assertEqual(audit_schema(scenario)['properties']['checks']['maxItems'],len(rs))
            self.assertIn('poster_posting',classification_schema(scenario)['properties']['flags']['properties'])

    def test_personnel_wage_training_sales_and_mixed_pos_requirements(self):
        s='personnel_incentive'
        for overrides,required in [({},True),({'temporary_staff':True},False),({'training':True},False),
            ({'temporary_staff':True,'sales_commission':True},True),({'store_incentive':True},True)]:
            f=flags_for(s,**overrides);self.assertEqual(applicability('personnel_pos',s,f),required)
        wage=flags_for(s,temporary_staff=True)
        self.assertTrue(applicability('confirmed_temporary_staff',s,wage));self.assertFalse(applicability('personnel_sku',s,wage))
        self.assertTrue(applicability('training',s,flags_for(s,training=True)))
        self.assertIn('training_photos',{m.id for m in requirements(s)})

    def test_poster_list_is_required_for_production_and_not_for_posting(self):
        for flag,wanted in [(True,False),(False,True),(None,None)]:
            self.assertIs(applicability('material_production','poster_material',flags_for('poster_material',poster_posting=flag)),wanted)
        documents=[doc('c','本次海报张贴更换费用')]
        value=classification('poster_material',['c'],flags=flags_for('poster_material',poster_posting=True))
        value['material_matches']=[m for m in value['material_matches'] if m['scenario']=='poster_material']
        validate_classification(value,documents,'poster_material')
        documents=[doc('c','本次展示道具制作，未附制作清单')]
        with self.assertRaisesRegex(AuditError,'不能根据缺少制作清单'):
            validate_classification(value,documents,'poster_material')

    def test_new_quantity_directions_and_target_has_no_per_sku_reward_checks(self):
        self.assertEqual(comparison_operators('price_difference_support','settlement_pos_quantity'),{'quantity':'eq'})
        for s in ('giveaway_promotion','self_procured_gift_material'):
            self.assertEqual(comparison_operators(s,'gift_rule_quantity'),{'quantity':'le'})
        ids={r.id for r in rules('pos_target_incentive')}
        self.assertNotIn('reward_unit_price',ids);self.assertNotIn('settlement_pos_quantity',ids)
        self.assertTrue({'target_reward','pos_sales_amount'}<=ids)
        price=next(r for r in rules('price_difference_support') if r.id=='price_evidence')
        self.assertNotIn('quantity/le',price.text)
        self.assertNotIn('不恢复临促照片内容必审',json.dumps(catalogue(),ensure_ascii=False))

    def test_application_and_approval_cannot_be_claim_or_contract_self_certification(self):
        docs=[doc('s','实际核销100'),doc('c','合同预算100'),doc('a','申请总金额100'),doc('v','批复金额合计100')]
        materials=[mat('settlement','s'),mat('promotion_contract','c'),mat('activity_application','a','s','c'),mat('approved_claim','v','s')]
        self.assertEqual(application_sources(docs,materials),{'a'});self.assertEqual(approval_sources(docs,materials),{'v'})
        r=next(r for r in rules('poster_material') if r.id=='contract_budget_amount')
        check={'rule_id':r.id,'status':'pass','reason':'相同','source_ids':['c'],'comparisons':[]}
        self.assertEqual(constrain_application_check('poster_material',r,check,docs,materials)['status'],'unknown')

    def test_budget_and_approval_totals_are_independent_original_fields_not_summed_per_sku(self):
        docs=[doc('s','实际核销80'),doc('c','合同预算100'),doc('a','申请总金额100'),doc('v','批复金额合计80')]
        materials=[mat('settlement','s'),mat('promotion_contract','c'),mat('activity_application','a'),mat('approved_claim','v')]
        budget=comparison(operand('c','合同预算100',100),[operand('a','申请总金额100',100)])
        validate_workbook_comparison('poster_material','contract_budget_amount',budget,docs,materials)
        approval=comparison(operand('s','实际核销80',80),[operand('v','批复金额合计80',80)])
        validate_workbook_comparison('poster_material','claim_ceiling',approval,docs,materials)
        bad=deepcopy(approval);bad['right']=[operand('a','申请总金额100',100)]
        with self.assertRaises(AuditError):validate_workbook_comparison('poster_material','claim_ceiling',bad,docs,materials)
        bad=deepcopy(budget);bad['operation']='sum';bad['right']*=2
        with self.assertRaisesRegex(AuditError,'完整覆盖'):
            validate_workbook_comparison('poster_material','contract_budget_amount',bad,docs,materials)

    def test_staff_total_unit_price_has_the_right_application_and_wage_roles(self):
        docs=[doc('a','单价600'),doc('s','单日工资200','执行天数3')]
        materials=[mat('activity_application','a'),mat('settlement','s')]
        c=comparison(operand('a','单价600',600),[operand('s','单日工资200',200),operand('s','执行天数3',3)],'unit_price','product')
        validate_workbook_comparison('personnel_incentive','staff_total_unit_price',c,docs,materials)
        bad=deepcopy(c);bad['left']=operand('s','单日工资200',200)
        with self.assertRaises(AuditError):validate_workbook_comparison('personnel_incentive','staff_total_unit_price',bad,docs,materials)

    def test_calendar_month_end_and_leap_year_boundaries(self):
        self.assertEqual(add_months(date(2026,11,30),3),date(2027,2,28))
        self.assertEqual(add_months(date(2023,11,30),3),date(2024,2,29))
        self.assertEqual(add_months(date(2026,8,31),3),date(2026,11,30))

    def test_grouped_entry_reference_keeps_explicit_application_barcode(self):
        from tests.test_qince_context import fixture
        from audit_core.qince_context import entry_context_sources
        raw=fixture();detail=raw[VERIFICATION][0]['details'][0]
        detail.update(customer_id='C1',customer_name='测试甲店')
        raw[APPLICATIONS][0]['subList'][0].update(pdNames='本次商品',barcode='6970000000001')
        units,docs=entry_context_sources(raw,'HX_TEST','a001')
        self.assertEqual(len(units),1)
        self.assertIn('6970000000001',json.dumps(docs,ensure_ascii=False))
        self.assertEqual(application_sources(docs,[mat('entry_agreement',units[0]['unit_id'])],units),{units[0]['unit_id']})

    def test_payment_dates_are_source_bound_and_final_day_is_inclusive(self):
        docs=[doc('a','2026-11-01','2026-11-30'),doc('p','2027-02-28','2027-03-01','2026-10-31')]
        materials=[mat('activity_application','a'),mat('payment','p')]
        evidence={'start':ref('a','2026-11-01'),'end':ref('a','2026-11-30'),'payments':[ref('p','2027-02-28')],'complete':True}
        self.assertEqual(payment_date_check(evidence,docs,materials)['status'],'pass')
        for day in ('2027-03-01','2026-10-31'):
            bad=deepcopy(evidence);bad['payments']=[ref('p',day)]
            self.assertEqual(payment_date_check(bad,docs,materials)['status'],'fail')
        bad=deepcopy(evidence);bad['start']=ref('p','2026-11-01')
        with self.assertRaises(AuditError):payment_date_check(bad,docs,materials)
        bad=deepcopy(evidence);bad['complete']=False
        self.assertEqual(payment_date_check(bad,docs,materials)['status'],'unknown')

    def test_target_tier_uses_actual_sales_and_all_amounts_are_source_bound(self):
        docs=[doc('x','实际销售金额60000'),doc('s','奖励金额1800','奖励金额6000'),doc('c','销售达到50000奖励3%','销售达到100000奖励10%')]
        materials=[mat('pos_excel','x'),mat('settlement','s'),mat('promotion_contract','c')]
        evidence={'sales':operand('x','实际销售金额60000',60000),'reward':operand('s','奖励金额1800',1800),
          'tiers':[{'threshold':operand('c','销售达到50000奖励3%',50000),'rate':operand('c','销售达到50000奖励3%','3%'),'rate_unit':'percent'},
                   {'threshold':operand('c','销售达到100000奖励10%',100000),'rate':operand('c','销售达到100000奖励10%','10%'),'rate_unit':'percent'}],'complete':True}
        result=target_tier_comparison(evidence,docs,materials)
        self.assertEqual(result['status'],'pass');self.assertEqual(result['tier_calculation']['expected'],'1800.00')
        bad=deepcopy(evidence);bad['reward']=operand('s','奖励金额6000',6000)
        self.assertEqual(target_tier_comparison(bad,docs,materials)['status'],'fail')
        bad=deepcopy(evidence);bad['tiers'][0]['threshold']['unit_id']='s'
        with self.assertRaises(AuditError):target_tier_comparison(bad,docs,materials)
        bad=deepcopy(evidence);bad['complete']=False
        self.assertIsNone(target_tier_comparison(bad,docs,materials))

    def test_every_current_check_can_be_rendered_and_classified(self):
        from audit_core.pdf_workflow import CHECK_CATEGORIES
        for scenario in catalogue()['types']:
            for r in rules(scenario):
                with self.subTest(scenario=scenario,rule=r.id):
                    self.assertIn(r.id,CHECK_CATEGORIES)

    def test_seal_policy_distinguishes_contracts_from_pos_and_payment(self):
        from audit_core.pdf_policy import SEAL_CLARIFICATION
        self.assertIn('收款方公司财务专用章', SEAL_CLARIFICATION)
        self.assertIn('其他本类实际要求验章的资料须使用公章', SEAL_CLARIFICATION)
        for scenario in catalogue()['types']:
            for r in rules(scenario):
                if r.id in {'settlement_seal', 'settlement_template_seal', 'contract_signed'}:
                    self.assertIn('公章', r.text)
                    self.assertNotIn('公章或财务专用章', r.text)
                elif r.id in {'payment_statement_seal', 'pos_seal'}:
                    self.assertIn('公章', r.text)
                    self.assertNotIn('不区分', r.text)

    def test_qince_header_total_and_approval_are_emitted_once_not_per_sku(self):
        from tests.test_qince_context import fixture
        raw=fixture();app=raw[APPLICATIONS][0]
        app['applyAmount']='100';raw[VERIFICATION][0]['approvedAmount']='80'
        extra=deepcopy(app['subList'][0]);extra['subId']='SECOND';app['subList'].append(extra)
        detail=deepcopy(raw[VERIFICATION][0]['details'][0]);detail['id']='V2';detail['activity_cm_id']='SECOND'
        raw[VERIFICATION][0]['details'].append(detail)
        units,docs=context_sources(raw,'HX_TEST','a001')
        self.assertEqual(sum(u['source_file']=='勤策申请主档' for u in units),1)
        self.assertEqual(sum(u['source_file']=='勤策核销批复' for u in units),1)
        row=next(u['unit_id'] for u in units if u['source_file']=='勤策申请明细')
        docs.append(doc('c','预算999999'));materials=[mat('promotion_contract','c')]
        line=next(d for d in docs if d['unit_id']==row)
        quote=next(f for f in line['facts'] if '999999' in f)
        c=comparison(operand('c','预算999999',999999),[operand(row,quote,999999)])
        with self.assertRaises(AuditError):
            validate_workbook_comparison('poster_material','contract_budget_amount',c,docs,materials,units)

    def test_empty_auxiliary_approval_is_not_a_client_missing_material_issue(self):
        s='poster_material';flags=flags_for(s);docs=[doc('s','实际核销金额80'),doc('c','合同预算100')]
        materials=[mat('settlement','s'),mat('promotion_contract','c')]
        evidence=unknown_checks(s,flags)
        result=audit_decision(s,flags,evidence,docs,materials)
        check=next(c for c in result['checks'] if c['rule_id']=='claim_ceiling')
        self.assertEqual(check['status'],'not_applicable')
        self.assertEqual(result['policy_version'],POLICY_VERSION)

if __name__=='__main__':unittest.main()
