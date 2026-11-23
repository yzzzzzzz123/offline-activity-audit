from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from audit_core.workbench_html import load_analysis_summary, render_static_run_archive_html
from audit_core.workbench_server import WorkbenchCatalog
from audit_core.workbench_store import ANALYSIS_SUMMARY_FILENAME, atomic_write_json
from audit_core.summary_display_backfill import publish_summary_displays


class AISummaryDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "20261109-summary"
        self.workspace.mkdir()
        self.manifest = {"workspace_id": self.workspace.name, "status": "completed", "error_count": 0}
        self.view = {"sheets": []}

    def test_saved_callback_text_takes_priority_over_current_view_and_metadata_path(self):
        text = '## 人员激励\n\n1. 原回传问题 100 元。\n2. <script>alert("x")</script>\n'
        (self.workspace / ANALYSIS_SUMMARY_FILENAME).write_text(text, encoding="utf-8")
        self.manifest["analysis_summary"] = {"path": "../private.txt"}
        self.assertEqual(load_analysis_summary(self.workspace, self.view, self.manifest),
                         {"text": text.strip(), "source": "saved"})

    def test_legacy_without_summary_is_projected_without_writing_history(self):
        self.assertEqual(load_analysis_summary(self.workspace, self.view, self.manifest),
                         {"text": "未发现错误。", "source": "legacy_projection"})
        self.assertEqual(list(self.workspace.iterdir()), [])
        self.assertEqual(load_analysis_summary(self.workspace, None, self.manifest)["source"], "unavailable")
        self.manifest["status"] = "running"
        self.assertEqual(load_analysis_summary(self.workspace, self.view, self.manifest)["source"], "pending")

    def test_unreadable_saved_summary_is_not_replaced_by_a_pass(self):
        (self.workspace / ANALYSIS_SUMMARY_FILENAME).write_bytes(b"\xff")
        self.assertEqual(load_analysis_summary(self.workspace, self.view, self.manifest),
                         {"text": "", "source": "unavailable"})

    def test_summary_change_invalidates_record_html_cache_without_changing_snapshot(self):
        atomic_write_json(self.workspace / "manifest.json", self.manifest)
        atomic_write_json(self.workspace / "snapshot.json", {"run": self.manifest, "view": self.view})
        snapshot = (self.workspace / "snapshot.json").read_bytes()
        catalog = WorkbenchCatalog(self.root)
        first = catalog.html_signature(self.workspace.name)
        (self.workspace / ANALYSIS_SUMMARY_FILENAME).write_text("原回传总结。", encoding="utf-8")
        self.assertNotEqual(first, catalog.html_signature(self.workspace.name))
        self.assertEqual((self.workspace / "snapshot.json").read_bytes(), snapshot)

    def test_standalone_archive_embeds_the_same_safe_saved_text(self):
        text = '## 人员激励\n\n1. 金额差异 10 元。\n2. </script><script>alert(1)</script>'
        (self.workspace / ANALYSIS_SUMMARY_FILENAME).write_text(text, encoding="utf-8")
        atomic_write_json(self.workspace / "manifest.json", self.manifest)
        atomic_write_json(self.workspace / "snapshot.json", {"run": self.manifest, "view": self.view})
        template = self.root / "template.html"
        template.write_text('<html><head></head><body><script id="audit-data" type="application/json">{}</script></body></html>', encoding="utf-8")
        before = {p.name: p.read_bytes() for p in self.workspace.iterdir()}
        html = render_static_run_archive_html(self.workspace, template)
        marker = '<script id="audit-workbench-context" type="application/json">'
        context = json.loads(html.split(marker, 1)[1].split("</script>", 1)[0])
        self.assertEqual(context["analysis_summary"], {"text": text, "source": "saved"})
        self.assertNotIn('</script><script>alert(1)', html)
        self.assertEqual({p.name: p.read_bytes() for p in self.workspace.iterdir()}, before)

    def test_live_compatibility_migration_only_adds_display_metadata_and_skips_active_runs(self):
        atomic_write_json(self.workspace / "manifest.json", self.manifest)
        atomic_write_json(self.workspace / "snapshot.json", {"run": self.manifest, "view": self.view})
        (self.workspace / ANALYSIS_SUMMARY_FILENAME).write_text("## 人员激励\n\n1. 原总结。", encoding="utf-8")
        original = {p.name: p.read_bytes() for p in self.workspace.iterdir()}
        active = self.root / "20261109-active"
        atomic_write_json(active / "manifest.json", {"workspace_id": active.name, "status": "running"})
        active_bytes = (active / "manifest.json").read_bytes()
        backup = self.root / ".backup"
        self.assertEqual(publish_summary_displays(self.root, backup)["updated"], 0)
        self.assertEqual({p.name: p.read_bytes() for p in self.workspace.iterdir()}, original)
        report = publish_summary_displays(self.root, backup, apply=True)
        self.assertEqual(report["updated"], 1)
        self.assertEqual(report["pending"], [active.name])
        updated = json.loads((self.workspace / "manifest.json").read_bytes())
        self.assertEqual(updated.pop("analysis_summary_display")["text"], "## 人员激励\n\n1. 原总结。")
        self.assertEqual(updated, self.manifest)
        for name in ("snapshot.json", ANALYSIS_SUMMARY_FILENAME):
            self.assertEqual((self.workspace / name).read_bytes(), original[name])
        self.assertEqual((backup / self.workspace.name / "manifest.json").read_bytes(), original["manifest.json"])
        self.assertEqual((active / "manifest.json").read_bytes(), active_bytes)
        self.assertEqual(publish_summary_displays(self.root, backup, apply=True)["updated"], 0)


if __name__ == "__main__":
    unittest.main()
