"""Source-bound activity dates and normal program stages; no real inference."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.activity_periods import read_activity_periods, case_activity_periods, program_activity_checks
from audit_core.audit_rag import reference_comparison
from audit_core.audit_tools import execute_tool
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_policy import rules
from audit_core.submitted_source_checks import application_period_issues
from tests.test_three_claim_source_checks import period_case
from tests.test_audit_rag_contract import case_for


class ActivityPeriodToolTests(unittest.TestCase):
    def test_actual_narrative_and_short_table_field_match_without_borrowing_application(self):
        facts = {'s': ['本次某经销商所做的渠道，于2026年10月10-10月31日，进行产品店内促销。',
                       '项目：市场费用；活动时间：10.10-10.31']}
        before = deepcopy(facts)
        result = read_activity_periods(facts, ['s'])
        self.assertEqual(result['state'], 'available')
        self.assertEqual(result['coverage'], [['2026-10-10', '2026-10-31']])
        self.assertEqual({r['basis'] for r in result['periods']}, {'explicit_activity_period','same_source_activity_year'})
        self.assertTrue(all(s['quote'] == facts[s['unit_id']][s['fact_index']] for p in result['periods'] for s in p['sources']))
        self.assertEqual(facts, before)

    def test_explicit_phases_are_unioned_without_hiding_gaps(self):
        for second, expected in [('2026.6.22', [['2026-06-03','2026-07-07']]),
                                 ('2026.6.24', [['2026-06-03','2026-06-21'],['2026-06-24','2026-07-07']])]:
            with self.subTest(second=second):
                result = read_activity_periods({'s':[f'活动时间：2613档：2026.6.3-2026.6.21；2614档：{second}-2026.7.7']}, ['s'])
                self.assertEqual(result['state'], 'available')
                self.assertEqual(result['coverage'], expected)
                self.assertEqual(len(result['periods']), 2)

    def test_different_versions_are_not_merged_as_phases(self):
        result = read_activity_periods({'s':['活动时间：2026.6.3-2026.6.21','另一活动时间：2026.6.22-2026.7.7']}, ['s'])
        self.assertEqual(result['state'], 'unknown')
        self.assertEqual(len(result['periods']), 2)
        result = read_activity_periods({'s':['活动时间：2613档：2026.6.3-2026.6.21；2613档：2026.6.4-2026.6.22']}, ['s'])
        self.assertEqual(result['state'], 'unknown')

    def test_only_exact_related_contract_can_supply_settlement_year(self):
        facts = {'c':['执行周期：2026.5.21-2026.6.5'], 's':['活动时间：5月21日-6月5日'],
                 'other':['执行周期：2025.5.21-2025.6.5']}
        materials = [{'id':'promotion_contract','state':'present','source_ids':['c']},
                     {'id':'settlement','state':'present','source_ids':['s']}]
        result = case_activity_periods(facts, materials)['settlement']
        self.assertEqual(result['coverage'], [['2026-05-21','2026-06-05']])
        self.assertEqual(result['periods'][0]['basis'], 'related_contract_activity_year')
        self.assertEqual({s['unit_id'] for p in result['periods'] for s in p['sources']}, {'c','s'})
        facts['s'] = ['活动时间：5月22日-6月5日']
        self.assertEqual(case_activity_periods(facts, materials)['settlement']['state'],'unknown')

    def test_signing_payment_other_unit_and_expected_dates_cannot_supply_a_year(self):
        for extra in ['签章日期：2026年6月5日','支付日期：2026年6月5日']:
            with self.subTest(extra=extra):
                result = read_activity_periods({'s':['活动时间：5月21日-6月5日', extra],
                                               'application':['startDate="2026-05-21"','endDate="2026-06-05"']}, ['s'])
                self.assertEqual(result['state'], 'unknown')
        self.assertEqual(read_activity_periods({'s':['活动时间：5月21日-6月5日'],
                                               'x':['执行周期2026.5.21-2026.6.5']}, ['s'])['state'],'unknown')

    def test_false_unreadable_narrative_is_not_a_second_blank_date(self):
        result = read_activity_periods({'s':['执行周期：2026.5.21-2026.6.5',
                                           '在此活动期间，每件奖励2元，合计2000元。']}, ['s'])
        self.assertEqual(result['state'], 'available')
        result = read_activity_periods({'s':['执行周期：2026.5.21-2026.6.5','另一活动时间：未填写']}, ['s'])
        self.assertEqual(result['state'], 'unknown')

    def test_uncertain_invalid_and_reversed_dates_are_not_repaired_into_pass(self):
        for value in ['活动时间疑似2026年5月21日-2026年6月5日', '活动时间：2026.2.30-2026.3.5',
                      '活动时间：2026.6.5-2026.5.21', '活动时间：10.10-10.31']:
            with self.subTest(value=value):
                self.assertEqual(read_activity_periods({'s':[value]}, ['s'])['state'], 'unknown')

    def test_received_check_and_reference_check_use_same_complete_sources(self):
        units, docs, materials = period_case()
        docs[-2]['facts'] = ['执行周期：2026年10月1日-2026年10月31日']
        docs[-1]['facts'] = ['正文写明于2026年10月1-10月31日进行产品促销。','活动时间：10.1-10.31']
        case={'scenario':'personnel_incentive','archive_id':'a001','documents':docs,'materials':materials,'units':units}
        self.assertEqual(application_period_issues(case['scenario'],docs,materials,units), [])
        checks, receipt = program_activity_checks(case)
        self.assertEqual(set(checks), {'contract_activity_dates','settlement_application_dates'})
        self.assertTrue(all(c['status']=='pass' for c in checks.values()))
        before=deepcopy(case)
        with patch('audit_core.model_metrics.save_model_observations') as save:
            tool=execute_tool(case,'activity_periods')
        save.assert_called_once()
        self.assertTrue(all('quote' not in s for p in tool['documents']['settlement']['periods'] for s in p['sources']))
        self.assertEqual(tool['model_calls'],0)
        self.assertEqual(case,before)

    def test_clear_other_period_is_still_a_real_mismatch(self):
        units,docs,materials=period_case()
        docs[-1]['facts']=['活动时间：2613档：2026.10.2-2026.10.15；2614档：2026.10.16-2026.10.30']
        checks,_=program_activity_checks({'scenario':'personnel_incentive','documents':docs,'materials':materials,'units':units})
        self.assertEqual(checks['settlement_application_dates']['status'],'fail')
        self.assertIn('2026-10-02至2026-10-30',checks['settlement_application_dates']['reason'])

    def test_normal_stage_keeps_date_judgments_with_the_model(self):
        from tests.test_audit_rag_capacity import CapacityRecoveryTests
        from tests.test_audit_rag_contract import case_for
        case,_=case_for();runner=CapacityRecoveryTests()
        result=runner._run(case,lambda ids:False)
        self.assertIn('contract_activity_dates',runner.calls[0])
        self.assertEqual(next(c['status'] for c in result['checks'] if c['rule_id']=='contract_activity_dates'),'unknown')


if __name__ == '__main__': unittest.main()
