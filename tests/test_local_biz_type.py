from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from audit_core.scenario_registry import SCENARIO_LABELS
from audit_core.workbench_runtime import LocalBizTypeRequiredError, main, run_persistent_audit
from tests.pdf_test_support import PolicyProvider, bundle


class LocalBizTypeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="audit-local-type-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "input"
        self.worktrees = self.root / "worktrees"
        self.archive = bundle(self.inputs, "陈列堆头.zip")
        self.arguments = ["--run-id", "20260923-local-type", "--producer-model", "codex",
                          "--input-dir", str(self.archive), "--worktrees", str(self.worktrees)]

    def test_missing_type_never_opens_zip_calls_ai_or_creates_run(self):
        for biz_type in (None, "", " \t\n"):
            for selected_input in (self.archive, self.inputs):
                with self.subTest(biz_type=biz_type, input=selected_input), \
                        patch("audit_core.workbench_runtime._input_archive_names") as scan, \
                        patch("audit_core.workbench_runtime.run_audit") as audit:
                    with self.assertRaises(LocalBizTypeRequiredError):
                        run_persistent_audit("20260923-local-type", producer_model="codex",
                            input_dir=selected_input, worktrees_root=self.worktrees,
                            biz_type=biz_type, scenario="promotional_display")
                    scan.assert_not_called()
                    audit.assert_not_called()
                    self.assertFalse(self.worktrees.exists())

    def test_bundled_noninteractive_cli_requires_answer_even_with_scenario(self):
        entrypoint = Path(__file__).resolve().parents[1] / "skills/orchestrate-offline-audit/scripts/run.py"
        receipt = self.root / "receipt.json"
        original = self.archive.read_bytes()
        result = subprocess.run([sys.executable, "-B", str(entrypoint), *self.arguments,
            "--scenario", "promotional_display", "--result-json", str(receipt)],
            input="", capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 2)
        self.assertIn("请先询问用户", result.stderr)
        self.assertNotIn("核销失败", result.stderr)
        for label in SCENARIO_LABELS.values():
            self.assertIn(label, result.stderr)
        self.assertFalse(self.worktrees.exists())
        self.assertFalse(receipt.exists())
        self.assertEqual(self.archive.read_bytes(), original)

    def test_interactive_cli_waits_for_a_valid_answer_before_running(self):
        answers = iter(("", "poster_material", "KT版等物料制作", "KT板等物料制作"))
        with patch("audit_core.workbench_runtime.run_persistent_audit", return_value={"status": "completed"}) as audit, \
                patch("sys.stdin.isatty", return_value=True), \
                contextlib.redirect_stdout(io.StringIO()) as stdout, \
                contextlib.redirect_stderr(io.StringIO()) as stderr:
            def answer():
                audit.assert_not_called()
                return next(answers)
            with patch("builtins.input", side_effect=answer):
                self.assertEqual(main(self.arguments), 0)
        self.assertEqual(audit.call_count, 1)
        self.assertEqual(audit.call_args.kwargs["biz_type"], "KT板等物料制作")
        self.assertIn("不会自动判断", stderr.getvalue())
        self.assertEqual(json.loads(stdout.getvalue()), {"status": "completed"})

    def test_terminal_choice_number_maps_to_the_displayed_type(self):
        with patch("audit_core.workbench_runtime.run_persistent_audit", return_value={"status": "completed"}) as audit, \
                patch("sys.stdin.isatty", return_value=True), patch("builtins.input", return_value="2"), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as stderr:
            self.assertEqual(main(self.arguments), 0)
        label = tuple(SCENARIO_LABELS.values())[1]
        self.assertIn(f"2. {label}", stderr.getvalue())
        self.assertEqual(audit.call_args.kwargs["biz_type"], label)

    def test_cancel_eof_and_interrupt_do_not_run_or_default_to_a_type(self):
        for answer in ("q", EOFError(), KeyboardInterrupt()):
            with self.subTest(answer=type(answer).__name__), \
                    patch("audit_core.workbench_runtime.run_persistent_audit") as audit, \
                    patch("sys.stdin.isatty", return_value=True), \
                    patch("builtins.input", side_effect=[answer]), \
                    contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(main(self.arguments), 2)
                audit.assert_not_called()
                self.assertFalse(self.worktrees.exists())

    def test_explicit_current_type_does_not_prompt_again(self):
        with patch("audit_core.workbench_runtime.run_persistent_audit", return_value={"status": "completed"}) as audit, \
                patch("builtins.input", side_effect=AssertionError("不得重复询问")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*self.arguments, "--biz-type", "搭赠"]), 0)
        self.assertEqual(audit.call_args.kwargs["biz_type"], "搭赠")

    def test_all_eight_confirmed_local_types_override_names_and_material_candidates(self):
        original = self.archive.read_bytes()
        for scenario, label in SCENARIO_LABELS.items():
            with self.subTest(label=label):
                provider = PolicyProvider(("poster_material",))
                result = run_persistent_audit("20260923-local-type", producer_model="codex",
                    input_dir=self.archive, worktrees_root=self.worktrees, biz_type=label,
                    evidence_provider=provider)
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["scenarios"], [scenario])
                self.assertEqual(provider.calls[0]["selected_scenario"], scenario)
                manifest = json.loads((Path(result["worktree"]) / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["scenario_classification_policy"], "biz_type")
                self.assertEqual(manifest["input_source"], "input")
        self.assertEqual(self.archive.read_bytes(), original)

    def test_oss_missing_type_does_not_open_a_local_question(self):
        with patch("audit_core.workbench_runtime.run_persistent_audit", return_value={"status": "failed"}) as audit, \
                patch("builtins.input", side_effect=AssertionError("OSS 不应询问本地类型")), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main([*self.arguments, "--input-source", "oss"]), 2)
        self.assertEqual(audit.call_args.kwargs["biz_type"], "")


if __name__ == "__main__":
    unittest.main()
