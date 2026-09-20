from __future__ import annotations

import tempfile
import unittest
import zipfile
from copy import deepcopy
from pathlib import Path

from openpyxl import Workbook

from audit_core.archive_input import ArchiveInputError, discover_archives, prepare_cases
from audit_core.codex_runner import _validate_maintenance_fee_sources
from audit_core.common import validate_json
from audit_core.html_report import create_html_report_from_workbook, verify_html_report
from audit_core.maintenance_fee import audit_maintenance_fee_case
from audit_core.report import create_combined_report, verify_workbook


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_SCHEMA = (
    PROJECT_ROOT
    / "skills/create-offline-audit-scenario/references"
    / "legacy"
    / "maintenance_fee"
    / "references"
    / "evidence.schema.json"
)
RESULT_SCHEMA = PROJECT_ROOT / "skills/orchestrate-offline-audit/references/contracts" / "audit-result.schema.json"


def _document(source: str, role: str, **overrides):
    value = {
        "source_file": source,
        "role": role,
        "document_type": "图片",
        "title": None,
        "party_names": [],
        "customer_name": None,
        "activity_start": None,
        "activity_end": None,
        "fee_type": None,
        "expense_lines": [],
        "eligible_pos_scope": None,
        "calculation_method": None,
        "rate": None,
        "amount_ceiling": None,
        "sales_quantity": None,
        "sales_amount": None,
        "claimed_amount": None,
        "company_template_visible": "not_applicable",
        "dealer_seal_visible": "not_visible",
        "signed_visible": "not_applicable",
        "pos_lines": [],
        "pos_total_quantity": None,
        "pos_total_sales_amount": None,
        "activity_date": None,
        "activity_location": None,
        "activity_content": None,
        "visible_summary": "可见资料摘要",
        "limitations": [],
    }
    value.update(overrides)
    return value


def _write_pos_workbook(path: Path) -> None:
    workbook = Workbook()
    worksheet = workbook.active
    worksheet.title = "POS明细"
    worksheet.append(["日期", "门店名称", "货品名称", "货品数量", "含税售额"])
    worksheet.append(["2026-01-01", "经销商甲", "商品甲", 10, 1000])
    worksheet.append(["2026-01-01", "经销商甲", "商品乙", 5, 500])
    worksheet.append(["合计", "", "", 15, 1500])
    workbook.save(path)
    workbook.close()


def _complete_evidence() -> dict:
    return {
        "schema_version": "1.0",
        "scenario": "maintenance_fee",
        "documents": [
            _document(
                "盖章POS.jpg",
                "stamped_pos_data",
                title="POS销售数据",
                customer_name="经销商甲",
                activity_start="2026-01-01",
                activity_end="2026-01-31",
                dealer_seal_visible="visible",
                pos_lines=[
                    {"product_name": "商品甲", "quantity": 10, "sales_amount": 1000},
                    {"product_name": "商品乙", "quantity": 5, "sales_amount": 500},
                ],
                pos_total_quantity=15,
                pos_total_sales_amount=1500,
                visible_summary="经销商盖章POS，合计15件、1500元",
            ),
            _document(
                "促销合同.jpg",
                "signed_promotional_contract",
                title="维护费用促销合同",
                party_names=["品牌公司", "经销商甲"],
                customer_name="经销商甲",
                activity_start="2026-01-01",
                activity_end="2026-01-31",
                fee_type="渠道维护费用",
                expense_lines=[
                    {
                        "description": "渠道维护费用",
                        "calculation_basis": "全部POS销售额的10%",
                        "quantity": None,
                        "unit": None,
                        "unit_price": None,
                        "rate": 0.1,
                        "amount": None,
                    }
                ],
                eligible_pos_scope="全部POS销售",
                calculation_method="全部POS销售额乘以10%",
                rate=0.1,
                signed_visible="visible",
                visible_summary="双方签章维护费用合同，约定全部POS销售额10%",
            ),
            _document(
                "结算单.jpg",
                "settlement",
                title="维护费用结算单",
                party_names=["经销商甲"],
                customer_name="经销商甲",
                activity_start="2026-01-01",
                activity_end="2026-01-31",
                fee_type="维护费用",
                expense_lines=[
                    {
                        "description": "维护费用",
                        "calculation_basis": "POS销售额1500元×10%",
                        "quantity": None,
                        "unit": None,
                        "unit_price": None,
                        "rate": 0.1,
                        "amount": 150,
                    }
                ],
                eligible_pos_scope="全部POS销售",
                calculation_method="POS销售额乘以10%",
                rate=0.1,
                sales_quantity=15,
                sales_amount=1500,
                claimed_amount=150,
                company_template_visible="visible",
                dealer_seal_visible="visible",
                signed_visible="not_applicable",
                visible_summary="统一模板结算单，经销商盖章，申报150元",
            ),
            _document(
                "维护协议.jpg",
                "supporting_document",
                title="渠道维护服务协议",
                party_names=["品牌公司", "经销商甲"],
                customer_name="经销商甲",
                activity_start="2026-01-01",
                activity_end="2026-01-31",
                fee_type="渠道维护费用",
                signed_visible="visible",
                visible_summary="维护服务协议",
            ),
        ],
        "extraction_notes": [],
    }


class MaintenanceFeeRoutingTests(unittest.TestCase):
    def test_incomplete_marked_package_routes_to_maintenance_without_changing_other(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            maintenance = root / "申请-维护费用.zip"
            with zipfile.ZipFile(maintenance, "w") as archive:
                archive.writestr("维护费用/门店POS.jpg", b"pos")
                archive.writestr("维护费用/服务费结算单.jpg", b"settlement")
            classified = discover_archives(root)
            self.assertEqual(set(classified), {"maintenance_fee"})

            extracted = root / "prepared"
            case = prepare_cases(root, extracted, {"maintenance_fee"})["maintenance_fee"]
            self.assertIsNone(case["pos_spreadsheet"])
            self.assertIsNone(case["promotional_contract"])
            self.assertEqual(Path(case["settlement_document"]).name, "服务费结算单.jpg")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            other = root / "申请-其他.zip"
            with zipfile.ZipFile(other, "w") as archive:
                archive.writestr("促销合同.jpg", b"contract")
                archive.writestr("结算单.jpg", b"settlement")
                archive.writestr("维护协议.jpg", b"support")
                archive.writestr("活动照片.jpg", b"photo")
            self.assertEqual(set(discover_archives(root)), {"other_expense"})

    def test_duplicate_pos_spreadsheets_fail_unique_binding(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "维护费用.zip"
            with zipfile.ZipFile(source, "w") as archive:
                archive.writestr("POS.jpg", b"pos")
                archive.writestr("结算单.jpg", b"settlement")
                archive.writestr("POS一.xlsx", b"one")
                archive.writestr("POS二.xlsx", b"two")
            with self.assertRaisesRegex(ArchiveInputError, "POS电子表应最多1份"):
                prepare_cases(root, root / "prepared", {"maintenance_fee"})


class MaintenanceFeeAuditTests(unittest.TestCase):
    def test_complete_chain_recalculates_and_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workbook = root / "POS数据.xlsx"
            _write_pos_workbook(workbook)
            case = {
                "scenario": "maintenance_fee",
                "source_archive": root / "维护费用.zip",
                "pos_spreadsheet": workbook,
                "excluded_files": [],
            }
            evidence = _complete_evidence()
            validate_json(evidence, EVIDENCE_SCHEMA)

            result = audit_maintenance_fee_case(case, evidence)

            validate_json(result, RESULT_SCHEMA)
            self.assertEqual(result["summary"]["decision_label"], "可核销")
            self.assertEqual(result["summary"]["claimed_amount"], 150)
            self.assertEqual(result["summary"]["recalculated_amount"], 150)
            self.assertEqual(result["summary"]["suggested_approved_amount"], 150)
            self.assertEqual(result["maintenance_fee_audit"]["issues"], [])

    def test_incomplete_sample_reports_missing_roles_and_fee_conflict(self) -> None:
        evidence = {
            "schema_version": "1.0",
            "scenario": "maintenance_fee",
            "documents": [
                _document(
                    "门店POS.jpg",
                    "stamped_pos_data",
                    customer_name="经销商甲",
                    dealer_seal_visible="visible",
                    pos_lines=[
                        {"product_name": "商品甲", "quantity": 10, "sales_amount": 1000}
                    ],
                    pos_total_quantity=10,
                    pos_total_sales_amount=1000,
                    visible_summary="盖章POS数据",
                ),
                _document(
                    "服务费结算单.jpg",
                    "settlement",
                    title="人员激励结算单",
                    party_names=["经销商甲"],
                    customer_name="经销商甲",
                    activity_start="2026-01-01",
                    activity_end="2026-01-31",
                    fee_type="人员激励费用",
                    expense_lines=[
                        {
                            "description": "人员激励费用",
                            "calculation_basis": "销售额的15%",
                            "quantity": None,
                            "unit": None,
                            "unit_price": None,
                            "rate": 0.15,
                            "amount": 48,
                        }
                    ],
                    calculation_method="销售额乘以15%",
                    rate=0.15,
                    sales_amount=1000,
                    claimed_amount=48,
                    company_template_visible="unclear",
                    dealer_seal_visible="visible",
                    visible_summary="人员激励结算，申报48元",
                ),
            ],
            "extraction_notes": [],
        }
        validate_json(evidence, EVIDENCE_SCHEMA)
        result = audit_maintenance_fee_case(
            {
                "scenario": "maintenance_fee",
                "source_archive": "维护费用.zip",
                "pos_spreadsheet": None,
                "excluded_files": [],
            },
            evidence,
        )
        validate_json(result, RESULT_SCHEMA)
        codes = {item["code"] for item in result["maintenance_fee_audit"]["issues"]}
        self.assertIn("required_materials_missing", codes)
        self.assertIn("fee_nature_conflict", codes)
        self.assertIn("pos_spreadsheet_invalid", codes)
        self.assertIn("promotional_contract_invalid", codes)
        self.assertIn("amount_recalculation_failed", codes)
        self.assertEqual(result["summary"]["suggested_approved_amount"], 0)
        self.assertEqual(result["summary"]["temporarily_held_amount"], 48)

    def test_source_coverage_rejects_invented_or_rebound_files(self) -> None:
        case = {
            "document_roles": [
                {"path": Path("POS.jpg"), "role": "stamped_pos_data"},
                {"path": Path("结算单.jpg"), "role": "settlement"},
            ]
        }
        evidence = {
            "documents": [
                {"source_file": "POS.jpg", "role": "stamped_pos_data"},
                {"source_file": "虚构.jpg", "role": "settlement"},
            ]
        }
        with self.assertRaisesRegex(Exception, "完整覆盖"):
            _validate_maintenance_fee_sources(case, evidence)

        rebound = deepcopy(evidence)
        rebound["documents"][1] = {
            "source_file": "结算单.jpg",
            "role": "supporting_document",
        }
        with self.assertRaisesRegex(Exception, "角色被改写"):
            _validate_maintenance_fee_sources(case, rebound)

    def test_result_renders_in_fifth_error_only_interface(self) -> None:
        evidence = {
            "schema_version": "1.0",
            "scenario": "maintenance_fee",
            "documents": [
                _document(
                    "结算单.jpg",
                    "settlement",
                    title="维护费用结算单",
                    fee_type="维护费用",
                    claimed_amount=100,
                    visible_summary="维护费用申报100元",
                )
            ],
            "extraction_notes": [],
        }
        result = audit_maintenance_fee_case(
            {
                "scenario": "maintenance_fee",
                "source_archive": "维护费用.zip",
                "pos_spreadsheet": None,
                "excluded_files": [],
            },
            evidence,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workbook = root / "report.xlsx"
            html = root / "report.html"
            create_combined_report([result], workbook)
            workbook_check = verify_workbook(workbook, ["maintenance_fee"])
            self.assertEqual(workbook_check["sheet_names"], ["维护费用核销"])
            create_html_report_from_workbook(workbook, html)
            html_check = verify_html_report(
                html,
                ["maintenance_fee"],
                workbook_path=workbook,
            )
            self.assertEqual(html_check["template_version"], "3.9.0")
            self.assertGreater(html_check["record_counts"]["维护费用核销"], 0)
            self.assertIn("maintenance_fee", html.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
