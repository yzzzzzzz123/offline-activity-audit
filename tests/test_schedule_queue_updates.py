"""Changing a schedule re-evaluates BOTH intake paths, including queued work.

Real HTTP endpoints and queues, synthetic ZIPs, controlled clocks and fake runners.
"""
import json
import threading
import time
import unittest
from dataclasses import replace
import urllib.request
from urllib.error import HTTPError
from urllib.parse import urlencode

from tests import test_audit_schedule as schedule_tests
from tests import test_oss_intake as oss_tests
from tests.test_offpeak_gate import MONDAY, moment


class ScheduleQueueUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = schedule_tests.ScheduleHTTPQueueTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.service = self.fixture.service
        self.clock = self.fixture.harness.now
        self.counter = 0

    def submit(self, source):
        self.counter += 1
        if source == 'system':
            payload = oss_tests._payload(verify_code=f'HX-SCHEDULE-{self.counter}', analyze_id=self.counter)
            request = urllib.request.Request(self.fixture.base + '/api/intake/oss',
                data=json.dumps(payload).encode('utf-8'), headers={'Content-Type':'application/json'})
        else:
            query = urlencode({'filename':'隔离测试资料.zip','bizType':'条码费','requestId':f'{self.counter:024x}'})
            request = urllib.request.Request(self.fixture.base + '/api/intake/upload?' + query,
                data=oss_tests._zip_bytes(), headers={'Content-Type':'application/zip',
                    'Origin':self.fixture.base,'X-Offline-Audit-Upload-Token':self.fixture.server.upload_token})
        with self.fixture.opener.open(request, timeout=5) as response:
            return json.load(response)['job']

    def wait(self, job, statuses):
        return self.fixture.harness._wait_for(job['job_id'], set(statuses), timeout=4)

    def save(self, start='10:30', end='11:30', *, mode='custom'):
        current = self.fixture.request()['settings']
        return self.fixture.request({**current, 'mode':mode,
            'weekly':[[[start,end]] for _ in range(7)]})

    def assert_eta(self, jobs, hour, minute):
        for job in jobs:
            value = self.service.get(job['job_id'])
            self.assertEqual(value['offpeak_next_start'], moment(MONDAY,hour,minute).isoformat())

    def test_both_http_intakes_move_later_and_earlier_with_no_runner_calls(self):
        jobs = [self.submit(source) for source in ('system','upload')]
        for job in jobs:self.wait(job, {'waiting_offpeak'})
        self.save('10:30','11:30');self.assert_eta(jobs,10,30)
        self.save('11:15','12:00');self.assert_eta(jobs,11,15)
        self.save('10:15','11:00');self.assert_eta(jobs,10,15)
        for job in jobs:self.assertEqual(self.service.get(job['job_id'])['status'],'waiting_offpeak')
        self.assertEqual(self.fixture.harness.runs,[])

    def test_open_now_releases_both_http_intakes_without_waiting_twenty_seconds(self):
        jobs = [self.submit(source) for source in ('system','upload')]
        for job in jobs:self.wait(job, {'waiting_offpeak'})
        self.save('10:30','11:30')
        started=time.monotonic();self.save('09:30','11:30')
        for job in jobs:self.wait(job, {'completed'})
        self.assertLess(time.monotonic()-started,3)
        self.assertEqual(len(self.fixture.harness.runs),2)
        self.assertEqual(len({run['run_id'] for run in self.fixture.harness.runs}),2)

    def test_always_now_releases_both_sources_once(self):
        jobs = [self.submit(source) for source in ('system','upload')]
        for job in jobs:self.wait(job, {'waiting_offpeak'})
        self.save(mode='always')
        for job in jobs:self.wait(job, {'completed'})
        self.assertEqual(len(self.fixture.harness.runs),2)
        self.service._release_offpeak_jobs()
        self.assertEqual(len(self.fixture.harness.runs),2)

    def test_downloaded_jobs_behind_busy_runner_get_latest_eta_for_both_sources(self):
        started,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        calls=[]
        def runner(**kwargs):
            calls.append(kwargs['run_id'])
            if len(calls)==1:started.set();release.wait(5)
            return {'run_id':kwargs['run_id'],'status':'completed','workspace_id':'fixture','error_count':0}
        self.service.runner=runner;self.save('09:00','11:00')
        first=self.submit('upload');self.assertTrue(started.wait(3))
        jobs=[self.submit(source) for source in ('system','upload')]
        for job in jobs:self.wait(job, {'downloaded'})
        self.save('10:45','11:30')
        # These jobs are queued behind a worker, not yet in _offpeak_pending.
        self.assert_eta(jobs,10,45)
        self.assertEqual(self.service.get(first['job_id'])['status'],'running')
        release.set();self.wait(first,{'completed'})
        for job in jobs:self.wait(job,{'waiting_offpeak'})
        self.assertEqual(len(calls),1)
        self.clock[0]=moment(MONDAY,10,45);self.save('10:45','11:30')
        for job in jobs:self.wait(job,{'completed'})
        self.assertEqual(len(calls),3)

    def test_closing_window_at_runner_handoff_rechecks_latest_policy(self):
        self.save('09:00','11:00')
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        original=self.service._begin_job_stage
        def delayed(submission):
            # Only pause the audit worker after its first scheduling check.
            if threading.current_thread().name.startswith('offline-audit-oss-audit'):
                entered.set();release.wait(5)
            return original(submission)
        self.service._begin_job_stage=delayed
        job=self.submit('upload');self.assertTrue(entered.wait(3))
        self.save('10:30','11:30');release.set()
        self.wait(job,{'waiting_offpeak'})
        self.assertEqual(self.fixture.harness.runs,[])
        self.assert_eta([job],10,30)

    def test_repeated_closed_window_updates_do_not_release_or_duplicate_jobs(self):
        jobs=[self.submit(source) for source in ('system','upload')]
        for job in jobs:self.wait(job,{'waiting_offpeak'})
        for minute in range(10,21):self.save(f'10:{minute:02d}','11:00')
        self.assert_eta(jobs,10,20)
        self.assertEqual(self.fixture.harness.runs,[])
        self.save('09:00','11:00')
        for job in jobs:self.wait(job,{'completed'})
        self.assertEqual(len(self.fixture.harness.runs),2)

    def test_invalid_update_keeps_both_waiting_jobs_on_original_plan(self):
        jobs=[self.submit(source) for source in ('system','upload')]
        for job in jobs:self.wait(job,{'waiting_offpeak'})
        self.save('10:30','11:00')
        with self.assertRaises(HTTPError) as raised:self.save('bad','11:00')
        self.assertEqual(raised.exception.code,400)
        raised.exception.close()
        self.assert_eta(jobs,10,30)
        self.assertEqual(self.fixture.harness.runs,[])

    def test_ten_mixed_queued_jobs_follow_latest_schedule_while_running_job_continues(self):
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        calls=[]
        def runner(**kwargs):
            calls.append(kwargs['run_id'])
            if len(calls)==1:entered.set();release.wait(15)
            return {'run_id':kwargs['run_id'],'status':'completed','workspace_id':'fixture','error_count':0}
        self.service.runner=runner;self.save('09:00','11:00')
        running=self.submit('upload');self.assertTrue(entered.wait(3))
        jobs=[self.submit('system' if index%2 else 'upload') for index in range(10)]
        for job in jobs:self.wait(job,{'downloaded'})
        self.service.request_immediate(jobs[0]['job_id'])
        self.save('10:45','11:30')
        self.assert_eta(jobs,10,45)
        self.assertFalse(self.service.get(jobs[0]['job_id']).get('force_now'))
        self.assertEqual(self.service.get(running['job_id'])['status'],'running')
        release.set();self.wait(running,{'completed'})
        for job in jobs:self.wait(job,{'waiting_offpeak'})
        self.assertEqual(len(calls),1)
        self.save('10:15','11:00');self.assert_eta(jobs,10,15)
        self.save('09:30','11:00')
        for job in jobs:self.wait(job,{'completed'})
        self.assertEqual(len(calls),11)
        self.assertEqual(len(set(calls)),11)

    def test_saved_schedule_supersedes_legacy_always_startup_setting(self):
        self.service.config=replace(self.service.config,offpeak_only=False)
        self.assertTrue(self.service.offpeak_status()['allowed_now'])
        response=self.save('10:30','11:30')
        self.assertFalse(response['status']['allowed_now'])
        self.assertFalse(response['status']['startup_override'])
        for source in ('system','upload'):
            self.wait(self.submit(source),{'waiting_offpeak'})
        self.assertEqual(self.fixture.harness.runs,[])

    def test_changed_schedule_revokes_immediate_grant_at_final_handoff(self):
        job=self.submit('upload');self.wait(job,{'waiting_offpeak'})
        entered,release=threading.Event(),threading.Event();self.addCleanup(release.set)
        original=self.service._begin_job_stage
        def delayed(submission):
            if threading.current_thread().name.startswith('offline-audit-oss-audit'):
                entered.set();release.wait(5)
            return original(submission)
        self.service._begin_job_stage=delayed
        self.service.request_immediate(job['job_id']);self.assertTrue(entered.wait(3))
        self.save('10:30','11:30');release.set()
        self.wait(job,{'waiting_offpeak'})
        self.assertFalse(self.service.get(job['job_id']).get('force_now'))
        self.assertEqual(self.fixture.harness.runs,[])


if __name__=='__main__':unittest.main()
