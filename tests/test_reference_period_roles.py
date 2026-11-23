from copy import deepcopy
import json
from pathlib import Path
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_decision, audit_schema, validate
from tests.pdf_test_support import flags_for
from tests.test_audit_rag_contract import case_for


class ReferencePeriodRoleTests(unittest.TestCase):
    def test_schema_rejects_qince_in_observed_contract_period(self):
        case,_=case_for(1)
        schema=audit_schema(case['scenario'],materials=case['materials'],units=case['units'])
        item=schema['properties']['checks']['items']
        check={'rule_id':'contract_activity_dates','status':'pass','reason':'活动周期一致',
            'source_ids':['contract'],'comparisons':[],
            'reference_evidence':{'written_name':None,'seal_name':None,'periods':[
                {'unit_id':case['units'][0]['unit_id'],'quote':'startDate="2026-09-01"'}]}}
        with self.assertRaises(AuditError):validate(check,item)
        check['reference_evidence']['periods']=[{'unit_id':'contract','quote':case['documents'][-2]['facts'][0]}]
        validate(check,item)

    def test_saved_real_failed_dates_after_role_correction_validate_without_model(self):
        root=Path(__file__).resolve().parents[1]/'worktrees'/(
            'HX202610150048-商丘恒迪商贸有限公司8.22-9.5活动申请-9-20261109_1454_16')
        if not root.exists():self.skipTest('Private original trace not in source checkout')
        traces=[json.loads(p.read_text(encoding='utf-8')) for p in sorted((root/'analysis/execution-trace').glob('step-*.json'))]
        failed=next(t for t in reversed(traces) if t['type']=='ai.validation_failed' and 'dates' in t['label'])
        request=next(t for t in reversed(traces) if t['type']=='ai.request' and t['call_id']==failed['call_id'])
        documents=json.loads(next(line.split('：',1)[1] for line in request['data']['input_text'].splitlines()
            if line.startswith('本批完整对应原始事实：')))
        materials=[{'id':'promotion_contract','state':'present','source_ids':['a001-f0002']},
                   {'id':'settlement','state':'present','source_ids':['a001-f0003']}]
        units=[{'unit_id':d['unit_id'],'source_kind':'qince' if '-qince-' in d['unit_id'] else 'submitted',
                'source_file':'勤策申请明细' if '-qince-' in d['unit_id'] else d['unit_id']+'.jpg'} for d in documents]
        original=json.loads(failed['data']['visible_response'])
        schema=audit_schema('promotional_display',materials=materials,units=units,include_photo_reviews=False)
        for check in original['checks']:
            if check['rule_id'] in {'contract_activity_dates','settlement_application_dates'}:
                with self.assertRaises(AuditError):validate(check,schema['properties']['checks']['items'])
        corrected=deepcopy(original)
        for check in corrected['checks']:
            check['reference_evidence']['periods']=[p for p in check['reference_evidence']['periods']
                                                   if '-qince-' not in p['unit_id']]
        result=audit_decision('promotional_display',flags_for('promotional_display'),corrected,
            documents,materials,units=units,selected_rule_ids=[c['rule_id'] for c in corrected['checks']])
        self.assertTrue(all(c['status']=='pass' for c in result['checks']))
        # Native dates remain checked independently against the real Qince values.
        different=deepcopy(corrected)
        contract=next(d for d in documents if d['unit_id']=='a001-f0002')
        contract['facts'].append('活动执行周期：2026年8月21日至2026年9月5日')
        target=next(c for c in different['checks'] if c['rule_id']=='contract_activity_dates')
        target['reference_evidence']['periods'].append({'unit_id':'a001-f0002','quote':contract['facts'][-1]})
        result=audit_decision('promotional_display',flags_for('promotional_display'),different,
            documents,materials,units=units,selected_rule_ids=[c['rule_id'] for c in different['checks']])
        self.assertEqual(next(c for c in result['checks'] if c['rule_id']=='contract_activity_dates')['status'],'fail')
