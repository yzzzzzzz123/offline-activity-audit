"""Exact but optional amount assistance, including the actual worker and self-check paths."""
from contextlib import nullcontext
from copy import deepcopy
from decimal import Decimal
from io import BytesIO
import json
from pathlib import Path
import re
from random import Random
import sys
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.agent_orchestration import expand_tool_sources, scoped_case
from audit_core.amount_calculation import source_amounts
from audit_core.audit_tools import dispatch_requests, execute_tool, initial_amount_advice, tool_protocol
from audit_core.model_authority import execution
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_owned_audit import run_single
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.tool_observations import observations
from tests.pdf_test_support import flags_for


def sample(claim='3,000,401.80', sales=('2407179.10',)):
    def doc(uid, facts):
        return {'unit_id': uid, 'facts': facts, 'roles': [], 'limitations': []}
    total = sum(map(Decimal, sales))
    native = ['POS!R1: C1=序号 | C2=销售金额']
    native.extend(f'POS!R{i}: C1=商品{i} | C2={v}' for i, v in enumerate(sales, 2))
    native.append(f'POS!R{len(native)+1}: C1=合计 | C2={total}')
    docs = [doc('excel', native), doc('claim', ['奖励金额：1 元', 'POS销售金额：'+claim+'元']),
            doc('agreement', [])]
    materials = [{'id': role, 'state': 'present', 'reason': '', 'source_ids': [d['unit_id']]}
                 for role, d in zip(('pos_excel', 'settlement', 'promotion_contract'), docs)]
    return {'archive_id': 'a001', 'scenario': 'pos_target_incentive', 'flags': flags_for('pos_target_incentive'),
            'documents': docs, 'materials': materials,
            'units': [{'unit_id': d['unit_id'], 'file_id': d['unit_id'], 'source_file': d['unit_id'],
                       'locator': d['unit_id'], 'source_kind': 'fixture'} for d in docs]}


def read(case, kind):
    return [c for c in source_amounts(case)['calculations'] if c['kind'] == kind]


class AmountAssistanceTests(TestCase):
    def test_native_sum_compared_only_to_settlement_pos_sales(self):
        case = sample(sales=('2400000', '7179.10'))
        result, = read(case, 'pos_sales_difference')
        self.assertEqual(Decimal(result['settlement_minus_pos']), Decimal('593222.70'))
        self.assertEqual(result['row_count'], 2)
        self.assertEqual(result['pos_source_ids'], ['excel'])
        self.assertEqual(result['settlement_source_id'], 'claim')
        self.assertNotIn('奖励金额', result['settlement_quote'])

    def test_signed_difference_across_200_source_totals_including_zero_returns(self):
        rng = Random(20261124)
        for index in range(200):
            values = [Decimal(rng.randrange(-1000000, 1000000)).scaleb(-2) for _ in range(1 + index % 7)]
            delta = Decimal(0) if index % 5 == 0 else Decimal(rng.randint(-10000, 10000)).scaleb(-2)
            with self.subTest(index=index):
                case = sample(f'{sum(values)+delta:,.2f}', tuple(map(str, values)))
                self.assertEqual(Decimal(read(case, 'pos_sales_difference')[0]['settlement_minus_pos']), delta)

    def test_ambiguous_missing_foreign_or_nonyuan_sales_are_not_combined(self):
        for change in ('multiple_amounts', 'missing', 'foreign', 'currency', 'scale', 'bad_group',
                       'subtotal_only', 'role_overlap', 'unread_sheet'):
            with self.subTest(change=change):
                case = sample()
                if change == 'multiple_amounts': case['documents'][1]['facts'].append('POS销售金额：42元')
                if change == 'missing': case['documents'][1]['facts'] = ['实际核销金额：3000401.80元']
                if change == 'foreign':
                    case['documents'].append({'unit_id': 'other', 'facts': ['POS销售金额：42元']})
                    case['materials'][1]['source_ids'].append('other')
                if change == 'currency': case['documents'][1]['facts'].append('币种：USD')
                if change == 'scale': case['documents'][0]['facts'][0] = 'POS!R1: C1=序号 | C2=销售金额（万元）'
                if change == 'bad_group': case['documents'][1]['facts'] = ['POS销售金额 3,00,40.10元']
                if change == 'subtotal_only': case['documents'][0]['facts'].pop()
                if change == 'role_overlap': case['materials'][1]['source_ids'].append('excel')
                if change == 'unread_sheet': case['documents'][0]['facts'].append('无法解析的附加表行')
                self.assertEqual(read(case, 'pos_sales_difference'), [])

    def test_independent_contract_and_settlement_multiplications(self):
        case = sample()
        case['documents'][2]['facts'] = ['合同厂价5.6元(SP-1厂价)*2500支=15000元']
        case['documents'][1]['facts'] += ['明细｜搭赠数量2500｜搭赠单价5.7｜搭赠金额14000']
        contract, = read(case, 'printed_product')
        settlement, = read(case, 'gift_line_amount')
        self.assertEqual((contract['role'], Decimal(contract['calculated'])), ('promotion_contract', 14000))
        self.assertEqual((settlement['role'], Decimal(settlement['calculated'])), ('settlement', 14250))
        for fact in ('费率 3 × 4 × 10=120元', '假设3×4=12万元', '涉及商品SKU12×3=36',
                     '比例3×4=12.5×2', '3×4=2,00元'):
            with self.subTest(fact=fact):
                case['documents'][2]['facts'] = [fact]
                self.assertEqual(read(case, 'printed_product'), [])

    def test_reward_precision_and_payroll_units(self):
        case = sample()
        case['documents'][1]['facts'] = ['表格行4：奖励标准：7044773.5*0.025', '实际核销金额：150000元']
        reward, = read(case, 'printed_reward_basis')
        self.assertEqual(reward['unrounded_reward'], '176119.3375')
        self.assertEqual(reward['nearest_whole_yuan'], '176119')
        for rate, expected in [('2.5%', '176119.3375'), ('2.5％', '176119.3375')]:
            case['documents'][1]['facts'] = ['奖励标准：7044773.5×'+rate]
            self.assertEqual(read(case, 'printed_reward_basis')[0]['unrounded_reward'], expected)
        case['scenario'] = 'personnel_incentive'
        case['documents'][1]['facts'] = ['表格行：人数 1（人）','表格行：天数 3天','表格行：单日工资 200（元）/天']
        wages, = read(case, 'staff_wage')
        self.assertEqual(wages['wages'], '600')
        self.assertEqual(wages['operands']['people']['number'], '1')
        case['documents'][1]['facts'].append('人数 2人')
        self.assertEqual(read(case, 'staff_wage'), [])

    def test_requested_arithmetic_is_quoted_and_cannot_read_another_case(self):
        case = sample()
        case['documents'][2]['facts'] = ['每支3.15元','促销2520支','合同列出7900元']
        def operand(quote, number):
            return {'unit_id': 'agreement', 'quote': quote, 'number': number}
        op = {'value_kind': 'amount', 'operation': 'product', 'operator': 'eq',
              'left': operand('合同列出7900元','7900'), 'right': [operand('每支3.15元','3.15'), operand('促销2520支','2520')]}
        req = {'tool': 'amount_calculation', 'rule_ids': ['contract_budget_amount'], 'fields': [], 'comparisons': [op]}
        with execution():
            result = dispatch_requests(case, [req], ['contract_budget_amount'])[0]['result']
            self.assertEqual(Decimal(result['calculations'][0]['left_minus_calculated']), -38)
            for key, bad in [('quote','虚构的文本2520'), ('unit_id','other-case'), ('number','2500')]:
                with self.subTest(key=key):
                    invalid = deepcopy(req)
                    invalid['comparisons'][0]['right'][1][key] = bad
                    self.assertEqual(dispatch_requests(case, [invalid], ['contract_budget_amount'])[0]['result']['status'],
                                     'model_takeover')
            for quote, truncated in [('促销252', '252'), ('520支', '520')]:
                invalid = deepcopy(req)
                invalid['comparisons'][0]['right'][1] = operand(quote, truncated)
                self.assertEqual(dispatch_requests(case, [invalid], ['contract_budget_amount'])[0]['result']['status'],
                                 'model_takeover')

    def test_worker_and_self_check_complete_with_real_tool_or_failing_tool(self):
        case = scoped_case(sample(), 'numbers')
        case['_agent_rule_specs'] = [s for s in case['_agent_rule_specs'] if s['id'] == 'pos_sales_amount']
        for failure in (None, TimeoutError('timeout'), OSError('read error'), RuntimeError('unexpected failure'),
                        ImportError('optional helper missing')):
            with self.subTest(failure=failure), TemporaryDirectory() as folder, execution(), \
                 collect_model_artifacts(Path(folder)/'analysis'):
                provider = PdfEvidenceProvider('fixture','medium')
                seen = []
                def respond(root, skill, schema, prompt, **kwargs):
                    data = json.loads(prompt.partial_variables['request'])
                    seen.append(data)
                    self.assertIn('POS销售金额', json.dumps(data['documents'], ensure_ascii=False))
                    check = {'rule_id': 'pos_sales_amount', 'status': 'fail', 'reason': '结算比明细高593222.70元。',
                             'source_ids': ['claim', 'excel']}
                    return {'checks':[check], **({'ready':True,'notes':[]} if 'model_result' in data else {})}
                faulty = patch('audit_core.amount_calculation.source_amounts', side_effect=failure) if failure else nullcontext()
                with faulty, patch('audit_core.pdf_materials.prepare_pos_identities', return_value={}), \
                     patch.object(provider,'_call',side_effect=respond):
                    result = run_single(provider, deepcopy(case), Path(folder))
                self.assertEqual(len(seen), 2, 'one amount decision and one normal self-check, without forced tool retry')
                for request in seen:
                    self.assertEqual(request['amount_assistance']['status'], 'model_takeover' if failure else 'observed')
                self.assertEqual(result['checks'][0]['reason'], '结算比明细高593222.70元。')

    def test_optional_storage_or_discovery_errors_also_leave_raw_evidence_with_worker(self):
        case = sample()
        before = deepcopy(case)
        for target in ('audit_core.tool_observations.remember','audit_core.audit_tools._amount_hint_applicable'):
            with self.subTest(target=target), patch(target,side_effect=OSError('injected')):
                self.assertEqual(initial_amount_advice(case)['status'], 'model_takeover')
        self.assertEqual(case, before)
        with patch.dict(sys.modules, {'audit_core.amount_calculation': None}):
            self.assertEqual(initial_amount_advice(case)['status'], 'model_takeover')
        with patch('audit_core.amount_calculation.monotonic', side_effect=[0, 4]):
            self.assertEqual(initial_amount_advice(case)['error_type'], 'TimeoutError')
        large = deepcopy(case)
        large['documents'][0]['facts'].append('量' * 2_000_001)
        self.assertEqual(initial_amount_advice(large)['status'], 'model_takeover')
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            execute_tool(case, 'amount_calculation')
            self.assertTrue(any(o['request']['tool'] == 'amount_calculation' for o in observations(case)))
        self.assertNotIn('amount_calculation', tool_protocol(['display_size']))
        self.assertIn('amount_calculation', tool_protocol(['pos_sales_amount']))
        self.assertIsNone(initial_amount_advice({'documents': [], 'materials': [], 'units': []}))

    def test_explicit_tool_can_request_independent_originals_from_current_case_only(self):
        from unittest.mock import Mock
        worker = scoped_case(sample(), 'numbers')
        provider = Mock()
        provider._agent_expand_sources.side_effect = lambda current, ids: ids
        req = {'tool': 'amount_calculation', 'comparisons': [
            {'left': {'unit_id':'claim'}, 'right': [{'unit_id':'excel'}, {'unit_id':'foreign'}]},
            {'right': 'malformed'}, 12]}
        self.assertEqual(expand_tool_sources(provider, worker, [req]), ['claim', 'excel'])

    def test_pos_parser_checks_deadline_inside_long_worksheet(self):
        from audit_core import pos_arithmetic
        case = sample(sales=('10.05',) * 400)
        read_cell = pos_arithmetic.parse_cells
        time_and_rows = {'now': 10.0, 'rows': 0}
        def slow_row(row):
            time_and_rows['rows'] += 1
            time_and_rows['now'] += 1.0
            return read_cell(row)
        with patch('audit_core.amount_calculation.monotonic',side_effect=lambda: time_and_rows['now']), \
             patch.object(pos_arithmetic,'parse_cells',side_effect=slow_row):
            result = initial_amount_advice(case)
        self.assertEqual(result['error_type'], 'TimeoutError')
        self.assertEqual(time_and_rows['rows'], 4)

    def test_same_failing_tool_is_tried_once_across_worker_and_self_check(self):
        case = scoped_case(sample(), 'numbers')
        case['_agent_rule_specs'] = [s for s in case['_agent_rule_specs'] if s['id'] == 'pos_sales_amount']
        provider = PdfEvidenceProvider('fixture','medium')
        counts = {'worker': 0, 'self_check': 0}
        def respond(root, skill, schema, prompt, **kwargs):
            data = json.loads(prompt.partial_variables['request'])
            phase = 'self_check' if 'model_result' in data else 'worker'
            counts[phase] += 1
            self.assertLessEqual(counts[phase], 2)
            check = {'rule_id':'pos_sales_amount','status':'fail','reason':f'独立复核{counts[phase]}，差额593222.70元。',
                     'source_ids':['claim','excel']}
            result = {'checks':[check], 'tool_requests':[{'tool':'amount_calculation','fields':[],
                                                       'rule_ids':['pos_sales_amount']}]}
            if phase == 'self_check':
                result.update(ready=False, notes=[])
            return result
        with TemporaryDirectory() as folder, execution(), \
             patch('audit_core.amount_calculation.source_amounts',side_effect=OSError('tool error')) as attempt, \
             patch('audit_core.pdf_materials.prepare_pos_identities',return_value={}), patch.object(provider,'_call',side_effect=respond):
            result = run_single(provider,case,Path(folder))
        self.assertEqual(attempt.call_count,1)
        self.assertEqual(counts,{'worker':2,'self_check':2})
        self.assertEqual(result['checks'][0]['status'],'fail')

    def test_expanded_source_scope_and_changed_operands_can_be_used_after_failure(self):
        case = sample()
        case['_amount_request_cache'] = {}
        with patch('audit_core.amount_calculation.source_amounts',side_effect=TimeoutError()) as attempt:
            execute_tool(case, 'amount_calculation')
            execute_tool(case, 'amount_calculation')
            self.assertEqual(attempt.call_count, 1)
            case['documents'].append({'unit_id':'new', 'facts':['金额10元']})
            execute_tool(case, 'amount_calculation')
            self.assertEqual(attempt.call_count, 2)
            execute_tool(case, 'amount_calculation', comparisons=[{'operation':'sum'}])
            self.assertEqual(attempt.call_count, 3)

    def test_upload_to_customer_summary_with_optional_helper_online_and_offline(self):
        from openpyxl import Workbook
        from audit_core.pdf_workflow import PdfWorkflow
        from audit_core.scenario_registry import SCENARIO_LABELS
        from tests.pdf_test_support import bundle, classification, image_bytes
        scenario = 'pos_target_incentive'
        for offline in (False, True):
            with self.subTest(offline=offline), TemporaryDirectory() as folder:
                root = Path(folder)
                book = Workbook()
                book.active.append(['序号','销售金额'])
                book.active.append(['商品',2407179.10])
                book.active.append(['合计',2407179.10])
                excel = BytesIO()
                book.save(excel)
                bundle(root/'input', files={'pos.xlsx': excel.getvalue(), '结算.png': image_bytes()})
                provider = PdfEvidenceProvider('fixture','medium',agent_mode='single')
                case = sample()
                def read(archive, *args):
                    return [{'unit_id':u['unit_id'], 'roles':[], 'limitations':[],
                             **({'sheet_layout':u['sheet_layout']} if 'sheet_layout' in u else {}),
                             'facts':u['native_facts'] if u['source_file'].endswith('.xlsx') else case['documents'][1]['facts']}
                            for u in archive['units']]
                def model(stage, skill, schema, prompt, **kwargs):
                    variables = prompt.partial_variables
                    if 'candidate_scenarios' in schema['properties']:
                        docs = json.loads(variables['documents'])
                        pos_id = next(d['unit_id'] for d in docs if '销售金额' in d['facts'][0])
                        claim_id = next(d['unit_id'] for d in docs if d['unit_id'] != pos_id)
                        value = classification(scenario, [pos_id, claim_id])
                        match = next(m for m in value['material_matches'] if m['scenario'] == scenario)
                        for m in match['materials']:
                            m['source_ids'] = [pos_id if m['id'] == 'pos_excel' else claim_id]
                        value.update(materials=match['materials'], material_matches=[match])
                    else:
                        data = json.loads(variables.get('request', '{}'))
                        if kwargs['label'].endswith('模型自主审核'):
                            hint = data['amount_assistance']
                            if offline:
                                self.assertEqual(hint['status'], 'model_takeover')
                                raw = [f for d in data['documents'] for f in d['facts']]
                                left = Decimal(next(re.search(r'POS销售金额：([\d,.]+)', f)[1].replace(',','')
                                                    for f in raw if f.startswith('POS销售金额：')))
                                right = Decimal(next(re.search(r'C2=([\d.]+)', f)[1]
                                                     for f in raw if 'C1=合计' in f))
                                difference = left - right
                            else:
                                difference = Decimal(hint['calculations'][0]['settlement_minus_pos'])
                            value = {'checks':[{'rule_id':'pos_sales_amount','status':'fail',
                                     'reason':f'POS明细比结算单少{difference:.2f}元。',
                                     'source_ids':[d['unit_id'] for d in data['documents']]}]}
                        elif 'model_result' in data:
                            value = {'ready':True, 'notes':[], 'checks':data['model_result']['checks']}
                        elif 'items' in schema['properties']:
                            value = {'ready':True, 'issues':[], 'items':[
                                {'source_problem_ids':[p['id']], 'text':p['text'], 'uncertainty_preserved':True}
                                for p in data['problems']]}
                        else:
                            value = {'ready':True, 'findings':[], 'notes':[]}
                    kwargs['validator'](value)
                    return value
                flow = PdfWorkflow('amount-tool-flow','fixture', root/'input', root/'model', provider,
                                   None, None, biz_type=SCENARIO_LABELS[scenario])
                failure = patch('audit_core.amount_calculation.source_amounts',side_effect=TimeoutError()) if offline else nullcontext()
                with failure, patch.object(provider,'_read_documents',side_effect=read), \
                     patch.object(provider,'_call',side_effect=model), \
                     patch('audit_core.pdf_materials.prepare_product_references',return_value=({}, [])), \
                     patch('audit_core.pdf_materials.prepare_pos_identities',return_value={}), \
                     patch('audit_core.pdf_materials.template_comparison',return_value=({}, [])):
                    receipt = flow.run()
                self.assertIn('593222.70元', receipt['reviewed_analysis_summary'])
                self.assertFalse(receipt['verification']['pdf_policy']['deterministic_business_gates'])
