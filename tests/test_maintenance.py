from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from audit_core.maintenance import clean_project_caches


class MaintenanceTests(unittest.TestCase):
    def test_preview_and_apply_only_remove_reproducible_code_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "audit_core/__pycache__/example.cpython-314.pyc"
            cache.parent.mkdir(parents=True)
            cache.write_bytes(b"cache")
            protected = [
                "input/material.zip", "input-oss/job/material.zip", "worktrees/run/snapshot.json",
                "artifacts/diagnostics/report.json", "shared/product-database/runtime/data.db",
                "shared/product-database/.env", ".git/objects/pack/archive.pack",
                "tests/fixtures/example.tmp", "tests/__pycache__/keep.txt",
            ]
            for name in protected:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"keep")
            preview = clean_project_caches(root)
            self.assertEqual(preview["bytes"], 5)
            self.assertEqual(preview["removed"], 0)
            self.assertTrue(cache.exists())
            applied = clean_project_caches(root, apply=True)
            self.assertEqual(applied["removed"], 1)
            self.assertFalse(cache.parent.exists())
            self.assertIn("tests/__pycache__", applied["deferred"])
            for name in protected:
                self.assertEqual((root / name).read_bytes(), b"keep")
            self.assertEqual(clean_project_caches(root, apply=True)["removed"], 0)

    def test_linked_cache_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            protected = root / "worktrees/run"
            protected.mkdir(parents=True)
            (protected / "important.pyc").write_bytes(b"keep")
            try:
                (root / "__pycache__").symlink_to(protected, target_is_directory=True)
            except OSError:
                self.skipTest("当前账户不能创建符号链接")
            result = clean_project_caches(root, apply=True)
            self.assertEqual(result["removed"], 0)
            self.assertIn("__pycache__", result["deferred"])
            self.assertEqual((protected / "important.pyc").read_bytes(), b"keep")


if __name__ == "__main__":
    unittest.main()
