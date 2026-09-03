from __future__ import annotations

import hashlib
import json
import unittest
from tempfile import TemporaryDirectory
from pathlib import Path

from PIL import Image, ImageDraw

from audit_core.common import AuditError, validate_json
from audit_core.codex_runner import (
    ALLOWED_REASONING_EFFORTS,
    DEFAULT_REASONING_EFFORT,
    MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO,
    PRODUCT_QUERY_REASONING_EFFORT,
    _apply_display_standard_calibrations,
    _apply_display_standard_review,
    _copy_product_reference_images,
    _apply_contract_product_cells,
    _is_non_retryable_codex_error,
    _prepare_contract_product_cell_views,
    _select_product_rag_candidates,
    _validate_contract_product_cells,
    _validate_display_standard_review,
    _validate_photo_result,
    _write_codex_output_schema,
)
from audit_core import display as display_module
from audit_core.display import (
    _contract_attachment_knowledge_reconciliation,
    _contract_product_knowledge_reconciliation,
    _product_records_catalog_reconciliation,
)
from audit_core.personnel import (
    _line_result,
    _map_settlement_lines,
    _personnel_sales_knowledge_reconciliation,
)
from audit_core.product_rag import (
    SHARED_PRODUCT_RAG_DIR,
    apply_visible_catalog_text_exact_hits,
    apply_visible_short_code_exact_hits,
    ean13_is_valid,
    load_pending_product_rag,
    load_product_rag,
    product_reference_label,
    resolve_product_reference_hits,
)


PRODUCT_RAG_ASSET_ROOT = SHARED_PRODUCT_RAG_DIR.parent


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
        cls.catalog = load_product_rag()

    def test_catalog_has_real_multi_view_products_with_valid_identifiers(self) -> None:
        products = self.catalog["products"]
        self.assertEqual(len(products), 130)
        self.assertEqual(sum(len(product["views"]) for product in products), 537)
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
        products_root = SHARED_PRODUCT_RAG_DIR / "products"
        directories = [item for item in products_root.iterdir() if item.is_dir()]
        physical_codes = {
            directory.name.rsplit("__", 1)[-1] for directory in directories
        }
        catalog_codes = {
            str(product["product_code"]) for product in self.catalog["products"]
        }
        self.assertEqual(catalog_codes, physical_codes)

        physical_images = {
            image.relative_to(PRODUCT_RAG_ASSET_ROOT).as_posix()
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

    def test_personnel_corrupted_settlement_name_uses_unique_knowledge_backed_quantity(self) -> None:
        sales_skus = [
            {
                "barcode": "6970356162391",
                "product_name": "参半沸石净齿牙膏140g清新苍兰",
                "quantity": 275,
                "knowledge_match": {
                    "knowledge_status": "fuzzy_matched",
                    "knowledge_product_id": "canban-6970356162391",
                    "knowledge_product_name": "参半沸石净齿牙膏(140g)-线下",
                },
            },
            {
                "barcode": "6970356162810",
                "product_name": "参半沸石美白牙膏140g海角雏菊",
                "quantity": 284,
                "knowledge_match": {
                    "knowledge_status": "fuzzy_matched",
                    "knowledge_product_id": "canban-6970356162810-cp-kq-yg-0260",
                    "knowledge_product_name": "参半沸石美白牙膏(140g)-线下",
                },
            },
        ]
        mapped = _map_settlement_lines(
            [
                {
                    "line_no": 1,
                    "product_name": "净白净齿牙膏",
                    "barcode_visible": None,
                    "quantity": 275,
                    "notes": [],
                }
            ],
            sales_skus,
            self.catalog,
        )
        self.assertEqual(mapped[0]["sales_match"]["barcode"], "6970356162391")
        self.assertEqual(mapped[0]["sales_match"]["confidence"], "medium")
        self.assertIn("结算数量", mapped[0]["sales_match"]["basis"])
        self.assertIn("商品名称只作模糊辅助", mapped[0]["sales_match"]["basis"])

    def test_personnel_duplicate_quantity_stays_ambiguous_when_name_cannot_help(self) -> None:
        sales_skus = [
            {
                "barcode": "6970356162391",
                "product_name": "参半沸石净齿牙膏140g清新苍兰",
                "quantity": 275,
                "knowledge_match": {"knowledge_status": "fuzzy_matched"},
            },
            {
                "barcode": "6970356162810",
                "product_name": "参半沸石美白牙膏140g海角雏菊",
                "quantity": 275,
                "knowledge_match": {"knowledge_status": "fuzzy_matched"},
            },
        ]
        mapped = _map_settlement_lines(
            [
                {
                    "line_no": 1,
                    "product_name": "OCR",
                    "barcode_visible": None,
                    "quantity": 275,
                    "notes": [],
                }
            ],
            sales_skus,
            self.catalog,
        )
        self.assertIsNone(mapped[0]["sales_match"]["barcode"])
        self.assertEqual(mapped[0]["sales_match"]["confidence"], "ambiguous")

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
        manifest = load_pending_product_rag()
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

    def test_unique_visible_short_code_without_visual_hit_is_not_exact(self) -> None:
        resolved = apply_visible_short_code_exact_hits(
            [],
            ["包装只能看清 sp - 1"],
            self.catalog,
        )
        self.assertEqual(resolved, [])

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

    def test_unique_visible_bundle_text_promotes_existing_visual_hit(self) -> None:
        raw = {
            "reference_product_id": "canban-6970356166979",
            "confidence": "candidate",
            "matched_view_ids": ["sample-009-v01"],
            "visible_basis": ["现场可见3+2、420g、量贩装"],
            "limitations": [],
        }
        candidate = resolve_product_reference_hits([raw], self.catalog)
        resolved = apply_visible_catalog_text_exact_hits(
            candidate,
            ["3+2", "牙膏420g", "量贩装"],
            self.catalog,
        )
        self.assertEqual(resolved[0]["confidence"], "exact")
        self.assertIn("完整知识库唯一收敛", resolved[0]["visible_basis"][-1])

    def test_visible_bundle_text_without_visual_hit_never_creates_product(self) -> None:
        self.assertEqual(
            apply_visible_catalog_text_exact_hits(
                [],
                ["3+2", "牙膏420g", "量贩装"],
                self.catalog,
            ),
            [],
        )

    def test_contract_product_requires_exact_code_and_barcode_but_not_exact_name(self) -> None:
        record = {
            "excel_row": 3,
            "product_code": "CP-KQ-YG-0085",
            "product_name": "玫瑰清茶净清新牙膏180克",
            "barcode": "6970356167341",
            "quantity": 12,
            "unit": "支",
            "retail_price": 19.9,
            "total_amount": 238.8,
        }
        result = _product_records_catalog_reconciliation([record], self.catalog)
        item = result["records"][0]
        self.assertEqual(result["status"], "pass")
        self.assertEqual(item["knowledge_status"], "fuzzy_matched")
        self.assertEqual(item["knowledge_product_code"], "CP-KQ-YG-0085")
        self.assertEqual(item["knowledge_barcode_69"], "6970356167341")
        self.assertEqual(item["name_match_type"], "fuzzy")
        self.assertNotIn("product_name", item["unmatched_fields"])
        self.assertNotIn("product_name", item["conflicting_fields"])

    def test_contract_wrong_product_code_fails_even_when_barcode_and_fuzzy_name_locate_product(self) -> None:
        record = {
            "excel_row": 20,
            "product_code": "030160007",
            "product_name": "玫瑰清茶净清新牙膏180g",
            "barcode": "6970356167341",
            "quantity": 12,
            "unit": "支",
            "retail_price": 19.9,
            "total_amount": 238.8,
        }
        result = _product_records_catalog_reconciliation([record], self.catalog)
        item = result["records"][0]
        self.assertEqual(result["status"], "fail")
        self.assertEqual(item["knowledge_status"], "conflict")
        self.assertEqual(item["knowledge_product_code"], "CP-KQ-YG-0085")
        self.assertEqual(item["knowledge_barcode_69"], "6970356167341")
        self.assertIn("product_code", item["conflicting_fields"])
        self.assertNotIn("product_name", item["unmatched_fields"])
        self.assertNotIn("商品名称", item["basis"])

    def test_focused_contract_product_cells_require_exact_row_order_and_dense_values(self) -> None:
        requested = [
            {
                "line_no": 1,
                "source_page": 3,
                "product_code": None,
                "product_name": None,
                "barcode_69": "6970356167341",
                "quantity": 12,
                "retail_price": 19.9,
                "total_amount": 238.8,
            },
            {
                "line_no": 2,
                "source_page": 3,
                "product_code": None,
                "product_name": None,
                "barcode_69": "6970356166979",
                "quantity": 8,
                "retail_price": 39.0,
                "total_amount": 312.0,
            },
        ]
        reread = {
            "records": [
                {
                    "line_no": 1,
                    "source_page": 3,
                    "product_code": "030160007",
                    "product_name": "玫瑰清茶净清新牙膏",
                    "barcode_69": "6970356167341",
                },
                {
                    "line_no": 2,
                    "source_page": 3,
                    "product_code": "030160011",
                    "product_name": "3+2量贩装",
                    "barcode_69": "6970356166979",
                },
            ],
            "extraction_notes": [],
        }
        _validate_contract_product_cells(requested, reread)

        reversed_rows = {**reread, "records": list(reversed(reread["records"]))}
        with self.assertRaisesRegex(AuditError, "逐行、按页、按原顺序"):
            _validate_contract_product_cells(requested, reversed_rows)

        missing_name = {
            **reread,
            "records": [
                {**reread["records"][0], "product_name": None},
                reread["records"][1],
            ],
        }
        with self.assertRaisesRegex(AuditError, "仍漏掉商品名称"):
            _validate_contract_product_cells(requested, missing_name)

        missing_barcode = {
            **reread,
            "records": [
                {**reread["records"][0], "barcode_69": None},
                reread["records"][1],
            ],
        }
        with self.assertRaisesRegex(AuditError, "仍漏掉69码"):
            _validate_contract_product_cells(requested, missing_barcode)

        invalid_barcode = {
            **reread,
            "records": [
                {**reread["records"][0], "barcode_69": "6970356167342"},
                reread["records"][1],
            ],
        }
        with self.assertRaisesRegex(AuditError, "无效EAN-13"):
            _validate_contract_product_cells(requested, invalid_barcode)

    def test_focused_contract_product_cells_replace_only_nonempty_readings(self) -> None:
        contract = {
            "contract": {
                "sales_attachment": {
                    "records": [
                        {
                            "line_no": 1,
                            "source_page": 3,
                            "product_code": "旧编码",
                            "product_name": "旧名称",
                            "barcode_69": "6970356164500",
                        },
                        {
                            "line_no": 2,
                            "source_page": 3,
                            "product_code": "保留编码",
                            "product_name": "保留名称",
                            "barcode_69": "6970356164395",
                        },
                    ]
                }
            },
            "extraction_notes": ["首轮读取"],
        }
        reread = {
            "records": [
                {
                    "line_no": 1,
                    "source_page": 3,
                    "product_code": "030160007",
                    "product_name": "聚焦复读名称",
                    "barcode_69": "6970356167341",
                },
                {
                    "line_no": 2,
                    "source_page": 3,
                    "product_code": None,
                    "product_name": None,
                    "barcode_69": None,
                },
            ],
            "extraction_notes": ["第二行确实不可读"],
        }
        _apply_contract_product_cells(contract, reread)
        records = contract["contract"]["sales_attachment"]["records"]
        self.assertEqual(records[0]["product_code"], "030160007")
        self.assertEqual(records[0]["product_name"], "聚焦复读名称")
        self.assertEqual(records[0]["barcode_69"], "6970356167341")
        self.assertEqual(records[1]["product_code"], "保留编码")
        self.assertEqual(records[1]["product_name"], "保留名称")
        self.assertEqual(records[1]["barcode_69"], "6970356164395")
        self.assertIn("第二行确实不可读", contract["extraction_notes"])
        self.assertIn("已完成原图聚焦二次复核", contract["extraction_notes"][-1])

    def test_contract_product_cell_views_include_all_orientations_and_row_bands(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "contract-page-01.png"
            image = Image.new("RGB", (300, 500), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((80, 50, 220, 450), outline="black", width=3)
            for offset in range(70, 450, 20):
                draw.line((80, offset, 220, offset), fill="black", width=2)
            image.save(page)

            views = _prepare_contract_product_cell_views(
                [page],
                [
                    {"line_no": line_no, "source_page": 1}
                    for line_no in range(1, 17)
                ],
                root / "focus",
            )

            names = {path.name for path in views}
            self.assertEqual(len(views), 6)
            self.assertIn("contract-page-01--clockwise--full.png", names)
            self.assertIn("contract-page-01--counterclockwise--full.png", names)
            self.assertIn(
                "contract-page-01--clockwise--band-01-of-02.png", names
            )
            self.assertIn(
                "contract-page-01--counterclockwise--band-02-of-02.png", names
            )
            self.assertTrue(all(path.is_file() for path in views))

    def test_contract_product_cell_views_add_black_ink_bands_for_red_seals(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "contract-page-01.png"
            image = Image.new("RGB", (300, 500), "white")
            draw = ImageDraw.Draw(image)
            draw.rectangle((80, 50, 220, 450), outline="black", width=3)
            for offset in range(70, 450, 20):
                draw.line((80, offset, 220, offset), fill="black", width=2)
            draw.ellipse((105, 310, 205, 410), outline=(190, 20, 20), width=10)
            image.save(page)

            views = _prepare_contract_product_cell_views(
                [page],
                [
                    {"line_no": line_no, "source_page": 1}
                    for line_no in range(1, 17)
                ],
                root / "focus",
            )

            black_ink_views = [
                path for path in views if "black-ink-product-cells" in path.name
            ]
            self.assertTrue(black_ink_views)
            with Image.open(black_ink_views[0]) as black_ink:
                self.assertGreater(black_ink.width, black_ink.height)
                self.assertEqual(black_ink.mode, "RGB")

    def test_contract_product_cell_views_reject_unknown_source_page(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            page = root / "contract-page-01.png"
            Image.new("RGB", (20, 20), "white").save(page)
            with self.assertRaisesRegex(AuditError, "不存在的PDF页"):
                _prepare_contract_product_cell_views(
                    [page],
                    [{"line_no": 1, "source_page": 2}],
                    root / "focus",
                )

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

    def _attachment_contract(self, records: list[dict]) -> dict:
        return {
            "sales_attachment": {
                "present": True,
                "records": records,
            }
        }

    def _attachment_record(
        self,
        *,
        line_no: int,
        product_code: str | None,
        product_name: str | None,
        barcode_69: str | None,
    ) -> dict:
        return {
            "line_no": line_no,
            "source_page": 4,
            "customer_name": "示例经销商",
            "business_date": "2026-04",
            "product_code": product_code,
            "product_name": product_name,
            "barcode_69": barcode_69,
            "unit": "件",
            "quantity": 1,
            "retail_price": 10,
            "total_amount": 10,
        }

    def test_contract_attachment_uses_strict_code_and_barcode_with_fuzzy_name(self) -> None:
        contract = self._attachment_contract(
            [
                self._attachment_record(
                    line_no=1,
                    product_code="CP-KQ-YG-0085",
                    product_name="参半oralshark玫瑰清茶味净清新牙膏(180g)-线下",
                    barcode_69="6970356167341",
                ),
                self._attachment_record(
                    line_no=2,
                    product_code="CP-KQ-YG-0085",
                    product_name="参半玫瑰清茶净清新牙膏180g",
                    barcode_69="6970356167341",
                ),
            ]
        )
        result = _contract_attachment_knowledge_reconciliation(
            contract,
            self.catalog,
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["fuzzy_count"], 1)
        exact, fuzzy = result["records"]
        self.assertEqual(exact["knowledge_status"], "matched")
        self.assertEqual(fuzzy["knowledge_status"], "fuzzy_matched")
        self.assertEqual(fuzzy["knowledge_product_code"], "CP-KQ-YG-0085")
        fuzzy_fields = {
            item["field"]: item["comparison"]
            for item in fuzzy["field_comparisons"]
        }
        self.assertEqual(fuzzy_fields["product_code"], "matched")
        self.assertEqual(fuzzy_fields["product_name"], "fuzzy")
        self.assertEqual(fuzzy_fields["barcode_69"], "matched")

    def test_contract_attachment_rejects_wrong_or_packaging_alias_code(self) -> None:
        for product_code in ("NOT-THE-KNOWLEDGE-CODE", "SP-1"):
            with self.subTest(product_code=product_code):
                result = _contract_attachment_knowledge_reconciliation(
                    self._attachment_contract(
                        [
                            self._attachment_record(
                                line_no=1,
                                product_code=product_code,
                                product_name=(
                                    "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下"
                                ),
                                barcode_69="6970356167341",
                            )
                        ]
                    ),
                    self.catalog,
                )
                self.assertEqual(result["status"], "fail")
                record = result["records"][0]
                self.assertEqual(record["knowledge_status"], "conflict")
                comparisons = {
                    item["field"]: item["comparison"]
                    for item in record["field_comparisons"]
                }
                self.assertEqual(comparisons["product_code"], "conflict")
                self.assertEqual(comparisons["product_name"], "matched")
                self.assertEqual(comparisons["barcode_69"], "matched")

    def test_contract_attachment_same_barcode_prefers_authoritative_non_edition_name(self) -> None:
        result = _contract_attachment_knowledge_reconciliation(
            self._attachment_contract(
                [
                    self._attachment_record(
                        line_no=1,
                        product_code="020260009",
                        product_name="参半-Oralshark-SP2凝香茉莉味氨基酸牙膏",
                        barcode_69="6970356164241",
                    )
                ]
            ),
            self.catalog,
        )
        record = result["records"][0]
        self.assertEqual(record["knowledge_status"], "conflict")
        self.assertEqual(record["knowledge_product_code"], "CP-KQ-YG-0016")
        self.assertEqual(record["knowledge_barcode_69"], "6970356164241")
        self.assertEqual(record["name_match_type"], "fuzzy")

    def test_core_contract_product_can_resolve_by_exact_catalog_code(self) -> None:
        result = _contract_product_knowledge_reconciliation(
            {
                "requires_specific_products": True,
                "required_products": ["合同印刷商品编码 CP-KQ-YG-0085"],
                "required_product_identities": [
                    {
                        "visible_text": "合同印刷商品编码 CP-KQ-YG-0085",
                        "product_code": "CP-KQ-YG-0085",
                        "product_name": None,
                        "barcode_69": None,
                    }
                ],
            },
            self.catalog,
        )
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            result["records"][0]["knowledge_product_id"],
            "canban-6970356167341",
        )

    def test_core_contract_conflicting_identifiers_fail_closed(self) -> None:
        result = _contract_product_knowledge_reconciliation(
            {
                "requires_specific_products": True,
                "required_products": ["冲突商品"],
                "required_product_identities": [
                    {
                        "visible_text": "冲突商品",
                        "product_code": "CP-KQ-YG-0085",
                        "product_name": (
                            "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下"
                        ),
                        "barcode_69": "6970356164241",
                    }
                ],
            },
            self.catalog,
        )
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["records"][0]["knowledge_status"], "conflict")

    def test_standalone_sales_has_no_catalog_reconciliation_cross_link(self) -> None:
        self.assertFalse(hasattr(display_module, "_sales_catalog_reconciliation"))
        self.assertFalse(hasattr(display_module, "sales_product_correspondence"))

    def test_packaging_code_alias_retrieves_photo_candidate(self) -> None:
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
                SHARED_PRODUCT_RAG_DIR,
                Path(temporary),
                selected,
            )
            self.assertLessEqual(len(copied), 4)
            self.assertTrue(all(item["path"].is_file() for item in copied))

    def test_candidate_retrieval_honors_four_per_photo_contract(self) -> None:
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
            for index in range(1, 6)
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
        self.assertEqual(MAX_PRODUCT_REFERENCE_CANDIDATES_PER_PHOTO, 4)
        self.assertEqual(
            [product["product_id"] for product in selected["products"]],
            ["product-1", "product-2", "product-3", "product-4"],
        )

    def test_reasoning_policy_keeps_final_judgment_high(self) -> None:
        self.assertEqual(PRODUCT_QUERY_REASONING_EFFORT, "medium")
        self.assertEqual(DEFAULT_REASONING_EFFORT, "high")
        self.assertIn("max", ALLOWED_REASONING_EFFORTS)

    def test_invalid_codex_request_configuration_is_not_retried(self) -> None:
        self.assertTrue(
            _is_non_retryable_codex_error(
                "invalid_json_schema: uniqueItems is not permitted"
            )
        )
        self.assertTrue(_is_non_retryable_codex_error("invalid_request_error"))
        self.assertFalse(_is_non_retryable_codex_error("request timed out"))

    def test_codex_output_schema_omits_unique_items_but_full_validation_keeps_it(
        self,
    ) -> None:
        schema = {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "additionalProperties": False,
            "required": ["party_names"],
            "properties": {
                "party_names": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": {"type": "string"},
                },
                "approval": {
                    "oneOf": [
                        {"type": "null"},
                        {"type": "string"},
                    ]
                },
            },
        }
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "evidence.schema.json"
            destination = root / "codex-output.schema.json"
            source.write_text(
                json.dumps(schema, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            _write_codex_output_schema(source, destination)

            compatible = json.loads(destination.read_text(encoding="utf-8"))
            compatible_array = compatible["properties"]["party_names"]
            self.assertNotIn("uniqueItems", compatible_array)
            self.assertEqual(compatible_array["minItems"], 1)
            self.assertNotIn("oneOf", compatible["properties"]["approval"])
            self.assertEqual(
                compatible["properties"]["approval"]["anyOf"],
                [{"type": "null"}, {"type": "string"}],
            )
            self.assertTrue(
                json.loads(source.read_text(encoding="utf-8"))["properties"][
                    "party_names"
                ]["uniqueItems"]
            )
            with self.assertRaisesRegex(AuditError, "party_names"):
                validate_json({"party_names": ["same", "same"]}, source)

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

    def test_focused_display_standard_review_preserves_routing_and_overwrites_only_observation(self) -> None:
        requested = [
            {
                "store_line_no": 1,
                "contract_store_name": "百佳华百货（公明店）",
                "photo_files": ["3.公明百佳华.jpg"],
                "recognized_products": ["保留商品"],
                "display_observation": {
                    "standard_evidence": "meets",
                    "matched_standard": "four_vertical",
                    "vertical_facing_count": 4,
                    "vertical_facing_basis": ["旧1", "旧2", "旧3", "旧4"],
                    "stack_1sqm_basis": None,
                    "description": "旧结论",
                    "limitations": [],
                },
            },
            {
                "store_line_no": 2,
                "contract_store_name": "华都超市（东坑大道北店）",
                "photo_files": ["5.华都超市(东坑大道北店).jpg"],
                "recognized_products": ["保留商品2"],
                "display_observation": {
                    "standard_evidence": "unclear",
                    "matched_standard": "unclear",
                    "vertical_facing_count": 3,
                    "vertical_facing_basis": ["旧1", "旧2", "旧3"],
                    "stack_1sqm_basis": None,
                    "description": "旧结论2",
                    "limitations": [],
                },
            },
        ]
        focused = {
            "schema_version": "1.0",
            "display_reviews": [
                {
                    "store_line_no": 1,
                    "contract_store_name": "百佳华百货（公明店）",
                    "photo_files": ["3.公明百佳华.jpg"],
                    "display_observation": {
                        "standard_evidence": "unclear",
                        "matched_standard": "unclear",
                        "vertical_facing_count": 3,
                        "vertical_facing_basis": [
                            "左侧礼盒列",
                            "中间礼盒列",
                            "右侧礼盒列",
                        ],
                        "stack_1sqm_basis": "照片未提供尺寸或比例依据",
                        "description": "三个正面礼盒；右侧窄面属于第三个盒子的侧板。",
                        "limitations": [],
                    },
                },
                {
                    "store_line_no": 2,
                    "contract_store_name": "华都超市（东坑大道北店）",
                    "photo_files": ["5.华都超市(东坑大道北店).jpg"],
                    "display_observation": {
                        "standard_evidence": "meets",
                        "matched_standard": "four_vertical",
                        "vertical_facing_count": 4,
                        "vertical_facing_basis": [
                            "左侧绿色独立堆列",
                            "中间红色独立堆列",
                            "右侧礼盒正面独立堆列",
                            "最右侧额外包装独立堆列",
                        ],
                        "stack_1sqm_basis": None,
                        "description": "三列正面商品之外还有一列具有独立边界的包装堆列。",
                        "limitations": [],
                    },
                },
            ],
            "extraction_notes": ["只复核陈列标准"],
        }

        _validate_display_standard_review(requested, focused)
        self.assertIsNone(
            focused["display_reviews"][0]["display_observation"]["stack_1sqm_basis"]
        )
        self.assertIn(
            "照片未提供尺寸或比例依据",
            focused["display_reviews"][0]["display_observation"]["limitations"],
        )
        photo_result = {"photo_reviews": requested, "extraction_notes": []}
        _apply_display_standard_review(photo_result, focused)
        self.assertEqual(
            photo_result["photo_reviews"][0]["display_observation"]["vertical_facing_count"],
            3,
        )
        self.assertEqual(
            photo_result["photo_reviews"][1]["display_observation"]["vertical_facing_count"],
            4,
        )
        self.assertEqual(
            photo_result["photo_reviews"][0]["recognized_products"], ["保留商品"]
        )
        self.assertIn("独立陈列标准聚焦复核", photo_result["extraction_notes"][-1])

        wrong_route = {
            **focused,
            "display_reviews": [
                {
                    **focused["display_reviews"][0],
                    "photo_files": ["错误照片.jpg"],
                },
                focused["display_reviews"][1],
            ],
        }
        with self.assertRaisesRegex(AuditError, "保持合同门店和照片绑定"):
            _validate_display_standard_review(requested, wrong_route)

    def test_display_calibration_requires_exact_store_and_ordered_photo_hashes(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            photo = root / "现场.jpg"
            photo.write_bytes(b"accepted display photo")
            digest = hashlib.sha256(photo.read_bytes()).hexdigest()
            accepted = {
                "standard_evidence": "meets",
                "matched_standard": "four_vertical",
                "vertical_facing_count": 4,
                "vertical_facing_basis": ["列1", "列2", "列3", "列4"],
                "stack_1sqm_basis": None,
                "description": "用户验收的四纵观察",
                "limitations": [],
            }
            registry = root / "calibrations.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "calibrations": [
                            {
                                "calibration_id": "accepted-example",
                                "contract_store_name": "验收门店",
                                "photo_sha256": [digest],
                                "display_observation": accepted,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def result() -> dict:
                return {
                    "photo_reviews": [
                        {
                            "store_line_no": 1,
                            "contract_store_name": "验收门店",
                            "photo_files": [photo.name],
                            "recognized_products": ["保留商品"],
                            "display_observation": {
                                "standard_evidence": "unclear",
                                "matched_standard": "unclear",
                                "vertical_facing_count": 3,
                                "vertical_facing_basis": ["旧1", "旧2", "旧3"],
                                "stack_1sqm_basis": None,
                                "description": "模型原观察",
                                "limitations": [],
                            },
                        }
                    ],
                    "extraction_notes": [],
                }

            matched = result()
            _apply_display_standard_calibrations(matched, [photo], registry)
            self.assertEqual(
                matched["photo_reviews"][0]["display_observation"], accepted
            )
            self.assertEqual(
                matched["photo_reviews"][0]["recognized_products"], ["保留商品"]
            )
            self.assertIn("accepted-example", matched["extraction_notes"][-1])

            photo.write_bytes(b"changed display photo")
            changed = result()
            _apply_display_standard_calibrations(changed, [photo], registry)
            self.assertEqual(
                changed["photo_reviews"][0]["display_observation"]["vertical_facing_count"],
                3,
            )
            self.assertEqual(changed["extraction_notes"], [])


if __name__ == "__main__":
    unittest.main()
