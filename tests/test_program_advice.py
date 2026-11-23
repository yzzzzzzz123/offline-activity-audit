"""A helper cannot veto model-owned evidence before final logic review."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.program_advice import schema_advice
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_judgment import save_judgments, provisional
from audit_core.pdf_evidence import validate, audit_schema
from audit_core.pdf_policy import rules
from audit_core.pdf_workflow import PdfWorkflow
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.pdf_evidence import audit_decision
from audit_core.final_logic_review import context, SKILL
from tests.test_final_logic_stage import fixture, accepted
from tests.pdf_test_support import unknown_checks


def repeated_case():
    case, packet, provider = fixture('price_difference_support')
    case['documents'].append({'unit_id': 'pos-original', 'roles': ['POS销售数据'],
        'facts': ['申报销量8', '甲店销量4', '乙店销量4'], 'limitations': []})
    case['units'].append({'unit_id': 'pos-original', 'source_file': '销售.xlsx', 'locator': 'Sheet1', 'image': None})
    case['materials'].append({'id': 'pos_excel', 'state': 'present', 'source_ids': ['pos-original'], 'reason': '隔离原表'})
    packet['audit_evidence'] = unknown_checks(case['scenario'], case['flags'], materials=case['materials'], units=case['units'])
    check = next(c for c in packet['audit_evidence']['checks'] if c['rule_id'] == 'settlement_pos_quantity')
    operand = {'unit_id': 'pos-original', 'quote': '销量4', 'number': '4'}
    check.update(status='pass', reason='两家店各4支合计8支，与申报一致。', source_ids=['pos-original'],
        comparisons=[{'label': '结算与全量POS', 'value_kind': 'quantity', 'operator': 'eq', 'operation': 'sum',
            'left': {'unit_id': 'pos-original', 'quote': '申报销量8', 'number': '8'},
            'right': [deepcopy(operand), deepcopy(operand)]}])
    schema = audit_schema(case['scenario'], materials=case['materials'], include_photo_reviews=False, model_output=False)
    return case, packet, provider, schema


class ProgramAdviceTests(TestCase):
    def test_repeated_reference_diagnostic_identifies_the_actual_rule_without_echoing_materials(self):
        case, packet, _, schema = repeated_case()
        original = deepcopy(packet['audit_evidence'])
        advice = schema_advice(original, schema)
        issue = next(i for i in advice['issues'] if i['constraint'] == 'uniqueItems')
        self.assertEqual(issue['rule_id'], 'settlement_pos_quantity')
        self.assertEqual(issue['path'], 'checks/20/comparisons/0/right')
        self.assertEqual(issue['duplicate_positions'], [[0, 1]])
        self.assertEqual(issue['item_count'], 2)
        self.assertNotIn('甲店', json.dumps(advice, ensure_ascii=False))
        self.assertEqual(packet['audit_evidence'], original)
        self.assertFalse(advice['authoritative'])

    def test_equal_numbers_from_different_rows_are_never_deduplicated(self):
        _, packet, _, schema = repeated_case()
        operands = packet['audit_evidence']['checks'][20]['comparisons'][0]['right']
        for operand, quote in zip(operands, ['甲店销量4', '乙店销量4']):
            operand['quote'] = quote
        self.assertEqual(schema_advice(packet['audit_evidence'], schema)['issues'], [])
        self.assertEqual([x['number'] for x in operands], ['4', '4'])

    def test_strict_component_validator_stays_strict_but_reports_the_rule_and_constraint(self):
        case, packet, _, schema = repeated_case()
        with self.assertRaises(AuditError) as raised:
            validate(packet['audit_evidence'], schema)
        detail = raised.exception.validation_detail
        self.assertEqual(detail['rule_id'], 'settlement_pos_quantity')
        self.assertEqual(detail['constraint'], 'uniqueItems')
        result = provisional(case, packet['audit_evidence'], [r.id for r in rules(case['scenario'])],
            lambda: validate(packet['audit_evidence'], schema))
        self.assertEqual(result['program_advice']['issues'], [detail])
        self.assertEqual(result['checks'], packet['audit_evidence']['checks'])

    def test_model_owned_assembly_keeps_original_judgment_at_summary_only_final_stage(self):
        case, packet, provider, schema = repeated_case()
        original = deepcopy(packet['audit_evidence'])
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            save_judgments(case, original, [r.id for r in rules(case['scenario'])])
            provider._validate_audit_assembly(case, original, schema)
            self.assertEqual(original, packet['audit_evidence'])
            flow = PdfWorkflow('isolated-repeated-references', 'fixture', folder, Path(folder)/'model',
                provider, None, None, packets=[packet])
            flow.evidence(); flow.decision()
            request, _, _ = context([packet], provider)
            advice = request['archives'][0]['stage_advice'][0]
            self.assertTrue(advice['soft_gate'])
            self.assertEqual(advice['rule_ids'], ['settlement_pos_quantity'])
            self.assertEqual(advice['issues'][0]['constraint'], 'uniqueItems')
            rounds = []
            def call(root, skill, output_schema, prompt, **kwargs):
                self.assertEqual(skill, SKILL)
                self.assertEqual(kwargs['images'], [])
                request = json.loads(prompt.partial_variables['request'])
                self.assertNotIn('archives', request)
                rounds.append(root)
                return {'ready': True, 'issues': [], 'items': [
                    {'source_problem_ids': [p['id']], 'text': p['text'], 'uncertainty_preserved': True}
                    for p in request['problems']]}
            with patch.object(provider, '_call', side_effect=call):
                flow.logic_review()
            self.assertEqual(flow.logic_review_result['status'], 'passed')
            self.assertEqual(len(rounds), 1)
            revised = packet['audit_evidence']['checks'][20]
            self.assertEqual(revised['status'], 'pass')
            self.assertEqual([x['number'] for x in revised['comparisons'][0]['right']], ['4', '4'])
            self.assertEqual(revised, original['checks'][20])
            for index, check in enumerate(original['checks']):
                if index != 20:
                    self.assertEqual(packet['audit_evidence']['checks'][index], check)
            observations = json.loads((Path(folder)/'analysis/model-observations/program-assembly-a001.json').read_text('utf-8'))
            self.assertEqual(observations['original_model_result'], original)

    def test_assembly_rejects_foreign_sources_and_retains_unmatched_quotes_for_model_review(self):
        for foreign in [True, False]:
            with self.subTest(foreign=foreign):
                case, packet, provider, schema = repeated_case()
                operand = packet['audit_evidence']['checks'][20]['comparisons'][0]['right'][0]
                operand.update({'unit_id': 'another-run'} if foreign else {'quote': '原文没有的引用'})
                original=deepcopy(packet['audit_evidence'])
                if foreign:
                    with self.assertRaises(AuditError):
                        provider._validate_audit_assembly(case, packet['audit_evidence'], schema)
                    self.assertEqual(provider._soft_check_advice, [])
                else:
                    provider._validate_audit_assembly(case, packet['audit_evidence'], schema)
                    advice=provider._soft_check_advice[-1]
                    self.assertTrue(advice['soft_gate'])
                    issue=next(i for i in advice['issues'] if i['constraint']=='quote_source_match')
                    self.assertEqual(issue['unit_id'],'pos-original')
                    self.assertEqual(issue['quote'],'原文没有的引用')
                self.assertEqual(packet['audit_evidence'],original)

    def test_advice_does_not_create_completed_model_or_final_review_authority(self):
        case, packet, provider, schema = repeated_case()
        with TemporaryDirectory() as folder, collect_model_artifacts(Path(folder)/'analysis'):
            provider._validate_audit_assembly(case, packet['audit_evidence'], schema)
            self.assertFalse((Path(folder)/'analysis/model-judgments').exists())
            self.assertEqual(packet['audit_evidence']['checks'][20]['status'], 'pass')

    def test_normal_assembly_does_not_add_advice_or_duplicate_model_calls(self):
        case, packet, provider, schema = repeated_case()
        for operand, quote in zip(packet['audit_evidence']['checks'][20]['comparisons'][0]['right'], ['甲店销量4', '乙店销量4']):
            operand['quote'] = quote
        with patch.object(provider, '_call', side_effect=AssertionError('no extra model calls')):
            provider._validate_audit_assembly(case, packet['audit_evidence'], schema)
        self.assertEqual(provider._soft_check_advice, [])

    def test_normal_provider_does_not_enter_legacy_assembly_validation(self):
        from tests.test_audit_rag_capacity import CapacityRecoveryTests
        case,packet,provider,_=repeated_case()
        runner=CapacityRecoveryTests()
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._validate_audit_assembly',side_effect=AssertionError('business gate')):
            result=runner._run(case,lambda ids:False)
        self.assertEqual(len(runner.calls),1)
        self.assertTrue(result['checks'])

    def test_schema_const_diagnostics_do_not_echo_source_values(self):
        advice = schema_advice({'private_field': 'actual'}, {'properties': {'private_field': {'const': 'PRIVATE-EXPECTED-CONTENT'}}})
        self.assertNotIn('PRIVATE-EXPECTED-CONTENT', json.dumps(advice))

    def test_duplicate_positions_follow_json_number_and_boolean_equality(self):
        value = [{'n': 1}, {'n': 1.0}, {'n': True}, {'n': '1'}]
        advice = schema_advice(value, {'uniqueItems': True})
        self.assertEqual(advice['issues'][0]['duplicate_positions'], [[0, 1]])
        self.assertEqual(len(value), 4)
