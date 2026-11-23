"""Normal runtime creates a provider per case; HTTP never shares chat/cookies."""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import httpx

from audit_core.model_settings import api_selection
from audit_core.qwen_model import complete_json


class AuditContextIsolationTests(unittest.TestCase):
    def test_ten_cases_per_provider_have_fresh_closed_clients_and_no_prior_messages_or_cookies(self):
        original_client = httpx.AsyncClient
        for provider in ('ppio','deepseek','zimaocloud'):
            with self.subTest(provider=provider):
                requests, clients = [], []
                def client(*args, **kwargs):
                    instance = original_client(*args, **kwargs)
                    clients.append(instance)
                    return instance
                def handler(request):
                    payload = json.loads(request.content)
                    requests.append(payload)
                    self.assertNotIn('cookie', request.headers)
                    answer = {'choices':[{'delta':{'content':'{"ok":true}'},'finish_reason':'stop'}]}
                    return httpx.Response(200,headers={'set-cookie':'previous_case=do-not-reuse; Path=/'},
                        text='data: '+json.dumps(answer)+'\n\ndata: [DONE]\n\n')
                selection=api_selection({'provider':provider,'api_key':
                    'sk_synthetic_never_send' if provider=='ppio' else 'sk-synthetic-never-send'})
                with patch('audit_core.qwen_model.httpx.AsyncClient',side_effect=client), \
                     patch.object(httpx.AsyncHTTPTransport,'handle_async_request',side_effect=AssertionError('real network forbidden')):
                    for case in range(10):
                        text = f'ONLY_CASE_{case:02d} ' + '当前阶段资料' * 500
                        answer,metrics=complete_json(selection,[{'type':'text','text':text}],system='当前阶段固定规则',
                            transport=httpx.MockTransport(handler))
                        self.assertEqual(answer,'{"ok":true}')
                        self.assertEqual(metrics['context_scope'],'current_stage_only')
                        self.assertFalse(metrics['conversation_history_reused'])
                self.assertEqual(len(clients),10)
                self.assertEqual(len({id(c) for c in clients}),10)
                self.assertTrue(all(c.is_closed for c in clients))
                for case,payload in enumerate(requests):
                    self.assertEqual(len(payload['messages']),2)
                    self.assertEqual([m['role'] for m in payload['messages']],['system','user'])
                    text=json.dumps(payload,ensure_ascii=False)
                    self.assertIn(f'ONLY_CASE_{case:02d}',text)
                    self.assertTrue(all(f'ONLY_CASE_{other:02d}' not in text for other in range(10) if other!=case))
                    self.assertFalse({'conversation_id','thread_id','previous_response_id','session_id'} & payload.keys())

    def test_normal_run_factory_does_not_reuse_provider_objects_between_cases(self):
        from audit_core import workbench_runtime
        from audit_core.model_settings import ModelSettingsStore, SNAPSHOT_ENV
        from tests.test_workbench import _view_payload
        providers=[]
        def fake_run(*args,**kwargs):
            providers.append(kwargs['evidence_provider'])
            return {'view_payload':_view_payload(),'scenarios':['maintenance_fee'],'verification':{}}
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            source=root/'fixture.zip'
            with ZipFile(source,'w') as archive:archive.writestr('fixture.txt','synthetic only')
            selection=api_selection({'provider':'deepseek','api_key':'sk-synthetic-never-send'})
            ModelSettingsStore(root/'worktrees').save(selection,expected_revision=0)
            with patch.dict(os.environ,{SNAPSHOT_ENV:'','OFFLINE_AUDIT_CURRENT_SESSION':''}), \
                 patch('audit_core.workbench_runtime.run_audit',side_effect=fake_run), \
                 patch('audit_core.qince_context.load_context',return_value=None), \
                 patch('httpx.AsyncClient',side_effect=AssertionError('real network forbidden')):
                receipts=[workbench_runtime.run_persistent_audit(f'20261110-isolation-{case}',producer_model='codex',
                    input_dir=source,worktrees_root=root/'worktrees',biz_type='人员激励') for case in range(2)]
            self.assertIsNot(providers[0],providers[1])
            self.assertEqual(providers[0].connection,providers[1].connection)
            self.assertNotEqual(receipts[0]['workspace_id'],receipts[1]['workspace_id'])
            for receipt in receipts:
                traces=[json.loads(p.read_text(encoding='utf-8')) for p in
                        (Path(receipt['worktree'])/'analysis/execution-trace').glob('step-*.json')]
                started=next(t['data'] for t in traces if t['type']=='run.started')
                self.assertEqual(started['model_context']['run_id'],receipt['run_id'])
                self.assertFalse(started['model_context']['cross_run_conversation'])


if __name__ == '__main__': unittest.main()
