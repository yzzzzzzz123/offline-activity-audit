"""Multi-source conclusions stay complete across worker collection and summary."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from audit_core.agent_orchestration import check_report, report_for, summary_reports
from audit_core.common import AuditError
from audit_core.model_authority import decision
from audit_core.model_metrics import collect_model_artifacts
from tests.test_agent_orchestration import FixtureProvider, fixture_case, offline_helpers


class MultiSourceReports(TestCase):
    def test_same_rule_keeps_different_source_conclusions_and_same_source_aspects(self):
        checks = [
            {'rule_id': 'pos_fields', 'status': 'fail', 'reason': '门店甲缺少商品编码。', 'source_ids': ['store-a']},
            {'rule_id': 'pos_fields', 'status': 'pass', 'reason': '门店乙字段齐全。', 'source_ids': ['store-b']},
            {'rule_id': 'pos_fields', 'status': 'fail', 'reason': '门店甲也缺少销售日期。', 'source_ids': ['store-a']},
        ]
        value = {'checks': deepcopy(checks)}
        case = {'archive_id': 'a01', '_agent_id': 'numbers',
                '_agent_rule_specs': [{'id': 'pos_fields'}],
                'documents': [{'unit_id': 'store-a'}, {'unit_id': 'store-b'}]}
        report = report_for(case, value)
        self.assertEqual(report['result']['checks'], checks)
        self.assertEqual(value['checks'], checks)
        self.assertEqual(report['pending_rule_ids'], [])

    def test_foreign_rule_still_rejected_with_its_identity(self):
        value = {'checks': [{'rule_id': 'contract_signed'}]}
        with self.assertRaisesRegex(AuditError, 'contract_signed'):
            check_report(value, ['pos_fields'])
        value = {'checks': [], 'photo_reviews': [{'unit_id': 'photo-a',
                 'checks': [{'rule_id': 'contract_signed'}]}]}
        with self.assertRaisesRegex(AuditError, 'contract_signed'):
            check_report(value, ['photo_authenticity'])

    def test_multisource_worker_collection_preserves_both_statuses_in_summary(self):
        class Provider(FixtureProvider):
            def _call_stage(self, *args, **kwargs):
                value = super()._call_stage(*args, **kwargs)
                prompt = args[3] if len(args) > 3 else kwargs['prompt']
                import json
                request = json.loads(prompt.partial_variables.get('request', '{}'))
                if request.get('agent_id') == 'numbers' and 'rules' in request:
                    original = next(check for check in value['checks'] if check['rule_id'] == 'pos_fields')
                    original.update(status='fail', reason='门店甲缺少商品编码。', source_ids=['pos_excel'])
                    value['checks'].append({**deepcopy(original), 'status': 'pass',
                        'reason': '门店乙字段齐全。', 'source_ids': ['settlement']})
                return value

        with TemporaryDirectory() as folder, ExitStack() as stack:
            offline_helpers(stack)
            stack.enter_context(collect_model_artifacts(Path(folder) / 'analysis'))
            provider = Provider()
            case = fixture_case('self_procured_gift_material')
            evidence = provider.audit(case, Path(folder) / 'audit')
            checks = [check for check in evidence['checks'] if check['rule_id'] == 'pos_fields']
            self.assertEqual([check['status'] for check in checks], ['fail', 'pass'])
            reports = summary_reports(provider)
            saved = next(report for report in reports if report['agent_id'] == 'numbers')
            self.assertEqual([check for check in saved['checks'] if check['rule_id'] == 'pos_fields'], checks)
            result = decision(case['scenario'], evidence, case['documents'])
            self.assertEqual([check['status'] for check in result['checks']
                              if check['rule_id'] == 'pos_fields'], ['fail', 'pass'])
