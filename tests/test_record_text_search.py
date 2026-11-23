"""Search literal record metadata across the full unified catalog."""
import json
import subprocess
import unittest
from audit_core.workbench_runtime import ROOT_HTML


class RecordTextSearchTests(unittest.TestCase):
    def test_literal_unicode_names_ids_types_and_multiple_terms(self):
        html=ROOT_HTML.read_text('utf-8')
        functions='const normalizedRecordSearch = '+html.split('      const normalizedRecordSearch = ',1)[1].split('      const filteredRuns = ',1)[0]
        run={'display_name':'诚成门店陈列活动','source_archives':['合同/HX202611100001-资料[原件].zip'],'run_id':'20261110-fixture','workspace_id':'history-101','scenarios':['promotional_display']}
        queries=['诚成','hx202611100001','ＨＸ２０２６１１１００００１','诚成 陈列','资料[原件]','history-101','20261110-fixture','不存在','诚成 人员激励','.*','   ']
        code='const runTitle = run => run.display_name || "";const scenarioName = value => ({promotional_display:"陈列堆头"}[value] || "");\n'+functions+'\nconst run='+json.dumps(run,ensure_ascii=True)+';const queries='+json.dumps(queries,ensure_ascii=True)+';console.log(JSON.stringify(queries.map(query=>recordMatchesQuery(run,query))));'
        result=subprocess.run(['node','-e',code],capture_output=True,text=True,encoding='utf-8',check=True)
        self.assertEqual(json.loads(result.stdout),[True]*7+[False,False,False,True])

    def test_legacy_source_filters_are_cleared_and_search_state_is_bounded(self):
        html=ROOT_HTML.read_text('utf-8')
        function='const normalizeRecordFilters = '+html.split('      const normalizeRecordFilters = ',1)[1].split('      const recordFiltersMarkup = ',1)[0]
        code='const scenarioLabels={promotional_display:"陈列堆头"};\n'+function+'''\nconsole.log(JSON.stringify([normalizeRecordFilters({source:'input-oss',query:'诚成',scenario:'promotional_display'}),normalizeRecordFilters({source:'input',query:'x'.repeat(201)}),normalizeRecordFilters({query:{invalid:1}})]));'''
        result=subprocess.run(['node','-e',code],capture_output=True,text=True,encoding='utf-8',check=True)
        first,long_query,invalid=json.loads(result.stdout)
        self.assertEqual(first['source'],'');self.assertEqual(first['query'],'诚成')
        self.assertEqual(first['scenario'],'promotional_display')
        self.assertEqual(long_query['source'],'');self.assertEqual(len(long_query['query']),200)
        self.assertEqual(invalid['query'],'')


if __name__=='__main__':unittest.main()
