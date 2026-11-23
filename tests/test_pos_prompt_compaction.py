"""Large final prompts retain all POS outcomes without repeating raw evidence."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.extraction_chain import render_prompt
from audit_core.pdf_materials import PdfEvidenceProvider, _prompt_pos_identities, _prompt_pos_calculations
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from tests.test_pos_product_identity import case_for
from tests.pdf_test_support import unknown_checks


def large_verification():
    rows = []
    for i in range(2000):
        rows.append({"unit_id": f"excel-{i // 150}",
                     "source": {"fact_indexes": [i % 150], "product_name": {"quote": "original row " * 50}},
                     "submitted": {"product_name": "合成商品", "barcode_69": "6900000000000"},
                     "status": "unknown" if i == 1999 else "pass",
                     "reason_code": "catalog_product_missing" if i == 1999 else "matched",
                     "reason": "库内无参考商品。" if i == 1999 else "准确69码匹配",
                     "references": [{"product_code": "A", "product_name": "合成商品", "barcode_69": "6900000000000"}]})
    return {"applicable": True, "rows": rows, "limitations": [{"unit_id": "excel-13", "reason": "真实限制"}]}


def large_calculation_summary():
    terms = [{"unit_id": f"excel-{i // 150}", "quote": f"R{i}: " + "原始明细 " * 80, "number": "1.005"}
             for i in range(2000)]
    total = {"label": "完整POS金额合计", "value_kind": "amount", "operation": "sum", "operator": "eq",
             "left": {"unit_id": "total", "quote": "合计2010", "number": "2010"}, "right": terms,
             "left_value": "2010", "right_value": "2010.000", "matches": True}
    difference = {**deepcopy(total), "label": "合计真实差异", "left_value": "2009", "matches": False}
    return {"complete": False, "tables": [], "source_ids": ["total"], "limitations": ["未知字段仍保留"],
            "calculation_count": 2001, "differences": [difference], "totals": [total]}


class PosPromptCompactionTests(unittest.TestCase):
    def test_small_values_keep_existing_representation(self):
        for value in ({}, {"rows": [], "limitations": []}):
            self.assertEqual(_prompt_pos_identities(value), json.dumps(value, ensure_ascii=False))
        self.assertEqual(_prompt_pos_calculations(None), "null")
        self.assertEqual(json.loads(_prompt_pos_calculations({"complete": False})), {"complete": False})

    def test_all_rows_and_unmatched_outcomes_remain_separately_bound(self):
        original = large_verification(); before = deepcopy(original)
        packed = json.loads(_prompt_pos_identities(original))
        self.assertEqual(original, before)
        self.assertEqual(packed["row_count"], 2000)
        self.assertEqual(packed["limitations"], original["limitations"])
        bindings = [(g["result"]["status"], uid, indices) for g in packed["groups"]
                    for uid, positions in g["sources"].items() for indices in positions]
        expected = [(r["status"], r["unit_id"], r["source"]["fact_indexes"]) for r in original["rows"]]
        self.assertCountEqual(bindings, expected)
        self.assertEqual(len(packed["groups"]), 2)
        self.assertEqual(packed["groups"][-1]["result"]["reason_code"], "catalog_product_missing")
        self.assertLess(len(json.dumps(packed, ensure_ascii=False)), len(json.dumps(original, ensure_ascii=False)) // 5)

    def test_original_sum_proof_and_exact_values_remain_in_program_object(self):
        original = large_calculation_summary(); before = deepcopy(original)
        packed = json.loads(_prompt_pos_calculations(original))
        self.assertEqual(original, before)
        for kind in ("differences", "totals"):
            c, raw = packed[kind][0], original[kind][0]
            for key in ("label", "operation", "operator", "left", "left_value", "right_value", "matches"):
                self.assertEqual(c[key], raw[key])
            self.assertEqual(c["right"]["operand_count"], 2000)
            self.assertEqual(c["right"]["source_ids"], list(dict.fromkeys(t["unit_id"] for t in raw["right"])))
        self.assertFalse(packed["complete"])
        self.assertEqual(packed["limitations"], original["limitations"])
        self.assertEqual(packed["calculation_count"], 2001)

    def test_non_sum_differences_keep_all_operands(self):
        original = large_calculation_summary()
        product = {**deepcopy(original["differences"][0]), "operation": "product", "right": [{"unit_id": "u", "quote": "2", "number": "2"}]}
        original["differences"].append(product)
        packed = json.loads(_prompt_pos_calculations(original))
        self.assertEqual(packed["differences"][-1], product)

    def test_actual_audit_uses_compact_prompt_but_unmodified_validation_input(self):
        case = case_for(); verified = large_verification(); evidence = unknown_checks(case["scenario"], case["flags"])
        for row in verified["rows"]:
            row["unit_id"] = "excel"
        check = next(c for c in evidence["checks"] if c["rule_id"] == "pos_product_identity")
        check.update(status="unknown", reason="合成商品：库内无参考商品。", source_ids=list(dict.fromkeys(r["unit_id"] for r in verified["rows"])))
        seen = []
        def call(root, skill, schema, prompt, **kwargs):
            text = render_prompt(prompt);seen.append(text)
            request=json.loads(prompt.partial_variables['request'])
            self.assertEqual(request['pos_product_identity']['row_count'],2000)
            self.assertIn("全部行均保留", text)
            kwargs["validator"](evidence)
            return evidence
        provider = PdfEvidenceProvider("synthetic", "medium", stage_summary_reviewer=isolated_stage_summary)
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", side_effect=call), \
             patch("audit_core.pdf_materials.prepare_pos_identities", return_value=verified), \
             patch("audit_core.pdf_materials.prepare_pos_arithmetic", return_value=None), \
             patch("audit_core.pdf_materials.project_pos_identity_reporting", side_effect=lambda result, facts: result), \
             patch("audit_core.pdf_materials.validate_pos_identity_reporting") as validation:
            provider.audit(case, Path(temporary))
        self.assertEqual(len(seen), 1)
        validation.assert_not_called()

if __name__ == "__main__":unittest.main()
