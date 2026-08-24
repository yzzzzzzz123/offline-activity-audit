from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from audit_core.common import AuditError
from audit_core.codex_runner import (
    _copy_product_reference_images,
    _select_product_rag_candidates,
)
from audit_core.display import sales_product_correspondence
from audit_core.product_rag import (
    ean13_is_valid,
    load_pending_product_rag,
    load_product_rag,
    product_reference_label,
    resolve_product_reference_hits,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DISPLAY_SKILL = PROJECT_ROOT / "skills" / "audit-promotional-display"


def _raw_hit(confidence: str = "exact") -> dict:
    return {
        "reference_product_id": "canban-6970356167341",
        "confidence": confidence,
        "matched_view_ids": ["sampleimg-003-v01"],
        "visible_basis": ["现场包装可见参半、SP-1、净清新牙膏和180g"],
        "limitations": [],
    }


class ProductRagTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalog = load_product_rag(DISPLAY_SKILL)

    def test_catalog_has_real_multi_view_products_with_valid_identifiers(self) -> None:
        products = self.catalog["products"]
        self.assertGreaterEqual(len(products), 99)
        by_barcode = {product["barcode_69"]: product for product in products}
        self.assertEqual(
            by_barcode["6970356167341"]["product_name"],
            "参半oralshark玫瑰清茶味净清新牙膏180g",
        )
        self.assertEqual(
            by_barcode["6970356167341"]["product_code"],
            "CP-KQ-YG-0085",
        )
        self.assertIn(
            "SP-1",
            by_barcode["6970356167341"]["product_code_aliases"],
        )
        self.assertNotIn("6970356161042", by_barcode)
        for product in products:
            self.assertGreaterEqual(len(product["views"]), 1)
            self.assertEqual(
                len({item["view_id"] for item in product["views"]}),
                len(product["views"]),
            )
            self.assertTrue(ean13_is_valid(product["barcode_69"]))

    def test_resolved_barcodes_leave_quarantine_and_enter_catalog(self) -> None:
        manifest = load_pending_product_rag(DISPLAY_SKILL)
        pending = manifest["pending_products"]
        pending_ids = {product["pending_id"] for product in pending}
        self.assertNotIn("sampleimg-013", pending_ids)
        self.assertNotIn("sampleimg-026", pending_ids)
        for product in pending:
            self.assertNotIn("barcode_69", product)
            self.assertIn("完整69码", product["reason"])

        by_source = {
            source["source_id"]: product
            for product in self.catalog["products"]
            for source in product["sources"]
        }
        expected = {
            "sampleimg-013": (
                "6970356163763",
                "参半锁白牙膏 沁爽青提味120g（pingu联名款）",
            ),
            "sampleimg-026": (
                "6970356168997",
                "参半-益生菌清新口腔喷雾20ml-沁润蜜桃胶盒装",
            ),
        }
        for source_id, (barcode, product_name) in expected.items():
            product = by_source[source_id]
            self.assertEqual(product["barcode_69"], barcode)
            self.assertEqual(product["product_name"], product_name)
            barcode_views = [
                view for view in product["views"] if view["face"] == "barcode"
            ]
            self.assertGreaterEqual(len(barcode_views), 1)
            self.assertTrue(
                any(
                    view["identity_strength"] == "strong"
                    and f"69码 {barcode}" in view["visible_anchors"]
                    for view in barcode_views
                )
            )

    def test_ean13_rejects_wrong_check_digit_or_format(self) -> None:
        self.assertTrue(ean13_is_valid("6970356169338"))
        self.assertTrue(ean13_is_valid("6970356167341"))
        self.assertFalse(ean13_is_valid("6970356169339"))
        self.assertFalse(ean13_is_valid("6 970356 169338"))

    def test_model_hit_is_resolved_to_immutable_catalog_identity(self) -> None:
        resolved = resolve_product_reference_hits([_raw_hit()], self.catalog)
        self.assertEqual(
            resolved[0],
            {
                "reference_product_id": "canban-6970356167341",
                "product_name": "参半oralshark玫瑰清茶味净清新牙膏180g",
                "product_code": "CP-KQ-YG-0085",
                "product_code_aliases": ["SP-1"],
                "barcode_69": "6970356167341",
                "specification": "180g",
                "variant": "玫瑰清茶味",
                "confidence": "exact",
                "matched_view_ids": ["sampleimg-003-v01"],
                "visible_basis": ["现场包装可见参半、SP-1、净清新牙膏和180g"],
                "limitations": [],
            },
        )
        self.assertIn("产品编码：CP-KQ-YG-0085", product_reference_label(resolved[0]))
        self.assertIn("69码：6970356167341", product_reference_label(resolved[0]))

    def test_unknown_model_product_or_view_is_rejected(self) -> None:
        unknown_product = _raw_hit()
        unknown_product["reference_product_id"] = "not-in-catalog"
        with self.assertRaises(AuditError):
            resolve_product_reference_hits([unknown_product], self.catalog)

        unknown_view = _raw_hit()
        unknown_view["matched_view_ids"] = ["not-a-view"]
        with self.assertRaises(AuditError):
            resolve_product_reference_hits([unknown_view], self.catalog)

        duplicate_view = _raw_hit()
        duplicate_view["matched_view_ids"] = ["sampleimg-003-v01", "sampleimg-003-v01"]
        with self.assertRaises(AuditError):
            resolve_product_reference_hits([duplicate_view], self.catalog)

    def test_exact_rag_hit_maps_sales_by_69_code_before_name_similarity(self) -> None:
        resolved = resolve_product_reference_hits([_raw_hit()], self.catalog)
        records = [
            {
                "product_name": "Excel原始名称-参半SP1净清新牙膏",
                "product_code": "some-other-code",
                "barcode": "6970356167341",
            },
            {
                "product_name": "文字非常相似但条码不同的参半净清新牙膏",
                "product_code": "SP-1",
                "barcode": "6978974200716",
            },
        ]
        result = sales_product_correspondence(
            ["参半oralshark净清新牙膏"],
            records,
            resolved,
        )
        self.assertEqual(result["status"], "exact")
        self.assertEqual(result["names"], ["Excel原始名称-参半SP1净清新牙膏"])
        self.assertIn("69码6970356167341", result["basis"])

    def test_candidate_rag_hit_cannot_be_promoted_to_exact_by_catalog_code(self) -> None:
        resolved = resolve_product_reference_hits([_raw_hit("candidate")], self.catalog)
        result = sales_product_correspondence(
            ["只看清参半红银盒"],
            [
                {
                    "product_name": "Excel原始名称-参半SP1净清新牙膏",
                    "product_code": "SP-1",
                    "barcode": "6970356167341",
                }
            ],
            resolved,
        )
        self.assertEqual(result["status"], "candidate")

    def test_packaging_code_alias_still_retrieves_and_matches_sales(self) -> None:
        alias_query = {
            "photo_queries": [
                {
                    "visible_barcodes_69": [],
                    "visible_product_names": [],
                    "visible_product_codes": ["SP-1"],
                    "visible_text": ["SP-1"],
                }
            ]
        }
        selected = _select_product_rag_candidates(self.catalog, alias_query)
        self.assertIn(
            "6970356167341",
            [product["barcode_69"] for product in selected["products"]],
        )

        resolved = resolve_product_reference_hits([_raw_hit()], self.catalog)
        result = sales_product_correspondence(
            ["参半SP-1净清新牙膏"],
            [
                {
                    "product_name": "Excel原始名称-参半SP1净清新牙膏",
                    "product_code": "SP-1",
                    "barcode": "",
                }
            ],
            resolved,
        )
        self.assertEqual(result["status"], "exact")
        self.assertIn("产品编码别名SP-1", result["basis"])

    def test_human_review_product_is_not_exposed_to_runtime_catalog(self) -> None:
        raw = {
            "reference_product_id": "canban-6978974201812",
            "confidence": "exact",
            "matched_view_ids": ["dental-028-v01"],
            "visible_basis": ["现场只看到宇航员包装"],
            "limitations": ["同码包装规格存在冲突"],
        }
        with self.assertRaises(AuditError):
            resolve_product_reference_hits([raw], self.catalog)
        raw["confidence"] = "candidate"
        with self.assertRaises(AuditError):
            resolve_product_reference_hits([raw], self.catalog)

    def test_candidate_retrieval_is_bounded_and_ignores_generic_terms(self) -> None:
        exact_query = {
            "photo_queries": [
                {
                    "visible_barcodes_69": ["6970356169338"],
                    "visible_product_names": [],
                    "visible_product_codes": [],
                    "visible_text": [],
                }
            ]
        }
        selected = _select_product_rag_candidates(self.catalog, exact_query)
        self.assertEqual(
            [product["barcode_69"] for product in selected["products"]],
            ["6970356169338"],
        )
        generic_query = {
            "photo_queries": [
                {
                    "visible_barcodes_69": [],
                    "visible_product_names": ["参半牙膏"],
                    "visible_product_codes": [],
                    "visible_text": ["参半", "牙膏"],
                }
            ]
        }
        self.assertEqual(
            _select_product_rag_candidates(self.catalog, generic_query)["products"],
            [],
        )
        with TemporaryDirectory() as temporary:
            copied = _copy_product_reference_images(
                DISPLAY_SKILL,
                Path(temporary),
                selected,
            )
            self.assertLessEqual(len(copied), 4)
            self.assertTrue(all(item["path"].is_file() for item in copied))


if __name__ == "__main__":
    unittest.main()
