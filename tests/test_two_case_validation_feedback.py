"""Regressions for normal local uploads; all examples and model calls are synthetic."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.display_source_evidence import DisplaySourceError, validate_display_sources
from audit_core.fee_policy_checks import validate_workbook_comparison
from audit_core.pdf_evidence import audit_decision, calculate
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_display_source_evidence import fixture, check


class LocalUploadValidationFeedbackTests(unittest.TestCase):
    def test_missing_approval_is_unknown_for_sales_and_not_a_business_exemption(self):
        flags = flags_for('personnel_incentive', uses_pos=False)
        value = unknown_checks('personnel_incentive', flags)
        target = next(c for c in value['checks'] if c['rule_id'] == 'claim_ceiling')
        target.update(status='not_applicable', reason='没有取得系统批复金额。')
        original = deepcopy(value)
        result=audit_decision('personnel_incentive', flags, value, [], [], units=[])
        self.assertEqual(next(c for c in result['checks'] if c['rule_id']=='claim_ceiling')['status'],'not_applicable')
        self.assertEqual(value, original)
        target['status'] = 'unknown'
        result = audit_decision('personnel_incentive', flags, value, [], [], units=[])
        self.assertEqual(next(c for c in result['checks'] if c['rule_id'] == 'claim_ceiling')['status'], 'not_applicable')

    def test_normal_audit_prompt_delivers_conditions_without_prejudging_applicability(self):
        for overrides in ({},{'training':True},{'temporary_staff':True},{'sales_commission':None}):
            with self.subTest(overrides=overrides),tempfile.TemporaryDirectory() as directory:
                flags=flags_for('personnel_incentive',uses_pos=False,**overrides)
                provider=PdfEvidenceProvider(None,None,stage_summary_reviewer=isolated_stage_summary)
                case={'scenario':'personnel_incentive','archive_id':'synthetic','flags':flags,'documents':[],'materials':[],'units':[]}
                with patch.object(provider,'_call',return_value=unknown_checks('personnel_incentive',flags)) as model:
                    provider.audit(case,Path(directory))
                request=json.loads(model.call_args.args[3].partial_variables['request'])
                rule=next(r for r in request['rules'] if r['id']=='claim_ceiling')
                self.assertNotIn('applicable',rule)
                self.assertEqual(request['flags'],flags)
                self.assertIn('审批前的AI核销不以最终审批完成为前置',model.call_args.args[3].format())

    def test_equal_quantities_on_distinct_rows_are_supported_but_repeated_quote_is_rejected(self):
        rows = ['POS!R2: C1=甲店 | C5=52', 'POS!R3: C1=乙店 | C5=52']
        comparison = {'label': '两店销量合计', 'value_kind': 'quantity', 'operation': 'sum', 'operator': 'eq',
                      'left': {'unit_id': 's', 'quote': '结算申报104', 'number': '104'},
                      'right': [{'unit_id': 'p', 'quote': row, 'number': '52'} for row in rows]}
        docs = {'s': {'facts': ['结算申报104']}, 'p': {'facts': rows}}
        validate_workbook_comparison('personnel_incentive', 'settlement_pos_quantity', comparison, [], [])
        self.assertTrue(calculate(comparison, docs)['matches'])
        comparison['right'][1] = deepcopy(comparison['right'][0])
        before = deepcopy(comparison)
        with self.assertRaises(AuditError) as error:
            validate_workbook_comparison('personnel_incentive', 'settlement_pos_quantity', comparison, [], [])
        self.assertIn('settlement_pos_quantity', str(error.exception))
        self.assertIn('工作表和行定位', str(error.exception))
        self.assertEqual(comparison, before)

    def test_store_object_before_uncertainty_is_not_a_positive_address_claim(self):
        docs, materials, units = fixture()
        for reason in ('地址对应申请活动门店无法确认。', '地址对应本次合同门店仍无法核实。',
                       '地址对应活动门店尚未确认。', '地址对应本次申请活动门店的身份不能确认。'):
            with self.subTest(reason=reason):
                value = check(reason)
                before = deepcopy((value, docs, materials, units))
                validate_display_sources(value, docs, materials, units, path='checks/0')
                self.assertEqual((value, docs, materials, units), before)
                self.assertEqual(value['status'], 'unknown')

    def test_uncertainty_cannot_cover_an_address_affirmation_or_missing_attachment_claim(self):
        docs, materials, units = fixture()
        for reason in ('地址对应申请活动门店，面积无法确认。',
                       '地址对应申请活动门店已确认面积无法确认。',
                       '地址对应申请活动门店无法确认，但另一地址一致。',
                       '地址对应申请活动门店无法确认；附件未在本图出现。'):
            with self.subTest(reason=reason), self.assertRaises(DisplaySourceError):
                validate_display_sources(check(reason), docs, materials, units, path='checks/0')


if __name__ == '__main__':
    unittest.main()
