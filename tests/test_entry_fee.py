from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image

from audit_core.archive_input import discover_archives, prepare_cases
from audit_core.codex_runner import _validate_entry_fee_sources
from audit_core.common import validate_json
from audit_core.entry_fee import audit_entry_fee_case
from audit_core.html_report import create_html_report_from_workbook, verify_html_report
from audit_core.report import create_combined_report, verify_workbook


ROOT = Path(__file__).resolve().parents[1]


def _document(source: str, role: str, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "source_file": source,
        "role": role,
        "document_type": role,
        "title": None,
        "party_names": [],
        "party_a": None,
        "party_b": None,
        "agreement_date": None,
        "agreement_year": None,
        "terminal_system": None,
        "terminal_type": None,
        "signed_visible": "not_applicable",
        "party_a_seal_visible": "not_applicable",
        "party_b_seal_visible": "not_applicable",
        "contract_items": [],
        "contract_stores": [],
        "contract_total_amount": None,
        "tax_included_visible": "not_applicable",
        "payment_method": None,
        "per_order_deduction_rate_cap": None,
        "actual_shelving_only_visible": "not_applicable",
        "shelf_photos_required_visible": "not_applicable",
        "system_deduction_proof_required_visible": "not_applicable",
        "new_store_cost_dealer_visible": "not_applicable",
        "photo_date": None,
        "photo_time": None,
        "photo_location": None,
        "photo_store_name": None,
        "watermark_date_visible": "not_applicable",
        "watermark_time_visible": "not_applicable",
        "watermark_address_visible": "not_applicable",
        "shelf_display_visible": "not_applicable",
        "visible_products": [],
        "deduction_date": None,
        "deduction_subject": None,
        "deduction_amount": None,
        "deduction_proof_visible": "not_applicable",
        "visible_summary": "可见资料",
        "limitations": [],
    }
    value.update(overrides)
    return value


def _evidence(*, include_proof: bool = True) -> dict[str, object]:
    documents = [
        _document(
            "产品推广协议.pdf",
            "entry_fee_contract",
            title="产品推广协议",
            party_names=["深圳星阔生物科技有限公司", "连云港蕙风商贸有限公司"],
            party_a="深圳星阔生物科技有限公司",
            party_b="连云港蕙风商贸有限公司",
            agreement_date="2026-07-13",
            agreement_year=2026,
            terminal_system="家得福系统",
            terminal_type="KA",
            signed_visible="visible",
            party_a_seal_visible="visible",
            party_b_seal_visible="visible",
            contract_items=[
                {
                    "line_no": 1,
                    "product_code": "CP-XH-XFS-0012",
                    "product_name": "净澈去屑洗发水500ml",
                    "contracted_store_count": 1,
                    "barcode_fee": 8500,
                }
            ],
            contract_stores=[
                {
                    "line_no": 1,
                    "store_name": "春晓店",
                    "terminal_system": "家得福系统",
                    "store_type": "KA",
                    "full_address": "江苏省连云港市海州区春晓店",
                }
            ],
            contract_total_amount=8500,
            tax_included_visible="visible",
            payment_method="乙方提交核销材料后以货款抵扣，每张订单抵扣不超过50%",
            per_order_deduction_rate_cap=0.5,
            actual_shelving_only_visible="visible",
            shelf_photos_required_visible="visible",
            system_deduction_proof_required_visible="visible",
            new_store_cost_dealer_visible="visible",
            visible_summary="1个条码、1家门店、条码费8500元，双方签章",
        ),
        _document(
            "春晓上架.jpg",
            "shelf_photo",
            photo_date="2026-07-22",
            photo_time="15:30:00",
            photo_location="江苏省连云港市海州区春晓店",
            photo_store_name="春晓店",
            watermark_date_visible="visible",
            watermark_time_visible="visible",
            watermark_address_visible="visible",
            shelf_display_visible="visible",
            visible_products=[
                {
                    "product_code": "CP-XH-XFS-0012",
                    "product_name": "净澈去屑洗发水500ml",
                    "visible_package_text": "净澈去屑 500ml",
                }
            ],
            visible_summary="春晓店水印货架照片，合同商品清楚",
        ),
    ]
    if include_proof:
        documents.append(
            _document(
                "系统扣款凭证.jpg",
                "system_deduction_proof",
                deduction_date="2026-07-28",
                deduction_subject="家得福系统产品条码上架费",
                deduction_amount=8500,
                deduction_proof_visible="visible",
                visible_summary="系统扣款8500元",
            )
        )
    return {
        "schema_version": "1.0",
        "scenario": "entry_fee",
        "documents": documents,
        "extraction_notes": [],
    }


class EntryFeeTests(unittest.TestCase):
    def test_route_and_prepare_rar_photos_with_store_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            archive = root / "SQ-进场费.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("连云港蕙风商贸有限公司产品推广协议.pdf", b"pdf")
                bundle.writestr("家得福进场照片.rar", b"rar")
            self.assertEqual(set(discover_archives(root)), {"entry_fee"})

            def fake_rar(_: Path, target: Path) -> dict[str, object]:
                store = target / "春晓"
                store.mkdir(parents=True)
                image = store / "photo.jpg"
                Image.new("RGB", (2, 2), "white").save(image)
                return {
                    "archive_sha256": "0" * 64,
                    "root": target.resolve(),
                    "files": [image.resolve()],
                }

            with patch("audit_core.archive_input._extract_rar_archive", fake_rar):
                case = prepare_cases(root, root / "prepared", {"entry_fee"})["entry_fee"]
            self.assertEqual(len(case["shelf_photo_files"]), 1)
            photo_role = next(item for item in case["document_roles"] if item["role"] == "shelf_photo")
            self.assertEqual(photo_role["store_hint"], "春晓")
            self.assertEqual(photo_role["relative_path"], "春晓/photo.jpg")

    def test_contract_barcode_fee_is_not_multiplied_by_store_count(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            photo = root / "春晓上架.jpg"
            Image.new("RGB", (4, 4), "white").save(photo)
            evidence = _evidence(include_proof=True)
            case = {
                "scenario": "entry_fee",
                "source_archive": root / "SQ-进场费.zip",
                "document_roles": [
                    {"path": root / "产品推广协议.pdf", "role": "entry_fee_contract"},
                    {"path": photo, "role": "shelf_photo"},
                    {"path": root / "系统扣款凭证.jpg", "role": "system_deduction_proof"},
                ],
            }
            result = audit_entry_fee_case(case, evidence)
            validate_json(result, ROOT / "contracts" / "audit-result.schema.json")
            self.assertEqual(result["summary"]["claimed_amount"], 8500)
            self.assertEqual(result["summary"]["suggested_approved_amount"], 8500)
            self.assertEqual(result["summary"]["temporarily_held_amount"], 0)
            self.assertIn("不重复乘入条码费", result["entry_fee_audit"]["controls"][2]["basis"])

    def test_missing_system_deduction_proof_holds_full_claim_and_renders(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            photo = root / "春晓上架.jpg"
            Image.new("RGB", (4, 4), "white").save(photo)
            evidence = _evidence(include_proof=False)
            case = {
                "scenario": "entry_fee",
                "source_archive": root / "SQ-进场费.zip",
                "document_roles": [
                    {"path": root / "产品推广协议.pdf", "role": "entry_fee_contract"},
                    {"path": photo, "role": "shelf_photo"},
                ],
            }
            _validate_entry_fee_sources(case, evidence)
            result = audit_entry_fee_case(case, evidence)
            self.assertEqual(result["summary"]["claimed_amount"], 8500)
            self.assertEqual(result["summary"]["suggested_approved_amount"], 0)
            self.assertEqual(result["summary"]["temporarily_held_amount"], 8500)
            codes = {item["code"] for item in result["entry_fee_audit"]["issues"]}
            self.assertIn("system_deduction_proof_missing", codes)
            report = create_combined_report([result], root / "audit.xlsx")
            self.assertEqual(verify_workbook(report, ["entry_fee"])["sheet_names"], ["进场费核销"])
            html = create_html_report_from_workbook(report, root / "audit.html")
            check = verify_html_report(html, ["entry_fee"], workbook_path=report)
            self.assertEqual(check["template_version"], "3.9.0")
            text = html.read_text(encoding="utf-8")
            self.assertIn("缺少合同要求的系统扣款凭证", text)


if __name__ == "__main__":
    unittest.main()
