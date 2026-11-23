from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.final_review_resume import completed_business_packets, FinalReviewResumeProvider
from audit_core.pdf_policy import POLICY_VERSION


def checkpoint():
    unit = {'unit_id': 'u1', 'source_file': 'original.jpg', 'locator': 'page 1',
            'image': '/old-temporary/image.jpg', 'native_facts': [], 'limitations': []}
    packet = {'archive_id': 'a1', 'units': [unit], 'documents': [{'unit_id': 'u1', 'facts': ['original']}],
              'route': {'scenario': 'poster_material'}, 'classification': {'flags': {}, 'materials': []},
              'audit_evidence': {'checks': [{'rule_id': 'example', 'status': 'unknown', 'source_ids': ['u1']}]}}
    traces = [{'type': 'run.started', 'sequence': 1, 'data': {'run_id': 'same-run'}},
              {'type': 'workflow.pdf_materials.validated', 'sequence': 2,
               'data': {'packets': [packet], 'policy_version': POLICY_VERSION}},
              {'type': 'workflow.result.validated', 'sequence': 3, 'data': {'scenario': 'poster_material'}}]
    manifest = {'run_id': 'same-run', 'failure': {'type': 'ImportError', 'message': "cannot import name 'restore_reports'"}}
    return packet, traces, manifest


class FinalReviewResumeTests(TestCase):
    def test_only_explicit_pre_review_import_failure_uses_completed_results(self):
        packet, traces, manifest = checkpoint()
        self.assertEqual(completed_business_packets(traces, manifest), [packet])
        self.assertIsNone(completed_business_packets(traces, {**manifest, 'failure': {'type': 'TimeoutError'}}))
        with self.assertRaises(AuditError):
            completed_business_packets(traces, {**manifest, 'run_id': 'another-run'})

    def test_missing_or_ambiguous_business_checkpoint_cannot_start_reaudit(self):
        _, traces, manifest = checkpoint()
        for altered in (traces[:-1], [traces[0], traces[-1]], traces + [traces[1]]):
            with self.subTest(traces=altered), self.assertRaises(AuditError):
                completed_business_packets(altered, manifest)

    def test_final_review_already_started_requires_its_own_resume_path(self):
        _, traces, manifest = checkpoint()
        traces.append({'type': 'workflow.logic_review.started', 'sequence': 4, 'data': {}})
        with self.assertRaises(AuditError):
            completed_business_packets(traces, manifest)

    def test_old_assignment_format_restores_only_the_original_saved_worker_split(self):
        packet, traces, manifest = checkpoint()
        packet['audit_evidence'].update(_agent_mode='main_three', _agent_owners={'example': 'visual'})
        traces.insert(1, {'type': 'audit.agents.started', 'sequence': 1.5, 'data': {
            'archive_id': 'a1', 'workers': [
                {'agent_id': 'visual', 'rule_ids': ['example']},
                {'agent_id': 'documents', 'rule_ids': []},
                {'agent_id': 'numbers', 'rule_ids': []}]}})
        restored = completed_business_packets(traces, manifest)
        self.assertEqual(restored[0]['audit_evidence']['_agent_assignments']['visual'], ['example'])
        self.assertNotIn('_agent_assignments', packet['audit_evidence'])
        packet['audit_evidence']['_agent_owners']['example'] = 'numbers'
        with self.assertRaises(AuditError):
            completed_business_packets(traces, manifest)

    def provider_and_case(self):
        packet, _, _ = checkpoint()
        connection = SimpleNamespace(model='deepseek-v4-1-flash', reasoning_effort='max')
        provider = FinalReviewResumeProvider(connection, [packet])
        case = {'archive_id': 'a1', 'units': deepcopy(packet['units']), 'selected_scenario': 'poster_material',
                'scenario': 'poster_material', 'documents': deepcopy(packet['documents']), 'materials': []}
        case['units'][0]['image'] = '/new-temporary/image.jpg'
        return provider, case, packet

    def test_only_temporary_image_paths_can_change_and_paid_audits_are_not_called(self):
        provider, case, packet = self.provider_and_case()
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call', side_effect=AssertionError('No model call')):
            reading = provider.classify(case, Path('unused'))
            evidence = provider.audit(case, Path('unused'))
        self.assertEqual(reading['documents'], packet['documents'])
        self.assertEqual(evidence, packet['audit_evidence'])
        self.assertEqual(len(provider.reused), 2)
        evidence['checks'].clear()
        self.assertTrue(provider.packets['a1']['audit_evidence']['checks'])

    def test_changed_sources_or_documents_stop_instead_of_repeating_model_work(self):
        for field in ('native_facts', 'source_file'):
            provider, case, _ = self.provider_and_case()
            case['units'][0][field] = 'changed'
            with self.subTest(field=field), self.assertRaises(AuditError):
                provider.classify(case, Path('unused'))
        provider, case, _ = self.provider_and_case()
        case['documents'][0]['facts'] = ['changed']
        with self.assertRaises(AuditError):
            provider.audit(case, Path('unused'))

    def test_continuation_preserves_legacy_history_and_keeps_credential_guard(self):
        from audit_core.run_conversation import RunConversation
        with TemporaryDirectory() as directory:
            old_profile = {'provider': 'deepseek', 'model': 'deepseek-flash'}
            legacy = RunConversation(old_profile, Path(directory), separated=True)
            legacy.append([{'type': 'text', 'text': 'LEGACY_RAW'}], '{}')
            original = (legacy.directory/'session.json').read_bytes()
            profile = {**old_profile, 'connection_scope': 'current-fixture-key'}
            continuation = RunConversation(profile, Path(directory), separated=True, namespace='final-review-test')
            self.assertEqual(continuation.turns, [])
            self.assertEqual((legacy.directory/'session.json').read_bytes(), original)
            continuation.append([{'type': 'text', 'text': 'SAVED_RESULTS_ONLY'}], '{}')
            restored = RunConversation(profile, Path(directory), separated=True, namespace='final-review-test')
            self.assertEqual(len(restored.turns), 1)
            self.assertNotIn('LEGACY_RAW', str(restored.messages('review', [])))
            with self.assertRaises(AuditError):
                RunConversation({**profile, 'connection_scope': 'another-key'}, Path(directory),
                                separated=True, namespace='final-review-test')
