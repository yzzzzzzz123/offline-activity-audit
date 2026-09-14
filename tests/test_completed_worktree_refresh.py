from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from audit_core.analysis_summary_backfill import backfill_analysis_summaries, build_parser
from audit_core.common import AuditError
from audit_core.completed_worktree_refresh import refresh_completed_worktree_archives
from audit_core.html_report import DATA_CLOSE, DATA_OPEN
from audit_core.workbench_html import SYSTEM_VERSION, customer_projection_error_count
from audit_core.workbench_store import ANALYSIS_SUMMARY_FILENAME, atomic_write_json, atomic_write_text


class CompletedWorktreeRefreshTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.root = self.project / "worktrees"
        self.root.mkdir()
        self.template = self.project / "offline-activity-audit.html"
        self.template.write_text(f"<html><head></head><body>{DATA_OPEN}{{}}{DATA_CLOSE}</body></html>", encoding="utf-8")
        self.backup = self.project / "artifacts" / "refresh-backup"

    def create_workspace(self, name="20260910_1756_59-gpt6astra_medium", status="completed"):
        workspace = self.root / name
        workspace.mkdir()
        archive = "HX202601160053-广州南雄维护费用申请-9.zip"
        issues = [
            {"code": "scenario_unconfirmed", "title": "核销类型待确认", "source_files": [archive],
             "observed": "材料包申报类型为维护费用；价格补差：合同每支补差3元；人员激励：按比例核销；具体核销场景需确认。"},
            {"code": "missing_material", "title": "POS 销售电子表未提交", "source_files": [],
             "observed": "POS 销售电子表未提交。"},
        ]
        view = {"sheets": [{"name": "维护费用材料诊断", "scenario": "maintenance_fee", "audit_type_label": "维护费用",
            "projection_kind": "material_diagnostic", "source_archive": archive,
            "diagnostic_issues": issues,
            "rows": [{"status": "issue", "section": "detail", "heading": f"问题：{issue['title']}",
                      "values": [issue["observed"], "处理方式：补交 POS 销售电子表"]} for issue in issues],
            "audit_counts": {"source_row_count": 2, "error_count": 2, "detail_error_count": 2, "context_error_count": 0}}]}
        manifest = {"schema_version": "1.0", "workspace_id": name, "status": status, "scenarios": ["maintenance_fee"],
                    "scenario_count": 1, "error_count": 2, "conclusion": "human_review", "snapshot_sha256": "old-hash"}
        snapshot = {"schema_version": "1.0", "run": deepcopy(manifest), "view": view,
                    "verification": {"error_count": 2, "business_decision": "human_review"},
                    "analysis_files": [{"path": "analysis/evidence.json"}], "dom_checkpoints": []}
        snapshot["run"]["customer_projection"] = {"schema_version": "stale"}
        atomic_write_json(workspace / "snapshot.json", snapshot)
        manifest["snapshot_sha256"] = hashlib.sha256((workspace / "snapshot.json").read_bytes()).hexdigest()
        atomic_write_json(workspace / "manifest.json", manifest)
        atomic_write_json(workspace / "analysis" / "evidence.json", {"raw_business_evidence": "原始文件与金额"})
        atomic_write_text(workspace / "logs" / "run.log", "既有业务日志\n")
        atomic_write_text(workspace / ANALYSIS_SUMMARY_FILENAME, "旧版摘要：多类型候选与建议\n")
        atomic_write_text(workspace / "offline-activity-audit.html", "旧版静态页面\n")
        return workspace

    def workspace_bytes(self):
        return {path.relative_to(self.root).as_posix(): path.read_bytes() for path in self.root.rglob("*") if path.is_file()}

    def refresh(self, **kwargs):
        return refresh_completed_worktree_archives(self.root, root_html=self.template, backup_dir=self.backup, **kwargs)

    def test_preview_is_read_only_and_all_changed_files_are_backed_up_before_first_write(self):
        first = self.create_workspace()
        second = self.create_workspace("20260910_1856_59-gpt6astra_medium")
        before = self.workspace_bytes()
        preview = self.refresh()
        self.assertEqual(preview["would_update"], 2)
        self.assertFalse(self.backup.exists())
        self.assertEqual(self.workspace_bytes(), before)
        self.assertEqual([item["error_count_after"] for item in preview["results"]], [1, 1])
        for item in preview["results"]:
            self.assertEqual(len(item["files"]), 4)
            self.assertEqual(item["summary"], "## 维护费用\n\n- POS 销售电子表未提交。")
        calls = []

        def write_after_all_backups(path, content):
            index = json.loads((self.backup / "backup-index.json").read_text(encoding="utf-8"))
            self.assertEqual(len(index["files"]), 8)
            for entry in index["files"]:
                self.assertEqual((self.backup / entry["path"]).read_bytes(), before[entry["path"]])
            calls.append(path)
            atomic_write_text(path, content)

        with mock.patch("audit_core.completed_worktree_refresh.atomic_write_text", side_effect=write_after_all_backups):
            result = self.refresh(apply=True)
        self.assertEqual(result["updated"], 2)
        self.assertEqual(len(calls), 8)
        for workspace in (first, second):
            original_snapshot = json.loads(before[f"{workspace.name}/snapshot.json"])
            snapshot = json.loads((workspace / "snapshot.json").read_bytes())
            manifest = json.loads((workspace / "manifest.json").read_bytes())
            self.assertEqual(snapshot["view"], original_snapshot["view"])
            self.assertEqual(snapshot["verification"], original_snapshot["verification"])
            self.assertEqual(snapshot["analysis_files"], original_snapshot["analysis_files"])
            self.assertEqual(snapshot["run"]["error_count"], 2)
            self.assertEqual(manifest["error_count"], 2)
            self.assertEqual(manifest["conclusion"], "human_review")
            self.assertNotIn("customer_projection", snapshot["run"])
            snapshot_hash = hashlib.sha256((workspace / "snapshot.json").read_bytes()).hexdigest()
            self.assertEqual(manifest["snapshot_sha256"], snapshot_hash)
            self.assertEqual(manifest["customer_projection"], {"schema_version": "1.0", "policy_version": SYSTEM_VERSION,
                "source_snapshot_sha256": snapshot_hash, "error_count": 1})
            self.assertEqual(customer_projection_error_count(manifest), 1)
            summary = (workspace / ANALYSIS_SUMMARY_FILENAME).read_bytes()
            self.assertEqual(manifest["analysis_summary"], snapshot["run"]["analysis_summary"])
            self.assertEqual(manifest["analysis_summary"]["sha256"], hashlib.sha256(summary).hexdigest())
            self.assertEqual(manifest["analysis_summary"]["size"], len(summary))
            html = (workspace / "offline-activity-audit.html").read_text(encoding="utf-8")
            embedded = json.loads(html.split(DATA_OPEN, 1)[1].split(DATA_CLOSE, 1)[0])
            self.assertEqual(len(embedded["sheets"][0]["rows"]), 1)
            self.assertNotIn("人员激励：", html)
            for name in ("analysis/evidence.json", "logs/run.log"):
                self.assertEqual((workspace / name).read_bytes(), before[f"{workspace.name}/{name}"])

    def test_repeated_preview_and_apply_are_idempotent_with_existing_backup(self):
        self.create_workspace()
        self.refresh(apply=True)
        once = self.workspace_bytes()
        backups = {str(path.relative_to(self.backup)): path.read_bytes() for path in self.backup.rglob("*") if path.is_file()}
        preview = self.refresh()
        self.assertEqual(preview["would_update"], 0)
        self.assertEqual(preview["unchanged"], 1)
        result = self.refresh(apply=True)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["unchanged"], 1)
        self.assertEqual(self.workspace_bytes(), once)
        self.assertEqual({str(path.relative_to(self.backup)): path.read_bytes() for path in self.backup.rglob("*") if path.is_file()}, backups)

    def test_failed_preparation_of_any_completed_workspace_prevents_all_writes(self):
        self.create_workspace()
        broken = self.create_workspace("20260910_1856_59-gpt6astra_medium")
        snapshot = json.loads((broken / "snapshot.json").read_bytes())
        snapshot["view"] = None
        atomic_write_json(broken / "snapshot.json", snapshot)
        before = self.workspace_bytes()
        with self.assertRaisesRegex(AuditError, "结构无效"):
            self.refresh(apply=True)
        self.assertEqual(self.workspace_bytes(), before)
        self.assertFalse(self.backup.exists())

    def test_snapshot_digest_and_run_identity_must_be_valid_before_refresh(self):
        workspace = self.create_workspace()
        original = self.workspace_bytes()
        for defect in ("hash", "workspace_id", "status"):
            with self.subTest(defect=defect):
                for relative, content in original.items():
                    (self.root / relative).write_bytes(content)
                if defect == "hash":
                    manifest = json.loads((workspace / "manifest.json").read_bytes())
                    manifest["snapshot_sha256"] = "stale"
                    atomic_write_json(workspace / "manifest.json", manifest)
                else:
                    snapshot = json.loads((workspace / "snapshot.json").read_bytes())
                    snapshot["run"][defect] = "wrong-run" if defect == "workspace_id" else "running"
                    atomic_write_json(workspace / "snapshot.json", snapshot)
                before = self.workspace_bytes()
                with self.assertRaisesRegex(AuditError, "摘要校验失败|身份或完成状态不一致"):
                    self.refresh(apply=True)
                self.assertEqual(self.workspace_bytes(), before)
                self.assertFalse(self.backup.exists())

    def test_windows_junctions_in_workspace_or_backup_parent_are_rejected(self):
        workspace = self.create_workspace()
        before = self.workspace_bytes()
        for junction in (workspace, self.project / "artifacts", workspace / "snapshot.json"):
            with self.subTest(junction=junction):
                with mock.patch.object(type(workspace), "is_junction", lambda path: path == junction, create=True):
                    with self.assertRaisesRegex(AuditError, "目录联接"):
                        self.refresh(apply=True)
                self.assertEqual(self.workspace_bytes(), before)
                self.assertFalse(self.backup.exists())

    def test_noncompleted_directories_are_skipped_even_with_incomplete_snapshot(self):
        active = self.create_workspace(status="running")
        (active / "snapshot.json").write_bytes(b"{unfinished")
        before = self.workspace_bytes()
        result = self.refresh(apply=True)
        self.assertEqual(result["updated"], 0)
        self.assertEqual(result["skipped"], [{"workspace_id": active.name, "reason": "not_completed"}])
        self.assertEqual(self.workspace_bytes(), before)
        self.assertFalse(self.backup.exists())

    def test_apply_requires_new_backup_within_project_artifacts(self):
        self.create_workspace()
        before = self.workspace_bytes()
        for backup, message in ((None, "显式提供"), (self.project / "outside", "artifacts"),
                                (self.project / "artifacts", "artifacts")):
            with self.subTest(backup=backup), self.assertRaisesRegex(AuditError, message):
                refresh_completed_worktree_archives(self.root, root_html=self.template, backup_dir=backup, apply=True)
        self.backup.mkdir(parents=True)
        with self.assertRaisesRegex(AuditError, "已存在"):
            self.refresh(apply=True)
        self.assertEqual(self.workspace_bytes(), before)

    def test_missing_artifacts_are_recorded_in_backup_and_created_once(self):
        workspace = self.create_workspace()
        (workspace / ANALYSIS_SUMMARY_FILENAME).unlink()
        (workspace / "offline-activity-audit.html").unlink()
        self.refresh(apply=True)
        records = json.loads((self.backup / "backup-index.json").read_bytes())["files"]
        missing = [entry for entry in records if not entry["existed"]]
        self.assertEqual(len(missing), 2)
        for entry in missing:
            self.assertIsNone(entry["before_sha256"])
            self.assertFalse((self.backup / entry["path"]).exists())
            self.assertTrue((self.root / entry["path"]).is_file())
        self.assertEqual(self.refresh()["would_update"], 0)

    def test_write_failure_rolls_back_already_written_artifacts(self):
        self.create_workspace()
        before = self.workspace_bytes()
        calls = []

        def fail_after_second_write(path, content):
            atomic_write_text(path, content)
            calls.append(path)
            if len(calls) == 2:
                raise OSError("simulated write failure")

        with mock.patch("audit_core.completed_worktree_refresh.atomic_write_text", side_effect=fail_after_second_write):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.refresh(apply=True)
        self.assertEqual(self.workspace_bytes(), before)
        self.assertTrue((self.backup / "backup-index.json").is_file())

    def test_backup_failure_prevents_every_worktree_write(self):
        self.create_workspace()
        before = self.workspace_bytes()
        original_open = type(self.backup).open
        backup_writes = []

        def fail_second_backup(path, mode="r", *args, **kwargs):
            if mode == "xb" and self.backup in path.parents:
                backup_writes.append(path)
                if len(backup_writes) == 2:
                    raise OSError("simulated backup failure")
            return original_open(path, mode, *args, **kwargs)

        with mock.patch.object(type(self.backup), "open", fail_second_backup):
            with self.assertRaisesRegex(OSError, "simulated backup"):
                self.refresh(apply=True)
        self.assertEqual(self.workspace_bytes(), before)
        self.assertFalse((self.backup / "backup-index.json").exists())

    def test_optional_mode_and_cli_preserve_default_summary_only_behavior(self):
        workspace = self.create_workspace()
        original_html = (workspace / "offline-activity-audit.html").read_bytes()
        backfill_analysis_summaries(self.root, apply=True)
        self.assertEqual((workspace / "offline-activity-audit.html").read_bytes(), original_html)
        self.assertNotIn("customer_projection", json.loads((workspace / "manifest.json").read_bytes()))
        self.assertFalse(self.backup.exists())
        result = backfill_analysis_summaries(self.root, refresh_archives=True, backup_dir=self.backup, root_html=self.template)
        self.assertTrue(result["refresh_archives"])
        self.assertEqual(result["would_update"], 1)
        args = build_parser().parse_args(["--refresh-archives", "--backup-dir", str(self.backup)])
        self.assertTrue(args.refresh_archives)
        self.assertEqual(args.backup_dir, self.backup)
        self.assertNotIn("两句式", build_parser().description)
        with self.assertRaisesRegex(AuditError, "一起使用"):
            backfill_analysis_summaries(self.root, apply=True, backup_dir=self.backup)


if __name__ == "__main__":
    unittest.main()
