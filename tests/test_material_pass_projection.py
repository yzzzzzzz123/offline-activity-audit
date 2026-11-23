"""Saved material checks must reach the API and standalone page read-only."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from audit_core.html_report import DATA_OPEN, DATA_CLOSE
from audit_core.pdf_evidence import classification_issues
from audit_core.workbench_html import render_static_run_archive_html
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_server import WorkbenchCatalog
from audit_core.workbench_store import atomic_write_json


class MaterialPassProjectionTests(unittest.TestCase):
    def test_saved_gate_items_reach_api_and_static_archive_without_changing_history(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "20261102-material-pass"
            workspace.mkdir()
            sheet = {
                "name": "人员激励", "title": "人员激励核销", "scenario": "personnel_incentive",
                "projection_kind": "classification_rejection", "confirmed_scenario": True,
                "audit_type_label": "人员激励", "archive_id": "archive-1", "source_archive": "活动.zip",
                "rows": [{"status": "issue", "section": "detail", "excel_row": 4,
                          "heading": "促销合同未提供", "error_reason": "促销合同未提供。",
                          "error_reasons": ["促销合同未提供。"], "values": ["", "促销合同未提供。", ""]}],
                "audit_counts": {"error_count": 1, "source_row_count": 1},
            }
            view = {"schema_version": "1.1", "sheets": [sheet]}
            manifest = {"workspace_id": workspace.name, "run_id": workspace.name, "status": "completed",
                        "scenarios": ["personnel_incentive"], "scenario_count": 1, "error_count": 1}
            snapshot = {"run": deepcopy(manifest), "view": view, "analysis_files": [], "dom_checkpoints": []}
            materials = [{"id": "settlement", "state": "present", "reason": "已见结算单。", "source_ids": ["unit-1"]},
                         {"id": "promotion_contract", "state": "missing", "reason": "促销合同未提供。", "source_ids": []},
                         {"id": "staff_photos", "state": "missing", "reason": "未提供临促资料。", "source_ids": []}]
            packet = {"archive_id": "archive-1", "source_archive": "活动.zip",
                      "units": [{"unit_id": "unit-1", "source_file": "结算单.pdf", "locator": "第1页"}],
                      "documents": [{"unit_id": "unit-1", "roles": ["settlement"], "facts": ["结算单"], "limitations": []}],
                      "route": {"scenario": "personnel_incentive", "matched": False},
                      "classification": {"candidate_scenarios": ["personnel_incentive"], "materials": materials,
                                         "material_matches": [{"scenario": "personnel_incentive", "materials": materials,
                                                               "extra_materials": []}]}}
            # Historical matches without flags must not gain a retrospective staff gap.
            issues = classification_issues(packet["classification"], "personnel_incentive")
            self.assertEqual(len(issues), 1)
            self.assertIn("促销合同", issues[0]["reason"])
            self.assertNotIn("临促", issues[0]["reason"])
            atomic_write_json(workspace / "manifest.json", manifest)
            atomic_write_json(workspace / "snapshot.json", snapshot)
            evidence = workspace / "analysis/pdf-policy/evidence.json"
            atomic_write_json(evidence, {"packets": [packet]})
            before = {p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}
            catalog = WorkbenchCatalog(root)
            first, etag = catalog.snapshot_document(workspace.name)
            projected = json.loads(first)
            log = projected["view"]["pass_check_log"]
            self.assertEqual((log["material_total"], log["business_total"], log["total"]), (1, 0, 1))
            self.assertEqual(log["groups"][0]["items"][0]["source_files"], ["结算单.pdf"])
            self.assertEqual(projected["view"]["sheets"][0]["rows"][0]["error_reasons"], sheet["rows"][0]["error_reasons"])
            self.assertEqual(projected["run"]["error_count"], 1)
            html = render_static_run_archive_html(workspace, ROOT_HTML)
            static_view = json.loads(html.split(DATA_OPEN, 1)[1].split(DATA_CLOSE, 1)[0])
            self.assertEqual(static_view["pass_check_log"], log)
            self.assertEqual(catalog.snapshot_document(workspace.name), (first, etag))
            self.assertEqual({p.relative_to(workspace): p.read_bytes() for p in workspace.rglob("*") if p.is_file()}, before)

            # Changing only saved evidence invalidates both caches, without rerunning an audit.
            packet["classification"]["materials"][0]["state"] = "unclear"
            atomic_write_json(evidence, {"packets": [packet]})
            refreshed, refreshed_etag = catalog.snapshot_document(workspace.name)
            self.assertNotEqual(refreshed_etag, etag)
            self.assertEqual(json.loads(refreshed)["view"]["pass_check_log"]["material_total"], 0)


if __name__ == "__main__":
    unittest.main()
