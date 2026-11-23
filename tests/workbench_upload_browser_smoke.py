"""Real browser + HTTP ZIP upload; isolated queue/runner, no model/OSS/callback."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import sync_playwright, expect

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from tests.test_local_upload import LocalUploadTests, archive


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--out',type=Path,default=ROOT/'artifacts/upload-module-20261026/browser')
    out=parser.parse_args().out;out.mkdir(parents=True,exist_ok=True)
    fixture=LocalUploadTests();fixture.setUp();fixture.release.clear()
    errors=[];checks=[];upload_requests=[]
    try:
        with sync_playwright() as p:
            browser=p.chromium.launch(channel='chrome',headless=True,args=['--no-proxy-server'])
            page=browser.new_page(viewport={'width':1440,'height':960})
            page.on('pageerror',lambda error:errors.append(str(error)))
            page.on('request',lambda request:upload_requests.append(parse_qs(urlsplit(request.url).query))
                    if request.method=='POST' and urlsplit(request.url).path=='/api/intake/upload' else None)
            page.goto(fixture.base,wait_until='networkidle')
            expect(page.get_by_role('heading',name='核销管理中心').first).to_be_visible()
            page.locator('#as-tab-upload').click()
            expect(page.locator('#as-view-upload')).to_be_visible()
            expect(page.locator('#as-upload-type')).to_be_enabled()
            expect(page.locator('#as-upload-type option')).to_have_count(9)
            expect(page.locator('#as-upload-type')).to_have_value('')
            expect(page.locator('#as-upload-verify-code')).to_have_value('')
            expect(page.locator('#as-upload-analyze-id')).to_have_value('')
            expect(page.locator('#as-upload-submit')).to_be_disabled()
            for width in (1440,1280,850,390):
                page.set_viewport_size({'width':width,'height':960})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'),width
                expect(page.locator('#as-upload-drop')).to_be_visible()
                page.screenshot(path=str(out/f'upload-{width}.png'),full_page=True)
            page.set_viewport_size({'width':1440,'height':960})
            checks.append('Upload is a fourth independent module; 1440/1280/850/390 have no horizontal overflow; type starts empty.')

            page.locator('#as-upload-file').set_input_files({'name':'说明.txt','mimeType':'text/plain','buffer':b'not zip'})
            expect(page.locator('#as-upload-message')).to_contain_text('ZIP')
            expect(page.locator('#as-upload-submit')).to_be_disabled()
            with page.expect_file_chooser() as chosen:page.locator('#as-upload-drop').click()
            chosen.value.set_files({'name':'隔离核销资料.zip','mimeType':'application/zip','buffer':archive()})
            expect(page.locator('#as-upload-filename')).to_have_text('隔离核销资料.zip')
            expect(page.locator('#as-upload-submit')).to_be_disabled()
            page.locator('#as-upload-type').select_option('陈列堆头')
            expect(page.locator('#as-upload-venue-field')).to_be_visible()
            expect(page.locator('#as-upload-venue')).to_have_value('')
            page.locator('#as-upload-type').select_option('条码费')
            expect(page.locator('#as-upload-venue-field')).to_be_hidden()
            expect(page.locator('#as-upload-channel-field')).to_be_visible()
            expect(page.locator('#as-upload-channel')).to_have_value('false')
            page.locator('#as-upload-channel').select_option('true')
            for width in (1440, 1280):
                page.set_viewport_size({'width': width, 'height': 960})
                expect(page.locator('#as-upload-channel-field')).to_be_visible()
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1'), width
                page.screenshot(path=str(out/f'entry-channel-{width}.png'), full_page=True)
            page.set_viewport_size({'width': 1440, 'height': 960})
            expect(page.locator('#as-upload-submit')).to_be_enabled()
            page.locator('#as-upload-file').set_input_files({'name':'损坏资料.zip','mimeType':'application/zip','buffer':b'not a zip'})
            page.locator('#as-upload-submit').click()
            expect(page.locator('#as-upload-message')).to_have_attribute('data-error','true')
            expect(page.locator('#as-upload-message')).to_contain_text('ZIP')
            expect(page.locator('#as-upload-submit')).to_be_enabled()
            checks.append('ZIP-only chooser and type selection work; optional venue flag is never defaulted; a corrupt ZIP shows a recoverable server error.')

            page.locator('#as-upload-verify-code').fill('HX-STALE')
            page.locator('#as-upload-analyze-id').fill('8')
            page.locator('#as-upload-file').set_input_files({'name':'隔离核销资料.zip','mimeType':'application/zip','buffer':archive()})
            expect(page.locator('#as-upload-verify-code')).to_have_value('')
            expect(page.locator('#as-upload-analyze-id')).to_have_value('')
            before=len(upload_requests)
            for invalid in ('中文', '../other', 'x'*81):
                page.locator('#as-upload-verify-code').fill(invalid)
                page.locator('#as-upload-submit').click()
                expect(page.locator('#as-upload-message')).to_contain_text('核销单号须')
                assert len(upload_requests)==before and not fixture.intake.list()
            page.locator('#as-upload-verify-code').fill('  HX-SYNTHETIC  ')
            for invalid in ('0', '-1', '1.2', '01', 'true', '9223372036854775808'):
                page.locator('#as-upload-analyze-id').fill(invalid)
                page.locator('#as-upload-submit').click()
                expect(page.locator('#as-upload-message')).to_contain_text('记录编号须')
                assert len(upload_requests)==before and not fixture.intake.list()
            page.locator('#as-upload-analyze-id').fill('9223372036854775807')
            page.locator('#as-upload-submit').click()
            expect(page.locator('#as-upload-result')).to_be_visible(timeout=15000)
            expect(page.locator('#as-upload-result-text')).to_contain_text('资料已保存并进入核销队列')
            expect(page.locator('#as-upload-type')).to_have_value('')
            expect(page.locator('#as-upload-verify-code')).to_have_value('')
            expect(page.locator('#as-upload-analyze-id')).to_have_value('')
            assert fixture.entered.wait(3)
            first=fixture.intake.list()[0]
            assert first['input_source']=='input' and first['bizType']=='条码费'
            assert first['verifyCode']=='HX-SYNTHETIC' and first['analyzeId']==9223372036854775807
            assert fixture.calls[0]['verify_code']=='HX-SYNTHETIC'
            assert upload_requests[-1]['verifyCode']==['HX-SYNTHETIC']
            assert upload_requests[-1]['analyzeId']==['9223372036854775807']
            assert first['cvsOtcChannel'] is True and fixture.calls[0]['cvs_otc_channel'] is True
            expect(page.locator('#as-upload-channel-field')).to_be_hidden()
            assert Path(first['delivery']['input_file']).read_bytes()==archive()
            page.screenshot(path=str(out/'upload-success.png'),full_page=True)
            page.locator('#as-upload-result [data-as-upload-overview]').click()
            expect(page.locator(f'[data-intake-job="{first["job_id"]}"]')).to_contain_text('本地导入')
            checks.append('The browser sent actual ZIP bytes with its Origin/token; the existing queue shows local provenance, and the original bytes are retained.')
            checks.append('Optional identity reaches the existing queue/runner; invalid identity sends no HTTP request; 64-bit record IDs retain exact digits; replacing a ZIP and successful submission clear identity.')

            page.locator('#as-tab-upload').click()
            page.locator('#as-upload-file').set_input_files({'name':'第二份资料.zip','mimeType':'application/zip','buffer':archive()})
            page.locator('#as-upload-verify-code').fill('HX-REMOVE')
            page.locator('#as-upload-analyze-id').fill('9')
            page.locator('#as-upload-clear').click()
            expect(page.locator('#as-upload-verify-code')).to_have_value('')
            expect(page.locator('#as-upload-analyze-id')).to_have_value('')
            page.locator('#as-upload-file').set_input_files({'name':'第二份资料.zip','mimeType':'application/zip','buffer':archive()})
            page.locator('#as-upload-type').select_option('条码费')
            expect(page.locator('#as-upload-channel')).to_have_value('false')
            page.locator('#as-upload-submit').click()
            expect(page.locator('#as-upload-result')).to_be_visible()
            expect(page.locator('#as-upload-result-text')).to_contain_text('第二份资料.zip')
            second=next(job for job in fixture.intake.list() if job['job_id']!=first['job_id'])
            assert 'verifyCode' not in upload_requests[-1] and 'analyzeId' not in upload_requests[-1]
            assert second['verifyCode'] is None and second['analyzeId'] is None
            assert second['status']=='downloaded' and len(fixture.calls)==1
            page.locator('#as-tab-overview').click()
            queued=page.locator(f'[data-intake-job="{second["job_id"]}"]')
            expect(queued).to_contain_text('排队中')
            queued.locator('[data-as-delete-job]').click()
            expect(page.locator('#as-delete-dialog')).to_be_visible()
            page.locator('#as-delete-confirm').click()
            expect(queued).to_have_count(0)
            assert not (fixture.root/'input'/second['job_id']).exists()
            fixture.release.set();assert fixture.wait(first['job_id'])['status']=='completed'
            assert Path(first['delivery']['input_file']).is_file()
            assert len(fixture.calls)==1 and not fixture.callbacks and not fixture.downloads
            checks.append('Two uploads remain serial; queued deletion removes only that attempt; completing the first preserves its ZIP and never calls OSS or upstream callback.')
            page.locator('#as-tab-upload').click();page.reload(wait_until='networkidle')
            expect(page.locator('#as-tab-overview')).to_have_attribute('aria-selected','true')
            page.locator('#as-tab-upload').click()
            expect(page.locator('#as-tab-upload')).to_have_attribute('aria-selected','true')
            expect(page.locator('#as-upload-type')).to_be_enabled()
            expect(page.locator('#as-upload-type')).to_have_value('')
            assert not errors,errors;browser.close()
    finally:fixture.tearDown()
    report={'checks':checks,'page_errors':errors,'database_and_files':'temporary only',
            'real_model_calls':0,'oss_downloads':0,'business_callbacks':0,'runner':'isolated fixture on existing serial worker'}
    (out/'result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
