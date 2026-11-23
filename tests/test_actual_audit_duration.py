"""Completed card duration comes from run clocks, excluding verified pauses."""
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from unittest import TestCase

from audit_core.run_timing import completed_timing
from audit_core.workbench_server import WorkbenchCatalog
from audit_core.workbench_html import render_static_run_archive_html, WORKBENCH_CONTEXT_ID
from audit_core.workbench_runtime import ROOT_HTML


class ActualAuditDurationTests(TestCase):
    def manifest(self):
        return {'workspace_id':'duration-fixture','run_id':'fixture','status':'completed',
                'created_at':'2026-11-11T00:00:00Z','analysis_started_at':'2026-11-11T00:05:00Z',
                'completed_at':'2026-11-11T00:07:38Z','scenarios':[]}

    def test_real_analysis_time_and_resumed_active_time(self):
        run=self.manifest()
        self.assertEqual(completed_timing(run)['active_seconds'],158)
        run['resume_history']=[{'previous_completed_at':'2026-11-11T00:05:20Z','resumed_at':'2026-11-11T00:06:20Z'}]
        result=completed_timing(run)
        self.assertEqual((result['active_seconds'],result['elapsed_seconds'],result['paused_seconds']),(98,158,60))
        for change in [{'status':'running'},{'status':'failed'},{'completed_at':None},
                       {'completed_at':'2026-11-10T00:00:00Z'},
                       {'resume_history':[{'previous_completed_at':'bad','resumed_at':'bad'}]}]:
            self.assertIsNone(completed_timing({**run,**change}))

    def test_catalog_cached_snapshot_and_static_archive_keep_historical_bytes(self):
        with TemporaryDirectory() as directory:
            run=self.manifest();workspace=Path(directory)/run['workspace_id'];workspace.mkdir()
            for name,value in [('manifest.json',run),('snapshot.json',{'run':run,'view':None,'analysis_files':[],'dom_checkpoints':[]})]:
                (workspace/name).write_text(json.dumps(value),encoding='utf-8')
            before={p.name:p.read_bytes() for p in workspace.iterdir()}
            catalog=WorkbenchCatalog(Path(directory))
            self.assertEqual(catalog.list_runs()[0]['audit_timing']['active_seconds'],158)
            for _ in range(2):self.assertEqual(catalog.snapshot(workspace.name)['run']['audit_timing']['active_seconds'],158)
            context=json.loads(render_static_run_archive_html(workspace,ROOT_HTML).split(f'<script id="{WORKBENCH_CONTEXT_ID}" type="application/json">',1)[1].split('</script>',1)[0])
            self.assertEqual(context['run']['audit_timing']['active_seconds'],158)
            self.assertEqual(before,{p.name:p.read_bytes() for p in workspace.iterdir()})
            copy=catalog.list_runs();copy[0]['audit_timing']['active_seconds']=1
            self.assertEqual(catalog.list_runs()[0]['audit_timing']['active_seconds'],158)

    def test_frontend_system_route_and_duration_are_on_the_same_line(self):
        html=ROOT_HTML.read_text(encoding='utf-8')
        functions=html.split('      const systemExecutionRoute = ',1)[1].split('      const completionEstimateContent = ',1)[0]
        code='const systemExecutionRoute = '+functions+"\nconsole.log(JSON.stringify([systemExecutionRoute(['已建立本次核销任务','勤策','AI 识别','商品数据库','AI 识别','本次核销未完成']),actualAuditDurationText({status:'completed',audit_timing:{active_seconds:158}}),actualAuditDurationText({status:'completed',audit_timing:{active_seconds:3670}}),actualAuditDurationText({status:'completed'}),actualAuditDurationText({status:'running',audit_timing:{active_seconds:10}}),actualAuditDurationText({status:'completed',audit_timing:{active_seconds:null}})]));"
        result=subprocess.run(['node','-e',code],capture_output=True,text=True,encoding='utf-8',check=True)
        self.assertEqual(json.loads(result.stdout),[['勤策','AI 识别','商品数据库','AI 识别'],' · 实际耗时：2 分 38 秒',' · 实际耗时：1 小时 1 分 10 秒','','',''])
        renderer=html.split('const executionRouteMarkup = ',1)[1].split('const refreshExecutionRouteText = ',1)[0]
        self.assertIn('runExecutionRouteText(run)',renderer)
