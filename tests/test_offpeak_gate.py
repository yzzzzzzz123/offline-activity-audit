"""New audits start only inside DeepSeek's off-peak window; running ones never stop."""
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from audit_core import offpeak
from audit_core.oss_intake import OSSIntakeConfig, OSSIntakeConflictError, OSSIntakeService
from tests import test_oss_intake as intake_tests

MONDAY = datetime(2026, 11, 16)
SATURDAY = datetime(2026, 11, 21)
try:
    BEIJING = ZoneInfo('Asia/Shanghai')
except ZoneInfoNotFoundError:
    # Windows test environments may omit the optional tzdata package. The
    # production release gate still requires real Asia/Shanghai tzdata.
    BEIJING = timezone(timedelta(hours=8))


def moment(day: datetime, hour: int, minute: int = 0) -> datetime:
    return day.replace(hour=hour, minute=minute, tzinfo=BEIJING)


class OffpeakWindowTests(unittest.TestCase):
    def test_peak_hours_follow_the_official_deepseek_table(self):
        self.assertTrue(offpeak.is_peak(moment(MONDAY, 9, 0)))
        self.assertTrue(offpeak.is_peak(moment(MONDAY, 11, 59)))
        self.assertFalse(offpeak.is_peak(moment(MONDAY, 8, 59)))
        self.assertFalse(offpeak.is_peak(moment(MONDAY, 12, 0)))
        self.assertFalse(offpeak.is_peak(moment(MONDAY, 13, 59)))
        self.assertTrue(offpeak.is_peak(moment(MONDAY, 14, 0)))
        self.assertTrue(offpeak.is_peak(moment(MONDAY, 17, 59)))
        self.assertFalse(offpeak.is_peak(moment(MONDAY, 18, 0)))
        self.assertFalse(offpeak.is_peak(moment(MONDAY, 23, 30)))
        self.assertFalse(offpeak.is_peak(moment(MONDAY, 0, 5)))

    def test_weekends_and_configured_holidays_are_offpeak_all_day(self):
        self.assertFalse(offpeak.is_peak(moment(SATURDAY, 10, 0)))
        holiday = MONDAY.date()
        self.assertFalse(offpeak.is_peak(moment(MONDAY, 10, 0), holidays=frozenset({holiday})))
        self.assertTrue(offpeak.is_peak(moment(MONDAY, 10, 0), holidays=frozenset()))

    def test_next_start_skips_to_the_nearest_offpeak_minute(self):
        self.assertEqual(offpeak.next_offpeak_start(moment(MONDAY, 9, 30)),
                         moment(MONDAY, 12, 0))
        self.assertEqual(offpeak.next_offpeak_start(moment(MONDAY, 17, 30)),
                         moment(MONDAY, 18, 0))
        self.assertEqual(offpeak.next_offpeak_start(moment(MONDAY, 12, 30)),
                         moment(MONDAY, 12, 30))

    def test_status_document_names_the_official_window(self):
        value = offpeak.describe(moment(MONDAY, 10, 0))
        self.assertTrue(value['peak_now'])
        self.assertFalse(value['offpeak_now'])
        self.assertEqual(value['next_offpeak_label'], '今天 12:00')
        self.assertEqual(value['seconds_to_next_offpeak'], 2 * 60 * 60)
        self.assertIn('09:00-12:00', value['window'])
        self.assertIn('DeepSeek', value['source'])
        quiet = offpeak.describe(moment(MONDAY, 20, 0))
        self.assertTrue(quiet['offpeak_now'])
        self.assertIsNone(quiet['next_offpeak_start'])
        self.assertIsNone(quiet['seconds_to_next_offpeak'])


class OffpeakGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.now = [moment(MONDAY, 10, 0)]
        self.runs: list[dict] = []
        self.service = self._service()

    def _service(self, *, offpeak_only: bool = True, runner=None) -> OSSIntakeService:
        worktrees = self.root / 'worktrees'
        worktrees.mkdir(exist_ok=True)
        service = OSSIntakeService(
            worktrees_root=worktrees,
            input_root=self.root / 'input',
            config=OSSIntakeConfig(
                allowed_hosts=(intake_tests.OSS_HOST,),
                producer_model='codex',
                workbench_url='http://127.0.0.1:8080/',
                callback_required=False,
                audit_workers=1,
                offpeak_only=offpeak_only,
            ),
            downloader=intake_tests._fake_downloader,
            runner=runner or self._runner,
            url_validator=intake_tests._no_network_url_validator,
            callback_sender=lambda payload, config: {'attempts': 1, 'http_status': 200},
            clock=lambda: self.now[0],
        )
        self.addCleanup(service.close)
        return service

    def _runner(self, **kwargs):
        self.runs.append(kwargs)
        return {'run_id': kwargs['run_id'], 'status': 'completed',
                'workspace_id': 'fixture', 'error_count': 0}

    def _submit(self, service=None):
        job, created = (service or self.service).submit(intake_tests._payload())
        self.assertTrue(created)
        return job

    def _wait_for(self, job_id: str, statuses: set[str], *, service=None,
                  timeout: float = 10.0) -> dict:
        service = service or self.service
        deadline = time.monotonic() + timeout
        job = service.get(job_id)
        while time.monotonic() < deadline:
            job = service.get(job_id)
            if str(job.get('status')) in statuses:
                return job
            time.sleep(0.02)
        self.fail(f'job stayed in {job.get("status")!r}, expected one of {statuses}')

    def test_peak_job_is_parked_before_any_model_call(self):
        job = self._submit()
        parked = self._wait_for(job['job_id'], {'waiting_offpeak'})
        self.assertEqual(self.runs, [])
        self.assertEqual(parked['offpeak_next_start'],
                         moment(MONDAY, 12, 0).isoformat())
        self.assertIn('09:00-12:00', parked['offpeak_window'])
        self.assertEqual(self.service.offpeak_status()['waiting'], 1)
        self.assertTrue(self.service.offpeak_status()['peak_now'])

    def test_parked_job_starts_when_the_window_opens(self):
        job = self._submit()
        self._wait_for(job['job_id'], {'waiting_offpeak'})
        self.now[0] = moment(MONDAY, 12, 0)
        self.assertEqual(self.service._release_offpeak_jobs(), [job['job_id']])
        self._wait_for(job['job_id'], {'completed'})
        self.assertEqual(len(self.runs), 1)

    def test_a_started_job_keeps_running_after_the_window_closes(self):
        self.now[0] = moment(MONDAY, 8, 59)
        started, release = threading.Event(), threading.Event()

        def runner(**kwargs):
            self.runs.append(kwargs)
            started.set()
            release.wait(10)
            return {'run_id': kwargs['run_id'], 'status': 'completed',
                    'workspace_id': 'fixture', 'error_count': 0}

        service = self._service(runner=runner)
        job = self._submit(service)
        self.assertTrue(started.wait(10), 'the audit never started')
        # A peak window opens while the formal run is still in flight.
        self.now[0] = moment(MONDAY, 9, 30)
        self.assertEqual(service._release_offpeak_jobs(), [])
        release.set()
        self._wait_for(job['job_id'], {'completed'}, service=service)
        self.assertEqual(len(self.runs), 1)
        finished = service.get(job['job_id'])
        self.assertEqual(str(finished['status']), 'completed')
        self.assertIsNone(finished.get('failure'))

    def test_offpeak_window_allows_an_immediate_start(self):
        self.now[0] = moment(MONDAY, 20, 0)
        job = self._submit()
        self._wait_for(job['job_id'], {'completed'})
        self.assertEqual(len(self.runs), 1)

    def test_operator_override_starts_a_parked_job_right_away(self):
        job = self._submit()
        self._wait_for(job['job_id'], {'waiting_offpeak'})
        started = self.service.request_immediate(job['job_id'])
        self.assertTrue(started['force_now'])
        self._wait_for(job['job_id'], {'completed'})
        self.assertEqual(len(self.runs), 1)
        self.assertEqual(self.service.offpeak_status()['waiting'], 0)

    def test_operator_override_releases_every_parked_job(self):
        first = self._submit()
        second, created = self.service.submit(
            intake_tests._payload(verify_code='HX202608310002', analyze_id=124))
        self.assertTrue(created)
        for job in (first, second):
            self._wait_for(job['job_id'], {'waiting_offpeak'})
        released = self.service.request_immediate_all()
        self.assertEqual({item['job_id'] for item in released},
                         {first['job_id'], second['job_id']})
        for job in (first, second):
            self._wait_for(job['job_id'], {'completed'})
        self.assertEqual(len(self.runs), 2)
        self.assertEqual(self.service.offpeak_status()['waiting'], 0)
        self.assertEqual(self.service.request_immediate_all(), [])

    def test_override_is_rejected_for_a_finished_job(self):
        self.now[0] = moment(MONDAY, 20, 0)
        job = self._submit()
        self._wait_for(job['job_id'], {'completed'})
        with self.assertRaises(OSSIntakeConflictError):
            self.service.request_immediate(job['job_id'])

    def test_disabled_gate_starts_everything_immediately(self):
        service = self._service(offpeak_only=False)
        job = self._submit(service)
        self._wait_for(job['job_id'], {'completed'}, service=service)
        self.assertEqual(len(self.runs), 1)
        self.assertFalse(service.offpeak_status()['enabled'])

    def test_restart_requeues_parked_jobs_instead_of_failing_them(self):
        job = self._submit()
        self._wait_for(job['job_id'], {'waiting_offpeak'})
        self.service.close()
        restarted = self._service()
        again = self._wait_for(job['job_id'], {'waiting_offpeak'}, service=restarted)
        self.assertIsNone(again.get('failure'))
        self.assertEqual(self.runs, [])
        self.now[0] = moment(MONDAY, 18, 0)
        restarted._release_offpeak_jobs()
        self._wait_for(job['job_id'], {'completed'}, service=restarted)
        self.assertEqual(len(self.runs), 1)

    def test_public_job_projection_keeps_the_waiting_state_visible(self):
        from audit_core.oss_intake import public_job
        job = self._submit()
        parked = self._wait_for(job['job_id'], {'waiting_offpeak'})
        value = public_job(parked)
        self.assertEqual(value['status'], 'waiting_offpeak')
        self.assertTrue(value['offpeak_next_start'])
        self.assertNotIn('idempotency_fingerprint', json.dumps(value))


if __name__ == '__main__':
    unittest.main()
