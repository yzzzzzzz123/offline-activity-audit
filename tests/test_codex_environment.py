from __future__ import annotations

from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from audit_core import codex_environment as environment
from audit_core.common import AuditError


class CodexEnvironmentTests(unittest.TestCase):
    def test_windows_explicit_backend_keeps_login_profile_out(self) -> None:
        with patch.object(environment.sys, "platform", "win32"):
            self.assertEqual(environment.sandbox_arguments(), [
                "-c", "allow_login_shell=false", "-c", 'windows.sandbox="unelevated"',
            ])
        with patch.object(environment.sys, "platform", "linux"):
            self.assertEqual(environment.sandbox_arguments(), ["-c", "allow_login_shell=false"])

    def test_only_same_user_read_execute_is_granted_to_staged_subtree(self) -> None:
        with TemporaryDirectory() as temporary:
            base = Path(temporary)
            stage = base / "model"; stage.mkdir()
            with (patch.object(environment.sys, "platform", "win32"),
                  patch.object(environment.tempfile, "gettempdir", return_value=str(base)),
                  patch.object(environment, "_current_user_sid", return_value="S-1-5-21-101-202-303-1001"),
                  patch.object(environment.subprocess, "run") as run):
                environment.prepare_model_directory(stage)
                self.assertEqual(run.call_args.args[0], [
                    "icacls.exe", str(stage.resolve()), "/grant",
                    "*S-1-5-21-101-202-303-1001:(OI)(CI)(RX)",
                ])
                run.reset_mock()
                for invalid in [base, base.parent]:
                    with self.subTest(path=invalid), self.assertRaises(AuditError):
                        environment.prepare_model_directory(invalid)
                run.assert_not_called()

    def test_acl_failure_does_not_expose_process_output(self) -> None:
        with TemporaryDirectory() as temporary:
            stage = Path(temporary) / "stage"; stage.mkdir()
            with (patch.object(environment.sys, "platform", "win32"),
                  patch.object(environment.tempfile, "gettempdir", return_value=temporary),
                  patch.object(environment, "_current_user_sid", return_value="S-1-5-21-1"),
                  patch.object(environment.subprocess, "run", side_effect=subprocess.CalledProcessError(
                      1, "icacls", stderr="PRIVATE_DIAGNOSTIC"))):
                with self.assertRaises(AuditError) as caught:
                    environment.prepare_model_directory(stage)
                self.assertIn("configuration", str(caught.exception))
                self.assertNotIn("PRIVATE_DIAGNOSTIC", str(caught.exception))

    def test_source_access_failure_is_distinct_from_visual_uncertainty(self) -> None:
        for message in ["原图访问被拒绝", "重新打开照片：拒绝访问", "Get-Content blocked by policy"]:
            self.assertTrue(environment.reported_material_access_failure({"reviews": [{"limitations": [message]}]}))
        self.assertFalse(environment.reported_material_access_failure({"limitations": ["照片模糊，未能辨认列数"]}))
        self.assertTrue(environment.required_read_was_blocked("ERROR view_image failed: access is denied"))
        self.assertFalse(environment.required_read_was_blocked("unrelated optional write failed"))

    def test_preflight_targets_canary_absolutely_and_sets_process_cwd(self) -> None:
        calls = []
        def native(command, **kwargs):
            root = Path(kwargs["cwd"])
            canary = root / "canary.txt"
            literal = "'" + str(canary).replace("'", "''") + "'"
            self.assertIn("-LiteralPath " + literal, command[-1])
            self.assertEqual(command[command.index("-C") + 1], str(root))
            calls.append(command)
            return subprocess.CompletedProcess(command, 0 if command[-1].startswith("Get-Content") else 1,
                                               canary.read_text(encoding="utf-8"), "")
        with TemporaryDirectory() as temporary:
            with (patch.object(environment.sys, "platform", "win32"),
                  patch.object(environment, "prepare_model_directory"),
                  patch.object(environment.subprocess, "run", side_effect=native)):
                environment.verify_windows_sandbox("codex.exe", Path(temporary))
        self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
