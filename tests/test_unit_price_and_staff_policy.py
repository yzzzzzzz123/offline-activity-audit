"""Exact contract unit prices and store-by-day temporary-staff photos."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.customer_language import customer_reason, numeric_problem
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import STAFF_PHOTO_CLARIFICATION, UNIT_PRICE_CLARIFICATION
from audit_core.pdf_workflow import build_view
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_brand_amount_policy import decide, numeric_case


PRICE_RULES = [(scenario, rule_id)
               for scenario in ("personnel_incentive", "giveaway_promotion", "price_difference_support", "pos_target_incentive")
               for rule_id in ("reward_unit_price", "application_quantity_price")]
PRICE_RULES.append(("promotional_display", "display_quantity_price"))


def staff_case(temporary_staff=True):
    flags = flags_for("personnel_incentive", temporary_staff=temporary_staff)
    evidence = unknown_checks("personnel_incentive", flags)
    check = next(c for c in evidence["checks"] if c["rule_id"] == "staff_daily_photos")
    documents = [{"unit_id": "contract", "facts": ["合同安排甲店和乙店各在2026-09-01、2026-09-02做临促"]},
                 {"unit_id": "photos", "facts": ["甲店1日、2日及乙店1日、2日分别有对应水印照片，包含临促人员及产品"]}]
    materials = [{"id": "promotion_contract", "state": "present", "source_ids": ["contract"], "reason": "合同原件可见"},
                 {"id": "staff_photos", "state": "present", "source_ids": ["photos"], "reason": "临促照片已收到"}]
    case = {"scenario": "personnel_incentive", "archive_id": "a001", "flags": flags,
            "documents": documents, "materials": materials}
    return case, evidence, check


class UnitPriceEqualityTests(unittest.TestCase):
    def test_lower_equal_and_higher_prices_in_every_existing_contract_price_check(self):
        for scenario, rule_id in PRICE_RULES:
            for price, status in (("1.9", "fail"), ("2", "pass"), ("2.0001", "fail"), ("2.01", "fail")):
                with self.subTest(scenario=scenario, rule_id=rule_id, price=price):
                    case, evidence, _ = numeric_case(rule_id, price, ["2"], scenario=scenario,
                                                     operation="value", value_kind="unit_price", operator="eq")
                    original = deepcopy(evidence)
                    actual = decide(case, evidence, rule_id)
                    self.assertEqual(actual["status"], status)
                    self.assertEqual(actual["calculations"][0]["left_value"], price)
                    self.assertEqual(evidence, original, "程序判断不能改写原始模型证据")

    def test_unit_price_must_not_revert_to_lower_claim_allowance(self):
        for scenario, rule_id in PRICE_RULES:
            for operator in ("le", "ge"):
                with self.subTest(scenario=scenario, rule_id=rule_id, operator=operator):
                    case, evidence, _ = numeric_case(rule_id, "1.9", ["2"], scenario=scenario,
                                                     operation="value", value_kind="unit_price", operator=operator)
                    with self.assertRaisesRegex(AuditError, "比较方向"):
                        decide(case, evidence, rule_id)

    def test_rounding_final_amount_does_not_excuse_a_different_unit_price(self):
        case, evidence, price = numeric_case("reward_unit_price", "1.9", ["2"],
                                           operation="value", value_kind="unit_price", operator="eq")
        case["documents"].append({"unit_id": "total", "facts": ["最终结算金额抹零为100元"]})
        actual = decide(case, evidence, "reward_unit_price")
        self.assertEqual(actual["status"], "fail")
        self.assertFalse(actual["calculations"][0]["matches"])

    def test_claim_and_contract_price_sources_are_not_interchangeable(self):
        for mutation in ("reverse", "left_is_pos", "left_is_contract", "right_is_settlement"):
            with self.subTest(mutation=mutation):
                case, evidence, check = numeric_case("reward_unit_price", "1.9", ["2"],
                                                     operation="value", value_kind="unit_price", operator="eq")
                comparison = check["comparisons"][0]
                if mutation == "reverse":
                    comparison["left"], comparison["right"][0] = comparison["right"][0], comparison["left"]
                elif mutation == "left_is_pos":
                    case["materials"][0]["id"] = "pos_excel"
                elif mutation == "left_is_contract":
                    case["materials"][0]["id"] = "promotion_contract"
                else:
                    case["materials"][1]["id"] = "settlement"
                with self.assertRaises(AuditError):
                    decide(case, evidence, "reward_unit_price")

    def test_mixed_quantity_and_price_check_does_not_hide_a_quantity_mismatch(self):
        for scenario, rule_id in (("personnel_incentive", "application_quantity_price"),
                                   ("promotional_display", "display_quantity_price")):
            with self.subTest(scenario=scenario):
                case, evidence, check = numeric_case(rule_id, "2", ["2"], scenario=scenario,
                                                     operation="value", value_kind="unit_price", operator="eq")
                case["documents"][0]["facts"].append("申报数量=9")
                case["documents"][1]["facts"].append("合同对应约定数量=10")
                check["comparisons"].append({"label": "同一项目数量", "value_kind": "quantity", "operation": "value", "operator": "eq",
                                             "left": {"unit_id": "left", "quote": "申报数量=9", "number": "9"},
                                             "right": [{"unit_id": "right1", "quote": "合同对应约定数量=10", "number": "10"}]})
                actual = decide(case, evidence, rule_id)
                self.assertEqual(actual["status"], "fail")
                self.assertEqual([c["matches"] for c in actual["calculations"]], [True, False])

    def test_customer_reason_explains_both_lower_and_higher_unit_price_mismatches(self):
        units = [{"unit_id": "left", "source_file": "结算单.xlsx"}, {"unit_id": "right1", "source_file": "促销合同.pdf"}]
        for price in ("1.9", "2", "2.01"):
            with self.subTest(price=price):
                case, evidence, _ = numeric_case("reward_unit_price", price, ["2"],
                                                 operation="value", value_kind="unit_price", operator="eq")
                actual = decide(case, evidence, "reward_unit_price")
                reason = numeric_problem(actual, units)
                if price != "2":
                    for text in ("结算单.xlsx", "促销合同.pdf", price, "合同约定单价一致", "相差"):
                        self.assertIn(text, reason)
                    self.assertNotIn("核销金额", reason)
                else:
                    self.assertIsNone(reason)
        for scenario, rule_id in PRICE_RULES:
            with self.subTest(scenario=scenario, rule_id=rule_id, status="unknown"):
                flags = flags_for(scenario)
                result = audit_decision(scenario, flags, unknown_checks(scenario, flags), [])
                actual = next(c for c in result["checks"] if c["rule_id"] == rule_id)
                reason = customer_reason(actual, [])
                self.assertIn("单价是否与合同约定相同", reason)
                self.assertNotIn("单价是否超过合同约定", reason)

    def test_actual_price_prompts_require_equal_unit_price_and_only_allow_final_amount_rounding(self):
        for scenario in dict(PRICE_RULES):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                flags = flags_for(scenario)
                response = unknown_checks(scenario, flags)
                provider = PdfEvidenceProvider("gpt-6-astra", "medium")
                with patch.object(provider, "_call", return_value=response) as call:
                    provider.audit({"scenario": scenario, "archive_id": "a001", "flags": flags,
                                    "documents": [], "materials": []}, Path(temporary))
                prompt = render_prompt(call.call_args.args[3])
                self.assertIn(UNIT_PRICE_CLARIFICATION, prompt)
                self.assertIn("申报单价使用unit_price/eq", prompt)
                self.assertIn("只有最终结算金额可以抹零或四舍五入", prompt)
                self.assertNotIn("申报单价使用unit_price/le", prompt)


class TemporaryStaffPhotoTests(unittest.TestCase):
    def test_staff_pass_requires_both_contract_schedule_and_actual_photos(self):
        for sources, passes in ((["contract", "photos"], True), (["contract"], False), (["photos"], False)):
            with self.subTest(sources=sources):
                case, evidence, check = staff_case()
                check.update(status="pass", source_ids=sources, reason="甲乙两店9月1日和2日均有符合要求的临促照片")
                if passes:
                    self.assertEqual(decide(case, evidence, "staff_daily_photos")["status"], "pass")
                else:
                    with self.assertRaises(AuditError):
                        decide(case, evidence, "staff_daily_photos")

    def test_missing_store_day_and_visible_content_problems_keep_the_specific_customer_reason(self):
        for reason in ("甲店9月2日的临促照片缺少，乙店同日照片不能代替甲店。",
                       "甲店9月2日照片没有水印。", "甲店9月2日照片有产品，但没有临促人员。",
                       "甲店9月2日照片有临促人员，但没有产品。"):
            with self.subTest(reason=reason):
                case, evidence, check = staff_case()
                case["documents"][1]["facts"] = [reason]
                check.update(status="fail", source_ids=["contract", "photos"], reason=reason)
                result = audit_decision(case["scenario"], case["flags"], evidence, case["documents"], case["materials"])
                actual = next(c for c in result["checks"] if c["rule_id"] == "staff_daily_photos")
                self.assertEqual(actual["status"], "fail")
                self.assertEqual(actual["reason"], reason)
                self.assertEqual(case["materials"][1]["state"], "present")
                packet = {"archive_id": "a001", "source_archive": "本次临促资料.zip", "documents": case["documents"],
                          "units": [{"unit_id": "contract", "source_file": "促销合同.pdf", "locator": "第1页"},
                                    {"unit_id": "photos", "source_file": "临促照片.pdf", "locator": "第1页"}], "result": result}
                rows = build_view([packet], None)["sheets"][0]["rows"]
                actual_row = next(row for row in rows if row["rule_id"] == "staff_daily_photos")
                self.assertIn("甲店9月2日", actual_row["error_reason"])

    def test_unclear_store_or_date_is_unknown_and_cannot_become_missing_material(self):
        case, evidence, check = staff_case()
        reason = "照片水印日期模糊，暂时无法确认甲店9月2日是否已覆盖。"
        case["documents"][1]["facts"] = [reason]
        check.update(status="unknown", source_ids=["contract", "photos"], reason=reason)
        actual = decide(case, evidence, "staff_daily_photos")
        self.assertEqual(actual["status"], "unknown")
        self.assertEqual(actual["reason"], reason)

    def test_non_staff_and_unclear_staff_branches_remain_distinct(self):
        for temporary_staff, expected in ((False, "not_applicable"), (None, "unknown")):
            with self.subTest(temporary_staff=temporary_staff):
                case, evidence, check = staff_case(temporary_staff)
                case["materials"] = []
                actual = decide(case, evidence, "staff_daily_photos")
                self.assertEqual(actual["status"], expected)
                check.update(status="pass", source_ids=["contract", "photos"])
                with self.assertRaises(AuditError):
                    decide(case, evidence, "staff_daily_photos")

    def test_actual_staff_prompt_checks_each_store_day_and_watermark_people_products(self):
        case, evidence, _ = staff_case()
        provider = PdfEvidenceProvider("gpt-6-astra", "medium")
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", return_value=evidence) as call:
            provider.audit(case, Path(temporary))
        prompt = render_prompt(call.call_args.args[3])
        self.assertIn(STAFF_PHOTO_CLARIFICATION, prompt)
        self.assertIn("不能只看整场日期齐全或照片总数足够就通过", prompt)
        self.assertNotIn("不从资料栏另加水印组成、人员或产品画面检查", prompt)
        self.assertNotIn("临促只按审核第3项逐日核对照片，不扩出画面或水印字段要求", prompt)


if __name__ == "__main__":
    unittest.main()
