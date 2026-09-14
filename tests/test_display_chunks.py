from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from audit_core.common import AuditError
from audit_core.display_chunks import (
    chunk_attachment_records,
    chunk_routed_reviews,
    chunk_sequence,
    merge_contract_product_cells,
    merge_photo_assignments,
    merge_product_queries,
    merge_routed_results,
)


def route(line: int, photos: list[str]) -> dict:
    return {
        "store_line_no": line,
        "contract_store_name": f"门店{line}",
        "photo_files": photos,
    }


def attachment(line: int, page: int = 1) -> dict:
    return {
        "line_no": line,
        "source_page": page,
        "product_code": f"SKU-{line}",
        "product_name": f"原始名称{line}",
        "barcode_69": None,
    }


def queries(*names: str, notes: list[str] | None = None) -> dict:
    return {
        "schema_version": "1.0",
        "photo_queries": [
            {"photo_file": name, "visible_text": [f"可见文字{name}"]} for name in names
        ],
        "extraction_notes": notes or [],
    }


def assignments(*items: tuple[str, int | None]) -> dict:
    return {
        "photo_assignments": [
            {
                "photo_file": name,
                "store_line_no": line,
                "visible_location": f"可见地点{line}" if line is not None else None,
                "location_basis": "根据照片可见水印；模糊时保持未分配",
            }
            for name, line in items
        ],
        "extraction_notes": ["路由不证明地点已通过"],
    }


def routed(key: str, *rows: dict, notes: list[str] | None = None) -> dict:
    result = {
        "schema_version": "2.5" if key == "photo_reviews" else "1.0",
        key: list(rows),
        "extraction_notes": notes or [],
    }
    if key == "photo_reviews":
        result["scenario"] = "promotional_display"
    return result


class DisplayChunksTests(unittest.TestCase):
    def test_chunk_sequence_preserves_order_tail_and_generic_values(self) -> None:
        items = tuple(range(21))
        groups = chunk_sequence(items, 8)
        self.assertEqual([len(group) for group in groups], [8, 8, 5])
        self.assertEqual([item for group in groups for item in group], list(items))
        self.assertEqual(chunk_sequence([Path("a.jpg"), Path("b.jpg")], 1), [[Path("a.jpg")], [Path("b.jpg")]])
        self.assertEqual(chunk_sequence([], 3), [])

    def test_chunk_sizes_reject_zero_negative_boolean_and_fraction(self) -> None:
        for size in (0, -1, True, 1.5, "2"):
            with self.subTest(size=size), self.assertRaises(AuditError):
                chunk_sequence([], size)
            with self.subTest(size=size), self.assertRaises(AuditError):
                chunk_attachment_records([], size)
            with self.subTest(size=size), self.assertRaises(AuditError):
                chunk_routed_reviews([], size)

    def test_attachment_chunks_never_mix_pages_or_renumber_lines(self) -> None:
        records = [attachment(line, 2 if line < 11 else 4) for line in range(1, 14)]
        original = deepcopy(records)
        chunks = chunk_attachment_records(records)
        self.assertEqual([[row["line_no"] for row in block] for block in chunks], [list(range(1, 9)), [9, 10], [11, 12, 13]])
        self.assertEqual({row["source_page"] for row in chunks[-1]}, {4})
        chunks[0][0]["product_code"] = "不能改写输入"
        self.assertEqual(records, original)

    def test_attachment_order_and_duplicate_rows_are_rejected(self) -> None:
        for records in (
            [attachment(1), attachment(1)],
            [attachment(2), attachment(1)],
            [attachment(1, 3), attachment(2, 2)],
            [attachment(True)],
        ):
            with self.subTest(records=records), self.assertRaises(AuditError):
                chunk_attachment_records(records)
        self.assertEqual(chunk_attachment_records([attachment(9), attachment(10)]), [[attachment(9), attachment(10)]])

    def test_product_queries_restore_input_order_and_deduplicate_notes(self) -> None:
        results = [queries("b.jpg", notes=["说明甲", "说明乙"]), queries("a.jpg", notes=["说明乙", "说明丙"])]
        original = deepcopy(results)
        merged = merge_product_queries([Path("photos/a.jpg"), r"D:\photos\b.jpg"], results)
        self.assertEqual([row["photo_file"] for row in merged["photo_queries"]], ["a.jpg", "b.jpg"])
        self.assertEqual(merged["extraction_notes"], ["说明甲", "说明乙", "说明丙"])
        merged["photo_queries"][0]["visible_text"].append("不能改写输入")
        self.assertEqual(results, original)

    def test_product_queries_reject_missing_duplicate_unknown_and_path_sources(self) -> None:
        for results, message in (
            ([queries("a.jpg")], "缺少照片"),
            ([queries("a.jpg", "b.jpg"), queries("a.jpg")], "重复返回照片"),
            ([queries("a.jpg", "c.jpg")], "未知照片"),
            ([queries("a.jpg", "photos/b.jpg")], "未知照片"),
        ):
            with self.subTest(results=results), self.assertRaisesRegex(AuditError, message):
                merge_product_queries(["a.jpg", "b.jpg"], results)

    def test_product_queries_reject_bad_envelope_and_expected_name_collisions(self) -> None:
        with self.assertRaisesRegex(AuditError, "版本"):
            merge_product_queries(["a.jpg"], [{**queries("a.jpg"), "schema_version": "2.5"}])
        with self.assertRaisesRegex(AuditError, "大小写冲突"):
            merge_product_queries(["A.jpg", "a.jpg"], [])
        with self.assertRaisesRegex(AuditError, "说明"):
            merge_product_queries([], [{"schema_version": "1.0", "photo_queries": [], "extraction_notes": "不能按字符切分"}])

    def test_photo_assignments_preserve_unassigned_photos_and_original_order(self) -> None:
        results = [assignments(("c.jpg", None)), assignments(("b.jpg", 7), ("a.jpg", 3))]
        merged = merge_photo_assignments(["a.jpg", "b.jpg", "c.jpg"], results)
        self.assertEqual([row["store_line_no"] for row in merged["photo_assignments"]], [3, 7, None])
        self.assertIsNone(merged["photo_assignments"][-1]["visible_location"])
        self.assertEqual(len(merged["extraction_notes"]), 1)

    def test_photo_assignments_require_exactly_once_full_coverage(self) -> None:
        for results, message in (
            ([assignments(("a.jpg", 1))], "缺少照片"),
            ([assignments(("a.jpg", 1), ("a.jpg", 1), ("b.jpg", None))], "重复返回照片"),
            ([assignments(("a.jpg", 1), ("other.jpg", 2))], "未知照片"),
        ):
            with self.subTest(results=results), self.assertRaisesRegex(AuditError, message):
                merge_photo_assignments(["a.jpg", "b.jpg"], results)

    def test_photo_assignments_reject_invalid_route_facts(self) -> None:
        for change in ({"store_line_no": True}, {"store_line_no": 0}, {"visible_location": 123}, {"location_basis": " "}, {"guessed": True}):
            result = assignments(("a.jpg", 1))
            result["photo_assignments"][0].update(change)
            with self.subTest(change=change), self.assertRaises(AuditError):
                merge_photo_assignments(["a.jpg"], [result])

    def test_store_chunks_keep_all_single_store_photos_together(self) -> None:
        reviews = [route(1, [f"photo-{n}.jpg" for n in range(21)]), route(2, []), route(3, ["tail.jpg"])]
        original = deepcopy(reviews)
        chunks = chunk_routed_reviews(reviews)
        self.assertEqual([len(chunk) for chunk in chunks], [1, 1, 1])
        self.assertEqual(len(chunks[0][0]["photo_files"]), 21)
        chunks[0][0]["photo_files"].clear()
        self.assertEqual(reviews, original)
        self.assertEqual([len(chunk) for chunk in chunk_routed_reviews(reviews, 2)], [2, 1])

    def test_routed_merges_restore_store_order_without_combining_observations(self) -> None:
        expected = [route(1, ["a.jpg", "b.jpg"]), route(2, [])]
        first = {**expected[0], "display_observation": {"vertical_facing_count": 3, "visible_basis": ["仅一张照片的三列"]}}
        second = {**expected[1], "display_observation": {"vertical_facing_count": None}}
        for key in ("photo_reviews", "display_reviews"):
            results = [routed(key, second, notes=["甲"]), routed(key, first, notes=["甲", "乙"])]
            original = deepcopy(results)
            merged = merge_routed_results(expected, results, key)
            self.assertEqual([row["store_line_no"] for row in merged[key]], [1, 2])
            self.assertEqual(merged[key][0]["display_observation"]["vertical_facing_count"], 3)
            self.assertEqual(merged["extraction_notes"], ["甲", "乙"])
            merged[key][0]["display_observation"]["vertical_facing_count"] = 999
            self.assertEqual(results, original)

    def test_routed_merge_rejects_route_name_photo_order_and_membership_changes(self) -> None:
        expected = [route(1, ["a.jpg", "b.jpg"])]
        for change in (
            {"contract_store_name": "改过的门店"},
            {"contract_store_name": "门店1 "},
            {"photo_files": ["b.jpg", "a.jpg"]},
            {"photo_files": ["a.jpg"]},
            {"photo_files": ["a.jpg", "b.jpg", "extra.jpg"]},
        ):
            changed = {**expected[0], **change}
            with self.subTest(change=change), self.assertRaisesRegex(AuditError, "改变"):
                merge_routed_results(expected, [routed("display_reviews", changed)], "display_reviews")

    def test_routed_merge_rejects_missing_duplicate_unknown_and_wrong_envelope(self) -> None:
        expected = [route(1, ["a.jpg"]), route(2, [])]
        for results, message in (
            ([routed("display_reviews", expected[0])], "缺少"),
            ([routed("display_reviews", *expected, expected[0])], "重复"),
            ([routed("display_reviews", *expected, route(3, []))], "未知"),
            ([{**routed("display_reviews", *expected), "schema_version": "2.5"}], "版本"),
        ):
            with self.subTest(results=results), self.assertRaisesRegex(AuditError, message):
                merge_routed_results(expected, results, "display_reviews")
        with self.assertRaisesRegex(AuditError, "场景"):
            merge_routed_results(expected, [{**routed("photo_reviews", *expected), "scenario": "personnel_incentive"}], "photo_reviews")
        with self.assertRaises(AuditError):
            merge_routed_results(expected, [], "other")

    def test_routed_inputs_reject_duplicate_store_and_photo_members(self) -> None:
        for reviews in ([route(1, []), route(1, [])], [route(1, ["a.jpg", "a.jpg"])], [route(1, ["folder/a.jpg"])], [route(True, [])]):
            with self.subTest(reviews=reviews), self.assertRaises(AuditError):
                chunk_routed_reviews(reviews)

    def test_contract_cells_merge_restores_global_order_and_preserves_null_reads(self) -> None:
        expected = [attachment(1, 2), attachment(2, 2), attachment(3, 4)]
        null_row = {**expected[1], "product_name": None}
        results = [
            {"records": [expected[2]], "extraction_notes": ["无法确认的原值保持null"]},
            {"records": [null_row, expected[0]], "extraction_notes": ["无法确认的原值保持null", "保留印刷顺序"]},
        ]
        original = deepcopy(results)
        merged = merge_contract_product_cells(expected, results)
        self.assertEqual([row["line_no"] for row in merged["records"]], [1, 2, 3])
        self.assertIsNone(merged["records"][1]["product_name"])
        self.assertEqual(len(merged["extraction_notes"]), 2)
        merged["records"][0]["product_code"] = "不能改写输入"
        self.assertEqual(results, original)

    def test_contract_cells_merge_rejects_missing_duplicate_unknown_and_wrong_page(self) -> None:
        expected = [attachment(1, 2), attachment(2, 3)]
        for rows, message in (
            ([expected[0]], "缺少"),
            ([*expected, expected[0]], "重复"),
            ([*expected, attachment(3, 3)], "未知"),
            ([attachment(1, 3), expected[1]], "未知"),
        ):
            with self.subTest(rows=rows), self.assertRaisesRegex(AuditError, message):
                merge_contract_product_cells(expected, [{"records": rows, "extraction_notes": []}])

    def test_empty_inputs_merge_without_inventing_records(self) -> None:
        self.assertEqual(merge_product_queries([], [queries()])["photo_queries"], [])
        self.assertEqual(merge_photo_assignments([], [assignments()])["photo_assignments"], [])
        self.assertEqual(merge_routed_results([], [], "display_reviews")["display_reviews"], [])
        self.assertEqual(merge_contract_product_cells([], [])["records"], [])

    def test_invalid_chunk_objects_raise_audit_error(self) -> None:
        for results in ([None], [{"photo_assignments": ["not-an-object"]}], [{"photo_assignments": None}]):
            with self.subTest(results=results), self.assertRaises(AuditError):
                merge_photo_assignments(["a.jpg"], results)


if __name__ == "__main__":
    unittest.main()
