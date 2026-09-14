from __future__ import annotations

import io
import json
import os
import subprocess
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from audit_core import codex_runner
from audit_core.common import AuditError
from audit_core.model_metrics import collect_model_artifacts, summarize_codex_events


_PRIVATE_STDOUT = "PRIVATE_STDOUT_DO_NOT_PERSIST"
_PRIVATE_STDERR = "PRIVATE_STDERR_DO_NOT_PERSIST"
_PRIVATE_REASONING = "PRIVATE_REASONING_DO_NOT_PERSIST"


def _jsonl(*events: dict) -> str:
    return "\n".join(json.dumps(event, ensure_ascii=False) for event in events)


class CodexAttemptTests(unittest.TestCase):
    """只使用隔离文件及模拟子进程，覆盖真实重试和计量分支。"""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.model_root = self.root / "model"
        self.model_root.mkdir()
        self.skill_dir = self.root / "skill"
        self.skill_dir.mkdir()
        self.analysis = self.root / "analysis"
        self.schema = self.root / "evidence.schema.json"
        self.schema.write_text(json.dumps({
            "type": "object", "properties": {"count": {"type": "integer", "minimum": 0}},
            "required": ["count"], "additionalProperties": False,
        }), encoding="utf-8")
        self.raw_output = self.model_root / "evidence.json"
        self.prompt = "请读取当前块原图并仅返回可见事实。"
        self.output = io.StringIO()
        self.errors = io.StringIO()
        self.calls: list[tuple[list[str], dict]] = []

    def invoke(self, outcomes: list, *, max_attempts: int = 3, effort: str = "ultra", post_validate=None):
        pending = iter(outcomes)

        def simulated_run(command, **kwargs):
            self.calls.append((list(command), dict(kwargs)))
            outcome = next(pending)
            if isinstance(outcome, Exception):
                raise outcome
            if "payload" in outcome:
                payload = outcome["payload"]
                raw = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
                self.raw_output.write_text(raw, encoding="utf-8")
            return subprocess.CompletedProcess(
                command, outcome.get("returncode", 0), outcome.get("stdout", ""), outcome.get("stderr", ""),
            )

        ticks = iter(range(10000))
        with (
            collect_model_artifacts(self.analysis),
            redirect_stdout(self.output), redirect_stderr(self.errors),
            patch.object(codex_runner.subprocess, "run", side_effect=simulated_run),
            patch.object(codex_runner, "prepare_model_directory"),
            patch.object(codex_runner.time, "sleep"),
            patch.object(codex_runner.time, "monotonic", side_effect=lambda: float(next(ticks))),
        ):
            return codex_runner._run_codex_json(
                codex="mock-codex.exe", model_root=self.model_root, skill_dir=self.skill_dir,
                schema=self.schema, raw_output=self.raw_output, prompt=self.prompt,
                images=[self.root / "photo-1.png", self.root / "photo-2.png"],
                selected_model="gpt-6-astra", model_catalog=self.root / "models.json",
                label="堆头现场照片", max_attempts=max_attempts, attempt_timeout_seconds=1200,
                reasoning_effort=effort, post_validate=post_validate,
            )

    def metrics(self) -> list[dict]:
        return [json.loads(line) for line in (self.analysis / "model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]

    def assert_no_private_diagnostics(self, exception: Exception | None = None) -> None:
        text = self.output.getvalue() + self.errors.getvalue()
        if (self.analysis / "model-metrics.jsonl").exists():
            text += (self.analysis / "model-metrics.jsonl").read_text(encoding="utf-8")
        if exception is not None:
            text += str(exception)
        for private in (_PRIVATE_STDOUT, _PRIVATE_STDERR, _PRIVATE_REASONING):
            self.assertNotIn(private, text)

    def test_ultra_is_accepted_and_numeric_usage_is_collected(self) -> None:
        stream = _jsonl(
            {"type": "item.completed", "item": {"type": "reasoning", "text": _PRIVATE_REASONING}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": _PRIVATE_STDOUT}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 100, "cached_input_tokens": 90, "output_tokens": 10, "reasoning_tokens": 0,
            }},
        )
        self.assertEqual(self.invoke([{"payload": {"count": 4}, "stdout": stream, "stderr": _PRIVATE_STDERR}]), {"count": 4})
        command, kwargs = self.calls[0]
        self.assertIn('model_reasoning_effort="ultra"', command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--json", command)
        self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
        self.assertEqual(kwargs["input"], self.prompt)
        record = self.metrics()[0]
        self.assertEqual(record["reasoning_effort"], "ultra")
        self.assertEqual(record["status"], "success")
        self.assertEqual(record["usage"], {"input_tokens": 100, "cached_input_tokens": 90, "output_tokens": 10, "reasoning_tokens": 0})
        self.assertEqual(record["delegated_agents_count"], 0)
        self.assertEqual(record["image_count"], 2)
        self.assertEqual(record["prompt_bytes"], len(self.prompt.encode("utf-8")))
        self.assertGreater(record["elapsed_seconds"], 0)
        self.assert_no_private_diagnostics()

    def test_unconfigured_model_proxy_keeps_inherited_environment(self) -> None:
        original = {"HTTP_PROXY": "http://127.0.0.1:10808", "ALL_PROXY": "socks5://127.0.0.1:10808"}
        with patch.dict(os.environ, original, clear=True):
            before = dict(os.environ)
            self.assertEqual(self.invoke([{"payload": {"count": 1}}]), {"count": 1})
            self.assertIsNone(self.calls[0][1]["env"])
            self.assertEqual(dict(os.environ), before)

    def test_explicit_model_proxy_overrides_only_child_environment(self) -> None:
        proxy = "https://proxy-user:proxy-secret@127.0.0.1:7897"
        configured = {"OFFLINE_AUDIT_MODEL_PROXY": proxy,
                      "HTTP_PROXY": "http://127.0.0.1:10808", "https_proxy": "http://127.0.0.1:10808",
                      "ALL_PROXY": "socks5://127.0.0.1:10808", "all_proxy": "socks5://127.0.0.1:10808",
                      "NO_PROXY": "localhost", "UNRELATED_SETTING": "retained"}
        with patch.dict(os.environ, configured, clear=True):
            before = dict(os.environ)
            self.assertEqual(self.invoke([{"payload": {"count": 2}, "stderr": proxy}]), {"count": 2})
            self.assertEqual(dict(os.environ), before)
            command, kwargs = self.calls[0]
            child = kwargs["env"]
            self.assertIsNot(child, os.environ)
            for variable in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
                self.assertEqual(child[variable], proxy)
            self.assertEqual(child["NO_PROXY"], "localhost")
            self.assertEqual(child["UNRELATED_SETTING"], "retained")
            self.assertNotIn(proxy, " ".join(command))
            self.assertNotIn(proxy, kwargs["input"])
        visible = self.output.getvalue() + self.errors.getvalue()
        visible += (self.analysis / "model-metrics.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(proxy, visible)
        self.assertNotIn("proxy-secret", visible)

    def test_configured_proxy_secret_is_not_exposed_by_failure_diagnostics(self) -> None:
        proxy = "http://proxy-user:never-log-this-secret@127.0.0.1:7897"
        with patch.dict(os.environ, {"OFFLINE_AUDIT_MODEL_PROXY": proxy}, clear=True):
            with self.assertRaises(codex_runner.CodexRequestConfigurationError) as caught:
                self.invoke([{"returncode": 1, "stderr": "authentication_error " + proxy}])
        diagnostics = self.output.getvalue() + self.errors.getvalue() + str(caught.exception)
        diagnostics += (self.analysis / "model-metrics.jsonl").read_text(encoding="utf-8")
        self.assertNotIn(proxy, diagnostics)
        self.assertNotIn("never-log-this-secret", diagnostics)

    def test_invalid_model_proxy_fails_safely_before_launch(self) -> None:
        for proxy in ("socks5://user:secret@localhost:7897", "http:///secret", "http://:7897",
                      "http://localhost:secret", "http://localhost:70000", "http://[secret", "http://host secret"):
            with self.subTest(proxy=proxy), patch.dict(os.environ, {"OFFLINE_AUDIT_MODEL_PROXY": proxy}, clear=True):
                before = dict(os.environ)
                with self.assertRaises(codex_runner.CodexRequestConfigurationError) as caught:
                    self.invoke([])
                self.assertIn("configuration", str(caught.exception))
                self.assertNotIn(proxy, str(caught.exception))
                self.assertNotIn("secret", str(caught.exception))
                self.assertEqual(dict(os.environ), before)
        self.assertEqual(self.calls, [])

    def test_blank_model_proxy_keeps_inherited_behavior(self) -> None:
        with patch.dict(os.environ, {"OFFLINE_AUDIT_MODEL_PROXY": "  ", "HTTP_PROXY": "http://localhost:10808"}, clear=True):
            self.invoke([{"payload": {"count": 1}}])
        self.assertIsNone(self.calls[0][1]["env"])

    def test_nonzero_exit_retries_without_persisting_raw_diagnostics(self) -> None:
        failed = {"returncode": 1, "stdout": _PRIVATE_STDOUT + _PRIVATE_REASONING, "stderr": _PRIVATE_STDERR}
        with self.assertRaises(codex_runner.CodexExtractionError) as caught:
            self.invoke([failed, failed], max_attempts=2)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual([row["attempt"] for row in self.metrics()], [1, 2])
        self.assertEqual([row["error_code"] for row in self.metrics()], ["model_error", "model_error"])
        self.assert_no_private_diagnostics(caught.exception)

    def test_success_exit_with_required_read_rejected_is_configuration_failure(self) -> None:
        with self.assertRaises(codex_runner.CodexRequestConfigurationError) as caught:
            self.invoke([{"payload": {"count": 0}, "stderr":
                          "exec_command failed: Get-Content rejected: blocked by policy " + _PRIVATE_STDERR}])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.metrics()[0]["error_code"], "configuration")
        self.assert_no_private_diagnostics(caught.exception)

    def test_reported_source_access_denial_is_not_valid_unclear_evidence(self) -> None:
        with self.assertRaises(codex_runner.CodexRequestConfigurationError):
            self.invoke([{"payload": {"count": 0, "notes": "原始照片访问被拒绝"}}])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.metrics()[0]["status"], "failed")

    def test_context_capacity_stops_unchanged_retry_and_preserves_error_code(self) -> None:
        failed = {"returncode": 1, "stderr": "context_length_exceeded " + _PRIVATE_STDERR,
                  "stdout": _jsonl({"type": "turn.failed", "error": {"message": "context window exceeded " + _PRIVATE_REASONING}})}
        with self.assertRaises(codex_runner.CodexContextCapacityError) as caught:
            self.invoke([failed], max_attempts=3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.metrics()[0]["error_code"], "context_limit")
        self.assert_no_private_diagnostics(caught.exception)

    def test_output_capacity_stops_unchanged_retry(self) -> None:
        failed = {"returncode": 1, "stderr": "max_output_tokens exceeded " + _PRIVATE_STDERR}
        with self.assertRaises(codex_runner.CodexContextCapacityError) as caught:
            self.invoke([failed], max_attempts=3)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.metrics()[0]["error_code"], "output_limit")
        self.assert_no_private_diagnostics(caught.exception)

    def test_long_stdout_does_not_hide_stderr_capacity_error(self) -> None:
        failed = {"returncode": 1, "stderr": "context_length_exceeded " + _PRIVATE_STDERR,
                  "stdout": _PRIVATE_STDOUT * 300}
        with self.assertRaises(codex_runner.CodexContextCapacityError):
            self.invoke([failed, failed, failed])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.metrics()[0]["error_code"], "context_limit")
        self.assert_no_private_diagnostics()

    def test_configuration_failure_does_not_retry_and_is_classified(self) -> None:
        failed = {"returncode": 1, "stderr": "invalid_json_schema " + _PRIVATE_STDERR, "stdout": _PRIVATE_STDOUT}
        with self.assertRaises(codex_runner.CodexRequestConfigurationError) as caught:
            self.invoke([failed])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.metrics()[0]["error_code"], "configuration")
        self.assert_no_private_diagnostics(caught.exception)

    def test_authentication_failure_does_not_retry_and_is_classified(self) -> None:
        failed = {"returncode": 1, "stderr": "authentication_error " + _PRIVATE_STDERR}
        with self.assertRaises(codex_runner.CodexRequestConfigurationError) as caught:
            self.invoke([failed])
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.metrics()[0]["error_code"], "auth")
        self.assert_no_private_diagnostics(caught.exception)

    def test_invalid_json_retries_same_block_with_bounded_feedback(self) -> None:
        result = self.invoke([{"payload": "{invalid"}, {"payload": {"count": 3}}])
        self.assertEqual(result, {"count": 3})
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(self.calls[0][0], self.calls[1][0])
        self.assertEqual(self.calls[0][1]["input"], self.prompt)
        self.assertTrue(self.calls[1][1]["input"].startswith(self.prompt))
        self.assertIn("上一次当前块输出未通过校验", self.calls[1][1]["input"])
        self.assertEqual([row["status"] for row in self.metrics()], ["failed", "success"])
        self.assertEqual(self.metrics()[0]["error_code"], "invalid_output")
        self.assertGreater(self.metrics()[1]["prompt_bytes"], self.metrics()[0]["prompt_bytes"])

    def test_schema_failure_retries_then_validates_fresh_response(self) -> None:
        self.assertEqual(self.invoke([{"payload": {}}, {"payload": {"count": 2}}]), {"count": 2})
        self.assertEqual(len(self.calls), 2)
        self.assertIn("count", self.calls[1][1]["input"])
        self.assertEqual([row["status"] for row in self.metrics()], ["failed", "success"])

    def test_schema_rejected_value_does_not_leak_into_logs_or_terminal_error(self) -> None:
        invalid = {"payload": {"count": _PRIVATE_REASONING}}
        with self.assertRaises(codex_runner.CodexExtractionError) as caught:
            self.invoke([invalid], max_attempts=1)
        self.assertEqual(self.metrics()[0]["error_code"], "invalid_output")
        self.assert_no_private_diagnostics(caught.exception)

    def test_business_validation_retries_only_current_block(self) -> None:
        seen: list[int] = []
        def validate(value: dict) -> None:
            seen.append(value["count"])
            if value["count"] != 4:
                raise AuditError("当前块 photo_reviews 覆盖数量不正确")
        self.assertEqual(self.invoke([{"payload": {"count": 3}}, {"payload": {"count": 4}}], post_validate=validate), {"count": 4})
        self.assertEqual(seen, [3, 4])
        self.assertIn("photo_reviews", self.calls[1][1]["input"])
        self.assertNotIn('{"count": 3}', self.calls[1][1]["input"])
        self.assertEqual([row["attempt"] for row in self.metrics()], [1, 2])

    def test_timeout_is_recorded_as_attempt_with_unknown_not_zero_usage(self) -> None:
        expired = subprocess.TimeoutExpired("mock-codex.exe", 1200, output=_PRIVATE_STDOUT.encode(), stderr=_PRIVATE_STDERR.encode())
        self.assertEqual(self.invoke([expired, {"payload": {"count": 1}}]), {"count": 1})
        rows = self.metrics()
        self.assertEqual([row["attempt"] for row in rows], [1, 2])
        self.assertEqual(rows[0]["status"], "timeout")
        self.assertEqual(rows[0]["error_code"], "timeout")
        self.assertGreater(rows[0]["elapsed_seconds"], 0)
        self.assertIsNone(rows[0].get("usage", {}).get("input_tokens"))
        self.assertIsNone(rows[0].get("delegated_agents_count"))
        self.assert_no_private_diagnostics()

    def test_existing_output_cannot_be_reused_when_new_attempt_produces_none(self) -> None:
        self.raw_output.write_text('{"count": 999}', encoding="utf-8")
        with self.assertRaises(codex_runner.CodexExtractionError):
            self.invoke([{}], max_attempts=1)
        self.assertFalse(self.raw_output.exists())
        self.assertEqual(self.metrics()[0]["status"], "failed")
        self.assertEqual(self.metrics()[0]["error_code"], "invalid_output")

    def test_safe_error_enum_inputs_preserve_their_meaning(self) -> None:
        for code in ("context_limit", "output_limit", "timeout", "rate_limit", "auth", "configuration", "invalid_output", "model_error"):
            with self.subTest(code=code):
                self.assertEqual(codex_runner._codex_error_code(code), code)

    def test_structured_safe_error_code_is_not_lost_when_message_is_present(self) -> None:
        for code in ("auth", "timeout", "configuration", "invalid_output"):
            with self.subTest(code=code):
                result = summarize_codex_events(_jsonl({"type": "turn.failed", "error": {
                    "code": code, "message": _PRIVATE_STDERR,
                }}))
                self.assertEqual(result["error_code"], code)
                self.assertNotIn(_PRIVATE_STDERR, json.dumps(result))


if __name__ == "__main__":
    unittest.main()
