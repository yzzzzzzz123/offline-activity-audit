"""Bounded same-source date correction for both providers; no external calls."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.model_settings import api_selection
from audit_core.price_support_dates import PRICE_ACTIVITY_DATES_SCHEMA, price_activity_date_checks
from audit_core.qwen_model import extract_qwen_json
from audit_core.pdf_evidence import audit_schema, validate
from tests.test_price_support_activity_dates import fixture
from tests.pdf_test_support import flags_for, unknown_checks


class PriceDateQuoteRetryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        dates, self.docs, self.materials, self.units = fixture()
        self.good = {'activity_dates': dates}
        self.bad = deepcopy(self.good)
        self.bad['activity_dates']['contract']['periods'][0]['quote'] = '活动时间：2026-06-02至2026-06-15'
        self.schema = self.root / 'schema.json'
        self.schema.write_text(json.dumps({'type': 'object', 'additionalProperties': False,
            'properties': {'activity_dates': PRICE_ACTIVITY_DATES_SCHEMA}, 'required': ['activity_dates']}), encoding='utf-8')
        self.path = 'activity_dates/contract/periods/0/quote'

    def validate(self, value):
        return price_activity_date_checks(value['activity_dates'], self.docs, self.materials, self.units)

    def parser(self):
        return EvidenceOutputParser(schema_path=self.schema, post_validate=self.validate)

    def run_model(self, provider, responses):
        prompts = []
        def complete(selection, content, **kwargs):
            self.assertEqual(selection.provider, provider)
            prompts.append(content[0]['text'])
            return json.dumps(responses[min(len(prompts)-1, len(responses)-1)], ensure_ascii=False), {'input_tokens': 1, 'output_tokens': 1}
        with redirect_stdout(io.StringIO()), patch('audit_core.qwen_model.complete_json', side_effect=complete), \
                patch('audit_core.qwen_model.time.sleep'), \
                patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
            result = extract_qwen_json(connection=api_selection({'provider': provider,
                'api_key': 'sk_synthetic_not_sent' if provider == 'ppio' else 'sk-synthetic-not-sent'}),
                root=self.root / provider, skill=self.root, schema_path=self.schema, prompt='Synthetic date quotes',
                images=[], label='隔离日期引用纠正', validator=self.validate)
        return result, prompts

    def test_ppio_and_deepseek_receive_same_bounded_quote_context_and_can_correct(self):
        before = deepcopy((self.docs, self.bad))
        for provider in ('ppio', 'deepseek'):
            with self.subTest(provider=provider):
                result, prompts = self.run_model(provider, [self.bad, self.good])
                self.assertEqual(result, self.good)
                self.assertEqual(len(prompts), 2)
                suffix = prompts[1].split('以下是本次失败引用与同一unit的少量完整原文', 1)[1]
                context = json.loads(suffix[suffix.index('{'):])
                self.assertEqual(context['path'], self.path)
                self.assertEqual(context['unit_id'], 'contract')
                self.assertEqual(context['facts'], self.docs[0]['facts'])
                self.assertNotIn('number', context)
                self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode('utf-8')), 8192)
        self.assertEqual(before, (self.docs, self.bad))

    def test_bad_quote_is_still_rejected_with_concrete_safe_position(self):
        parser = self.parser()
        with self.assertRaises(AuditError):
            parser.parse(json.dumps(self.bad, ensure_ascii=False))
        self.assertEqual(parser.last_diagnostics['validation_code'], 'quote_not_in_source')
        self.assertEqual(parser.last_diagnostics['validation_paths'], [self.path])
        self.assertEqual(json.loads(parser.last_retry_context)['quote'], self.bad['activity_dates']['contract']['periods'][0]['quote'])

    def test_photo_quote_path_follows_original_response_order(self):
        bad = deepcopy(self.good)
        bad['activity_dates']['photos'].reverse()
        bad['activity_dates']['photos'][0]['quote'] = '水印2026/06/15'
        parser = self.parser()
        with self.assertRaises(AuditError):
            parser.parse(json.dumps(bad, ensure_ascii=False))
        self.assertEqual(parser.last_diagnostics['validation_paths'], ['activity_dates/photos/0/quote'])
        self.assertEqual(json.loads(parser.last_retry_context)['unit_id'], 'photo-2')

    def test_wrong_role_cannot_receive_other_units_as_correction(self):
        bad = deepcopy(self.bad)
        bad['activity_dates']['contract']['periods'][0]['unit_id'] = 'settlement'
        parser = self.parser()
        with self.assertRaises(AuditError):
            parser.parse(json.dumps(bad, ensure_ascii=False))
        self.assertEqual(parser.last_retry_context, '')

    def test_date_facts_are_preferred_and_oversized_facts_are_not_truncated(self):
        date_fact = self.docs[0]['facts'][0]
        self.docs[0]['facts'] = ['PRIVATE_TITLE', 'PRIVATE_NOTE', 'PRIVATE_OTHER', '长'*3000, date_fact]
        parser = self.parser()
        with self.assertRaises(AuditError):
            parser.parse(json.dumps(self.bad, ensure_ascii=False))
        context = json.loads(parser.last_retry_context)
        self.assertEqual(context['facts'][0], date_fact)
        self.assertLessEqual(len(context['facts']), 3)
        self.assertTrue(all(len(f.encode('utf-8')) <= 2048 for f in context['facts']))
        self.assertTrue(context['facts_omitted'])

    def test_repeated_invented_date_stops_at_existing_three_attempts(self):
        calls = []
        def complete(selection, content, **kwargs):
            calls.append(1)
            return json.dumps(self.bad, ensure_ascii=False), {}
        with redirect_stdout(io.StringIO()), patch('audit_core.qwen_model.complete_json', side_effect=complete), \
                patch('audit_core.qwen_model.time.sleep'), \
                patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
            with self.assertRaises(AuditError):
                extract_qwen_json(connection=api_selection({'provider': 'deepseek', 'api_key': 'synthetic'}),
                    root=self.root, skill=self.root, schema_path=self.schema, prompt='Synthetic', images=[],
                    label='隔离日期拒绝', validator=self.validate)
        self.assertEqual(len(calls), 3)

    def test_existing_excel_only_constraint_is_in_dynamic_generation_schema(self):
        materials = self.materials + [{'id': 'pos_excel', 'state': 'present', 'source_ids': ['excel']}]
        schema = audit_schema('price_difference_support', materials=materials)
        value = unknown_checks('price_difference_support', flags_for('price_difference_support'))
        value.update(price_photo_reviews=[], activity_dates=self.good['activity_dates'])
        check = next(c for c in value['checks'] if c['rule_id'] == 'pos_product_identity')
        check['source_ids'] = ['excel']
        validate(value, schema)
        check['source_ids'].append('other-product-reference')
        with self.assertRaises(AuditError):
            validate(value, schema)

    def test_distinct_equal_sales_rows_are_allowed_but_duplicate_quote_is_not(self):
        materials = self.materials + [{'id': 'pos_excel', 'state': 'present', 'source_ids': ['excel']}]
        schema = audit_schema('price_difference_support', materials=materials)
        value = unknown_checks('price_difference_support', flags_for('price_difference_support'))
        value.update(price_photo_reviews=[], activity_dates=self.good['activity_dates'])
        check = next(c for c in value['checks'] if c['rule_id'] == 'settlement_pos_quantity')
        check.update(source_ids=['claim', 'excel'], comparisons=[{'label': '申报与POS数量', 'value_kind': 'quantity',
            'operation': 'sum', 'operator': 'eq', 'left': {'unit_id': 'claim', 'quote': '申报20', 'number': '20'},
            'right': [{'unit_id': 'excel', 'quote': 'Sheet1!R2: C7=10', 'number': '10'},
                      {'unit_id': 'excel', 'quote': 'Sheet1!R3: C7=10', 'number': '10'}]}])
        validate(value, schema)
        check['comparisons'][0]['right'][1] = deepcopy(check['comparisons'][0]['right'][0])
        with self.assertRaises(AuditError):
            validate(value, schema)

    def test_all_contract_pages_are_required_without_forcing_a_period_on_signing_page(self):
        materials = deepcopy(self.materials)
        materials[0]['source_ids'].append('signing-page')
        schema = audit_schema('price_difference_support', materials=materials)
        value = unknown_checks('price_difference_support', flags_for('price_difference_support'))
        value.update(price_photo_reviews=[], activity_dates=deepcopy(self.good['activity_dates']))
        with self.assertRaises(AuditError):
            validate(value, schema)
        value['activity_dates']['contract']['source_ids'].append('signing-page')
        validate(value, schema)
        self.assertEqual(len(value['activity_dates']['contract']['periods']), 1)

    def test_missing_contract_allows_empty_sources_but_not_a_borrowed_period(self):
        materials = [m for m in self.materials if m['id'] != 'promotion_contract']
        schema = audit_schema('price_difference_support', materials=materials)
        value = unknown_checks('price_difference_support', flags_for('price_difference_support'))
        value.update(price_photo_reviews=[], activity_dates=deepcopy(self.good['activity_dates']))
        value['activity_dates']['contract'].update(source_ids=[], periods=[])
        validate(value, schema)
        value['activity_dates']['contract']['periods'] = deepcopy(value['activity_dates']['settlement']['periods'])
        with self.assertRaises(AuditError):
            validate(value, schema)


if __name__ == '__main__':
    unittest.main()
