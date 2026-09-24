from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from audit_core import run_temporary as runtime


class RunTemporaryTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        self.scope = self.root / "project" / "worktrees"

    def create(self, kind="render"):
        value = runtime.ManagedTemporaryDirectory(kind, scope=self.scope, temp_root=self.root)
        (Path(value.name) / "image.jpg").write_bytes(b"isolated test image")
        return value

    def abandon(self, value):
        # 单元夹具模拟 OS 已释放租约；下面另有真实强杀子进程验证。
        value._lease.close()
        value._closed = True

    def sweep(self):
        return runtime.cleanup_abandoned_temporaries(scope=self.scope, temp_root=self.root)

    def test_success_and_exception_remove_data_and_registration(self):
        for kind in ("render", "product-images"):
            value = self.create(kind)
            with self.assertRaisesRegex(RuntimeError, "fixture failure"):
                with value:
                    raise RuntimeError("fixture failure")
            self.assertFalse(value.container.exists())
            value.cleanup()  # 幂等，不删除其他目录。

    def test_active_lease_protects_even_very_old_directory(self):
        value = self.create()
        self.addCleanup(value.cleanup)
        os.utime(value.container, (1, 1))
        with patch.object(runtime, "_process_uses") as check:
            self.assertEqual(self.sweep(), {"removed": 0, "busy": 1, "deferred": 0})
            check.assert_not_called()
        self.assertTrue((Path(value.name) / "image.jpg").is_file())

    def test_pid_reuse_does_not_control_deletion(self):
        value = self.create()
        self.abandon(value)
        # 夹具 PID 恰为仍运行的本进程：真实判断使用占用锁，不使用 PID/年龄。
        self.assertEqual(json.loads((value.container / runtime._OWNER).read_text())["pid"], os.getpid())
        with patch.object(runtime, "_process_uses", return_value=False):
            self.assertEqual(self.sweep()["removed"], 1)
            self.assertEqual(self.sweep()["removed"], 0)
        self.assertFalse(value.container.exists())

    def test_surviving_model_child_and_failed_process_check_defer(self):
        value = self.create()
        self.abandon(value)
        with patch.object(runtime, "_process_uses", return_value=True):
            self.assertEqual(self.sweep()["busy"], 1)
        with patch.object(runtime, "_process_uses", side_effect=subprocess.TimeoutExpired("fixture", 1)):
            self.assertEqual(self.sweep()["deferred"], 1)
        self.assertTrue((Path(value.name) / "image.jpg").is_file())
        with patch.object(runtime, "_process_uses", return_value=False):
            self.assertEqual(self.sweep()["removed"], 1)

    def test_foreign_legacy_and_business_directories_untouched(self):
        names = ["input", "input-oss", "worktrees", "offline-product-images-legacy",
                 "offline-audit-render-legacy", "oa-" + runtime._scope_hash(self.scope) + "-abcdefgh"]
        for name in names:
            directory = self.root / name
            directory.mkdir()
            (directory / "preserve.zip").write_bytes(b"original")
        other = runtime.ManagedTemporaryDirectory("render", scope=self.root / "other", temp_root=self.root)
        self.abandon(other)
        self.sweep()
        self.assertTrue(other.container.exists())
        for name in names:
            self.assertEqual((self.root / name / "preserve.zip").read_bytes(), b"original")

    def test_wrong_owner_and_unregistered_top_level_preserved(self):
        for alteration in ("scope", "file"):
            value = self.create()
            self.abandon(value)
            if alteration == "scope":
                marker = value.container / runtime._OWNER
                owner = json.loads(marker.read_text())
                owner["scope"] = "foreign"
                marker.write_text(json.dumps(owner))
            else:
                (value.container / "keep.txt").write_text("foreign")
            self.assertGreaterEqual(self.sweep()["deferred"], 1)
            self.assertTrue((Path(value.name) / "image.jpg").is_file())

    def test_hardlink_rejected_before_any_material_is_removed(self):
        value = self.create()
        self.abandon(value)
        source = self.root / "original.jpg"
        source.write_bytes(b"never alter")
        os.link(source, Path(value.name) / "linked.jpg")
        with patch.object(runtime, "_process_uses", return_value=False):
            self.assertEqual(self.sweep()["deferred"], 1)
        self.assertEqual(source.read_bytes(), b"never alter")
        self.assertTrue((Path(value.name) / "image.jpg").is_file())

    def test_locked_file_failure_preserves_registration_and_retries(self):
        value = self.create()
        image = Path(value.name) / "image.jpg"
        original = Path.unlink

        def locked(path, *args, **kwargs):
            if path == image:
                raise PermissionError("fixture file is locked")
            return original(path, *args, **kwargs)

        message = io.StringIO()
        with patch.object(Path, "unlink", locked), redirect_stderr(message):
            value.cleanup()
        self.assertIn("下次正式运行", message.getvalue())
        self.assertTrue((value.container / runtime._OWNER).is_file())
        self.assertTrue(image.is_file())
        with patch.object(runtime, "_process_uses", return_value=False):
            self.assertEqual(self.sweep()["removed"], 1)

    def test_creation_in_worker_thread_cleanup_in_main_thread(self):
        with ThreadPoolExecutor(max_workers=1) as pool:
            value = pool.submit(self.create, "product-images").result()
        with patch.object(runtime, "_process_uses", return_value=False):
            self.assertEqual(self.sweep()["busy"], 1)
        value.cleanup()
        self.assertFalse(value.container.exists())

    def test_concurrent_startups_are_safe_and_idempotent(self):
        value = self.create()
        self.abandon(value)
        with patch.object(runtime, "_process_uses", return_value=False), ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.sweep(), range(4)))
        self.assertFalse(value.container.exists())
        self.assertGreaterEqual(sum(item["removed"] for item in results), 1)

    def test_next_scope_start_reaps_previous_but_preserves_current(self):
        previous = self.create("product-images")
        self.abandon(previous)
        with patch.object(runtime.tempfile, "gettempdir", return_value=str(self.root)), \
                patch.object(runtime, "_process_uses", return_value=False), redirect_stderr(io.StringIO()):
            with runtime.temporary_scope(self.scope):
                self.assertFalse(previous.container.exists())
                with runtime.ManagedTemporaryDirectory("render") as current:
                    self.assertTrue(Path(current).is_dir())
                    self.assertEqual(self.sweep()["busy"], 1)
        self.assertFalse(Path(current).parent.exists())

    def test_explicit_legacy_registration_moves_without_deleting_then_reaps(self):
        legacy = self.root / "offline-audit-render-abcdefgh"
        legacy.mkdir()
        (legacy / "old-image.jpg").write_bytes(b"old temporary only")
        with patch.object(runtime, "_process_uses", return_value=False):
            moved = runtime.register_abandoned_legacy_temporary(legacy, scope=self.scope, temp_root=self.root)
            self.assertFalse(legacy.exists())
            self.assertEqual((moved / "old-image.jpg").read_bytes(), b"old temporary only")
            self.assertEqual(self.sweep()["removed"], 1)
            self.assertFalse(moved.parent.exists())

    def test_legacy_registration_rejects_active_and_business_directories(self):
        for name in ("input", "worktrees", "offline-audit-render-abcdefgh"):
            source = self.root / name
            source.mkdir()
            (source / "preserve.zip").write_bytes(b"original")
            with patch.object(runtime, "_process_uses", return_value=True), self.assertRaises(ValueError):
                runtime.register_abandoned_legacy_temporary(source, scope=self.scope, temp_root=self.root)
            self.assertEqual((source / "preserve.zip").read_bytes(), b"original")

    def test_real_process_referencing_abandoned_material_defers_cleanup(self):
        restricted = runtime._process_uses(self.root / "unowned-probe")
        value = self.create()
        self.abandon(value)
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", "import time; print('ready',flush=True); time.sleep(120)", value.name],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            self.assertEqual(child.stdout.readline().strip(), "ready")
            self.assertEqual(self.sweep()["busy"], 1)
            self.assertTrue((Path(value.name) / "image.jpg").is_file())
        finally:
            child.kill()
            child.communicate(timeout=10)
        result = self.sweep()
        self.assertEqual(result, {"removed": 0, "busy": 1, "deferred": 0} if restricted
                         else {"removed": 1, "busy": 0, "deferred": 0})
        self.assertEqual(value.container.exists(), restricted)

    def test_bundled_cli_reaps_old_temp_and_publishes_only_its_own_archive(self):
        # 真实 bundled CLI 与隔离模型夹具；不连接模型、数据库或OSS。
        from tests.pdf_test_support import fixture_cli_command
        restricted = runtime._process_uses(self.root / "unowned-probe")
        project = Path(__file__).resolve().parents[1]
        inputs = self.root / "input"
        inputs.mkdir()
        source = inputs / "未标明类型.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("说明.txt", "isolated fixture")
        before_zip = source.read_bytes()
        before_html = (project / 'skills/orchestrate-offline-audit/assets/offline-activity-audit.html').read_bytes()
        previous = self.create()
        self.abandon(previous)
        environment = os.environ.copy()
        environment.update({"TEMP": str(self.root), "TMP": str(self.root), "TMPDIR": str(self.root),
                            "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
        completed = subprocess.run(
            fixture_cli_command(
                ["--run-id", "20260914-temp-cleanup-test", "--producer-model", "codex",
                 "--biz-type", "KT板等物料制作",
                 "--input-dir", str(inputs), "--worktrees", str(self.scope)], candidates={0: []}),
            env=environment, cwd=project, capture_output=True, text=True, encoding="utf-8",
            timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        if not restricted:
            self.assertIn("已清理 1 个目录", completed.stderr)
        self.assertEqual(previous.container.exists(), restricted)
        self.assertEqual(list(self.root.glob("oa-*")), [previous.container] if restricted else [])
        self.assertEqual(source.read_bytes(), before_zip)
        self.assertEqual((project / 'skills/orchestrate-offline-audit/assets/offline-activity-audit.html').read_bytes(), before_html)
        outputs = list(self.scope.glob("*/offline-activity-audit.html"))
        self.assertEqual(len(outputs), 1)
        manifest = json.loads((outputs[0].parent / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["status"], "completed")
        self.assertIsNone(manifest["failure"])
        self.assertEqual(list(self.scope.rglob("*.xlsx")), [])

    def test_real_forced_exit_then_next_process_cleans_both_kinds(self):
        restricted = runtime._process_uses(self.root / "unowned-probe")
        script = (
            "import json,sys,time; from pathlib import Path; "
            "from audit_core.run_temporary import ManagedTemporaryDirectory; "
            "items=[ManagedTemporaryDirectory(k,scope=Path(sys.argv[1]),temp_root=Path(sys.argv[2])) "
            "for k in ('render','product-images')]; "
            "[(Path(i.name)/'image.jpg').write_bytes(b'test') for i in items]; "
            "print(json.dumps([str(i.container) for i in items]),flush=True); time.sleep(120)"
        )
        child = subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(self.scope), str(self.root)],
            cwd=Path(__file__).resolve().parents[1], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        try:
            containers = [Path(name) for name in json.loads(child.stdout.readline())]
            self.assertEqual(self.sweep()["busy"], 2)
            child.kill()
            child.wait(timeout=10)
            command = (
                "import json,sys; from pathlib import Path; "
                "from audit_core.run_temporary import cleanup_abandoned_temporaries; "
                "print(json.dumps(cleanup_abandoned_temporaries(scope=Path(sys.argv[1]),temp_root=Path(sys.argv[2]))))"
            )
            completed = subprocess.run(
                [sys.executable, "-B", "-c", command, str(self.scope), str(self.root)],
                cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True,
                check=True, timeout=45, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(json.loads(completed.stdout), {"removed": 0, "busy": 2, "deferred": 0} if restricted
                             else {"removed": 2, "busy": 0, "deferred": 0})
            self.assertTrue(all(container.exists() == restricted for container in containers))
        finally:
            if child.poll() is None:
                child.kill()
            child.communicate(timeout=10)

    @unittest.skipUnless(os.name == "nt", "验证 Windows junction 不被递归清理")
    def test_windows_junction_preserves_external_target_and_all_materials(self):
        value = self.create()
        self.abandon(value)
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "original.zip").write_bytes(b"original")
        junction = Path(value.name) / "link"
        created = subprocess.run(
            ["cmd.exe", "/d", "/c", "mklink", "/J", str(junction), str(outside)],
            capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.assertEqual(created.returncode, 0)
        try:
            with patch.object(runtime, "_process_uses", return_value=False):
                self.assertEqual(self.sweep()["deferred"], 1)
            self.assertTrue((Path(value.name) / "image.jpg").is_file())
            self.assertEqual((outside / "original.zip").read_bytes(), b"original")
        finally:
            junction.rmdir()  # 只移除夹具链接本身，不递归遍历外部目标。


if __name__ == "__main__":
    unittest.main()
