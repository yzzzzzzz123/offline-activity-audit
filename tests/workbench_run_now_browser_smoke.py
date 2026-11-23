"""Exercise immediate controls with real HTTP/queue and an isolated fake runner."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT), str(ROOT / 'skills/orchestrate-offline-audit/scripts')]
from audit_core.oss_intake import OSSIntakeConfig, OSSIntakeService
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from tests.test_local_upload import archive


def wait_until(check):
    deadline = time.monotonic() + 10
    while not check():
        assert time.monotonic() < deadline, 'isolated queue did not reach the expected state'
        time.sleep(.02)


def exercise(out: Path, *, busy: bool):
    release = threading.Event()
    calls, posts, errors = [], [], []

    def runner(**kwargs):
        calls.append(kwargs['run_id'])
        assert release.wait(90), 'isolated runner was not released'
        return {'run_id': kwargs['run_id'], 'workspace_id': 'fixture',
                'status': 'completed', 'error_count': 0}

    def forbidden(*args, **kwargs):
        raise AssertionError('a browser regression must not call an external service')

    with tempfile.TemporaryDirectory(dir=out) as temporary:
        files = Path(temporary)
        service = OSSIntakeService(
            worktrees_root=files / 'worktrees', input_root=files / 'input',
            config=OSSIntakeConfig(allowed_hosts=('example.test',), producer_model='codex',
                                   callback_required=False, audit_workers=1),
            runner=runner, downloader=forbidden, callback_sender=forbidden,
            clock=lambda: datetime(2026, 11, 18, 10, tzinfo=timezone(timedelta(hours=8))),
        )
        server = WorkbenchHTTPServer(('127.0.0.1', 0), Handler,
            catalog=WorkbenchCatalog(files / 'worktrees'), intake=service)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            jobs = []
            raw = archive()
            for index in range(3):
                job, _ = service.submit_upload(io.BytesIO(raw), length=len(raw),
                    filename=f'isolated-{index}.zip', biz_type='搭赠', request_id=f'{index + 1:024x}')
                jobs.append(job['job_id'])
                if busy and index == 0:
                    wait_until(lambda: service.get(jobs[0])['status'] == 'waiting_offpeak')
                    service.request_immediate(jobs[0])
                    wait_until(lambda: len(calls) == 1)
            wait_until(lambda: all(service.get(job)['status'] == (
                'running' if busy and index == 0 else 'downloaded' if busy else 'waiting_offpeak'
            ) for index, job in enumerate(jobs)))
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=['--no-proxy-server'])
                try:
                    page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                    page.on('pageerror', lambda error: errors.append(str(error)))
                    page.on('request', lambda request: posts.append(urlsplit(request.url).path)
                            if request.method == 'POST' and request.url.endswith('/run-now') else None)
                    page.goto(f'http://127.0.0.1:{server.server_port}', wait_until='networkidle')
                    bulk = page.locator('#as-run-all-now')
                    expect(bulk).to_have_text(f'全部立即分析（{2 if busy else 3}）')
                    expect(bulk).to_be_enabled()
                    assert bulk.locator('xpath=ancestor::header').count() == 1
                    for width in (1440, 1280, 850, 390):
                        page.set_viewport_size({'width': width, 'height': 1000})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                        expect(bulk).to_be_visible()
                        header = page.locator('#as-intake-panel > header').bounding_box()
                        button = bulk.bounding_box()
                        assert header['x'] <= button['x'] and button['x'] + button['width'] <= header['x'] + header['width'] + 1
                        assert header['y'] <= button['y'] and button['y'] + button['height'] <= header['y'] + header['height'] + 1
                        if width >= 850:
                            heading = page.locator('#as-intake-panel h2').bounding_box()
                            assert button['x'] > heading['x'] + heading['width'] / 2
                        page.locator('#as-intake-panel').screenshot(path=str(out / f'{"busy" if busy else "parked"}-{width}.png'))
                    page.set_viewport_size({'width': 1440, 'height': 1000})

                    # A catalog outage must not undo the queue acknowledgement.
                    page.route('**/api/runs', lambda route: route.fulfill(status=503,
                        content_type='application/json', body='{"error":"isolated catalog outage"}'))
                    if not busy:
                        selector = f'[data-intake-job="{jobs[0]}"] [data-as-run-now]'
                        page.evaluate('(selector) => {const button=document.querySelector(selector);button.click();button.click();}', selector)
                        expect(page.locator(f'[data-intake-job="{jobs[0]}"] [data-as-run-now]')).to_have_count(0)
                        wait_until(lambda: len(calls) == 1)
                        assert posts.count(f'/api/intake/jobs/{jobs[0]}/run-now') == 1

                        # One failed item stays eligible; retry sends only that item.
                        failed_path = f'/api/intake/jobs/{jobs[1]}/run-now'
                        fail_once = [True]
                        def intermittent(route):
                            if fail_once[0]:
                                fail_once[0] = False
                                route.fulfill(status=503, content_type='application/json', body='{"error":"isolated temporary failure"}')
                            else:
                                route.continue_()
                        page.route('**' + failed_path, intermittent)
                        page.evaluate('() => {const button=document.getElementById("as-run-all-now");button.click();button.click();}')
                        expect(bulk).to_have_text('全部立即分析（1）')
                        expect(bulk).to_be_enabled()
                        assert service.get(jobs[2])['force_now'] is True
                        assert not service.get(jobs[1]).get('force_now')
                        assert posts.count(f'/api/intake/jobs/{jobs[2]}/run-now') == 1
                        bulk.click()
                    else:
                        bulk.click()
                    expect(bulk).to_be_disabled()
                    expect(bulk).to_have_text('全部立即分析')
                    expect(page.locator('#as-run-all-note')).to_have_text('单据已放行，有核销空位后依次开始')
                    expect(page.locator('.as-intake-progress strong').filter(has_text='立即分析排队中')).to_have_count(2)
                    assert all(service.get(job)['force_now'] is True for job in jobs)
                    assert len(calls) == 1 and service.get(jobs[0])['status'] == 'running'
                    assert '/api/intake/jobs/run-now' not in posts
                    if busy:
                        assert len(posts) == 2 and all(posts.count(path) == 1 for path in posts)
                    else:
                        assert len(posts) == 4 and posts.count(f'/api/intake/jobs/{jobs[0]}/run-now') == 1
                    assert not errors, errors
                    page.unroute('**/api/runs')
                    page.wait_for_timeout(100)
                    return {'case': 'busy-worker-downloaded' if busy else 'parked-partial-retry',
                            'post_count': len(posts), 'runner_calls': len(calls), 'errors': errors,
                            'viewports': [1440, 1280, 850, 390], 'catalog_failure_did_not_block': True}
                finally:
                    browser.close()
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            service.close()
            thread.join(5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, required=True, help='Task output directory outside the source workspace')
    out = parser.parse_args().out.resolve()
    assert not out.is_relative_to(ROOT.parent), 'test artifacts must remain outside D:/codex'
    out.mkdir(parents=True, exist_ok=True)
    results = [exercise(out, busy=False), exercise(out, busy=True)]
    (out / 'run-now-browser-checks.json').write_text(json.dumps(results, ensure_ascii=False, indent=2), 'utf-8')
    print(json.dumps({'passed': True, 'checks': results}, ensure_ascii=False))


if __name__ == '__main__':
    main()
