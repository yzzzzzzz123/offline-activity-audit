"""Conditional reference alignment shared by all audit scenarios."""
from copy import deepcopy
import unittest

from audit_core.common import AuditError
from audit_core.fee_policy_checks import validate_workbook_comparison
from audit_core.reference_matching import name_matches, numeric_equal, unique_name_match
from tests.test_qince_optional_amounts import amount_case


class ReferenceMatchingTests(unittest.TestCase):
    def test_numbers_are_exact_and_zero_is_valid(self):
        for a, b, result in [('1.00','1',True),('0','0.000',True),
                             ('2','2.0000000000001',False),('',0,False),
                             (None,0,False),(False,0,False),('NaN','NaN',False)]:
            with self.subTest(a=a,b=b):self.assertEqual(numeric_equal(a,b),result)

    def test_text_variants_and_minor_writing_differences_match(self):
        for a,b in [('美道家（时代广场店）线上','美道家(时代广场店)'),
                    ('商丘恒迪商贸有限公司线上','商丘恒迪商贸有限公司'),
                    ('深圳市新世纪购物广场','深圳新世纪购物广场')]:
            with self.subTest(a=a,b=b):self.assertTrue(name_matches(a,b))

    def test_different_branches_and_ambiguous_candidates_are_not_merged(self):
        for a,b in [('好多多购物广场青闸线店','好多多购物广场玉田店'),
                    ('新世纪购物广场1店','新世纪购物广场2店'),
                    ('新世纪购物广场一店','新世纪购物广场二店')]:
            with self.subTest(a=a,b=b):self.assertFalse(name_matches(a,b))
        self.assertIsNone(unique_name_match('商丘恒迪商贸有限公司',
            ['商丘恒迪商贸有限公司线上','商丘恒迪商贸有限公司线下']))

    def test_nonempty_numeric_reference_cannot_be_replaced_by_contract(self):
        case=amount_case()
        uid='qince_price'
        case['units'].append({'unit_id':uid,'source_kind':'qince','source_file':'勤策申请明细'})
        case['documents'].append({'unit_id':uid,'facts':['unitPrice="2"']})
        case['documents'].append({'unit_id':'claim_price','facts':['奖励单价1.9']})
        case['documents'].append({'unit_id':'contract_price','facts':['合同奖励单价1.9']})
        case['materials'] += [{'id':'settlement','state':'present','source_ids':['claim_price']},
                              {'id':'promotion_contract','state':'present','source_ids':['contract_price']}]
        c={'label':'奖励单价','value_kind':'unit_price','operation':'value','operator':'eq',
            'left':{'unit_id':'claim_price','quote':'奖励单价1.9','number':'1.9'},
            'right':[{'unit_id':'contract_price','quote':'合同奖励单价1.9','number':'1.9'}]}
        with self.assertRaises(AuditError):
            validate_workbook_comparison('personnel_incentive','reward_unit_price',c,
                case['documents'],case['materials'],case['units'])
        c['right']=[{'unit_id':uid,'quote':'unitPrice="2"','number':'2'}]
        validate_workbook_comparison('personnel_incentive','reward_unit_price',c,
            case['documents'],case['materials'],case['units'])

    def test_empty_numeric_reference_keeps_original_contract_check(self):
        from tests.test_unit_price_and_staff_policy import numeric_case, decide
        case,evidence,_=numeric_case('reward_unit_price','1.9',['2'],
            operation='value',value_kind='unit_price',operator='eq')
        case['units']=[{'unit_id':'q','source_kind':'qince','source_file':'勤策申请明细'}]
        case['documents'].append({'unit_id':'q','facts':['unitPrice=""']})
        # Existing helper does not forward units; explicitly pass them here.
        from audit_core.pdf_evidence import audit_decision
        evidence['checks']=[c for c in evidence['checks'] if c['rule_id']=='reward_unit_price']
        result=audit_decision(case['scenario'],case['flags'],evidence,case['documents'],
            case['materials'],units=case['units'],selected_rule_ids=['reward_unit_price'])
        self.assertEqual(result['checks'][0]['status'],'fail')
        self.assertFalse(result['checks'][0]['calculations'][0]['matches'])


if __name__=='__main__':unittest.main()
