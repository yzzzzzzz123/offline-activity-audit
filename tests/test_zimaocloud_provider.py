"""Zimao compatibility uses isolated credentials, HTTP substitutes and ZIPs."""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx

from audit_core.common import AuditError
from audit_core.model_availability import fetch_candidate_models, refresh_available_models
from audit_core.model_catalog import model_connections, public_model_id
from audit_core.model_settings import (
    DEFAULT_ZIMAO_BASE_URL, DEFAULT_ZIMAO_MODEL, ModelSettingsError,
    ModelSettingsStore, SNAPSHOT_ENV, api_selection, selection_from_environment,
)
from audit_core.qwen_model import QwenRequestError, complete_json, probe_qwen
from tests import test_model_switching as model_tests


class ZimaoProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selection = api_selection({'provider': 'zimaocloud', 'api_key': 'zimao-synthetic-secret'})
        self.store = ModelSettingsStore(self.root)

    def test_public_and_actual_model_ids_map_to_the_separate_gateway(self):
        self.assertEqual(self.selection.model, 'deepseek-v4-1-flash')
        self.assertEqual(self.selection.reasoning_effort, 'default')
        self.assertEqual(model_connections(DEFAULT_ZIMAO_MODEL), {'zimaocloud': DEFAULT_ZIMAO_MODEL})
        self.assertEqual(model_connections('deepseek-v4.1-flash')['zimaocloud'], DEFAULT_ZIMAO_MODEL)
        self.assertEqual(public_model_id(DEFAULT_ZIMAO_MODEL), 'deepseek-v4.1-flash')
        self.assertEqual(self.store.candidate({'model': DEFAULT_ZIMAO_MODEL,
                                              'api_key': self.selection.api_key}), self.selection)
        self.assertEqual(api_selection({'provider':'zimaocloud','model':'qwen3.7-max',
                                        'api_key':'fixture'}).model,'qwen3.7-max')
        for model in ('deepseek-v4-pro', 'deepseek-v4-pro-0813', 'deepseek-v4-flash-0731', 'DeepSeek-V3.2'):
            self.assertEqual(api_selection({'provider':'zimaocloud','model':model,'api_key':'fixture'}).model,model)
        for model in ('deepseek-chat', 'deepseek-unlisted-fixture'):
            with self.subTest(model=model), self.assertRaises(ModelSettingsError):
                api_selection({'provider': 'zimaocloud', 'model': model, 'api_key': 'fixture'})

    def test_only_the_verified_https_gateway_accepts_credentials(self):
        self.assertEqual(api_selection({'provider': 'zimaocloud', 'api_key': 'fixture',
                                        'base_url': DEFAULT_ZIMAO_BASE_URL + '/'}).base_url,
                         DEFAULT_ZIMAO_BASE_URL)
        for url in ('http://aiclub.zimaocloud.com/model/openApi/v1',
                    'https://aiclub.zimaocloud.com.evil.test/model/openApi/v1',
                    'https://user@aiclub.zimaocloud.com/model/openApi/v1',
                    DEFAULT_ZIMAO_BASE_URL + '?key=x', DEFAULT_ZIMAO_BASE_URL + '#x',
                    'https://aiclub.zimaocloud.com:444/model/openApi/v1',
                    'https://aiclub.zimaocloud.com/model-square', 'https://api.deepseek.com'):
            with self.subTest(url=url), self.assertRaises(ModelSettingsError):
                api_selection({'provider': 'zimaocloud', 'api_key': 'fixture', 'base_url': url})

    def test_saved_ppio_and_zimao_keys_remain_independent_and_snapshots_immutable(self):
        ppio = api_selection({'provider': 'ppio', 'api_key': 'sk_ppio-fixture'})
        self.store.save(ppio, expected_revision=0)
        frozen = self.store.snapshot()
        with patch.dict(os.environ, {'ZIMAO_API_KEY': '', 'ZIMAO_API_KEY_FILE': ''}):
            with self.assertRaises(ModelSettingsError):
                self.store.candidate({'provider': 'zimaocloud'})
        self.store.availability.record(self.selection, passed=True)
        self.store.save(self.selection, expected_revision=1)
        restarted = ModelSettingsStore(self.root)
        self.assertEqual(restarted.snapshot(), self.selection)
        self.assertEqual(restarted.candidate({'provider': 'ppio'}), ppio)
        self.assertEqual(frozen, ppio)
        self.assertEqual([x['id'] for x in restarted.public()['model_choices']], ['deepseek-v4.1-flash'])
        self.assertEqual(restarted.candidate({'model': 'deepseek-v4.1-flash',
                                              'api_key': self.selection.api_key}), self.selection)
        self.assertEqual(restarted.candidate({'api_key': self.selection.api_key}), self.selection)
        self.assertNotIn(self.selection.api_key, json.dumps(restarted.public()) + repr(self.selection))
        self.assertNotIn(ppio.api_key, json.dumps(restarted.public()))
        if os.name == 'nt':
            self.assertNotIn(self.selection.api_key, restarted.path.read_text())
        with patch.dict(os.environ, {SNAPSHOT_ENV: self.selection.subprocess_value()}):
            self.assertEqual(selection_from_environment(), self.selection)
        with self.assertRaises(ModelSettingsError):
            restarted.availability.require(replace(self.selection, api_key='different-fixture'))

    def test_directory_intersection_only_probes_supported_account_models(self):
        def remote(request):
            self.assertEqual(str(request.url), DEFAULT_ZIMAO_BASE_URL + '/models')
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + self.selection.api_key)
            return httpx.Response(200, json={'object': 'list', 'data': [
                {'id': DEFAULT_ZIMAO_MODEL}, {'id': 'deepseek-v4-pro'},
                {'id': 'qwen3.8-max'}, {'id': 'deepseek-flash'}]})
        candidates = fetch_candidate_models(self.selection, transport=httpx.MockTransport(remote))
        self.assertEqual({item.model for item in candidates}, {DEFAULT_ZIMAO_MODEL, 'qwen3.8-max'})
        with patch('audit_core.model_availability.fetch_candidate_models', return_value=candidates), \
             patch('audit_core.qwen_model.probe_qwen', return_value={}) as probe:
            checked = refresh_available_models(self.selection, self.store.availability)
        self.assertEqual((checked['checked'], checked['available']), (2, 2))
        self.assertCountEqual([call.args[0] for call in probe.call_args_list], candidates)
        self.assertFalse(self.store.path.exists())

    def test_streaming_vision_json_and_usage_do_not_expose_reasoning(self):
        def remote(request):
            self.assertEqual(str(request.url), DEFAULT_ZIMAO_BASE_URL + '/chat/completions')
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], DEFAULT_ZIMAO_MODEL)
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            self.assertEqual(payload['messages'][1]['content'][0]['type'], 'image_url')
            for key in ('enable_thinking', 'thinking', 'reasoning_effort'):
                self.assertNotIn(key, payload)
            events = [
                {'choices': [{'delta': {'reasoning_content': 'private reasoning'}}]},
                {'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]},
                {'choices': [], 'usage': {'prompt_tokens': 263, 'completion_tokens': 158,
                    'prompt_tokens_details': {'cached_tokens': 10},
                    'completion_tokens_details': {'reasoning_tokens': 147}}}]
            return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
                                  + 'data: [DONE]\n\n')
        answer, metrics = complete_json(self.selection,
            [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}],
            system='Return JSON', transport=httpx.MockTransport(remote))
        self.assertEqual(json.loads(answer), {'ok': True})
        self.assertEqual((metrics['input_tokens'], metrics['output_tokens']), (263, 158))
        self.assertEqual((metrics['cached_input_tokens'], metrics['reasoning_tokens']), (10, 147))
        self.assertNotIn('private reasoning', answer + json.dumps(metrics))

    def test_probe_keeps_a_sufficient_output_budget_and_checks_image_content(self):
        with patch('audit_core.qwen_model.complete_json', return_value=(
                '{"number":5274,"color":"blue"}', {'elapsed_seconds': 0})) as complete:
            probe_qwen(self.selection)
        self.assertEqual(complete.call_args.kwargs['max_tokens'], 2048)
        content = complete.call_args.args[1]
        self.assertTrue(content[1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_auth_and_redirect_failures_keep_keys_and_raw_diagnostics_private(self):
        for status, code in ((401, 'auth'), (403, 'auth'), (402, 'quota'), (302, 'model_error')):
            with self.subTest(status=status), self.assertRaises(QwenRequestError) as error:
                complete_json(self.selection, [], system='JSON', transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text=self.selection.api_key + ' private diagnostics',
                                                   headers={'Location': 'https://evil.test'})))
            self.assertEqual(error.exception.error_code, code)
            self.assertIn('紫猫云', str(error.exception))
            self.assertNotIn(self.selection.api_key, str(error.exception))
            self.assertNotIn('diagnostics', str(error.exception))

    def test_formal_queue_process_receives_actual_provider_and_frozen_connection(self):
        from audit_core.oss_intake import run_formal_audit_subprocess
        self.store.save(self.selection, expected_revision=0)
        def run(command, **kwargs):
            self.assertEqual(command[command.index('--producer-model') + 1], 'zimaocloud')
            self.assertEqual(command[command.index('--model') + 1], DEFAULT_ZIMAO_MODEL)
            self.assertEqual(json.loads(kwargs['env'][SNAPSHOT_ENV]),
                             json.loads(self.selection.subprocess_value()))
            self.assertNotIn(self.selection.api_key, ' '.join(command))
            result = Path(command[command.index('--result-json') + 1])
            result.write_text(json.dumps({'status': 'completed', 'workspace_id': 'fixture', 'error_count': 0}))
            return subprocess.CompletedProcess(command, 0, '')
        with patch('audit_core.oss_intake.subprocess.run', side_effect=run):
            result = run_formal_audit_subprocess(run_id='20261109-zimao-test', producer_model='ppio',
                input_dir=self.root, worktrees_root=self.root, scenario=None, workbench_url=None,
                biz_type='人员激励')
        self.assertEqual(result['status'], 'completed')

    def test_formal_runtime_uses_zimao_evidence_provider_and_records_actual_identity(self):
        from audit_core.workbench_runtime import run_persistent_audit
        source = self.root / 'fixture.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('fixture.txt', 'synthetic fixture')
        worktrees = self.root / 'worktrees'
        ModelSettingsStore(worktrees).save(self.selection, expected_revision=0)
        def audit(*args, **kwargs):
            self.assertEqual(kwargs['evidence_provider'].connection, self.selection)
            raise AuditError('synthetic execution interruption')
        with patch.dict(os.environ, {SNAPSHOT_ENV: ''}), \
             patch('audit_core.workbench_runtime.run_audit', side_effect=audit), \
             patch('audit_core.qwen_model.complete_json', return_value=(json.dumps({
                 'category':'unknown', 'diagnosis':'隔离测试主动中断执行',
                 'evidence':['正式业务执行由测试替身抛出中断异常'],
                 'operator_action':'检查本单执行记录', 'developer_fix':None}), {})) as diagnostic:
            with self.assertRaisesRegex(AuditError, 'synthetic execution interruption'):
                run_persistent_audit('20261109-zimao-test', producer_model='ppio', input_dir=source,
                                     worktrees_root=worktrees, biz_type='人员激励')
        diagnostic.assert_called_once()
        manifest = json.loads(next(worktrees.glob('*/manifest.json')).read_text(encoding='utf-8'))
        self.assertEqual((manifest['producer_model'], manifest['audit_model'], manifest['reasoning_effort']),
                         ('zimaocloud', DEFAULT_ZIMAO_MODEL, 'default'))


class ZimaoSettingsAPITests(unittest.TestCase):
    def test_refresh_and_activation_keep_original_ppio_profile_and_test_is_independent(self):
        fixture = model_tests.ModelSettingsAPITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = fixture.server.model_settings
        ppio = api_selection({'provider': 'ppio', 'api_key': 'sk_saved-fixture'})
        store.save(ppio, expected_revision=0)
        selection = api_selection({'provider': 'zimaocloud', 'api_key': 'zimao-api-fixture'})
        before = store.path.read_bytes()
        with patch('audit_core.model_availability.fetch_candidate_models', return_value=[selection]), \
             patch('audit_core.qwen_model.probe_qwen', return_value={'response': {'number': 5274, 'color': 'blue'}}):
            result = fixture.request({'action': 'refresh', 'revision': 1,
                                      'provider': 'zimaocloud', 'api_key': selection.api_key})
        self.assertEqual(before, store.path.read_bytes())
        self.assertEqual([x['id'] for x in result['settings']['model_choices']], ['deepseek-v4.1-flash'])
        with patch('audit_core.qwen_model.probe_qwen') as probe:
            result = fixture.request({'action': 'activate', 'revision': 1, 'provider': 'zimaocloud',
                                      'model': 'deepseek-v4.1-flash', 'api_key': selection.api_key})
            probe.assert_not_called()
        self.assertEqual(store.snapshot(), selection)
        self.assertEqual(store.candidate({'provider': 'ppio'}), ppio)
        self.assertEqual(result['settings']['provider'], 'zimaocloud')
        self.assertNotIn(selection.api_key, json.dumps(result))
        before = store.path.read_bytes()
        with patch('audit_core.qwen_model.probe_qwen', return_value={}) as probe:
            fixture.request({'action': 'test', 'revision': 2, 'provider': 'zimaocloud',
                             'model': 'deepseek-v4.1-flash', 'api_key': ''})
        probe.assert_called_once_with(selection)
        self.assertEqual(before, store.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
