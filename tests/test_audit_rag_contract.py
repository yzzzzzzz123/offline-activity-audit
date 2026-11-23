"""Network-free source retrieval/schema/merge tests, not original-case AI reruns."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import jsonschema

from audit_core.audit_rag import (batches, merge, part_schema, reference_fields, reference_fold,
    reference_view, validate_reads,
    needed_fields, REFERENCE_INPUT_SCHEMA, reference_comparison, ROLE_GROUPS, pos_chunks,
    pos_quantity_index, expand_quantity_refs, quantity_wire_schema, validate_visible_inventory,
    VISIBLE_FIELD_INVENTORY_SCHEMA, program_pos_checks, repair_visible_inventory)
from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_schema
from audit_core.pdf_materials import _prompt_documents, PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.pdf_policy import rules, requirements
from audit_core.qince_context import context_sources, APPLICATIONS, VERIFICATION
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_qince_context import fixture


def case_for(count=140):
    raw = fixture()
    application = raw[APPLICATIONS][0]
    row = application['subList'][0]
    row.update(dealerName='合成商贸有限公司', dealerCode='D_TEST', startDate='2026-09-20', endDate='2026-10-15',
        extColumns=[{'ext_key': '数量', 'ext_value': '50'}, {'ext_key': '单价', 'ext_value': '2'},
                    {'ext_key': '活动SKU', 'ext_value_code': 'SKU_TEST', 'ext_value_name': '测试商品'}])
    application['subList'] = [{**deepcopy(row), 'subId': str(n), 'cusName': f'合成门店{n}'} for n in range(count)]
    raw[VERIFICATION][0]['details'] = [{'id': str(n), 'activity_code': 'SQ_TEST', 'activity_cm_id': str(n)} for n in range(count)]
    units, docs = context_sources(raw, 'HX_TEST', 'a001')
    for uid, text in [('contract', '活动执行周期：2026年9月20号至2026年10月15号'),
                      ('settlement', '活动执行周期：2026-09-20至2026-10-15')]:
        facts = [text, '填写经销商名称：合成商贸有限公司', '红色公章内名称：合成商贸有限公司']
        units.append({'unit_id': uid, 'file_id': uid, 'source_file': uid+'.jpg', 'locator': uid,
                      'native_facts': facts, 'limitations': [], 'image': None})
        docs.append({'unit_id': uid, 'roles': [], 'facts': facts, 'limitations': []})
    materials = [{'id': r.id, 'state': 'not_applicable', 'reason': '隔离测试', 'source_ids': []}
                 for r in requirements('personnel_incentive')]
    for m in materials:
        if m['id'] in {'promotion_contract', 'settlement'}:
            m.update(state='present', source_ids=['contract' if m['id']=='promotion_contract' else 'settlement'])
    return {'scenario': 'personnel_incentive', 'archive_id': 'a001', 'units': units,
            'documents': docs, 'materials': materials, 'flags': flags_for('personnel_incentive', uses_pos=False)}, raw


def reads_for(fields):
    return [{'field': key, 'state': info['state'], 'source_ids': list(dict.fromkeys(
        row['source_ids'][0] for row in info['values']))} for key, info in fields.items()]


class RetrievalTests(unittest.TestCase):
    def test_long_case_is_split_by_purpose_and_every_rule_owned_once(self):
        case, _ = case_for()
        parts = batches(case)
        self.assertGreater(len(parts), 3)
        ids = [key for part in parts for key in part['rule_ids']]
        self.assertCountEqual(ids, [r.id for r in rules(case['scenario'])])
        self.assertEqual(len(ids), len(set(ids)))
        dates = next(p for p in parts if p['name']=='dates')
        self.assertIn('contract', [d['unit_id'] for d in dates['documents']])
        self.assertTrue(any('startDate=' in f for d in dates['documents'] for f in d['facts']))
        self.assertFalse(any('dealerName=' in f for d in dates['documents'] for f in d['facts']))
        identity = next(p for p in parts if p['name']=='documents' and not p.get('program_not_applicable'))
        self.assertTrue(any('dealerName=' in f for d in identity['documents'] for f in d['facts']))
        original = {d['unit_id']: d['facts'] for d in case['documents']}
        for part in parts:
            for document in part['documents']:
                for fact in document['facts']:
                    self.assertTrue(any(fact in full for full in original[document['unit_id']]))

    def test_all_eight_rule_sets_have_explicit_retrieval_dependencies(self):
        from audit_core.scenario_registry import SKILL_BY_SCENARIO
        registered = set().union(*(members for _, members in ROLE_GROUPS.values()))
        for scenario in SKILL_BY_SCENARIO:
            self.assertLessEqual({r.id for r in rules(scenario)}, registered)

    def test_other_case_never_enters_source_index_or_retrieval(self):
        case, raw = case_for(1)
        raw[APPLICATIONS][0]['subList'].append({'subId': 'OTHER', 'dealerName': 'OTHER_PRIVATE', 'startDate': '2099-01-01'})
        raw[VERIFICATION].append({'verify_code':'OTHER', 'details':[{'activity_code':'SQ_TEST','activity_cm_id':'OTHER'}]})
        units, docs = context_sources(raw, 'HX_TEST', 'a001')
        self.assertNotIn('OTHER_PRIVATE', json.dumps(reference_fields(docs, units), ensure_ascii=False))

    def test_repeated_system_facts_pack_losslessly_including_empty_and_null(self):
        case, _ = case_for()
        packed = json.loads(_prompt_documents(case['documents']))
        self.assertIn('fragments', packed)
        decoded = []
        for d in packed['documents']:
            facts = [packed['fragments'][f] if isinstance(f, int) else f for f in d['facts']]
            decoded.append({**packed['document_metadata'][d['metadata']], 'unit_id': d['unit_id'], 'facts':facts})
        self.assertEqual(decoded, case['documents'])
        self.assertLess(len(json.dumps(packed, ensure_ascii=False)), len(json.dumps(case['documents'], ensure_ascii=False)))


class FieldContractTests(unittest.TestCase):
    def test_visual_inventory_is_source_bound_and_its_fields_survive_in_facts(self):
        item = {'unit_id':'u', **{k:{'state':'absent','fact_indexes':[]} for k in
            ('written_dealer_name','seal_name','activity_period')}}
        item['activity_period']={'state':'present','fact_indexes':[0]}
        value={'documents':[{'unit_id':'u','facts':[]}], 'field_inventory':[item]}
        jsonschema.validate([item],VISIBLE_FIELD_INVENTORY_SCHEMA)
        with self.assertRaises(AuditError):validate_visible_inventory(value)
        value['documents'][0]['facts']=['活动时间2026年9月20日至2026年10月15日']
        validate_visible_inventory(value)
        value['field_inventory'].append(deepcopy(item))
        with self.assertRaises(AuditError):validate_visible_inventory(value)

    def test_out_of_range_visible_inventory_reports_the_field_and_fact_count(self):
        item = {'unit_id':'u', **{k:{'state':'absent','fact_indexes':[]} for k in
            ('written_dealer_name','seal_name','activity_period')}}
        item['seal_name']={'state':'unclear','fact_indexes':[7]}
        value={'documents':[{'unit_id':'u','facts':['本图可见出库明细']}], 'field_inventory':[item]}
        with self.assertRaises(AuditError) as raised:
            validate_visible_inventory(value)
        message = str(raised.exception)
        self.assertIn('读图关键字段未完整进入本张原始事实', message)
        self.assertIn('seal_name', message)
        self.assertIn('"facts_count":1', message)

    def test_out_of_range_inventory_indexes_are_repaired_from_source_facts(self):
        item = {'unit_id': 'u', **{k: {'state': 'absent', 'fact_indexes': []} for k in
            ('written_dealer_name', 'seal_name', 'activity_period')}}
        item['seal_name'] = {'state': 'present', 'fact_indexes': [3, 4]}
        value = {'documents': [{'unit_id': 'u', 'facts': [
            '表格标题：销售汇总表2026.9.30-2026.10.07',
            '第1行：参半牙膏，盒，1*30，30，19.80，594.00',
            '表格左下方有一枚红色圆形印章，可见文字为“菏泽市恒拓商贸有限公司”',
        ]}], 'field_inventory': [item]}
        notes = repair_visible_inventory(value)
        self.assertEqual(value['field_inventory'][0]['seal_name']['fact_indexes'], [2])
        self.assertEqual(notes[0]['field'], 'seal_name')
        self.assertEqual(notes[0]['invalid_indexes'], [3, 4])
        validate_visible_inventory(value)

    def test_inventory_repair_recognizes_contract_and_settlement_seal_names(self):
        for seal in ('合同章内名称', '结算单章面名称'):
            item = {'unit_id': 'u', **{k: {'state': 'absent', 'fact_indexes': []} for k in
                ('written_dealer_name', 'seal_name', 'activity_period')}}
            item['seal_name'] = {'state': 'present', 'fact_indexes': [9]}
            value = {'documents': [{'unit_id': 'u', 'facts': [
                '填写经销商名称：合成商贸有限公司',
                f'{seal}：合成商贸有限公司',
            ]}], 'field_inventory': [item]}
            with self.subTest(seal=seal):
                notes = repair_visible_inventory(value)
                self.assertEqual(value['field_inventory'][0]['seal_name']['fact_indexes'], [1])
                self.assertEqual(notes[0]['repaired_indexes'], [1])
                validate_visible_inventory(value)

    def test_unlocatable_inventory_indexes_degrade_to_unclear_instead_of_failing(self):
        item = {'unit_id': 'u', **{k: {'state': 'absent', 'fact_indexes': []} for k in
            ('written_dealer_name', 'seal_name', 'activity_period')}}
        item['seal_name'] = {'state': 'present', 'fact_indexes': [7]}
        value = {'documents': [{'unit_id': 'u', 'facts': ['本图可见出库明细']}],
                 'field_inventory': [item]}
        notes = repair_visible_inventory(value)
        self.assertEqual(value['field_inventory'][0]['seal_name'],
                         {'state': 'unclear', 'fact_indexes': []})
        self.assertEqual(notes[0]['unresolved_indexes'], [7])
        validate_visible_inventory(value)

    def test_generation_guides_explanations_without_new_length_failure(self):
        schema=part_schema(audit_schema('personnel_incentive'),['contract_dealer'],{})
        c={'rule_id':'contract_dealer','status':'pass','reason':'字'*121,'source_ids':['u'],'comparisons':[],
           'reference_evidence':{'written_name':None,'seal_name':None,'periods':[]}}
        value={'checks':[c], 'reference_reads':[]}
        jsonschema.validate(value,schema)
        c.update(status='unknown',reason='字'*480)
        jsonschema.validate(value,schema)
        c['reason']+='字'
        jsonschema.validate(value,schema)
        self.assertNotIn('maxLength',schema['properties']['checks']['items']['properties']['reason'])
        self.assertIn('不是审核失败',schema['properties']['checks']['items']['properties']['reason']['description'])

    def test_program_merge_preserves_all_chunk_problems_beyond_model_prose_limit(self):
        from tests.pdf_test_support import unknown_checks
        value=unknown_checks('personnel_incentive', flags_for('personnel_incentive'))
        value['checks'][0]['reason']='真实问题'*200
        jsonschema.validate(value,audit_schema('personnel_incentive',model_output=False))
        jsonschema.validate(value,audit_schema('personnel_incentive'))

    def test_formula_representation_is_grouped_without_hiding_real_shortfall_or_changing_status(self):
        from audit_core.pos_arithmetic import prepare_pos_arithmetic, is_binary_formula_cache_difference
        from audit_core.pdf_evidence import calculate, document_map
        from audit_core.customer_language import numeric_problem
        facts=['S!R1: C1=数量 | C2=单价 | C3=金额']+[
            f'S!R{i}: C1=68 | C2=29.9 | C3=公式=A{i}*B{i}；缓存值=2033.1999999999998'
            for i in range(2,22)]
        # This literal amount is genuinely below the exact product; no formula
        # representation proof exists, so it must remain a separate problem.
        facts.append('S!R22: C1=68 | C2=29.9 | C3=2033.19')
        docs=[{'unit_id':'u','roles':[],'facts':facts,'limitations':[]}]
        units=[{'unit_id':'u','file_id':'f','source_file':'test.xlsx','native_facts':facts}]
        materials=[{'id':'pos_excel','state':'present','source_ids':['u']}]
        plan=prepare_pos_arithmetic(docs,materials,units)
        calculations=[calculate(c,document_map(docs)) for c in plan['comparisons']]
        snapshot=deepcopy(calculations)
        self.assertEqual(sum(is_binary_formula_cache_difference(c,units) for c in calculations),20)
        text=numeric_problem({'rule_id':'pos_arithmetic','calculations':calculations},units)
        self.assertIn('20处原生乘法公式',text)
        self.assertIn('少了0.01',text)
        self.assertEqual(len(text.splitlines()),2)
        self.assertEqual(calculations,snapshot)
        self.assertTrue(all(not c['matches'] for c in calculations))

    def test_activity_narrative_with_prices_is_not_a_second_missing_date_field(self):
        from audit_core.submitted_source_checks import application_period_issues
        case,_=case_for(1)
        case['documents'][-2]['facts'].append('正文约定在此活动期间，每个商品2元/盒，执行10个商品。')
        self.assertEqual(application_period_issues(case['scenario'],case['documents'],case['materials'],case['units']),[])
        case['documents'][-2]['facts'].append('另一活动执行周期：未填写')
        self.assertEqual(application_period_issues(case['scenario'],case['documents'],case['materials'],case['units'])[0]['status'],'unknown')
    def test_pos_chunks_cover_all_facts_repeat_only_real_headers_and_detect_missing_tail(self):
        facts = ['S!R1: C1=条码 | C2=商品名称 | C3=数量 | C4=单价 | C5=金额'] + [
            f'S!R{i}: C1=6900000000000 | C2=测试商品 | C3=1 | C4=2 | C5=2' for i in range(2,1002)]
        docs = [{'unit_id':'excel','facts':facts,'roles':[],'limitations':[]}]
        units = [{'unit_id':'excel','source_file':'test.xlsx'}]
        chunks = pos_chunks(docs, units)
        self.assertGreater(len(chunks),1)
        covered = [i for c in chunks for uid,start,end in c['pos_ranges'] for i in range(start,end)]
        self.assertEqual(covered,list(range(len(facts))))
        self.assertTrue(all(facts[0] in c['documents'][0]['facts'] for c in chunks))
        parts = [{'_pos_ranges':c['pos_ranges'], 'checks':[{'rule_id':'pos_fields','status':'pass',
            'source_ids':['excel'],'reason':'本块字段完整','comparisons':[]}]} for c in chunks]
        self.assertEqual(merge(parts,['pos_fields'],pos_fact_counts={'excel':len(facts)})['checks'][0]['status'],'pass')
        with self.assertRaises(AuditError):merge(parts[:-1],['pos_fields'],pos_fact_counts={'excel':len(facts)})
        with self.assertRaises(AuditError):merge(parts+parts,['pos_fields'],pos_fact_counts={'excel':len(facts)})

    def test_program_quantity_index_expands_to_all_original_rows_not_fake_subtotal(self):
        from audit_core.pos_arithmetic import prepare_pos_arithmetic
        from audit_core.pdf_evidence import calculate, document_map
        facts = ['S!R1: C1=条码 | C2=商品名称 | C3=数量 | C4=单价 | C5=金额',
                 'S!R2: C1=6900000000000 | C2=测试商品 | C3=2 | C4=2 | C5=4',
                 'S!R3: C1=6900000000000 | C2=测试商品 | C3=3 | C4=2 | C5=6',
                 'S!R4: C1=合计 | C3=5 | C5=10']
        docs=[{'unit_id':'excel','facts':facts,'roles':[],'limitations':[]}]
        units=[{'unit_id':'excel','file_id':'f','source_file':'test.xlsx','native_facts':facts}]
        materials=[{'id':'pos_excel','state':'present','source_ids':['excel']}]
        plan=prepare_pos_arithmetic(docs,materials,units)
        identities={'limitations':[],'rows':[{'unit_id':'excel','source':{'fact_indexes':[1,2],
            'product_name':{'value':'测试商品'},'barcode_69':{'value':'6900000000000'}}}]}
        groups=pos_quantity_index(docs,plan,identities)
        self.assertEqual(groups[0]['quantity'],'5')
        self.assertEqual(groups[0]['row_count'],2)
        value={'checks':[{'rule_id':'settlement_pos_quantity','status':'pass','reason':'销量对应',
            'source_ids':['settlement'],'comparisons':[{'label':'测试商品销量','value_kind':'quantity',
                'left':{'unit_id':'settlement','quote':'申报销量5','number':'5'},'right':[],
                'right_group_ref':groups[0]['id'],'operation':'sum','operator':'eq'}]}]}
        schema=part_schema(audit_schema('personnel_incentive'),['settlement_pos_quantity'],{})
        quantity_wire_schema(schema,groups)
        value['reference_reads']=[]
        value['quantity_group_reads']=[g['id'] for g in groups]
        jsonschema.validate(value,schema)
        expanded=expand_quantity_refs(value,groups)
        comparison=expanded['checks'][0]['comparisons'][0]
        self.assertEqual(comparison['right'],groups[0]['terms'])
        self.assertEqual(value['checks'][0]['comparisons'][0]['right'],[])
        self.assertTrue(calculate(comparison,document_map(docs+[{'unit_id':'settlement','facts':['申报销量5']}]))['matches'])
        self.assertIsNone(pos_quantity_index(docs,plan,{'limitations':[],'rows':[]}))

    def test_fields_are_typed_and_complete_linked_total_is_explicitly_derived(self):
        case, _ = case_for(1)
        indexed = reference_fields(case['documents'], case['units'])
        jsonschema.validate(indexed, REFERENCE_INPUT_SCHEMA)
        self.assertEqual(indexed['advance_dealer']['state'], 'available')
        self.assertEqual(indexed['activity_period']['state'], 'available')
        self.assertEqual(indexed['application_total']['state'], 'available')
        self.assertIn('linked_application_rows', indexed['application_total']['values'][0]['source_fields'])
        self.assertEqual(indexed['approved_total']['state'], 'missing')
        self.assertEqual(indexed['application_quantity']['state'], 'available')
        self.assertEqual(indexed['application_unit_price']['state'], 'available')
        self.assertTrue(indexed['application_products']['values'])

    def test_missing_empty_incomplete_invalid_and_multiple_are_distinct(self):
        for start, end, expected in [(None, None, 'empty'), ('2026-09-20', None, 'incomplete'),
                                     ('not-a-date', '2026-10-15', 'invalid')]:
            case, raw = case_for(1)
            row = raw[APPLICATIONS][0]['subList'][0]
            row.update(startDate=start, endDate=end)
            units, docs = context_sources(raw, 'HX_TEST', 'a001')
            self.assertEqual(reference_fields(docs, units)['activity_period']['state'], expected)
        case, raw = case_for(2)
        raw[APPLICATIONS][0]['subList'][1]['dealerName'] = '另一合成经销商'
        units, docs = context_sources(raw, 'HX_TEST', 'a001')
        info = reference_fields(docs, units)['advance_dealer']
        self.assertEqual(info['state'], 'multiple')
        with self.assertRaises(AuditError):
            validate_reads({'reference_reads':[{'field':'advance_dealer','state':'multiple',
                'source_ids':info['values'][0]['source_ids']}]}, {'advance_dealer':info})

    def test_available_fields_cannot_be_reported_missing_or_reference_verification_row(self):
        case, _ = case_for(1)
        info = reference_fields(case['documents'], case['units'])
        fields = {'advance_dealer':info['advance_dealer']}
        for reads in ([], [{'field':'advance_dealer','state':'missing','source_ids':[]}],
                      [{'field':'advance_dealer','state':'available','source_ids':['a001-qince-0001']}]):
            with self.assertRaises(AuditError):validate_reads({'reference_reads':reads}, fields)

    def test_schema_accepts_legacy_without_auxiliary_but_validates_typed_evidence(self):
        case, _ = case_for(1)
        ids = ['contract_dealer']
        fields = {'advance_dealer': reference_fields(case['documents'], case['units'])['advance_dealer']}
        schema = part_schema(audit_schema(case['scenario']), ids, fields)
        value = {'checks':[{'rule_id':'contract_dealer','status':'unknown','reason':'未取得','source_ids':[], 'comparisons':[]}],
                 'reference_reads':reads_for(fields)}
        jsonschema.validate(value, schema)
        legacy=deepcopy(value);legacy.pop('reference_reads')
        jsonschema.validate(legacy,schema)
        validate_reads(legacy,fields,require_all=False)
        value['checks'][0]['reference_evidence'] = {'written_name':None, 'seal_name':None, 'periods':[]}
        jsonschema.validate(value, schema)
        value['checks'][0]['reference_evidence']['seal_name']={'bad':'not a quoted source'}
        with self.assertRaises(jsonschema.ValidationError):jsonschema.validate(value,schema)

    def test_grouped_rows_share_one_period_without_hiding_their_source_count(self):
        # A grouped entry record links the same application period once per row.
        # The model index states the value once and how many real bindings say
        # it; every row and unit_id stays in the case and in the full index.
        rows = [{'verification_row_id': str(n), 'activity_code': 'SQ_TEST', 'activity_cm_id': str(n),
                 'startDate': '2026-05-23', 'endDate': '2026-12-31',
                 '活动SKU': [{'ext_key': '活动SKU', 'ext_value_code': f'SKU-{n}'}]} for n in range(40)]
        fields = {'activity_period': {'state': 'available', 'values': [
            {'value': {'start': '2026-05-23', 'end': '2026-12-31'},
             'source_fields': {'linked_application_rows': [row]},
             'source_ids': [f'a001-qince-entry-{n // 2:04d}']} for n, row in enumerate(rows)]}}
        view = reference_view(fields)
        self.assertEqual(len(fields['activity_period']['values']), 40)
        self.assertEqual(len(view['activity_period']['values']), 1)
        folded = view['activity_period']['values'][0]
        self.assertEqual(folded['value'], {'start': '2026-05-23', 'end': '2026-12-31'})
        self.assertEqual(folded['equivalent_source_count'], 20)
        self.assertEqual(folded['source_ids'], ['a001-qince-entry-0000'])
        self.assertNotIn('活动SKU', json.dumps(folded, ensure_ascii=False))
        # Request validation folds identically: one real id per distinct value
        # is enough, and an id from outside this batch is still refused.
        folded_index = reference_fold(fields)
        validate_reads({'reference_reads': [{'field': 'activity_period', 'state': 'available',
            'source_ids': ['a001-qince-entry-0000']}]}, folded_index)
        with self.assertRaises(AuditError):
            validate_reads({'reference_reads': [{'field': 'activity_period', 'state': 'available',
                'source_ids': ['a001-qince-entry-9999']}]}, folded_index)

    def test_program_identity_preserves_real_failures_and_native_calculation_is_not_guessed_pass(self):
        identities={'applicable':True,'rows':[{'unit_id':'u','status':'fail','reason_code':'catalog_product_missing',
            'reason':'库内无参考商品','submitted':{'product_name':'测试牙膏','barcode_69':'6900000000000'}}],
            'limitations':[]}
        actual=program_pos_checks({'complete':True,'source_ids':['u']},identities)
        self.assertEqual(actual['pos_product_identity']['status'],'fail')
        self.assertIn('测试牙膏',actual['pos_product_identity']['reason'])
        self.assertIn('库内无参考商品',actual['pos_product_identity']['reason'])
        self.assertEqual(actual['pos_arithmetic']['status'],'unknown')
        self.assertEqual(actual['pos_arithmetic']['comparisons'],[])

    def test_correct_period_beats_false_missing_claim_but_real_mismatch_is_preserved(self):
        case, _ = case_for(1)
        check = {'rule_id':'contract_activity_dates','status':'unknown','reason':'未取得系统申请', 'source_ids':[], 'comparisons':[],
                 'reference_evidence':{'written_name':None,'seal_name':None,
                     'periods':[{'unit_id':'contract','quote':case['documents'][-2]['facts'][0]}]}}
        actual = reference_comparison(check, case['documents'], case['materials'], case['units'])
        self.assertEqual(actual['status'], 'pass')
        changed = deepcopy(case)
        changed['documents'][-2]['facts'][0] = '活动执行周期：2026年9月1日至2026年10月15日'
        check['reference_evidence']['periods'][0]['quote'] = changed['documents'][-2]['facts'][0]
        self.assertEqual(reference_comparison(check, changed['documents'], changed['materials'], changed['units'])['status'], 'fail')

    def test_written_and_seal_names_are_separate_and_typos_are_not_fixed(self):
        case, _ = case_for(1)
        check = {'rule_id':'contract_dealer','status':'unknown','reason':'未取得申请', 'source_ids':[], 'comparisons':[],
                 'reference_evidence':{'periods':[], 'written_name':{'unit_id':'contract','quote':'填写经销商名称：合成商贸有限公司','value':'合成商贸有限公司'},
                     'seal_name':{'unit_id':'contract','quote':'红色公章内名称：合成商贸有限公司','value':'合成商贸有限公司'}}}
        self.assertEqual(reference_comparison(check, case['documents'], case['materials'], case['units'])['status'], 'pass')
        bad = deepcopy(case);bad['documents'][-2]['facts'][2] = '红色公章内名称：合成商贸有限公可'
        check['reference_evidence']['seal_name'].update(quote=bad['documents'][-2]['facts'][2],value='合成商贸有限公可')
        self.assertEqual(reference_comparison(check, bad['documents'], bad['materials'], bad['units'])['status'], 'pass')
        self.assertEqual(check['reference_evidence']['seal_name']['value'], '合成商贸有限公可')
        check['reference_evidence']['seal_name']['unit_id'] = 'settlement'
        with self.assertRaises(AuditError):reference_comparison(check, bad['documents'], bad['materials'], bad['units'])

    def test_merge_rejects_omitted_duplicate_and_conflicting_evidence(self):
        one = {'checks':[{'rule_id':'one'}]}
        for parts, ids in (([one], ['one','two']), ([one,one],['one']),
                            ([{**one,'payment_dates':None},{'checks':[{'rule_id':'two'}],'payment_dates':{}}], ['one','two'])):
            with self.assertRaises(AuditError):merge(parts, ids)


class ProviderTests(unittest.TestCase):
    def test_twelve_sequential_requests_do_not_accumulate_history_for_either_provider(self):
        import httpx
        from audit_core.model_settings import api_selection
        from audit_core.qwen_model import complete_json
        for provider in ('ppio','deepseek'):
            payloads=[]
            def handler(request):
                payload=json.loads(request.content);payloads.append(payload)
                event={'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}],
                       'usage':{'prompt_tokens':7,'completion_tokens':4}}
                return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
            selection=api_selection({'provider':provider,'api_key':
                'sk_synthetic_never_send' if provider=='ppio' else 'sk-synthetic-never-send'})
            for index in range(12):
                complete_json(selection,[{'type':'text','text':f'case_marker_{index:02d}'}],
                              system='fixed rules',transport=httpx.MockTransport(handler))
            self.assertEqual(len(payloads),12)
            for index,payload in enumerate(payloads):
                self.assertEqual([m['role'] for m in payload['messages']],['system','user'])
                self.assertEqual(payload['messages'][1]['content'],[{'type':'text','text':f'case_marker_{index:02d}'}])
                self.assertFalse({'conversation_id','session_id','previous_response_id'} & payload.keys())

    def test_real_provider_leaves_rule_coverage_and_applicability_to_the_model(self):
        from tests.test_audit_rag_capacity import CapacityRecoveryTests
        case,_=case_for();runner=CapacityRecoveryTests()
        result=runner._run(case,lambda ids:False)
        self.assertEqual(len(runner.calls),1)
        self.assertCountEqual(runner.calls[0],[r.id for r in rules(case['scenario'])])
        self.assertCountEqual([c['rule_id'] for c in result['checks']],runner.calls[0])
        self.assertIn('程序不设置业务门禁',runner.prompts[0])


if __name__ == '__main__':unittest.main()
