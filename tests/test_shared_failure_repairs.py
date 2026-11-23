"""General source and arithmetic regressions, without live services or models."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import jsonschema

from audit_core.common import AuditError, CheckEvidenceError
from audit_core.audit_rag import reference_comparison
from audit_core.comparison_policy import expected_operator
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.pdf_evidence import audit_decision, audit_schema, AUDIT_SCHEMA
from audit_core.pdf_policy import rules, MATERIALS
from tests.pdf_test_support import flags_for
from tests.test_audit_rag_contract import case_for


def operand(quote, number, uid='s'):
    return {'unit_id': uid, 'quote': quote, 'number': str(number)}


def comparison(left, right, operation, operator):
    return {'label': '原始结算字段复算', 'value_kind': 'amount', 'left': left,
            'right': right, 'operation': operation, 'operator': operator}


def settlement_fixture(amounts=(6, 8), total=14, claim=13, include_total=True):
    facts = [f'结算表第1行：销售数量2；奖励单价3；奖励金额{amounts[0]}。',
             f'结算表第2行：销售数量4；奖励单价2；奖励金额{amounts[1]}。',
             f'实际核销金额：{claim}元。']
    if include_total:
        facts.append(f'合计行：奖励金额{total}。')
    docs = [{'unit_id': 's', 'facts': facts, 'roles': ['结算单'], 'limitations': []}]
    materials = [{'id': 'settlement', 'state': 'present', 'source_ids': ['s'], 'reason': '原件'}]
    comparisons = [comparison(operand(f'奖励金额{a}', a),
                             [operand(f'销售数量{q}', q), operand(f'奖励单价{p}', p)], 'product', 'eq')
                   for a, q, p in [(amounts[0], 2, 3), (amounts[1], 4, 2)]]
    if include_total:
        comparisons += [comparison(operand(f'奖励金额{total}', total),
            [operand(f'奖励金额{a}', a) for a in amounts], 'sum', 'eq'),
            comparison(operand(f'实际核销金额：{claim}元', claim),
                       [operand(f'奖励金额{total}', total)], 'value', 'le')]
    else:
        comparisons.append(comparison(operand(f'实际核销金额：{claim}元', claim),
            [operand(f'奖励金额{a}', a) for a in amounts], 'sum', 'le'))
    return docs, materials, {'checks': [{'rule_id': 'settlement_calculation', 'status': 'pass',
        'reason': '按原值复算', 'source_ids': ['s'], 'comparisons': comparisons}]}


def decide(fixture, scenario='price_difference_support'):
    docs, materials, evidence = fixture
    result = audit_decision(scenario, flags_for(scenario), evidence, docs, materials,
                            units=[], selected_rule_ids=['settlement_calculation'])
    return result['checks'][0]


class SettlementPurposeTests(unittest.TestCase):
    def test_all_applicable_scenarios_share_row_total_and_claim_rules(self):
        scenarios = [s for s in MATERIALS if 'settlement_calculation' in {r.id for r in rules(s)}]
        self.assertEqual(set(scenarios), {'personnel_incentive', 'promotional_display',
            'self_procured_gift_material', 'price_difference_support', 'giveaway_promotion'})
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                fixture = settlement_fixture()
                before = deepcopy(fixture)
                result = decide(fixture, scenario)
                self.assertEqual(result['status'], 'pass')
                self.assertEqual([c['operator'] for c in result['comparisons']], ['eq','eq','eq','le'])
                self.assertEqual(fixture, before)

    def test_real_row_differences_in_both_directions_remain_business_failures(self):
        for amount in (5, 7):
            with self.subTest(amount=amount):
                self.assertEqual(decide(settlement_fixture((amount, 8), total=amount+8))['status'], 'fail')

    def test_wrong_written_total_is_not_hidden_by_a_lower_claim(self):
        self.assertEqual(decide(settlement_fixture(total=13, claim=12))['status'], 'fail')

    def test_claim_can_be_lower_or_equal_but_never_higher(self):
        for claim, status in [(13,'pass'), (14,'pass'), (15,'fail')]:
            with self.subTest(claim=claim):
                self.assertEqual(decide(settlement_fixture(claim=claim))['status'], status)

    def test_full_detail_sum_without_written_total_is_supported(self):
        self.assertEqual(decide(settlement_fixture(include_total=False))['status'], 'pass')

    def test_reversed_claim_relation_is_role_proven_not_unconditionally_ge(self):
        fixture = settlement_fixture()
        c = fixture[2]['checks'][0]['comparisons'][-1]
        c['left'], c['right'] = c['right'][0], [c['left']]
        c['operator'] = 'ge'
        before = deepcopy(c)
        self.assertEqual(decide(fixture)['status'], 'pass')
        self.assertEqual(c, before)
        fixture[2]['checks'][0]['comparisons'][0]['operator'] = 'ge'
        with self.assertRaises(CheckEvidenceError): decide(fixture)

    def test_unchecked_written_total_is_recomputed_by_code_with_original_values(self):
        fixture = settlement_fixture(total=13, claim=12)
        del fixture[2]['checks'][0]['comparisons'][-2]
        result = decide(fixture)
        self.assertEqual(result['status'], 'fail')
        self.assertEqual(len(result['comparisons']), 4)
        self.assertEqual(result['comparisons'][-1]['operator'], 'eq')

    def test_program_sum_preserves_thousands_format_and_original_precision(self):
        fixture = settlement_fixture()
        fixture[0][0]['facts'] = ['第1行：奖励金额1,000.00。', '第2行：奖励金额2,000.25。',
                                '合计行：奖励金额3,000.25。', '实际核销金额：3000元。']
        fixture[2]['checks'][0]['comparisons'] = [comparison(operand('实际核销金额：3000元',3000),
            [operand('奖励金额3,000.25','3,000.25')], 'value', 'le')]
        result = decide(fixture)
        self.assertEqual(result['status'],'pass')
        self.assertEqual(result['comparisons'][-1]['right'][0]['number'],'1,000.00')
        self.assertEqual(result['calculations'][-1]['right_value'],'3000.25')

    def test_direct_claim_against_product_keeps_the_approved_ceiling(self):
        fixture = settlement_fixture(amounts=(6,8), claim=5)
        c = fixture[2]['checks'][0]['comparisons'][0]
        c['left'] = operand('实际核销金额：5元',5)
        c['operator'] = 'le'
        fixture[2]['checks'][0]['comparisons'] = [c]
        self.assertEqual(decide(fixture)['status'], 'pass')

    def test_cross_row_product_partial_sum_and_wrong_material_stay_rejected(self):
        for mode in ('cross_row','partial','role','forged','non_contiguous'):
            fixture = settlement_fixture(include_total=False)
            cs = fixture[2]['checks'][0]['comparisons']
            if mode == 'cross_row': cs[0]['right'][0] = operand('销售数量4',4)
            elif mode == 'partial': cs[-1]['right'].pop()
            elif mode == 'role': fixture[1][0]['id'] = 'promotion_contract'
            elif mode == 'forged': cs[0]['right'][0]['number'] = '99'
            else: cs[0]['right'][0]['quote'] = '销售数量2奖励金额6'
            with self.subTest(mode=mode), self.assertRaises(AuditError): decide(fixture)

    def test_model_labels_do_not_change_the_operator_or_source_role(self):
        fixture = settlement_fixture()
        fixture[2]['checks'][0]['comparisons'][0].update(label='这是整单核销上限', operator='le')
        with self.assertRaises(CheckEvidenceError): decide(fixture)

    def test_equal_number_elsewhere_in_fact_does_not_turn_row_amount_into_claim(self):
        docs = [{'unit_id':'s','facts':['第1行：数量2；单价3；行金额6；实际核销金额：6元。']}]
        c = comparison(operand('行金额6',6), [operand('数量2',2),operand('单价3',3)], 'product','eq')
        self.assertEqual(expected_operator('price_difference_support','settlement_calculation',c,docs),'eq')
        c['left'] = operand('实际核销金额：6元',6);c['operator'] = 'le'
        self.assertEqual(expected_operator('price_difference_support','settlement_calculation',c,docs),'le')

    def test_generated_schemas_explain_three_purposes_without_a_new_required_field(self):
        for scenario in MATERIALS:
            schema = audit_schema(scenario)
            jsonschema.Draft202012Validator.check_schema(schema)
            conditions = schema['properties']['checks']['items'].get('allOf', [])
            found = [c for c in conditions if c.get('if',{}).get('properties',{}).get('rule_id',{}).get('const') == 'settlement_calculation']
            if 'settlement_calculation' in {r.id for r in rules(scenario)}:
                self.assertEqual(len(found), 1)
                text = json.dumps(found, ensure_ascii=False)
                for term in ('行金额','表内合计','实际核销金额'): self.assertIn(term, text)
                self.assertNotIn('purpose', found[0]['then']['properties']['comparisons']['items'].get('required', []))
            else: self.assertFalse(found)

    def test_operator_error_points_to_its_actual_field_without_retry_prompt(self):
        fixture = settlement_fixture()
        fixture[2]['checks'][0]['comparisons'][1]['operator'] = 'le'
        with TemporaryDirectory() as temporary:
            schema = Path(temporary)/'schema.json'
            schema.write_text(json.dumps(AUDIT_SCHEMA), encoding='utf-8')
            parser = EvidenceOutputParser(schema_path=schema, post_validate=lambda value:
                decide((fixture[0], fixture[1], value)))
            with self.assertRaises(AuditError): parser.parse(json.dumps(fixture[2]))
            self.assertEqual(parser.last_diagnostics['validation_paths'], ['checks/0/comparisons/1/operator'])
            self.assertEqual(parser.last_diagnostics['validation_code'], 'comparison_operator')
            self.assertEqual(parser.last_retry_context, '')

    def test_settlement_schema_rejects_quantity_while_other_quantity_checks_remain(self):
        from audit_core.audit_rag import part_schema
        scenario = 'personnel_incentive'
        schema = part_schema(audit_schema(scenario), ['settlement_calculation'], {})
        value = settlement_fixture()[2]
        value['reference_reads'] = []
        jsonschema.validate(value, schema)
        value['checks'][0]['comparisons'][0]['value_kind'] = 'quantity'
        with self.assertRaises(jsonschema.ValidationError): jsonschema.validate(value, schema)
        from audit_core.pdf_policy import comparison_operators
        self.assertEqual(comparison_operators(scenario,'staff_headcount'), {'quantity':'eq'})
        self.assertEqual(comparison_operators(scenario,'settlement_pos_quantity'), {'quantity':'le'})


class SealFactTests(unittest.TestCase):
    def fixture(self, facts, quote, value='合成商贸有限公司'):
        case, _ = case_for(1)
        case['documents'].append({'unit_id':'pos','roles':['盖章POS'],'facts':facts,'limitations':[]})
        case['materials'].append({'id':'pos','state':'present','source_ids':['pos'],'reason':'原件'})
        check={'rule_id':'pos_dealer','status':'unknown','reason':'待核对','source_ids':['pos'],'comparisons':[],
            'reference_evidence':{'written_name':None,'periods':[],
                'seal_name':{'unit_id':'pos','quote':quote,'value':value}}}
        return case,check

    def test_short_quotes_use_the_same_full_seal_fact_and_keep_a_real_mismatch(self):
        fact='页面上有两个红色圆形印章，外圈可辨认为“其他商贸有限公司”，五角星居中；部分笔画受影响。'
        for quote in ('外圈可辨认为“其他商贸有限公司”','其他商贸有限公司'):
            case,check=self.fixture([fact],quote,'其他商贸有限公司')
            result=reference_comparison(check,case['documents'],case['materials'],case['units'])
            self.assertEqual(result['status'],'fail')
            self.assertIn('不一致',result['reason'])

    def test_contract_settlement_and_business_stamps_all_count_as_seal_evidence(self):
        for fact in (
            '合同章内名称：合成商贸有限公司',
            '结算单章内名称：合成商贸有限公司',
            '业务专用章章面名称：合成商贸有限公司',
            '部门章名称：合成商贸有限公司',
            '方形印章内名称：合成商贸有限公司',
        ):
            case,check=self.fixture([fact],'合成商贸有限公司')
            with self.subTest(fact=fact):
                result=reference_comparison(check,case['documents'],case['materials'],case['units'])
                self.assertEqual(result['status'],'pass')

    def test_unrelated_seal_negation_filled_names_and_ambiguity_are_not_seal_evidence(self):
        for facts in [
            ['填写经销商名称：合成商贸有限公司','本页有红色印章'],
            ['填写经销商名称：合成商贸有限公司；另一位置有红色印章'],
            ['未见印章，填写经销商名称：合成商贸有限公司'],
            ['印章无法辨认，抬头为合成商贸有限公司'],
            ['红色公章内名称：合成商贸有限公司','填写经销商名称：合成商贸有限公司']]:
            case,check=self.fixture(facts,'合成商贸有限公司')
            with self.subTest(facts=facts),self.assertRaises(CheckEvidenceError):
                reference_comparison(check,case['documents'],case['materials'],case['units'])

    def test_wrong_unit_forged_name_and_non_contiguous_quote_are_not_accepted(self):
        for mode in ('unit','value','quote'):
            case,check=self.fixture(['红色公章内名称：合成商贸有限公司'],'合成商贸有限公司')
            item=check['reference_evidence']['seal_name']
            if mode=='unit':item['unit_id']='contract'
            elif mode=='value':item['value']='篡改公司'
            else:item['quote']='公章名称合成商贸有限公司'
            with self.subTest(mode=mode),self.assertRaises(CheckEvidenceError):
                reference_comparison(check,case['documents'],case['materials'],case['units'])

    def test_parser_has_exact_general_schema_position_and_no_new_retry_context(self):
        case,check=self.fixture(['填写经销商名称：合成商贸有限公司'],'合成商贸有限公司')
        with TemporaryDirectory() as temporary:
            schema=Path(temporary)/'schema.json';schema.write_text(json.dumps(AUDIT_SCHEMA),encoding='utf-8')
            parser=EvidenceOutputParser(schema_path=schema,post_validate=lambda v:
                reference_comparison(v['checks'][0],case['documents'],case['materials'],case['units']))
            with self.assertRaises(AuditError):parser.parse(json.dumps({'checks':[check]}))
            self.assertEqual(parser.last_diagnostics['validation_paths'],['checks/0/reference_evidence/seal_name/quote'])
            self.assertEqual(parser.last_diagnostics['validation_code'],'seal_source')
            self.assertEqual(parser.last_retry_context,'')


if __name__ == '__main__': unittest.main()
