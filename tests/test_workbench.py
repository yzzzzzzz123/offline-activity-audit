from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from unittest import mock

from audit_core.html_report import DATA_CLOSE, DATA_OPEN
from audit_core.common import AuditError
from audit_core.orchestrator import run_audit
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import (
    WorkbenchRunStore,
    main_flow_task_list,
    reserve_workspace,
)
from audit_core.workbench_runtime import ROOT_HTML, run_persistent_audit


def _view_payload() -> dict:
    return {
        "schema_version": "1.1",
        "title": "线下活动核销结果",
        "sheets": [
            {
                "name": "维护费用核销",
                "scenario": "maintenance_fee",
                "title": "维护费用核销｜只显示错误",
                "note": "测试快照",
                "headers": ["问题文件", "对照文件", "错误原因", "处理方式", "影响", "结论"],
                "rows": [
                    {
                        "excel_row": 4,
                        "kind": "record",
                        "section": "detail",
                        "status": "issue",
                        "confidence": "high",
                        "heading": "POS电子表缺失",
                        "values": [
                            "盖章POS.jpg",
                            "POS电子表",
                            "未提交电子表",
                            "补交POS电子表",
                            "金额不能复算",
                            "资料需补正",
                        ],
                    }
                ],
                "audit_counts": {
                    "source_row_count": 1,
                    "error_count": 1,
                    "detail_error_count": 1,
                    "context_error_count": 0,
                },
            }
        ],
    }


class WorkbenchStoreTests(unittest.TestCase):
    def test_orchestrator_groups_all_scenarios_into_monotonic_main_flow_stages(self) -> None:
        observed: list[tuple[str, str | None]] = []
        cases = {
            "maintenance_fee": {"scenario": "maintenance_fee"},
            "entry_fee": {"scenario": "entry_fee"},
        }

        def provider(case: dict, _temporary_root: Path) -> dict:
            return {"scenario": case["scenario"]}

        def audit_case(case: dict, _evidence: dict) -> dict:
            return {"scenario": case["scenario"], "summary": {}}

        def observer(event_type: str, payload: dict) -> None:
            observed.append((event_type, payload.get("scenario")))

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with (
                mock.patch("audit_core.orchestrator.prepare_cases", return_value=cases),
                mock.patch("audit_core.orchestrator.validate_json"),
                mock.patch(
                    "audit_core.orchestrator.audit_maintenance_fee_case",
                    side_effect=audit_case,
                ),
                mock.patch(
                    "audit_core.orchestrator.audit_entry_fee_case",
                    side_effect=audit_case,
                ),
                mock.patch("audit_core.orchestrator.create_combined_report"),
                mock.patch("audit_core.orchestrator.verify_workbook", return_value={}),
                mock.patch("audit_core.orchestrator.create_html_report_from_workbook"),
                mock.patch("audit_core.orchestrator.verify_html_report", return_value={}),
                mock.patch(
                    "audit_core.orchestrator._publish_html_without_overwrite",
                    return_value=root / "published.html",
                ),
            ):
                run_audit(
                    "20260831-stage-flow",
                    producer_model="codex",
                    input_dir=root / "input",
                    output_dir=root,
                    evidence_provider=provider,
                    observer=observer,
                )

        self.assertEqual(
            observed,
            [
                ("cases.prepared", None),
                ("scenario.started", "maintenance_fee"),
                ("scenario.started", "entry_fee"),
                ("evidence.validated", "maintenance_fee"),
                ("evidence.validated", "entry_fee"),
                ("result.validated", "maintenance_fee"),
                ("result.validated", "entry_fee"),
                ("report.verified", None),
            ],
        )

    def test_main_flow_checklist_stays_aligned_with_skill_contract(self) -> None:
        tasks = main_flow_task_list()
        self.assertEqual(len(tasks), 6)
        skill = (
            Path("skills/orchestrate-offline-audit/SKILL.md")
            .read_text(encoding="utf-8")
        )
        positions = []
        for task in tasks:
            marker = (
                f'- [ ] `{int(task["order"]):02d} {task["stage"]}` '
                f'— {task["label"]}'
            )
            self.assertIn(marker, skill)
            positions.append(skill.index(marker))
        self.assertEqual(positions, sorted(positions))

    def test_workspace_uses_second_level_model_reasoning_name_without_revision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "20260828-codex.html").touch()
            (root / "20260828-codex-1.4.html").touch()
            started_at = datetime(2026, 8, 28, 17, 55, 32)
            workspace = reserve_workspace(
                "20260828-example",
                "codex",
                root,
                audit_model="gpt-5.6-sol",
                reasoning_effort="xhigh",
                started_at=started_at,
            )
            self.assertEqual(workspace.name, "20260828_1755_32-gpt5.6sol_xhigh")
            with self.assertRaisesRegex(AuditError, "同一秒"):
                reserve_workspace(
                    "20260828-example",
                    "codex",
                    root,
                    audit_model="gpt-5.6-sol",
                    reasoning_effort="xhigh",
                    started_at=started_at,
                )

    def test_workspace_name_supports_other_models_and_max_reasoning(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = reserve_workspace(
                "20260902-example",
                "qwen3.8",
                temporary,
                audit_model="qwen3.8",
                reasoning_effort="max",
                started_at=datetime(2026, 9, 2, 17, 55, 32),
            )
            self.assertEqual(workspace.name, "20260902_1755_32-qwen3.8_max")

    def test_store_persists_trace_analysis_and_dom_checkpoints(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = reserve_workspace("20260828-example", "codex", root)
            store = WorkbenchRunStore(
                workspace,
                run_id="20260828-example",
                producer_model="codex",
                root_html=Path("offline-activity-audit.html"),
                workbench_url="http://127.0.0.1:8080/",
            )
            store.observe(
                "cases.prepared",
                {
                    "scenarios": ["maintenance_fee"],
                    "cases": {
                        "maintenance_fee": {
                            "scenario": "maintenance_fee",
                            "source_archive": "D:/temporary/维护费用.zip",
                        }
                    },
                },
            )
            store.observe(
                "evidence.validated",
                {"scenario": "maintenance_fee", "evidence": {"documents": []}},
            )
            store.observe(
                "result.validated",
                {
                    "scenario": "maintenance_fee",
                    "result": {
                        "scenario": "maintenance_fee",
                        "summary": {"conclusion": "fail"},
                        "maintenance_fee_audit": {
                            "controls": [
                                {
                                    "control_id": "pos_visual_seal",
                                    "status": "pass",
                                    "basis": "盖章POS清晰可见",
                                }
                            ]
                        },
                    },
                },
            )
            store.complete(
                view_payload=_view_payload(),
                scenarios=["maintenance_fee"],
                verification={"html": {"verified": True}},
            )

            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema_version"], "1.2")
            self.assertEqual(manifest["audit_model"], "codex")
            self.assertEqual(manifest["reasoning_effort"], "high")
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["error_count"], 1)
            self.assertEqual(manifest["main_flow_tasks"], main_flow_task_list())
            self.assertEqual(
                [task["stage"] for task in manifest["main_flow_tasks"]],
                [
                    "bootstrap",
                    "intake",
                    "analysis",
                    "evidence",
                    "decision",
                    "verification",
                ],
            )
            self.assertEqual(snapshot["view"]["sheets"][0]["scenario"], "maintenance_fee")
            self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 1)
            self.assertEqual(
                snapshot["view"]["pass_check_log"]["groups"][0]["items"][0]["title"],
                "盖章POS核验通过",
            )
            self.assertGreaterEqual(len(snapshot["dom_checkpoints"]), 5)
            self.assertTrue((workspace / "analysis" / "evidence" / "maintenance_fee.json").is_file())
            events = (workspace / "logs" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("run.completed", events)

    def test_persistent_runner_publishes_data_directory_not_an_html_result(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def fake_run_audit(*args, **kwargs):  # type: ignore[no-untyped-def]
                observer = kwargs["observer"]
                observer(
                    "cases.prepared",
                    {
                        "scenarios": ["maintenance_fee"],
                        "cases": {"maintenance_fee": {"scenario": "maintenance_fee"}},
                    },
                )
                observer(
                    "evidence.validated",
                    {"scenario": "maintenance_fee", "evidence": {"documents": []}},
                )
                observer(
                    "result.validated",
                    {
                        "scenario": "maintenance_fee",
                        "result": {"scenario": "maintenance_fee", "summary": {}},
                    },
                )
                observer(
                    "report.verified",
                    {"verification": {"html": {"verified": True}}},
                )
                generated = Path(kwargs["output_dir"]) / "20260828-codex.html"
                generated.write_text(
                    "<!doctype html>"
                    + DATA_OPEN
                    + json.dumps(_view_payload(), ensure_ascii=False)
                    + DATA_CLOSE,
                    encoding="utf-8",
                )
                return {
                    "output": str(generated),
                    "scenarios": ["maintenance_fee"],
                    "verification": {"html": {"verified": True}},
                }

            self.assertTrue(ROOT_HTML.is_file())
            with mock.patch(
                "audit_core.workbench_runtime.run_audit",
                side_effect=fake_run_audit,
            ):
                result = run_persistent_audit(
                    "20260828-persistent-test",
                    producer_model="codex",
                    worktrees_root=root,
                )
            workspace = Path(result["worktree"])
            self.assertTrue((workspace / "snapshot.json").is_file())
            self.assertEqual(result["output"], str(ROOT_HTML))
            self.assertEqual(
                result["workbench_url"],
                f"http://192.0.0.148:8080/?run={workspace.name}",
            )
            self.assertRegex(
                workspace.name,
                r"^20260828_\d{4}_\d{2}-gpt5\.6sol_high$",
            )
            self.assertEqual(result["audit_model"], "gpt-5.6-sol")
            self.assertEqual(result["reasoning_effort"], "high")
            self.assertEqual(list(root.glob("*.html")), [])


class WorkbenchServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        workspace = reserve_workspace("20260828-server", "codex", self.root)
        store = WorkbenchRunStore(
            workspace,
            run_id="20260828-server",
            producer_model="codex",
            root_html=Path("offline-activity-audit.html"),
            workbench_url="http://127.0.0.1:8080/",
        )
        store.write_analysis("facts.json", {"visible": True}, label="结构化事实")
        store.observe(
            "result.validated",
            {
                "scenario": "maintenance_fee",
                "result": {
                    "scenario": "maintenance_fee",
                    "summary": {"conclusion": "supplement"},
                    "maintenance_fee_audit": {
                        "controls": [
                            {
                                "control_id": "pos_visual_seal",
                                "status": "pass",
                                "basis": "盖章POS清晰可见",
                            },
                            {
                                "control_id": "amount_recalculation",
                                "status": "fail",
                                "basis": "金额无法复算",
                            },
                        ]
                    },
                },
            },
        )
        store.complete(
            view_payload=_view_payload(),
            scenarios=["maintenance_fee"],
            verification={"verified": True},
        )
        self.workspace_id = workspace.name
        self.server = WorkbenchHTTPServer(
            ("127.0.0.1", 0),
            Handler,
            catalog=WorkbenchCatalog(self.root),
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temporary.cleanup()

    def _json(self, path: str) -> dict:
        with self.opener.open(self.base + path, timeout=5) as response:
            return json.load(response)

    def _html(self, path: str) -> str:
        with self.opener.open(self.base + path, timeout=5) as response:
            return response.read().decode("utf-8")

    def _post_json(self, path: str, payload: dict) -> dict:
        request = urllib.request.Request(
            self.base + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
            method="POST",
        )
        with self.opener.open(request, timeout=5) as response:
            return json.load(response)

    @staticmethod
    def _script_payload(html: str, element_id: str) -> dict:
        marker = f'<script id="{element_id}" type="application/json">'
        start = html.index(marker) + len(marker)
        end = html.index("</script>", start)
        return json.loads(html[start:end])

    def test_lists_and_loads_persisted_run(self) -> None:
        listing = self._json("/api/runs")
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["runs"][0]["workspace_id"], self.workspace_id)
        self.assertEqual(listing["runs"][0]["audit_model"], "codex")
        self.assertEqual(listing["runs"][0]["reasoning_effort"], "high")
        self.assertFalse(listing["runs"][0]["manual_reviewed"])
        self.assertIsNone(listing["runs"][0]["manual_reviewed_at"])
        self.assertEqual(listing["runs"][0]["main_flow_tasks"], main_flow_task_list())
        snapshot = self._json(f"/api/runs/{self.workspace_id}/snapshot")
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(snapshot["run"]["main_flow_tasks"], main_flow_task_list())
        self.assertEqual(snapshot["view"]["sheets"][0]["audit_counts"]["error_count"], 1)
        self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 1)
        self.assertEqual(snapshot["view"]["pass_check_log"]["groups"][0]["audit_type"], "维护费用")

    def test_manual_review_marker_persists_without_mutating_business_manifest(self) -> None:
        manifest_path = self.root / self.workspace_id / "manifest.json"
        original_manifest = manifest_path.read_bytes()
        marked = self._post_json(
            f"/api/runs/{self.workspace_id}/manual-review",
            {"reviewed": True},
        )
        self.assertTrue(marked["manual_reviewed"])
        self.assertIsNotNone(marked["manual_reviewed_at"])
        review_path = self.root / ".reviews" / f"{self.workspace_id}.json"
        self.assertTrue(review_path.is_file())
        self.assertEqual(manifest_path.read_bytes(), original_manifest)

        listing = self._json("/api/runs")["runs"][0]
        snapshot = self._json(f"/api/runs/{self.workspace_id}/snapshot")
        self.assertTrue(listing["manual_reviewed"])
        self.assertTrue(snapshot["run"]["manual_reviewed"])

        cleared = self._post_json(
            f"/api/runs/{self.workspace_id}/manual-review",
            {"reviewed": False},
        )
        self.assertFalse(cleared["manual_reviewed"])
        self.assertIsNone(cleared["manual_reviewed_at"])
        self.assertFalse(self._json("/api/runs")["runs"][0]["manual_reviewed"])

    def test_manual_review_endpoint_rejects_invalid_or_missing_run(self) -> None:
        for payload in ({}, {"reviewed": "yes"}, {"reviewed": True, "extra": 1}):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                self._post_json(
                    f"/api/runs/{self.workspace_id}/manual-review",
                    payload,
                )
            try:
                self.assertEqual(raised.exception.code, 400)
            finally:
                raised.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self._post_json(
                "/api/runs/20260902_1755_32-gpt5.6sol_xhigh/manual-review",
                {"reviewed": True},
            )
        try:
            self.assertEqual(raised.exception.code, 404)
        finally:
            raised.exception.close()

    def test_root_is_primary_system_and_run_query_is_secondary_record(self) -> None:
        primary = self._html("/")
        primary_context = self._script_payload(primary, "audit-workbench-context")
        self.assertEqual(primary_context["mode"], "system")
        self.assertIsNone(primary_context["selected_run"])
        self.assertEqual(primary_context["system_version"], "2.7.1")
        self.assertEqual(primary_context["main_flow_tasks"], main_flow_task_list())
        self.assertIn("audit-system-extension-script", primary)
        self.assertIn('content="2.7.1"', primary)
        self.assertIn('<link rel="icon" href="data:,">', primary)
        self.assertNotIn("累计持久化运行次数", primary)
        self.assertIn("const latestCompletedZipCount", primary)
        self.assertIn("最近一次 input 的 ZIP 总数", primary)
        self.assertGreaterEqual(primary.count("待人工核验"), 2)
        self.assertIn('id="as-recent-list"', primary)
        self.assertIn('data-as-review=', primary)
        self.assertIn('/manual-review', primary)
        self.assertIn("run.status === 'completed' && !run.manual_reviewed", primary)
        self.assertNotIn('id="as-monitor-list"', primary)
        self.assertNotIn("核销运行链路", primary)
        self.assertNotIn("RUNTIME MONITOR", primary)
        self.assertNotIn("data-runtime-stage", primary)
        self.assertNotIn("renderRuntimeMonitor", primary)
        self.assertNotIn("实时刷新", primary)
        self.assertNotIn("window.location.reload(), 5000", primary)
        self.assertNotIn('data-as-view="monitor"', primary)
        self.assertNotIn('id="as-view-monitor"', primary)

        config = self._json("/api/config")
        self.assertEqual(config["api_version"], "1.7")
        self.assertEqual(config["system_version"], "2.7.1")
        self.assertFalse(config["read_only"])
        self.assertFalse(config["workbench_read_only"])
        self.assertTrue(config["business_results_read_only"])
        self.assertTrue(config["manual_review"]["writable"])
        self.assertEqual(
            config["manual_review"]["endpoint"],
            "/api/runs/{workspace_id}/manual-review",
        )
        self.assertFalse(config["oss_intake"]["enabled"])
        self.assertEqual(config["main_flow_tasks"], main_flow_task_list())
        self.assertEqual(
            config["refresh_policy"]["overview"]["mode"],
            "catalog_signature",
        )
        self.assertEqual(
            config["refresh_policy"]["overview"]["visible_update_rule"],
            "run_catalog_signature_change",
        )
        self.assertEqual(
            config["refresh_policy"]["record"]["mode"],
            "stage_boundary",
        )
        self.assertEqual(
            config["refresh_policy"]["record"]["visible_update_rule"],
            "workspace_status_or_stage_index_change",
        )

        secondary = self._html(f"/?run={self.workspace_id}")
        secondary_context = self._script_payload(
            secondary, "audit-workbench-context"
        )
        self.assertEqual(secondary_context["mode"], "record")
        self.assertEqual(secondary_context["selected_run"], self.workspace_id)
        self.assertEqual(secondary_context["main_flow_tasks"], main_flow_task_list())
        self.assertTrue(secondary_context["view_available"])
        injected = self._script_payload(secondary, "audit-data")
        self.assertEqual(
            injected["sheets"][0]["scenario"],
            "maintenance_fee",
        )
        self.assertIn('id="eoErrorList"', secondary)
        self.assertIn('id="eoErrorType"', secondary)
        self.assertIn('id="eoErrorCategory"', secondary)
        self.assertIn('id="eoErrorConfidence"', secondary)
        self.assertIn('id="eoErrorKeyword"', secondary)
        self.assertIn('id="eoErrorFacetSummary"', secondary)
        self.assertIn('id="eoPassList"', secondary)
        self.assertIn('id="eoPassType"', secondary)
        self.assertIn('id="eoPassCategory"', secondary)
        self.assertIn('id="eoPassConfidence"', secondary)
        self.assertIn('id="eoPassKeyword"', secondary)
        self.assertIn("核销错误结果", secondary)
        self.assertIn("正确检查项日志", secondary)
        self.assertIn("种核销方式", secondary)
        self.assertIn("核销类型 ·", secondary)
        self.assertIn("错误原因分类", secondary)
        self.assertIn('data-error-categories=', secondary)
        self.assertIn('data-error-confidence-score=', secondary)
        self.assertIn('data-pass-confidence-score=', secondary)
        self.assertIn('data-eo-view="passed"', secondary)
        self.assertNotIn("单项通过不等于整单核销通过", secondary)
        self.assertNotIn("核销方式始终展示全部；置信度随核销方式和关键词联动", secondary)
        self.assertNotIn("核销方式始终展示全部；检查分类和置信度只保留当前存在项", secondary)
        self.assertEqual(injected["pass_check_log"]["total"], 1)
        self.assertNotIn('class="eo-scenario-card"', secondary)
        self.assertNotIn('class="eo-enter"', secondary)
        self.assertNotIn("进入错误清单", secondary)
        self.assertNotIn("错误场景队列", secondary)

    def test_serves_analysis_and_rejects_unlisted_path(self) -> None:
        with self.opener.open(
            self.base + f"/api/runs/{self.workspace_id}/analysis?path=analysis%2Ffacts.json",
            timeout=5,
        ) as response:
            self.assertEqual(json.load(response), {"visible": True})
        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.opener.open(
                self.base + f"/api/runs/{self.workspace_id}/analysis?path=..%2Fmanifest.json",
                timeout=5,
            )
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_reads_legacy_embedded_html_without_modifying_it(self) -> None:
        legacy = self.root / "20260827-codex.html"
        legacy.write_text(
            "<!doctype html><html><body>"
            + DATA_OPEN
            + json.dumps(_view_payload(), ensure_ascii=False)
            + DATA_CLOSE
            + "</body></html>",
            encoding="utf-8",
        )
        listing = self._json("/api/runs")
        self.assertEqual(listing["count"], 2)
        snapshot = self._json("/api/runs/20260827-codex/snapshot")
        self.assertEqual(snapshot["run"]["storage_type"], "legacy_html")
        self.assertEqual(snapshot["run"]["error_count"], 1)
        self.assertEqual(snapshot["run"]["main_flow_tasks"], main_flow_task_list())

    @unittest.skipUnless(os.name == "nt", "Windows requires exclusive workbench ports")
    def test_windows_rejects_a_second_server_on_the_same_port(self) -> None:
        first = WorkbenchHTTPServer(
            ("127.0.0.1", 0),
            Handler,
            catalog=WorkbenchCatalog(self.root / "exclusive-first"),
        )
        try:
            port = int(first.server_address[1])
            with self.assertRaises(OSError):
                WorkbenchHTTPServer(
                    ("0.0.0.0", port),
                    Handler,
                    catalog=WorkbenchCatalog(self.root / "exclusive-second"),
                )
        finally:
            first.server_close()


if __name__ == "__main__":
    unittest.main()
