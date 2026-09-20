from __future__ import annotations

import argparse
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[1] / "skills/audit-promotional-display/scripts/benchmark_display_effort.py"
SPEC = importlib.util.spec_from_file_location("benchmark_display_effort", SOURCE)
assert SPEC and SPEC.loader
driver = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(driver)


class BenchmarkDriverTests(unittest.TestCase):
    def score(self, effort: str, *, eligible: bool = True) -> dict:
        return {
            "workspace_id": "workspace-" + effort, "workspace": "/worktrees/" + effort,
            "reasoning_effort": effort, "status": "completed" if eligible else "failed", "eligible": eligible,
            "comparability": {"source": "same"},
            "quality": {"false_positive_count": 0, "state_correct_count": 5, "count_correct_count": 5, "comparable_label_count": 5},
            "integrity": {"passed_check_count": 12, "check_count": 12},
            "efficiency": {"wall_seconds": 10 + driver.EFFORTS.index(effort), "attempts": {}},
        }

    def state(self, efforts: list[str] | None = None) -> dict:
        efforts = efforts or list(driver.EFFORTS)
        return {
            "requested_efforts": efforts, "runs": [{"effort": effort, "run_id": "20260909-" + effort, "finished_at": "now", "formal_runner_attempted": True, "exit_code": 0, "score": self.score(effort)} for effort in efforts],
            "fingerprint_changes": [], "fingerprint_errors": [], "status": "completed",
            "requested_run_id": "20260909", "batch_id": "batch", "started_at": "now", "updated_at": "now", "input_dir": "/input", "worktrees": "/worktrees",
        }

    def test_command_only_invokes_formal_runner_and_preserves_defaults(self) -> None:
        command = driver.build_command(Path("/project"), "20260909-low", "low", Path("/receipts/low.json"))
        self.assertTrue(Path(command[2]).as_posix().endswith("skills/orchestrate-offline-audit/scripts/run.py"))
        self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")
        self.assertEqual(command[command.index("--producer-model") + 1], "codex")
        self.assertEqual(command[command.index("--scenario") + 1], "promotional_display")
        self.assertNotIn("--input-dir", command)
        self.assertNotIn("--worktrees", command)

    def test_recommendation_requires_all_six_and_stable_fingerprints(self) -> None:
        self.assertIsNotNone(driver.build_report(self.state())["recommendation"])
        self.assertIsNone(driver.build_report(self.state(["low", "medium"]))["recommendation"])
        changed = self.state()
        changed["fingerprint_changes"] = ["source/skills/orchestrate-offline-audit/scripts/audit_core/codex_runner.py"]
        report = driver.build_report(changed)
        self.assertFalse(report["comparable"])
        self.assertIsNone(report["recommendation"])

    def test_failed_run_is_kept_and_cannot_be_recommended(self) -> None:
        state = self.state()
        state["runs"][0]["score"] = self.score("low", eligible=False)
        report = driver.build_report(state)
        self.assertEqual(len(report["runs"]), 6)
        self.assertEqual(report["recommendation"]["reasoning_effort"], "medium")

    def test_invalidated_execution_conditions_cannot_recommend_an_effort(self) -> None:
        state = self.state()
        state["execution_issues"] = ["必需原图读取被沙箱拒绝"]
        report = driver.build_report(state)
        self.assertFalse(report["comparable"])
        self.assertIsNone(report["recommendation"])
        self.assertIn(state["execution_issues"][0], report["no_recommendation_reasons"])

    def test_preflight_failure_is_not_six_executed_efforts(self) -> None:
        state = self.state()
        state["runs"][0]["formal_runner_attempted"] = False
        report = driver.build_report(state)
        self.assertFalse(report["full_six_effort_matrix"])
        self.assertIsNone(report["recommendation"])

    def test_different_gold_subset_prevents_recommendation(self) -> None:
        state = self.state()
        state["runs"][1]["score"]["comparability"] = {"source": "different"}
        self.assertIsNone(driver.build_report(state)["recommendation"])

    def test_safe_progress_does_not_echo_model_events_or_errors(self) -> None:
        self.assertEqual(driver._safe_progress("AI 正在识别 堆头合同（1/3）\n"), "AI 正在识别 堆头合同（1/3）")
        for text in ('{"type":"item.completed","text":"private"}', "AI 识别失败：private", "AI 完成 {private}", "token secret"):
            self.assertIsNone(driver._safe_progress(text))

    def test_markdown_distinguishes_unknown_tokens_and_limited_accuracy(self) -> None:
        text = driver.render_report_markdown(driver.build_report(self.state()))
        self.assertIn("未知 token", text)
        self.assertIn("不是准确率", text)
        self.assertIn("校准前", text)
        self.assertIn("每个档位只有本批一次", text)
        self.assertNotIn("<html", text)

    def test_fingerprints_detect_code_input_and_reference_image_changes(self) -> None:
        source = {"row_count": 1, "sha256": "d" * 64, "image_manifest_keys_sha256": "f" * 64, "read_at_utc": "first"}
        database_mock = patch.object(driver, "load_product_catalog", return_value={"data_source": source})
        database_mock.start()
        self.addCleanup(database_mock.stop)
        remote_data = {"oss-images/CP/a.jpg": {"size": 9, "sha256": "a" * 64}}
        remote = patch.object(driver, "reference_fingerprint", side_effect=lambda _: json.loads(json.dumps(remote_data)))
        remote.start()
        self.addCleanup(remote.stop)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            inputs = root / "input"
            inputs.mkdir()
            (inputs / "a.zip").write_bytes(b"input")
            (root / "skills/orchestrate-offline-audit/scripts/audit_core").mkdir(parents=True)
            code = root / "skills/orchestrate-offline-audit/scripts/audit_core/a.py"
            code.write_text("version=1", encoding="utf-8")
            baseline = driver.source_fingerprint(root, inputs)
            source["read_at_utc"] = "next"
            self.assertEqual(driver.fingerprint_changes(baseline, driver.source_fingerprint(root, inputs)), [])
            source["sha256"] = "e" * 64
            self.assertEqual(driver.fingerprint_changes(baseline, driver.source_fingerprint(root, inputs)), ["database/product_catalog.products"])
            source["sha256"] = "d" * 64
            code.write_text("version=2", encoding="utf-8")
            (inputs / "a.zip").write_bytes(b"changed")
            changed = driver.source_fingerprint(root, inputs)
            self.assertEqual(driver.fingerprint_changes(baseline, changed), ["input/a.zip", "source/skills/orchestrate-offline-audit/scripts/audit_core/a.py"])
            remote_data["oss-images/CP/a.jpg"]["sha256"] = "b" * 64
            current = driver.source_fingerprint(root, inputs)
            self.assertIn("oss-images/CP/a.jpg", driver.fingerprint_changes(changed, current))
            source["image_manifest_keys_sha256"] = "0" * 64
            self.assertIn("database/product_catalog.products", driver.fingerprint_changes(current, driver.source_fingerprint(root, inputs)))

    def test_failed_workspace_discovery_requires_exact_unique_run_identity(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            failed = root / "failed"
            failed.mkdir()
            manifest = {"run_id": "20260909-unique", "reasoning_effort": "high", "audit_model": driver.MODEL, "producer_model": "codex", "status": "failed"}
            (failed / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            self.assertEqual(driver.find_workspace(root, "20260909-unique", "high", root / "absent.json"), failed)
            self.assertIsNone(driver.find_workspace(root, "20260909-other", "high", root / "absent.json"))
            duplicate = root / "duplicate"
            duplicate.mkdir()
            (duplicate / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "多个"):
                driver.find_workspace(root, "20260909-unique", "high", root / "absent.json")

    def test_batch_runs_sequentially_and_continues_after_formal_failure(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            args = argparse.Namespace(run_id="20260909", efforts=list(driver.EFFORTS), input_dir=None, worktrees=None)
            calls = []
            def fake_execute(command: list[str], project: Path, codex: Path) -> dict:
                effort = command[command.index("--reasoning-effort") + 1]
                calls.append(effort)
                return {"exit_code": 2 if effort == "medium" else 0, "runner_wall_seconds": 1}
            fingerprint = {"sha256": "a" * 64, "files": {"input/a.zip": {"sha256": "b" * 64}}, "captured_at": "now"}
            with patch("audit_core.codex_runner._find_codex", return_value=str(root / "codex.exe")), patch.object(driver, "source_fingerprint", return_value=fingerprint), patch.object(driver, "execute_formal_runner", side_effect=fake_execute), patch.object(driver, "find_workspace", return_value=None):
                report = driver.run_benchmark(args, project_root=root)
            self.assertEqual(calls, list(driver.EFFORTS))
            self.assertEqual(report["runs"][1]["status"], "failed")
            self.assertEqual(len(report["runs"]), 6)
            batch = Path(report["artifact_directory"])
            self.assertTrue((batch / "progress.json").is_file())
            self.assertTrue((batch / "report.json").is_file())
            self.assertTrue((batch / "report.md").is_file())
            self.assertFalse(list(batch.rglob("*.html")))
            self.assertFalse(list(batch.rglob("*.tmp")))
            self.assertIsNone(report["recommendation"])

    def test_configuration_failure_stops_before_later_efforts(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary); workspace = root / "failed"; workspace.mkdir()
            (workspace / "manifest.json").write_text(json.dumps({"status": "failed", "failure": {
                "type": "AuditError", "message": "只读沙箱预检失败（configuration）",
            }}), encoding="utf-8")
            args = argparse.Namespace(run_id="20260909", efforts=list(driver.EFFORTS), input_dir=None, worktrees=None)
            fingerprint = {"sha256": "a" * 64, "files": {}}
            with (patch("audit_core.codex_runner._find_codex", return_value=str(root / "codex.exe")),
                  patch.object(driver, "source_fingerprint", return_value=fingerprint),
                  patch.object(driver, "execute_formal_runner", return_value={"exit_code": 2}) as execute,
                  patch.object(driver, "find_workspace", return_value=workspace),
                  patch.object(driver, "score_display_run", return_value=self.score("low", eligible=False))):
                report = driver.run_benchmark(args, project_root=root)
            self.assertEqual(execute.call_count, 1)
            self.assertEqual(report["status"], "failed")
            self.assertFalse(report["full_six_effort_matrix"])
            self.assertFalse(report["comparable"])
            self.assertIsNone(report["recommendation"])


if __name__ == "__main__":
    unittest.main()
