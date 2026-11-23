"""Browser QA of the real server/renderer against an isolated synthetic archive."""
import argparse
import json
import re
from pathlib import Path
import tempfile
import threading
from urllib.parse import quote, unquote, urlparse
from playwright.sync_api import sync_playwright
from audit_core.model_metrics import collect_model_artifacts, save_model_observations
from audit_core.execution_trace import record_step
from audit_core.workbench_store import WorkbenchRunStore
from audit_core.workbench_server import WorkbenchCatalog, WorkbenchHTTPServer, Handler
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_html import render_static_run_archive
from tests.test_pass_check_details import PassCheckDetailsTests


def open_operation(step):
    operation=step.locator(':scope > details.as-chain-operation[data-as-operation]')
    if operation.get_attribute('open') is None:
        operation.locator(':scope > summary').click()
    return operation


def assert_default_operations(page, minimum=3):
    page.wait_for_function("""minimum=>{
        const nodes=[...document.querySelectorAll('li.as-chain-step:not([hidden])[data-as-trace]')];
        return nodes.length>=minimum && nodes.every(node=>node.querySelector(':scope > details.as-chain-operation > summary h3')?.getClientRects().length);
    }""",arg=minimum)
    steps=page.locator('li.as-chain-step:not([hidden])[data-as-trace]')
    # Legacy metadata hydrates asynchronously and can regroup visible steps.
    # Read one coherent DOM snapshot rather than indexing a changing locator.
    rows=steps.evaluate_all("""nodes=>nodes.map(node=>{
        const operations=node.querySelectorAll(':scope > details.as-chain-operation[data-as-operation]');
        const operation=operations[0],summary=operation?.querySelector(':scope > summary h3');
        return {count:operations.length,open:operation?.open,number:node.querySelector(':scope > .as-chain-number')?.textContent,
                summaryVisible:Boolean(summary?.getClientRects().length),bodyPresent:Boolean(operation?.querySelector(':scope > .as-chain-body'))};
    })""")
    assert len(rows)>=minimum
    for index,row in enumerate(rows):
        assert row['count']==1,row
        assert row['open'] is False, 'Each operation must initially be collapsed'
        assert row['number']==str(index+1).zfill(2),row
        assert row['summaryVisible'] and row['bodyPresent'],row
    first=steps.first;operation=open_operation(first)
    assert operation.locator(':scope > .as-chain-body').is_visible()
    operation.locator(':scope > summary').click()
    assert operation.locator(':scope > .as-chain-body').is_hidden()


def open_all_operations(page):
    steps=page.locator('li.as-chain-step:not([hidden])[data-as-trace]')
    for index in range(steps.count()):open_operation(steps.nth(index))


def assert_no_horizontal_overflow(page):
    dimensions=page.evaluate('({width:document.documentElement.clientWidth,scrollWidth:document.documentElement.scrollWidth})')
    assert dimensions['scrollWidth']<=dimensions['width'],dimensions
    return dimensions


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    checks=[];errors=[];requests=[]
    with tempfile.TemporaryDirectory(prefix='execution-browser-') as directory:
        root=Path(directory);workspace=root/'isolated-browser';workspace.mkdir()
        store=WorkbenchRunStore(workspace,run_id='20261106',producer_model='ppio',root_html=ROOT_HTML,workbench_url='http://127.0.0.1:8080/')
        view,packet=PassCheckDetailsTests().fixture();observation=packet.pop('_presentation_observations')['pos-product-identity']
        packet['units'].extend([{'unit_id':'claim','source_file':'结算单.pdf','locator':'第1页'},{'unit_id':'qince','source_file':'勤策申请明细','locator':'申请SQ-FIXTURE，第1行'}])
        store.write_analysis('pdf-policy/evidence.json',{'packets':[packet]},label='隔离来源证据')
        with collect_model_artifacts(workspace/'analysis',observer=store.observe):
            record_step('database.query','隔离商品数据库查询',{'source':{'name':'测试商品主数据库','host':'db.fixture.invalid','database':'fixture_catalog'},'table':'fixture_catalog.products','fields':['barcode_69','product_name'],'condition':'barcode_69 = 6900000000001'})
            record_step('qince.response','隔离勤策接口返回',{'endpoint':'/api/cuxiao/verify/v1/queryVerificationRegular','rows':[{'verify_code':'isolated','amount':'100.00','details':[{'id':9000000000000000001,'customer_name':'隔离客户甲','quantity':2,'unitPrice':'50.00'}]}]})
            record_step('oss.image_verified','隔离OSS参考图校验',{'view_id':'front','sha256':'a'*64,'size_bytes':123})
            save_model_observations('pos-product-identity-a001',observation)
            record_step('command.completed','隔离长命令返回',{'return_code':0,'stdout':'START-'+('x'*100000)+'-TRACE_TAIL'})
            for attempt, kind, usage in [(1,'ai.validation_failed',{'input_tokens':10,'output_tokens':4}),(2,'ai.validated',{'input_tokens':20,'output_tokens':8})]:
                record_step('ai.request',f'隔离调用第{attempt}次',{'attempt':attempt,'provider':'fixture','model':'fixture-model','images':[]},status='started',call_id='retry-call')
                record_step('ai.transport','隔离发送',{'attempt':attempt,'model':'fixture-model'},status='started',call_id='retry-call')
                result_data={'attempt':attempt,'metrics':{'usage':usage}}
                if attempt==2:result_data['validated_result']={'checks':[{'rule_id':'quantity','status':'pass','reason':'结算数量2与申请数量2一致','source_ids':['claim','qince'],'comparisons':[{'label':'结算与申请数量核对','value_kind':'quantity','operation':'value','operator':'eq','left':{'unit_id':'claim','number':'2','quote':'结算数量2'},'right':[{'unit_id':'qince','number':'2','quote':'申请数量2'}]}]}]}
                record_step(kind,'隔离返回',result_data,status='failed' if attempt==1 else 'completed',call_id='retry-call')
                if attempt == 1: record_step('ai.retry','安排重试',{'next_attempt':2,'delay_seconds':1},status='scheduled',call_id='retry-call')
            for call, usage in [('missing',{}),('zero',{'input_tokens':0,'output_tokens':0})]:
                record_step('ai.request',f'隔离{call}调用',{'attempt':1,'model':'fixture-model'},status='started',call_id=call)
                record_step('ai.failed' if call=='missing' else 'ai.validated',f'隔离{call}返回',{'attempt':1,'metrics':usage},status='failed' if call=='missing' else 'completed',call_id=call)
            record_step('qince.linked','核销明细与申请行精确关联',{'verify_code':'isolated','join_fields':['核销details.activity_code = 申请applyCode','核销details.activity_cm_id = 申请subList.subId'],'linked_application_rows':[{'application_code':'SQ-FIXTURE','row':{'subId':9000000000000000001,'cusName':'隔离客户甲','quantity':2,'unitPrice':'50.00','applyAmount':'100.00','extColumns':[{'ext_key':'联系电话','ext_value':'DO-NOT-DISPLAY-PHONE'},{'ext_key':'批复金额合计','ext_value':'88.00'}]}}]})
        store.complete(view_payload=view,scenarios=['personnel_incentive'],verification={'isolated':True})
        live_workspace=root/'isolated-live';live_workspace.mkdir()
        live_store=WorkbenchRunStore(live_workspace,run_id='20261106-live',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1/')
        live_store.observe('scenario.started',{'scenario':'promotional_display'})
        with collect_model_artifacts(live_workspace/'analysis',observer=live_store.observe):
            record_step('qince.request','查询隔离勤策核销明细',{'endpoint':'/fixture/verification','business_parameters':{'verify_code':'fixture-only','page':1}},status='started',call_id='live-qince')
        cycle_workspace=root/'isolated-route-cycle';cycle_workspace.mkdir()
        cycle_store=WorkbenchRunStore(cycle_workspace,run_id='20261109-route-cycle',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1/')
        with collect_model_artifacts(cycle_workspace/'analysis',observer=cycle_store.observe):
            record_step('qince.response','读取隔离申请',{'endpoint':'/fixture/application','row_count':1},call_id='cycle-qince-first')
            record_step('ai.request','隔离识别',{'attempt':1,'model':'fixture-model'},status='started',call_id='cycle-ai-first')
            record_step('ai.validated','隔离识别完成',{'attempt':1,'metrics':{'input_tokens':1,'output_tokens':1}},call_id='cycle-ai-first')
            record_step('database.query','隔离商品查询',{'table':'fixture_catalog.products'},status='completed')
            record_step('oss.image_verified','隔离参考图',{'size_bytes':1,'sha256':'b'*64})
            record_step('ai.request','隔离复核',{'attempt':1,'model':'fixture-model'},status='started',call_id='cycle-ai-last')
            record_step('ai.validated','隔离复核完成',{'attempt':1,'metrics':{'input_tokens':1,'output_tokens':1}},call_id='cycle-ai-last')
            record_step('qince.response','再次读取隔离申请',{'endpoint':'/fixture/application','row_count':1},call_id='cycle-qince-last')
        cycle_store.complete(view_payload=view,scenarios=['personnel_incentive'],verification={'isolated':True})
        empty_workspace=root/'isolated-no-steps';empty_workspace.mkdir()
        empty_store=WorkbenchRunStore(empty_workspace,run_id='20261109-no-steps',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1/')
        empty_store.complete(view_payload=view,scenarios=['personnel_incentive'],verification={'isolated':True})
        stable_workspace=root/'isolated-route-stable';stable_workspace.mkdir()
        stable_store=WorkbenchRunStore(stable_workspace,run_id='20261109-route-stable',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1/')
        stable_store.observe('scenario.started',{'scenario':'promotional_display'})
        with collect_model_artifacts(stable_workspace/'analysis',observer=stable_store.observe):
            record_step('qince.request','隔离稳定性取数',{'endpoint':'/fixture/stable'},status='started',call_id='stable-qince')
        failed_workspace=root/'isolated-route-failed';failed_workspace.mkdir()
        failed_store=WorkbenchRunStore(failed_workspace,run_id='20261109-route-failed',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1/')
        with collect_model_artifacts(failed_workspace/'analysis',observer=failed_store.observe):
            record_step('oss.image_verified','隔离失败任务已读参考图',{'size_bytes':1,'sha256':'c'*64})
        failed_store.fail(RuntimeError('Isolated fixture failure; no real operation'))
        server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,catalog=WorkbenchCatalog(root));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_address[1]}'
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True,args=['--no-proxy-server']);page=browser.new_page(viewport={'width':1440,'height':960})
                page.on('pageerror',lambda e:errors.append(str(e)));page.on('request',lambda r:requests.append(r.url))
                # Every fixture has its own observed route, including non-consecutive revisits.
                page.goto(base,wait_until='networkidle')
                page.wait_for_selector('[data-as-execution-route="isolated-route-cycle"]')
                cycle_route=page.locator('[data-as-execution-route="isolated-route-cycle"]:visible').first.inner_text()
                assert cycle_route.startswith('本单实际链路：'),cycle_route
                route_parts=[part.strip() for part in cycle_route.split(' · 实际耗时：')[0].split('：',1)[1].split('→')]
                assert route_parts==['勤策','AI 识别','商品数据库','商品 OSS','AI 识别','勤策'],route_parts
                live_route=page.locator('[data-as-execution-route="isolated-live"]:visible').first.inner_text()
                assert live_route=='本单实际链路：勤策',live_route
                empty_route=page.locator('[data-as-execution-route="isolated-no-steps"]:visible').first.inner_text()
                assert '当前未保存外部查询或模型调用的逐步记录' in empty_route and '已建立本次核销任务' not in empty_route,empty_route
                assert '实际耗时：' in empty_route,empty_route
                assert page.locator('[data-as-execution-route="isolated-browser"]:visible').count()>=1
                assert not any('/api/runs/'+name+'/snapshot' in url for url in requests for name in ['isolated-browser','isolated-route-cycle','isolated-no-steps','isolated-route-failed']), 'A catalog with route arrays must not add per-history snapshot requests'
                page.screenshot(path=str(args.output/'overview-routes-1440.png'),full_page=True)
                checks.append({'passed':True,'overview_routes_are_per_run':True,'nonconsecutive_revisits_preserved':True,'empty_route_explicit':True,'cycle_route':route_parts})
                stable_card=page.locator('[data-active-run="isolated-route-stable"]')
                stable_card.wait_for(state='visible')
                stable_button=stable_card.locator('[data-as-open="isolated-route-stable"]')
                stable_button.focus()
                stable_card_handle=stable_card.element_handle()
                stable_button_handle=stable_button.element_handle()
                with collect_model_artifacts(stable_workspace/'analysis',observer=stable_store.observe):
                    record_step('database.query','仅追加隔离商品读取步骤',{'table':'fixture_catalog.products'},status='started')
                page.wait_for_function("document.querySelector('[data-active-run=\"isolated-route-stable\"] [data-as-execution-route]')?.textContent.includes('商品数据库')",timeout=15000)
                assert stable_card_handle.evaluate("node=>node.isSameNode(document.querySelector('[data-active-run=\"isolated-route-stable\"]'))"), 'A route-only refresh must not replace its card'
                assert stable_button_handle.evaluate('node=>node.isSameNode(document.activeElement)'), 'A route-only refresh must preserve focus'
                assert stable_card.locator('[data-as-execution-route]').inner_text().split(' · 实际耗时：')[0]=='本单实际链路：勤策 → 商品数据库'
                checks.append({'passed':True,'route_only_refresh_same_card_node':True,'route_only_refresh_preserves_focus':True,'route_text_updated':True})
                stable_store.complete(view_payload=view,scenarios=['promotional_display'],verification={'isolated':True})
                # A deployed older backend may omit the catalog field while its
                # saved snapshot contains the exact same trace shown by the archive.
                legacy=browser.new_page(viewport={'width':1440,'height':960})
                legacy.on('pageerror',lambda e:errors.append(str(e)));legacy.on('request',lambda r:requests.append(r.url))
                legacy.add_init_script("""(() => {
                    const original=window.fetch.bind(window);
                    window.__routeProbe={held:true,resume:[],active:0,max:0,calls:{}};
                    window.fetch=async (...args)=>{
                        const path=new URL(String(args[0]),location.href).pathname;
                        if(!/^\\/api\\/runs\\/[^/]+\\/snapshot$/.test(path))return original(...args);
                        const qa=window.__routeProbe;qa.calls[path]=(qa.calls[path]||0)+1;
                        qa.active++;qa.max=Math.max(qa.max,qa.active);
                        try {
                            if(qa.held)await new Promise(resolve=>qa.resume.push(resolve));
                            return await original(...args);
                        } finally {qa.active--;}
                    };
                })()""")
                failed_reads={'isolated-route-failed'}
                legacy_empty_missing={'enabled':False}
                def legacy_catalog(route):
                    headers={key:value for key,value in route.request.headers.items() if key.lower()!='if-none-match'}
                    response=route.fetch(headers=headers);payload=response.json()
                    for run in payload['runs']:
                        if run['workspace_id']!='isolated-no-steps' or legacy_empty_missing['enabled']:run.pop('execution_route',None)
                    route.fulfill(response=response,json=payload)
                def legacy_snapshot(route):
                    workspace_id=unquote(urlparse(route.request.url).path.split('/')[-2])
                    if workspace_id in failed_reads:
                        route.fulfill(status=503,json={'error':'Isolated snapshot temporarily unavailable'});return
                    response=route.fetch();snapshot=response.json();snapshot['run'].pop('execution_route',None)
                    route.fulfill(response=response,json=snapshot)
                legacy.route('**/api/runs',legacy_catalog)
                legacy.route('**/api/runs/*/snapshot',legacy_snapshot)
                legacy.goto(base,wait_until='domcontentloaded')
                legacy.wait_for_function("window.__routeProbe.active===4")
                pending=legacy.locator('[data-as-execution-route="isolated-route-cycle"]:visible')
                assert '正在读取对应技术档案' in pending.inner_text()
                legacy_card=pending.locator('xpath=ancestor::article[1]').element_handle()
                legacy_button=pending.locator('xpath=ancestor::article[1]').locator('[data-as-open]').first
                legacy_button.focus();legacy_focus=legacy_button.element_handle()
                legacy.evaluate('window.__routeProbe.held=false;window.__routeProbe.resume.splice(0).forEach(resolve=>resolve())')
                legacy.wait_for_function("document.querySelector('[data-as-execution-route=\"isolated-route-cycle\"]')?.textContent.includes('商品 OSS')")
                legacy.wait_for_function("document.querySelector('[data-as-execution-route=\"isolated-route-failed\"]')?.textContent.includes('读取失败')")
                assert pending.inner_text()==cycle_route
                assert legacy_card.evaluate("node=>node.isSameNode(document.querySelector('[data-as-execution-route=\"isolated-route-cycle\"]').closest('article'))")
                assert legacy_focus.evaluate('node=>node.isSameNode(document.activeElement)')
                failed_text=legacy.locator('[data-as-execution-route="isolated-route-failed"]:visible').inner_text()
                assert '尚无' not in failed_text and '读取失败' in failed_text
                legacy.wait_for_function('window.__routeProbe.active===0')
                initial_calls=legacy.evaluate('window.__routeProbe.calls')
                assert initial_calls.get('/api/runs/isolated-no-steps/snapshot',0)==0
                assert all(count==1 for count in initial_calls.values()),initial_calls
                assert len(initial_calls)==5,initial_calls
                assert legacy.evaluate('window.__routeProbe.max')<=4
                assert legacy.locator('[data-as-execution-route="isolated-route-stable"]:visible').inner_text().split(' · 实际耗时：')[0]=='本单实际链路：勤策 → 商品数据库'
                # Failed reads retry without becoming an invented empty route.
                failed_reads.clear()
                legacy.wait_for_function("document.querySelector('[data-as-execution-route=\"isolated-route-failed\"]')?.textContent==='本单实际链路：商品 OSS'",timeout=15000)
                failed_reads.add('isolated-live')
                legacy.wait_for_function("document.querySelector('[data-as-execution-route=\"isolated-live\"]')?.textContent.includes('更新读取失败')",timeout=15000)
                assert '勤策' in legacy.locator('[data-as-execution-route="isolated-live"]:visible').first.inner_text()
                failed_reads.clear()
                legacy.wait_for_function("document.querySelector('[data-as-execution-route=\"isolated-live\"]')?.textContent==='本单实际链路：勤策'",timeout=15000)
                final_calls=legacy.evaluate('window.__routeProbe.calls')
                for name in ('isolated-browser','isolated-route-cycle','isolated-route-stable'):
                    assert final_calls['/api/runs/'+name+'/snapshot']==1,final_calls
                assert final_calls.get('/api/runs/isolated-no-steps/snapshot',0)==0
                assert legacy.evaluate('window.__routeProbe.max')<=4
                legacy_empty_missing['enabled']=True
                # Start without the authoritative [] cached by the preceding catalog.
                legacy.goto(base,wait_until='domcontentloaded')
                legacy.wait_for_function('window.__routeProbe.active===4')
                legacy.evaluate('window.__routeProbe.held=false;window.__routeProbe.resume.splice(0).forEach(resolve=>resolve())')
                legacy.wait_for_function("window.__routeProbe.calls['/api/runs/isolated-no-steps/snapshot']===1",timeout=15000)
                legacy.wait_for_function("document.querySelector('[data-as-execution-route=\"isolated-no-steps\"]')?.textContent.includes('当前未保存外部查询或模型调用的逐步记录')")
                legacy.screenshot(path=str(args.output/'overview-legacy-route-1440.png'),full_page=True)
                checks.append({'passed':True,'legacy_catalog_and_snapshot_routes_missing':True,'completed_and_failed_routes_derived_from_saved_trace':True,'unknown_loading_distinct_from_empty':True,'failed_read_retries_and_preserves_known_route':True,'explicit_empty_no_snapshot_fetch':True,'missing_field_empty_route_confirmed_by_snapshot':True,'per_round_workspace_deduplication':True,'maximum_snapshot_concurrency':legacy.evaluate('window.__routeProbe.max'),'legacy_route_same_card_and_focus':True,'new_catalog_no_history_snapshot_fanout':True})
                legacy.close()
                for width in [1440,1280]:
                    page.set_viewport_size({'width':width,'height':960});page.goto(base+'/?run='+quote(workspace.name),wait_until='networkidle')
                    page.locator('#eo-tab-passed').click();page.wait_for_selector('.eo-pass-card')
                    assert '精确查询' in page.locator('.eo-pass-card').inner_text()
                    page.locator('.eo-pass-card summary').filter(has_text='逐项核对明细').click()
                    assert '销售表，第7行' in page.locator('.eo-pass-card').inner_text();assert 'P1' in page.locator('.eo-pass-card').inner_text();assert 'P2' in page.locator('.eo-pass-card').inner_text()
                    page.screenshot(path=str(args.output/f'correct-details-{width}.png'),full_page=True)
                    page.locator('#as-record-technical').click()
                    assert page.locator('[data-as-tech-tab]').count()==0
                    assert page.locator('[data-as-trace]').count()==17
                    assert_default_operations(page)
                    page.screenshot(path=str(args.output/f'execution-collapsed-{width}.png'),full_page=True)
                    open_all_operations(page)
                    page.wait_for_function("[...document.querySelectorAll('[data-as-customer-evidence]')].some(n=>n.textContent.includes('逐行商品核对明细'))")
                    pos=page.locator('[data-as-trace]:visible').filter(has=page.get_by_role('heading',name='逐行核对销售明细与商品库',exact=True))
                    assert '经销商简称' in pos.inner_text() and '正式商品' in pos.inner_text() and '渠道商品' in pos.inner_text()
                    assert 'POS.xlsx' in pos.inner_text() and '第7行' in pos.inner_text()
                    assert '6900000000001' in pos.inner_text()
                    qince=page.locator('[data-as-trace]').nth(1)
                    page.wait_for_function("[...document.querySelectorAll('.as-customer-table')].some(n=>n.textContent.includes('9000000000000000001'))")
                    assert '隔离客户甲' in qince.inner_text() and '50.00' in qince.inner_text()
                    linked=page.locator('[data-as-trace]').last
                    page.wait_for_function("[...document.querySelectorAll('[data-as-customer-evidence]')].some(n=>n.textContent.includes('SQ-FIXTURE'))")
                    assert 'SQ-FIXTURE' in linked.inner_text() and '88.00' in linked.inner_text()
                    assert 'DO-NOT-DISPLAY-PHONE' not in linked.locator('[data-as-customer-evidence]').inner_text()
                    await_count=page.locator('[data-as-trace]:visible').count()
                    assert await_count<17, 'Transport and result receipts must be grouped under the actual operation'
                    pos.locator('h3').scroll_into_view_if_needed()
                    page.screenshot(path=str(args.output/f'customer-matching-{width}.png'),full_page=False)
                    assert page.locator('#as-chain-attachments [data-as-analysis^="analysis/execution-trace/"]').count()==0
                    first=page.locator('[data-as-trace]').nth(0)
                    assert 'db.fixture.invalid' in first.inner_text() and 'fixture_catalog.products' in first.inner_text()
                    assert first.locator('[data-as-step-tokens]').is_hidden()
                    assert 'Token：0（程序操作' not in first.inner_text()
                    assert '100.00' in page.locator('[data-as-trace]').nth(1).inner_text()
                    assert '合计 42 Token' in page.locator('#as-chain-tokens').inner_text()
                    assert '部分调用未返回' in page.locator('#as-chain-tokens').inner_text()
                    assert '合计 14 Token' in page.locator('[data-as-trace]').nth(5).inner_text()
                    assert '同第 6 步' in page.locator('[data-as-trace]').nth(6).inner_text()
                    assert '合计 28 Token' in page.locator('[data-as-trace]').nth(9).inner_text()
                    page.wait_for_function("[...document.querySelectorAll('[data-as-customer-evidence]')].some(n=>n.textContent.includes('结算与申请数量核对'))")
                    numeric=page.locator('[data-as-trace]').nth(9).locator('[data-as-customer-evidence]')
                    assert '左侧应等于右侧' in numeric.inner_text()
                    assert '结算单.pdf' in numeric.inner_text() and '申请SQ-FIXTURE' in numeric.inner_text()
                    assert '结算数量2' in numeric.inner_text() and '申请数量2' in numeric.inner_text()
                    assert '未返回' in page.locator('[data-as-trace]').nth(12).inner_text()
                    assert '合计 0 Token' in page.locator('[data-as-trace]').nth(14).inner_text()
                    first.locator('[data-as-operation-originals] > summary').click()
                    first.locator('[data-as-resource] > summary').first.click();page.wait_for_function("document.querySelector('[data-as-trace] .as-code').textContent.includes('barcode_69')")
                    command=page.locator('[data-as-trace]').nth(4)
                    command.locator('[data-as-operation-originals] > summary').click()
                    command.locator('[data-as-resource] > summary').first.click();command.locator('.as-code-notice:not([hidden])').wait_for()
                    command.locator('[data-as-code-expand]').click()
                    page.wait_for_function("[...document.querySelectorAll('[data-as-trace] .as-code')].some(node=>node.textContent.includes('TRACE_TAIL'))")
                    assert len(command.locator('.as-code').inner_text())>100000
                    page.screenshot(path=str(args.output/f'execution-{width}.png'),full_page=True)
                    page.locator('#as-chain-support > summary').click();assert page.locator('[data-as-checkpoint]').count()>=17
                    checkpoint=page.locator('[data-as-checkpoint]').nth(1)
                    checkpoint.locator(':scope > summary').click()
                    page.wait_for_function("[...document.querySelectorAll('[data-as-checkpoint] .as-code')].some(node=>node.textContent.includes('execution-trace'))")
                    assert_no_horizontal_overflow(page)
                    checks.append({'width':width,'steps':17,'passed':True,'default_collapsed_operations':True,'full_output_tail_visible':True,'source_row_visible':True,'checkpoints_complete':True,'retry_tokens_total':42,'unknown_and_zero_distinct':True,'customer_pos_comparison':True,'qince_fields_and_exact_large_ids':True})
                # Existing runtimes can omit display summaries; the UI reads only saved step files.
                def old_snapshot(route):
                    response=route.fetch();snapshot=response.json()
                    for step in snapshot['execution_trace']['steps']:step.pop('display_data',None)
                    route.fulfill(response=response,json=snapshot)
                page.route('**/api/runs/isolated-browser/snapshot',old_snapshot)
                page.goto(base,wait_until='networkidle');page.locator('#as-tab-archive').click()
                page.locator('[data-as-technical="isolated-browser"]').click()
                page.wait_for_function("document.querySelector('#as-chain-tokens')?.textContent.includes('合计 42 Token')")
                page.wait_for_load_state('networkidle')  # Saved legacy metadata finishes regrouping operations asynchronously.
                assert_default_operations(page)
                open_operation(page.locator('[data-as-trace]').first)
                assert 'db.fixture.invalid' in page.locator('[data-as-trace]').first.inner_text()
                page.keyboard.press('Escape')
                page.locator('[data-as-technical="isolated-live"]').click()
                page.wait_for_function("document.querySelector('#as-chain-current')?.textContent.includes('查询隔离勤策')")
                assert_default_operations(page,minimum=1)
                first=page.locator('[data-as-trace]').first;operation=open_operation(first)
                operation.evaluate("node=>node.__qaPreserved='same-operation-node'")
                first.locator('[data-as-operation-originals] > summary').click();first.locator('[data-as-resource] > summary').first.click()
                with collect_model_artifacts(live_workspace/'analysis',observer=live_store.observe):
                    record_step('qince.response','隔离勤策返回',{'row_count':1},call_id='live-qince')
                    record_step('database.query','正在查询隔离商品主数据库',{'source':{'name':'测试商品库','host':'db.fixture.invalid'},'table':'fixture_catalog.products'},status='started')
                    record_step('ai.request','正在隔离识别',{'attempt':1,'model':'fixture-model'},status='started',call_id='live-ai')
                page.wait_for_function("document.querySelectorAll('[data-as-trace]').length===4",timeout=15000)
                assert operation.get_attribute('open') is not None
                assert operation.evaluate('node=>node.__qaPreserved')=='same-operation-node'
                assert first.locator('[data-as-operation-originals]').get_attribute('open') is not None
                visible_steps=page.locator('li.as-chain-step:not([hidden])[data-as-trace]')
                assert visible_steps.count()==3
                assert visible_steps.locator(':scope > .as-chain-number').all_text_contents()==['01','02','03']
                for index in (1,2):
                    assert visible_steps.nth(index).locator(':scope > details.as-chain-operation').get_attribute('open') is None
                page.keyboard.press('Escape');page.locator('#as-tab-overview').click()
                page.wait_for_function("[...document.querySelectorAll('[data-as-execution-route=\"isolated-live\"]')].some(node=>node.textContent.includes('商品数据库')&&node.textContent.includes('AI 识别'))",timeout=15000)
                live_route=page.locator('[data-as-execution-route="isolated-live"]:visible').first.inner_text()
                assert live_route=='本单实际链路：勤策 → 商品数据库 → AI 识别',live_route
                page.locator('#as-tab-archive').click();page.locator('[data-as-technical="isolated-live"]').click()
                live_store.complete(view_payload=view,scenarios=['promotional_display'],verification={'isolated':True})
                page.wait_for_function("document.querySelector('#as-chain-current').textContent.includes('核销已完成')",timeout=15000)
                page.keyboard.press('Escape');assert page.locator('#as-drawer-backdrop').is_hidden()
                checks.append({'passed':True,'legacy_metadata_fallback':True,'live_steps_appended':True,'open_operation_and_nested_details_preserved':True,'same_operation_dom_node':True,'new_steps_collapsed':True,'live_overview_route_refreshed':True,'completed_status_refreshed':True})
                page.set_viewport_size({'width':390,'height':844});page.goto(base,wait_until='networkidle')
                assert_no_horizontal_overflow(page)
                page.screenshot(path=str(args.output/'overview-routes-390.png'),full_page=True)
                page.locator('#as-tab-archive').click();page.locator('[data-as-technical="isolated-route-cycle"]').click()
                page.wait_for_selector('li.as-chain-step:not([hidden]) details.as-chain-operation')
                assert_default_operations(page)
                assert_no_horizontal_overflow(page)
                open_operation(page.locator('li.as-chain-step:not([hidden])[data-as-trace]').first)
                assert_no_horizontal_overflow(page)
                page.screenshot(path=str(args.output/'execution-mobile-390.png'),full_page=True)
                checks.append({'passed':True,'mobile_width':390,'overview_and_collapsed_expanded_timeline_no_horizontal_overflow':True})
                page.set_viewport_size({'width':1440,'height':960})
                originals={name:(workspace/name).read_bytes() for name in ['manifest.json','snapshot.json']}
                archive=render_static_run_archive(workspace,ROOT_HTML)
                page.context.set_offline(True);page.goto(archive.as_uri())
                page.locator('#as-tab-archive').click();page.locator('[data-as-technical]').click()
                page.wait_for_selector('[data-as-trace]')
                assert page.locator('[data-as-trace]').count()==17
                assert '合计 42 Token' in page.locator('#as-chain-tokens').inner_text()
                assert_default_operations(page)
                ai=page.locator('[data-as-trace]').nth(5)
                open_operation(ai)
                ai.locator('[data-as-operation-originals] > summary').click();ai.locator('[data-as-resource] > summary').first.click()
                page.wait_for_function("[...document.querySelectorAll('[data-as-trace] .as-code')].some(node=>node.textContent.includes('fixture-model'))")
                assert originals=={name:(workspace/name).read_bytes() for name in originals}
                checks.append({'passed':True,'static_archive_offline':True,'history_unchanged':True})
                def legacy_static_payload(match):
                    payload=json.loads(match.group(2))
                    for catalog in (payload.get('static_catalog',{}),payload.get('catalog',{})):
                        for run in catalog.get('runs',[]):run.pop('execution_route',None)
                    payload.get('snapshot',{}).get('run',{}).pop('execution_route',None)
                    return match.group(1)+json.dumps(payload,ensure_ascii=False).replace('<','\\u003c')+match.group(3)
                legacy_archive=root/'legacy-static.html'
                legacy_archive.write_text(re.sub(r'(<script id="(?:audit-workbench-context|audit-static-archive)" type="application/json">)(.*?)(</script>)',legacy_static_payload,archive.read_text(encoding='utf-8'),flags=re.S),encoding='utf-8')
                page.goto(legacy_archive.as_uri());page.locator('#as-tab-overview').click()
                page.wait_for_function("document.querySelector('[data-as-execution-route]')?.textContent.includes('商品数据库')")
                static_route=page.locator('[data-as-execution-route]:visible').inner_text()
                page.locator('#as-tab-archive').click();page.locator('[data-as-technical]').click()
                assert page.locator('#as-chain-route').inner_text()==static_route
                assert originals=={name:(workspace/name).read_bytes() for name in originals}
                checks.append({'passed':True,'legacy_static_archive_offline_route_matches_technical_archive':True,'history_unchanged':True})
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
    assert not errors,errors;assert all(url.startswith(base) or url.startswith('file:') for url in requests)
    (args.output/'browser-verification.json').write_text(json.dumps({'passed':True,'cases':checks,'page_errors':errors,'external_requests':0,'real_model_calls':0},ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'passed':True,'cases':len(checks),'page_errors':errors,'external_requests':0}))


if __name__=='__main__':main()
