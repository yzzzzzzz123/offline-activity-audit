"""Brand agreements define the standard; dealer submissions remain claims."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import (
    BRAND_CONTRACT_CLARIFICATION, CONTRACT_REFERENCE_RULES,
    GIFT_CONTRACT_CLARIFICATION, MATERIALS, catalogue, requirements,
)
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_personnel_contract_source import contract_fixture


def operand(uid, quote, number):
    return {"unit_id": uid, "quote": quote, "number": number}


def gift_fixture(operation="ratio"):
    scenario = "self_procured_gift_material"
    flags = flags_for(scenario)
    documents = [
        {"unit_id": "s", "facts": ["本次申报赠品数量=50"]},
        {"unit_id": "c", "facts": ["合同本次约定赠品数量=50", "合同购买门槛=2", "合同每次赠品数=1",
                                    "按本次活动合计销量计算，按完整达标次数赠送"]},
        {"unit_id": "p", "facts": ["POS本次实际销量=100"]},
        {"unit_id": "i", "facts": ["发票采购数量=50"]},
        {"unit_id": "r", "facts": ["经销商备注赠送规则：购买门槛=2，每次赠品数=1"]},
    ]
    materials = [{"id": material, "state": "present", "source_ids": [uid], "reason": "资料实际内容可见"}
                 for material, uid in (("settlement", "s"), ("promotion_contract", "c"),
                                       ("pos_excel", "p"), ("invoice", "i"), ("gift_rule", "r"))]
    right = [operand("c", "合同本次约定赠品数量=50", "50")] if operation == "value" else [
        operand("p", "POS本次实际销量=100", "100"),
        operand("c", "合同购买门槛=2", "2"),
        operand("c", "合同每次赠品数=1", "1"),
    ]
    comparison = {"label": "本次赠品申报数量与品牌方合同约定数量", "value_kind": "quantity", "operation": operation, "operator": "eq",
                  "left": operand("s", "本次申报赠品数量=50", "50"), "right": right}
    evidence = unknown_checks(scenario, flags)
    check = next(c for c in evidence["checks"] if c["rule_id"] == "gift_rule_quantity")
    check.update(status="pass", reason="本次申报赠品数符合合同规则", source_ids=["s", "p", "c"],
                 comparisons=[comparison])
    return flags, documents, materials, evidence, check


def decide_gift(fixture):
    flags, documents, materials, evidence, _ = fixture
    result = audit_decision("self_procured_gift_material", flags, evidence, documents, materials)
    return next(c for c in result["checks"] if c["rule_id"] == "gift_rule_quantity")


class BrandContractSourceTests(unittest.TestCase):
    def test_display_quantity_and_price_use_contract_and_reject_dealer_self_reference(self):
        for contract_quote, claim_quote, number in (
                ("合同约定堆头数量=2", "结算堆头数量=2", "2"),
                ("合同约定堆头单价=300", "结算堆头单价=300", "300")):
            with self.subTest(contract_quote=contract_quote):
                flags, documents, materials, evidence = contract_fixture("promotional_display")
                documents[0]["facts"].append(claim_quote)
                documents[2]["facts"].append(contract_quote)
                check = next(c for c in evidence["checks"] if c["rule_id"] == "display_quantity_price")
                comparison = {"label": "本次堆头申报与合同约定", "operation": "value", "operator": "eq",
                              "value_kind": "unit_price" if "单价" in contract_quote else "quantity",
                              "left": operand("s", claim_quote, number),
                              "right": [operand("c", contract_quote, number)]}
                check.update(status="pass", reason="与合同约定一致", source_ids=["s", "c"], comparisons=[comparison])
                result = audit_decision("promotional_display", flags, evidence, documents, materials)
                self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == check["rule_id"])["status"], "pass")
                declared_standard = contract_quote.replace("合同约定", "结算单自填申请")
                documents[0]["facts"].append(declared_standard)
                comparison["right"][0].update(unit_id="s", quote=declared_standard)
                with self.assertRaises(AuditError):
                    audit_decision("promotional_display", flags, evidence, documents, materials)

    def test_gift_direct_and_sales_formula_values_are_recalculated(self):
        for operation in ("value", "ratio", "floor_ratio"):
            with self.subTest(operation=operation):
                fixture = gift_fixture(operation)
                result = decide_gift(fixture)
                self.assertEqual(result["status"], "pass")
                self.assertEqual(result["calculations"][0]["right_value"], "50")
                fixture[1][0]["facts"].append("本次申报赠品数量=51")
                fixture[4]["comparisons"][0]["left"].update(quote="本次申报赠品数量=51", number="51")
                result = decide_gift(fixture)
                self.assertEqual(result["status"], "fail")
                self.assertEqual(result["calculations"][0]["left_value"], "51")

    def test_gift_formula_preserves_contract_rounding_and_sales_operand(self):
        for operation, expected, status in (("ratio", "50.5", "fail"), ("floor_ratio", "50", "pass")):
            with self.subTest(operation=operation):
                fixture = gift_fixture(operation)
                fixture[1][2]["facts"].append("POS本次实际销量=101")
                fixture[4]["comparisons"][0]["right"][0].update(quote="POS本次实际销量=101", number="101")
                result = decide_gift(fixture)
                self.assertEqual(result["status"], status)
                self.assertEqual(result["calculations"][0]["right_value"], expected)
                self.assertEqual(result["calculations"][0]["right"][0]["unit_id"], "p")

    def test_gift_product_can_combine_actual_sales_with_contract_ratio(self):
        fixture = gift_fixture()
        fixture[1][1]["facts"].append("合同每件购买商品赠品数=0.5")
        comparison = fixture[4]["comparisons"][0]
        comparison.update(operation="product", right=[comparison["right"][0], operand("c", "合同每件购买商品赠品数=0.5", "0.5")])
        self.assertEqual(decide_gift(fixture)["status"], "pass")
        replacement = deepcopy(comparison["right"][1])
        replacement.update(unit_id="p", quote="POS自行记载赠送比例=0.5")
        fixture[1][2]["facts"].append(replacement["quote"])
        comparison["right"][1] = replacement
        with self.assertRaises(AuditError):
            decide_gift(fixture)

    def test_dealer_rules_or_claimed_values_cannot_replace_contract_gift_parameters(self):
        for operation in ("value", "ratio", "floor_ratio", "product"):
            for index in ((0,) if operation == "value" else (1, 2)):
                for uid in ("s", "p", "r", "i"):
                    with self.subTest(operation=operation, index=index, uid=uid):
                        fixture = gift_fixture(operation)
                        term = fixture[4]["comparisons"][0]["right"][index]
                        next(d for d in fixture[1] if d["unit_id"] == uid)["facts"].append(term["quote"])
                        term["unit_id"] = uid
                        fixture[4]["source_ids"] = list(dict.fromkeys(["s", "p", "c", uid]))
                        with self.assertRaises(AuditError):
                            decide_gift(fixture)

    def test_gift_sales_must_come_from_pos_not_contract_plan_or_settlement(self):
        for uid in ("c", "s", "i"):
            with self.subTest(uid=uid):
                fixture = gift_fixture()
                term = fixture[4]["comparisons"][0]["right"][0]
                next(d for d in fixture[1] if d["unit_id"] == uid)["facts"].append(term["quote"])
                term["unit_id"] = uid
                fixture[4]["source_ids"] = list(dict.fromkeys(["s", "c", uid]))
                with self.assertRaises(AuditError):
                    decide_gift(fixture)

    def test_contract_quantity_or_purchase_invoice_does_not_prove_actual_gifts(self):
        for uid, quote in (("c", "合同约定本次采购预算数量=50"), ("i", "发票采购数量=50")):
            with self.subTest(uid=uid):
                fixture = gift_fixture("value")
                next(d for d in fixture[1] if d["unit_id"] == uid)["facts"].append(quote)
                fixture[4]["comparisons"][0]["left"] = operand(uid, quote, "50")
                fixture[4]["source_ids"] = list(dict.fromkeys(["c", uid]))
                with self.assertRaises(AuditError):
                    decide_gift(fixture)

    def test_unknown_contract_does_not_guess_a_gift_quantity(self):
        for state in ("missing", "unclear", "present"):
            with self.subTest(state=state):
                fixture = gift_fixture()
                fixture[2][1].update(state=state, source_ids=[] if state == "missing" else ["c"])
                fixture[1][1]["facts"] = ["合同没有写清本次赠送规则"]
                fixture[4].update(status="unknown", reason="合同未明确本次赠送门槛，无法核对申报的50份赠品", comparisons=[])
                result = decide_gift(fixture)
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["calculations"], [])
                if state != "present":
                    attempted = gift_fixture()
                    attempted[2][1].update(state=state)
                    with self.assertRaises(AuditError):
                        decide_gift(attempted)

    def test_visual_agreement_checks_cannot_pass_using_only_execution_or_claims(self):
        for scenario, rule_ids in CONTRACT_REFERENCE_RULES.items():
            for rule_id in rule_ids:
                with self.subTest(scenario=scenario, rule_id=rule_id):
                    flags = flags_for(scenario, temporary_staff=scenario == "personnel_incentive")
                    evidence = unknown_checks(scenario, flags)
                    check = next(c for c in evidence["checks"] if c["rule_id"] == rule_id)
                    check.update(status="pass", reason="现场与本次约定相符", source_ids=["photo"])
                    documents = [{"unit_id": "photo", "facts": ["本次现场照片内容"]},
                                 {"unit_id": "c", "facts": ["品牌方本次活动约定"]}]
                    contract_id = "entry_agreement" if scenario == "entry_fee" else "promotion_contract"
                    materials = [{"id": contract_id, "state": "present", "source_ids": ["c"], "reason": "本次品牌方约定"}]
                    if rule_id == "staff_daily_photos":
                        materials.append({"id": "staff_photos", "state": "present", "source_ids": ["photo"], "reason": "临促现场照片"})
                    with self.assertRaises(AuditError):
                        audit_decision(scenario, flags, evidence, documents, materials)
                    check["source_ids"].append("c")
                    result = audit_decision(scenario, flags, evidence, documents, materials)
                    self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == rule_id)["status"], "pass")
                    check.update(status="unknown", reason="合同没有明确本次活动范围", source_ids=["c"])
                    result = audit_decision(scenario, flags, evidence, documents, materials)
                    self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == rule_id)["status"], "unknown")

    def test_visible_missing_fields_can_still_fail_without_contract_reference(self):
        flags = flags_for("promotional_display")
        evidence = unknown_checks("promotional_display", flags)
        check = next(c for c in evidence["checks"] if c["rule_id"] == "display_watermark")
        check.update(status="fail", reason="陈列照片的水印没有拍摄日期", source_ids=["photo"])
        result = audit_decision("promotional_display", flags, evidence,
                                [{"unit_id": "photo", "facts": ["照片水印只有地址"]}], [])
        self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == check["rule_id"])["status"], "fail")

    def test_all_eight_runtime_prompts_use_brand_standard_and_existing_materials(self):
        policy = catalogue()
        self.assertIn(BRAND_CONTRACT_CLARIFICATION, policy["clarifications"])
        for scenario in MATERIALS:
            with self.subTest(scenario=scenario):
                flags = flags_for(scenario)
                response = unknown_checks(scenario, flags)
                provider = PdfEvidenceProvider("gpt-6-astra", "medium")
                captured = []

                def fake_call(root, used_skill, schema, prompt, **kwargs):
                    captured.append(render_prompt(prompt))
                    kwargs["validator"](response)
                    return response

                case = {"scenario": scenario, "archive_id": "a001", "flags": flags, "documents": [], "materials": []}
                with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", side_effect=fake_call):
                    provider.audit(case, Path(temporary))
                self.assertEqual(len(captured), 1)
                self.assertIn(BRAND_CONTRACT_CLARIFICATION, captured[0])
                if scenario == "self_procured_gift_material":
                    self.assertIn(GIFT_CONTRACT_CLARIFICATION, captured[0])
                material_ids = {m.id for m in requirements(scenario)}
                self.assertNotIn("application", material_ids)
                if scenario == "entry_fee":
                    self.assertIn("entry_agreement", material_ids)
                    self.assertNotIn("promotion_contract", material_ids)


if __name__ == "__main__":
    unittest.main()
