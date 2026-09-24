from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from audit_core.error_reason import attach_error_reasons, project_error_reasons
from audit_core.workbench_store import render_analysis_summary_markdown
from audit_core.html_report import _workbook_payload
from audit_core.report import create_combined_report
from tests.test_output_contracts import _personnel_result, _display_result


SCENARIOS = ("personnel_incentive", "promotional_display", "poster_material", "other_expense", "maintenance_fee", "giveaway_promotion", "price_difference_support", "pos_target_incentive", "entry_fee", "self_procured_gift_material")


class ErrorReasonTests(unittest.TestCase):
    @staticmethod
    def _legacy_material_diagnostic_view():
        issues = [
            {"code": "material_unreadable", "title": "文件部分内容无法确认",
             "observed": "陈列销售.jpg 的印章模糊，无法确认完整店名。", "source_files": ["陈列销售.jpg"]},
            {"code": "scenario_unconfirmed", "title": "核销类型待确认",
             "observed": "材料包申报类型为维护费用；价格补差：合同约定每支补差3元；人员激励：结算单记载人员代售机制；具体核销场景需确认。",
             "source_files": ["HX202601160053-广州南雄维护费用申请-9.zip"]},
            {"code": "missing_material", "title": "POS 销售电子表未提交",
             "observed": "POS 销售电子表（.xlsx 或 .xlsm）未提交。", "source_files": []},
            {"code": "singleton_role_ambiguous", "title": "结算单主件待确认",
             "observed": "主件与补充件关系未明确。", "source_files": ["服务费结算单.jpg", "结算单.jpg"]},
            {"code": "amount_mismatch", "title": "金额不一致",
             "observed": "申请金额1200元、应有金额1100元，相差100元。", "source_files": ["结算单.jpg"]},
        ]
        rows = [{"excel_row": index + 4, "kind": "record", "section": "detail", "status": "issue",
                 "heading": f"问题：{issue['title']}", "values": [issue["observed"], f"处理方式：原处理方式{index}"]}
                for index, issue in enumerate(issues)]
        return {"sheets": [{"name": "维护费用材料诊断", "scenario": "maintenance_fee",
            "source_archive": "HX202601160053-广州南雄维护费用申请-9.zip", "projection_kind": "material_diagnostic",
            "rows": rows, "diagnostic_issues": issues,
            "audit_counts": {"source_row_count": 5, "error_count": 5, "detail_error_count": 5, "context_error_count": 0}}]}

    def test_legacy_content_type_guess_is_omitted_when_zip_and_saved_type_agree(self):
        view = self._legacy_material_diagnostic_view()
        original = deepcopy(view)
        projected = attach_error_reasons(view)
        sheet = projected["sheets"][0]
        self.assertEqual([issue["code"] for issue in sheet["diagnostic_issues"]],
                         ["material_unreadable", "missing_material", "singleton_role_ambiguous", "amount_mismatch"])
        self.assertEqual([row["excel_row"] for row in sheet["rows"]], [4, 6, 7, 8])
        for row, original_row in zip(sheet["rows"], [original["sheets"][0]["rows"][index] for index in (0, 2, 3, 4)]):
            self.assertEqual(row["values"], original_row["values"])
        self.assertIn("陈列销售.jpg", sheet["rows"][0]["error_reason"])
        self.assertIn("POS 销售电子表", sheet["rows"][1]["error_reason"])
        self.assertEqual(sheet["rows"][2]["error_reason"], "服务费结算单.jpg、结算单.jpg：主件与补充件关系未明确。")
        self.assertIn("相差100元", sheet["rows"][3]["error_reason"])
        self.assertEqual(sheet["audit_counts"], {"source_row_count": 4, "error_count": 4,
                                                "detail_error_count": 4, "context_error_count": 0})
        self.assertEqual(view, original)
        self.assertEqual(attach_error_reasons(projected), projected)
        summary = render_analysis_summary_markdown(view, {"error_count": 5})
        self.assertNotIn("具体核销场景需确认", summary)
        self.assertNotIn("人员激励", summary)
        self.assertNotIn("价格补差", summary)
        self.assertIn("POS 销售电子表", summary)
        self.assertIn("相差100元", summary)

    def test_legacy_type_guess_stays_when_filename_does_not_uniquely_confirm_saved_type(self):
        cases = [
            {"source_archive": "ai-pack-8.zip"},
            {"source_archive": "维护费用-人员激励.zip"},
            {"source_archive": "维护费用/ai-pack-8.zip"},
            {"source_archive": r"维护费用\ai-pack-8.zip"},
            {"source_archive": ""},
            {"scenario": None},
            {"scenario": "price_difference_support"},
            {"projection_kind": "classification_rejection"},
        ]
        for replacement in cases:
            with self.subTest(replacement=replacement):
                view = self._legacy_material_diagnostic_view()
                view["sheets"][0].update(replacement)
                original = deepcopy(view)
                sheet = attach_error_reasons(view)["sheets"][0]
                self.assertEqual(len(sheet["rows"]), 5)
                self.assertEqual(len(sheet["diagnostic_issues"]), 5)
                self.assertEqual(sheet["audit_counts"], original["sheets"][0]["audit_counts"])
                self.assertEqual(view, original)

    def test_legacy_type_omission_requires_aligned_issue_and_row(self):
        for mismatch in ("length", "title", "status"):
            with self.subTest(mismatch=mismatch):
                view = self._legacy_material_diagnostic_view()
                sheet = view["sheets"][0]
                if mismatch == "length":
                    sheet["diagnostic_issues"].pop()
                elif mismatch == "title":
                    sheet["rows"][1]["heading"] = "问题：合同费用类型不符合维护费用要求"
                else:
                    sheet["rows"][1]["status"] = "pass"
                projected = attach_error_reasons(view)["sheets"][0]
                self.assertEqual(len(projected["rows"]), 5)
                self.assertIn("scenario_unconfirmed", [issue["code"] for issue in projected["diagnostic_issues"]])

    def test_legacy_type_projection_uses_only_archive_basename_and_shared_specific_markers(self):
        for source, scenario in ((r"人员激励\客户资料\维护费用.zip", "maintenance_fee"),
                                 ("维护费用/客户自采赠品物料.zip", "self_procured_gift_material")):
            with self.subTest(source=source):
                view = self._legacy_material_diagnostic_view()
                view["sheets"][0].update(source_archive=source, scenario=scenario)
                sheet = attach_error_reasons(view)["sheets"][0]
                self.assertEqual(len(sheet["rows"]), 4)
                self.assertNotIn("scenario_unconfirmed", [issue["code"] for issue in sheet["diagnostic_issues"]])

    def test_all_ten_report_renderers_feed_the_common_reason_projection(self):
        results = [_personnel_result(), _display_result()]
        for scenario in SCENARIOS[2:]:
            results.append({"scenario": scenario, "summary": {"decision_label": "资料需补正"},
                f"{scenario}_audit": {"issues": [{"title": "缺少费用附件", "source_files": [f"客户目录/{scenario}.jpg"],
                    "observed": "合同印章已通过；费用附件未提交；申请金额2300元，应有金额2000元，差额300元。建议：补交费用附件。",
                    "expected": "费用附件完整", "impact": "暂不能核销", "resubmission": "补交费用附件", "confidence": "high"}]}})
        with tempfile.TemporaryDirectory() as temporary:
            report = create_combined_report(results, Path(temporary) / "synthetic-ten-scenario.xlsx")
            view = _workbook_payload(report)
        projected = attach_error_reasons(view)
        self.assertEqual(len(projected["sheets"]), 10)
        for sheet in projected["sheets"][2:]:
            issue = next(row for row in sheet["rows"] if row["status"] == "issue")
            self.assertIn("费用附件未提交", issue["error_reason"])
            self.assertIn("2300元", issue["error_reason"])
            self.assertIn("2000元", issue["error_reason"])
            self.assertIn("300元", issue["error_reason"])
            self.assertNotIn("已通过", issue["error_reason"])
            self.assertNotIn("建议", issue["error_reason"])
            self.assertNotIn("客户目录/", issue["error_reason"])
            self.assertTrue(any("补交费用附件" in value for value in issue["values"]))

    def test_all_scenarios_prefer_explicit_reason_and_keep_action_separate(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                view = {"sheets": [{"scenario": scenario, "name": scenario,
                    "headers": ["已识别内容", "错误原因", "处理方式", "核销影响"],
                    "rows": [{"status": "issue", "heading": "金额不一致", "values": [
                        "印章已通过；合同已识别；核验过程已完成", "申请金额1200元、应有金额1100元，相差100元。置信度：高；建议：更正结算单。核销影响：暂不能核销。",
                        "更正结算单并保留原始合同", "暂不能核销"]}]}]}
                original = deepcopy(view)
                projected = attach_error_reasons(view)
                row = projected["sheets"][0]["rows"][0]
                self.assertEqual(row["error_reason"], "申请金额1200元、应有金额1100元，相差100元。")
                self.assertEqual(row["values"], original["sheets"][0]["rows"][0]["values"])
                self.assertEqual(view, original)
                self.assertEqual(attach_error_reasons(projected), projected)
                summary = render_analysis_summary_markdown(view, {})
                self.assertNotIn("AI 小结", summary)
                self.assertNotIn("置信度", summary)
                self.assertNotIn("更正", summary)
                self.assertNotIn("已通过", summary)

    def test_diagnostic_long_duplicate_names_and_uncertainty_are_preserved(self):
        sources = [f"压缩包/第{index}组/很长的客户业务照片名称{index:03}.jpg" for index in range(40)]
        issues = [{"observed": "以下文件内容完全相同：" + "、".join(sources), "source_files": sources},
                  {"observed": "本包未能确认已签合同；目录/不清楚的扫描.pdf 内容仍无法确认。", "source_files": ["目录/不清楚的扫描.pdf"]}]
        view = {"sheets": [{"name": "维护费用", "projection_kind": "material_diagnostic", "diagnostic_issues": issues,
                            "rows": [{"status": "issue", "values": ["补交完整清晰文件"]} for _ in issues]}]}
        summary = render_analysis_summary_markdown(view, {})
        for source in sources:
            self.assertIn(source.rsplit("/", 1)[-1], summary)
        self.assertNotIn("压缩包/", summary)
        self.assertNotIn("目录/", summary)
        self.assertIn("未能确认已签合同", summary)
        self.assertNotIn("合同未提交", summary)
        self.assertNotIn("补交", summary)
        self.assertNotIn("具体明细见", summary)

    def test_distinct_objects_and_failed_fields_are_not_grouped_away(self):
        sheet = {"name": "堆头核销", "headers": ["具体对比结果"], "rows": [
            {"status": "issue", "heading": f"附件第{index}行", "values": [
                f"商品编码：不匹配（合同编码 C{index} 未登记）；条形码：精确匹配；名称：模糊匹配；处理方式：修正"]}
            for index in range(1, 16)]}
        summary = render_analysis_summary_markdown({"sheets": [sheet]}, {})
        self.assertEqual(summary.count("\n- "), 15)
        for index in range(1, 16):
            self.assertIn(f"附件第{index}行", summary)
            self.assertIn(f"C{index} 未登记", summary)
        self.assertNotIn("精确匹配", summary)
        self.assertNotIn("模糊匹配", summary)
        self.assertNotIn("修正", summary)

    def test_failure_consequence_does_not_repeat_the_actual_amount_error(self):
        row = {"status": "issue", "heading": "实际申请金额", "values": [
            "错误原因：结算单.jpg申请7350元，转账.jpg实付7353元，少3元。现有材料不能判断应以哪一份金额为准，金额证据链未闭合。"]}
        reason = project_error_reasons({}, row)
        self.assertEqual(reason, ["结算单.jpg申请7350元，转账.jpg实付7353元，少3元。"])

    def test_store_read_count_is_not_a_reason_but_missing_fields_and_differences_remain(self):
        missing = "转账.jpg、转账2.jpg没有完整显示每笔收款人、对应门店和完整交易日期"
        difference = "结算单.jpg申请7350元，转账.jpg实付7353元，少3元"
        for process in ("销售Excel读取到7家门店", "销售.xlsx读取到7家门店", "销售.xlsm：识别到 7 家门店"):
            with self.subTest(process=process):
                view = {"sheets": [{"scenario": "personnel_incentive", "headers": ["错误原因"], "rows": [
                    {"status": "issue", "heading": "收款人与日期", "values": [f"{process}；{missing}。{difference}。"]}]}]}
                original = deepcopy(view)
                projected = attach_error_reasons(view)
                self.assertEqual(projected["sheets"][0]["rows"][0]["error_reason"], f"{missing}；{difference}。")
                self.assertEqual(view, original)
        row = {"status": "issue", "heading": "门店数量不一致", "values": [
            "销售.xlsx读取到7家门店，合同要求8家，缺少1家门店"]}
        self.assertIn("合同要求8家，缺少1家门店", project_error_reasons({"headers": ["错误原因"]}, row)[0])

    def test_unknown_photo_location_uses_business_identity_reason_without_requiring_watermark(self):
        row = {"status": "issue", "heading": "金大福（东浦金正和总店）", "values": [
            "识别日期：未识别\n识别地点：未识别\n地点核验依据：文件名不能单独证明门店",
            "主要问题：活动日期、门店水印缺失或无法核对\n处理方式：原处理方式"]}
        view = {"sheets": [{"scenario": "promotional_display", "rows": [row]}]}
        original = deepcopy(view)
        projected = attach_error_reasons(view)
        self.assertEqual(projected["sheets"][0]["rows"][0]["error_reasons"], [
            "金大福（东浦金正和总店）：活动日期无法核验。",
            "金大福（东浦金正和总店）：现场照片未显示可与合同门店对应的名称或地址。"])
        self.assertEqual(view, original)
        summary = render_analysis_summary_markdown(view, {})
        self.assertNotIn("水印", summary)
        self.assertIn("活动日期无法核验", summary)

    def test_recognized_or_unrecorded_photo_location_is_not_rewritten_as_missing(self):
        for location in ("识别地点：另一门店\n地点核验：与合同门店不一致",
                         "识别地点：未识别\n识别地点：另一门店", ""):
            with self.subTest(location=location):
                row = {"status": "issue", "heading": "门店甲", "values": [
                    location, "主要问题：门店水印缺失或无法核对"]}
                self.assertEqual(project_error_reasons({"scenario": "promotional_display"}, row),
                                 ["门店甲：门店水印缺失或无法核对。"])

    def test_legacy_photo_header_is_not_a_generic_observed_column(self):
        sheet = {"scenario": "promotional_display", "headers": ["合同PDF", "现场照片\n逐张显示文件名和识别内容", "销售Excel", "与合同具体对比结果", "核销金额", "结论 / 要重新提交什么"]}
        row = {"status": "issue", "heading": "门店甲", "values": [
            "门店：门店甲", "地点核验：同一POI\n地点置信度：高\n陈列标准核验：无法确认（照片不足以判断1平米堆头或4纵陈列）\n促销价格29.9元", "销售Excel不参与现场照片对比",
            "合同 → 现场：置信度：中（日期一致；陈列无法确认）\n已确认现场商品 → 合同商品范围：置信度：低（合同商品尚未全部通过知识库，不能确认现场商品是否属于合同。）", "建议核销0元",
            "主要问题：陈列标准、现场商品与合同\n处理方式：补交清晰照片"]}
        reasons = project_error_reasons(sheet, row)
        self.assertEqual(len(reasons), 1)
        self.assertIn("1平米堆头或4纵陈列", reasons[0])
        self.assertNotIn("置信度", "".join(reasons))
        self.assertNotIn("同一POI", "".join(reasons))
        self.assertNotIn("促销价格", "".join(reasons))

    def test_common_observation_preserves_failure_and_removes_inline_advice(self):
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario):
                sheet = {"scenario": scenario, "headers": ["问题来源", "已识别内容", "规则", "审核结论", "核销影响", "需要补交"]}
                row = {"status": "issue", "heading": "问题：材料不完整", "values": [
                    "问题：材料不完整\n文件：客户目录/合同.jpg", "识别结果：合同印章已通过；附件未提交，建议补交附件；金额应为2000元、实际2300元，差额300元。", "附件必需", "资料需补正", "暂不能核销", "补交附件"]}
                reason = project_error_reasons(sheet, row)[0]
                self.assertIn("合同.jpg", reason)
                self.assertIn("附件未提交", reason)
                self.assertIn("金额应为2000元、实际2300元，差额300元", reason)
                self.assertNotIn("客户目录/", reason)
                self.assertNotIn("已通过", reason)
                self.assertNotIn("建议", reason)
                self.assertNotIn("补交", reason)

    def test_absent_view_and_nonissue_rows_are_not_fabricated(self):
        self.assertIsNone(attach_error_reasons(None))
        self.assertEqual(attach_error_reasons({}), {})
        view = {"sheets": [{"rows": [{"status": "pass", "values": ["通过"]}]}]}
        self.assertEqual(attach_error_reasons(view), view)
        self.assertEqual(render_analysis_summary_markdown(view, {}), "未发现错误。")
        self.assertEqual(render_analysis_summary_markdown({}, {"error_count": 1}), "具体错误原因未记录。")

    def test_failed_comparison_keeps_both_values_inside_parentheses(self):
        row = {"status": "issue", "heading": "附件第2行", "values": [
            "金额：不一致（合同金额2000元；Excel实际金额2300元；差额300元）；数量：一致；处理方式：更正"]}
        reasons = project_error_reasons({"headers": ["具体对比结果"]}, row)
        self.assertEqual(len(reasons), 1)
        for value in ("2000元", "2300元", "300元"):
            self.assertIn(value, reasons[0])
        self.assertNotIn("数量：一致", reasons[0])
        self.assertNotIn("更正", reasons[0])

    def test_missing_supplemental_contract_is_a_reason_not_an_action(self):
        row = {"status": "issue", "heading": "合同缺失", "values": ["补充协议未提交；建议：补交该协议"]}
        self.assertEqual(project_error_reasons({"headers": ["错误原因"]}, row), ["补充协议未提交。"])

    def test_bare_problem_labels_do_not_claim_a_confirmed_mismatch(self):
        row = {"status": "issue", "heading": "门店甲", "values": ["主要问题：陈列标准、活动日期"]}
        self.assertEqual(project_error_reasons({}, row), ["门店甲：陈列标准无法核验。", "门店甲：活动日期无法核验。"])

    def test_filename_subjects_that_start_like_advice_keep_their_actual_reason(self):
        reasons = ("请款单.jpg：申请1200元，应有1000元，差额200元",
                   "建议书.pdf：缺少盖章", "更正后的结算单.jpg：数量不一致")
        for reason in reasons:
            with self.subTest(reason=reason):
                row = {"status": "issue", "heading": "问题：材料有误", "values": [reason]}
                self.assertEqual(project_error_reasons({"headers": ["错误原因"]}, row), [reason + "。"])
        row = {"status": "issue", "heading": "问题：数量不一致", "values": [
            "更正后的结算单.jpg：数量不一致；建议：补交建议书.pdf；请补交请款单.jpg；更正后的结算单.jpg：请重新拍摄。"]}
        self.assertEqual(project_error_reasons({"headers": ["错误原因"]}, row), ["更正后的结算单.jpg：数量不一致。"])

    def test_explicit_reasons_keep_different_product_objects_without_repeating_error_titles(self):
        sheet = {"scenario": "personnel_incentive", "name": "人员激励核销", "headers": ["错误原因"],
                 "rows": [{"status": "issue", "heading": name, "values": ["销售.xlsx数量10、结算单.jpg数量8"]}
                          for name in ("商品甲", "商品乙")]}
        summary = render_analysis_summary_markdown({"sheets": [sheet]}, {})
        self.assertIn("商品甲：销售.xlsx数量10、结算单.jpg数量8", summary)
        self.assertIn("商品乙：销售.xlsx数量10、结算单.jpg数量8", summary)
        self.assertEqual(summary.count("\n- "), 2)
        row = {"status": "issue", "heading": "问题：数量不一致", "values": ["结算单.jpg：数量不一致"]}
        self.assertEqual(project_error_reasons(sheet, row), ["结算单.jpg：数量不一致。"])

    def test_contract_code_reason_uses_business_name_and_actual_contract_code(self):
        sheet = {"scenario": "promotional_display", "headers": ["合同PDF（视觉AI识别）", "与合同具体对比结果"]}
        for actual_code in ("020260012", "CP-ACTUAL-CONTRACT-001"):
            with self.subTest(actual_code=actual_code):
                row = {"status": "issue", "heading": "合同销售附件第7行｜PDF第3页", "values": [
                    f"商品名称：参半茉莉牙膏180g\n商品编码：{actual_code}",
                    "商品编码：不匹配（知识库主编码 CP-KQ-YG-0084，且 product_code_aliases 未登记合同编码）；数量：不一致（合同12；Excel10）"]}
                reasons = project_error_reasons(sheet, row)
                self.assertIn(f"合同第3页第7行，商品“参半茉莉牙膏180g”：合同商品编码“{actual_code}”未在商品资料中登记。", reasons)
                self.assertEqual(len(reasons), 2)
                self.assertTrue(any("合同12" in reason and "Excel10" in reason for reason in reasons))
                self.assertNotIn("CP-KQ-YG-0084", "".join(reasons))
                self.assertNotIn("product_code_aliases", "".join(reasons))
        row = {"status": "issue", "heading": "合同销售附件第8行｜PDF第3页", "values": ["未识别合同编码", "商品编码：不匹配（知识库未登记合同业务编码）"]}
        self.assertEqual(project_error_reasons(sheet, row), ["合同第3页第8行：合同商品编码未在商品资料中登记。"])

    def test_attachment_row21_keeps_knowledge_and_sales_failures_in_separate_scopes(self):
        row = {"status": "issue", "heading": "合同销售附件第21行｜PDF第3页", "values": [
            "商品名称：参半极光白超值装牙膏\n商品编码：020360006", "", "商品编码：020360066",
            "合同商品 → 商品知识库\n知识库商品编码：CP-KQ-YG-0076\n商品编码：不匹配（知识库未登记合同业务编码）\n商品名称：模糊匹配（辅助项）\n条形码：精确匹配\n本项结果：商品存在条件未满足；置信度：低\n\n合同附件 → 销售Excel\n客户名称：精确匹配\n商品编码：不一致\n数量：一致\n合计金额：一致", "", "处理方式：核对编码"]}
        sheet = {"scenario": "promotional_display", "headers": ["合同PDF", "现场照片", "销售Excel", "与合同具体对比结果", "核销金额", "处理方式"], "rows": [row]}
        view = {"sheets": [sheet]}
        original = deepcopy(view)
        projected = attach_error_reasons(view)
        result = projected["sheets"][0]["rows"][0]
        scopes = result["error_reasons_by_scope"]
        self.assertEqual(len(scopes["knowledge"]), 1)
        self.assertEqual(len(scopes["sales"]), 1)
        self.assertIn("020360006", scopes["knowledge"][0])
        self.assertIn("参半极光白超值装牙膏", scopes["knowledge"][0])
        self.assertIn("未在商品资料中登记", scopes["knowledge"][0])
        self.assertNotIn("不一致", scopes["knowledge"][0])
        self.assertIn("商品编码：不一致", scopes["sales"][0])
        self.assertNotIn("未在商品资料中登记", scopes["sales"][0])
        self.assertEqual(result["error_reasons"], scopes["knowledge"] + scopes["sales"])
        self.assertEqual(view, original)
        self.assertEqual(attach_error_reasons(projected), projected)
        row["values"][3] = row["values"][3].replace("商品编码：不一致", "商品编码：精确匹配")
        scopes = attach_error_reasons(view)["sheets"][0]["rows"][0]["error_reasons_by_scope"]
        self.assertEqual(scopes["sales"], [])
        self.assertEqual(len(scopes["knowledge"]), 1)

    def test_unfinished_original_photo_review_is_not_a_customer_photo_quality_error(self):
        row = {"status": "issue", "heading": "门店甲", "values": [
            "陈列标准核验：无法确认（照片不足以判断1平米堆头或4纵陈列）\n视觉依据：本次无法完成要求的原始分辨率独立复核，未形成实体列数或占地面积的有效复核结论。",
            "主要问题：陈列标准"]}
        self.assertEqual(project_error_reasons({}, row), ["门店甲：现场照片复核未完成，尚未确认陈列列数和堆头面积。"])
        row["values"][0] = "陈列标准核验：无法确认（照片模糊，无法数清陈列列数）\n视觉依据：现场照片文字模糊。"
        reason = project_error_reasons({}, row)[0]
        self.assertIn("照片模糊", reason)
        self.assertNotIn("复核未完成", reason)
        row["values"][0] = "陈列标准核验：无法确认\n视觉依据：必做复核受文件访问限制阻断，未能以原始分辨率独立重新打开照片，无法给出可靠实体列计数或占地面积证明。"
        self.assertEqual(project_error_reasons({}, row), ["门店甲：系统无法读取现场原图，陈列列数和堆头面积尚未核验。"])

    def test_display_store_omits_upstream_and_already_confirmed_product_topics(self):
        row = {"status": "issue", "heading": "百佳华松岗店", "values": [
            "门店：百佳华松岗店", "知识库结论：置信度：中\n视觉依据：必做复核受文件访问限制阻断，未能原始分辨率重新打开照片，无法确认实体列数或占地面积。", "", "", "",
            "主要问题：销售Excel对合同附件、陈列标准、现场商品知识库、现场商品与合同"]}
        self.assertEqual(project_error_reasons({"scenario": "promotional_display"}, row), ["百佳华松岗店：系统无法读取现场原图，陈列列数和堆头面积尚未核验。"])
        row["values"][1] += "\n现场商品：商品甲，置信度：高；商品乙未确认，置信度：低"
        self.assertIn("百佳华松岗店：现场商品尚未确认。", project_error_reasons({"scenario": "promotional_display"}, row))

    def test_multiple_archives_keep_each_missing_object_and_remove_process_words(self):
        sheets = [{"name": "维护费用", "source_archive": name, "projection_kind": "material_diagnostic",
                   "diagnostic_issues": [{"code": "missing_material", "observed": "已盘点本包全部材料，未发现已签合同。", "source_files": []}],
                   "rows": [{"status": "issue"}]} for name in ("甲维护费用.zip", "乙维护费用.zip")]
        summary = render_analysis_summary_markdown({"sheets": sheets}, {})
        self.assertIn("### 甲维护费用.zip\n\n- 未发现已签合同", summary)
        self.assertIn("### 乙维护费用.zip\n\n- 未发现已签合同", summary)
        self.assertNotIn(".zip：", summary)
        self.assertEqual(summary.count("未发现已签合同"), 2)
        row = {"status": "issue"}
        issue = {"code": "singleton_role_ambiguous", "observed": "识别到 2 份结算单；尚不能确定主件与补充件关系，不能据此认定实际重复。", "source_files": ["目录/服务费结算单.jpg", "目录/结算单.jpg"]}
        self.assertEqual(project_error_reasons({}, row, issue), ["服务费结算单.jpg、结算单.jpg：主件与补充件关系未明确。"])
        issue = {"code": "duplicate_submission", "observed": "本次2个资料包中的文件字节内容完全相同；已保留各包的材料归属，需确认是否为误交。", "source_files": []}
        self.assertEqual(project_error_reasons({}, row, issue), ["本次2个资料包中的文件字节内容完全相同。"])


if __name__ == "__main__":
    unittest.main()
