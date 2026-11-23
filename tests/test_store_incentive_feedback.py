"""Store achievement rewards must keep their actual units and document roles."""
from copy import deepcopy
from unittest.mock import Mock
import unittest

from audit_core.common import AuditError
from audit_core.customer_language import plain_text
from audit_core.pdf_evidence import audit_decision, classification_decision
from audit_core.pdf_policy import MATERIALS, applicability, rules
from audit_core.pos_arithmetic import prepare_pos_arithmetic
from audit_core.pos_product_identity import verify_identity
from tests.pdf_test_support import classification, flags_for, unknown_checks
from tests.test_brand_amount_policy import numeric_case, decide


def sources(contract="门店达成奖励500元/家；区域达成奖励500元/人"):
    return [
        {"unit_id": "contract", "roles": ["促销合同"], "facts": [contract], "limitations": []},
        {"unit_id": "settlement", "roles": ["结算单"],
         "facts": ["奖励单品：门店达成激励；销售数量：22人；奖励单价500元；奖励金额11000元",
                   "区域达成奖励：10人；单价500元；金额5000元"], "limitations": []},
        {"unit_id": "payment", "roles": ["付款凭证"], "facts": ["本次奖励支出16000元"], "limitations": []},
        {"unit_id": "stamp", "roles": ["盖章版POS"], "facts": ["本次盖章销售明细"], "limitations": []},
        {"unit_id": "pos", "roles": ["spreadsheet"], "facts": ["销售!R1: C1=销售数量 | C2=销售单价 | C3=销售金额"], "limitations": []},
    ]


def gate(documents=None, *, store=True):
    documents = deepcopy(documents if documents is not None else sources())
    flags = flags_for("personnel_incentive", store_incentive=store, red_packet=True)
    ids = [d["unit_id"] for d in documents]
    value = classification("personnel_incentive", ids, flags=flags)
    value["material_matches"] = [m for m in value["material_matches"] if m["scenario"] == "personnel_incentive"]
    match = value["material_matches"][0]
    actual = {"promotion_contract": "contract", "settlement": "settlement", "payment": "payment", "pos": "stamp", "pos_excel": "pos"}
    for item in match["materials"]:
        if item["id"] in actual:
            item.update(state="present", source_ids=[actual[item["id"]]])
    value["materials"] = deepcopy(match["materials"])
    return value, documents


def native_pos(amount_header="销售金额"):
    return [{"unit_id": "pos", "roles": ["spreadsheet"], "facts": [
        f"销售!R1: C1=商品名称 | C2=69码 | C3=销售数量 | C4=销售单价 | C5={amount_header}",
        "销售!R2: C1=甲商品 | C2=6970000000001 | C3=2 | C4=3 | C5=6",
        "销售!R3: C1=乙商品 | C2=6970000000002 | C3=4 | C4=2 | C5=8",
        "销售!R4: C1=合计 | C3=6 | C5=14"], "limitations": []}]


class StoreIncentiveFeedbackTests(unittest.TestCase):
    def test_only_sku_settlement_quantity_and_company_name_are_exempt(self):
        flags = flags_for("personnel_incentive", store_incentive=True, red_packet=True)
        by_id = {r.id: r for r in rules("personnel_incentive")}
        for rid in ("settlement_pos_quantity", "reward_unit_price", "payment_company"):
            self.assertFalse(applicability(by_id[rid].when, "personnel_incentive", flags))
        for rid in ("pos_fields", "pos_arithmetic", "application_quantity_price", "claim_ceiling", "payment_claim_amount"):
            self.assertTrue(applicability(by_id[rid].when, "personnel_incentive", flags), rid)

    def test_unknown_or_non_store_case_does_not_claim_the_exemption(self):
        by_id = {r.id: r for r in rules("personnel_incentive")}
        for store in (False, None):
            flags = flags_for("personnel_incentive", store_incentive=store, red_packet=True)
            for rid in ("settlement_pos_quantity", "payment_company"):
                self.assertTrue(applicability(by_id[rid].when, "personnel_incentive", flags))

    def test_confirmed_store_and_regional_reward_contract_passes_source_validation(self):
        value, documents = gate()
        result = classification_decision(value, documents, "personnel_incentive")
        self.assertTrue(result["matched"])
        self.assertTrue(result["classification"]["flags"]["store_incentive"])

    def test_template_sales_quantity_with_person_units_does_not_force_sku_comparison(self):
        flags = flags_for("personnel_incentive", store_incentive=True, red_packet=True)
        value, documents = gate()
        evidence = unknown_checks("personnel_incentive", flags)
        result = audit_decision("personnel_incentive", flags, evidence, documents, value["materials"])
        by_id = {c["rule_id"]: c for c in result["checks"]}
        self.assertEqual(by_id["settlement_pos_quantity"]["status"], "not_applicable")
        self.assertEqual(by_id["payment_company"]["status"], "not_applicable")

    def test_people_count_filename_or_payment_text_alone_cannot_waive_checks(self):
        for hint in ("激励人数22人", "本次合同未写奖励对象", "是否门店奖励？", "本次不是门店奖励"):
            docs = sources(hint)
            docs[1]["facts"] = ["结算激励人数22人，单价500元"]
            docs[0]["source_file"] = "门店达成奖励合同.jpg"
            docs[2]["facts"] = ["门店达成奖励支付16000元"]
            value, docs = gate(docs)
            with self.subTest(hint=hint), self.assertRaisesRegex(AuditError, "明确依据"):
                classification_decision(value, docs, "personnel_incentive")

    def test_mixed_sku_reward_cannot_claim_a_whole_case_exemption(self):
        value, docs = gate(sources("门店达成奖励500元/家；另按单品销量每支奖励2元"))
        with self.assertRaisesRegex(AuditError, "明确依据"):
            classification_decision(value, docs, "personnel_incentive")

    def test_absence_observations_do_not_invent_a_mixed_sku_reward(self):
        absent_observations = (
            "结算单奖励项目为门店达成激励、区域达成奖励，数量单位为人，未出现临促工资、培训或按SKU销量计提的提成表述",
            "本单没有按单品销量计提的奖励",
            "未见每支奖励的项目",
            "未列按SKU销量计提的奖励",
        )
        for observation in absent_observations:
            with self.subTest(observation=observation):
                docs = sources()
                docs[1]["facts"].append(observation)
                value, docs = gate(docs)
                before = deepcopy((value, docs))
                result = classification_decision(value, docs, "personnel_incentive")
                self.assertTrue(result["classification"]["flags"]["store_incentive"])
                self.assertEqual((value, docs), before)

    def test_a_real_sku_reward_after_a_contrast_is_not_hidden_by_negation(self):
        for text in ("不涉及临促但按SKU销量计提奖励", "未出现培训，不过每支奖励2元", "不是没有按SKU销量计提奖励",
                     "数量不足仍按SKU销量计提奖励", "没有临促人员而按SKU销量计提奖励"):
            with self.subTest(text=text):
                docs = sources()
                docs[1]["facts"].append(text)
                value, docs = gate(docs)
                with self.assertRaisesRegex(AuditError, "明确依据"):
                    classification_decision(value, docs, "personnel_incentive")

    def test_model_cannot_restore_the_removed_company_name_issue(self):
        value, docs = gate()
        flags = value["flags"]
        evidence = unknown_checks("personnel_incentive", flags)
        item = next(c for c in evidence["checks"] if c["rule_id"] == "payment_company")
        item.update(status="fail", reason="单笔超过1000元未写公司名称", source_ids=["payment"])
        with self.assertRaisesRegex(AuditError, "条件例外"):
            audit_decision("personnel_incentive", flags, evidence, docs, value["materials"])

    def test_model_cannot_compare_reward_people_with_pos_sku_sales(self):
        value, docs = gate()
        evidence = unknown_checks("personnel_incentive", value["flags"])
        item = next(c for c in evidence["checks"] if c["rule_id"] == "settlement_pos_quantity")
        item.update(status="unknown", reason="人数无法与SKU销量核对", source_ids=["settlement"])
        with self.assertRaisesRegex(AuditError, "条件例外"):
            audit_decision("personnel_incentive", value["flags"], evidence, docs, value["materials"])

    def test_actual_payment_shortfall_is_still_reported(self):
        case, _, numeric = numeric_case("payment_claim_amount", "16000", ["15999"])
        case["flags"]["store_incentive"] = True
        case["documents"].append(sources()[0])
        case["materials"].append({"id": "promotion_contract", "state": "present", "source_ids": ["contract"]})
        evidence = unknown_checks("personnel_incentive", case["flags"])
        index = next(i for i, c in enumerate(evidence["checks"]) if c["rule_id"] == "payment_claim_amount")
        evidence["checks"][index] = numeric
        result = decide(case, evidence, "payment_claim_amount")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["calculations"][0]["right_value"], "15999")

    def test_store_flag_does_not_change_other_seven_business_types(self):
        for scenario in set(MATERIALS) - {"personnel_incentive"}:
            for rule in rules(scenario):
                self.assertEqual(applicability(rule.when, scenario, flags_for(scenario)),
                                 applicability(rule.when, scenario, flags_for(scenario, store_incentive=True)))

    def test_explicit_amount_headers_keep_complete_native_amount_calculations(self):
        material = [{"id": "pos_excel", "state": "present", "source_ids": ["pos"]}]
        for title in ("销售金额", "销售额", "实销金额", "合计金额", "销\n售\n金额", "求和项:销售金额"):
            with self.subTest(title=title):
                result = prepare_pos_arithmetic(native_pos(title), material, [])
                self.assertTrue(result["complete"])
                self.assertEqual(result["tables"][0]["detail_rows"], 2)
                self.assertEqual(len(result["comparisons"]), 4)

    def test_store_tracking_bound_to_settlement_does_not_contaminate_pos(self):
        documents = native_pos() + [{"unit_id": "tracking", "roles": ["spreadsheet"], "facts": [
            "跟踪!R1: C1=门店 | C2=目标金额 | C3=含税pos | C4=奖金",
            "跟踪!R2: C1=甲店 | C2=3000 | C3=17383.56006223 | C4=500"]}]
        material = [{"id": "pos_excel", "state": "present", "source_ids": ["pos"]},
                    {"id": "settlement", "state": "present", "source_ids": ["tracking"]}]
        result = prepare_pos_arithmetic(documents, material, [])
        self.assertTrue(result["complete"])
        self.assertEqual(result["source_ids"], ["pos"])

    def test_store_aggregate_does_not_invent_sku_unit_price(self):
        documents = [{"unit_id": "pos", "facts": [
            "Sheet1!R1: C1=门店 | C2=含税pos | C3=SKU条码 | C4=商品名称 | C5=销售数量",
            "Sheet1!R2: C1=甲店 | C2=17383.56 | C3=6970000000001 | C4=甲商品 | C5=47"]}]
        result = prepare_pos_arithmetic(documents, [{"id": "pos_excel", "state": "present", "source_ids": ["pos"]}], [])
        self.assertFalse(result["complete"])
        self.assertEqual(result["comparisons"], [])

    def test_exact_barcode_keeps_all_records_without_name_disagreement_error(self):
        catalog = {"products": [{"product_code": "A", "barcode_69": "6970000000001", "product_name": "库内甲商品"},
                                {"product_code": "B", "barcode_69": "6970000000001", "product_name": "另一内部营销名称"}]}
        row = {"product_name": {"value": "经销商不同名称"}, "barcode_69": {"value": "6970000000001"}}
        match = Mock(side_effect=AssertionError("Must not call a name model for an exact barcode"))
        result = verify_identity(row, catalog, match)
        self.assertEqual(result["status"], "pass")
        self.assertEqual({p["product_code"] for p in result["references"]}, {"A", "B"})
        match.assert_not_called()

    def test_missing_barcode_still_is_not_filled_from_product_name(self):
        result = verify_identity({"product_name": {"value": "甲商品"}, "barcode_69": None}, {"products": []}, Mock())
        self.assertEqual(result["reason_code"], "barcode_missing")

    def test_original_pos_amount_header_is_not_rewritten_as_a_stamped_material(self):
        for quote in ('“含税pos”', '「含税POS」', '"含税pos"'):
            with self.subTest(quote=quote):
                value = plain_text(f"表内{quote}为门店销售额，不是SKU金额；缺少pos。", [])
                self.assertIn(quote, value)
                self.assertNotIn("含税盖章版", value)
                self.assertIn("缺少盖章版销售明细", value)

    def test_store_reward_internal_condition_is_presented_in_plain_language(self):
        value = plain_text("store_incentive=null，暂时不能确认奖励用途。", [])
        self.assertNotIn("store_incentive", value)
        self.assertNotIn("null", value)
        self.assertIn("这次是否属于门店或区域达成奖励还不清楚", value)


if __name__ == "__main__":
    unittest.main()
