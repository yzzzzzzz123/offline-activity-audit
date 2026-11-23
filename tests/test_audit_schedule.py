"""Schedule persistence, boundary semantics, and real HTTP-to-queue behavior.

All materials, clocks and runners are isolated; no model, OSS or callback calls.
"""
from copy import deepcopy
from datetime import timedelta, timezone
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

from audit_core.audit_schedule import (
    AuditScheduleStore, ScheduleSettingsConflict, ScheduleSettingsError, default_settings,
)
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from tests import test_offpeak_gate as gate_tests
from tests.test_offpeak_gate import MONDAY, moment


def custom(weekly=None, **changes):
    value = {"revision": 0, **default_settings(), "mode": "custom"}
    value["weekly"] = weekly if weekly is not None else [[["10:00", "11:00"]] for _ in range(7)]
    return {**value, **changes}


class ScheduleStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = AuditScheduleStore(self.root)

    def test_default_is_read_only_and_preserves_official_behavior(self):
        value = self.store.describe(moment(MONDAY, 10))
        self.assertFalse(self.store.path.exists())
        self.assertFalse(value['allowed_now'])
        self.assertEqual(value['next_offpeak_label'], '今天 12:00')

    def test_saved_custom_schedule_survives_a_new_store(self):
        self.store.save(custom())
        reopened = AuditScheduleStore(self.root)
        self.assertEqual(reopened.public()['revision'], 1)
        self.assertTrue(reopened.describe(moment(MONDAY, 10))['allowed_now'])
        self.assertFalse(reopened.describe(moment(MONDAY, 11))['allowed_now'])
        self.assertEqual(reopened.describe(moment(MONDAY, 11))['next_offpeak_label'], '明天 10:00')

    def test_overnight_sunday_wraps_into_monday_with_exclusive_end(self):
        weekly = [[] for _ in range(7)]
        weekly[6] = [['22:00', '06:00']]
        self.store.save(custom(weekly))
        self.assertTrue(self.store.describe(moment(MONDAY, 5, 59))['allowed_now'])
        self.assertFalse(self.store.describe(moment(MONDAY, 6))['allowed_now'])
        self.assertEqual(self.store.describe(moment(MONDAY, 5))['weekly'][0]['windows'], [['00:00', '06:00']])

    def test_next_opening_can_be_six_days_away(self):
        weekly = [[] for _ in range(7)]
        weekly[6] = [['22:00', '23:00']]
        self.store.save(custom(weekly))
        value = self.store.describe(moment(MONDAY, 10))
        self.assertEqual(value['next_offpeak_start'], moment(MONDAY + timedelta(days=6), 22).isoformat())

    def test_same_instant_in_utc_uses_beijing_schedule(self):
        self.store.save(custom())
        self.assertTrue(self.store.describe(moment(MONDAY, 10).astimezone(timezone.utc))['allowed_now'])

    def test_holiday_override_is_explicit_and_applies_in_official_mode(self):
        self.store.save(custom(holidays=[MONDAY.date().isoformat()]))
        self.assertTrue(self.store.describe(moment(MONDAY, 14))['allowed_now'])
        payload = self.store.public()
        payload['holiday_all_day'] = False
        self.store.save(payload)
        self.assertFalse(self.store.describe(moment(MONDAY, 14))['allowed_now'])
        payload = self.store.public()
        payload['mode'] = 'official'
        self.store.save(payload)
        self.assertTrue(self.store.describe(moment(MONDAY, 14))['allowed_now'])

    def test_all_day_and_startup_override_are_reported_honestly(self):
        self.store.save(custom(mode='always'))
        self.assertTrue(self.store.describe(moment(MONDAY, 10))['allowed_now'])
        self.assertFalse(self.store.describe(moment(MONDAY, 10))['enabled'])
        forced = self.store.describe(moment(MONDAY, 10), forced_always=True)
        self.assertTrue(forced['startup_override'])

    def test_conflicting_revision_does_not_overwrite_saved_schedule(self):
        self.store.save(custom())
        before = self.store.path.read_bytes()
        with self.assertRaises(ScheduleSettingsConflict):
            self.store.save(custom(mode='always'))
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_invalid_or_overlapping_values_are_rejected_before_writing(self):
        invalid = [custom(mode=[]), custom(weekly=[[]] * 7), custom(holidays=['2026-02-30']),
                   custom(holiday_all_day=1), custom(revision=True)]
        for windows in ([['25:00', '26:00']], [['12:00', '12:00']], [['09:00', '12:00'], ['11:00', '13:00']]):
            invalid.append(custom(weekly=[windows] + [[] for _ in range(6)]))
        overnight = [[] for _ in range(7)]
        overnight[0] = [['22:00', '06:00']]
        overnight[1] = [['05:00', '08:00']]
        invalid.append(custom(overnight))
        for value in invalid:
            with self.subTest(value=value):
                with self.assertRaises(ScheduleSettingsError):
                    self.store.save(value)
                self.assertFalse(self.store.path.exists())

    def test_corrupt_saved_file_fails_closed(self):
        self.store.path.parent.mkdir(parents=True)
        for value in ('[]', '{', '{"revision":true}'):
            self.store.path.write_text(value, encoding='utf-8')
            with self.assertRaises(ScheduleSettingsError):
                self.store.describe(moment(MONDAY, 10))

    def test_failed_atomic_write_keeps_previous_version(self):
        self.store.save(custom())
        before = self.store.path.read_bytes()
        with mock.patch('audit_core.audit_schedule.atomic_write_json', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.store.save({**self.store.public(), 'mode': 'always'})
        self.assertEqual(self.store.path.read_bytes(), before)


class ScheduleHTTPQueueTests(unittest.TestCase):
    def setUp(self):
        self.harness = gate_tests.OffpeakGateTests()
        self.harness.setUp()
        self.addCleanup(self.harness.doCleanups)
        self.service = self.harness.service
        self.server = WorkbenchHTTPServer(('127.0.0.1', 0), Handler,
            catalog=WorkbenchCatalog(self.service.worktrees_root), intake=self.service)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.base = 'http://127.0.0.1:' + str(self.server.server_address[1])
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(5)

    def request(self, payload=None, *, origin=None, token=True):
        headers = {'Content-Type': 'application/json', 'Origin': origin or self.base}
        if token:
            headers['X-Offline-Audit-Schedule-Token'] = self.server.schedule_settings_token
        request = urllib.request.Request(self.base + '/api/audit-schedule', headers=headers,
            data=json.dumps(payload).encode('utf-8') if payload is not None else None)
        with self.opener.open(request, timeout=5) as response:
            return json.load(response)

    def test_http_save_releases_waiting_job_and_persists(self):
        job = self.harness._submit()
        self.harness._wait_for(job['job_id'], {'waiting_offpeak'})
        response = self.request(custom())
        self.assertTrue(response['status']['allowed_now'])
        self.harness._wait_for(job['job_id'], {'completed'})
        self.assertEqual(len(self.harness.runs), 1)
        self.assertEqual(self.request()['settings']['mode'], 'custom')
        self.assertEqual(AuditScheduleStore(self.service.worktrees_root).public()['revision'], 1)

    def test_changed_closed_window_updates_waiting_eta_without_model(self):
        job = self.harness._submit()
        self.harness._wait_for(job['job_id'], {'waiting_offpeak'})
        self.request(custom(weekly=[[['22:00', '23:00']]] * 7))
        self.assertEqual(self.service.get(job['job_id'])['offpeak_next_start'], moment(MONDAY, 22).isoformat())
        self.assertEqual(self.harness.runs, [])

    def test_http_rejects_foreign_origin_missing_token_and_invalid_settings(self):
        for kwargs in ({'origin': 'http://evil.invalid'}, {'token': False}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.request(custom(), **kwargs)
            self.assertEqual(error.exception.code, 403)
            error.exception.close()
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(custom(mode=[]))
        self.assertEqual(error.exception.code, 400)
        error.exception.close()
        self.assertEqual(self.request()['settings']['revision'], 0)

    def test_stale_browser_gets_conflict_and_preserves_current_values(self):
        self.request(custom())
        with self.assertRaises(urllib.error.HTTPError) as error:
            self.request(custom(mode='always'))
        self.assertEqual(error.exception.code, 409)
        error.exception.close()
        self.assertEqual(self.request()['settings']['mode'], 'custom')

    def test_schedule_edit_does_not_interrupt_started_runner(self):
        started, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)
        self.harness.now[0] = moment(MONDAY, 8)
        def runner(**kwargs):
            started.set()
            release.wait(5)
            return {'run_id': kwargs['run_id'], 'status': 'completed', 'workspace_id': 'fixture', 'error_count': 0}
        self.service.runner = runner
        job = self.harness._submit()
        self.assertTrue(started.wait(5))
        self.request(custom(weekly=[[['22:00', '23:00']]] * 7))
        self.assertEqual(self.service.get(job['job_id'])['status'], 'running')
        release.set()
        self.harness._wait_for(job['job_id'], {'completed'})


if __name__ == '__main__':
    unittest.main()
