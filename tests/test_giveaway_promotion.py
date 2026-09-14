from __future__ import annotations

import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

from audit_core.archive_input import discover_archives, prepare_cases
from audit_core.codex_runner import _validate_giveaway_promotion_sources
from audit_core.common import validate_json
from audit_core.giveaway_promotion import audit_giveaway_promotion_case
from audit_core.html_report import create_html_report_from_workbook, verify_html_report
from audit_core.report import create_combined_report, verify_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCHEMA = (
    PROJECT_ROOT
    / "skills"
    / "audit-giveaway-promotion"
    / "references"
    / "evidence.schema.json"
)
RESULT_SCHEMA = PROJECT_ROOT / "contracts" / "audit-result.schema.json"


def _line(
    line_no: int,
    line_type: str,
    product_name: str,
    *,
    quantity: float | None = None,
    unit_price: float | None = None,
    amount: float | None = None,
    product_code: str | None = None,
    gift_ratio_text: str | None = None,
) -> dict:
    return {
        "line_no": line_no,
        "line_type": line_type,
        "product_code": product_code,
        "barcode_69": None,
        "product_name": product_name,
        "quantity": quantity,
        "unit": "盒" if quantity is not None else None,
        "unit_price": unit_price,
        "amount": amount,
        "line_date": None,
        "gift_ratio_text": gift_ratio_text,
    }


def _document(source_file: str, document_type: str, **overrides) -> dict:
    value = {
        "source_file": source_file,
        "role": "visual_document",
        "document_type": document_type,
        "title": None,
        "party_names": [],
        "dealer_name": None,
        "store_name": None,
        "document_date": None,
        "activity_start": None,
        "activity_end": None,
        "fee_type": None,
        "signed_visible": "not_applicable",
        "dealer_seal_visible": "not_applicable",
        "company_template_visible": "not_applicable",
        "contract_budget": None,
        "shipment_amount": None,
        "claimed_gift_amount": None,
        "total_gift_quantity": None,
        "gift_unit_price": None,
        "calculation_text": None,
        "gift_ratio_text": None,
        "product_lines": [],
        "receipt_number": None,
        "receipt_total_paid": None,
        "activity_date": None,
        "activity_location": None,
        "activity_content": None,
        "promotion_visible": "not_applicable",
        "visible_summary": "可见业务材料",
        "limitations": [],
    }
    value.update(overrides)
    return value


def _complete_evidence() -> dict:
    return {
        "schema_version": "1.0",
        "scenario": "giveaway_promotion",
        "documents": [
            _document(
                "合同.jpg",
                "signed_promotional_contract",
                title="额外搭赠促销合同",
                party_names=["经销商甲"],
                dealer_name="经销商甲",
                activity_start="2026-01-01",
                activity_end="2026-01-31",
                fee_type="额外搭赠",
                signed_visible="visible",
                contract_budget=100,
                total_gift_quantity=10,
                gift_unit_price=10,
                calculation_text="赠品10盒×10元=100元",
                gift_ratio_text="1:1",
                product_lines=[
                    _line(1, "eligible_sale", "清新牙膏", quantity=10, gift_ratio_text="1:1"),
                    _line(2, "gift", "谷净白牙膏180g", quantity=10, unit_price=10, amount=100),
                ],
                visible_summary="经销商签章额外搭赠合同，约定买一赠一和100元预算",
            ),
            _document(
                "结算.jpg",
                "settlement",
                title="市场费用结算单",
                party_names=["经销商甲"],
                dealer_name="经销商甲",
                activity_start="2026-01-01",
                activity_end="2026-01-31",
                fee_type="额外搭赠",
                dealer_seal_visible="visible",
                company_template_visible="visible",
                shipment_amount=1000,
                claimed_gift_amount=100,
                total_gift_quantity=10,
                gift_unit_price=10,
                calculation_text="10盒×10元=100元",
                product_lines=[
                    _line(1, "gift", "谷净白牙膏180g", quantity=10, unit_price=10, amount=100),
                ],
                visible_summary="经销商盖章结算，正常出货1000元，搭赠100元",
            ),
            _document(
                "系统出货.jpg",
                "sales_delivery_statement",
                title="系统销售出货明细",
                party_names=["经销商甲", "门店一"],
                dealer_name="经销商甲",
                store_name="门店一",
                document_date="2026-01-15",
                dealer_seal_visible="visible",
                shipment_amount=1000,
                product_lines=[
                    _line(1, "shipment", "参半清新牙膏", quantity=10, amount=1000),
                    _line(2, "gift", "参半谷净白牙膏180g", quantity=10, amount=0),
                ],
                visible_summary="系统出货合计1000元",
            ),
            _document(
                "小票.jpg",
                "store_receipt",
                title="门店销售小票",
                store_name="门店一",
                document_date="2026-01-15",
                receipt_number="R001",
                receipt_total_paid=100,
                product_lines=[
                    _line(1, "eligible_sale", "清新牙膏", quantity=1, amount=100),
                    _line(2, "gift", "谷净白牙膏180g", quantity=1, amount=0),
                ],
                visible_summary="购买一盒并零金额赠送一盒",
            ),
            _document(
                "活动照片.jpg",
                "activity_photo",
                activity_date="2026-01-15",
                activity_location="门店一",
                activity_content="清新牙膏买一赠一活动",
                promotion_visible="visible",
                visible_summary="门店现场可见买一赠一活动",
            ),
        ],
        "extraction_notes": [],
    }


def _case(root: Path, evidence: dict) -> dict:
    files = []
    for index, document in enumerate(evidence["documents"], start=1):
        path = root / document["source_file"]
        path.write_bytes(f"unique-giveaway-source-{index}".encode("utf-8"))
        files.append(path)
    return {
        "scenario": "giveaway_promotion",
        "source_archive": root / "额外搭赠.zip",
        "visual_files": files,
        "excluded_files": [],
    }


class GiveawayPromotionRoutingTests(unittest.TestCase):
    def test_marked_generic_image_package_routes_and_binds_neutral_roles(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "申请-额外搭赠.zip"
            with zipfile.ZipFile(source, "w") as archive:
                for index in range(1, 7):
                    archive.writestr(f"mmexport{index}.jpg", f"image-{index}".encode())
            classified = discover_archives(root)
            self.assertEqual(set(classified), {"giveaway_promotion"})
            case = prepare_cases(root, root / "prepared", {"giveaway_promotion"})[
                "giveaway_promotion"
            ]
            self.assertEqual(len(case["document_roles"]), 6)
            self.assertEqual(
                {item["role"] for item in case["document_roles"]},
                {"visual_document"},
            )

    def test_source_validator_requires_coverage_but_preserves_authority_candidates(self) -> None:
        case = {
            "document_roles": [
                {"path": "合同.jpg", "role": "visual_document"},
                {"path": "结算.jpg", "role": "visual_document"},
            ]
        }
        evidence = {
            "documents": [
                {"source_file": "合同.jpg", "role": "visual_document", "document_type": "signed_promotional_contract"},
                {"source_file": "虚构.jpg", "role": "visual_document", "document_type": "settlement"},
            ]
        }
        with self.assertRaisesRegex(Exception, "完整覆盖"):
            _validate_giveaway_promotion_sources(case, evidence)

        duplicated = deepcopy(evidence)
        duplicated["documents"] = [
            {"source_file": "合同.jpg", "role": "visual_document", "document_type": "settlement"},
            {"source_file": "结算.jpg", "role": "visual_document", "document_type": "settlement"},
        ]
        _validate_giveaway_promotion_sources(case, duplicated)

        repeated_source = deepcopy(duplicated)
        repeated_source["documents"][1]["source_file"] = "合同.jpg"
        with self.assertRaisesRegex(Exception, "重复返回同一来源"):
            _validate_giveaway_promotion_sources(case, repeated_source)

        rebound = deepcopy(duplicated)
        rebound["documents"][0]["role"] = "settlement"
        with self.assertRaisesRegex(Exception, "来源角色被改写"):
            _validate_giveaway_promotion_sources(case, rebound)


class GiveawayPromotionAuditTests(unittest.TestCase):
    def test_multiple_principal_candidates_report_files_without_selecting_or_approving(self) -> None:
        for document_type, extra_file in (
            ("signed_promotional_contract", "客户另交合同.jpg"),
            ("settlement", "客户另交结算.jpg"),
            ("sales_delivery_statement", "客户另交系统明细.jpg"),
        ):
            with self.subTest(document_type=document_type):
                evidence = _complete_evidence()
                original = next(
                    document for document in evidence["documents"]
                    if document["document_type"] == document_type
                )
                additional = deepcopy(original)
                additional["source_file"] = extra_file
                if document_type == "settlement":
                    additional["claimed_gift_amount"] = 999
                evidence["documents"].append(additional)
                validate_json(evidence, EVIDENCE_SCHEMA)
                with tempfile.TemporaryDirectory() as temporary:
                    case = _case(Path(temporary), evidence)
                    case["document_roles"] = [
                        {"path": path, "role": "visual_document"}
                        for path in case["visual_files"]
                    ]
                    _validate_giveaway_promotion_sources(case, evidence)
                    result = audit_giveaway_promotion_case(case, evidence)
                validate_json(result, RESULT_SCHEMA)
                audit = result["giveaway_promotion_audit"]
                self.assertEqual(audit["missing_roles"], [])
                self.assertEqual(result["summary"]["missing_role_count"], 0)
                self.assertEqual(result["summary"]["suggested_approved_amount"], 0)
                self.assertEqual(result["summary"]["conclusion"], "human_review")
                self.assertEqual(len(audit["documents"]), 6)
                self.assertEqual(len(audit["issues"]), 1)
                issue = audit["issues"][0]
                self.assertEqual(issue["code"], "multiple_role_candidates")
                self.assertEqual(issue["source_files"], [original["source_file"], extra_file])
                self.assertIn("2份", issue["observed"])
                self.assertIn(original["source_file"], issue["observed"])
                self.assertIn(extra_file, issue["observed"])
                self.assertIn("未选择", issue["impact"])
                material_control = next(
                    control for control in audit["controls"]
                    if control["control_id"] == "required_materials"
                )
                self.assertEqual(material_control["status"], "fail")
                self.assertNotIn("缺少", material_control["basis"])
                if document_type == "settlement":
                    self.assertEqual(result["summary"]["claimed_amount"], 0)
                    self.assertIsNone(result["summary"]["shipment_amount"])
                if document_type == "signed_promotional_contract":
                    self.assertIsNone(result["summary"]["recalculated_gift_amount"])

    def test_duplicate_contract_does_not_hide_actual_missing_photo(self) -> None:
        evidence = _complete_evidence()
        extra_contract = deepcopy(evidence["documents"][0])
        extra_contract["source_file"] = "另一合同.jpg"
        evidence["documents"] = [
            document for document in evidence["documents"]
            if document["document_type"] != "activity_photo"
        ] + [extra_contract]
        with tempfile.TemporaryDirectory() as temporary:
            result = audit_giveaway_promotion_case(_case(Path(temporary), evidence), evidence)
        validate_json(result, RESULT_SCHEMA)
        audit = result["giveaway_promotion_audit"]
        self.assertEqual(audit["missing_roles"], ["活动期内门店现场照片"])
        self.assertEqual(
            {issue["code"] for issue in audit["issues"]},
            {"multiple_role_candidates", "required_materials_missing", "activity_execution_missing"},
        )
        self.assertEqual(result["summary"]["suggested_approved_amount"], 0)

    def test_complete_chain_recalculates_and_passes(self) -> None:
        evidence = _complete_evidence()
        validate_json(evidence, EVIDENCE_SCHEMA)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = audit_giveaway_promotion_case(_case(root, evidence), evidence)
        validate_json(result, RESULT_SCHEMA)
        self.assertEqual(result["summary"]["decision_label"], "可核销")
        self.assertEqual(result["summary"]["shipment_amount"], 1000)
        self.assertEqual(result["summary"]["claimed_amount"], 100)
        self.assertEqual(result["summary"]["recalculated_gift_amount"], 100)
        self.assertEqual(result["summary"]["suggested_approved_amount"], 100)
        self.assertEqual(result["giveaway_promotion_audit"]["issues"], [])

    def test_missing_activity_photo_blocks_full_claim_and_renders_error_only(self) -> None:
        evidence = _complete_evidence()
        evidence["documents"] = [
            document
            for document in evidence["documents"]
            if document["document_type"] != "activity_photo"
        ]
        validate_json(evidence, EVIDENCE_SCHEMA)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = audit_giveaway_promotion_case(_case(root, evidence), evidence)
            validate_json(result, RESULT_SCHEMA)
            codes = {
                issue["code"]
                for issue in result["giveaway_promotion_audit"]["issues"]
            }
            self.assertIn("required_materials_missing", codes)
            self.assertIn("activity_execution_missing", codes)
            self.assertEqual(result["summary"]["suggested_approved_amount"], 0)
            self.assertEqual(result["summary"]["temporarily_held_amount"], 100)

            workbook = root / "report.xlsx"
            html = root / "report.html"
            create_combined_report([result], workbook)
            workbook_check = verify_workbook(workbook, ["giveaway_promotion"])
            self.assertEqual(workbook_check["sheet_names"], ["额外搭赠核销"])
            create_html_report_from_workbook(workbook, html)
            html_check = verify_html_report(
                html,
                ["giveaway_promotion"],
                workbook_path=workbook,
            )
            self.assertEqual(html_check["template_version"], "3.9.0")
            html_text = html.read_text(encoding="utf-8")
            self.assertIn("giveaway_promotion", html_text)
            self.assertIn("额外搭赠", html_text)


if __name__ == "__main__":
    unittest.main()
