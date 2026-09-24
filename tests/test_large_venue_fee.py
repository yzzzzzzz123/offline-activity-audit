"""Business-supplied venue fees control the display agreement requirement."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.oss_intake import (
    FORMAL_RUNNER, OSSIntakeConflictError, OSSIntakeRequestError, OSSIntakeService,
    OSSIntakeStore, normalize_submission, public_job, run_formal_audit_subprocess,
)
from audit_core.pdf_evidence import classification_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.scenario_registry import SCENARIO_LABELS
from audit_core.workbench_runtime import run_persistent_audit
from tests.pdf_test_support import PolicyProvider, bundle, classification, fixture_cli_command, flags_for
from tests.test_oss_intake import (
    _config, _fake_callback_sender, _fake_downloader, _no_network_url_validator,
    _payload, _wait_for_job,
)


class LargeVenueFeeTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def submission(self, value=None):
        payload = _payload()
        if value is not None:
            payload["largeVenueFee"] = value
        return {**normalize_submission(payload, _config(), url_validator=_no_network_url_validator),
                "producer_model": "codex"}

    def run_display(self, value, *, missing=False, flags=None, folder="run", name="普通小型活动.zip"):
        root = self.root / folder
        bundle(root / "input", name)
        provider = PolicyProvider(missing={0: ("atrium_agreement",)} if missing else {},
                                  flags={0: flags} if flags is not None else {})
        result = run_persistent_audit(
            "20260923-venue-fee", producer_model="codex", input_dir=root / "input",
            worktrees_root=root / "worktrees", evidence_provider=provider,
            biz_type="陈列堆头", input_source="oss", large_venue_fee=value,
        )
        return result, provider

    def test_boolean_values_preserved_without_changing_historical_identity(self):
        original = self.submission()
        self.assertIsNone(original["large_venue_fee"])
        for value in (True, False):
            actual = self.submission(value)
            self.assertIs(actual["large_venue_fee"], value)
            self.assertEqual(actual["fingerprint"], original["fingerprint"])
            self.assertEqual(actual["job_id"], original["job_id"])

    def test_strings_numbers_null_and_containers_are_rejected(self):
        for value in ("true", "false", "True", "", 0, 1, 0.0, 1.0, None, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(OSSIntakeRequestError, "largeVenueFee"):
                normalize_submission({**_payload(), "largeVenueFee": value}, _config(),
                                     url_validator=_no_network_url_validator)

    def test_old_receipt_projection_is_unknown_and_does_not_rewrite_history(self):
        receipt = {"verifyCode": "HX202608310001", "analyzeId": 123}
        original = deepcopy(receipt)
        self.assertIsNone(public_job(receipt)["largeVenueFee"])
        self.assertEqual(receipt, original)

    def test_active_attempt_cannot_change_fee_flag_including_omitted_vs_false(self):
        for index, (before, after) in enumerate(((True, False), (False, True), (None, False), (False, None))):
            with self.subTest(before=before, after=after):
                store = OSSIntakeStore(self.root / str(index))
                first, created = store.create_or_get(self.submission(before))
                self.assertTrue(created)
                duplicate, created = store.create_or_get(self.submission(before))
                self.assertFalse(created)
                self.assertEqual(first["job_id"], duplicate["job_id"])
                with self.assertRaisesRegex(OSSIntakeConflictError, "largeVenueFee"):
                    store.create_or_get(self.submission(after))
                store.update(first["job_id"], status="completed")
                saved = store._path(first["job_id"]).read_bytes()
                next_job, created = store.create_or_get(self.submission(after))
                self.assertTrue(created)
                self.assertEqual(next_job["attempt"], 2)
                self.assertIs(next_job["largeVenueFee"], after)
                self.assertEqual(store._path(first["job_id"]).read_bytes(), saved)

    def test_queue_preserves_both_values_and_callback_stays_three_fields(self):
        runner = mock.Mock(side_effect=lambda **kw: {
            "status": "completed", "run_id": kw["run_id"], "workspace_id": "fixture",
        })
        callback = mock.Mock(side_effect=_fake_callback_sender)
        service = OSSIntakeService(
            config=_config(), input_root=self.root / "input", worktrees_root=self.root / "worktrees",
            downloader=_fake_downloader, runner=runner, callback_sender=callback,
            url_validator=_no_network_url_validator,
        )
        self.addCleanup(service.close)
        for index, value in enumerate((True, False), 1):
            accepted, _ = service.submit({**_payload(analyze_id=index), "bizType": "陈列堆头", "largeVenueFee": value})
            completed = _wait_for_job(service, accepted["job_id"], "completed")
            self.assertIs(completed["largeVenueFee"], value)
            self.assertIs(runner.call_args.kwargs["large_venue_fee"], value)
            self.assertEqual(set(callback.call_args.args[0]), {"verifyCode", "analyzeId", "result"})

    def test_true_missing_agreement_completes_with_specific_material_issue(self):
        for index, name in enumerate(("普通小型活动.zip", "中庭大型活动有入场协议.zip")):
            result, provider = self.run_display(True, missing=True, folder=str(index), name=name)
            self.assertEqual(result["status"], "completed")
            self.assertIsNone(result["failure"])
            self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification"])
            summary = Path(result["analysis_summary"]).read_text(encoding="utf-8")
            self.assertIn("缺少商场入场协议", summary)
            self.assertNotIn("largeVenueFee", summary)
            manifest = json.loads((Path(result["worktree"]) / "manifest.json").read_text(encoding="utf-8"))
            self.assertIs(manifest["largeVenueFee"], True)

    def test_false_does_not_require_agreement_and_survives_saved_evidence(self):
        result, provider = self.run_display(False, missing=True, name="中庭大型活动.zip")
        self.assertEqual(result["status"], "completed")
        self.assertEqual([c["kind"] for c in provider.calls], ["pdf_material_classification", "pdf_policy_audit"])
        self.assertIs(provider.calls[1]["flags"]["atrium"], False)
        workspace = Path(result["worktree"])
        manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
        self.assertIs(manifest["largeVenueFee"], False)
        evidence = json.loads((workspace / "analysis/pdf-policy/evidence.json").read_text(encoding="utf-8"))
        packet = evidence["packets"][0]
        self.assertIs(packet["large_venue_fee"], False)
        material = next(m for m in packet["classification"]["materials"] if m["id"] == "atrium_agreement")
        self.assertEqual(material["state"], "not_applicable")
        self.assertNotIn("缺少商场入场协议", Path(result["analysis_summary"]).read_text(encoding="utf-8"))

    def test_true_with_agreement_enters_existing_audit(self):
        result, provider = self.run_display(True)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(provider.calls), 2)
        self.assertIs(provider.calls[1]["flags"]["atrium"], True)
        self.assertEqual(next(m["state"] for m in provider.calls[1]["materials"] if m["id"] == "atrium_agreement"), "present")

    def test_omitted_flag_preserves_unknown_condition_instead_of_assuming_false(self):
        result, provider = self.run_display(None, flags=flags_for("promotional_display", atrium=None))
        self.assertEqual(result["status"], "completed")
        self.assertEqual(len(provider.calls), 1)
        self.assertIsNone(provider.calls[0]["large_venue_fee"])
        summary = Path(result["analysis_summary"]).read_text(encoding="utf-8")
        self.assertNotIn("缺少商场入场协议", summary)
        evidence = json.loads((Path(result["worktree"]) / "analysis/pdf-policy/evidence.json").read_text(encoding="utf-8"))
        material = next(m for m in evidence["packets"][0]["classification"]["materials"] if m["id"] == "atrium_agreement")
        self.assertEqual(material["state"], "unclear")

    def test_model_cannot_reverse_or_erase_explicit_flag(self):
        for supplied in (True, False):
            for model_flag in (not supplied, None):
                value = classification("promotional_display", ["u"], flags=flags_for("promotional_display", atrium=model_flag))
                selected = next(m for m in value["material_matches"] if m["scenario"] == "promotional_display")
                value.update(candidate_scenarios=["promotional_display"], material_matches=[selected],
                             flags=selected["flags"], materials=selected["materials"])
                with self.subTest(supplied=supplied, model_flag=model_flag), self.assertRaises(AuditError):
                    classification_decision(value, [{"unit_id": "u"}], "promotional_display", large_venue_fee=supplied)

    def test_other_seven_types_do_not_acquire_an_agreement_requirement(self):
        for scenario in SCENARIO_LABELS:
            if scenario == "promotional_display":
                continue
            value = classification(scenario, ["u"])
            selected = next(m for m in value["material_matches"] if m["scenario"] == scenario)
            value["material_matches"] = [selected]
            result = classification_decision(value, [{"unit_id": "u"}], scenario, large_venue_fee=True)
            self.assertTrue(result["matched"])
            self.assertNotIn("atrium_agreement", [m["id"] for m in selected["materials"]])

    def test_real_provider_prompt_and_schema_use_supplied_boolean(self):
        for value in (True, False):
            provider = PdfEvidenceProvider(None, None)
            root = self.root / str(value)
            root.mkdir()
            case = {"kind": "pdf_material_classification", "archive_id": "A001",
                    "selected_scenario": "promotional_display", "large_venue_fee": value,
                    "units": [{"unit_id": "u", "image": None, "native_facts": ["本次资料"], "limitations": []}]}
            reading = classification("promotional_display", ["u"], flags=flags_for("promotional_display", atrium=value))
            selected = next(m for m in reading["material_matches"] if m["scenario"] == "promotional_display")
            reading.update(material_matches=[selected], flags=selected["flags"], materials=selected["materials"])

            def call(call_root, skill, schema, prompt, **kwargs):
                self.assertIn("largeVenueFee=" + json.dumps(value), render_prompt(prompt))
                self.assertEqual(schema["properties"]["flags"]["properties"]["atrium"]["enum"], [value])
                kwargs["validator"](reading)
                return reading

            with mock.patch.object(provider, "_call", side_effect=call):
                provider(case, root)

    def test_formal_subprocess_cli_preserves_true_and_false(self):
        native_run = subprocess.run

        def fixture_run(command, **kwargs):
            self.assertEqual(command[2], str(FORMAL_RUNNER))
            self.assertIn("--large-venue-fee=" + json.dumps(value), command)
            return native_run(fixture_cli_command(command[3:]), **kwargs)

        for value in (True, False):
            root = self.root / str(value)
            bundle(root / "input")
            with mock.patch("audit_core.oss_intake.subprocess.run", side_effect=fixture_run):
                result = run_formal_audit_subprocess(
                    run_id="20260923-venue-cli", producer_model="codex", input_dir=root / "input",
                    worktrees_root=root / "worktrees", scenario=None, workbench_url=None,
                    biz_type="陈列堆头", large_venue_fee=value,
                )
            self.assertEqual(result["status"], "completed")
            manifest = json.loads((Path(result["worktree"]) / "manifest.json").read_text(encoding="utf-8"))
            self.assertIs(manifest["largeVenueFee"], value)
            self.assertTrue((Path(result["worktree"]) / "offline-activity-audit.html").is_file())
