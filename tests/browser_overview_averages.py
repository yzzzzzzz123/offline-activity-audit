"""Read-only live UI checks and isolated trimmed-average/filter fixtures."""
import argparse
import json
import math
from pathlib import Path
from statistics import mean
from tempfile import TemporaryDirectory
import threading
from urllib.parse import urlsplit

from playwright.sync_api import expect, sync_playwright
from audit_core.model_metrics import collect_model_artifacts, record_model_attempt
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import atomic_write_json


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--live-url')
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    report, errors, blocked = {}, [], []
    with TemporaryDirectory(prefix='overview-average-fixtures-') as temporary:
        root = Path(temporary)
        for name, status, total, seconds, day, source in [
            ('a','completed',1200,120,'10','input'),
            ('b','completed',600,240,'10','input'),
            ('zero','completed',0,0,'11','oss'),
            ('partial','completed',9999,None,'11','oss'),
            ('missing','completed',None,None,'12','input'),
            ('failed','failed',999999,99999,'12','input'),
            ('running','running',888888,88888,'12','input'),
        ]:
            workspace = root / ('20261112-averages-' + name)
            workspace.mkdir()
            data = {'workspace_id':workspace.name,'run_id':workspace.name,'status':status,
                    'created_at':f'2026-11-{day}T00:00:00Z' if seconds is not None else None,
                    'analysis_started_at':f'2026-11-{day}T00:00:00Z' if seconds is not None else None,
                    'completed_at':f'2026-11-{day}T00:{int(seconds)//60:02}:{int(seconds)%60:02}Z' if seconds is not None and seconds < 3600 else None,
                    'scenarios':['personnel_incentive'] if name != 'missing' else ['price_difference_support'],
                    'input_source':source,'source_archives':[name+'.zip']}
            if name == 'b':
                data['completed_at']='2026-11-10T00:05:00Z'
                data['resume_history']=[{'previous_completed_at':'2026-11-10T00:01:00Z','resumed_at':'2026-11-10T00:02:00Z'}]
            atomic_write_json(workspace/'manifest.json',data)
            if total is not None:
                with collect_model_artifacts(workspace/'analysis'):
                    record_model_attempt({'status':'success','input_tokens':total,'output_tokens':0})
                    if name == 'partial':record_model_attempt({'status':'timeout'})
        server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,catalog=WorkbenchCatalog(root))
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True,args=['--no-proxy-server'])
                context=browser.new_context(viewport={'width':1440,'height':960})
                allowed={'127.0.0.1'}
                if args.live_url:allowed.add(urlsplit(args.live_url).hostname)
                def guard(route):
                    if route.request.method == 'POST' and urlsplit(route.request.url).hostname in allowed and urlsplit(route.request.url).path.endswith('/api/presence'):
                        route.fulfill(status=200, content_type='application/json', body='{"online":0}')
                        return
                    if route.request.method != 'GET' or urlsplit(route.request.url).hostname not in allowed:
                        blocked.append(route.request.method+' '+route.request.url);route.abort()
                    else:route.continue_()
                context.route('**/*',guard)
                page=context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
                page.goto(f'http://127.0.0.1:{server.server_address[1]}/')
                tokens=page.locator('[data-as-average="tokens"]')
                duration=page.locator('[data-as-average="duration"]')
                expect(tokens.locator('strong')).to_have_text('600 Token')
                assert page.locator('#as-view-overview .as-hero').count()==0
                assert page.locator('.as-connection').count()==0
                assert page.get_by_text('点击记录可查看详情',exact=True).count()==0
                expect(duration.locator('strong')).to_have_text('2 分')
                expect(tokens.locator('small').first).to_contain_text('1 条参与')
                assert page.locator('#as-metrics .as-metric').count()==4
                assert page.locator('#as-metrics').get_by_text('核销完成',exact=True).count()==0
                assert '/单' not in tokens.inner_text()
                report['trimmedValuesAndMissingExclusion']=True
                page.locator('#as-overview-date-from').fill('2026-11-10')
                page.locator('#as-overview-date-to').fill('2026-11-10')
                expect(tokens.locator('strong')).to_have_text('样本不足')
                expect(duration.locator('strong')).to_have_text('样本不足')
                page.locator('#as-view-overview [data-as-clear-filters]').click()
                expect(tokens.locator('strong')).to_have_text('600 Token')
                page.locator('#as-overview-query').fill('a.zip 人员激励')
                expect(page.locator('[data-run-card]')).to_have_count(1)
                expect(tokens.locator('strong')).to_have_text('样本不足')
                page.locator('#as-overview-query').fill('没有匹配的材料')
                expect(page.locator('[data-run-card]')).to_have_count(0)
                expect(tokens.locator('strong')).to_have_text('暂无记录')
                page.locator('#as-view-overview [data-as-clear-filters]').click()
                expect(page.locator('#as-overview-query')).to_have_value('')
                expect(tokens.locator('strong')).to_have_text('600 Token')
                report['textSearchUpdatesRecordsAndAverages']=True
                assert page.locator('[data-as-filter="source"]').count()==0
                assert page.locator('[data-input-source]').count()==0
                assert page.get_by_text('本地导入',exact=True).count()==0
                assert page.get_by_text('系统上传',exact=True).count()==0
                page.locator('#as-overview-scenario').select_option('personnel_incentive')
                expect(tokens.locator('strong')).to_have_text('600 Token')
                page.locator('#as-overview-date-from').fill('2099-01-01')
                expect(tokens.locator('strong')).to_have_text('暂无记录')
                expect(duration.locator('strong')).to_have_text('暂无记录')
                page.locator('#as-view-overview [data-as-clear-filters]').click()
                report['dateTypeEmptyAndClearFilters']=True
                report['sourcesUnified']=True
                for width in [1440,1280]:
                    page.set_viewport_size({'width':width,'height':960})
                    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                    assert page.locator('#as-metrics .as-metric').evaluate_all('nodes => nodes.every(n => n.scrollWidth <= n.clientWidth)')
                    page.screenshot(path=str(args.output/f'isolated-{width}.png'))
                manifest=root/'20261112-averages-a'/'manifest.json'
                data=json.loads(manifest.read_text('utf-8'));data['completed_at']='2026-11-10T00:06:00Z';atomic_write_json(manifest,data)
                expect(duration.locator('strong')).to_have_text('4 分',timeout=20000)
                report['durationOnlyRefresh']=True
                if args.live_url:
                    page.goto(args.live_url)
                    expect(tokens).to_be_visible(timeout=30000)
                    runs=page.request.get(args.live_url.rstrip('/')+'/api/runs').json()['runs']
                    completed=[run for run in runs if run.get('status')=='completed']
                    values=sorted(run['token_usage']['total_tokens'] for run in completed if run.get('token_usage',{}).get('status')=='recorded' and type(run['token_usage'].get('total_tokens')) is int and run['token_usage']['total_tokens']>=0)
                    if len(values)>=3:expected=f'{math.floor(mean(values[1:-1])+0.5):,} Token'
                    else:expected='样本不足' if values else '暂无记录'
                    expect(tokens.locator('strong')).to_have_text(expected)
                    assert '/单' not in tokens.inner_text()
                    assert page.locator('#as-metrics').get_by_text('核销完成',exact=True).count()==0
                    times=sorted(run['audit_timing']['active_seconds'] for run in completed if isinstance(run.get('audit_timing'),dict) and type(run['audit_timing'].get('active_seconds')) in (int,float) and math.isfinite(run['audit_timing']['active_seconds']) and run['audit_timing']['active_seconds']>=0)
                    if len(times)>=3:
                        average=mean(times[1:-1]);seconds=math.floor(average)
                        hours,rest=divmod(seconds,3600);minutes,rest=divmod(rest,60)
                        expected_time='不到 1 秒' if seconds==0 and average>0 else ' '.join(part for part in [f'{hours} 小时' if hours else '',f'{minutes} 分' if minutes else '',f'{rest} 秒' if rest or not(hours or minutes) else ''] if part)
                    else:expected_time='样本不足' if times else '暂无记录'
                    expect(duration.locator('strong')).to_have_text(expected_time)
                    report['live']={'records':len(runs),'completed':len(completed),'tokenSamples':len(values),'durationSamples':len(times),'tokenDisplay':expected,'durationDisplay':expected_time,'apiVerified':True}
                    for width in [1440,1280]:
                        page.set_viewport_size({'width':width,'height':960})
                        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
                        assert page.locator('#as-metrics .as-metric').evaluate_all('nodes => nodes.every(n => n.scrollWidth <= n.clientWidth)')
                        page.screenshot(path=str(args.output/f'live-{width}.png'))
                browser.close()
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
    assert not errors,errors
    assert not blocked,blocked
    report.update({'pageErrors':errors,'blockedRequests':blocked,'modelCalls':0,'businessWrites':0})
    (args.output/'browser-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
