"""Current PDF-chain regressions for the confirmed price-support comparisons."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision, classification_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from tests.pdf_test_support import classification, flags_for, unknown_checks
from tests.test_brand_amount_policy import decide, numeric_case


SCENARIO = "price_difference_support"


def selected_gate(full_reduction, *, missing=()):
    value = classification(SCENARIO, ["u"], missing=missing,
                           flags=flags_for(SCENARIO, full_reduction=full_reduction))
    match = next(row for row in value["material_matches"] if row["scenario"] == SCENARIO)
    value.update(material_matches=[match], candidate_scenarios=[SCENARIO],
                 flags=deepcopy(match["flags"]), materials=deepcopy(match["materials"]))
    facts = ["本次商品按销售数量给予活动价格补差"]
    if full_reduction is True:
        facts = ["本次促销合同明确满100元减10元，按满减返还结算"]
    return value, [{"unit_id": "u", "facts": facts}]


class PriceSupportConfirmedQuantityTests(unittest.TestCase):
    def test_claim_quantity_within_actual_pos_is_accepted_without_rounding(self):
        for claim, pos, status in (("24000", "24023", "fail"),
                                   ("15060", "16890", "fail"),
                                   ("100", "100", "pass"),
                                   ("100.001", "100.002", "fail"),
                                   ("100.002", "100.001", "fail")):
            with self.subTest(claim=claim, pos=pos):
                case, evidence, _ = numeric_case("settlement_pos_quantity", claim, [pos],
                    scenario=SCENARIO, operation="value", value_kind="quantity", operator="eq")
                before = deepcopy((case, evidence))
                result = decide(case, evidence, "settlement_pos_quantity")
                self.assertEqual(result["status"], status)
                self.assertEqual(result["calculations"][0]["left_value"], claim)
                self.assertEqual(result["calculations"][0]["right_value"], pos)
                self.assertEqual((case, evidence), before)

    def test_direction_and_settlement_excel_roles_cannot_be_reversed(self):
        for fault in ("le", "ge", "reverse_sources", "stamped_pos"):
            with self.subTest(fault=fault):
                case, evidence, check = numeric_case("settlement_pos_quantity", "20", ["21"],
                    scenario=SCENARIO, operation="value", value_kind="quantity", operator="eq")
                comparison = check["comparisons"][0]
                if fault in {"le", "ge"}:
                    comparison["operator"] = fault
                elif fault == "reverse_sources":
                    comparison["left"], comparison["right"][0] = comparison["right"][0], comparison["left"]
                else:
                    case["materials"][1]["id"] = "pos_stamped"
                with self.assertRaises(AuditError):
                    decide(case, evidence, "settlement_pos_quantity")

    def test_commission_allows_lower_quantity_while_price_support_keeps_equality(self):
        for scenario, operator, status in (("personnel_incentive","le","pass"),("price_difference_support","eq","fail")):
            with self.subTest(scenario=scenario):
                case, evidence, check = numeric_case("settlement_pos_quantity", "20", ["21"],
                    scenario=scenario, operation="value", value_kind="quantity", operator=operator)
                self.assertEqual(decide(case, evidence, "settlement_pos_quantity")["status"], status)
                check["comparisons"][0]["operator"] = 'eq' if operator=='le' else 'le'
                with self.assertRaises(AuditError):
                    decide(case, evidence, "settlement_pos_quantity")

    def test_pos_internal_quantity_total_is_still_exact(self):
        for total, status in (("100", "pass"), ("99", "fail"), ("101", "fail")):
            with self.subTest(total=total):
                case, evidence, check = numeric_case("pos_arithmetic", total, ["60", "40"],
                    scenario=SCENARIO, value_kind="quantity", operation="sum", operator="eq")
                self.assertEqual(decide(case, evidence, "pos_arithmetic")["status"], status)
                check["comparisons"][0]["operator"] = "le"
                with self.assertRaises(AuditError):
                    decide(case, evidence, "pos_arithmetic")

    def test_reward_price_and_claim_ceiling_stay_separate(self):
        for price, status in (("2", "pass"), ("1.9", "fail"), ("2.0001", "fail")):
            with self.subTest(price=price):
                case, evidence, _ = numeric_case("reward_unit_price", price, ["2"],
                    scenario=SCENARIO, operation="value", value_kind="unit_price", operator="eq")
                self.assertEqual(decide(case, evidence, "reward_unit_price")["status"], status)
        case, evidence, _ = numeric_case("claim_ceiling", "100.001", ["100"],
            scenario=SCENARIO, operation="value", value_kind="amount", operator="eq")
        self.assertEqual(decide(case, evidence, "claim_ceiling")["status"], "fail")

    def test_distinct_contract_forecast_does_not_override_corresponding_billing_quantity(self):
        for claimed, expected in (("400", "pass"), ("401", "fail")):
            with self.subTest(claimed=claimed):
                case, evidence, check = numeric_case("application_quantity_price", claimed, ["400"],
                    scenario=SCENARIO, operation="value", value_kind="quantity", operator="eq")
                case["documents"][1]["facts"] = ["预计销售数量=500支", "本次补差计费数量=400支"]
                check["comparisons"][0]["right"][0]["quote"] = "本次补差计费数量=400支"
                before = deepcopy((case, evidence))
                result = decide(case, evidence, "application_quantity_price")
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["calculations"][0]["right_value"], "400")
                self.assertEqual((case, evidence), before)


class PriceSupportConfirmedEvidenceTests(unittest.TestCase):
    def test_no_confirmed_full_reduction_does_not_require_its_proof(self):
        for flag in (False, None):
            with self.subTest(flag=flag):
                value, documents = selected_gate(flag, missing=("full_reduction_evidence",))
                before = deepcopy((value, documents))
                result = classification_decision(value, documents, SCENARIO)
                self.assertTrue(result["matched"])
                self.assertEqual(result["reasons"], [])
                self.assertEqual((value, documents), before)

    def test_explicit_full_reduction_still_requires_real_activity_proof(self):
        for missing in ((), ("full_reduction_evidence",)):
            with self.subTest(missing=missing):
                value, documents = selected_gate(True, missing=missing)
                result = classification_decision(value, documents, SCENARIO)
                self.assertEqual(result["matched"], not missing)
                if missing:
                    self.assertTrue(any("满减" in reason for reason in result["reasons"]))

    def test_readable_and_obscured_photos_keep_individual_facts_in_actual_prompt(self):
        documents = [
            {"unit_id": "clear", "roles": ["photo"], "facts": ["甲店活动价19.90元，水印日期2026-10-05"], "limitations": []},
            {"unit_id": "no-price", "roles": ["photo"], "facts": ["甲店本图未展示价签，水印日期2026-10-05"], "limitations": []},
            {"unit_id": "other-store", "roles": ["photo"], "facts": ["乙店活动价9.90元，水印日期2026-09-01"], "limitations": []},
        ]
        flags = flags_for(SCENARIO)
        case = {"scenario": SCENARIO, "archive_id": "synthetic", "flags": flags,
                "documents": documents, "materials": []}
        original = deepcopy(case)
        captured = []
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        response = unknown_checks(SCENARIO, flags)

        def fake_call(root, skill, schema, prompt, **kwargs):
            captured.append(render_prompt(prompt))
            kwargs["validator"](response)
            return response

        with tempfile.TemporaryDirectory() as directory, patch.object(provider, "_call", side_effect=fake_call):
            provider.audit(case, Path(directory))
        self.assertEqual(case, original)
        self.assertEqual(len(captured), 1)
        for document in documents:
            self.assertTrue(document["unit_id"] in captured[0], "Synthetic source id must reach the audit prompt")
            self.assertTrue(document["facts"][0] in captured[0], "Complete synthetic source fact must reach the audit prompt")
        for instruction in ("活动价格须逐张核对", "不能用其他照片清楚的价格自动豁免该图",
                            "销售预估数量与实际补差计费数量可以不同", "含起止当日",
                            "不能将另一张照片的日期移用为本图日期"):
            self.assertTrue(instruction in captured[0], "Confirmed scene instruction must reach the audit prompt: " + instruction)
        self.assertFalse("补差必须核验满减返还活动存在证明" in captured[0], "Audit prompt must not restore unconditional full-reduction proof")
        self.assertFalse("不能以full_reduction为null或false免除此项" in captured[0], "Audit prompt must preserve the confirmed conditional branch")
        decision = audit_decision(SCENARIO, flags, response, documents, [])
        price = next(check for check in decision["checks"] if check["rule_id"] == "price_evidence")
        self.assertEqual(price["status"], "unknown", "Instructions must not invent photo readings or turn unknown into pass")


if __name__ == "__main__":
    unittest.main()
