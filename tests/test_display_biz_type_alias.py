"""Approved display alias reaches the real pipeline; no external calls are made."""
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from audit_core.common import BIZ_TYPE_NOT_REQUIRED_MESSAGE
from audit_core.model_settings import ModelSelection
from audit_core.oss_intake import OSSIntakeService, run_formal_audit_subprocess
from audit_core.scenario_registry import BIZ_TYPE_ALIASES, BIZ_TYPE_SCENARIOS, scenario_for_biz_type
from audit_core.workbench_runtime import build_parser, run_persistent_audit
from tests.pdf_test_support import PolicyProvider, bundle
from tests.test_oss_intake import _config, _fake_callback_sender, _no_network_url_validator, _payload, _wait_for_job


class DisplayBizTypeAliasTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="audit-display-alias-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for target in ("httpx.AsyncClient", "urllib.request.OpenerDirector.open", "socket.create_connection"):
            patcher = mock.patch(target, side_effect=AssertionError("External I/O forbidden in display alias fixture"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_only_the_explicit_display_alias_is_accepted(self):
        self.assertEqual(BIZ_TYPE_ALIASES, {"堆头": "陈列堆头", "陈列": "陈列堆头", "物料制作": "KT板等物料制作", "pos达标激励": "POS达标激励"})
        self.assertEqual(len(BIZ_TYPE_SCENARIOS), 8)
        for value in ("堆头", "陈列", "陈列堆头"):
            self.assertEqual(scenario_for_biz_type(value), "promotional_display")
        for value in (" 堆头", "堆头 ", "堆头费", " 陈列", "陈列 ", "陈 列", "陈列费", "陳列", "地堆", "promotional_display", "进场费", "", None, 1):
            with self.subTest(value=value):
                self.assertIsNone(scenario_for_biz_type(value))

    def test_alias_and_canonical_value_select_the_same_skill_and_keep_original_value(self):
        for index, (value, large_venue_fee) in enumerate((
            ("堆头", False), ("陈列堆头", False), ("陈列", False), ("陈列", True),
        )):
            with self.subTest(biz_type=value, large_venue_fee=large_venue_fee):
                provider = PolicyProvider(scenarios=("entry_fee",))
                inputs = self.root / str(index) / "input"
                bundle(inputs, "其他费用.zip")
                result = run_persistent_audit("20261024-display-alias", producer_model="codex",
                    input_dir=inputs, worktrees_root=self.root / str(index) / "worktrees",
                    evidence_provider=provider, biz_type=value, large_venue_fee=large_venue_fee,
                    cvs_otc_channel=True, input_source="oss")
                self.assertEqual(result["status"], "completed")
                self.assertEqual(result["scenarios"], ["promotional_display"])
                self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification", "pdf_policy_audit"])
                self.assertEqual(provider.calls[0]["selected_scenario"], "promotional_display")
                self.assertIs(provider.calls[0]["large_venue_fee"], large_venue_fee)
                self.assertIs(provider.calls[0]["cvs_otc_channel"], True)
                workspace = Path(result["worktree"])
                manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
                snapshot = json.loads(Path(result["snapshot"]).read_text(encoding="utf-8"))
                self.assertEqual(manifest["bizType"], value)
                self.assertIs(manifest["largeVenueFee"], large_venue_fee)
                self.assertIs(manifest["cvsOtcChannel"], True)
                self.assertEqual(snapshot["view"]["sheets"][0]["audit_type_label"], "陈列堆头")

    def test_oss_alias_downloads_audits_callbacks_and_replay_creates_a_new_archive(self):
        providers = []

        def download(_submission, destination, _settings):
            bundle(destination.parent, destination.name)
            content = destination.read_bytes()
            return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

        def run(**kwargs):
            kwargs.pop("cancel_event", None)
            provider = PolicyProvider()
            providers.append(provider)
            return run_persistent_audit(**kwargs, evidence_provider=provider, input_source="oss")

        callback = mock.Mock(side_effect=_fake_callback_sender)
        service = OSSIntakeService(worktrees_root=self.root / "worktrees", input_root=self.root / "input",
            config=_config(), downloader=download, runner=run,
            url_validator=_no_network_url_validator, callback_sender=callback)
        try:
            payload = {**_payload(), "bizType": "陈列", "largeVenueFee": False}
            first, created = service.submit(payload)
            self.assertTrue(created)
            complete = _wait_for_job(service, first["job_id"], "completed", timeout=20)
            original_receipt = service.store._path(first["job_id"]).read_bytes()
            manifest_path = Path(complete["result"]["worktree"]) / "manifest.json"
            original_manifest = manifest_path.read_bytes()
            replay, created = service.submit(payload)
            self.assertTrue(created)
            self.assertEqual(replay["attempt"], 2)
            second = _wait_for_job(service, replay["job_id"], "completed", timeout=20)
            self.assertNotEqual(first["job_id"], replay["job_id"])
            self.assertNotEqual(complete["result"]["workspace_id"], second["result"]["workspace_id"])
            self.assertEqual(service.store._path(first["job_id"]).read_bytes(), original_receipt)
            self.assertEqual(manifest_path.read_bytes(), original_manifest)
            self.assertEqual(len(providers), 2)
            self.assertEqual(callback.call_count, 2)
            for item, call in zip((complete, second), callback.call_args_list):
                self.assertEqual(item["bizType"], "陈列")
                self.assertIsNotNone(item["audit_started_at"])
                self.assertEqual(item["result"]["scenarios"], ["promotional_display"])
                self.assertEqual(item["callback"]["status"], "delivered")
                body = call.args[0]
                self.assertEqual(set(body), {"verifyCode", "analyzeId", "result"})
                self.assertNotEqual(body["result"], BIZ_TYPE_NOT_REQUIRED_MESSAGE)
                self.assertEqual(body["result"], Path(item["result"]["analysis_summary"]).read_text(encoding="utf-8").strip())
        finally:
            service.close()
            service._download_worker.join(timeout=5)
            for worker in service._audit_workers:worker.join(timeout=5)

    def test_display_alias_reaches_the_formal_cli_without_changing_input_flags(self):
        captured = []

        def invoke(command, **kwargs):
            captured.extend(command)
            result = Path(command[command.index("--result-json") + 1])
            result.write_text(json.dumps({"run_id": "20261103-display-alias-cli",
                                         "workspace_id": "fixture-display", "status": "completed"}),
                              encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="")

        with (mock.patch("audit_core.oss_intake.subprocess.run", side_effect=invoke),
              mock.patch("audit_core.model_settings.ModelSettingsStore.snapshot", return_value=ModelSelection())):
            result = run_formal_audit_subprocess(
                run_id="20261103-display-alias-cli", producer_model="codex",
                input_dir=self.root / "input", worktrees_root=self.root / "worktrees",
                scenario="promotional_display", workbench_url=None,
                biz_type="陈列", large_venue_fee=True, cvs_otc_channel=True,
            )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(Path(captured[2]).name, "run.py")
        parsed = build_parser().parse_args(captured[3:])
        self.assertEqual(parsed.biz_type, "陈列")
        self.assertEqual(parsed.scenario, "promotional_display")
        self.assertEqual(parsed.large_venue_fee, "true")
        self.assertEqual(parsed.cvs_otc_channel, "true")
        self.assertEqual(parsed.input_source, "oss")

    def test_old_skipped_display_history_is_not_rewritten_or_automatically_replayed(self):
        def download(_submission, destination, _settings):
            bundle(destination.parent, destination.name)
            content = destination.read_bytes()
            return {"bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}

        providers = []

        def run(**kwargs):
            kwargs.pop("cancel_event", None)
            provider = PolicyProvider(missing={0: ("settlement",)})
            providers.append(provider)
            return run_persistent_audit(**kwargs, evidence_provider=provider, input_source="oss")

        downloader, runner = mock.Mock(side_effect=download), mock.Mock(side_effect=run)
        callback = mock.Mock(side_effect=_fake_callback_sender)
        service = OSSIntakeService(worktrees_root=self.root / "worktrees", input_root=self.root / "input",
            config=_config(), downloader=downloader, runner=runner,
            url_validator=_no_network_url_validator, callback_sender=callback)
        try:
            payload = {**_payload(verify_code="HX-FIXTURE-DISPLAY-ALIAS", analyze_id=705),
                       "bizType": "陈列", "largeVenueFee": False}
            old_aliases = {key: value for key, value in BIZ_TYPE_ALIASES.items() if key != "陈列"}
            with mock.patch.dict(BIZ_TYPE_ALIASES, old_aliases, clear=True):
                first, _ = service.submit(payload)
                skipped = _wait_for_job(service, first["job_id"], "completed", timeout=20)
            receipt_path = service.store._path(first["job_id"])
            frozen = receipt_path.read_bytes()
            self.assertFalse(skipped["result"]["audit_required"])
            self.assertIsNone(skipped["delivery"])
            self.assertIsNone(skipped["audit_started_at"])
            downloader.assert_not_called()
            runner.assert_not_called()
            self.assertEqual(callback.call_args.args[0]["result"], BIZ_TYPE_NOT_REQUIRED_MESSAGE)
            self.assertEqual(service.get(first["job_id"]), skipped)
            self.assertEqual(len(service.list()), 1)
            self.assertEqual(receipt_path.read_bytes(), frozen)
            self.assertEqual(callback.call_count, 1)
            replay, created = service.submit(payload)
            self.assertTrue(created)
            self.assertEqual(replay["attempt"], 2)
            self.assertNotEqual(first["job_id"], replay["job_id"])
            complete = _wait_for_job(service, replay["job_id"], "completed", timeout=20)
            self.assertEqual(complete["result"]["scenarios"], ["promotional_display"])
            self.assertEqual(complete["bizType"], "陈列")
            self.assertEqual(runner.call_args.kwargs["biz_type"], "陈列")
            downloader.assert_called_once()
            runner.assert_called_once()
            self.assertEqual(len(providers), 1)
            self.assertEqual([call["kind"] for call in providers[0].calls], ["pdf_material_classification"])
            self.assertEqual(callback.call_count, 2)
            self.assertIn("缺少结算单", callback.call_args.args[0]["result"])
            self.assertNotIn(BIZ_TYPE_NOT_REQUIRED_MESSAGE, callback.call_args.args[0]["result"])
            self.assertEqual(receipt_path.read_bytes(), frozen)
        finally:
            service.close()
            service._download_worker.join(timeout=5)
            for worker in service._audit_workers:worker.join(timeout=5)


if __name__ == "__main__":
    unittest.main()
