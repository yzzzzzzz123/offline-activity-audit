"""Regression cases for the user's confirmed POS and red-packet rules."""
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
    def test_mandatory_pos_types_require_both_versions(self):
        # Purchased gifts now have an aggregate POS-or-photo requirement;
        # its complete, partial and absent alternatives have separate fixtures.
        for scenario in (s for s in POS_SCENARIOS if s != 'self_procured_gift_material'):
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

    def test_cvs_otc_requires_inventory_and_voluntary_pos_still_requires_pair(self):
        scenario = "entry_fee"
        for uses_pos in (False, True):
            flags = flags_for(scenario, cvs_otc=True, uses_pos=uses_pos)
            value = separated_classification(scenario, flags=flags)
            documents = [{"unit_id": uid} for uid in value["source_ids"]]
            with self.subTest(uses_pos=uses_pos):
                self.assertTrue(classification_decision(value, documents, cvs_otc_channel=True)["matched"])
                materials = {m["id"]: m for m in value["materials"]}
                self.assertEqual(materials["entry_system"]["state"], "present")
                for role in ("pos", "pos_excel"):
                    self.assertEqual(materials[role]["state"], "present" if uses_pos else "not_applicable")
                    if uses_pos:
                        incomplete = separated_classification(scenario, flags=flags, missing=[role])
                        self.assertFalse(classification_decision(incomplete, documents, cvs_otc_channel=True)["matched"])
        for cvs_otc, expected in ((False, False), (None, None)):
            flags = flags_for(scenario, cvs_otc=cvs_otc, uses_pos=True)
            self.assertIs(applicability("entry_pos", scenario, flags), expected)
        unclear = separated_classification(scenario, flags=flags_for(scenario, cvs_otc=None, uses_pos=True))
        with self.assertRaises(AuditError):
            classification_decision(unclear, documents)

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



# Pinned to the eight PDF audit columns and explicit confirmed additions, not materials.
AUDIT_COLUMN_IDS = {'entry_fee': {'entry_agreement',
               'entry_application_products',
               'entry_duplicate',
               'entry_inventory',
               'entry_photo_coverage',
               'entry_sku',
               'entry_watermark'},
 'giveaway_promotion': {'activity_photo_content',
                        'application_products',
                        'claim_ceiling',
                        'contract_activity_dates',
                        'contract_budget_amount',
                        'contract_dealer',
                        'contract_signed',
                        'gift_rule_quantity',
                        'giveaway_evidence',
                        'pos_dealer',
                        'pos_seal',
                        'reward_unit_price',
                        'settlement_activity_dates',
                        'settlement_application_dates',
                        'settlement_calculation',
                        'settlement_dealer',
                        'settlement_seal'},
 'personnel_incentive': {'application_claim_ceiling',
                         'application_products',
                         'application_quantity_price',
                         'claim_ceiling',
                         'contract_activity_dates',
                         'contract_budget_amount',
                         'contract_dealer',
                         'contract_signed',
                         'payment_activity_dates',
                         'payment_claim_amount',
                         'payment_company',
                         'payment_statement_dealer',
                         'payment_statement_seal',
                         'pos_dealer',
                         'pos_seal',
                         'reward_unit_price',
                         'settlement_activity_dates',
                         'settlement_application_dates',
                         'settlement_calculation',
                         'settlement_dealer',
                         'settlement_pos_quantity',
                         'settlement_seal',
                         'staff_headcount',
                         'staff_photo_content',
                         'staff_total_unit_price',
                         'training_photo_content'},
 'pos_target_incentive': {'contract_activity_dates',
                          'contract_budget_amount',
                          'contract_dealer',
                          'contract_signed',
                          'pos_dealer',
                          'pos_sales_amount',
                          'pos_seal',
                          'settlement_activity_dates',
                          'settlement_application_dates',
                          'settlement_dealer',
                          'settlement_seal',
                          'target_reward'},
 'poster_material': {'claim_ceiling',
                     'contract_activity_dates',
                     'contract_budget_amount',
                     'contract_dealer',
                     'contract_signed',
                     'finished_photo',
                     'invoice_amount',
                     'invoice_details',
                     'material_list_amount',
                     'posting_before_after',
                     'settlement_activity_dates',
                     'settlement_application_dates',
                     'settlement_dealer',
                     'settlement_template_seal'},
 'price_difference_support': {'activity_photo_content',
                              'application_products',
                              'application_quantity_price',
                              'claim_ceiling',
                              'contract_activity_dates',
                              'contract_budget_amount',
                              'contract_dealer',
                              'contract_signed',
                              'full_reduction_evidence',
                              'photo_activity_dates',
                              'pos_dealer',
                              'pos_seal',
                              'price_evidence',
                              'price_matches_contract',
                              'reward_unit_price',
                              'settlement_activity_dates',
                              'settlement_application_dates',
                              'settlement_calculation',
                              'settlement_dealer',
                              'settlement_pos_quantity',
                              'settlement_seal'},
 'promotional_display': {'application_claim_ceiling',
                         'atrium_agreement',
                         'contract_activity_dates',
                         'contract_budget_amount',
                         'contract_dealer',
                         'contract_signed',
                         'display_brand',
                         'display_material_brand',
                         'display_quantity_price',
                         'display_size',
                         'display_watermark',
                         'settlement_activity_dates',
                         'settlement_application_dates',
                         'settlement_calculation',
                         'settlement_dealer',
                         'settlement_template_seal'},
 'self_procured_gift_material': {'application_quantity_price',
                                 'claim_ceiling',
                                 'contract_activity_dates',
                                 'contract_budget_amount',
                                 'contract_dealer',
                                 'contract_signed',
                                 'gift_finished_photos',
                                 'gift_rule_present',
                                 'gift_rule_quantity',
                                 'gift_store_photo_content',
                                 'invoice_amount',
                                 'invoice_details',
                                 'pos_dealer',
                                 'pos_seal',
                                 'settlement_activity_dates',
                                 'settlement_application_dates',
                                 'settlement_calculation',
                                 'settlement_dealer',
                                 'settlement_seal'}}


class AuditColumnScopeTests(unittest.TestCase):
    def test_each_type_matches_its_audit_column_and_every_rule_has_a_source(self):
        for scenario, expected in AUDIT_COLUMN_IDS.items():
            with self.subTest(scenario=scenario):
                actual = rules(scenario)
                common = {"pos_fields", "pos_arithmetic", "pos_product_identity"}
                meeting = {'stamped_document_pdf'} | ({'gift_activity_evidence'} if scenario == 'self_procured_gift_material' else set())
                additions = {'personnel_incentive': {'pos_activity_dates','contract_reward_unit_price'},
                    'promotional_display': {'claim_ceiling'}, 'poster_material': {'receipt_finance_seal','poster_payment_dates'},
                    'self_procured_gift_material': {'pos_activity_dates','receipt_finance_seal'},
                    'price_difference_support': {'pos_activity_dates'}, 'giveaway_promotion': {'pos_activity_dates'},
                    'pos_target_incentive': {'claim_ceiling','pos_activity_dates'}}
                self.assertEqual({r.id for r in actual}, expected | common | meeting | additions.get(scenario,set()))
                self.assertTrue(all('Sheet1!H' in r.source or r.source.startswith('用户确认') for r in actual))

    def test_material_only_and_background_audits_are_rejected_for_every_type(self):
        forbidden = {"scene_authenticity", "fraud", "bi_quota", "submission_deadline", "review_deadline",
                     "pos_excel_present", "pos_content_consistency", "pos_numeric_consistency",
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
                                      ("self_procured_gift_material", "pos_excel"),
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
            self.assertEqual("settlement_seal" in checks, scenario not in {"entry_fee","promotional_display","poster_material"})
            self.assertEqual("contract_signed" in checks, scenario != "entry_fee")

    def test_unsigned_contract_is_present_at_gate_and_only_audited_in_self_procured_gifts(self):
        for scenario in ("personnel_incentive", "self_procured_gift_material"):
            value = separated_classification(scenario)
            documents = [{"unit_id": uid, "facts": ["本次促销合同未签章"]} for uid in value["source_ids"]]
            self.assertTrue(classification_decision(value, documents)["matched"])
            flags = flags_for(scenario)
            evidence = unknown_checks(scenario, flags)
            if scenario in {"personnel_incentive","self_procured_gift_material"}:
                check = next(c for c in evidence["checks"] if c["rule_id"] == "contract_signed")
                contract = next(m for m in value["materials"] if m["id"] == "promotion_contract")
                check.update(status="fail", source_ids=contract["source_ids"], reason="本次促销合同已收到，但未签章")
            actual = audit_decision(scenario, flags, evidence, documents, value["materials"])
            self.assertEqual(next(c for c in actual["checks"] if c["rule_id"] == "contract_signed")["status"], "fail")

    def test_red_packet_and_wages_compare_actual_claim_with_separate_brand_applicability(self):
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
        flags = flags_for(scenario, red_packet=False, temporary_staff=True, sales_commission=False)
        actual = audit_decision(scenario, flags, unknown_checks(scenario, flags), [])
        self.assertEqual(next(c for c in actual['checks'] if c['rule_id']=='payment_company')['status'],'not_applicable')
        self.assertNotEqual(next(c for c in actual['checks'] if c['rule_id']=='payment_claim_amount')['status'],'not_applicable')


class MultipleRedPacketTests(unittest.TestCase):
    def test_audit_lists_payment_originals_without_preloading_them(self):
        # Included images beyond the first six-page transcription batch stay
        # addressable by filename; none is attached unless the model asks.
        for scenario, red_packet in (("personnel_incentive", True),
                                     ("personnel_incentive", False),
                                     ("poster_material", False)):
            with self.subTest(scenario=scenario, red_packet=red_packet), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                units = [{"unit_id": f"p{i}", "image": root / f"p{i}.png"} for i in range(8)]
                units.append({"unit_id": "unrelated", "image": root / "unrelated.png"})
                flags = flags_for(scenario, red_packet=red_packet)
                evidence = unknown_checks(scenario, flags)
                documents = [{"unit_id": unit["unit_id"], "facts": ["可见原始记录"]} for unit in units]
                materials = [{"id": "payment", "state": "present", "source_ids": [f"p{i}" for i in range(8)]}]
                provider = PdfEvidenceProvider("gpt-6-astra", "medium", stage_summary_reviewer=isolated_stage_summary)
                with patch.object(provider, "_call", return_value=evidence) as call:
                    provider.audit({"scenario": scenario, "archive_id": "a001", "flags": flags,
                                    "materials": materials, "documents": documents, "units": units}, root)
                images = call.call_args.kwargs["images"]
                prompt = render_prompt(call.call_args.args[3])
                self.assertEqual(images, [])
                for unit in units:
                    self.assertIn(f'"unit_id": "{unit["unit_id"]}", "image": '
                                  f'"{unit["image"].name}"', prompt)
                if scenario == "personnel_incentive" and red_packet:
                    self.assertIn(PAYMENT_COMPANY_CLARIFICATION, prompt)
                    self.assertIn("参半、小箭头或重点品牌字样", prompt)
                    self.assertIn("不按1000元门槛检查经销商公司名称", prompt)
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
