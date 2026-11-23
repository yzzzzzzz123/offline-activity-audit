"""Real date-summary failure shapes, with synthetic source-bound fixtures."""
from copy import deepcopy
import unittest

from tests.test_price_support_activity_dates import fixture, check


class PriceDateSummaryFeedbackTests(unittest.TestCase):
    def feedback(self):
        data = fixture(
            contract="执行周期：2025 年 12 月 4 日-2025 年 12 月 15 日",
            settlement="活动时间：2025.12.4-2025.12.15",
            photos=("水印日期2025 12 12 星期五", "水印2025.11.10", "水印2025.11.12",
                    "水印2025.11.12", "水印2025.11.10", "水印2025.11.11"))
        evidence, documents, materials, _ = data
        documents.append({"unit_id": "signature", "facts": [
            "对方签章确认：测试经销商", "时间：2025年12月4日", "本页其余区域为空白，无其他条款"]})
        materials[0]["source_ids"].append("signature")
        evidence["contract"]["source_ids"].append("signature")
        evidence["contract"]["limitations"] = [
            "合同第2页仅有签章确认时间2025年12月4日，属签章日期，不作为活动期间",
            "合同两页中仅第1页写有执行周期，无其他可比较的活动期间"]
        return data

    def test_real_shape_keeps_period_and_five_outside_photos(self):
        data = self.feedback(); before = deepcopy(data)
        result = check(data)
        self.assertEqual(result["settlement_activity_dates"]["status"], "pass")
        photos = result["photo_activity_dates"]
        self.assertEqual(photos["status"], "fail")
        self.assertEqual(photos["reason"].count("不在促销合同"), 5)
        self.assertNotIn("photo-1", photos["source_ids"])
        self.assertNotIn("无法确认", photos["reason"])
        self.assertEqual(data, before)

    def test_spaced_watermark_has_explicit_year_and_inclusive_date(self):
        for text in ("水印2025 12 4", "水印2025  12  15 星期一", "水印２０２５　１２　１２"):
            with self.subTest(text=text):
                result = check(fixture(contract="活动周期2025-12-04至2025-12-15", photos=(text,)))
                self.assertEqual(result["photo_activity_dates"]["status"], "pass")

    def test_spaced_watermark_does_not_infer_or_ignore_a_second_date(self):
        for text in ("水印12 12", "水印2025 12", "水印20251212", "水印2025 02 30",
                     "水印2025 12 12，2025-12-13", "水印2025 12 12，2025 12 13",
                     "水印2025 12 12，看不清", "商品效期2025 12 12"):
            with self.subTest(text=text):
                result = check(fixture(contract="活动周期2025-12-04至2025-12-15", photos=(text,)))
                self.assertEqual(result["photo_activity_dates"]["status"], "unknown")

    def test_scope_note_cannot_hide_a_conflict_or_missing_source_period(self):
        for mode in ("conflict", "no_period", "ambiguous", "no_activity_fact"):
            data = self.feedback()
            if mode == "conflict":
                data[1][-1]["facts"].append("活动周期2025-12-05至2025-12-15")
            elif mode == "no_period":
                data[0]["contract"]["periods"] = []
            elif mode == "ambiguous":
                data[1][-1]["facts"].append("活动结束日期待确认")
            else:
                data[1][0]["facts"] = ["合同没有活动周期"]
                data[0]["contract"]["periods"] = []
            with self.subTest(mode=mode):
                self.assertEqual(check(data)["settlement_activity_dates"]["status"], "unknown")

    def test_scope_note_with_a_real_missing_item_stays_unknown(self):
        for note in (
            "合同缺页，仅第1页写有执行周期，无其他可比较的活动期间",
            "合同两页中仅第1页写有执行周期，无其他可比较的活动期间；另页有遮挡",
            "合同两页中仅第1页写有执行周期，未见其他活动期间",
            "合同活动周期可能有冲突，不能确认",
        ):
            data = self.feedback(); data[0]["contract"]["limitations"][1] = note
            with self.subTest(note=note):
                self.assertEqual(check(data)["settlement_activity_dates"]["status"], "unknown")

    def test_three_clause_signing_note_still_needs_matching_signing_source(self):
        for fact in ("时间：2025年12月3日", "付款日期：2025年12月4日", "签章时间疑似2025年12月4日"):
            data = self.feedback(); data[1][-1]["facts"][1] = fact
            with self.subTest(fact=fact):
                self.assertEqual(check(data)["settlement_activity_dates"]["status"], "unknown")

    def test_reversed_year_is_not_repaired_by_a_scope_note(self):
        data = fixture(contract="执行周期：2026年7月21日-2025年8月05日",
                       settlement="活动时间：2026.07.10-07.20", photos=("水印2026 07 28",))
        data[0]["contract"]["limitations"] = ["合同仅写有执行周期，无其他可比较的活动期间"]
        result = check(data)
        self.assertEqual(result["settlement_activity_dates"]["status"], "unknown")
        self.assertIn("结束日期早于开始日期", result["settlement_activity_dates"]["reason"])
        self.assertIn("2026.07.10-07.20", result["settlement_activity_dates"]["reason"])


if __name__ == "__main__":
    unittest.main()
