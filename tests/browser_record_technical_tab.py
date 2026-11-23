"""Completed records keep technical evidence beside the result tabs."""
import argparse
import hashlib
import json
from pathlib import Path
import tempfile
import threading

from playwright.sync_api import expect, sync_playwright
from audit_core.execution_trace import collect_execution_trace, record_step
from audit_core.workbench_html import render_static_run_archive
from audit_core.workbench_runtime import ROOT_HTML
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import WorkbenchRunStore, atomic_write_json
from tests.test_pass_check_details import PassCheckDetailsTests


def check_record(page, output, name, expected_files=None):
    tabs=page.locator('.eo-rail-nav [role="tab"]')
    expect(tabs).to_have_count(5)
    assert tabs.evaluate_all('nodes => nodes.map(n => n.querySelector("strong").textContent)')==['提交材料','错误总览','正确检查项','技术档案','AI 总结']
    materials=page.locator('#as-record-materials');materials.click()
    expect(page.locator('#materials')).to_be_visible()
    if expected_files is not None:
        expect(page.locator('[data-as-submitted-file]')).to_have_count(len(expected_files))
        assert page.locator('[data-as-submitted-file] td:nth-child(2)').all_text_contents()==expected_files
        assert page.locator('#materials img').count()==0
        if name.startswith('isolated') or name=='static-record':
            expect(page.locator('#materials')).to_contain_text('来自：活动返图.xls')
            expect(page.locator('#materials')).to_contain_text('Word 文档（DOCX）')
    page.screenshot(path=str(output/(name+'-materials.png')))
    assert page.locator('.as-record-toolbar #as-record-technical').count()==0
    technical=page.get_by_role('tab',name='档 技术档案 本次核销的执行全链路',exact=True)
    technical.click()
    expect(technical).to_have_attribute('aria-selected','true')
    expect(page.locator('#technical')).to_be_visible()
    expect(page.locator('#as-chain-current')).not_to_be_empty()
    expect(page.locator('[data-as-query-summary]').first).to_contain_text('查询条件：')
    if name.startswith('isolated') or name=='static-record':
        expect(page.locator('[data-as-query-summary]').first).to_contain_text('核销单号：HX-FIXTURE')
        expect(page.locator('[data-as-query-summary]').first).to_contain_text('返回 1 条记录，内含 2 条明细')
        expect(page.locator('[data-as-trace]:visible h3').first).to_have_text('查询勤策核销单 · 核销单号：HX-FIXTURE')
        assert page.locator('[data-as-operation][open]').count()==0
        first=page.locator('[data-as-trace]:visible').first
        first.locator('[data-as-operation] > summary').click()
        expect(first.locator('.as-chain-body')).to_be_visible()
        first.locator('[data-as-operation] > summary').click()
    assert page.locator('[role="dialog"]:visible').count()==0
    assert page.evaluate('document.body.style.overflow')!='hidden'
    page.screenshot(path=str(output/(name+'.png')))
    summary=page.locator('#eo-tab-ai-summary')
    summary.click();expect(page.locator('#ai-summary')).to_be_visible()
    expect(page.locator('#technical')).to_be_hidden()
    passed=page.locator('#eo-tab-passed');passed.click()
    expect(page.locator('#passed')).to_be_visible()
    passed.focus();page.keyboard.press('ArrowDown')
    expect(technical).to_be_focused();expect(technical).to_have_attribute('aria-selected','true')
    expect(page.locator('#technical')).to_be_visible()
    page.keyboard.press('ArrowDown');expect(summary).to_be_focused()
    expect(page.locator('#ai-summary')).to_be_visible()
    assert page.locator('.eo-rail-nav [role="tab"][tabindex="0"]').count()==1
    page.locator('#eo-tab-home').click();expect(page.locator('#home')).to_be_visible()
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);parser.add_argument('--live-url')
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    errors=[];writes=[];report=[]
    with tempfile.TemporaryDirectory(prefix='technical-result-tab-') as temporary:
        root=Path(temporary);workspace=root/'isolated-record-technical'
        store=WorkbenchRunStore(workspace,run_id='20261112-technical-tab',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1:8080/')
        with collect_execution_trace(store.analysis_root,store.observe):
            record_step('qince.request','读取本次核销申请资料',{'endpoint':'/api/cuxiao/verify/v1/queryVerificationRegular','business_parameters':{'verify_code':'HX-FIXTURE','page':'1','rows':'100'}},status='started',call_id='fixture-query')
            record_step('qince.response','已取得本次核销资料',{'endpoint':'/api/cuxiao/verify/v1/queryVerificationRegular','business_parameters':{'verify_code':'HX-FIXTURE','page':'1','rows':'100'},'row_count':1,'rows':[{'verify_code':'HX-FIXTURE','details':[{'id':'1'},{'id':'2'}]}]},status='completed',call_id='fixture-query')
        inventory={'a001':{'archive_id':'a001','source_archive':'D:/private/测试资料包.zip','inventory':[
            {'file_id':'a001-f1','source_file':'合同/促销合同.docx','suffix':'.docx','kind':'visual'},
            {'file_id':'a001-f2','source_file':'活动返图.xls','suffix':'.xls'},
            {'file_id':'a001-f3','source_file':'活动返图.xls/照片01.png','suffix':'.png','parent_source':'活动返图.xls'},
            {'file_id':'a001-f4','source_file':'<img src=x onerror=alert(1)>.png','suffix':'.png'},
        ]}}
        expected_files=[item['source_file'] for item in inventory['a001']['inventory']]
        store.write_analysis('input-cases.json',inventory,label='材料分类与角色绑定')
        view,packet=PassCheckDetailsTests().fixture()
        store.complete(view_payload=view,scenarios=['personnel_incentive'],verification={'verified':True})
        static=render_static_run_archive(workspace,ROOT_HTML)
        before={str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.rglob('*') if p.is_file()}
        server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,catalog=WorkbenchCatalog(root))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True,args=['--no-proxy-server'])
                page=browser.new_page(viewport={'width':1440,'height':960})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request:writes.append(request.method+' '+request.url) if request.method not in ['GET','HEAD'] else None)
                base=f'http://127.0.0.1:{server.server_address[1]}'
                for width in [1440,1280]:
                    page.set_viewport_size({'width':width,'height':960})
                    page.goto(base+'/?run='+workspace.name)
                    check_record(page,args.output,f'isolated-{width}',expected_files)
                    report.append({'mode':'isolated','width':width,'passed':True})
                page.route('**/static-record?run=*',lambda route:route.fulfill(status=200,content_type='text/html; charset=utf-8',body=static.read_text('utf-8')))
                page.goto(base+'/static-record?run='+workspace.name)
                check_record(page,args.output,'static-record',expected_files)
                report.append({'mode':'staticArchive','passed':True})
                pattern='**/api/runs/*/analysis?path=analysis%2Finput-cases.json'
                page.route(pattern,lambda route:route.fulfill(status=503,json={'error':'isolated read failure'}))
                page.goto(base+'/?run='+workspace.name);page.locator('#as-record-materials').click()
                expect(page.locator('[data-as-materials-retry]')).to_be_visible()
                page.unroute(pattern);page.locator('[data-as-materials-retry]').click()
                expect(page.locator('[data-as-submitted-file]')).to_have_count(4)
                report.append({'mode':'materialsReadFailureAndRetry','passed':True})
                missing=WorkbenchRunStore(root/'missing-inventory',run_id='20261112-missing-inventory',producer_model='fixture',root_html=ROOT_HTML,workbench_url=base,source_archives=['原始资料包.zip'])
                missing.complete(view_payload=view,scenarios=['personnel_incentive'],verification={'verified':True})
                page.goto(base+'/?run=missing-inventory');page.locator('#as-record-materials').click()
                expect(page.locator('#materials')).to_contain_text('未留存可读取的逐文件材料清单')
                expect(page.locator('#materials')).to_contain_text('原始资料包.zip')
                report.append({'mode':'legacyMissingInventory','passed':True})
                failed=WorkbenchRunStore(root/'failed-materials',run_id='20261112-failed-materials',producer_model='fixture',root_html=ROOT_HTML,workbench_url=base)
                failed.write_analysis('input-cases.json',inventory,label='材料分类与角色绑定')
                failed.fail(RuntimeError('isolated task failure'))
                page.goto(base+'/?run=failed-materials')
                expect(page.locator('[data-as-submitted-file]')).to_have_count(4)
                report.append({'mode':'failedRunMaterials','passed':True})
                if args.live_url:
                    runs=page.request.get(args.live_url.rstrip('/')+'/api/runs').json()['runs']
                    run=next(run for run in runs if run['status']=='completed')
                    live_inventory=page.request.get(args.live_url.rstrip('/')+'/api/runs/'+run['workspace_id']+'/analysis?path=analysis%2Finput-cases.json').json()
                    live_files=[item['source_file'] for packet in live_inventory.values() for item in packet.get('inventory',[]) if item.get('source_file')]
                    for width in [1440,1280]:
                        page.set_viewport_size({'width':width,'height':960})
                        page.goto(args.live_url.rstrip('/')+'/?run='+run['workspace_id'])
                        check_record(page,args.output,f'live-{width}',live_files)
                        report.append({'mode':'liveReadOnly','width':width,'passed':True})
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
        after={str(p.relative_to(workspace)):hashlib.sha256(p.read_bytes()).hexdigest() for p in workspace.rglob('*') if p.is_file()}
        assert before==after
    assert not errors,errors
    assert not writes,writes
    result={'passed':True,'checks':report,'javascriptErrors':errors,'businessWriteRequests':writes,'modelCalls':0,'savedHistoryUnchanged':True}
    (args.output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
