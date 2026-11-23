from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json

from audit_core.completion_estimates import CompletionEstimator, filtered_mean
from audit_core.run_timing import finish_timing

NOW = datetime(2026,11,10,10,tzinfo=timezone.utc)


def save(root, name, duration=900, *, running=False, elapsed=120, model='flash',
         scenario='promotional_display', files=10, points=(), **extra):
    workspace = root / name
    workspace.mkdir(parents=True, exist_ok=True)
    start = NOW - timedelta(seconds=elapsed) if running else NOW - timedelta(days=1,seconds=duration)
    manifest = {'workspace_id':name,'run_id':name,'status':'running' if running else 'completed',
        'producer_model':'zimaocloud','audit_model':model,'reasoning_effort':'default',
        'scenarios':[scenario], 'analysis_started_at':start.isoformat(), 'created_at':start.isoformat(),
        'completed_at':None if running else (start+timedelta(seconds=duration)).isoformat(), **extra}
    (workspace/'manifest.json').write_text(json.dumps(manifest),encoding='utf-8')
    (workspace/'analysis').mkdir(exist_ok=True)
    (workspace/'analysis/input-cases.json').write_text(json.dumps({'a001':{'inventory':[{}]*files}}),encoding='utf-8')
    (workspace/'logs').mkdir(exist_ok=True)
    with (workspace/'logs/events.jsonl').open('w',encoding='utf-8') as stream:
        for label,offset in points:
            event={'type':'execution.step','timestamp':(start+timedelta(seconds=offset)).isoformat(),
                'details':{'type':'ai.validated','label':label+' · 第1次返回及校验通过'}}
            stream.write(json.dumps(event,ensure_ascii=False)+'\n')
    # Forecasting must not parse the potentially enormous full audit snapshot.
    (workspace/'snapshot.json').write_text('not a snapshot for this test',encoding='utf-8')
    return workspace


class CompletionEstimateTests(TestCase):
    def setUp(self):
        temporary=TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);self.estimator=CompletionEstimator(self.root)

    def history(self, **kwargs):
        for i,duration in enumerate((600,900,1200)):save(self.root,f'history-{i}',duration,**kwargs)

    def current(self, **kwargs):
        save(self.root,'current',running=True,**kwargs)
        return next(v for v in self.estimator.document(now=NOW)['estimates'] if v.get('workspace_id')=='current')

    def test_matches_model_and_scenario_and_uses_real_elapsed_time(self):
        self.history()
        for i in range(3):
            save(self.root,f'other-model-{i}',10000,model='other')
            save(self.root,f'other-scenario-{i}',100,scenario='entry_fee')
        result=self.current()
        self.assertEqual(result['status'],'estimated')
        self.assertEqual(result['remaining_seconds'],780)
        self.assertEqual(result['sample_count'],3)
        self.assertEqual(result['match'],'model_and_scenario')
        self.assertEqual(result['estimated_completion_at'],(NOW+timedelta(seconds=780)).isoformat())

    def test_completed_step_updates_forecast_even_when_earlier_work_was_slow(self):
        self.history(points=[('内容读取 a001 第1批',300)])
        result=self.current(elapsed=1000,points=[('内容读取 a001 第1批',940)])
        self.assertTrue(result['progress_matched'])
        self.assertEqual(result['remaining_seconds'],540)

    def test_repeated_step_occurrence_is_not_matched_to_an_earlier_packet(self):
        self.history(points=[('审核',100),('审核',400)])
        result=self.current(elapsed=800,points=[('审核',100),('审核',700)])
        self.assertEqual(result['remaining_seconds'],400)

    def test_similar_material_count_is_preferred(self):
        self.history(files=10)
        for i in range(3):save(self.root,f'large-{i}',10000,files=1000)
        result=self.current(files=12)
        self.assertTrue(result['workload_matched'])
        self.assertEqual(result['sample_count'],3)
        self.assertEqual(result['remaining_seconds'],780)

    def test_failed_demo_resumed_and_invalid_timestamps_never_train(self):
        save(self.root,'good')
        save(self.root,'failed',status='failed')
        save(self.root,'demo',demo=True)
        save(self.root,'resumed',resume_history=[{'resumed_at':NOW.isoformat()}])
        save(self.root,'invalid',completed_at='bad-date')
        save(self.root,'future',completed_at=(NOW+timedelta(days=1)).isoformat())
        result=self.current()
        self.assertEqual(result['status'],'insufficient_history')
        self.assertNotIn('estimated_completion_at',result)

    def test_overdue_does_not_show_a_past_time_or_invent_a_countdown(self):
        self.history()
        result=self.current(elapsed=2000)
        self.assertEqual(result['status'],'beyond_history')
        self.assertNotIn('remaining_seconds',result)
        self.assertNotIn('estimated_completion_at',result)

    def test_resuming_deducts_only_recorded_inactive_wait(self):
        self.history()
        result=self.current(elapsed=10000,resume_history=[{
            'previous_completed_at':(NOW-timedelta(seconds=9900)).isoformat(),
            'resumed_at':(NOW-timedelta(seconds=60)).isoformat()}])
        self.assertEqual(result['remaining_seconds'],740)  # 100 seconds before stop + 60 after resume

    def test_missing_resume_stop_boundary_does_not_count_wait_as_work(self):
        self.history()
        result=self.current(resume_history=[{'resumed_at':NOW.isoformat()}])
        self.assertEqual(result['status'],'insufficient_history')

    def test_queue_callback_and_finished_records_have_no_fake_finish_clock(self):
        self.history()
        doc=self.estimator.document([{'job_id':'q','run_id':'queued','status':'downloaded'},
            {'job_id':'c','run_id':'callback','status':'callback'}],now=NOW)
        self.assertEqual([item['status'] for item in doc['estimates']],['queued','sending_result'])
        self.assertTrue(all('estimated_completion_at' not in item for item in doc['estimates']))

    def test_cache_refreshes_and_completed_current_disappears(self):
        self.history();self.current()
        save(self.root,'current',duration=100)
        self.assertEqual(self.estimator.document(now=NOW)['estimates'],[])

    def test_endpoint_is_readonly_and_does_not_change_catalog_contract(self):
        from tests.test_model_switching import ModelSettingsAPITests
        fixture=ModelSettingsAPITests();fixture.setUp()
        try:
            for i in range(3):save(fixture.root,f'h-{i}')
            save(fixture.root,'current',running=True,elapsed=0,
                 analysis_started_at=(datetime.now(timezone.utc)-timedelta(seconds=10)).isoformat())
            with fixture.opener.open(fixture.base+'/api/completion-estimates') as response:data=json.load(response)
            self.assertEqual(data['history_count'],3)
            self.assertEqual(data['estimates'][0]['status'],'estimated')
            self.assertNotIn('manifest',data['estimates'][0])
        finally:fixture.doCleanups()

    def test_forecast_uses_arithmetic_mean_after_removing_both_extremes(self):
        for i,duration in enumerate((1,600,610,620,650,9000)):
            save(self.root,f'h-{i}',duration)
        result=self.current()
        self.assertEqual(result['remaining_seconds'],500)  # mean(600,610,620,650)-120
        self.assertEqual(result['sample_count'],4)
        self.assertEqual(result['excluded_count'],2)
        self.assertEqual(result['calculation'],'outlier_filtered_mean')
        self.assertNotIn('remaining_range_seconds',result)

    def test_forecast_is_not_median_or_average_of_only_the_slowest_survivors(self):
        for i,duration in enumerate((600,750,1050)):
            save(self.root,f'h-{i}',duration)
        result=self.current(elapsed=100)
        self.assertEqual(result['remaining_seconds'],700)  # mean(600,750,1050)-100
        result=self.current(elapsed=900)
        self.assertEqual(result['status'],'beyond_history')

    def test_a_new_completed_run_updates_the_average_automatically(self):
        self.history()
        first=self.current()
        save(self.root,'new-completed',700)
        second=next(v for v in self.estimator.document(now=NOW)['estimates'] if v['workspace_id']=='current')
        self.assertEqual(first['remaining_seconds'],780)
        self.assertEqual(second['remaining_seconds'],730)
        self.assertEqual(second['sample_count'],4)

    def test_repeated_central_durations_do_not_hide_extremes_when_mad_is_zero(self):
        self.assertEqual(filtered_mean([600,600,600,600,9000]),(600,4))

    def test_saved_timing_counts_real_work_and_excludes_recorded_pause(self):
        manifest={'created_at':(NOW-timedelta(seconds=10000)).isoformat(),'completed_at':NOW.isoformat(),
            'resume_history':[{'previous_completed_at':(NOW-timedelta(seconds=9900)).isoformat(),
                               'resumed_at':(NOW-timedelta(seconds=60)).isoformat()}]}
        timing=finish_timing(manifest)
        self.assertEqual(timing['elapsed_seconds'],10000)
        self.assertEqual(timing['active_seconds'],160)
        self.assertEqual(timing['paused_seconds'],9840)

    @patch('audit_core.workbench_store.utc_now', lambda: NOW.isoformat())
    def test_completion_persists_timing_and_keeps_it_in_snapshot(self):
        from audit_core.workbench_runtime import ROOT_HTML
        from audit_core.workbench_store import WorkbenchRunStore
        from tests.test_workbench import _view_payload
        store=WorkbenchRunStore(self.root/'timed-run',run_id='20261110-timing-test',producer_model='codex',
            root_html=ROOT_HTML,workbench_url='http://127.0.0.1:8080',source_archives=['timing.zip'])
        store.manifest['analysis_started_at']=(NOW-timedelta(seconds=30)).isoformat()
        store.complete(view_payload=_view_payload(),scenarios=['maintenance_fee'],verification={'valid':True})
        persisted=json.loads((store.workspace/'manifest.json').read_text(encoding='utf-8'))
        snapshot=json.loads((store.workspace/'snapshot.json').read_text(encoding='utf-8'))
        self.assertGreaterEqual(persisted['audit_timing']['active_seconds'],30)
        self.assertEqual(snapshot['run']['audit_timing'],persisted['audit_timing'])

    @patch('audit_core.workbench_store.utc_now', lambda: NOW.isoformat())
    def test_failure_and_resume_preserve_each_real_timing_record(self):
        from audit_core.common import AuditError
        from audit_core.workbench_runtime import ROOT_HTML
        from audit_core.workbench_store import WorkbenchRunStore
        run_id='20261110-timing-resume'
        store=WorkbenchRunStore(self.root/'timing-resume',run_id=run_id,producer_model='codex',
            root_html=ROOT_HTML,workbench_url='http://127.0.0.1:8080',source_archives=['timing.zip'])
        store.manifest['analysis_started_at']=(NOW-timedelta(seconds=30)).isoformat()
        store.fail(AuditError('isolated timing failure'))
        prior=store.manifest['audit_timing']
        self.assertGreaterEqual(prior['active_seconds'],30)
        resumed=WorkbenchRunStore.resume_failed(store.workspace,run_id=run_id)
        self.assertEqual(resumed.manifest['resume_history'][-1]['previous_audit_timing'],prior)
        self.assertNotIn('audit_timing',resumed.manifest)
        resumed.fail(AuditError('isolated second failure'))
        self.assertGreaterEqual(resumed.manifest['audit_timing']['active_seconds'],prior['active_seconds'])
