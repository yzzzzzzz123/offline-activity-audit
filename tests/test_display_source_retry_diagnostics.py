"""Strict display provenance failures reuse the existing bounded model retry."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.display_source_evidence import (
    ASSERTIONS_SCHEMA, DisplaySourceError, validate_display_sources,
)
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.model_metrics import collect_model_artifacts, record_model_attempt
from audit_core.model_settings import api_selection
from audit_core.qwen_model import extract_qwen_json


class DisplaySourceRetryDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fact = 'PRIVATE_CONTRACT:约定陈列规格为1平堆'
        self.documents = [{'unit_id': 'contract', 'facts': [self.fact]}]
        self.units = [{'unit_id': 'contract', 'source_kind': 'image'}]
        self.materials = [{'id': 'promotion_contract', 'state': 'present', 'source_ids': ['contract']}]
        statement = f'本次合同参照原文：“{self.fact}”。'
        self.good_check = {
            'status': 'unknown', 'reason': statement + '照片边界不清，无法确认实际面积。',
            'source_ids': ['contract'], 'source_assertions': [{
                'kind': 'contract_reference', 'statement': statement,
                'references': [{'unit_id': 'contract', 'field': 'fact', 'quote': self.fact}],
            }],
        }
        self.good = {'checks': [deepcopy(self.good_check)]}
        check_schema = {'type': 'object', 'additionalProperties': False,
            'required': ['status', 'reason', 'source_ids', 'source_assertions'],
            'properties': {'status': {'enum': ['pass', 'fail', 'unknown']},
                'reason': {'type': 'string'},
                'source_ids': {'type': 'array', 'items': {'type': 'string'}},
                'source_assertions': deepcopy(ASSERTIONS_SCHEMA)}}
        schema = {'type': 'object', 'additionalProperties': False,
            'properties': {'checks': {'type': 'array', 'items': check_schema},
                'photo_reviews': {'type': 'array', 'items': {
                    'type': 'object', 'additionalProperties': False, 'required': ['checks'],
                    'properties': {'checks': {'type': 'array', 'items': check_schema}}}}}}
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps(schema), encoding='utf-8')
        self.parser = EvidenceOutputParser(schema_path=self.schema, post_validate=self.validate)
        self.prompts = []
        self.output = io.StringIO()

    def validate(self, value):
        for i, check in enumerate(value.get('checks', [])):
            validate_display_sources(check, self.documents, self.materials, self.units, path=f'checks/{i}')
        for i, photo in enumerate(value.get('photo_reviews', [])):
            for j, check in enumerate(photo['checks']):
                validate_display_sources(check, self.documents, self.materials, self.units,
                                         path=f'photo_reviews/{i}/checks/{j}')

    def reject(self, value, path):
        before = deepcopy((value, self.documents, self.units, self.materials))
        with self.assertRaises(DisplaySourceError) as raised:
            self.parser.parse(json.dumps(value, ensure_ascii=False))
        self.assertEqual(self.parser.last_diagnostics, {'validation_stage': 'evidence',
            'validation_code': 'display_source_claim', 'validation_paths': [path]})
        self.assertEqual(self.parser.last_retry_context, '')
        self.assertEqual(before, (value, self.documents, self.units, self.materials))
        self.assertLessEqual(len(str(raised.exception).encode('utf-8')), 1000)
        self.assertNotIn('PRIVATE_', str(raised.exception) + json.dumps(self.parser.last_diagnostics))
        return raised.exception

    def test_uncovered_source_claim_is_rejected_but_visual_unknown_is_preserved(self):
        bad = deepcopy(self.good)
        bad['checks'][0].update(source_assertions=[], reason='合同门店清单经该申请行取得。')
        self.reject(bad, 'checks/0/reason')
        good = deepcopy(bad)
        good['checks'][0]['reason'] = '照片边界不清，无法确认实际面积。'
        self.assertEqual(self.parser.parse(json.dumps(good)), good)
        self.assertEqual(good['checks'][0]['status'], 'unknown')
        self.assertEqual(self.parser.last_diagnostics, {})
        self.assertEqual(self.parser.last_retry_context, '')

    def test_exact_reference_and_statement_paths_are_public_without_values(self):
        for field, value, suffix in (
                ('quote', 'PRIVATE_FABRICATED', 'references/0/quote'),
                ('field', 'PRIVATE_WRONG_FIELD', 'references/0/field'),
                ('statement', 'PRIVATE_WRONG_STATEMENT', 'statement')):
            bad = deepcopy(self.good)
            assertion = bad['checks'][0]['source_assertions'][0]
            if field == 'statement':
                assertion[field] = value
            else:
                assertion['references'][0][field] = value
            with self.subTest(field=field):
                self.reject(bad, 'checks/0/source_assertions/0/' + suffix)

    def test_nested_photo_failure_keeps_the_real_position(self):
        bad = {'photo_reviews': [{'checks': [deepcopy(self.good_check), deepcopy(self.good_check)]}]}
        bad['photo_reviews'][0]['checks'][1]['source_assertions'][0]['references'][0]['quote'] = 'PRIVATE_FORGED'
        self.reject(bad, 'photo_reviews/0/checks/1/source_assertions/0/references/0/quote')

    def test_arbitrary_error_metadata_is_not_trusted(self):
        ordinary = AuditError('Ordinary synthetic error')
        ordinary.code = 'display_source_claim'
        ordinary.validation_paths = ['checks/0/reason']
        bad_errors = [ordinary]
        for paths in (['checks/99/reason'], ['checks/0/PRIVATE_KEY'], ['checks/-1/reason'],
                      ['checks/0/reason', 'checks/0/source_assertions'],
                      ['checks/0/source_assertions/0/references/90/quote'], ['checks/0/reason/0'],
                      ['checks/0/source_assertions/0/statement/' + 'x' * 200]):
            error = DisplaySourceError('checks/0/reason')
            error.validation_paths = paths
            bad_errors.append(error)
        wrong_code = DisplaySourceError('checks/0/reason')
        wrong_code.code = 'PRIVATE_NOT_ALLOWED'
        bad_errors.append(wrong_code)
        for error in bad_errors:
            parser = EvidenceOutputParser(schema_path=self.schema,
                post_validate=lambda value, current=error: (_ for _ in ()).throw(current))
            with self.subTest(paths=error.validation_paths), self.assertRaises(AuditError):
                parser.parse(json.dumps(self.good))
            self.assertEqual(parser.last_diagnostics, {'validation_stage': 'evidence',
                'validation_code': 'evidence_validation', 'validation_paths': ['$']})
            self.assertEqual(parser.last_retry_context, '')

    def test_typed_error_never_copies_an_extra_private_context(self):
        error = DisplaySourceError('checks/0/reason')
        error.retry_context = {'facts': ['PRIVATE_NOT_FOR_RETRY']}
        parser = EvidenceOutputParser(schema_path=self.schema,
            post_validate=lambda value: (_ for _ in ()).throw(error))
        with self.assertRaises(DisplaySourceError):
            parser.parse(json.dumps(self.good))
        self.assertEqual(parser.last_retry_context, '')
        self.assertNotIn('PRIVATE_', json.dumps(parser.last_diagnostics))

    def test_schema_errors_are_not_reclassified_as_source_claims(self):
        bad = deepcopy(self.good)
        bad['checks'][0]['source_assertions'] = 'PRIVATE_INVALID_SHAPE'
        with self.assertRaises(AuditError):
            self.parser.parse(json.dumps(bad))
        self.assertEqual(self.parser.last_diagnostics['validation_code'], 'schema_validation')
        self.assertEqual(self.parser.last_diagnostics['validation_stage'], 'schema')
        self.assertEqual(self.parser.last_retry_context, '')

    def test_metrics_keep_only_the_safe_code_and_path(self):
        with collect_model_artifacts(self.root / 'metrics'):
            record_model_attempt({'status': 'failed', 'validation_stage': 'evidence',
                'validation_code': 'display_source_claim', 'validation_paths': ['checks/0/reason'],
                'retry_context': 'PRIVATE_CONTEXT', 'reason': 'PRIVATE_REASON', 'facts': ['PRIVATE_FACT']})
            record_model_attempt({'status': 'failed', 'validation_code': 'PRIVATE_NOT_ALLOWED'})
        text = (self.root / 'metrics/model-metrics.jsonl').read_text(encoding='utf-8')
        rows = [json.loads(line) for line in text.splitlines()]
        self.assertEqual(rows[0]['validation_code'], 'display_source_claim')
        self.assertEqual(rows[0]['validation_paths'], ['checks/0/reason'])
        self.assertNotIn('validation_code', rows[1])
        self.assertNotIn('PRIVATE_', text)

    def extraction(self, responses):
        def respond(selection, content, **kwargs):
            self.prompts.append(content[0]['text'])
            value = responses[min(len(self.prompts) - 1, len(responses) - 1)]
            return json.dumps(value, ensure_ascii=False), {'input_tokens': 12, 'output_tokens': 5}
        with collect_model_artifacts(self.root / 'analysis'), redirect_stdout(self.output), \
                patch('audit_core.qwen_model.complete_json', side_effect=respond), \
                patch('audit_core.qwen_model.time.sleep'), \
                patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
            return extract_qwen_json(
                connection=api_selection({'provider': 'ppio', 'api_key': 'sk_synthetic_not_sent'}),
                root=self.root, skill=self.root, schema_path=self.schema,
                prompt='Only this synthetic display batch.', images=[], label='隔离陈列来源纠正',
                validator=self.validate)

    def test_existing_qwen_retry_uses_safe_path_then_strictly_accepts_correction(self):
        bad = deepcopy(self.good)
        bad['checks'][0]['source_assertions'][0]['references'][0]['quote'] = 'PRIVATE_FORGED'
        originals = deepcopy((bad, self.good, self.documents))
        self.assertEqual(self.extraction([bad, self.good]), self.good)
        self.assertEqual(len(self.prompts), 2)
        self.assertIn('evidence:display_source_claim', self.prompts[1])
        self.assertIn('checks/0/source_assertions/0/references/0/quote', self.prompts[1])
        self.assertIn('申请客户名称不能改称终端地址', self.prompts[1])
        self.assertNotIn('PRIVATE_', self.prompts[1])
        self.assertEqual((bad, self.good, self.documents), originals)
        rows = [json.loads(line) for line in (self.root / 'analysis/model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual([row['status'] for row in rows], ['failed', 'success'])
        self.assertEqual(rows[0]['validation_code'], 'display_source_claim')
        self.assertNotIn('PRIVATE_', json.dumps(rows) + self.output.getvalue())

    def test_repeated_invalid_claim_exhausts_original_three_attempts(self):
        bad = deepcopy(self.good)
        bad['checks'][0].update(source_assertions=[], reason='合同门店清单经该申请行取得。')
        with self.assertRaisesRegex(AuditError, 'display_source_claim') as raised:
            self.extraction([bad])
        self.assertEqual(len(self.prompts), 3)
        rows = [json.loads(line) for line in (self.root / 'analysis/model-metrics.jsonl').read_text(encoding='utf-8').splitlines()]
        self.assertEqual(len(rows), 3)
        self.assertTrue(all(row['status'] == 'failed' for row in rows))
        self.assertEqual(sum(row['usage']['input_tokens'] for row in rows), 36)
        self.assertNotIn('PRIVATE_', str(raised.exception) + json.dumps(rows) + self.output.getvalue())


if __name__ == '__main__':
    unittest.main()
