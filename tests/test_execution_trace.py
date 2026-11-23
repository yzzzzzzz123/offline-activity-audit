"""Real recording code with isolated files and model/HTTP substitutes only."""
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from audit_core.execution_trace import collect_execution_trace, record_step, technical_inventory, run_command
from audit_core.model_metrics import collect_model_artifacts, save_model_observations
from audit_core.model_settings import api_selection
from audit_core.qwen_model import extract_qwen_json, QwenRequestError
from audit_core.workbench_store import WorkbenchRunStore
from audit_core.workbench_server import WorkbenchCatalog
from tests.test_workbench import ROOT_HTML


class ExecutionTraceTests(unittest.TestCase):
    def test_all_steps_survive_retries_restart_and_secret_redaction(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); analysis = root / 'analysis';analysis.mkdir()
            with collect_execution_trace(analysis):
                for i in range(135):record_step('query', f'第{i}步', {'row':i,'api_key':'never-store','url':'https://example.test/file?signature=private'})
            before = {p.name:p.read_bytes() for p in (analysis/'execution-trace').glob('*.json')}
            with collect_execution_trace(analysis):record_step('done','最终完成',{'answer':0})
            resources, trace = technical_inventory(root)
            self.assertEqual(trace['count'],136)
            self.assertEqual([r['sequence'] for r in trace['steps']],list(range(1,137)))
            self.assertEqual(before,{p.name:p.read_bytes() for p in (analysis/'execution-trace').glob('*.json') if p.name in before})
            text=''.join(p.read_text(encoding='utf8') for p in (analysis/'execution-trace').glob('*.json'))
            self.assertNotIn('never-store',text);self.assertNotIn('signature=private',text)
            self.assertEqual(len(resources),136)

    def test_attempts_keep_invalid_response_correction_and_valid_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory); analysis=root/'analysis';analysis.mkdir();schema=root/'schema.json'
            schema.write_text(json.dumps({'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False}))
            calls=[]
            def complete(selection,content,**kwargs):
                calls.append(content)
                answer='not-json' if len(calls)==1 else '{"ok":true}'
                kwargs['output_observer'](answer)
                return answer,{'input_tokens':10,'output_tokens':4}
            with collect_model_artifacts(analysis),patch('audit_core.qwen_model.complete_json',side_effect=complete),patch('audit_core.qwen_model.time.sleep'):
                result=extract_qwen_json(connection=api_selection({'provider':'qwen','api_key':'sk-synthetic-trace-only'}),root=root/'call',skill=root,
                    schema_path=schema,prompt='本次来源，第7行',images=[],label='隔离调用',validator=None)
            self.assertEqual(result,{'ok':True})
            files, trace=technical_inventory(root)
            events=[json.loads((root/x['path']).read_text(encoding='utf8')) for x in trace['steps']]
            self.assertEqual([e['type'] for e in events],['ai.request','ai.transport','ai.validation_failed','ai.retry','ai.request','ai.transport','ai.validated'])
            self.assertEqual(events[2]['data']['visible_response'],'not-json')
            self.assertEqual(events[-1]['data']['visible_response'],'{"ok":true}')
            self.assertEqual(len({e['call_id'] for e in events}),1)
            self.assertIn('上一次',events[4]['data']['input_text'])
            self.assertNotIn('sk-synthetic-trace-only',json.dumps(events))

    def test_partial_return_and_timeout_stay_failed_without_invented_result(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);analysis=root/'analysis';analysis.mkdir();schema=root/'schema.json';schema.write_text('{"type":"object"}')
            def complete(*args,**kwargs):
                kwargs['output_observer']('{"partial":')
                raise QwenRequestError('safe timeout','timeout',retryable=False,timeout_stage='read')
            with collect_model_artifacts(analysis),patch('audit_core.qwen_model.complete_json',side_effect=complete):
                with self.assertRaises(QwenRequestError):extract_qwen_json(connection=api_selection({'provider':'qwen','api_key':'sk-synthetic-trace-only'}),root=root/'call',skill=root,
                    schema_path=schema,prompt='scope',images=[],label='失败调用',validator=None)
            _,trace=technical_inventory(root);last=json.loads((root/trace['steps'][-1]['path']).read_text(encoding='utf8'))
            self.assertEqual(last['type'],'ai.failed');self.assertEqual(last['status'],'failed')
            self.assertEqual(last['data']['partial_visible_response'],'{"partial":');self.assertNotIn('validated_result',last['data'])

    def test_commands_retain_whole_stdout_stderr_and_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);analysis=root/'analysis';analysis.mkdir()
            completed=subprocess.CompletedProcess(['tool'],3,'first\n'+'x'*70000+'\nlast','failure-tail')
            with collect_execution_trace(analysis),patch('audit_core.execution_trace.subprocess.run',return_value=completed):
                actual=run_command(['tool','--api-key','never-store'],capture_output=True,text=True)
            self.assertIs(actual,completed)
            _,trace=technical_inventory(root);events=[json.loads((root/x['path']).read_text(encoding='utf8')) for x in trace['steps']]
            self.assertEqual(events[-1]['data']['stdout'],completed.stdout);self.assertEqual(events[-1]['data']['stderr'],'failure-tail')
            self.assertEqual(events[-1]['status'],'failed');self.assertNotIn('never-store',json.dumps(events))

    def test_runtime_checkpoint_and_readonly_history_discovery(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);workspace=root/'isolated';workspace.mkdir()
            store=WorkbenchRunStore(workspace,run_id='20261106',producer_model='ppio',root_html=ROOT_HTML,workbench_url='http://127.0.0.1:8080/')
            with collect_model_artifacts(workspace/'analysis',observer=store.observe):
                record_step('database.query','核对商品库',{'fields':['barcode_69'],'rows':[{'barcode_69':'6900000000001'}]})
                save_model_observations('photo-product-selection-a001',{'references':[]})
            catalog=WorkbenchCatalog(root)
            snapshot=catalog.snapshot('isolated')
            self.assertEqual(snapshot['execution_trace']['count'],2)
            self.assertTrue(any(c['stage']=='execution' for c in snapshot['dom_checkpoints']))
            record=snapshot['execution_trace']['steps'][0]
            self.assertTrue(catalog.analysis_file('isolated',record['path']).is_file())
            observation='analysis/model-observations/photo-product-selection-a001.json'
            self.assertTrue(catalog.analysis_file('isolated',observation).is_file())
            with self.assertRaises(FileNotFoundError):catalog.analysis_file('isolated','../private.json')
            before=(workspace/'snapshot.json').read_bytes();catalog.snapshot('isolated');self.assertEqual(before,(workspace/'snapshot.json').read_bytes())

    def test_no_collector_creates_no_fake_trace(self):
        self.assertIsNone(record_step('query','not in a run',{'x':1}))

    def test_step_display_keeps_actual_source_attempt_and_usage_without_large_payloads(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); analysis = root / 'analysis'; analysis.mkdir()
            with collect_execution_trace(analysis):
                record_step('database.result', '实际商品库返回', {
                    'data_source': {'table':'fixture_catalog.products','host':'db.fixture.invalid','password':'never-store'},
                    'rows':[{'product_name':'fixture'}] * 100})
                record_step('ai.failed', '失败尝试', {'attempt':1,'visible_response':'x'*100000,
                    'metrics':{'usage':{'input_tokens':10,'output_tokens':4,'cached_input_tokens':0,'reasoning_tokens':True}}},
                    status='failed',call_id='fixture-call')
                record_step('ai.failed', '未返回用量', {'attempt':2,'metrics':{}},status='failed',call_id='fixture-call')
            _, trace = technical_inventory(root)
            database, failed, missing = [step['display_data'] for step in trace['steps']]
            self.assertEqual(database['data_source']['host'],'db.fixture.invalid')
            self.assertEqual(database['data_source']['password'],'[已隐藏]')
            self.assertEqual(database['row_count'],100)
            self.assertNotIn('rows',database)
            self.assertEqual(failed['usage'],{'input_tokens':10,'output_tokens':4,'cached_input_tokens':0})
            self.assertEqual(failed['attempt'],1)
            self.assertNotIn('visible_response',failed)
            self.assertEqual(missing['usage'],{})
            self.assertLess(len(json.dumps(trace)),6000)


if __name__=='__main__':unittest.main()
