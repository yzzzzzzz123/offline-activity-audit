"""Verify independent model actions through real browser/HTTP, with no remote calls."""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from unittest.mock import patch

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_model_switching import ModelSettingsAPITests
from audit_core.model_settings import api_selection
from audit_core.qwen_model import QwenRequestError


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--out', type=Path, default=ROOT / 'artifacts/model-actions-20261026/browser')
    out = parser.parse_args().out
    out.mkdir(parents=True, exist_ok=True)
    fixture = ModelSettingsAPITests()
    fixture.setUp()
    probes = []
    posts = []
    page_errors = []
    fail_probe = False

    def probe(selection):
        probes.append({'provider': selection.provider, 'model': selection.model})
        if fail_probe:
            raise QwenRequestError('隔离测试：模型暂时不可用', 'auth')
        return {'provider': selection.provider, 'model': selection.model,
                'elapsed_seconds': 0, 'response': {'number': '5274', 'color': 'blue'}}

    def is_model_response(response):
        return response.url.endswith('/api/model-settings') and response.request.method == 'POST'

    def candidates(selection):
        names = ('qwen/qwen3.8-max', 'moonshotai/kimi-k3') if selection.provider == 'ppio' else ('qwen3-vl-plus',)
        return [replace(selection, model=name) for name in names]

    try:
        store = fixture.server.model_settings
        store.save(api_selection({'provider': 'ppio', 'api_key': 'sk_browser_fixture_not_real'}),
                   expected_revision=0)
        for candidate in candidates(store.snapshot()):
            store.availability.record(candidate, passed=True)
        initial = store.path.read_bytes()
        with patch('audit_core.qwen_model.probe_qwen', side_effect=probe), \
             patch('audit_core.model_availability.fetch_candidate_models', side_effect=candidates), sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            page = browser.new_page(viewport={'width': 1440, 'height': 960})
            page.on('pageerror', lambda error: page_errors.append(str(error)))
            page.on('request', lambda request: posts.append(request.post_data_json['action'])
                    if request.url.endswith('/api/model-settings') and request.method == 'POST' else None)
            page.goto(fixture.base, wait_until='networkidle')
            page.locator('#as-tab-model').click()
            expect(page.locator('#as-model-provider')).to_have_count(0)
            expect(page.locator('#as-model-url')).to_have_count(0)
            expect(page.locator('#as-model-choice')).to_be_enabled()
            assert page.locator('#as-model-choice option').count() == 2
            expect(page.locator('#as-model-name')).to_have_count(0)
            expect(page.locator('#as-model-choice option[value="__custom__"]')).to_have_count(0)
            for text in ('服务平台', 'PPIO', '派欧云', '百炼', '接口地址'):
                assert text not in page.locator('#as-view-model').inner_text(), text
            for width in (1440, 1280, 390):
                page.set_viewport_size({'width': width, 'height': 960})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
            page.set_viewport_size({'width': 1440, 'height': 960})
            page.locator('#as-model-key').fill('browser_bailian_fixture_not_real')
            expect(page.locator('#as-model-choice')).to_be_disabled()
            expect(page.locator('#as-model-activate')).to_be_disabled()
            with page.expect_response(is_model_response) as refreshed:
                page.locator('[data-as-model-action="refresh"]').click()
            assert refreshed.value.status == 200
            expect(page.locator('#as-model-choice')).to_have_value('qwen3-vl-plus')
            assert page.locator('#as-model-choice option').count() == 1
            assert store.path.read_bytes() == initial and len(probes) == 1

            with page.expect_response(is_model_response) as tested:
                page.locator('[data-as-model-action="test"]').click()
            assert tested.value.status == 200
            expect(page.locator('#as-model-result')).to_be_visible()
            expect(page.locator('#as-model-result')).to_contain_text('5274')
            expect(page.locator('#as-model-current')).to_contain_text('qwen3.8-max')
            expect(page.locator('#as-model-choice')).to_have_value('qwen3-vl-plus')
            expect(page.locator('#as-model-key')).to_have_value('browser_bailian_fixture_not_real')
            assert store.path.read_bytes() == initial and len(probes) == 2
            page.screenshot(path=str(out / 'test-only.png'), full_page=True)

            with page.expect_response(is_model_response) as switched:
                page.locator('#as-model-activate').click()
            assert switched.value.status == 200 and 'test' not in switched.value.json()
            expect(page.locator('#as-model-current')).to_contain_text('qwen3-vl-plus')
            expect(page.locator('#as-model-result')).to_be_hidden()
            expect(page.locator('#as-model-result')).to_have_text('')
            expect(page.locator('#as-model-key')).to_have_value('')
            assert store.public()['revision'] == 2 and len(probes) == 2
            saved = store.path.read_bytes()
            page.screenshot(path=str(out / 'switch-only.png'), full_page=True)

            fail_probe = True
            with page.expect_response(is_model_response) as failed:
                page.locator('[data-as-model-action="test"]').click()
            assert failed.value.status == 502
            expect(page.locator('#as-model-message')).to_have_attribute('data-error', 'true')
            expect(page.locator('#as-model-result')).to_be_hidden()
            expect(page.locator('#as-model-current')).to_contain_text('qwen3-vl-plus')
            assert store.path.read_bytes() == saved and len(probes) == 3
            expect(page.locator('#as-model-choice')).to_be_disabled()
            expect(page.locator('#as-model-activate')).to_be_disabled()
            expect(page.locator('#as-model-choice option[value="qwen3-vl-plus"]')).to_have_count(0)
            fail_probe = False
            with page.expect_response(is_model_response):
                page.locator('[data-as-model-action="refresh"]').click()
            expect(page.locator('#as-model-choice')).to_be_enabled()
            assert store.path.read_bytes() == saved and len(probes) == 4

            page.locator('#as-model-key').fill('sk_browser_fixture_not_real')
            with page.expect_response(is_model_response):
                page.locator('[data-as-model-action="refresh"]').click()
            page.locator('#as-model-choice').select_option('qwen3.8-max')
            with page.expect_response(is_model_response) as switched_back:
                page.locator('#as-model-activate').click()
            assert switched_back.value.status == 200 and 'test' not in switched_back.value.json()
            expect(page.locator('#as-model-current')).to_contain_text('qwen3.8-max')
            expect(page.locator('#as-model-result')).to_be_hidden()
            assert store.public()['revision'] == 3 and len(probes) == 6
            page.locator('#as-model-choice').select_option('kimi-k3')
            with page.expect_response(is_model_response) as kimi_test:
                page.locator('[data-as-model-action="test"]').click()
            assert kimi_test.value.status == 200
            expect(page.locator('#as-model-result')).to_contain_text('kimi-k3')
            assert probes[-1]['model'] == 'moonshotai/kimi-k3'
            assert store.public()['revision'] == 3 and len(probes) == 7
            for text in ('服务平台', 'PPIO', '派欧云', '百炼', '接口地址', '平台：'):
                assert text not in page.locator('#as-view-model').inner_text(), text
            assert posts == ['refresh', 'test', 'activate', 'test', 'refresh', 'refresh', 'activate', 'test'], posts
            assert not page_errors, page_errors
            browser.close()
    finally:
        fixture.doCleanups()
    report = {'http_actions': posts, 'isolated_provider_probes': probes,
              'test_preserves_configuration': True, 'switch_never_probes': True,
              'failed_test_preserves_configuration': True, 'page_errors': page_errors,
              'real_model_requests': 0, 'production_changes': 0}
    (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
