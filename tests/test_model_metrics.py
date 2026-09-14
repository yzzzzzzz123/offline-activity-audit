from __future__ import annotations

import asyncio
import json
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier

from audit_core.model_metrics import (
    collect_model_artifacts,
    record_model_attempt,
    save_model_observations,
    summarize_codex_events,
)


def _events(*events: dict) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


def _records(directory: Path) -> list[dict]:
    return [json.loads(line) for line in (directory / "model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]


class CodexEventSummaryTests(unittest.TestCase):
    def test_missing_values_are_unknown_and_explicit_zero_is_preserved(self) -> None:
        self.assertEqual(summarize_codex_events(""), {"usage": {}, "delegated_agents_count": None})
        result = summarize_codex_events(_events({"type": "turn.completed", "usage": {"input_tokens": 0, "output_tokens": 7, "cached_input_tokens": None}}))
        self.assertEqual(result, {"usage": {"input_tokens": 0, "output_tokens": 7}, "delegated_agents_count": 0})

    def test_only_nonnegative_integer_token_counters_survive(self) -> None:
        result = summarize_codex_events(_events({"type": "turn.completed", "usage": {
            "input_tokens": True, "cached_input_tokens": -1, "output_tokens": "5", "reasoning_tokens": 1.5,
            "total_tokens": 999, "message": "private raw data",
        }}))
        self.assertEqual(result["usage"], {})

    def test_nested_usage_details_and_duplicate_turns(self) -> None:
        first = {"type": "turn.completed", "turn_id": "private-turn", "usage": {
            "input_tokens": 100, "output_tokens": 10,
            "input_tokens_details": {"cached_tokens": 50}, "output_tokens_details": {"reasoning_tokens": 0},
        }}
        result = summarize_codex_events(_events(first, first, {"type": "turn.completed", "turn_id": "another", "usage": {"input_tokens": 5, "output_tokens": 2}}))
        self.assertEqual(result["usage"], {"input_tokens": 105, "output_tokens": 12})
        self.assertNotIn("private-turn", json.dumps(result))

    def test_messages_commands_and_reasoning_cannot_forge_counters_or_delegation(self) -> None:
        private = "PRIVATE_PROMPT_PATH_C:/secrets/token.txt"
        result = summarize_codex_events(_events(
            {"type": "item.completed", "usage": {"input_tokens": 12345}, "item": {"type": "agent_message", "text": private + " spawn_agent context_limit"}},
            {"type": "item.completed", "item": {"type": "command_execution", "tool": "spawn_agent", "aggregated_output": '{"usage":{"input_tokens":12}}'}},
            {"type": "item.completed", "item": {"type": "reasoning", "tool": "spawn_agent", "text": private}},
            {"type": "turn.completed"},
        ))
        self.assertEqual(result, {"usage": {}, "delegated_agents_count": 0})
        self.assertNotIn(private, json.dumps(result))

    def test_only_successful_explicit_spawn_completion_is_counted_once(self) -> None:
        item = {"type": "collab_tool_call", "tool": "spawn_agent", "id": "private-call", "status": "completed"}
        result = summarize_codex_events(_events(
            {"type": "item.started", "item": item},
            {"type": "item.completed", "item": item},
            {"type": "item.completed", "item": item},
            {"type": "item.completed", "item": dict(item, id="failed", status="failed")},
            {"type": "item.completed", "item": dict(item, id="wait", tool="wait_agent")},
            {"type": "turn.completed"},
        ))
        self.assertEqual(result["delegated_agents_count"], 1)
        self.assertNotIn("private-call", json.dumps(result))

    def test_incomplete_or_malformed_stream_does_not_claim_zero_agents(self) -> None:
        for stream in (
            "broken\n" + _events({"type": "turn.completed"}),
            _events({"type": "turn.started"}),
            _events({"type": "turn.completed"}, {"type": "turn.started"}),
            "null\n", "[]\n", '{"type":"turn.completed"',
        ):
            with self.subTest(stream=stream):
                self.assertIsNone(summarize_codex_events(stream)["delegated_agents_count"])

    def test_errors_are_classified_without_retaining_sensitive_text(self) -> None:
        for marker, expected in (
            ("context_length_exceeded", "context_limit"), ("max_output_tokens", "output_limit"),
            ("timed out", "timeout"), ("rate_limit_exceeded", "rate_limit"),
            ("authentication_error", "auth"), ("invalid_json_schema", "configuration"),
            ("schema validation failed", "invalid_output"), ("unknown vendor error", "model_error"),
        ):
            with self.subTest(marker=marker):
                result = summarize_codex_events(_events({"type": "turn.failed", "error": {"message": marker + " PRIVATE_SECRET C:/private/path"}}))
                self.assertEqual(result["error_code"], expected)
                self.assertNotIn("PRIVATE_SECRET", json.dumps(result))
                self.assertNotIn("private/path", json.dumps(result))

    def test_error_event_and_response_usage_are_supported(self) -> None:
        result = summarize_codex_events(_events(
            {"type": "error", "message": "rate_limit_exceeded private"},
            {"type": "response.completed", "response": {"id": "hidden", "usage": {"input_tokens": 2, "output_tokens": 0}}},
        ))
        self.assertEqual(result, {"usage": {"input_tokens": 2, "output_tokens": 0}, "delegated_agents_count": 0, "error_code": "rate_limit"})

    def test_structured_error_code_takes_priority_over_messages_and_nested_errors(self) -> None:
        result = summarize_codex_events(_events({"type": "turn.failed", "error": {
            "code": "auth", "message": "PRIVATE context window text",
            "error": {"code": "timeout", "message": "PRIVATE nested"},
        }}))
        self.assertEqual(result["error_code"], "auth")
        self.assertNotIn("PRIVATE", json.dumps(result))
        nested = summarize_codex_events(_events({"type": "error", "error": {
            "code": "unknown_vendor_code", "error": {"code": "invalid_output", "message": "PRIVATE"},
        }}))
        self.assertEqual(nested["error_code"], "invalid_output")

    def test_untrusted_nested_field_types_do_not_raise_or_become_metrics(self) -> None:
        result = summarize_codex_events(_events(
            {"type": "item.completed", "item": {"type": []}},
            {"type": "item.completed", "item": {"type": "tool_call", "tool": {"message": "PRIVATE"}}},
            {"type": "turn.failed", "error": {"code": ["PRIVATE"], "message": {"private": True}}},
        ))
        self.assertEqual(result, {"usage": {}, "delegated_agents_count": 0, "error_code": "model_error"})


class ModelArtifactCollectionTests(unittest.TestCase):
    def test_no_context_is_noop_and_context_restores_after_exception(self) -> None:
        record_model_attempt({"status": "success"})
        save_model_observations("promotional-display", {"visible": "fact"})
        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / "analysis"
            with self.assertRaisesRegex(RuntimeError, "expected"):
                with collect_model_artifacts(directory):
                    record_model_attempt({"attempt": 1})
                    raise RuntimeError("expected")
            record_model_attempt({"attempt": 2})
            self.assertEqual(_records(directory), [{"attempt": 1}])

    def test_nested_contexts_restore_outer_sink(self) -> None:
        with TemporaryDirectory() as temporary:
            outer, inner = Path(temporary) / "outer", Path(temporary) / "inner"
            with collect_model_artifacts(outer):
                record_model_attempt({"attempt": 1})
                with collect_model_artifacts(inner):
                    record_model_attempt({"attempt": 2})
                record_model_attempt({"attempt": 3})
            self.assertEqual(_records(outer), [{"attempt": 1}, {"attempt": 3}])
            self.assertEqual(_records(inner), [{"attempt": 2}])

    def test_attempt_allowlist_excludes_raw_content_and_invalid_types(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with collect_model_artifacts(directory):
                record_model_attempt({
                    "label": "海报/物料制作材料", "model": "gpt-6-astra", "reasoning_effort": "ultra",
                    "attempt": 1, "status": "success", "elapsed_seconds": 0.0, "image_count": 0,
                    "prompt_bytes": 20, "usage": {"input_tokens": 0, "output_tokens": 8, "raw": "PRIVATE"},
                    "delegated_agents_count": None, "error_code": "invented_PRIVATE",
                    "stdout": "PRIVATE", "prompt": "PRIVATE", "reasoning": "PRIVATE", "path": "C:/PRIVATE",
                })
                record_model_attempt({"label": "C:/PRIVATE", "model": "../PRIVATE", "status": "PRIVATE", "attempt": True,
                                      "elapsed_seconds": float("inf"), "image_count": -1, "prompt_bytes": "PRIVATE", "usage": {"reasoning_tokens": None}})
            rows = _records(directory)
            self.assertEqual(rows[0], {
                "label": "海报/物料制作材料", "model": "gpt-6-astra", "reasoning_effort": "ultra",
                "attempt": 1, "status": "success", "elapsed_seconds": 0.0, "image_count": 0,
                "prompt_bytes": 20, "usage": {"input_tokens": 0, "output_tokens": 8}, "delegated_agents_count": None,
            })
            self.assertEqual(rows[1], {"usage": {}})
            self.assertNotIn("PRIVATE", (directory / "model-metrics.jsonl").read_text(encoding="utf-8"))

    def test_safe_error_codes_only_and_no_nan_or_negative_counts(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with collect_model_artifacts(directory):
                record_model_attempt({"error_code": "timeout", "status": "timeout", "elapsed_seconds": float("nan"), "delegated_agents_count": -1})
            self.assertEqual(_records(directory), [{"status": "timeout", "error_code": "timeout"}])

    def test_observations_are_json_preserved_and_replaced_without_temporary_files(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            payload = {"photo_reviews": [{"visible_text": "参半", "count": 0, "limitation": None}]}
            with collect_model_artifacts(directory):
                save_model_observations("promotional-display", {"old": True})
                save_model_observations("promotional-display", payload)
            target = directory / "model-observations" / "promotional-display.json"
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(target.parent.iterdir()), [target])

    def test_observation_filename_rejects_traversal_and_windows_special_names(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with collect_model_artifacts(directory):
                for name in ("../escape", "..", "/absolute", "C:\\escape", "a/b", "a\\b", "a:stream", "con", "com1", "nul", "a.json", "a\n", "", "a" * 65):
                    with self.subTest(name=name), self.assertRaises(ValueError):
                        save_model_observations(name, {})
            self.assertEqual(list(directory.iterdir()), [])

    def test_invalid_observation_json_does_not_damage_previous_observations(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            with collect_model_artifacts(directory):
                save_model_observations("promotional-display", {"valid": True})
                with self.assertRaises(ValueError):
                    save_model_observations("promotional-display", {"invalid": float("nan")})
            self.assertEqual(json.loads((directory / "model-observations" / "promotional-display.json").read_text(encoding="utf-8")), {"valid": True})
            self.assertEqual(len(list((directory / "model-observations").iterdir())), 1)

    def test_existing_symlink_cannot_redirect_metric_or_observation_writes(self) -> None:
        with TemporaryDirectory() as temporary:
            directory = Path(temporary) / "analysis"
            directory.mkdir()
            outside = Path(temporary) / "outside.json"
            outside.write_text("untouched", encoding="utf-8")
            link = directory / "model-metrics.jsonl"
            try:
                link.symlink_to(outside)
            except (OSError, NotImplementedError):
                self.skipTest("当前 Windows 会话没有创建符号链接的权限")
            with collect_model_artifacts(directory), self.assertRaises(ValueError):
                record_model_attempt({"status": "success"})
            self.assertEqual(outside.read_text(encoding="utf-8"), "untouched")

    def test_concurrent_threads_with_separate_contexts_are_isolated(self) -> None:
        with TemporaryDirectory() as temporary:
            barrier = Barrier(2)
            directories = [Path(temporary) / str(index) for index in range(2)]
            def write(index: int) -> None:
                with collect_model_artifacts(directories[index]):
                    barrier.wait(timeout=10)
                    for _ in range(10):
                        record_model_attempt({"attempt": index})
            with ThreadPoolExecutor(max_workers=2) as executor:
                list(executor.map(write, range(2)))
            for index, directory in enumerate(directories):
                self.assertEqual(_records(directory), [{"attempt": index}] * 10)

    def test_async_tasks_with_separate_contexts_are_isolated(self) -> None:
        with TemporaryDirectory() as temporary:
            directories = [Path(temporary) / str(index) for index in range(2)]
            async def write(index: int) -> None:
                with collect_model_artifacts(directories[index]):
                    await asyncio.sleep(0)
                    record_model_attempt({"attempt": index})
            async def run() -> None:
                await asyncio.gather(write(0), write(1))
            asyncio.run(run())
            self.assertEqual(_records(directories[0]), [{"attempt": 0}])
            self.assertEqual(_records(directories[1]), [{"attempt": 1}])


if __name__ == "__main__":
    unittest.main()
