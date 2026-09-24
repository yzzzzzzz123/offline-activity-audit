"""Regression cases for the user's confirmed POS and red-packet rules."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision, classification_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import PAYMENT_COMPANY_CLARIFICATION, applicability, requirements, rules
from tests.pdf_test_support import classification, flags_for, unknown_checks


POS_SCENARIOS = (
    "personnel_incentive", "giveaway_promotion", "price_difference_support",
    "self_procured_gift_material", "pos_target_incentive",
)


def separated_classification(scenario, *, flags=None, missing=()):
    """Keep the two POS versions separate even when other roles share a page."""
    value = classification(scenario, ["stamped-pos", "excel", "other"], flags=flags, missing=missing)
    selected = next(m for m in value["material_matches"] if m["scenario"] == scenario)
    for item in selected["materials"]:
        if item["state"] in {"present", "unclear"}:
            item["source_ids"] = [{"pos": "stamped-pos", "pos_excel": "excel"}.get(item["id"], "other")]
    covered = {uid for item in selected["materials"] for uid in item["source_ids"]}
    unassigned = [uid for uid in ("stamped-pos", "excel", "other") if uid not in covered]
    if unassigned:
        # Absorb synthetic bookkeeping sources into an existing non-POS role.
        support = next(item for item in selected["materials"]
                       if item["state"] == "present" and item["id"] not in {"pos", "pos_excel"})
        support["source_ids"].extend(unassigned)
    return value


class ConfirmedPosMaterialTests(unittest.TestCase):
    def test_every_existing_pos_type_requires_both_versions(self):
        for scenario in POS_SCENARIOS:
            with self.subTest(scenario=scenario):
                self.assertTrue({"pos", "pos_excel"}.issubset({r.id for r in requirements(scenario)}))
                value = separated_classification(scenario)
                documents = [{"unit_id": uid} for uid in value["source_ids"]]
                self.assertTrue(classification_decision(value, documents)["matched"])
                for missing in ("pos", "pos_excel"):
                    incomplete = separated_classification(scenario, missing=[missing])
                    self.assertFalse(classification_decision(incomplete, documents)["matched"])

    def test_training_exemption_is_preserved_but_optional_pos_must_be_a_pair(self):
        scenario = "personnel_incentive"
        for uses_pos in (False, True):
            flags = flags_for(scenario, training=True, uses_pos=uses_pos)
            value = separated_classification(scenario, flags=flags)
            if uses_pos:
                for item in value["materials"]:
                    if item["id"] in {"pos", "pos_excel"}:
                        item.update(state="present", source_ids=["stamped-pos" if item["id"] == "pos" else "excel"])
            documents = [{"unit_id": uid} for uid in value["source_ids"]]
            self.assertTrue(classification_decision(value, documents)["matched"])
            if uses_pos:
                for missing_version in ("pos", "pos_excel"):
                    with self.subTest(missing_version=missing_version):
                        incomplete = deepcopy(value)
                        next(m for m in incomplete["materials"] if m["id"] == missing_version).update(
                            state="not_applicable", source_ids=[])
                        try:
                            decision = classification_decision(incomplete, documents)
                        except AuditError:
                            decision = None
                        if decision is not None:
                            self.assertFalse(decision["matched"], "培训自愿交POS也不能只交一个版本")
                        missing = deepcopy(value)
                        next(m for m in missing["materials"] if m["id"] == missing_version).update(
                            state="missing", source_ids=[], reason="培训自愿提供POS时缺少另一版本")
                        next(m for m in missing["material_matches"] if m["scenario"] == scenario)["supported"] = False
                        missing.update(candidate_scenarios=[], materials=[])
                        self.assertFalse(classification_decision(missing, documents)["matched"])
            else:
                checks = unknown_checks(scenario, flags)
                self.assertTrue(all(c["status"] == "not_applicable" for c in checks["checks"]
                                    if c["rule_id"] in {"pos_fields", "pos_arithmetic"}))

    def test_cvs_otc_entry_inventory_choice_is_preserved_and_pos_choice_requires_pair(self):
        scenario = "entry_fee"
        for uses_pos in (False, True):
            flags = flags_for(scenario, cvs_otc=True, uses_pos=uses_pos)
            value = separated_classification(scenario, flags=flags)
            documents = [{"unit_id": uid} for uid in value["source_ids"]]
            with self.subTest(uses_pos=uses_pos):
                self.assertTrue(classification_decision(value, documents)["matched"])
                materials = {m["id"]: m for m in value["materials"]}
                self.assertEqual(materials["entry_system"]["state"], "present")
                for role in ("pos", "pos_excel"):
                    self.assertEqual(materials[role]["state"], "present" if uses_pos else "not_applicable")
                    if uses_pos:
                        incomplete = separated_classification(scenario, flags=flags, missing=[role])
                        self.assertFalse(classification_decision(incomplete, documents)["matched"])
        for cvs_otc, expected in ((False, False), (None, None)):
            flags = flags_for(scenario, cvs_otc=cvs_otc, uses_pos=True)
            self.assertIs(applicability("entry_pos", scenario, flags), expected)
        unclear = separated_classification(scenario, flags=flags_for(scenario, cvs_otc=None, uses_pos=True))
        self.assertFalse(classification_decision(unclear, documents)["matched"])

    def test_pos_is_optional_for_display_and_poster_but_checked_when_present(self):
        for scenario in ("promotional_display", "poster_material"):
            for uses_pos in (False, True):
                flags = flags_for(scenario, uses_pos=uses_pos)
                pos = [r for r in requirements(scenario) if r.id in {"pos", "pos_excel"}]
                self.assertEqual(len(pos), 2)
                self.assertTrue(all(applicability(r.when, scenario, flags) is uses_pos for r in pos))
                value = separated_classification(scenario, flags=flags)
                documents = [{"unit_id": uid} for uid in value["source_ids"]]
                self.assertTrue(classification_decision(value, documents)["matched"])
                if uses_pos:
                    incomplete = separated_classification(scenario, flags=flags, missing=["pos_excel"])
                    self.assertFalse(classification_decision(incomplete, documents)["matched"])



# Pinned independently to the eight PDF audit columns, not inferred from materials.
AUDIT_COLUMN_IDS = {
    "promotional_display": {"display_watermark", "display_brand", "display_material_brand", "atrium_agreement", "settlement_template_seal", "display_quantity_price", "display_size"},
    "personnel_incentive": {"pos_fields", "pos_arithmetic", "reward_unit_price", "settlement_pos_quantity", "claim_ceiling", "payment_company", "payment_claim_amount", "staff_daily_photos", "application_quantity_price"},
    "poster_material": {"invoice_details", "finished_photo", "settlement_template_seal"},
    "self_procured_gift_material": {"gift_rule_present", "pos_fields", "pos_arithmetic", "gift_rule_quantity", "gift_finished_photos", "settlement_seal", "contract_signed"},
    "price_difference_support": {"pos_fields", "pos_arithmetic", "reward_unit_price", "settlement_pos_quantity", "claim_ceiling", "full_reduction_evidence", "price_evidence", "application_quantity_price"},
    "giveaway_promotion": {"pos_fields", "pos_arithmetic", "reward_unit_price", "settlement_pos_quantity", "claim_ceiling", "giveaway_evidence", "application_quantity_price"},
    "entry_fee": {"entry_agreement", "entry_sku", "entry_watermark", "entry_duplicate"},
    "pos_target_incentive": {"pos_fields", "pos_arithmetic", "reward_unit_price", "settlement_pos_quantity", "claim_ceiling", "application_quantity_price"},
}


class AuditColumnScopeTests(unittest.TestCase):
    def test_each_type_matches_its_audit_column_and_every_rule_has_a_source(self):
        for scenario, expected in AUDIT_COLUMN_IDS.items():
            with self.subTest(scenario=scenario):
                actual = rules(scenario)
                common = {"pos_fields", "pos_arithmetic", "pos_product_identity"}
                self.assertEqual({r.id for r in actual}, expected | common)
                self.assertTrue(all(r.source == "用户2026-09-22确认·所有核销方式POS通用标准" if r.id in common
                                    else r.source.startswith("PDF第1页") and "审核要点" in r.source for r in actual))

    def test_material_only_and_background_audits_are_rejected_for_every_type(self):
        forbidden = {"scene_authenticity", "fraud", "bi_quota", "submission_deadline", "review_deadline",
                     "pos_seal", "pos_excel_present", "pos_content_consistency", "pos_numeric_consistency",
                     "payment_application_amount", "payment_evidence", "gift_all_store_photos", "entry_system"}
        for scenario in AUDIT_COLUMN_IDS:
            for rule_id in forbidden:
                with self.subTest(scenario=scenario, rule_id=rule_id):
                    flags = flags_for(scenario)
                    evidence = unknown_checks(scenario, flags)
                    evidence["checks"][0]["rule_id"] = rule_id
                    with self.assertRaises(AuditError):
                        audit_decision(scenario, flags, evidence, [])

    def test_material_gate_keeps_files_even_when_they_have_no_independent_audit(self):
        for scenario, material_id in (("personnel_incentive", "settlement"),
                                      ("personnel_incentive", "promotion_contract"),
                                      ("self_procured_gift_material", "invoice"),
                                      ("self_procured_gift_material", "gift_store_photos"),
                                      ("giveaway_promotion", "gift_rule")):
            with self.subTest(scenario=scenario, material=material_id):
                value = separated_classification(scenario)
                documents = [{"unit_id": uid} for uid in value["source_ids"]]
                self.assertTrue(classification_decision(value, documents)["matched"])
                missing = separated_classification(scenario, missing=[material_id])
                self.assertFalse(classification_decision(missing, documents)["matched"])

    def test_template_and_signature_requirements_do_not_spread_between_types(self):
        for scenario in AUDIT_COLUMN_IDS:
            checks = {r.id for r in rules(scenario)}
            self.assertEqual("settlement_template_seal" in checks, scenario in {"promotional_display", "poster_material"})
            self.assertEqual("settlement_seal" in checks, scenario == "self_procured_gift_material")
            self.assertEqual("contract_signed" in checks, scenario == "self_procured_gift_material")

    def test_unsigned_contract_is_present_at_gate_and_only_audited_in_self_procured_gifts(self):
        for scenario in ("personnel_incentive", "self_procured_gift_material"):
            value = separated_classification(scenario)
            documents = [{"unit_id": uid, "facts": ["本次促销合同未签章"]} for uid in value["source_ids"]]
            self.assertTrue(classification_decision(value, documents)["matched"])
            flags = flags_for(scenario)
            evidence = unknown_checks(scenario, flags)
            if scenario == "self_procured_gift_material":
                check = next(c for c in evidence["checks"] if c["rule_id"] == "contract_signed")
                contract = next(m for m in value["materials"] if m["id"] == "promotion_contract")
                check.update(status="fail", source_ids=contract["source_ids"], reason="本次促销合同已收到，但未签章")
            actual = audit_decision(scenario, flags, evidence, documents, value["materials"])
            if scenario == "personnel_incentive":
                self.assertNotIn("contract_signed", {c["rule_id"] for c in actual["checks"]})
            else:
                self.assertEqual(next(c for c in actual["checks"] if c["rule_id"] == "contract_signed")["status"], "fail")

    def test_red_packet_compares_claim_not_application_and_wages_do_not_inherit_it(self):
        scenario = "personnel_incentive"
        flags = flags_for(scenario, red_packet=True)
        documents = [{"unit_id": "p", "facts": ["红包金额=900"]}, {"unit_id": "s", "facts": ["核销金额=1000"]}]
        evidence = unknown_checks(scenario, flags)
        check = next(c for c in evidence["checks"] if c["rule_id"] == "payment_claim_amount")
        check.update(status="pass", source_ids=["p", "s"], reason="模型误判为一致", comparisons=[{
            "label": "核销金额与红包合计", "value_kind": "amount",
            "left": {"unit_id": "s", "quote": "核销金额=1000", "number": "1000"},
            "right": [{"unit_id": "p", "quote": "红包金额=900", "number": "900"}], "operation": "sum", "operator": "le"}])
        actual = audit_decision(scenario, flags, evidence, documents)
        self.assertEqual(next(c for c in actual["checks"] if c["rule_id"] == "payment_claim_amount")["status"], "fail")
        self.assertNotIn("payment_application_amount", {c["rule_id"] for c in actual["checks"]})
        flags = flags_for(scenario, red_packet=False)
        actual = audit_decision(scenario, flags, unknown_checks(scenario, flags), [])
        self.assertTrue(all(c["status"] == "not_applicable" for c in actual["checks"] if c["rule_id"] in {"payment_company", "payment_claim_amount"}))


class MultipleRedPacketTests(unittest.TestCase):
    def test_audit_receives_all_payment_images_for_cross_image_comparison(self):
        # Includes images beyond the first six-page transcription batch.
        for scenario, red_packet, expected_count in (("personnel_incentive", True, 8),
                                                      ("personnel_incentive", False, 0),
                                                      ("poster_material", False, 0)):
            with self.subTest(scenario=scenario, red_packet=red_packet), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                units = [{"unit_id": f"p{i}", "image": root / f"p{i}.png"} for i in range(8)]
                units.append({"unit_id": "unrelated", "image": root / "unrelated.png"})
                flags = flags_for(scenario, red_packet=red_packet)
                evidence = unknown_checks(scenario, flags)
                documents = [{"unit_id": unit["unit_id"], "facts": ["可见原始记录"]} for unit in units]
                materials = [{"id": "payment", "state": "present", "source_ids": [f"p{i}" for i in range(8)]}]
                provider = PdfEvidenceProvider("gpt-6-astra", "medium")
                with patch.object(provider, "_call", return_value=evidence) as call:
                    provider.audit({"scenario": scenario, "archive_id": "a001", "flags": flags,
                                    "materials": materials, "documents": documents, "units": units}, root)
                images = call.call_args.kwargs["images"]
                payment_paths = {unit["image"] for unit in units}
                self.assertEqual([image for image in images if image in payment_paths],
                                 [unit["image"] for unit in units[:expected_count]])
                self.assertNotIn(root / "unrelated.png", images)
                if scenario == "personnel_incentive" and red_packet:
                    prompt = render_prompt(call.call_args.args[3])
                    self.assertIn(PAYMENT_COMPANY_CLARIFICATION, prompt)
                    self.assertIn("单笔>1000元须有名称，单笔<=1000元不强制", prompt)
                    self.assertIn("600元+600元虽合计1200元，两笔均不触发名称要求", prompt)
                    self.assertNotIn("检查大额品牌公司名称", prompt)

    def fixture(self, amounts, claim="1000"):
        scenario = "personnel_incentive"
        flags = flags_for(scenario, red_packet=True)
        documents = [{"unit_id": "s", "facts": [f"本次核销金额={claim}"]}]
        terms = []
        for index, amount in enumerate(amounts, start=1):
            uid = f"p{index}"
            quote = f"本次红包第{index}笔金额={amount}"
            documents.append({"unit_id": uid, "facts": [quote]})
            terms.append({"unit_id": uid, "quote": quote, "number": amount})
        evidence = unknown_checks(scenario, flags)
        check = next(c for c in evidence["checks"] if c["rule_id"] == "payment_claim_amount")
        check.update(status="pass", reason="这些红包属于本次核销", source_ids=[d["unit_id"] for d in documents],
                     comparisons=[{"label": "本次核销金额与本次红包合计", "value_kind": "amount", "operator": "le", "operation": "sum",
                                   "left": {"unit_id": "s", "quote": f"本次核销金额={claim}", "number": claim},
                                   "right": terms}])
        return flags, documents, evidence, check

    def test_combined_packets_match_claim_even_when_each_packet_does_not(self):
        # Different payments of the same amount must both contribute to the sum.
        for amounts in (("600", "400"), ("500", "500"), ("600.25", "399.75"), ("600", "500")):
            with self.subTest(amounts=amounts):
                flags, documents, evidence, _ = self.fixture(amounts)
                result = audit_decision("personnel_incentive", flags, evidence, documents)
                actual = next(c for c in result["checks"] if c["rule_id"] == "payment_claim_amount")
                self.assertEqual(actual["status"], "pass")
                self.assertTrue(actual["calculations"][0]["matches"])
                self.assertEqual(len(actual["calculations"][0]["right"]), 2)

    def test_program_rejects_insufficient_packet_total_even_when_model_says_pass(self):
        for amounts, total in ((("600", "300"), "900"), (("600", "399.99"), "999.99")):
            with self.subTest(amounts=amounts):
                flags, documents, evidence, _ = self.fixture(amounts)
                result = audit_decision("personnel_incentive", flags, evidence, documents)
                actual = next(c for c in result["checks"] if c["rule_id"] == "payment_claim_amount")
                self.assertEqual(actual["status"], "fail")
                self.assertEqual(actual["calculations"][0]["right_value"], total)
                self.assertEqual(actual["calculations"][0]["left_value"], "1000")

    def test_uncertain_packet_membership_does_not_become_zero_or_missing(self):
        flags, documents, evidence, check = self.fixture(("600", "400"))
        reason = "第二张红包截图未能与本次活动对应，暂时不能确认本次红包合计。"
        check.update(status="unknown", reason=reason, comparisons=[])
        result = audit_decision("personnel_incentive", flags, evidence, documents)
        actual = next(c for c in result["checks"] if c["rule_id"] == "payment_claim_amount")
        self.assertEqual(actual["status"], "unknown")
        self.assertEqual(actual["reason"], reason)
        self.assertEqual(actual["calculations"], [])

    def test_model_cannot_substitute_invented_total_for_raw_packet_amounts(self):
        flags, documents, evidence, check = self.fixture(("600", "400"))
        check["comparisons"][0]["right"] = [{"unit_id": "p1", "quote": "红包合计=1000", "number": "1000"}]
        with self.assertRaises(AuditError):
            audit_decision("personnel_incentive", flags, evidence, documents)

    def test_completed_partial_record_keeps_both_image_sources_but_counts_payment_once(self):
        flags, documents, evidence, check = self.fixture(("600", "400"))
        documents.append({"unit_id": "partial", "facts": ["下方红包记录被截断，与第二张图的上方记录重叠"]})
        check["source_ids"].append("partial")
        check["reason"] = "半条记录在第二张图中补全，是同一笔400元红包，本次两笔合计1000元。"
        result = audit_decision("personnel_incentive", flags, evidence, documents)
        actual = next(c for c in result["checks"] if c["rule_id"] == "payment_claim_amount")
        self.assertEqual(actual["status"], "pass")
        self.assertIn("partial", actual["source_ids"])
        self.assertEqual(actual["calculations"][0]["right_value"], "1000")
        self.assertEqual(len(actual["calculations"][0]["right"]), 2)


if __name__ == "__main__":
    unittest.main()
