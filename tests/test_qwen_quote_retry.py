"""Source-bound retry integration: real strict parser, synthetic model only."""
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
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_settings import api_selection
from audit_core.pdf_evidence import audit_decision, audit_schema
from audit_core.qwen_model import extract_qwen_json
from tests.pdf_test_support import flags_for, unknown_checks


class QwenQuoteRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.flags = flags_for('personnel_incentive')
        self.materials = [
            {'id': 'activity_application', 'state': 'present', 'source_ids': ['contract'], 'reason': 'Synthetic contract'},
            {'id': 'settlement', 'state': 'present', 'source_ids': ['claim'], 'reason': 'Synthetic settlement'},
        ]
        self.contract_fact = 'PRIVATE_CONTRACT：合同奖励单价  2.00 元'
        self.documents = [
            {'unit_id': 'contract', 'facts': [self.contract_fact]},
            {'unit_id': 'claim', 'facts': ['PRIVATE_CLAIM：申报奖励单价2元']},
        ]
        self.good = unknown_checks('personnel_incentive', self.flags)
        self.index = next(i for i, c in enumerate(self.good['checks']) if c['rule_id'] == 'reward_unit_price')
        self.good['checks'][self.index].update(
            status='pass', reason='Synthetic exact source comparison', source_ids=['claim', 'contract'],
            comparisons=[{'label': 'Synthetic reward price', 'value_kind': 'unit_price',
                'left': {'unit_id': 'claim', 'quote': '申报奖励单价2元', 'number': '2'},
                'right': [{'unit_id': 'contract', 'quote': self.contract_fact, 'number': '2'}],
                'operation': 'value', 'operator': 'eq'}])
        self.bad = deepcopy(self.good)
        self.bad['checks'][self.index]['comparisons'][0]['right'][0]['quote'] = 'PRIVATE_CONTRACT：合同奖励单价2.00元'
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps(audit_schema('personnel_incentive', materials=self.materials)), encoding='utf8')
        self.expected_path = f'checks/{self.index}/comparisons/0/right/0/quote'
        self.prompts = []
        self.output = io.StringIO()

    def run_extraction(self, responses):
        def respond(selection, content, **kwargs):
            self.prompts.append(content[0]['text'])
            response = responses[min(len(self.prompts) - 1, len(responses) - 1)]
            return json.dumps(response, ensure_ascii=False), {'input_tokens': 12, 'output_tokens': 5}
        with collect_model_artifacts(self.root / 'analysis'), redirect_stdout(self.output), \
                patch('audit_core.qwen_model.complete_json', side_effect=respond), \
                patch('audit_core.qwen_model.time.sleep'), \
                patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
            return extract_qwen_json(
                connection=api_selection({'provider': 'ppio', 'api_key': 'sk_synthetic_not_sent'}),
                root=self.root, skill=self.root, schema_path=self.schema,
                prompt='Only this synthetic batch: ' + json.dumps(self.documents, ensure_ascii=False),
                images=[], label='隔离引用纠正',
                validator=lambda value: audit_decision('personnel_incentive', self.flags, value,
                                                       self.documents, self.materials))

    def metrics(self):
        return [json.loads(line) for line in (self.root / 'analysis/model-metrics.jsonl').read_text(encoding='utf8').splitlines()]

    def test_failed_quote_gets_exact_field_and_original_same_source_then_passes(self):
        originals = deepcopy((self.documents, self.bad))
        result = self.run_extraction([self.bad, self.good])
        self.assertEqual(result, self.good)
        self.assertEqual(len(self.prompts), 2)
        marker = '以下是本次失败引用与同一unit的少量完整原文'
        self.assertNotIn(marker, self.prompts[0])
        suffix = self.prompts[1].split(marker, 1)[1]
        context = json.loads(suffix[suffix.index('{'):])
        self.assertEqual(context['path'], self.expected_path)
        self.assertEqual(context['unit_id'], 'contract')
        self.assertEqual(context['facts'], [self.contract_fact])
        self.assertNotIn('PRIVATE_CLAIM', json.dumps(context))
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode('utf8')), 8192)
        self.assertEqual((self.documents, self.bad), originals)
        rows = self.metrics()
        self.assertEqual([r['status'] for r in rows], ['failed', 'success'])
        self.assertEqual(rows[0]['validation_paths'], [self.expected_path])
        self.assertEqual(sum(r['usage']['input_tokens'] for r in rows), 24)
        self.assertNotIn('PRIVATE_', self.output.getvalue() + json.dumps(rows))
        self.assertFalse((self.root / 'response.json').exists())

    def test_repeated_fabricated_quote_still_fails_at_existing_attempt_limit(self):
        with self.assertRaises(AuditError) as raised:
            self.run_extraction([self.bad])
        self.assertEqual(len(self.prompts), 3)
        self.assertIn('quote_not_in_source', str(raised.exception))
        rows = self.metrics()
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(r['validation_paths'] == [self.expected_path] for r in rows))
        self.assertNotIn('PRIVATE_', str(raised.exception) + self.output.getvalue() + json.dumps(rows))

    def test_other_unit_quote_cannot_be_substituted_by_retry_context(self):
        forged = deepcopy(self.bad)
        forged['checks'][self.index]['comparisons'][0]['right'][0]['quote'] = 'PRIVATE_CLAIM：申报奖励单价2元'
        with self.assertRaises(AuditError):
            self.run_extraction([forged])
        marker = '以下是本次失败引用与同一unit的少量完整原文'
        suffix = self.prompts[1].split(marker, 1)[1]
        context = json.loads(suffix[suffix.index('{'):])
        self.assertEqual(context['unit_id'], 'contract')
        self.assertEqual(context['facts'], [self.contract_fact])
        self.assertEqual(len(self.prompts), 3)

    def test_oversized_private_retry_context_is_not_sent_or_truncated(self):
        oversized = 'PRIVATE_OVERSIZED_' + '长' * 8192
        with patch.object(EvidenceOutputParser, 'last_retry_context', new_callable=PropertyMock, return_value=oversized):
            result = self.run_extraction([self.bad, self.good])
        self.assertEqual(result, self.good)
        self.assertNotIn('PRIVATE_OVERSIZED_', ''.join(self.prompts) + self.output.getvalue())
        self.assertNotIn('以下是本次失败引用与同一unit的少量完整原文', self.prompts[1])


if __name__ == '__main__':
    unittest.main()
