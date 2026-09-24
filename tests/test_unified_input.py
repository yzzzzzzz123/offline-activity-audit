from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from audit_core.archive_input import ArchiveInputError, discover_zip_paths
from audit_core.material_intake import prepare_material_diagnosis
from audit_core.orchestrator import DEFAULT_INPUT_DIR
from audit_core.oss_intake import DEFAULT_OSS_INPUT_ROOT, public_job
from audit_core.workbench_runtime import _input_archive_names
from tests.pdf_test_support import bundle, fixture_cli_command


class UnifiedInputTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="audit-unified-input-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.inputs = self.root / "input"

    def prepare(self):
        return prepare_material_diagnosis(
            self.inputs, self.root / "prepared", original_error="", classify_by_content=True,
        )

    def test_mixed_twelve_archives_keep_duplicate_names_and_contents_separate(self):
        for index in range(10):
            bundle(self.inputs, f"{index:02d}.zip")
        for job in ("a" * 24, "b" * 24):
            bundle(self.inputs / job, "00.zip")
        (self.inputs / "README.md").write_text("说明", encoding="utf-8")
        case = self.prepare()
        archives = case["archives"]
        self.assertEqual(len(archives), 12)
        self.assertEqual(len({a["source_archive"] for a in archives}), 12)
        self.assertEqual(len({a["archive_id"] for a in archives}), 12)
        self.assertEqual(case["source_archives"].count("00.zip"), 3)
        files = [f for a in archives for f in a["inventory"]]
        self.assertEqual(len({f["file_id"] for f in files}), len(files))
        self.assertEqual(_input_archive_names(self.inputs, None), case["source_archives"])

    def test_single_zip_or_task_directory_selects_only_that_input(self):
        bundle(self.inputs, "local.ZIP")
        task = self.inputs / ("a" * 24)
        bundle(task, "download.zip")
        for selected, expected in ((self.inputs / "local.ZIP", "local.ZIP"), (task, "download.zip")):
            with self.subTest(selected=selected):
                case = prepare_material_diagnosis(
                    selected, self.root / expected, original_error="", classify_by_content=True,
                )
                self.assertEqual(case["source_archives"], [expected])
                self.assertEqual(_input_archive_names(selected, None), [expected])

    def test_only_immediate_task_folders_are_scanned(self):
        bundle(self.inputs, "local.zip")
        bundle(self.inputs / "task" / "unpacked", "attachment.zip")
        self.assertEqual([p.name for p in discover_zip_paths(self.inputs)], ["local.zip"])

    def test_ten_archive_limit_applies_to_each_directory(self):
        for index in range(11):
            bundle(self.inputs / "task", f"{index:02d}.zip")
        with self.assertRaisesRegex(ArchiveInputError, "最多包含 10 个"):
            discover_zip_paths(self.inputs)

    def test_extraction_budget_is_shared_across_root_and_tasks(self):
        bundle(self.inputs, "local.zip")
        bundle(self.inputs / "task", "download.zip")
        with patch("audit_core.material_intake.MAX_DIAGNOSTIC_FILES", 1), \
                patch("audit_core.material_intake._visual_limitations") as read_visual:
            with self.assertRaisesRegex(ArchiveInputError, "总成员数超限"):
                self.prepare()
        read_visual.assert_not_called()
        self.assertFalse((self.root / "prepared/material_diagnostic").exists())

    def test_linked_task_or_zip_cannot_import_external_files(self):
        bundle(self.root / "outside", "external.zip")
        self.inputs.mkdir()
        for name, target in (("task", self.root / "outside"), ("linked.zip", self.root / "outside/external.zip")):
            with self.subTest(name=name):
                link = self.inputs / name
                try:
                    link.symlink_to(target, target_is_directory=target.is_dir())
                except OSError:
                    self.skipTest("symlink unavailable")
                try:
                    with self.assertRaisesRegex(ArchiveInputError, "符号链接"):
                        discover_zip_paths(self.inputs)
                    with self.assertRaisesRegex(ArchiveInputError, "符号链接"):
                        discover_zip_paths(link / "external.zip" if target.is_dir() else link)
                finally:
                    link.unlink()

    def test_default_paths_match_and_legacy_receipts_project_without_mutation(self):
        self.assertEqual(DEFAULT_INPUT_DIR, DEFAULT_OSS_INPUT_ROOT)
        self.assertEqual(DEFAULT_INPUT_DIR.name, "input")
        job_id = "a" * 24
        bundle(self.inputs / job_id, "download.zip")
        for old_dir in (str(self.root / "input-oss" / job_id), "D:\\audit\\input-oss\\" + job_id):
            with self.subTest(old_dir=old_dir):
                receipt = {"job_id": job_id, "delivery": {
                    "input_directory": old_dir, "input_file": old_dir + "/download.zip", "sha256": "original",
                }}
                original = deepcopy(receipt)
                result = public_job(receipt, input_root=self.inputs)
                self.assertEqual(receipt, original)
                self.assertEqual(Path(result["delivery"]["input_file"]), self.inputs / job_id / "download.zip")
                self.assertEqual(result["delivery"]["sha256"], "original")
                receipt["delivery"]["input_file"] = old_dir + "/missing.zip"
                self.assertEqual(public_job(receipt, input_root=self.inputs)["delivery"], receipt["delivery"])

    def test_bundled_cli_publishes_all_mixed_sources_in_one_archive(self):
        names = [f"{i:02d}.zip" for i in range(10)]
        for name in names:
            bundle(self.inputs, name)
        for job in ("a" * 24, "b" * 24):
            bundle(self.inputs / job, "download.zip")
        for rejected in (False, True):
            with self.subTest(rejected=rejected):
                receipt = self.root / "result.json"
                command = fixture_cli_command([
                    "--run-id", "20260922-unified-input", "--producer-model", "codex",
                    "--biz-type", "KT板等物料制作",
                    "--input-dir", str(self.inputs), "--worktrees", str(self.root / "worktrees"),
                    "--result-json", str(receipt),
                ], candidates={i: [] for i in range(12)} if rejected else None)
                completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=90)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                result = json.loads(receipt.read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "completed")
                workspace = Path(result["worktree"])
                manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
                self.assertEqual(manifest["source_archives"], names + ["download.zip", "download.zip"])
                self.assertEqual(len(list(workspace.glob("*.html"))), 1)
                self.assertFalse(list(workspace.rglob("*.xlsx")))
                if rejected:
                    issues = json.loads((workspace / "analysis/classification-rejection/result.json").read_text(encoding="utf-8"))
                    self.assertEqual(len(issues["archives"]), 12)


if __name__ == "__main__":
    unittest.main()
