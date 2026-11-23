"""跨核销参考图缓存：合成图片及内存 OSS，不读取真实业务或调用网络。"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core import product_image_cache as cache
from audit_core import product_image_runtime as runtime
from audit_core.product_database import product_catalog_scope
from audit_core.product_oss import json_bytes, manifest_key
from tests.product_test_support import isolate_product_image_cache, png_bytes
from tests.test_product_oss import FakeOSS, ROW, fixture


class ProductImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.scope = isolate_product_image_cache(self)
        self.picture, self.manifest, self.catalog = fixture()
        self.key = self.manifest["views"][0]["object_key"]
        self.manifest_key = manifest_key(ROW["product_code"])
        self.remote = FakeOSS({self.manifest_key: json_bytes(self.manifest), self.key: self.picture})
        self.enterContext(patch.object(runtime, "ProductOSS", return_value=self.remote))
        self.enterContext(patch("audit_core.qwen_model.complete_json", side_effect=AssertionError("缓存测试禁止调用模型")))

    def run_once(self, name):
        target = self.scope.parent / (name + ".png")
        with product_catalog_scope():
            product = runtime.attach_oss_images(self.catalog)["products"][0]
            runtime.copy_oss_image(product, product["views"][0], target)
            temporary = Path(runtime._STATE.get().temporary.name)
        self.assertFalse(temporary.exists())
        return target

    def cached_path(self, view=None):
        view = view or self.manifest["views"][0]
        return cache.cache_directory(self.scope) / (view["sha256"] + ".png")

    def test_two_runs_read_fresh_manifests_but_download_image_only_once(self):
        first = self.run_once("first")
        cached = self.cached_path()
        self.assertTrue(cached.is_file())
        # 后一单不共享前一单工作区，缓存跨任务存活且不依赖现场/历史附件。
        first.unlink()
        second = self.run_once("second")
        self.assertEqual(second.read_bytes(), self.picture)
        self.assertEqual(self.remote.calls, [self.manifest_key, self.key, self.manifest_key])
        self.assertEqual(cached.read_bytes(), self.picture)

    def test_model_copy_cannot_pollute_persistent_bytes(self):
        self.run_once("first").write_bytes(b"changed by isolated consumer")
        self.assertEqual(self.run_once("second").read_bytes(), self.picture)
        self.assertEqual(self.remote.calls.count(self.key), 1)

    def test_changed_hash_downloads_new_bytes_even_for_same_object_key(self):
        self.run_once("first")
        new_bytes = png_bytes("red")
        changed = deepcopy(self.manifest)
        changed["views"][0].update(sha256=hashlib.sha256(new_bytes).hexdigest(), size_bytes=len(new_bytes))
        self.remote.objects.update({self.manifest_key: json_bytes(changed), self.key: new_bytes})
        self.assertEqual(self.run_once("second").read_bytes(), new_bytes)
        self.assertEqual(self.remote.calls.count(self.key), 2)
        self.assertTrue(self.cached_path(changed["views"][0]).exists())

    def test_corrupted_same_size_cache_is_redownloaded_and_repaired(self):
        self.run_once("first")
        self.cached_path().write_bytes(b"x" * len(self.picture))
        self.assertEqual(self.run_once("second").read_bytes(), self.picture)
        self.assertEqual(self.cached_path().read_bytes(), self.picture)
        self.assertEqual(self.remote.calls.count(self.key), 2)

    def test_latest_manifest_unavailable_never_uses_existing_cache(self):
        self.run_once("first")
        with patch.object(runtime, "ProductOSS", side_effect=AuditError("当前组织 OSS 无权读取")):
            with self.assertRaisesRegex(AuditError, "无权读取"):
                self.run_once("second")
        self.assertFalse((self.scope.parent / "second.png").exists())

    def test_changed_manifest_identity_or_dimensions_cannot_use_old_bytes(self):
        self.run_once("first")
        changed = deepcopy(self.manifest)
        changed["barcode_69"] = "6970356161042"
        self.remote.objects[self.manifest_key] = json_bytes(changed)
        with self.assertRaisesRegex(AuditError, "身份校验失败"):
            self.run_once("wrong-identity")
        changed = deepcopy(self.manifest)
        changed["views"][0]["width"] = 9
        self.remote.objects[self.manifest_key] = json_bytes(changed)
        with self.assertRaisesRegex(AuditError, "尺寸"):
            self.run_once("wrong-dimensions")

    def test_interrupted_download_is_not_persisted(self):
        def interrupted(view, target):
            raise AuditError("模拟下载中断")
        with patch.object(runtime, "_download", side_effect=interrupted):
            with self.assertRaisesRegex(AuditError, "中断"):
                self.run_once("failed")
        self.assertFalse(self.cached_path().exists())
        self.assertEqual(self.run_once("retried").read_bytes(), self.picture)

    def test_manifest_changed_during_download_stops_without_mixing_snapshots(self):
        def interrupted(view, target):
            changed = deepcopy(self.manifest)
            changed["views"][0]["sha256"] = "a" * 64
            self.remote.objects[self.manifest_key] = json_bytes(changed)
            raise AuditError("模拟取图期间清单变更")
        with patch.object(runtime, "_download", side_effect=interrupted):
            with self.assertRaisesRegex(AuditError, "清单快照已变化"):
                self.run_once("failed")
        self.assertFalse(self.cached_path().exists())
        self.assertFalse((self.scope.parent / "failed.png").exists())

    def test_corrupt_cache_and_failed_oss_download_do_not_reuse_bad_bytes(self):
        self.run_once("first")
        self.cached_path().write_bytes(b"corrupt")
        self.remote.objects[self.key] = b"also corrupt"
        with self.assertRaises(AuditError):
            self.run_once("failed")
        self.assertFalse(self.cached_path().exists())
        self.assertFalse((self.scope.parent / "failed.png").exists())

    def test_lru_eviction_only_touches_owned_cache_images(self):
        self.run_once("first")
        first_cached = self.cached_path()
        unrelated = self.scope / "business-evidence.png"
        unrelated.write_bytes(b"business must survive")
        marker = first_cached.parent / "unrelated.txt"
        marker.write_text("leave alone", encoding="utf-8")
        partial = first_cached.parent / "download.part"
        partial.write_bytes(b"interrupted")
        other = png_bytes("red")
        changed = deepcopy(self.manifest)
        changed["views"][0].update(sha256=hashlib.sha256(other).hexdigest(), size_bytes=len(other))
        self.remote.objects.update({self.manifest_key: json_bytes(changed), self.key: other})
        with patch.object(cache, "MAX_CACHE_BYTES", max(len(other), len(self.picture))):
            self.assertEqual(self.run_once("second").read_bytes(), other)
        self.assertFalse(first_cached.exists())
        self.assertEqual(unrelated.read_bytes(), b"business must survive")
        self.assertTrue(marker.exists())
        self.assertFalse(partial.exists())

    def test_image_over_cache_budget_still_works_without_persistence(self):
        with patch.object(cache, "MAX_CACHE_BYTES", len(self.picture) - 1):
            self.assertEqual(self.run_once("first").read_bytes(), self.picture)
        self.assertFalse(self.cached_path().exists())

    def test_cache_permission_failure_falls_back_to_verified_oss_download(self):
        with patch.object(cache, "_prepare", side_effect=PermissionError("synthetic")):
            self.assertEqual(self.run_once("first").read_bytes(), self.picture)
        self.assertEqual(self.remote.calls.count(self.key), 1)
        self.assertFalse(self.cached_path().exists())

    def test_cache_publish_disk_failure_does_not_repeat_completed_download(self):
        copy = cache._copy
        def fail_pending(source, target):
            if target.suffix == ".part":
                raise OSError("synthetic full disk")
            return copy(source, target)
        with patch.object(cache, "_copy", side_effect=fail_pending):
            self.assertEqual(self.run_once("first").read_bytes(), self.picture)
        self.assertEqual(self.remote.calls.count(self.key), 1)
        self.assertFalse(self.cached_path().exists())

    def test_concurrent_tasks_share_one_download(self):
        original = runtime._download
        def slow_download(view, target):
            time.sleep(0.1)
            return original(view, target)
        with patch.object(runtime, "_download", side_effect=slow_download):
            # ContextVar 不自动进入新线程，显式传相同部署的缓存 scope。
            def one(index):
                view = self.manifest["views"][0]
                target = self.scope.parent / f"thread-{index}.png"
                return cache.copy_or_download(view, target, scope=self.scope, download=slow_download)
            with ThreadPoolExecutor(max_workers=4) as pool:
                statuses = list(pool.map(one, range(4)))
        self.assertEqual(statuses.count("downloaded"), 1)
        self.assertEqual(statuses.count("persistent_hit"), 3)
        self.assertEqual(self.remote.calls.count(self.key), 1)

    def test_other_deployment_has_separate_cache(self):
        self.run_once("first")
        view = self.manifest["views"][0]
        other_scope = self.scope.parent / "other-deployment" / "worktrees"
        target = self.scope.parent / "other.png"
        status = cache.copy_or_download(view, target, scope=other_scope, download=runtime._download)
        self.assertEqual(status, "downloaded")
        self.assertEqual(self.remote.calls.count(self.key), 2)

    def test_cache_rejects_hardlinks_without_deleting_external_file(self):
        self.run_once("first")
        external = self.scope.parent / "external.png"
        os.link(self.cached_path(), external)
        with self.assertRaisesRegex(AuditError, "链接"):
            self.run_once("second")
        self.assertEqual(external.read_bytes(), self.picture)

    def test_separate_processes_share_one_download_and_survive_restart(self):
        script = '''
import json, sys, time
from pathlib import Path
from audit_core.product_image_cache import copy_or_download
scope, target, view = Path(sys.argv[1]), Path(sys.argv[2]), json.loads(sys.argv[3])
def download(view, target):
    with (scope.parent / "downloads.log").open("a") as output:
        output.write("download\\n")
    time.sleep(0.15)
    target.write_bytes((scope.parent / "synthetic.png").read_bytes())
print(copy_or_download(view, target, scope=scope, download=download))
'''
        (self.scope.parent / "synthetic.png").write_bytes(self.picture)
        environment = {**os.environ, "PYTHONPATH": str(Path(runtime.__file__).parents[1]),
                       "PYTHONDONTWRITEBYTECODE": "1"}
        processes = []
        try:
            for i in range(3):
                processes.append(subprocess.Popen(
                    [sys.executable, "-B", "-c", script, str(self.scope),
                     str(self.scope.parent / f"process-{i}.png"), json.dumps(self.manifest["views"][0])],
                    env=environment, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)))
            statuses = []
            for process in processes:
                output, error = process.communicate(timeout=25)
                self.assertEqual(process.returncode, 0, error)
                statuses.append(output.strip())
            self.assertEqual(statuses.count("downloaded"), 1)
            self.assertEqual(statuses.count("persistent_hit"), 2)
            self.assertEqual((self.scope.parent / "downloads.log").read_text().splitlines(), ["download"])
            self.assertEqual(self.run_once("after-process-exit").read_bytes(), self.picture)
            self.assertEqual(self.remote.calls, [self.manifest_key])
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                    process.communicate()


if __name__ == "__main__":
    unittest.main()
