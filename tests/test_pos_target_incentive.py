from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from openpyxl import Workbook

from audit_core.archive_input import discover_archives, prepare_cases
from audit_core.codex_runner import _validate_pos_target_incentive_sources
from audit_core.common import validate_json
from audit_core.html_report import create_html_report_from_workbook, verify_html_report
from audit_core.pos_target_incentive import audit_pos_target_incentive_case
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
        "recipient_type": "not_applicable",
        "channel_name": None,
        "channel_eligibility_visible": "not_applicable",
        "activity_start": None,
        "activity_end": None,
        "fee_type": None,
        "promotion_mechanic": None,
        "pos_scope": None,
        "target_tiers": [],
        "amount_ceiling": None,
        "pos_sales_amount": None,
        "calculated_amount": None,
        "claimed_amount": None,
        "company_template_visible": "not_applicable",
        "dealer_seal_visible": "not_applicable",
        "signed_visible": "not_applicable",
        "pos_rows": [],
        "pos_total_sales_amount": None,
        "activity_date": None,
        "activity_time": None,
        "activity_location": None,
        "watermark_date_visible": "not_applicable",
        "watermark_time_visible": "not_applicable",
        "watermark_address_visible": "not_applicable",
        "receipt_number": None,
        "receipt_amount": None,
        "activity_exists_visible": "not_applicable",
        "visible_summary": "可见资料",
        "limitations": [],
    }
    value.update(overrides)
    return value


def _make_pos_workbook(path: Path) -> tuple[list[dict[str, object]], float]:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "20家门店销售明细"
    sheet.append(["日期", "系统门店名称", "销售金额"])
    rows: list[dict[str, object]] = []
    amounts = [10000.00] * 19 + [112574.14]
    for index, amount in enumerate(amounts, 1):
        store = f"测试门店{index:02d}"
        sheet.append(["1.1-1.20", store, amount])
        rows.append({"period_text": "1.1-1.20", "store_name": store, "sales_amount": amount})
    sheet.append(["合计", None, 302574.14])
    workbook.save(path)
    workbook.close()
    return rows, 302574.14


def _evidence(rows: list[dict[str, object]], total: float) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "scenario": "pos_target_incentive",
        "documents": [
            _document(
                "1月20家数据.jpg",
                "stamped_pos_data",
                dealer_name="陕西和丰日盛贸易有限公司",
                recipient_type="dealer",
                dealer_seal_visible="visible",
                pos_rows=rows,
                pos_total_sales_amount=total,
                visible_summary="20家门店销售金额合计302574.14元，经销商盖章",
            ),
            _document(
                "1月结算单 华润.jpg",
                "settlement",
                title="市场费用结算单",
                party_names=["陕西和丰日盛贸易有限公司", "厦门参半商贸有限公司"],
                dealer_name="陕西和丰日盛贸易有限公司",
                recipient_type="dealer",
                channel_name="华润万家",
                activity_start="2026-01-01",
                activity_end="2026-01-20",
                fee_type="POS达标激励",
                promotion_mechanic="品牌月活动，客户区域推广宣传",
                target_tiers=[
                    {"threshold_amount": 200000, "rate": 0.08},
                    {"threshold_amount": 300000, "rate": 0.10},
                ],
                amount_ceiling=30000,
                pos_sales_amount=302574.14,
                calculated_amount=30257.41,
                claimed_amount=30000,
                company_template_visible="visible",
                dealer_seal_visible="visible",
                visible_summary="302574.14×10%=30257.41，按上限核销30000元",
            ),
        ],
        "extraction_notes": [],
    }


class PosTargetIncentiveTests(unittest.TestCase):
    def test_route_and_prepare_incomplete_marked_package(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            archive = root / "SQ-POS激励达标.zip"
            with zipfile.ZipFile(archive, "w") as bundle:
                bundle.writestr("1月20家数据.jpg", b"image")
                bundle.writestr("1月结算单.jpg", b"image")
                bundle.writestr("POS数据.xlsx", b"workbook")
            self.assertEqual(set(discover_archives(root)), {"pos_target_incentive"})
            case = prepare_cases(root, root / "prepared", {"pos_target_incentive"})["pos_target_incentive"]
            self.assertIsNone(case["promotional_contract"])
            self.assertEqual(len(case["activity_evidence_files"]), 0)
            self.assertEqual([item["role"] for item in case["document_roles"]], ["settlement", "stamped_pos_data"])

    def test_real_formula_shape_reconciles_but_missing_contract_and_activity_hold_claim(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            workbook = root / "POS数据.xlsx"
            rows, total = _make_pos_workbook(workbook)
            evidence = _evidence(rows, total)
            case = {
                "scenario": "pos_target_incentive",
                "source_archive": ROOT / "input" / "SQ202512310105-HX202602050027-POS激励达标(1).zip",
                "pos_spreadsheet": workbook,
            }
            result = audit_pos_target_incentive_case(case, evidence)
            validate_json(result, ROOT / "contracts" / "audit-result.schema.json")
            self.assertEqual(result["summary"]["claimed_amount"], 30000)
            self.assertEqual(result["summary"]["suggested_approved_amount"], 0)
            self.assertEqual(result["summary"]["temporarily_held_amount"], 30000)
            controls = {item["control_id"]: item["status"] for item in result["pos_target_incentive_audit"]["controls"]}
            self.assertEqual(controls["pos_spreadsheet"], "pass")
            self.assertEqual(controls["pos_correspondence"], "pass")
            self.assertEqual(controls["contract_authority"], "fail")
            self.assertEqual(controls["activity_existence"], "fail")

    def test_source_coverage_and_error_only_html(self) -> None:
        with tempfile.TemporaryDirectory() as temp_value:
            root = Path(temp_value)
            workbook_source = root / "POS数据.xlsx"
            rows, total = _make_pos_workbook(workbook_source)
            evidence = _evidence(rows, total)
            case = {
                "scenario": "pos_target_incentive",
                "source_archive": ROOT / "input" / "SQ-POS激励达标.zip",
                "pos_spreadsheet": workbook_source,
                "document_roles": [
                    {"path": Path(str(item["source_file"])), "role": item["role"]}
                    for item in evidence["documents"]
                ],
            }
            _validate_pos_target_incentive_sources(case, evidence)
            result = audit_pos_target_incentive_case(case, evidence)
            report = create_combined_report([result], root / "audit.xlsx")
            self.assertEqual(verify_workbook(report, ["pos_target_incentive"])["sheet_names"], ["POS达标激励核销"])
            html = create_html_report_from_workbook(report, root / "audit.html")
            check = verify_html_report(html, ["pos_target_incentive"], workbook_path=report)
            self.assertEqual(check["template_version"], "3.9.0")
            text = html.read_text(encoding="utf-8")
            self.assertIn("促销合同未建立POS达标激励权威规则", text)
            self.assertIn("满减活动缺少有效存在证明", text)


if __name__ == "__main__":
    unittest.main()
