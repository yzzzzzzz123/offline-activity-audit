from __future__ import annotations

from contextvars import ContextVar
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.globals import get_llm_cache, set_llm_cache
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langsmith import tracing_context
from langsmith.utils import tracing_is_enabled

from audit_core import codex_runner
from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.extraction_chain import build_extraction_chain, render_prompt
from audit_core.langchain_model import CodexChatModel
from audit_core.product_retriever import CatalogProductRetriever, select_product_candidates
from audit_core.prompts import bound_prompt
from audit_core.workflow import batch_local, invoke_local


class LangChainComponentTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.photo = self.root / 'photo-{source}.png'
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps({
            'type': 'object', 'properties': {'source_file': {'type': 'string'}},
            'required': ['source_file'], 'additionalProperties': False,
        }), encoding='utf-8')
        self.model = CodexChatModel(
            model_name='gpt-6-astra', reasoning_effort='medium', command=['test-codex'],
            model_root=self.root, raw_output=self.root / 'output.json', images=(self.photo,),
            environment={'PRIVATE_TOKEN': 'must-never-serialize'}, label='test',
            timeout_seconds=60, transport_timeout=30,
        )

    def test_chain_preserves_literal_json_and_attachment_names_through_transport(self):
        route = json.dumps({'source_file': self.photo.name})
        prompt = bound_prompt('Current route: {route}', route=route)
        prompt += bound_prompt('\nRetry: {feedback}', feedback='{only-current-block}')
        received = []

        def transport(command, **kwargs):
            received.append(kwargs['input'])
            self.assertFalse(tracing_is_enabled())
            self.model.raw_output.write_text(route, encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        validate = Mock()
        chain = build_extraction_chain(prompt, self.model,
            EvidenceOutputParser(schema_path=self.schema, post_validate=validate), images=[self.photo])
        with patch.object(codex_runner, 'run_model_process', side_effect=transport):
            self.assertEqual(invoke_local(chain, {}), {'source_file': self.photo.name})
        self.assertEqual(received, [f'Current route: {route}\nRetry: {{only-current-block}}'])
        self.assertEqual(render_prompt(prompt), received[0])
        validate.assert_called_once_with({'source_file': self.photo.name})

    def test_foreign_attachments_and_history_fail_before_transport(self):
        text = {'type': 'text', 'text': 'current block'}
        image = {'type': 'image_url', 'image_url': {'url': self.photo.as_uri()}}
        foreign = {'type': 'image_url', 'image_url': {'url': 'https://example.invalid/other-run.png'}}
        cases = [
            [HumanMessage(content=[text, foreign])],
            [HumanMessage(content=[text])],
            [HumanMessage(content=[text, image, image])],
            [AIMessage(content='old evidence'), HumanMessage(content=[text, image])],
            [HumanMessage(content='unbounded plain chat')],
        ]
        with patch.object(codex_runner, 'run_model_process') as transport:
            for messages in cases:
                with self.subTest(messages=messages), self.assertRaises(codex_runner.CodexRequestConfigurationError):
                    invoke_local(self.model, messages)
            transport.assert_not_called()

    def test_parser_never_repairs_partial_json_or_accepts_unknown_fields(self):
        validator = Mock()
        parser = EvidenceOutputParser(schema_path=self.schema, post_validate=validator)
        for invalid in ('{"source_file":"photo.png"', '[]',
                        '{"source_file":"photo.png","invented":true}'):
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, AuditError)):
                invoke_local(parser, invalid)
        validator.assert_not_called()
        valid = '```json\n{"source_file":"photo.png"}\n```'
        self.assertEqual(invoke_local(parser, valid), {'source_file': 'photo.png'})
        validator.assert_called_once()
        parser.post_validate = Mock(side_effect=AuditError('foreign source'))
        with self.assertRaisesRegex(AuditError, 'foreign source'):
            invoke_local(parser, valid)

    def test_model_bypasses_global_cache_and_parent_observers(self):
        observed = []

        class Observer(BaseCallbackHandler):
            def on_chat_model_start(self, serialized, messages, **kwargs):
                observed.append(messages)

            def on_chain_end(self, outputs, **kwargs):
                observed.append(outputs)

        def transport(command, **kwargs):
            self.assertFalse(tracing_is_enabled())
            self.model.raw_output.write_text('{"source_file":"private-photo"}', encoding='utf-8')
            return subprocess.CompletedProcess(command, 0, '', '')

        cache = Mock()
        previous = get_llm_cache()
        self.addCleanup(set_llm_cache, previous)
        set_llm_cache(cache)
        chain = build_extraction_chain('private facts', self.model,
            EvidenceOutputParser(schema_path=self.schema), images=[self.photo])

        def outer(_):
            invoke_local(chain, {})
            invoke_local(chain, {})
            return 'public completion'

        with tracing_context(enabled=False), patch.object(codex_runner, 'run_model_process', side_effect=transport) as run:
            RunnableLambda(outer).invoke(None, config={'callbacks': [Observer()]})
        self.assertEqual(run.call_count, 2)
        self.assertEqual(cache.mock_calls, [])
        self.assertEqual(observed, ['public completion'])
        self.assertNotIn('must-never-serialize', repr(self.model))
        self.assertNotIn('must-never-serialize', str(self.model.model_dump()))

    def test_debug_mode_fails_before_any_business_node(self):
        node = Mock()
        with patch('audit_core.workflow.get_debug', return_value=True):
            with self.assertRaises(AuditError):
                invoke_local(RunnableLambda(node), {'private': 'facts'})
        node.assert_not_called()

    def test_batch_preserves_order_and_current_catalog_context(self):
        catalog = ContextVar('current-test-catalog', default='missing')
        catalog.set('this-run-only')
        barrier = threading.Barrier(2)
        active = 0
        peak = 0
        lock = threading.Lock()

        def analyze(value):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            barrier.wait(timeout=10)
            self.assertEqual(catalog.get(), 'this-run-only')
            self.assertFalse(tracing_is_enabled())
            with lock:
                active -= 1
            return value * 2

        with tracing_context(enabled=True):
            values = batch_local(RunnableLambda(analyze), [3, 1, 4, 2], max_concurrency=2)
            self.assertTrue(tracing_is_enabled())
        self.assertEqual(values, [6, 2, 8, 4])
        self.assertEqual(peak, 2)

    def test_retriever_balances_photos_and_preserves_authoritative_records(self):
        products = [dict(product_id=str(i), product_name=f'catalog product {i}',
                         product_code=f'SKU{i}', barcode_69=f'69000000000{i:02}',
                         image_manifest_key=f'private/reference-{i}.json') for i in range(4)]
        catalog = {'snapshot_id': 'current-run', 'products': products}
        query = {'photo_queries': [{'visible_product_codes': ['SKU0', 'SKU1']},
                                   {'visible_product_codes': ['SKU2', 'SKU3']}]}
        retriever = CatalogProductRetriever(catalog=catalog, max_products=2)
        documents = invoke_local(retriever, json.dumps(query))
        self.assertEqual([d.id for d in documents], ['0', '2'])
        self.assertEqual(documents[0].page_content, products[0]['product_name'])
        self.assertEqual(documents[0].metadata['retrieval_score'], 700)
        self.assertNotIn('image_manifest_key', documents[0].metadata)
        selected = select_product_candidates(catalog, query, max_products=2)
        self.assertEqual(selected['snapshot_id'], 'current-run')
        self.assertIs(selected['products'][0], products[0])
        self.assertEqual(len(catalog['products']), 4)
        # Contract or Excel text outside the photo query cannot seed retrieval.
        self.assertEqual(invoke_local(retriever, json.dumps({'contract': 'SKU0', 'excel': 'SKU1'})), [])


if __name__ == '__main__':
    unittest.main()
