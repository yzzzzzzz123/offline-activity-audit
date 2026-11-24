"""Native POS field proofs and normal-stage delivery; no real model calls."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from openpyxl import Workbook

from audit_core.audit_tools import execute_tool
from audit_core.native_pos_fields import native_pos_field_check
from audit_core.pdf_evidence import audit_decision, audit_schema, validate
from audit_core.pdf_materials import _spreadsheet_units
from audit_core.pdf_policy import MATERIALS
from audit_core.pos_arithmetic import prepare_pos_arithmetic
from audit_core.pos_product_identity import prepare_pos_identities
from audit_core.product_database import catalog_from_rows
from audit_core.spreadsheet_rows import encode_row
from tests.pdf_test_support import unknown_checks
from tests.test_audit_rag_contract import case_for
from tests import test_audit_rag_capacity as capacity_support


def native_case(count=3, *, sheet='销售明细', file_id='file', time_column=True, chunk=100):
    case, _ = case_for(1)
    case['flags']['uses_pos'] = True
    def row(n, values):
        return encode_row(sheet, n, [(col, str(value)) for col, value in values.items()])
    facts = []
    if not time_column:
        facts.append(row(1, {1: '活动时间：2026-09-20至2026-10-15'}))
    first = len(facts) + 1
    header = {1: '商品名称', 2: '69码', 3: '销售数量', 4: '销售单价', 5: '销售金额'}
    if time_column:
        header[6] = '销售日期'
    facts.append(row(first, header))
    for index in range(count):
        values = {1: '测试商品', 2: '6970356168997', 3: 2, 4: '3.1', 5: '6.2'}
        if time_column:
            values[6] = '2026-09-20'
        facts.append(row(first+index+1, values))
    facts.append(row(first+count+1, {1: '合计', 3: count*2, 5: Decimal('6.2')*count}))
    ids = []
    for offset in range(0, len(facts), chunk):
        uid = file_id+'-u'+str(offset//chunk)
        selected = facts[offset:offset+chunk]
        case['units'].append({'unit_id': uid, 'file_id': file_id, 'source_file': file_id+'.xlsx',
            'native_facts': selected, 'limitations': [], 'image': None})
        case['documents'].append({'unit_id': uid, 'roles': ['spreadsheet'],
                                  'facts': selected, 'limitations': []})
        ids.append(uid)
    next(m for m in case['materials'] if m['id'] == 'pos_excel').update(state='present', source_ids=ids)
    return case


def alter(case, function, uid='file-u0'):
    document = next(d for d in case['documents'] if d['unit_id'] == uid)
    document['facts'] = function(list(document['facts']))
    next(u for u in case['units'] if u['unit_id'] == uid)['native_facts'] = list(document['facts'])


class NativePosFieldTests(unittest.TestCase):
    def test_all_original_rows_and_sources_across_chunks_are_proved_without_mutation(self):
        case = native_case(251, chunk=50)
        original = deepcopy(case)
        check, receipt = native_pos_field_check(case)
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(receipt['detail_rows'], 251)
        self.assertEqual(receipt['tables'][0]['last_row'], 252)
        self.assertEqual(len(receipt['sources']), 6)
        self.assertEqual(case, original)

    def test_same_sheet_names_in_separate_files_do_not_share_columns_or_dates(self):
        case = native_case(time_column=False)
        other = native_case(2, file_id='other', time_column=False)
        case['units'] += [u for u in other['units'] if u['file_id'] == 'other']
        case['documents'] += [d for d in other['documents'] if d['unit_id'].startswith('other-')]
        next(m for m in case['materials'] if m['id'] == 'pos_excel')['source_ids'].append('other-u0')
        check, receipt = native_pos_field_check(case)
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(receipt['detail_rows'], 5)
        alter(case, lambda facts: facts[1:], uid='other-u0')
        self.assertIsNone(native_pos_field_check(case)[0])

    def test_multisheet_and_repeated_header_keep_every_table(self):
        case = native_case(4)
        # Use distinct physical row positions, as a paginated native sheet does.
        alter(case, lambda _: [
            '销售明细!R1: C1=商品名称 | C2=69码 | C3=数量 | C4=单价 | C5=金额 | C6=日期',
            '销售明细!R2: C1=商品 | C2=6900000000000 | C3=2 | C4=3 | C5=6 | C6=2026-09-20',
            '销售明细!R4: C1=商品名称 | C2=69码 | C3=数量 | C4=单价 | C5=金额 | C6=日期',
            '销售明细!R5: C1=商品 | C2=6900000000001 | C3=4 | C4=3 | C5=12 | C6=2026-09-21',
            '销售明细!R6: C1=合计 | C3=6 | C5=18',
            '隐藏表!R1: C1=商品名称 | C2=69码 | C3=数量 | C4=单价 | C5=金额 | C6=日期',
            '隐藏表!R2: C1=商品 | C2=6900000000002 | C3=1 | C4=3 | C5=3 | C6=2026-09-22',
            '隐藏表!R3: C1=合计 | C3=1 | C5=3'])
        check, receipt = native_pos_field_check(case)
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(receipt['detail_rows'], 3)
        self.assertEqual(len(receipt['tables']), 2)
        self.assertEqual(len(receipt['tables'][0]['header_sources']), 2)

    def test_each_required_detail_field_including_final_row_is_checked(self):
        for field, old, replacement in [
                ('name', 'C1=测试商品', 'C1='), ('barcode', 'C2=6970356168997', 'C2='),
                ('quantity', 'C3=2', 'C3='), ('price', 'C4=3.1', 'C4='),
                ('amount', 'C5=6.2', 'C5='), ('time', 'C6=2026-09-20', 'C6=')]:
            with self.subTest(field=field):
                case = native_case()
                alter(case, lambda f: f[:-2]+[f[-2].replace(old, replacement)]+f[-1:])
                self.assertIsNone(native_pos_field_check(case)[0])

    def test_ambiguous_missing_and_bad_date_fields_return_to_model_without_failure(self):
        for date in ('', '2月30日', '2026-02-30', '2026-09-20至', '待确认', '45920'):
            with self.subTest(date=date):
                case = native_case()
                alter(case, lambda f: [r.replace('C6=2026-09-20', 'C6='+date) for r in f])
                check, receipt = native_pos_field_check(case)
                self.assertIsNone(check)
                self.assertFalse(receipt['complete'])
                self.assertNotIn('status', receipt)

    def test_source_titles_can_supply_time_but_contracts_and_other_sheets_cannot(self):
        case = native_case(time_column=False)
        check, receipt = native_pos_field_check(case)
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(receipt['tables'][0]['activity_title_source'], {'unit_id':'file-u0', 'fact_index':0})
        alter(case, lambda f: f[1:])
        self.assertIsNone(native_pos_field_check(case)[0])

    def test_date_cells_accept_native_datetime_and_full_written_range(self):
        for date in ('2026-09-20 00:00:00', '2026年9月20日至2026年10月15日', '2026/9/20',
                     '9月20日', '9.20-10.15', '12月20日至1月15日'):
            with self.subTest(date=date):
                case = native_case()
                alter(case, lambda f: [r.replace('C6=2026-09-20', 'C6='+date) for r in f])
                self.assertEqual(native_pos_field_check(case)[0]['status'], 'pass')

    def test_separate_start_end_columns_and_sales_report_heading(self):
        case = native_case()
        # Real report title uses a valid native position; no arbitrary note skip.
        alter(case, lambda f: ['销售明细!R1: C1=合成系统销售']+
              [
              r.replace('!R'+str(i+1)+':', '!R'+str(i+2)+':').replace('C6=销售日期',
                'C6=销售开始时间 | C7=销售结束时间').replace('C6=2026-09-20',
                'C6=2026-09-20T00:00:00 | C7=2026-10-15T00:00:00') for i,r in enumerate(f)])
        self.assertEqual(native_pos_field_check(case)[0]['status'], 'pass')
        alter(case, lambda f: [r.replace('C7=2026-10-15T00:00:00', 'C7=2026-09-01T00:00:00') for r in f])
        self.assertIsNone(native_pos_field_check(case)[0])

    def test_sales_period_as_amount_header_and_generic_product_name_are_native_fields(self):
        case = native_case()
        alter(case, lambda f: [r.replace('C5=销售金额 | C6=销售日期',
            'C5=2026年09月20日-2026年10月15日').replace(' | C6=2026-09-20', '') for r in f])
        check, receipt = native_pos_field_check(case)
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(receipt['tables'][0]['activity_header_source'], {'unit_id':'file-u0','fact_index':0})
        case = native_case()
        alter(case, lambda f: [r.replace('C1=商品名称', 'C1=名称') for r in f])
        self.assertEqual(native_pos_field_check(case)[0]['status'], 'pass')

    def test_general_pos_fields_include_target_incentive_after_new_workbook(self):
        for scenario in MATERIALS:
            with self.subTest(scenario=scenario):
                case = native_case(); case['scenario'] = scenario
                result = execute_tool(case, 'pos_field_inventory')
                self.assertEqual(result['check']['status'], 'pass')

    def test_target_accepts_numbered_merged_title_and_only_an_amount_total(self):
        case = native_case()
        case['scenario'] = 'pos_target_incentive'
        alter(case, lambda _: [
            '销售明细!R1: C1=合作门店7月份销售数据',
            '销售明细!R2: C1=日期 | C2=商品名称 | C3=69码 | C4=数量 | C5=单价 | C6=金额',
            '销售明细!R3: C1=2026-07-01 | C2=牙膏 | C3=6900000000001 | C4=2 | C5=3.2 | C6=6.4',
            '销售明细!R4: C1=合计 | C6=6.4'])
        pos_unit = next(unit for unit in case['units'] if unit['unit_id'] == 'file-u0')
        pos_unit['sheet_layout'] = {'sheet': '销售明细', 'merged_ranges': ['A1:F1']}
        check, proof = native_pos_field_check(case)
        self.assertIsNotNone(check, proof)
        self.assertEqual(check['status'], 'pass')
        self.assertEqual(proof['fields'][-1], '销售金额合计')
        case['scenario'] = 'giveaway_promotion'
        self.assertIsNone(native_pos_field_check(case)[0])

    def test_pos_export_headers_share_native_arithmetic_and_identity_tools(self):
        case = native_case()
        alter(case, lambda f: [r.replace('C2=69码', 'C2=SKU条码').replace('C5=销售金额',
            'C5=POS金额').replace('C6=销售日期', 'C6=POS日期') for r in f])
        check, _ = native_pos_field_check(case)
        self.assertEqual(check['status'], 'pass')
        with tempfile.TemporaryDirectory() as directory, patch(
                'audit_core.pos_product_identity.load_product_catalog', return_value=catalog_from_rows([
                    {'product_code':'TEST', 'product_name':'测试商品', 'barcode_69':'6970356168997'}])):
            identities = prepare_pos_identities(case, Path(directory),
                lambda *a, **kw: self.fail('clear POS export headers must use native cells'))
        self.assertEqual(len(identities['rows']), 3)
        self.assertTrue(all(r['status']=='pass' for r in identities['rows']))
        alter(case, lambda f: [r.replace('C5=POS金额', 'C5=核销金额') for r in f])
        self.assertIsNone(native_pos_field_check(case)[0], 'a claim amount is not a POS sale amount')

    def test_ambiguous_column_mapping_or_unread_footer_never_proves_completeness(self):
        for change in (lambda f: [f[0]+' | C7=单价']+f[1:],
                       lambda f: f+['销售明细!R99: C1=另有销售明细未包含'],
                       lambda f: f+[f[1]],
                       lambda f: f[:-1]):
            case = native_case()
            alter(case, change)
            self.assertIsNone(native_pos_field_check(case)[0])

    def test_native_facts_must_be_original_not_ocr_or_modified_or_partial(self):
        for change in (lambda u: u.update(image=Path('scan.png')),
                       lambda u: u.update(limitations=['原表部分内容未解析']),
                       lambda u: u.update(native_facts=['不是原始事实']),
                       lambda u: u.update(source_kind='qince'),
                       lambda u: u.update(file_id=None)):
            case = native_case()
            change(next(u for u in case['units'] if u['unit_id']=='file-u0'))
            self.assertIsNone(native_pos_field_check(case)[0])

    def test_escaping_and_original_precision_are_not_rewritten_by_field_proof(self):
        case = native_case()
        def change(facts):
            facts[1] = encode_row('销售明细', 2, [(1, '原名 | C99=\n"系列\\新品'),
                (2, '6970356168997'), (3, '2'), (4, '3.1'), (5, '6.2'), (6, '2026-09-20')])
            return facts
        alter(case, change)
        original = deepcopy(case)
        self.assertEqual(native_pos_field_check(case)[0]['status'], 'pass')
        self.assertEqual(case, original)

    def test_present_fields_do_not_hide_real_arithmetic_discrepancies(self):
        case = native_case()
        alter(case, lambda f: [r.replace('C5=6.2', 'C5=6.199999999999999') for r in f])
        field_check, _ = native_pos_field_check(case)
        self.assertEqual(field_check['status'], 'pass')
        value = unknown_checks(case['scenario'], case['flags'])
        value['checks'] = [field_check if c['rule_id']=='pos_fields' else c for c in value['checks']]
        final = audit_decision(case['scenario'], case['flags'], value, case['documents'],
                               case['materials'], units=case['units'])
        self.assertEqual(next(c for c in final['checks'] if c['rule_id']=='pos_arithmetic')['status'], 'fail')
        self.assertEqual(next(c for c in final['checks'] if c['rule_id']=='pos_fields')['status'], 'pass')

    def test_actual_upload_reader_yields_same_native_proof_across_sheets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            book = Workbook()
            for name in ('首页', '隐藏表'):
                sheet = book.active if name=='首页' else book.create_sheet(name)
                sheet.title = name
                sheet.append(['商品名称', '69码', '数量', '销售单价', '销售金额', '活动日期'])
                sheet.append(['商品', '6900000000000', 2, 3, '=C2*D2', '2026-09-20'])
                # No cached formula is genuinely unresolved, not a missing tool.
                sheet.cell(2, 5, 6)
                sheet.append(['合计', None, 2, None, 6])
            book['隐藏表'].sheet_state = 'hidden'
            source = root/'original.xlsx'; book.save(source); book.close()
            output = root/'units'; output.mkdir()
            units = _spreadsheet_units({'path': source, 'source_file': source.name, 'file_id':'f'}, output)
            case, _ = case_for(1); case['flags']['uses_pos'] = True
            case['units'] += units
            case['documents'] += [{'unit_id': u['unit_id'], 'roles':['spreadsheet'],
                'facts':u['native_facts'], 'limitations':u['limitations']} for u in units]
            next(m for m in case['materials'] if m['id']=='pos_excel').update(
                state='present', source_ids=[u['unit_id'] for u in units])
            check, receipt = native_pos_field_check(case)
            self.assertEqual(check['status'], 'pass')
            self.assertEqual({t['sheet'] for t in receipt['tables']}, {'首页', '隐藏表'})

    def test_readonly_tool_and_cli_do_not_call_network_or_change_case(self):
        case = native_case(); original = deepcopy(case)
        with patch('httpx.Client', side_effect=AssertionError('no network')), \
             patch('httpx.AsyncClient', side_effect=AssertionError('no network')):
            tool = execute_tool(case, 'pos_field_inventory')
        self.assertTrue(tool['complete']); self.assertEqual(tool['model_calls'], 0)
        self.assertEqual(case, original)
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'case.json'; source.write_text(json.dumps(case), encoding='utf-8')
            script = Path(__file__).resolve().parents[1]/'skills/orchestrate-offline-audit/scripts/audit_tools.py'
            result = subprocess.run([sys.executable, '-B', str(script), '--case', str(source), 'pos_field_inventory'],
                                    text=True, capture_output=True, encoding='utf-8', check=True)
            self.assertTrue(json.loads(result.stdout)['complete'])

    def test_normal_large_stage_keeps_native_field_proof_as_model_advice(self):
        case=native_case(1000,chunk=150)
        runner=capacity_support.CapacityRecoveryTests()
        result=runner._run(case,lambda ids:False)
        self.assertTrue({'pos_fields','pos_arithmetic','pos_product_identity'} <= set(runner.calls[0]))
        # A tool can prove fields; the runner cannot replace the model's conclusion.
        self.assertEqual(native_pos_field_check(case)[0]['status'],'pass')
        self.assertEqual(next(c['status'] for c in result['checks'] if c['rule_id']=='pos_fields'),'unknown')


if __name__ == '__main__': unittest.main()
