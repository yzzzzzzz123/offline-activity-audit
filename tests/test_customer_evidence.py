"""Business evidence projection preserves the original technical record."""
from copy import deepcopy
import json
import unittest

from audit_core.customer_evidence import strip_material_source_facts


def encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def material_view(facts, count=1):
    return {"schema_version": "1.1", "sheets": [{
        "projection_kind": "classification_rejection",
        "audit_counts": {"error_count": count},
        "rows": [{
            "heading": "资料需要补充", "status": "issue", "excel_row": index + 4,
            "values": ["合成资料.xlsx", "缺少合同。", ""],
            "error_reason": "缺少合同。", "business_source_files": [],
            "card_evidence": {
                "source_files": ["合成资料.xlsx"], "source_file_count": 1,
                "sources": [{"file": "合成资料.xlsx", "original_file": "合成资料.xlsx",
                             "kind": "submitted", "role": "本次核销资料",
                             "locator": f"工作表一，第{index + 1}段", "facts": facts}],
                "references": [], "comparisons": [{"kept": True}],
                "limitations": ["保留原有说明"], "file_count_complete": True,
            },
        } for index in range(count)],
    }]}


class CustomerEvidenceTests(unittest.TestCase):
    def test_repeated_large_facts_fit_api_without_changing_original_or_technical_source(self):
        facts = ["synthetic-fact:" + "x" * (2 * 1024 * 1024 + 20000)]
        original = material_view(facts, count=12)
        technical_source = {"documents": [{"unit_id": "synthetic-unit", "facts": facts}]}
        before_size = encoded_size(original)
        self.assertGreater(before_size, 24 * 1024 * 1024)

        copied = deepcopy(original)
        self.assertIs(strip_material_source_facts(copied), copied)
        self.assertLess(encoded_size(copied), 100_000)
        self.assertEqual(encoded_size(original), before_size)
        for row in original["sheets"][0]["rows"]:
            self.assertEqual(row["card_evidence"]["sources"][0]["facts"], facts)
        self.assertIs(technical_source["documents"][0]["facts"], facts)
        self.assertEqual(technical_source["documents"][0]["facts"][0], facts[0])

    def test_only_source_facts_are_removed_with_rows_locators_and_other_fields_intact(self):
        original = material_view([{"item": "合成原始事实"}], count=3)
        source = original["sheets"][0]["rows"][0]["card_evidence"]["sources"][0]
        source["other_fact_metadata"] = {"facts": ["不属于直接facts字段"]}
        original["facts"] = ["view级字段保留"]
        original["sheets"][0]["rows"][0]["facts"] = ["row级字段保留"]
        expected = deepcopy(original)
        for row in expected["sheets"][0]["rows"]:
            del row["card_evidence"]["sources"][0]["facts"]

        copied = deepcopy(original)
        strip_material_source_facts(copied)
        self.assertEqual(copied, expected)
        strip_material_source_facts(copied)
        self.assertEqual(copied, expected)
        self.assertIn("facts", source)

    def test_nonclassification_sheets_and_other_evidence_containers_are_unchanged(self):
        original = material_view(["合成事实"])
        ordinary = deepcopy(original["sheets"][0])
        ordinary["projection_kind"] = "pdf_policy"
        original["sheets"].append(ordinary)
        original["sheets"][0]["card_evidence"] = {"sources": [{"facts": ["sheet级不改"]}]}
        copied = deepcopy(original)
        strip_material_source_facts(copied)
        self.assertEqual(copied["sheets"][1], ordinary)
        self.assertEqual(copied["sheets"][0]["card_evidence"], original["sheets"][0]["card_evidence"])

    def test_missing_or_unexpected_shapes_are_preserved_without_guessing(self):
        for view in [None, {}, {"sheets": None}, {"sheets": [None, {"projection_kind": "classification_rejection", "rows": None}]}]:
            before = deepcopy(view)
            self.assertIs(strip_material_source_facts(view), view)
            self.assertEqual(view, before)
        view = {"sheets": [{"projection_kind": "classification_rejection", "rows": [
            None, {}, {"card_evidence": None}, {"card_evidence": {"sources": "unchanged"}},
            {"card_evidence": {"sources": [None, "unchanged", {"locator": "原位置"}]}},
        ]}]}
        before = deepcopy(view)
        strip_material_source_facts(view)
        self.assertEqual(view, before)


if __name__ == "__main__":
    unittest.main()
