"""Only real call receipts belong in the system route, including old logs."""
from copy import deepcopy
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.execution_trace import legacy_execution_route, read_execution_route, collect_execution_trace, record_step
from audit_core.workbench_server import WorkbenchCatalog
from audit_core.workbench_html import render_static_run_archive_html,WORKBENCH_CONTEXT_ID
from audit_core.workbench_runtime import ROOT_HTML


class LegacyExecutionRouteTests(TestCase):
    def fixture(self,root):
        workspace=Path(root)/'legacy-route';(workspace/'logs').mkdir(parents=True)
        run={'workspace_id':workspace.name,'run_id':'legacy-fixture','status':'completed','scenarios':['pos_target_incentive']}
        for name,value in [('manifest.json',run),('snapshot.json',{'run':run,'view':None,'analysis_files':[],'dom_checkpoints':[]})]:
            (workspace/name).write_text(json.dumps(value),encoding='utf-8')
        self.append(workspace,'run.created','intake.completed','material_classification.started','biz_type.resolved','scenario.started','result.validated','run.completed')
        return workspace

    def append(self,workspace,*kinds):
        path=workspace/'logs/events.jsonl'
        count=len(path.read_text(encoding='utf-8').splitlines()) if path.exists() else 0
        with path.open('a',encoding='utf-8') as stream:
            for index,kind in enumerate(kinds):
                stream.write(json.dumps({'seq':count+index+1,'type':kind,'scenario':'pos_target_incentive' if kind=='scenario.started' else None})+'\n')

    def test_phases_and_business_types_never_become_system_visits(self):
        events=[{'type':kind} for kind in ['run.created','scenario.started','scenario.started','result.validated','scenario.started','run.failed']]
        original=deepcopy(events)
        self.assertEqual(legacy_execution_route(events),[])
        self.assertEqual(events,original)
        self.assertEqual(legacy_execution_route([None,{}, {'type':'future.event','message':'sk-private-do-not-display'}]),[])
        calls=[{'type':'execution.step','details':{'type':kind,'sequence':5-index}} for index,kind in enumerate(['qince.request','qince.result','ai.request','database.query','ai.request'])]
        self.assertEqual(legacy_execution_route(events+calls),['勤策','AI 识别','商品数据库','AI 识别'])

    def test_catalog_snapshot_and_static_archive_share_readonly_history_route(self):
        with TemporaryDirectory() as directory:
            workspace=self.fixture(directory);before={str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
            expected=read_execution_route(workspace)
            self.assertEqual(expected,[])
            catalog=WorkbenchCatalog(Path(directory))
            with patch.object(catalog,'snapshot',side_effect=AssertionError('Catalog must not load snapshots')):
                self.assertEqual(catalog.list_runs()[0]['execution_route'],expected)
            for _ in range(2):self.assertEqual(catalog.snapshot(workspace.name)['run']['execution_route'],expected)
            html=render_static_run_archive_html(workspace,ROOT_HTML)
            marker=f'<script id="{WORKBENCH_CONTEXT_ID}" type="application/json">'
            context=json.loads(html.split(marker,1)[1].split('</script>',1)[0])
            self.assertEqual(context['run']['execution_route'],expected)
            self.assertEqual(context['static_catalog']['runs'][0]['execution_route'],expected)
            self.assertEqual(before,{str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()})

    def test_append_refreshes_catalog_without_manifest_change_or_snapshot_load(self):
        with TemporaryDirectory() as directory:
            workspace=self.fixture(directory);catalog=WorkbenchCatalog(Path(directory));first,etag=catalog.catalog_document()
            manifest=(workspace/'manifest.json').read_bytes()
            with patch('audit_core.execution_trace._event_route',side_effect=AssertionError('Stable journal reread')):
                self.assertEqual(catalog.catalog_document(),(first,etag))
            self.append(workspace,'run.resumed','qince.request','ai.request','database.query','ai.request','run.failed')
            latest,new_etag=catalog.catalog_document()
            self.assertNotEqual(etag,new_etag)
            self.assertEqual(json.loads(latest)['runs'][0]['execution_route'],['勤策','AI 识别','商品数据库','AI 识别'])
            self.assertEqual((workspace/'manifest.json').read_bytes(),manifest)

    def test_external_trace_takes_precedence_and_missing_corrupt_events_never_invent_steps(self):
        with TemporaryDirectory() as directory:
            workspace=self.fixture(directory)
            (workspace/'analysis').mkdir()
            with collect_execution_trace(workspace/'analysis'):
                record_step('qince.request','已保存请求');record_step('ai.request','已保存模型调用')
            self.assertEqual(read_execution_route(workspace),['勤策','AI 识别'])
            empty=Path(directory)/'empty';empty.mkdir()
            self.assertEqual(read_execution_route(empty),[])
            (empty/'logs').mkdir();(empty/'logs/events.jsonl').write_text('{partial\n{}\nnull\n',encoding='utf-8')
            self.assertEqual(read_execution_route(empty),[])

    def test_old_console_calls_refresh_without_phase_substitution_or_history_writes(self):
        with TemporaryDirectory() as directory:
            workspace=self.fixture(directory);log=workspace/'logs/run.log'
            log.write_text('AI 正在识别 核销资料读取（第 1/3 次，模型 fixture）...\n',encoding='utf-8')
            before={str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
            catalog=WorkbenchCatalog(Path(directory))
            first,etag=catalog.catalog_document()
            self.assertEqual(json.loads(first)['runs'][0]['execution_route'],['AI 识别'])
            self.assertEqual(before,{str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()})
            with log.open('a',encoding='utf-8') as stream:
                stream.write('2026-11-11T01:00:00Z INFO  intake 已读取商品数据库 12 条，供本次核销使用\nAI 正在识别 商品核对（第 1/3 次，模型 fixture）...\n')
            latest,new_etag=catalog.catalog_document()
            self.assertNotEqual(etag,new_etag)
            expected=['AI 识别','商品数据库','AI 识别']
            self.assertEqual(json.loads(latest)['runs'][0]['execution_route'],expected)
            self.assertEqual(catalog.snapshot(workspace.name)['run']['execution_route'],expected)

    def test_original_progress_clocks_and_database_clock_recover_visits_but_copied_clocks_do_not(self):
        from datetime import datetime,timezone
        with TemporaryDirectory() as directory:
            workspace=self.fixture(directory)
            events=[{'type':'run.created','timestamp':'2026-11-11T00:00:00Z'},
                    {'type':'run.completed','timestamp':'2026-11-11T00:03:00Z'}]
            (workspace/'logs/events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events),encoding='utf-8')
            folder=workspace/'analysis/model-observations';folder.mkdir(parents=True)
            files=[]
            for index,(saved,elapsed) in enumerate([(20,10),(110,10)]):
                path=folder/('call-progress-'+str(index)*16+'.json')
                path.write_text(json.dumps({'attempt':1,'phase':'model_output','elapsed_seconds':elapsed}),encoding='utf-8')
                stamp=datetime(2026,11,11,tzinfo=timezone.utc).timestamp()+saved
                os.utime(path,(stamp,stamp));files.append(path)
            database=folder/'photo-product-database-a001.json'
            database.write_text(json.dumps({'data_source':{'type':'mysql','read_at_utc':'2026-11-11T00:01:00Z'}}),encoding='utf-8')
            before={str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()}
            self.assertEqual(read_execution_route(workspace),['AI 识别','商品数据库','AI 识别'])
            self.assertEqual(before,{str(p.relative_to(workspace)):p.read_bytes() for p in workspace.rglob('*') if p.is_file()})
            for path in files:os.utime(path,(0,0))
            self.assertEqual(read_execution_route(workspace),[])
