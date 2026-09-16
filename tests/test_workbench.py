from __future__ import annotations

import gzip
import hashlib
import http.client
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote
from unittest import mock

from audit_core.analysis_summary_backfill import backfill_analysis_summaries
from audit_core import workbench_server
from audit_core.html_report import DATA_CLOSE, DATA_OPEN
from audit_core.common import AuditError
from audit_core.orchestrator import run_audit
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import (
    ANALYSIS_SUMMARY_FILENAME,
    WorkbenchRunStore,
    atomic_write_json,
    main_flow_task_list,
    read_json_file,
    render_analysis_summary_markdown,
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
    def test_customer_projection_count_requires_current_matching_completed_snapshot(self) -> None:
        from audit_core.workbench_html import SYSTEM_VERSION, customer_projection_error_count

        manifest = {"status": "completed", "error_count": 4, "snapshot_sha256": "a" * 64,
                    "customer_projection": {"schema_version": "1.0", "policy_version": SYSTEM_VERSION,
                        "source_snapshot_sha256": "a" * 64, "error_count": 3}}
        self.assertEqual(customer_projection_error_count(manifest), 3)
        self.assertEqual(manifest["error_count"], 4)
        for change in ({"schema_version": "0.9"}, {"policy_version": "old"},
                       {"source_snapshot_sha256": "b" * 64}, {"error_count": -1},
                       {"error_count": True}, {"error_count": "3"}):
            with self.subTest(change=change):
                value = dict(manifest, customer_projection={**manifest["customer_projection"], **change})
                self.assertEqual(customer_projection_error_count(value), 4)
        self.assertEqual(customer_projection_error_count(dict(manifest, status="running")), 4)

    def test_catalog_derived_count_refreshes_manifest_signature_without_reading_snapshot(self) -> None:
        from audit_core.workbench_html import SYSTEM_VERSION

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "20260911-customer-projection"
            workspace.mkdir()
            manifest = {"workspace_id": workspace.name, "run_id": workspace.name,
                        "status": "completed", "error_count": 4, "snapshot_sha256": "a" * 64,
                        "source_archives": ["维护费用.zip"], "analysis_started_at": "2026-09-11T00:00:00Z",
                        "customer_projection": {"schema_version": "1.0", "policy_version": SYSTEM_VERSION,
                            "source_snapshot_sha256": "a" * 64, "error_count": 3}}
            atomic_write_json(workspace / "manifest.json", manifest)
            catalog = WorkbenchCatalog(Path(temporary))
            with mock.patch("audit_core.workbench_server.read_json_file", wraps=read_json_file) as reader:
                first, first_etag = catalog.catalog_document()
                second, second_etag = catalog.catalog_document()
                self.assertEqual(first, second)
                self.assertEqual(first_etag, second_etag)
                self.assertEqual(json.loads(first)["runs"][0]["error_count"], 3)
                self.assertEqual(reader.call_count, 1)
                manifest["customer_projection"]["error_count"] = 2
                atomic_write_json(workspace / "manifest.json", manifest)
                refreshed, refreshed_etag = catalog.catalog_document()
                self.assertEqual(json.loads(refreshed)["runs"][0]["error_count"], 2)
                self.assertNotEqual(first_etag, refreshed_etag)
                self.assertEqual(reader.call_count, 2)
                self.assertTrue(all(Path(call.args[0]).name == "manifest.json" for call in reader.call_args_list))
            self.assertEqual(read_json_file(workspace / "manifest.json")["error_count"], 4)

    def test_saved_nanxiong_count_projects_three_without_rewriting_four_original_errors(self) -> None:
        from audit_core.workbench_html import SYSTEM_VERSION, render_static_run_archive_html

        source = ROOT_HTML.parent / "worktrees/HX202601160053-广州南雄维护费用申请-9-20260910_1756_59"
        if not (source / "snapshot.json").is_file():
            self.skipTest("真实南雄历史快照不在本地")
        originals = {name: (source / name).read_bytes() for name in ("manifest.json", "snapshot.json")}
        original_snapshot = json.loads(originals["snapshot.json"])
        original_manifest = json.loads(originals["manifest.json"])
        self.assertEqual(original_manifest["error_count"], 4)
        self.assertEqual(original_snapshot["view"]["sheets"][0]["audit_counts"]["error_count"], 4)
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / source.name
            workspace.mkdir()
            snapshot = {**original_snapshot, "analysis_files": [], "dom_checkpoints": []}
            atomic_write_json(workspace / "snapshot.json", snapshot)
            snapshot_bytes = (workspace / "snapshot.json").read_bytes()
            digest = hashlib.sha256(snapshot_bytes).hexdigest()
            manifest = {**original_manifest, "snapshot_sha256": digest,
                        "customer_projection": {"schema_version": "1.0", "policy_version": SYSTEM_VERSION,
                            "source_snapshot_sha256": digest, "error_count": 3}}
            atomic_write_json(workspace / "manifest.json", manifest)
            before = {name: (workspace / name).read_bytes() for name in ("manifest.json", "snapshot.json")}
            catalog = WorkbenchCatalog(Path(temporary))
            self.assertEqual(catalog.list_runs()[0]["error_count"], 3)
            with mock.patch("audit_core.workbench_server.attach_error_reasons", wraps=workbench_server.attach_error_reasons) as project:
                first, first_etag = catalog.snapshot_document(workspace.name)
                second, second_etag = catalog.snapshot_document(workspace.name)
                self.assertEqual((first, first_etag), (second, second_etag))
                self.assertEqual(project.call_count, 1)
            projected = json.loads(first)
            self.assertEqual(projected["run"]["error_count"], 3)
            self.assertEqual(len(projected["view"]["sheets"][0]["rows"]), 3)
            html = render_static_run_archive_html(workspace, ROOT_HTML, manifest=manifest, snapshot=snapshot)
            context = WorkbenchServerTests._script_payload(html, "audit-workbench-context")
            archive = WorkbenchServerTests._script_payload(html, "audit-static-archive")
            self.assertEqual(context["run"]["error_count"], 3)
            self.assertEqual(context["static_catalog"]["runs"][0]["error_count"], 3)
            self.assertEqual(archive["catalog"]["runs"][0]["error_count"], 3)
            self.assertEqual(archive["snapshot"]["run"]["error_count"], 3)
            self.assertFalse((workspace / "offline-activity-audit.html").exists())
            self.assertEqual(manifest["error_count"], 4)
            self.assertEqual(snapshot["view"]["sheets"][0]["audit_counts"]["error_count"], 4)
            self.assertEqual({name: (workspace / name).read_bytes() for name in before}, before)
        self.assertEqual({name: (source / name).read_bytes() for name in originals}, originals)

    def test_archive_named_workspaces_keep_unicode_and_never_overwrite(self) -> None:
        name = "HX202606040013-核销资料-诚成26年4月【堆头20家】.zip"
        with tempfile.TemporaryDirectory() as temporary:
            args = dict(archive_names=[name], started_at=datetime(2026, 9, 10, 9, 12, 20))
            first = reserve_workspace("20260827-one", "codex", temporary, **args)
            (first / "keep.txt").write_text("original")
            second = reserve_workspace("20260827-two", "codex", temporary, **args)
            self.assertEqual(first.name, name[:-4] + "-20260910_0912_20")
            self.assertEqual(second.name, first.name + "-02")
            self.assertEqual((first / "keep.txt").read_text(), "original")
            self.assertEqual(workbench_server._safe_workspace_id(quote(first.name, safe="")), first.name)
            unsafe = reserve_workspace("20260827-three", "codex", temporary, archive_names=['../../CON:<evil>?%.zip'])
            self.assertEqual(unsafe.parent, Path(temporary).resolve())
            self.assertNotIn("%", unsafe.name)
            for value in ("../资料", "资料/..", "资料\\..", "%2e%2e%2f资料", ".隐藏", "CON", "资料."):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    workbench_server._safe_workspace_id(value)

    def test_record_labels_read_saved_sources_and_analysis_start_without_rewriting(self) -> None:
        from audit_core.workbench_labels import record_labels

        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            cases_path = workspace / "analysis/input-cases.json"
            events_path = workspace / "logs/events.jsonl"
            atomic_write_json(cases_path, {"promotional_display": {"source_archive": "HX-堆头20家.zip"}})
            events_path.parent.mkdir()
            events_path.write_text('{"type":"scenario.started","timestamp":"2026-09-09T16:01:00+00:00"}\n', encoding="utf-8")
            before = {path: path.read_bytes() for path in (cases_path, events_path)}
            manifest = {"workspace_id": "legacy", "run_id": "20260827-oss-123456abcdef"}
            labels = record_labels(manifest, workspace)
            self.assertEqual(labels, {"source_archives": ["HX-堆头20家.zip"], "display_name": "HX-堆头20家", "analysis_started_at": "2026-09-09T16:01:00+00:00", "input_source": "oss"})
            self.assertEqual(record_labels(dict(manifest, input_source="input"), workspace)["input_source"], "input")
            self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_selected_scenario_names_workspace_for_its_archive(self) -> None:
        from audit_core.workbench_runtime import _input_archive_names

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ("A-其他.zip", "B-堆头.zip"):
                (root / name).touch()
            with mock.patch("audit_core.workbench_runtime.discover_archives", return_value={"promotional_display": {"path": root / "B-堆头.zip"}}):
                self.assertEqual(_input_archive_names(root, "promotional_display"), ["B-堆头.zip"])
            self.assertEqual(_input_archive_names(root, None), ["A-其他.zip", "B-堆头.zip"])

    def test_analysis_start_uses_first_analysis_event_and_survives_completion(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "20260910_0000_00-start"
            workspace.mkdir()
            store = WorkbenchRunStore(
                workspace, run_id="20260827-start", producer_model="codex",
                root_html=ROOT_HTML, workbench_url="http://127.0.0.1/",
            )
            self.assertIsNone(store.manifest["analysis_started_at"])
            with mock.patch("audit_core.workbench_store.utc_now", return_value="2026-09-09T16:01:00+00:00"):
                store.observe("scenario.started", {"scenario": "maintenance_fee"})
            with mock.patch("audit_core.workbench_store.utc_now", return_value="2026-09-10T03:00:00+00:00"):
                store.observe("scenario.started", {"scenario": "entry_fee"})
            store.complete(view_payload=_view_payload(), scenarios=["maintenance_fee"], verification={})
            manifest = read_json_file(workspace / "manifest.json")
            self.assertEqual(manifest["analysis_started_at"], "2026-09-09T16:01:00+00:00")
            self.assertEqual(manifest["business_date"], "20260827")
            self.assertEqual(WorkbenchCatalog(workspace.parent).list_runs()[0]["analysis_started_at"], manifest["analysis_started_at"])

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
            # The run ID keeps its business date, while the directory uses the
            # actual Shanghai time at which the run is claimed/reserved.
            started_at = datetime(2026, 9, 9, 3, 50, 30, tzinfo=timezone.utc)
            workspace = reserve_workspace(
                "20260115-example",
                "codex",
                root,
                audit_model="gpt-6-astra",
                reasoning_effort="high",
                started_at=started_at,
            )
            self.assertEqual(workspace.name, "20260909_1150_30-gpt6astra_high")
            with self.assertRaisesRegex(AuditError, "同一秒"):
                reserve_workspace(
                    "20260115-example",
                    "codex",
                    root,
                    audit_model="gpt-6-astra",
                    reasoning_effort="high",
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

    def test_analysis_summary_formats_concise_error_facts_by_scenario(self) -> None:
        view_payload = {
            "sheets": [
                {
                    "name": "人员激励核销",
                    "rows": [
                        {
                            "status": "issue",
                            "heading": "实际申请金额",
                            "values": [
                                "错误原因：结算单申请金额为7350元，转账金额为7353元，申请金额比转账金额少3元。",
                                "置信度：低\n要重新提交：更正结算单。",
                            ],
                        },
                        {
                            "status": "issue",
                            "heading": "收款人与日期",
                            "values": [
                                "错误原因：销售Excel读取到7家门店；转账截图未完整显示收款人、门店和日期。",
                                "要重新提交：补拍截图。",
                            ],
                        },
                    ],
                },
                {
                    "name": "堆头核销",
                    "rows": [
                        {
                            "status": "issue",
                            "heading": "合同销售附件第1行｜PDF第3页",
                            "values": [
                                "商品编码：不匹配（知识库未登记合同业务编码）；条形码：精确匹配",
                                "要重新提交：核对商品编码。",
                            ],
                        },
                        {
                            "status": "issue",
                            "heading": "示例门店一",
                            "values": [
                                "结论：暂不能核销 主要问题：现场商品与合同 要重新提交：补拍照片"
                            ],
                        },
                        {
                            "status": "issue",
                            "heading": "示例门店二",
                            "values": [
                                "主要问题：活动日期、门店水印缺失或无法核对、陈列标准、现场商品与合同\n"
                                "要重新提交：补拍照片"
                            ],
                        },
                    ],
                },
            ]
        }
        summary = render_analysis_summary_markdown(
            view_payload,
            {"scenario_count": 1, "error_count": 2},
        )
        self.assertEqual(
            summary,
            "## 人员激励核销\n\n"
            "- 结算单申请金额为7350元，转账金额为7353元，申请金额比转账金额少3元。\n"
            "- 转账截图未完整显示收款人、门店和日期。\n\n"
            "## 堆头核销\n\n"
            "- 合同第3页第1行：合同商品编码未在商品资料中登记。\n"
            "- 示例门店二：活动日期无法核验。\n"
            "- 示例门店二：门店水印缺失或无法核对。\n"
            "- 示例门店二：陈列标准无法核验。",
        )
        self.assertNotIn("场景", summary)
        self.assertNotIn("错误项", summary)
        self.assertIn("7350", summary)
        self.assertIn("7353", summary)
        self.assertNotIn("另有", summary)
        self.assertNotIn("置信度", summary)
        self.assertNotIn("重新提交", summary)

    def test_analysis_summary_does_not_truncate_distinct_error_types(self) -> None:
        error_types = [f"错误类型{index}缺失" for index in range(1, 13)]
        summary = render_analysis_summary_markdown(
            {
                "sheets": [
                    {
                        "name": "维护费用核销",
                        "rows": [
                            {
                                "status": "issue",
                                "heading": f"问题：{error_type}",
                                "values": [],
                            }
                            for error_type in error_types
                        ],
                    }
                ]
            },
            {"scenario_count": 1, "error_count": len(error_types)},
        )
        for error_type in error_types:
            self.assertIn(error_type, summary)
        self.assertNotIn("另有", summary)
        self.assertNotIn("…", summary)

    def test_analysis_summary_without_errors_only_reports_no_error(self) -> None:
        summary = render_analysis_summary_markdown(
            {"sheets": [{"name": "人员激励核销", "rows": []}]},
            {"scenario_count": 1, "error_count": 0},
        )
        self.assertEqual(summary, "未发现错误。")

    def test_completed_worktree_summaries_can_be_backfilled_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "20260908_1700_00-gpt5.6sol_high"
            workspace.mkdir()
            summary_path = workspace / ANALYSIS_SUMMARY_FILENAME
            summary_path.write_text("# 旧版长小结\n", encoding="utf-8")
            manifest = {
                "workspace_id": workspace.name,
                "status": "completed",
                "scenario_count": 1,
                "error_count": 1,
                "snapshot_sha256": "stale",
                "analysis_summary": {
                    "path": ANALYSIS_SUMMARY_FILENAME,
                    "size": summary_path.stat().st_size,
                    "sha256": hashlib.sha256(summary_path.read_bytes()).hexdigest(),
                },
            }
            atomic_write_json(workspace / "manifest.json", manifest)
            atomic_write_json(
                workspace / "snapshot.json",
                {
                    "schema_version": "1.0",
                    "run": dict(manifest),
                    "view": _view_payload(),
                    "verification": {},
                    "analysis_files": [],
                    "dom_checkpoints": [],
                },
            )

            dry_run = backfill_analysis_summaries(root)
            self.assertEqual(dry_run["would_update"], 1)
            self.assertEqual(summary_path.read_text(encoding="utf-8"), "# 旧版长小结\n")

            applied = backfill_analysis_summaries(root, apply=True)
            self.assertEqual(applied["updated"], 1)
            summary = summary_path.read_text(encoding="utf-8")
            self.assertEqual(
                summary,
                "## 维护费用核销\n\n- 未提交电子表。",
            )
            self.assertNotIn("置信度", summary)
            self.assertNotIn("重新提交", summary)
            updated_manifest = read_json_file(workspace / "manifest.json")
            updated_snapshot = read_json_file(workspace / "snapshot.json")
            metadata = updated_manifest["analysis_summary"]
            self.assertEqual(metadata["size"], summary_path.stat().st_size)
            self.assertEqual(
                metadata["sha256"],
                hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(updated_snapshot["run"]["analysis_summary"], metadata)
            self.assertEqual(
                updated_manifest["snapshot_sha256"],
                hashlib.sha256((workspace / "snapshot.json").read_bytes()).hexdigest(),
            )

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
            self.assertEqual(manifest["reasoning_effort"], "medium")
            self.assertEqual(manifest["status"], "completed")
            self.assertEqual(manifest["error_count"], 1)
            self.assertEqual(
                manifest["analysis_summary"]["path"],
                ANALYSIS_SUMMARY_FILENAME,
            )
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
            summary_path = workspace / ANALYSIS_SUMMARY_FILENAME
            summary = summary_path.read_text(encoding="utf-8")
            self.assertEqual(
                summary,
                "## 维护费用核销\n\n- 未提交电子表。",
            )
            self.assertEqual(
                manifest["analysis_summary"]["sha256"],
                hashlib.sha256(summary_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                snapshot["run"]["analysis_summary"],
                manifest["analysis_summary"],
            )
            events = (workspace / "logs" / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn("run.completed", events)

    def test_persistent_runner_publishes_self_contained_html_in_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            archive_name = "HX202606040013-核销资料-诚成26年4月【堆头20家】.zip"
            (inputs / archive_name).touch()
            original_root_html = ROOT_HTML.read_bytes()

            def fake_run_audit(*args, **kwargs):  # type: ignore[no-untyped-def]
                observer = kwargs["observer"]
                observer(
                    "cases.prepared",
                    {
                        "scenarios": ["maintenance_fee"],
                        "cases": {"maintenance_fee": {"scenario": "maintenance_fee", "source_archive": archive_name}},
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
                    input_dir=inputs,
                )
            workspace = Path(result["worktree"])
            self.assertTrue((workspace / "snapshot.json").is_file())
            static_html = workspace / "offline-activity-audit.html"
            self.assertTrue(static_html.is_file())
            self.assertEqual(result["output"], str(static_html))
            self.assertEqual(result["static_html"], str(static_html))
            self.assertEqual(result["system_html"], str(ROOT_HTML))
            self.assertEqual(
                result["analysis_summary"],
                str(workspace / ANALYSIS_SUMMARY_FILENAME),
            )
            self.assertTrue(Path(result["analysis_summary"]).is_file())
            self.assertEqual(
                result["workbench_url"],
                f"http://192.0.0.148:8080/?run={quote(workspace.name, safe='')}",
            )
            self.assertRegex(
                workspace.name,
                r"^HX202606040013-核销资料-诚成26年4月【堆头20家】-\d{8}_\d{4}_\d{2}$",
            )
            self.assertEqual(result["audit_model"], "gpt-6-astra")
            self.assertEqual(result["reasoning_effort"], "medium")
            self.assertEqual(list(root.glob("*.html")), [])
            self.assertEqual(
                list(root.glob("*/offline-activity-audit.html")),
                [static_html],
            )
            self.assertEqual(ROOT_HTML.read_bytes(), original_root_html)

            html = static_html.read_text(encoding="utf-8")
            context = WorkbenchServerTests._script_payload(
                html, "audit-workbench-context"
            )
            self.assertEqual(context["delivery_mode"], "static_archive")
            self.assertEqual(context["mode"], "system")
            self.assertEqual(context["static_catalog"]["count"], 1)
            self.assertEqual(context["selected_run"], workspace.name)
            self.assertTrue(context["view_available"])
            self.assertNotIn("static_archive", context)
            archive = WorkbenchServerTests._script_payload(
                html, "audit-static-archive"
            )
            self.assertEqual(archive["catalog"]["count"], 1)
            self.assertIsNone(archive["snapshot"]["view"])
            self.assertEqual(
                WorkbenchServerTests._script_payload(html, "audit-data")["sheets"][
                    0
                ]["scenario"],
                "maintenance_fee",
            )
            self.assertIn(
                "analysis/results/maintenance_fee.json",
                archive["analysis"],
            )
            self.assertIn(" INFO  complete ", archive["log"])
            self.assertIn(
                '<meta name="offline-audit-page-mode" content="system">',
                html,
            )
            self.assertIn(
                '<meta name="offline-audit-delivery-mode" content="static_archive">',
                html,
            )
            self.assertIn(
                '<meta name="offline-audit-view-available" content="true">',
                html,
            )
            self.assertIn('id="eoPassList"></div>', html)
            self.assertIn("const initializePassLedger = () => {", html)
            self.assertIn(
                "if (viewId === 'passed') initializePassLedger();",
                html,
            )
            self.assertIn("const embeddedStaticArchive = () => {", html)
            self.assertIn("deliveryMode !== 'static_archive'", html)

    def test_failed_persistent_run_still_publishes_diagnostic_static_html(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            with (
                mock.patch(
                    "audit_core.workbench_runtime.run_audit",
                    side_effect=AuditError("测试运行失败"),
                ),
                self.assertRaisesRegex(AuditError, "测试运行失败"),
            ):
                run_persistent_audit(
                    "20260828-failed-static-test",
                    producer_model="codex",
                    worktrees_root=root,
                )

            workspaces = [path for path in root.iterdir() if path.is_dir()]
            self.assertEqual(len(workspaces), 1)
            static_html = workspaces[0] / "offline-activity-audit.html"
            self.assertTrue(static_html.is_file())
            html = static_html.read_text(encoding="utf-8")
            context = WorkbenchServerTests._script_payload(
                html, "audit-workbench-context"
            )
            self.assertEqual(context["delivery_mode"], "static_archive")
            self.assertFalse(context["view_available"])
            self.assertNotIn("static_archive", context)
            self.assertEqual(context["run"]["status"], "failed")
            self.assertEqual(context["run"]["failure"]["message"], "测试运行失败")
            self.assertIn(
                '<meta name="offline-audit-view-available" content="false">',
                html,
            )
            archive = WorkbenchServerTests._script_payload(
                html, "audit-static-archive"
            )
            self.assertEqual(archive["catalog"]["count"], 1)
            self.assertIsNone(archive["snapshot"]["view"])


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
        self.assertEqual(listing["runs"][0]["reasoning_effort"], "medium")
        self.assertFalse(listing["runs"][0]["manual_reviewed"])
        self.assertIsNone(listing["runs"][0]["manual_reviewed_at"])
        self.assertEqual(listing["runs"][0]["main_flow_tasks"], main_flow_task_list())
        snapshot = self._json(f"/api/runs/{self.workspace_id}/snapshot")
        self.assertEqual(snapshot["run"]["status"], "completed")
        self.assertEqual(snapshot["run"]["main_flow_tasks"], main_flow_task_list())
        self.assertEqual(snapshot["view"]["sheets"][0]["audit_counts"]["error_count"], 1)
        self.assertEqual(snapshot["view"]["pass_check_log"]["total"], 1)
        self.assertEqual(snapshot["view"]["pass_check_log"]["groups"][0]["audit_type"], "维护费用")

    def test_catalog_reuses_unchanged_manifests_and_refreshes_changed_one(self) -> None:
        catalog = self.server.catalog
        manifest_path = self.root / self.workspace_id / "manifest.json"
        original_reader = workbench_server.read_json_file
        with mock.patch(
            "audit_core.workbench_server.read_json_file",
            wraps=original_reader,
        ) as reader:
            first = catalog.list_runs()
            first_manifest_reads = sum(
                Path(call.args[0]).name == "manifest.json"
                for call in reader.call_args_list
            )
            second = catalog.list_runs()
            second_manifest_reads = sum(
                Path(call.args[0]).name == "manifest.json"
                for call in reader.call_args_list
            )

            self.assertEqual(first, second)
            self.assertEqual(first_manifest_reads, 1)
            self.assertEqual(second_manifest_reads, first_manifest_reads)

            manifest = original_reader(manifest_path)
            manifest["error_count"] = 17
            atomic_write_json(manifest_path, manifest)
            refreshed = catalog.list_runs()
            refreshed_manifest_reads = sum(
                Path(call.args[0]).name == "manifest.json"
                for call in reader.call_args_list
            )

        self.assertEqual(refreshed[0]["error_count"], 17)
        self.assertEqual(refreshed_manifest_reads, first_manifest_reads + 1)

    def test_catalog_bounds_full_manifests_without_reloading_stable_summaries(
        self,
    ) -> None:
        source = read_json_file(self.root / self.workspace_id / "manifest.json")
        for index in range(5):
            workspace_id = f"2026082{index}_1200_00-codex_high"
            workspace = self.root / workspace_id
            workspace.mkdir()
            manifest = dict(source)
            manifest["workspace_id"] = workspace_id
            manifest["run_id"] = f"bounded-{index}"
            manifest["business_date"] = f"2026082{index}"
            atomic_write_json(workspace / "manifest.json", manifest)

        catalog = WorkbenchCatalog(self.root)
        original_reader = workbench_server.read_json_file
        with (
            mock.patch(
                "audit_core.workbench_server.MAX_CACHED_MANIFESTS",
                2,
            ),
            mock.patch(
                "audit_core.workbench_server.read_json_file",
                wraps=original_reader,
            ) as reader,
        ):
            first, first_etag = catalog.catalog_document()
            first_manifest_reads = sum(
                Path(call.args[0]).name == "manifest.json"
                for call in reader.call_args_list
            )
            second, second_etag = catalog.catalog_document()
            second_manifest_reads = sum(
                Path(call.args[0]).name == "manifest.json"
                for call in reader.call_args_list
            )

        self.assertEqual(first, second)
        self.assertEqual(first_etag, second_etag)
        self.assertEqual(first_manifest_reads, 6)
        self.assertEqual(second_manifest_reads, first_manifest_reads)
        self.assertEqual(len(catalog._manifest_cache), 2)
        self.assertEqual(len(catalog._run_summary_cache), 6)

    def test_catalog_review_enumeration_detects_external_add_and_remove(self) -> None:
        catalog = WorkbenchCatalog(self.root)
        with (
            mock.patch.object(
                catalog,
                "_catalog_review_metadata",
                wraps=catalog._catalog_review_metadata,
            ) as review_scan,
            mock.patch.object(
                catalog,
                "_review_state",
                wraps=catalog._review_state,
            ) as review_reader,
        ):
            initial, initial_etag = catalog.catalog_document()
            self.assertFalse(json.loads(initial)["runs"][0]["manual_reviewed"])

            review_path = self.root / ".reviews" / f"{self.workspace_id}.json"
            atomic_write_json(
                review_path,
                {
                    "schema_version": "1.0",
                    "workspace_id": self.workspace_id,
                    "reviewed": True,
                    "reviewed_at": "2026-08-29T00:00:00+00:00",
                    "updated_at": "2026-08-29T00:00:00+00:00",
                },
            )
            reviewed, reviewed_etag = catalog.catalog_document()
            self.assertTrue(json.loads(reviewed)["runs"][0]["manual_reviewed"])
            self.assertNotEqual(reviewed_etag, initial_etag)

            review_path.unlink()
            removed, removed_etag = catalog.catalog_document()

        self.assertFalse(json.loads(removed)["runs"][0]["manual_reviewed"])
        self.assertNotEqual(removed_etag, reviewed_etag)
        self.assertEqual(review_scan.call_count, 3)
        self.assertTrue(
            all("metadata" in call.kwargs for call in review_reader.call_args_list)
        )
        self.assertFalse(catalog._review_cache)

    def test_catalog_review_scan_falls_back_to_point_lookup(self) -> None:
        catalog = WorkbenchCatalog(self.root)
        atomic_write_json(
            self.root / ".reviews" / f"{self.workspace_id}.json",
            {
                "schema_version": "1.0",
                "workspace_id": self.workspace_id,
                "reviewed": True,
                "reviewed_at": "2026-08-29T00:00:00+00:00",
                "updated_at": "2026-08-29T00:00:00+00:00",
            },
        )
        original_scandir = os.scandir

        def scandir(path: str | os.PathLike[str]):
            if Path(path) == catalog.review_root:
                raise OSError("review directory temporarily unavailable")
            return original_scandir(path)

        with mock.patch(
            "audit_core.workbench_server.os.scandir",
            side_effect=scandir,
        ):
            encoded, _ = catalog.catalog_document()

        self.assertTrue(json.loads(encoded)["runs"][0]["manual_reviewed"])

    def test_catalog_document_reuses_serialization_until_source_signature_changes(
        self,
    ) -> None:
        catalog = self.server.catalog
        manifest_path = self.root / self.workspace_id / "manifest.json"
        with mock.patch.object(
            catalog,
            "_encode_catalog",
            wraps=catalog._encode_catalog,
        ) as serializer:
            first, first_etag = catalog.catalog_document()
            second, second_etag = catalog.catalog_document()
            self.assertEqual(first, second)
            self.assertEqual(first_etag, second_etag)
            self.assertEqual(serializer.call_count, 1)

            manifest = read_json_file(manifest_path)
            manifest["error_count"] = 17
            atomic_write_json(manifest_path, manifest)
            changed, changed_etag = catalog.catalog_document()
            self.assertEqual(serializer.call_count, 2)
            self.assertNotEqual(changed_etag, first_etag)
            self.assertEqual(json.loads(changed)["runs"][0]["error_count"], 17)

            catalog.set_manual_review(self.workspace_id, True)
            reviewed, reviewed_etag = catalog.catalog_document()
            self.assertEqual(serializer.call_count, 3)
            self.assertNotEqual(reviewed_etag, changed_etag)
            self.assertTrue(json.loads(reviewed)["runs"][0]["manual_reviewed"])

    def test_concurrent_catalog_reads_coalesce_document_serialization(self) -> None:
        catalog = self.server.catalog
        worker_count = 12
        barrier = threading.Barrier(worker_count)

        def read_document() -> tuple[bytes, str]:
            barrier.wait(timeout=5)
            return catalog.catalog_document()

        with mock.patch.object(
            catalog,
            "_encode_catalog",
            wraps=catalog._encode_catalog,
        ) as serializer:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                documents = list(
                    executor.map(lambda _: read_document(), range(worker_count))
                )

        self.assertTrue(all(document == documents[0] for document in documents))
        self.assertEqual(serializer.call_count, 1)

    def test_overlapping_catalog_reads_use_a_post_arrival_generation(self) -> None:
        catalog = self.server.catalog
        manifest_path = self.root / self.workspace_id / "manifest.json"
        original_scan = catalog._catalog_state_once
        first_scanned = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        scan_count = 0

        def controlled_scan():
            nonlocal scan_count
            with call_lock:
                scan_count += 1
                current_scan = scan_count
            result = original_scan()
            if current_scan == 1:
                first_scanned.set()
                if not release_first.wait(timeout=5):
                    raise TimeoutError("first catalog generation was not released")
            return result

        follower_count = 7
        with mock.patch.object(
            catalog,
            "_catalog_state_once",
            side_effect=controlled_scan,
        ):
            with ThreadPoolExecutor(max_workers=follower_count + 1) as executor:
                first_future = executor.submit(catalog.catalog_document)
                self.assertTrue(first_scanned.wait(timeout=5))
                follower_futures = [
                    executor.submit(catalog.catalog_document)
                    for _ in range(follower_count)
                ]
                deadline = time.monotonic() + 5
                while True:
                    with catalog._catalog_batch_condition:
                        participants = catalog._catalog_generation_participants.get(
                            2,
                            0,
                        )
                    if participants == follower_count:
                        break
                    if time.monotonic() >= deadline:
                        release_first.set()
                        self.fail("followers did not join the next catalog generation")
                    time.sleep(0.01)

                manifest = read_json_file(manifest_path)
                original_error_count = manifest["error_count"]
                manifest["error_count"] = 17
                atomic_write_json(manifest_path, manifest)
                release_first.set()
                first = first_future.result(timeout=5)
                followers = [future.result(timeout=5) for future in follower_futures]

        self.assertEqual(scan_count, 2)
        self.assertTrue(all(document == followers[0] for document in followers))
        self.assertEqual(
            json.loads(first[0])["runs"][0]["error_count"],
            original_error_count,
        )
        self.assertEqual(json.loads(followers[0][0])["runs"][0]["error_count"], 17)
        self.assertFalse(catalog._catalog_generation_participants)
        self.assertFalse(catalog._catalog_generation_outcomes)

    def test_catalog_generation_recovers_after_leader_failure(self) -> None:
        catalog = self.server.catalog
        original_scan = catalog._catalog_state_once
        scan_count = 0

        def fail_once():
            nonlocal scan_count
            scan_count += 1
            if scan_count == 1:
                raise OSError("temporary catalog scan failure")
            return original_scan()

        with mock.patch.object(
            catalog,
            "_catalog_state_once",
            side_effect=fail_once,
        ):
            with self.assertRaisesRegex(OSError, "temporary catalog scan failure"):
                catalog.catalog_document()
            encoded, _ = catalog.catalog_document()

        self.assertEqual(json.loads(encoded)["count"], 1)
        self.assertEqual(scan_count, 2)
        self.assertIsNone(catalog._catalog_active_generation)
        self.assertFalse(catalog._catalog_generation_participants)
        self.assertFalse(catalog._catalog_generation_outcomes)

    def test_snapshot_document_reuses_serialization_until_sources_change(
        self,
    ) -> None:
        catalog = self.server.catalog
        expected = json.dumps(
            catalog.snapshot(self.workspace_id),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        events_path = self.root / self.workspace_id / "logs" / "events.jsonl"
        with mock.patch.object(
            catalog,
            "_encode_snapshot",
            wraps=catalog._encode_snapshot,
        ) as serializer:
            first, first_etag = catalog.snapshot_document(self.workspace_id)
            second, second_etag = catalog.snapshot_document(self.workspace_id)
            self.assertEqual(first, expected)
            self.assertEqual(first, second)
            self.assertEqual(first_etag, hashlib.sha256(first).hexdigest())
            self.assertEqual(first_etag, second_etag)
            self.assertEqual(serializer.call_count, 1)

            with events_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(
                        {
                            "seq": 999,
                            "timestamp": "2026-09-04T00:00:00+00:00",
                            "type": "test.refresh",
                            "stage": "verification",
                            "message": "快照签名刷新",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            changed, changed_etag = catalog.snapshot_document(self.workspace_id)
            self.assertEqual(serializer.call_count, 2)
            self.assertNotEqual(changed_etag, first_etag)
            self.assertEqual(json.loads(changed)["recent_events"][-1]["seq"], 999)

            catalog.set_manual_review(self.workspace_id, True)
            reviewed, reviewed_etag = catalog.snapshot_document(self.workspace_id)
            self.assertEqual(serializer.call_count, 3)
            self.assertNotEqual(reviewed_etag, changed_etag)
            self.assertTrue(json.loads(reviewed)["run"]["manual_reviewed"])

    def test_concurrent_snapshot_reads_coalesce_document_serialization(self) -> None:
        catalog = self.server.catalog
        worker_count = 12
        barrier = threading.Barrier(worker_count)

        def read_document() -> tuple[bytes, str]:
            barrier.wait(timeout=5)
            return catalog.snapshot_document(self.workspace_id)

        with mock.patch.object(
            catalog,
            "_encode_snapshot",
            wraps=catalog._encode_snapshot,
        ) as serializer:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                documents = list(
                    executor.map(lambda _: read_document(), range(worker_count))
                )

        self.assertTrue(all(document == documents[0] for document in documents))
        self.assertEqual(serializer.call_count, 1)

    def test_snapshot_document_cache_has_fixed_count_and_byte_bounds(self) -> None:
        catalog = WorkbenchCatalog(self.root)

        def signature(workspace_id: str) -> tuple[str, str]:
            return ("test", workspace_id)

        def snapshot(workspace_id: str) -> dict[str, str]:
            return {"workspace_id": workspace_id, "payload": "x" * 80}

        with (
            mock.patch.object(catalog, "html_signature", side_effect=signature),
            mock.patch.object(catalog, "snapshot", side_effect=snapshot),
            mock.patch(
                "audit_core.workbench_server.MAX_CACHED_SNAPSHOT_DOCUMENTS",
                2,
            ),
            mock.patch(
                "audit_core.workbench_server.MAX_CACHED_SNAPSHOT_BYTES",
                1024,
            ),
        ):
            for workspace_id in ("first", "second", "third"):
                catalog.snapshot_document(workspace_id)

        self.assertEqual(list(catalog._snapshot_document_cache), ["second", "third"])
        self.assertEqual(
            catalog._snapshot_document_cache_bytes,
            sum(
                len(cached[1])
                for cached in catalog._snapshot_document_cache.values()
            ),
        )

        uncached = WorkbenchCatalog(self.root)
        with (
            mock.patch.object(uncached, "html_signature", side_effect=signature),
            mock.patch.object(uncached, "snapshot", side_effect=snapshot),
            mock.patch(
                "audit_core.workbench_server.MAX_CACHED_SNAPSHOT_BYTES",
                1,
            ),
        ):
            uncached.snapshot_document("oversized")
        self.assertFalse(uncached._snapshot_document_cache)
        self.assertEqual(uncached._snapshot_document_cache_bytes, 0)

    def test_resource_document_reuses_bytes_until_file_signature_changes(
        self,
    ) -> None:
        catalog = self.server.catalog
        target = catalog.analysis_file(self.workspace_id, "analysis/facts.json")
        expected = target.read_bytes()
        with mock.patch.object(
            catalog,
            "_load_resource",
            wraps=catalog._load_resource,
        ) as reader:
            first, first_etag = catalog.resource_document(target)
            second, second_etag = catalog.resource_document(target)
            self.assertEqual(first, expected)
            self.assertEqual(first, second)
            self.assertEqual(first_etag, hashlib.sha256(first).hexdigest())
            self.assertEqual(first_etag, second_etag)
            self.assertEqual(reader.call_count, 1)

            atomic_write_json(target, {"visible": False})
            changed, changed_etag = catalog.resource_document(target)
            self.assertEqual(reader.call_count, 2)
            self.assertNotEqual(changed_etag, first_etag)
            self.assertEqual(json.loads(changed), {"visible": False})

    def test_concurrent_resource_reads_coalesce_file_loading(self) -> None:
        catalog = self.server.catalog
        target = catalog.analysis_file(self.workspace_id, "analysis/facts.json")
        worker_count = 12
        barrier = threading.Barrier(worker_count)

        def read_document() -> tuple[bytes, str]:
            barrier.wait(timeout=5)
            return catalog.resource_document(target)

        with mock.patch.object(
            catalog,
            "_load_resource",
            wraps=catalog._load_resource,
        ) as reader:
            with ThreadPoolExecutor(max_workers=worker_count) as executor:
                documents = list(
                    executor.map(lambda _: read_document(), range(worker_count))
                )

        self.assertTrue(all(document == documents[0] for document in documents))
        self.assertEqual(reader.call_count, 1)

    def test_missing_log_document_refreshes_when_file_appears(self) -> None:
        log_path = self.root / self.workspace_id / "logs" / "run.log"
        log_path.unlink()
        catalog = WorkbenchCatalog(self.root)

        missing, missing_etag = catalog.run_log_document(self.workspace_id)
        repeated, repeated_etag = catalog.run_log_document(self.workspace_id)
        self.assertEqual(missing.decode("utf-8"), "该历史结果没有持久化运行日志。\n")
        self.assertEqual(repeated, missing)
        self.assertEqual(repeated_etag, missing_etag)

        log_path.write_text("运行日志已创建。\n", encoding="utf-8")
        created, created_etag = catalog.run_log_document(self.workspace_id)
        self.assertEqual(created.decode("utf-8"), "运行日志已创建。\n")
        self.assertNotEqual(created_etag, missing_etag)

    def test_resource_document_cache_has_fixed_count_and_byte_bounds(self) -> None:
        catalog = WorkbenchCatalog(self.root)
        resources = []
        for index in range(3):
            path = self.root / f"resource-{index}.json"
            payload = (
                f'{{"index":{index},"payload":"' + "x" * 40 + '"}'
            ).encode()
            path.write_bytes(payload)
            resources.append(path)

        with (
            mock.patch(
                "audit_core.workbench_server.MAX_CACHED_RESOURCE_DOCUMENTS",
                2,
            ),
            mock.patch(
                "audit_core.workbench_server.MAX_CACHED_RESOURCE_BYTES",
                1024,
            ),
        ):
            for path in resources:
                catalog.resource_document(path)

        self.assertEqual(len(catalog._resource_document_cache), 2)
        self.assertNotIn(
            ("bytes", str(resources[0].resolve())),
            catalog._resource_document_cache,
        )
        self.assertEqual(
            catalog._resource_document_cache_bytes,
            sum(
                len(cached[1])
                for cached in catalog._resource_document_cache.values()
            ),
        )

        uncached = WorkbenchCatalog(self.root)
        with mock.patch(
            "audit_core.workbench_server.MAX_CACHED_RESOURCE_BYTES",
            1,
        ):
            uncached.resource_document(resources[0])
        self.assertFalse(uncached._resource_document_cache)
        self.assertEqual(uncached._resource_document_cache_bytes, 0)

    def test_catalog_reuses_unchanged_legacy_html_and_refreshes_changed_one(self) -> None:
        legacy = self.root / "20260827-codex.html"

        def legacy_html(error_count: int) -> str:
            view = _view_payload()
            view["sheets"][0]["audit_counts"]["error_count"] = error_count
            return (
                "<!doctype html><html><body>"
                + DATA_OPEN
                + json.dumps(view, ensure_ascii=False)
                + DATA_CLOSE
                + "</body></html>"
            )

        legacy.write_text(legacy_html(1), encoding="utf-8")
        original_parser = workbench_server._legacy_payload_from_html
        with mock.patch(
            "audit_core.workbench_server._legacy_payload_from_html",
            wraps=original_parser,
        ) as parser:
            first = self.server.catalog.list_runs()
            second = self.server.catalog.list_runs()
            self.assertEqual(first, second)
            self.assertEqual(parser.call_count, 1)

            legacy.write_text(legacy_html(3), encoding="utf-8")
            refreshed = self.server.catalog.list_runs()

        legacy_run = next(
            run for run in refreshed if run["workspace_id"] == legacy.stem
        )
        self.assertEqual(legacy_run["error_count"], 3)
        self.assertEqual(parser.call_count, 2)

    def test_large_html_and_snapshot_support_gzip_content_negotiation(self) -> None:
        for path, expected_type in (
            ("/", "text/html"),
            (f"/api/runs/{self.workspace_id}/snapshot", "application/json"),
        ):
            request = urllib.request.Request(
                self.base + path,
                headers={"Accept-Encoding": "gzip"},
            )
            with self.opener.open(request, timeout=5) as response:
                compressed = response.read()
                self.assertEqual(response.headers["Content-Encoding"], "gzip")
                self.assertEqual(response.headers["Vary"], "Accept-Encoding")
                self.assertTrue(response.headers["Content-Type"].startswith(expected_type))
                uncompressed = gzip.decompress(compressed)
                self.assertLess(len(compressed), len(uncompressed))
                if expected_type == "application/json":
                    self.assertEqual(json.loads(uncompressed)["run"]["status"], "completed")
                else:
                    self.assertIn(b"<!doctype html>", uncompressed.lower())

    def test_html_document_cache_reuses_signature_and_refreshes_review(self) -> None:
        path = f"/?run={self.workspace_id}"
        with mock.patch(
            "audit_core.workbench_server._inject_workbench_context",
            wraps=workbench_server._inject_workbench_context,
        ) as injector:
            first = self._html(path)
            second = self._html(path)
            self.assertEqual(first, second)
            self.assertEqual(injector.call_count, 1)

            events_path = self.root / self.workspace_id / "logs" / "events.jsonl"
            with events_path.open("a", encoding="utf-8", newline="\n") as stream:
                stream.write(
                    json.dumps(
                        {
                            "seq": 999,
                            "timestamp": "2026-09-04T00:00:00+00:00",
                            "type": "test.refresh",
                            "stage": "verification",
                            "message": "签名刷新",
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
            event_refreshed = self._html(path)
            event_context = self._script_payload(
                event_refreshed,
                "audit-workbench-context",
            )
            self.assertEqual(event_context["recent_events"][-1]["seq"], 999)
            self.assertEqual(injector.call_count, 2)

            self._post_json(
                f"/api/runs/{self.workspace_id}/manual-review",
                {"reviewed": True},
            )
            refreshed = self._html(path)

        context = self._script_payload(refreshed, "audit-workbench-context")
        self.assertTrue(context["run"]["manual_reviewed"])
        self.assertEqual(injector.call_count, 3)

    def test_html_document_cache_has_a_fixed_memory_bound(self) -> None:
        limit = workbench_server.MAX_CACHED_HTML_DOCUMENTS
        for index in range(limit + 3):
            value, etag = self.server.html_document(
                ("test", index),
                lambda index=index: f"document-{index}".encode("utf-8"),
            )
            self.assertEqual(value, f"document-{index}".encode("utf-8"))
            self.assertEqual(etag, hashlib.sha256(value).hexdigest())
        self.assertEqual(len(self.server._html_cache), limit)
        self.assertNotIn(("test", 0), self.server._html_cache)

    def test_http_11_burst_queue_and_conditional_revalidation(self) -> None:
        self.assertEqual(Handler.protocol_version, "HTTP/1.1")
        self.assertGreaterEqual(self.server.request_queue_size, 64)
        for path in (
            "/",
            "/api/runs",
            f"/api/runs/{self.workspace_id}/snapshot",
        ):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                self.server.server_address[1],
                timeout=5,
            )
            try:
                connection.request(
                    "GET",
                    path,
                    headers={"Accept-Encoding": "gzip"},
                )
                first = connection.getresponse()
                first.read()
                self.assertEqual(first.status, 200)
                self.assertEqual(first.version, 11)
                self.assertEqual(
                    first.headers["Cache-Control"],
                    "private, no-cache, must-revalidate",
                )
                etag = first.headers["ETag"]
                self.assertTrue(etag)

                connection.request(
                    "GET",
                    path,
                    headers={
                        "Accept-Encoding": "gzip",
                        "If-None-Match": f"W/{etag}",
                    },
                )
                second = connection.getresponse()
                self.assertEqual(second.status, 304)
                self.assertEqual(second.version, 11)
                self.assertEqual(second.headers["ETag"], etag)
                self.assertEqual(second.headers["Content-Length"], "0")
                self.assertEqual(second.read(), b"")
            finally:
                connection.close()

    def test_expected_keep_alive_disconnect_does_not_emit_traceback(self) -> None:
        with mock.patch.object(
            workbench_server.ThreadingHTTPServer,
            "handle_error",
        ) as fallback:
            try:
                raise ConnectionResetError("client closed keep-alive socket")
            except ConnectionResetError:
                self.server.handle_error(
                    self.server.socket,
                    ("127.0.0.1", 54321),
                )
            fallback.assert_not_called()

            try:
                raise RuntimeError("unexpected handler failure")
            except RuntimeError:
                self.server.handle_error(
                    self.server.socket,
                    ("127.0.0.1", 54321),
                )
            fallback.assert_called_once_with(
                self.server.socket,
                ("127.0.0.1", 54321),
            )

    def test_aborted_sse_client_does_not_turn_sent_200_into_server_error(self) -> None:
        handler = object.__new__(Handler)
        handler.server = self.server
        handler.path = f"/api/runs/{self.workspace_id}/events"
        handler.headers = {}
        handler.send_response = mock.Mock()
        handler.send_header = mock.Mock()
        handler.end_headers = mock.Mock()
        handler._send_json = mock.Mock()
        handler.wfile = mock.Mock()
        handler.wfile.write.side_effect = ConnectionAbortedError("client closed SSE")
        handler.do_GET()
        handler.send_response.assert_called_once_with(200)
        handler._send_json.assert_not_called()
        handler.wfile.write.assert_called_once()

    def test_conditional_get_returns_before_recompressing_unchanged_body(self) -> None:
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=5,
        )
        original_compressor = workbench_server._gzip_payload
        try:
            with mock.patch(
                "audit_core.workbench_server._gzip_payload",
                wraps=original_compressor,
            ) as compressor:
                connection.request(
                    "GET",
                    "/",
                    headers={"Accept-Encoding": "gzip"},
                )
                first = connection.getresponse()
                first.read()
                self.assertEqual(first.status, 200)
                etag = first.headers["ETag"]
                self.assertEqual(compressor.call_count, 1)

                connection.request(
                    "GET",
                    "/",
                    headers={
                        "Accept-Encoding": "gzip",
                        "If-None-Match": etag,
                    },
                )
                second = connection.getresponse()
                self.assertEqual(second.status, 304)
                self.assertEqual(second.read(), b"")
                self.assertEqual(compressor.call_count, 1)
        finally:
            connection.close()

    def test_snapshot_conditional_get_reuses_prepared_json_document(self) -> None:
        expected = json.dumps(
            self.server.catalog.snapshot(self.workspace_id),
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=5,
        )
        try:
            with mock.patch.object(
                self.server.catalog,
                "_encode_snapshot",
                wraps=self.server.catalog._encode_snapshot,
            ) as serializer:
                connection.request(
                    "GET",
                    f"/api/runs/{self.workspace_id}/snapshot",
                    headers={"Accept-Encoding": "identity"},
                )
                first = connection.getresponse()
                body = first.read()
                self.assertEqual(first.status, 200)
                self.assertEqual(body, expected)
                etag = first.headers["ETag"]
                self.assertEqual(etag, f'"{hashlib.sha256(body).hexdigest()}"')

                connection.request(
                    "GET",
                    f"/api/runs/{self.workspace_id}/snapshot",
                    headers={
                        "Accept-Encoding": "identity",
                        "If-None-Match": etag,
                    },
                )
                second = connection.getresponse()
                self.assertEqual(second.status, 304)
                self.assertEqual(second.headers["Content-Length"], "0")
                self.assertEqual(second.read(), b"")
                self.assertEqual(serializer.call_count, 1)
        finally:
            connection.close()

    def test_technical_resources_use_private_conditional_etags(self) -> None:
        catalog = self.server.catalog
        analysis_path = catalog.analysis_file(
            self.workspace_id,
            "analysis/facts.json",
        )
        checkpoint_path = catalog.checkpoint(self.workspace_id, "001-bootstrap")
        log_path = self.root / self.workspace_id / "logs" / "run.log"
        resources = (
            (
                f"/api/runs/{self.workspace_id}/analysis?path=analysis%2Ffacts.json",
                analysis_path.read_bytes(),
            ),
            (
                f"/api/runs/{self.workspace_id}/checkpoints/001-bootstrap",
                checkpoint_path.read_bytes(),
            ),
            (
                f"/api/runs/{self.workspace_id}/log",
                log_path.read_text(encoding="utf-8").encode("utf-8"),
            ),
        )
        for endpoint, expected in resources:
            with self.subTest(endpoint=endpoint):
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    self.server.server_address[1],
                    timeout=5,
                )
                try:
                    connection.request(
                        "GET",
                        endpoint,
                        headers={"Accept-Encoding": "identity"},
                    )
                    first = connection.getresponse()
                    body = first.read()
                    self.assertEqual(first.status, 200)
                    self.assertEqual(body, expected)
                    self.assertEqual(
                        first.headers["Cache-Control"],
                        "private, no-cache, must-revalidate",
                    )
                    etag = first.headers["ETag"]
                    self.assertEqual(
                        etag,
                        f'"{hashlib.sha256(body).hexdigest()}"',
                    )

                    connection.request(
                        "GET",
                        endpoint,
                        headers={
                            "Accept-Encoding": "identity",
                            "If-None-Match": f"W/{etag}",
                        },
                    )
                    second = connection.getresponse()
                    self.assertEqual(second.status, 304)
                    self.assertEqual(second.headers["ETag"], etag)
                    self.assertEqual(second.headers["Content-Length"], "0")
                    self.assertEqual(second.read(), b"")
                finally:
                    connection.close()

    def test_technical_resource_304_returns_before_recompression(self) -> None:
        target = self.server.catalog.analysis_file(
            self.workspace_id,
            "analysis/facts.json",
        )
        atomic_write_json(target, {"payload": "x" * 4096})
        endpoint = (
            f"/api/runs/{self.workspace_id}/analysis?path=analysis%2Ffacts.json"
        )
        connection = http.client.HTTPConnection(
            "127.0.0.1",
            self.server.server_address[1],
            timeout=5,
        )
        original_compressor = workbench_server._gzip_payload
        try:
            with mock.patch(
                "audit_core.workbench_server._gzip_payload",
                wraps=original_compressor,
            ) as compressor:
                connection.request(
                    "GET",
                    endpoint,
                    headers={"Accept-Encoding": "gzip"},
                )
                first = connection.getresponse()
                first.read()
                self.assertEqual(first.status, 200)
                etag = first.headers["ETag"]
                self.assertEqual(compressor.call_count, 1)

                connection.request(
                    "GET",
                    endpoint,
                    headers={
                        "Accept-Encoding": "gzip",
                        "If-None-Match": etag,
                    },
                )
                second = connection.getresponse()
                self.assertEqual(second.status, 304)
                self.assertEqual(second.read(), b"")
                self.assertEqual(compressor.call_count, 1)
        finally:
            connection.close()

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
        self.assertEqual(primary_context["system_version"], "2.11.26")
        self.assertEqual(primary_context["delivery_mode"], "server")
        self.assertEqual(primary_context["main_flow_tasks"], main_flow_task_list())
        self.assertIn("audit-system-extension-script", primary)
        self.assertIn('content="2.11.26"', primary)
        self.assertFalse(primary_context["oss_intake_enabled"])
        self.assertEqual(self._script_payload(primary, "audit-data")["sheets"], [])
        self.assertLess(
            len(primary.encode("utf-8")),
            ROOT_HTML.stat().st_size * 0.75,
        )
        self.assertIn(
            'meta[name="offline-audit-page-mode"]',
            primary,
        )
        self.assertIn('<link rel="icon" href="data:,">', primary)
        self.assertIn("全部 worktree 运行记录", primary)
        self.assertNotIn("累计持久化运行次数", primary)
        self.assertIn(
            'id="as-search" type="search" aria-label="搜索核销运行记录"',
            primary,
        )
        self.assertNotIn('id="as-status-filter"', primary)
        self.assertIn("AUDIT LEDGER", primary)
        self.assertIn("const terminalRun = (run) => ['completed', 'failed'].includes(run?.status);", primary)
        self.assertIn(
            'role="tablist" aria-label="核销管理中心视图"', primary
        )
        self.assertEqual(primary.count('role="tab" aria-controls="as-view-'), 2)
        self.assertEqual(primary.count('role="tabpanel" aria-labelledby="as-tab-'), 2)
        self.assertIn('role="tablist" aria-label="技术档案分类"', primary)
        self.assertIn("const contextTechnicalSnapshot = () => ({", primary)
        self.assertIn(
            "context.mode === 'record' && workspaceId === context.selected_run",
            primary,
        )
        self.assertIn(
            'class="as-code" tabindex="0" role="region" aria-label="完整运行日志"',
            primary,
        )
        self.assertIn("return resource.value;", primary)
        self.assertIn("const TECHNICAL_PREVIEW_CHARACTERS = 32768;", primary)
        self.assertIn("primaryCatalogEtag: ''", primary)
        self.assertIn("primaryRenderRevision: 0", primary)
        self.assertIn("ledgerRenderRevision: -1", primary)
        self.assertIn("archiveRenderRevision: -1", primary)
        self.assertIn("const PRIMARY_LIST_BATCH_SIZE = 100;", primary)
        self.assertIn("recordVisibleCount: 100", primary)
        self.assertIn("const recordListWindow = () => {", primary)
        self.assertIn('id="as-archive-search" type="search" aria-label="搜索核销技术档案"', primary)
        self.assertIn('id="as-ledger-list" role="list"', primary)
        self.assertIn('id="as-archive-list" role="list"', primary)
        self.assertIn('role="listitem"', primary)
        self.assertIn('data-as-load-more="${view}"', primary)
        self.assertIn("releaseInactivePrimaryViewData(view);", primary)
        self.assertIn("values.slice(0, visible)", primary)
        self.assertNotIn("state.runs.slice(0, visible)", primary)
        self.assertIn("ensurePrimaryViewData(state.primaryView);", primary)
        self.assertIn("ensurePrimaryViewData(view);", primary)
        self.assertIn("{ 'If-None-Match': state.primaryCatalogEtag }", primary)
        self.assertIn("if (response.status === 304) {", primary)
        self.assertIn("? '/api/intake/jobs?completed=0'", primary)
        self.assertIn("result.jobs.filter((job) => job?.status !== 'completed')", primary)
        self.assertIn(
            "state.primaryCatalogEtag = response.headers.get('ETag') || '';",
            primary,
        )
        self.assertIn("if (!next.unchanged) commitPrimaryState(next);", primary)
        self.assertIn(
            "const response = await fetch(path, { cache: 'no-cache' });",
            primary,
        )
        self.assertIn("cache: 'no-store'", primary)
        self.assertIn("data-as-code-expand", primary)
        self.assertIn("当前为长内容预览", primary)
        self.assertEqual(primary.count('role="tab" aria-controls="as-tech-panel-'), 4)
        self.assertEqual(primary.count('role="tabpanel" aria-labelledby="as-tech-tab-'), 4)
        self.assertIn("const latestCompletedZipCount", primary)
        self.assertIn("最近一次 input 的 ZIP 总数", primary)
        self.assertGreaterEqual(primary.count("待人工核验"), 2)
        self.assertNotIn('id="as-recent-list"', primary)
        self.assertNotIn('id="as-tab-ledger"', primary)
        self.assertNotIn('id="as-view-ledger"', primary)
        self.assertIn("saved.view === 'ledger' ? 'overview' : saved.view", primary)
        self.assertIn('id="as-intake-list" role="list"', primary)
        self.assertIn("const intakeCard = (job, queuePosition) =>", primary)
        self.assertIn("return state.runs.filter((run) => {", primary)
        self.assertNotIn("renderOverviewRecords", primary)
        self.assertIn("const renderLedger = () => {", primary)
        self.assertNotIn("slice(0, 5)", primary)
        self.assertIn("if (job?.status === 'failed') return { label: '失败', className: 'failed' };", primary)
        self.assertIn("失败原因：${message}", primary)
        self.assertIn("data-as-delete-job=", primary)
        self.assertIn("取消并删除这条投递任务？", primary)
        self.assertIn("永久删除这条失败投递记录？", primary)
        self.assertIn('data-as-review=', primary)
        self.assertIn('/manual-review', primary)
        self.assertIn("node.setAttribute('role', 'status')", primary)
        self.assertIn("node.setAttribute('aria-live', 'polite')", primary)
        self.assertIn("button.setAttribute('tabindex', selected ? '0' : '-1')", primary)
        self.assertIn(
            "['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', 'Home', 'End']",
            primary,
        )
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
        self.assertEqual(config["api_version"], "1.39")
        self.assertEqual(config["scenario_classification_policy"], "zip_name")
        self.assertEqual(config["unclassified_archive_policy"], "fail_before_ai_and_callback_reason")
        self.assertEqual(config["error_text_policy"], "reason_and_action_separate")
        self.assertEqual(config["business_file_display"], "filenames_only")
        self.assertEqual(config["confidence_badge_display"], "hidden")
        self.assertEqual(config["result_summary_display"], "hidden")
        self.assertEqual(config["result_list_heading_display"], "hidden")
        self.assertEqual(config["filter_header_display"], "hidden")
        self.assertEqual(config["result_filters"], ["audit_type", "category"])
        self.assertEqual(config["system_version"], "2.11.26")
        self.assertEqual(config["material_problem_policy"], "analyze_and_report")
        self.assertEqual(config["refresh_policy"]["overview"]["record_count_statuses"], "all")
        self.assertEqual(config["refresh_policy"]["record_lists"], {
            "source": "/api/runs",
            "views": ["overview", "archive"],
            "statuses": "all",
            "shared_search": True,
            "shared_visible_count": True,
            "record_selector_uses_same_statuses": True,
            "visible_update_rule": "run_catalog_signature_change",
            "date_field": "analysis_started_at", "date_fallback": "created_at",
            "date_timezone": "Asia/Shanghai",
            "title_field": "display_name", "title_source": "source_archives",
            "model_display": "tag", "workspace_naming": "archive_stem_timestamp_unique_suffix",
            "status_colors": {"running": "amber", "failed": "red", "completed": "green"},
        })
        self.assertEqual(config["refresh_policy"]["overview"]["record_list_statuses"], "all")
        self.assertEqual(config["refresh_policy"]["overview"]["record_list_scope"], "all_runs")
        self.assertTrue(config["refresh_policy"]["overview"]["includes_ledger_actions"])
        self.assertEqual(config["refresh_policy"]["overview"]["active_records"]["sources"], ["input", "oss"])
        self.assertFalse(config["read_only"])
        self.assertFalse(config["workbench_read_only"])
        self.assertTrue(config["business_results_read_only"])
        self.assertTrue(config["manual_review"]["writable"])
        self.assertEqual(config["run_deletion"], {
            "writable": True,
            "endpoint": "/api/runs/{workspace_id}",
            "method": "DELETE",
            "request_fields": ["confirm_workspace_id"],
            "confirmation_token": self.server.deletion_token,
            "terminal_only": True,
            "permanent": True,
        })
        self.assertGreaterEqual(len(config["run_deletion"]["confirmation_token"]), 32)
        self.assertIn('data-as-delete=', primary)
        self.assertIn('aria-labelledby="as-delete-title"', primary)
        self.assertEqual(
            config["manual_review"]["endpoint"],
            "/api/runs/{workspace_id}/manual-review",
        )
        self.assertFalse(config["oss_intake"]["enabled"])
        self.assertEqual(config["oss_intake"]["list_endpoint"], "/api/intake/jobs")
        self.assertEqual(config["oss_intake"]["deletion"], {
            "writable": False,
            "endpoint": "/api/intake/jobs/{job_id}",
            "method": "DELETE",
            "request_fields": ["confirm_job_id"],
            "active_allowed": True,
            "permanent": True,
        })
        self.assertEqual(
            config["oss_intake"]["callback_result_format"],
            "scenario_error_facts_markdown",
        )
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
        self.assertNotIn("static_archive", secondary_context)
        injected = self._script_payload(secondary, "audit-data")
        self.assertEqual(
            injected["sheets"][0]["scenario"],
            "maintenance_fee",
        )
        self.assertIn('id="eoErrorList"', secondary)
        self.assertIn('id="eoErrorType"', secondary)
        self.assertIn('id="eoErrorCategory"', secondary)
        self.assertNotIn('id="eoErrorConfidence"', secondary)
        self.assertNotIn('id="eoErrorKeyword"', secondary)
        self.assertNotIn('id="eoErrorFacetSummary"', secondary)
        self.assertIn('id="eoPassList"', secondary)
        self.assertIn('id="eoPassList"></div>', secondary)
        self.assertIn("const initializePassLedger = () => {", secondary)
        self.assertIn(
            "if (viewId === 'passed') initializePassLedger();",
            secondary,
        )
        self.assertIn("const embeddedStaticArchive = () => {", secondary)
        self.assertNotIn('id="audit-static-archive"', secondary)
        self.assertIn('id="eoPassType"', secondary)
        self.assertIn('id="eoPassCategory"', secondary)
        self.assertNotIn('id="eoPassConfidence"', secondary)
        self.assertNotIn('id="eoPassKeyword"', secondary)
        self.assertNotIn("REJECTED ERROR RESULTS", secondary)
        self.assertNotIn("VERIFIED PASS RESULTS", secondary)
        self.assertIn("筛选正确检查项", secondary)
        self.assertIn("种核销方式", secondary)
        self.assertIn("核销类型 ·", secondary)
        self.assertIn("错误原因分类", secondary)
        self.assertIn('data-error-categories=', secondary)
        self.assertIn('data-error-confidence-score=', secondary)
        self.assertIn('aria-label="${escapeHtml(accessibleName)}"', secondary)
        self.assertIn('data-pass-confidence-score=', secondary)
        self.assertIn('data-eo-view="passed"', secondary)
        self.assertIn('role="tablist" aria-label="核销结果视图"', secondary)
        self.assertEqual(secondary.count('role="tab" type="button" data-eo-view='), 2)
        self.assertEqual(
            secondary.count('role="tabpanel" aria-labelledby="eo-tab-'), 2
        )
        self.assertNotIn("单项通过不等于整单核销通过", secondary)
        self.assertNotIn("核销方式始终展示全部；置信度随核销方式和关键词联动", secondary)
        self.assertNotIn("核销方式始终展示全部；检查分类和置信度只保留当前存在项", secondary)
        self.assertEqual(injected["pass_check_log"]["total"], 1)
        self.assertNotIn('class="eo-scenario-card"', secondary)
        self.assertNotIn('class="eo-enter"', secondary)
        self.assertNotIn("进入错误清单", secondary)
        self.assertNotIn("错误场景队列", secondary)

    def test_serves_analysis_and_rejects_unlisted_path(self) -> None:
        with (
            mock.patch("audit_core.workbench_server.attach_pass_check_log") as projection,
            mock.patch(
                "audit_core.workbench_server.read_json_file",
                wraps=workbench_server.read_json_file,
            ) as snapshot_reader,
        ):
            for _ in range(2):
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
            projection.assert_not_called()
            self.assertEqual(snapshot_reader.call_count, 1)
        self.assertEqual(raised.exception.code, 404)
        raised.exception.close()

    def test_completed_snapshot_reuses_projection_without_staling_review(self) -> None:
        endpoint = f"/api/runs/{self.workspace_id}/snapshot"
        with mock.patch(
            "audit_core.workbench_server.attach_pass_check_log",
            wraps=workbench_server.attach_pass_check_log,
        ) as projection:
            first = self._json(endpoint)
            second = self._json(endpoint)
            self.assertEqual(first["view"]["pass_check_log"], second["view"]["pass_check_log"])
            self.assertEqual(projection.call_count, 1)

            self._post_json(
                f"/api/runs/{self.workspace_id}/manual-review",
                {"reviewed": True},
            )
            reviewed = self._json(endpoint)
            self.assertTrue(reviewed["run"]["manual_reviewed"])
            self.assertEqual(projection.call_count, 1)

    def test_saved_evidence_change_invalidates_snapshot_and_html_projection(self) -> None:
        endpoint = f"/api/runs/{self.workspace_id}/snapshot"
        evidence_path = self.root / self.workspace_id / "analysis" / "evidence" / "maintenance_fee.json"
        evidence_path.parent.mkdir(parents=True, exist_ok=True)
        before_signature = self.server.catalog.html_signature(self.workspace_id)
        with mock.patch(
            "audit_core.workbench_server.attach_pass_check_log",
            wraps=workbench_server.attach_pass_check_log,
        ) as projection:
            self._json(endpoint)
            evidence_path.write_text(json.dumps({"documents": [{"source_file": "补存合同.pdf", "role": "signed_promotional_contract"}]}), encoding="utf-8")
            self._json(endpoint)
            self.assertEqual(projection.call_count, 2)
        self.assertNotEqual(before_signature, self.server.catalog.html_signature(self.workspace_id))

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
