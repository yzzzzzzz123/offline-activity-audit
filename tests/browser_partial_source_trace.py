"""Isolated browser regression for source/provenance before final evidence exists.

All archives, product rows and model observations in this test are synthetic.
No source business service, database, OSS or model is contacted.
"""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import parse_qs, quote, urlparse

from playwright.sync_api import sync_playwright

from audit_core.execution_trace import record_step
from audit_core.model_metrics import collect_model_artifacts, save_model_observations
from audit_core.workbench_html import render_static_run_archive
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import WorkbenchRunStore
from tests.test_pass_check_details import PassCheckDetailsTests


PRODUCT = 'CP-KQ-YG-0205'
BARCODE = '6970356166832'
LONG_ID = 6731026748309494745
NAME = '参半隔离桂雨牙膏100g'
UID = 'a001-f0001-table-0001'
BATCH_UID = 'a001-f0002-table-0001'
UNKNOWN_UID = 'a001-unmapped-9000000000000000001'


def source_row(uid, name, barcode, code, row_number, *, status='pass'):
    quote_text = f'销售Sheet!R{row_number}C1=商品名称={name}；R{row_number}C2=69码={barcode}'
    return {
        'unit_id': uid, 'status': status,
        'submitted': {'product_name': name, 'barcode_69': barcode},
        'source': {'fact_indexes': [1],
                   'product_name': {'fact_index': 1, 'quote': quote_text, 'value': name},
                   'barcode_69': {'fact_index': 1, 'quote': quote_text, 'value': barcode}},
        'references': [{'product_code': code, 'product_name': name, 'barcode_69': barcode}],
        'reason': '本行名称及69码与商品主数据库快照一致；这是商品库核对记录。',
    }


def create_archive(root, name, *, status='failed', final=False, extra_sources=False, application=False):
    workspace = root / name
    workspace.mkdir()
    store = WorkbenchRunStore(workspace, run_id='20261109-' + name,
                              producer_model='fixture', root_html=ROOT_HTML,
                              workbench_url='http://127.0.0.1/')
    inventory = [
        {'file_id': 'a001-f0001', 'source_file': '隔离POS.xlsx', 'kind': 'spreadsheet'},
        {'file_id': 'a001-f0002', 'source_file': '批次POS.xlsx', 'kind': 'spreadsheet'},
        {'file_id': 'a001-f0003', 'source_file': '同名错误商品.xlsx', 'kind': 'spreadsheet'},
    ]
    if extra_sources:
        inventory += [
            {'file_id': 'a001-f0004', 'source_file': '原始转账凭证.jpg',
             'kind': 'visual', 'sha256': 'a' * 64},
            {'file_id': 'a001-f0005', 'source_file': '压缩原行POS.xlsx', 'kind': 'spreadsheet'},
        ]
    cases = {'personnel_incentive': {'archive_id': 'a001', 'scenario': 'personnel_incentive',
              'source_archive': '隔离来源测试.zip', 'inventory': inventory}}
    store.observe('cases.prepared', {'cases': cases, 'scenarios': ['personnel_incentive']})
    primary = source_row(UID, NAME, BARCODE, PRODUCT, 7)
    batch = source_row(BATCH_UID, NAME, BARCODE, PRODUCT, 11)
    wrong = source_row('a001-f0003-table-0001', NAME, '6970356166899', 'CP-WRONG-9999', 13)
    unknown = source_row(UNKNOWN_UID, NAME, BARCODE, PRODUCT, 19)
    facts = ['销售Sheet!R1C1=商品名称；R1C2=69码', primary['source']['product_name']['quote']]
    batch_facts = ['销售Sheet!R1C1=商品名称；R1C2=69码', batch['source']['product_name']['quote']]
    products = [
        {'prd_code': PRODUCT, 'prd_name': NAME, 'id': LONG_ID,
         'prd_waiqin365_id': LONG_ID, 'prd_spec': '100g',
         'prd_units': [{'prd_barcode': BARCODE, 'prd_is_base': '1'}]},
        {'prd_code': 'CP-NOT-IN-POS-1000', 'prd_name': NAME,
         'id': LONG_ID + 1, 'prd_units': [{'prd_barcode': BARCODE, 'prd_is_base': '1'}]},
        {'prd_name': NAME, 'id': LONG_ID + 2, 'prd_spec': '100g'},
        {'prd_code': PRODUCT, 'prd_name': NAME, 'id': LONG_ID + 3,
         'prd_units': [{'prd_barcode': '6970356166800', 'prd_is_base': '1'},
                       {'prd_barcode': BARCODE, 'prd_is_base': '0'}]},
    ]
    steps = {}
    with collect_model_artifacts(workspace / 'analysis', observer=store.observe):
        # Product queries are deliberately earlier than source/identity records.
        for index, product in enumerate(products):
            meta = record_step('qince.response', f'隔离商品查询第{index + 1}条', {
                'endpoint': '/api/pd/v2/queryProduct',
                'business_parameters': {'prd_code': product.get('prd_code', '')},
                'rows': [product], 'row_count': 1,
            })
            steps[('product', 'wrong_product', 'name_only', 'wrong_base_barcode')[index]] = meta['path']
        meta = record_step('ai.request', '隔离POS原行提取', {
            'model': 'fixture', 'attempt': 1, 'images': [],
            'input_text': '当前来源unit_id：' + UID
                + '\n同一文件的分段开头（仅解释表头）：[]'
                + '\n本段facts（下标从0开始）：' + json.dumps(facts, ensure_ascii=False)
                + '\n原读取限制：[]',
        }, status='started', call_id='source-extraction')
        steps['request'] = meta['path']
        record_step('ai.validated', '隔离POS原行提取完成', {
            'validated_result': {'rows': [primary['source']], 'other_facts': []},
            'attempt': 1,
        }, call_id='source-extraction')
        record_step('ai.request', '隔离批次带原始事实', {
            'model': 'fixture', 'attempt': 1, 'images': [],
            'input_text': '本批动态口径与已验证程序事实：{}\n本批完整对应原始事实：'
                + json.dumps([{'unit_id': BATCH_UID, 'facts': batch_facts,
                               'roles': ['pos_detail'], 'limitations': []}], ensure_ascii=False),
        }, status='started', call_id='source-batch')
        observation_rows = [primary, batch, wrong, unknown]
        if extra_sources:
            compressed_row = source_row('a001-f0005-table-0001', NAME, BARCODE, PRODUCT, 27)
            compressed_row['source']['product_name']['quote'] = f'压缩Sheet!R27: C1={NAME} | C2={BARCODE}'
            compressed_row['source']['barcode_69']['quote'] = compressed_row['source']['product_name']['quote']
            encoded = {
                'encoding': '无损原文片段编码：隔离合成语料',
                'fragments': ['压缩Sheet!R', f'C1={NAME}', f'C2={BARCODE}',
                              '压缩Sheet!R1: C1=商品名称 | C2=69码'],
                'document_metadata': [{'roles': ['pos_detail'], 'limitations': []}],
                'documents': [{'unit_id': compressed_row['unit_id'], 'metadata': 0,
                               'facts': [3, [0, 27, [1, 2]]]}],
            }
            record_step('ai.request', '隔离压缩事实输入', {
                'model': 'fixture', 'images': [],
                'input_text': '本批完整对应原始事实：' + json.dumps(encoded, ensure_ascii=False),
            }, status='started', call_id='compressed-input')
            observation_rows.append(compressed_row)
            record_step('ai.request', '隔离图片指纹输入', {'model': 'fixture', 'images': [
                {'file': 'image-contenthash.jpg', 'sha256': 'a' * 64, 'size_bytes': 125},
                {'file': 'image-wronghash.jpg', 'sha256': 'b' * 64, 'size_bytes': 125},
            ]}, status='started', call_id='image-source')
            meta = record_step('ai.validated', '隔离图片原始事实', {'validated_result': {
                'documents': [
                    {'unit_id': 'image-contenthash', 'facts': ['正确图片指纹来源：付款金额500元']},
                    {'unit_id': 'image-wronghash', 'facts': ['不同图片指纹，不可串为转账凭证']},
                ],
            }}, call_id='image-source')
            steps['image_result'] = meta['path']
        save_model_observations('pos-product-identity-a001', {
            'data_source': {'name': '隔离商品主数据库'},
            'verification': {'applicable': True, 'rows': observation_rows,
                             'limitations': []},
        })
        # Trace records retain intermediate output, but it must not override final evidence.
        record_step('ai.validated', '隔离中间检查', {'validated_result': {
            'checks': [{'rule_id': 'intermediate-only', 'status': 'pass',
                        'requirement': '中间记录不能变成最终结论',
                        'reason': '中间通过不得覆盖最终核对结论', 'source_ids': [UID]}],
        }})
        if application:
            record_step('qince.linked', '隔离商品对应申请行', {
                'verify_code': 'HX-READ-FAILURE',
                'linked_application_rows': [{'application_code': 'SQ-READ-FAILURE', 'row': {
                    'subId': '9000000000000000027', 'cusName': '读取失败隔离门店',
                    'pdCodes': PRODUCT, 'pdNames': NAME,
                }}],
            })
    if final:
        authoritative = {'unit_id': UID, 'source_file': '终版定位POS.xlsx',
                         'locator': '终版Sheet，第7行', 'native_facts': [], 'image': None,
                         'limitations': []}
        store.write_analysis('pdf-policy/evidence.json', {'packets': [{
            'archive_id': 'a001', 'units': [authoritative],
            'documents': [{'unit_id': UID, 'facts': ['最终保存的原行事实'],
                           'roles': [], 'limitations': []}],
            'audit_evidence': {'checks': [{'rule_id': 'final-scope', 'status': 'pass',
                                         'reason': '废弃旧证据结论不得覆盖最终结果', 'source_ids': [UID]}]},
            'result': {'checks': [{'rule_id': 'final-scope', 'status': 'fail',
                                  'requirement': '最终核对范围', 'source_ids': [UID],
                                  'reason': '最终核对未通过，商品库单行通过不能替代整单结论'}]},
        }]}, label='隔离最终证据')
    if status == 'failed':
        store.fail(RuntimeError('隔离任务在最终证据生成前停止，未调用真实模型'))
    elif status == 'complete':
        view, _ = PassCheckDetailsTests().fixture()
        store.complete(view_payload=view, scenarios=['personnel_incentive'], verification={'isolated': True})
    else:
        store.observe('scenario.started', {'scenario': 'personnel_incentive'})
    observation = next(path for path in (workspace / 'analysis/execution-trace').glob('*.json')
                       if json.loads(path.read_text(encoding='utf-8'))['type'] == 'analysis.observation')
    steps['observation'] = observation.relative_to(workspace).as_posix()
    return workspace, steps


def show_technical(page, url, *, wait='networkidle'):
    page.goto(url, wait_until=wait)
    if page.locator('#as-record-technical').is_visible():
        page.locator('#as-record-technical').click()
    else:
        page.locator('#as-tab-archive').click()
        page.locator('[data-as-technical]').first.click()
    page.wait_for_selector('li.as-chain-step:not([hidden]) details.as-chain-operation')
    operations = page.locator('li.as-chain-step:not([hidden]) details.as-chain-operation')
    assert operations.evaluate_all('nodes => nodes.every(node => !node.open)'), 'Steps default collapsed'
    operations.evaluate_all('nodes => nodes.forEach(node => {node.open=true;node.dispatchEvent(new Event("toggle"));})')


def evidence(page, path):
    return page.locator(f'[data-as-trace="{path}"] [data-as-customer-evidence]')


def require_text(locator, text):
    locator.get_by_text(text, exact=False).first.wait_for(timeout=15000)
    return locator.inner_text()


def check_partial(page, steps, *, final=False):
    product = evidence(page, steps['product'])
    counterpart = product.locator('[data-as-qince-comparisons]')
    expected_source = '终版定位POS.xlsx' if final else '隔离POS.xlsx'
    text = require_text(counterpart, expected_source)
    for required in (expected_source, '第7行' if final else 'R7', '批次POS.xlsx', 'R11', BARCODE, PRODUCT):
        assert required in text, (required, text)
    assert 'Sheet' in text
    assert '同名错误商品.xlsx' not in text, text
    assert 'CP-WRONG-9999' not in text, text
    assert '中间通过不得覆盖最终核对结论' not in text, text
    assert '废弃旧证据结论不得覆盖最终结果' not in text, text
    assert '本单未留存可供对应的核对证据文件' not in text
    assert '商品库' in text
    assert any(scope in text for scope in ('不等于', '不代表', '不能替代', '未保存', '尚未')), (
        'Existing POS database pass must not imply a saved Qince comparison or whole-case pass')
    assert str(LONG_ID) in product.inner_text(), product.inner_text()
    for key in ('wrong_product', 'name_only', 'wrong_base_barcode'):
        other = evidence(page, steps[key]).locator('[data-as-qince-comparisons]')
        other.wait_for()
        for forbidden in ('隔离POS.xlsx', '批次POS.xlsx', '终版定位POS.xlsx', '同名错误商品.xlsx'):
            assert forbidden not in other.inner_text(), (key, other.inner_text())
    observation = evidence(page, steps['observation'])
    observation_text = require_text(observation, expected_source)
    assert UNKNOWN_UID in observation_text, observation_text
    assert '本次资料（详细位置未留存）' not in observation_text, observation_text
    assert ('第7行' if final else 'R7') in observation_text and 'R11' in observation_text
    if final:
        primary_row = observation.locator('tbody tr').filter(has_text='终版定位POS.xlsx').first
        assert '终版Sheet' in primary_row.inner_text()
        assert '隔离POS.xlsx' not in primary_row.inner_text()
    return counterpart


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    errors, requests, checks = [], [], []
    html_before = ROOT_HTML.read_bytes()
    with tempfile.TemporaryDirectory(prefix='partial-source-browser-') as directory:
        root = Path(directory)
        failed, failed_steps = create_archive(root, 'partial-failed')
        running, running_steps = create_archive(root, 'partial-running', status='running')
        final, final_steps = create_archive(root, 'partial-final', status='complete', final=True)
        extra, extra_steps = create_archive(root, 'partial-image-and-compression', extra_sources=True)
        read_failure, read_failure_steps = create_archive(root, 'partial-read-failure', application=True)
        originals = {path: path.read_bytes() for path in root.rglob('*.json')}
        server = WorkbenchHTTPServer(('127.0.0.1', 0), Handler, catalog=WorkbenchCatalog(root))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f'http://127.0.0.1:{server.server_address[1]}'
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(channel='chrome', headless=True,
                                                       args=['--no-proxy-server'])
                page = browser.new_page(viewport={'width': 1440, 'height': 960})
                page.on('pageerror', lambda error: (errors.append(str(error)), print('PAGEERROR:', error, flush=True)))
                page.on('request', lambda request: requests.append(request.url))
                for workspace, steps, is_final in ((failed, failed_steps, False),
                                                   (running, running_steps, False),
                                                   (final, final_steps, True)):
                    show_technical(page, base + '/?run=' + quote(workspace.name))
                    counterpart = check_partial(page, steps, final=is_final)
                    counterpart.scroll_into_view_if_needed()
                    page.screenshot(path=str(args.output / (workspace.name + '.png')), full_page=True)
                    checks.append({'passed': True, 'fixture': workspace.name,
                                   'sources_without_final_evidence': not is_final,
                                   'file_sheet_and_row_visible': True, 'product_exact_scope': True,
                                   'same_barcode_wrong_code_excluded': True,
                                   'same_code_wrong_base_barcode_excluded': True,
                                   'same_name_not_linked': True, 'long_id_preserved': True,
                                   'unknown_source_id_preserved': True,
                                   'database_pass_scope_not_promoted': True,
                                   'final_evidence_has_priority': is_final})
                # Hold only inventory loading; trace/observation returns first.
                delayed = browser.new_page(viewport={'width': 1280, 'height': 960})
                delayed.on('pageerror', lambda error: errors.append(str(error)))
                delayed.on('request', lambda request: requests.append(request.url))
                delayed.add_init_script('''(() => {
                    const original=window.fetch.bind(window);
                    window.__sourceProbe={held:true,waiting:0,resume:[]};
                    window.fetch=async (...args)=>{
                        const url=new URL(String(args[0]),location.href);
                        if(url.searchParams.get('path')==='analysis/input-cases.json') {
                            const probe=window.__sourceProbe;probe.waiting++;
                            if(probe.held)await new Promise(resolve=>probe.resume.push(resolve));
                        }
                        return original(...args);
                    };
                })();''')
                show_technical(delayed, base + '/?run=' + quote(failed.name), wait='domcontentloaded')
                delayed.wait_for_function('window.__sourceProbe.waiting>0', timeout=15000)
                require_text(evidence(delayed, failed_steps['observation']), BARCODE)
                assert not evidence(delayed, failed_steps['observation']).get_by_text('隔离POS.xlsx', exact=False).count()
                delayed.evaluate('window.__sourceProbe.held=false;window.__sourceProbe.resume.splice(0).forEach(resolve=>resolve())')
                check_partial(delayed, failed_steps)
                dimensions = delayed.evaluate('({width:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth})')
                assert dimensions['scrollWidth'] <= dimensions['width'], dimensions
                delayed.screenshot(path=str(args.output / 'partial-late-input-1280.png'), full_page=True)
                checks.append({'passed': True, 'late_inventory_rehydrates_existing_trace': True,
                               'facts_and_observation_arrive_before_inventory': True,
                               'width': 1280, 'no_horizontal_overflow': True})
                delayed.close()
                show_technical(page, base + '/?run=' + quote(extra.name))
                counterpart = check_partial(page, extra_steps)
                compressed = counterpart.locator('tbody tr').filter(has_text='压缩原行POS.xlsx')
                compressed_text = require_text(compressed, '压缩Sheet!R27:')
                assert f'C1={NAME} | C2={BARCODE}' in compressed_text
                assert '[0,27' not in compressed_text and 'fragments' not in compressed_text
                images = evidence(page, extra_steps['image_result'])
                correct_image = images.locator('tbody tr').filter(has_text='正确图片指纹来源：')
                assert '原始转账凭证.jpg' in require_text(correct_image, '原始转账凭证.jpg')
                wrong_image = images.locator('tbody tr').filter(has_text='不同图片指纹')
                assert '原始转账凭证.jpg' not in wrong_image.inner_text()
                assert 'image-wronghash.jpg' in wrong_image.inner_text()
                images.scroll_into_view_if_needed()
                page.screenshot(path=str(args.output / 'partial-image-hash-and-compressed-facts.png'), full_page=True)
                checks.append({'passed': True, 'image_inventory_sha256_source_bridge': True,
                               'different_image_hash_not_linked': True,
                               'compressed_facts_losslessly_decoded_to_original_row': True})
                failures = browser.new_page(viewport={'width': 1440, 'height': 960})
                failures.on('pageerror', lambda error: errors.append(str(error)))
                failures.on('request', lambda request: requests.append(request.url))
                blocked = set()

                def fail_saved_source(route):
                    resource = parse_qs(urlparse(route.request.url).query).get('path', [''])[0]
                    if resource in blocked:
                        route.fulfill(status=503, json={'error': 'Isolated saved source temporarily unreadable'})
                    else:
                        route.continue_()

                failures.route('**/api/runs/partial-read-failure/analysis?*', fail_saved_source)
                for source_kind, target in (('inventory', 'analysis/input-cases.json'),
                                            ('observation', read_failure_steps['observation'])):
                    blocked.add(target)
                    show_technical(failures, base + '/?run=' + quote(read_failure.name))
                    failed_counterpart = evidence(failures, read_failure_steps['product']).locator('[data-as-qince-comparisons]')
                    failure_text = require_text(failed_counterpart, '暂时未读到')
                    assert 'SQ-READ-FAILURE' in require_text(failed_counterpart, 'SQ-READ-FAILURE')
                    assert '未留存' not in failure_text
                    assert '未保存可精确关联' not in failure_text
                    assert '不能据此判断其余记录没有留存' in failure_text
                    failed_counterpart.scroll_into_view_if_needed()
                    failures.screenshot(path=str(args.output / f'partial-{source_kind}-http-failure.png'), full_page=True)
                    blocked.clear()
                    # Reopen the same archive without a document reload to verify cache reset.
                    failures.keyboard.press('Escape')
                    failures.locator('#as-record-technical').click()
                    operations = failures.locator('li.as-chain-step:not([hidden]) details.as-chain-operation')
                    operations.first.wait_for()
                    operations.evaluate_all('nodes => nodes.forEach(node => {node.open=true;node.dispatchEvent(new Event("toggle"));})')
                    recovered = check_partial(failures, read_failure_steps)
                    failures.wait_for_function('''path => {
                        const node=document.querySelector('[data-as-trace="'+path+'"] [data-as-qince-comparisons]');
                        return node && !node.textContent.includes('暂时未读到');
                    }''', arg=read_failure_steps['product'], timeout=15000)
                    assert 'SQ-READ-FAILURE' in recovered.inner_text()
                    checks.append({'passed': True, 'http_read_failure_source': source_kind,
                                   'failure_is_distinct_from_missing_history': True,
                                   'warning_visible_alongside_saved_application': True,
                                   'reopening_recovers_source_and_clears_failure': True})
                failures.close()
                archive = render_static_run_archive(failed, ROOT_HTML)
                page.context.set_offline(True)
                show_technical(page, archive.as_uri())
                check_partial(page, failed_steps)
                checks.append({'passed': True, 'failed_archive_offline_sources': True})
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert originals == {path: path.read_bytes() for path in originals}, 'Saved histories mutated'
    assert not errors, errors
    assert all(url.startswith(base) or url.startswith('file:') for url in requests), requests
    receipt = {'passed': True, 'cases': checks, 'page_errors': errors,
               'external_requests': 0, 'real_model_calls': 0, 'history_unchanged': True,
               'html_start_sha256': hashlib.sha256(html_before).hexdigest(),
               'html_end_sha256': hashlib.sha256(ROOT_HTML.read_bytes()).hexdigest()}
    (args.output / 'browser-verification.json').write_text(
        json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
