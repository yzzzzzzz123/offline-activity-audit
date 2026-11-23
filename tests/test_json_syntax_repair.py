"""Damaged model JSON reaches the recorded recovery agent instead of ending a claim.

HX202607010003 (analyzeId 64) failed with ``invalid_output:json:json_syntax`` on
核销资料盘点: the single attempt emitted syntactically broken JSON, the repair
handler was never consulted for the ``json`` stage, and the whole claim ended
before any business audit. Syntax damage is the same failure class as a shape
miss, so the same validator-checked recovery must be reachable.
"""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_settings import api_selection
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.prompts import bound_prompt


BROKEN = ('{"candidate_scenarios": ["giveaway_promotion"], "flags": {"a": true, "b": }, '
          '"reason": "资料可读"')

FIXED = {"candidate_scenarios": ["giveaway_promotion"],
         "flags": {"a": True, "b": False},
         "reason": "资料可读",
         "source_ids": ["a001-f0001"]}

SCHEMA = {'type': 'object', 'additionalProperties': False,
          'required': ['candidate_scenarios', 'flags', 'reason', 'source_ids'],
          'properties': {'candidate_scenarios': {'type': 'array', 'minItems': 1,
                                                 'items': {'type': 'string'}},
                         'flags': {'type': 'object'},
                         'reason': {'type': 'string', 'minLength': 1},
                         'source_ids': {'type': 'array', 'items': {'type': 'string'}}}}


class JsonSyntaxRepairTests(TestCase):
    def provider(self):
        selection = api_selection({'provider': 'zimaocloud', 'api_key': 'isolated-test'})
        return PdfEvidenceProvider(selection.model, selection.reasoning_effort, connection=selection)

    def run_stage(self, replies):
        provider = self.provider()
        recovery = {'diagnosis': '原回答只是JSON语法损坏，按原文补齐标点后重发。',
                    'developer_fix': None, 'answer': FIXED}
        with TemporaryDirectory() as directory:
            root = Path(directory)
            with collect_model_artifacts(root / 'run/analysis'), \
                 patch('httpx.Client', side_effect=AssertionError('no network')), \
                 patch('audit_core.qwen_model.complete_json',
                       side_effect=[(BROKEN, {}), (json.dumps(recovery), {})]) as model:
                result = provider._call(root / 'stage', Path('skill'), SCHEMA,
                                        bound_prompt('本包已读事实：{data}', data='无'),
                                        images=[], label='核销资料盘点 a001',
                                        validator=lambda value: value,
                                        max_attempts=1, allow_repair=True)
            return result, model

    def test_broken_json_enters_recovery_and_keeps_the_corrected_answer(self):
        result, model = self.run_stage(None)
        self.assertEqual(result, FIXED)
        self.assertEqual(model.call_count, 2)

    def test_recovery_prompt_carries_the_unparsed_text_for_syntax_only_repair(self):
        provider = self.provider()
        captured = []

        def capture(*args, **kwargs):
            captured.append(args[1] if len(args) > 1 else kwargs.get('content'))
            return (BROKEN, {}) if len(captured) == 1 else (json.dumps(
                {'diagnosis': '修复标点', 'developer_fix': None, 'answer': FIXED}), {})

        with TemporaryDirectory() as directory:
            root = Path(directory)
            with collect_model_artifacts(root / 'run/analysis'), \
                 patch('httpx.Client', side_effect=AssertionError('no network')), \
                 patch('audit_core.qwen_model.complete_json', side_effect=capture):
                provider._call(root / 'stage', Path('skill'), SCHEMA,
                               bound_prompt('本包已读事实：{data}', data='无'), images=[],
                               label='核销资料盘点 a001', validator=lambda value: value,
                               max_attempts=1, allow_repair=True)
        repair_text = json.dumps(captured[-1], ensure_ascii=False)
        self.assertIn('candidate_scenarios', repair_text)
        self.assertIn('未解析成功的原始JSON文本', repair_text)

    def test_parser_reports_json_syntax_for_this_shape(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / 'schema.json'
            path.write_text(json.dumps(SCHEMA), encoding='utf-8')
            parser = EvidenceOutputParser(schema_path=path, post_validate=lambda value: value)
            with self.assertRaises(Exception):
                parser.parse(BROKEN)
            self.assertEqual(parser.last_diagnostics['validation_stage'], 'json')
            self.assertEqual(parser.last_diagnostics['validation_code'], 'json_syntax')
