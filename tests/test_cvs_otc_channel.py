"""Explicit channel selection survives intake, routing and the formal runner."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.local_upload import upload_metadata
from audit_core.oss_intake import (FORMAL_RUNNER, OSSIntakeConflictError, OSSIntakeStore,
                                  normalize_submission, public_job, run_formal_audit_subprocess)
from audit_core.pdf_evidence import classification_decision, classification_schema, validate
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.scenario_registry import SCENARIO_LABELS
from audit_core.workbench_runtime import run_persistent_audit
from tests.pdf_test_support import (PolicyProvider, bundle, classification, fixture_cli_command, flags_for)
from tests.test_oss_intake import _config, _no_network_url_validator, _payload


class CvsOtcChannelTests(unittest.TestCase):
    def setUp(self):
        from audit_core.model_settings import ModelSelection
        patcher = mock.patch('audit_core.model_settings.ModelSettingsStore.snapshot',
                             return_value=ModelSelection(api_key='sk_test_fixture_only'))
        patcher.start()
        self.addCleanup(patcher.stop)
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def submission(self, **fields):
        return {**normalize_submission({**_payload(), 'bizType': '条码费', **fields}, _config(),
                                       url_validator=_no_network_url_validator),
                'producer_model': 'codex'}

    def test_only_json_true_enables_channel_without_changing_business_identity(self):
        original = self.submission()
        self.assertIs(original['cvs_otc_channel'], False)
        for raw in (True, False, None, '', 'true', 'false', 'True', '是', 0, 1, 1.0, [], {}, [True]):
            with self.subTest(raw=raw):
                normalized = self.submission(cvsOtcChannel=raw)
                self.assertIs(normalized['cvs_otc_channel'], raw is True)
                self.assertEqual(normalized['fingerprint'], original['fingerprint'])
                self.assertEqual(normalized['job_id'], original['job_id'])

    def test_active_job_cannot_change_channel_but_false_equivalents_are_duplicates(self):
        store = OSSIntakeStore(self.root)
        first, created = store.create_or_get(self.submission())
        self.assertTrue(created)
        for raw in (None, False, 'true', 1):
            duplicate, created = store.create_or_get(self.submission(cvsOtcChannel=raw))
            self.assertFalse(created)
            self.assertEqual(duplicate['job_id'], first['job_id'])
        with self.assertRaisesRegex(OSSIntakeConflictError, 'cvsOtcChannel'):
            store.create_or_get(self.submission(cvsOtcChannel=True))
        store.update(first['job_id'], status='completed')
        saved = store._path(first['job_id']).read_bytes()
        second, created = store.create_or_get(self.submission(cvsOtcChannel=True))
        self.assertTrue(created)
        self.assertEqual(second['attempt'], 2)
        self.assertIs(second['cvsOtcChannel'], True)
        self.assertEqual(store._path(first['job_id']).read_bytes(), saved)

    def test_old_receipts_are_only_projected_and_upload_choice_is_type_scoped(self):
        receipt = {'verifyCode': 'HX202608160003', 'analyzeId': 123}
        original = deepcopy(receipt)
        self.assertIs(public_job(receipt)['cvsOtcChannel'], False)
        self.assertEqual(receipt, original)
        self.assertIs(upload_metadata('资料.zip', '条码费', 'a' * 24, None, True)['cvs_otc_channel'], True)
        self.assertIs(upload_metadata('资料.zip', '条码费', 'a' * 24, None, 'true')['cvs_otc_channel'], False)
        self.assertIs(upload_metadata('资料.zip', '陈列堆头', 'a' * 24, None, True)['cvs_otc_channel'], False)

    def selected_reading(self, flag):
        value = classification('entry_fee', ['u'], flags=flags_for('entry_fee', cvs_otc=flag))
        selected = next(m for m in value['material_matches'] if m['scenario'] == 'entry_fee')
        value.update(candidate_scenarios=['entry_fee'], material_matches=[selected],
                     flags=selected['flags'], materials=selected['materials'])
        return value

    def test_model_cannot_override_channel_or_make_it_unknown(self):
        for supplied in (True, False):
            for model_flag in (not supplied, None):
                with self.subTest(supplied=supplied, model_flag=model_flag), self.assertRaises(AuditError):
                    classification_decision(self.selected_reading(model_flag), [{'unit_id': 'u'}],
                                            'entry_fee', cvs_otc_channel=supplied)

    def test_other_seven_types_do_not_acquire_inventory_requirement(self):
        for scenario in SCENARIO_LABELS:
            if scenario == 'entry_fee':
                continue
            value = classification(scenario, ['u'])
            selected = next(m for m in value['material_matches'] if m['scenario'] == scenario)
            value.update(candidate_scenarios=[scenario], material_matches=[selected],
                         flags=selected['flags'], materials=selected['materials'])
            self.assertTrue(classification_decision(value, [{'unit_id': 'u'}], scenario,
                                                    cvs_otc_channel=True)['matched'])
            self.assertNotIn('entry_system', [m['id'] for m in selected['materials']])

    def test_provider_binds_prompt_and_schema_to_the_boolean(self):
        for flag in (True, False):
            case = {'kind': 'pdf_material_classification', 'archive_id': 'A001',
                    'selected_scenario': 'entry_fee', 'cvs_otc_channel': flag,
                    'units': [{'unit_id': 'u', 'image': None, 'native_facts': ['本次资料'], 'limitations': []}]}
            provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)

            def call(call_root, skill, schema, prompt, **kwargs):
                self.assertIn('cvsOtcChannel=' + json.dumps(flag), render_prompt(prompt))
                self.assertEqual(schema['properties']['flags']['properties']['cvs_otc']['enum'], [flag])
                kwargs['validator'](self.selected_reading(flag))
                return self.selected_reading(flag)

            with mock.patch.object(provider, '_call', side_effect=call):
                root = self.root / str(flag)
                root.mkdir()
                provider(case, root)

    def test_false_with_missing_channel_text_uses_normal_rules_and_saves_flag(self):
        bundle(self.root / 'input', 'CVS-OTC-资料.zip')
        provider = PolicyProvider()
        result = run_persistent_audit('20261103-entry-channel', producer_model='codex',
            input_dir=self.root / 'input', worktrees_root=self.root / 'worktrees',
            evidence_provider=provider, biz_type='条码费')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(len(provider.calls), 2)
        self.assertIs(provider.calls[1]['flags']['cvs_otc'], False)
        workspace = Path(result['worktree'])
        self.assertIs(json.loads((workspace / 'manifest.json').read_text(encoding='utf-8'))['cvsOtcChannel'], False)
        packet = json.loads((workspace / 'analysis/pdf-policy/evidence.json').read_text(encoding='utf-8'))['packets'][0]
        self.assertIs(packet['cvs_otc_channel'], False)
        self.assertEqual(next(m['state'] for m in packet['classification']['materials']
                              if m['id'] == 'entry_system'), 'not_applicable')

    def test_formal_subprocess_cli_preserves_both_values(self):
        native_run = subprocess.run
        for flag in (True, False):
            root = self.root / str(flag)
            bundle(root / 'input')

            def fixture_run(command, **kwargs):
                self.assertEqual(command[2], str(FORMAL_RUNNER))
                self.assertIn('--cvs-otc-channel=' + json.dumps(flag), command)
                return native_run(fixture_cli_command(command[3:]), **kwargs)

            with mock.patch('audit_core.oss_intake.subprocess.run', side_effect=fixture_run):
                result = run_formal_audit_subprocess(run_id='20261103-entry-cli', producer_model='codex',
                    input_dir=root / 'input', worktrees_root=root / 'worktrees', scenario=None,
                    workbench_url=None, biz_type='条码费', cvs_otc_channel=flag)
            self.assertEqual(result['status'], 'completed')
            self.assertIs(json.loads((Path(result['worktree']) / 'manifest.json').read_text(encoding='utf-8'))['cvsOtcChannel'], flag)
