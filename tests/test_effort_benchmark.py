from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from audit_core.effort_benchmark import rank_display_runs, score_display_run


class EffortBenchmarkTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "run-high"
        (self.workspace / "analysis/model-observations").mkdir(parents=True)
        self.labels = self.root / "labels.json"
        self.hashes = [hashlib.sha256(value.encode()).hexdigest() for value in ("a", "b", "c", "d")]
        observations = [
            {"standard_evidence": "unclear", "vertical_facing_count": 3},
            {"standard_evidence": "meets", "vertical_facing_count": 4},
            {"standard_evidence": "meets", "vertical_facing_count": 5},
        ]
        self.reviews = [
            {"store_line_no": 1, "contract_store_name": "甲店", "photo_files": ["a.jpg", "b.jpg"], "display_observation": observations[0]},
            {"store_line_no": 2, "contract_store_name": "乙店", "photo_files": ["c.jpg"], "display_observation": observations[1]},
            {"store_line_no": 3, "contract_store_name": "无金标店", "photo_files": ["d.jpg"], "display_observation": observations[2]},
        ]
        self.registry = {
            "schema_version": "1.0", "calibrations": [
                {"calibration_id": "a", "contract_store_name": "甲店", "photo_sha256": self.hashes[:2], "display_observation": observations[0]},
                {"calibration_id": "b", "contract_store_name": "乙店", "photo_sha256": self.hashes[2:3], "display_observation": observations[1]},
            ],
        }
        self.observation = {
            "version": 1,
            "photo_inventory": [{"file_name": f"{name}.jpg", "sha256": digest} for name, digest in zip("abcd", self.hashes)],
            "contract": {"stores": [{"line_no": review["store_line_no"], "store_name": review["contract_store_name"]} for review in self.reviews]},
            "first_pass_reviews": copy.deepcopy(self.reviews),
            "focused_reviews": copy.deepcopy(self.reviews),
            "calibrated_reviews": copy.deepcopy(self.reviews),
        }
        self.manifest = {"workspace_id": self.workspace.name, "status": "completed", "audit_model": "gpt-6-astra", "reasoning_effort": "high", "created_at": "2026-09-09T01:00:00Z", "completed_at": "2026-09-09T01:02:00Z"}
        self.attempts = [{"status": "completed", "attempt": 1, "label": "堆头照片", "reasoning_effort": "high", "elapsed_seconds": 10, "image_count": 4, "prompt_bytes": 300, "usage": {"input_tokens": 100, "output_tokens": 20, "cached_input_tokens": 30, "reasoning_tokens": None}, "delegated_agents_count": 0}]
        self.write_fixture()

    def write_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def write_fixture(self) -> None:
        self.write_json(self.labels, self.registry)
        self.write_json(self.workspace / "analysis/model-observations/promotional-display.json", self.observation)
        self.write_json(self.workspace / "snapshot.json", {"run": dict(self.manifest), "verification": {"status": "passed"}})
        self.manifest["snapshot_sha256"] = hashlib.sha256((self.workspace / "snapshot.json").read_bytes()).hexdigest()
        self.write_json(self.workspace / "manifest.json", self.manifest)
        (self.workspace / "offline-activity-audit.html").write_text("<!doctype html><title>测试档案</title>", encoding="utf-8")
        (self.workspace / "analysis/model-metrics.jsonl").write_text("\n".join(json.dumps(row) for row in self.attempts) + "\n", encoding="utf-8")

    def score(self) -> dict:
        return score_display_run(self.workspace, self.labels)

    def test_scores_only_labeled_subset_and_does_not_mutate_receipts(self) -> None:
        before = {path: path.read_bytes() for path in self.workspace.rglob("*") if path.is_file()}
        score = self.score()
        self.assertTrue(score["eligible"])
        self.assertEqual(score["quality"]["comparable_label_count"], 2)
        self.assertEqual(score["quality"]["state_accuracy_on_labeled_subset"], 1)
        self.assertEqual(score["unlabeled_completeness"]["stores_without_applicable_gold_label"], 1)
        self.assertEqual(score["efficiency"]["wall_seconds"], 120)
        self.assertEqual(before, {path: path.read_bytes() for path in self.workspace.rglob("*") if path.is_file()})

    def test_calibration_cannot_hide_raw_false_positive(self) -> None:
        self.observation["focused_reviews"][0]["display_observation"] = {"standard_evidence": "meets", "vertical_facing_count": 4}
        self.write_fixture()
        score = self.score()
        self.assertEqual(score["quality"]["false_positive_count"], 1)
        self.assertEqual(score["quality"]["state_correct_count"], 1)
        self.assertEqual(score["phase_scores"]["calibrated_reviews"]["state_correct_count"], 2)

    def test_photo_order_must_match_exactly(self) -> None:
        for phase in ("first_pass_reviews", "focused_reviews", "calibrated_reviews"):
            self.observation[phase][0]["photo_files"].reverse()
        self.write_fixture()
        score = self.score()
        self.assertFalse(score["eligible"])
        self.assertEqual(score["quality"]["applicable_label_count"], 2)
        self.assertEqual(score["quality"]["comparable_label_count"], 1)
        self.assertIsNone(score["quality"]["details"][0]["state_correct"])

    def test_access_blocked_review_is_ineligible_even_if_calibration_matches(self) -> None:
        self.observation["focused_reviews"][0]["display_observation"]["limitations"] = ["原图访问被拒绝，未完成必需复核"]
        self.write_fixture()
        score = self.score()
        self.assertFalse(score["eligible"])
        self.assertFalse(score["integrity"]["checks"]["material_access_not_reported_blocked"])
        self.assertTrue(score["issues"])

    def test_changed_photo_bytes_disable_that_label(self) -> None:
        self.observation["photo_inventory"][0]["sha256"] = "f" * 64
        self.write_fixture()
        score = self.score()
        self.assertEqual(score["quality"]["applicable_label_count"], 1)
        self.assertFalse(score["quality"]["details"][0]["source_available"])

    def test_unknown_usage_is_not_counted_as_zero_and_retries_are_included(self) -> None:
        self.attempts.append({"status": "timeout", "attempt": 2, "label": "堆头照片", "reasoning_effort": "high", "elapsed_seconds": 30, "usage": None, "delegated_agents_count": None})
        self.write_fixture()
        metrics = self.score()["efficiency"]["attempts"]
        self.assertEqual(metrics["elapsed_seconds"]["total"], 40)
        self.assertEqual(metrics["retry_attempt_count"], 1)
        self.assertIsNone(metrics["usage"]["input_tokens"]["total"])
        self.assertEqual(metrics["usage"]["input_tokens"]["known_subtotal"], 100)
        self.assertEqual(metrics["usage"]["input_tokens"]["unknown_attempts"], 1)
        self.assertIsNone(metrics["usage"]["reasoning_tokens"]["known_subtotal"])
        self.assertIsNone(metrics["delegated_agents_count"]["total"])

    def test_corrupt_metrics_do_not_masquerade_as_complete_usage(self) -> None:
        with (self.workspace / "analysis/model-metrics.jsonl").open("a", encoding="utf-8") as stream:
            stream.write("{broken\n")
        score = self.score()
        metrics = score["efficiency"]["attempts"]
        self.assertEqual(metrics["invalid_lines"], 1)
        self.assertIsNone(metrics["usage"]["input_tokens"]["total"])
        self.assertEqual(metrics["usage"]["input_tokens"]["known_subtotal"], 100)
        self.assertTrue(score["issues"])

    def test_failed_or_incomplete_run_is_not_eligible(self) -> None:
        self.manifest["status"] = "failed"
        self.write_fixture()
        self.assertFalse(self.score()["eligible"])
        (self.workspace / "analysis/model-observations/promotional-display.json").unlink()
        score = self.score()
        self.assertFalse(score["eligible"])
        self.assertIsNone(score["quality"]["state_accuracy_on_labeled_subset"])

    def test_tampered_snapshot_and_unknown_photo_fail_integrity(self) -> None:
        self.observation["focused_reviews"][0]["photo_files"].append("foreign.jpg")
        self.write_fixture()
        with (self.workspace / "snapshot.json").open("a", encoding="utf-8") as stream:
            stream.write(" ")
        score = self.score()
        self.assertFalse(score["eligible"])
        self.assertFalse(score["integrity"]["checks"]["snapshot_hash_matches"])
        self.assertEqual(score["unlabeled_completeness"]["phases"]["focused_reviews"]["unknown_photo_files"], ["foreign.jpg"])

    def test_duplicate_gold_route_is_rejected(self) -> None:
        self.registry["calibrations"].append(copy.deepcopy(self.registry["calibrations"][0]))
        self.write_fixture()
        with self.assertRaisesRegex(ValueError, "重复"):
            self.score()

    def test_malformed_observation_does_not_crash_or_invent_scores(self) -> None:
        self.observation["photo_inventory"] = 5
        self.observation["first_pass_reviews"] = 5
        self.write_fixture()
        score = self.score()
        self.assertFalse(score["eligible"])
        self.assertFalse(score["integrity"]["checks"]["photo_inventory_valid"])
        self.assertIsNone(score["quality"]["state_accuracy_on_labeled_subset"])

    def test_missing_numeric_observation_is_not_a_valid_comparison(self) -> None:
        self.observation["focused_reviews"][0]["display_observation"].pop("vertical_facing_count")
        self.write_fixture()
        score = self.score()
        self.assertFalse(score["eligible"])
        self.assertEqual(score["quality"]["comparable_label_count"], 1)

    def test_quality_precedes_speed_and_failed_perfect_run_cannot_win(self) -> None:
        correct = self.score()
        fast_false_positive = copy.deepcopy(correct)
        fast_false_positive["quality"]["false_positive_count"] = 1
        fast_false_positive["efficiency"]["wall_seconds"] = 1
        failed = copy.deepcopy(correct)
        failed["eligible"] = False
        failed["efficiency"]["wall_seconds"] = 0.1
        original = copy.deepcopy([failed, fast_false_positive, correct])
        ranked = rank_display_runs([failed, fast_false_positive, correct])
        self.assertIs(ranked[0], correct)
        self.assertIs(ranked[-1], failed)
        self.assertEqual(original, [failed, fast_false_positive, correct])

    def test_ranking_rejects_different_sources_or_gold_sets(self) -> None:
        first = self.score()
        second = copy.deepcopy(first)
        second["comparability"]["photo_inventory_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "不能直接排序"):
            rank_display_runs([first, second])


if __name__ == "__main__":
    unittest.main()
