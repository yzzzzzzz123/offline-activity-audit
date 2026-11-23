"""Customer summary layout from synthetic data, with zero model/API calls."""
from copy import deepcopy
import unittest

from audit_core.customer_language import CUSTOMER_PROBLEM_STYLE
from audit_core.pdf_policy import rules
from audit_core.stage_context import compile_context
from audit_core.summary_layout import group_summary_issues, issue_lines
from audit_core.workbench_store import render_analysis_summary_markdown
from tests.pdf_test_support import flags_for
from tests.test_optional_staff_and_display_photos import review
from tests.test_problem_only_summary import summary_packet
from audit_core.pdf_workflow import build_view
from audit_core.scenario_registry import SCENARIO_ORDER


class SummaryLayoutTests(unittest.TestCase):
    def test_newline_separates_issues_not_decimal_or_explanation_sentences(self):
        reason = "合同写1.50元，结算单写1.51元，相差0.01元。单价不一致。\n照片日期早于活动开始日期。"
        self.assertEqual(issue_lines(reason), [
            "合同写1.50元，结算单写1.51元，相差0.01元。单价不一致。",
            "照片日期早于活动开始日期。"])

    def test_line_break_in_original_quote_is_not_another_issue(self):
        reason = '《甲.xlsx》中“数量=2\n金额=29.90”与《乙.xlsx》中“数量=3\n金额=39.90”不一致。\n缺少合同。'
        self.assertEqual(issue_lines(reason), [reason.rsplit("\n", 1)[0], "缺少合同。"])

    def test_exact_repetition_is_stated_once_but_different_words_values_rules_and_statuses_stay(self):
        item = {"rule_id": "price_matches_contract", "status": "fail", "reason": "甲店价格1.51元高于合同1.50元。"}
        different = [dict(item, reason="乙店价格1.52元高于合同1.50元。"),
                     dict(item, status="unknown"), dict(item, rule_id="pos_fields")]
        result = group_summary_issues([item, deepcopy(item), *different])
        self.assertEqual(len(result), 4)
        self.assertIn("乙店价格1.52元高于合同1.50元。", result)

    def test_many_identical_photo_issues_use_examples_and_keep_all_audit_records(self):
        packet = summary_packet([review(f"p{i:02}", status="unknown") for i in range(1, 21)])
        original = deepcopy(packet)
        view = build_view([packet], None)
        summary = render_analysis_summary_markdown(view, {"status": "completed"})
        self.assertEqual(summary.count("水印日期被遮挡"), 1)
        self.assertIn("多张照片（如", summary)
        self.assertIn("甲店!R6C2", summary)
        self.assertNotIn("甲店!R25C2", summary)
        self.assertEqual(len(view["sheets"][0]["rows"]), 20)
        self.assertIn("甲店!R25C2", view["sheets"][0]["rows"][-1]["values"][0])
        self.assertEqual(packet, original)

    def test_different_issues_on_one_photo_each_have_their_own_number(self):
        photo = review("p01", status="unknown", problem="照片水印日期被遮挡，无法确认拍摄日期。")
        photo["checks"][1].update(status="fail", reason="堆头内混放其他品牌牙膏。")
        view = build_view([summary_packet([photo])], None)
        summary = render_analysis_summary_markdown(view, {"status": "completed"})
        self.assertIn("1. 照片收集.xlsx · 甲店!R6C2（单元格图片）：照片水印日期被遮挡", summary)
        self.assertIn("\n2. 照片收集.xlsx · 甲店!R6C2（单元格图片）：堆头内混放其他品牌牙膏", summary)

    def test_multiline_check_and_numeric_results_are_separate_without_changing_original_values(self):
        reason = ("薄荷牙膏：列示金额110.00，复算120.00，少了10.00。\n"
                  "水果牙膏：列示金额99.99，复算100.00，少了0.01。")
        view = {"sheets": [{"name": "人员激励", "rows": [{"status": "issue", "numeric": True,
            "summary_items": [{"rule_id": "pos_arithmetic", "status": "fail", "reason": reason,
                               "source_ids": ["pos"]}]}]}]}
        original = deepcopy(view)
        summary = render_analysis_summary_markdown(view, {"status": "completed"})
        self.assertIn("1. " + reason.splitlines()[0], summary)
        self.assertIn("\n2. " + reason.splitlines()[1], summary)
        self.assertEqual(view, original)

    def test_archive_and_scenario_boundaries_and_source_scopes_do_not_merge(self):
        reason = "缺少盖章版销售明细。"
        def sheet(name, archive, source):
            return {"name": name, "source_archive": archive, "rows": [{"status": "issue", "summary_items": [
                {"rule_id": "pos_seal", "status": "fail", "reason": reason, "source_ids": [source]}]}]}
        view = {"sheets": [sheet("人员激励", "甲.zip", "pos-a"), sheet("人员激励", "乙.zip", "pos-b"),
                           sheet("补差", "甲.zip", "pos-a")]}
        summary = render_analysis_summary_markdown(view, {"status": "completed"})
        self.assertEqual(summary.count(reason), 3)
        item = {"rule_id": "pos_seal", "status": "fail", "reason": reason, "scope": ("pos-a",)}
        self.assertEqual(len(group_summary_issues([item, dict(item, scope=("pos-b",))])), 2)

    def test_unstructured_legacy_reasons_are_not_semantically_guessed(self):
        issues = [{"reason": "照片看不清。", "location": "甲.jpg"},
                  {"reason": "照片看不清。", "location": "乙.jpg"}]
        self.assertEqual(group_summary_issues(issues), ["甲.jpg：照片看不清。", "乙.jpg：照片看不清。"])

    def test_very_long_single_issue_is_not_cut_off_or_failed(self):
        reason = "商品A金额差异：" + "原值1.5000000000001；" * 100 + "原始金额须保留。"
        self.assertEqual(group_summary_issues([{"reason": reason}]), [reason])
        self.assertEqual(issue_lines(reason), [reason])

    def test_compact_policy_for_all_eight_scenarios_has_same_style_without_extra_call(self):
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario):
                packet = compile_context(scenario, [rules(scenario)[0].id], flags_for(scenario))
                self.assertIsNotNone(packet)
                self.assertEqual(packet["policies"]["customer_language"], CUSTOMER_PROBLEM_STYLE)
                self.assertIn("一条只讲一件事", packet["policies"]["customer_language"])
                self.assertIn("不同问题用换行分开", packet["policies"]["customer_language"])
                self.assertIsNone(packet["max_text_bytes"])
                self.assertIsNone(packet["max_output_tokens"])


if __name__ == "__main__":
    unittest.main()
