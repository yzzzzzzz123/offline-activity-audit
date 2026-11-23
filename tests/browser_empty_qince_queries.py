"""Verify the timeline suppresses completed empty reads while retaining evidence."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import sync_playwright

from audit_core.execution_trace import record_step
from audit_core.model_metrics import collect_model_artifacts
from audit_core.workbench_html import render_static_run_archive
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import WorkbenchRunStore


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    errors=[];external=[];checks=[]
    source_sha=hashlib.sha256(ROOT_HTML.read_bytes()).hexdigest()
    with tempfile.TemporaryDirectory(prefix='empty-qince-browser-') as directory:
        root=Path(directory);workspace=root/'isolated-empty-query';workspace.mkdir()
        store=WorkbenchRunStore(workspace,run_id='20261109-isolated-empty-query',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1/')
        store.observe('scenario.started',{'scenario':'personnel_incentive'})
        paths={}
        with collect_model_artifacts(workspace/'analysis',observer=store.observe):
            for name in ['empty','nonempty','failed','pending','unread','mixed']:
                paths[name]=record_step('qince.request','隔离勤策查询'+name,{'endpoint':'/api/v2/customer/query','business_parameters':{'id':'9000000000000000001'}},status='started',call_id=name)['path']
                if name=='pending':continue
                if name=='failed':
                    record_step('qince.failed','隔离查询失败',{'error_code':'fixture'},status='failed',call_id=name)
                elif name=='unread':
                    record_step('qince.response','旧记录未保存返回数组',{'row_count':0},call_id=name)
                else:
                    response=record_step('qince.response','隔离勤策返回',{'rows':[{'cm_name':'真实返回门店'}] if name=='nonempty' else []},call_id=name)
                    if name=='empty':paths['empty_response']=response['path']
                    if name=='mixed':record_step('qince.response','隔离同次查询有数据',{'rows':[{'cm_name':'同次查询有值门店'}]},call_id=name)
            paths['self_test']=record_step('validation.stage_self_test','阶段代码自测',{'can_advance':True})['path']
            paths['failed_self_test']=record_step('validation.stage_self_test','阶段自测未通过',{'can_advance':False},status='failed')['path']
            paths['unknown_validation']=record_step('validation.other_check','其他实际检查',{'result':'kept'})['path']
            paths['workflow_marker']=record_step('workflow.biz_type.resolved','已确认业务类型',{'scenario':'personnel_incentive'})['path']
            paths['advisory']=record_step('validation.source_prose_warning','来源表述提示，继续完成核销',{'extra_model_calls':0})['path']
        server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,catalog=WorkbenchCatalog(root))
        server.RequestHandlerClass.log_message=lambda *args:None
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base=f'http://127.0.0.1:{server.server_address[1]}'
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True,args=['--no-proxy-server'])
                page=browser.new_page(viewport={'width':1440,'height':960})
                page.on('pageerror',lambda error:errors.append(str(error)))
                def route(request):
                    if request.request.url.startswith(base+'/') and request.request.method=='GET':request.continue_()
                    else:external.append(request.request.url);request.abort()
                page.route('**/*',route)
                page.goto(base+'/?run='+workspace.name,wait_until='networkidle')
                if page.locator('#as-record-technical').count():page.locator('#as-record-technical').click()
                else:page.locator('#as-tab-archive').click();page.locator('[data-as-technical]').first.click()
                visible=page.locator('li.as-chain-step:not([hidden])[data-as-trace]')
                page.wait_for_function("path=>document.querySelector(`[data-as-trace='${path}']`)?.hidden",arg=paths['empty'])
                page.wait_for_function("path=>document.querySelector(`[data-as-trace='${path}']`)?.hidden",arg=paths['self_test'])
                for name in ['nonempty','failed','pending','unread','mixed']:
                    assert visible.filter(has=page.locator(f'[data-as-path="{paths[name]}"]')).count()==1,name
                for name in ['self_test','workflow_marker','advisory']:
                    assert page.locator(f'li[data-as-trace="{paths[name]}"]').get_attribute('hidden') is not None,name
                    assert page.locator(f'#as-chain-attachments [data-as-path="{paths[name]}"]').count()==1,name
                for name in ['failed_self_test','unknown_validation']:
                    assert visible.filter(has=page.locator(f'[data-as-path="{paths[name]}"]')).count()==1,name
                assert '来源表述提示' not in page.locator('#as-chain-current').inner_text()
                assert page.locator(f'#as-chain-attachments [data-as-path="{paths["empty_response"]}"]').count()==1
                assert page.locator('#as-chain-support').get_attribute('open') is None
                states=visible.evaluate_all("nodes=>nodes.map(n=>({number:n.querySelector('.as-chain-number').textContent,open:n.querySelector(':scope > details').open,zero:n.querySelector('[data-as-step-tokens]').hidden}))")
                assert all(not item['open'] and item['zero'] for item in states)
                assert all(item['number']==str(index+1).zfill(2) for index,item in enumerate(states))
                checks.append({'passed':True,'empty_completed_hidden':True,'failed_pending_unread_retained':True,'mixed_response_retained':True,'raw_empty_response_preserved':True,'numbering_continuous':True,'no_program_zero_tokens':True,
                    'nonblocking_advisory_and_successful_self_test_hidden':True,'failed_and_unknown_checks_retained':True,'internal_records_accessible_in_folded_archive':True,'current_activity_excludes_advisory':True})
                target=page.locator(f'li[data-as-trace="{paths["nonempty"]}"] > details')
                target.locator(':scope > summary').click()
                with collect_model_artifacts(workspace/'analysis',observer=store.observe):
                    record_step('qince.response','等待中的查询返回空记录',{'rows':[]},call_id='pending')
                page.wait_for_function("path=>document.querySelector(`[data-as-trace='${path}']`)?.hidden",arg=paths['pending'],timeout=15000)
                assert target.get_attribute('open') is not None
                numbers=visible.locator('.as-chain-number').all_text_contents()
                assert numbers==[str(index+1).zfill(2) for index in range(len(numbers))]
                page.screenshot(path=str(args.output/'running-query-filter.png'))
                checks.append({'passed':True,'pending_becomes_empty_and_disappears':True,'other_expansion_preserved':True,'renumbered_after_live_update':True})
                # Static archives use the same template and keep raw requests.
                static=root/'static';static.mkdir()
                archive=render_static_run_archive(workspace,ROOT_HTML)
                (static/'index.html').write_bytes(archive.read_bytes())
                from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
                from functools import partial
                static_server=ThreadingHTTPServer(('127.0.0.1',0),partial(SimpleHTTPRequestHandler,directory=str(static)))
                static_thread=threading.Thread(target=static_server.serve_forever,daemon=True);static_thread.start()
                try:
                    page.unroute('**/*',route)
                    static_base=f'http://127.0.0.1:{static_server.server_address[1]}'
                    def static_route(request):
                        if request.request.url.startswith(static_base+'/') and request.request.method=='GET':request.continue_()
                        else:external.append(request.request.url);request.abort()
                    page.route('**/*',static_route)
                    page.goto(static_base+'/index.html',wait_until='networkidle')
                    if page.locator('#as-record-technical').count():page.locator('#as-record-technical').click()
                    else:page.locator('#as-tab-archive').click();page.locator('[data-as-technical]').first.click()
                    page.wait_for_function("path=>document.querySelector(`[data-as-trace='${path}']`)?.hidden",arg=paths['empty'])
                    assert page.locator(f'#as-chain-attachments [data-as-path="{paths["empty_response"]}"]').count()==1
                    checks.append({'passed':True,'offline_static_archive':True})
                finally:static_server.shutdown();static_server.server_close();static_thread.join()
                browser.close()
        finally:server.shutdown();server.server_close();thread.join()
    assert not errors,errors
    assert not external,external
    assert hashlib.sha256(ROOT_HTML.read_bytes()).hexdigest()==source_sha
    receipt={'passed':True,'cases':checks,'page_errors':errors,'external_requests':len(external),'real_model_calls':0,'html_sha256':source_sha}
    (args.output/'browser-verification.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(receipt,ensure_ascii=False))


if __name__=='__main__':main()
