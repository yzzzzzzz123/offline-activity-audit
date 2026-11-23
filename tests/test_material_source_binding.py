"""File-to-worksheet routing and POS lookups stay independent of missing prices."""
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from audit_core.agent_orchestration import scoped_case, expand_sources, expand_tool_sources
from audit_core.audit_tools import execute_tool
from audit_core.common import AuditError
from audit_core.material_sources import material_source_ids, resolve_source_ids
from audit_core.model_authority import execution
from audit_core.native_pos_identity import native_identity_extractions
from audit_core.pos_product_identity import prepare_pos_identities, lookup_pos_products
from tests.test_native_pos_identity import native_case
from tests.test_pos_product_identity import catalog
from tests.test_agent_orchestration import FixtureProvider, fixture_case, offline_helpers


def file_case(scenario='poster_material'):
    case = native_case(scenario, count=12, chunk=5)
    case['materials'] = [{'id': 'pos_excel', 'state': 'unclear',
        'reason': '已提供Excel，缺少销售单价；名称与69码可读。', 'source_ids': ['original-pos']}]
    for document, unit in zip(case['documents'], case['units']):
        document['facts'] = [fact.replace(' | C4=销售单价', '').replace(' | C4=1.005', '')
                             for fact in document['facts']]
        unit['native_facts'] = list(document['facts'])
    return case


class MaterialSourceBindingTests(TestCase):
    def test_file_reference_expands_all_chunks_using_explicit_file_metadata_only(self):
        case = file_case()
        expected = [document['unit_id'] for document in case['documents']]
        unrelated = {'unit_id': 'original-pos-table-foreign', 'roles': ['spreadsheet'],
                     'facts': ['FOREIGN_SOURCE'], 'limitations': []}
        case['documents'].append(unrelated)
        case['units'].append({'unit_id': unrelated['unit_id'], 'file_id': 'another-file',
                              'source_file': '原始POS.xlsx'})
        original = deepcopy(case)
        self.assertEqual(resolve_source_ids(case, ['original-pos', expected[0], 'missing']), expected)
        self.assertEqual(material_source_ids(case, 'pos_excel'), expected)
        self.assertEqual(case, original)

    def test_parent_reference_routes_raw_pos_to_numbers_without_changing_material_status(self):
        for scenario in ('poster_material', 'personnel_incentive', 'giveaway_promotion',
                         'price_difference_support', 'pos_target_incentive', 'entry_fee',
                         'promotional_display', 'self_procured_gift_material'):
            with self.subTest(scenario=scenario):
                case = file_case(scenario)
                numbers = scoped_case(case, 'numbers')
                self.assertEqual(numbers['documents'], case['documents'])
                self.assertEqual(numbers['materials'], case['materials'])
                self.assertFalse(scoped_case(case, 'visual')['documents'])
                self.assertEqual(numbers['_agent_source_catalog'][0]['roles'], ['pos_excel', 'spreadsheet'])

    def test_missing_price_and_unclear_inventory_still_query_every_original_barcode(self):
        case = file_case(); original = deepcopy(case)
        model = Mock(side_effect=AssertionError('native name/barcode columns need no model'))
        with TemporaryDirectory() as directory, \
             patch('audit_core.pos_product_identity.load_product_catalog', return_value=catalog()) as read_db:
            result = prepare_pos_identities(scoped_case(case, 'numbers'), Path(directory), model)
        model.assert_not_called(); read_db.assert_called_once()
        self.assertEqual(len(result['rows']), 12)
        self.assertTrue(all(row['status'] == 'pass' for row in result['rows']))
        self.assertEqual(case, original)

    def test_targeted_lookup_accepts_actual_pos_units_and_keeps_source_quotes(self):
        case = file_case()
        extracted = native_identity_extractions(case['documents'], case['units'])
        uid = case['documents'][0]['unit_id']
        product = {'unit_id': uid, **deepcopy(extracted[uid]['extracted']['rows'][0])}
        with patch('audit_core.pos_product_identity.load_product_catalog', return_value=catalog()):
            result = lookup_pos_products(case, [product])
        self.assertEqual(result['rows'][0]['status'], 'pass')
        self.assertEqual(result['coverage'], 'requested_rows_only')
        self.assertEqual(result['rows'][0]['source'], extracted[uid]['extracted']['rows'][0])
        with self.assertRaises(AuditError):
            lookup_pos_products(case, [{**product, 'unit_id': 'another-run-source'}])

    def test_absent_material_and_similar_id_do_not_grant_pos_access(self):
        case = file_case()
        case['materials'][0]['state'] = 'absent'
        self.assertEqual(material_source_ids(case, 'pos_excel'), [])
        case['materials'][0].update(state='unclear', source_ids=['original'])
        self.assertEqual(material_source_ids(case, 'pos_excel'), [])

    def test_no_bound_sources_reports_internal_not_executed_state(self):
        case = file_case(); case['materials'][0]['source_ids'] = ['unrelated-id']
        with TemporaryDirectory() as directory, \
             patch('audit_core.pos_product_identity.load_product_catalog') as read_db:
            result = prepare_pos_identities(case, Path(directory), Mock())
        read_db.assert_not_called()
        self.assertFalse(result['complete'])
        self.assertIn('未执行商品查询', result['instruction'])
        self.assertIn('内部来源状态', result['instruction'])

    def test_tool_source_request_loads_only_catalogued_same_case_sources(self):
        full = file_case(); scoped = scoped_case(full, 'numbers')
        missing = scoped['documents'].pop()
        scoped['units'] = [unit for unit in scoped['units'] if unit['unit_id'] != missing['unit_id']]
        provider = SimpleNamespace(_agent_expand_sources=lambda target, requested: expand_sources(target, full, requested))
        request = {'tool': 'case_python', 'source_ids': [missing['unit_id']],
                   'code': f'result = len(source({missing["unit_id"]!r})["facts"])'}
        self.assertEqual(expand_tool_sources(provider, scoped, [request]), [missing['unit_id']])
        with TemporaryDirectory(), execution():
            result = execute_tool(scoped, **{key: value for key, value in request.items() if key != 'tool'}, name='case_python')
        self.assertTrue(result['complete'], result)
        self.assertEqual(result['result'], len(missing['facts']))
        self.assertEqual(expand_tool_sources(provider, scoped, [{'source_ids': ['other-run']}]), [])
        with self.assertRaises(AuditError):
            execute_tool(scoped, 'case_python', source_ids=['other-run'], code='result=1')

    def test_lookup_product_unit_id_is_also_expanded_before_dispatch(self):
        full = file_case(); scoped = scoped_case(full, 'numbers')
        missing = scoped['documents'].pop()
        scoped['units'] = [unit for unit in scoped['units'] if unit['unit_id'] != missing['unit_id']]
        provider = SimpleNamespace(_agent_expand_sources=lambda target, requested: expand_sources(target, full, requested))
        self.assertEqual(expand_tool_sources(provider, scoped,
            [{'tool': 'pos_product_lookup', 'products': [{'unit_id': missing['unit_id']}]}]), [missing['unit_id']])
        self.assertEqual(scoped['documents'][-1], missing)

    def test_audit_dispatch_fetches_catalogued_tool_source_before_execution(self):
        from audit_core.model_owned_audit import run_single
        full = fixture_case(); case = scoped_case(full, 'visual')
        provider = FixtureProvider(); provider._agent_id = 'visual'
        provider._agent_expand_sources = lambda target, requested: expand_sources(target, full, requested)
        value = {'checks': [{'rule_id': 'photo_authenticity', 'status': 'unknown',
                            'reason': '原始图片仍有遮挡。', 'source_ids': ['display_photos']}]}
        requests = []
        def reply(root, skill, schema, prompt, **kwargs):
            requests.append(json.loads(prompt.partial_variables['request']))
            return {**deepcopy(value), **({'tool_requests': [{'tool': 'case_python',
                'source_ids': ['pos_excel'], 'code': "result = len(source('pos_excel')['facts'])"}]}
                if len(requests) == 1 else {})}
        with TemporaryDirectory() as folder, execution(), ExitStack() as stack:
            offline_helpers(stack)
            stack.enter_context(patch.object(provider, '_call', side_effect=reply))
            stack.enter_context(patch.object(provider, '_review_stage_summary',
                                            side_effect=lambda case, result, *a, **kw: result))
            result = run_single(provider, case, Path(folder))
        self.assertEqual(result, value)
        self.assertEqual(len(requests), 2)
        self.assertNotIn('pos_excel', {d['unit_id'] for d in requests[0]['documents']})
        self.assertEqual(next(d for d in requests[1]['documents'] if d['unit_id'] == 'pos_excel'),
                         full['documents'][2])
        self.assertTrue(requests[1]['tools'][0]['result']['complete'])
        self.assertEqual(requests[1]['tools'][0]['result']['result'], 150)

    def test_self_check_dispatch_fetches_catalogued_tool_source_before_execution(self):
        from audit_core.model_self_check import review_stage
        full = fixture_case(); case = scoped_case(full, 'visual')
        provider = FixtureProvider(); provider._agent_id = 'visual'
        provider._agent_expand_sources = lambda target, requested: expand_sources(target, full, requested)
        value = {'checks': [{'rule_id': 'photo_authenticity', 'status': 'unknown',
                            'reason': '原始图片仍有遮挡。', 'source_ids': ['display_photos']}]}
        requests = []
        def reply(root, skill, schema, prompt, **kwargs):
            requests.append(json.loads(prompt.partial_variables['request']))
            return {**deepcopy(value), 'ready': len(requests) > 1, 'notes': [],
                **({'tool_requests': [{'tool': 'case_python', 'source_ids': ['pos_excel'],
                       'code': "result = len(source('pos_excel')['facts'])"}]}
                   if len(requests) == 1 else {})}
        with TemporaryDirectory() as folder, execution(), ExitStack() as stack:
            offline_helpers(stack)
            stack.enter_context(patch.object(provider, '_call', side_effect=reply))
            result = review_stage(provider, case, value, value, Path(folder),
                                  ['photo_authenticity'], lambda v, ids: v)
        self.assertEqual(result, value)
        self.assertEqual(len(requests), 2)
        self.assertEqual(next(d for d in requests[1]['documents'] if d['unit_id'] == 'pos_excel'),
                         full['documents'][2])
        self.assertTrue(requests[1]['tools'][0]['result']['complete'])
        self.assertEqual(requests[1]['tools'][0]['result']['result'], 150)
