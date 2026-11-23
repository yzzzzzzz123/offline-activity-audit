"""POS semantic resolutions distinguish real ambiguity from marketing records."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from audit_core.common import AuditError
from audit_core.pos_product_identity import verify_identity
from audit_core.pos_product_matching import pos_name_matcher, validate_resolution
from tests.test_pos_product_identity import row


def candidates():
    return [
        {"product_code": "A", "product_name": "某品牌美白牙膏(140g)-线下", "barcode_69": "6900000000000"},
        {"product_code": "B", "product_name": "某品牌美白牙膏(140g)-线下代言人", "barcode_69": "6900000000000"},
    ]


def resolved(decision="same_product", *, confidence="medium"):
    relations = {"same_product": ["same_product", "same_product"], "matched": ["match", "excluded"],
                 "ambiguous": ["possible", "possible"], "no_match": ["excluded", "excluded"]}[decision]
    return {"decision": decision, "confidence": confidence,
            "product_codes": [p["product_code"] for p, rel in zip(candidates(), relations) if rel != "excluded"],
            "assessments": [{"product_code": p["product_code"], "relation": rel,
                             "reason": "核对候选原始名称及规格，保留真实差异。"}
                            for p, rel in zip(candidates(), relations)],
            "reason": "核心商品与规格相同，差别只在渠道营销标记。"}


class PosNameDisambiguationTests(unittest.TestCase):
    def test_multiple_marketing_records_may_resolve_one_business_product_without_picking_a_code(self):
        result = verify_identity(row(name="某品牌140g美白牙膏", barcode="6900000000000"),
                                 {"products": candidates()}, Mock(return_value=resolved()))
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reason_code"], "matched_barcode")
        self.assertEqual({p["product_code"] for p in result["references"]}, {"A", "B"})
        self.assertNotIn("reference", result, "不任意选取一个内部编码冒充唯一编码")

    def test_pos_does_not_select_one_record_using_a_semantic_guess(self):
        matcher = Mock(return_value=resolved("matched"))
        result = verify_identity(row(name="某品牌140g美白牙膏", barcode="6900000000000"),
                                 {"products": candidates()}, matcher)
        self.assertEqual(result["status"], "pass")
        self.assertNotIn("reference", result)
        self.assertEqual(len(result["references"]), 2)
        matcher.assert_not_called()

    def test_pos_barcode_rule_keeps_different_catalog_names_without_rejecting_dealer(self):
        products = candidates(); products[1]["product_name"] = "某品牌清新牙膏(180g)"
        result = verify_identity(row(name="某品牌牙膏", barcode="6900000000000"),
                                 {"products": products}, Mock())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reason_code"], "matched_barcode")
        self.assertEqual(len(result["references"]), 2)

    def test_pos_does_not_use_low_confidence_name_resolution(self):
        value = resolved("ambiguous", confidence="low")
        matcher = Mock(return_value=value)
        result = verify_identity(row(name="牙膏", barcode="6900000000000"),
                                 {"products": candidates()}, matcher)
        self.assertEqual(result["status"], "pass")
        self.assertNotIn("reference", result)
        matcher.assert_not_called()

    def test_low_confidence_incomplete_or_outside_resolutions_are_rejected(self):
        low = resolved(confidence="low")
        missing = resolved(); missing["assessments"].pop()
        outside = resolved(); outside["product_codes"] = ["OUTSIDE"]
        inconsistent = resolved(); inconsistent["assessments"][0]["relation"] = "excluded"
        for value in (low, missing, outside, inconsistent):
            with self.subTest(value=value), self.assertRaises(AuditError):
                validate_resolution(value, candidates())
        products = candidates(); products[1]["barcode_69"] = "6900000000001"
        with self.assertRaises(AuditError):
            validate_resolution(resolved(), products)

    def test_exact_name_record_duplicates_no_longer_require_semantic_resolution(self):
        products = candidates(); products[1]["product_name"] = products[0]["product_name"]
        matcher = Mock(return_value=resolved())
        result = verify_identity(row(name=products[0]["product_name"], barcode="6900000000000"),
                                 {"products": products}, matcher)
        self.assertEqual(result["status"], "pass")
        matcher.assert_not_called()

    def test_semantic_matching_does_not_relax_exact_barcode(self):
        result = verify_identity(row(name="某品牌140g美白牙膏", barcode="06900000000000"),
                                 {"products": candidates()}, Mock(return_value=resolved()))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason_code"], "catalog_product_missing")

    def test_model_receives_all_identity_candidates_and_no_images_and_result_is_cached(self):
        call = Mock(return_value=resolved())
        with tempfile.TemporaryDirectory() as temporary:
            match = pos_name_matcher({"archive_id": "test", "scenario": "personnel_incentive"}, Path(temporary), call)
            request = row(name="某品牌140g美白牙膏", barcode="6900000000000")
            value = match(request, candidates())
            self.assertEqual(match(request, candidates()), value)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(call.call_args.kwargs["images"], [])
        call.call_args.kwargs["validator"](value)


if __name__ == "__main__":
    unittest.main()
