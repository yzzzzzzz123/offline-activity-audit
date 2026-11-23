"""Real material-gate validators and retry transport with synthetic data only."""
from tests.pdf_test_support import isolated_stage_summary
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import PropertyMock, patch

from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.model_metrics import GATE_VALIDATION_CODES, collect_model_artifacts, record_model_attempt
from audit_core.model_settings import api_selection
from audit_core.pdf_evidence import classification_schema, validate_classification
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.qwen_model import extract_qwen_json
from tests.pdf_test_support import classification


MARKER = '以下是本次资料门禁失败字段'


class QwenGateRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='audit-gate-retry-')
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.documents = [
            {'unit_id': 'contract', 'roles': ['contract'],
             'facts': ['PRIVATE_GATE_CONTRACT：本次促销奖励单价  2.00 元'], 'limitations': []},
            {'unit_id': 'claim', 'roles': ['settlement'],
             'facts': ['PRIVATE_GATE_CLAIM：本次申报奖励金额200元'], 'limitations': []},
        ]
        self.good = classification('personnel_incentive', ['contract', 'claim'])
        selected = next(m for m in self.good['material_matches'] if m['scenario'] == 'personnel_incentive')
        self.good.update(material_matches=[selected], candidate_scenarios=['personnel_incentive'],
                         flags=deepcopy(selected['flags']), materials=deepcopy(selected['materials']))
        validate_classification(self.good, self.documents, 'personnel_incentive')
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps(classification_schema('personnel_incentive')), encoding='utf-8')
        self.prompts = []
        self.output = io.StringIO()
        self.selection = api_selection({'provider': 'ppio', 'api_key': 'sk_synthetic_not_sent'})

    def run_extraction(self, responses, *, provider=False):
        def respond(_selection, content, **kwargs):
            self.prompts.append(content[0]['text'])
            value = responses[min(len(self.prompts) - 1, len(responses) - 1)]
            return json.dumps(value, ensure_ascii=False), {'input_tokens': 12, 'output_tokens': 5}
        with collect_model_artifacts(self.root / 'analysis'), redirect_stdout(self.output), \
                patch('audit_core.qwen_model.complete_json', side_effect=respond), \
                patch('audit_core.qwen_model.time.sleep'), \
                patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
            if provider:
                instance = PdfEvidenceProvider(None, None, connection=self.selection, stage_summary_reviewer=isolated_stage_summary)
                return instance.classify({'archive_id': 'synthetic-gate',
                    'selected_scenario': 'personnel_incentive', 'units': [
                        {'unit_id': item['unit_id'], 'image': None, 'native_facts': item['facts'],
                         'limitations': []} for item in self.documents]}, self.root)
            return extract_qwen_json(connection=self.selection, root=self.root, skill=self.root,
                schema_path=self.schema, prompt='Synthetic gate only: ' + json.dumps(self.documents, ensure_ascii=False),
                images=[], label='隔离资料门禁纠正', validator=lambda value:
                    validate_classification(value, self.documents, 'personnel_incentive'))

    def metrics(self):
        return [json.loads(line) for line in
                (self.root / 'analysis/model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]

    def retry_context(self):
        text = self.prompts[1].split(MARKER, 1)[1]
        return json.loads(text[text.index('{'):])

    def assert_private(self, error=''):
        public = self.output.getvalue() + json.dumps(self.metrics(), ensure_ascii=False) + str(error)
        self.assertNotIn('PRIVATE_', public)
        self.assertNotIn('retry_context', public)
        self.assertFalse(list(self.root.rglob('response.json')))

    def test_real_provider_does_not_repair_a_business_inventory_to_satisfy_source_gate(self):
        candidate=deepcopy(self.good);candidate['source_ids']=['contract']
        original=deepcopy((candidate,self.documents))
        result=self.run_extraction([candidate,self.good],provider=True)
        self.assertEqual(result['classification'],candidate)
        self.assertEqual([d['facts'] for d in result['documents']],[d['facts'] for d in self.documents])
        self.assertEqual((candidate,self.documents),original)
        self.assertEqual(len(self.prompts),1)
        self.assertIn('不设置资料门禁',self.prompts[0])
        self.assertEqual([r['status'] for r in self.metrics()],['success'])
        self.assert_private()

    def test_unknown_source_retains_its_existing_code_and_gets_bounded_gate_context(self):
        bad = deepcopy(self.good)
        index = next(i for i, item in enumerate(bad['material_matches'][0]['materials']) if item['state'] == 'present')
        bad['material_matches'][0]['materials'][index]['source_ids'] = ['PRIVATE_FAKE_SOURCE']
        self.assertEqual(self.run_extraction([bad, self.good]), self.good)
        context = self.retry_context()
        expected = f'material_matches/0/materials/{index}/source_ids'
        self.assertEqual(context['path'], expected)
        self.assertEqual(self.metrics()[0]['validation_code'], 'unknown_source')
        self.assertEqual(self.metrics()[0]['validation_paths'], [expected])
        self.assertEqual(len(self.prompts), 2)
        self.assert_private()

    def test_top_level_condition_mismatch_is_corrected_without_changing_saved_match(self):
        bad = deepcopy(self.good)
        bad['flags']['red_packet'] = not bad['flags']['red_packet']
        self.assertEqual(self.run_extraction([bad, self.good]), self.good)
        context = self.retry_context()
        self.assertTrue(context['path'].startswith('flags'))
        self.assertEqual(self.metrics()[0]['validation_code'], 'gate_consistency')
        self.assertIn('不改核销类型、资料要求或原facts', self.prompts[1])
        self.assertEqual(len(self.prompts), 2)
        self.assert_private()

    def test_repeated_false_material_state_still_fails_at_three_attempts(self):
        bad = deepcopy(self.good)
        index = next(i for i, item in enumerate(bad['material_matches'][0]['materials']) if item['id'] == 'settlement')
        bad['material_matches'][0]['materials'][index]['state'] = 'not_applicable'
        with self.assertRaises(AuditError) as raised:
            self.run_extraction([bad])
        self.assertEqual(len(self.prompts), 3)
        self.assertEqual([r['status'] for r in self.metrics()], ['failed'] * 3)
        self.assertTrue(all(r['validation_code'] == 'gate_material_state' for r in self.metrics()))
        self.assertIn('gate_material_state', str(raised.exception))
        self.assert_private(raised.exception)

    def test_oversized_context_is_not_sent_or_truncated(self):
        bad = deepcopy(self.good)
        bad['source_ids'] = ['contract']
        with patch.object(EvidenceOutputParser, 'last_retry_context', new_callable=PropertyMock,
                          return_value='PRIVATE_OVERSIZED_' + '长' * 8192):
            self.assertEqual(self.run_extraction([bad, self.good]), self.good)
        self.assertNotIn('PRIVATE_OVERSIZED_', ''.join(self.prompts))
        self.assertNotIn(MARKER, self.prompts[1])
        self.assert_private()

    def test_unregistered_code_cannot_enable_private_gate_context_or_public_metrics(self):
        bad = deepcopy(self.good)
        bad['source_ids'] = ['contract']
        with patch.object(EvidenceOutputParser, 'last_diagnostics', new_callable=PropertyMock,
                          return_value={'validation_stage': 'evidence', 'validation_code': 'gate_unregistered',
                                        'validation_paths': ['source_ids']}), \
                patch.object(EvidenceOutputParser, 'last_retry_context', new_callable=PropertyMock,
                             return_value='PRIVATE_UNTRUSTED_CONTEXT'):
            self.assertEqual(self.run_extraction([bad, self.good]), self.good)
        self.assertNotIn('PRIVATE_UNTRUSTED_CONTEXT', ''.join(self.prompts))
        self.assertNotIn('validation_code', self.metrics()[0])
        self.assertNotIn(MARKER, self.prompts[1])
        self.assert_private()

    def test_metrics_keep_only_registered_gate_codes_not_context_or_values(self):
        with collect_model_artifacts(self.root / 'analysis'):
            for code in sorted(GATE_VALIDATION_CODES):
                record_model_attempt({'validation_stage': 'evidence', 'validation_code': code,
                    'validation_paths': ['material_matches/0/materials/2/state'],
                    'retry_context': 'PRIVATE_CONTEXT', 'fields': ['PRIVATE_FIELD'], 'sources': ['PRIVATE_FACT']})
        self.assertEqual({r['validation_code'] for r in self.metrics()}, GATE_VALIDATION_CODES)
        self.assertTrue(all(set(r) == {'validation_stage', 'validation_code', 'validation_paths'} for r in self.metrics()))
        self.assert_private()


if __name__ == '__main__':
    unittest.main()
