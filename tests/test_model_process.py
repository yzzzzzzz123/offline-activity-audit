from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from audit_core.model_process import ModelProcessTimeout, ModelProgress, run_model_process, configured_timeout


class ModelProcessTests(unittest.TestCase):
    def test_timeouts_are_configurable_without_exposing_invalid_values(self):
        with patch.dict(os.environ, {"AUDIT_TEST_TIMEOUT": "7200"}):
            self.assertEqual(configured_timeout(3600, "AUDIT_TEST_TIMEOUT"), 7200)
        for value in ("PRIVATE_SECRET", "0", "-1", "86401", "1.2"):
            with self.subTest(value=value), patch.dict(os.environ, {"AUDIT_TEST_TIMEOUT": value}):
                with self.assertRaises(ValueError) as caught:
                    configured_timeout(3600, "AUDIT_TEST_TIMEOUT")
                self.assertNotIn("PRIVATE_SECRET", str(caught.exception))

    def invoke(self, script, *, input="", timeout=5, transport_timeout=120):
        progress = []
        output = io.StringIO()
        with redirect_stdout(output):
            result = run_model_process(
                [sys.executable, "-u", "-c", script], input=input, timeout=timeout,
                label="测试材料", progress_callback=progress.append,
                transport_timeout=transport_timeout, text=True, encoding="utf-8",
                errors="replace", stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
        return result, progress, output.getvalue()

    def test_drains_both_pipes_and_delivers_prompt_without_deadlock(self):
        script = (
            "import sys,json; "
            "sys.stderr.write('e'*200000+'\\n');sys.stderr.flush();"
            "text=sys.stdin.read();"
            "print(json.dumps({'type':'turn.completed','usage':{'input_tokens':len(text)}}))"
        )
        result, progress, output = self.invoke(script, input="x" * 200000)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout)["usage"]["input_tokens"], 200000)
        self.assertEqual(progress[-1]["phase"], "finished")
        self.assertNotIn("e" * 100, output)

    def test_creation_and_reconnection_are_not_model_progress(self):
        progress = ModelProgress(0)
        for kind in ("thread.started", "turn.started"):
            progress.observe("stdout", json.dumps({"type": kind}), 1)
        self.assertEqual(progress.phase, "awaiting_model")
        progress.observe("stdout", json.dumps({"type": "error", "message": "stream disconnected: TLS handshake EOF"}), 2)
        progress.observe("stdout", json.dumps({"type": "error", "message": "Reconnecting: stream disconnected"}), 60)
        self.assertEqual(progress.model_event_count, 0)
        self.assertEqual(progress.transport_failure_since, 2)

    def test_unstructured_output_cannot_break_progress_or_forge_model_events(self):
        progress = ModelProgress(0)
        for event in ({"type": []}, {"type": "item.completed", "item": {"type": {}}}, [1, 2]):
            progress.observe("stdout", json.dumps(event), 1)
        self.assertEqual(progress.model_event_count, 0)

    def test_large_output_keeps_early_diagnostics_for_required_read_validation(self):
        script = (
            "import sys;sys.stderr.write('REQUIRED_READ_DENIED\\n');"
            "sys.stderr.write(('x'*1000+'\\n')*5000);"
            "print('{\"type\":\"turn.completed\"}')"
        )
        result, _, output = self.invoke(script, timeout=10)
        self.assertTrue(result.stderr.startswith('REQUIRED_READ_DENIED\n'))
        self.assertGreater(len(result.stderr), 5_000_000)
        self.assertNotIn('REQUIRED_READ_DENIED', output)

    def test_model_progress_recovers_transport_wait_and_exposes_no_reasoning(self):
        progress = ModelProgress(0)
        progress.observe("stdout", json.dumps({"type": "error", "message": "TLS handshake EOF"}), 1)
        progress.observe("stdout", json.dumps({"type": "item.completed", "item": {
            "type": "reasoning", "text": "PRIVATE_REASONING stream disconnected",
        }}), 2000)
        self.assertIsNone(progress.transport_failure_since)
        snapshot = progress.snapshot(2001)
        self.assertEqual(snapshot["model_event_count"], 1)
        self.assertEqual(snapshot["transport_error_count"], 1)
        self.assertNotIn("PRIVATE_REASONING", json.dumps(snapshot))

    def test_stderr_telemetry_failure_does_not_cancel_model_analysis(self):
        progress = ModelProgress(0)
        progress.observe("stderr", "telemetry export failed: error sending request: TLS handshake EOF", 1)
        self.assertIsNone(progress.transport_failure_since)
        self.assertEqual(progress.transport_error_count, 0)

    def test_transport_failure_stops_early_and_preserves_partial_event_metadata(self):
        script = "import time; print('{\"type\":\"error\",\"message\":\"TLS handshake EOF\"}',flush=True);time.sleep(30)"
        with redirect_stdout(io.StringIO()), self.assertRaises(ModelProcessTimeout) as caught:
            self.invoke(script, timeout=10, transport_timeout=0.1)
        self.assertEqual(caught.exception.timeout_kind, "transport")
        self.assertEqual(caught.exception.progress["model_event_count"], 0)
        self.assertLess(caught.exception.progress["elapsed_seconds"], 8)

    def test_total_deadline_preserves_partial_stdout(self):
        with redirect_stdout(io.StringIO()), self.assertRaises(ModelProcessTimeout) as caught:
            self.invoke("import time;print('{\"type\":\"turn.started\"}',flush=True);time.sleep(30)", timeout=0.2)
        self.assertEqual(caught.exception.timeout_kind, "total")
        self.assertIn("turn.started", caught.exception.stdout)

    def test_exited_root_does_not_wait_for_descendant_that_keeps_pipe_open(self):
        started = time.monotonic()
        script = (
            "import subprocess,sys;"
            "subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
            "print('{\"type\":\"turn.completed\"}',flush=True)"
        )
        result, _, _ = self.invoke(script)
        self.assertEqual(result.returncode, 0)
        self.assertLess(time.monotonic() - started, 4)


if __name__ == "__main__":
    unittest.main()
