"""Source reading requests must not replay the run's growing conversation."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from audit_core.model_settings import api_selection
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.prompts import bound_prompt
from audit_core.qwen_model import complete_json
from audit_core.run_conversation import RunConversation, current, isolated, isolation_requested


class ReadingRequestIsolationTests(unittest.TestCase):
    def test_isolated_context_hides_and_restores_the_run_conversation(self):
        session = RunConversation({'provider': 'zimaocloud', 'model': 'deepseek-v4-1-flash'})
        with session.activate('business-stage'):
            self.assertIs(current(), session)
            self.assertFalse(isolation_requested())
            with isolated():
                self.assertIsNone(current())
                self.assertTrue(isolation_requested())
            self.assertIs(current(), session)
        self.assertIsNone(current())

    def test_reading_call_does_not_create_or_append_a_run_conversation(self):
        selection = api_selection({'provider': 'zimaocloud', 'api_key': 'sk-synthetic-never-send'})
        provider = PdfEvidenceProvider(selection.model, selection.reasoning_effort,
                                       connection=selection)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / 'visible.png'
            Image.new('RGB', (8, 8), 'white').save(image)
            with patch.object(provider, '_call_stage', return_value={'documents': []}) as stage:
                with isolated():
                    provider._call(root / 'reading', root, {}, bound_prompt('read'),
                                   images=[image], label='reading', validator=None,
                                   transcription=True)
            stage.assert_called_once()
        self.assertIsNone(getattr(provider, '_run_conversation', None))

    def test_read_documents_runs_each_visual_request_without_history(self):
        selection = api_selection({'provider': 'zimaocloud', 'api_key': 'sk-synthetic-never-send'})
        provider = PdfEvidenceProvider(selection.model, selection.reasoning_effort,
                                       connection=selection)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            image = root / 'visible.png'
            Image.new('RGB', (8, 8), 'white').save(image)
            units = [{'unit_id': 'source-1', 'image': image, 'source_file': 'photo.png',
                      'locator': '照片1', 'native_facts': [], 'limitations': []}]

            def stage(call_root, skill, schema, prompt, *, images, label, validator, **kwargs):
                self.assertIsNone(current())
                manifest = json.loads(prompt.partial_variables['manifest'])
                value = {'documents': [{'unit_id': item['unit_id'], 'roles': ['photo'],
                                        'facts': ['本图可见商品'], 'limitations': []} for item in manifest]}
                validator(value)
                return value

            with patch.object(provider, '_call_stage', side_effect=stage):
                documents = provider._read_documents(
                    {'archive_id': 'a001', 'units': units, 'run_id': 'isolation-test'},
                    root, {})
        self.assertEqual(documents[0]['facts'], ['本图可见商品'])
        self.assertIsNone(getattr(provider, '_run_conversation', None))


class ZimaoTranscriptionPayloadTests(unittest.TestCase):
    def test_explicit_none_and_default_remain_supported_when_actually_selected(self):
        selection = api_selection({'provider': 'zimaocloud', 'api_key': 'sk-synthetic-never-send'})
        seen = []

        def handler(request):
            seen.append(json.loads(request.content))
            event = {'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}]}
            return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')

        from dataclasses import replace
        text, _ = complete_json(replace(selection, reasoning_effort='none'), [],
                                system='read', transport=httpx.MockTransport(handler))
        self.assertEqual(json.loads(text), {'ok': True})
        self.assertEqual(seen[0]['reasoning_effort'], 'none')

        seen.clear()
        complete_json(selection, [], system='read', transport=httpx.MockTransport(handler))
        self.assertNotIn('reasoning_effort', seen[0])


if __name__ == '__main__':
    unittest.main()
