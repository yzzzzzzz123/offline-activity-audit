"""Large-table inventory preserves source coverage and full audit evidence."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from audit_core.pdf_materials import PdfEvidenceProvider, _gate_documents
from audit_core.extraction_chain import render_prompt
from audit_core.workbench_server import WorkbenchCatalog
from audit_core.workbench_store import WorkbenchRunStore, project_run_status, reserve_workspace
from tests.pdf_test_support import classification


class MaterialGateInventoryTests(unittest.TestCase):
    def test_all_columns_values_sources_and_limits_survive_without_row_sampling(self):
        documents = [{"unit_id": f"table-{chunk}", "roles": ["spreadsheet"], "limitations": [],
            "facts": [f"销售数据!R{chunk * 150 + row}: C1=069001 | C2=原文 | C3=2.0001 | C4=备注含 | 分隔符"
                      for row in range(1, 151)]} for chunk in range(68)]
        documents[-1]["facts"][-1] += " | C5=最后一行的特殊条款"
        documents.append({"unit_id": "photo", "roles": ["photo"], "facts": ["水印不清楚"], "limitations": ["遮挡"]})
        original = deepcopy(documents)
        encoded = _gate_documents(documents)
        projected = json.loads(encoded)
        self.assertEqual(documents, original)
        self.assertEqual([d["unit_id"] for d in projected], [d["unit_id"] for d in original])
        for document in projected[:-1]:
            inventory = document["native_table_inventory"]
            self.assertEqual(inventory["row_count"], 150)
            self.assertEqual(inventory["columns"]["C1"], ["069001"])
            self.assertEqual(inventory["columns"]["C3"], ["2.0001"])
            self.assertEqual(inventory["columns"]["C4"], ["备注含 | 分隔符"])
        self.assertEqual(projected[-2]["native_table_inventory"]["columns"]["C5"], ["最后一行的特殊条款"])
        self.assertEqual(projected[-1], original[-1])
        self.assertLess(len(encoded.encode()), len(json.dumps(original, ensure_ascii=False).encode()) // 10)

    def test_unrecognized_syntax_and_non_native_documents_keep_complete_facts(self):
        documents = [
            {"unit_id": "malformed", "roles": ["spreadsheet"], "facts": ["Sheet1!R1: C1=标题", "保留无法识别的末行"], "limitations": []},
            {"unit_id": "empty", "roles": ["unknown"], "facts": [], "limitations": ["空表"]},
            {"unit_id": "scan", "roles": ["pos"], "facts": ["Sheet1!R1: C1=原图内容"], "limitations": []},
        ]
        self.assertEqual(json.loads(_gate_documents(documents)), documents)

    def test_gate_uses_inventory_but_validation_and_next_stage_keep_exact_rows(self):
        facts = [f"明细!R{i}: C1=门店甲 | C2={i}" for i in range(1, 301)]
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        captured = []
        def call(root, skill, schema, prompt, **kwargs):
            captured.append(render_prompt(prompt))
            value = classification("poster_material", ["u"])
            selected = next(m for m in value["material_matches"] if m["scenario"] == "poster_material")
            value.update(material_matches=[selected], candidate_scenarios=["poster_material"],
                         flags=selected["flags"], materials=selected["materials"])
            kwargs["validator"](value)
            return value
        with tempfile.TemporaryDirectory() as temporary, mock.patch.object(provider, "_call", side_effect=call):
            result = provider.classify({"archive_id": "a1", "selected_scenario": "poster_material", "units": [
                {"unit_id": "u", "image": None, "native_facts": facts, "limitations": []},
            ]}, Path(temporary))
        self.assertIn("不设置资料门禁", captured[0])
        self.assertIn('"row_count":300', captured[0])
        self.assertIn('"last_row":"明细!R300"', captured[0])
        self.assertIn('"300"', captured[0])
        self.assertEqual(result["documents"][0]["facts"], facts)
        self.assertEqual(result["classification"]["source_ids"], ["u"])


class ResolvedFailedScenarioTests(unittest.TestCase):
    def test_old_failed_record_projects_fixed_type_in_catalog_and_snapshot_without_history_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = reserve_workspace("20261010-existing-failure", "codex", root)
            store = WorkbenchRunStore(workspace, run_id="20261010-existing-failure", producer_model="codex",
                root_html=Path("offline-activity-audit.html"), workbench_url="http://127.0.0.1:8080/")
            # Reproduce the historical shape: routing resolved, no finished packet.
            store.manifest.update(bizType="陈列堆头", selected_scenario="promotional_display", scenarios=[], scenario_count=0)
            store.fail(RuntimeError("synthetic model timeout"))
            before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.rglob("*") if p.is_file()}
            catalog = WorkbenchCatalog(root)
            item = catalog.list_runs()[0]
            snapshot = catalog.snapshot(workspace.name)
            for projection in (item, snapshot["run"]):
                self.assertEqual(projection["scenarios"], ["promotional_display"])
                self.assertEqual(projection["scenario_count"], 1)
                self.assertEqual(projection["status"], "failed")
            self.assertEqual(before, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in before})

    def test_no_type_is_invented_for_unresolved_or_conflicting_routing(self):
        for changes in ({}, {"bizType": "不存在的核销类型", "selected_scenario": "promotional_display"},
                        {"bizType": "陈列堆头", "selected_scenario": "poster_material"}):
            original = {"status": "failed", "scenarios": [], "scenario_count": 0, **changes}
            self.assertEqual(project_run_status(original), original)
