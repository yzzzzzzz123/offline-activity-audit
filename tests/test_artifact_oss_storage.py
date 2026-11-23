"""无真实网络、无模型与无回调的产物主存储回归。"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import hashlib
import http.client
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from urllib.parse import quote
from unittest import mock
import zipfile

from audit_core.artifact_oss import (ArtifactStorageError, OWNER, PREFIX,
                                     canonical, check_key, configuration)
from audit_core.artifact_repository import ArtifactRepository, index_key, tree_inventory
from audit_core.artifact_runtime import catalog_type, handler_type, create_catalog, StorageManager
from audit_core.workbench_server import WorkbenchCatalog, WorkbenchHTTPServer, Handler


class MemoryOSS:
    def __init__(self):
        self.objects = {}
        self.fail_put = False

    def close(self):
        pass

    def head(self, key):
        check_key(key)
        if key not in self.objects:
            return None
        value = self.objects[key]
        return {"size": len(value), "metadata": {"managed-by": OWNER,
                "sha256": hashlib.sha256(value).hexdigest()}}

    def read(self, key, **_):
        check_key(key)
        return self.objects[key]

    def list(self, section):
        for key, value in sorted(self.objects.items()):
            if key.startswith(PREFIX+section+"/"):
                yield {"key": key, "etag": hashlib.sha256(value).hexdigest(), "size": len(value)}

    def read_to(self, key, output, *, expected_size=None, expected_sha=None, limit=4*1024**3):
        value = self.read(key)
        if (len(value) > limit or expected_size is not None and len(value) != expected_size
                or expected_sha and hashlib.sha256(value).hexdigest() != expected_sha):
            raise ArtifactStorageError("模拟完整性不符")
        output.write(value)
        return hashlib.sha256(value).hexdigest()

    def put_immutable(self, key, source, *, size, sha, **_):
        check_key(key)
        if self.fail_put:
            raise ArtifactStorageError("模拟存储不可用")
        value = source.read()
        if len(value) != size or hashlib.sha256(value).hexdigest() != sha:
            raise ArtifactStorageError("模拟上传内容不符")
        if key in self.objects and self.objects[key] != value:
            raise ArtifactStorageError("模拟禁止覆盖")
        self.objects[key] = value

    def put_json(self, key, value):
        raw = canonical(value)
        self.put_immutable(key, io.BytesIO(raw), size=len(raw), sha=hashlib.sha256(raw).hexdigest())

    def delete_index(self, key):
        if not key.startswith(PREFIX+"index/"):
            raise ArtifactStorageError("只能删除索引")
        del self.objects[key]


class ArtifactStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)/"project"
        self.root = self.project/"worktrees"
        self.root.mkdir(parents=True)
        self.storage = MemoryOSS()
        self.repository = ArtifactRepository(self.storage, self.root, temporary_root=self.temporary.name)
        self.name = "HX202611170001-参半产物-20261117_1122_33"
        self.workspace = self.root/self.name
        self.fixture()

    def fixture(self, *, status="completed"):
        workspace = self.workspace
        (workspace/"analysis").mkdir(parents=True)
        (workspace/"logs").mkdir()
        original = self.project/"input"/("a"*24)/"核销材料.zip"
        original.parent.mkdir(parents=True)
        self.material = "原始资料，不是模型回答。".encode("utf-8")
        with zipfile.ZipFile(original, "w") as archive:
            archive.writestr("资料.txt", self.material)
        archive_sha = hashlib.sha256(original.read_bytes()).hexdigest()
        self.manifest = {"schema_version": "1.2", "workspace_id": self.name,
            "run_id": "20261117-test-storage", "producer_model": "codex", "audit_model": "storage-test-model",
            "status": status, "created_at": "2026-11-17T00:00:00Z",
            "analysis_started_at": "2026-11-17T00:00:01Z", "completed_at": "2026-11-17T00:00:02Z",
            "updated_at": "2026-11-17T00:00:02Z", "source_archives": [original.name],
            "scenarios": [], "scenario_count": 0, "error_count": 0}
        snapshot = {"schema_version": "1.1", "run": self.manifest,
                    "view": {"schema_version": "1.1", "title": "测试档案", "sheets": []},
                    "analysis_files": [{"path": "analysis/test.json", "label": "测试原始分析"}],
                    "recent_events": [], "dom_checkpoints": [], "verification": {"passed": True}}
        cases = [{"source_archive": original.name, "archive_sha256": archive_sha,
            "inventory": [{"file_id": "f001", "source_file": "资料.txt", "sha256": hashlib.sha256(self.material).hexdigest()}]}]
        for name, value in [("manifest.json", self.manifest), ("snapshot.json", snapshot),
                            ("analysis/input-cases.json", cases), ("analysis/test.json", {"text": "原样保留"})]:
            (workspace/name).write_bytes(canonical(value))
        (workspace/"logs/run.log").write_text("执行记录原文\n", encoding="utf-8")
        (workspace/"logs/events.jsonl").write_text("", encoding="utf-8")
        (workspace/"ai-analysis-summary.md").write_text("# 原有总结\n不重写。\n", encoding="utf-8")
        (workspace/"offline-activity-audit.html").write_text("<!doctype html><html lang=zh-CN><body>原有静态档案</body></html>", encoding="utf-8")
        self.original = original
        self.before = {row["path"]: (workspace/row["path"]).read_bytes() for row in tree_inventory(workspace)}

    def archive(self):
        summary = WorkbenchCatalog(self.root).list_runs()[0]
        self.sources = self.repository.source_inventory()
        return self.repository.archive(self.name, summary, self.sources)

    def test_upload_and_restore_preserve_every_original_byte(self):
        value = self.archive()
        self.assertEqual({row["path"] for row in value["files"]}, set(self.before))
        self.assertEqual(self.storage.read(value["reports"]["offline-activity-audit.html"]["key"]), self.before["offline-activity-audit.html"])
        self.assertEqual(len(value["originals"]), 1)
        output = Path(self.temporary.name)/"restored"
        self.repository.restore(value, output)
        self.assertEqual({name: (output/name).read_bytes() for name in self.before}, self.before)
        self.assertTrue(self.original.exists())
        self.assertTrue(self.workspace.exists())

    def test_prune_only_after_complete_remote_verification(self):
        self.archive()
        result = self.repository.prune_workspace(self.name)
        self.assertEqual(result["files"], len(self.before))
        self.assertFalse(self.workspace.exists())
        self.assertTrue(self.original.exists())
        self.assertEqual(self.repository.prune_sources(self.sources), 1)
        self.assertFalse(self.original.exists())
        self.repository.verify(self.name)

    def test_read_scope_removes_all_downloaded_files(self):
        self.archive()
        self.repository.prune_workspace(self.name)
        with self.repository.request_scope():
            restored = self.repository.checkout(self.name)
            self.assertEqual((restored/"manifest.json").read_bytes(), self.before["manifest.json"])
            self.assertIs(restored, self.repository.checkout(self.name))
            inputs = self.repository.preview_inputs(self.name)
            self.assertEqual(len(list(inputs.rglob("*.zip"))), 1)
        self.assertFalse(restored.exists())
        self.assertFalse(inputs.exists())
        with self.assertRaises(ArtifactStorageError):
            self.repository.checkout(self.name)

    def test_failed_upload_never_commits_index_or_deletes_originals(self):
        self.storage.fail_put = True
        with self.assertRaises(ArtifactStorageError):
            self.archive()
        self.assertNotIn(index_key(self.name), self.storage.objects)
        self.assertTrue(self.original.exists())
        self.assertEqual((self.workspace/"manifest.json").read_bytes(), self.before["manifest.json"])

    def test_corrupt_remote_bundle_blocks_all_pruning(self):
        value = self.archive()
        self.storage.objects[value["bundle"]["key"]] = b"damaged"
        with self.assertRaises(ArtifactStorageError):
            self.repository.prune_workspace(self.name)
        self.assertEqual({name: (self.workspace/name).read_bytes() for name in self.before}, self.before)

    def test_modified_local_history_is_not_overwritten_or_pruned(self):
        value = self.archive()
        (self.workspace/"logs/run.log").write_text("changed", encoding="utf-8")
        with self.assertRaises(ArtifactStorageError):
            self.archive()
        with self.assertRaises(ArtifactStorageError):
            self.repository.prune_workspace(self.name)
        self.assertEqual(self.repository.get(self.name), value)

    def test_active_manifest_and_run_pid_are_preserved(self):
        self.manifest["status"] = "running"
        (self.workspace/"manifest.json").write_bytes(canonical(self.manifest))
        with self.assertRaises(ArtifactStorageError):
            self.archive()
        self.manifest["status"] = "completed"
        (self.workspace/"manifest.json").write_bytes(canonical(self.manifest))
        (self.workspace/"run.pid").write_bytes(b"{}")
        with self.assertRaises(ArtifactStorageError):
            self.archive()

    def test_callback_in_progress_blocks_archival(self):
        jobs = self.root/".intake/jobs"
        jobs.mkdir(parents=True)
        (jobs/("a"*24+".json")).write_bytes(canonical({"job_id": "a"*24, "status": "callback",
             "run_id": self.manifest["run_id"], "result": {"workspace_id": self.name}}))
        with self.assertRaises(ArtifactStorageError):
            self.archive()

    def test_traversal_in_remote_index_is_rejected(self):
        value = self.archive()
        value = copy.deepcopy(value)
        value["files"][0]["path"] = "../escaped.txt"
        self.storage.objects[index_key(self.name)] = canonical(value)
        with self.assertRaises(ArtifactStorageError):
            self.repository.get(self.name)

    def test_prefix_protects_product_reference_and_other_deployments(self):
        for key in ["offline-verify/product-reference/test.json", PREFIX+"../a", PREFIX+"a//b", "other/a"]:
            with self.subTest(key=key), self.assertRaises(ArtifactStorageError):
                check_key(key)

    def test_changed_input_and_missing_location_index_are_preserved(self):
        self.archive()
        self.original.write_bytes(b"new file")
        with self.assertRaises(ArtifactStorageError):
            self.repository.prune_sources(self.sources)
        self.assertTrue(self.original.exists())

    def test_without_configuration_original_catalog_and_handler_are_used(self):
        with mock.patch("audit_core.artifact_runtime.configuration", return_value=None), mock.patch(
                "audit_core.artifact_runtime.ArtifactOSS", side_effect=AssertionError("不应连接真实OSS")):
            catalog, handler = create_catalog(self.root, WorkbenchCatalog, Handler)
        self.assertIs(type(catalog), WorkbenchCatalog)
        self.assertIs(handler, Handler)

    def test_read_only_manager_does_not_start_background_work(self):
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        manager = StorageManager(catalog, self.repository, cleanup_enabled=False)
        manager.start(None)
        self.assertIsNone(manager.thread)
        self.assertFalse(self.storage.objects)

    def test_manager_archives_and_prunes_only_terminal_owned_inputs(self):
        jobs = self.root/".intake/jobs"
        jobs.mkdir(parents=True)
        (jobs/("a"*24+".json")).write_bytes(canonical({"job_id": "a"*24, "status": "completed",
            "run_id": self.manifest["run_id"], "result": {"workspace_id": self.name}}))
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        manager = StorageManager(catalog, self.repository, cleanup_enabled=True)
        self.assertEqual(manager.sweep(), 1)
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.original.exists())
        self.assertTrue((jobs/("a"*24+".json")).exists())
        self.assertEqual(catalog.list_runs()[0]["workspace_id"], self.name)

    def test_shared_unowned_original_is_not_pruned_by_runtime(self):
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        manager = StorageManager(catalog, self.repository, cleanup_enabled=True)
        self.assertEqual(manager.sweep(), 1)
        self.assertFalse(self.workspace.exists())
        self.assertTrue(self.original.exists())

    def test_early_failed_job_archives_owned_original_without_inventing_a_report(self):
        jobs = self.root/".intake/jobs"
        jobs.mkdir(parents=True)
        (jobs/("a"*24+".json")).write_bytes(canonical({"job_id": "a"*24, "status": "failed",
            "run_id": "unrelated-failed-download", "result": None}))
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        manager = StorageManager(catalog, self.repository, cleanup_enabled=True)
        manager.sweep()
        self.assertFalse(self.original.exists())
        self.assertTrue((jobs/("a"*24+".json")).exists())
        self.assertEqual(len(list(self.storage.list("index"))), 1)

    def test_unresolved_terminal_job_is_not_owned_only_by_a_reused_run_id(self):
        jobs = self.root/".intake/jobs"
        jobs.mkdir(parents=True)
        (jobs/("a"*24+".json")).write_bytes(canonical({"job_id": "a"*24, "status": "failed",
            "run_id": self.manifest["run_id"], "result": None}))
        self.assertEqual(self.repository._jobs(self.name, self.manifest["run_id"]), [])

    def test_repeated_archiving_is_idempotent(self):
        first = self.archive()
        count = len(self.storage.objects)
        second = self.archive()
        self.assertEqual(first, second)
        self.assertEqual(len(self.storage.objects), count)

    def legacy_model_usage_entry(self, *, unknown=False):
        records = [
            {"model": "deepseek-flash", "usage": {"input_tokens": 12, "output_tokens": 3}},
            {"model": "qwen3.8-max", "usage": {"input_tokens": 20, "output_tokens": 5}},
        ]
        if unknown:
            records[0].pop("model")
        (self.workspace / "analysis/model-metrics.jsonl").write_bytes(
            b"".join(canonical(row) for row in records))
        entry = self.archive()
        del entry["summary"]["token_usage"]["by_model"]
        # 模拟在分模型字段上线前已经封存的索引；只改测试内存替身。
        self.storage.objects[index_key(self.name)] = canonical(entry)
        return entry

    def test_legacy_model_usage_backfill_preserves_original_index_and_totals(self):
        entry = self.legacy_model_usage_entry()
        original_objects = dict(self.storage.objects)
        document = self.repository.save_model_usage(self.name, self.workspace, entry)
        self.assertEqual(document["usage"]["total_tokens"], 40)
        projected = self.repository.display_summary(self.name, entry)
        self.assertEqual(projected["token_usage"]["total_tokens"], 40)
        self.assertEqual({row["model"]: row["total_tokens"] for row in projected["token_usage"]["by_model"]},
                         {"deepseek-flash": 15, "qwen3.8-max": 25})
        self.assertNotIn("by_model", entry["summary"]["token_usage"])
        self.assertEqual({key: self.storage.objects[key] for key in original_objects}, original_objects)
        self.assertEqual(len(self.storage.objects), len(original_objects) + 1)
        projected["token_usage"]["by_model"][0]["total_tokens"] = 999
        self.assertEqual(self.repository.display_summary(self.name, entry)["token_usage"]["by_model"][0]["total_tokens"], 15)

    def test_oss_list_uses_light_usage_sidecar_without_restore_or_writes(self):
        entry = self.legacy_model_usage_entry()
        self.repository.save_model_usage(self.name, self.workspace, entry)
        self.repository.prune_workspace(self.name)
        before = dict(self.storage.objects)
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        with mock.patch.object(self.repository, "restore", side_effect=AssertionError("列表不能下载整包")):
            for _ in range(2):
                run = catalog.list_runs()[0]
                self.assertEqual(len(run["token_usage"]["by_model"]), 2)
                self.assertEqual(run["token_usage"]["total_tokens"], 40)
        self.assertEqual(self.storage.objects, before)
        self.assertFalse(self.workspace.exists())

    def test_unknown_historical_call_is_not_assigned_to_main_or_current_model(self):
        entry = self.legacy_model_usage_entry(unknown=True)
        entry["summary"]["audit_model"] = "different-current-model"
        self.repository.save_model_usage(self.name, self.workspace, entry)
        rows = self.repository.display_summary(self.name, entry)["token_usage"]["by_model"]
        self.assertEqual({row["model"] for row in rows}, {"unknown", "qwen3.8-max"})

    def test_usage_backfill_rejects_changed_source_and_changed_original_total(self):
        entry = self.legacy_model_usage_entry()
        entry["summary"]["token_usage"]["total_tokens"] += 1
        with self.assertRaises(ArtifactStorageError):
            self.repository.save_model_usage(self.name, self.workspace, entry)
        self.assertIsNone(self.storage.head(self.repository.model_usage_key(self.name, entry)))
        (self.workspace / "logs/run.log").write_text("改变原件", encoding="utf-8")
        with self.assertRaises(ArtifactStorageError):
            self.repository.save_model_usage(self.name, self.workspace, entry)

    def test_missing_or_invalid_sidecar_keeps_original_aggregate_available(self):
        entry = self.legacy_model_usage_entry()
        projected = self.repository.display_summary(self.name, entry)
        self.assertEqual(projected["model_usage_status"], "unrecorded")
        self.assertEqual(projected["token_usage"], entry["summary"]["token_usage"])
        self.repository.save_model_usage(self.name, self.workspace, entry)
        key = self.repository.model_usage_key(self.name, entry)
        document = json.loads(self.storage.objects[key])
        document["archive_sha256"] = "0" * 64
        self.storage.objects[key] = canonical(document)
        self.repository._model_usage_cache.clear()
        projected = self.repository.display_summary(self.name, entry)
        self.assertEqual(projected["model_usage_status"], "unavailable")
        self.assertEqual(projected["token_usage"], entry["summary"]["token_usage"])

    def test_new_archive_with_model_rows_needs_no_legacy_sidecar(self):
        entry = self.archive()
        with mock.patch.object(self.storage, "head", side_effect=AssertionError("新摘要不额外读OSS")):
            self.assertEqual(self.repository.display_summary(self.name, entry), entry["summary"])

    def test_completion_statistics_remain_identical_after_disk_history_is_removed(self):
        from audit_core.completion_estimates import CompletionEstimator
        now = datetime(2026, 11, 18, tzinfo=timezone.utc)
        jobs = [{'job_id':'queued', 'run_id':'queued-run', 'status':'downloaded'},
                {'job_id':'callback', 'run_id':'callback-run', 'status':'callback'}]
        expected = CompletionEstimator(self.root).document(jobs, now=now)
        self.assertEqual(expected['history_count'], 1)
        self.archive()
        self.repository.prune_workspace(self.name)
        self.repository._statistics_cache.clear()
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        self.assertEqual(catalog.completion_document(jobs, now=now), expected)

    def test_legacy_statistics_fallback_reads_archive_without_oss_writes(self):
        self.archive()
        for row in list(self.storage.list('statistics')):
            del self.storage.objects[row['key']]
        self.repository.prune_workspace(self.name)
        self.repository._statistics_cache.clear()
        before = set(self.storage.objects)
        value = self.repository.statistical_profile(self.name, self.repository.get(self.name))
        self.assertEqual(value['workspace_id'], self.name)
        self.assertEqual(before, set(self.storage.objects))

    def test_missing_source_location_index_blocks_source_pruning(self):
        self.archive()
        key = next(row["key"] for row in self.storage.list("inputs"))
        del self.storage.objects[key]
        with self.assertRaises((ArtifactStorageError, KeyError)):
            self.repository.prune_sources(self.sources)
        self.assertTrue(self.original.exists())

    def test_local_delete_also_removes_already_published_remote_index(self):
        self.archive()
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        with self.repository.request_scope():
            result = catalog.delete_run(self.name, input_root=self.project/"input")
        self.assertTrue(result["deleted"])
        self.assertNotIn(index_key(self.name), self.storage.objects)
        self.assertEqual(catalog.list_runs(), [])

    def test_remote_delete_retains_recoverable_objects_not_a_visible_record(self):
        value = self.archive()
        self.repository.prune_workspace(self.name)
        self.repository.prune_sources(self.sources)
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        with self.repository.request_scope():
            self.assertTrue(catalog.delete_run(self.name, input_root=self.project/"input")["deleted"])
        self.assertEqual(catalog.list_runs(), [])
        self.assertIn(value["bundle"]["key"], self.storage.objects)

    def test_idle_http_connection_does_not_hold_storage_lock(self):
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        server = WorkbenchHTTPServer(("127.0.0.1", 0), handler_type(Handler, self.repository), catalog=catalog)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
        try:
            connection.connect()
            with self.repository.gate:
                self.assertEqual(len(catalog.local_runs()), 1)
        finally:
            connection.close()
            server.shutdown()
            worker.join(timeout=5)
            server.server_close()

    def test_config_cannot_enable_other_machine_or_project(self):
        path = self.project/"config/artifact-oss.json"
        path.parent.mkdir()
        path.write_text("{}", encoding="utf-8")
        with self.assertRaises(ArtifactStorageError):
            configuration(self.project)

    def test_oss_only_http_history_snapshot_html_technical_and_original(self):
        self.archive()
        self.repository.prune_workspace(self.name)
        self.repository.prune_sources(self.sources)
        catalog = catalog_type(WorkbenchCatalog, self.repository)(self.root)
        server = WorkbenchHTTPServer(("127.0.0.1", 0), handler_type(Handler, self.repository), catalog=catalog)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(server.server_close)
        self.addCleanup(lambda: worker.join(timeout=5))
        self.addCleanup(server.shutdown)
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        base = "http://127.0.0.1:"+str(server.server_address[1])
        name = quote(self.name, safe="")
        def get(route):
            with opener.open(base+route, timeout=10) as response:
                return response.read()
        listing = json.loads(get("/api/runs"))
        self.assertEqual(listing["count"], 1)
        self.assertEqual(listing["runs"][0]["artifact_storage"], "oss")
        self.assertEqual(json.loads(get('/api/completion-estimates'))['history_count'], 1)
        snapshot = json.loads(get("/api/runs/"+name+"/snapshot"))
        self.assertEqual(snapshot["run"]["workspace_id"], self.name)
        self.assertIn(b"<!doctype html", get("/?run="+name).lower())
        self.assertEqual(get("/api/runs/"+name+"/materials/preview?file_id=f001&download=1"), self.material)
        self.assertFalse(self.workspace.exists())
        self.assertFalse(self.original.exists())
        server.shutdown()
        worker.join(timeout=5)
        server.server_close()
        self.assertFalse(list(Path(self.temporary.name).glob("offline-artifact-read-*")))
