"""Query headings disclose saved business scope without inventing missing facts."""
import json
import subprocess
import unittest
from audit_core.workbench_runtime import ROOT_HTML


class QinceQueryPresentationTests(unittest.TestCase):
    def evaluate(self, cases):
        html=ROOT_HTML.read_text('utf-8')
        functions='const customerQinceQuery = '+html.split('      const customerQinceQuery = ',1)[1].split('      const customerTitle = ',1)[0]
        code='const technicalData = step => step.display_data || {};\n'+functions+'\nconst cases='+json.dumps(cases,ensure_ascii=True)+';console.log(JSON.stringify(cases.map(([data,response])=>{const step={type:"qince.request",display_data:data};return {...customerQinceQuery(step),detail:customerQinceQuerySummary(step,response)};})))'
        result=subprocess.run(['node','-e',code],capture_output=True,text=True,encoding='utf-8',check=True)
        return json.loads(result.stdout)

    def test_core_queries_keep_exact_identity_paging_and_actual_return_count(self):
        outputs=self.evaluate([
            [{'endpoint':'/api/cuxiao/verify/v1/queryVerificationRegular','business_parameters':{'verify_code':'HX-fixture','page':'2','rows':'100'}},{'type':'qince.response','display_data':{'row_count':1,'rows':[{'details':[{},{}]}]}}],
            [{'endpoint':'/api/cuxiao/v1/queryRegularSaleActivities','business_parameters':{'activity_code':'SQ-fixture','page_number':'1','page_size':'100'}},None],
            [{'endpoint':'/api/cuxiao/v1/queryRegularSale','business_parameters':{'plan_code':'PLAN-fixture'}},None],
        ])
        self.assertIn('核销单号：HX-fixture',outputs[0]['title'])
        self.assertIn('第 2 页，每页 100 条',outputs[0]['detail'])
        self.assertIn('返回 1 条记录，内含 2 条明细',outputs[0]['detail'])
        self.assertIn('活动申请单',outputs[1]['title']);self.assertIn('SQ-fixture',outputs[1]['title'])
        self.assertIn('尚未保存',outputs[1]['detail'])
        self.assertIn('活动方案',outputs[2]['title'])

    def test_large_customer_id_and_product_code_are_not_generic_queries(self):
        outputs=self.evaluate([
            [{'endpoint':'/api/v2/customer/query','business_parameters':{'id':'8384181264169217512'}},{'type':'qince.response','display_data':{'rows':[]}}],
            [{'endpoint':'/api/pd/v2/queryProduct','business_parameters':{'prd_code':'CP-FIXTURE'}},None],
            [{'endpoint':'/api/ai/v1/queryAiSkuCode','business_parameters':{}},{'type':'qince.response','display_data':{'row_count':112}}],
        ])
        self.assertIn('客户／门店主档',outputs[0]['title']);self.assertIn('8384181264169217512',outputs[0]['title'])
        self.assertIn('返回 0 条记录',outputs[0]['detail'])
        self.assertIn('商品主档',outputs[1]['title']);self.assertIn('CP-FIXTURE',outputs[1]['title'])
        self.assertIn('商品识别码与包装尺寸',outputs[2]['title'])
        self.assertIn('接口未设置业务筛选条件',outputs[2]['detail'])
        self.assertIn('返回 112 条记录',outputs[2]['detail'])

    def test_missing_unknown_and_failed_do_not_claim_query_success(self):
        outputs=self.evaluate([
            [{},None],
            [{'endpoint':'/unknown/future','business_parameters':{'future_filter':'kept-in-raw','api_key':'DO_NOT_DISPLAY'}},{'type':'qince.failed','display_data':{}}],
            [{'endpoint':'/api/v2/customer/query','business_parameters':{'id':8384181264169217512}},None],
            [{'method':'queryProductV2','business_parameters':{'prd_code':'CP-FAILED'}},{'type':'qince.failed','display_data':{}}],
        ])
        self.assertIn('未留存具体查询条件',outputs[0]['detail'])
        self.assertIn('具体查询类型未留存',outputs[1]['detail'])
        self.assertIn('本次查询未完成',outputs[1]['detail'])
        self.assertNotIn('DO_NOT_DISPLAY',json.dumps(outputs))
        self.assertNotIn('838418',outputs[2]['title'])
        self.assertIn('商品主档',outputs[3]['title']);self.assertIn('CP-FAILED',outputs[3]['title'])
        self.assertIn('本次查询未完成',outputs[3]['detail'])


if __name__=='__main__':unittest.main()
