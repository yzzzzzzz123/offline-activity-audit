"""Regressions for the three November 4 feedback cases; no live services."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from audit_core.extraction_chain import render_prompt
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_workflow import PdfWorkflow, _with_submitted_checks
from audit_core.qince_context import context_sources
from audit_core.submitted_source_checks import application_period_issues, pos_unit_price_issues
from audit_core.workbench_store import render_analysis_summary_markdown
from tests.pdf_test_support import PolicyProvider, bundle, flags_for, image_bytes, unknown_checks
from tests.test_optional_staff_and_display_photos import photo_case, review
from tests.test_qince_context import fixture
from tests.test_pos_arithmetic_coverage import native_formula_source


def period_case():
    raw = fixture()
    row = raw['queryRegularSaleActivities'][0]['subList'][0]
    row.update(startDate='2026-10-01', endDate='2026-10-31')
    units, documents = context_sources(raw, 'HX_TEST', 'a001')
    for uid, text in [('contract', '促销合同执行周期：2026年10月2日至2026年10月30日'),
                      ('settlement', '结算单活动时间：2026年10月2日至2026年10月30日')]:
        units.append({'unit_id': uid, 'source_file': uid+'.jpg', 'locator': uid,
                      'image': None, 'native_facts': [], 'limitations': []})
        documents.append({'unit_id': uid, 'facts': [text], 'limitations': []})
    materials = [{'id': name, 'state': 'present', 'source_ids': [uid]}
                 for name, uid in [('promotion_contract', 'contract'), ('settlement', 'settlement')]]
    return units, documents, materials


def pos_case(*, header='商品名称 | C2=69码 | C3=销售数量 | C4=销售金额',
             data='甲商品 | C2=6900000000001 | C3=2 | C4=20'):
    facts = ['销售!R1: C1='+header, '销售!R2: C1='+data]
    unit = {'unit_id': 'excel', 'source_file': 'POS.xlsx', 'locator': '销售',
            'image': None, 'native_facts': facts, 'limitations': []}
    document = {'unit_id': 'excel', 'facts': list(facts), 'limitations': []}
    materials = [{'id': 'pos_excel', 'state': 'present', 'source_ids': ['excel']},
                 {'id': 'pos', 'state': 'missing', 'source_ids': []}]
    return [unit], [document], materials


class ApplicationPeriodTests(unittest.TestCase):
    def test_matching_contract_and_settlement_cannot_hide_mismatch_with_application(self):
        units, documents, materials = period_case()
        before = deepcopy((units, documents, materials))
        issues = application_period_issues('personnel_incentive', documents, materials, units)
        self.assertEqual(len(issues), 2)
        self.assertTrue(all(issue['status'] == 'fail' for issue in issues))
        self.assertIn('促销合同执行周期2026-10-02至2026-10-30', issues[0]['reason'])
        self.assertIn('结算单执行周期2026-10-02至2026-10-30', issues[1]['reason'])
        self.assertTrue(all('2026-10-01至2026-10-31' in issue['reason'] for issue in issues))
        self.assertEqual((units, documents, materials), before)

    def test_all_three_agree_produces_no_extra_issue(self):
        units, documents, materials = period_case()
        for document in documents[-2:]:
            document['facts'] = ['活动执行周期2026年10月1日至2026年10月31日']
        self.assertEqual(application_period_issues('personnel_incentive', documents, materials, units), [])

    def test_each_document_is_compared_independently(self):
        units, documents, materials = period_case()
        documents[-2]['facts'] = ['活动执行周期2026年10月1日至2026年10月31日']
        issues = application_period_issues('personnel_incentive', documents, materials, units)
        self.assertEqual(len(issues), 1)
        self.assertIn('结算单执行周期', issues[0]['reason'])
        self.assertNotIn('contract', issues[0]['source_ids'])

    def test_missing_optional_qince_is_not_a_new_material_requirement(self):
        units, documents, materials = period_case()
        units = [u for u in units if u.get('source_kind') != 'qince']
        self.assertEqual(application_period_issues('personnel_incentive', documents, materials, units), [])

    def test_multiple_application_periods_are_not_arbitrarily_selected(self):
        units, documents, materials = period_case()
        unit = next(u for u in units if u['source_file'] == '勤策申请明细')
        source = next(d for d in documents if d['unit_id'] == unit['unit_id'])
        units.append({**deepcopy(unit), 'unit_id': 'qince-other'})
        extra = deepcopy(source)
        extra.update(unit_id='qince-other', facts=[fact.replace('2026-10-01', '2026-10-05') for fact in source['facts']])
        documents.append(extra)
        issues = application_period_issues('personnel_incentive', documents, materials, units)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['status'], 'unknown')

    def test_signing_or_ambiguous_dates_never_become_activity_dates(self):
        for fact in ['合同签订日期2026年10月2日至2026年10月30日',
                     '活动执行周期疑似2026年10月2日至2026年10月30日', '活动时间10月2日至10月30日']:
            with self.subTest(fact=fact):
                units, documents, materials = period_case()
                documents[-2]['facts'] = [fact]
                issue = application_period_issues('personnel_incentive', documents, materials, units)[0]
                self.assertEqual(issue['status'], 'unknown')

    def test_other_scenarios_keep_their_existing_date_rules(self):
        units, documents, materials = period_case()
        for scenario in ['price_difference_support', 'promotional_display', 'self_procured_gift_material']:
            self.assertEqual(application_period_issues(scenario, documents, materials, units), [])


class MissingSalePriceTests(unittest.TestCase):
    def test_missing_unit_price_survives_missing_stamped_pos(self):
        units, documents, materials = pos_case()
        issues = pos_unit_price_issues(flags_for('personnel_incentive'), documents, materials, units)
        self.assertEqual(len(issues), 1)
        self.assertEqual(issues[0]['status'], 'fail')
        self.assertIn('缺少SKU销售单价列', issues[0]['reason'])
        self.assertEqual(issues[0]['source_ids'], ['excel'])

    def test_blank_price_of_one_sku_is_reported_without_dividing_amount(self):
        units, documents, materials = pos_case(header='商品名称 | C2=69码 | C3=销售数量 | C4=销售金额 | C5=销售单价',
                                               data='甲商品 | C2=6900000000001 | C3=2 | C4=20 | C5=')
        issues = pos_unit_price_issues(flags_for('personnel_incentive'), documents, materials, units)
        self.assertEqual(len(issues), 1)
        self.assertIn('第2行商品甲商品（6900000000001）未填写销售单价', issues[0]['reason'])

    def test_zero_and_integer_prices_are_present_values(self):
        for price in ['0', '10', '9.90']:
            units, documents, materials = pos_case(header='商品名称 | C2=69码 | C3=销售数量 | C4=销售金额 | C5=销售单价',
                                                   data='甲商品 | C2=6900000000001 | C3=2 | C4=20 | C5='+price)
            self.assertEqual(pos_unit_price_issues(flags_for('personnel_incentive'), documents, materials, units), [])

    def test_reward_price_does_not_replace_sale_unit_price(self):
        units, documents, materials = pos_case(header='商品名称 | C2=69码 | C3=销售数量 | C4=销售金额 | C5=奖励单价')
        self.assertEqual(pos_unit_price_issues(flags_for('personnel_incentive'), documents, materials, units)[0]['status'], 'fail')

    def test_unrecognized_price_label_is_unknown_not_confirmed_missing(self):
        units, documents, materials = pos_case(header='商品名称 | C2=69码 | C3=销售数量 | C4=销售金额 | C5=其他价')
        self.assertEqual(pos_unit_price_issues(flags_for('personnel_incentive'), documents, materials, units)[0]['status'], 'unknown')

    def test_no_pos_no_native_rows_or_incomplete_reading_does_not_invent_missing_columns(self):
        units, documents, materials = pos_case()
        self.assertEqual(pos_unit_price_issues({'uses_pos': False}, documents, materials, units), [])
        units[0]['limitations'] = ['部分原生单元格未读取']
        self.assertEqual(pos_unit_price_issues({'uses_pos': True}, documents, materials, units), [])
        units[0].update(limitations=[], native_facts=[])
        self.assertEqual(pos_unit_price_issues({'uses_pos': True}, documents, materials, units), [])

    def test_stamped_document_cannot_supply_missing_excel_price(self):
        units, documents, materials = pos_case()
        units.append({'unit_id': 'stamp', 'source_file': '盖章.jpg', 'image': 'stamp.png', 'native_facts': ['销售单价10']})
        documents.append({'unit_id': 'stamp', 'facts': ['销售单价10']})
        materials[1].update(state='present', source_ids=['stamp'])
        self.assertIn('缺少SKU销售单价列', pos_unit_price_issues({'uses_pos': True}, documents, materials, units)[0]['reason'])

    def test_existing_all_row_native_formula_proof_is_not_misreported_as_missing_price(self):
        documents, materials, units = native_formula_source()
        by_id = {document['unit_id']: document for document in documents}
        for unit in units:
            unit.update(native_facts=deepcopy(by_id[unit['unit_id']]['facts']), image=None, limitations=[])
        self.assertEqual(pos_unit_price_issues({'uses_pos': True}, documents, materials, units), [])


class WorkflowAndPhotoTests(unittest.TestCase):
    def test_normal_audit_decision_and_view_also_report_native_price_and_application_date_problems(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            units, documents, materials = period_case()
            pos_units, pos_documents, pos_materials = pos_case()
            units.extend(pos_units)
            documents.extend(pos_documents)
            materials.extend(pos_materials)
            flags = flags_for('personnel_incentive')
            evidence = unknown_checks('personnel_incentive', flags)
            from audit_core.pdf_evidence import audit_decision
            from audit_core.submitted_source_checks import submitted_source_checks
            from audit_core.pdf_workflow import build_view
            packet = {'archive_id': 'a001', 'source_archive': '合成资料.zip', 'units': units,
                      'documents': documents, 'classification': {'flags': flags, 'materials': materials}}
            original = deepcopy(evidence)
            result = audit_decision('personnel_incentive', flags, evidence, documents, materials, units=units)
            checks = submitted_source_checks('personnel_incentive', flags, documents, materials, units)
            packet['result'] = _with_submitted_checks(result, checks)
            view = build_view([packet], None)
            summary = render_analysis_summary_markdown(view, {'status': 'completed'})
            self.assertIn('缺少商品销售单价列', summary)
            self.assertIn('促销合同执行周期2026-10-02至2026-10-30', summary)
            self.assertIn('结算单执行周期2026-10-02至2026-10-30', summary)
            self.assertEqual(evidence, original)

    def test_visible_three_columns_and_unique_store_can_pass_without_a_ruler(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = photo_case(root, 1)
            case['documents'][0]['facts'] = [
                '本次3纵位指三组并排陈列单元；好多多青闸线店在青闸线1号，玉田店在玉田路2号。']
            case['documents'][1]['facts'] = ['照片可见三组并排陈列单元，水印地址青闸线1号。']
            provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
            def call(root, skill, schema, prompt, *, images, label, validator):
                text = render_prompt(prompt)
                self.assertIn('本次3纵位指三组并排陈列单元', text)
                self.assertIn('水印地址青闸线1号', text)
                value = {'photo_reviews': [review('p01')]}
                value['photo_reviews'][0]['checks'][0]['reason'] = '水印青闸线1号唯一对应青闸线店，排除地址为玉田路2号的玉田店。'
                value['photo_reviews'][0]['checks'][3]['reason'] = '完整边界内可辨认三组并排单元，与合同本次3纵位含义一致。'
                validator(value)
                return value
            with patch.object(provider, '_call', side_effect=call):
                actual = provider._audit_display_photos(case, root, [], [])
            self.assertEqual(actual[0]['checks'][0]['status'], 'pass')
            self.assertEqual(actual[0]['checks'][3]['status'], 'pass')

    def test_real_workflow_keeps_gate_closed_and_reports_all_received_source_problems(self):
        class ReceivedProvider(PolicyProvider):
            def __call__(self, case, temporary):
                if case['kind'] != 'pdf_material_classification':
                    raise AssertionError('Missing stamped POS must not invoke full business audit')
                value = super().__call__(case, temporary)
                units = {u['unit_id']:u for u in case['units']}
                by_file = {u['source_file']: u['unit_id'] for u in case['units']}
                for document in value['documents']:
                    if units[document['unit_id']].get('sheet_layout'):
                        document['sheet_layout'] = deepcopy(units[document['unit_id']]['sheet_layout'])
                    if document['unit_id'] == by_file['合同.png']:
                        document['facts'] = ['促销合同执行周期2026年10月2日至2026年10月30日']
                    if document['unit_id'] == by_file['结算.png']:
                        document['facts'] = ['结算单活动时间2026年10月2日至2026年10月30日']
                match = value['classification']['material_matches'][0]
                for material in match['materials']:
                    role = material['id']
                    if role in {'promotion_contract', 'settlement', 'pos_excel'}:
                        filename = {'promotion_contract': '合同.png', 'settlement': '结算.png', 'pos_excel': 'POS.xlsx'}[role]
                        material['source_ids'] = [by_file[filename]]
                value['classification']['materials'] = deepcopy(match['materials'])
                return value
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workbook = Workbook()
            workbook.active.title = '销售'
            workbook.active.append(['商品名称', '69码', '销售数量', '销售金额'])
            workbook.active.append(['甲商品', '6900000000001', 2, 20])
            path = root/'POS.xlsx'; workbook.save(path)
            bundle(root/'input', files={'合同.png': image_bytes(), '结算.png': image_bytes(), 'POS.xlsx': path.read_bytes()})
            raw = fixture()
            raw['queryRegularSaleActivities'][0]['subList'][0].update(startDate='2026-10-01', endDate='2026-10-31')
            provider = ReceivedProvider(scenarios=('personnel_incentive',), missing={0: ('pos',)})
            workflow = PdfWorkflow('20261104-source-test', 'fixture', root/'input', root/'temporary', provider,
                                   None, None, biz_type='人员激励', verify_code='HX_TEST', qince_data=raw)
            with patch('httpx.Client', side_effect=AssertionError('No real Qince')):
                receipt = workflow.run()
            self.assertFalse(workflow.packets[0]['route']['matched'])
            self.assertNotIn('audit_evidence', workflow.packets[0])
            summary = render_analysis_summary_markdown(receipt['view_payload'], {'status': 'completed'})
            self.assertIn('盖章', summary)
            self.assertIn('缺少商品销售单价列', summary)
            self.assertIn('促销合同执行周期2026-10-02至2026-10-30', summary)
            self.assertIn('结算单执行周期2026-10-02至2026-10-30', summary)
            self.assertIn('2026-10-01至2026-10-31', summary)

    def test_received_source_problem_overrides_model_pass_without_rewriting_model_evidence(self):
        result = {'checks': [{'rule_id': 'pos_fields', 'status': 'pass', 'reason': '模型声称字段齐全',
                             'source_ids': ['excel']}], 'summary': {'conclusion': 'no_issues_found', 'error_count': 0}}
        before = deepcopy(result)
        units, documents, materials = pos_case()
        merged = _with_submitted_checks(result, pos_unit_price_issues({'uses_pos': True}, documents, materials, units))
        self.assertEqual(result, before)
        self.assertEqual(merged['checks'][0]['status'], 'fail')
        self.assertEqual(merged['summary']['error_count'], 1)

    def test_actual_photo_prompt_distinguishes_columns_area_and_same_name_stores_and_keeps_unknown(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            case = photo_case(root, 1)
            provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
            def call(root, skill, schema, prompt, *, images, label, validator):
                text = render_prompt(prompt)
                self.assertIn('不能因没有平方米、米数或尺子就判无法确认纵位', text)
                self.assertIn('可用可辨认的同类同规格包装常见尺寸区间作估算', text)
                self.assertIn('不把所有牙膏、纸箱、地砖预设为同一固定尺寸', text)
                self.assertIn('水印只写连锁简称不自动表示无法对应', text)
                self.assertIn('仍有两家无法排除时明确列出两家和缺少的区分依据', text)
                value = {'photo_reviews': [review('p01', status='unknown', problem='青闸线店与玉田店均无法排除，照片未见可区分的地址') ]}
                value['photo_reviews'][0]['checks'][3].update(status='unknown', reason='合同1平米，照片边界遮挡且无有依据的尺寸参照')
                validator(value)
                return value
            with patch.object(provider, '_call', side_effect=call):
                actual = provider._audit_display_photos(case, root, [], [])
            self.assertEqual(actual[0]['checks'][0]['status'], 'unknown')
            self.assertEqual(actual[0]['checks'][3]['status'], 'unknown')


if __name__ == '__main__':
    unittest.main()
