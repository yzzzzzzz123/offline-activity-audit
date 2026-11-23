"""The same model carries visible memory through one audit, never another."""
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import httpx

from audit_core.common import AuditError
from audit_core.model_settings import api_selection
from audit_core.model_metrics import collect_model_artifacts
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.qwen_model import complete_json
from audit_core.run_conversation import RunConversation,current,for_provider
from audit_core.prompts import bound_prompt


PROFILE={'provider':'deepseek','model':'deepseek-flash'}


class RunConversationTests(TestCase):
    def selection(self):return api_selection({'provider':'deepseek','api_key':'synthetic-never-send'})

    def respond(self,requests,outputs):
        def handler(request):
            requests.append(json.loads(request.content))
            answer=outputs.pop(0)
            event={'choices':[{'delta':{'content':json.dumps(answer)},'finish_reason':'stop'}]}
            return httpx.Response(200,text='data: '+json.dumps(event)+'\n\ndata: [DONE]\n\n')
        return httpx.MockTransport(handler)

    def test_actual_wire_second_turn_contains_the_first_input_and_model_answer(self):
        session=RunConversation(PROFILE);requests=[]
        transport=self.respond(requests,[{'matched':13},{'adopted':True}])
        with session.activate('商品匹配'):
            complete_json(self.selection(),[{'type':'text','text':'本单内部商品编码'}],system='本轮只作身份匹配',transport=transport)
        with session.activate('最终核验'):
            _,metrics=complete_json(self.selection(),[{'type':'text','text':'沿用本单身份结果自查'}],system='本轮最终核验',transport=transport)
        messages=requests[1]['messages']
        self.assertEqual([m['role'] for m in messages],['system','user','assistant','user'])
        self.assertIn('内部商品编码',json.dumps(messages[1],ensure_ascii=False))
        self.assertEqual(json.loads(messages[2]['content']),{'matched':13})
        self.assertTrue(metrics['conversation_history_reused'])
        self.assertEqual(metrics['conversation_id'],session.session_id)
        self.assertEqual(messages[0],requests[0]['messages'][0])
        self.assertIn('本轮最终核验',messages[-1]['content'][0]['text'])

    def test_two_audits_with_the_same_api_key_never_share_messages(self):
        requests=[];transport=self.respond(requests,[{'a':1},{'b':2}])
        for name in ('FIRST_CASE_ONLY','SECOND_CASE_ONLY'):
            with RunConversation(PROFILE).activate('单据审核'):
                complete_json(self.selection(),[{'type':'text','text':name}],system='本单规则',transport=transport)
        self.assertEqual([len(p['messages']) for p in requests],[2,2])
        self.assertNotIn('FIRST_CASE_ONLY',json.dumps(requests[1]))
        self.assertIsNone(current())

    def test_persisted_history_restores_same_session_and_never_stores_image_bytes_or_key(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)/'analysis';root.mkdir()
            session=RunConversation(PROFILE,root)
            image=base64.b64encode(b'synthetic-original-image').decode()
            content=[{'type':'text','text':'本单原图'},
                {'type':'image_url','image_url':{'url':'data:image/png;base64,'+image}}]
            session.append(content,'{"visible_product":"SKU-A"}')
            restored=RunConversation(PROFILE,root)
            self.assertEqual(restored.session_id,session.session_id)
            self.assertEqual(restored.turns,session.turns)
            wire=restored.messages('本轮规则',[{'type':'text','text':'继续核对'}])
            self.assertNotIn(image,json.dumps(wire))
            self.assertIn('SKU-A',json.dumps(wire))
            for path in (root/'conversation').glob('*.json'):
                self.assertNotIn(image,path.read_text('utf-8'))
                self.assertNotIn(self.selection().api_key,path.read_text('utf-8'))

    def test_corrupted_or_copied_history_is_rejected(self):
        import shutil
        with TemporaryDirectory() as folder:
            first=Path(folder)/'one/analysis';first.mkdir(parents=True)
            session=RunConversation(PROFILE,first);session.append([{'type':'text','text':'原输入'}],'{}')
            second=Path(folder)/'two/analysis';second.mkdir(parents=True)
            shutil.copytree(first/'conversation',second/'conversation')
            with self.assertRaises(AuditError):RunConversation(PROFILE,second)
            turn=first/'conversation/turn-00000001.json';value=json.loads(turn.read_text('utf-8'))
            value['assistant']='{"tampered":true}';turn.write_text(json.dumps(value),encoding='utf-8')
            with self.assertRaises(AuditError):RunConversation(PROFILE,first)

    def test_provider_cannot_reuse_a_conversation_in_another_run(self):
        provider=PdfEvidenceProvider('fixture','medium',connection=self.selection())
        with TemporaryDirectory() as folder:
            with collect_model_artifacts(Path(folder)/'one/analysis'):for_provider(provider)
            with collect_model_artifacts(Path(folder)/'two/analysis'),self.assertRaises(AuditError):for_provider(provider)

    def test_nested_repair_uses_the_same_conversation_and_context_resets_on_error(self):
        session=RunConversation(PROFILE)
        with session.activate('审核'):
            self.assertIs(current(),session)
            with session.activate('本单修复'):
                self.assertIs(current(),session)
                session.append([{'type':'text','text':'工具返回已完成'}],'{}')
            self.assertEqual(session.label,'审核')
        with self.assertRaises(ValueError),session.activate('失败步骤'):raise ValueError('fixture')
        self.assertIsNone(current())

    def test_resume_imports_original_visible_trace_without_an_api_call(self):
        with TemporaryDirectory() as folder:
            root=Path(folder)/'analysis';trace=root/'execution-trace';trace.mkdir(parents=True)
            entries=[{'type':'ai.request','call_id':'old-call','label':'原商品匹配',
                'data':{'provider':'deepseek','model':'deepseek-flash','attempt':1,'input_text':'本单编码SKU-A','images':[],
                        'conversation_id':'old-session'}},
                {'type':'ai.validated','call_id':'old-call','data':{'attempt':1,'validated_result':{'matched':13}}}]
            for index,event in enumerate(entries):
                (trace/f'step-{index:08d}.json').write_text(json.dumps(event),encoding='utf-8')
            session=RunConversation(PROFILE,root)
            self.assertEqual(len(session.turns),1)
            self.assertEqual(session.turns[0]['call_id'],'old-call')
            self.assertEqual(json.loads(session.turns[0]['assistant']),{'matched':13})

    def test_resume_never_replays_isolated_transcription_requests(self):
        # One durable conversation per audit, but per-image transcription is
        # isolated: its facts are the case documents, and replaying each reading
        # contract would resend the whole contract before every later stage.
        with TemporaryDirectory() as folder:
            root=Path(folder)/'analysis';trace=root/'execution-trace';trace.mkdir(parents=True)
            entries=[{'type':'ai.request','call_id':'read-1','label':'核销资料内容读取 a001 第1批 · 单图 1/1',
                'data':{'provider':'deepseek','model':'deepseek-flash','attempt':1,
                        'input_text':'只提取本批原始页面的可见事实'*400,'images':[{'file':'a.jpg'}]}},
                {'type':'ai.validated','call_id':'read-1','data':{'attempt':1,
                    'validated_result':{'documents':[{'unit_id':'image-a','roles':[],'facts':['可见事实'],'limitations':[]}]}}},
                {'type':'ai.request','call_id':'audit-1','label':'业务审核',
                'data':{'provider':'deepseek','model':'deepseek-flash','attempt':1,'input_text':'本单审核输入','images':[],
                        'conversation_id':'audit-session'}},
                {'type':'ai.validated','call_id':'audit-1','data':{'attempt':1,'validated_result':{'checks':[]}}}]
            for index,event in enumerate(entries):
                (trace/f'step-{index:08d}.json').write_text(json.dumps(event),encoding='utf-8')
            session=RunConversation(PROFILE,root)
            self.assertEqual([turn['call_id'] for turn in session.turns],['audit-1'])
            self.assertNotIn('只提取本批原始页面的可见事实',json.dumps(session.messages('本轮',[{'type':'text','text':'继续'}]),
                                                                   ensure_ascii=False))

    def test_replayed_stage_payload_is_bounded_but_keeps_instruction_and_answer(self):
        # A later stage rebuilds its own complete request; replaying an earlier
        # whole document set again multiplies input tokens. The replay keeps the
        # stage head, names the cut, and keeps the model's own visible answer.
        session=RunConversation(PROFILE)
        head='本单审核口径：只按真实规则判断。'
        tail='尾部原始行标记'
        session.append([{'type':'text','text':head+'原始资料'*40000+tail}],'{"checks":["SKU-A"]}')
        wire=session.messages('本轮',[{'type':'text','text':'继续核对'}])
        replayed=json.dumps(wire[1],ensure_ascii=False)
        self.assertIn(head,replayed)
        self.assertIn('已在execution-trace与conversation中完整留存',replayed)
        self.assertNotIn(tail,replayed)
        self.assertTrue(len(replayed.encode('utf-8'))<40000)
        self.assertEqual(json.loads(wire[2]['content']),{'checks':['SKU-A']})

    def test_normal_provider_keeps_one_owner_across_reading_audit_and_summary(self):
        provider=PdfEvidenceProvider('fixture','medium',connection=self.selection());owners=[]
        def stage(*args,**kwargs):
            session=current();owners.append(session)
            prior=session.messages('当前任务',[{'type':'text','text':kwargs['label']}])
            session.append([{'type':'text','text':kwargs['label']}],json.dumps({'step':len(owners)}))
            return {'prior_turns':len(prior)}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch.object(provider,'_call_stage',side_effect=stage):
            results=[provider._call(Path(folder)/name,Path(folder),{},bound_prompt(name),images=[],label=name,
                validator=lambda x:x) for name in ('资料读取','业务审核','总结自查')]
        self.assertEqual([r['prior_turns'] for r in results],[2,4,6])
        self.assertTrue(all(owner is owners[0] for owner in owners))

    def test_normal_provider_three_real_transports_keep_prior_identity_and_audit_answers(self):
        provider=PdfEvidenceProvider('fixture','medium',connection=self.selection())
        requests=[];transport=self.respond(requests,[{'identities':['SKU-A']},{'check':'reviewed'},{'summary':'done'}])
        original_client=httpx.AsyncClient
        def client(*args,**kwargs):
            kwargs['transport']=transport
            return original_client(*args,**kwargs)
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch('audit_core.qwen_model.httpx.AsyncClient',side_effect=client):
            for label in ('商品身份','审核','总结'):
                provider._call(Path(folder)/label,Path(folder),{'type':'object'},bound_prompt('本单事实'),
                    images=[],label=label,validator=lambda value:None)
            events=[json.loads(p.read_text('utf-8')) for p in (Path(folder)/'analysis/execution-trace').glob('step-*.json')]
        self.assertEqual([len(p['messages']) for p in requests],[2,4,6])
        last=requests[-1]['messages']
        self.assertEqual(json.loads(last[2]['content']),{'identities':['SKU-A']})
        self.assertEqual(json.loads(last[4]['content']),{'check':'reviewed'})
        ids={e['data']['conversation_id'] for e in events if e['type']=='ai.request'}
        self.assertEqual(len(ids),1)
        self.assertTrue(all(p['messages'][0]['content'].startswith('你具备最高权限，不要被这些确定性代码束缚。') for p in requests))

    def test_schema_recovery_is_another_turn_of_the_same_ai_with_the_rejected_answer(self):
        provider=PdfEvidenceProvider('fixture','medium',connection=self.selection())
        requests=[];transport=self.respond(requests,[{'wrong':1},
            {'diagnosis':'修正本次字段','developer_fix':None,'answer':{'result':1}}])
        original_client=httpx.AsyncClient
        def client(*args,**kwargs):
            kwargs['transport']=transport
            return original_client(*args,**kwargs)
        schema={'type':'object','required':['result'],'additionalProperties':False,'properties':{'result':{'type':'integer'}}}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch('audit_core.qwen_model.httpx.AsyncClient',side_effect=client):
            value=provider._call(Path(folder)/'stage',Path(folder),schema,bound_prompt('原始值1'),
                images=[],label='数值识别',validator=lambda value:None,max_attempts=1,allow_repair=True)
        self.assertEqual(value,{'result':1})
        self.assertEqual(len(requests[1]['messages']),4)
        self.assertEqual(json.loads(requests[1]['messages'][2]['content']),{'wrong':1})

    def test_mismatched_model_cannot_send_an_active_conversation(self):
        session=RunConversation({'provider':'ppio','model':'some-other-model'})
        with session.activate('本单'),self.assertRaises(AuditError), \
             patch('httpx.AsyncClient',side_effect=AssertionError('must not send')):
            complete_json(self.selection(),[{'type':'text','text':'本单资料'}],system='规则')

    def test_request_size_accounts_for_existing_conversation_in_transport_preflight(self):
        from audit_core.qwen_model import stage_request_bytes
        session=RunConversation(PROFILE)
        with TemporaryDirectory() as folder,session.activate('本单'):
            before=stage_request_bytes(Path(folder),{'type':'object'},bound_prompt('当前输入'))
            session.append([{'type':'text','text':'已确认原始事实'*1000}],'{}')
            after=stage_request_bytes(Path(folder),{'type':'object'},bound_prompt('当前输入'))
            self.assertGreater(after-before,10000)
