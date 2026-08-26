from __future__ import annotations

import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from audit_core.common import AuditError
from audit_core.codex_runner import (
    DEFAULT_REASONING_EFFORT,
    MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO,
    PRODUCT_QUERY_REASONING_EFFORT,
    _copy_product_reference_images,
    _is_non_retryable_codex_error,
    _select_product_rag_candidates,
    _validate_photo_result,
)
from audit_core.display import _sales_catalog_reconciliation, sales_product_correspondence
from audit_core.personnel import (
    _line_result,
    _personnel_sales_knowledge_reconciliation,
)
from audit_core.product_rag import (
    apply_visible_short_code_exact_hits,
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
        self.assertEqual(len(products), 118)
        self.assertEqual(sum(len(product["views"]) for product in products), 499)
        by_barcode = {product["barcode_69"]: product for product in products}
        self.assertEqual(
            by_barcode["6970356167341"]["product_name"],
            "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下",
        )
        self.assertEqual(
            by_barcode["6970356167341"]["product_code"],
            "CP-KQ-YG-0085",
        )
        self.assertIn(
            "SP-1",
            by_barcode["6970356167341"]["product_code_aliases"],
        )
        self.assertEqual(
            by_barcode["6970356161042"]["product_code"],
            "CP-KQ-YG-0200",
        )
        for product in products:
            self.assertGreaterEqual(len(product["views"]), 1)
            self.assertEqual(
                len({item["view_id"] for item in product["views"]}),
                len(product["views"]),
            )
            self.assertTrue(ean13_is_valid(product["barcode_69"]))

    def test_catalog_covers_every_controlled_product_directory_and_image(self) -> None:
        products_root = (
            DISPLAY_SKILL / "canban-product-multimodal-knowledge-base" / "products"
        )
        directories = [item for item in products_root.iterdir() if item.is_dir()]
        physical_codes = {
            directory.name.rsplit("__", 1)[-1] for directory in directories
        }
        catalog_codes = {
            str(product["product_code"]) for product in self.catalog["products"]
        }
        self.assertEqual(catalog_codes, physical_codes)

        physical_images = {
            image.relative_to(DISPLAY_SKILL).as_posix()
            for directory in directories
            for image in directory.iterdir()
            if image.is_file()
        }
        catalog_images = {
            str(view["image_file"])
            for product in self.catalog["products"]
            for view in product["views"]
        }
        self.assertEqual(catalog_images, physical_images)

    def test_personnel_sales_requires_exact_barcode_and_allows_fuzzy_name(self) -> None:
        reconciliation = _personnel_sales_knowledge_reconciliation(
            [
                {
                    "barcode": "6970356166832",
                    "product_name": "参半羟基磷灰石牙膏100g满陇桂雨味",
                    "quantity": 238,
                    "excel_rows": [5, 17, 29, 41, 53, 65, 77],
                },
                {
                    "barcode": "6970356169338",
                    "product_name": "参半可益白牙膏葡萄知梨120g",
                    "quantity": 252,
                    "excel_rows": [10, 22, 34, 46, 58, 70, 82],
                },
            ],
            self.catalog,
        )
        self.assertEqual(reconciliation["status"], "pass")
        self.assertEqual(reconciliation["matched_count"], 1)
        self.assertEqual(reconciliation["fuzzy_count"], 1)
        fuzzy = reconciliation["records"][0]
        self.assertEqual(fuzzy["knowledge_status"], "fuzzy_matched")
        self.assertEqual(fuzzy["barcode_match"], "exact")
        self.assertEqual(fuzzy["name_match_type"], "fuzzy")
        self.assertEqual(fuzzy["knowledge_product_code"], "CP-KQ-YG-0205")

    def test_personnel_same_barcode_is_disambiguated_by_fuzzy_product_name(self) -> None:
        reconciliation = _personnel_sales_knowledge_reconciliation(
            [
                {
                    "barcode": "6970356162810",
                    "product_name": "参半沸石美白牙膏140g海角雏菊",
                    "quantity": 284,
                    "excel_rows": [7, 19, 31, 43, 55, 67, 79],
                }
            ],
            self.catalog,
        )
        item = reconciliation["records"][0]
        self.assertEqual(item["knowledge_status"], "fuzzy_matched")
        self.assertEqual(item["knowledge_product_code"], "CP-KQ-YG-0260")
        self.assertGreater(len(item["candidate_product_ids"]), 1)

    def test_personnel_knowledge_rejects_invalid_or_name_conflicting_sales_sku(self) -> None:
        invalid = _personnel_sales_knowledge_reconciliation(
            [
                {
                    "barcode": "6970356166833",
                    "product_name": "参半羟基磷灰石牙膏100g满陇桂雨味",
                    "quantity": 10,
                    "excel_rows": [2],
                }
            ],
            self.catalog,
        )["records"][0]
        self.assertEqual(invalid["knowledge_status"], "conflict")
        self.assertEqual(invalid["barcode_match"], "invalid")

        conflicting = _personnel_sales_knowledge_reconciliation(
            [
                {
                    "barcode": "6970356166832",
                    "product_name": "完全无关的儿童牙刷",
                    "quantity": 10,
                    "excel_rows": [2],
                }
            ],
            self.catalog,
        )["records"][0]
        self.assertEqual(conflicting["knowledge_status"], "conflict")
        self.assertEqual(conflicting["barcode_match"], "exact")
        self.assertEqual(conflicting["name_match_type"], "unmatched")

    def test_personnel_failed_knowledge_gate_caps_supported_reward_at_zero(self) -> None:
        line = {
            "line_no": 1,
            "product_name": "完全无关的儿童牙刷",
            "quantity": 10,
            "unit_reward": 3,
            "reward_amount": 30,
            "notes": [],
            "sales_match": {
                "barcode": "6970356166832",
                "excel_product_name": "完全无关的儿童牙刷",
                "confidence": "high",
                "basis": "测试映射",
            },
        }
        knowledge = _personnel_sales_knowledge_reconciliation(
            [
                {
                    "barcode": "6970356166832",
                    "product_name": "完全无关的儿童牙刷",
                    "quantity": 10,
                    "excel_rows": [2],
                }
            ],
            self.catalog,
        )["records"][0]
        sku_map = {
            "6970356166832": {
                "barcode": "6970356166832",
                "product_name": "完全无关的儿童牙刷",
                "quantity": 10,
                "excel_rows": [2],
                "store_quantities": [{"store_name": "示例门店", "quantity": 10}],
                "knowledge_match": knowledge,
            }
        }
        exceptions: list[dict] = []
        result, supported = _line_result(line, sku_map, set(), exceptions)
        self.assertEqual(supported, 0)
        self.assertEqual(result["supported_reward_amount"], 0)
        self.assertEqual(result["knowledge_status"], "conflict")
        self.assertIn(
            "SALES_PRODUCT_KNOWLEDGE_MISMATCH",
            {item["code"] for item in exceptions},
        )

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
                "参半锁白牙膏 沁爽青提味(120g)-线下新零售pingu联名款",
            ),
            "sampleimg-026": (
                "6970356168997",
                "参半益生菌清新口腔喷雾(20ml)沁润蜜桃胶盒装-线下",
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
                "product_name": "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下",
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
        self.assertIn("精确匹配（高置信度）", product_reference_label(resolved[0]))

    def test_unique_visible_short_code_is_exact_without_full_product_name(self) -> None:
        resolved = apply_visible_short_code_exact_hits(
            [],
            ["包装只能看清 sp - 1"],
            self.catalog,
        )
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["reference_product_id"], "canban-6970356167341")
        self.assertEqual(resolved[0]["confidence"], "exact")
        self.assertEqual(resolved[0]["matched_view_ids"], [])
        self.assertIn("SP-1", resolved[0]["visible_basis"][0])

    def test_unique_visible_short_code_promotes_fuzzy_hit_to_exact(self) -> None:
        candidate = resolve_product_reference_hits([_raw_hit("candidate")], self.catalog)
        resolved = apply_visible_short_code_exact_hits(
            candidate,
            ["SP1"],
            self.catalog,
        )
        self.assertEqual(resolved[0]["confidence"], "exact")

    def test_shared_visible_short_code_remains_fuzzy_without_disambiguation(self) -> None:
        resolved = apply_visible_short_code_exact_hits([], ["SP-4"], self.catalog)
        self.assertEqual(resolved, [])

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

    def test_wrong_barcode_row_is_not_attached_to_the_field_product(self) -> None:
        resolved = resolve_product_reference_hits([_raw_hit()], self.catalog)
        records = [
            {
                "excel_row": 2,
                "quantity": 1,
                "product_name": "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下",
                "product_code": "SP-1",
                "barcode": "6970356167341",
            },
            {
                "excel_row": 3,
                "quantity": 1,
                "product_name": "文字非常相似但条码不同的参半净清新牙膏",
                "product_code": "SP-1",
                "barcode": "6970356164241",
            },
        ]
        result = sales_product_correspondence(
            _sales_catalog_reconciliation(records, self.catalog)["records"],
            resolved,
        )
        self.assertEqual(result["status"], "exact")
        self.assertEqual(
            result["names"],
            ["参半oralshark玫瑰清茶味净清新牙膏(180g)-线下"],
        )
        checks = {item["excel_row"]: item for item in result["product_checks"]}
        self.assertEqual(checks[2]["status"], "exact")
        self.assertNotIn(3, checks)

    def test_candidate_rag_hit_cannot_be_promoted_to_exact_by_catalog_code(self) -> None:
        resolved = resolve_product_reference_hits([_raw_hit("candidate")], self.catalog)
        result = sales_product_correspondence(
            _sales_catalog_reconciliation(
                [
                    {
                        "excel_row": 2,
                        "quantity": 1,
                        "product_name": "参半oralshark玫瑰清茶味净清新牙膏180g",
                        "product_code": "SP-1",
                        "barcode": "6970356167341",
                    }
                ],
                self.catalog,
            )["records"],
            resolved,
        )
        self.assertEqual(result["status"], "candidate")
        self.assertEqual(result["product_checks"][0]["product_code_match"], "unverifiable")
        self.assertEqual(result["product_checks"][0]["barcode_match"], "unverifiable")
        self.assertIn("69码暂不比较", result["product_checks"][0]["barcode_comparison_basis"])

    def test_photo_barcode_is_compared_only_after_catalog_identity_is_exact(self) -> None:
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 2,
                    "quantity": 1,
                    "product_name": "参半玫瑰清茶净清新牙膏180g",
                    "product_code": "020299999",
                    "barcode": "6970356167341",
                }
            ],
            self.catalog,
        )
        self.assertEqual(reconciliation["status"], "fail")
        result = sales_product_correspondence(
            reconciliation["records"],
            resolve_product_reference_hits([_raw_hit("exact")], self.catalog),
        )
        self.assertEqual(result["status"], "unmatched")
        check = result["product_checks"][0]
        self.assertEqual(check["excel_row"], 2)
        self.assertEqual(check["product_code_match"], "mismatch")
        self.assertEqual(check["name_match"], "fuzzy")
        self.assertEqual(check["barcode_match"], "exact")
        self.assertEqual(check["status"], "unmatched")
        self.assertEqual(check["confidence"], "low")
        self.assertIn("先确定知识库商品", check["barcode_comparison_basis"])
        self.assertIn("商品编码改为CP-KQ-YG-0085", check["resubmission"])
        self.assertIn("商品名称、69码不用改", check["resubmission"])

    def test_exact_code_and_barcode_allow_a_fuzzy_product_name(self) -> None:
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 6,
                    "quantity": 3,
                    "product_name": "参半玫瑰清茶净清新牙膏180g",
                    "product_code": "CP-KQ-YG-0085",
                    "barcode": "6970356167341",
                }
            ],
            self.catalog,
        )
        result = sales_product_correspondence(
            reconciliation["records"],
            resolve_product_reference_hits([_raw_hit("exact")], self.catalog),
        )
        self.assertEqual(result["status"], "fuzzy")
        check = result["product_checks"][0]
        self.assertEqual(check["product_code_match"], "exact")
        self.assertEqual(check["name_match"], "fuzzy")
        self.assertEqual(check["barcode_match"], "exact")
        self.assertEqual(check["status"], "fuzzy")
        self.assertEqual(check["confidence"], "medium")
        self.assertIsNone(check["resubmission"])

    def test_every_sales_row_resolved_to_the_same_field_product_is_kept(self) -> None:
        resolved = resolve_product_reference_hits([_raw_hit("exact")], self.catalog)
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 2,
                    "quantity": 3,
                    "product_name": "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下",
                    "product_code": "CP-KQ-YG-0085",
                    "barcode": "6970356167341",
                },
                {
                    "excel_row": 9,
                    "quantity": 5,
                    "product_name": "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下",
                    "product_code": "SP-1",
                    "barcode": "6970356167341",
                },
            ],
            self.catalog,
        )
        result = sales_product_correspondence(reconciliation["records"], resolved)
        self.assertEqual(
            [item["excel_row"] for item in result["product_checks"]],
            [2, 9],
        )
        self.assertEqual(result["status"], "exact")
        self.assertTrue(
            all(item["status"] == "exact" for item in result["product_checks"])
        )

    def test_same_barcode_uses_name_to_select_the_relevant_sales_row(self) -> None:
        hit = {
            "reference_product_id": "canban-6970356164159-cp-gj-sds-0168",
            "product_code": "CP-GJ-SDS-0168",
            "product_code_aliases": [],
            "product_name": "参半白巧棒牙刷（单支装 ）代言人",
            "barcode_69": "6970356164159",
            "confidence": "exact",
        }
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 10,
                    "quantity": 2,
                    "product_name": "参半白巧棒成人牙刷（单支装）",
                    "product_code": "020260010",
                    "barcode": "6970356164159",
                },
                {
                    "excel_row": 11,
                    "quantity": 4,
                    "product_name": "参半白巧棒牙刷（单支装）代言人",
                    "product_code": "020260011",
                    "barcode": "6970356164159",
                },
            ],
            self.catalog,
        )
        result = sales_product_correspondence(reconciliation["records"], [hit])
        self.assertEqual([item["excel_row"] for item in result["product_checks"]], [11])
        check = result["product_checks"][0]
        self.assertEqual(check["product_code_match"], "mismatch")
        self.assertEqual(check["name_match"], "exact")
        self.assertEqual(check["barcode_match"], "exact")
        self.assertEqual(result["status"], "unmatched")

    def test_sales_name_short_code_cannot_attach_a_different_barcode_product(self) -> None:
        hit = {
            "reference_product_id": "canban-6970356164265",
            "product_code": "CP-KQ-YG-0439",
            "product_code_aliases": ["SP-3"],
            "product_name": "参半oralshark绿野青提味星钻白牙膏（160g)",
            "barcode_69": "6970356164265",
            "confidence": "exact",
        }
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 6,
                    "quantity": 61,
                    "product_name": "参半oralshark-SP3清水白桃味星璨白牙膏160g",
                    "product_code": "020260008",
                    "barcode": "6970356164258",
                }
            ],
            self.catalog,
        )
        result = sales_product_correspondence(reconciliation["records"], [hit])
        self.assertEqual([item["excel_row"] for item in result["product_checks"]], [None])
        check = result["product_checks"][0]
        self.assertEqual(check["product_code_match"], "unverifiable")
        self.assertEqual(check["barcode_match"], "unverifiable")
        self.assertEqual(check["status"], "unmatched")
        self.assertIn("69码为6970356164265", check["basis"])
        self.assertNotIn("第6行", check["resubmission"])
        self.assertIn("不要把其他商品行改成这一商品", check["resubmission"])

    def test_sales_excel_must_resolve_through_knowledge_before_file_comparison(self) -> None:
        records = [
            {
                "excel_row": 2,
                "quantity": 8,
                "product_name": "参半oralshark玫瑰清茶味净清新牙膏180g",
                "product_code": "CP-KQ-YG-0085",
                "barcode": "6970356167341",
            },
            {
                "excel_row": 3,
                "quantity": 5,
                "product_name": "完全不在知识库的商品",
                "product_code": "020299999",
                "barcode": "6970356167334",
            },
        ]
        reconciliation = _sales_catalog_reconciliation(records, self.catalog)
        self.assertEqual(reconciliation["status"], "fail")
        self.assertEqual(reconciliation["matched_count"], 1)
        self.assertEqual(reconciliation["fuzzy_count"], 0)
        self.assertEqual(reconciliation["problem_rows"], [3])
        first, second = reconciliation["records"]
        self.assertEqual(first["knowledge_status"], "matched")
        self.assertEqual(first["knowledge_product_code"], "CP-KQ-YG-0085")
        self.assertEqual(first["matched_fields"], ["product_code", "product_name", "barcode_69"])
        self.assertEqual(first["unmatched_fields"], [])
        self.assertEqual(second["knowledge_status"], "conflict")
        first_fields = {item["field"]: item for item in first["field_comparisons"]}
        self.assertEqual(first_fields["product_code"]["comparison"], "matched")
        self.assertEqual(
            first_fields["product_code"]["selected_knowledge_value"],
            "CP-KQ-YG-0085",
        )
        self.assertEqual(first_fields["product_name"]["comparison"], "matched")
        self.assertEqual(first_fields["barcode_69"]["comparison"], "matched")
        second_fields = {item["field"]: item for item in second["field_comparisons"]}
        self.assertEqual(second_fields["product_code"]["comparison"], "not_found")
        self.assertEqual(second_fields["product_name"]["comparison"], "not_found")
        self.assertEqual(second_fields["barcode_69"]["comparison"], "matched")

    def test_sales_name_may_be_fuzzy_only_after_code_and_barcode_are_strict(self) -> None:
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 8,
                    "quantity": 1,
                    "product_name": "参半玫瑰清茶净清新牙膏180g",
                    "product_code": "SP-1",
                    "barcode": "6970356167341",
                }
            ],
            self.catalog,
        )
        item = reconciliation["records"][0]
        self.assertEqual(reconciliation["status"], "pass")
        self.assertEqual(reconciliation["fuzzy_count"], 1)
        self.assertEqual(item["knowledge_status"], "fuzzy_matched")
        comparisons = {value["field"]: value for value in item["field_comparisons"]}
        self.assertEqual(comparisons["product_code"]["comparison"], "matched")
        self.assertEqual(comparisons["product_name"]["comparison"], "fuzzy")
        self.assertEqual(comparisons["barcode_69"]["comparison"], "matched")

    def test_wrong_product_code_fails_even_when_name_and_barcode_match(self) -> None:
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 9,
                    "quantity": 1,
                    "product_name": "参半oralshark玫瑰清茶味净清新牙膏180g",
                    "product_code": "NOT-THE-KNOWLEDGE-CODE",
                    "barcode": "6970356167341",
                }
            ],
            self.catalog,
        )
        item = reconciliation["records"][0]
        self.assertEqual(reconciliation["status"], "fail")
        self.assertEqual(item["knowledge_status"], "conflict")
        self.assertIn("product_code", item["unmatched_fields"])

    def test_same_barcode_is_disambiguated_by_exact_knowledge_name(self) -> None:
        reconciliation = _sales_catalog_reconciliation(
            [
                {
                    "excel_row": 11,
                    "quantity": 1,
                    "product_name": "参半白巧棒牙刷（单支装 ）代言人",
                    "product_code": "CP-GJ-SDS-0168",
                    "barcode": "6970356164159",
                }
            ],
            self.catalog,
        )
        item = reconciliation["records"][0]
        self.assertEqual(item["knowledge_status"], "matched")
        self.assertEqual(item["knowledge_product_code"], "CP-GJ-SDS-0168")
        self.assertIn("product_name", item["matched_fields"])
        self.assertIn("barcode_69", item["matched_fields"])

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
            _sales_catalog_reconciliation(
                [
                    {
                        "excel_row": 2,
                        "quantity": 1,
                        "product_name": "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下",
                        "product_code": "SP-1",
                        "barcode": "6970356167341",
                    }
                ],
                self.catalog,
            )["records"],
            resolved,
        )
        self.assertEqual(result["status"], "exact")
        self.assertIn("商品编码、商品名称和69码均严格一致", result["basis"])

    def test_newly_registered_controlled_product_is_exposed_to_runtime_catalog(self) -> None:
        raw = {
            "reference_product_id": "canban-6978974201812",
            "confidence": "exact",
            "matched_view_ids": ["kb-cp-etgj-sds-0021-v01"],
            "visible_basis": ["现场包装可见小黄人联名款、黄色刷柄和单支装"],
            "limitations": [],
        }
        resolved = resolve_product_reference_hits([raw], self.catalog)
        self.assertEqual(len(resolved), 1)
        self.assertEqual(resolved[0]["product_code"], "CP-ETGJ-SDS-0021")
        self.assertEqual(resolved[0]["barcode_69"], "6978974201812")
        self.assertEqual(
            resolved[0]["product_name"],
            "参半儿童分龄护齿牙刷（单支装）小黄人联名款 黄色刷柄-线下",
        )

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

    def test_candidate_retrieval_honors_two_per_photo_contract(self) -> None:
        products = [
            {
                "product_id": f"product-{index}",
                "barcode_69": f"unreadable-{index}",
                "product_name": f"参半清新牙膏{index}",
                "product_code": "未标注",
                "product_code_aliases": [],
                "aliases": [],
                "specification": "",
                "specification_aliases": [],
                "variant": "",
                "variant_aliases": [],
                "views": [{}],
            }
            for index in range(1, 4)
        ]
        selected = _select_product_rag_candidates(
            {"schema_version": "test", "products": products},
            {
                "photo_queries": [
                    {
                        "visible_barcodes_69": [],
                        "visible_product_names": ["参半清新牙膏"],
                        "visible_product_codes": [],
                        "visible_text": [],
                    }
                ]
            },
        )
        self.assertEqual(MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO, 2)
        self.assertEqual(
            [product["product_id"] for product in selected["products"]],
            ["product-1", "product-2"],
        )

    def test_reasoning_policy_keeps_final_judgment_high(self) -> None:
        self.assertEqual(PRODUCT_QUERY_REASONING_EFFORT, "medium")
        self.assertEqual(DEFAULT_REASONING_EFFORT, "high")

    def test_invalid_codex_request_configuration_is_not_retried(self) -> None:
        self.assertTrue(
            _is_non_retryable_codex_error(
                "invalid_json_schema: uniqueItems is not permitted"
            )
        )
        self.assertTrue(_is_non_retryable_codex_error("invalid_request_error"))
        self.assertFalse(_is_non_retryable_codex_error("request timed out"))

    def test_four_vertical_requires_matching_left_to_right_facing_evidence(self) -> None:
        case = {"photo_files": ["store.jpg"]}
        contract = {
            "contract": {
                "stores": [{"line_no": 1, "store_name": "示例门店"}],
            }
        }

        def evidence(count: int, basis: list[str]) -> dict:
            return {
                "photo_reviews": [
                    {
                        "store_line_no": 1,
                        "contract_store_name": "示例门店",
                        "photo_files": ["store.jpg"],
                        "product_reference_hits": [],
                        "display_observation": {
                            "standard_evidence": "meets",
                            "matched_standard": "four_vertical",
                            "vertical_facing_count": count,
                            "vertical_facing_basis": basis,
                            "stack_1sqm_basis": None,
                            "description": "同一展示面从左到右可数出4个纵向陈列列",
                        },
                    }
                ]
            }

        _validate_photo_result(
            case,
            contract,
            evidence(4, ["左一绿盒", "左二红盒", "右一白紫盒", "右二窄白盒"]),
        )
        with self.assertRaisesRegex(AuditError, "逐列依据数量不一致"):
            _validate_photo_result(
                case,
                contract,
                evidence(4, ["左一绿盒", "左二红盒", "右一白紫盒"]),
            )
        with self.assertRaisesRegex(AuditError, "至少4个可见纵列"):
            _validate_photo_result(
                case,
                contract,
                evidence(3, ["左一绿盒", "左二红盒", "右一白紫盒"]),
            )
        with self.assertRaisesRegex(AuditError, "逐列依据存在重复"):
            _validate_photo_result(
                case,
                contract,
                evidence(4, ["左一绿盒", "左二红盒", "右一白紫盒", "右一白紫盒"]),
            )
        with self.assertRaisesRegex(AuditError, "逐列依据存在重复"):
            _validate_photo_result(
                case,
                contract,
                evidence(4, ["左一绿盒", "左二红盒", "右一白紫盒", " 右一白紫盒 "]),
            )
        with self.assertRaisesRegex(AuditError, "逐列依据包含空白项"):
            _validate_photo_result(
                case,
                contract,
                evidence(4, ["左一绿盒", "左二红盒", "右一白紫盒", "   "]),
            )


if __name__ == "__main__":
    unittest.main()
