"""Customer wording must preserve the original materials and computed facts."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.customer_language import concise_reason, customer_reason, numeric_problem, plain_text
from audit_core.error_reason import attach_error_reasons
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision, calculate, document_map
from audit_core.pdf_materials import ORCHESTRATOR_SKILL, PdfEvidenceProvider
from audit_core.pdf_policy import MATERIALS
from audit_core.pdf_workflow import build_view
from audit_core.oss_intake import build_callback_result, _bounded_callback_result, MAX_CALLBACK_RESULT_CHARS
from audit_core.scenario_registry import SKILL_BY_SCENARIO, SCENARIO_LABELS
from audit_core.workbench_store import render_analysis_summary_markdown
from tests.pdf_test_support import classification, flags_for, unknown_checks


def calculated_check(rule_id, left, right, *, operation="value", operator=None, label="商品甲销售金额",
                     left_file="核销资料/结算单.xlsx", right_files=None, left_quote=None, right_quotes=None):
    """Get calculations from the actual evidence validator, not invented totals."""
    flags = flags_for("personnel_incentive", red_packet=True)
    evidence = unknown_checks("personnel_incentive", flags)
    value_kind = {"claim_ceiling": "amount", "payment_claim_amount": "amount",
                  "pos_arithmetic": "amount", "reward_unit_price": "unit_price"}.get(rule_id, "quantity")
    operator = operator or {"claim_ceiling": "le", "payment_claim_amount": "le", "pos_arithmetic": "ge", "reward_unit_price": "eq"}.get(rule_id, "eq")
    if rule_id == "payment_claim_amount" and operation == "value":
        operation = "sum"
    right_files = right_files or ["核销资料/活动申请.xlsx"] * len(right)
    left_quote = left_quote or f"本次数值={left}"
    right_quotes = right_quotes or [f"对应数值{index + 1}={value}" for index, value in enumerate(right)]
    units = [{"unit_id": "a001-f0001", "source_file": left_file, "locator": "销售表第2行"}]
    documents = [{"unit_id": "a001-f0001", "facts": [left_quote]}]
    terms = []
    for index, (number, filename, quote) in enumerate(zip(right, right_files, right_quotes), start=2):
        uid = f"a001-f{index:04d}"
        units.append({"unit_id": uid, "source_file": filename, "locator": f"申请表第{index + 1}行"})
        documents.append({"unit_id": uid, "facts": [quote]})
        terms.append({"unit_id": uid, "quote": quote, "number": number})
    observation = next(c for c in evidence["checks"] if c["rule_id"] == rule_id)
    observation.update(status="pass", reason="模型误判：两边完全一致，不需要处理", source_ids=[u["unit_id"] for u in units],
                       comparisons=[{"label": label, "value_kind": value_kind,
                                     "left": {"unit_id": units[0]["unit_id"], "quote": left_quote, "number": left},
                                     "right": terms, "operation": operation, "operator": operator}])
    result = audit_decision("personnel_incentive", flags, evidence, documents)
    check = next(c for c in result["checks"] if c["rule_id"] == rule_id)
    return check, units, documents


def packet(checks, units, documents):
    return {"archive_id": "a001", "source_archive": "本次资料.zip", "units": units, "documents": documents,
            "result": {"scenario": "personnel_incentive", "checks": checks,
                       "summary": {"conclusion": "failed", "error_count": sum(c["status"] != "pass" for c in checks)}}}


class CustomerLanguageTests(unittest.TestCase):
    def test_simplification_keeps_the_uncertain_subject_and_store_date(self):
        self.assertEqual(concise_reason("尚不能确认的资料或适用情况：临促每天的活动照片（条件不明）"),
                         "无法确认临促每天的活动照片。")
        detail = "甲店9月2日照片未提供，促销合同未提供，无法确认有无临促及照片是否必交。"
        self.assertEqual(concise_reason("尚不能确认的资料或适用情况：临促每天的活动照片（" + detail + "）"), detail)

    def test_all_eight_types_show_facts_without_repeated_archive_or_type(self):
        for scenario, label in SCENARIO_LABELS.items():
            with self.subTest(scenario=scenario):
                reason = f"本次资料.zip：{label}：缺少资料：促销合同"
                view = {"sheets": [{"scenario": scenario, "audit_type_label": label,
                    "source_archive": "本次资料.zip", "projection_kind": "classification_rejection",
                    "rows": [{"status": "issue", "error_reason": reason, "values": [reason]}]}]}
                original = deepcopy(view)
                projected = attach_error_reasons(view)
                self.assertEqual(projected["sheets"][0]["rows"][0]["error_reason"], "缺少促销合同。")
                self.assertEqual(render_analysis_summary_markdown(view, {}), f"## {label}\n\n- 缺少促销合同。")
                self.assertEqual(view, original)
                self.assertEqual(attach_error_reasons(projected), projected)

    def test_material_gaps_are_concise_without_losing_uncertainty_or_actual_files(self):
        reasons = ["人员激励：缺少资料：盖章版销售明细（POS）",
            "人员激励：尚不能确认的资料或适用情况：临促每天的活动照片（未见临促现场照片；本次促销合同未提供，结算单未列活动申请说明或临促安排，无法确认有无临促及照片是否必交，不能推定豁免或具体缺少的门店日期。）",
            "人员激励：缺少资料：促销合同",
            "人员激励：销售明细（POS）须成套提供Excel版和盖章版，已提供一版时不能免交另一版",
            "人员激励：尚不能确认的资料或适用情况：临促每天的活动照片（《甲店9月2日.jpg》的日期看不清。）"]
        sheet = {"scenario": "personnel_incentive", "source_archive": "本次资料.zip",
                 "projection_kind": "classification_rejection", "audit_counts": {"error_count": 5},
                 "rows": [{"status": "issue", "section": "detail", "error_reason": "本次资料.zip：" + reason,
                           "card_evidence": {"source_files": ["甲店9月2日.jpg"]}} for reason in reasons]}
        original = deepcopy(sheet)
        projected = attach_error_reasons({"sheets": [sheet]})["sheets"][0]
        actual = [row["error_reason"] for row in projected["rows"]]
        self.assertEqual(actual, ["缺少盖章版销售明细。", "未提供促销合同，无法确认是否安排临促、是否需要临促照片。",
                                  "缺少促销合同。", "《甲店9月2日.jpg》的日期看不清。"])
        self.assertEqual(projected["audit_counts"]["error_count"], 4)
        self.assertEqual(sheet, original)
        self.assertEqual(attach_error_reasons({"sheets": [projected]})["sheets"][0], projected)

    def test_unclear_large_red_packet_names_ask_about_the_dealer_company(self):
        flags = flags_for("personnel_incentive", red_packet=True)
        result = audit_decision("personnel_incentive", flags, unknown_checks("personnel_incentive", flags), [])
        check = next(c for c in result["checks"] if c["rule_id"] == "payment_company")
        reason = customer_reason(check, [])
        self.assertIn("超过1000元", reason)
        self.assertIn("经销商公司名称", reason)
        self.assertNotIn("品牌公司名称", reason)
        self.assertNotIn("缺少", reason)

    def test_entry_duplicate_preserves_matching_address_and_both_photo_sources(self):
        address = "示例市示例路18号"
        units = [{"unit_id": "photo-a", "source_file": "资料/门店照片甲.jpg", "locator": "照片"},
                 {"unit_id": "photo-b", "source_file": "资料/门店照片乙.jpg", "locator": "照片"}]
        docs = [{"unit_id": unit["unit_id"], "facts": [f"申报门店：{store}", f"水印门店地址：{address}"]}
                for unit, store in zip(units, ("甲店", "乙店"))]
        flags = flags_for("entry_fee")
        evidence = unknown_checks("entry_fee", flags)
        check = next(c for c in evidence["checks"] if c["rule_id"] == "entry_duplicate")
        check.update(status="fail", source_ids=[u["unit_id"] for u in units],
                     reason=f"申报为甲店的《photo-a》和申报为乙店的《photo-b》，水印都显示“{address}”，不同申报门店地址重复。")
        result = audit_decision("entry_fee", flags, evidence, docs)
        item = packet(result["checks"], units, docs)
        item["result"] = result
        view = build_view([item], None)
        row = next(r for r in view["sheets"][0]["rows"] if r["rule_id"] == "entry_duplicate")
        summary = render_analysis_summary_markdown(view, {"error_count": result["summary"]["error_count"]})
        self.assertEqual(set(row["card_evidence"]["source_files"]), {u["source_file"] for u in units})
        for expected in (address, "门店照片甲.jpg", "门店照片乙.jpg", "甲店", "乙店", "不同申报门店", "地址重复"):
            self.assertIn(expected, row["error_reason"])
            self.assertIn(expected, summary)
        for unsupported in ("重复核销", "以往进场记录", "历史业务依据", "造假"):
            self.assertNotIn(unsupported, row["error_reason"])
        self.assertEqual(result["summary"]["conclusion"], "issues_found")

    def test_entry_duplicate_unknown_does_not_ask_for_past_entry_records(self):
        evidence = unknown_checks("entry_fee", flags_for("entry_fee"))
        result = audit_decision("entry_fee", flags_for("entry_fee"), evidence, [])
        check = next(c for c in result["checks"] if c["rule_id"] == "entry_duplicate")
        check["reason"] = "缺少" + check["requirement"].split("。")[0] + "的核对依据，无法核验。"
        reason = customer_reason(check, [])
        self.assertIn("不同申报门店", reason)
        self.assertIn("照片水印", reason)
        for unsupported in ("上架", "申请记录", "以往进场记录", "重复核销"):
            self.assertNotIn(unsupported, reason)

    def test_confirmed_same_store_photos_do_not_appear_as_a_duplicate_issue(self):
        flags = flags_for("entry_fee")
        evidence = unknown_checks("entry_fee", flags)
        units = [{"unit_id": "photo-a", "source_file": "甲店洗护.jpg", "locator": "照片"},
                 {"unit_id": "photo-b", "source_file": "甲店沐浴露.jpg", "locator": "照片"}]
        docs = [{"unit_id": unit["unit_id"], "facts": ["本图申报甲店，水印地址为示例路18号"]} for unit in units]
        check = next(c for c in evidence["checks"] if c["rule_id"] == "entry_duplicate")
        # This checks delivery of an AI-confirmed grouping, not visual model accuracy.
        check.update(status="pass", source_ids=[unit["unit_id"] for unit in units],
                     reason="已确认两图均为甲店的补充照片，不因水印地址相同列问题。")
        result = audit_decision("entry_fee", flags, evidence, docs)
        item = packet(result["checks"], units, docs)
        item["result"] = result
        view = build_view([item], None)
        row = next(r for r in view["sheets"][0]["rows"] if r["rule_id"] == "entry_duplicate")
        self.assertEqual(row["status"], "pass")
        summary = render_analysis_summary_markdown(view, {"error_count": result["summary"]["error_count"]})
        self.assertNotIn("甲店洗护.jpg", summary)
        self.assertNotIn("甲店沐浴露.jpg", summary)

    def test_uncertain_photo_store_assignment_preserves_the_specific_gap(self):
        check = {"rule_id": "entry_duplicate", "status": "unknown", "source_ids": ["photo-a", "photo-b"],
                 "reason": "《photo-a》和《photo-b》的水印地址相同，但现有资料无法确认是同店补充照片还是分别申报甲店和乙店。"}
        units = [{"unit_id": "photo-a", "source_file": "甲图.jpg"}, {"unit_id": "photo-b", "source_file": "乙图.jpg"}]
        reason = customer_reason(check, units)
        for expected in ("甲图.jpg", "乙图.jpg", "无法确认", "同店", "甲店", "乙店"):
            self.assertIn(expected, reason)
        self.assertNotIn("地址重复", reason)
        self.assertNotIn("未交", reason)

    def test_callback_system_failure_does_not_expose_private_exception_or_claim_missing_material(self):
        result = build_callback_result({"status": "failed", "failure": {
            "code": "model_failed", "message": "InternalError at C:\\private\\customer.xlsx: timeout (rule_id=fraud)",
        }})
        self.assertIn("系统处理没有完成", result)
        for hidden in ("InternalError", "private", "customer.xlsx", "timeout", "rule_id", "未交", "造假"):
            self.assertNotIn(hidden, result)

    def test_callback_length_limit_is_explained_in_plain_chinese(self):
        value = _bounded_callback_result("中" * (MAX_CALLBACK_RESULT_CHARS + 100))
        self.assertLessEqual(len(value), MAX_CALLBACK_RESULT_CHARS)
        self.assertIn("这里只显示部分", value)
        self.assertIn("核销记录中查看", value)
        self.assertNotIn("回调", value)

    def test_plain_text_preserves_source_names_with_internal_words(self):
        names = ("SKU-POS-核验-null-None明细.xlsx", "red_packet=null-盖章版.pdf")
        units = [{"unit_id": f"a001-f{index:04d}", "source_file": f"原始目录/{name}"}
                 for index, name in enumerate(names, start=1)]
        for unit, name in zip(units, names):
            for reference in (unit["unit_id"], unit["source_file"], name):
                with self.subTest(reference=reference):
                    result = plain_text(f"《{reference}》POS两版内容一致性无法核验，SKU不清。", units)
                    self.assertIn(f"《{name}》", result)
                    self.assertIn("暂时不能确认", result)
                    self.assertIn("商品不清", result)
                    self.assertNotIn("原始目录/", result)

    def test_build_view_also_preserves_names_before_condition_translation(self):
        filename = "SKU-POS-核验-null-None明细.xlsx"
        units = [{"unit_id": "a001-f0001", "source_file": "原始目录/" + filename, "locator": "第1页"}]
        docs = [{"unit_id": units[0]["unit_id"], "facts": ["付款图片上的金额看不清"]}]
        check = {"rule_id": "payment_company", "status": "unknown", "source_ids": [units[0]["unit_id"]],
                 "reason": f"《{filename}》付款金额看不清，red_packet为null，无法核验。",
                 "requirement": "提供支付证明", "effect": "missing_material", "calculations": []}
        row = build_view([packet([check], units, docs)], None)["sheets"][0]["rows"][0]
        self.assertIn(f"《{filename}》", row["error_reason"])
        self.assertIn("红包截图", row["error_reason"])
        self.assertNotIn("red_packet", row["error_reason"])
        self.assertIn("暂时不能确认", row["error_reason"])

    def test_amount_inequalities_keep_both_values_and_difference(self):
        for rule_id, left, right, expected, reference in (
            ("claim_ceiling", "1200", "1000", "超出200", "促销合同.xlsx"),
            ("payment_claim_amount", "1200", "1000", "少了200", "红包截图.jpg"),
        ):
            with self.subTest(rule_id=rule_id):
                check, units, _ = calculated_check(rule_id, left, [right], right_files=[f"核销资料/{reference}"])
                self.assertEqual(check["status"], "fail")
                reason = numeric_problem(check, units)
                for text in ("《结算单.xlsx》", f"《{reference}》", left, right, expected):
                    self.assertIn(text, reason)

    def test_same_file_values_are_identified_by_original_quotes(self):
        check, units, _ = calculated_check("settlement_pos_quantity", "12", ["10"],
                                          left_file="活动资料/本次表格.xlsx", right_files=["活动资料/本次表格.xlsx"],
                                          left_quote="结算销量=12", right_quotes=["销售明细销量=10"])
        reason = numeric_problem(check, units)
        for text in ("本次表格.xlsx", "结算销量=12", "销售明细销量=10", "相差2"):
            self.assertIn(text, reason)

    def test_different_original_files_with_same_basename_are_not_called_one_file(self):
        check, units, _ = calculated_check("settlement_pos_quantity", "12", ["10"],
                                          left_file="客户提交/结算/明细.xlsx", right_files=["客户提交/销售/明细.xlsx"],
                                          left_quote="结算销量=12", right_quotes=["销售明细销量=10"])
        reason = numeric_problem(check, units)
        self.assertNotIn("同一文件", reason)
        for text in ("12", "10", "相差2"):
            self.assertIn(text, reason)

    def test_product_and_sum_explain_decimal_arithmetic_without_float_roundoff(self):
        for operation, left, right, outcome, gap in (
            ("product", "0.29", ["3", "0.1"], "0.3", "0.01"),
            ("sum", "0.19", ["0.1", "0.1"], "0.2", "0.01"),
        ):
            with self.subTest(operation=operation):
                check, units, _ = calculated_check("pos_arithmetic", left, right, operation=operation)
                reason = numeric_problem(check, units)
                self.assertIn(left, reason)
                self.assertIn(f"结果应为{outcome}", reason)
                self.assertIn(f"少了{gap}", reason)
                self.assertIn("相乘" if operation == "product" else "加总", reason)
                self.assertNotIn("0000000000", reason)
                for value in right:
                    self.assertIn(value, reason)

    def test_product_accepts_the_same_money_spelling_as_numeric_evidence(self):
        check, units, _ = calculated_check("pos_arithmetic", "29元", ["3", "￥10元"], operation="product")
        reason = numeric_problem(check, units)
        self.assertIn("结果应为30", reason)
        self.assertIn("少了1", reason)

    def test_equal_numbers_or_no_comparisons_do_not_invent_a_problem(self):
        check, units, _ = calculated_check("payment_claim_amount", "1000", ["1000"])
        self.assertEqual(check["status"], "pass")
        self.assertIsNone(numeric_problem(check, units))
        check.update(status="unknown", calculations=[])
        self.assertIsNone(numeric_problem(check, units))

    def test_unknown_stays_uncertain_instead_of_claiming_missing_material_or_fraud(self):
        units = [{"unit_id": "a001-f0001", "source_file": "本次印章.jpg", "locator": "图片"}]
        docs = [{"unit_id": units[0]["unit_id"], "facts": ["图片中有印章，但文字模糊"]}]
        check = {"rule_id": "contract_signed", "status": "unknown", "source_ids": [units[0]["unit_id"]],
                 "reason": "印章文字看不清，无法核验是否为本次客户的章。", "requirement": "POS盖章版有客户章",
                 "effect": "missing_material", "calculations": []}
        row = build_view([packet([check], units, docs)], None)["sheets"][0]["rows"][0]
        self.assertEqual(row["confidence"], "low")
        self.assertIn("暂时不能确认", row["heading"])
        self.assertIn("印章文字看不清", row["error_reason"])
        for incorrect in ("未提交", "未盖章", "造假", "不能报销"):
            self.assertNotIn(incorrect, row["error_reason"])

    def test_unknown_that_only_repeats_a_penalty_rule_does_not_claim_penalty_or_missing_file(self):
        flags = flags_for("personnel_incentive")
        result = audit_decision("personnel_incentive", flags, unknown_checks("personnel_incentive", flags), [])
        for rule_id in ("application_quantity_price",):
            with self.subTest(rule_id=rule_id):
                check = next(c for c in result["checks"] if c["rule_id"] == rule_id)
                original = deepcopy(check)
                view = build_view([packet([check], [], [])], None)
                row = view["sheets"][0]["rows"][0]
                summary = render_analysis_summary_markdown(view, {"error_count": 1})
                self.assertTrue(row["error_reason"].startswith("目前资料还不能确认"))
                self.assertIn("暂时不能确认", row["heading"])
                self.assertEqual(row["confidence"], "low")
                self.assertEqual(row["values"][4], "")
                for misleading in ("0核销", "罚款", "不能报销", "未提交", "没有收到", "《"):
                    self.assertNotIn(misleading, summary)
                self.assertEqual(check, original, "简化空话不能修改unknown或原始证据")

    def test_fallback_preserves_specific_files_values_dates_and_observed_causes(self):
        flags = flags_for("personnel_incentive")
        result = audit_decision("personnel_incentive", flags, unknown_checks("personnel_incentive", flags), [])
        template = next(c for c in result["checks"] if c["rule_id"] == "pos_fields")
        name = "SKU-POS-核验-null-None明细.xlsx"
        units = [{"unit_id": "a001-f0001", "source_file": "原始目录/" + name}]
        for detail, expected in (
            (f"《{name}》的金额看不清。", (name, "金额看不清")),
            ("已读到红包金额800元，活动申请金额看不清。", ("800元", "活动申请金额看不清")),
            ("合同列明活动结束日2026-09-01，照片拍摄日期看不清。", ("2026-09-01", "拍摄日期看不清")),
            ("图片右下角的印章被裁掉一半。", ("右下角", "被裁掉一半")),
        ):
            with self.subTest(detail=detail):
                check = deepcopy(template)
                check["reason"] += "；" + detail
                original = deepcopy(check)
                output = customer_reason(check, units)
                self.assertFalse(output.startswith("目前资料还不能确认"))
                for fact in expected:
                    self.assertIn(fact, output)
                self.assertEqual(check, original)

    def test_short_source_id_does_not_replace_characters_inside_condition_words(self):
        units = [{"unit_id": "u", "source_file": "资料/现场.jpg"},
                 {"unit_id": "p", "source_file": "资料/销售.xlsx"}]
        text = "uses_pos为null；red_packet为None；《u》印章看不清；《p》销量看不清。"
        output = plain_text(text, units)
        self.assertIn("这次是否使用销售明细还不清楚", output)
        self.assertIn("这次是否用红包截图证明还不清楚", output)
        self.assertIn("《现场.jpg》印章看不清", output)
        self.assertIn("《销售.xlsx》销量看不清", output)
        self.assertEqual(output.count("现场.jpg"), 1)
        self.assertEqual(output.count("销售.xlsx"), 1)
        self.assertNotIn("uses_pos", output)
        self.assertNotIn("red_packet", output)

    def test_view_and_summary_keep_each_computed_mismatch_and_discard_wrong_model_reason(self):
        check, units, docs = calculated_check("pos_arithmetic", "110", ["10", "12"], operation="product", label="薄荷牙膏金额")
        second, second_units, second_docs = calculated_check("claim_ceiling", "1100", ["1000"], operator="le", label="本次核销金额")
        # Use independent sources for the second comparison within one packet.
        for index, unit in enumerate(second_units, start=len(units) + 1):
            old = unit["unit_id"]
            new = f"a001-f{index:04d}"
            unit["unit_id"] = new
            next(doc for doc in second_docs if doc["unit_id"] == old)["unit_id"] = new
            second["source_ids"] = [new if uid == old else uid for uid in second["source_ids"]]
            for comparison in second["calculations"]:
                for operand in [comparison["left"], *comparison["right"]]:
                    if operand["unit_id"] == old:
                        operand["unit_id"] = new
        check["reason"] = "错误模型解释：发票缺失，客户造假"
        second["reason"] = "错误模型解释：数量完全相等"
        original = deepcopy([check, second])
        view = build_view([packet([check, second], units + second_units, docs + second_docs)], None)
        summary = render_analysis_summary_markdown(view, {"error_count": 2})
        rows = view["sheets"][0]["rows"]
        self.assertEqual(len(rows), 2)
        for row in rows:
            self.assertIn(row["error_reason"], summary)
        for text in ("薄荷牙膏金额", "110", "120", "少了10", "本次核销金额", "1100", "1000", "超出100"):
            self.assertIn(text, summary)
        for wrong in ("错误模型解释", "发票缺失", "客户造假", "数量完全相等"):
            self.assertNotIn(wrong, summary)
        self.assertEqual([check, second], original, "面向客户改写不能改动审核事实")

    def test_multiple_failed_rows_in_one_numeric_check_are_all_preserved(self):
        check, units, docs = calculated_check("settlement_pos_quantity", "12", ["10"], label="薄荷牙膏销量")
        second = deepcopy(check["calculations"][0])
        second.update(label="水果牙膏销量")
        second["left"].update(number="8", quote="水果牙膏结算销量=8")
        second["right"][0].update(number="7", quote="水果牙膏销售明细销量=7")
        docs[0]["facts"].append(second["left"]["quote"])
        docs[1]["facts"].append(second["right"][0]["quote"])
        check["calculations"].append(calculate(second, document_map(docs)))
        view = build_view([packet([check], units, docs)], None)
        summary = render_analysis_summary_markdown(view, {"error_count": 1})
        for label, gap in (("薄荷牙膏销量", "相差2"), ("水果牙膏销量", "相差1")):
            self.assertIn(label, summary)
            self.assertIn(gap, summary)


class BusinessGuidePromptTests(unittest.TestCase):
    def test_each_of_eight_audit_prompts_loads_its_business_guide_and_customer_instructions(self):
        customer = (ORCHESTRATOR_SKILL / "references/customer-language.md").read_text(encoding="utf-8")
        background = (ORCHESTRATOR_SKILL / "references/business-background.md").read_text(encoding="utf-8")
        provider = PdfEvidenceProvider("gpt-6-astra", "medium")
        with tempfile.TemporaryDirectory() as temporary:
            for scenario in MATERIALS:
                with self.subTest(scenario=scenario):
                    skill = SKILL_BY_SCENARIO[scenario]
                    guide = (skill / "references/business-guide.md").read_text(encoding="utf-8")
                    self.assertIn("references/business-guide.md", (skill / "SKILL.md").read_text(encoding="utf-8"))
                    flags = flags_for(scenario)
                    value = classification(scenario, ["a001-f0001"], flags=flags)
                    case = {"scenario": scenario, "archive_id": "a001", "flags": flags,
                            "materials": value["materials"],
                            "documents": [{"unit_id": "a001-f0001", "facts": ["本次测试资料"]}]}
                    captured = []

                    def fake_call(root, used_skill, schema, prompt, **kwargs):
                        if root.name.startswith("pdf-pos-identities-"):
                            response = {"rows": [], "other_facts": [{"fact_index": 0, "kind": "unreadable", "reason": "测试资料不包含商品明细"}]}
                            kwargs["validator"](response)
                            return response
                        captured.append(render_prompt(prompt))
                        self.assertEqual(used_skill, skill)
                        response = unknown_checks(scenario, flags)
                        kwargs["validator"](response)
                        return response

                    with patch.object(provider, "_call", side_effect=fake_call):
                        provider.audit(case, Path(temporary))
                    self.assertEqual(len(captured), 1)
                    self.assertIn(guide, captured[0])
                    self.assertIn(customer, captured[0])
                    self.assertIn(background, captured[0])


if __name__ == "__main__":
    unittest.main()
