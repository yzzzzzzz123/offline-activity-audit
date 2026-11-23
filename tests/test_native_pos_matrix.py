"""Native matrix identity, full provenance and normal provider integration."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from openpyxl import Workbook

from audit_core.audit_tools import execute_tool, dispatch_requests, tool_protocol, TOOL_PROTOCOL
from audit_core.audit_rag import part_schema
from audit_core.pdf_evidence import audit_schema, validate
from audit_core.stage_context import compile_context
from audit_core.model_metrics import collect_model_artifacts
from audit_core.native_pos_identity import native_identity_extractions
from audit_core.native_pos_matrix import native_matrix_extractions
from audit_core.pdf_materials import _spreadsheet_units
from audit_core.pdf_policy import MATERIALS
from audit_core.pos_product_identity import prepare_pos_identities, validate_extraction
from audit_core.product_database import catalog_from_rows
from audit_core.spreadsheet_rows import encode_row, ROW, parse_cells
from tests.test_native_pos_fields import native_case
from tests import test_audit_rag_capacity as capacity_support


_CODES = ('6970356168997', '6970356167341', '6978056440184')


def matrix_case(count=4, *, width=3, chunk=150, sheet='矩阵', file_id='matrix'):
    case = native_case(1)
    case['units'] = [u for u in case['units'] if u['file_id'] != 'file']
    case['documents'] = [d for d in case['documents'] if not d['unit_id'].startswith('file-')]
    facts = []
    def add(row, values):
        facts.append(encode_row(sheet, row, [(k,str(v)) for k,v in values.items()]))
    add(1, {1:'销售单价', **{4+n:'9.9' for n in range(width)}, width+4:'销量总计', width+5:'销售总计'})
    add(2, {4+n:_CODES[n%len(_CODES)] for n in range(width)})
    add(3, {1:'门店',2:'促销名字',3:'活动周期',**{4+n:'原名'+str(n) for n in range(width)}})
    for index in range(count):
        add(4+index, {1:'门店'+str(index),3:'2026-09-20至2026-10-15',
            **{4+n:index+1 for n in range(width)},width+4:width*(index+1),width+5:'9.9'})
    add(4+count, {1:'合计', **{4+n:count*(count+1)//2 for n in range(width)},
                  width+4:width*count*(count+1)//2,width+5:'99'})
    ids=[]
    for offset in range(0,len(facts),chunk):
        uid=file_id+'-'+str(offset//chunk); selected=facts[offset:offset+chunk]
        case['documents'].append({'unit_id':uid,'roles':['spreadsheet'],'facts':selected,'limitations':[]})
        case['units'].append({'unit_id':uid,'file_id':file_id,'source_file':file_id+'.xlsx',
            'native_facts':selected,'limitations':[],'image':None})
        ids.append(uid)
    next(m for m in case['materials'] if m['id']=='pos_excel').update(source_ids=ids)
    return case


def pos_docs(case):
    ids={u for m in case['materials'] if m['id']=='pos_excel' for u in m['source_ids']}
    return [d for d in case['documents'] if d['unit_id'] in ids]


def sync(case):
    docs={d['unit_id']:d for d in case['documents']}
    for unit in case['units']:
        unit['native_facts']=list(docs[unit['unit_id']]['facts'])


def catalog():
    return catalog_from_rows([{'product_code':'CP'+str(i),'product_name':'库名'+str(i),'barcode_69':code}
                              for i,code in enumerate(_CODES)])


class NativePosMatrixTests(unittest.TestCase):
    def extract(self, case):
        return native_matrix_extractions(pos_docs(case),case['units'])

    def verify(self, case):
        direct, proof=self.extract(case)
        for d in pos_docs(case):
            if d['unit_id'] in direct:validate_extraction(direct[d['unit_id']]['extracted'],d)
        return direct,proof

    def test_matrix_name_barcode_pairs_and_multilevel_totals_preserve_originals(self):
        case=matrix_case(); before=deepcopy(case)
        result,proof=self.verify(case)
        rows=result['matrix-0']['extracted']['rows']
        self.assertEqual([r['barcode_69']['value'] for r in rows],list(_CODES))
        self.assertEqual([r['product_name']['value'] for r in rows],['原名0','原名1','原名2'])
        self.assertEqual(proof['files'][0]['tables'][0]['data_rows'],4)
        self.assertEqual(proof['files'][0]['tables'][0]['summary_columns'],[7,8])
        self.assertEqual(case,before)

    def test_all_sales_chunks_are_covered_without_repeating_identity_per_store(self):
        case=matrix_case(10013,chunk=150)
        result,proof=self.verify(case)
        self.assertEqual(set(result),{d['unit_id'] for d in pos_docs(case)})
        self.assertEqual(sum(len(v['extracted']['rows']) for v in result.values()),3)
        self.assertEqual(proof['files'][0]['tables'][0]['data_rows'],10013)
        final=pos_docs(case)[-1]
        covered=result[final['unit_id']]['extracted']['other_facts']
        self.assertEqual({i['fact_index'] for i in covered},set(range(len(final['facts']))))

    def test_name_row_before_code_row_also_binds_by_column(self):
        case=matrix_case()
        facts=pos_docs(case)[0]['facts']
        facts[1],facts[2]=facts[2].replace('!R3:','!R2:'),facts[1].replace('!R2:','!R3:')
        sync(case)
        result,_=self.verify(case)
        self.assertEqual(result['matrix-0']['extracted']['rows'][0]['product_name']['fact_index'],1)

    def test_header_pair_crossing_units_or_files_is_not_invented(self):
        for chunk in (2,):
            case=matrix_case(chunk=chunk)
            self.assertEqual(self.extract(case)[0],{})
        case=matrix_case(chunk=2)
        case['units'][-1]['file_id']='other'
        self.assertEqual(self.extract(case)[0],{})

    def test_identical_sheet_names_in_different_files_never_share_headers(self):
        case=matrix_case(); other=matrix_case(file_id='other')
        other['documents'][-1]['facts']=other['documents'][-1]['facts'][3:]
        sync(other)
        case['documents']+=pos_docs(other)
        case['units'] += [u for u in other['units'] if u['file_id']=='other']
        next(m for m in case['materials'] if m['id']=='pos_excel')['source_ids'].append('other-0')
        result,_=self.verify(case)
        self.assertIn('matrix-0',result);self.assertNotIn('other-0',result)

    def test_different_sheets_and_hidden_sheet_data_are_read_independently(self):
        case=matrix_case(); facts=pos_docs(case)[0]['facts']
        second=[f.replace('矩阵!','隐藏表!').replace('原名0','另一表原名') for f in list(facts)]
        facts.extend(second);sync(case)
        result,proof=self.verify(case)
        self.assertEqual(len(result['matrix-0']['extracted']['rows']),6)
        self.assertEqual({t['sheet'] for t in proof['files'][0]['tables']},{'矩阵','隐藏表'})

    def test_missing_names_bad_codes_and_numeric_name_header_fall_back(self):
        for change in (
                lambda f: [r.replace('C4=原名0','C4=') for r in f],
                lambda f: [r.replace('C4=6970356168997','C4=697035616899') for r in f],
                lambda f: [r.replace('C4=原名0','C4=123') for r in f],
                lambda f: [r.replace('C4=6970356168997','C4=公式=A1；缓存值=6970356168997') for r in f]):
            case=matrix_case();pos_docs(case)[0]['facts']=change(pos_docs(case)[0]['facts']);sync(case)
            self.assertEqual(self.extract(case)[0],{})

    def test_unbound_identifier_or_new_product_at_the_tail_is_not_skipped(self):
        for tail in ('矩阵!R999: C4=另一个商品 | C5=6970356167341',
                     '矩阵!R999: C1=另一个商品',
                     '矩阵!R999: C1=门店 | C4=未知商品',
                     '矩阵!R999: C1=门店 | C99=1'):
            case=matrix_case();pos_docs(case)[0]['facts'].append(tail);sync(case)
            self.assertEqual(self.extract(case)[0],{})

    def test_real_missing_identity_in_prefix_is_not_a_report_heading(self):
        case=matrix_case()
        pos_docs(case)[0]['facts'][0]='矩阵!R1: C1=另一个商品'
        sync(case)
        self.assertEqual(self.extract(case)[0],{})

    def test_duplicate_rows_metadata_and_untrusted_sources_keep_model_path(self):
        changes=[lambda c:c['units'][-1].update(image=Path('scan.jpg')),
                 lambda c:c['units'][-1].update(source_kind='qince'),
                 lambda c:c['units'][-1].update(native_facts=['changed']),
                 lambda c:pos_docs(c)[0].update(limitations=['部分表未读取']),
                 lambda c:c['units'].append(deepcopy(c['units'][-1])),
                 lambda c:pos_docs(c)[0]['facts'].append(pos_docs(c)[0]['facts'][2])]
        for change in changes:
            case=matrix_case(); change(case)
            if change is changes[-1]:sync(case)
            self.assertEqual(self.extract(case)[0],{})

    def test_cell_escaping_preserves_exact_name_quote(self):
        case=matrix_case()
        facts=pos_docs(case)[0]['facts']; m=ROW.fullmatch(facts[2]); cells=parse_cells(m[3])
        cells[4]['raw']='原名 | C8=\n"系列\\子款'
        facts[2]=encode_row('矩阵',3,[(col,c['raw']) for col,c in cells.items()]);sync(case)
        result,_=self.verify(case)
        self.assertEqual(result['matrix-0']['extracted']['rows'][0]['product_name']['value'],cells[4]['raw'])

    def test_regular_flat_table_retains_its_existing_extractor(self):
        case=native_case()
        self.assertEqual(self.extract(case)[0],{})
        native=native_identity_extractions(pos_docs(case),case['units'])
        self.assertEqual(len(native['file-u0']['extracted']['rows']),3)

    def test_stage_tool_instructions_only_describe_current_work_without_rejecting_old_replies(self):
        photo=compile_context('promotional_display',['display_brand'],{})['policies']['tool_protocol']
        self.assertIn('source_inventory',photo)
        self.assertNotIn('pos_matrix_identity',photo)
        self.assertNotIn('linked_application_amounts',photo)
        self.assertLess(len(photo.encode('utf-8')),len(TOOL_PROTOCOL.encode('utf-8')))
        identity=tool_protocol(['pos_product_identity'])
        self.assertIn('pos_matrix_identity',identity);self.assertNotIn('activity_periods',identity)
        # Instruction selection is not a new failure gate on saved answers.
        case=native_case()
        schema=part_schema(audit_schema(case['scenario']),['contract_signed'],{})
        value={'checks':[{'rule_id':'contract_signed','status':'unknown','reason':'原资料仍需核对',
                          'source_ids':[],'comparisons':[]}],
               'tool_requests':[{'tool':'pos_calculation','rule_ids':['contract_signed'],'fields':[]}]}
        validate(value,schema)
        self.assertEqual(dispatch_requests(case,value['tool_requests'],['contract_signed'])[0]['result']['model_calls'],0)

    def test_all_eight_scenarios_keep_db_identity_check_without_model_extraction(self):
        for scenario in MATERIALS:
            with self.subTest(scenario=scenario),tempfile.TemporaryDirectory() as directory:
                case=matrix_case(200,chunk=50);case['scenario']=scenario
                call=Mock(side_effect=AssertionError('no model extraction'))
                with patch('audit_core.pos_product_identity.load_product_catalog',return_value=catalog()) as db:
                    result=prepare_pos_identities(case,Path(directory),call)
                self.assertEqual(len(result['rows']),3);self.assertTrue(all(r['status']=='pass' for r in result['rows']))
                db.assert_called_once();call.assert_not_called()

    def test_unknown_actual_barcode_is_still_a_db_problem_not_silently_matched(self):
        case=matrix_case()
        pos_docs(case)[0]['facts'][1]=pos_docs(case)[0]['facts'][1].replace('6970356168997','6999999999999')
        sync(case)
        with tempfile.TemporaryDirectory() as directory,patch(
                'audit_core.pos_product_identity.load_product_catalog',return_value=catalog()):
            result=prepare_pos_identities(case,Path(directory),Mock(side_effect=AssertionError('no model')))
        self.assertEqual(result['rows'][0]['reason_code'],'catalog_product_missing')
        self.assertEqual(result['rows'][0]['submitted']['barcode_69'],'6999999999999')

    def test_fixed_tool_and_cli_return_bindings_not_the_whole_matrix(self):
        case=matrix_case(1500)
        with patch('httpx.Client',side_effect=AssertionError('no network')), \
             patch('httpx.AsyncClient',side_effect=AssertionError('no network')):
            result=execute_tool(case,'pos_matrix_identity')
        self.assertTrue(result['complete']);self.assertEqual(len(result['products']),3)
        self.assertLess(len(json.dumps(result,ensure_ascii=False)),10000)
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'case.json';source.write_text(json.dumps(case),encoding='utf-8')
            script=Path(__file__).resolve().parents[1]/'skills/orchestrate-offline-audit/scripts/audit_tools.py'
            output=subprocess.run([sys.executable,'-B',str(script),'--case',str(source),'pos_matrix_identity'],
                capture_output=True,text=True,encoding='utf-8',check=True)
            self.assertTrue(json.loads(output.stdout)['complete'])

    def test_normal_workbook_reader_and_provider_use_native_matrix_identity(self):
        case=matrix_case(1000)
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);book=Workbook()
            # Use the same normal native reader, not pre-filled model output.
            for name in ('矩阵','隐藏矩阵'):
                sheet=book.active if name=='矩阵' else book.create_sheet(name)
                sheet.title=name
                for doc in pos_docs(case):
                    for fact in doc['facts']:
                        m=ROW.fullmatch(fact)
                        for col,cell in parse_cells(m[3]).items():
                            sheet.cell(int(m[2]),col,cell['raw'])
                if name=='隐藏矩阵':sheet.sheet_state='hidden'
            source=root/'matrix.xlsx';book.save(source);book.close();out=root/'units';out.mkdir()
            units=_spreadsheet_units({'path':source,'source_file':source.name,'file_id':'read-matrix'},out)
            case['documents']=[d for d in case['documents'] if d not in pos_docs(case)]
            case['units']=[u for u in case['units'] if u['file_id']!='matrix']+units
            case['documents'] += [{'unit_id':u['unit_id'],'roles':['spreadsheet'],
                'facts':u['native_facts'],'limitations':u['limitations']} for u in units]
            next(m for m in case['materials'] if m['id']=='pos_excel')['source_ids']=[u['unit_id'] for u in units]
            call=Mock(side_effect=AssertionError('no model identity read'))
            with collect_model_artifacts(root/'analysis'),patch(
                    'audit_core.pos_product_identity.load_product_catalog',return_value=catalog()):
                identities=prepare_pos_identities(case,root,call)
            self.assertEqual(len(identities['rows']),6)
            trace=[json.loads(p.read_text(encoding='utf-8')) for p in (root/'analysis/execution-trace').glob('*.json')]
            self.assertTrue(any(e['data'].get('method')=='explicit_native_matrix_cells' for e in trace))
            call.assert_not_called()
        runner=capacity_support.CapacityRecoveryTests()
        result=runner._run(case,lambda ids:False,pos_identities=identities)
        self.assertIn('pos_product_identity',{key for group in runner.calls for key in group})
        self.assertIn('pos_fields',{key for group in runner.calls for key in group})
        self.assertEqual(next(c['status'] for c in result['checks'] if c['rule_id']=='pos_product_identity'),'unknown')


if __name__=='__main__':unittest.main()
