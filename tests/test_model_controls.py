"""Synthetic account catalogues and atomic changes to the two audit routes."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

import httpx
import urllib.error

from audit_core.model_controls import catalog, current_controls, save_controls, supports_images, effort_choices, DEFAULT_ROUTES
from audit_core.model_settings import ModelSettingsStore, ModelSettingsError, ModelSettingsConflict, api_selection, selection_from_environment, SNAPSHOT_ENV
from audit_core.qwen_model import complete_json, QwenRequestError


def seed(store):
    main=api_selection({'provider':'deepseek','model':'deepseek-flash',
        'api_key':'synthetic-deepseek-only','reasoning_effort':'max'})
    visual=api_selection({'provider':'zimaocloud','model':'qwen3.8-max',
        'api_key':'synthetic-qwen-only','reasoning_effort':'max'})
    store.save(main,expected_revision=0)
    store.save_visual(visual,expected_revision=1)
    return store.snapshot()


def directory(request):
    if request.url.host=='api.deepseek.com':
        assert request.headers['Authorization']=='Bearer synthetic-deepseek-only'
        rows=[{'id':'deepseek-flash'}, {'id':'deepseek-v4-pro'}]
    else:
        assert request.url.host=='aiclub.zimaocloud.com'
        assert request.headers['Authorization']=='Bearer synthetic-qwen-only'
        rows=[{'id':name} for name in ('qwen3.8-max','qwen3.8-flash','qwen3.7-plus',
            'qwen3-asr-flash','qwen2.5-vl-embedding','doubao-seed-2-1-pro-260915','gpt-fixture')]
    assert request.method=='GET' and request.url.path.endswith('/models')
    return httpx.Response(200,json={'data':rows})


class ModelControlTests(unittest.TestCase):
    def setUp(self):
        folder=TemporaryDirectory();self.addCleanup(folder.cleanup)
        self.store=ModelSettingsStore(Path(folder.name));self.original=seed(self.store)
        self.transport=httpx.MockTransport(directory)

    def payload(self, **changes):
        return {'revision':self.store.public()['revision'], **current_controls(self.store), **changes}

    def test_account_catalogue_isolated_keys_qwen_only_and_no_inference(self):
        with patch('audit_core.qwen_model.complete_json',side_effect=AssertionError('no inference')):
            result=catalog(self.store,transport=self.transport)
        self.assertEqual([row['id'] for row in result['deepseek']],['deepseek-flash','deepseek-v4-pro'])
        self.assertEqual(len(result['qwen']),3)
        self.assertTrue(all(row['id'].startswith('qwen') for row in result['qwen']))
        self.assertFalse(any('asr' in row['id'] or 'embedding' in row['id'] for row in result['qwen']))
        levels=next(row for row in result['qwen'] if row['id']=='qwen3.8-max')['efforts']
        self.assertEqual([row['value'] for row in levels],['default','none','low','medium','xhigh'])
        self.assertEqual([row['label'] for row in levels][1:],['none','low','medium','xhigh'])
        wire=json.dumps(result)
        self.assertNotIn(self.original.api_key,wire);self.assertNotIn(self.original.visual.api_key,wire)
        self.assertEqual(self.store.snapshot(),self.original)

    def test_both_models_change_atomically_preserving_keys_and_old_snapshot(self):
        before=self.store._read();probe=Mock(return_value={'passed':True})
        result=save_controls(self.store,self.payload(deepseek_model='deepseek-v4-pro',
            qwen_model='qwen3.8-flash',qwen_effort='medium'),transport=self.transport,probe=probe)
        after=self.store._read();current=self.store.snapshot()
        self.assertEqual(after['revision'],before['revision']+1)
        self.assertEqual(probe.call_count,2)
        self.assertEqual([call.args[1] for call in probe.call_args_list],['deepseek','qwen'])
        self.assertEqual(current.model,'deepseek-v4-pro');self.assertEqual(current.visual.model,'qwen3.8-flash')
        self.assertEqual(current.visual.reasoning_effort,'medium')
        self.assertEqual(after['profiles']['deepseek']['protected_key'],before['profiles']['deepseek']['protected_key'])
        self.assertEqual(after['visual_profile']['protected_key'],before['visual_profile']['protected_key'])
        self.assertEqual(self.original.model,'deepseek-flash');self.assertEqual(self.original.visual.model,'qwen3.8-max')
        self.assertNotIn('confirmation_token',result['settings'])
        with patch.dict('os.environ',{SNAPSHOT_ENV:current.subprocess_value()}):
            self.assertEqual(selection_from_environment(),current)

    def test_effort_only_save_does_not_call_a_model_or_rewrite_other_profiles(self):
        before=self.store._read();probe=Mock(side_effect=AssertionError('effort-only change is local'))
        save_controls(self.store,self.payload(deepseek_effort='low',qwen_effort='medium'),
                      transport=self.transport,probe=probe)
        after=self.store._read();probe.assert_not_called()
        self.assertEqual(after['profiles'],{**before['profiles'],'deepseek':{
            **before['profiles']['deepseek'],'reasoning_effort':'low'}})
        self.assertEqual(after['visual_profile'],{**before['visual_profile'],'reasoning_effort':'medium'})

    def test_probe_failure_invalid_selection_and_stale_revision_leave_entire_file_unchanged(self):
        before=self.store.path.read_bytes()
        for changes in ({'qwen_model':'gpt-fixture'}, {'qwen_model':'qwen3-asr-flash'},
                        {'qwen_model':'qwen3.7-plus','qwen_effort':'max'},
                        {'deepseek_model':'not-in-account'}, {'qwen_effort':[]}, {'revision':True}):
            with self.subTest(changes=changes), self.assertRaises((ModelSettingsError,ModelSettingsConflict)):
                save_controls(self.store,self.payload(**changes),transport=self.transport)
            self.assertEqual(self.store.path.read_bytes(),before)
        with self.assertRaises(QwenRequestError):
            save_controls(self.store,self.payload(deepseek_model='deepseek-v4-pro'),
                transport=self.transport,probe=Mock(side_effect=QwenRequestError('synthetic failed')))
        self.assertEqual(self.store.path.read_bytes(),before)

    def test_concurrent_configuration_change_during_probe_cannot_be_overwritten(self):
        before=self.store._read()
        def probe(selection, group):
            value=deepcopy(before);value['revision']+=1;value['external_marker']='preserve'
            self.store._write(value)
        with self.assertRaises(ModelSettingsConflict):
            save_controls(self.store,self.payload(deepseek_model='deepseek-v4-pro'),transport=self.transport,probe=probe)
        self.assertEqual(self.store._read()['external_marker'],'preserve')
        self.assertEqual(self.store.snapshot(),self.original)

    def test_all_exposed_qwen38_efforts_and_pro_efforts_reach_native_payload(self):
        for group,original,efforts in (
            ('qwen',self.original.visual,('default','none','low','medium','xhigh','max')),
            ('deepseek',replace(self.original,model='deepseek-v4-pro',visual=None),('none','low','high','max'))):
            for effort in efforts:
                selected=replace(original,reasoning_effort=effort)
                def remote(request):
                    body=json.loads(request.content)
                    self.assertEqual(body['model'],selected.model)
                    if group=='qwen':
                        self.assertFalse(body['preserve_thinking'])
                        if effort!='default':self.assertEqual(body['enable_thinking'],effort!='none')
                        expected='xhigh' if effort=='max' else effort
                    else:
                        self.assertEqual(body['thinking']['type'],'disabled' if effort=='none' else 'enabled')
                        expected=effort
                    if effort in {'default','none'}:self.assertNotIn('reasoning_effort',body)
                    else:self.assertEqual(body['reasoning_effort'],expected)
                    return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
                with self.subTest(group=group,effort=effort):
                    answer,_=complete_json(selected,[{'type':'text','text':'synthetic'}],system='JSON',transport=httpx.MockTransport(remote))
                    self.assertEqual(json.loads(answer),{'ok':True})

    def test_pro_rejects_direct_image_input_before_network(self):
        pro=replace(self.original,model='deepseek-v4-pro')
        with patch('httpx.AsyncClient',side_effect=AssertionError('network forbidden')), self.assertRaises(QwenRequestError):
            complete_json(pro,[{'type':'image_url','image_url':{'url':'data:image/png;base64,fixture'}}],system='JSON')

    def test_vision_catalogue_requires_confirmed_image_input_and_hides_other_tasks(self):
        for model in ('qwen3.6-flash','qwen3.7-flash','qwen3.8-flash','qwen3.8-max',
                      'qwen3-vl-plus','qwen3.7-plus','qwen3.6-plus','qwen3.5-plus','qwen3.5-omni-plus'):
            self.assertTrue(supports_images({'id':model,'tag':'文本'}),model)
        for model in ('qwen3.7-max','qwen3-max','qwen3-asr-flash','qwen2.5-vl-embedding',
                      'qwen-image-plus','qwen-future-unknown','gpt-fixture'):
            self.assertFalse(supports_images({'id':model}),model)
        for metadata in ({'input_modalities':['text']},{'modalities':{'input':['text']}},
                         {'supports_vision':False},{'capabilities':{'vision':False}}):
            self.assertFalse(supports_images({'id':'qwen3.8-max',**metadata}))
        self.assertTrue(supports_images({'id':'qwen-future-vision','input_modalities':['text','image']}))
        self.assertFalse(supports_images({'id':'qwen3-asr-flash','input_modalities':['image']}))
        self.assertEqual(effort_choices('qwen3.5-omni-plus'),[{'value':'default','label':'default（模型默认）'}])

    def test_legacy_effort_alias_is_displayed_officially_without_rewriting_untouched_route(self):
        before=self.store._read()
        self.assertEqual(current_controls(self.store)['qwen_effort'],'xhigh')
        save_controls(self.store,self.payload(deepseek_effort='low'),transport=self.transport,
            probe=Mock(side_effect=AssertionError('no inference')))
        self.assertEqual(self.store._read()['visual_profile'],before['visual_profile'])

    def test_aiclub_main_and_visual_routes_keep_independent_accounts_and_native_ids(self):
        folder=TemporaryDirectory();self.addCleanup(folder.cleanup)
        store=ModelSettingsStore(Path(folder.name))
        main=api_selection({'provider':'zimaocloud','model':'deepseek-v4-1-flash',
            'api_key':'synthetic-main-aiclub','reasoning_effort':'max'})
        visual=api_selection({'provider':'zimaocloud','model':'qwen3.8-max',
            'api_key':'synthetic-visual-aiclub','reasoning_effort':'max'})
        store.save(main,expected_revision=0);store.save_visual(visual,expected_revision=1)
        original=store.snapshot();before=store._read();accounts=[]
        def remote(request):
            accounts.append(request.headers['Authorization'])
            ids=(('deepseek-v4-pro-0813','deepseek-v4-1-flash','DeepSeek-V3.2',
                  'deepseek-v4-flash-0731','deepseek-v4-pro','qwen3.8-max','gpt-fixture')
                 if accounts[-1]=='Bearer synthetic-main-aiclub' else
                 ('qwen3.8-max','qwen3.7-plus','qwen3.7-max','qwen3-asr-flash','gpt-fixture'))
            return httpx.Response(200,json={'data':[{'id':name} for name in ids]})
        transport=httpx.MockTransport(remote)
        result=catalog(store,transport=transport)
        self.assertEqual(len(result['deepseek']),5)
        self.assertEqual([row['id'] for row in result['qwen']],['qwen3.8-max','qwen3.7-plus'])
        self.assertEqual(accounts,['Bearer synthetic-main-aiclub','Bearer synthetic-visual-aiclub'])
        probes=[]
        save_controls(store,{'revision':2,**current_controls(store),'deepseek_model':'deepseek-v4-pro-0813'},
            transport=transport,probe=lambda selection,group:probes.append((selection,group)))
        self.assertEqual(len(probes),1)
        self.assertEqual(probes[0][0].api_key,main.api_key)
        self.assertEqual(probes[0][1],'deepseek')
        self.assertEqual(store.snapshot().model,'deepseek-v4-pro-0813')
        self.assertEqual(store._read()['profiles']['zimaocloud']['protected_key'],
                         before['profiles']['zimaocloud']['protected_key'])
        self.assertEqual(store._read()['visual_profile'],before['visual_profile'])
        self.assertEqual(original.model,'deepseek-v4-1-flash')

    def test_legacy_thinking_switch_and_omni_text_output_reach_native_payload(self):
        for model,effort,expected in (
            ('qwen3.7-plus','enabled',{'enable_thinking':True}),
            ('qwen3.7-plus','none',{'enable_thinking':False}),
            ('qwen3.5-omni-plus','default',{'modalities':['text']}),
            ('qwen3.8-omni-flash','xhigh',{'modalities':['text'],'reasoning_effort':'xhigh'}),
            ('DeepSeek-V3.2','enabled',{'thinking':{'type':'enabled'}})):
            selected=api_selection({'provider':'zimaocloud','model':model,
                'api_key':'synthetic-only','reasoning_effort':effort})
            def remote(request):
                payload=json.loads(request.content)
                for key,value in expected.items():self.assertEqual(payload[key],value)
                if model in {'qwen3.7-plus','qwen3.5-omni-plus','DeepSeek-V3.2'}:
                    self.assertNotIn('reasoning_effort',payload)
                if model=='qwen3.5-omni-plus':self.assertNotIn('enable_thinking',payload)
                return httpx.Response(200,text='data: {"choices":[{"delta":{"content":"{\\"ok\\":true}"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n')
            with self.subTest(model=model,effort=effort):
                answer,_=complete_json(selected,[{'type':'text','text':'synthetic'}],system='JSON',transport=httpx.MockTransport(remote))
                self.assertEqual(json.loads(answer),{'ok':True})

    def test_enabled_thinking_switch_survives_runner_arguments_and_snapshot(self):
        from audit_core.workbench_runtime import build_parser
        args=build_parser().parse_args(['--run-id','20261119-synthetic','--producer-model','zimaocloud',
            '--reasoning-effort','enabled'])
        self.assertEqual(args.reasoning_effort,'enabled')
        selected=api_selection({'provider':'zimaocloud','model':'DeepSeek-V3.2',
            'api_key':'synthetic-only','reasoning_effort':args.reasoning_effort})
        with patch.dict('os.environ',{SNAPSHOT_ENV:selected.subprocess_value()}):
            self.assertEqual(selection_from_environment(),selected)


class ModelControlsAPITests(unittest.TestCase):
    def test_two_route_api_uses_existing_origin_token_and_rejects_hidden_settings(self):
        from tests.test_model_switching import ModelSettingsAPITests
        fixture=ModelSettingsAPITests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        store=fixture.server.model_settings;seed(store)
        def rows(selection, transport):
            request=httpx.Request('GET',selection.base_url+'/models',headers={'Authorization':'Bearer '+selection.api_key})
            return directory(request).json()['data']
        with patch('audit_core.model_controls._directory',side_effect=rows), \
             patch('audit_core.model_controls.probe_text_json',return_value={'ok':True}) as text_probe:
            result=fixture.request({'action':'model_catalog','revision':2})
            self.assertEqual(len(result['catalog']['deepseek']),2)
            payload={'action':'save_controls','revision':2,**DEFAULT_ROUTES}
            for kwargs in ({'origin':False},{'token':False}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    fixture.request(payload,**kwargs)
                self.assertEqual(error.exception.code,403)
            for extra in ({'api_key':'cannot-replace-key'},{'provider':'ppio'}):
                with self.assertRaises(urllib.error.HTTPError) as error:
                    fixture.request({**payload,**extra})
                self.assertEqual(error.exception.code,400)
            saved=fixture.request(payload)
            self.assertEqual(saved['settings']['active_model'],'deepseek-v4-pro')
            self.assertEqual(saved['settings']['revision'],3)
            text_probe.assert_called_once()
            with self.assertRaises(urllib.error.HTTPError) as error:
                fixture.request(payload)
            self.assertEqual(error.exception.code,409)
