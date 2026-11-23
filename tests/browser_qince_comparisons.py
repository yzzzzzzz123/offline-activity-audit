"""Browser QA of saved Qince comparisons using only isolated synthetic archives."""
import argparse
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import threading
from urllib.parse import quote

from playwright.sync_api import sync_playwright

from audit_core.execution_trace import record_step
from audit_core.model_metrics import collect_model_artifacts
from audit_core.qince_context import APPLICATIONS, VERIFICATION, entry_context_sources
from audit_core.workbench_html import render_static_run_archive
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import WorkbenchRunStore
from tests.test_pass_check_details import PassCheckDetailsTests


def fixture():
    rows = [{'subId': str(9000000000000000001 + index), 'cusName': '隔离同名门店',
             'unitPrice': '500.00', 'quantity': '1', 'applyAmount': '500.00'} for index in range(2)]
    units, documents = [], []
    for index, row in enumerate(rows):
        facts = [f'{key}={json.dumps(value, ensure_ascii=False)}'
                 for key, value in {'verify_code': 'HX-ISOLATED', 'applyCode': 'SQ-ISOLATED', **row}.items()]
        units.append({'unit_id': f'a001-qince-{index + 1:04}', 'source_kind': 'qince',
                      'source_file': '勤策申请明细', 'locator': f'申请SQ-ISOLATED · 申请行{row["subId"]}',
                      'native_facts': facts, 'limitations': [], 'image': None})
        documents.append({'unit_id': units[-1]['unit_id'], 'facts': facts, 'roles': [], 'limitations': []})
    for uid, source, locator, facts in [
        ('a001-contract', '合同隔离1.png', '第1张原图', ['合同奖励单价500.00元；执行门店为隔离同名门店。']),
        ('a001-settlement', '结算隔离.pdf', '第2页', ['结算奖励单价500.00元；本条结算单已盖章。']),
        ('a001-photo', '现场隔离.xlsx', 'Sheet2!R6C3（浮动图片5）', ['水印：隔离同名门店；日期2026-09-19。']),
        ('a001-upload', '勤策上传截图.jpg', '第3张客户提交图', ['客户提交截图记载500.00元。']),
        ('a001-other', '不可串接隔离.png', '第9张原图', ['不可串接对象；同样记载500.00元。']),
    ]:
        units.append({'unit_id': uid, 'source_file': source, 'locator': locator, 'image': None,
                      'native_facts': [], 'limitations': []})
        documents.append({'unit_id': uid, 'facts': facts, 'roles': [], 'limitations': []})
    def operand(uid, quote_text, number='500.00'):
        return {'unit_id': uid, 'quote': quote_text, 'number': number}
    def numeric(rule, label, left, right):
        comparison = {'label': label, 'value_kind': 'unit_price', 'left': left, 'right': [right],
                      'operation': 'value', 'operator': 'eq'}
        return {'rule_id': rule, 'requirement': label, 'status': 'pass',
                'reason': label + '：两侧原值相等。', 'source_ids': [left['unit_id'], right['unit_id']],
                'comparisons': [comparison], 'calculations': [{**comparison, 'left_value': '500.00',
                                                              'right_value': '500.00', 'matches': True}]}
    qince = operand('a001-qince-0001', 'unitPrice="500.00"')
    checks = [
        numeric('contract_price_fixture', '隔离合同与勤策单价核对', qince,
                operand('a001-contract', '合同奖励单价500.00元')),
        numeric('claim_price_fixture', '隔离结算与勤策单价核对',
                operand('a001-settlement', '结算奖励单价500.00元'), qince),
        numeric('upload_fixture', '隔离上传截图比对', qince,
                operand('a001-upload', '客户提交截图记载500.00元')),
        {'rule_id': 'semantic_fixture', 'requirement': '隔离文字关联结论', 'status': 'unknown',
         'reason': '已引用这张结算单，但原记录未保存逐字段数值对应；不能据相同金额判断同一门店。',
         'source_ids': ['a001-qince-0001', 'a001-settlement'], 'comparisons': [], 'calculations': []},
        {'rule_id': 'other_row_fixture', 'requirement': '另一申请行独立结论', 'status': 'pass',
         'reason': '这是另一申请行的已保存结论，不属于第一行。',
         'source_ids': ['a001-qince-0002', 'a001-other'], 'comparisons': [], 'calculations': []},
    ]
    reviews = [{'unit_id': 'a001-photo', 'checks': [{
        'rule_id': 'display_watermark', 'requirement': '隔离逐图门店核对', 'status': 'unknown',
        'reason': '已保存本张水印及所引用的申请门店，尚不能确认本张具体门店对应。',
        'source_ids': ['a001-photo', 'a001-qince-0001'], 'comparisons': [],
        'source_assertions': [{'kind': 'application_customer', 'statement': '隔离本张门店名称采用已引用申请客户作为参照。',
            'references': [{'unit_id': 'a001-qince-0001', 'field': 'cusName', 'quote': 'cusName="隔离同名门店"'}]}],
    }]}]
    packet = {'archive_id': 'a001', 'units': units, 'documents': documents,
              'classification': {'materials': []},
              'audit_evidence': {'checks': [{**checks[0], 'reason': '旧模型原始结果不应成为最终结论'}]},
              'result': {'scenario': 'promotional_display', 'checks': checks, 'photo_reviews': reviews}}
    view, _ = PassCheckDetailsTests().fixture()
    return rows, packet, view


def create_archive(root, name, *, result_kind='result'):
    workspace = root / name
    workspace.mkdir()
    store = WorkbenchRunStore(workspace, run_id='20261109-' + name, producer_model='fixture',
                              root_html=ROOT_HTML, workbench_url='http://127.0.0.1/')
    rows, packet, view = fixture()
    if result_kind == 'audit_evidence':
        packet['audit_evidence'] = deepcopy(packet.pop('result'))
    elif result_kind == 'none':
        packet.pop('result')
        packet.pop('audit_evidence')
    store.write_analysis('pdf-policy/evidence.json', {'packets': [packet]}, label='隔离核对证据')
    with collect_model_artifacts(workspace / 'analysis', observer=store.observe):
        record_step('qince.request', '隔离申请行查询', {
            'endpoint': '/api/cuxiao/v1/queryRegularSaleActivities',
            'business_parameters': {'activity_code': 'SQ-ISOLATED', 'page_number': '1'},
        }, status='started', call_id='qince-fixture')
        record_step('qince.response', '隔离申请行返回', {
            'endpoint': '/api/cuxiao/v1/queryRegularSaleActivities',
            'business_parameters': {'activity_code': 'SQ-ISOLATED', 'page_number': '1'},
            'row_count': 1, 'rows': [{'applyCode': 'SQ-ISOLATED', 'subList': [rows[0]]}],
        }, call_id='qince-fixture')
        for row in rows:
            record_step('qince.linked', '隔离申请行关联', {
                'verify_code': 'HX-ISOLATED',
                'join_fields': ['核销details.activity_code = 申请applyCode',
                                '核销details.activity_cm_id = 申请subList.subId'],
                'linked_application_rows': [{'application_code': 'SQ-ISOLATED', 'row': row}],
            })
        record_step('ai.validated', '旧模型返回仅供原始过程追溯', {
            'validated_result': {'checks': [{'rule_id': 'obsolete', 'status': 'pass',
                'reason': '旧尝试结论不得混入勤策最终比对表',
                'source_ids': ['a001-qince-0001'], 'comparisons': []}]},
        })
    store.complete(view_payload=view, scenarios=['promotional_display'], verification={'isolated': True})
    return workspace


def create_verification_bridge_archive(root, name, *, aggregate=False):
    """No check cites the response's ordinary verification source directly."""
    workspace = root / name
    workspace.mkdir()
    store = WorkbenchRunStore(workspace, run_id='20261109-' + name, producer_model='fixture',
                              root_html=ROOT_HTML, workbench_url='http://127.0.0.1/')
    rows, packet, view = fixture()
    details = [{'id': str(9100000000000000001 + index), 'activity_code': 'SQ-ISOLATED',
                'activity_cm_id': rows[0 if index < 2 else 1]['subId'],
                'customer_id': 'CUSTOMER-1' if index < 2 else 'CUSTOMER-2',
                'customer_name': '隔离同名门店', 'init_amount': '500.00', 'audit_amount': '500.00'}
               for index in range(3)]
    if aggregate:
        for index, detail in enumerate(details):
            detail['customer_name'] = '隔离甲门店' if index < 2 else '隔离乙门店'
        application_rows = [{**row, 'cusId': f'CUSTOMER-{index + 1}', 'pdNames': '隔离SKU',
                             'cusName': '隔离甲门店' if index == 0 else '隔离乙门店'}
                            for index, row in enumerate(rows)]
        raw = {VERIFICATION: [{'verify_code': 'HX-ISOLATED', 'details': details}],
               APPLICATIONS: [{'applyCode': 'SQ-ISOLATED', 'subList': application_rows}]}
        grouped_units, grouped_documents = entry_context_sources(raw, 'HX-ISOLATED', 'a001')
        assert len(grouped_units) == 2
        packet['units'] = [unit for unit in packet['units'] if unit.get('source_kind') != 'qince'] + grouped_units
        packet['documents'] = [document for document in packet['documents']
                               if not document['unit_id'].startswith('a001-qince-')] + grouped_documents
        first_uid, other_uid = [unit['unit_id'] for unit in grouped_units]
        packet['result'] = {'scenario': 'entry_fee', 'checks': [
            {'rule_id': 'entry_agreement', 'requirement': '聚合进场范围核对', 'status': 'pass',
             'reason': '合同隔离1.png的门店范围与本单勤策进场关联明细核对。',
             'source_ids': [first_uid, 'a001-contract'], 'comparisons': []},
            {'rule_id': 'entry_sku', 'requirement': '聚合进场商品核对', 'status': 'unknown',
             'reason': '引用现场隔离.xlsx的本张图片及关联申请中的隔离SKU，未保存逐字段数值比较。',
             'source_ids': [first_uid, 'a001-photo'], 'comparisons': []},
            {'rule_id': 'entry_agreement', 'requirement': '聚合另一客户独立结论', 'status': 'pass',
             'reason': '同名另一客户有独立申请行，不属于本次返回的核销行。',
             'source_ids': [other_uid, 'a001-other'], 'comparisons': []},
            {'rule_id': 'entry_agreement', 'requirement': '跨店整项原始不符', 'status': 'fail',
             'reason': '另一门店协议名称不一致，保留原整项不符。',
             'source_ids': [first_uid, other_uid, 'a001-contract'], 'comparisons': []},
        ]}
        other_photo = {'unit_id': 'a001-other-photo', 'source_file': '别店照片不能串接.xlsx',
                       'locator': 'Sheet1!R2C3（浮动图片1）', 'image': None, 'native_facts': [], 'limitations': []}
        packet['units'].append(other_photo)
        packet['documents'].append({'unit_id': other_photo['unit_id'], 'roles': [], 'limitations': [],
                                    'facts': ['水印：隔离乙门店；日期2026-09-19。']})
        for document in packet['documents']:
            if document['unit_id'] == 'a001-photo':
                document['facts'] = ['水印：隔离甲门店；日期2026-09-19。']
        packet['result']['entry_coverage'] = {
            'scope_complete': True, 'scope_source_ids': [first_uid, other_uid], 'total_stores': None,
            'stores': [{'name': '隔离甲门店' if index == 0 else '隔离乙门店',
                        'source': {'unit_id': unit['unit_id'], 'quote': unit['native_facts'][1]}}
                       for index, unit in enumerate(grouped_units)],
            'photos': [{'unit_id': 'a001-photo', 'store_name': '隔离甲门店'},
                       {'unit_id': other_photo['unit_id'], 'store_name': '隔离乙门店'}], 'limitations': [],
        }
        foreign_unit = deepcopy(grouped_units[0])
        foreign_unit['unit_id'] = 'a002-qince-entry-0001'
    else:
        for detail in details:
            facts = [f'{key}={json.dumps(value, ensure_ascii=False)}'
                     for key, value in {'verify_code': 'HX-ISOLATED', **detail}.items()]
            unit = {'unit_id': 'a001-verification-' + detail['id'], 'source_kind': 'qince',
                    'source_file': '勤策核销明细', 'locator': '核销行' + detail['id'],
                    'native_facts': facts, 'limitations': [], 'image': None}
            packet['units'].append(unit)
            packet['documents'].append({'unit_id': unit['unit_id'], 'facts': facts, 'roles': [], 'limitations': []})
        foreign_unit = deepcopy(packet['units'][0])
        foreign_unit['unit_id'] = 'a002-qince-0001'
    foreign_unit['native_facts'] = [fact.replace('HX-ISOLATED', 'HX-FOREIGN')
                                    for fact in foreign_unit['native_facts']]
    foreign_document = {'unit_id': foreign_unit['unit_id'], 'facts': foreign_unit['native_facts'],
                        'roles': [], 'limitations': []}
    foreign_file = {'unit_id': 'a002-file', 'source_file': '跨核销同申请诱饵.png', 'locator': '第1张原图'}
    foreign_packet = {'archive_id': 'a002', 'units': [foreign_unit, foreign_file],
        'documents': [foreign_document, {'unit_id': foreign_file['unit_id'],
                                      'facts': ['同申请、同申请行、同名称、同金额，但所属核销单不同。']}],
        'result': {'checks': [{'rule_id': 'foreign', 'requirement': '不同核销单不得串接', 'status': 'pass',
                              'reason': '仅属于HX-FOREIGN。',
                              'source_ids': [foreign_unit['unit_id'], foreign_file['unit_id']], 'comparisons': []}]}}
    store.write_analysis('pdf-policy/evidence.json', {'packets': [packet, foreign_packet]}, label='隔离核销行桥接证据')
    with collect_model_artifacts(workspace / 'analysis', observer=store.observe):
        parameters = {'verify_code': 'HX-ISOLATED', 'page': '1', 'rows': '100'}
        endpoint = '/api/cuxiao/verify/v1/queryVerificationRegular'
        record_step('qince.request', '隔离核销明细查询', {'endpoint': endpoint, 'business_parameters': parameters},
                    status='started', call_id='verification-fixture')
        record_step('qince.response', '隔离核销明细返回', {'endpoint': endpoint, 'business_parameters': parameters,
                    'rows': [{'verify_code': 'HX-ISOLATED', 'details': details[:2]}], 'row_count': 1},
                    call_id='verification-fixture')
        orphan = {**details[0], 'id': '9100000000000000099'}
        orphan.pop('activity_cm_id')
        record_step('qince.response', '隔离缺关联键核销行返回', {'endpoint': endpoint, 'business_parameters': parameters,
                    'rows': [{'verify_code': 'HX-ISOLATED', 'details': [orphan]}], 'row_count': 1})
    store.complete(view_payload=view, scenarios=['entry_fee' if aggregate else 'promotional_display'],
                   verification={'isolated': True})
    return workspace


def show_technical(page, url):
    page.goto(url, wait_until='networkidle')
    if page.locator('#as-record-technical').is_visible():
        page.locator('#as-record-technical').click()
    else:
        page.locator('#as-tab-archive').click()
        page.locator('[data-as-technical]').first.click()
    page.wait_for_selector('li.as-chain-step:not([hidden]) details.as-chain-operation')
    operations = page.locator('li.as-chain-step:not([hidden]) details.as-chain-operation')
    assert operations.evaluate_all('nodes => nodes.every(node => !node.open)'), 'Operations should initially be collapsed'
    first = operations.first
    first.locator(':scope > summary').click()
    assert first.locator(':scope > .as-chain-body').is_visible()
    first.locator(':scope > summary').click()
    assert first.locator(':scope > .as-chain-body').is_hidden()
    operations.evaluate_all(
        'nodes => nodes.forEach(node => {node.open = true; node.dispatchEvent(new Event("toggle"));})')


def step_evidence(page, sequence):
    return page.locator(f'[data-as-trace="analysis/execution-trace/step-{sequence:08}.json"] '
                        '[data-as-customer-evidence]')


def assert_final_comparisons(page):
    first = step_evidence(page, 3)
    comparisons = first.locator('[data-as-qince-comparisons]')
    comparisons.get_by_text('隔离合同与勤策单价核对', exact=False).first.wait_for()
    text = comparisons.inner_text()
    for required in ('合同隔离1.png', '第1张原图', '结算隔离.pdf', '第2页', '500.00',
                     '合同奖励单价500.00元', '结算奖励单价500.00元', '隔离文字关联结论',
                     '不能据相同金额判断同一门店', '现场隔离.xlsx', 'Sheet2!R6C3（浮动图片5）'):
        assert required in text, (required, text)
    for forbidden in ('不可串接隔离.png', '另一申请行独立结论', '旧模型原始结果不应成为最终结论',
                      '旧尝试结论不得混入勤策最终比对表'):
        assert forbidden not in text, (forbidden, text)
    assert any(word in text for word in ('等于', '相等')), text
    assert any(word in text for word in ('逐字段', '逐值')), text
    for label in ('隔离合同与勤策单价核对', '隔离结算与勤策单价核对'):
        assert comparisons.locator('tbody tr').filter(has_text=label).count() == 1, 'Saved comparisons must not duplicate calculations'
    uploaded = comparisons.locator('tbody tr').filter(has_text='隔离上传截图比对')
    assert '勤策上传截图.jpg' not in uploaded.locator('td').nth(0).inner_text(), 'A submitted filename is not a system source'
    assert '勤策上传截图.jpg' in uploaded.locator('td').nth(1).inner_text()
    photo = comparisons.locator('tbody tr').filter(has_text='隔离逐图门店核对')
    assert '申请客户名称' in photo.locator('td').nth(0).inner_text()
    assert 'cusName="隔离同名门店"' in photo.locator('td').nth(0).inner_text()
    second = step_evidence(page, 4)
    second.get_by_text('另一申请行独立结论', exact=False).first.wait_for()
    second_text = second.inner_text()
    assert '不可串接隔离.png' in second_text, second_text
    for forbidden in ('合同隔离1.png', '结算隔离.pdf', '现场隔离.xlsx'):
        assert forbidden not in second_text, second_text
    response = step_evidence(page, 1).locator('[data-as-qince-comparisons]')
    response.get_by_text('隔离合同与勤策单价核对', exact=False).first.wait_for()
    assert '不可串接隔离.png' not in response.inner_text()
    return first, text


def assert_verification_bridge(page, *, aggregate=False):
    evidence = step_evidence(page, 1)
    comparisons = evidence.locator('[data-as-qince-comparisons]')
    label = '聚合进场范围核对' if aggregate else '隔离合同与勤策单价核对'
    comparisons.get_by_text(label, exact=False).first.wait_for()
    text = comparisons.inner_text()
    for required in ('核销行', '进场关联来源' if aggregate else '申请行', '9100000000000000001', '9100000000000000002',
                     '9000000000000000001', 'SQ-ISOLATED', '合同隔离1.png', '现场隔离.xlsx'):
        assert required in text, (required, text)
    for forbidden in ('跨核销同申请诱饵.png', '不同核销单不得串接', '不可串接隔离.png',
                      '聚合另一客户独立结论', '另一申请行独立结论'):
        assert forbidden not in text, (forbidden, text)
    assert comparisons.locator('tbody tr').filter(has_text=label).count() == 1, 'Two linked verification rows must not duplicate one saved check'
    if aggregate:
        assert '勤策进场关联明细' in text
        assert '聚合进场商品核对' in text
        photos = comparisons.locator('.as-customer-evidence-group').filter(has_text='本次查询具体对照的照片')
        assert photos.count() == 1
        photo_text = photos.inner_text()
        assert '隔离甲门店' in photo_text and '现场隔离.xlsx' in photo_text
        assert 'Sheet2!R6C3（浮动图片5）' in photo_text
        assert '隔离乙门店' not in photo_text and '别店照片不能串接.xlsx' not in photo_text
        cross_store = comparisons.locator('tbody tr').filter(has_text='跨店整项原始不符')
        assert '跨多条勤策来源的整项结论' in cross_store.inner_text()
        assert '不能当作本记录单独结论' in cross_store.inner_text()
        assert '核对不符' in cross_store.inner_text()
        assert '另一门店协议名称不一致，保留原整项不符。' in cross_store.inner_text()
    else:
        numeric = comparisons.locator('tbody tr').filter(has_text='隔离合同与勤策单价核对')
        assert 'unitPrice="500.00"' in numeric.inner_text()
        assert 'init_amount=' not in numeric.inner_text() and 'audit_amount=' not in numeric.inner_text(), 'An application comparison cannot become verification-amount comparison'
    assert evidence.evaluate('''node => {
        const compared = node.querySelector('[data-as-qince-comparisons]');
        const fields = [...node.querySelectorAll('.as-customer-evidence-group')].find(item => !item.closest('[data-as-qince-comparisons]'));
        return Boolean(compared && fields && (compared.compareDocumentPosition(fields) & Node.DOCUMENT_POSITION_FOLLOWING));
    }'''), 'Actual comparison objects must precede the returned field table'
    orphan = step_evidence(page, 3).locator('[data-as-qince-comparisons]')
    orphan.wait_for()
    for forbidden in ('合同隔离1.png', '现场隔离.xlsx', '跨核销同申请诱饵.png', '不可串接隔离.png'):
        assert forbidden not in orphan.inner_text(), 'Missing relation key must not fall back to matching name/amount'
    return evidence


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    errors, requests, checks = [], [], []
    html_before = ROOT_HTML.read_bytes()
    with tempfile.TemporaryDirectory(prefix='qince-comparison-browser-') as directory:
        root = Path(directory)
        workspace = create_archive(root, 'qince-comparison-final')
        audit_only = create_archive(root, 'qince-comparison-audit-only', result_kind='audit_evidence')
        no_final = create_archive(root, 'qince-comparison-without-final', result_kind='none')
        bridges = [(create_verification_bridge_archive(root, 'qince-verification-bridge'), False),
                   (create_verification_bridge_archive(root, 'qince-entry-aggregate', aggregate=True), True)]
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
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.on('request', lambda request: requests.append(request.url))
                for width in (1440, 1280):
                    page.set_viewport_size({'width': width, 'height': 960})
                    show_technical(page, base + '/?run=' + quote(workspace.name))
                    first, _ = assert_final_comparisons(page)
                    first.scroll_into_view_if_needed()
                    page.screenshot(path=str(args.output / f'qince-comparisons-{width}.png'), full_page=True)
                    checks.append({'width': width, 'passed': True,
                                   'exact_application_row_scope': True,
                                   'both_operand_directions': True, 'final_result_only': True,
                                   'photo_review_source_locator': True, 'reason_only_is_not_field_pass': True,
                                   'source_assertion_originals': True, 'response_step_exact_scope': True,
                                   'default_collapsed_and_click_expansion': True,
                                   'no_duplicated_calculation_rows': True, 'submitted_filename_not_system_source': True})
                show_technical(page, base + '/?run=' + quote(audit_only.name))
                assert_final_comparisons(page)
                checks.append({'passed': True, 'validated_audit_evidence_legacy_fallback': True})
                show_technical(page, base + '/?run=' + quote(no_final.name))
                first = step_evidence(page, 3)
                first.get_by_text('9000000000000000001', exact=False).first.wait_for()
                text = first.inner_text()
                assert '隔离合同与勤策单价核对' not in text
                assert '旧模型原始结果不应成为最终结论' not in text
                assert '旧尝试结论不得混入勤策最终比对表' not in text
                checks.append({'passed': True, 'no_final_result_does_not_borrow_model_attempt': True})
                for bridge, aggregate in bridges:
                    show_technical(page, base + '/?run=' + quote(bridge.name))
                    evidence = assert_verification_bridge(page, aggregate=aggregate)
                    evidence.locator('[data-as-qince-comparisons]').scroll_into_view_if_needed()
                    page.screenshot(path=str(args.output / ('qince-entry-aggregate.png' if aggregate else 'qince-verification-bridge.png')), full_page=True)
                    checks.append({'passed': True, 'verification_response_exact_bridge': True,
                                   'grouped_entry_source': aggregate, 'same_application_other_verification_excluded': True,
                                   'same_value_other_row_excluded': True, 'two_verification_rows_one_check': True,
                                   'missing_join_key_not_guessed': True, 'comparison_before_raw_fields': True,
                                   **({'explicit_store_photo_mapping_only': True, 'cross_store_check_status_scope_preserved': True}
                                      if aggregate else {})})
                archive = render_static_run_archive(workspace, ROOT_HTML)
                page.context.set_offline(True)
                show_technical(page, archive.as_uri())
                assert_final_comparisons(page)
                checks.append({'passed': True, 'offline_static_archive': True})
                browser.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
        assert originals == {path: path.read_bytes() for path in originals}, 'Saved evidence/history mutated'
    assert not errors, errors
    assert all(url.startswith(base) or url.startswith('file:') for url in requests), requests
    receipt = {'passed': True, 'cases': checks, 'page_errors': errors, 'external_requests': 0,
               'real_model_calls': 0, 'history_unchanged': True,
               'html_start_sha256': hashlib.sha256(html_before).hexdigest(),
               'html_end_sha256': hashlib.sha256(ROOT_HTML.read_bytes()).hexdigest()}
    (args.output / 'browser-verification.json').write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(receipt, ensure_ascii=False))


if __name__ == '__main__':
    main()
