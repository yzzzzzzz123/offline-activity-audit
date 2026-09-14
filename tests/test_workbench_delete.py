from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from audit_core import workbench_server
from audit_core.workbench_delete import RunDeletionConflict, prepare_run_deletion
from audit_core.workbench_html import render_static_run_archive
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import WorkbenchRunStore, atomic_write_json, read_json_file


class _RunDeletionFixture:
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="audit-delete-test-")
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name).resolve()
        self.root = self.project / "worktrees"
        self.root.mkdir()
        self.inputs = self.project / "input-oss"
        self.workspace_id = "20260907_1200_00-codex_high"
        self.workspace = self.root / self.workspace_id
        self.store = self.seed(self.workspace_id)

    def seed(self, workspace_id: str, *, status: str = "completed") -> WorkbenchRunStore:
        store = WorkbenchRunStore(
            self.root / workspace_id,
            run_id="20260907-delete-" + workspace_id,
            producer_model="codex",
            root_html=Path("offline-activity-audit.html"),
            workbench_url="http://127.0.0.1:8080/",
        )
        store.write_analysis("facts.json", {"secret": workspace_id}, label="测试事实")
        if status == "completed":
            store.complete(
                view_payload={"schema_version": "1.1", "sheets": []},
                scenarios=[],
                verification={"verified": True},
            )
        elif status == "failed":
            store.fail(RuntimeError("fixture failure"))
        return store

    def plan(self, workspace_id: str | None = None):
        return prepare_run_deletion(self.root, workspace_id or self.workspace_id, input_root=self.inputs)

    def job(self, *, status: str = "completed", result: bool = True, job_id: str = "a" * 24):
        receipt = self.root / ".intake" / "jobs" / f"{job_id}.json"
        source = self.inputs / job_id
        source.mkdir(parents=True)
        (source / "personnel.zip").write_bytes(b"test owned source")
        atomic_write_json(receipt, {
            "job_id": job_id,
            "run_id": self.store.manifest["run_id"],
            "status": status,
            "result": {"workspace_id": self.workspace_id} if result else None,
            "delivery": {"input_directory": str(self.project / "shared")},
        })
        return receipt, source


class RunDeletionTests(_RunDeletionFixture, unittest.TestCase):
    def test_removes_all_owned_files_and_standalone_git_without_touching_other_data(self):
        other = self.seed("20260907_1200_01-codex_high")
        before = (other.workspace / "manifest.json").read_bytes()
        render_static_run_archive(self.workspace, Path("offline-activity-audit.html"))
        atomic_write_json(self.workspace / ".git" / "objects" / "fixture", {"git": True})
        legacy = self.root / f"{self.workspace_id}.html"
        legacy.write_text("owned legacy", encoding="utf-8")
        review = self.root / ".reviews" / f"{self.workspace_id}.json"
        atomic_write_json(review, {"reviewed": True})
        shared = self.project / "shared" / "input.zip"
        shared.parent.mkdir()
        shared.write_bytes(b"do not delete")
        receipt, source = self.job()

        self.assertEqual(self.plan().execute(), {
            "workspace_id": self.workspace_id, "deleted": True, "deleted_jobs": 1,
        })
        self.assertTrue(all(not path.exists() for path in (self.workspace, legacy, review, receipt, source)))
        self.assertEqual((other.workspace / "manifest.json").read_bytes(), before)
        self.assertEqual(shared.read_bytes(), b"do not delete")
        self.assertFalse(list(self.project.rglob("*" + self.workspace_id + "*")))

    def test_failed_run_and_legacy_only_record_can_be_deleted(self):
        failed = self.seed("20260907_1200_02-codex_high", status="failed")
        self.plan(failed.workspace_id).execute()
        self.assertFalse(failed.workspace.exists())
        legacy = self.root / "20260906-old.html"
        legacy.write_text("legacy", encoding="utf-8")
        self.plan("20260906-old").execute()
        self.assertFalse(legacy.exists())

    def test_running_and_unknown_statuses_are_refused_before_any_deletion(self):
        for status in ("running", "created", "queued", "unknown"):
            with self.subTest(status=status):
                manifest = dict(self.store.manifest, status=status)
                atomic_write_json(self.workspace / "manifest.json", manifest)
                with self.assertRaises(RunDeletionConflict):
                    self.plan()
                self.assertTrue((self.workspace / "analysis" / "facts.json").is_file())

    def test_active_oss_job_is_refused_even_when_run_is_terminal(self):
        receipt, source = self.job(status="callback")
        with self.assertRaises(RunDeletionConflict):
            self.plan()
        self.assertTrue(receipt.is_file())
        self.assertTrue(source.is_dir())
        self.assertTrue(self.workspace.is_dir())

    def test_failed_early_oss_receipt_uses_unique_run_id(self):
        receipt, source = self.job(status="failed", result=False)
        self.plan().execute()
        self.assertFalse(receipt.exists())
        self.assertFalse(source.exists())

    def test_ambiguous_oss_retry_is_refused(self):
        receipt, source = self.job(status="failed", result=False)
        other = self.seed("20260907_1200_03-codex_high")
        atomic_write_json(other.manifest_path, dict(other.manifest, run_id=self.store.manifest["run_id"]))
        with self.assertRaises(RunDeletionConflict):
            self.plan()
        self.assertTrue(receipt.exists())
        self.assertTrue(source.exists())

    def test_explicit_other_run_ownership_is_not_overridden_by_shared_run_id(self):
        receipt, source = self.job()
        job = read_json_file(receipt)
        job["result"]["workspace_id"] = "20260907-other"
        atomic_write_json(receipt, job)
        self.plan().execute()
        self.assertTrue(receipt.exists())
        self.assertTrue(source.exists())

    def test_unsafe_ids_and_manifest_mismatch_are_refused(self):
        for value in ("..", ".", "../input", "/", "a/b", "a\\b", "a%2fb"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.plan(value)
        atomic_write_json(self.workspace / "manifest.json", dict(self.store.manifest, workspace_id="another"))
        with self.assertRaises(RunDeletionConflict):
            self.plan()
        self.assertTrue(self.workspace.exists())

    def test_unexpected_metadata_types_and_unsafe_job_id_are_refused(self):
        legacy = self.root / f"{self.workspace_id}.html"
        legacy.mkdir()
        with self.assertRaises(RunDeletionConflict):
            self.plan()
        legacy.rmdir()
        receipt, _ = self.job()
        atomic_write_json(receipt, dict(read_json_file(receipt), job_id="../../outside"))
        with self.assertRaises(RunDeletionConflict):
            self.plan()

    def test_external_symlink_is_never_traversed_or_removed(self):
        outside = self.project / "outside"
        outside.mkdir()
        sentinel = outside / "sentinel"
        sentinel.write_text("keep", encoding="utf-8")
        link = self.workspace / "external"
        try:
            link.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {exc}")
        with self.assertRaises(RunDeletionConflict):
            self.plan()
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "keep")
        self.assertTrue(link.is_symlink())

    @unittest.skipUnless(os.name == "nt", "Windows junction guard")
    def test_windows_junction_is_refused(self):
        outside = self.project / "outside"
        outside.mkdir()
        link = self.workspace / "junction"
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(outside)], check=True, capture_output=True)
        try:
            with self.assertRaises(RunDeletionConflict):
                self.plan()
            self.assertTrue(outside.is_dir())
        finally:
            link.rmdir()

    def test_gitlink_to_unregistered_repository_is_refused(self):
        (self.workspace / ".git").write_text("gitdir: ../../outside\n", encoding="utf-8")
        with self.assertRaises(RunDeletionConflict):
            self.plan()

    def test_file_lock_failure_keeps_primary_record_for_retry(self):
        with mock.patch("audit_core.workbench_delete._remove_directory", side_effect=PermissionError("locked")):
            with self.assertRaises(PermissionError):
                self.plan().execute()
        self.assertEqual(read_json_file(self.workspace / "manifest.json")["status"], "completed")
        self.plan().execute()
        self.assertFalse(self.workspace.exists())

    @unittest.skipUnless(os.name == "nt", "Windows read-only file cleanup")
    def test_readonly_git_objects_and_top_level_files_are_removed(self):
        for path in (self.workspace / ".git" / "objects" / "blob", self.workspace / "top.log"):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"readonly fixture")
            path.chmod(stat.S_IREAD)
        self.plan().execute()
        self.assertFalse(self.workspace.exists())


class GitRunDeletionTests(_RunDeletionFixture, unittest.TestCase):
    # Only run Git-specific cases here; ordinary deletion fixtures above do not
    # require Git and must not silently fall back when testing real registration.
    def setUp(self):
        super().setUp()
        self.git_executable = shutil.which("git")
        if not self.git_executable:
            self.skipTest("Git unavailable")
        self.git("init", "-b", "main")
        self.git("-c", "user.name=Deletion Fixture", "-c", "user.email=fixture@example.invalid", "commit", "--allow-empty", "-m", "fixture")
        self.head = self.git("rev-parse", "HEAD")

    def git(self, *args, check=True):
        result = subprocess.run(
            [self.git_executable, "-c", "core.hooksPath=/dev/null", "-C", str(self.project), *args],
            check=check, capture_output=True, text=True, encoding="utf-8",
        )
        return result.stdout.strip()

    def linked(self, *, branch="audit-delete-fixture", detached=False):
        workspace_id = "20260907_1300_00-codex_high"
        target = self.root / workspace_id
        self.git("worktree", "add", *( ["--detach"] if detached else ["-b", branch]), str(target), "HEAD")
        self.seed(workspace_id)
        return target

    def test_registered_worktree_branch_reflog_and_admin_are_completely_removed(self):
        target = self.linked()
        self.git("config", "branch.audit-delete-fixture.description", "owned branch metadata")
        keep = self.project / "unrelated-tree"
        self.git("worktree", "add", "-b", "keep-other", str(keep), "HEAD")
        self.plan(target.name).execute()
        self.assertFalse(target.exists())
        self.assertNotIn(str(target).replace("\\", "/"), self.git("worktree", "list", "--porcelain"))
        self.assertEqual(self.git("branch", "--list", "audit-delete-fixture"), "")
        self.assertFalse((self.project / ".git" / "logs" / "refs" / "heads" / "audit-delete-fixture").exists())
        self.assertFalse((self.project / ".git" / "worktrees" / target.name).exists())
        self.assertEqual(self.git("config", "--get", "branch.audit-delete-fixture.description", check=False), "")
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)
        self.assertTrue(keep.is_dir())
        self.assertIn("keep-other", self.git("branch", "--list"))

    def test_detached_worktree_can_be_deleted_without_touching_main(self):
        target = self.linked(detached=True)
        self.plan(target.name).execute()
        self.assertFalse(target.exists())
        self.assertEqual(self.git("rev-parse", "HEAD"), self.head)

    def test_locked_worktree_is_refused_before_removing_files(self):
        target = self.linked()
        self.git("worktree", "lock", str(target))
        with self.assertRaises(RunDeletionConflict):
            self.plan(target.name)
        self.assertTrue((target / "analysis" / "facts.json").exists())

    def test_shared_branch_is_refused(self):
        target = self.linked()
        self.git("worktree", "add", "--force", str(self.project / "other"), "audit-delete-fixture")
        with self.assertRaises(RunDeletionConflict):
            self.plan(target.name)
        self.assertTrue(target.exists())

    def test_branch_lock_failure_preserves_registration_and_record_for_retry(self):
        target = self.linked()
        branch_lock = self.project / ".git" / "refs" / "heads" / "audit-delete-fixture.lock"
        branch_lock.touch()
        with self.assertRaises(RunDeletionConflict):
            self.plan(target.name).execute()
        self.assertTrue((target / "manifest.json").is_file())
        self.assertTrue((target / ".git").is_file())
        self.assertIn("refs/heads/audit-delete-fixture", self.git("worktree", "list", "--porcelain"))
        branch_lock.unlink()
        self.plan(target.name).execute()
        self.assertFalse(target.exists())


class RunDeletionHTTPTests(_RunDeletionFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = WorkbenchHTTPServer(("127.0.0.1", 0), Handler, catalog=WorkbenchCatalog(self.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def request(self, path, *, method="GET", payload=None, headers=None):
        request_headers = {"Content-Type": "application/json", "Origin": self.base,
                           "X-Offline-Audit-Delete-Token": self.server.deletion_token}
        request_headers.update(headers or {})
        request = urllib.request.Request(self.base + path, method=method, headers=request_headers,
                                         data=None if payload is None else json.dumps(payload).encode("utf-8"))
        try:
            response = self.opener.open(request, timeout=5)
        except urllib.error.HTTPError as exc:
            response = exc
        with response:
            return response.status, response.read(), response.headers

    def delete(self, **kwargs):
        kwargs.setdefault("payload", {"confirm_workspace_id": self.workspace_id})
        return self.request(f"/api/runs/{self.workspace_id}", method="DELETE", **kwargs)

    def test_full_http_delete_invalidates_all_resources_and_caches(self):
        self.server.catalog.set_manual_review(self.workspace_id, True)
        routes = [f"/api/runs/{self.workspace_id}{tail}" for tail in (
            "", "/snapshot", "/log", "/analysis?path=analysis/facts.json", "/checkpoints/001-bootstrap", "/events",
        )]
        for route in ["/api/runs", f"/?run={self.workspace_id}", *routes]:
            self.assertEqual(self.request(route)[0], 200, route)
        self.assertTrue(self.server._html_cache)
        self.assertEqual(self.delete()[0], 200)
        self.assertFalse(self.server._html_cache)
        for attribute in ("_manifest_cache", "_review_cache", "_run_summary_cache", "_snapshot_document_cache", "_resource_document_cache"):
            self.assertFalse(getattr(self.server.catalog, attribute), attribute)
        for function in (workbench_server._read_completed_snapshot_projection, workbench_server._read_analysis_allowlist, workbench_server._gzip_payload):
            self.assertEqual(function.cache_info().currsize, 0)
        self.assertEqual(json.loads(self.request("/api/runs")[1]), {"runs": [], "count": 0})
        for route in [f"/?run={self.workspace_id}", *routes]:
            self.assertEqual(self.request(route)[0], 404, route)
        self.assertEqual(self.delete()[0], 404)
        status = self.request(f"/api/runs/{self.workspace_id}/manual-review", method="POST", payload={"reviewed": True})[0]
        self.assertEqual(status, 404)
        self.assertFalse((self.root / ".reviews" / f"{self.workspace_id}.json").exists())

    def test_missing_nonce_cross_origin_and_dns_rebinding_are_rejected(self):
        for headers in (
            {"X-Offline-Audit-Delete-Token": ""},
            {"Origin": ""},
            {"Origin": "http://evil.invalid"},
            {"Origin": "http://evil.invalid", "Host": "evil.invalid"},
            {"Sec-Fetch-Site": "cross-site"},
        ):
            with self.subTest(headers=headers):
                self.assertEqual(self.delete(headers=headers)[0], 403)
                self.assertTrue(self.workspace.exists())

    def test_confirmation_and_request_shape_are_strict(self):
        for payload in ({}, {"confirm_workspace_id": "other"}, {"confirm_workspace_id": self.workspace_id, "force": True}):
            with self.subTest(payload=payload):
                self.assertEqual(self.delete(payload=payload)[0], 400)
                self.assertTrue(self.workspace.exists())
        self.assertEqual(self.delete(headers={"Content-Type": "text/plain"})[0], 400)

    def test_corrupt_metadata_and_permission_failure_return_conflict_not_success(self):
        with mock.patch("audit_core.workbench_delete._remove_directory", side_effect=PermissionError("locked")):
            self.assertEqual(self.delete()[0], 409)
        self.assertTrue(self.workspace.exists())
        (self.workspace / "manifest.json").write_text("broken json", encoding="utf-8")
        self.assertEqual(self.delete()[0], 409)

    def test_review_cannot_recreate_marker_during_delete(self):
        from audit_core.workbench_delete import RunDeletionPlan
        entered = threading.Event()
        proceed = threading.Event()
        original = RunDeletionPlan.execute
        errors = []

        def delayed(plan):
            entered.set()
            self.assertTrue(proceed.wait(5))
            return original(plan)

        def review():
            try:
                self.server.catalog.set_manual_review(self.workspace_id, True)
            except FileNotFoundError:
                errors.append("not found")

        with mock.patch.object(RunDeletionPlan, "execute", delayed):
            deletion = threading.Thread(target=lambda: self.delete())
            deletion.start()
            self.assertTrue(entered.wait(5))
            reviewer = threading.Thread(target=review)
            reviewer.start()
            proceed.set()
            deletion.join(5)
            reviewer.join(5)
        self.assertFalse(deletion.is_alive())
        self.assertFalse(reviewer.is_alive())
        self.assertEqual(errors, ["not found"])
        self.assertFalse(self.workspace.exists())
        self.assertFalse((self.root / ".reviews" / f"{self.workspace_id}.json").exists())


if __name__ == "__main__":
    unittest.main()
