from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from audit_core.archive_input import discover_archives, prepare_cases
from audit_core.codex_runner import _validate_price_difference_support_sources
from audit_core.common import validate_json
from audit_core.html_report import create_html_report_from_workbook, verify_html_report
from audit_core.price_difference_support import audit_price_difference_support_case
from audit_core.report import create_combined_report, verify_workbook


ROOT = Path(__file__).resolve().parents[1]


def _document(source: str, role: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_file": source,
        "role": role,
        "document_type": role,
        "title": None,
        "party_names": [],
        "dealer_name": None,
        "activity_start": None,
        "activity_end": None,
        "fee_type": None,
        "store_count": None,
        "store_names": [],
        "product_code": None,
        "barcode": None,
        "product_name": None,
        "original_price": None,
        "activity_price": None,
        "support_unit_amount": None,
        "planned_quantity": None,
        "amount_ceiling": None,
        "calculation_method": None,
        "settlement_quantity": None,
        "claimed_amount": None,
        "company_template_visible": "not_applicable",
        "dealer_seal_visible": "not_applicable",
        "signed_visible": "not_applicable",
        "pos_lines": [],
        "pos_total_quantity": None,
        "pos_total_sales_amount": None,
        "photo_date": None,
        "photo_time": None,
        "photo_location": None,
        "watermark_date_visible": "not_applicable",
        "watermark_time_visible": "not_applicable",
        "watermark_address_visible": "not_applicable",
        "photo_activity_price": None,
        "activity_price_visible": "not_applicable",
        "visible_summary": "可见资料",
        "limitations": [],
    }
    value.update(overrides)
    return value


def _evidence() -> dict[str, object]:
    stores = [f"测试门店{i:02d}" for i in range(1, 62)]
    documents: list[dict[str, object]] = [
        _document(
            "促销协议.jpg",
            "signed_promotional_contract",
            title="经销商签订促销合同（补差）",
            party_names=["徐州芃之誉商贸有限公司"],
            dealer_name="徐州芃之誉商贸有限公司",
            activity_start="2025-12-04",
            activity_end="2025-12-31",
            fee_type="价格补差",
            store_count=61,
            product_name="小紫管",
            original_price=39.9,
            activity_price=29.9,
            support_unit_amount=3,
            planned_quantity=3660,
            amount_ceiling=10980,
            calculation_method="3元×3660支=10980元",
            signed_visible="visible",
            visible_summary="促销价格差额补偿，合同补差3元每支",
        ),
        _document(
            "结算单.jpg",
            "settlement",
            title="市场费用结算单",
            party_names=["徐州芃之誉商贸有限公司"],
            dealer_name="徐州芃之誉商贸有限公司",
            activity_start="2025-12-04",
            activity_end="2025-12-31",
            fee_type="价格补差",
            store_count=61,
            support_unit_amount=3,
            settlement_quantity=3660,
            claimed_amount=10980,
            calculation_method="3元×3660支=10980元",
            company_template_visible="visible",
            dealer_seal_visible="visible",
            visible_summary="价格补差结算",
        ),
        _document(
            "陈列销售.jpg",
            "stamped_pos_data",
            party_names=["徐州芃之誉商贸有限公司"],
            dealer_name="徐州芃之誉商贸有限公司",
            dealer_seal_visible="visible",
            pos_lines=[
                {
                    "store_name": store,
                    "product_code": None,
                    "barcode": "6970356162636",
                    "product_name": "参半专研清新美白牙膏100g",
                    "quantity": 60 if index <= 60 else 234,
                    "unit_price": 29.9,
                    "sales_amount": (60 if index <= 60 else 234) * 29.9,
                }
                for index, store in enumerate(stores, 1)
            ],
            pos_total_quantity=3834,
            pos_total_sales_amount=114636.6,
            visible_summary="61家门店盖章销售数据",
        ),
    ]
    documents.extend(
        _document(
            f"现场照片{i:02d}.jpg",
            "activity_photo",
            photo_date="2025-12-29",
            photo_time="16:22:00",
            photo_location=stores[i - 1],
            watermark_date_visible="visible",
            watermark_time_visible="visible",
            watermark_address_visible="visible",
            photo_activity_price=29.9,
            activity_price_visible="visible",
            visible_summary="门店现场价签29.9元",
        )
        for i in range(1, 16)
    )
    return {
        "schema_version": "1.0",
        "scenario": "price_difference_support",
        "documents": documents,
        "extraction_notes": [],
    }


class PriceDifferenceSupportTests(unittest.TestCase):
    def test_route_and_prepare_roles_with_safe_rar_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            archive = root / "SQ-补差.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                for name in ("促销协议.jpg", "结算单.jpg", "陈列销售.jpg"):
                    bundle.writestr(name, b"image")
                bundle.writestr("活动照片.rar", b"rar")
            self.assertEqual(set(discover_archives(root)), {"price_difference_support"})

            def fake_rar(_: Path, target: Path) -> dict[str, object]:
                target.mkdir(parents=True)
                image = target / "现场照片.jpg"
                Image.new("RGB", (2, 2), "white").save(image)
                return {
                    "archive_sha256": "0" * 64,
                    "root": target.resolve(),
                    "files": [image.resolve()],
                }

            with patch("audit_core.archive_input._extract_rar_archive", fake_rar):
                case = prepare_cases(
                    root,
                    root / "prepared",
                    {"price_difference_support"},
                )["price_difference_support"]
            roles = [item["role"] for item in case["document_roles"]]
            self.assertEqual(roles.count("signed_promotional_contract"), 1)
            self.assertEqual(roles.count("settlement"), 1)
            self.assertEqual(roles.count("stamped_pos_data"), 1)
            self.assertEqual(roles.count("activity_photo"), 1)
            self.assertIsNone(case["pos_spreadsheet"])

    def test_missing_excel_and_partial_photo_coverage_hold_full_claim(self) -> None:
        evidence = _evidence()
        case = {
            "scenario": "price_difference_support",
            "source_archive": ROOT / "input" / "SQ202512270003-HX202601160053-补差.zip",
            "pos_spreadsheet": None,
        }
        result = audit_price_difference_support_case(case, evidence)
        validate_json(result, ROOT / "contracts" / "audit-result.schema.json")
        self.assertEqual(result["summary"]["claimed_amount"], 10980)
        self.assertEqual(result["summary"]["suggested_approved_amount"], 0)
        self.assertEqual(result["summary"]["temporarily_held_amount"], 10980)
        codes = {
            item["code"]
            for item in result["price_difference_support_audit"]["issues"]
        }
        self.assertIn("pos_spreadsheet_invalid", codes)
        self.assertIn("activity_photo_coverage_failed", codes)
        photo_issue = next(
            item
            for item in result["price_difference_support_audit"]["issues"]
            if item["code"] == "activity_photo_coverage_failed"
        )
        self.assertIn("应覆盖61家", photo_issue["observed"])
        self.assertIn("唯一匹配15家", photo_issue["observed"])

    def test_source_manifest_is_exact_and_report_is_error_only(self) -> None:
        evidence = _evidence()
        case = {
            "scenario": "price_difference_support",
            "source_archive": ROOT / "input" / "SQ202512270003-HX202601160053-补差.zip",
            "pos_spreadsheet": None,
            "document_roles": [
                {"path": Path(str(item["source_file"])), "role": item["role"]}
                for item in evidence["documents"]
            ],
        }
        _validate_price_difference_support_sources(case, evidence)
        result = audit_price_difference_support_case(case, evidence)
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            workbook = create_combined_report([result], root / "audit.xlsx")
            self.assertEqual(verify_workbook(workbook, ["price_difference_support"])["sheet_names"], ["价格补差核销"])
            html = create_html_report_from_workbook(workbook, root / "audit.html")
            verification = verify_html_report(
                html,
                ["price_difference_support"],
                workbook_path=workbook,
            )
            self.assertEqual(verification["template_version"], "3.7.0")
            text = html.read_text(encoding="utf-8")
            self.assertIn("价格补差", text)
            self.assertIn("POS数据电子表缺失或不可核验", text)


if __name__ == "__main__":
    unittest.main()
