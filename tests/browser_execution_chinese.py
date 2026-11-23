"""Replay saved history read-only; all browser requests use isolated responses."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright
from audit_core.workbench_html import inject_workbench_context, replace_audit_payload


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--asset',type=Path,required=True)
    parser.add_argument('--snapshot',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    original=args.snapshot.read_bytes()
    source=json.loads(original)
    html=args.asset.read_text(encoding='utf-8')
    checks=[];errors=[];mutations=[];external=[]
    with sync_playwright() as p:
        browser=p.chromium.launch(headless=True,args=['--no-proxy-server'])
        try:
            for width in [1440,390]:
                snapshot=deepcopy(source)
                page=browser.new_page(viewport={'width':width,'height':1050})
                page.set_default_timeout(10000)
                page.on('pageerror',lambda error:errors.append(str(error)))
                def fulfill(route):
                    request=route.request;parsed=urlparse(request.url)
                    if request.method not in {'GET','HEAD'}:
                        mutations.append(request.method);route.abort();return
                    if parsed.netloc!='127.0.0.1:18375':
                        external.append(parsed.netloc);route.abort();return
                    if parsed.path=='/':
                        context={'mode':'system' if snapshot['run']['status']=='running' else 'record','delivery_mode':'server','selected_run':snapshot['run']['workspace_id'],
                                 'main_flow_tasks':snapshot['run']['main_flow_tasks'],
                                 'view_available':True,**{key:snapshot[key] for key in ['run','analysis_files','dom_checkpoints','recent_events','execution_trace']}}
                        value=inject_workbench_context(replace_audit_payload(html,snapshot['view']),context)
                        route.fulfill(status=200,content_type='text/html; charset=utf-8',body=value)
                    elif parsed.path=='/api/runs':route.fulfill(status=200,json={'runs':[snapshot['run']],'count':1})
                    elif parsed.path.endswith('/snapshot'):route.fulfill(status=200,json=snapshot)
                    elif parsed.path=='/api/completion-estimates':route.fulfill(status=200,json={'estimates':[]})
                    else:route.fulfill(status=404,json={'error':'隔离预览未加载此原始附件'})
                page.route('**/*',fulfill)
                page.goto('http://127.0.0.1:18375/?run=fixture',wait_until='networkidle')
                if not page.locator('#as-record-technical').count():
                    page.screenshot(path=str(args.output/'failed-render.png'))
                    raise AssertionError({'errors':errors,'title':page.title(),'body':page.locator('body').inner_text()[:2200]})
                page.locator('#as-record-technical').click()
                page.wait_for_selector('[data-as-legacy-event] h3')
                titles=page.locator('[data-as-legacy-event] h3').all_text_contents()
                assert len(titles)==len(snapshot['recent_events'])==9,titles
                assert titles[0]=='已建立本次核销任务' and titles[-1]=='本次核销已完成',titles
                assert all(any('\u3400'<=c<='\u9fff' for c in title) for title in titles),titles
                assert not any('.' in title or '_' in title for title in titles),titles
                current=page.locator('#as-chain-current').inner_text()
                assert '9 条阶段记录' in current and '0 项实际操作' not in current,current
                route_text=page.locator('#as-chain-route').inner_text()
                assert '当前未保存外部查询或模型调用的逐步记录' in route_text,route_text
                assert '已建立本次核销任务' not in route_text and 'POS达标激励' not in route_text,route_text
                assert page.locator('[data-as-legacy-event] [data-as-operation][open]').count()==0
                first=page.locator('[data-as-legacy-event]').first
                first.locator('[data-as-operation] > summary').click()
                assert first.locator('.as-chain-description').is_visible()
                first.locator('.as-customer-originals > summary').click()
                assert 'run.created' in first.locator('pre').inner_text()
                first.locator('[data-as-operation] > summary').click()
                page.screenshot(path=str(args.output/f'history-chinese-{width}.png'),full_page=False)
                assert page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth')
                checks.append({'width':width,'titles':titles,'stage_count_correct':True,'original_identifiers_preserved':True,'default_collapsed':True})
                page.close()

            # A future producer without a translation must still display Chinese.
            snapshot=deepcopy(source);snapshot['run']['status']='running'
            snapshot['recent_events'].append({'seq':10,'timestamp':'2026-11-11T03:00:00Z','type':'qince.future_action','message':'Raw English detail'})
            page=browser.new_page(viewport={'width':1440,'height':1050})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.route('**/*',fulfill)
            page.goto('http://127.0.0.1:18375/?run=fixture',wait_until='networkidle')
            page.locator('#as-tab-archive').click()
            page.locator('[data-as-technical]').click()
            page.wait_for_selector('[data-as-legacy-event="10"] h3')
            assert page.locator('[data-as-legacy-event="10"] h3').inner_text()=='勤策业务资料记录'
            first=page.locator('[data-as-legacy-event]').first
            first.locator('[data-as-operation] > summary').click()
            snapshot['recent_events'].append({'seq':11,'timestamp':'2026-11-11T03:00:01Z','event':'future.unmapped','message':'New stage'})
            page.wait_for_selector('[data-as-legacy-event="11"] h3',timeout=10000)
            assert page.locator('[data-as-legacy-event="11"] h3').inner_text()=='核销处理记录'
            assert first.locator('[data-as-operation]').get_attribute('open') is not None
            assert '11 条阶段记录' in page.locator('#as-chain-current').inner_text()
            checks.append({'future_events_chinese':True,'legacy_poll_refreshes_events':True,'expanded_state_preserved':True})
            page.close()
        finally:browser.close()
    assert args.snapshot.read_bytes()==original
    assert not errors and not mutations and not external,(errors,mutations,external)
    result={'passed':True,'cases':checks,'javascript_errors':errors,'write_requests':mutations,
            'external_requests':external,'real_model_calls':0,'saved_history_unchanged':True,'asset':str(args.asset)}
    (args.output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
