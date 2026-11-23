"""Exercise original previews from submitted materials and both result sections."""
import argparse,hashlib,io,json,tempfile,threading,zipfile
from pathlib import Path
from unittest.mock import patch
from playwright.sync_api import sync_playwright,expect
from audit_core.workbench_server import WorkbenchCatalog,WorkbenchHTTPServer,Handler
from audit_core.workbench_store import WorkbenchRunStore
from audit_core.workbench_runtime import ROOT_HTML
from tests.test_pass_check_details import PassCheckDetailsTests

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args();args.output.mkdir(parents=True,exist_ok=True)
    import openpyxl
    from PIL import Image
    book=openpyxl.Workbook();sheet=book.active;sheet.title='销售明细';sheet.append(['原始商品','原始数量'])
    for i in range(104):sheet.append(['商品'+str(i),i])
    data=io.BytesIO();book.save(data);book.close()
    photo=io.BytesIO();Image.new('RGB',(120,80),'#068b74').save(photo,format='PNG')
    files={'POS.xlsx':data.getvalue(),'照片.png':photo.getvalue(),'原件.txt':'本次提交的原始文字 <script>bad()</script>'.encode()}
    errors=[];writes=[];checks=[]
    with tempfile.TemporaryDirectory(prefix='material-preview-browser-') as temporary,patch('audit_core.qwen_model.complete_json',side_effect=AssertionError('No model calls for preview')):
        root=Path(temporary);inputs=root/'input';inputs.mkdir();archive=inputs/'原始资料.zip'
        with zipfile.ZipFile(archive,'w') as z:
            for name,raw in files.items():z.writestr(name,raw)
        workspace=root/'worktrees'/'preview-fixture'
        store=WorkbenchRunStore(workspace,run_id='20261112-preview',producer_model='fixture',root_html=ROOT_HTML,workbench_url='http://127.0.0.1:8080/')
        inventory={'a001':{'source_archive':'原始资料.zip','archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'inventory':[{'file_id':'a001-f'+str(i),'source_file':name,'suffix':Path(name).suffix,'sha256':hashlib.sha256(raw).hexdigest()} for i,(name,raw) in enumerate(files.items(),1)]}}
        store.write_analysis('input-cases.json',inventory,label='本次材料清单')
        view,_=PassCheckDetailsTests().fixture()
        view['sheets'][0]['rows'].append({'status':'issue','rule_id':'photo','values':['照片.png','图中缺少日期'],'card_evidence':{'source_files':['照片.png']}})
        store.complete(view_payload=view,scenarios=['personnel_incentive'],verification={'verified':True})
        before={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
        server=WorkbenchHTTPServer(('127.0.0.1',0),Handler,catalog=WorkbenchCatalog(root/'worktrees'));thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        try:
            with sync_playwright() as p:
                browser=p.chromium.launch(channel='chrome',headless=True,args=['--no-proxy-server']);page=browser.new_page(viewport={'width':1440,'height':960})
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request:writes.append(request.method+' '+request.url) if request.method not in ['GET','HEAD'] else None)
                page.goto(f'http://127.0.0.1:{server.server_address[1]}/?run=preview-fixture')
                page.locator('#as-record-materials').click()
                page.locator('[data-as-preview-id="a001-f2"]').click()
                dialog=page.get_by_role('dialog');expect(dialog).to_be_visible();expect(dialog.locator('img')).to_be_visible()
                assert dialog.locator('img').evaluate('e=>e.complete&&e.naturalWidth===120')
                expect(dialog).to_contain_text('不产生 Token 消耗');page.screenshot(path=str(args.output/'image-preview.png'))
                page.keyboard.press('Escape');expect(dialog).to_have_count(0);expect(page.locator('[data-as-preview-id="a001-f2"]')).to_be_focused();checks.append('imageAndFocusRestoration')
                page.locator('[data-as-preview-id="a001-f1"]').click();frame=page.frame_locator('.as-material-preview iframe')
                expect(frame.locator('body')).to_contain_text('原始商品');expect(frame.locator('body')).to_contain_text('105 行')
                frame.get_by_role('link',name='下一页').first.click();expect(frame.locator('body')).to_contain_text('商品103');page.screenshot(path=str(args.output/'spreadsheet-preview.png'))
                page.get_by_role('button',name='关闭材料预览').click();checks.append('spreadsheetPagingWithoutModel')
                page.locator('[data-as-preview-id="a001-f3"]').click();expect(frame.locator('body')).to_contain_text('<script>bad()</script>');assert frame.locator('script').count()==0
                page.get_by_role('button',name='关闭材料预览').click();checks.append('escapedText')
                page.locator('#eo-tab-home').click();button=page.locator('#home [data-as-preview-name="照片.png"]').first;expect(button).to_be_visible();button.click();expect(page.get_by_role('dialog').locator('img')).to_be_visible();page.keyboard.press('Escape');checks.append('errorItemPreview')
                page.locator('#eo-tab-passed').click();button=page.locator('#passed [data-as-preview-name="POS.xlsx"]').first;expect(button).to_be_visible();button.click();expect(frame.locator('body')).to_contain_text('原始商品');page.get_by_role('button',name='关闭材料预览').click();checks.append('correctItemPreview')
                assert page.locator('.eo-pass-group-head').count()==0
                browser.close()
        finally:server.shutdown();server.server_close();thread.join(5)
        assert before=={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file()}
    assert not errors,errors;assert not writes,writes
    result={'passed':True,'checks':checks,'javascriptErrors':errors,'businessWrites':writes,'modelCalls':0,'historyUnchanged':True};(args.output/'verification.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(result))

if __name__=='__main__':main()
