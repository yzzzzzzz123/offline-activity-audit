from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook
from PIL import Image

from audit_core.archive_input import discover_archives, prepare_cases
from audit_core.codex_runner import _validate_self_procured_gift_material_sources
from audit_core.common import validate_json
from audit_core.html_report import create_html_report_from_workbook, verify_html_report
from audit_core.report import create_combined_report, verify_workbook
from audit_core.self_procured_gift_material import audit_self_procured_gift_material_case


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCHEMA = (
    ROOT
    / "skills"
    / "audit-self-procured-gift-material"
    / "references"
    / "evidence.schema.json"
)


def _document(source: str, role: str, document_type: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_file": source,
        "role": role,
        "document_type": document_type,
        "title": None,
        "party_names": [],
        "dealer_name": None,
        "activity_start": None,
        "activity_end": None,
        "contract_signing_date": None,
        "store_count": None,
        "store_names": [],
        "activity_budget": None,
        "qualifying_product_or_set": None,
        "qualifying_purchase_amount": None,
        "gift_material_name": None,
        "gift_material_code": None,
        "buy_quantity": None,
        "gift_quantity": None,
        "limited_quantity_rule": None,
        "material_quantity": None,
        "material_unit_price": None,
        "document_amount": None,
        "calculation_method": None,
        "company_template_visible": "not_applicable",
        "customer_seal_visible": "not_applicable",
        "signed_visible": "not_applicable",
        "receipt_number": None,
        "document_date": None,
        "seller_or_payee": None,
        "receipt_detail_visible": "not_applicable",
        "payer_name": None,
        "payee_name": None,
        "payment_amount": None,
        "payment_time": None,
        "transaction_number": None,
        "pos_rows": [],
        "pos_total_quantity": None,
        "pos_total_sales_amount": None,
        "photo_date": None,
        "photo_time": None,
        "photo_location": None,
        "photo_store_name": None,
        "watermark_date_visible": "not_applicable",
        "watermark_time_visible": "not_applicable",
        "watermark_address_visible": "not_applicable",
        "promotion_content_visible": "not_applicable",
        "self_procured_material_visible": "not_applicable",
        "photo_material_name": None,
        "qualifying_product_visible": "not_applicable",
        "gift_rule_visible": "not_applicable",
        "visible_summary": "可见资料",
        "limitations": [],
    }
    value.update(overrides)
    return value


def _evidence(photo_name: str) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "scenario": "self_procured_gift_material",
        "documents": [
            _document(
                "促销协议.jpg",
                "signed_promotional_contract",
                "signed_promotional_contract",
                title="经销商签订促销合同",
                party_names=["常州都市女孩商贸有限公司"],
                dealer_name="常州都市女孩商贸有限公司",
                activity_start="2026-01-01",
                activity_end="2026-01-15",
                contract_signing_date="2026-01-29",
                store_count=1,
                store_names=["尚美仓春晓店"],
                activity_budget=42000,
                qualifying_product_or_set="49.9元产品套装",
                qualifying_purchase_amount=49.9,
                gift_material_name="塑料凳子",
                buy_quantity=1,
                gift_quantity=1,
                material_quantity=4200,
                material_unit_price=10,
                document_amount=42000,
                calculation_method="4200个凳子×10元=42000元",
                signed_visible="visible",
            ),
            _document(
                "结算单.jpg",
                "settlement",
                "settlement",
                dealer_name="常州都市女孩商贸有限公司",
                activity_start="2026-01-01",
                activity_end="2026-01-15",
                qualifying_product_or_set="49.9元产品套装",
                gift_material_name="凳子",
                gift_material_code="GD-05-7773",
                buy_quantity=1,
                gift_quantity=1,
                material_quantity=4200,
                material_unit_price=10,
                document_amount=42000,
                calculation_method="4200×10=42000",
                company_template_visible="visible",
                customer_seal_visible="visible",
            ),
            _document(
                "物料购买凭证.jpg",
                "purchase_invoice_or_receipt",
                "purchase_invoice_or_receipt",
                title="收据",
                gift_material_name="塑料凳子",
                material_quantity=4200,
                material_unit_price=10,
                document_amount=42000,
                receipt_number="1452942",
                document_date="2025-12-26",
                seller_or_payee="样例日用品商行",
                receipt_detail_visible="visible",
                signed_visible="visible",
            ),
            _document(
                "物料购买付款记录.jpg",
                "purchase_payment_record",
                "purchase_payment_record",
                payer_name="常州都市女孩商贸有限公司",
                payee_name="样例收款人",
                payment_amount=42000,
                payment_time="2025-12-24 10:00:00",
                transaction_number="TX-001",
            ),
            _document(
                "销售pos.jpg",
                "stamped_pos_data",
                "stamped_pos_data",
                customer_seal_visible="visible",
                pos_rows=[
                    {
                        "period_text": "2026-01-01至2026-01-15",
                        "store_name": "尚美仓春晓店",
                        "sales_quantity": 4200,
                        "sales_amount": 209580,
                    }
                ],
                pos_total_quantity=4200,
                pos_total_sales_amount=209580,
            ),
            _document(
                photo_name,
                "activity_photo",
                "activity_photo",
                photo_date="2026-01-08",
                photo_time="14:30:00",
                photo_location="江苏省常州市尚美仓春晓店",
                photo_store_name="尚美仓春晓店",
                watermark_date_visible="visible",
                watermark_time_visible="visible",
                watermark_address_visible="visible",
                promotion_content_visible="visible",
                self_procured_material_visible="visible",
                photo_material_name="塑料凳子",
                qualifying_product_visible="visible",
                gift_rule_visible="visible",
            ),
        ],
        "extraction_notes": [],
    }


def _write_pos(path: Path) -> None:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "POS汇总"
    sheet.append(["日期", "门店名称", "销售数量", "销售金额"])
    sheet.append(["2026-01-01至2026-01-15", "尚美仓春晓店", 4200, 209580])
    sheet.append(["合计", "合计", 4200, 209580])
    workbook.save(path)
    workbook.close()


class SelfProcuredGiftMaterialTests(unittest.TestCase):
    def test_route_and_prepare_legacy_activity_photo_hints(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            archive = root / "SQ-自采赠品物料.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("促销协议.jpg", b"contract")
                bundle.writestr("结算单.jpg", b"settlement")
                bundle.writestr("物料购买凭证.jpg", b"receipt")
                bundle.writestr("销售pos.jpg", b"pos")
                bundle.writestr("活动返图.xls", b"legacy")
            self.assertEqual(set(discover_archives(root)), {"self_procured_gift_material"})

            def fake_activity(_: Path, target: Path) -> dict[str, object]:
                target.mkdir(parents=True)
                photo = target / "activity-row-0002.jpg"
                Image.new("RGB", (4, 4), "white").save(photo)
                return {
                    "source_file": "活动返图.xls",
                    "source_sha256": "0" * 64,
                    "sheet": "活动返图",
                    "records": [
                        {
                            "excel_row": 2,
                            "customer_code": "C001",
                            "store_name": "尚美仓春晓店",
                            "period_text": "2026-01-01至2026-01-15",
                            "photo_file": str(photo.resolve()),
                        }
                    ],
                }

            with patch(
                "audit_core.archive_input.extract_activity_return_workbook",
                fake_activity,
            ):
                case = prepare_cases(
                    root,
                    root / "prepared",
                    {"self_procured_gift_material"},
                )["self_procured_gift_material"]
            self.assertIsNone(case["pos_spreadsheet"])
            photo_role = next(
                item for item in case["document_roles"] if item["role"] == "activity_photo"
            )
            self.assertEqual(photo_role["store_hint"], "尚美仓春晓店")
            self.assertEqual(photo_role["period_hint"], "2026-01-01至2026-01-15")
            self.assertEqual(photo_role["activity_excel_row"], 2)

    def test_complete_chain_supports_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            photo = root / "activity-row-0002.jpg"
            Image.new("RGB", (4, 4), "white").save(photo)
            pos = root / "销售POS电子表.xlsx"
            _write_pos(pos)
            evidence = _evidence(photo.name)
            validate_json(evidence, EVIDENCE_SCHEMA)
            case = {
                "scenario": "self_procured_gift_material",
                "source_archive": root / "SQ-自采赠品物料.zip",
                "activity_return_workbook": root / "活动返图.xls",
                "activity_return": {
                    "source_file": "活动返图.xls",
                    "source_sha256": "0" * 64,
                    "sheet": "活动返图",
                    "records": [
                        {
                            "excel_row": 2,
                            "store_name": "尚美仓春晓店",
                            "period_text": "2026-01-01至2026-01-15",
                            "customer_code": "C001",
                            "photo_file": str(photo),
                        }
                    ],
                },
                "pos_spreadsheet": pos,
                "document_roles": [
                    {"path": root / str(item["source_file"]), "role": item["role"]}
                    for item in evidence["documents"]
                ],
            }
            _validate_self_procured_gift_material_sources(case, evidence)
            result = audit_self_procured_gift_material_case(case, evidence)
            validate_json(result, ROOT / "contracts" / "audit-result.schema.json")
            self.assertEqual(result["summary"]["claimed_amount"], 42000)
            self.assertEqual(result["summary"]["suggested_approved_amount"], 42000)
            self.assertEqual(result["summary"]["temporarily_held_amount"], 0)

    def test_missing_pos_spreadsheet_holds_claim_and_renders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            photo = root / "activity-row-0002.jpg"
            Image.new("RGB", (4, 4), "white").save(photo)
            evidence = _evidence(photo.name)
            case = {
                "scenario": "self_procured_gift_material",
                "source_archive": root / "SQ-自采赠品物料.zip",
                "activity_return_workbook": root / "活动返图.xls",
                "activity_return": {
                    "source_file": "活动返图.xls",
                    "source_sha256": "0" * 64,
                    "sheet": "活动返图",
                    "records": [
                        {
                            "excel_row": 2,
                            "store_name": "尚美仓春晓店",
                            "period_text": "2026-01-01至2026-01-15",
                            "customer_code": "C001",
                            "photo_file": str(photo),
                        }
                    ],
                },
                "pos_spreadsheet": None,
                "document_roles": [
                    {"path": root / str(item["source_file"]), "role": item["role"]}
                    for item in evidence["documents"]
                ],
            }
            result = audit_self_procured_gift_material_case(case, evidence)
            self.assertEqual(result["summary"]["claimed_amount"], 42000)
            self.assertEqual(result["summary"]["suggested_approved_amount"], 0)
            self.assertEqual(result["summary"]["temporarily_held_amount"], 42000)
            codes = {item["code"] for item in result["self_procured_gift_material_audit"]["issues"]}
            self.assertIn("required_materials_missing", codes)
            self.assertIn("pos_correspondence_failed", codes)
            report = create_combined_report([result], root / "audit.xlsx")
            self.assertEqual(
                verify_workbook(report, ["self_procured_gift_material"])["sheet_names"],
                ["自采赠品物料核销"],
            )
            html = create_html_report_from_workbook(report, root / "audit.html")
            check = verify_html_report(
                html,
                ["self_procured_gift_material"],
                workbook_path=report,
            )
            self.assertEqual(check["template_version"], "3.9.0")
            self.assertIn("活动返图.xls不能替代POS电子表", html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
