from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
import zipfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import httpx
from PIL import Image
from langchain_core.messages import HumanMessage

from audit_core.common import AuditError
from audit_core.model_settings import (ModelSelection, ModelSettingsStore, ModelSettingsError,
                                      ModelSettingsConflict, SNAPSHOT_ENV, qwen_selection,
                                      api_selection, DEFAULT_PPIO_MODEL, selection_from_environment)
from audit_core.qwen_model import QwenChatModel, QwenRequestError, complete_json
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.workbench_server import WorkbenchCatalog, WorkbenchHTTPServer, Handler
from audit_core.workflow import invoke_local


class ModelSwitchingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selection = qwen_selection({"api_key": "test-secret-not-real"})

    def test_default_and_snapshot_survive_switch_and_restart_without_secret_disclosure(self):
        store = ModelSettingsStore(self.root)
        self.assertEqual(store.snapshot().provider, "gpt")
        self.assertFalse(store.path.exists())
        store.save(self.selection, expected_revision=0)
        old_run = store.snapshot()
        self.assertEqual(ModelSettingsStore(self.root).snapshot(), old_run)
        self.assertNotIn(self.selection.api_key, json.dumps(store.public()))
        self.assertNotIn(self.selection.api_key, repr(old_run))
        if os.name == "nt":
            self.assertNotIn(self.selection.api_key, store.path.read_text())
        store.save(ModelSelection(), expected_revision=1)
        self.assertEqual(store.snapshot().provider, "gpt")
        self.assertEqual(old_run.provider, "qwen")
        self.assertEqual(store.candidate({"api_key": ""}), old_run)
        with self.assertRaises(ModelSettingsConflict):
            store.save(old_run, expected_revision=1)

    def test_credentials_can_only_be_sent_to_bailian(self):
        for url in ("https://evil.test/compatible-mode/v1", "http://dashscope.aliyuncs.com/compatible-mode/v1",
                    "https://dashscope.aliyuncs.com.evil.test/compatible-mode/v1",
                    "https://name@dashscope.aliyuncs.com/compatible-mode/v1",
                    "https://dashscope.aliyuncs.com/compatible-mode/v1?key=bad"):
            with self.subTest(url=url), self.assertRaises(ModelSettingsError):
                qwen_selection({"api_key": "test", "base_url": url})

    def test_platform_credentials_are_isolated_and_legacy_bailian_settings_migrate(self):
        store = ModelSettingsStore(self.root)
        store.save(self.selection, expected_revision=0)
        persisted = json.loads(store.path.read_text(encoding="utf-8"))
        legacy = {"provider": "qwen", "revision": 1, **persisted["profiles"]["qwen"]}
        store.path.write_text(json.dumps(legacy), encoding="utf-8")
        self.assertEqual(store.snapshot(), self.selection)
        with patch.dict(os.environ, {"PPIO_API_KEY": ""}):
            with self.assertRaises(ModelSettingsError):
                store.candidate({"provider": "ppio"})
        ppio = store.candidate({"provider": "ppio", "api_key": "sk_ppio-test-secret"})
        store.save(ppio, expected_revision=1)
        self.assertEqual(ModelSettingsStore(self.root).snapshot(), ppio)
        self.assertEqual(store.candidate({"provider": "qwen"}), self.selection)
        self.assertEqual(store.public()["profiles"]["ppio"]["model"], DEFAULT_PPIO_MODEL)
        self.assertNotIn("sk_ppio-test-secret", json.dumps(store.public()))
        if os.name == "nt":
            self.assertNotIn("sk_ppio-test-secret", store.path.read_text())
        store.save(ModelSelection(), expected_revision=2)
        self.assertEqual(store.candidate({"provider": "ppio"}), ppio)
        self.assertEqual(store.candidate({"provider": "qwen"}), self.selection)
        with patch.dict(os.environ, {SNAPSHOT_ENV: ppio.subprocess_value()}):
            self.assertEqual(selection_from_environment(), ppio)

    def test_wrong_platform_key_endpoint_or_model_is_rejected_before_request(self):
        for payload in (
            {"provider": "qwen", "api_key": "sk_ppio-not-bailian"},
            {"provider": "ppio", "api_key": "sk-openai-not-ppio"},
            {"provider": "ppio", "api_key": "sk_test", "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1"},
            {"provider": "ppio", "api_key": "sk_test", "base_url": "https://api.ppio.com.evil.test/openai/v1"},
            {"provider": "ppio", "api_key": "sk_test", "base_url": "http://api.ppio.com/openai/v1"},
            {"provider": "ppio", "api_key": "sk_test", "base_url": "https://name@api.ppio.com/openai/v1"},
            {"provider": "ppio", "api_key": "sk_test", "base_url": "https://api.ppio.com/openai/v1?key=bad"},
            {"provider": "ppio", "api_key": "sk_test", "model": "qwen3-vl-plus"},
            {"provider": "ppio", "api_key": "sk_test", "model": "qwen/../qwen-model"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ModelSettingsError):
                api_selection(payload)

    def test_ppio_request_uses_official_endpoint_without_bailian_only_options(self):
        selection = api_selection({"provider": "ppio", "api_key": "sk_ppio-test", "base_url": "https://api.ppio.com/openai"})
        def handler(request):
            self.assertEqual(str(request.url), "https://api.ppio.com/openai/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer sk_ppio-test")
            payload = json.loads(request.content)
            self.assertEqual(payload["model"], DEFAULT_PPIO_MODEL)
            self.assertNotIn("enable_thinking", payload)
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            return httpx.Response(200, text='data: {"choices":[{"delta":{"content":"{}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
        self.assertEqual(complete_json(selection, [], system="JSON", transport=httpx.MockTransport(handler))[0], "{}")
        for status in (401, 403, 402):
            with self.subTest(status=status), self.assertRaises(QwenRequestError) as error:
                complete_json(selection, [], system="JSON", transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text="sk_ppio-test private diagnostics")))
            self.assertIn("PPIO", str(error.exception))
            self.assertNotIn("百炼", str(error.exception))
            self.assertNotIn("sk_ppio-test", str(error.exception))

    def test_bailian_vision_catalog_requests_use_the_models_supported_json_options(self):
        for model in ("qwen3.8-max", "qwen3.7-max-2026-06-08", "qwen3.8-omni-flash",
                      "qwen3.5-omni-plus", "qwen3.5-omni-plus-2026-03-15"):
            with self.subTest(model=model):
                selection = qwen_selection({"model": model, "api_key": "test-secret-not-real"})
                def handler(request):
                    payload = json.loads(request.content)
                    self.assertEqual(payload["model"], model)
                    self.assertEqual(payload["response_format"], {"type": "json_object"})
                    self.assertEqual(payload["messages"][1]["content"][0]["type"], "image_url")
                    self.assertTrue(payload["stream"])
                    if "omni" in model:
                        self.assertEqual(payload["modalities"], ["text"])
                        self.assertNotIn("enable_thinking", payload)
                        if model == "qwen3.8-omni-flash":
                            self.assertEqual(payload["reasoning_effort"], "none")
                        else:
                            self.assertNotIn("reasoning_effort", payload)
                    else:
                        self.assertFalse(payload["enable_thinking"])
                        self.assertNotIn("modalities", payload)
                    event = {"choices": [{"delta": {"content": '{"ok":true}'}, "finish_reason": "stop"}]}
                    return httpx.Response(200, text="data: " + json.dumps(event) + "\n\ndata: [DONE]\n\n")
                answer, _ = complete_json(selection, [{"type": "image_url", "image_url": {
                    "url": "data:image/png;base64,fixture"}}], system="Return JSON",
                    transport=httpx.MockTransport(handler))
                self.assertEqual(json.loads(answer), {"ok": True})
                self.assertEqual(selection.reasoning_effort, "none")

    def test_ppio_runtime_preserves_full_model_id_and_uses_safe_workspace_name(self):
        from audit_core.workbench_runtime import run_persistent_audit
        selection = api_selection({"provider": "ppio", "api_key": "sk_ppio-fixture"})
        source = self.root / "fixture.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("fixture.txt", "synthetic fixture")
        worktrees = self.root / "worktrees"
        ModelSettingsStore(worktrees).save(selection, expected_revision=0)
        def audit(*args, **kwargs):
            self.assertEqual(kwargs["evidence_provider"].connection, selection)
            raise AuditError("模拟执行中断")
        with patch.dict(os.environ, {SNAPSHOT_ENV: ""}), patch("audit_core.workbench_runtime.run_audit", side_effect=audit):
            with self.assertRaisesRegex(AuditError, "模拟执行中断"):
                run_persistent_audit("20261016-ppio-test", producer_model="codex", input_dir=source,
                    worktrees_root=worktrees, biz_type="陈列堆头")
        manifests = list(worktrees.glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["audit_model"], DEFAULT_PPIO_MODEL)
        self.assertEqual(manifest["producer_model"], "ppio")
        self.assertEqual(manifest["reasoning_effort"], "default")
        for path in manifests[0].parent.rglob("*"):
            if path.is_file():
                self.assertNotIn(selection.api_key.encode(), path.read_bytes())

    def test_sse_retains_only_final_content_and_requires_complete_response(self):
        events = [
            {"choices": [{"index": 0, "delta": {"reasoning_content": "private-thought", "content": '{"ok":'}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": "true}"}, "finish_reason": "stop"}]},
            {"choices": [], "usage": {"prompt_tokens": 12, "completion_tokens": 5}},
        ]
        def handler(request):
            payload = json.loads(request.content)
            self.assertEqual(request.url.host, "dashscope.aliyuncs.com")
            self.assertEqual(payload["model"], "qwen3-vl-plus")
            self.assertFalse(payload["enable_thinking"])
            self.assertEqual(payload["response_format"], {"type": "json_object"})
            self.assertNotIn("tools", payload)
            body = "".join("data: " + json.dumps(e) + "\n\n" for e in events) + "data: [DONE]\n\n"
            return httpx.Response(200, text=body)
        answer, metrics = complete_json(self.selection, [{"type": "text", "text": "json"}], system="JSON",
                                       transport=httpx.MockTransport(handler))
        self.assertEqual(json.loads(answer), {"ok": True})
        self.assertEqual(metrics["input_tokens"], 12)
        self.assertNotIn("private-thought", str(metrics) + answer)
        events[1]["choices"][0]["finish_reason"] = "length"
        with self.assertRaises(QwenRequestError) as error:
            complete_json(self.selection, [], system="JSON", transport=httpx.MockTransport(handler))
        self.assertEqual(error.exception.error_code, "output_limit")
        events[1]["choices"][0]["finish_reason"] = None
        with self.assertRaises(QwenRequestError):
            complete_json(self.selection, [], system="JSON", transport=httpx.MockTransport(handler))

    def test_auth_and_redirect_errors_never_echo_provider_body_or_key(self):
        for status in (401, 403, 302, 429, 500):
            with self.subTest(status=status), self.assertRaises(QwenRequestError) as error:
                complete_json(self.selection, [], system="JSON", transport=httpx.MockTransport(
                    lambda request: httpx.Response(status, text="private-body test-secret-not-real")))
            self.assertNotIn("private-body", str(error.exception))
            self.assertNotIn(self.selection.api_key, str(error.exception))

    def test_total_timeout_cancels_stalled_stream(self):
        class Stalled(httpx.AsyncByteStream):
            async def __aiter__(self):
                await asyncio.sleep(5)
                yield b'data: [DONE]\n\n'
        started = time.monotonic()
        with self.assertRaises(QwenRequestError) as error:
            complete_json(self.selection, [], system="JSON", timeout_seconds=0.05,
                transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=Stalled())))
        self.assertEqual(error.exception.error_code, "timeout")
        self.assertLess(time.monotonic() - started, 1)

    def test_runtime_uses_one_selected_provider_and_saves_actual_model_metadata(self):
        from audit_core.workbench_runtime import run_persistent_audit
        worktrees = self.root / "worktrees"
        source = self.root / "test-input.zip"
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("fixture.txt", "synthetic fixture, not business evidence")
        settings = ModelSettingsStore(worktrees)
        settings.save(self.selection, expected_revision=0)
        def audit(*args, **kwargs):
            provider = kwargs["evidence_provider"]
            self.assertEqual(provider.connection, self.selection)
            settings.save(ModelSelection(), expected_revision=1)
            self.assertEqual(provider.connection, self.selection)
            raise AuditError("模拟执行中断")
        with patch.dict(os.environ, {SNAPSHOT_ENV: ""}), patch("audit_core.workbench_runtime.run_audit", side_effect=audit):
            with self.assertRaisesRegex(AuditError, "模拟执行中断"):
                run_persistent_audit("20261016-model-test", producer_model="codex", input_dir=source,
                    worktrees_root=worktrees, biz_type="陈列堆头")
        manifests = list(worktrees.glob("*/manifest.json"))
        self.assertEqual(len(manifests), 1)
        manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
        self.assertEqual(manifest["audit_model"], "qwen3-vl-plus")
        self.assertEqual(manifest["reasoning_effort"], "none")
        for path in manifests[0].parent.rglob("*"):
            if path.is_file():
                self.assertNotIn(self.selection.api_key.encode(), path.read_bytes())

    def test_only_attached_images_are_encoded_and_no_codex_is_launched(self):
        photo = self.root / "page.png"
        Image.new("RGB", (10, 10), "white").save(photo)
        provider = PdfEvidenceProvider(self.selection.model, "none", connection=self.selection)
        skill = self.root / "skill"
        (skill / "references").mkdir(parents=True)
        (skill / "SKILL.md").write_text("only-approved-skill", encoding="utf-8")
        schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}
        def complete(selection, content, **kwargs):
            self.assertIn("only-approved-skill", kwargs["system"])
            self.assertTrue(content[-1]["image_url"]["url"].startswith("data:image/png;base64,"))
            self.assertNotIn("file:", str(content))
            return '{"ok":true}', {"elapsed_seconds": 0.01}
        with patch("audit_core.qwen_model.complete_json", side_effect=complete), patch("audit_core.codex_runner._find_codex") as codex:
            value = provider._call(self.root / "call", skill, schema, "Return JSON", images=[photo], label="测试", validator=lambda v: None)
            self.assertEqual(value, {"ok": True})
            codex.assert_not_called()
        model = QwenChatModel(selection=self.selection, images=(photo,), system_prompt="JSON", model_root=self.root)
        with self.assertRaises(QwenRequestError), patch("audit_core.qwen_model.complete_json") as remote:
            invoke_local(model, [HumanMessage(content=[{"type": "text", "text": "JSON"},
                {"type": "image_url", "image_url": {"url": "https://foreign.invalid/image.png"}}])])
        remote.assert_not_called()

    def test_qwen_invalid_schema_is_not_accepted_or_repaired(self):
        provider = PdfEvidenceProvider(self.selection.model, "none", connection=self.selection)
        schema = {"type": "object", "required": ["real_source"], "properties": {"real_source": {"type": "string"}}}
        with patch("audit_core.qwen_model.complete_json", return_value=('{"invented":true}', {})), patch("audit_core.qwen_model.time.sleep"):
            with self.assertRaises(AuditError):
                provider._call(self.root / "invalid", self.root, schema, "JSON", images=[], label="测试", validator=lambda v: None)

    def test_formal_subprocess_gets_snapshot_and_actual_model(self):
        from audit_core.oss_intake import run_formal_audit_subprocess
        store = ModelSettingsStore(self.root)
        store.save(self.selection, expected_revision=0)
        def run(command, **kwargs):
            self.assertEqual(command[command.index("--model") + 1], "qwen3-vl-plus")
            snapshot = json.loads(kwargs["env"][SNAPSHOT_ENV])
            store.save(ModelSelection(), expected_revision=1)
            self.assertEqual(snapshot["provider"], "qwen")
            self.assertNotIn(self.selection.api_key, " ".join(command))
            result = Path(command[command.index("--result-json") + 1])
            result.write_text(json.dumps({"status": "completed", "workspace_id": "test", "error_count": 0}), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "")
        with patch("audit_core.oss_intake.subprocess.run", side_effect=run):
            result = run_formal_audit_subprocess(run_id="2026-10-16-test", producer_model="codex", input_dir=self.root,
                worktrees_root=self.root, scenario=None, workbench_url=None, biz_type="陈列堆头")
        self.assertEqual(result["status"], "completed")


class ModelSettingsAPITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.server = WorkbenchHTTPServer(("127.0.0.1", 0), Handler, catalog=WorkbenchCatalog(self.root))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(self, payload=None, *, origin=True, token=True):
        headers = {"Content-Type": "application/json"}
        if origin:
            headers["Origin"] = self.base
        if token:
            headers["X-Offline-Audit-Model-Token"] = self.server.model_settings_token
        request = urllib.request.Request(self.base + "/api/model-settings", headers=headers,
                                         data=json.dumps(payload).encode() if payload is not None else None)
        with self.opener.open(request, timeout=5) as response:
            return json.load(response)

    def test_test_does_not_activate_and_failed_activation_preserves_current_model(self):
        payload = {"action": "test", "revision": 0, "api_key": "test-secret"}
        self.assertEqual(self.request()["provider"], "gpt")
        with patch("audit_core.qwen_model.probe_qwen", return_value={"response": {"number": "5274", "color": "blue"}}):
            result = self.request(payload)
            self.assertEqual(result["settings"]["provider"], "gpt")
            self.assertFalse(self.server.model_settings.path.exists())
            payload["action"] = "activate"
            result = self.request(payload)
            self.assertEqual(result["settings"]["provider"], "qwen")
        self.assertNotIn("test-secret", json.dumps(self.request()))
        payload.update(revision=1, api_key="bad-key")
        with patch("audit_core.qwen_model.probe_qwen", side_effect=QwenRequestError("测试失败", "auth")):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(payload)
            self.assertEqual(error.exception.code, 502)
            error.exception.close()
        self.assertEqual(self.server.model_settings.snapshot().api_key, "test-secret")
        self.request({"action": "gpt", "revision": 1})
        self.assertEqual(self.request()["provider"], "gpt")

    def test_cross_origin_missing_token_and_empty_key_do_not_call_provider(self):
        with patch("audit_core.qwen_model.probe_qwen") as remote:
            for origin, token in ((False, True), (True, False)):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request({"action": "activate", "revision": 0, "api_key": "test"}, origin=origin, token=token)
                self.assertEqual(error.exception.code, 403)
                error.exception.close()
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request({"action": "activate", "revision": 0, "api_key": ""})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            remote.assert_not_called()

    def test_ppio_activation_is_explicit_and_bailian_does_not_reuse_its_key(self):
        payload = {"action": "test", "revision": 0, "provider": "ppio", "api_key": "sk_ppio-api-fixture"}
        with patch("audit_core.qwen_model.probe_qwen", return_value={"provider": "ppio", "response": {"number": "5274", "color": "blue"}}) as probe:
            result = self.request(payload)
            self.assertEqual(probe.call_args.args[0].provider, "ppio")
            self.assertEqual(result["settings"]["provider"], "gpt")
            result = self.request({**payload, "action": "activate"})
            self.assertEqual(result["settings"]["provider"], "ppio")
            self.assertIn("PPIO", result["message"])
            with patch.dict(os.environ, {"DASHSCOPE_API_KEY": ""}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    self.request({"action": "activate", "revision": 1, "provider": "qwen"})
                self.assertEqual(error.exception.code, 400)
                error.exception.close()
            self.assertEqual(self.request()["provider"], "ppio")
            self.request({"action": "gpt", "revision": 1})
            self.assertEqual(self.request()["provider"], "gpt")
            self.assertTrue(self.request()["profiles"]["ppio"]["has_api_key"])


if __name__ == "__main__":
    unittest.main()
