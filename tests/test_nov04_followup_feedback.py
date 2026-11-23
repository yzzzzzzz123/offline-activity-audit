"""Source-backed regression for barcode, temporary staff and store coverage."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from audit_core.common import AuditError
from audit_core.giveaway_coverage import giveaway_coverage_check
from audit_core.pdf_evidence import audit_schema, audit_decision, classification_decision
from audit_core.pdf_workflow import PdfWorkflow
from audit_core.pdf_policy import applicability, rules
from audit_core.pos_arithmetic import _columns, prepare_pos_arithmetic
from audit_core.pos_product_identity import verify_identity
from audit_core.qince_context import context_sources, entry_context_sources, VERIFICATION, APPLICATIONS, PLANS
from tests.pdf_test_support import flags_for, unknown_checks, PolicyProvider, bundle, classification


def catalog():
    return {"products": [{"product_code": "A", "barcode_69": "6970000000001", "product_name": "甲牙膏100g"},
                         {"product_code": "B", "barcode_69": "6970000000001", "product_name": "乙牙刷3支"}]}


def identity(name="经销商自建商品", code="6970000000001"):
    return {k: {"value": v} if v is not None else None for k, v in (("product_name", name), ("barcode_69", code))}


def qince_fixture():
    return {VERIFICATION: [{"verify_code": "HX_TEST", "details": [
        {"id": "V1", "activity_code": "SQ1", "activity_cm_id": "R1", "customer_id": "C1", "customer_name": "甲店"},
        {"id": "V2", "activity_code": "SQ1", "activity_cm_id": "R2", "customer_id": "C1", "customer_name": "甲店"}]}],
        APPLICATIONS: [{"applyCode": "SQ1", "subList": [
            {"subId": "R1", "cusId": "C1", "cusName": "甲店", "pdNames": "牙膏", "pdCodes": "P1"},
            {"subId": "R2", "cusId": "C1", "cusName": "甲店", "pdNames": "牙刷", "pdCodes": "P2"},
            {"subId": "OTHER", "cusId": "C2", "cusName": "无关店", "pdNames": "其他商品"}]}], PLANS: []}


def coverage_fixture(total=15, covered=5):
    names = [f"活动门店{i:02d}" for i in range(1, total+1)]
    documents = [{"unit_id": "contract", "facts": ["本次活动门店："+"、".join(names)]}]
    documents += [{"unit_id": f"proof{i}", "facts": [name+"：本店搭赠活动小票，购买商品、赠品、购买日期时间齐全"]}
                  for i, name in enumerate(names[:covered])]
    materials = [{"id": "promotion_contract", "state": "present", "source_ids": ["contract"]},
                 {"id": "giveaway_evidence", "state": "present", "source_ids": [d["unit_id"] for d in documents[1:]]}]
    evidence = {"scope_complete": True, "scope_source_ids": ["contract"],
        "stores": [{"name": name, "source": {"unit_id": "contract", "quote": documents[0]["facts"][0]}} for name in names],
        "evidence": [{"unit_id": f"proof{i}", "store_name": name, "store_quote": name, "valid": True,
                      "reason": "本张实际小票信息完整"} for i, name in enumerate(names[:covered])], "limitations": []}
    return evidence, documents, materials, []


class Nov04FeedbackTests(unittest.TestCase):
    def test_verified_qince_scope_can_satisfy_entry_basis_without_protocol_file(self):
        units, docs = entry_context_sources(qince_fixture(), "HX_TEST", "a")
        value = classification("entry_fee", [u["unit_id"] for u in units])
        value["material_matches"] = [m for m in value["material_matches"] if m["scenario"] == "entry_fee"]
        self.assertTrue(classification_decision(value, docs, "entry_fee")["matched"])

    def test_plain_partial_qince_records_cannot_bypass_entry_protocol_gate(self):
        units, docs = context_sources(qince_fixture(), "HX_TEST", "a")
        value = classification("entry_fee", [u["unit_id"] for u in units])
        value["material_matches"] = [m for m in value["material_matches"] if m["scenario"] == "entry_fee"]
        with self.assertRaisesRegex(AuditError, "精确关联"):
            classification_decision(value, docs, "entry_fee")

    def test_complete_verified_entry_scope_can_also_cite_current_application_supplement(self):
        units, docs = entry_context_sources(qince_fixture(), 'HX_TEST', 'a')
        supplement = {'unit_id': 'current-application-supplement',
            'roles': ['勤策本次业务补充明细（独立来源）'],
            'facts': ['verify_code="HX_TEST"', 'application_rows=[{"applyCode":"SQ1","subId":"R1"}]'],
            'limitations': []}
        docs.append(supplement)
        before = deepcopy(docs)
        value = classification('entry_fee', [d['unit_id'] for d in docs])
        value['material_matches'] = [m for m in value['material_matches'] if m['scenario'] == 'entry_fee']
        self.assertTrue(classification_decision(value, docs, 'entry_fee')['matched'])
        self.assertEqual(docs, before)

    def test_supplement_does_not_allow_omitting_another_verified_customer_scope(self):
        raw = qince_fixture()
        raw[VERIFICATION][0]['details'].append({'id': 'V3', 'activity_code': 'SQ1',
            'activity_cm_id': 'R3', 'customer_id': 'C2', 'customer_name': '乙店'})
        raw[APPLICATIONS][0]['subList'].append({'subId': 'R3', 'cusId': 'C2',
            'cusName': '乙店', 'pdNames': '牙膏', 'pdCodes': 'P3'})
        units, docs = entry_context_sources(raw, 'HX_TEST', 'a')
        self.assertEqual(len(units), 2)
        value = classification('entry_fee', [d['unit_id'] for d in docs])
        value['material_matches'] = [m for m in value['material_matches'] if m['scenario'] == 'entry_fee']
        for materials in (value['materials'], value['material_matches'][0]['materials']):
            item = next(m for m in materials if m['id'] == 'entry_agreement')
            item['source_ids'] = [units[0]['unit_id']]
        with self.assertRaisesRegex(AuditError, '精确关联'):
            classification_decision(value, docs, 'entry_fee')

    def test_native_final_unlabelled_total_requires_all_detail_values(self):
        facts = ["销售!R1: C1=商品名称 | C2=条码 | C3=数量 | C4=单价 | C5=销售金额",
                 "销售!R2: C1=甲 | C2=6970000000001 | C3=2 | C4=2 | C5=4",
                 "销售!R3: C1=乙 | C2=6970000000002 | C3=2 | C4=3 | C5=6",
                 "销售!R4: C3=4 | C5=10"]
        materials = [{"id": "pos_excel", "state": "present", "source_ids": ["xls"]}]
        for final, complete in ((facts[-1], True), ("销售!R4: C3=3 | C5=10", False),
                                ("销售!R4: C3=4 | C5=9.99", False)):
            result = prepare_pos_arithmetic([{"unit_id": "xls", "facts": facts[:-1]+[final]}], materials, [])
            self.assertIs(result["complete"], complete)
            if complete:
                self.assertEqual(result["tables"][0]["detail_rows"], 2)
                self.assertEqual(result["tables"][0]["total_rows"], [4])

    def test_entry_business_context_reaches_the_real_workflow_gate(self):
        provider = PolicyProvider(scenarios=("entry_fee",))
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            bundle(root/"input")
            flow = PdfWorkflow("20261104-entry", "fixture", root/"input", root/"temporary", provider,
                               None, None, biz_type="条码费", verify_code="HX_TEST", qince_data=qince_fixture())
            flow.run()
            gate = next(c for c in provider.calls if c["kind"] == "pdf_material_classification")
            self.assertEqual(sum(u.get("source_kind") == "qince" for u in gate["units"]), 1)
            self.assertEqual(sum(u.get("source_kind") == "qince" for u in flow.packets[0]["units"]), 1)

    def test_decision_publishes_program_missing_store_difference_without_mutating_model(self):
        coverage, documents, materials, units = coverage_fixture()
        flags = flags_for("giveaway_promotion")
        evidence = unknown_checks("giveaway_promotion", flags)
        evidence["giveaway_coverage"] = coverage
        original = deepcopy(evidence)
        result = audit_decision("giveaway_promotion", flags, evidence, documents, materials, units=units)
        check = next(c for c in result["checks"] if c["rule_id"] == "giveaway_evidence")
        self.assertEqual(check["status"], "fail")
        self.assertEqual(len(check["coverage"]["missing_stores"]), 10)
        self.assertEqual(evidence, original)

    def test_complete_scope_cannot_omit_linked_qince_customer(self):
        args = list(coverage_fixture(1, 1))
        args[3] = context_sources(qince_fixture(), "HX_TEST", "q")[0]
        with self.assertRaises(AuditError):
            giveaway_coverage_check(*args)

    def test_exact_barcode_overrides_dealer_name_and_keeps_all_records(self):
        match = Mock(side_effect=AssertionError("must not ask semantic model"))
        result = verify_identity(identity(), catalog(), match)
        self.assertEqual(result["status"], "pass")
        self.assertEqual([r["product_code"] for r in result["references"]], ["A", "B"])
        self.assertNotIn("reference", result)
        match.assert_not_called()

    def test_missing_name_is_still_missing(self):
        self.assertEqual(verify_identity(identity(None), catalog(), Mock())["status"], "unknown")

    def test_barcode_is_not_normalized_or_replaced_by_name(self):
        for code in ("06970000000001", "6970000000001.0", "697000000000", "missing"):
            result = verify_identity(identity("甲牙膏100g", code), catalog(), Mock())
            self.assertEqual(result["reason_code"], "catalog_product_missing")
            self.assertEqual(result["submitted"]["barcode_69"], code)

    def test_temporary_staff_only_waives_sku_settlement_quantity(self):
        for temporary, expected in ((True, False), (False, True), (None, True)):
            flags = flags_for("personnel_incentive", temporary_staff=temporary)
            by_id = {r.id: r for r in rules("personnel_incentive")}
            self.assertIs(applicability(by_id["settlement_pos_quantity"].when, "personnel_incentive", flags), expected)
            for key in ("pos_fields", "pos_arithmetic", "pos_product_identity"):
                self.assertIs(applicability(by_id[key].when, "personnel_incentive", flags), temporary is not True)

    def test_other_types_keep_settlement_quantity_check(self):
        for scenario in ("price_difference_support",):
            rule = next(r for r in rules(scenario) if r.id == "settlement_pos_quantity")
            self.assertTrue(applicability(rule.when, scenario, flags_for(scenario, temporary_staff=True)))

    def test_date_title_sales_amount_layout_keeps_explicit_unit_price(self):
        cells = {i: {"raw": value} for i, value in enumerate(
            ("系统", "商品名称", "条码", "零售价", "2026年08月07日-2026年08月09日", "数量"), 1)}
        self.assertEqual(_columns(cells), {"quantity": 6, "price": 4, "amount": 5})
        cells[4]["raw"] = "采购价"
        self.assertIsNone(_columns(cells))

    def test_ambiguous_date_columns_do_not_choose_one(self):
        cells = {i: {"raw": value} for i, value in enumerate(
            ("商品名称", "条码", "零售价", "数量", "2026年08月07日-2026年08月09日", "2026年08月10日-2026年08月12日"), 1)}
        self.assertIsNone(_columns(cells))

    def test_actual_qince_field_names_are_projected(self):
        _, docs = context_sources(qince_fixture(), "HX_TEST", "a")
        text = str(docs)
        self.assertIn('customer_name=', text)
        self.assertIn('customer_id=', text)
        self.assertNotIn("无关店", text)

    def test_entry_groups_duplicate_sku_rows_without_losing_source_links(self):
        units, docs = entry_context_sources(qince_fixture(), "HX_TEST", "a")
        self.assertEqual(len(units), 1)
        text = str(docs)
        for value in ("V1", "V2", "R1", "R2", "牙膏", "牙刷"):
            self.assertIn(value, text)
        self.assertNotIn("无关店", text)

    def test_entry_conflicting_customer_cannot_generate_compact_basis(self):
        raw = qince_fixture()
        raw[VERIFICATION][0]["details"][0]["customer_id"] = "OTHER"
        units, _ = entry_context_sources(raw, "HX_TEST", "a")
        self.assertTrue(all("-qince-entry-" not in u["unit_id"] for u in units))

    def test_entry_incomplete_product_scope_cannot_generate_compact_basis(self):
        raw = qince_fixture()
        del raw[APPLICATIONS][0]["subList"][0]["pdNames"]
        units, _ = entry_context_sources(raw, "HX_TEST", "a")
        self.assertTrue(all("-qince-entry-" not in u["unit_id"] for u in units))

    def test_fifteen_minus_five_names_every_missing_store(self):
        args = coverage_fixture()
        result = giveaway_coverage_check(*args)
        self.assertEqual(result["status"], "fail")
        self.assertEqual(len(result["coverage"]["missing_stores"]), 10)
        for i in range(6, 16):
            self.assertIn(f"活动门店{i:02d}", result["reason"])

    def test_receipts_and_photos_share_one_store_coverage(self):
        args = coverage_fixture(3, 3)
        args[0]["evidence"][0]["reason"] = "本店实际活动照片信息完整"
        self.assertEqual(giveaway_coverage_check(*args)["status"], "pass")

    def test_unreadable_proof_is_not_reported_as_not_uploaded(self):
        args = coverage_fixture(3, 3)
        args[0]["evidence"][0].update(valid=False, reason="本张已交小票的赠品内容被遮住")
        result = giveaway_coverage_check(*args)
        self.assertEqual(result["status"], "fail")
        self.assertIn("已交小票", result["reason"])
        self.assertEqual(result["coverage"]["missing_stores"], ["活动门店01"])

    def test_unknown_store_cannot_create_definite_missing_count(self):
        args = coverage_fixture(3, 2)
        args[0]["evidence"][0].update(store_name=None, store_quote="", valid=False, reason="店名模糊")
        self.assertEqual(giveaway_coverage_check(*args)["status"], "unknown")

    def test_no_full_scope_is_unknown_even_when_all_listed_stores_have_proofs(self):
        args = coverage_fixture(3, 3)
        args[0]["scope_complete"] = False
        self.assertEqual(giveaway_coverage_check(*args)["status"], "unknown")

    def test_cannot_omit_a_proof(self):
        args = coverage_fixture()
        args[0]["evidence"].pop()
        with self.assertRaises(AuditError):
            giveaway_coverage_check(*args)

    def test_cannot_invent_store_or_copy_another_proofs_quote(self):
        args = coverage_fixture()
        args[0]["evidence"][0]["store_quote"] = "不存在的店名"
        with self.assertRaises(AuditError):
            giveaway_coverage_check(*args)

    def test_pos_is_not_the_activity_scope(self):
        args = coverage_fixture()
        args[1].append({"unit_id": "pos", "facts": ["其他商店"]})
        args[0]["scope_source_ids"] = ["pos"]
        with self.assertRaises(AuditError):
            giveaway_coverage_check(*args)

    def test_new_giveaway_generation_requires_coverage_but_other_types_do_not(self):
        self.assertIn("giveaway_coverage", audit_schema("giveaway_promotion")["required"])
        for scenario in ("entry_fee", "personnel_incentive", "promotional_display"):
            self.assertNotIn("giveaway_coverage", audit_schema(scenario)["properties"])


if __name__ == "__main__":
    unittest.main()
