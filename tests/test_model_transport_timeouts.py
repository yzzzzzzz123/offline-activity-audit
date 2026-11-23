"""Transport faults use synthetic data, a loopback server and in-memory streams."""
from __future__ import annotations

import asyncio
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from audit_core.model_metrics import collect_model_artifacts, record_model_attempt
from audit_core.model_settings import api_selection
from audit_core.qwen_model import QwenRequestError, complete_json, extract_qwen_json


def stream_response():
    event = {'choices': [{'index': 0, 'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
             'usage': {'prompt_tokens': 12, 'completion_tokens': 5}}
    return ('data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n').encode()


class ModelTransportTimeoutTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selection = api_selection({'provider': 'ppio', 'api_key': 'sk_timeout_fixture_never_send'})
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps({'type': 'object', 'properties': {'ok': {'const': True}},
                                          'required': ['ok'], 'additionalProperties': False}))

    def test_production_wait_is_longer_and_probe_stays_bounded(self):
        budgets = []
        def handler(request):
            self.assertEqual(request.url.host, 'api.ppio.com')
            budgets.append(request.extensions['timeout'])
            return httpx.Response(200, content=stream_response())
        transport = httpx.MockTransport(handler)
        complete_json(self.selection, [], system='fixture', transport=transport)
        complete_json(self.selection, [], system='fixture', timeout_seconds=45, transport=transport)
        self.assertEqual(budgets[0], {'connect': 30, 'read': 300, 'write': 120, 'pool': 30})
        self.assertEqual(budgets[1], {'connect': 30, 'read': 45, 'write': 45, 'pool': 30})

    def test_delayed_first_packet_times_out_then_recovers_without_changing_evidence(self):
        bodies = []
        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                bodies.append(self.rfile.read(int(self.headers['Content-Length'])))
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.send_header('X-Request-ID', 'synthetic-delay-test')
                self.end_headers()
                self.wfile.flush()
                time.sleep(.15)
                try:
                    self.wfile.write(stream_response())
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = replace(self.selection, base_url='http://127.0.0.1:' + str(server.server_port))
        content = [{'type': 'text', 'text': 'Synthetic contract and photo fixture'}] + [
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,synthetic-' + str(i)}} for i in range(10)]
        observed = []
        try:
            with self.assertRaises(QwenRequestError) as raised:
                complete_json(connection, content, system='fixture', timeout_seconds=2,
                              read_wait_seconds=.03, progress=observed.append)
            self.assertEqual(raised.exception.timeout_stage, 'read')
            self.assertEqual(observed[-1]['model_event_count'], 0)
            self.assertTrue(observed[-1]['response_received'])
            self.assertEqual(observed[-1]['http_status'], 200)
            answer, _ = complete_json(connection, content, system='fixture', timeout_seconds=2,
                                      read_wait_seconds=.6)
            self.assertEqual(json.loads(answer), {'ok': True})
            self.assertEqual(bodies[0], bodies[1])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=3)

    def test_each_transport_timeout_retains_exact_stage_without_exception_text(self):
        for stage, exception in [('connect', httpx.ConnectTimeout), ('write', httpx.WriteTimeout),
                                 ('read', httpx.ReadTimeout), ('pool', httpx.PoolTimeout)]:
            seen = []
            def fail(request):
                raise exception('PRIVATE_PROVIDER_ERROR sk_private_not_a_real_key', request=request)
            with self.subTest(stage=stage), self.assertRaises(QwenRequestError) as raised:
                complete_json(self.selection, [], system='fixture', transport=httpx.MockTransport(fail),
                              progress=seen.append)
            self.assertEqual(raised.exception.timeout_stage, stage)
            self.assertEqual(seen[-1]['timeout_stage'], stage)
            self.assertEqual(seen[-1]['phase'], 'awaiting_model')
            self.assertNotIn('PRIVATE_PROVIDER_ERROR', str(raised.exception) + json.dumps(seen))

    def test_midstream_timeout_keeps_usage_and_response_phase(self):
        class Interrupted(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield stream_response().split(b'data: [DONE]')[0]
                raise httpx.ReadTimeout('PRIVATE_STREAM_ERROR')
        seen = []
        with self.assertRaises(QwenRequestError) as raised:
            complete_json(self.selection, [], system='fixture', progress=seen.append,
                          transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Interrupted())))
        self.assertEqual(raised.exception.timeout_stage, 'read')
        self.assertEqual(seen[-1]['phase'], 'model_output')
        self.assertEqual(seen[-1]['model_event_count'], 1)
        self.assertEqual(seen[-1]['input_tokens'], 12)

    def test_total_deadline_cancels_stalled_response(self):
        class Stalled(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(5)
                yield stream_response()
        started = time.monotonic()
        with self.assertRaises(QwenRequestError) as raised:
            complete_json(self.selection, [], system='fixture', timeout_seconds=.03,
                          read_wait_seconds=900, write_wait_seconds=480,
                          transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stalled())))
        self.assertEqual(raised.exception.timeout_stage, 'total')
        self.assertLess(time.monotonic() - started, 1)

    def test_retry_extends_only_the_failed_wait_and_keeps_all_ten_images(self):
        images = []
        for i in range(10):
            path = self.root / ('synthetic-' + str(i) + '.png')
            Image.new('RGB', (4, 4), 'white').save(path)
            images.append(path)
        for stage, reads, writes in [('read', [300, 600, 900], [120, 120, 120]),
                                     ('write', [300, 300, 300], [120, 240, 480]),
                                     ('connect', [300, 300, 300], [120, 120, 120])]:
            calls = []
            def complete(selection, content, **kwargs):
                calls.append((selection, content, kwargs['read_wait_seconds'], kwargs['write_wait_seconds']))
                if len(calls) < 3:
                    raise QwenRequestError('Synthetic timeout', 'timeout', retryable=True, timeout_stage=stage)
                return '{"ok":true}', {'input_tokens': 10, 'output_tokens': 5}
            with self.subTest(stage=stage), collect_model_artifacts(self.root / stage), \
                    patch('audit_core.qwen_model.complete_json', side_effect=complete), \
                    patch('audit_core.qwen_model.time.sleep'), \
                    patch('httpx.AsyncClient', side_effect=AssertionError('External access forbidden')):
                self.assertEqual(extract_qwen_json(connection=self.selection, root=self.root / stage,
                    skill=self.root, schema_path=self.schema, prompt='Synthetic shared evidence',
                    images=images, label='隔离十图核验', validator=None), {'ok': True})
            self.assertEqual([call[2] for call in calls], reads)
            self.assertEqual([call[3] for call in calls], writes)
            self.assertTrue(all(call[0] == self.selection for call in calls))
            self.assertTrue(all(call[1] == calls[0][1] for call in calls))
            self.assertEqual(sum(item['type'] == 'image_url' for item in calls[0][1]), 10)
            records = [json.loads(line) for line in (self.root / stage / 'model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]
            self.assertEqual([r['read_wait_seconds'] for r in records], reads)
            self.assertEqual(records[0]['timeout_stage'], stage)
            self.assertNotIn('timeout_stage', records[-1])

    def test_metric_allowlist_keeps_diagnostics_and_rejects_secrets(self):
        with collect_model_artifacts(self.root / 'metrics'):
            record_model_attempt({'label': '隔离超时计量', 'timeout_stage': 'read', 'timeout_kind': 'transport',
                'phase': 'awaiting_model', 'read_wait_seconds': 600, 'write_wait_seconds': 120,
                'http_status': 200, 'response_received': True, 'request_id': 'request-fixture-12',
                'api_key': 'PRIVATE_KEY', 'response_body': 'PRIVATE_BODY', 'reasoning_content': 'PRIVATE_REASONING'})
            record_model_attempt({'timeout_stage': {}, 'read_wait_seconds': float('nan'),
                                  'write_wait_seconds': -1, 'request_id': 'sk-private-fixture'})
        rows = [json.loads(line) for line in (self.root / 'metrics/model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(rows[0]['timeout_stage'], 'read')
        self.assertEqual(rows[0]['read_wait_seconds'], 600)
        self.assertEqual(rows[0]['request_id'], 'request-fixture-12')
        self.assertEqual(rows[1], {})
        self.assertNotIn('PRIVATE_', json.dumps(rows))


if __name__ == '__main__':
    unittest.main()
