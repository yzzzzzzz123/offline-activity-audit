from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from audit_core.model_catalog import model_choices, model_connections, public_model_id
from audit_core.model_settings import ModelSettingsStore, ModelSettingsError, api_selection
from audit_core.qwen_model import QwenRequestError
from tests import test_model_switching as model_tests


class UnifiedModelSettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = ModelSettingsStore(Path(temporary.name))

    def test_catalog_has_many_unique_models_without_service_labels(self):
        choices = model_choices()
        self.assertGreater(len(choices), 0)
        self.assertEqual(self.store.public()['model_choices'], [])
        self.assertEqual(len({row["id"] for row in choices}), len(choices))
        self.assertTrue({"Kimi", "GLM", "MiniMax", "DeepSeek"} <= {row["group"] for row in choices})
        public = json.dumps(choices, ensure_ascii=False)
        for name in ("PPIO", "派欧云", "百炼", "服务平台", "base_url", "api_key"):
            self.assertNotIn(name, public)
        for row in choices:
            self.assertTrue(model_connections(row["id"]))

    def test_key_and_model_automatically_select_connection_without_cross_reusing_keys(self):
        self.store.save(api_selection({"provider": "ppio", "api_key": "sk_saved-fixture"}), expected_revision=0)
        selected = self.store.candidate({"model": "qwen3.8-flash", "api_key": ""})
        self.assertEqual((selected.provider, selected.model, selected.api_key),
                         ("ppio", "qwen/qwen3.8-flash", "sk_saved-fixture"))
        selected = self.store.candidate({"model": "qwen3-vl-plus", "api_key": "separate-key-fixture"})
        self.assertEqual((selected.provider, selected.model, selected.api_key),
                         ("qwen", "qwen3-vl-plus", "separate-key-fixture"))
        with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}), self.assertRaises(ModelSettingsError):
            self.store.candidate({"model": "qwen3-vl-plus", "api_key": ""})
        self.assertEqual(self.store.snapshot().api_key, "sk_saved-fixture")

    def test_every_supported_model_mapping_passes_existing_connection_validation(self):
        for option in model_choices():
            for provider, actual in model_connections(option["id"]).items():
                with self.subTest(model=option["id"], provider=provider):
                    selected = self.store.candidate({"provider": provider, "model": option["id"],
                        "api_key": "sk_synthetic-fixture" if provider == "ppio" else "synthetic-key-fixture"})
                    self.assertEqual(selected.model, actual)
                    self.assertEqual(public_model_id(actual), option["id"])

    def test_mismatched_key_and_model_are_rejected_with_neutral_error(self):
        for payload in ({"model": "qwen3-vl-plus", "api_key": "sk_incompatible-fixture"},
                        {"model": "kimi-k3", "api_key": "incompatible-key-fixture"},
                        {"model": "../../private", "api_key": "sk_invalid-fixture"}):
            with self.subTest(payload=payload), self.assertRaises(ModelSettingsError) as error:
                self.store.candidate(payload)
            for name in ("PPIO", "百炼", "派欧云", payload["api_key"]):
                self.assertNotIn(name, str(error.exception))


class UnifiedModelSettingsAPITests(unittest.TestCase):
    def setUp(self):
        self.fixture = model_tests.ModelSettingsAPITests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_two_field_activation_and_other_model_testing_use_correct_internal_ids(self):
        self.fixture.server.model_settings.availability.record(self.fixture.server.model_settings.candidate({
            'model': 'qwen3.8-flash', 'api_key': 'sk_isolated-api-fixture'}), passed=True)
        result = self.fixture.request({"action": "activate", "revision": 0,
            "model": "qwen3.8-flash", "api_key": "sk_isolated-api-fixture"})
        self.assertEqual(result["settings"]["active_model_id"], "qwen3.8-flash")
        self.assertEqual([row['id'] for row in result['settings']['model_choices']], ['qwen3.8-flash'])
        before = self.fixture.server.model_settings.path.read_bytes()
        with patch("audit_core.qwen_model.probe_qwen", return_value={"model": "moonshotai/kimi-k3",
                "response": {"number": "5274", "color": "blue"}, "elapsed_seconds": 0}) as probe:
            result = self.fixture.request({"action": "test", "revision": 1, "model": "kimi-k3", "api_key": ""})
        self.assertEqual(probe.call_args.args[0].model, "moonshotai/kimi-k3")
        self.assertEqual(before, self.fixture.server.model_settings.path.read_bytes())
        self.assertNotIn("PPIO", result["message"])

    def test_model_test_errors_do_not_expose_service_or_diagnostic_text(self):
        with patch("audit_core.qwen_model.probe_qwen", side_effect=QwenRequestError("PPIO private diagnostics", "auth")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.fixture.request({"action": "test", "revision": 0,
                                      "model": "qwen3.8-max", "api_key": "sk_isolated-fixture"})
        body = json.load(error.exception)
        error.exception.close()
        self.assertEqual(error.exception.code, 502)
        self.assertIn("API Key", body["error"])
        for text in ("PPIO", "private", "百炼", "服务平台"):
            self.assertNotIn(text, body["error"])


if __name__ == "__main__":
    unittest.main()
