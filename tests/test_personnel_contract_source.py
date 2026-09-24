"""Confirmed application values come from the submitted promotion contract."""
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
    PERSONNEL_CONTRACT_CLARIFICATION,
    PERSONNEL_CONTRACT_COMPARISON_RULES,
    SKU_CONTRACT_CLARIFICATION,
    SKU_CONTRACT_SCENARIOS,
    catalogue,
    material_catalogue,
    requirements,
)
from tests.pdf_test_support import flags_for, unknown_checks


COMPARISONS = {
    "application_quantity_price": ("s", "结算数量=20", "20", "合同本次约定数量=20", "20", "eq"),
    "reward_unit_price": ("s", "结算奖励单价=2", "2", "合同本次约定奖励单价=2", "2", "eq"),
    "claim_ceiling": ("s", "本次核销金额=800", "800", "合同明确本次申请金额=1000", "1000", "le"),
}


def contract_fixture(scenario="personnel_incentive"):
    flags = flags_for(scenario, red_packet=scenario == "personnel_incentive")
    documents = [
        {"unit_id": "s", "facts": ["结算数量=20", "结算奖励单价=2", "本次核销金额=800",
                                    "结算单填的申请数量=20", "结算单填的申请奖励单价=2", "结算单填的申请金额=1000"]},
        {"unit_id": "p", "facts": ["本次红包金额=1000"]},
        {"unit_id": "c", "facts": ["合同本次约定数量=20", "合同本次约定奖励单价=2",
                                    "合同明确本次申请金额=1000", "跨全年合同总额=50000"]},
    ]
    materials = [{"id": material_id, "state": "present", "source_ids": [uid], "reason": "已读到该资料实际内容"}
                 for material_id, uid in (("settlement", "s"), ("promotion_contract", "c"))]
    if scenario == "personnel_incentive":
        materials.append({"id": "payment", "state": "present", "source_ids": ["p"], "reason": "已读到支付截图"})
    return flags, documents, materials, unknown_checks(scenario, flags)


def set_contract_comparison(evidence, rule_id):
    left_id, left_quote, left_value, right_quote, right_value, operator = COMPARISONS[rule_id]
    check = next(c for c in evidence["checks"] if c["rule_id"] == rule_id)
    check.update(status="pass", reason="本次申报与促销合同中的明确约定相符", source_ids=[left_id, "c"], comparisons=[{
        "label": rule_id,
        "value_kind": {"application_quantity_price": "quantity", "reward_unit_price": "unit_price",
                       "claim_ceiling": "amount"}[rule_id],
        "left": {"unit_id": left_id, "quote": left_quote, "number": left_value},
        "right": [{"unit_id": "c", "quote": right_quote, "number": right_value}],
        "operation": "value", "operator": operator,
    }])
    return check


class PersonnelContractSourceTests(unittest.TestCase):
    def test_all_three_personnel_comparisons_accept_values_from_actual_contract(self):
        self.assertEqual(PERSONNEL_CONTRACT_COMPARISON_RULES, frozenset(COMPARISONS))
        for rule_id in COMPARISONS:
            with self.subTest(rule_id=rule_id):
                flags, documents, materials, evidence = contract_fixture()
                original = set_contract_comparison(evidence, rule_id)
                result = audit_decision("personnel_incentive", flags, evidence, documents, materials)
                actual = next(c for c in result["checks"] if c["rule_id"] == rule_id)
                self.assertEqual(actual["status"], "pass")
                self.assertEqual(actual["calculations"][0]["right"][0]["unit_id"], "c")
                self.assertEqual(original["status"], "pass")

    def test_settlement_cannot_prove_its_own_application_values(self):
        wrong_quotes = {"application_quantity_price": "结算单填的申请数量=20",
                        "reward_unit_price": "结算单填的申请奖励单价=2",
                        "claim_ceiling": "结算单填的申请金额=1000"}
        for rule_id, quote in wrong_quotes.items():
            with self.subTest(rule_id=rule_id):
                flags, documents, materials, evidence = contract_fixture()
                check = set_contract_comparison(evidence, rule_id)
                check["source_ids"] = list(dict.fromkeys([check["comparisons"][0]["left"]["unit_id"], "s"]))
                check["comparisons"][0]["right"][0].update(unit_id="s", quote=quote)
                with self.assertRaises(AuditError):
                    audit_decision("personnel_incentive", flags, evidence, documents, materials)

    def test_unknown_with_no_comparison_does_not_guess_missing_contract_reference(self):
        for state in ("missing", "unclear", "present"):
            with self.subTest(contract_state=state):
                flags, documents, materials, evidence = contract_fixture()
                contract = next(m for m in materials if m["id"] == "promotion_contract")
                contract.update(state=state, source_ids=[] if state == "missing" else ["c"],
                                reason="没有找到可确认的合同本次申请金额或奖励单价")
                if state == "missing":
                    documents = [doc for doc in documents if doc["unit_id"] != "c"]
                else:
                    next(doc for doc in documents if doc["unit_id"] == "c")["facts"] = ["跨全年合同总额=50000"]
                for check in evidence["checks"]:
                    if check["rule_id"] in COMPARISONS:
                        check.update(status="unknown", source_ids=[] if state == "missing" else ["c"],
                                     reason="合同没有明确本次申请金额和商品奖励单价，不能拿全年合同总额比较", comparisons=[])
                result = audit_decision("personnel_incentive", flags, evidence, documents, materials)
                for check in result["checks"]:
                    if check["rule_id"] in COMPARISONS:
                        self.assertEqual(check["status"], "unknown")
                        self.assertEqual(check["calculations"], [])

    def test_missing_or_unclear_contract_cannot_support_a_definite_numeric_finding(self):
        for state in ("missing", "unclear"):
            for status in ("pass", "unknown"):
                with self.subTest(contract_state=state, check_status=status):
                    flags, documents, materials, evidence = contract_fixture()
                    contract = next(m for m in materials if m["id"] == "promotion_contract")
                    contract.update(state=state, source_ids=[] if state == "missing" else ["c"])
                    check = set_contract_comparison(evidence, "claim_ceiling")
                    check["status"] = status
                    with self.assertRaises(AuditError):
                        audit_decision("personnel_incentive", flags, evidence, documents, materials)

    def test_one_page_can_contain_contract_and_settlement_with_independent_quotes(self):
        flags, documents, materials, evidence = contract_fixture()
        check = set_contract_comparison(evidence, "reward_unit_price")
        check["source_ids"] = ["shared-page"]
        comparison = check["comparisons"][0]
        comparison["left"]["unit_id"] = "shared-page"
        comparison["right"][0]["unit_id"] = "shared-page"
        for material in materials:
            if material["id"] in {"settlement", "promotion_contract"}:
                material["source_ids"] = ["shared-page"]
        documents = [{"unit_id": "shared-page", "facts": [comparison["left"]["quote"], comparison["right"][0]["quote"]]},
                     {"unit_id": "p", "facts": ["本次红包金额=1000"]}]
        result = audit_decision("personnel_incentive", flags, evidence, documents, materials)
        self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == "reward_unit_price")["status"], "pass")
        comparison["right"][0] = deepcopy(comparison["left"])
        with self.assertRaises(AuditError):
            audit_decision("personnel_incentive", flags, evidence, documents, materials)
        shared_quote = "结算奖励单价=3，合同本次约定奖励单价=2"
        documents[0]["facts"].append(shared_quote)
        comparison["left"].update(quote=shared_quote, number="3")
        comparison["right"][0].update(quote=shared_quote, number="2")
        result = audit_decision("personnel_incentive", flags, evidence, documents, materials)
        actual = next(c for c in result["checks"] if c["rule_id"] == "reward_unit_price")
        self.assertEqual(actual["status"], "fail")
        self.assertEqual(actual["calculations"][0]["left_value"], "3")
        self.assertEqual(actual["calculations"][0]["right_value"], "2")

    def test_other_types_now_require_brand_contract_for_approved_quantity_values(self):
        rule_id = "application_quantity_price"
        for scenario in SKU_CONTRACT_SCENARIOS:
            with self.subTest(scenario=scenario):
                flags, documents, materials, evidence = contract_fixture(scenario)
                check = set_contract_comparison(evidence, rule_id)
                result = audit_decision(scenario, flags, evidence, documents, materials)
                self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == rule_id)["status"], "pass")
                comparison = check["comparisons"][0]
                quote = comparison["right"][0]["quote"].replace("合同", "本次申请")
                documents.append({"unit_id": "application-reference", "facts": [quote]})
                comparison["right"][0].update(unit_id="application-reference", quote=quote)
                check["source_ids"] = [comparison["left"]["unit_id"], "application-reference"]
                with self.assertRaises(AuditError):
                    audit_decision(scenario, flags, evidence, documents, materials)

    def test_contract_confirmation_is_published_without_adding_an_application_material(self):
        expected = {"pos", "pos_excel", "payment", "staff_photos", "settlement", "promotion_contract"}
        self.assertEqual({r.id for r in requirements("personnel_incentive")}, expected)
        for policy in (catalogue(), material_catalogue()):
            with self.subTest(catalogue_keys=list(policy)):
                self.assertIn(PERSONNEL_CONTRACT_CLARIFICATION, policy["clarifications"])
                self.assertEqual({m["id"] for m in policy["types"]["personnel_incentive"]["materials"]}, expected)

    def test_actual_personnel_audit_prompt_includes_confirmed_contract_source(self):
        flags, documents, materials, response = contract_fixture()
        provider = PdfEvidenceProvider("gpt-6-astra", "medium")
        captured = []

        def fake_call(root, used_skill, schema, prompt, **kwargs):
            captured.append(render_prompt(prompt))
            kwargs["validator"](response)
            return response

        case = {"scenario": "personnel_incentive", "archive_id": "a001", "flags": flags,
                "documents": documents, "materials": materials}
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", side_effect=fake_call):
            provider.audit(case, Path(temporary))
        self.assertEqual(len(captured), 1)
        self.assertIn(PERSONNEL_CONTRACT_CLARIFICATION, captured[0])


class SkuContractSourceTests(unittest.TestCase):
    def test_contract_values_drive_actual_amount_and_unit_price_findings(self):
        for scenario in SKU_CONTRACT_SCENARIOS:
            for rule_id, too_high in (("claim_ceiling", "1200"), ("reward_unit_price", "3")):
                with self.subTest(scenario=scenario, rule_id=rule_id):
                    flags, documents, materials, evidence = contract_fixture(scenario)
                    check = set_contract_comparison(evidence, rule_id)
                    result = audit_decision(scenario, flags, evidence, documents, materials)
                    self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == rule_id)["status"], "pass")
                    left = check["comparisons"][0]["left"]
                    quote = left["quote"].replace(left["number"], too_high)
                    documents[0]["facts"].append(quote)
                    left.update(quote=quote, number=too_high)
                    result = audit_decision(scenario, flags, evidence, documents, materials)
                    actual = next(c for c in result["checks"] if c["rule_id"] == rule_id)
                    self.assertEqual(actual["status"], "fail")
                    self.assertEqual(actual["calculations"][0]["left_value"], too_high)
                    self.assertEqual(actual["calculations"][0]["right"][0]["unit_id"], "c")

    def test_settlement_and_pos_cannot_replace_the_contract_reference(self):
        for scenario in SKU_CONTRACT_SCENARIOS:
            for rule_id in ("claim_ceiling", "reward_unit_price"):
                for source, label in (("s", "结算单填的申请值"), ("pos", "POS销售值")):
                    with self.subTest(scenario=scenario, rule_id=rule_id, source=source):
                        flags, documents, materials, evidence = contract_fixture(scenario)
                        check = set_contract_comparison(evidence, rule_id)
                        term = check["comparisons"][0]["right"][0]
                        quote = label + "=" + term["number"]
                        if source == "s":
                            documents[0]["facts"].append(quote)
                        else:
                            documents.append({"unit_id": source, "facts": [quote]})
                        term.update(unit_id=source, quote=quote)
                        check["source_ids"] = list(dict.fromkeys(["s", source]))
                        with self.assertRaises(AuditError):
                            audit_decision(scenario, flags, evidence, documents, materials)

    def test_unclear_contract_reference_remains_unknown(self):
        for scenario in SKU_CONTRACT_SCENARIOS:
            for state in ("missing", "unclear", "present"):
                with self.subTest(scenario=scenario, contract_state=state):
                    flags, documents, materials, evidence = contract_fixture(scenario)
                    materials[1].update(state=state, source_ids=[] if state == "missing" else ["c"])
                    documents[2]["facts"] = ["跨全年合同总额=50000"]
                    for check in evidence["checks"]:
                        if check["rule_id"] in {"claim_ceiling", "reward_unit_price"}:
                            check.update(status="unknown", comparisons=[], source_ids=[],
                                         reason="合同中的本次申请金额或商品奖励单价没有写清，暂时无法比较")
                    result = audit_decision(scenario, flags, evidence, documents, materials)
                    for check in result["checks"]:
                        if check["rule_id"] in {"claim_ceiling", "reward_unit_price"}:
                            self.assertEqual(check["status"], "unknown")
                            self.assertEqual(check["calculations"], [])
                    if state != "present":
                        documents[2]["facts"].append(COMPARISONS["claim_ceiling"][3])
                        set_contract_comparison(evidence, "claim_ceiling")
                        with self.assertRaises(AuditError):
                            audit_decision(scenario, flags, evidence, documents, materials)

    def test_sales_quantity_still_compares_settlement_to_pos(self):
        for scenario in SKU_CONTRACT_SCENARIOS:
            with self.subTest(scenario=scenario):
                flags, documents, materials, evidence = contract_fixture(scenario)
                documents.append({"unit_id": "pos", "facts": ["POS商品甲销量=20"]})
                materials.append({"id": "pos_excel", "state": "present", "source_ids": ["pos"]})
                check = next(c for c in evidence["checks"] if c["rule_id"] == "settlement_pos_quantity")
                check.update(status="pass", reason="商品甲在结算单与销售明细中均为20件", source_ids=["s", "pos"],
                             comparisons=[{"label": "商品甲结算销量与销售明细销量", "value_kind": "quantity", "operation": "value", "operator": "eq",
                                           "left": {"unit_id": "s", "quote": "结算数量=20", "number": "20"},
                                           "right": [{"unit_id": "pos", "quote": "POS商品甲销量=20", "number": "20"}]}])
                result = audit_decision(scenario, flags, evidence, documents, materials)
                self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == "settlement_pos_quantity")["status"], "pass")

    def test_confirmation_reaches_actual_audit_prompts_and_preserves_material_lists(self):
        expected = {
            "giveaway_promotion": {"pos", "pos_excel", "gift_rule", "giveaway_evidence", "settlement", "promotion_contract"},
            "price_difference_support": {"pos", "pos_excel", "price_evidence", "full_reduction_evidence", "settlement", "promotion_contract"},
            "pos_target_incentive": {"pos", "pos_excel", "settlement", "promotion_contract"},
        }
        for scenario in SKU_CONTRACT_SCENARIOS:
            with self.subTest(scenario=scenario):
                self.assertEqual({r.id for r in requirements(scenario)}, expected[scenario])
                flags, documents, materials, response = contract_fixture(scenario)
                provider = PdfEvidenceProvider("gpt-6-astra", "medium")
                captured = []

                def fake_call(root, used_skill, schema, prompt, **kwargs):
                    captured.append(render_prompt(prompt))
                    kwargs["validator"](response)
                    return response

                case = {"scenario": scenario, "archive_id": "a001", "flags": flags,
                        "documents": documents, "materials": materials}
                with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", side_effect=fake_call):
                    provider.audit(case, Path(temporary))
                self.assertEqual(len(captured), 1)
                self.assertIn(SKU_CONTRACT_CLARIFICATION, captured[0])
        for policy in (catalogue(), material_catalogue()):
            self.assertIn(SKU_CONTRACT_CLARIFICATION, policy["clarifications"])


if __name__ == "__main__":
    unittest.main()
