"""Optional Qince references never become a client missing-material demand."""
from copy import deepcopy
import json
from pathlib import Path
import unittest

from audit_core.audit_rag import reference_fields
from audit_core.common import AuditError
from audit_core.fee_policy_checks import validate_workbook_comparison
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_policy import rules
from audit_core.qince_amount_basis import linked_application_basis, program_amount_check
from audit_core.qince_amount_basis import constrain_commission_plan_quantities
from audit_core.qince_context import APPLICATIONS, VERIFICATION, context_sources
from audit_core.scenario_registry import SCENARIO_SPECS
from audit_core.workbench_store import render_analysis_summary_markdown
from tests.pdf_test_support import flags_for
from tests.test_audit_rag_contract import case_for


def amount_case(total='1200.00'):
    case, raw = case_for(2)
    for row in raw[APPLICATIONS][0]['subList']:
        row['applyAmount'] = '600.00'
    raw[VERIFICATION][0].update(verify_status='3', reverse_status='0', verify_amount=total)
    native = [(u,d) for u,d in zip(case['units'],case['documents']) if u.get('source_kind') != 'qince']
    case['units'], case['documents'] = context_sources(raw,'HX_TEST','a001')
    for unit, document in native:
        if unit['unit_id']=='contract':document['facts'].append('活动预算金额：'+total+'元')
        if unit['unit_id']=='settlement':document['facts'].append('实际核销金额：1000元')
        case['units'].append(unit);case['documents'].append(document)
    return case


def decide(case, key, **extra):
    item = {'rule_id':key,'status':'unknown','reason':'本单未取得独立整单申请总金额或最终批复合计。',
            'source_ids':[],'comparisons':[],**extra}
    if case['scenario']=='promotional_display':item['source_assertions']=[]
    return audit_decision(case['scenario'],case['flags'],{'checks':[item]},case['documents'],
        case['materials'],units=case['units'],selected_rule_ids=[key])['checks'][0]


class OptionalAmountTests(unittest.TestCase):
    def test_complete_linked_rows_are_compared_using_all_original_operands(self):
        case=amount_case()
        value=decide(case,'contract_budget_amount')
        self.assertEqual(value['status'],'pass')
        self.assertEqual(value['calculations'][0]['right_value'],'1200.00')
        self.assertEqual(len(value['comparisons'][0]['right']),2)
        self.assertEqual(value['comparisons'][0]['operation'],'sum')
        indexed=reference_fields(case['documents'],case['units'])['application_total']
        self.assertEqual(indexed['state'],'available')
        self.assertIn('linked_application_rows',indexed['values'][0]['source_fields'])

    def test_real_budget_difference_is_reported_at_original_decimal_precision(self):
        case=amount_case('1200.0000000000001')
        value=decide(case,'contract_budget_amount')
        self.assertEqual(value['status'],'fail')
        self.assertFalse(value['calculations'][0]['matches'])

    def test_claim_amount_accepts_explicit_display_settlement_wording(self):
        case=amount_case()
        case['scenario']='promotional_display'
        case['flags']=flags_for('promotional_display')
        settlement=next(d for d in case['documents'] if d['unit_id']=='settlement')
        settlement['facts']=[fact for fact in settlement['facts'] if not fact.startswith('实际核销金额')]
        settlement['facts'].append('门店堆头实际执行2家，500元/店，堆头核销金额为1000元，核销明细如下。')
        value=decide(case,'application_claim_ceiling')
        self.assertEqual(value['status'],'pass')
        self.assertEqual(value['comparisons'][0]['left']['number'],'1000')
        self.assertIn('堆头核销金额为1000元',value['comparisons'][0]['left']['quote'])
        self.assertEqual(value['comparisons'][0]['operator'],'le')
        self.assertEqual(value['calculations'][0]['right_value'],'1200.00')

    def test_unlabelled_or_conflicting_claim_totals_are_not_guessed(self):
        case=amount_case()
        settlement=next(d for d in case['documents'] if d['unit_id']=='settlement')
        settlement['facts']=[fact for fact in settlement['facts'] if not fact.startswith('实际核销金额')]
        settlement['facts'].append('合计1000元；预算1200元；单店500元。')
        self.assertIsNone(program_amount_check({'rule_id':'application_claim_ceiling'},case['documents'],case['materials'],case['units']))
        settlement['facts'].extend(['堆头核销金额为1000元','本次核销金额为1100元'])
        self.assertIsNone(program_amount_check({'rule_id':'application_claim_ceiling'},case['documents'],case['materials'],case['units']))

    def test_saved_display_case_compares_both_amounts_without_rewriting_history(self):
        path=Path(__file__).resolve().parents[1]/'worktrees'/(
            'HX202610150048-商丘恒迪商贸有限公司8.22-9.5活动申请-9-20261109_1351_36')/'analysis/pdf-policy/evidence.json'
        if not path.exists():self.skipTest('Private saved display case is not part of source checkout.')
        original_bytes=path.read_bytes()
        packet=json.loads(original_bytes)['packets'][0]
        keys=['contract_budget_amount','application_claim_ceiling']
        evidence={'checks':[deepcopy(c) for c in packet['audit_evidence']['checks'] if c['rule_id'] in keys]}
        decision=audit_decision('promotional_display',packet['classification']['flags'],evidence,
            packet['documents'],packet['classification']['materials'],units=packet['units'],selected_rule_ids=keys)
        self.assertEqual({c['rule_id'] for c in decision['checks']},set(keys))
        for check in decision['checks']:
            self.assertEqual(check['status'],'pass')
            self.assertEqual(len(check['comparisons'][0]['right']),7)
            self.assertEqual(check['calculations'][0]['right_value'],'7000.00')
            self.assertTrue(check['calculations'][0]['matches'])
        self.assertEqual(path.read_bytes(),original_bytes)

    def test_partial_duplicate_or_cross_case_sum_cannot_pass_as_a_complete_scope(self):
        case=amount_case()
        good=program_amount_check({'rule_id':'contract_budget_amount'},case['documents'],case['materials'],case['units'])
        for change in ('partial','duplicate','other'):
            c=deepcopy(good['comparisons'][0])
            if change=='partial':c['right'].pop()
            elif change=='duplicate':c['right'].append(deepcopy(c['right'][0]))
            else:c['right'][0]['unit_id']='OTHER_CASE'
            with self.subTest(change=change),self.assertRaises(AuditError):
                validate_workbook_comparison(case['scenario'],'contract_budget_amount',c,
                    case['documents'],case['materials'],case['units'])
        case['documents'][0]['facts'].append('verify_code="OTHER_CASE"')
        self.assertIsNone(linked_application_basis(case['documents'],case['units']))

    def test_no_final_approval_is_a_precondition_and_existing_approval_remains_checked(self):
        case=amount_case()
        self.assertEqual(decide(case,'claim_ceiling')['status'],'not_applicable')
        # A real recorded independent approval still must match exactly.
        uid='approved'
        case['units'].append({'unit_id':uid,'source_kind':'qince','source_file':'勤策核销批复'})
        case['documents'].append({'unit_id':uid,'facts':['approvedAmount="1000"']})
        state=next(d for u,d in zip(case['units'],case['documents']) if u.get('source_file')=='勤策核销审批状态')
        state['facts']=[f.replace('verify_status="3"','verify_status="1"') for f in state['facts']]
        c={'label':'实际核销与已取得批复','value_kind':'amount',
           'left':{'unit_id':'settlement','quote':'实际核销金额：1000元','number':'1000'},
           'right':[{'unit_id':uid,'quote':'approvedAmount="1000"','number':'1000'}],
           'operation':'value','operator':'eq'}
        value=decide(case,'claim_ceiling',status='pass',source_ids=['settlement',uid],comparisons=[c])
        self.assertEqual(value['status'],'pass')

    def test_missing_auxiliary_references_are_not_client_findings_in_any_scenario(self):
        for spec in SCENARIO_SPECS:
            with self.subTest(scenario=spec.scenario):
                ids={r.id for r in rules(spec.scenario)}
                key='settlement_application_dates' if 'settlement_application_dates' in ids else 'entry_application_products'
                case={'scenario':spec.scenario,'flags':flags_for(spec.scenario),'documents':[],
                      'units':[],'materials':[]}
                self.assertEqual(decide(case,key)['status'],'not_applicable')

    def test_saved_original_case_has_no_missing_header_or_final_approval_customer_summary(self):
        path=Path(__file__).resolve().parents[1]/'worktrees'/(
            'HX202610170008-焦作鑫豫达人员激励活动费用申请-8-20261109_1321_20')/'analysis/pdf-policy/evidence.json'
        if not path.exists():self.skipTest('Private saved original case is not part of source checkout.')
        packet=json.loads(path.read_text(encoding='utf-8'))['packets'][0]
        evidence=deepcopy(packet['audit_evidence'])
        # Exercise current semantics in memory; the saved original answer and
        # its original policy/operator remain untouched on disk.
        for check in evidence['checks']:
            if check['rule_id']=='settlement_pos_quantity':
                for comparison in check.get('comparisons',[]):
                    if comparison.get('value_kind')=='quantity':comparison['operator']='le'
        evidence.pop('reference_reads',None)  # Rebuild the obsolete companion index from current original facts.
        # New rules have no historical model answer. Keep them unknown in this
        # in-memory compatibility fixture, without changing the saved evidence.
        from tests.pdf_test_support import unknown_checks
        known = {check['rule_id'] for check in evidence['checks']}
        evidence['checks'].extend(check for check in unknown_checks('personnel_incentive',
            packet['classification']['flags'])['checks'] if check['rule_id'] not in known)
        before=deepcopy(packet)
        decision=audit_decision('personnel_incentive',packet['classification']['flags'],evidence,
            packet['documents'],packet['classification']['materials'],units=packet['units'])
        checks={c['rule_id']:c for c in decision['checks']}
        self.assertEqual(checks['contract_budget_amount']['status'],'pass')
        self.assertEqual(checks['contract_budget_amount']['calculations'][0]['right_value'],'14000.00')
        self.assertEqual(len(checks['contract_budget_amount']['comparisons'][0]['right']),140)
        self.assertEqual(checks['claim_ceiling']['status'],'not_applicable')
        self.assertEqual(checks['application_products']['status'],'not_applicable')
        self.assertEqual(checks['application_quantity_price']['status'],'pass')
        self.assertEqual(len(checks['application_quantity_price']['comparisons']),1)
        self.assertEqual(checks['application_quantity_price']['comparisons'][0]['value_kind'],'unit_price')
        self.assertEqual(len(checks['application_quantity_price']['excluded_plan_quantity_comparisons']),2)
        self.assertTrue(checks['application_quantity_price']['calculations'][0]['matches'])
        self.assertEqual(checks['settlement_pos_quantity']['status'],'pass')
        self.assertEqual(len(checks['settlement_pos_quantity']['calculations']),10)
        self.assertEqual(checks['payment_activity_dates']['reason'], next(c['reason'] for c in evidence['checks']
            if c['rule_id']=='payment_activity_dates'))
        view={'sheets':[{'name':'人员激励','rows':[{'status':'issue','summary_items':[
            {'status':c['status'],'reason':c['reason'],'check_category':c['rule_id']}]} for c in decision['checks'] if c['rule_id'] in known]}]}
        summary=render_analysis_summary_markdown(view,{'status':'completed'})
        self.assertNotIn('未取得独立整单申请总金额',summary)
        self.assertNotIn('未取得系统核销最终批复',summary)
        self.assertNotIn('本检查未引用',summary)
        self.assertIn('没有显示付款年月日',summary)
        self.assertEqual(packet,before)  # No historical result is rewritten.


class CommissionPlanQuantityTests(unittest.TestCase):
    def case(self, price='2'):
        sources={'settlement':{'unit_id':'settlement','facts':['实际销量90，奖励单价'+price]},
                 'application':{'unit_id':'application','facts':['计划数量50，奖励单价2']}}
        def value(uid, number):
            return {'unit_id':uid,'quote':sources[uid]['facts'][0],'number':number}
        comparisons=[{'label':'数量','value_kind':'quantity','left':value('settlement','90'),
                      'right':[value('application','50')],'operation':'value','operator':'eq'},
                     {'label':'单价','value_kind':'unit_price','left':value('settlement',price),
                      'right':[value('application','2')],'operation':'value','operator':'eq'}]
        return sources,{'rule_id':'application_quantity_price','status':'fail','reason':'数量不同',
                        'source_ids':list(sources),'comparisons':comparisons}

    def test_plan_comparison_is_archived_and_price_operands_are_preserved(self):
        sources,check=self.case()
        original=deepcopy(check)
        result=constrain_commission_plan_quantities('personnel_incentive',{'sales_commission':True},check,sources)
        self.assertEqual(result['comparisons'],[check['comparisons'][1]])
        archived=result['excluded_plan_quantity_comparisons'][0]
        self.assertEqual(archived['comparison'],check['comparisons'][0])
        self.assertFalse(archived['calculation']['matches'])
        self.assertEqual(check,original)

    def test_bad_original_quote_cannot_be_hidden_as_an_excluded_plan_comparison(self):
        sources,check=self.case()
        check['comparisons'][0]['left']['quote']='实际销量999'
        with self.assertRaises(AuditError):
            constrain_commission_plan_quantities('personnel_incentive',{'sales_commission':True},check,sources)

    def test_price_mismatch_remains_an_exact_numeric_mismatch(self):
        from audit_core.pdf_evidence import calculate
        sources,check=self.case('2.0000000001')
        result=constrain_commission_plan_quantities('personnel_incentive',{'sales_commission':True},check,sources)
        calculation=calculate(result['comparisons'][0],sources)
        self.assertFalse(calculation['matches'])
        self.assertEqual(calculation['left_value'],'2.0000000001')

    def test_formal_decision_retains_real_price_difference_after_excluding_plan_quantity(self):
        for price,status in [('2','pass'),('2.0000000001','fail')]:
            sources,check=self.case(price)
            units=[{'unit_id':uid,'source_file':uid+'.jpg','source_kind':'submitted'} for uid in sources]
            sources['plan']={'unit_id':'plan','facts':['申请计划数量50']}
            units.append({'unit_id':'plan','source_kind':'qince','source_file':'勤策申请明细'})
            check['comparisons'][0]['right']=[{'unit_id':'plan','quote':'申请计划数量50','number':'50'}]
            check['source_ids'].append('plan')
            materials=[{'id':'settlement','state':'present','source_ids':['settlement'],'reason':'原始结算'},
                       {'id':'promotion_contract','state':'present','source_ids':['application'],'reason':'原始约定'}]
            with self.subTest(price=price):
                result=audit_decision('personnel_incentive',flags_for('personnel_incentive'),
                    {'checks':[check]},list(sources.values()),materials,units=units,
                    selected_rule_ids=['application_quantity_price'])['checks'][0]
                self.assertEqual(result['status'],status)
                self.assertEqual(len(result['calculations']),1)
                self.assertEqual(result['calculations'][0]['left_value'],price)
                self.assertEqual(len(result['excluded_plan_quantity_comparisons']),1)

    def test_contract_quantity_comparison_is_not_excluded_as_qince_plan_data(self):
        sources,check=self.case()
        units=[{'unit_id':uid,'source_kind':'submitted','source_file':uid+'.jpg'} for uid in sources]
        self.assertIs(constrain_commission_plan_quantities('personnel_incentive',{'sales_commission':True},
            check,sources,units=units),check)

    def test_quantity_only_or_uncertain_price_evidence_requires_stage_rework(self):
        sources,check=self.case()
        for kind in ('quantity_only','unknown'):
            value=deepcopy(check)
            if kind=='quantity_only':value['comparisons'].pop()
            else:value['status']='unknown'
            with self.subTest(kind=kind),self.assertRaises(AuditError):
                constrain_commission_plan_quantities('personnel_incentive',{'sales_commission':True},value,sources)

    def test_retained_contract_quantity_cannot_replace_missing_reward_price(self):
        sources,check=self.case()
        sources['contract']={'unit_id':'contract','facts':['约定数量2']}
        check['comparisons'][1].update(value_kind='quantity',
            right=[{'unit_id':'contract','quote':'约定数量2','number':'2'}])
        units=[{'unit_id':'application','source_kind':'qince','source_file':'勤策申请明细'},
               {'unit_id':'contract','source_kind':'submitted','source_file':'contract.jpg'}]
        with self.assertRaises(AuditError):
            constrain_commission_plan_quantities('personnel_incentive',{'sales_commission':True},
                check,sources,units=units)

    def test_other_scenarios_and_staff_checks_remain_unchanged(self):
        sources,check=self.case()
        for scenario,flags,key in [('price_difference_support',{'sales_commission':True},check['rule_id']),
                                   ('personnel_incentive',{'sales_commission':False},check['rule_id']),
                                   ('personnel_incentive',{'sales_commission':True},'staff_headcount'),
                                   ('personnel_incentive',{'sales_commission':True},'settlement_pos_quantity')]:
            value={**check,'rule_id':key}
            self.assertIs(constrain_commission_plan_quantities(scenario,flags,value,sources),value)


if __name__=='__main__':unittest.main()
