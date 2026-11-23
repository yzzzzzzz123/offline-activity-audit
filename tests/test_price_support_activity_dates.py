"""Source-bound price-support dates; no network, model or real business writes."""
from copy import deepcopy
import unittest

import jsonschema

from audit_core.common import AuditError
from audit_core.price_support_dates import PRICE_ACTIVITY_DATES_SCHEMA, price_activity_date_checks


def fixture(contract="活动时间：2026年6月2日至6月15日", settlement=None,
            photos=("水印拍摄日期2026-06-02 12:30:00", "水印拍摄日期2026-06-15 18:00:00")):
    settlement = settlement if settlement is not None else contract
    documents = [{"unit_id": "contract", "facts": [contract]}, {"unit_id": "settlement", "facts": [settlement]}]
    materials = [{"id": "promotion_contract", "state": "present", "source_ids": ["contract"]},
                 {"id": "settlement", "state": "present", "source_ids": ["settlement"]},
                 {"id": "price_evidence", "state": "present", "source_ids": []}]
    evidence = {"contract": {"source_ids": ["contract"], "periods": [{"unit_id": "contract", "quote": contract}], "limitations": []},
                "settlement": {"source_ids": ["settlement"], "periods": [{"unit_id": "settlement", "quote": settlement}], "limitations": []},
                "photos": []}
    units = []
    for i, text in enumerate(photos, 1):
        uid = f"photo-{i}"
        documents.append({"unit_id": uid, "facts": [text]})
        materials[-1]["source_ids"].append(uid)
        units.append({"unit_id": uid, "source_file": "照片.xlsx", "locator": f"照片!R{i}C2", "image": f"synthetic-{i}.png"})
        evidence["photos"].append({"unit_id": uid, "quote": text})
    return evidence, documents, materials, units


def check(data, **kwargs):
    jsonschema.Draft202012Validator(PRICE_ACTIVITY_DATES_SCHEMA).validate(data[0])
    return price_activity_date_checks(*data, **kwargs)


class PriceSupportActivityDatesTests(unittest.TestCase):
    def test_explicit_same_quote_year_and_inclusive_boundaries(self):
        data = fixture(); before = deepcopy(data)
        value = check(data)
        self.assertEqual(value["settlement_activity_dates"]["status"], "pass")
        self.assertEqual(value["photo_activity_dates"]["status"], "pass")
        self.assertEqual(data, before)
        self.assertEqual(value["photo_activity_dates"]["comparisons"], [])

    def test_outside_photos_fail_with_specific_cell_and_date(self):
        value = check(fixture(photos=("水印2026-06-01", "水印2026-06-16")))["photo_activity_dates"]
        self.assertEqual(value["status"], "fail")
        for text in ("R1C2", "R2C2", "2026-06-01", "2026-06-16", "2026-06-02至2026-06-15"):
            self.assertIn(text, value["reason"])

    def test_settlement_period_conflict_is_independent_of_valid_photo(self):
        value = check(fixture(settlement="活动时间2026.06.03-2026.06.15"))
        self.assertEqual(value["settlement_activity_dates"]["status"], "fail")
        self.assertEqual(value["photo_activity_dates"]["status"], "pass")
        self.assertIn("2026-06-03", value["settlement_activity_dates"]["reason"])

    def test_single_day_and_fully_explicit_cross_year(self):
        for contract, photos in (("活动日期2026-06-02", ("水印2026/6/2",)),
                                 ("活动期间2026.12.25-2027.01.05", ("水印2027-01-01",)),
                                 ("本次于2026.6.2--2026.6.15开展补差活动", ("水印2026.6.15",))):
            with self.subTest(contract=contract):
                result = check(fixture(contract, photos=photos))
                self.assertTrue(all(v["status"] == "pass" for v in result.values()))

    def test_missing_year_or_ambiguous_cross_year_never_inferred(self):
        for contract in ("活动6月2日至6月15日", "活动期间2026年12月25日至1月5日", "活动开始日期2026年6月2日",
                         "活动时间2026-02-30至2026-03-01", "活动时间2026年6月2日至待定"):
            with self.subTest(contract=contract):
                result = check(fixture(contract))
                self.assertTrue(all(v["status"] == "unknown" for v in result.values()))

    def test_photo_missing_year_or_null_stays_unknown_not_violation(self):
        data = fixture(photos=("水印6月2日", "水印看不清")); data[0]["photos"][1]["quote"] = None
        result = check(data)["photo_activity_dates"]
        self.assertEqual(result["status"], "unknown")
        self.assertIn("暂时不能确认", result["reason"])
        self.assertNotIn("不在促销合同", result["reason"])

    def test_mixed_outside_and_unknown_preserves_both_problems(self):
        data = fixture(photos=("水印2026-06-01", "水印看不清")); data[0]["photos"][1]["quote"] = None
        result = check(data)["photo_activity_dates"]
        self.assertEqual(result["status"], "fail")
        self.assertIn("R1C2", result["reason"]); self.assertIn("R2C2", result["reason"])
        self.assertIn("暂时不能确认", result["reason"])

    def test_wrong_role_or_nonverbatim_quote_rejected(self):
        for mutation in (lambda e: e["contract"]["periods"][0].update(unit_id="settlement"),
                         lambda e: e["contract"]["periods"][0].update(quote="活动时间：2026年6月1日至6月15日"),
                         lambda e: e["photos"][0].update(quote="水印拍摄日期2026-06-15 18:00:00")):
            data = fixture(); mutation(data[0])
            with self.assertRaises(AuditError): check(data)

    def test_extra_or_missing_photo_never_silently_covered(self):
        for mutation in (lambda e: e["photos"].pop(), lambda e: e["photos"].append({"unit_id": "other", "quote": None}),
                         lambda e: e["photos"].append(deepcopy(e["photos"][0]))):
            data = fixture(); mutation(data[0])
            with self.assertRaises(AuditError): check(data)

    def test_multi_page_coverage_and_conflict_cannot_select_one_contract(self):
        data = fixture(); e, documents, materials, _ = data
        documents.append({"unit_id": "contract-2", "facts": ["活动时间2026-07-02至2026-07-15"]})
        materials[0]["source_ids"].append("contract-2")
        with self.assertRaisesRegex(AuditError, "完整覆盖"):
            check(data)
        e["contract"]["source_ids"].append("contract-2")
        result = check(data)
        self.assertTrue(all(v["status"] == "unknown" for v in result.values()))
        self.assertIn("不同原文", result["settlement_activity_dates"]["reason"])
        self.assertIn("contract-2", result["photo_activity_dates"]["source_ids"])

    def test_blank_contract_page_is_covered_without_invented_dates(self):
        data = fixture(); e, documents, materials, _ = data
        documents.append({"unit_id": "contract-2", "facts": ["合同签署日期2026-05-30"]})
        materials[0]["source_ids"].append("contract-2"); e["contract"]["source_ids"].append("contract-2")
        self.assertEqual(check(data)["settlement_activity_dates"]["status"], "pass")

    def test_declared_limitation_forces_unknown(self):
        data = fixture(); data[0]["contract"]["limitations"] = ["续页活动周期有遮挡"]
        result = check(data)
        self.assertTrue(all(v["status"] == "unknown" for v in result.values()))
        self.assertIn("遮挡", result["photo_activity_dates"]["reason"])

    def test_unreadable_extracted_photo_cannot_be_passed_using_cell_date(self):
        data = fixture(); data[3][0].update(image=None, photo_reference=True)
        self.assertEqual(check(data)["photo_activity_dates"]["status"], "unknown")

    def test_online_exception_is_only_callers_flag_and_keeps_settlement_check(self):
        data = fixture(settlement="活动时间2026.06.03-2026.06.15"); data[0]["photos"] = []
        result = check(data, online=True)
        self.assertEqual(result["photo_activity_dates"]["status"], "not_applicable")
        self.assertEqual(result["settlement_activity_dates"]["status"], "fail")
        with self.assertRaises(AuditError): check(data)
        data[0]["online"] = True
        with self.assertRaises(jsonschema.ValidationError): check(data, online=True)

    def test_historical_absence_is_unknown_not_pass_or_exception(self):
        data = fixture()
        result = price_activity_date_checks(None, *data[1:])
        self.assertTrue(all(v["status"] == "unknown" for v in result.values()))

    def test_uncertain_photo_context_cannot_be_clipped_to_a_clean_date(self):
        for fact in ("水印日期疑似2026-06-05，看不清", "水印日期2026-06-05，看不清"):
            for clipped in (False, True):
                data = fixture(photos=(fact,))
                if clipped: data[0]["photos"][0]["quote"] = "2026-06-05"
                with self.subTest(fact=fact, clipped=clipped):
                    self.assertEqual(check(data)["photo_activity_dates"]["status"], "unknown")

    def test_signing_payment_or_expiry_date_cannot_be_activity_or_photo_date(self):
        for field in ("签订日期", "付款日期", "效期"):
            for clipped in (False, True):
                data = fixture(contract=field + "2026-06-05", photos=("水印2026-06-05",))
                if clipped:
                    for key in ("contract", "settlement"):
                        data[0][key]["periods"][0]["quote"] = "2026-06-05"
                with self.subTest(field=field, clipped=clipped):
                    self.assertEqual(check(data)["settlement_activity_dates"]["status"], "unknown")
        data = fixture(photos=("商品效期2026-06-05",)); data[0]["photos"][0]["quote"] = "2026-06-05"
        self.assertEqual(check(data)["photo_activity_dates"]["status"], "unknown")

    def test_unclear_price_does_not_erase_a_clear_watermark_date(self):
        data = fixture(photos=("水印日期2026-06-05，价格被遮挡",)); data[0]["photos"][0]["quote"] = "2026-06-05"
        self.assertEqual(check(data)["photo_activity_dates"]["status"], "pass")

    def test_logistics_date_cannot_be_activity_even_when_clipped(self):
        for field in ("收货日期", "计划到货时间", "发货日期", "出库日期", "物流日期"):
            for clipped in (False, True):
                data = fixture(contract=field + "2026-06-05", photos=("水印2026-06-05",))
                if clipped:
                    for key in ("contract", "settlement"):
                        data[0][key]["periods"][0]["quote"] = "2026-06-05"
                with self.subTest(field=field, clipped=clipped):
                    self.assertTrue(all(c["status"] == "unknown" for c in check(data).values()))

    def test_supplied_unclear_price_photos_still_require_full_date_coverage(self):
        data = fixture(photos=("水印2026-06-01", "水印2026-06-05"))
        data[2][-1]["state"] = "unclear"
        self.assertEqual(check(data)["photo_activity_dates"]["status"], "fail")
        data[0]["photos"].pop()
        with self.assertRaisesRegex(AuditError, "完整覆盖"):
            check(data)

    def test_problem_sources_exclude_passing_photos_but_keep_full_technical_evidence(self):
        data = fixture(photos=("水印2026-06-01", "水印2026-06-05", "水印看不清"))
        data[0]["photos"][2]["quote"] = None
        before = deepcopy(data)
        result = check(data)["photo_activity_dates"]
        self.assertEqual(result["source_ids"], ["contract", "photo-1", "photo-3"])
        self.assertNotIn("R2C2", result["reason"])
        self.assertEqual(data, before)
        data = fixture(contract="活动时间待定")
        result = check(data)["photo_activity_dates"]
        self.assertEqual(result["source_ids"], ["contract", "photo-1", "photo-2"])
        self.assertIn("全部2张", result["reason"])

    def test_online_date_exception_survives_real_audit_decision(self):
        from audit_core.pdf_evidence import audit_decision
        from tests.pdf_test_support import flags_for, unknown_checks

        dates, documents, materials, units = fixture(photos=())
        flags = flags_for("price_difference_support", online=True)
        evidence = unknown_checks("price_difference_support", flags)
        evidence.update(activity_dates=dates, price_photo_reviews=[])
        before = deepcopy(evidence)
        result = audit_decision("price_difference_support", flags, evidence, documents, materials, units=units)
        checks = {item["rule_id"]: item for item in result["checks"]}
        self.assertEqual(checks["photo_activity_dates"]["status"], "not_applicable")
        self.assertEqual(checks["settlement_activity_dates"]["status"], "pass")
        self.assertEqual(result["activity_dates"], dates)
        self.assertEqual(evidence, before)

    def test_optional_full_reduction_material_cannot_override_not_applicable(self):
        from audit_core.pdf_evidence import audit_decision
        from tests.pdf_test_support import flags_for, unknown_checks

        for full_reduction in (False, None, True):
            for state in ("missing", "unclear"):
                with self.subTest(full_reduction=full_reduction, material_state=state):
                    _, documents, materials, units = fixture(photos=())
                    documents.append({"unit_id": "optional-proof", "facts": ["满减活动情况无法确认"]})
                    materials.append({"id": "full_reduction_evidence", "state": state,
                                      "source_ids": [] if state == "missing" else ["optional-proof"],
                                      "reason": "缺少证明" if state == "missing" else "证明用途无法确认"})
                    # A separate mandatory item must still report the missing material.
                    next(m for m in materials if m["id"] == "price_evidence").update(
                        state="missing", source_ids=[], reason="未提供活动价格证明")
                    flags = flags_for("price_difference_support", full_reduction=full_reduction)
                    evidence = unknown_checks("price_difference_support", flags, materials=materials, units=units)
                    before = deepcopy((evidence, materials))
                    result = audit_decision("price_difference_support", flags, evidence, documents, materials, units=units)
                    checks = {item["rule_id"]: item for item in result["checks"]}
                    expected = ("not_applicable" if full_reduction is not True
                                else "fail" if state == "missing" else "unknown")
                    self.assertEqual(checks["full_reduction_evidence"]["status"], expected)
                    self.assertEqual(checks["price_evidence"]["status"], "fail")
                    self.assertEqual((evidence, materials), before)

    def _with_signing_page(self, data):
        evidence, documents, materials, _ = data
        documents.append({"unit_id": "signature", "facts": [
            "对方签章确认：测试经销商", "时间：2025年12月4日", "本页其余区域为空白，无其他条款"]})
        materials[0]["source_ids"].append("signature")
        evidence["contract"]["source_ids"].append("signature")
        evidence["contract"]["limitations"] = [
            "合同签章页的“时间：2025年12月4日”是对方签章确认时间，不作为活动期间使用。"]
        return data

    def test_verified_signing_note_does_not_hide_explicit_period_or_outside_photos(self):
        data = self._with_signing_page(fixture(
            contract="执行周期：2025年12月4日-2025年12月15日",
            settlement="活动时间：2025.12.4--2025.12.15",
            photos=("水印2025-12-12", "水印2025.11.10", "水印2025.11.12",
                    "水印2025.11.12", "水印2025.11.10", "水印2025.11.11")))
        before = deepcopy(data)
        result = check(data)
        self.assertEqual(result["settlement_activity_dates"]["status"], "pass")
        self.assertEqual(result["photo_activity_dates"]["status"], "fail")
        self.assertNotIn("photo-1", result["photo_activity_dates"]["source_ids"])
        for index in range(2, 7):
            self.assertIn(f"photo-{index}", result["photo_activity_dates"]["source_ids"])
        self.assertEqual(result["photo_activity_dates"]["reason"].count("不在促销合同"), 5)
        self.assertEqual(data, before)  # Original limitations remain technical evidence.

    def test_signing_note_requires_date_and_signing_facts_in_same_allowed_role(self):
        for mode in ("no_page", "different_date", "unread_source", "no_signing_fact", "other_date"):
            data = self._with_signing_page(fixture(contract="活动周期：2025年12月4日至2025年12月15日"))
            evidence, documents, materials, _ = data
            if mode == "no_page":
                documents.pop(); materials[0]["source_ids"].pop(); evidence["contract"]["source_ids"].pop()
            elif mode == "different_date":
                documents[-1]["facts"][1] = "时间：2025年12月3日"
            elif mode == "unread_source":
                materials[0]["source_ids"].pop(); evidence["contract"]["source_ids"].pop()
            elif mode == "no_signing_fact":
                documents[-1]["facts"][0] = "合同签订方：测试经销商"
            else:
                documents[-1]["facts"][1] = "付款日期：2025年12月4日"
            with self.subTest(mode=mode):
                self.assertEqual(check(data)["settlement_activity_dates"]["status"], "unknown")

    def test_mixed_or_uncertain_limitations_are_not_ignored_by_signing_keywords(self):
        notes = (
            "签章日期2025年12月4日仅供参考，活动周期有遮挡，不能确认。",
            "签章日期2025年12月4日，不作为活动期间使用；合同活动结束日期不明确。",
            "签章日期2025年12月4日，不作为活动期间使用，另页活动周期不一致。",
            "合同活动周期和签章日期2025年12月4日，不作为活动期间使用。",
            "签章日期疑似2025年12月4日，不作为活动期间使用。",
            "签章日期2025年12月4日至2025年12月15日，不作为活动期间使用。",
        )
        for note in notes:
            data = self._with_signing_page(fixture(contract="活动周期：2025年12月4日至2025年12月15日"))
            data[0]["contract"]["limitations"] = [note]
            with self.subTest(note=note):
                self.assertEqual(check(data)["settlement_activity_dates"]["status"], "unknown")

    def test_signing_note_does_not_override_real_activity_conflict_or_missing_period(self):
        for mode in ("conflict", "missing", "unclear_signing_date"):
            data = self._with_signing_page(fixture(contract="活动周期：2025年12月4日至2025年12月15日"))
            if mode == "conflict":
                data[1][0]["facts"].append("活动周期：2025年12月5日至2025年12月15日")
            elif mode == "missing":
                data[0]["contract"]["periods"] = []
                data[1][0]["facts"] = ["活动周期未明确"]
            else:
                data[1][-1]["facts"][1] = "签章日期疑似2025年12月4日，无法确认"
            with self.subTest(mode=mode):
                self.assertEqual(check(data)["settlement_activity_dates"]["status"], "unknown")

    def test_reversed_contract_year_keeps_unknown_and_displays_both_original_periods(self):
        data = fixture(contract="执行周期：2026年7月21日-2025年8月05日",
                       settlement="活动时间：2026.07.10-07.20", photos=("水印2026-07-28",))
        before = deepcopy(data)
        result = check(data)
        self.assertEqual(result["settlement_activity_dates"]["status"], "unknown")
        for value in ("结算单活动周期原文", "2026.07.10-07.20", "促销合同活动周期原文",
                      "2026年7月21日-2025年8月05日", "结束日期早于开始日期"):
            self.assertIn(value, result["settlement_activity_dates"]["reason"])
        self.assertEqual(result["photo_activity_dates"]["status"], "unknown")
        self.assertEqual(data, before)


if __name__ == "__main__":
    unittest.main()
