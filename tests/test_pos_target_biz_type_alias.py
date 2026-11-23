"""Approved POS alias follows the real runner; all external I/O is forbidden."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from audit_core.common import BIZ_TYPE_NOT_REQUIRED_MESSAGE
from audit_core.oss_intake import OSSIntakeService
from audit_core.scenario_registry import BIZ_TYPE_ALIASES, BIZ_TYPE_SCENARIOS, scenario_for_biz_type
from audit_core.workbench_runtime import run_persistent_audit
from tests.pdf_test_support import PolicyProvider, bundle
from tests.test_oss_intake import (
    _config, _fake_callback_sender, _no_network_url_validator, _payload, _wait_for_job,
)


class PosTargetBizTypeAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="audit-pos-target-alias-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("httpx.AsyncClient", "urllib.request.OpenerDirector.open", "socket.create_connection"):
            patcher = mock.patch(target, side_effect=AssertionError("External I/O forbidden in POS alias fixture"))
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
            provider = PolicyProvider(scenarios=("entry_fee",), missing={0: missing})
            providers.append(provider)
            return run_persistent_audit(**kwargs, evidence_provider=provider, input_source="oss")

        downloader = mock.Mock(side_effect=download)
        runner = mock.Mock(side_effect=run)
        callback = mock.Mock(side_effect=_fake_callback_sender)
        service = OSSIntakeService(worktrees_root=self.root / "worktrees", input_root=self.root / "input",
            config=_config(), downloader=downloader, runner=runner,
            url_validator=_no_network_url_validator, callback_sender=callback)

        def close():
            service.close()
            service._download_worker.join(timeout=5)
            for worker in service._audit_workers:worker.join(timeout=5)

        self.addCleanup(close)
        return service, downloader, runner, callback, providers

    def _payload(self):
        return {**_payload(verify_code="HX-FIXTURE-POS-TARGET-ALIAS", analyze_id=704),
                "bizType": "pos达标激励"}

    def _assert_result(self, result, original_type):
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["scenarios"], ["pos_target_incentive"])
        manifest = json.loads((Path(result["worktree"]) / "manifest.json").read_text(encoding="utf-8"))
        snapshot = json.loads(Path(result["snapshot"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["bizType"], original_type)
        self.assertEqual(snapshot["run"]["bizType"], original_type)
        self.assertEqual(snapshot["view"]["sheets"][0]["audit_type_label"], "POS达标激励")

    def test_only_the_approved_lowercase_pos_name_is_added(self):
        self.assertEqual(len(BIZ_TYPE_SCENARIOS), 8)
        for value in ("pos达标激励", "POS达标激励"):
            self.assertEqual(scenario_for_biz_type(value), "pos_target_incentive")
        for value in ("Pos达标激励", "pOs达标激励", "pos达标激励 ", " pos达标激励", "pos达标", "pos_target_incentive"):
            with self.subTest(biz_type=value):
                self.assertIsNone(scenario_for_biz_type(value))

    def test_alias_and_canonical_type_use_the_same_skill_and_preserve_original_input(self):
        for index, biz_type in enumerate(("pos达标激励", "POS达标激励")):
            with self.subTest(biz_type=biz_type):
                inputs = self.root / str(index) / "input"
                bundle(inputs, "其他类型-隔离资料.zip")
                provider = PolicyProvider(scenarios=("entry_fee",))
                result = run_persistent_audit("20261103-pos-target-alias", producer_model="codex",
                    input_dir=inputs, worktrees_root=self.root / str(index) / "worktrees",
                    evidence_provider=provider, biz_type=biz_type, input_source="oss")
                self._assert_result(result, biz_type)
                self.assertEqual([call["kind"] for call in provider.calls],
                                 ["pdf_material_classification", "pdf_policy_audit"])
                self.assertEqual(provider.calls[0]["selected_scenario"], "pos_target_incentive")
                self.assertEqual(provider.calls[1]["scenario"], "pos_target_incentive")

    def test_alias_downloads_and_reports_missing_material_instead_of_skipping_ai(self):
        service, downloader, runner, callback, providers = self._service(missing=("settlement",))
        payload = self._payload()
        accepted, created = service.submit(payload)
        self.assertTrue(created)
        complete = _wait_for_job(service, accepted["job_id"], "completed", timeout=20)
        self._assert_result(complete["result"], payload["bizType"])
        downloader.assert_called_once()
        runner.assert_called_once()
        callback.assert_called_once()
        self.assertEqual(runner.call_args.kwargs["biz_type"], "pos达标激励")
        self.assertIsNotNone(complete["delivery"])
        self.assertIsNotNone(complete["audit_started_at"])
        self.assertEqual([call["kind"] for call in providers[0].calls], ["pdf_material_classification"])
        summary = Path(complete["result"]["analysis_summary"]).read_text(encoding="utf-8").strip()
        self.assertIn("缺少结算单", summary)
        self.assertNotIn(BIZ_TYPE_NOT_REQUIRED_MESSAGE, summary)
        self.assertEqual(callback.call_args.args[0], {
            "verifyCode": payload["verifyCode"], "analyzeId": payload["analyzeId"], "result": summary,
        })

    def test_existing_skipped_history_stays_frozen_and_resubmission_gets_a_new_attempt(self):
        service, downloader, runner, callback, providers = self._service(missing=("settlement",))
        payload = self._payload()
        previous = {key: value for key, value in BIZ_TYPE_ALIASES.items() if key != "pos达标激励"}
        with mock.patch.dict(BIZ_TYPE_ALIASES, previous, clear=True):
            first, _ = service.submit(payload)
            skipped = _wait_for_job(service, first["job_id"], "completed", timeout=20)
        receipt = service.store._path(first["job_id"])
        frozen = receipt.read_bytes()
        self.assertFalse(skipped["result"]["audit_required"])
        self.assertIsNone(skipped["delivery"])
        downloader.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(callback.call_args.args[0]["result"], BIZ_TYPE_NOT_REQUIRED_MESSAGE)
        self.assertEqual(service.get(first["job_id"]), skipped)
        self.assertEqual(receipt.read_bytes(), frozen)
        replay, created = service.submit(payload)
        self.assertTrue(created)
        self.assertEqual(replay["attempt"], 2)
        self.assertNotEqual(replay["job_id"], first["job_id"])
        complete = _wait_for_job(service, replay["job_id"], "completed", timeout=20)
        self._assert_result(complete["result"], "pos达标激励")
        downloader.assert_called_once()
        runner.assert_called_once()
        self.assertEqual(len(providers), 1)
        self.assertEqual(callback.call_count, 2)
        self.assertEqual(receipt.read_bytes(), frozen)
        self.assertNotEqual(callback.call_args.args[0]["result"], BIZ_TYPE_NOT_REQUIRED_MESSAGE)


if __name__ == "__main__":
    unittest.main()
