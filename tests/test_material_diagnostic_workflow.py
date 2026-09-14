from __future__ import annotations

import hashlib
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from PIL import Image

from audit_core.archive_input import ArchiveInputError
from audit_core.common import AuditError
from audit_core.oss_intake import OSSIntakeCallbackError, OSSIntakeConfig, OSSIntakeService
from audit_core.workbench_html import render_static_run_archive_html
from audit_core.workbench_runtime import ROOT_HTML, _input_archive_names, run_persistent_audit
from audit_core.workbench_server import WorkbenchCatalog


ARCHIVE_NAME = "HX202601160053-广州南雄维护费用申请-9.zip"
OSS_HOST = "audit-materials.oss-cn-hangzhou.aliyuncs.com"


def _image_bytes(color: str) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (12, 12), color).save(output, format="PNG")
    return output.getvalue()


def _archive_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


def _maintenance_files() -> dict[str, bytes]:
    return {
        "结算单.jpg": _image_bytes("white"),
        "服务费结算单.jpg": _image_bytes("blue"),
        "POS数据.jpg": _image_bytes("red"),
    }


class MaterialDiagnosticWorkflowTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.worktrees = self.root / "worktrees"
        self.provider_calls: list[dict] = []
        self.temporary_sources: list[Path] = []

    def bundle(self, files: dict[str, bytes], name: str = ARCHIVE_NAME) -> Path:
        path = self.inputs / name
        path.write_bytes(_archive_bytes(files))
        return path

    def provider(self, case: dict, temporary: Path) -> dict:
        self.assertEqual(case["kind"], "material_diagnostic")
        self.provider_calls.append(case)
        self.temporary_sources.append(temporary)
        archives = []
        for archive in case["archives"]:
            documents = []
            for item in archive["inventory"]:
                self.assertTrue(Path(item["path"]).is_file())
                if item["kind"] != "visual":
                    continue
                source = item["source_file"]
                role = "stamped_pos_data" if "POS" in source else "settlement"
                documents.append({
                    "file_id": item["file_id"], "document_type": role,
                    "confidence": "high", "title": "POS销售数据" if role == "stamped_pos_data" else "维护费用结算单",
                    "document_number": item["file_id"], "page_number": 1,
                    "total_pages": 1, "visible_facts": ["维护费用材料"],
                    "limitations": [],
                })
            archives.append({
                "archive_id": archive["archive_id"],
                "scenario_candidates": [{"scenario": "maintenance_fee", "confidence": "high", "basis": "提交材料可见维护费用标题"}],
                "documents": documents,
            })
        return {"schema_version": "1.0", "archives": archives}

    def run_audit(self, provider=None, *, scenario: str | None = None) -> dict:
        return run_persistent_audit(
            "20260910-material-workflow", producer_model="codex",
            input_dir=self.inputs, worktrees_root=self.worktrees,
            evidence_provider=provider or self.provider,
            scenario=scenario,
        )

    def conflicting_scenario_provider(self, case: dict, temporary: Path) -> dict:
        evidence = self.provider(case, temporary)
        for archive in evidence["archives"]:
            archive["scenario_candidates"] = [
                {"scenario": "price_difference_support", "confidence": "high", "basis": "文件可见补差字样"},
                {"scenario": "personnel_incentive", "confidence": "high", "basis": "文件可见人员激励字样"},
            ]
        return evidence

    def business_provider(self, case: dict, temporary: Path) -> dict:
        if case.get("kind") == "material_diagnostic":
            return self.provider(case, temporary)
        self.assertEqual(case["scenario"], "maintenance_fee")
        documents = []
        for item in case["document_roles"]:
            documents.append({
                "source_file": Path(item["path"]).name, "role": item["role"],
                "document_type": "图片", "title": None, "party_names": [],
                "customer_name": None, "activity_start": None, "activity_end": None,
                "fee_type": None, "expense_lines": [], "eligible_pos_scope": None,
                "calculation_method": None, "rate": None, "amount_ceiling": None,
                "sales_quantity": None, "sales_amount": None, "claimed_amount": None,
                "company_template_visible": "not_applicable", "dealer_seal_visible": "not_visible",
                "signed_visible": "not_applicable", "pos_lines": [], "pos_total_quantity": None,
                "pos_total_sales_amount": None, "activity_date": None, "activity_location": None,
                "activity_content": None, "visible_summary": "可见POS销售数据", "limitations": [],
            })
        return {"schema_version": "1.0", "scenario": "maintenance_fee", "documents": documents, "extraction_notes": []}

    def assert_completed_diagnostic(self, receipt: dict) -> tuple[dict, str]:
        workspace = Path(receipt["worktree"])
        self.assertEqual(receipt["status"], "completed")
        self.assertGreater(receipt["error_count"], 0)
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
        result = json.loads((workspace / "analysis/material-diagnostic/result.json").read_text(encoding="utf-8"))
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 0)
        self.assertTrue(result["diagnostic_only"])
        self.assertEqual(result["summary"], {"conclusion": "human_review"})
        self.assertNotIn("approved_amount", json.dumps(result))
        self.assertNotIn("material_diagnostic", receipt["scenarios"])
        self.assertTrue((workspace / "offline-activity-audit.html").is_file())
        self.assertFalse(list(workspace.rglob("*.xlsx")))
        self.assertFalse(list(workspace.rglob("*.jpg")))
        self.assertTrue(all(not path.exists() for path in self.temporary_sources))
        return result, Path(receipt["analysis_summary"]).read_text(encoding="utf-8")

    def test_two_settlements_and_missing_contract_continue_ai_and_publish_report(self) -> None:
        archive = self.bundle(_maintenance_files())
        original_zip = archive.read_bytes()
        original_html = ROOT_HTML.read_bytes()
        receipt = self.run_audit()
        result, summary = self.assert_completed_diagnostic(receipt)
        self.assertEqual(len(self.provider_calls), 1)
        issues = result["archives"][0]["issues"]
        self.assertTrue(any(item["code"] == "missing_material" and "合同" in item["title"] for item in issues))
        conflict = next(item for item in issues if item["code"] == "singleton_role_ambiguous")
        self.assertEqual(set(conflict["source_files"]), {"结算单.jpg", "服务费结算单.jpg"})
        self.assertFalse(any(item["code"] == "duplicate_submission" for item in issues))
        for text in ("已签促销合同", "POS", "结算单.jpg", "服务费结算单.jpg", "主件与补充件关系未明确"):
            self.assertIn(text, summary)
        self.assertEqual(archive.read_bytes(), original_zip)
        self.assertEqual(ROOT_HTML.read_bytes(), original_html)

    def test_zip_filename_keeps_maintenance_type_despite_conflicting_ai_candidates(self) -> None:
        self.bundle(_maintenance_files())
        receipt = self.run_audit(self.conflicting_scenario_provider)
        result, summary = self.assert_completed_diagnostic(receipt)
        self.assertEqual(receipt["scenarios"], ["maintenance_fee"])
        archive = result["archives"][0]
        self.assertEqual(archive["scenario_hint"], "maintenance_fee")
        self.assertEqual(archive["scenario"], "maintenance_fee")
        self.assertEqual(archive["scenario_label"], "维护费用")
        issues = archive["issues"]
        self.assertFalse(any(issue["code"] == "scenario_unconfirmed" for issue in issues))
        missing = [issue["title"] for issue in issues if issue["code"] == "missing_material"]
        self.assertTrue(any("已签促销合同" in title for title in missing))
        self.assertTrue(any("POS" in title and "电子表" in title for title in missing))
        self.assertTrue(any("费用专项支持材料" in title for title in missing))
        relationship = next(issue for issue in issues if issue["code"] == "singleton_role_ambiguous")
        self.assertEqual(set(relationship["source_files"]), {"结算单.jpg", "服务费结算单.jpg"})
        self.assertIn("维护费用", summary)
        self.assertIn("已签促销合同", summary)
        self.assertIn("结算单.jpg", summary)
        self.assertIn("服务费结算单.jpg", summary)
        for unselected in ("核销类型待确认", "价格补差", "人员激励", "付款或转账凭证"):
            self.assertNotIn(unselected, summary)

    def test_each_duplicate_group_survives_saved_summary(self) -> None:
        white, blue = _image_bytes("white"), _image_bytes("blue")
        names = {"甲/结算单.jpg": white, "乙/结算单.jpg": white,
                 "甲/服务费结算单.jpg": blue, "乙/服务费结算单.jpg": blue}
        self.bundle(names)
        result, summary = self.assert_completed_diagnostic(self.run_audit())
        duplicates = [item for item in result["archives"][0]["issues"] if item["code"] == "duplicate_submission"]
        self.assertEqual(len(duplicates), 2)
        for source in names:
            self.assertIn(source.rsplit("/", 1)[-1], summary)
        self.assertEqual(summary.count("以下 2 份文件内容完全相同"), 2)
        self.assertNotIn("甲/", summary)
        self.assertNotIn("乙/", summary)
        self.assertNotIn("另有", summary)

    def assert_classification_rejection(self, receipt: dict, *, pure: bool = True) -> tuple[dict, str]:
        workspace = Path(receipt["worktree"])
        self.assertEqual(receipt["status"], "failed")
        self.assertEqual(receipt["failure"]["code"], "classification_failed")
        result = json.loads((workspace / "analysis/classification-rejection/result.json").read_text(encoding="utf-8"))
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(snapshot["run"]["status"], "failed")
        events = [json.loads(line) for line in (workspace / "logs/events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertFalse(any(event["type"] == "run.completed" for event in events))
        self.assertEqual(events[-1]["type"], "run.failed")
        self.assertEqual(result["kind"], "classification_rejection")
        self.assertEqual(result["summary"]["conclusion"], "rejected")
        sheets = [sheet for sheet in snapshot["view"]["sheets"] if sheet.get("projection_kind") == "classification_rejection"]
        self.assertEqual(len(sheets), len(result["archives"]))
        for sheet in sheets:
            self.assertEqual(sheet["business_decision"], "rejected")
            self.assertEqual(len(sheet["rows"]), 1)
            self.assertEqual(sheet["rows"][0]["status"], "issue")
        for archive in result["archives"]:
            self.assertIsNone(archive["scenario"])
            self.assertIn(archive["reason"], receipt["failure"]["message"])
            for fabricated in ("documents", "scenario_candidates", "approved_amount", "supported_amount"):
                self.assertNotIn(fabricated, archive)
        self.assertFalse((workspace / "analysis/classification-rejection/evidence.json").exists())
        self.assertTrue((workspace / "offline-activity-audit.html").is_file())
        if pure:
            self.assertEqual(receipt["error_count"], len(result["archives"]))
            self.assertEqual(receipt["scenarios"], [])
            self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 0)
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertIsNone(manifest["analysis_started_at"])
            events = [json.loads(line) for line in (workspace / "logs/events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertFalse(any(event["type"] in {"scenario.started", "material_diagnostic.started", "evidence.validated", "material_diagnostic.evidence_validated"} for event in events))
            self.assertFalse((workspace / "analysis/material-diagnostic/result.json").exists())
            self.assertFalse(list((workspace / "analysis/results").glob("*.json")))
        summary = Path(receipt["analysis_summary"]).read_text(encoding="utf-8")
        self.assertIn("核销失败：核销方式无法确认", summary)
        return result, summary

    def no_ai_provider(self, case: dict, temporary: Path) -> dict:
        self.provider_calls.append(case)
        self.fail("无法唯一确认 ZIP 核销类型时不应调用 AI")

    def test_old_completed_classification_is_displayed_as_failed_without_rewriting_history(self) -> None:
        self.bundle(_maintenance_files(), "ai-pack-8.zip")
        receipt = self.run_audit(self.no_ai_provider)
        workspace = Path(receipt["worktree"])
        manifest_path = workspace / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest.update(status="completed", failure=None)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        before = {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()}
        catalog = WorkbenchCatalog(self.worktrees)
        self.assertEqual(catalog.list_runs()[0]["status"], "failed")
        snapshot = catalog.snapshot(workspace.name)
        self.assertEqual(snapshot["run"]["status"], "failed")
        self.assertIn("ai-pack-8.zip", snapshot["run"]["failure"]["message"])
        with self.assertRaisesRegex(ValueError, "完成"):
            catalog.set_manual_review(workspace.name, True)
        html = render_static_run_archive_html(workspace, ROOT_HTML)
        self.assertIn('name="offline-audit-view-available" content="false"', html)
        self.assertEqual(before, {path: path.read_bytes() for path in workspace.rglob("*") if path.is_file()})

    def test_unknown_zip_name_is_rejected_without_ai_or_missing_material_checklist(self) -> None:
        archive = self.bundle({"IMG_0001.jpg": _image_bytes("yellow")}, "ai-pack-8.zip")
        original = archive.read_bytes()
        receipt = self.run_audit(self.no_ai_provider)
        result, summary = self.assert_classification_rejection(receipt)
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(result["archives"][0]["reason_code"], "missing_scenario_marker")
        self.assertEqual(result["archives"][0]["matched_scenarios"], [])
        self.assertIn("ai-pack-8.zip", summary)
        for fabricated in ("IMG", "Excel", "POS", "已签促销合同", "已完成AI", "已完成 AI", "建议", "处理方式"):
            self.assertNotIn(fabricated, summary)
        self.assertEqual(archive.read_bytes(), original)

    def test_ambiguous_zip_name_is_rejected_without_ai_even_with_recognizable_materials(self) -> None:
        archive_name = "维护费用-进场费.zip"
        self.bundle(_maintenance_files(), archive_name)
        result, summary = self.assert_classification_rejection(self.run_audit(self.no_ai_provider))
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(result["archives"][0]["reason_code"], "ambiguous_scenario_markers")
        self.assertEqual(set(result["archives"][0]["matched_scenarios"]), {"maintenance_fee", "entry_fee"})
        self.assertIn(archive_name, summary)
        for fabricated in ("POS", "结算单.jpg", "已签促销合同", "需补交", "重新提交"):
            self.assertNotIn(fabricated, summary)

    def test_unknown_name_rejection_does_not_decode_business_documents(self) -> None:
        self.bundle({"坏照片.jpg": b"not an image", "活动返图.xls": b"not a workbook"}, "ai-pack-8.zip")
        with (
            patch("audit_core.material_intake._visual_limitations", side_effect=AssertionError("分类拒绝不应读取图片内容")),
            patch("audit_core.material_intake.extract_activity_return_workbook", side_effect=AssertionError("分类拒绝不应转换工作簿")),
        ):
            result, summary = self.assert_classification_rejection(self.run_audit(self.no_ai_provider))
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(result["archives"][0]["reason_code"], "missing_scenario_marker")
        self.assertNotIn("坏照片", summary)
        self.assertNotIn("活动返图", summary)

    def test_multiple_rejected_archives_keep_each_name_reason_without_ai(self) -> None:
        names = ("ai-pack-8.zip", "维护费用-进场费.zip")
        for name in names:
            self.bundle(_maintenance_files(), name)
        result, summary = self.assert_classification_rejection(self.run_audit(self.no_ai_provider))
        self.assertEqual(self.provider_calls, [])
        self.assertEqual({archive["source_archive"] for archive in result["archives"]}, set(names))
        self.assertEqual(len({archive["archive_id"] for archive in result["archives"]}), 2)
        self.assertEqual(summary.count("\n- "), 2)
        for name in names:
            self.assertIn(name, summary)

    def test_mixed_input_keeps_valid_case_on_business_pipeline_and_rejects_only_unknown_name(self) -> None:
        self.bundle({"POS数据.jpg": _image_bytes("red")}, "正常维护费用.zip")
        self.bundle({"IMG.jpg": _image_bytes("white")}, "ai-pack-8.zip")
        seen = []

        def provider(case: dict, temporary: Path) -> dict:
            seen.append(case.get("kind") or case["scenario"])
            return self.business_provider(case, temporary)

        receipt = self.run_audit(provider)
        rejection, summary = self.assert_classification_rejection(receipt, pure=False)
        self.assertEqual(receipt["scenarios"], ["maintenance_fee"])
        self.assertEqual(seen, ["maintenance_fee"])
        self.assertEqual([item["source_archive"] for item in rejection["archives"]], ["ai-pack-8.zip"])
        workspace = Path(receipt["worktree"])
        self.assertTrue((workspace / "analysis/results/maintenance_fee.json").is_file())
        self.assertFalse((workspace / "analysis/material-diagnostic/result.json").exists())
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
        known = [sheet for sheet in snapshot["view"]["sheets"] if sheet.get("scenario") == "maintenance_fee"]
        self.assertEqual(len(known), 1)
        known_reasons = "\n".join(reason for row in known[0]["rows"] for reason in row.get("error_reasons", []))
        self.assertNotIn("分类标记", known_reasons)
        self.assertIn("ai-pack-8.zip", summary)

    def test_mixed_input_runs_ai_only_for_known_broken_materials(self) -> None:
        self.bundle(_maintenance_files())
        self.bundle({"私有照片.jpg": _image_bytes("yellow")}, "ai-pack-8.zip")
        receipt = self.run_audit()
        rejection, summary = self.assert_classification_rejection(receipt, pure=False)
        self.assertEqual(len(self.provider_calls), 1)
        self.assertEqual([Path(archive["source_archive"]).name for archive in self.provider_calls[0]["archives"]], [ARCHIVE_NAME])
        self.assertEqual([archive["source_archive"] for archive in rejection["archives"]], ["ai-pack-8.zip"])
        workspace = Path(receipt["worktree"])
        diagnosis = json.loads((workspace / "analysis/material-diagnostic/result.json").read_text(encoding="utf-8"))
        self.assertEqual([archive["source_archive"] for archive in diagnosis["archives"]], [ARCHIVE_NAME])
        self.assertTrue(any(issue["code"] == "missing_material" for issue in diagnosis["archives"][0]["issues"]))
        self.assertNotIn("私有照片.jpg", summary)
        self.assertIn("已签促销合同", summary)

    def test_explicit_selection_diagnoses_only_the_selected_known_scenario(self) -> None:
        self.bundle(_maintenance_files())
        self.bundle({"现场.jpg": _image_bytes("yellow")}, "进场费缺合同.zip")
        result, summary = self.assert_completed_diagnostic(self.run_audit(scenario="maintenance_fee"))
        self.assertEqual([item["source_archive"] for item in result["archives"]], [ARCHIVE_NAME])
        self.assertEqual(len(self.provider_calls[0]["archives"]), 1)
        self.assertNotIn("进场费缺合同", summary)

    def test_selected_workspace_names_exclude_unselected_known_archives(self) -> None:
        self.bundle(_maintenance_files())
        self.bundle({"现场.jpg": _image_bytes("yellow")}, "进场费缺合同.zip")
        self.assertEqual(_input_archive_names(self.inputs, "maintenance_fee"), [ARCHIVE_NAME])
        self.bundle({"IMG.jpg": _image_bytes("white")}, "ai-pack-8.zip")
        self.assertEqual(set(_input_archive_names(self.inputs, "maintenance_fee")), {ARCHIVE_NAME, "ai-pack-8.zip"})

    def test_unselected_broken_case_does_not_block_selected_normal_case(self) -> None:
        self.bundle({"POS数据.jpg": _image_bytes("red")}, "正常维护费用.zip")
        self.bundle({"现场.jpg": _image_bytes("yellow")}, "进场费缺合同.zip")
        receipt = self.run_audit(self.business_provider, scenario="maintenance_fee")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(receipt["scenarios"], ["maintenance_fee"])
        self.assertEqual(self.provider_calls, [])
        workspace = Path(receipt["worktree"])
        self.assertTrue((workspace / "analysis/results/maintenance_fee.json").is_file())
        self.assertFalse((workspace / "analysis/material-diagnostic/result.json").exists())

    def test_two_packages_of_same_scenario_preserve_both_diagnostic_sources(self) -> None:
        names = ["甲维护费用.zip", "乙维护费用.zip"]
        for name, color in zip(names, ["red", "blue"]):
            self.bundle({"POS数据.jpg": _image_bytes(color)}, name)
        result, summary = self.assert_completed_diagnostic(self.run_audit())
        self.assertEqual({item["source_archive"] for item in result["archives"]}, set(names))
        self.assertEqual(len(self.provider_calls[0]["archives"]), 2)
        self.assertEqual(len({item["archive_id"] for item in result["archives"]}), 2)
        for name in names:
            self.assertIn(name, summary)

    def test_normal_and_broken_packages_of_same_scenario_both_require_diagnosis(self) -> None:
        self.bundle({"POS数据.jpg": _image_bytes("red")}, "A正常维护费用.zip")
        self.bundle(_maintenance_files(), "B维护费用多结算单.zip")
        receipt = self.run_audit()
        result, summary = self.assert_completed_diagnostic(receipt)
        self.assertEqual({item["source_archive"] for item in result["archives"]},
                         {"A正常维护费用.zip", "B维护费用多结算单.zip"})
        self.assertEqual(len(self.provider_calls), 1)
        self.assertEqual(len(self.provider_calls[0]["archives"]), 2)
        self.assertFalse((Path(receipt["worktree"]) / "analysis/results/maintenance_fee.json").exists())
        self.assertIn("A正常维护费用.zip", summary)
        self.assertIn("B维护费用多结算单.zip", summary)

    def test_unsafe_archive_still_fails_without_ai(self) -> None:
        self.bundle({"../outside.jpg": _image_bytes("red")})
        with self.assertRaises(ArchiveInputError):
            self.run_audit()
        self.assertEqual(self.provider_calls, [])
        self.assertFalse((self.root / "outside.jpg").exists())
        manifest = next(self.worktrees.glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "failed")

    def test_unsafe_unknown_archive_fails_security_validation_without_ai(self) -> None:
        self.bundle({"../outside.jpg": _image_bytes("red")}, "ai-pack-8.zip")
        with self.assertRaises(ArchiveInputError):
            self.run_audit(self.no_ai_provider)
        self.assertEqual(self.provider_calls, [])
        self.assertFalse((self.root / "outside.jpg").exists())
        manifest = next(self.worktrees.glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "failed")
        self.assertFalse((manifest.parent / "analysis/classification-rejection/result.json").exists())

    def test_unknown_name_cannot_hide_unsafe_members_inside_a_nested_zip(self) -> None:
        self.bundle({"附件.zip": _archive_bytes({"../outside.jpg": _image_bytes("red")})}, "ai-pack-8.zip")
        with self.assertRaises(ArchiveInputError):
            self.run_audit(self.no_ai_provider)
        self.assertEqual(self.provider_calls, [])
        manifest = next(self.worktrees.glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["status"], "failed")
        self.assertFalse((manifest.parent / "analysis/classification-rejection/result.json").exists())

    def test_explicit_selection_cannot_assign_an_unknown_zip_to_the_requested_type(self) -> None:
        self.bundle(_maintenance_files(), "ai-pack-8.zip")
        receipt = self.run_audit(self.no_ai_provider, scenario="maintenance_fee")
        result, summary = self.assert_classification_rejection(receipt)
        self.assertEqual(self.provider_calls, [])
        self.assertEqual(result["archives"][0]["reason_code"], "missing_scenario_marker")
        self.assertNotIn("已签促销合同", summary)

    def test_model_execution_error_is_not_converted_to_completed_diagnostic(self) -> None:
        self.bundle(_maintenance_files())

        def broken_provider(case: dict, temporary: Path) -> dict:
            self.provider(case, temporary)
            raise AuditError("模型隔离材料读取失败")

        with self.assertRaisesRegex(AuditError, "模型隔离材料读取失败"):
            self.run_audit(broken_provider)
        self.assertEqual(len(self.provider_calls), 1)
        manifest_path = next(self.worktrees.glob("*/manifest.json"))
        self.assertEqual(json.loads(manifest_path.read_text(encoding="utf-8"))["status"], "failed")
        self.assertFalse((manifest_path.parent / "ai-analysis-summary.md").exists())
        self.assertTrue(all(not path.exists() for path in self.temporary_sources))

    def run_oss_submission(self, archive_name: str, files: dict[str, bytes], provider, *, analyze_id: int = 9, expected_status: str = "completed", callback_required: bool = True, fail_callback: bool = False) -> tuple[dict, list[dict]]:
        zip_bytes = _archive_bytes(files)
        callbacks: list[dict] = []
        callback_received = threading.Event()

        def downloader(_submission: dict, destination: Path, _config: OSSIntakeConfig) -> dict:
            destination.write_bytes(zip_bytes)
            return {"bytes": len(zip_bytes), "sha256": hashlib.sha256(zip_bytes).hexdigest(),
                    "etag": None, "verified_at": "2026-09-10T00:00:00+00:00"}

        def runner(**kwargs) -> dict:
            kwargs.pop("cancel_event")
            return run_persistent_audit(**kwargs, evidence_provider=provider, input_source="oss")

        def callback_sender(payload: dict, _config: OSSIntakeConfig) -> dict:
            callbacks.append(payload)
            callback_received.set()
            if fail_callback:
                raise OSSIntakeCallbackError("测试回调失败", attempts=3, http_status=503)
            return {"http_status": 200, "attempts": 1}

        service = OSSIntakeService(
            worktrees_root=self.worktrees, input_root=self.root / "input-oss",
            config=OSSIntakeConfig(allowed_hosts=(OSS_HOST,), callback_url="https://business.example/api/v1/ai/analyze/callback", callback_required=callback_required),
            downloader=downloader, runner=runner, url_validator=lambda value, _config: value,
            callback_sender=callback_sender,
        )
        self.addCleanup(service.close)
        job, created = service.submit({
            "verifyCode": "HX202601160053", "analyzeId": analyze_id,
            "downloadUrl": f"https://{OSS_HOST}/incoming/{quote(archive_name)}?X-Oss-Signature=test-only-secret",
        })
        self.assertTrue(created)
        if callback_required:
            self.assertTrue(callback_received.wait(timeout=15), service.get(job["job_id"]))
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            job = service.get(job["job_id"])
            if job["status"] in {"completed", "failed"}:
                break
            time.sleep(0.02)
        service.close()
        self.assertEqual(job["status"], expected_status, job)
        self.assertEqual(job["callback"]["status"], "failed" if fail_callback else "delivered" if callback_required else "not_required")
        self.assertEqual((self.root / "input-oss" / job["job_id"] / archive_name).read_bytes(), zip_bytes)
        self.assertNotIn("test-only-secret", json.dumps(job, ensure_ascii=False))
        return job, callbacks

    def test_oss_service_runs_persistent_diagnostic_and_callbacks_saved_markdown(self) -> None:
        job, callbacks = self.run_oss_submission(ARCHIVE_NAME, _maintenance_files(), self.conflicting_scenario_provider)
        result, summary = self.assert_completed_diagnostic(job["result"])
        self.assertEqual(len(self.provider_calls), 1)
        self.assertEqual(callbacks, [{"verifyCode": "HX202601160053", "analyzeId": 9, "result": summary}])
        self.assertEqual(job["result"]["scenarios"], ["maintenance_fee"])
        self.assertEqual(result["archives"][0]["scenario"], "maintenance_fee")
        self.assertIn("已签促销合同", callbacks[0]["result"])
        self.assertIn("服务费结算单.jpg", callbacks[0]["result"])
        for unselected in ("核销类型待确认", "价格补差", "人员激励"):
            self.assertNotIn(unselected, callbacks[0]["result"])
        self.assertEqual(result["archives"][0]["source_archive"], ARCHIVE_NAME)

    def test_oss_unknown_and_ambiguous_names_callback_rejection_with_three_fields_and_no_ai(self) -> None:
        for analyze_id, archive_name, expected_code in (
            (10, "ai-pack-8.zip", "missing_scenario_marker"),
            (11, "维护费用-进场费.zip", "ambiguous_scenario_markers"),
        ):
            with self.subTest(archive_name=archive_name):
                job, callbacks = self.run_oss_submission(archive_name, _maintenance_files(), self.no_ai_provider, analyze_id=analyze_id, expected_status="failed")
                self.assertEqual(job["failure"]["code"], "classification_failed")
                result, summary = self.assert_classification_rejection(job["result"])
                self.assertEqual(self.provider_calls, [])
                self.assertEqual(result["archives"][0]["reason_code"], expected_code)
                self.assertEqual(callbacks, [{"verifyCode": "HX202601160053", "analyzeId": analyze_id, "result": summary}])
                self.assertIn(archive_name, summary)
                for fabricated in ("已签促销合同", "POS", "结算单.jpg", "建议", "处理方式", "需补交"):
                    self.assertNotIn(fabricated, summary)

    def test_oss_classification_stays_failed_when_callback_delivery_fails(self) -> None:
        job, callbacks = self.run_oss_submission("ai-pack-8.zip", _maintenance_files(), self.no_ai_provider,
                                               expected_status="failed", fail_callback=True)
        _, summary = self.assert_classification_rejection(job["result"])
        self.assertEqual(job["failure"]["code"], "callback_failed")
        self.assertEqual(callbacks[0]["result"], summary)
        self.assertEqual(self.provider_calls, [])

    def test_oss_classification_stays_failed_when_callback_is_disabled(self) -> None:
        job, callbacks = self.run_oss_submission("ai-pack-8.zip", _maintenance_files(), self.no_ai_provider,
                                               expected_status="failed", callback_required=False)
        self.assert_classification_rejection(job["result"])
        self.assertEqual(job["failure"]["code"], "classification_failed")
        self.assertEqual(callbacks, [])
        self.assertEqual(self.provider_calls, [])


if __name__ == "__main__":
    unittest.main()
