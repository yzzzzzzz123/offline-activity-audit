"""The Excel source controls POS data; a stamped copy cannot override it."""
from copy import deepcopy
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_decision
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_brand_amount_policy import numeric_case, decide
from tests.test_brand_contract_source import gift_fixture, decide_gift, operand


SALES_SCENARIOS = ("personnel_incentive", "giveaway_promotion", "price_difference_support", "pos_target_incentive")


def sales_case(scenario, excel, stamped, claimed):
    flags = flags_for(scenario)
    documents = [{"unit_id": uid, "facts": [f"本次销量={value}"]}
                 for uid, value in (("excel", excel), ("stamp", stamped), ("claim", claimed))]
    materials = [{"id": role, "state": "present", "source_ids": [uid]}
                 for role, uid in (("pos_excel", "excel"), ("pos", "stamp"), ("settlement", "claim"))]
    evidence = unknown_checks(scenario, flags)
    check = next(c for c in evidence["checks"] if c["rule_id"] == "settlement_pos_quantity")
    check.update(status="pass", source_ids=["claim", "excel"], reason="本次商品销量核对", comparisons=[{
        "label": "商品销量", "value_kind": "quantity", "operation": "value", "operator": "eq",
        "left": operand("claim", f"本次销量={claimed}", claimed),
        "right": [operand("excel", f"本次销量={excel}", excel)],
    }])
    return {"scenario": scenario, "flags": flags, "documents": documents, "materials": materials}, evidence, check


class PosExcelAuthorityTests(unittest.TestCase):
    def test_excel_controls_quantity_even_when_the_stamped_copy_differs(self):
        for scenario in SALES_SCENARIOS:
            for excel, stamp, expected in (("100", "110", "pass"), ("110", "100", "fail")):
                with self.subTest(scenario=scenario, excel=excel):
                    case, evidence, _ = sales_case(scenario, excel, stamp, "100")
                    actual = decide(case, evidence, "settlement_pos_quantity")
                    self.assertEqual(actual["status"], expected)
                    self.assertEqual(actual["calculations"][0]["right_value"], excel)

    def test_model_cannot_select_the_stamped_quantity_that_matches_the_claim(self):
        for scenario in SALES_SCENARIOS:
            with self.subTest(scenario=scenario):
                case, evidence, check = sales_case(scenario, "110", "100", "100")
                check["comparisons"][0]["right"] = [operand("stamp", "本次销量=100", "100")]
                check["source_ids"] = ["claim", "stamp"]
                with self.assertRaisesRegex(AuditError, "Excel"):
                    decide(case, evidence, "settlement_pos_quantity")

    def test_pos_fields_and_identity_cannot_report_only_stamped_copy_problems(self):
        case, evidence, _ = sales_case("personnel_incentive", "100", "110", "100")
        for rule_id in ("pos_fields", "pos_product_identity"):
            with self.subTest(rule_id=rule_id):
                value = deepcopy(evidence)
                check = next(c for c in value["checks"] if c["rule_id"] == rule_id)
                check.update(status="fail", source_ids=["stamp"], reason="盖章版中的销售明细内容有问题")
                with self.assertRaisesRegex(AuditError, "Excel"):
                    audit_decision(case["scenario"], case["flags"], value, case["documents"], case["materials"])

    def test_arithmetic_cannot_borrow_the_stamped_amount(self):
        case, evidence, check = numeric_case("pos_arithmetic", "100", ["10", "10"], operation="product", operator="ge")
        case["documents"].append({"unit_id": "stamp", "facts": ["盖章版金额=100"]})
        case["materials"].append({"id": "pos", "state": "present", "source_ids": ["stamp"]})
        self.assertEqual(decide(case, evidence, "pos_arithmetic")["status"], "pass")
        check["comparisons"][0]["left"] = operand("stamp", "盖章版金额=100", "100")
        check["source_ids"].append("stamp")
        with self.assertRaisesRegex(AuditError, "Excel"):
            decide(case, evidence, "pos_arithmetic")

    def test_gift_formula_sales_are_also_excel_only(self):
        for operation in ("ratio", "floor_ratio", "product"):
            with self.subTest(operation=operation):
                fixture = gift_fixture(operation)
                _, documents, materials, _, check = fixture
                documents.append({"unit_id": "stamp", "facts": ["盖章版销量=100"]})
                materials.append({"id": "pos", "state": "present", "source_ids": ["stamp"]})
                check["comparisons"][0]["right"][0] = operand("stamp", "盖章版销量=100", "100")
                check["source_ids"].append("stamp")
                with self.assertRaisesRegex(AuditError, "Excel"):
                    decide_gift(fixture)


if __name__ == "__main__":
    unittest.main()
