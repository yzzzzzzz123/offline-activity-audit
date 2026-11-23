from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

import httpx

from audit_core.model_availability import ModelAvailabilityStore, VALID_SECONDS, fetch_candidate_models, refresh_available_models
from audit_core.model_settings import ModelSettingsStore, ModelSettingsError, api_selection
from audit_core.qwen_model import QwenRequestError
from tests import test_model_switching as model_tests


class ModelAvailabilityTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = ModelSettingsStore(Path(directory.name))
        self.connection = api_selection({'provider': 'ppio', 'api_key': 'sk_availability_fixture_not_real'})

    def test_evidence_is_bound_to_key_provider_endpoint_and_expires_across_restart(self):
        self.store.availability.record(self.connection, passed=True)
        restarted = ModelAvailabilityStore(self.store.path.parent.parent)
        restarted.require(self.connection)
        for changed in (replace(self.connection, api_key='sk_different_fixture'),
                        replace(self.connection, provider='qwen'),
                        replace(self.connection, base_url='https://different.invalid/v1'),
                        replace(self.connection, model='qwen/qwen3.8-flash')):
            with self.subTest(changed=repr(changed)), self.assertRaises(ModelSettingsError):
                restarted.require(changed)
        with patch('audit_core.model_availability.time.time', return_value=time.time() + VALID_SECONDS + 1):
            self.assertEqual(restarted.choices(self.connection), [])
            with self.assertRaises(ModelSettingsError):
                restarted.require(self.connection)
        self.assertNotIn(self.connection.api_key, restarted.path.read_text(encoding='utf-8'))

    def test_public_catalog_is_verified_only_and_reading_never_calls_remote(self):
        self.store.save(self.connection, expected_revision=0)
        frozen = self.store.snapshot()
        with patch('httpx.Client', side_effect=AssertionError('Public reads must stay local')):
            self.assertEqual(self.store.public()['model_choices'], [])
            self.store.availability.record(self.connection, passed=True)
            self.assertEqual([r['id'] for r in self.store.public()['model_choices']], ['qwen3.8-max'])
            self.store.availability.record(self.connection, passed=False)
            self.assertEqual(self.store.public()['model_choices'], [])
        self.assertEqual(self.store.snapshot(), frozen)

    def test_catalog_intersection_excludes_text_only_unknown_and_retired_models(self):
        rows = [
            {'id': 'qwen/qwen3.8-max', 'input_modalities': ['text', 'image'], 'status': 1},
            {'id': 'qwen/qwen3.8-flash', 'input_modalities': ['text'], 'status': 1},
            {'id': 'qwen/qwen3.8-27b', 'input_modalities': ['image'], 'status': 0},
            {'id': 'qwen/qwen3.7-max', 'input_modalities': ['text'], 'status': 1},
            {'id': 'ai_infer_test_2', 'input_modalities': ['image'], 'status': 1},
            {'id': 'deepseek/deepseek-ocr-2', 'input_modalities': ['image'], 'status': 1},
        ]
        def remote(request):
            self.assertEqual(str(request.url), 'https://api.ppio.com/openai/v1/models')
            self.assertEqual(request.headers['Authorization'], 'Bearer ' + self.connection.api_key)
            return httpx.Response(200, json={'data': rows})
        candidates = fetch_candidate_models(self.connection, transport=httpx.MockTransport(remote))
        self.assertEqual([c.model for c in candidates], [self.connection.model])

    def test_refresh_retains_only_passing_results_without_changing_current_settings(self):
        self.store.save(self.connection, expected_revision=0)
        before = self.store.path.read_bytes()
        other = replace(self.connection, model='qwen/qwen3.8-flash')
        self.store.availability.record(other, passed=True)
        def probe(candidate):
            if candidate == other:
                raise QwenRequestError('private diagnostic must not be stored', 'auth')
            return {'response': {'number': '5274', 'color': 'blue'}}
        with patch('audit_core.model_availability.fetch_candidate_models', return_value=[self.connection, other]), \
             patch('audit_core.qwen_model.probe_qwen', side_effect=probe):
            summary = refresh_available_models(self.connection, self.store.availability)
        self.assertEqual((summary['available'], summary['unavailable']), (1, 1))
        self.assertEqual(summary['failure_counts'], {'auth': 1})
        self.assertEqual([r['id'] for r in self.store.public()['model_choices']], ['qwen3.8-max'])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertNotIn('private diagnostic', self.store.availability.path.read_text())
        with patch('audit_core.model_availability.fetch_candidate_models', side_effect=QwenRequestError('denied', 'auth')):
            with self.assertRaises(QwenRequestError):
                refresh_available_models(self.connection, self.store.availability)
        self.assertEqual(self.store.public()['model_choices'], [])

    def test_corrupted_registry_fails_closed(self):
        self.store.availability.record(self.connection, passed=True)
        self.store.availability.path.write_text('{invalid', encoding='utf-8')
        self.assertEqual(self.store.availability.choices(self.connection), [])


class ModelAvailabilityAPITests(unittest.TestCase):
    def setUp(self):
        self.fixture = model_tests.ModelSettingsAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.server.model_settings
        self.selection = api_selection({'provider': 'ppio', 'api_key': 'sk_api_availability_fixture'})
        self.store.save(self.selection, expected_revision=0)

    def test_unverified_arbitrary_text_only_or_wrong_key_cannot_activate(self):
        self.store.availability.record(self.selection, passed=True)
        before = self.store.path.read_bytes()
        for model, key in [('qwen3.8-flash', ''), ('qwen3.7-max', ''),
                           ('qwen3.8-max', 'sk_different_key'), ('qwen-made-up', '')]:
            with self.subTest(model=model, key=key), patch('audit_core.qwen_model.probe_qwen') as remote:
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.fixture.request({'action': 'activate', 'revision': 1, 'model': model, 'api_key': key})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
                remote.assert_not_called()
                self.assertEqual(before, self.store.path.read_bytes())

    def test_refresh_uses_draft_key_without_saving_or_switching_and_test_failure_hides_model(self):
        before = self.store.path.read_bytes()
        draft = replace(self.selection, api_key='sk_new_draft_key')
        with patch('audit_core.model_availability.fetch_candidate_models', return_value=[draft]), \
             patch('audit_core.qwen_model.probe_qwen', return_value={'response': {'number': '5274', 'color': 'blue'}}):
            result = self.fixture.request({'action': 'refresh', 'revision': 1, 'api_key': draft.api_key})
        self.assertEqual([r['id'] for r in result['settings']['model_choices']], ['qwen3.8-max'])
        self.assertEqual(result['check']['available'], 1)
        self.assertEqual(before, self.store.path.read_bytes())
        self.assertEqual(self.store.public()['model_choices'], [])
        with patch('audit_core.qwen_model.probe_qwen', side_effect=QwenRequestError('not allowed', 'auth')):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.fixture.request({'action': 'test', 'revision': 1, 'model': 'qwen3.8-max', 'api_key': draft.api_key})
            error.exception.close()
        self.assertEqual(self.store.public(candidate=draft)['model_choices'], [])
        self.assertEqual(before, self.store.path.read_bytes())


if __name__ == '__main__':
    unittest.main()
