"""Approved material alias exercises real routing/persistence without external I/O."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import quote

from audit_core.common import BIZ_TYPE_NOT_REQUIRED_MESSAGE
from audit_core.oss_intake import OSSIntakeService
from audit_core.scenario_registry import BIZ_TYPE_ALIASES, BIZ_TYPE_SCENARIOS, scenario_for_biz_type
from audit_core.workbench_runtime import run_persistent_audit
from tests.pdf_test_support import PolicyProvider, bundle
from tests.test_oss_intake import (
    OSS_HOST, _config, _fake_callback_sender, _no_network_url_validator,
    _payload, _wait_for_job,
)


class MaterialBizTypeAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="audit-material-alias-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        # A regression must fail instead of accidentally contacting a model/OSS/callback.
        for target in ("httpx.AsyncClient", "urllib.request.OpenerDirector.open", "socket.create_connection"):
            patcher = mock.patch(target, side_effect=AssertionError("External I/O forbidden in alias fixture"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def _service(self, *, missing=()):
        providers = []

        def download(_submission, destination, _settings):
            bundle(destination.parent, destination.name)
            content = destination.read_bytes()
            return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

        def run(**kwargs):
            kwargs.pop("cancel_event", None)
            provider = PolicyProvider(scenarios=("giveaway_promotion",), missing={0: missing})
            providers.append(provider)
            return run_persistent_audit(**kwargs, evidence_provider=provider, input_source="oss")

        downloader = mock.Mock(side_effect=download)
        runner = mock.Mock(side_effect=run)
        callback = mock.Mock(side_effect=_fake_callback_sender)
        service = OSSIntakeService(
            worktrees_root=self.root / "worktrees", input_root=self.root / "input",
            config=_config(), downloader=downloader, runner=runner,
            url_validator=_no_network_url_validator, callback_sender=callback,
        )

        def close():
            service.close()
            service._download_worker.join(timeout=5)
            for worker in service._audit_workers:worker.join(timeout=5)

        self.addCleanup(close)
        return service, downloader, runner, callback, providers

    def _payload(self, *, signature="fixture-only"):
        return {
            **_payload(verify_code="HX-FIXTURE-MATERIAL-ALIAS", analyze_id=703),
            "bizType": "物料制作",
            "downloadUrl": f"https://{OSS_HOST}/incoming/{quote('买赠搭赠-现场物料.zip')}?X-Oss-Signature={signature}",
        }

    def _assert_material_result(self, result, original_type):
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["scenarios"], ["poster_material"])
        manifest = json.loads((Path(result["worktree"]) / "manifest.json").read_text(encoding="utf-8"))
        snapshot = json.loads(Path(result["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["bizType"], original_type)
        self.assertEqual(snapshot["view"]["sheets"][0]["audit_type_label"], "KT板等物料制作")
        return snapshot

    def test_exact_material_alias_keeps_eight_canonical_types(self):
        self.assertEqual(BIZ_TYPE_ALIASES, {"堆头": "陈列堆头", "陈列": "陈列堆头", "物料制作": "KT板等物料制作", "pos达标激励": "POS达标激励"})
        self.assertEqual(len(BIZ_TYPE_SCENARIOS), 8)
        self.assertNotIn("物料制作", BIZ_TYPE_SCENARIOS)
        for value in ("物料制作", "KT板等物料制作"):
            self.assertEqual(scenario_for_biz_type(value), "poster_material")
        for value in (" 物料制作", "物料制作 ", "物料制作费", "KT版等物料制作", "买赠", "poster_material", "", None, 1):
            with self.subTest(biz_type=value):
                self.assertIsNone(scenario_for_biz_type(value))

    def test_alias_and_canonical_type_override_actual_buygift_and_giveaway_filenames(self):
        for index, (biz_type, filename) in enumerate((
            ("物料制作", "买赠促销-材料.zip"),
            ("KT板等物料制作", "搭赠活动-材料.zip"),
        )):
            with self.subTest(biz_type=biz_type, filename=filename):
                inputs = self.root / str(index) / "input"
                archive = bundle(inputs, filename)
                provider = PolicyProvider(scenarios=("giveaway_promotion",))
                result = run_persistent_audit(
                    "20261103-material-alias", producer_model="codex", input_dir=inputs,
                    worktrees_root=self.root / str(index) / "worktrees",
                    evidence_provider=provider, biz_type=biz_type, input_source="oss",
                )
                self.assertTrue(archive.is_file())
                self._assert_material_result(result, biz_type)
                self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification", "pdf_policy_audit"])
                self.assertEqual(provider.calls[0]["selected_scenario"], "poster_material")
                self.assertEqual(provider.calls[1]["scenario"], "poster_material")

    def test_fake_download_real_runner_reports_specific_missing_material_to_fake_callback(self):
        service, downloader, runner, callback, providers = self._service(missing=("settlement",))
        payload = self._payload()
        accepted, created = service.submit(payload)
        self.assertTrue(created)
        complete = _wait_for_job(service, accepted["job_id"], "completed", timeout=20)
        self._assert_material_result(complete["result"], "物料制作")
        downloader.assert_called_once()
        runner.assert_called_once()
        callback.assert_called_once()
        self.assertEqual(downloader.call_args.args[1].name, "买赠搭赠-现场物料.zip")
        self.assertIsNotNone(complete["delivery"])
        self.assertIsNotNone(complete["audit_started_at"])
        self.assertEqual(complete["callback"]["status"], "delivered")
        self.assertEqual([c["kind"] for c in providers[0].calls], ["pdf_material_classification"])
        summary = Path(complete["result"]["analysis_summary"]).read_text(encoding="utf-8").strip()
        self.assertIn("缺少结算单", summary)
        self.assertNotIn(BIZ_TYPE_NOT_REQUIRED_MESSAGE, summary)
        self.assertEqual(callback.call_args.args[0], {
            "verifyCode": payload["verifyCode"], "analyzeId": payload["analyzeId"], "result": summary,
        })
        receipt = service.store._path(accepted["job_id"]).read_text(encoding="utf-8")
        self.assertNotIn("X-Oss-Signature", receipt)

    def test_pre_alias_skipped_receipt_is_unchanged_and_explicit_resubmission_creates_new_attempt(self):
        service, downloader, runner, callback, providers = self._service()
        payload = self._payload(signature="old-fixture-only")
        # Reproduce the former exact registry in memory, never rewrite a real receipt.
        old_aliases = {key: value for key, value in BIZ_TYPE_ALIASES.items() if key != "物料制作"}
        with mock.patch.dict(BIZ_TYPE_ALIASES, old_aliases, clear=True):
            first, created = service.submit(payload)
            self.assertTrue(created)
            skipped = _wait_for_job(service, first["job_id"], "completed", timeout=20)
        original_path = service.store._path(first["job_id"])
        original_bytes = original_path.read_bytes()
        self.assertFalse(skipped["result"]["audit_required"])
        self.assertIsNone(skipped["delivery"])
        self.assertIsNone(skipped["audit_started_at"])
        self.assertEqual(list((self.root / "input").iterdir()), [])
        downloader.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(callback.call_args.args[0]["result"], BIZ_TYPE_NOT_REQUIRED_MESSAGE)

        # Reading history after the mapping fix does not rerun or rewrite it.
        self.assertEqual(service.get(first["job_id"]), skipped)
        self.assertEqual(len(service.list()), 1)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertEqual(callback.call_count, 1)
        replay, created = service.submit(self._payload(signature="fresh-fixture-only"))
        self.assertTrue(created)
        self.assertEqual(replay["attempt"], 2)
        self.assertNotEqual(replay["job_id"], first["job_id"])
        complete = _wait_for_job(service, replay["job_id"], "completed", timeout=20)
        self._assert_material_result(complete["result"], "物料制作")
        downloader.assert_called_once()
        runner.assert_called_once()
        self.assertEqual(len(providers), 1)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(complete["callback"]["status"], "delivered")
        summary = Path(complete["result"]["analysis_summary"]).read_text(encoding="utf-8").strip()
        self.assertEqual(callback.call_args.args[0]["result"], summary)
        self.assertNotEqual(summary, BIZ_TYPE_NOT_REQUIRED_MESSAGE)
        self.assertEqual(original_path.read_bytes(), original_bytes)
        self.assertIsNone(service.get(first["job_id"])["delivery"])


if __name__ == "__main__":
    unittest.main()
