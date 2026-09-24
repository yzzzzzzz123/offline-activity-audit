"""Brand reimbursement needs sufficient actual evidence, without a rounding allowance."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.customer_language import numeric_problem
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision, audit_schema
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import AMOUNT_CLARIFICATION, MATERIALS
from tests.pdf_test_support import flags_for, unknown_checks


POS_SCENARIOS = ("personnel_incentive", "giveaway_promotion", "price_difference_support",
                 "self_procured_gift_material", "pos_target_incentive")


def numeric_case(rule_id, left, right, *, scenario="personnel_incentive",
                 operation="sum", value_kind="amount", operator="le"):
    flags = flags_for(scenario, red_packet=scenario == "personnel_incentive")
    left_quote = f"本次核销金额={left}" if rule_id == "payment_claim_amount" else f"本次列示值={left}"
    documents = [{"unit_id": "left", "facts": [left_quote]}]
    terms = []
    for index, number in enumerate(right, start=1):
        uid, quote = f"right{index}", f"本次第{index}项原值={number}"
        documents.append({"unit_id": uid, "facts": [quote]})
        terms.append({"unit_id": uid, "quote": quote, "number": number})
    right_role = "payment" if rule_id == "payment_claim_amount" else "promotion_contract"
    if rule_id == "settlement_pos_quantity":
        right_role = "pos_excel"
    if rule_id == "pos_arithmetic":
        materials = [{"id": "pos_excel", "state": "present", "source_ids": [d["unit_id"] for d in documents]}]
    else:
        materials = [{"id": "settlement", "state": "present", "source_ids": ["left"]},
                     {"id": right_role, "state": "present", "source_ids": [t["unit_id"] for t in terms]}]
    evidence = unknown_checks(scenario, flags)
    check = next(c for c in evidence["checks"] if c["rule_id"] == rule_id)
    check.update(status="pass", reason="本次资料原值比较", source_ids=[d["unit_id"] for d in documents],
                 comparisons=[{"label": "本次金额核对", "value_kind": value_kind,
                               "left": {"unit_id": "left", "quote": left_quote, "number": left},
                               "right": terms, "operation": operation, "operator": operator}])
    case = {"scenario": scenario, "archive_id": "a001", "flags": flags,
            "materials": materials, "documents": documents}
    return case, evidence, check


def decide(case, evidence, rule_id):
    result = audit_decision(case["scenario"], case["flags"], evidence, case["documents"], case["materials"])
    return next(c for c in result["checks"] if c["rule_id"] == rule_id)


class BrandAmountPolicyTests(unittest.TestCase):
    def test_final_claim_may_round_down_but_not_above_unrounded_reference(self):
        for rule_id in ("claim_ceiling", "payment_claim_amount"):
            for claim, expected in (("100", "pass"), ("100.005", "pass"), ("100.01", "fail")):
                with self.subTest(rule_id=rule_id, claim=claim):
                    case, evidence, _ = numeric_case(rule_id, claim, ["100.005"],
                                                     operation="sum" if rule_id == "payment_claim_amount" else "value")
                    check = decide(case, evidence, rule_id)
                    self.assertEqual(check["status"], expected)
                    self.assertEqual(check["calculations"][0]["right_value"], "100.005")

    def test_confirmed_payment_boundaries_and_customer_shortfall(self):
        units = [{"unit_id": "left", "source_file": "结算单.xlsx"},
                 {"unit_id": "right1", "source_file": "本次红包.jpg"}]
        for amount, status in (("100", "pass"), ("100.1", "pass"), ("101", "pass"), ("99.99", "fail")):
            with self.subTest(amount=amount):
                case, evidence, _ = numeric_case("payment_claim_amount", "100", [amount])
                check = decide(case, evidence, "payment_claim_amount")
                self.assertEqual(check["status"], status)
                reason = numeric_problem(check, units)
                if status == "pass":
                    self.assertIsNone(reason)
                else:
                    for expected in ("结算单.xlsx", "本次红包.jpg", "100", "99.99", "少了0.01"):
                        self.assertIn(expected, reason)
                    self.assertNotIn("申请金额", reason)

    def test_payment_sum_preserves_fractional_cents_and_exact_decimal_addition(self):
        for claim, payments, status, total in (
                ("0.3", ["0.1", "0.2"], "pass", "0.3"),
                ("100", ["60", "40.1"], "pass", "100.1"),
                ("100", ["60", "39.999"], "fail", "99.999")):
            with self.subTest(payments=payments):
                case, evidence, _ = numeric_case("payment_claim_amount", claim, payments)
                check = decide(case, evidence, "payment_claim_amount")
                self.assertEqual(check["status"], status)
                self.assertEqual(check["calculations"][0]["right_value"], total)

    def test_payment_cannot_reverse_claim_and_receipt_or_substitute_contract(self):
        for mutation in ("reverse", "contract", "settlement"):
            with self.subTest(mutation=mutation):
                case, evidence, check = numeric_case("payment_claim_amount", "100", ["101"])
                comparison = check["comparisons"][0]
                if mutation == "reverse":
                    comparison["left"], comparison["right"][0] = comparison["right"][0], comparison["left"]
                else:
                    case["materials"][1]["id"] = "promotion_contract" if mutation == "contract" else "settlement"
                with self.assertRaisesRegex(AuditError, "不能互换或改用合同额度"):
                    decide(case, evidence, "payment_claim_amount")

    def test_payment_requires_supported_direction_and_sum_of_original_payments(self):
        for field, value in (("operator", "eq"), ("operator", "ge"), ("operation", "value"),
                             ("operation", "product"), ("value_kind", "quantity")):
            with self.subTest(field=field, value=value):
                case, evidence, check = numeric_case("payment_claim_amount", "100", ["100"])
                check["comparisons"][0][field] = value
                with self.assertRaises(AuditError):
                    decide(case, evidence, "payment_claim_amount")

    def test_uncertain_payment_membership_stays_unknown(self):
        case, evidence, check = numeric_case("payment_claim_amount", "100", ["101"])
        check.update(status="unknown", comparisons=[], reason="红包金额可见，但无法确认属于本次活动。")
        actual = decide(case, evidence, "payment_claim_amount")
        self.assertEqual(actual["status"], "unknown")
        self.assertEqual(actual["calculations"], [])

    def test_model_cannot_fail_a_sufficient_amount_for_an_extra_reason(self):
        case, evidence, check = numeric_case("payment_claim_amount", "100", ["101"])
        check.update(status="fail", reason="红包比核销金额多1元")
        with self.assertRaisesRegex(AuditError, "不能凭额外理由"):
            decide(case, evidence, "payment_claim_amount")

    def test_pos_product_and_sum_boundaries_apply_to_all_existing_pos_types(self):
        for scenario in POS_SCENARIOS:
            for operation, terms in (("product", ["4", "25"]), ("sum", ["60", "40"])):
                for amount, status in (("100", "pass"), ("100.1", "pass"), ("101", "pass"), ("99.99", "fail")):
                    with self.subTest(scenario=scenario, operation=operation, amount=amount):
                        case, evidence, _ = numeric_case("pos_arithmetic", amount, terms, scenario=scenario,
                                                         operation=operation, operator="ge")
                        actual = decide(case, evidence, "pos_arithmetic")
                        self.assertEqual(actual["status"], status)
                        self.assertEqual(actual["calculations"][0]["right_value"], "100")

    def test_pos_product_uses_original_precision_without_rounding_tolerance(self):
        for amount, status in (("7.04", "pass"), ("7.035", "pass"), ("7.03", "fail")):
            with self.subTest(amount=amount):
                case, evidence, _ = numeric_case("pos_arithmetic", amount, ["3", "2.345"],
                                                 operation="product", operator="ge")
                actual = decide(case, evidence, "pos_arithmetic")
                self.assertEqual(actual["status"], status)
                self.assertEqual(actual["calculations"][0]["right_value"], "7.035")

    def test_pos_quantity_totals_still_require_equality(self):
        for quantity, status in (("100", "pass"), ("101", "fail"), ("99", "fail")):
            with self.subTest(quantity=quantity):
                case, evidence, check = numeric_case("pos_arithmetic", quantity, ["60", "40"],
                                                     value_kind="quantity", operator="eq")
                self.assertEqual(decide(case, evidence, "pos_arithmetic")["status"], status)
                check["comparisons"][0]["operator"] = "ge"
                with self.assertRaisesRegex(AuditError, "比较方向"):
                    decide(case, evidence, "pos_arithmetic")

    def test_pos_arithmetic_must_use_actual_sales_sources_and_calculations(self):
        for wrong_role in ("settlement", "promotion_contract", "payment"):
            with self.subTest(wrong_role=wrong_role):
                case, evidence, _ = numeric_case("pos_arithmetic", "100", ["60", "40"], operator="ge")
                case["materials"][0]["id"] = wrong_role
                with self.assertRaisesRegex(AuditError, "Excel版数据"):
                    decide(case, evidence, "pos_arithmetic")
        case, evidence, check = numeric_case("pos_arithmetic", "100", ["100"], operator="ge")
        check["comparisons"][0]["operation"] = "value"
        with self.assertRaisesRegex(AuditError, "替代复算"):
            decide(case, evidence, "pos_arithmetic")

    def test_amount_kind_cannot_replace_quantity_or_contract_unit_price(self):
        for rule_id, value_kind in (("settlement_pos_quantity", "quantity"),
                                    ("reward_unit_price", "unit_price")):
            with self.subTest(rule_id=rule_id):
                case, evidence, check = numeric_case(rule_id, "101", ["100"], operation="value",
                                                     value_kind=value_kind, operator="eq")
                self.assertEqual(decide(case, evidence, rule_id)["status"], "fail")
                check["comparisons"][0].update(value_kind="amount", operator="ge")
                with self.assertRaisesRegex(AuditError, "数值类别"):
                    decide(case, evidence, rule_id)

    def test_contract_ceiling_and_payment_support_are_independent(self):
        for payment, ceiling, payment_status, ceiling_status in (
                ("101", "100", "pass", "pass"),
                ("99.99", "200", "fail", "pass"),
                ("200", "99.99", "pass", "fail")):
            with self.subTest(payment=payment, ceiling=ceiling):
                case, evidence, payment_check = numeric_case("payment_claim_amount", "100", [payment])
                case["documents"].append({"unit_id": "contract", "facts": [f"合同本次批准金额={ceiling}"]})
                case["materials"].append({"id": "promotion_contract", "state": "present", "source_ids": ["contract"]})
                cap_check = next(c for c in evidence["checks"] if c["rule_id"] == "claim_ceiling")
                comparison = deepcopy(payment_check["comparisons"][0])
                comparison.update(operation="value", right=[{"unit_id": "contract",
                                   "quote": f"合同本次批准金额={ceiling}", "number": ceiling}])
                cap_check.update(status="pass", reason="比较本次合同额度", source_ids=["left", "contract"],
                                 comparisons=[comparison])
                self.assertEqual(decide(case, evidence, "payment_claim_amount")["status"], payment_status)
                self.assertEqual(decide(case, evidence, "claim_ceiling")["status"], ceiling_status)

    def test_comparison_requires_an_explicit_quantity_amount_or_unit_price_category(self):
        case, evidence, check = numeric_case("payment_claim_amount", "100", ["100"])
        del check["comparisons"][0]["value_kind"]
        with self.assertRaisesRegex(AuditError, "证据结构无效"):
            decide(case, evidence, "payment_claim_amount")

    def test_confirmed_amount_policy_reaches_all_eight_actual_audit_prompts(self):
        for scenario in MATERIALS:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                flags = flags_for(scenario)
                response = unknown_checks(scenario, flags)
                provider = PdfEvidenceProvider("gpt-6-astra", "medium")
                captured = []

                def fake_call(root, used_skill, schema, prompt, **kwargs):
                    captured.append(render_prompt(prompt))
                    kwargs["validator"](response)
                    return response

                with patch.object(provider, "_call", side_effect=fake_call):
                    provider.audit({"scenario": scenario, "archive_id": "a001", "flags": flags,
                                    "documents": [], "materials": []}, Path(temporary))
                self.assertIn(AMOUNT_CLARIFICATION, captured[0])
                self.assertIn("本次核销金额<=本次红包合计", captured[0])
                self.assertIn("数量合计用quantity/eq", captured[0])
                comparison_schema = audit_schema(scenario)["properties"]["checks"]["items"]["properties"]["comparisons"]["items"]
                self.assertIn("value_kind", comparison_schema["required"])


if __name__ == "__main__":
    unittest.main()
