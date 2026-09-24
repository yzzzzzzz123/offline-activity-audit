from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote

from audit_core.archive_input import ArchiveInputError
from audit_core.common import AuditError
from audit_core.oss_intake import OSSIntakeCallbackError, OSSIntakeConfig, OSSIntakeService
from audit_core.scenario_registry import SCENARIO_LABELS
from audit_core.workbench_runtime import ROOT_HTML, _input_archive_names, run_persistent_audit
from audit_core.workbench_server import WorkbenchCatalog
from tests.pdf_test_support import PolicyProvider, archive_bytes as _archive_bytes, bundle, flags_for, image_bytes

OSS_HOST = "audit-materials.oss-cn-hangzhou.aliyuncs.com"


class MaterialDiagnosticWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "input"
        self.worktrees = self.root / "worktrees"
        self.provider = PolicyProvider()

    def bundle(self, name="未注明类型.zip", files=None):
        return bundle(self.inputs, name, files)

    def run_audit(self, provider=None, *, scenario=None, biz_type="KT板等物料制作", input_dir=None):
        return run_persistent_audit("20260917-pdf-workflow", producer_model="codex",
                                   input_dir=input_dir or self.inputs, worktrees_root=self.worktrees,
                                   evidence_provider=provider or self.provider, scenario=scenario, biz_type=biz_type)

    def assert_gate_issues(self, receipt):
        self.assertEqual(receipt["status"], "completed")
        self.assertIsNone(receipt["failure"])
        workspace = Path(receipt["worktree"])
        result = json.loads((workspace / "analysis/classification-rejection/result.json").read_text(encoding="utf-8"))
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(result["decision_source"], "biz_type")
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertTrue((workspace / "offline-activity-audit.html").is_file())
        summary = (workspace / "ai-analysis-summary.md").read_text(encoding="utf-8")
        self.assertNotIn("改ZIP", summary)
        self.assertNotIn("未注明核销方式", summary)
        self.assertNotIn("未发现错误", summary)
        if not receipt["scenarios"]:
            self.assertIn("## 核销资料问题", summary)
            self.assertNotIn("核销失败", summary)
        return result, summary, snapshot

    def test_arbitrary_and_conflicting_zip_names_do_not_override_confirmed_type(self):
        for name in ("未注明类型.zip", "维护费用-进场费.zip", "搭赠.zip"):
            with self.subTest(name=name):
                source = self.bundle(name)
                receipt = self.run_audit()
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(receipt["scenarios"], ["poster_material"])
                source.unlink()  # This test's generated fixture only.
        self.assertEqual(len(self.provider.calls), 6)

    def test_missing_materials_reject_before_skill_and_keep_all_chinese_names(self):
        self.bundle()
        provider = PolicyProvider(missing={0: ("invoice", "settlement", "promotion_contract")})
        receipt = self.run_audit(provider)
        result, summary, snapshot = self.assert_gate_issues(receipt)
        self.assertEqual(receipt["scenarios"], ["poster_material"])
        self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification"])
        workspace = Path(receipt["worktree"])
        self.assertFalse((workspace / "analysis/results/poster_material.json").exists())
        for text in ("发票或收据", "结算单", "促销合同"):
            self.assertIn(text, result["archives"][0]["reason"])
        self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 0)

    def test_personnel_audit_can_access_payment_images_until_analysis_finishes(self):
        self.bundle(files={"图一.png": image_bytes(), "图二.png": image_bytes("blue")})
        observed = []

        def inspect(case, value):
            if case["kind"] == "pdf_policy_audit":
                images = [unit["image"] for unit in case["units"] if unit.get("image")]
                observed.extend(images)
                self.assertEqual(len(images), 2)
                self.assertTrue(all(image.is_file() for image in images))

        provider = PolicyProvider(("personnel_incentive",),
                                  flags={0: flags_for("personnel_incentive", red_packet=True)}, mutate=inspect)
        receipt = self.run_audit(provider, biz_type="人员激励")
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(len(observed), 2)
        self.assertTrue(all(not image.exists() for image in observed))

    def test_only_settlement_and_pos_report_missing_items_for_confirmed_type(self):
        def two_materials(case, value):
            if case["kind"] != "pdf_material_classification":
                self.fail("只有结算单和POS的包不得进入任何类型审核")
            sources = {"settlement": case["units"][0]["unit_id"], "pos": case["units"][1]["unit_id"]}
            result = value["classification"]
            result.update(reason="仅有结算单和POS，人员激励资料尚不齐全")
            for match in result["material_matches"]:
                covered = set()
                for item in match["materials"]:
                    if item["id"] in sources and item["state"] != "not_applicable":
                        item.update(state="present", source_ids=[sources[item["id"]]], reason="对应资料实际存在")
                        covered.add(item["id"])
                    elif item["state"] != "not_applicable":
                        item.update(state="missing", source_ids=[], reason="本包仅含结算单和POS，未提交此资料项")
                match.update(supported=False, source_ids=list(sources.values()), extra_materials=[
                    {"description": role, "reason": "该类清单未列此资料项", "source_ids": [uid]}
                    for role, uid in sources.items() if role not in covered])
        for name in ("维护费用.zip", "人员激励.zip"):
            with self.subTest(name=name):
                source = self.bundle(name, {"01.png": image_bytes(), "02.png": image_bytes("blue")})
                provider = PolicyProvider(mutate=two_materials)
                receipt = self.run_audit(provider, biz_type="人员激励")
                result, summary, _ = self.assert_gate_issues(receipt)
                self.assertEqual(receipt["scenarios"], ["personnel_incentive"])
                self.assertEqual(len(provider.calls), 1)
                self.assertIn("促销合同", result["archives"][0]["reason"])
                self.assertIn("红包或工资打款截图", result["archives"][0]["reason"])
                packet = json.loads((Path(receipt["worktree"]) / "analysis/pdf-policy/evidence.json").read_text(encoding="utf-8"))["packets"][0]
                self.assertEqual(packet["classification"]["candidate_scenarios"], ["personnel_incentive"])
                self.assertEqual(len(packet["classification"]["material_matches"]), 1)
                self.assertTrue(all(not item["supported"] for item in packet["classification"]["material_matches"]))
                source.unlink()  # Only this generated fixture, never original input.

    def test_model_cannot_clear_or_change_the_user_confirmed_type(self):
        self.bundle()
        for candidates in ([], ["personnel_incentive"], ["personnel_incentive", "poster_material"]):
            with self.subTest(candidates=candidates):
                def mutate(case, value):
                    if case["kind"] == "pdf_material_classification":
                        value["classification"]["candidate_scenarios"] = candidates
                provider = PolicyProvider(mutate=mutate)
                with self.assertRaises(AuditError):
                    self.run_audit(provider)
                self.assertEqual(len(provider.calls), 1)

    def test_no_type_can_enter_audit_with_missing_material_items(self):
        from audit_core.pdf_policy import requirements
        from audit_core.scenario_registry import SCENARIO_ORDER
        self.bundle()
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario):
                provider = PolicyProvider((scenario,), missing={0: [r.id for r in requirements(scenario)]})
                receipt = self.run_audit(provider, biz_type=SCENARIO_LABELS[scenario])
                self.assert_gate_issues(receipt)
                self.assertEqual(receipt["scenarios"], [scenario])
                self.assertEqual(len(provider.calls), 1)
                workspace = Path(receipt["worktree"])
                self.assertFalse((workspace / f"analysis/results/{scenario}.json").exists())

    def test_extra_item_is_persisted_with_its_source_and_does_not_enter_audit(self):
        self.bundle(files={"合同和运输单.png": image_bytes()})
        extra = {"description": "独立运输单", "reason": "物料制作清单未列此资料项", "source_ids": ["a001-f0001"]}
        provider = PolicyProvider(extra={0: [extra]})
        receipt = self.run_audit(provider)
        result, summary, _ = self.assert_gate_issues(receipt)
        self.assertIn("多出的资料：独立运输单", result["archives"][0]["reason"])
        self.assertIn("合同和运输单.png", result["archives"][0]["reason"])
        self.assertEqual(len(provider.calls), 1)
        packet = json.loads((Path(receipt["worktree"]) / "analysis/pdf-policy/evidence.json").read_text(encoding="utf-8"))["packets"][0]
        match = next(m for m in packet["classification"]["material_matches"] if m["scenario"] == "poster_material")
        self.assertEqual(match["extra_materials"], [extra])

    def test_multiple_same_role_files_and_identical_photos_are_not_type_conflicts(self):
        self.bundle(files={"甲/照片.png": image_bytes(), "乙/照片.png": image_bytes(), "第二页.png": image_bytes("blue")})
        receipt = self.run_audit()
        self.assertEqual(receipt["status"], "completed")
        packet = json.loads((Path(receipt["worktree"]) / "analysis/pdf-policy/evidence.json").read_text(encoding="utf-8"))["packets"][0]
        self.assertEqual(len(packet["documents"]), 3)
        self.assertEqual(len(packet["classification"]["source_ids"]), 3)

    def test_two_packages_of_same_type_keep_results_passes_and_sources(self):
        self.bundle("甲.zip")
        self.bundle("乙.zip")
        receipt = self.run_audit()
        workspace = Path(receipt["worktree"])
        result = json.loads((workspace / "analysis/results/poster_material.json").read_text(encoding="utf-8"))
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
        self.assertEqual(len(result["archives"]), 2)
        self.assertEqual(len(snapshot["view"]["sheets"]), 2)
        self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 2)
        self.assertEqual(len({a["archive_id"] for a in result["archives"]}), 2)
        summary = Path(receipt["analysis_summary"]).read_text(encoding="utf-8")
        self.assertIn("甲.zip", summary)
        self.assertIn("乙.zip", summary)

    def test_mixed_batch_completes_and_keeps_gate_and_audit_issues(self):
        self.bundle("A.zip")
        self.bundle("B.zip")
        provider = PolicyProvider(candidates={1: []})
        receipt = self.run_audit(provider)
        result, summary, snapshot = self.assert_gate_issues(receipt)
        self.assertEqual(receipt["scenarios"], ["poster_material"])
        self.assertEqual(len(snapshot["view"]["sheets"]), 2)
        self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 1)
        self.assertTrue((Path(receipt["worktree"]) / "analysis/results/poster_material.json").exists())
        self.assertEqual(result["archives"][0]["source_archive"], "B.zip")
        self.assertNotIn("## 核销资料问题", summary)
        self.assertIn("B.zip", summary)
        self.assertIn("## KT板等物料制作", summary)
        self.assertIn("A.zip", summary)
        self.assertNotIn("资料清单不匹配", summary)

    def test_selection_does_not_filter_inputs_by_names_or_model_candidates(self):
        self.bundle("A-人员激励.zip")
        self.bundle("B-物料.zip")
        provider = PolicyProvider(("poster_material", "entry_fee"))
        receipt = self.run_audit(provider, scenario="entry_fee", biz_type="条码费")
        self.assertEqual(receipt["scenarios"], ["entry_fee"])
        self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification", "pdf_policy_audit"] * 2)
        self.assertTrue(all(c["selected_scenario"] == "entry_fee" for c in provider.calls
                            if c["kind"] == "pdf_material_classification"))
        self.assertEqual(_input_archive_names(self.inputs, "entry_fee"), ["A-人员激励.zip", "B-物料.zip"])
        manifest = json.loads((Path(receipt["worktree"]) / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["source_archives"]), 2)

    def test_selection_cannot_hide_an_input_with_missing_material_items(self):
        self.bundle("A.zip")
        self.bundle("B.zip")
        provider = PolicyProvider(("poster_material", "entry_fee"), missing={0: ("entry_agreement",)})
        receipt = self.run_audit(provider, scenario="entry_fee", biz_type="条码费")
        self.assert_gate_issues(receipt)
        self.assertEqual(receipt["scenarios"], ["entry_fee"])
        provider = PolicyProvider(candidates={0: [], 1: []})
        self.assert_gate_issues(self.run_audit(provider, scenario="entry_fee", biz_type="条码费"))

    def test_removed_types_cannot_be_selected(self):
        self.bundle()
        for scenario in ("maintenance_fee", "other_expense"):
            with self.subTest(scenario=scenario), self.assertRaisesRegex(AuditError, "八类"):
                self.run_audit(scenario=scenario)
        self.assertEqual(self.provider.calls, [])

    def test_pos_target_and_giveaway_are_independent_despite_archive_names(self):
        for name, scenario in (("A-搭赠.zip", "pos_target_incentive"), ("B-POS达标激励.zip", "giveaway_promotion")):
            with self.subTest(scenario=scenario):
                source = self.bundle(name)
                provider = PolicyProvider()
                receipt = self.run_audit(provider, input_dir=source, biz_type=SCENARIO_LABELS[scenario])
                self.assertEqual(receipt["status"], "completed")
                self.assertEqual(receipt["scenarios"], [scenario])
                self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification", "pdf_policy_audit"])
                self.assertEqual(provider.calls[1]["scenario"], scenario)
                result = json.loads((Path(receipt["worktree"]) / f"analysis/results/{scenario}.json").read_text(encoding="utf-8"))
                self.assertEqual(result["scenario"], scenario)
                self.assertIn(SCENARIO_LABELS[scenario], Path(receipt["analysis_summary"]).read_text(encoding="utf-8"))

    def test_present_but_invalid_material_is_a_business_check_failure(self):
        self.bundle()
        def mutate(case, value):
            if case["kind"] == "pdf_policy_audit":
                check = next(c for c in value["checks"] if c["rule_id"] == "settlement_template_seal")
                check.update(status="fail", reason="结算单缺少客户盖章")
        receipt = self.run_audit(PolicyProvider(mutate=mutate))
        self.assertEqual(receipt["status"], "completed")
        self.assertGreater(receipt["error_count"], 0)
        self.assertIn("结算单缺少客户盖章", Path(receipt["analysis_summary"]).read_text(encoding="utf-8"))
        self.assertFalse((Path(receipt["worktree"]) / "analysis/classification-rejection").exists())

    def test_pdf_zero_wording_produces_specific_issue_without_rejecting_payment(self):
        self.bundle()

        def mutate(case, value):
            if case["kind"] == "pdf_material_classification":
                value["documents"][0]["facts"].extend(["结算奖励单价=3", "合同奖励单价=2"])
            else:
                uid = case["documents"][0]["unit_id"]
                check = next(c for c in value["checks"] if c["rule_id"] == "reward_unit_price")
                check.update(status="pass", reason="模型误判一致", source_ids=[uid], comparisons=[{
                    "label": "商品甲奖励单价", "value_kind": "unit_price", "left": {"unit_id": uid, "quote": "结算奖励单价=3", "number": "3"},
                    "right": [{"unit_id": uid, "quote": "合同奖励单价=2", "number": "2"}],
                    "operator": "eq", "operation": "value"}])

        receipt = self.run_audit(PolicyProvider(("personnel_incentive",), mutate=mutate), biz_type="人员激励")
        self.assertEqual(receipt["status"], "completed")
        self.assertIsNone(receipt["failure"])
        workspace = Path(receipt["worktree"])
        result = json.loads((workspace / "analysis/results/personnel_incentive.json").read_text(encoding="utf-8"))
        self.assertEqual(result["archives"][0]["summary"]["conclusion"], "issues_found")
        summary = Path(receipt["analysis_summary"]).read_text(encoding="utf-8")
        self.assertIn("商品甲奖励单价", summary)
        self.assertIn("申报单价须与合同约定单价一致，相差1", summary)
        snapshot = json.loads(Path(receipt["snapshot"]).read_text(encoding="utf-8"))
        for forbidden in ("核销失败", "0核销", "不能报销", "不予核销", "zero_reimbursement", "approved_amount"):
            self.assertNotIn(forbidden, summary)
            self.assertNotIn(forbidden, json.dumps(snapshot["view"], ensure_ascii=False))

    def test_customer_reasons_translate_model_condition_fields(self):
        from tests.pdf_test_support import flags_for
        self.bundle()
        def mutate(case, value):
            if case["kind"] == "pdf_policy_audit":
                check = next(c for c in value["checks"] if c["rule_id"] == "payment_company")
                check["reason"] = "red_packet为null，无法确认付款截图是否为红包。"
        receipt = self.run_audit(PolicyProvider(("personnel_incentive",),
            flags={0: flags_for("personnel_incentive", red_packet=None)}, mutate=mutate), biz_type="人员激励")
        summary = Path(receipt["analysis_summary"]).read_text(encoding="utf-8")
        self.assertNotIn("red_packet", summary)
        self.assertNotIn("null", summary)
        self.assertIn("红包截图", summary)
        self.assertIn("还不清楚", summary)

    def test_all_later_evidence_validates_before_publishing_any_decision(self):
        self.bundle("A.zip")
        self.bundle("B.zip")
        def mutate(case, value):
            if case["kind"] == "pdf_policy_audit" and case["archive_id"] == "a002":
                value["checks"].pop()
        provider = PolicyProvider(mutate=mutate)
        with self.assertRaisesRegex(AuditError, "全部且仅覆盖"):
            self.run_audit(provider)
        workspace = next(self.worktrees.glob("*/manifest.json")).parent
        self.assertFalse((workspace / "analysis/results").exists())
        self.assertEqual(len(provider.calls), 4)
        self.assertTrue(all(not p.exists() for p in provider.temporary_roots))

    def test_model_failure_is_execution_failure_and_does_not_become_missing_material(self):
        self.bundle()
        def broken(case, temporary):
            raise AuditError("模型连接失败")
        with self.assertRaisesRegex(AuditError, "模型连接失败"):
            self.run_audit(broken)
        workspace = next(self.worktrees.glob("*/manifest.json")).parent
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "failed")
        self.assertNotEqual(manifest["failure"].get("code"), "classification_failed")
        self.assertFalse((workspace / "analysis/classification-rejection").exists())
        self.assertFalse((workspace / "ai-analysis-summary.md").exists())
        self.assertTrue((workspace / "offline-activity-audit.html").exists())

    def test_unsafe_archive_and_nested_archive_fail_before_any_model(self):
        for files in ({"../escape.png": image_bytes()}, {"附件.zip": _archive_bytes({"../escape.png": image_bytes()})}):
            self.bundle(files=files)
            with self.assertRaises(ArchiveInputError):
                self.run_audit()
            self.assertEqual(self.provider.calls, [])
            self.assertFalse((self.root / "escape.png").exists())

    def test_run_preserves_input_root_html_and_publishes_one_self_contained_archive(self):
        source = self.bundle()
        original = source.read_bytes(), ROOT_HTML.read_bytes()
        with patch("audit_core.product_database.load_product_catalog", side_effect=AssertionError("No database")):
            receipt = self.run_audit()
        workspace = Path(receipt["worktree"])
        self.assertEqual((source.read_bytes(), ROOT_HTML.read_bytes()), original)
        self.assertEqual(len(list(workspace.rglob("*.html"))), 1)
        self.assertFalse(list(workspace.rglob("*.xlsx")))
        self.assertFalse(list(workspace.rglob("*.png")))
        self.assertTrue(all(not p.exists() for p in self.provider.temporary_roots))
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["scenario_classification_policy"], "biz_type")
        self.assertTrue(manifest["analysis_started_at"])
        self.assertEqual(manifest["snapshot_sha256"], hashlib.sha256((workspace / "snapshot.json").read_bytes()).hexdigest())
        self.assertEqual(manifest["analysis_summary"]["sha256"], hashlib.sha256((workspace / "ai-analysis-summary.md").read_bytes()).hexdigest())
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
        unknown_invoice = next(row for row in snapshot["view"]["sheets"][0]["rows"] if row["rule_id"] == "invoice_details")
        self.assertEqual(unknown_invoice["heading"], "发票收据信息：暂时不能确认")
        self.assertTrue(list((workspace / "dom/checkpoints").glob("*.json")))
        self.assertEqual(WorkbenchCatalog(self.worktrees).list_runs()[0]["status"], "completed")

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
            worktrees_root=self.worktrees, input_root=self.root / "input",
            config=OSSIntakeConfig(allowed_hosts=(OSS_HOST,), callback_url="https://business.example/api/v1/ai/analyze/callback", callback_required=callback_required),
            downloader=downloader, runner=runner, url_validator=lambda value, _config: value,
            callback_sender=callback_sender,
        )
        def close_fixture_service():
            service.close()
            # close() queues shutdown asynchronously. Wait for this fixture's
            # workers before TemporaryDirectory removes their receipt files.
            for worker in (service._download_worker, service._audit_worker):
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive(), "测试后台线程未退出，不能开始删除临时资料")
        self.addCleanup(close_fixture_service)
        job, created = service.submit({
            "verifyCode": "HX202601160053", "analyzeId": analyze_id, "bizType": "KT板等物料制作",
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
        close_fixture_service()
        self.assertEqual(job["status"], expected_status, job)
        self.assertEqual(job["callback"]["status"], "failed" if fail_callback else "delivered" if callback_required else "not_required")
        self.assertEqual((self.root / "input" / job["job_id"] / archive_name).read_bytes(), zip_bytes)
        self.assertNotIn("test-only-secret", json.dumps(job, ensure_ascii=False))
        return job, callbacks


    def test_oss_callbacks_use_saved_markdown_and_exact_three_fields(self):
        job, callbacks = self.run_oss_submission("不按名称选型.zip", {"材料.png": image_bytes()}, self.provider)
        self.assertEqual(job["result"]["scenarios"], ["poster_material"])
        self.assertEqual(set(callbacks[0]), {"verifyCode", "analyzeId", "result"})
        self.assertEqual(callbacks[0]["result"], Path(job["result"]["analysis_summary"]).read_text(encoding="utf-8"))
        self.assertNotIn("downloadUrl", callbacks[0])

    def test_oss_missing_materials_complete_and_callback_exact_reason(self):
        provider = PolicyProvider(missing={0: ("invoice",)})
        job, callbacks = self.run_oss_submission("进场费.zip", {"材料.png": image_bytes()}, provider, expected_status="completed")
        self.assertEqual(job["result"]["scenarios"], ["poster_material"])
        self.assertIsNone(job["result"]["failure"])
        self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification"])
        self.assertGreater(job["result"]["error_count"], 0)
        self.assertIn("缺少发票或收据", callbacks[0]["result"])
        self.assertNotIn("核销失败", callbacks[0]["result"])

    def test_oss_gate_issues_complete_but_callback_transport_failure_stays_failed(self):
        for analyze_id, required, fail in ((10, True, True), (11, False, False)):
            with self.subTest(callback_required=required):
                provider = PolicyProvider(candidates={0: []})
                job, callbacks = self.run_oss_submission("无类型.zip", {"材料.png": image_bytes()}, provider,
                    analyze_id=analyze_id, expected_status="failed" if fail else "completed", callback_required=required, fail_callback=fail)
                self.assertIsNone(job["result"]["failure"])
                self.assertEqual(len(callbacks), int(required))
                self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification"])


if __name__ == "__main__":
    unittest.main()
