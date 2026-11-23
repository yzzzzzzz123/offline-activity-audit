"""Doubao (Volcengine Ark) uses the pinned Ark gateway and its own probe list."""
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from audit_core.model_availability import fetch_candidate_models, refresh_available_models
from audit_core.model_catalog import DOUBAO_MODELS, model_choices, model_connections, public_model_id
from audit_core.model_settings import (
    DEFAULT_DOUBAO_BASE_URL, DEFAULT_DOUBAO_MODEL, ModelSettingsError,
    ModelSettingsStore, api_selection,
)
from audit_core.qwen_model import QwenRequestError, complete_json, probe_qwen

ARK_KEY = "11111111-2222-3333-4444-555555555555"


class DoubaoProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selection = api_selection({'provider': 'doubao', 'api_key': ARK_KEY})
        self.store = ModelSettingsStore(self.root)

    def test_default_connection_and_catalogue_are_doubao_only(self):
        self.assertEqual(self.selection.model, DEFAULT_DOUBAO_MODEL)
        self.assertEqual(self.selection.base_url, DEFAULT_DOUBAO_BASE_URL)
        self.assertEqual(self.selection.reasoning_effort, 'default')
        self.assertEqual(model_connections(DEFAULT_DOUBAO_MODEL), {'doubao': DEFAULT_DOUBAO_MODEL})
        self.assertEqual(public_model_id(DEFAULT_DOUBAO_MODEL), DEFAULT_DOUBAO_MODEL)
        for name, label, group in DOUBAO_MODELS:
            self.assertIn({'id': name, 'label': label, 'group': group, 'recommended': False},
                          model_choices())
            self.assertEqual(set(model_connections(name)), {'doubao'})

    def test_only_the_ark_gateway_accepts_credentials(self):
        self.assertEqual(api_selection({'provider': 'doubao', 'api_key': ARK_KEY,
                                        'base_url': DEFAULT_DOUBAO_BASE_URL + '/'}).base_url,
                         DEFAULT_DOUBAO_BASE_URL)
        for url in ('http://ark.cn-beijing.volces.com/api/v3',
                    'https://ark.cn-beijing.volces.com.evil.test/api/v3',
                    'https://user@ark.cn-beijing.volces.com/api/v3',
                    'https://ark.cn-beijing.volces.com/api/v3?key=x',
                    'https://ark.cn-beijing.volces.com/api/v3#x',
                    'https://ark.cn-beijing.volces.com:444/api/v3',
                    'https://ark.cn-beijing.volces.com/api/v2',
                    'https://api.deepseek.com'):
            with self.subTest(url=url), self.assertRaises(ModelSettingsError):
                api_selection({'provider': 'doubao', 'api_key': ARK_KEY, 'base_url': url})

    def test_only_ark_model_ids_and_ark_keys_are_accepted(self):
        for model in (DEFAULT_DOUBAO_MODEL, 'doubao-seed-1-6-flash-250715',
                      'doubao-pro-32k-241215', 'ep-20260101000000-abcde'):
            with self.subTest(model=model):
                self.assertEqual(api_selection({'provider': 'doubao', 'model': model,
                                                'api_key': ARK_KEY}).model, model)
        for model in ('qwen3.8-max', 'deepseek-flash', 'doubao', 'doubao X', 'model/../x'):
            with self.subTest(model=model), self.assertRaises(ModelSettingsError):
                api_selection({'provider': 'doubao', 'model': model, 'api_key': ARK_KEY})
        with self.assertRaises(ModelSettingsError):
            api_selection({'provider': 'doubao', 'api_key': 'sk_saved-fixture'})
        with self.assertRaises(ModelSettingsError):
            api_selection({'provider': 'doubao', 'api_key': ''})

    def test_candidate_list_is_the_catalogue_and_never_calls_a_directory(self):
        def refuse(request):
            raise AssertionError(str(request.url))
        candidates = fetch_candidate_models(self.selection, transport=httpx.MockTransport(refuse))
        self.assertEqual([item.model for item in candidates],
                         [name for name, _, _ in DOUBAO_MODELS])
        self.assertEqual({item.provider for item in candidates}, {'doubao'})
        self.assertEqual({item.api_key for item in candidates}, {ARK_KEY})

    def test_refresh_probes_every_catalogue_id_and_records_only_passes(self):
        candidates = fetch_candidate_models(self.selection)
        def probe(item):
            if item.model == 'doubao-seed-1-6-250615':
                raise QwenRequestError('synthetic model not enabled', 'configuration')
            return {}
        with patch('audit_core.qwen_model.probe_qwen', side_effect=probe):
            checked = refresh_available_models(self.selection, self.store.availability)
        self.assertEqual((checked['checked'], checked['available'], checked['unavailable']),
                         (len(DOUBAO_MODELS), len(DOUBAO_MODELS) - 1, 1))
        available = {row['id'] for row in self.store.availability.choices(self.selection)}
        self.assertEqual(available, {name for name, _, _ in DOUBAO_MODELS
                                     if name != 'doubao-seed-1-6-250615'})
        for item in candidates:
            with self.subTest(model=item.model):
                self.assertNotIn(ARK_KEY, json.dumps(self.store.availability._read()))

    def test_activation_requires_a_passed_probe_and_keeps_other_profiles(self):
        with self.assertRaises(ModelSettingsError):
            self.store.availability.require(self.selection)
        self.store.availability.record(self.selection, passed=True)
        self.store.availability.require(self.selection)
        self.store.save(self.selection, expected_revision=0)
        settings = self.store.public()
        self.assertEqual(settings['provider'], 'doubao')
        self.assertEqual(settings['profiles']['doubao']['label'], '豆包（火山方舟）')
        self.assertTrue(settings['profiles']['doubao']['has_api_key'])
        self.assertNotIn(ARK_KEY, json.dumps(settings))
        self.assertEqual(self.store.snapshot(), self.selection)
        with self.assertRaises(ModelSettingsError):
            self.store.availability.require(replace(self.selection, api_key='other-key'))

    def test_a_verified_inference_endpoint_is_selectable_and_activatable(self):
        endpoint = api_selection({'provider': 'doubao', 'api_key': ARK_KEY,
                                  'model': 'ep-20260101000000-abcde'})
        with self.assertRaises(ModelSettingsError):
            self.store.availability.require(endpoint)
        self.store.availability.record(endpoint, passed=True)
        choices = self.store.availability.choices(endpoint)
        self.assertEqual([row['id'] for row in choices], ['ep-20260101000000-abcde'])
        self.store.availability.require(endpoint)
        self.store.save(endpoint, expected_revision=0)
        self.assertEqual(self.store.snapshot(), endpoint)
        # The verified endpoint never leaks into another key's identity.
        self.assertEqual(self.store.availability.choices(
            replace(endpoint, api_key='other-key')), [])

    def test_streaming_vision_json_disables_seed_thinking_and_hides_reasoning(self):
        def remote(request):
            self.assertEqual(str(request.url), DEFAULT_DOUBAO_BASE_URL + '/chat/completions')
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + ARK_KEY)
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], DEFAULT_DOUBAO_MODEL)
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            self.assertEqual(payload['messages'][1]['content'][0]['type'], 'image_url')
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            for key in ('enable_thinking', 'reasoning_effort', 'modalities'):
                self.assertNotIn(key, payload)
            events = [
                {'choices': [{'delta': {'reasoning_content': 'private reasoning'}}]},
                {'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]},
                {'choices': [], 'usage': {'prompt_tokens': 300, 'completion_tokens': 40,
                    'prompt_tokens_details': {'cached_tokens': 0},
                    'completion_tokens_details': {'reasoning_tokens': 0}}}]
            return httpx.Response(200, text=''.join('data: ' + json.dumps(e) + '\n\n' for e in events)
                                  + 'data: [DONE]\n\n')
        answer, metrics = complete_json(self.selection,
            [{'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,fixture'}}],
            system='Return JSON', transport=httpx.MockTransport(remote))
        self.assertEqual(json.loads(answer), {'ok': True})
        self.assertEqual((metrics['input_tokens'], metrics['output_tokens']), (300, 40))
        self.assertNotIn('private reasoning', answer + json.dumps(metrics))

    def test_legacy_families_are_not_sent_the_seed_thinking_switch(self):
        legacy = api_selection({'provider': 'doubao', 'model': 'doubao-1-5-vision-pro-32k-250115',
                                'api_key': ARK_KEY})
        def remote(request):
            payload = json.loads(request.content)
            self.assertNotIn('thinking', payload)
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"{}"},'
                                            '"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        answer, _ = complete_json(legacy, [{'type': 'text', 'text': 'x'}], system='Return JSON',
                                  transport=httpx.MockTransport(remote))
        self.assertEqual(json.loads(answer), {})

    def test_probe_keeps_a_sufficient_output_budget_and_checks_image_content(self):
        with patch('audit_core.qwen_model.complete_json', return_value=(
                '{"number":5274,"color":"blue"}', {'elapsed_seconds': 0})) as complete:
            probe_qwen(self.selection)
        self.assertEqual(complete.call_args.kwargs['max_tokens'], 2048)
        self.assertTrue(complete.call_args.args[1][1]['image_url']['url'].startswith('data:image/png;base64,'))

    def test_auth_failures_keep_keys_and_raw_diagnostics_private(self):
        for status, code in ((401, 'auth'), (403, 'auth'), (402, 'quota'),
                             (404, 'configuration'), (429, 'rate_limit')):
            with self.subTest(status=status), self.assertRaises(QwenRequestError) as error:
                complete_json(self.selection, [], system='JSON', transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text=ARK_KEY + ' private diagnostics')))
            self.assertEqual(error.exception.error_code, code)
            self.assertIn('豆包', str(error.exception))
            self.assertNotIn(ARK_KEY, str(error.exception))
            self.assertNotIn('diagnostics', str(error.exception))


if __name__ == '__main__':
    unittest.main()
