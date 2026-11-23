"""Generic model quality reviews with synthetic text and no network/model use."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_workflow import PdfWorkflow
from audit_core.summary_review import (
    final_request, render_reviewed_summary, review_stage, stage_request,
)
from audit_core.workbench_store import WorkbenchRunStore, ANALYSIS_SUMMARY_FILENAME
from audit_core.workbench_html import load_analysis_summary
from audit_core.oss_intake import build_callback_result
from tests.test_problem_only_summary import summary_packet


def row(reason, rule='giveaway_evidence', status='unknown', files=None):
    return {'status': 'issue', 'summary_items': [{'rule_id': rule, 'status': status,
             'reason': reason, 'source_ids': ['proof']}],
            'card_evidence': {'source_files': files or []}}


def view_for(rows, name='搭赠', archive='合成资料.zip'):
    return {'sheets': [{'name': name, 'source_archive': archive, 'rows': rows,
                       'audit_counts': {'error_count': len(rows)}}]}


def answer(*items):
    return {'ready': True, 'issues': [], 'items': [
        {'source_problem_ids': refs, 'text': text, 'uncertainty_preserved': True} for refs, text in items]}


class FinalReviewTests(unittest.TestCase):
    def test_final_review_sees_passed_checks_without_publishing_them_as_problems(self):
        passed = {'rule_id': 'entry_agreement', 'status': 'pass', 'heading': '进场约定已核对',
            'values': ['勤策关联明细', '本单已确认实际门店及商品范围。', '按本单有效约定来源审核', '符合要求', '', ''],
            'summary_items': []}
        issue = row('现有照片不能确认具体商品。', 'entry_sku')
        original = view_for([passed, issue], '条码费')
        original['sheets'][0]['headers'] = ['资料', '查到的情况', '要求', '结果']
        before = deepcopy(original)
        request = final_request(original)
        self.assertEqual(len(request['problems']), 1)
        self.assertEqual(request['audit_context'][0]['checks'][0]['finding'], passed['values'][1])
        self.assertNotIn('values', request['audit_context'][0]['checks'][0])
        self.assertEqual(request['audit_context'][0]['section_id'], 's1')
        self.assertIn('没有对应辅助参照时不执行', request['application_scope'])
        text = render_reviewed_summary(answer((['p1'], '现有照片不能确认具体商品。')), request)
        self.assertNotIn('已确认实际门店', text)
        self.assertEqual(original, before)

    def test_cross_rule_duplicate_can_merge_and_filenames_can_be_removed(self):
        files = ['手机图片_20261009_001.jpg', '手机图片_20261009_002.jpg']
        names = '、'.join(files)
        original = view_for([
            row(names + '：甲店、乙店尚未确认有效证明，共2家。', files=files),
            row(names + '：甲店、乙店共2家无法确认有效小票。', 'activity_photo_content', files=files)])
        before = deepcopy(original)
        request = final_request(original)
        text = render_reviewed_summary(answer((['p1', 'p2'], '甲店、乙店共2家尚未确认有效照片或小票。')), request)
        self.assertEqual(text.count('1. '), 1)
        self.assertNotIn('.jpg', text)
        self.assertEqual(original, before)

    def test_distinct_problems_are_both_covered_and_numbered(self):
        request = final_request(view_for([
            row('合同写1.50元，结算单写1.51元，相差0.01元。', 'reward_unit_price', 'fail'),
            row('乙店照片日期2026-10-01，无法确认是否在活动期间。', 'activity_photo_content')]))
        text = render_reviewed_summary(answer(
            (['p1'], request['problems'][0]['text']), (['p2'], request['problems'][1]['text'])), request)
        self.assertIn('1. 合同写1.50元', text)
        self.assertIn('\n2. 乙店照片日期2026-10-01', text)

    def test_review_preserves_source_mapping_and_model_uncertainty_attestation(self):
        request = final_request(view_for([row('甲店金额10.00元无法确认。'), row('乙店印章看不清。')]))
        certain = answer((['p1', 'p2'], '甲店金额10.00元填写错误，乙店印章不合格。'))
        certain['items'][0]['uncertainty_preserved'] = False
        for candidate in (
            answer((['p1'], '甲店金额10.00元无法确认。')),
            certain,
        ):
            with self.subTest(candidate=candidate), self.assertRaises(AuditError):
                render_reviewed_summary(candidate, request)

    def test_uncertainty_is_model_checked_without_a_fixed_chinese_phrase(self):
        request = final_request(view_for([row('甲店印章模糊，辨认不了。')]))
        result = render_reviewed_summary(answer((['p1'], '甲店印章模糊，辨认不了。')), request)
        self.assertIn('辨认不了', result)

    def test_multi_problem_source_can_split_without_repeating_all_its_numbers(self):
        request = final_request(view_for([row('甲店金额10元不符；乙店金额20元不符。', status='fail')]))
        result = render_reviewed_summary(answer((['p1'], '甲店金额10元不符。'),
                                              (['p1'], '乙店金额20元不符。')), request)
        self.assertIn('1. 甲店金额10元不符。\n2. 乙店金额20元不符。', result)

    def test_unknown_may_not_be_strengthened_but_fail_can_be_merged_with_unknown(self):
        request = final_request(view_for([row('甲店金额10元不符。', status='fail'), row('乙店金额10元无法确认。')]))
        result = render_reviewed_summary(answer((['p1', 'p2'], '甲店金额10元不符；乙店金额10元无法确认。')), request)
        self.assertIn('乙店金额10元无法确认', result)


    def test_different_archives_and_scenarios_cannot_be_merged(self):
        original = view_for([row('印章看不清。')])
        original['sheets'] += view_for([row('印章看不清。')], '人员激励', '另包.zip')['sheets']
        request = final_request(original)
        with self.assertRaises(AuditError):
            render_reviewed_summary(answer((['p1', 'p2'], '两份资料印章看不清。')), request)
        text = render_reviewed_summary(answer((['p1'], '印章看不清。'), (['p2'], '印章看不清。')), request)
        self.assertIn('## 人员激励', text)
        self.assertIn('### 另包.zip', text)
        self.assertIn('\n2. ', text)

    def test_no_issues_is_still_reviewed_but_cannot_acquire_new_finding(self):
        request = final_request(view_for([]))
        self.assertEqual(render_reviewed_summary(answer(), request), '未发现错误。')
        with self.assertRaises(AuditError):
            render_reviewed_summary(answer((['p1'], '新增问题。')), request)

    def test_not_ready_cannot_be_silently_published(self):
        request = final_request(view_for([row('印章看不清。')]))
        candidate = answer((['p1'], '印章看不清。'))
        candidate['ready'] = False
        with self.assertRaises(AuditError):
            render_reviewed_summary(candidate, request)

    def test_generic_review_applies_to_all_eight_types(self):
        from audit_core.scenario_registry import SCENARIO_LABELS
        for name in SCENARIO_LABELS.values():
            request = final_request(view_for([row('甲店印章看不清。', 'synthetic_rule')], name))
            with self.subTest(name=name):
                self.assertIn(name, render_reviewed_summary(answer((['p1'], '甲店印章看不清。')), request))


class ModelReviewIntegrationTests(unittest.TestCase):
    def fake_call(self, request_answer, calls):
        def call(root, skill, schema, prompt, **kwargs):
            calls.append((root.name, prompt.format()))
            self.assertEqual(kwargs['images'], [])
            self.assertEqual(kwargs['max_attempts'], 1)
            self.assertIn('前后是否矛盾', prompt.format())
            kwargs['validator'](request_answer)
            return request_answer
        return call

    def test_all_passed_and_not_applicable_stage_keeps_every_check(self):
        value={'checks':[
            {'rule_id':'contract_budget_amount','status':'pass','reason':'合同预算100元与申请100元一致。','source_ids':['contract'],'comparisons':[]},
            {'rule_id':'claim_budget_amount','status':'pass','reason':'核销90元未超预算100元。','source_ids':['claim'],'comparisons':[]},
            {'rule_id':'staff_contract','status':'not_applicable','reason':'本次活动不涉及临促人员。','source_ids':[],'comparisons':[]}]}
        before=deepcopy(value);calls=[]
        provider=PdfEvidenceProvider('fixture','medium')
        output=answer(*[([item['rule_id']],item['reason']) for item in value['checks']])
        with tempfile.TemporaryDirectory() as folder, patch('httpx.Client',side_effect=AssertionError('no network')), \
             patch.object(provider,'_call',side_effect=self.fake_call(output,calls)):
            result=review_stage(provider,{},value,value,Path(folder),[c['rule_id'] for c in value['checks']],lambda candidate, selected:candidate)
        self.assertEqual(result,before)
        self.assertEqual(value,before)
        self.assertEqual(len(calls),1)
        # Stage records remain complete; the final customer summary still
        # contains only actual findings from the same business results.
        final=final_request(view_for([row(check['reason'],check['rule_id'],check['status']) for check in value['checks']]))
        self.assertEqual(final['problems'],[])

    def test_unapproved_review_is_never_turned_into_an_automatic_success(self):
        from audit_core.summary_review import _validate_answer
        value={'checks':[{'rule_id':'contract_budget_amount','status':'pass','reason':'预算100元一致。','source_ids':[]}]}
        request=stage_request(value,value)
        rejected=answer((['contract_budget_amount'],'预算100元一致。'))
        rejected.update(ready=False,issues=['规则存在冲突'])
        with self.assertRaises(AuditError):_validate_answer(rejected,request,stage=True)

    def test_final_review_uses_same_provider_after_program_source_prefix(self):
        checks = [{'rule_id': 'contract_signed', 'status': 'unknown', 'reason': '合同印章看不清。',
                   'source_ids': ['contract'], 'requirement': '签章', 'comparisons': []}]
        packet = summary_packet([], checks)
        packet['route'] = {'matched': True}
        original = deepcopy(packet)
        provider = PdfEvidenceProvider('fixture', 'medium')
        calls = []
        with tempfile.TemporaryDirectory() as folder, patch('httpx.Client', side_effect=AssertionError('no network')), \
             patch.object(provider, '_call', side_effect=self.fake_call(
                 answer((['p1'], '合同.pdf：合同印章看不清。')), calls)):
            flow = PdfWorkflow('20261110-test', 'fixture', folder, Path(folder), provider, None, None,
                               packets=[packet], logic_review_result={'status':'passed','model_reviewed':True})
            flow.logic_review()
            flow.verification()
        self.assertEqual(len(calls), 1)
        self.assertIn('合同.pdf：合同印章看不清', calls[0][1])
        self.assertEqual(flow.receipt['verification']['summary_review']['status'], 'reviewed')
        self.assertIn('合同.pdf', flow.receipt['reviewed_analysis_summary'])
        self.assertEqual(packet, original)

    def test_stage_review_can_only_edit_reason_and_revalidates_all_evidence(self):
        value = {'checks': [{'rule_id': 'contract_signed', 'status': 'unknown',
            'reason': '合同章别不清，暂时不能确认章别。', 'source_ids': ['contract'], 'comparisons': []}]}
        original = deepcopy(value)
        provider = PdfEvidenceProvider('fixture', 'medium')
        calls, validations = [], []
        def validate(candidate, selected):
            validations.append(deepcopy(candidate))
            return candidate
        with tempfile.TemporaryDirectory() as folder, patch.object(provider, '_call', side_effect=self.fake_call(
                answer((['contract_signed'], '合同印章类型看不清，暂时不能确认。')), calls)):
            candidate = review_stage(provider, {}, value, value, Path(folder), ['contract_signed'], validate)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(validations), 1)
        for key in ('status', 'source_ids', 'comparisons', 'rule_id'):
            self.assertEqual(candidate['checks'][0][key], value['checks'][0][key])
        self.assertEqual(value, original)

    def test_real_provider_transport_uses_run_model_and_records_usage(self):
        from audit_core.model_settings import api_selection
        from audit_core.model_metrics import collect_model_artifacts, summarize_model_usage
        from audit_core import qwen_model
        selection = api_selection({'provider': 'deepseek', 'api_key': 'fixture'})
        provider = PdfEvidenceProvider(selection.model, selection.reasoning_effort, connection=selection)
        original = view_for([row('甲店印章看不清。')])
        output = answer((['p1'], '甲店印章看不清。'))
        captures = []
        def complete(connection, content, **kwargs):
            captures.append((connection, content, kwargs))
            return json.dumps(output, ensure_ascii=False), {'usage': {
                'input_tokens': 120, 'output_tokens': 40, 'total_tokens': 160}}
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with collect_model_artifacts(root/'analysis'), \
                 patch('httpx.Client', side_effect=AssertionError('no network')), \
                 patch.object(qwen_model, 'complete_json', side_effect=complete):
                reviewed = provider.review_summary(original, root/'temporary')
            usage = summarize_model_usage(root/'analysis')
            self.assertEqual(usage['input_tokens'], 120)
            self.assertEqual(usage['output_tokens'], 40)
            self.assertTrue((root/'analysis/model-observations/final-summary-review.json').is_file())
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0][0], selection)
        self.assertIsNone(captures[0][2]['max_tokens'])
        self.assertIn('summary_review', captures[0][2]['system'])
        self.assertNotIn('--- SKILL.md ---', captures[0][2]['system'])
        self.assertIn('甲店印章看不清', reviewed['text'])


    def test_legacy_summary_can_rephrase_program_explanation(self):
        value = {'checks': [{'rule_id': 'pos_arithmetic', 'status': 'pass', 'reason': '模型原判断。', 'source_ids': []}]}
        decision = deepcopy(value)
        decision['checks'][0].update(status='unknown', reason='程序尚未确认全量覆盖。')
        request = stage_request(value, decision)
        self.assertTrue(request['problems'][0]['read_only'])
        from audit_core.summary_review import _validate_answer
        _validate_answer(answer((['pos_arithmetic'], '程序尚未确认全量覆盖。')), request, stage=True)
        _validate_answer(answer((['pos_arithmetic'], '无法确认全量覆盖。')), request, stage=True)

    def test_stage_wording_must_not_change_evaluated_business_result(self):
        value = {'checks': [{'rule_id': 'contract_signed', 'status': 'unknown',
                            'reason': '印章看不清。', 'source_ids': ['contract'], 'comparisons': []}]}
        provider = PdfEvidenceProvider('fixture', 'medium')
        def changed_business(candidate, selected):
            changed = deepcopy(candidate)
            changed['checks'][0]['status'] = 'pass'
            return changed
        with tempfile.TemporaryDirectory() as folder, patch.object(provider, '_call', return_value=
                answer((['contract_signed'], '合同印章看不清。'))):
            with self.assertRaises(AuditError):
                review_stage(provider, {}, value, value, Path(folder), ['contract_signed'], changed_business)

    def test_saved_summary_display_and_callback_use_exact_reviewed_text(self):
        reviewed = '## 搭赠\n\n1. 甲店印章看不清。\n'
        with tempfile.TemporaryDirectory() as folder:
            workspace = Path(folder)/'20261110-summary-review'
            store = WorkbenchRunStore(workspace, run_id='20261110-test', producer_model='fixture',
                root_html=Path(folder)/'unused.html', workbench_url='http://127.0.0.1/')
            original_view = view_for([row('原始冗长说明无法确认。')])
            store.complete(view_payload=original_view, scenarios=['giveaway_promotion'],
                           verification={}, analysis_summary=reviewed)
            self.assertEqual((workspace/ANALYSIS_SUMMARY_FILENAME).read_text(encoding='utf-8'), reviewed)
            self.assertEqual(store.manifest['analysis_summary_display']['text'], reviewed.strip())
            self.assertEqual(load_analysis_summary(workspace, original_view, store.manifest)['text'], reviewed.strip())
            self.assertEqual(build_callback_result({'status': 'completed', 'worktree': str(workspace),
                'analysis_summary': str(workspace/ANALYSIS_SUMMARY_FILENAME)}), reviewed.strip())
            snapshot = json.loads((workspace/'snapshot.json').read_text(encoding='utf-8'))
            self.assertEqual(snapshot['view']['sheets'][0]['rows'][0]['summary_items'][0]['reason'],
                             '原始冗长说明无法确认。')


if __name__ == '__main__':
    unittest.main()
