from pathlib import Path
from unittest.mock import patch
import json
import os
import tempfile
import unittest
import zipfile

import httpx

from audit_core.common import AuditError
from audit_core.model_availability import fetch_candidate_models
from audit_core.model_catalog import model_connections, public_model_id
from audit_core.model_settings import ModelSettingsError, ModelSettingsStore, api_selection, SNAPSHOT_ENV
from audit_core.qwen_model import complete_json, QwenRequestError


class DeepSeekProviderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.selection = api_selection({'provider':'deepseek','api_key':'sk-synthetic-deepseek'})

    def test_official_image_model_and_existing_ppio_route_are_distinct(self):
        self.assertEqual(model_connections('deepseek-v4.1-flash'),
                         {'deepseek':'deepseek-flash','ppio':'deepseek/deepseek-v4.1-flash',
                          'zimaocloud':'deepseek-v4-1-flash'})
        self.assertEqual(public_model_id(self.selection.model), 'deepseek-v4.1-flash')
        self.assertEqual(self.selection.reasoning_effort, 'high')
        self.assertEqual(api_selection({'provider':'deepseek','model':'deepseek-v4-pro',
                                       'api_key':'synthetic','reasoning_effort':'max'}).model,'deepseek-v4-pro')
        for model in ('deepseek-chat','deepseek-reasoner','qwen3.8-max'):
            with self.subTest(model=model), self.assertRaises(ModelSettingsError):
                api_selection({'provider':'deepseek','model':model,'api_key':'synthetic'})

    def test_secret_is_only_sent_to_official_https_endpoint(self):
        self.assertEqual(api_selection({'provider':'deepseek','api_key':'synthetic',
                                       'base_url':'https://api.deepseek.com/v1/'}).base_url,
                         'https://api.deepseek.com')
        for url in ('http://api.deepseek.com','https://api.deepseek.com.evil.test',
                    'https://user@api.deepseek.com','https://api.deepseek.com?key=x',
                    'https://api.deepseek.com:444','https://api.deepseek.com/other',
                    'https://api.ppio.com/openai/v1'):
            with self.subTest(url=url), self.assertRaises(ModelSettingsError):
                api_selection({'provider':'deepseek','api_key':'synthetic','base_url':url})

    def test_activation_preserves_other_credentials_and_in_flight_snapshot(self):
        store = ModelSettingsStore(self.root)
        original = api_selection({'provider':'ppio','api_key':'sk_existing-fixture'})
        store.save(original, expected_revision=0)
        frozen = store.snapshot()
        candidate = store.candidate({'model':'deepseek-v4.1-flash','api_key':self.selection.api_key})
        self.assertEqual(candidate, self.selection)
        store.availability.record(candidate, passed=True)
        store.save(candidate, expected_revision=1)
        self.assertEqual(ModelSettingsStore(self.root).snapshot(), candidate)
        self.assertEqual(frozen, original)
        self.assertEqual(store.candidate({'provider':'ppio'}), original)
        public = json.dumps(store.public())
        self.assertNotIn(candidate.api_key, public)
        self.assertNotIn(original.api_key, public)
        self.assertNotIn(candidate.api_key, repr(candidate))
        self.assertEqual(store.public()['active_model_id'], 'deepseek-v4.1-flash')

    def test_provider_credentials_do_not_cross_reuse(self):
        store = ModelSettingsStore(self.root)
        store.save(api_selection({'provider':'ppio','api_key':'sk_existing-fixture'}),expected_revision=0)
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY':'','DEEPSEEK_API_KEY_FILE':''}):
            with self.assertRaises(ModelSettingsError):
                store.candidate({'provider':'deepseek'})
        self.assertEqual(store.candidate({'model':'deepseek-v4.1-flash','api_key':'sk_ppio-fixture'}).provider,'ppio')

    def test_explicit_platform_refresh_and_public_model_selection_remain_on_deepseek(self):
        store = ModelSettingsStore(self.root)
        original = api_selection({'provider':'ppio','api_key':'sk_existing-fixture'})
        store.save(original,expected_revision=0)
        refresh = store.candidate({'provider':'deepseek','api_key':'sk-synthetic-deepseek'})
        self.assertEqual(refresh, self.selection)
        selected = store.candidate({'provider':'deepseek','model':'deepseek-v4.1-flash',
                                    'api_key':'sk-synthetic-deepseek'})
        self.assertEqual(selected, self.selection)
        ppio = store.candidate({'provider':'ppio','model':'deepseek-v4.1-flash',
                               'api_key':'sk_existing-fixture'})
        self.assertEqual(ppio.model, 'deepseek/deepseek-v4.1-flash')
        with self.assertRaises(ModelSettingsError):
            store.candidate({'provider':'qwen','model':'deepseek-v4.1-flash',
                             'api_key':'sk-synthetic-deepseek'})
        self.assertEqual(store.snapshot(), original)
        self.assertEqual(store.public()['revision'], 1)

    def test_directory_filters_text_models_before_any_probe(self):
        result = fetch_candidate_models(self.selection, transport=httpx.MockTransport(lambda request:
            httpx.Response(200,json={'data':[
                {'id':'deepseek-flash','input_modalities':['text','image']},
                {'id':'deepseek-v4-pro','input_modalities':['text']}]})))
        self.assertEqual([s.model for s in result], ['deepseek-flash'])

    def test_streaming_image_json_reasoning_and_cache_usage_are_handled(self):
        def handler(request):
            self.assertEqual(str(request.url),'https://api.deepseek.com/chat/completions')
            self.assertEqual(request.headers['Authorization'],'Bearer '+self.selection.api_key)
            payload=json.loads(request.content)
            self.assertEqual(payload['thinking'], {'type':'enabled'})
            self.assertEqual(payload['reasoning_effort'],'high')
            self.assertNotIn('enable_thinking',payload)
            self.assertEqual(payload['messages'][1]['content'][0]['type'],'image_url')
            self.assertEqual(payload['response_format'],{'type':'json_object'})
            events=[{'choices':[{'delta':{'reasoning_content':'private reasoning'}}]},
                    {'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}]},
                    {'choices':[],'usage':{'prompt_tokens':10,'completion_tokens':4,'prompt_cache_hit_tokens':6}}]
            return httpx.Response(200,text=''.join('data: '+json.dumps(e)+'\n\n' for e in events)+'data: [DONE]\n\n')
        answer, metrics=complete_json(self.selection,[{'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}],
                                     system='Return JSON',transport=httpx.MockTransport(handler))
        self.assertEqual(json.loads(answer),{'ok':True})
        self.assertEqual(metrics['cached_input_tokens'],6)
        self.assertEqual((metrics['input_tokens'],metrics['output_tokens']),(10,4))
        self.assertNotIn('private reasoning',answer+json.dumps(metrics))

    def test_auth_errors_do_not_expose_key_or_provider_response(self):
        with self.assertRaises(QwenRequestError) as error:
            complete_json(self.selection,[],system='JSON',transport=httpx.MockTransport(lambda request:
                httpx.Response(401,text=self.selection.api_key+' confidential diagnostics')))
        self.assertEqual(error.exception.error_code,'auth')
        self.assertIn('DeepSeek',str(error.exception))
        self.assertNotIn(self.selection.api_key,str(error.exception))
        self.assertNotIn('diagnostics',str(error.exception))

    def test_official_run_uses_deepseek_snapshot_without_gpt_fallback(self):
        from audit_core.workbench_runtime import run_persistent_audit
        source=self.root/'fixture.zip'
        with zipfile.ZipFile(source,'w') as archive:archive.writestr('fixture.txt','synthetic fixture')
        worktrees=self.root/'worktrees'
        ModelSettingsStore(worktrees).save(self.selection,expected_revision=0)
        def audit(*args,**kwargs):
            self.assertEqual(kwargs['evidence_provider'].connection,self.selection)
            raise AuditError('synthetic execution interruption')
        with patch.dict(os.environ,{SNAPSHOT_ENV:''}),patch('audit_core.workbench_runtime.run_audit',side_effect=audit):
            with self.assertRaisesRegex(AuditError,'synthetic execution interruption'):
                run_persistent_audit('20261104-deepseek-test',producer_model='codex',input_dir=source,
                                     worktrees_root=worktrees,biz_type='人员激励')
        manifest=json.loads(next(worktrees.glob('*/manifest.json')).read_text(encoding='utf-8'))
        self.assertEqual((manifest['producer_model'],manifest['audit_model'],manifest['reasoning_effort']),
                         ('deepseek','deepseek-flash','high'))
        for p in worktrees.glob('*/**/*'):
            if p.is_file():self.assertNotIn(self.selection.api_key.encode(),p.read_bytes())


if __name__=='__main__':unittest.main()
