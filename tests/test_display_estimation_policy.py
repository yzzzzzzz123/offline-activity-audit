"""Approved approximate display sizing and auxiliary terminal-address policy.

These are prompt-delivery and simulated-model checks, not real-photo validation.
"""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch

from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision, audit_schema, photo_review_schema, validate
from audit_core.common import AuditError
from audit_core.customer_language import CUSTOMER_PROBLEM_STYLE
from audit_core.fee_policy_checks import constrain_application_check
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.pdf_policy import (
    DISPLAY_SIZE_ESTIMATION_CLARIFICATION, LOCATION_REFERENCE_CLARIFICATION, rules,
)
from audit_core.qince_context import POLICY
from tests.pdf_test_support import unknown_checks
from tests.test_optional_staff_and_display_photos import photo_case, review


class DisplayEstimationPolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.case = photo_case(self.root, 1)

    def decide(self, row):
        evidence = {**unknown_checks('promotional_display', self.case['flags']), 'photo_reviews': [row]}
        original = deepcopy(evidence)
        result = audit_decision('promotional_display', self.case['flags'], evidence,
                                self.case['documents'], self.case['materials'], units=self.case['units'])
        self.assertEqual(evidence, original)
        return result['photo_reviews'][0]['checks']

    def test_real_photo_prompt_allows_estimates_and_does_not_demand_full_address(self):
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        def call(root, skill, schema, prompt, *, images, label, validator):
            text = render_prompt(prompt)
            self.assertIn(DISPLAY_SIZE_ESTIMATION_CLARIFICATION, text)
            self.assertIn(LOCATION_REFERENCE_CLARIFICATION, text)
            self.assertIn(CUSTOMER_PROBLEM_STYLE, text)
            self.assertIn('不要求逐字同名', text)
            self.assertIn('客户摘要只说本图无法对应哪家活动门店', text)
            self.assertIn('沿宽/深方向可见的数量', text)
            self.assertIn('不能把无法精确测量写成已确认尺寸不达标', text)
            self.assertIn('不因缺少完整地址继续报身份或位置无法确认', text)
            self.assertNotIn('不能假定地砖、纸箱或商品的标准尺寸', text)
            self.assertNotIn('同名分店须逐候选核对本图可见的完整水印地址', text)
            self.assertIn('最外层JSON对象必须且只能含photo_reviews数组', text)
            self.assertIn(json.dumps({'photo_reviews':[{'unit_id':'p01','checks':[]}]}, ensure_ascii=False), text)
            value = {'photo_reviews': [review('p01')]}
            validator(value)
            return value
        with patch.object(provider, '_call', side_effect=call):
            provider._audit_display_photos(self.case, self.root, [], [])

    def test_final_audit_receives_the_same_policy(self):
        class Captured(Exception): pass
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        def call(root, skill, schema, prompt, **kwargs):
            text = render_prompt(prompt)
            self.assertIn(DISPLAY_SIZE_ESTIMATION_CLARIFICATION, text)
            self.assertIn(LOCATION_REFERENCE_CLARIFICATION, text)
            self.assertIn(CUSTOMER_PROBLEM_STYLE, text)
            raise Captured()
        with patch.object(provider, '_audit_display_photos', return_value=[review('p01')]), patch.object(
                provider, '_call', side_effect=call), patch(
                'audit_core.pdf_materials.prepare_product_references', return_value=({}, [])), patch(
                'audit_core.pdf_materials.prepare_pos_identities', return_value={}), patch(
                'audit_core.pdf_materials.template_comparison', return_value=([], [])):
            with self.assertRaises(Captured):
                provider.audit(self.case, self.root)

    def test_approximate_sizing_does_not_force_an_unknown_or_rewrite_ai(self):
        row = review('p01')
        row['checks'][3].update(status='pass', reason=(
            '底部未入镜，但柜体台面和侧面轮廓能合理对应占地投影；按可见同规格包装常见尺寸区间及宽深排列估算，范围支持合同面积；'
            '包装尺寸为估算假设，不是本次实测。'))
        self.assertEqual(self.decide(row)[3]['status'], 'pass')

    def test_occluded_depth_remains_unknown_and_obvious_shortfall_remains_fail(self):
        for status, reason in (
                ('unknown', '正面可见四排，但底部深度及右侧边界被遮挡，估算区间仍跨越合同要求。'),
                ('fail', '本图完整边界和对应包装参照支持的占地估算明显小于合同要求。')):
            with self.subTest(status=status):
                row = review('p01')
                row['checks'][3].update(status=status, reason=reason)
                actual = self.decide(row)[3]
                self.assertEqual((actual['status'], actual['reason']), (status, reason))

    def test_short_watermark_can_match_but_ambiguous_branches_are_not_forced_to_pass(self):
        for status, reason in (
                ('pass', '水印店名及本次门店关系对应，主档地址更详细但没有地点冲突；水印日期及拍摄时间可读。'),
                ('unknown', '水印只有连锁简称，青闸线店与玉田店仍均可对应，现有照片没有可区分的分店信息。'),
                ('fail', '水印明确是乙市乙店，与本次申请甲市甲店冲突。')):
            with self.subTest(status=status):
                row = review('p01')
                row['checks'][0].update(status=status, reason=reason)
                actual = self.decide(row)[0]
                self.assertEqual((actual['status'], actual['reason']), (status, reason))

    def test_actual_catalogue_and_shared_qince_policy_include_user_decision(self):
        self.assertIn(DISPLAY_SIZE_ESTIMATION_CLARIFICATION,
                      next(r.text for r in rules('promotional_display') if r.id == 'display_size'))
        self.assertIn(LOCATION_REFERENCE_CLARIFICATION, POLICY)
        self.assertIn('不能混用单件、盒、箱', POLICY)
        self.assertIn('地址简略不等于地址水印缺失', POLICY)

    def test_static_contract_does_not_claim_that_every_future_case_lacks_application_sources(self):
        static = audit_schema('promotional_display')
        # Generic response constraints (for example a concise pass reason) may
        # use allOf; only case-specific missing-source rules are forbidden here.
        actual = audit_schema('promotional_display', materials=[], units=[])
        for contract in (static, actual):
            self.assertFalse(any('status' in c.get('then', {}).get('properties', {})
                                 for c in contract['properties']['checks']['items'].get('allOf', [])))
        comparison_contracts = lambda schema: [c for c in
            schema['properties']['checks']['items'].get('allOf', [])
            if 'comparisons' in c.get('then', {}).get('properties', {})]
        self.assertEqual(comparison_contracts(static), comparison_contracts(actual))
        # Availability is evaluated from the current actual fields at runtime;
        # a generic schema must not force a missing-field customer issue.

    def test_photo_pass_schema_requires_actual_application_reference_when_available(self):
        schema=photo_review_schema(['p01'],['p01','contract','application'],['contract'],
                                   application_source_ids=['application'])
        row=review('p01')
        row['checks'][0]['source_ids'].remove('application')
        with self.assertRaises(AuditError):
            validate({'photo_reviews':[row]},schema)
        row['checks'][0]['source_ids'].append('application')
        validate({'photo_reviews':[row]},schema)
        # Only the watermark application correspondence needs this source.
        self.assertNotIn('application',row['checks'][3]['source_ids'])

    def test_available_but_uncited_application_is_not_reported_as_missing_data(self):
        rule=next(r for r in rules('promotional_display') if r.id=='display_watermark')
        check=deepcopy(review('p01')['checks'][0])
        check['source_ids'].remove('application')
        original=deepcopy(check)
        docs=[{'unit_id':'application','facts':['本单申请活动门店甲店，2026-10-01至2026-10-31']}]
        materials=[{'id':'activity_application','state':'present','source_ids':['application']}]
        result=constrain_application_check('promotional_display',rule,check,docs,materials,[])
        self.assertEqual(result,original)
        self.assertNotIn('未取得',result['reason'])
        self.assertEqual(check,original)
        absent=constrain_application_check('promotional_display',rule,check,[],[],[])
        self.assertEqual(absent,original)
        cited={**check,'source_ids':check['source_ids']+['application']}
        self.assertEqual(constrain_application_check('promotional_display',rule,cited,docs,materials,[]),cited)

    def test_plain_endcap_does_not_acquire_a_whole_fixture_requirement(self):
        from audit_core.display_size_options import alternative_contract_sources
        self.case['documents'][0]['facts']=['本次约定1平米堆头或者端架。']
        self.assertEqual(alternative_contract_sources(self.case['documents'],self.case['materials']),set())
        row=review('p01')
        row['checks'][3].update(status='pass',reason='本图结构及产品陈列可确认是合同允许的端架；合同未要求整组或面积。')
        self.assertEqual(self.decide(row)[3]['status'],'pass')
        schema=photo_review_schema(['p01'],['p01','contract','application'],['contract'],size_alternatives=False)
        validate({'photo_reviews':[row]},schema)
        from tests.test_display_size_options import size_review
        row['checks'][3]['size_options']=size_review('p01')['checks'][3]['size_options']
        with self.assertRaises(AuditError):validate({'photo_reviews':[row]},schema)


if __name__ == '__main__':
    unittest.main()
