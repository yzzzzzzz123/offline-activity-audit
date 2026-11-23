from __future__ import annotations

import http.client
import io
import json
import os
from email.message import Message
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from audit_core.http_rejection import discard_rejected_body
from audit_core.workbench_html import SYSTEM_VERSION

from audit_core.workbench_demo import (
    ASSETS, MAX_REJECTED_BODY_BYTES, REJECTED_BODY_TIMEOUT,
    DemoCatalog, DemoHandler, DemoHTTPServer,
)


class DemoWorkbenchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporary = tempfile.TemporaryDirectory()
        cls.server = DemoHTTPServer(("127.0.0.1", 0), settings_root=Path(cls.temporary.name))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.temporary.cleanup()

    def request(self, path, method="GET", body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=5)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, dict(response.headers), response.read()
        finally:
            connection.close()

    def test_catalog_is_synthetic_and_matches_details(self):
        status, _, raw = self.request("/api/runs")
        value = json.loads(raw)
        self.assertEqual((status, value["count"]), (200, 6))
        self.assertEqual({run["input_source"] for run in value["runs"]}, {"input", "oss"})
        for run in value["runs"]:
            self.assertTrue(run["demo"])
            _, _, data = self.request(f'/api/runs/{run["workspace_id"]}/snapshot')
            snapshot = json.loads(data)
            self.assertEqual(run, snapshot["run"])
            rows = snapshot["view"]["sheets"][0]["rows"]
            self.assertEqual(run["error_count"], len(rows))

    def test_every_write_path_is_denied_without_changing_catalog(self):
        before = self.request("/api/runs")[2]
        settings = Path(self.temporary.name)
        files_before = {path.relative_to(settings): path.read_bytes() if path.is_file() else None
                        for path in settings.rglob("*")}
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for path in ("/api/runs/demo-pos", "/api/runs/demo-pos/manual-review",
                         "/api/model-settings", "/api/intake/jobs", "/api/audit"):
                with self.subTest(method=method, path=path):
                    status, _, raw = self.request(path, method, b'{"reviewed":true}')
                    self.assertEqual(status, 403)
                    self.assertTrue(json.loads(raw)["demo"])
        self.assertEqual(before, self.request("/api/runs")[2])
        self.assertEqual(files_before, {path.relative_to(settings): path.read_bytes() if path.is_file() else None
                                       for path in settings.rglob("*")})

    def test_rejected_small_body_can_arrive_after_headers_in_separate_packets(self):
        before = self.request("/api/runs")[2]
        original = discard_rejected_body
        body = b'{"reviewed":true}'
        for method, path in (("POST", "/api/intake/jobs"), ("PUT", "/api/runs/demo-pos"),
                             ("PATCH", "/api/runs/demo-pos/manual-review"),
                             ("DELETE", "/api/model-settings"), ("POST", "/api/model-settings")):
            with self.subTest(method=method, path=path):
                started, finished = threading.Event(), threading.Event()

                def tracked(handler):
                    started.set()
                    original(handler)
                    finished.set()

                with patch("audit_core.workbench_demo.discard_rejected_body", tracked), \
                        patch("audit_core.workbench_server.discard_rejected_body", tracked):
                    with socket.create_connection(self.server.server_address, timeout=3) as connection:
                        header = (f"{method} {path} HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                                  f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n")
                        connection.sendall(header.encode("ascii"))
                        self.assertTrue(started.wait(2), "handler did not begin rejecting the request")
                        self.assertFalse(finished.wait(0.025), "rejection closed before the body arrived")
                        connection.sendall(body[:4])
                        self.assertFalse(finished.wait(0.025), "rejection did not consume the complete body")
                        connection.sendall(body[4:])
                        response = http.client.HTTPResponse(connection)
                        response.begin()
                        self.assertEqual(response.status, 403)
                        self.assertTrue(json.loads(response.read())["demo"])
                        self.assertTrue(finished.wait(1))
        self.assertEqual(before, self.request("/api/runs")[2])

    def test_configuration_disables_external_actions(self):
        config = json.loads(self.request("/api/config")[2])
        self.assertTrue(config["demo"] and config["read_only"])
        for key in ("mysql_enabled", "model_enabled", "callback_enabled", "oss_intake_enabled"):
            self.assertFalse(config[key])

    def test_unknown_records_and_file_traversal_are_not_served(self):
        for path in ("/?run=missing", "/api/runs/missing/snapshot",
                     "/api/runs/demo-pos/analysis?path=../../.env",
                     "/api/runs/demo-pos/analysis?path=demo/record.json&path=.env",
                     "/api/runs/demo-pos/checkpoints/../../.env", "/.env"):
            with self.subTest(path=path):
                self.assertEqual(self.request(path)[0], 404)

    def test_selected_html_reuses_canonical_template_without_fallback_rows(self):
        _, _, raw = self.request("/?run=demo-personnel")
        html = raw.decode("utf-8")
        self.assertIn('id="audit-system-extension-script"', html)
        self.assertIn('"demo":true', html)
        self.assertIn("演示_红包凭证.jpg", html)
        self.assertNotIn("雨瑶人员激励参半销售数据2.xlsx", html)
        self.assertIn(f'<meta name="offline-audit-system-version" content="{SYSTEM_VERSION}">', html)
        self.assertNotIn("全部为虚构样例，仅供展示；不执行真实核销，不作为业务结算依据。", html)
        self.assertNotIn('class="as-demo-notice"', html)

    def test_model_configuration_is_restored_with_existing_request_protection(self):
        with tempfile.TemporaryDirectory() as directory:
            from audit_core.model_settings import ModelSettingsStore
            self.server.model_settings = ModelSettingsStore(Path(directory))
            settings = json.loads(self.request('/api/model-settings')[2])
            self.assertEqual(settings['provider'], 'ppio')
            self.assertEqual(set(settings['profiles']), {'qwen', 'ppio', 'deepseek', 'zimaocloud', 'doubao'})
            headers = {'Origin': 'http://127.0.0.1:' + str(self.server.server_port),
                       'Content-Type': 'application/json',
                       'X-Offline-Audit-Model-Token': settings['confirmation_token']}
            payload = {'action': 'activate', 'revision': settings['revision'], 'provider': 'ppio',
                       'model': 'qwen/qwen3.5-397b-a17b', 'base_url': 'https://api.ppio.com/openai/v1',
                       'api_key': 'sk_synthetic_not_a_real_key'}
            self.server.model_settings.availability.record(self.server.model_settings.candidate(payload), passed=True)
            with patch('audit_core.qwen_model.probe_qwen', return_value={
                    'provider': 'ppio', 'model': payload['model'], 'elapsed_seconds': 0,
                    'response': {'fixture': True}}) as probe:
                status, _, raw = self.request('/api/model-settings', 'POST', json.dumps(payload), headers)
            self.assertEqual(status, 200, raw)
            probe.assert_not_called()
            self.assertNotIn('test', json.loads(raw))
            self.assertNotIn(b'sk_synthetic_not_a_real_key', raw)
            current = json.loads(self.request('/api/model-settings')[2])
            self.assertEqual(current['provider'], 'ppio')
            self.assertTrue(current['profiles']['ppio']['has_api_key'])
            self.assertFalse(json.loads(self.request('/api/config')[2])['model_enabled'])
            self.server.model_settings = ModelSettingsStore(Path(self.temporary.name))

    def test_conditional_get_and_head(self):
        _, headers, raw = self.request("/api/runs")
        status, _, unchanged = self.request("/api/runs", headers={"If-None-Match": headers["ETag"]})
        self.assertEqual((status, unchanged), (304, b""))
        status, headers, head = self.request("/api/runs", "HEAD")
        self.assertEqual((status, head), (200, b""))
        self.assertEqual(int(headers["Content-Length"]), len(raw))

    def test_demo_import_and_render_have_no_external_or_business_side_effects(self):
        scripts = str(ASSETS.parent / "scripts")
        code = '''
import os, sys
sys.path.insert(0, sys.argv[1])
def guard(event, args):
    if event in {"socket.connect", "subprocess.Popen", "os.mkdir"}:
        raise AssertionError("Forbidden demo side effect: " + event)
    if event == "open" and isinstance(args[2], int) and args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
        raise AssertionError("Demo must not write files")
sys.addaudithook(guard)
from audit_core.workbench_demo import DemoCatalog
c = DemoCatalog()
for key in c.records:
    assert c.html(key)
assert not any(key in sys.modules for key in ["pymysql", "mysql.connector", "audit_core.workbench_server", "audit_core.oss_intake", "audit_core.model_settings"])
'''
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run([sys.executable, "-B", "-c", code, scripts], cwd=directory,
                                    capture_output=True, text=True, timeout=30,
                                    env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(Path(directory).iterdir()), [])


class DemoRejectedBodyTests(unittest.TestCase):
    def handler(self, headers, body=b"", *, previous_timeout=3):
        handler = object.__new__(DemoHandler)
        handler.headers = Message()
        for key, value in headers:
            handler.headers[key] = value
        handler.connection = Mock()
        handler.connection.gettimeout.return_value = previous_timeout
        handler.rfile = io.BytesIO(body)
        handler._reply = Mock()
        return handler

    def assert_rejected(self, handler):
        handler._read_only()
        self.assertTrue(handler.close_connection)
        handler._reply.assert_called_once_with(
            {"error": "演示模式只允许查看，不执行真实核销或修改数据。", "demo": True}, 403)

    def test_exact_length_is_consumed_without_reading_trailing_bytes(self):
        for length in (1, MAX_REJECTED_BODY_BYTES):
            with self.subTest(length=length):
                handler = self.handler([("Content-Length", "000" + str(length))], b"x" * length + b"next")
                self.assert_rejected(handler)
                self.assertEqual(handler.rfile.read(), b"next")
                self.assertEqual(handler.connection.settimeout.call_args.args, (3,))
                for call in handler.connection.settimeout.call_args_list[:-1]:
                    self.assertGreater(call.args[0], 0)
                    self.assertLessEqual(call.args[0], REJECTED_BODY_TIMEOUT)

    def test_missing_ambiguous_invalid_and_oversized_lengths_are_not_read(self):
        cases = [[], [("Content-Length", "0")], [("Content-Length", "-1")],
                 [("Content-Length", "17, 17")], [("Content-Length", "１７")],
                 [("Content-Length", "17"), ("Content-Length", "17")],
                 [("Content-Length", "17"), ("Transfer-Encoding", "chunked")],
                 [("Content-Length", "17"), ("Transfer-Encoding", "")],
                 [("Content-Length", str(MAX_REJECTED_BODY_BYTES + 1))],
                 [("Content-Length", "9" * 5000)]]
        for headers in cases:
            with self.subTest(headers=[(key, value[:40]) for key, value in headers]):
                handler = self.handler(headers, b"unread")
                self.assert_rejected(handler)
                self.assertEqual(handler.rfile.tell(), 0)
                handler.connection.settimeout.assert_not_called()

    def test_delegated_rejection_does_not_consume_the_body_twice(self):
        handler = self.handler([("Content-Length", "4")], b"bodytrailing")
        discard_rejected_body(handler)
        timeouts = list(handler.connection.settimeout.call_args_list)
        self.assert_rejected(handler)
        self.assertEqual(handler.rfile.read(), b"trailing")
        self.assertEqual(handler.connection.settimeout.call_args_list, timeouts)

    def test_total_deadline_bounds_drip_feed_and_restores_socket_timeout(self):
        handler = self.handler([("Content-Length", "3")], previous_timeout=None)
        handler.rfile = Mock()
        handler.rfile.read1.side_effect = [b"a", b"b"]
        with patch("audit_core.http_rejection.time.monotonic", side_effect=[10, 10.1, 10.4, 10.51]):
            self.assert_rejected(handler)
        self.assertEqual([call.args for call in handler.rfile.read1.call_args_list], [(3,), (2,)])
        budgets = [call.args[0] for call in handler.connection.settimeout.call_args_list]
        self.assertAlmostEqual(budgets[0], 0.4)
        self.assertAlmostEqual(budgets[1], 0.1)
        self.assertIsNone(budgets[2])

    def test_timeout_and_early_eof_still_return_the_same_403(self):
        for outcome in (socket.timeout("isolated timeout"), b""):
            with self.subTest(outcome=type(outcome).__name__):
                handler = self.handler([("Content-Length", "17")], previous_timeout=0.2)
                handler.rfile = Mock()
                if isinstance(outcome, BaseException):
                    handler.rfile.read1.side_effect = outcome
                else:
                    handler.rfile.read1.return_value = outcome
                self.assert_rejected(handler)
                handler.rfile.read1.assert_called_once_with(17)
                self.assertEqual(handler.connection.settimeout.call_args.args, (0.2,))
                self.assertLessEqual(handler.connection.settimeout.call_args_list[0].args[0], 0.2)
