"""Bounded capacity recovery in the real audit orchestrator, using model doubles."""
from tests.pdf_test_support import isolated_stage_summary
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.audit_rag import batches, needed_fields, reference_fields
from audit_core.codex_runner import CodexContextCapacityError
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.pdf_policy import rules
from tests.pdf_test_support import unknown_checks
from tests.test_audit_rag_contract import case_for, reads_for


class CapacityRecoveryTests(unittest.TestCase):
    def test_normal_wire_keeps_long_input_without_output_caps_or_previous_case_history(self):
        import httpx
        from langchain_core.messages import HumanMessage
        from audit_core import qwen_model
        from audit_core.model_settings import api_selection
        from audit_core.scenario_registry import SKILL_BY_SCENARIO
        from audit_core.stage_context import compile_context

        context = compile_context('personnel_incentive', ['contract_signed'], {})
        system = qwen_model.system_prompt(SKILL_BY_SCENARIO['personnel_incentive'],
                                         '{"type":"object"}', context)
        original_complete = qwen_model.complete_json
        with tempfile.TemporaryDirectory() as temporary:
            for provider in ('ppio', 'deepseek', 'zimaocloud'):
                with self.subTest(provider=provider):
                    payloads = []

                    def handler(request):
                        payloads.append(json.loads(request.content))
                        event = {'choices': [{'delta': {'content': '{"ok":true}'}, 'finish_reason': 'stop'}],
                                 'usage': {'prompt_tokens': 7, 'completion_tokens': 4}}
                        return httpx.Response(200, text='data: ' + json.dumps(event) + '\n\ndata: [DONE]\n\n')

                    transport = httpx.MockTransport(handler)

                    def complete(selection, content, **kwargs):
                        return original_complete(selection, content, transport=transport, **kwargs)

                    model = qwen_model.QwenChatModel(
                        selection=api_selection({'provider': provider, 'api_key':
                            'sk_synthetic_never_send' if provider == 'ppio' else 'sk-synthetic-never-send'}),
                        images=(), system_prompt=system, model_root=Path(temporary)/provider)
                    inputs = ['CASE_FIRST_BEGIN\n' + '第一单完整原文' * 9000 + '\nCASE_FIRST_END',
                              'CASE_SECOND_BEGIN\n' + '第二单完整原文' * 9000 + '\nCASE_SECOND_END']
                    with patch.object(qwen_model, 'complete_json', side_effect=complete), \
                         patch.object(httpx.AsyncHTTPTransport, 'handle_async_request',
                                      side_effect=AssertionError('real network forbidden')):
                        for text in inputs:
                            self.assertGreater(len(text.encode('utf-8')), 128_000)
                            answer = model._generate([HumanMessage(content=[{'type': 'text', 'text': text}])])
                            self.assertEqual(answer.generations[0].message.content, '{"ok":true}')
                    self.assertEqual(len(payloads), 2)
                    for index, payload in enumerate(payloads):
                        self.assertNotIn('max_tokens', payload)
                        self.assertNotIn('max_completion_tokens', payload)
                        self.assertEqual(payload['messages'], [
                            {'role': 'system', 'content': system},
                            {'role': 'user', 'content': [{'type': 'text', 'text': inputs[index]}]}])
                        self.assertFalse({'conversation_id', 'session_id', 'previous_response_id'}.intersection(payload))
                    self.assertNotIn('CASE_FIRST_BEGIN', json.dumps(payloads[1], ensure_ascii=False))

    def _run(self, case, fail_when, *, mutate=None, pos_identities=None):
        provider = PdfEvidenceProvider('synthetic','medium',stage_summary_reviewer=isolated_stage_summary)
        self.calls,self.successes,self.prompts=[],[],[]
        def call(root,skill,schema,prompt,**kwargs):
            ids=tuple(r.id for r in rules(case['scenario']))
            self.calls.append(ids);self.prompts.append(prompt.format())
            if fail_when(ids):raise CodexContextCapacityError('synthetic provider capacity')
            value=unknown_checks(case['scenario'],case['flags'],materials=case['materials'],units=case['units'])
            if mutate:mutate(ids,value)
            self.successes.append(ids)
            return value
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(provider,'_call',side_effect=call), \
             patch('audit_core.pdf_materials.prepare_product_references',return_value=({},[])), \
             patch('audit_core.pdf_materials.prepare_pos_identities',return_value=pos_identities or {}):
            return provider.audit(case,Path(directory))

    def test_large_settlement_rules_are_independent_and_calculation_excludes_qince(self):
        case, _ = case_for()
        parts = batches(case)
        active = [p for p in parts if p['name'] == 'settlement' and not p.get('program_not_applicable')]
        self.assertGreater(len(active), 1)
        self.assertTrue(all(len(p['rule_ids']) == 1 for p in active))
        calculation = next(p for p in active if p['rule_ids'] == ['settlement_calculation'])
        qince_ids = {u['unit_id'] for u in case['units'] if u.get('source_kind') == 'qince'}
        self.assertFalse(qince_ids.intersection(d['unit_id'] for d in calculation['documents']))
        self.assertIn('settlement', {d['unit_id'] for d in calculation['documents']})
        ids = [key for part in parts for key in part['rule_ids']]
        self.assertCountEqual(ids, [r.id for r in rules(case['scenario'])])
        self.assertEqual(len(ids), len(set(ids)))







if __name__ == '__main__':
    unittest.main()


class ModelCapacityBoundaryTests(unittest.TestCase):
    def test_full_rules_and_input_are_left_to_the_model(self):
        case,_=case_for();original=deepcopy(case)
        runner=CapacityRecoveryTests();result=runner._run(case,lambda ids:False)
        self.assertEqual(runner.calls,[tuple(r.id for r in rules(case['scenario']))])
        self.assertIn('pos_arithmetic',runner.calls[0])
        self.assertIn('application_claim_ceiling',runner.calls[0])
        self.assertTrue(all(c['status'] in {'unknown','not_applicable'} for c in result['checks']))
        self.assertEqual(case,original)

    def test_actual_provider_capacity_error_does_not_invent_business_outcome(self):
        case,_=case_for();runner=CapacityRecoveryTests()
        with self.assertRaises(CodexContextCapacityError):runner._run(case,lambda ids:True)
        self.assertEqual(len(runner.calls),1)
        self.assertEqual(runner.successes,[])
