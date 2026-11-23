"""No live Qince/model calls: verify that unconsumed masters are never queried."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from audit_core.audit_rag import reference_fields
from audit_core.audit_self_check import money_context
from audit_core.qince_context import (APPLICATIONS, ENDPOINTS, PLANS, VERIFICATION,
                                      QinceClient, context_sources)
from audit_core.qince_references import CUSTOMERS, PRODUCTS, SKU_CODES, reference_needs
from audit_core.scenario_registry import SCENARIO_SPECS, BIZ_TYPE_ALIASES, scenario_for_biz_type
from audit_core.stage_context import stage_plan
from tests.test_qince_context import fixture
from tests.test_qince_references import references


def fetch_saved(raw, scenario):
    client = QinceClient('1234567890123456789', 'synthetic-never-send')
    with patch.object(client, 'pages', side_effect=lambda method, params: deepcopy(raw.get(method, []))) as pages, \
         patch('httpx.Client', side_effect=AssertionError('live network forbidden')), \
         patch('httpx.AsyncClient', side_effect=AssertionError('live network forbidden')):
        code = raw[VERIFICATION][0]['verify_code']
        result = client.fetch(code, include_references=True, scenario=scenario)
    return result, pages.call_args_list


class MinimalScopeTests(unittest.TestCase):
    def test_all_eight_scenarios_and_approved_biz_type_aliases_use_the_same_fetch_policy(self):
        expected = {
            'personnel_incentive': set(), 'pos_target_incentive': set(), 'poster_material': set(),
            'entry_fee': {CUSTOMERS}, 'giveaway_promotion': {CUSTOMERS},
            'self_procured_gift_material': {CUSTOMERS}, 'price_difference_support': {CUSTOMERS},
            'promotional_display': {CUSTOMERS, PRODUCTS, SKU_CODES},
        }
        names = [spec.label for spec in SCENARIO_SPECS] + list(BIZ_TYPE_ALIASES)
        self.assertEqual(set(expected), {spec.scenario for spec in SCENARIO_SPECS})
        for index, biz_type in enumerate(names):
            with self.subTest(biz_type=biz_type):
                raw = references()
                raw[VERIFICATION][0]['verify_code'] = f'HX_SCOPE_{index}'
                raw[APPLICATIONS][0]['subList'][0].update(pdRangeType='1', pdCodes='SKU1')
                scenario = scenario_for_biz_type(biz_type)
                result, calls = fetch_saved(raw, scenario)
                self.assertEqual({c.args[0] for c in calls}, {VERIFICATION, APPLICATIONS} | expected[scenario])
                self.assertEqual(result['reference_needs'], sorted(reference_needs(scenario)))
                self.assertEqual(calls[0].args[1], {'verify_code': f'HX_SCOPE_{index}'})

    def test_core_only_policy_is_independent_of_case_number_application_and_row_count(self):
        for scenario in ('personnel_incentive', 'pos_target_incentive', 'poster_material'):
            for count in (1, 3, 17):
                with self.subTest(scenario=scenario, count=count):
                    raw = fixture()
                    code, activity = f'HX_{scenario}_{count}', f'SQ_{scenario}_{count}'
                    row = raw[APPLICATIONS][0]['subList'][0]
                    raw[APPLICATIONS][0].update(applyCode=activity,
                        subList=[{**deepcopy(row), 'subId': str(n), 'pdRangeType': '1'} for n in range(count)])
                    raw[VERIFICATION][0].update(verify_code=code, details=[
                        {'id': f'V{n}', 'activity_code': activity, 'activity_cm_id': str(n)} for n in range(count)])
                    result, calls = fetch_saved(raw, scenario)
                    self.assertEqual([c.args[0] for c in calls], [VERIFICATION, APPLICATIONS])
                    self.assertEqual(calls[1].args[1], {'activity_code': activity})
                    self.assertEqual(result[APPLICATIONS], raw[APPLICATIONS])
                    units, docs = context_sources(result, code, 'a001')
                    self.assertEqual(sum(u['source_file']=='勤策申请明细' for u in units), count)
                    self.assertTrue(all(u['source_kind']=='qince' for u in units))

    def test_generated_stage_plans_match_runtime_fetch_policies_for_every_scenario(self):
        for spec in SCENARIO_SPECS:
            with self.subTest(scenario=spec.scenario):
                saved = json.loads((spec.skill_dir/'references/stage-plan.json').read_text(encoding='utf-8'))
                self.assertEqual(saved, stage_plan(spec.scenario))
                self.assertEqual(saved['qince_auxiliaries'], sorted(reference_needs(spec.scenario)))

    def test_personnel_pos_target_and_poster_only_fetch_core_sources(self):
        raw = fixture()
        raw[APPLICATIONS][0]['subList'][0]['pdRangeType'] = '1'
        for scenario in ('personnel_incentive', 'pos_target_incentive', 'poster_material', None):
            with self.subTest(scenario=scenario):
                result, calls = fetch_saved(raw, scenario)
                self.assertEqual([c.args[0] for c in calls], [VERIFICATION, APPLICATIONS])
                self.assertEqual(result['reference_needs'], [])
                self.assertEqual(result['reference_status'], [])
                self.assertEqual(result[APPLICATIONS], raw[APPLICATIONS])

    def test_inherited_products_keep_exact_scheme_query_without_extra_masters(self):
        raw = fixture()
        result, calls = fetch_saved(raw, 'personnel_incentive')
        self.assertEqual([c.args[0] for c in calls], [VERIFICATION, APPLICATIONS, PLANS])
        self.assertEqual(calls[-1].args[1], {'plan_code': 'CX_TEST'})
        self.assertEqual(result[PLANS], raw[PLANS])
        units, documents = context_sources(result, 'HX_TEST', 'a001')
        self.assertIn('测试商品', json.dumps(documents, ensure_ascii=False))
        self.assertFalse(any(u.get('qince_reference') for u in units))

    def test_saved_hx170008_needs_two_core_queries_not_twenty_four_master_queries(self):
        trace = Path(__file__).resolve().parents[1]/'worktrees'/(
            'HX202610170008-焦作鑫豫达人员激励活动费用申请-8-20261109_1321_20')/'analysis/execution-trace'
        if not trace.exists():
            self.skipTest('Saved original-case traces are private local artifacts.')
        by_endpoint = {endpoint: method for method, endpoint in ENDPOINTS.items()}
        original, response_count = {}, 0
        for path in sorted(trace.glob('step-*.json')):
            step = json.loads(path.read_text(encoding='utf-8'))
            if step.get('type') != 'qince.response':
                continue
            data = step['data']
            original.setdefault(by_endpoint[data['endpoint']], []).extend(data['rows'])
            response_count += 1
        self.assertEqual(response_count, 26)
        result, calls = fetch_saved(original, 'personnel_incentive')
        self.assertEqual([c.args[0] for c in calls], [VERIFICATION, APPLICATIONS])
        before_units, before_docs = context_sources(original, 'HX202610170008', 'a001')
        units, docs = context_sources(result, 'HX202610170008', 'a001')
        # Core source fields/IDs and every actual application value survive.
        ids = {u['unit_id'] for u in units}
        self.assertEqual(docs, [d for d in before_docs if d['unit_id'] in ids])
        self.assertEqual(reference_fields(docs, units), reference_fields(before_docs, before_units))
        amounts = money_context({'units': units, 'documents': docs})
        self.assertEqual(amounts['linked_row_count'], 140)
        self.assertEqual(amounts['linked_rows_sum'], '14000.00')
        self.assertEqual(amounts['approval_states'][0]['fields']['verify_status'], '3')


if __name__ == '__main__':
    unittest.main()
