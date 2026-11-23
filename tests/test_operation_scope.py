import json,subprocess,unittest
from audit_core.workbench_runtime import ROOT_HTML

class OperationScopeTests(unittest.TestCase):
    def evaluate(self,steps):
        html=ROOT_HTML.read_text('utf-8');source='const customerOperationScope = '+html.split('      const customerOperationScope = ',1)[1].split('      const customerFailure = ',1)[0]
        code='const technicalData=s=>s.data||{};const parseAuditJson=JSON.parse;const customerImageSources=new Map([["'+('a'*64)+'",new Set(["a001-f1"])]]);const customerSourceInventory=new Map([["a001-f1",{source_file:"合同/原件.png"}]]);const customerSourceUnits=new Map();const customerSourceLabels=new Map();'+source+'console.log(JSON.stringify('+json.dumps(steps,ensure_ascii=True)+'.map(s=>customerOperationScope(s,s.terminal))));'
        return json.loads(subprocess.run(['node','-e',code],capture_output=True,encoding='utf-8',check=True).stdout)

    def test_image_scope_uses_exact_saved_fingerprint(self):
        found,unknown=self.evaluate([{'type':'ai.request','label':'核销资料内容读取 第1批 单图1/3','data':{'images':[{'sha256':'a'*64}],'input_text':'只提取本批原始页面的可见事实'},'terminal':{'status':'completed'}},{'type':'ai.request','data':{'images':[{'sha256':'b'*64}]}}])
        self.assertIn('合同/原件.png',found);self.assertIn('逐张读取',found);self.assertIn('已有返回',found)
        self.assertNotIn('合同/原件.png',unknown);self.assertIn('尚未留存',unknown);self.assertIn('尚未保存',unknown)

    def test_self_test_and_reference_summary_disclose_actual_scope(self):
        selftest,references=self.evaluate([{'type':'ai.request','label':'模型自主阶段自测','data':{'input_text':'本阶段原始事实与辅助结果：'+json.dumps({'model_result':{'checks':[{'status':'unknown'},{'status':'fail'},{'status':'pass'}]}})}},{'type':'qince.references','data':{'verify_code':'HX-TEST','reference_needs':['products','terminals'],'query_count':3,'rows':[{'status':'available'},{'status':'empty_or_unlinked'},{'status':'failed'}]}}])
        self.assertIn('3 项阶段判断',selftest);self.assertIn('1 项暂不能确认、1 项不符合',selftest)
        self.assertIn('商品资料、客户／门店主档',references);self.assertIn('实际发起 3 次查询',references);self.assertIn('1 项取得有效参照，1 项为空或未关联',references)

if __name__=='__main__':unittest.main()
