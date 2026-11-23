"""Orphaned runs must stop showing as in-progress in the workbench."""
from pathlib import Path
from tempfile import TemporaryDirectory
import json
import os
import time
import unittest

from audit_core.run_liveness import (
    INTERRUPTED_CODE,
    claim_run,
    process_alive,
    release_run,
    seal_orphaned_runs,
)
from audit_core.workbench_store import WorkbenchRunStore, reserve_workspace


def running_workspace(root: Path, run_id: str) -> Path:
    workspace = reserve_workspace(run_id, "codex", root)
    store = WorkbenchRunStore(
        workspace,
        run_id=run_id,
        producer_model="codex",
        root_html=Path("root.html"),
        workbench_url="http://127.0.0.1:8080/",
    )
    return store.workspace


class RunLivenessTests(unittest.TestCase):
    def test_stale_running_workspace_without_claim_is_sealed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = running_workspace(root, "20261111-orphan")
            sealed = seal_orphaned_runs(root, now=time.time() + 3600)
            self.assertEqual([item["run_id"] for item in sealed], ["20261111-orphan"])
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "failed")
            self.assertEqual(manifest["failure"]["code"], INTERRUPTED_CODE)
            self.assertIn("核销进程已不在", manifest["failure"]["message"])

    def test_fresh_running_workspace_is_left_alone(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = running_workspace(root, "20261111-live")
            self.assertEqual(seal_orphaned_runs(root), [])
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["status"], "running")

    def test_claimed_live_process_is_not_sealed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = running_workspace(root, "20261111-claimed")
            claim_run(workspace)
            self.assertTrue(process_alive(os.getpid()))
            self.assertEqual(seal_orphaned_runs(root, now=time.time() + 3600), [])
            release_run(workspace)

    def test_claim_of_dead_process_is_sealed(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = running_workspace(root, "20261111-dead")
            (workspace / "run.pid").write_text(
                json.dumps({"pid": 2 ** 22 + 12345}), encoding="utf-8"
            )
            self.assertFalse(process_alive(2 ** 22 + 12345))
            sealed = seal_orphaned_runs(root, now=time.time() + 3600)
            self.assertEqual([item["run_id"] for item in sealed], ["20261111-dead"])
            self.assertFalse((workspace / "run.pid").exists())

    def test_completed_workspace_is_never_touched(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = running_workspace(root, "20261111-done")
            store = WorkbenchRunStore.fail_interrupted(
                workspace, run_id="20261111-done", reason="用户要求停止"
            )
            self.assertEqual(store.manifest["status"], "failed")
            before = (workspace / "manifest.json").read_text(encoding="utf-8")
            self.assertEqual(seal_orphaned_runs(root, now=time.time() + 3600), [])
            self.assertEqual(
                (workspace / "manifest.json").read_text(encoding="utf-8"), before
            )
