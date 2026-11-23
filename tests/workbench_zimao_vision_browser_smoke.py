"""紫猫云新型号的隔离浏览器回归；不触碰正式配置、业务或模型接口。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests import test_model_switching as model_tests
from audit_core.model_catalog import ZIMAO_DOUBAO_MODEL, ZIMAO_QWEN_MODEL
from audit_core.model_settings import DEFAULT_ZIMAO_MODEL, api_selection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', type=Path, required=True, help='工作区之外的验收目录')
    out = parser.parse_args().out.resolve()
    if out == ROOT or ROOT in out.parents:
        parser.error('验收目录必须位于项目工作区之外。')
    out.mkdir(parents=True, exist_ok=True)
    fixture = model_tests.ModelSettingsAPITests()
    fixture.setUp()
    probes, actions, errors = [], [], []

    def candidate(model):
        return api_selection({'provider': 'zimaocloud', 'model': model,
            'reasoning_effort': 'max', 'api_key': 'synthetic-browser-key'})

    def probe(selected):
        probes.append(selected.model)
        return {'model': selected.model, 'elapsed_seconds': 0,
                'response': {'number': '5274', 'color': 'blue'}}

    def model_post(response):
        return response.url.endswith('/api/model-settings') and response.request.method == 'POST'

    try:
        store = fixture.server.model_settings
        store.save(candidate(DEFAULT_ZIMAO_MODEL), expected_revision=0)
        original = store.path.read_bytes()
        with patch('audit_core.model_availability.fetch_candidate_models', return_value=[
                candidate(m) for m in (DEFAULT_ZIMAO_MODEL, ZIMAO_DOUBAO_MODEL, ZIMAO_QWEN_MODEL)]), \
             patch('audit_core.qwen_model.probe_qwen', side_effect=probe), sync_playwright() as p:
            browser = p.chromium.launch(channel='chrome', headless=True, args=['--no-proxy-server'])
            try:
                page = browser.new_page(viewport={'width': 1440, 'height': 960})
                page.route('**/*', lambda route: route.continue_()
                    if urlsplit(route.request.url).hostname == '127.0.0.1' else route.abort())
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: actions.append(request.post_data_json['action'])
                    if request.url.endswith('/api/model-settings') and request.method == 'POST' else None)
                page.goto(fixture.base, wait_until='networkidle')
                page.locator('#as-tab-model').click()
                expect(page.locator('#as-model-provider')).to_have_value('zimaocloud')
                expect(page.locator('#as-model-choice')).to_be_disabled()
                expect(page.locator('#as-model-activate')).to_be_disabled()
                expect(page.locator('#as-model-compatibility')).to_contain_text(ZIMAO_DOUBAO_MODEL)
                expect(page.locator('#as-model-compatibility')).to_contain_text(ZIMAO_QWEN_MODEL)
                assert not probes and store.path.read_bytes() == original
                with page.expect_response(model_post) as response:
                    page.locator('[data-as-model-action="refresh"]').click()
                assert response.value.status == 200
                expect(page.locator('#as-model-choice')).to_be_enabled()
                assert page.locator('#as-model-choice option').count() == 3
                assert len(probes) == 3 and store.path.read_bytes() == original
                for model in (ZIMAO_DOUBAO_MODEL, ZIMAO_QWEN_MODEL):
                    page.locator('#as-model-choice').select_option(model)
                    before = store.path.read_bytes()
                    with page.expect_response(model_post) as tested:
                        page.locator('[data-as-model-action="test"]').click()
                    assert tested.value.status == 200 and store.path.read_bytes() == before
                    expect(page.locator('#as-model-result')).to_contain_text(model)
                    expect(page.locator('#as-model-result')).to_contain_text('5274')
                    probe_count = len(probes)
                    with page.expect_response(model_post) as activated:
                        page.locator('#as-model-activate').click()
                    assert activated.value.status == 200 and len(probes) == probe_count
                    assert store.snapshot() == candidate(model)
                    expect(page.locator('#as-model-current')).to_contain_text(model)
                    expect(page.locator('#as-model-result')).to_be_hidden()
                    for width in (1440, 1280):
                        page.set_viewport_size({'width': width, 'height': 960})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), width
                        page.screenshot(path=str(out / f'{model}-{width}.png'), full_page=True)
                page.locator('#as-model-provider').select_option('doubao')
                expect(page.locator('#as-model-compatibility')).to_be_hidden()
                expect(page.locator('#as-model-choice')).to_be_disabled()
                legacy_settings = store.public()
                legacy_settings['profiles']['zimaocloud'].pop('compatible_model_ids')
                with patch.object(store, 'public', return_value=legacy_settings):
                    page.reload(wait_until='networkidle')
                    page.locator('#as-tab-model').click()
                    expect(page.locator('#as-model-compatibility')).to_be_hidden()
                assert actions == ['refresh', 'test', 'activate', 'test', 'activate'], actions
                assert not errors, errors
            finally:
                browser.close()
    finally:
        fixture.doCleanups()
    report = {'verified_models': [ZIMAO_DOUBAO_MODEL, ZIMAO_QWEN_MODEL],
        'http_actions': actions, 'synthetic_probes': probes, 'page_errors': errors,
        'viewports': [1440, 1280], 'test_preserves_configuration': True,
        'switch_never_probes': True, 'legacy_backend_does_not_claim_compatibility': True,
        'real_model_requests': 0, 'production_changes': 0}
    (out / 'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
