from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import httpx

from audit_core.model_metrics import collect_model_artifacts, record_model_attempt, summarize_model_usage
from audit_core.model_settings import api_selection
from audit_core.qwen_model import QwenRequestError, complete_json
from audit_core.workbench_server import WorkbenchCatalog
from audit_core.workbench_store import WorkbenchRunStore, atomic_write_json, read_json_file


class TokenUsageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.analysis = self.root / "analysis"

    def record(self, **fields):
        with collect_model_artifacts(self.analysis):
            record_model_attempt(fields)

    def test_ppio_top_level_counters_are_persisted_without_private_data(self):
        self.record(status="success", input_tokens=123, output_tokens=45,
                    cached_input_tokens=100, reasoning_tokens=20, prompt="PRIVATE", api_key="SECRET")
        raw = (self.analysis / "model-metrics.jsonl").read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw)["usage"], {"input_tokens": 123, "output_tokens": 45,
                                                  "cached_input_tokens": 100, "reasoning_tokens": 20})
        self.assertNotIn("PRIVATE", raw)
        self.assertNotIn("SECRET", raw)

    def test_retries_are_added_and_cached_or_reasoning_tokens_are_not_added_twice(self):
        self.record(status="failed", attempt=1, input_tokens=100, output_tokens=10,
                    cached_input_tokens=80, reasoning_tokens=5)
        self.record(status="success", attempt=2, usage={"input_tokens": 120, "output_tokens": 20,
                                                       "cached_input_tokens": 100, "reasoning_tokens": 8})
        usage = summarize_model_usage(self.analysis)
        self.assertEqual(usage["total_tokens"], 250)
        self.assertEqual(usage["cached_input_tokens"], 180)
        self.assertEqual(usage["reasoning_tokens"], 13)
        self.assertEqual(usage["recorded_calls"], 2)
        self.assertEqual(usage["status"], "recorded")

    def test_missing_usage_differs_from_explicit_zero_and_partial_usage(self):
        self.assertIsNone(summarize_model_usage(self.analysis)["total_tokens"])
        self.record(status="success", input_tokens=0, output_tokens=0)
        self.assertEqual(summarize_model_usage(self.analysis)["total_tokens"], 0)
        self.record(status="timeout")
        usage = summarize_model_usage(self.analysis)
        self.assertEqual(usage["status"], "partial")
        self.assertEqual(usage["total_tokens"], 0)
        self.assertEqual(usage["unrecorded_calls"], 1)

    def test_models_have_separate_actual_usage_including_retries_and_unknown_calls(self):
        self.record(model="deepseek-v4-1-flash", status="failed", input_tokens=100, output_tokens=20,
                    cached_input_tokens=50, reasoning_tokens=10)
        self.record(model="deepseek-v4-1-flash", status="success", input_tokens=150, output_tokens=30)
        self.record(model="qwen3.8-max", status="success", input_tokens=300, output_tokens=80)
        self.record(model="qwen3.8-max", status="timeout")
        self.record(status="success", input_tokens=10, output_tokens=2)
        before = (self.analysis / "model-metrics.jsonl").read_bytes()
        usage = summarize_model_usage(self.analysis)
        models = {row["model"]: row for row in usage["by_model"]}
        self.assertEqual(models["deepseek-v4-1-flash"]["total_tokens"], 300)
        self.assertEqual(models["deepseek-v4-1-flash"]["recorded_calls"], 2)
        self.assertEqual(models["deepseek-v4-1-flash"]["status"], "recorded")
        self.assertEqual(models["qwen3.8-max"]["total_tokens"], 380)
        self.assertEqual(models["qwen3.8-max"]["status"], "partial")
        self.assertEqual(models["unknown"]["total_tokens"], 12)
        self.assertEqual(usage["total_tokens"], 692)
        self.assertEqual(sum(row["total_tokens"] for row in models.values()), usage["total_tokens"])
        self.assertEqual(before, (self.analysis / "model-metrics.jsonl").read_bytes())

    def test_unattributed_malformed_and_zero_counters_remain_distinct(self):
        self.assertEqual(summarize_model_usage(self.analysis)["by_model"], [])
        self.record(model="qwen3.8-max", input_tokens=0, output_tokens=0)
        with (self.analysis / "model-metrics.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('broken\n{"model":"../private","usage":{"input_tokens":2}}\n')
        models = {row["model"]: row for row in summarize_model_usage(self.analysis)["by_model"]}
        self.assertEqual(models["qwen3.8-max"]["total_tokens"], 0)
        self.assertEqual(models["qwen3.8-max"]["status"], "recorded")
        self.assertIsNone(models["unknown"]["total_tokens"])
        self.assertEqual(models["unknown"]["unrecorded_calls"], 2)

    def test_old_qwen_final_progress_is_recovered_read_only_without_double_counting(self):
        self.record(status="success", attempt=1, model_event_count=40, elapsed_seconds=12.05)
        directory = self.analysis / "model-observations"
        directory.mkdir()
        progress = directory / "call-progress-fixture.json"
        atomic_write_json(progress, {"attempt": 1, "model_event_count": 40, "elapsed_seconds": 12.01,
                                     "input_tokens": 200, "output_tokens": 30})
        before = {path: path.read_bytes() for path in self.analysis.rglob("*") if path.is_file()}
        self.assertEqual(summarize_model_usage(self.analysis)["total_tokens"], 230)
        self.assertEqual(before, {path: path.read_bytes() for path in self.analysis.rglob("*") if path.is_file()})
        self.record(status="success", attempt=1, model_event_count=41, elapsed_seconds=13.05,
                    input_tokens=100, output_tokens=10)
        self.assertEqual(summarize_model_usage(self.analysis)["total_tokens"], 340)
        atomic_write_json(directory / "call-progress-duplicate.json", read_json_file(progress))
        usage = summarize_model_usage(self.analysis)
        self.assertEqual(usage["status"], "partial")
        self.assertEqual(usage["total_tokens"], 110)

    def test_unfinished_or_wrong_attempt_progress_is_not_used(self):
        self.record(status="failed", attempt=1, model_event_count=10, elapsed_seconds=2.1)
        directory = self.analysis / "model-observations"
        directory.mkdir()
        atomic_write_json(directory / "call-progress-fixture.json", {
            "attempt": 2, "model_event_count": 10, "elapsed_seconds": 2.0,
            "input_tokens": 100, "output_tokens": 20})
        self.assertIsNone(summarize_model_usage(self.analysis)["total_tokens"])
        atomic_write_json(directory / "call-progress-fixture.json", {
            "attempt": 1, "model_event_count": 10, "input_tokens": 100, "output_tokens": 20})
        self.assertIsNone(summarize_model_usage(self.analysis)["total_tokens"])

    def test_catalog_etag_and_snapshot_refresh_when_only_model_counters_change(self):
        workspace = self.root / "20261027-token-usage"
        workspace.mkdir()
        atomic_write_json(workspace / "manifest.json", {
            "workspace_id": workspace.name, "status": "running", "scenarios": []})
        self.analysis = workspace / "analysis"
        catalog = WorkbenchCatalog(self.root)
        _, first_etag = catalog.catalog_document()
        self.record(model="qwen3.8-max", status="success", input_tokens=100, output_tokens=20)
        encoded, second_etag = catalog.catalog_document()
        self.assertNotEqual(first_etag, second_etag)
        self.assertEqual(json.loads(encoded)["runs"][0]["token_usage"]["total_tokens"], 120)
        self.assertEqual(catalog.snapshot(workspace.name)["run"]["token_usage"]["total_tokens"], 120)
        listing = catalog.list_runs()
        listing[0]["token_usage"]["total_tokens"] = 999
        listing[0]["token_usage"]["by_model"][0]["total_tokens"] = 999
        self.assertEqual(catalog.list_runs()[0]["token_usage"]["total_tokens"], 120)
        self.assertEqual(catalog.list_runs()[0]["token_usage"]["by_model"][0]["total_tokens"], 120)
        self.record(status="success", input_tokens=50, output_tokens=5)
        self.assertEqual(catalog.list_runs()[0]["token_usage"]["total_tokens"], 175)

    def test_completed_and_failed_runs_persist_usage_in_manifest_and_snapshot(self):
        for terminal in ("completed", "failed"):
            with self.subTest(terminal=terminal):
                workspace = self.root / ("20261027-token-" + terminal)
                store = WorkbenchRunStore(workspace, run_id="20261027-token", producer_model="ppio",
                                          root_html=Path("offline-activity-audit.html"),
                                          workbench_url="http://127.0.0.1:8080/")
                self.analysis = store.analysis_root
                self.record(status="success", input_tokens=80, output_tokens=20)
                if terminal == "completed":
                    store.complete(view_payload={"schema_version": "1.1", "sheets": []},
                                   scenarios=[], verification={"verified": True})
                else:
                    store.fail(RuntimeError("isolated test failure"))
                self.assertEqual(read_json_file(store.manifest_path)["token_usage"]["total_tokens"], 100)
                self.assertEqual(read_json_file(store.snapshot_path)["run"]["token_usage"]["total_tokens"], 100)

    def test_final_stream_usage_is_available_even_when_output_limit_fails(self):
        selection = api_selection({"provider": "ppio", "api_key": "sk_isolated-test-key"})
        notifications = []
        events = [
            {"choices": [{"delta": {"content": "{}"}, "finish_reason": "length"}]},
            {"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                                       "completion_tokens_details": {"reasoning_tokens": 8}}},
        ]
        body = "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"
        with self.assertRaises(QwenRequestError):
            complete_json(selection, [], system="JSON", progress=notifications.append,
                          transport=httpx.MockTransport(lambda request: httpx.Response(200, text=body)))
        self.assertEqual(notifications[-1]["input_tokens"], 100)
        self.assertEqual(notifications[-1]["output_tokens"], 20)
        self.assertEqual(notifications[-1]["reasoning_tokens"], 8)


if __name__ == "__main__":
    unittest.main()
