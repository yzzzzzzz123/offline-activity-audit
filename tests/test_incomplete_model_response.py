"""Incomplete gateway replies must recover without accepting partial evidence."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from PIL import Image

from audit_core.model_metrics import collect_model_artifacts, record_model_attempt
from audit_core.model_settings import api_selection
from audit_core.qwen_model import QwenRequestError, complete_json, extract_qwen_json


def stream(events, done=True):
    body=''.join('data: '+json.dumps(e)+'\n\n' for e in events)
    if done:body+='data: [DONE]\n\n'
    return httpx.Response(200,text=body)


class IncompleteModelResponseTests(unittest.TestCase):
    def setUp(self):
        self.connection=api_selection({'provider':'zimaocloud','model':'qwen3.8-max',
            'reasoning_effort':'max','api_key':'sk_synthetic_never_send'})

    def test_reasoning_only_done_remains_failure_and_records_safe_end_facts(self):
        progress=[]
        with self.assertRaises(QwenRequestError) as error:
            complete_json(self.connection,[],system='fixture',progress=progress.append,
                transport=httpx.MockTransport(lambda _:stream([
                    {'choices':[{'delta':{'reasoning_content':'PRIVATE_THOUGHT'}}]}])))
        self.assertTrue(error.exception.incomplete_response)
        self.assertEqual(progress[-1]['response_finish_reason'],'missing')
        self.assertEqual(progress[-1]['reasoning_event_count'],1)
        self.assertEqual(progress[-1]['content_event_count'],0)
        self.assertTrue(progress[-1]['stream_done_seen'])
        self.assertFalse(progress[-1]['response_complete'])
        self.assertNotIn('input_tokens',progress[-1])
        self.assertNotIn('PRIVATE_THOUGHT',json.dumps(progress))

    def test_visible_json_without_finish_is_not_accepted(self):
        seen=[]
        with self.assertRaises(QwenRequestError) as error:
            complete_json(self.connection,[],system='fixture',progress=seen.append,
                transport=httpx.MockTransport(lambda _:stream([
                    {'choices':[{'delta':{'content':'{"ok":true}'}}]}],done=False)))
        self.assertTrue(error.exception.incomplete_response)
        self.assertFalse(seen[-1]['stream_done_seen'])
        self.assertTrue(seen[-1]['response_body_ended'])
        self.assertEqual(seen[-1]['visible_output_bytes'],11)

    def test_full_json_keeps_actual_usage_and_the_same_model_effort(self):
        def remote(request):
            payload=json.loads(request.content)
            self.assertFalse(payload['stream'])
            self.assertNotIn('stream_options',payload)
            self.assertEqual(payload['model'],'qwen3.8-max')
            self.assertEqual(payload['reasoning_effort'],'xhigh')
            self.assertNotIn('max_tokens',payload)
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}',
                'reasoning_content':'PRIVATE_THOUGHT'},'finish_reason':'stop'}],
                'usage':{'prompt_tokens':42,'completion_tokens':17,
                         'completion_tokens_details':{'reasoning_tokens':12}}})
        answer,metrics=complete_json(self.connection,[],system='fixture',stream_response=False,
                                    transport=httpx.MockTransport(remote))
        self.assertEqual(json.loads(answer),{'ok':True})
        self.assertEqual(metrics['response_mode'],'json')
        self.assertEqual(metrics['input_tokens'],42)
        self.assertEqual(metrics['output_tokens'],17)
        self.assertTrue(metrics['response_complete'])
        self.assertNotIn('PRIVATE_THOUGHT',answer+json.dumps(metrics))

    def test_full_json_without_stop_and_length_output_still_fail(self):
        for finish in (None,'length'):
            with self.subTest(finish=finish), self.assertRaises(QwenRequestError) as raised:
                complete_json(self.connection,[],system='fixture',stream_response=False,
                    transport=httpx.MockTransport(lambda _:httpx.Response(200,json={
                        'choices':[{'message':{'content':'{"ok":true}'},'finish_reason':finish}]})))
            self.assertEqual(raised.exception.error_code,'output_limit' if finish=='length' else 'invalid_output')

    def test_extraction_retries_incomplete_stream_with_identical_full_input(self):
        bodies=[]
        def remote(request):
            body=json.loads(request.content);bodies.append(body)
            if body['stream']:
                return stream([{'choices':[{'delta':{'reasoning_content':'PRIVATE_THOUGHT'}}]}])
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'},
                'finish_reason':'stop'}],'usage':{'prompt_tokens':42,'completion_tokens':17}})
        real_client=httpx.AsyncClient
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);skill=root/'skill';skill.mkdir()
            (skill/'SKILL.md').write_text('Return the supplied synthetic schema.','utf-8')
            schema=root/'schema.json';schema.write_text(json.dumps({'type':'object',
                'properties':{'ok':{'const':True}},'required':['ok'],'additionalProperties':False}))
            image=root/'original.png';Image.new('RGB',(4,4),'white').save(image)
            def client(*args,**kwargs):
                kwargs['transport']=httpx.MockTransport(remote)
                return real_client(*args,**kwargs)
            with collect_model_artifacts(root/'analysis'), \
                    patch('audit_core.qwen_model.httpx.AsyncClient',side_effect=client), \
                    patch('audit_core.qwen_model.time.sleep'):
                value=extract_qwen_json(connection=self.connection,root=root/'call',skill=skill,
                    schema_path=schema,prompt='Synthetic source; preserve all pixels.',images=[image],
                    label='隔离读图',validator=None)
            self.assertEqual(value,{'ok':True})
            self.assertEqual(len(bodies),2)
            first,second=bodies
            self.assertEqual(first.pop('stream_options'),{'include_usage':True})
            self.assertTrue(first.pop('stream'));self.assertFalse(second.pop('stream'))
            self.assertEqual(first,second)
            rows=[json.loads(line) for line in (root/'analysis/model-metrics.jsonl').read_text().splitlines()]
            self.assertEqual([r['response_mode'] for r in rows],['sse','json'])
            self.assertNotIn('input_tokens',rows[0])
            self.assertEqual(rows[1]['usage']['input_tokens'],42)
            self.assertNotIn('PRIVATE_THOUGHT',json.dumps(rows))

    def test_completion_metrics_allowlist_rejects_untrusted_finish_text(self):
        with tempfile.TemporaryDirectory() as directory:
            with collect_model_artifacts(Path(directory)):
                record_model_attempt({'response_mode':'json','response_finish_reason':'stop',
                    'stream_done_seen':False,'response_body_ended':True,'response_complete':True,
                    'content_event_count':1,'reasoning_event_count':3,'visible_output_bytes':11,
                    'reasoning_content':'PRIVATE_THOUGHT'})
                record_model_attempt({'response_finish_reason':'PRIVATE_FINISH','response_mode':'PRIVATE_MODE'})
            rows=[json.loads(line) for line in (Path(directory)/'model-metrics.jsonl').read_text().splitlines()]
        self.assertTrue(rows[0]['response_complete'])
        self.assertEqual(rows[1],{})
        self.assertNotIn('PRIVATE_',json.dumps(rows))


if __name__=='__main__':unittest.main()
