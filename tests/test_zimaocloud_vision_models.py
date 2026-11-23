"""紫猫云豆包/千问兼容：仅合成图、HTTP替身及隔离工作台，不使用真实Key。"""
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

import httpx

from audit_core.model_availability import VALID_SECONDS, fetch_candidate_models, refresh_available_models
from audit_core.model_catalog import (ZIMAO_DOUBAO_MODEL, ZIMAO_QWEN_MODEL,
                                      model_choices, model_connections)
from audit_core.model_settings import (DEFAULT_ZIMAO_MODEL, DEFAULT_ZIMAO_BASE_URL,
    ModelSettingsError, ModelSettingsStore, SNAPSHOT_ENV, api_selection, selection_from_environment)
from audit_core.qwen_model import QwenRequestError, complete_json, probe_qwen
from tests import test_model_switching as model_tests

MODELS = (ZIMAO_DOUBAO_MODEL, ZIMAO_QWEN_MODEL)


def selection(model, effort='max'):
    return api_selection({'provider': 'zimaocloud', 'model': model,
                          'reasoning_effort': effort, 'api_key': 'synthetic-zimao-vision-key'})


def stream(events=None):
    if events is None:
        events = [{'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}]
    return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
                          + 'data: [DONE]\n\n')


class ZimaoVisionModelsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = ModelSettingsStore(self.root)

    def test_catalogue_maps_exact_account_ids_without_changing_other_platforms(self):
        self.assertEqual(model_connections(ZIMAO_DOUBAO_MODEL), {'zimaocloud': ZIMAO_DOUBAO_MODEL})
        self.assertEqual(model_connections(ZIMAO_QWEN_MODEL), {
            'qwen': ZIMAO_QWEN_MODEL, 'ppio': 'qwen/' + ZIMAO_QWEN_MODEL,
            'zimaocloud': ZIMAO_QWEN_MODEL})
        choices = model_choices()
        self.assertEqual(len(choices), len({row['id'] for row in choices}))
        for model in MODELS:
            self.assertEqual(sum(row['id'] == model for row in choices), 1)
            self.assertEqual(selection(model).base_url, DEFAULT_ZIMAO_BASE_URL)
        self.assertEqual(selection('qwen3.7-max').model,'qwen3.7-max')
        for unsupported in ('doubao-seed-2-1-lite-260915',
                            'qwen/qwen3.8-max', 'ep-private', '../secret'):
            with self.subTest(model=unsupported), self.assertRaises(ModelSettingsError):
                selection(unsupported)

    def test_saved_connection_and_max_effort_are_inherited_without_switching(self):
        current = selection(DEFAULT_ZIMAO_MODEL)
        self.store.save(current, expected_revision=0)
        original = self.store.path.read_bytes()
        for model in MODELS:
            candidate = self.store.candidate({'provider': 'zimaocloud', 'model': model})
            self.assertEqual(candidate, selection(model))
            self.assertEqual(self.store.candidate({'model': model}), candidate)
            with patch.dict(os.environ, {SNAPSHOT_ENV: candidate.subprocess_value()}):
                self.assertEqual(selection_from_environment(), candidate)
            self.assertEqual(self.store.path.read_bytes(), original)
        self.assertEqual(self.store.snapshot(), current)

    def test_other_platform_keys_cannot_silently_supply_the_zimao_connection(self):
        ppio = api_selection({'provider': 'ppio', 'api_key': 'sk_synthetic-ppio-key'})
        self.store.save(ppio, expected_revision=0)
        with patch.dict(os.environ, {'ZIMAO_API_KEY': '', 'ZIMAO_API_KEY_FILE': ''}):
            for model in MODELS:
                with self.subTest(model=model), self.assertRaises(ModelSettingsError):
                    self.store.candidate({'provider': 'zimaocloud', 'model': model})
        self.assertEqual(self.store.snapshot(), ppio)

    def test_directory_is_only_an_intersection_not_availability_evidence(self):
        current = selection(DEFAULT_ZIMAO_MODEL)
        self.store.save(current, expected_revision=0)
        before = self.store.path.read_bytes()
        def remote(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(str(request.url), DEFAULT_ZIMAO_BASE_URL + '/models')
            return httpx.Response(200, json={'data': [
                {'id': DEFAULT_ZIMAO_MODEL}, {'id': ZIMAO_DOUBAO_MODEL}, {'id': ZIMAO_QWEN_MODEL},
                {'id': 'doubao-seed-2-1-lite-260915'}, {'id': 'qwen3.7-max'}]})
        candidates = fetch_candidate_models(current, transport=httpx.MockTransport(remote))
        self.assertEqual({item.model for item in candidates}, {DEFAULT_ZIMAO_MODEL, *MODELS})
        self.assertEqual({item.api_key for item in candidates}, {current.api_key})
        self.assertEqual(self.store.public()['model_choices'], [])
        self.assertEqual(set(self.store.public()['profiles']['zimaocloud']['compatible_model_ids']),
                         {DEFAULT_ZIMAO_MODEL, *MODELS})
        self.assertEqual(self.store.path.read_bytes(), before)
        for candidate in candidates:
            with self.assertRaises(ModelSettingsError):
                self.store.availability.require(candidate)

    def test_text_only_or_disabled_account_models_are_not_probed(self):
        response = httpx.MockTransport(lambda request: httpx.Response(200, json={'data': [
            {'id': ZIMAO_DOUBAO_MODEL, 'input_modalities': ['text']},
            {'id': ZIMAO_QWEN_MODEL, 'status': 0}]}))
        self.assertEqual(fetch_candidate_models(selection(DEFAULT_ZIMAO_MODEL), transport=response), [])

    def test_refresh_keeps_only_passed_models_and_never_saves_the_choice(self):
        current = selection(DEFAULT_ZIMAO_MODEL)
        self.store.save(current, expected_revision=0)
        before = self.store.path.read_bytes()
        def probe(candidate):
            if candidate.model == ZIMAO_QWEN_MODEL:
                raise QwenRequestError('隔离的图片识别失败', 'invalid_output')
            return {}
        with patch('audit_core.model_availability.fetch_candidate_models',
                   return_value=[selection(model) for model in MODELS]), \
             patch('audit_core.qwen_model.probe_qwen', side_effect=probe):
            result = refresh_available_models(current, self.store.availability)
        self.assertEqual((result['available'], result['unavailable']), (1, 1))
        self.assertEqual([row['id'] for row in self.store.public()['model_choices']], [ZIMAO_DOUBAO_MODEL])
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_new_model_proofs_stay_bound_to_model_key_endpoint_and_expiry(self):
        candidate = selection(ZIMAO_DOUBAO_MODEL)
        self.store.availability.record(candidate, passed=True)
        for changed in (selection(ZIMAO_QWEN_MODEL), replace(candidate, api_key='other-fixture'),
                        replace(candidate, provider='doubao'),
                        replace(candidate, base_url='https://other.invalid/v1')):
            with self.subTest(changed=repr(changed)), self.assertRaises(ModelSettingsError):
                self.store.availability.require(changed)
        with patch('audit_core.model_availability.time.time', return_value=time.time() + VALID_SECONDS + 1):
            with self.assertRaises(ModelSettingsError):
                self.store.availability.require(candidate)

    def test_each_effort_sends_only_its_own_model_protocol(self):
        for model in MODELS:
            for effort in ('default', 'none', 'low', 'high', 'max'):
                with self.subTest(model=model, effort=effort):
                    current = selection(model, effort)
                    def remote(request):
                        payload = json.loads(request.content)
                        self.assertEqual(str(request.url), DEFAULT_ZIMAO_BASE_URL + '/chat/completions')
                        self.assertEqual(request.headers['Authorization'], 'Bearer ' + current.api_key)
                        self.assertEqual(payload['model'], model)
                        self.assertEqual(payload['response_format'], {'type': 'json_object'})
                        self.assertEqual(payload['stream_options'], {'include_usage': True})
                        self.assertTrue(payload['stream'])
                        self.assertEqual(payload['messages'][1]['content'][0]['type'], 'image_url')
                        self.assertNotIn('max_tokens', payload)
                        self.assertNotIn('max_completion_tokens', payload)
                        if model == ZIMAO_DOUBAO_MODEL:
                            self.assertNotIn('enable_thinking', payload)
                            self.assertNotIn('preserve_thinking', payload)
                            if effort != 'default':
                                self.assertEqual(payload['thinking']['type'], 'disabled' if effort == 'none' else 'enabled')
                            else:
                                self.assertNotIn('thinking', payload)
                            expected = 'high' if effort == 'max' else effort
                        else:
                            self.assertNotIn('thinking', payload)
                            self.assertFalse(payload['preserve_thinking'])
                            if effort != 'default':
                                self.assertEqual(payload['enable_thinking'], effort != 'none')
                            else:
                                self.assertNotIn('enable_thinking', payload)
                            expected = 'xhigh' if effort in {'high', 'max'} else effort
                        if effort in {'none', 'default'}:
                            self.assertNotIn('reasoning_effort', payload)
                        else:
                            self.assertEqual(payload['reasoning_effort'], expected)
                        return stream()
                    answer, metrics = complete_json(current,
                        [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,synthetic'}}],
                        system='仅返回JSON', transport=httpx.MockTransport(remote))
                    self.assertEqual(json.loads(answer), {'ok': True})
                    self.assertEqual(metrics['configured_reasoning_effort'], effort)
                    native = effort
                    if model == ZIMAO_DOUBAO_MODEL and effort == 'max':
                        native = 'high'
                    if model == ZIMAO_QWEN_MODEL and effort in {'high', 'max'}:
                        native = 'xhigh'
                    self.assertEqual(metrics['request_reasoning_effort'], native)
                    from audit_core.model_metrics import _safe_attempt
                    safe = _safe_attempt(metrics)
                    self.assertEqual(safe['configured_reasoning_effort'], effort)
                    self.assertEqual(safe['request_reasoning_effort'], native)

    def test_visible_multi_turn_conversation_continues_without_storing_private_reasoning(self):
        from audit_core.run_conversation import RunConversation, _hash
        for model in MODELS:
            with self.subTest(model=model):
                current = selection(model)
                conversation = RunConversation({'provider': 'zimaocloud', 'model': model,
                    'connection_scope': _hash({'endpoint': current.base_url, 'key': current.api_key})},
                    self.root / model)
                counts = []
                def remote(request):
                    payload = json.loads(request.content)
                    counts.append(len(payload['messages']))
                    self.assertNotIn('private-thought', json.dumps(payload))
                    if model == ZIMAO_QWEN_MODEL:
                        self.assertFalse(payload['preserve_thinking'])
                    return stream([{'choices': [{'delta': {'reasoning_content': 'private-thought'}}]},
                        {'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}])
                with conversation.activate('隔离连续会话测试'):
                    for number in range(2):
                        complete_json(current, [{'type': 'text', 'text': f'合成资料阶段{number}'}],
                                      system='返回JSON', transport=httpx.MockTransport(remote))
                self.assertEqual(counts, [2, 4])
                self.assertEqual(len(conversation.turns), 2)
                self.assertNotIn('private-thought', json.dumps(conversation.turns))

    def test_streaming_json_and_usage_exclude_private_reasoning_and_encrypted_state(self):
        events = [
            {'choices': [{'delta': {'reasoning_content': 'private-thought', 'encrypted_content': 'private-encrypted'}}]},
            {'choices': [{'delta': {'content': '{"ok":'}, 'index': 0}]},
            {'choices': [{'delta': {'content': 'false}'}, 'index': 1}]},
            {'choices': [{'delta': {'content': 'true}'}, 'finish_reason': 'stop'}]},
            {'choices': [], 'usage': {'prompt_tokens': 300, 'completion_tokens': 125,
                'prompt_tokens_details': {'cached_tokens': 50},
                'completion_tokens_details': {'reasoning_tokens': 100}}}]
        for model in MODELS:
            with self.subTest(model=model):
                answer, metrics = complete_json(selection(model), [], system='JSON',
                    transport=httpx.MockTransport(lambda request: stream(events)))
                self.assertEqual(json.loads(answer), {'ok': True})
                self.assertEqual([metrics[k] for k in ('input_tokens', 'output_tokens',
                    'cached_input_tokens', 'reasoning_tokens')], [300, 125, 50, 100])
                self.assertNotIn('private-', answer + json.dumps(metrics))

    def test_truncated_output_and_wrong_image_results_do_not_pass(self):
        for model in MODELS:
            with self.subTest(model=model):
                with self.assertRaises(QwenRequestError) as error:
                    complete_json(selection(model), [], system='JSON', transport=httpx.MockTransport(
                        lambda request: stream([{'choices': [{'delta': {'content': '{}'}, 'finish_reason': 'length'}]}])))
                self.assertEqual(error.exception.error_code, 'output_limit')
                with patch('audit_core.qwen_model.complete_json', return_value=(
                        '{"number":1234,"color":"blue"}', {'elapsed_seconds': 0})):
                    with self.assertRaises(QwenRequestError):
                        probe_qwen(selection(model))

    def test_probe_preserves_effort_without_the_old_small_token_budget(self):
        for model in MODELS:
            with self.subTest(model=model), patch('audit_core.qwen_model.complete_json', return_value=(
                    '{"number":5274,"color":"blue"}', {'elapsed_seconds': 0})) as complete:
                candidate = selection(model)
                probe_qwen(candidate)
                self.assertEqual(complete.call_args.args[0], candidate)
                self.assertIsNone(complete.call_args.kwargs['max_tokens'])
                self.assertEqual(complete.call_args.kwargs['timeout_seconds'], 45)
                self.assertTrue(complete.call_args.args[1][1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_formal_queue_freezes_the_selected_model_without_key_in_command(self):
        from audit_core.oss_intake import run_formal_audit_subprocess
        for model in MODELS:
            with self.subTest(model=model):
                store = ModelSettingsStore(self.root / model)
                candidate = selection(model)
                store.save(candidate, expected_revision=0)
                def run(command, **kwargs):
                    self.assertEqual(command[command.index('--producer-model') + 1], 'zimaocloud')
                    self.assertEqual(command[command.index('--model') + 1], model)
                    self.assertEqual(command[command.index('--reasoning-effort') + 1], 'max')
                    self.assertEqual(json.loads(kwargs['env'][SNAPSHOT_ENV]), json.loads(candidate.subprocess_value()))
                    self.assertNotIn(candidate.api_key, ' '.join(command))
                    output = Path(command[command.index('--result-json') + 1])
                    output.write_text(json.dumps({'status': 'completed', 'workspace_id': 'fixture', 'error_count': 0}), encoding='utf-8')
                    return subprocess.CompletedProcess(command, 0, '')
                with patch('audit_core.oss_intake.subprocess.run', side_effect=run):
                    result = run_formal_audit_subprocess(run_id='20261117-vision-fixture', producer_model='ppio',
                        input_dir=self.root, worktrees_root=store.path.parent.parent, scenario=None,
                        workbench_url=None, biz_type='人员激励')
                self.assertEqual(result['status'], 'completed')


class ZimaoVisionSettingsAPITests(unittest.TestCase):
    def test_refresh_test_and_activation_are_independent_for_both_new_models(self):
        fixture = model_tests.ModelSettingsAPITests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        store = fixture.server.model_settings
        current = selection(DEFAULT_ZIMAO_MODEL)
        store.save(current, expected_revision=0)
        frozen = store.snapshot()
        original = store.path.read_bytes()
        for model in MODELS:
            with self.subTest(unverified=model), self.assertRaises(urllib.error.HTTPError) as error:
                fixture.request({'action': 'activate', 'revision': 1, 'provider': 'zimaocloud', 'model': model})
            error.exception.close()
            self.assertEqual(store.path.read_bytes(), original)
        with patch('audit_core.model_availability.fetch_candidate_models', return_value=[selection(m) for m in MODELS]), \
             patch('audit_core.qwen_model.probe_qwen', return_value={}) as probe:
            result = fixture.request({'action': 'refresh', 'revision': 1, 'provider': 'zimaocloud'})
        self.assertEqual(probe.call_count, 2)
        self.assertEqual({row['id'] for row in result['settings']['model_choices']}, set(MODELS))
        self.assertEqual(store.path.read_bytes(), original)
        for revision, model in enumerate(MODELS, 1):
            before = store.path.read_bytes()
            with patch('audit_core.qwen_model.probe_qwen', return_value={}) as probe:
                fixture.request({'action': 'test', 'revision': revision, 'provider': 'zimaocloud', 'model': model})
            probe.assert_called_once_with(selection(model))
            self.assertEqual(store.path.read_bytes(), before)
            with patch('audit_core.qwen_model.probe_qwen', side_effect=AssertionError('切换不得调用模型')):
                result = fixture.request({'action': 'activate', 'revision': revision,
                                          'provider': 'zimaocloud', 'model': model})
            self.assertEqual(store.snapshot(), selection(model))
            self.assertNotIn(current.api_key, json.dumps(result))
            self.assertEqual(frozen, current)
        self.assertEqual(store.public()['revision'], 3)


if __name__ == '__main__':
    unittest.main()
