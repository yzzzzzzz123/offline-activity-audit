"""Linux release preflight checks without real LO/RAR/network dependencies."""
from __future__ import annotations

from contextlib import redirect_stdout
from io import BytesIO, StringIO
import importlib.util
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

from PIL import Image

from tests.reader_test_support import new_test_dir, remove_test_dir, test_dir


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "deploy" / "material_reader_preflight.py"
SPEC = importlib.util.spec_from_file_location("material_reader_preflight_test", SOURCE)
preflight = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = preflight
SPEC.loader.exec_module(preflight)


def _png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (18, 14), "orange").save(output, format="PNG")
    return output.getvalue()


def _passing(check_id: str) -> dict:
    return preflight._check(check_id, "pass", "test double", {"synthetic": True})


class PreflightPlatformTests(unittest.TestCase):
    def test_non_linux_is_release_failure_unless_explicitly_allowed(self):
        with patch.object(preflight.sys, "platform", "win32"), \
                patch.object(preflight.os, "name", "nt"):
            failed = preflight.probe_platform()
            diagnostic = preflight.probe_platform(allow_non_linux=True)
        self.assertEqual(failed["status"], "fail")
        self.assertEqual(diagnostic["status"], "warn")

    def test_missing_libreoffice_is_a_system_capability_failure(self):
        with test_dir("material-preflight-") as root:
            with patch("audit_core.word_materials.libreoffice_path", return_value=None):
                result = preflight.probe_libreoffice(root)
        self.assertEqual(result["status"], "fail")
        self.assertIn("LibreOffice", result["message"])

    def test_gnu_tar_is_not_reported_as_rar_capability(self):
        with patch("audit_core.archive_tools.rar_tool", return_value="/usr/bin/tar"), \
                patch("audit_core.archive_tools._version_text",
                      return_value="tar (GNU tar) 1.35"):
            result = preflight.probe_rar()
        self.assertEqual(result["status"], "fail")
        self.assertIn("libarchive", result["message"])

    def test_bsdtar_is_reported_as_rar_capability(self):
        with patch("audit_core.archive_tools.rar_tool", return_value="/usr/bin/bsdtar"), \
                patch("audit_core.archive_tools._version_text",
                      return_value="bsdtar 3.7.4 - libarchive 3.7.4"):
            result = preflight.probe_rar()
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["evidence"]["executable"], "/usr/bin/bsdtar")


class PreflightLayoutTests(unittest.TestCase):
    def test_staging_is_inside_the_callers_output_directory(self):
        with test_dir("material-preflight-") as root:
            result = preflight.probe_staging_layout(root)
            expected_output = root / ".preflight-caller-output"
        self.assertEqual(result["status"], "pass")
        self.assertEqual(
            Path(result["evidence"]["staging"]),
            expected_output / ".staging",
        )
        self.assertTrue(result["evidence"]["system_temp_not_used"])

    def test_worktree_probe_writes_and_cleans_only_its_own_probe(self):
        with test_dir("material-preflight-") as root:
            keep = root / "keep.txt"
            keep.write_text("keep", encoding="utf-8")
            result = preflight.probe_worktree(root)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(keep.read_text(encoding="utf-8"), "keep")
            leftovers = list(root.glob(".preflight-write-*"))
            self.assertEqual(leftovers, [])


class PreflightSmokeTests(unittest.TestCase):
    def setUp(self):
        self.root = new_test_dir("material-preflight-")
        self.addCleanup(remove_test_dir, self.root)

    def _report(self, source: Path) -> dict:
        with patch.object(preflight, "probe_platform", return_value=_passing("platform-linux")), \
                patch.object(preflight, "probe_libreoffice",
                             return_value=_passing("libreoffice-writer-calc")), \
                patch.object(preflight, "probe_rar", return_value=_passing("rar-libarchive")), \
                patch.object(preflight, "probe_cjk_fonts", return_value=_passing("cjk-fonts")), \
                patch.object(preflight, "probe_timezone",
                             return_value=_passing("timezone-asia-shanghai")):
            return preflight.build_report(
                worktree_root=self.root,
                smoke_material=source,
                allow_non_linux=True,
            )

    def test_real_reader_smoke_returns_zero_model_calls_and_stays_in_worktrees(self):
        source = self.root / "real-format.png"
        source.write_bytes(_png_bytes())
        report = self._report(source)
        self.assertTrue(report["passed"], report)
        smoke = report["smoke"]
        self.assertEqual(smoke["status"], "pass")
        self.assertEqual(smoke["reading_status"], "read_ok")
        self.assertEqual(smoke["model_calls"], 0)
        self.assertEqual(smoke["result_model_calls"], 0)
        self.assertEqual(smoke["manifest_model_calls"], 0)
        self.assertGreaterEqual(smoke["unit_count"], 1)
        output = Path(smoke["output"]).resolve()
        self.assertIn(self.root.resolve(), output.parents)
        self.assertFalse(smoke["manifest_retained"])
        self.assertTrue(smoke["cleanup"]["removed"])
        self.assertFalse(smoke["cleanup"]["remaining"])
        self.assertFalse(output.exists())
        self.assertEqual(list(self.root.glob(".preflight*")), [])
        self.assertTrue(smoke["receipts"])
        self.assertEqual(report["test_material_retained"], False)
        cleanup = next(item for item in report["checks"] if item["id"] == "test-material-cleanup")
        self.assertEqual(cleanup["status"], "pass", cleanup)

    def test_bad_customer_file_fails_smoke_without_calling_a_model(self):
        source = self.root / "damaged.png"
        source.write_bytes(b"not-a-png")
        report = self._report(source)
        self.assertFalse(report["passed"])
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(report["smoke"]["status"], "fail")
        self.assertIn("read_ok", report["smoke"]["message"])
        self.assertTrue(report["smoke"]["cleanup"]["removed"])
        self.assertEqual(list(self.root.glob(".preflight*")), [])

    def test_explicit_diagnostic_mode_can_retain_smoke_output_but_is_not_release_clean(self):
        source = self.root / "keep.png"
        source.write_bytes(_png_bytes())
        with patch.object(preflight, "probe_platform", return_value=_passing("platform-linux")), \
                patch.object(preflight, "probe_libreoffice",
                             return_value=_passing("libreoffice-writer-calc")), \
                patch.object(preflight, "probe_rar", return_value=_passing("rar-libarchive")), \
                patch.object(preflight, "probe_cjk_fonts", return_value=_passing("cjk-fonts")), \
                patch.object(preflight, "probe_timezone",
                             return_value=_passing("timezone-asia-shanghai")):
            report = preflight.build_report(
                worktree_root=self.root,
                smoke_material=source,
                allow_non_linux=True,
                keep_smoke_output=True,
            )
        output = Path(report["smoke"]["output"])
        try:
            self.assertTrue(output.is_dir())
            self.assertTrue(report["test_material_retained"])
            cleanup = next(
                item for item in report["checks"] if item["id"] == "test-material-cleanup"
            )
            self.assertEqual(cleanup["status"], "warn")
        finally:
            preflight._remove_owned_directory(
                output.parent,
                parent=output.parent.parent,
                prefix="material-read-",
            )

    def test_cli_prints_json_and_returns_nonzero_when_gate_fails(self):
        failed = {
            "schema_version": "1.0",
            "checks": [],
            "failures": ["platform-linux"],
            "warnings": [],
            "model_calls": 0,
            "passed": False,
        }
        stream = StringIO()
        with patch.object(preflight, "build_report", return_value=failed), \
                redirect_stdout(stream):
            code = preflight.main(["--worktree-root", str(self.root)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stream.getvalue())["failures"], ["platform-linux"])


if __name__ == "__main__":
    unittest.main()
