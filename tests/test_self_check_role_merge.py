"""Final self-check ownership with real merge/role guards and no model calls."""
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.agent_orchestration import check_report, scoped_case
from audit_core.common import AuditError
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_authority import execution
from audit_core.model_self_check import merge_self_check_result, review_stage
from audit_core.model_owned_audit import run_single
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.test_agent_orchestration import fixture_case, offline_helpers


class SelfCheckRoleMergeTests(TestCase):
    def fixture(self, role='documents'):
        case = scoped_case(fixture_case(), role)
        selected = [s['id'] for s in case['_agent_rule_specs']]
        checks = [{'rule_id': rule, 'status': 'unknown', 'reason': '按本单原件保留待核实项。',
                   'source_ids': [case['documents'][0]['unit_id']]} for rule in selected]
        value = {'checks': checks, 'photo_reviews': [{'unit_id': 'display_photos', 'checks': [{
            'rule_id': 'display_watermark', 'status': 'fail', 'reason': '原照片判断。',
            'source_ids': ['display_photos']}]}]}
        return case, selected, value, {'ready': True, 'notes': [], 'checks': deepcopy(checks)}

    def test_omitted_nonowner_photo_field_does_not_revive_previous_rows(self):
        for role in ('documents', 'numbers'):
            with self.subTest(role=role):
                _, selected, value, answer = self.fixture(role)
                before = deepcopy((value, answer))
                result = merge_self_check_result(value, answer, agent_id=role)
                check_report(result, selected)
                self.assertEqual(result['checks'], answer['checks'])
                self.assertNotIn('photo_reviews', result)
                self.assertEqual((value, answer), before)

    def test_visual_and_single_worker_keep_omitted_owned_evidence(self):
        _, _, value, answer = self.fixture('visual')
        for role in ('visual', None):
            with self.subTest(role=role):
                result = merge_self_check_result(value, answer, agent_id=role)
                self.assertEqual(result['photo_reviews'], value['photo_reviews'])
        answer['photo_reviews'] = []
        self.assertEqual(merge_self_check_result(value, answer, agent_id='visual')['photo_reviews'], [])

    def test_fresh_foreign_checks_or_photo_answers_still_fail_role_guard(self):
        _, selected, value, answer = self.fixture()
        for field in ('checks', 'photo_reviews'):
            with self.subTest(field=field):
                fresh = deepcopy(answer)
                fresh[field] = deepcopy(value[field] if field == 'photo_reviews'
                                         else value['photo_reviews'][0]['checks'])
                result = merge_self_check_result(value, fresh, agent_id='documents')
                with self.assertRaisesRegex(AuditError, 'display_watermark'):
                    check_report(result, selected)

    def test_live_self_check_applies_model_answer_with_role_specific_schema(self):
        case, selected, value, answer = self.fixture()
        provider = PdfEvidenceProvider('fixture', 'max')
        def call(root, skill, schema, prompt, **kwargs):
            self.assertNotIn('photo_reviews', schema['properties'])
            kwargs['validator'](answer)
            return deepcopy(answer)
        with TemporaryDirectory() as folder, execution(), collect_model_artifacts(Path(folder)/'analysis'), \
                patch.object(provider, '_call', side_effect=call):
            result = review_stage(provider, case, value, {}, Path(folder)/'same-root',
                                  selected, lambda *args: {})
            check_report(result, selected)
            self.assertEqual(result['checks'], answer['checks'])
            self.assertTrue((Path(folder)/'analysis/model-observations/model-self-check-result-same-root-documents.json').is_file())

    def test_business_audit_schema_only_advertises_photo_reviews_to_owner(self):
        for role in ('visual', 'documents', 'numbers'):
            with self.subTest(role=role), TemporaryDirectory() as folder, ExitStack() as stack:
                offline_helpers(stack)
                stack.enter_context(collect_model_artifacts(Path(folder)/'analysis'))
                case, _, value, _ = self.fixture(role)
                value.pop('photo_reviews', None)
                provider = PdfEvidenceProvider('fixture', 'max')
                def call(root, skill, schema, prompt, **kwargs):
                    self.assertEqual('photo_reviews' in schema['properties'], role == 'visual')
                    return deepcopy(value)
                stack.enter_context(patch.object(provider, '_call', side_effect=call))
                stack.enter_context(patch.object(provider, '_review_stage_summary', side_effect=lambda case, value, *a, **k: value))
                run_single(provider, case, Path(folder)/'calls')
