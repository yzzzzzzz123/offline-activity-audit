from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest import mock

from audit_core.callback_delivery import deliver_completed_callback, prepare_callback_delivery, main
from audit_core.common import AuditError
from audit_core.oss_intake import MAX_CALLBACK_RESULT_CHARS, OSSIntakeCallbackError, OSSIntakeConfig
from audit_core.workbench_store import atomic_write_json


class CallbackDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.workspace = self.root / "明确选择的核销档案"
        self.workspace.mkdir()
        self.config = OSSIntakeConfig(allowed_hosts=("unused.invalid",), callback_url="https://callback.example/api/v1/ai/analyze/callback?secret=never-record", callback_token="never-record-token")
        self.summary = "## 维护费用\n\n- POS 销售电子表未提交。\n"
        self.manifest = {"workspace_id": self.workspace.name, "run_id": "20260910-callback-fixture",
                         "status": "completed", "updated_at": "after-snapshot", "snapshot_sha256": None}
        self.write_fixture()

    def write_fixture(self):
        encoded = self.summary.encode("utf-8")
        (self.workspace / "ai-analysis-summary.md").write_bytes(encoded)
        self.manifest["analysis_summary"] = {"path": "ai-analysis-summary.md", "size": len(encoded), "sha256": hashlib.sha256(encoded).hexdigest()}
        self.write_run_data()

    def write_run_data(self):
        snapshot_run = deepcopy(self.manifest)
        snapshot_run.update(updated_at="before-manifest", snapshot_sha256=None)
        snapshot = {"run": snapshot_run, "view": {"schema_version": "1.1", "sheets": []}}
        content = json.dumps(snapshot, ensure_ascii=False).encode("utf-8")
        (self.workspace / "snapshot.json").write_bytes(content)
        self.manifest["snapshot_sha256"] = hashlib.sha256(content).hexdigest()
        (self.workspace / "manifest.json").write_text(json.dumps(self.manifest, ensure_ascii=False), encoding="utf-8")

    def kwargs(self, **changes):
        return {"workspace_id": self.workspace.name, "verify_code": "HX202601160053", "analyze_id": 9, "config": self.config, **changes}

    def historic_bytes(self):
        return {path.name: path.read_bytes() for path in self.workspace.iterdir()}

    def receipts(self):
        return list((self.root / ".intake" / "callback-deliveries").glob("*.json"))

    def sender(self):
        return mock.Mock(return_value={"status": "delivered", "http_status": 204, "attempts": 1, "delivered_at": "2026-09-10T12:00:00Z"})

    def test_dry_run_is_read_only_and_exposes_exact_three_field_payload(self):
        before = self.historic_bytes()
        sender = self.sender()
        result = deliver_completed_callback(self.root, **self.kwargs(), sender=sender)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["payload"], {"verifyCode": "HX202601160053", "analyzeId": 9, "result": self.summary.strip()})
        self.assertEqual(set(result["payload"]), {"verifyCode", "analyzeId", "result"})
        self.assertFalse((self.root / ".intake").exists())
        self.assertEqual(self.historic_bytes(), before)
        sender.assert_not_called()
        self.assertNotIn("never-record", json.dumps(result))

    def test_success_is_persisted_and_same_payload_is_not_sent_twice(self):
        before = self.historic_bytes()
        sender = self.sender()
        first = deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=sender)
        second = deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=sender)
        self.assertEqual(first["status"], "delivered")
        self.assertEqual(second["status"], "already_delivered")
        sender.assert_called_once()
        self.assertEqual(len(self.receipts()), 1)
        saved = self.receipts()[0].read_text(encoding="utf-8")
        self.assertNotIn("never-record", saved)
        self.assertNotIn("https://", saved)
        self.assertEqual(json.loads(saved)["deliveries"][0]["http_status"], 204)
        self.assertEqual(self.historic_bytes(), before)

    def test_failure_keeps_receipt_and_retry_only_resends_same_payload(self):
        before = self.historic_bytes()
        failed_sender = mock.Mock(side_effect=OSSIntakeCallbackError("secret target never-record", attempts=3, http_status=503))
        with self.assertRaisesRegex(AuditError, "独立回执已保存"):
            deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=failed_sender)
        failed = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["deliveries"][0]["attempts"], 3)
        self.assertNotIn("never-record", json.dumps(failed))
        sender = self.sender()
        retried = deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=sender)
        self.assertEqual(retried["status"], "delivered")
        self.assertEqual([item["status"] for item in retried["deliveries"]], ["failed", "delivered"])
        self.assertEqual(sender.call_args.args[0], failed_sender.call_args.args[0])
        self.assertEqual(self.historic_bytes(), before)

    def test_successful_send_with_failed_receipt_write_is_reported_accurately(self):
        def write_before_send_only(path, value):
            if value.get("status") == "delivered":
                raise OSError("disk unavailable")
            atomic_write_json(path, value)

        sender = self.sender()
        with mock.patch("audit_core.callback_delivery.atomic_write_json", side_effect=write_before_send_only):
            with self.assertRaisesRegex(AuditError, "回调已返回成功.*回执保存失败"):
                deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=sender)
        sender.assert_called_once()
        pending = json.loads(self.receipts()[0].read_text(encoding="utf-8"))
        self.assertEqual(pending["status"], "sending")
        resumed = deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=self.sender())
        self.assertEqual(resumed["idempotency_key"], pending["idempotency_key"])
        self.assertEqual(resumed["status"], "delivered")

    def test_wrong_workspace_identity_nonterminal_or_tampered_snapshot_never_sends(self):
        for mode in ("wrong_workspace", "failed", "tampered_snapshot", "different_run", "saved_identity"):
            with self.subTest(mode=mode):
                self.manifest = {"workspace_id": self.workspace.name, "run_id": "fixture", "status": "completed"}
                self.write_fixture()
                if mode == "wrong_workspace":
                    self.manifest["workspace_id"] = "另一个档案"
                    self.write_run_data()
                elif mode == "failed":
                    self.manifest["status"] = "failed"
                    self.write_run_data()
                elif mode == "saved_identity":
                    self.manifest["analyzeId"] = 8
                    self.write_run_data()
                else:
                    path = self.workspace / "snapshot.json"
                    snapshot = json.loads(path.read_text(encoding="utf-8"))
                    snapshot["run"]["run_id"] = "wrong-run"
                    content = json.dumps(snapshot).encode("utf-8")
                    path.write_bytes(content)
                    if mode == "different_run":
                        self.manifest["snapshot_sha256"] = hashlib.sha256(content).hexdigest()
                        (self.workspace / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
                sender = self.sender()
                with self.assertRaises(AuditError):
                    deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=sender)
                sender.assert_not_called()
        self.assertFalse(self.receipts())

    def test_summary_path_size_hash_empty_or_truncation_cannot_be_bypassed(self):
        for mode in ("path", "size", "hash", "missing", "empty", "too_long", "truncated", "plain_truncated"):
            with self.subTest(mode=mode):
                self.summary = "可核对的完整小结"
                self.write_fixture()
                if mode in {"path", "size", "hash"}:
                    key, value = {"path": ("path", "../another-summary.md"), "size": ("size", 0), "hash": ("sha256", "0" * 64)}[mode]
                    self.manifest["analysis_summary"][key] = value
                    self.write_run_data()
                elif mode == "missing":
                    (self.workspace / "ai-analysis-summary.md").unlink()
                else:
                    self.summary = {"empty": " \n", "too_long": "中" * (MAX_CALLBACK_RESULT_CHARS + 1), "truncated": "回调内容已截断；完整小结另存。",
                                    "plain_truncated": "问题较多，这里只显示部分。完整问题请在核销记录中查看。"}[mode]
                    self.write_fixture()
                sender = self.sender()
                with self.assertRaises(AuditError):
                    deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=sender)
                sender.assert_not_called()

    def test_explicit_identity_validation_never_guesses_from_name(self):
        for change in ({"workspace_id": "../other"}, {"verify_code": "bad code"}, {"analyze_id": True}, {"analyze_id": "9"}, {"analyze_id": 0}, {"analyze_id": 2**63}):
            with self.subTest(change=change), self.assertRaises(AuditError):
                prepare_callback_delivery(self.root, **self.kwargs(**change))

    def test_receipt_identity_includes_payload_and_destination(self):
        original = prepare_callback_delivery(self.root, **self.kwargs())
        self.summary += "\n- 结算单主件待确认。"
        self.write_fixture()
        changed = prepare_callback_delivery(self.root, **self.kwargs())
        other_config = OSSIntakeConfig(allowed_hosts=("unused.invalid",), callback_url="https://second.example/api/v1/ai/analyze/callback")
        other_target = prepare_callback_delivery(self.root, **self.kwargs(config=other_config))
        self.assertNotEqual(original["delivery_id"], changed["delivery_id"])
        self.assertNotEqual(changed["delivery_id"], other_target["delivery_id"])
        self.assertEqual(changed["payload_sha256"], other_target["payload_sha256"])

    def test_active_oss_attempt_for_same_identity_blocks_local_delivery(self):
        jobs = self.root / ".intake" / "jobs"
        jobs.mkdir(parents=True)
        receipt = jobs / "active.json"
        receipt.write_text(json.dumps({"verifyCode": "HX202601160053", "analyzeId": 9, "status": "running"}), encoding="utf-8")
        before = receipt.read_bytes()
        sender = self.sender()
        with self.assertRaisesRegex(AuditError, "OSS 任务执行中"):
            deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=sender)
        sender.assert_not_called()
        self.assertEqual(receipt.read_bytes(), before)
        self.assertFalse(self.receipts())
        receipt.write_text(json.dumps({"verifyCode": "unrelated", "analyzeId": 9, "status": "running"}), encoding="utf-8")
        self.assertEqual(deliver_completed_callback(self.root, **self.kwargs())["status"], "ready")

    def test_linked_receipt_directory_is_rejected_before_writing(self):
        with mock.patch("audit_core.callback_delivery._is_link", side_effect=lambda path: path.name == ".intake"):
            with self.assertRaisesRegex(AuditError, "回执目录"):
                deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=self.sender())
        self.assertFalse((self.root / ".intake").exists())

    def test_concurrent_senders_cannot_deliver_same_business_target_twice(self):
        started, release = threading.Event(), threading.Event()
        outcomes = []

        def waiting_sender(payload, config):
            started.set()
            if not release.wait(5):
                raise AssertionError("test did not release sender")
            return self.sender()(payload, config)

        def first():
            try:
                outcomes.append(deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=waiting_sender))
            except Exception as exc:
                outcomes.append(exc)

        thread = threading.Thread(target=first)
        thread.start()
        try:
            self.assertTrue(started.wait(3))
            duplicate = self.sender()
            with self.assertRaisesRegex(AuditError, "补发正在执行"):
                deliver_completed_callback(self.root, **self.kwargs(), apply=True, sender=duplicate)
            duplicate.assert_not_called()
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(len(outcomes), 1)
        self.assertIsInstance(outcomes[0], dict)
        self.assertEqual(outcomes[0]["status"], "delivered")

    def test_cli_defaults_to_dry_run_using_existing_callback_environment(self):
        environment = {"OFFLINE_AUDIT_OSS_CALLBACK_URL": "http://127.0.0.1:8765/api/v1/ai/analyze/callback", "OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK": "1", "OFFLINE_AUDIT_OSS_CALLBACK_TOKEN": "not-output"}
        with mock.patch.dict("os.environ", environment), mock.patch("builtins.print") as output, mock.patch("audit_core.callback_delivery.post_analysis_callback") as sender:
            status = main(["--worktrees-root", str(self.root), "--workspace-id", self.workspace.name, "--verify-code", "HX202601160053", "--analyze-id", "9"])
        self.assertEqual(status, 0)
        self.assertNotIn("not-output", str(output.call_args))
        sender.assert_not_called()
        self.assertFalse((self.root / ".intake").exists())


if __name__ == "__main__":
    unittest.main()
