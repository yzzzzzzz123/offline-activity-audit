from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from audit_core.common import AuditError
from audit_core.display import _display_control
from audit_core.orchestrator import (
    next_output_path,
    normalize_producer_model,
    output_date_from_run_id,
)
from audit_core.report import create_combined_report, verify_workbook


def _personnel_result() -> dict:
    return {
        "scenario": "personnel_incentive",
        "summary": {
            "calculated_line_reward": 30,
            "settlement_line_reward": 30,
            "transfer_total": 30,
            "excel_mapped_quantity": 10,
            "settlement_line_quantity": 10,
            "claimed_amount": 30,
            "store_count": 1,
        },
        "sales": {"source_file": "人员销售.xlsx"},
        "settlement": {
            "source_file": "人员结算单.jpg",
            "activity_start": "2026-08-01",
            "activity_end": "2026-08-31",
        },
        "transfer_evidence": [
            {
                "source_files": ["转账截图.jpg"],
                "identity_visible": False,
                "recipient_name": None,
                "store_name": None,
                "event_date_visible": False,
                "transfer_date": None,
            }
        ],
        "sku_reconciliation": [
            {
                "line_no": 1,
                "settlement_product_name": "示例商品",
                "mapped_barcode": "6900000000000",
                "excel_quantity": 10,
                "calculated_settlement_reward": 30,
                "settlement_quantity": 10,
                "settlement_reward_amount": 30,
                "quantity_difference": 0,
                "settlement_line_amount_difference": 0,
                "mapping_status": "matched",
                "mapping_confidence": "medium",
            }
        ],
    }


def _display_result() -> dict:
    return {
        "scenario": "promotional_display",
        "summary": {
            "contract_store_count": 1,
            "fee_per_store": 1000,
            "claimed_amount": 1000,
            "sales_sku_count": 1,
            "sales_quantity": 20,
            "sales_retail_amount": 398,
            "photo_count": 1,
            "passed_store_count": 1,
            "supplement_store_count": 0,
            "suggested_approved_amount": 1000,
            "temporarily_held_amount": 0,
        },
        "contract": {
            "source_file": "堆头合同.pdf",
            "display_standard": "1平米堆头或4纵陈列",
        },
        "sales": {
            "source_file": "堆头销售.xlsx",
            "customers": ["示例客户"],
            "period_values": ["2026-08"],
            "store_level_available": False,
        },
        "store_reconciliation": [
            {
                "store_line_no": 1,
                "contract_store_name": "示例门店",
                "photo_files": ["示例门店.jpg"],
                "visible_date": "2026-08-18",
                "visible_location": "示例门店",
                "period_match": "match",
                "store_match": "exact",
                "display_match": "pass",
                "display_standard_basis": "four_vertical",
                "display_description": "画面中可清楚数出4列纵向陈列。",
                "display_vertical_facing_count": 4,
                "display_vertical_facing_basis": [
                    "左一绿色产品列",
                    "左二红色产品列",
                    "右一白紫色产品列",
                    "右二窄白色产品列",
                ],
                "display_stack_1sqm_basis": None,
                "duplicate_check": "none",
                "recognized_products": ["示例产品"],
                "product_reference_hits": [],
                "promotion_summary": "未识别到明确促销词或组合装",
                "sales_product_names": ["示例商品原名"],
                "sales_product_match": "candidate",
                "supported_amount": 1000,
                "status": "pass",
                "supplement_advice": [],
            }
        ],
    }


class ProducerFilenameTests(unittest.TestCase):
    def test_codex_and_repeat_use_date_model_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = next_output_path("20260818-simple-007", "codex", root)
            self.assertEqual(first.name, "20260818-codex.xlsx")
            first.touch()
            second = next_output_path("20260818-simple-007", "codex", root)
            self.assertEqual(second.name, "20260818-codex-1.1.xlsx")
            second.touch()
            third = next_output_path("20260818-simple-007", "codex", root)
            self.assertEqual(third.name, "20260818-codex-1.2.xlsx")

    def test_each_model_has_an_independent_sequence(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "20260818-codex.xlsx").touch()
            self.assertEqual(
                next_output_path("20260818-simple-008", "qwen3.7", root).name,
                "20260818-qwen3.7.xlsx",
            )

    def test_run_id_must_start_with_a_valid_business_date(self) -> None:
        self.assertEqual(output_date_from_run_id("20260818-simple-007"), "20260818")
        for value in ("simple-007", "20261301-simple-007", "20260230"):
            with self.subTest(value=value), self.assertRaises(AuditError):
                output_date_from_run_id(value)

    def test_other_model_label_is_normalized(self) -> None:
        self.assertEqual(normalize_producer_model(" QWEN3.7 "), "qwen3.7")

    def test_unsafe_model_label_is_rejected(self) -> None:
        for value in ("../qwen", "qwen/3.7", "", "."):
            with self.subTest(value=value), self.assertRaises(AuditError):
                normalize_producer_model(value)


class DisplayContractTests(unittest.TestCase):
    def test_display_control_requires_consistent_standard(self) -> None:
        self.assertEqual(
            _display_control(
                {"standard_evidence": "meets", "matched_standard": "four_vertical"}
            ),
            ("pass", "four_vertical"),
        )
        with self.assertRaises(AuditError):
            _display_control(
                {"standard_evidence": "meets", "matched_standard": "unclear"}
            )

    def test_workbook_separates_display_standard_and_photo_reuse(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "result.xlsx"
            create_combined_report([_personnel_result(), _display_result()], target)
            verification = verify_workbook(
                target, ["personnel_incentive", "promotional_display"]
            )
            self.assertEqual(verification["formula_count"], 0)

            workbook = load_workbook(target, data_only=False)
            try:
                self.assertEqual(str(workbook["人员激励核销"].freeze_panes), "A4")
                self.assertEqual(str(workbook["堆头核销"].freeze_panes), "A4")
                personnel_sheet = workbook["人员激励核销"]
                for column in ("C", "D", "F"):
                    self.assertGreaterEqual(
                        float(personnel_sheet.column_dimensions[column].width or 0),
                        28,
                    )
                self.assertGreaterEqual(float(personnel_sheet.row_dimensions[5].height or 0), 72)
                self.assertGreaterEqual(float(personnel_sheet.row_dimensions[7].height or 0), 72)
                photo_text = str(workbook["堆头核销"]["C4"].value)
                comparison_text = str(workbook["堆头核销"]["D4"].value)
                self.assertIn("陈列标准核验：符合（达到4纵陈列）", photo_text)
                self.assertIn("视觉依据：画面中可清楚数出4列纵向陈列。", photo_text)
                self.assertIn("可见纵列数：4", photo_text)
                self.assertIn(
                    "逐列依据（左到右）：左一绿色产品列；左二红色产品列；"
                    "右一白紫色产品列；右二窄白色产品列",
                    photo_text,
                )
                self.assertIn("1平米依据：未形成可验证的1平米依据", photo_text)
                self.assertIn("照片复用检查：未发现跨门店照片复用", photo_text)
                self.assertIn("陈列标准：符合（达到4纵陈列）", comparison_text)
                self.assertIn("照片复用：未发现跨门店照片复用", comparison_text)
                self.assertNotIn("陈列重复", photo_text + comparison_text)
            finally:
                workbook.close()


if __name__ == "__main__":
    unittest.main()
