"""Effort survives settings, subprocess snapshots and the actual HTTP payload."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import httpx

from audit_core.model_settings import (
    ModelSettingsError, ModelSettingsStore, SNAPSHOT_ENV, api_selection,
    selection_from_environment,
)
from audit_core.qwen_model import complete_json


class DeepSeekReasoningEffortTests(unittest.TestCase):
    def selection(self, provider, effort=None):
        values = {'provider': provider, 'api_key': 'fixture-effort-key'}
        if effort is not None:
            values['reasoning_effort'] = effort
        return api_selection(values)

    def test_legacy_defaults_do_not_change(self):
        self.assertEqual(self.selection('deepseek').reasoning_effort, 'high')
        self.assertEqual(self.selection('zimaocloud').reasoning_effort, 'default')

    def test_supported_efforts_round_trip_through_subprocess_snapshot(self):
        for provider in ('deepseek', 'zimaocloud'):
            for effort in ('none', 'low', 'high', 'max'):
                with self.subTest(provider=provider, effort=effort):
                    selected = self.selection(provider, effort)
                    with patch.dict(os.environ, {SNAPSHOT_ENV: selected.subprocess_value()}):
                        self.assertEqual(selection_from_environment(), selected)

    def test_invalid_effort_is_rejected_before_request(self):
        for provider in ('deepseek', 'zimaocloud'):
            for effort in ('MAX', '', 'unsupported', 1, ['max']):
                with self.subTest(provider=provider, effort=effort):
                    with self.assertRaises(ModelSettingsError):
                        self.selection(provider, effort)

    def test_save_reload_and_candidate_keep_effort_key_and_other_profiles(self):
        for provider in ('deepseek', 'zimaocloud'):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                store = ModelSettingsStore(Path(directory))
                other = api_selection({'provider': 'ppio', 'api_key': 'sk_other-fixture'})
                store.save(other, expected_revision=0)
                before = store._read()['profiles']['ppio']
                original = self.selection(provider)
                store.save(original, expected_revision=1)
                frozen = store.snapshot()
                maximum = store.candidate({'reasoning_effort': 'max'})
                store.save(maximum, expected_revision=2)
                restarted = ModelSettingsStore(Path(directory))
                self.assertEqual(restarted.snapshot(), maximum)
                self.assertEqual(restarted.candidate({}), maximum)
                self.assertEqual(restarted.snapshot().api_key, original.api_key)
                self.assertEqual(restarted._read()['profiles']['ppio'], before)
                self.assertEqual(frozen, original)
                with patch.dict(os.environ, {SNAPSHOT_ENV: frozen.subprocess_value()}):
                    self.assertEqual(selection_from_environment(), frozen)
                public = restarted.public()
                self.assertEqual(public['reasoning_effort'], 'max')
                self.assertEqual(public['profiles'][provider]['reasoning_effort'], 'max')
                self.assertNotIn(original.api_key, json.dumps(public))

    def test_maximum_effort_reaches_streamed_http_request(self):
        for provider in ('deepseek', 'zimaocloud'):
            with self.subTest(provider=provider):
                selected = self.selection(provider, 'max')

                def remote(request):
                    payload = json.loads(request.content)
                    self.assertEqual(str(request.url), selected.base_url + '/chat/completions')
                    self.assertEqual(payload['model'], selected.model)
                    self.assertEqual(payload['reasoning_effort'], 'max')
                    self.assertEqual(request.headers['Authorization'], 'Bearer ' + selected.api_key)
                    if provider == 'deepseek':
                        self.assertEqual(payload['thinking'], {'type': 'enabled'})
                    events = [{'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}]
                    return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
                                          + 'data: [DONE]\n\n')

                answer, _ = complete_json(selected, [{'type': 'text', 'text': 'Synthetic input only.'}],
                                         system='Return JSON', transport=httpx.MockTransport(remote))
                self.assertEqual(json.loads(answer), {'ok': True})

    def test_reading_identity_keeps_maximum_selection_across_frozen_environment(self):
        from audit_core.pdf_materials import PdfEvidenceProvider
        selected = self.selection('zimaocloud', 'max')
        with patch.dict(os.environ, {SNAPSHOT_ENV: selected.subprocess_value()}):
            restored = selection_from_environment()
            self.assertEqual(restored.reasoning_effort, 'max')
            provider = PdfEvidenceProvider(None, None, connection=restored)
            self.assertEqual(provider._reading_identity()[0]['effort'], 'max')
        self.assertEqual(selected.reasoning_effort, 'max')

    def test_formal_queue_passes_maximum_to_runner_and_its_frozen_environment(self):
        from audit_core.oss_intake import run_formal_audit_subprocess
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = self.selection('zimaocloud', 'max')
            ModelSettingsStore(root).save(selected, expected_revision=0)

            def run(command, **kwargs):
                self.assertEqual(command[command.index('--reasoning-effort') + 1], 'max')
                self.assertEqual(json.loads(kwargs['env'][SNAPSHOT_ENV]),
                                 json.loads(selected.subprocess_value()))
                self.assertNotIn(selected.api_key, ' '.join(command))
                output = Path(command[command.index('--result-json') + 1])
                output.write_text(json.dumps({'status': 'completed', 'workspace_id': 'fixture',
                                              'error_count': 0}), encoding='utf-8')
                return subprocess.CompletedProcess(command, 0, '')

            with patch('audit_core.oss_intake.subprocess.run', side_effect=run):
                result = run_formal_audit_subprocess(run_id='20261116-max-fixture', producer_model='ppio',
                    input_dir=root, worktrees_root=root, scenario=None, workbench_url=None,
                    biz_type='人员激励')
            self.assertEqual(result['status'], 'completed')


if __name__ == '__main__':
    unittest.main()
