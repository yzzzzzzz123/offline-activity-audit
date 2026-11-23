"""Overview paths come from real trace order, without fetching each full dossier."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.execution_trace import collect_execution_trace, execution_route, record_step
from audit_core.workbench_html import WORKBENCH_CONTEXT_ID, render_static_run_archive_html
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_server import WorkbenchCatalog


class OverviewExecutionRouteTests(unittest.TestCase):
    def make_run(self, root):
        workspace = root / "route-fixture"
        (workspace / "analysis").mkdir(parents=True)
        manifest = {"workspace_id": workspace.name, "run_id": "fixture", "status": "running",
                    "scenarios": [], "business_date": "20261109"}
        snapshot = {"run": manifest, "view": None, "analysis_files": [], "dom_checkpoints": []}
        for name, value in (("manifest.json", manifest), ("snapshot.json", snapshot)):
            (workspace / name).write_text(json.dumps(value), encoding="utf-8")
        return workspace

    def record(self, workspace, *kinds):
        with collect_execution_trace(workspace / "analysis"):
            for kind in kinds:
                record_step(kind, kind)

    def test_actual_sequence_and_nonconsecutive_return_visits_survive(self):
        steps = [{"sequence": seq, "type": kind, "status": state} for seq, kind, state in (
            (8, "ai.failed", "failed"), (3, "ai.request", "started"),
            (2, "qince.result", "completed"), (1, "qince.query", "completed"),
            (4, "command.completed", "completed"), (5, "ai.validated", "completed"),
            (6, "database.query", "skipped"), (7, "qince.result", "completed"),
            (10, "oss.download", "completed"), (9, "database.query", "completed"))]
        before = json.dumps(steps)
        self.assertEqual(execution_route(steps), ["勤策", "AI 识别", "勤策", "AI 识别", "商品数据库", "商品 OSS"])
        self.assertEqual(json.dumps(steps), before)
        self.assertEqual(execution_route([None, {}, {"sequence": True, "type": "ai.request"}]), [])
        self.assertEqual(execution_route([
            {"sequence": 1, "type": "qince.skipped", "status": "completed"},
            {"sequence": 2, "type": "ai.skipped"},
            {"sequence": 3, "type": "ai.request", "status": "started"},
        ]), ["AI 识别"])

    def test_catalog_reuses_summary_and_refreshes_appended_trace_without_manifest_change(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_run(root)
            self.record(workspace, "qince.query", "qince.result", "ai.request", "ai.validated")
            original = {name: (workspace / name).read_bytes() for name in ("manifest.json", "snapshot.json")}
            catalog = WorkbenchCatalog(root)
            with patch.object(catalog, "snapshot", side_effect=AssertionError("catalog fetched a dossier")):
                first, first_etag = catalog.catalog_document()
                self.assertEqual(json.loads(first)["runs"][0]["execution_route"], ["勤策", "AI 识别"])
                with patch("audit_core.workbench_server.read_execution_route", side_effect=AssertionError("stable route reread")):
                    second, second_etag = catalog.catalog_document()
                self.assertEqual((first, first_etag), (second, second_etag))
                self.record(workspace, "qince.query", "ai.request")
                latest, latest_etag = catalog.catalog_document()
                expected = ["勤策", "AI 识别", "勤策", "AI 识别"]
                self.assertEqual(json.loads(latest)["runs"][0]["execution_route"], expected)
                self.assertNotEqual(first_etag, latest_etag)
                copied = catalog.list_runs()
                copied[0]["execution_route"].append("caller mutation")
                self.assertEqual(catalog.list_runs()[0]["execution_route"], expected)
            self.assertEqual({name: (workspace / name).read_bytes() for name in original}, original)

    def test_missing_and_invalid_history_do_not_invent_a_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_run(root)
            catalog = WorkbenchCatalog(root)
            self.assertEqual(catalog.list_runs()[0]["execution_route"], [])
            trace = workspace / "analysis" / "execution-trace"
            trace.mkdir()
            (trace / "step-00000001.json").write_text("{partial", encoding="utf-8")
            (trace / "private.json").write_text('{"schema_version":"1.0","sequence":1,"type":"ai.request"}', encoding="utf-8")
            self.assertEqual(catalog.list_runs()[0]["execution_route"], [])
            (trace / "step-00000001.json").write_text('{"schema_version":"1.0","sequence":1,"type":"qince.query"}', encoding="utf-8")
            self.assertEqual(catalog.list_runs()[0]["execution_route"], ["勤策"])

    def test_snapshot_and_static_archive_share_the_catalog_route(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_run(root)
            self.record(workspace, "qince.query", "ai.request", "qince.query", "ai.failed")
            catalog = WorkbenchCatalog(root)
            expected = catalog.list_runs()[0]["execution_route"]
            self.assertEqual(catalog.snapshot(workspace.name)["run"]["execution_route"], expected)
            html = render_static_run_archive_html(workspace, ROOT_HTML)
            marker = f'<script id="{WORKBENCH_CONTEXT_ID}" type="application/json">'
            context = json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])
            self.assertEqual(context["static_catalog"]["runs"][0]["execution_route"], expected)
            self.assertEqual(context["run"]["execution_route"], expected)

    def test_completed_snapshot_cache_keeps_route_on_replacement_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_run(root)
            manifest_path = workspace / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["status"] = "completed"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            self.record(workspace, "qince.query", "ai.request")
            catalog = WorkbenchCatalog(root)
            for _ in range(2):
                self.assertEqual(catalog.snapshot(workspace.name)["run"]["execution_route"], ["勤策", "AI 识别"])

    def test_trace_append_invalidates_snapshot_document_even_without_observer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self.make_run(root)
            self.record(workspace, "qince.query")
            catalog = WorkbenchCatalog(root)
            before, before_etag = catalog.snapshot_document(workspace.name)
            self.record(workspace, "ai.request")
            after, after_etag = catalog.snapshot_document(workspace.name)
            self.assertNotEqual(before_etag, after_etag)
            self.assertEqual(json.loads(before)["run"]["execution_route"], ["勤策"])
            self.assertEqual(json.loads(after)["run"]["execution_route"], ["勤策", "AI 识别"])


if __name__ == "__main__":
    unittest.main()
