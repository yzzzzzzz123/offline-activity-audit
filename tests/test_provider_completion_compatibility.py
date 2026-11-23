"""Completion-oriented prose checks use the same path for every API provider."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from audit_core.common import AuditError
from audit_core.display_source_evidence import DisplaySourceError, validate_display_sources
from audit_core.model_settings import api_selection
from audit_core.pdf_evidence import DISPLAY_PHOTO_RULES, photo_review_schema, validate_photo_reviews
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.qwen_model import complete_json, QwenRequestError
from audit_core.workbench_store import reserve_workspace
from tests.test_display_source_evidence import fixture, application_assertion


class ProviderCompletionCompatibilityTests(unittest.TestCase):
    def observations(self):
        docs, materials, units = fixture()
        assertion = application_assertion(docs)
        assertion['statement'] = '本次勤策关联客户为甲店。'
        reviews = {'photo_reviews': [{'unit_id': 'p', 'checks': [
            {'rule_id': rule, 'status': 'unknown',
             'reason': (assertion['statement'] + '照片与申请客户名称接近，门店身份仍不能确认。'
                        if rule == 'display_watermark' else '本图边界被遮挡，暂时不能确认。'),
             'source_ids': ['p', 'c', 'a'],
             'source_assertions': [deepcopy(assertion)] if rule == 'display_watermark' else []}
            for rule in DISPLAY_PHOTO_RULES]}]}
        return reviews, docs, materials, units

    def test_real_execution_validator_accepts_prose_without_rewriting_unknown_or_sources(self):
        value, docs, materials, units = self.observations()
        before = deepcopy((value, docs, materials, units))
        with patch('audit_core.execution_trace.record_step') as trace:
            validate_photo_reviews(value, ['p'], docs, materials, units)
        self.assertEqual((value, docs, materials, units), before)
        self.assertTrue(all(c['status'] == 'unknown' for c in value['photo_reviews'][0]['checks']))
        self.assertTrue(any(call.args[0] == 'validation.source_prose_warning' for call in trace.call_args_list))

    def test_normal_api_adapter_accepts_same_prose_in_one_call_for_deepseek_and_ppio(self):
        for provider, key, model in (('deepseek', 'sk-synthetic', 'deepseek-flash'),
                                     ('ppio', 'sk_synthetic', 'qwen/qwen3.8-max'),
                                     ('ppio', 'sk_synthetic', 'deepseek/deepseek-v4.1-flash')):
            with self.subTest(provider=provider, model=model), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                selection = api_selection({'provider': provider, 'api_key': key, 'model': model})
                adapter = PdfEvidenceProvider(selection.model, selection.reasoning_effort, connection=selection, stage_summary_reviewer=isolated_stage_summary)
                value, docs, materials, units = self.observations()
                original = deepcopy(value)
                with patch('audit_core.qwen_model.complete_json',
                           return_value=(json.dumps(value, ensure_ascii=False),
                                         {'input_tokens': 12, 'output_tokens': 5})) as remote, \
                        patch('httpx.AsyncClient', side_effect=AssertionError('External model forbidden')):
                    result = adapter._call(root / 'batch', root,
                        photo_review_schema(['p'], [d['unit_id'] for d in docs], ['c']),
                        'Synthetic photo checks', images=[], label='隔离来源措辞测试',
                        validator=lambda v: validate_photo_reviews(v, ['p'], docs, materials, units))
                self.assertEqual(result, original)
                remote.assert_called_once()
                self.assertEqual(remote.call_args.args[0], selection)

    def test_fabricated_or_other_claim_reference_still_cannot_be_accepted(self):
        for fault in ('quote', 'field', 'claim'):
            with self.subTest(fault=fault):
                value, docs, materials, units = self.observations()
                ref = value['photo_reviews'][0]['checks'][0]['source_assertions'][0]['references'][0]
                if fault == 'quote':
                    ref['quote'] = 'cusName="invented"'
                elif fault == 'field':
                    ref['field'] = 'dealerName'
                else:
                    units[1]['native_facts'][0] = 'verify_code="OTHER"'
                    docs[1]['facts'][0] = 'verify_code="OTHER"'
                with self.assertRaises(DisplaySourceError):
                    validate_photo_reviews(value, ['p'], docs, materials, units)

    def test_completion_does_not_allow_missing_photos(self):
        value, docs, materials, units = self.observations()
        with self.assertRaises(AuditError):
            validate_photo_reviews(value, ['p', 'missing-photo'], docs, materials, units)

    def test_vendor_namespaced_models_can_reserve_real_workspace_names(self):
        for model, effort in (('deepseek-flash', 'high'),
                              ('deepseek/deepseek-v4.1-flash', 'default'),
                              ('qwen/qwen3.8-max', 'default')):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                workspace = reserve_workspace('20261106-synthetic', 'codex', directory,
                    audit_model=model, reasoning_effort=effort, started_at=datetime(2026, 11, 6, 12, 0, 0))
                self.assertTrue(workspace.is_dir())
                self.assertEqual(workspace.parent, Path(directory).resolve())
                self.assertNotIn('/', workspace.name)
                self.assertIn('deepseek' if 'deepseek' in model else 'qwen', workspace.name)
        for model in ('../deepseek', 'deepseek/../../escape', '/deepseek', 'deepseek\\model'):
            with self.subTest(model=model), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(AuditError):
                    reserve_workspace('20261106-synthetic', 'codex', directory, audit_model=model)

    def test_deepseek_transport_error_is_not_mislabeled_qwen(self):
        selection = api_selection({'provider': 'deepseek', 'api_key': 'sk-synthetic'})
        with self.assertRaises(QwenRequestError) as raised:
            complete_json(selection, [], system='JSON', transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text='data: malformed-json\n\n')))
        self.assertIn('DeepSeek', str(raised.exception))
        self.assertNotIn('千问', str(raised.exception))


if __name__ == '__main__':
    unittest.main()
