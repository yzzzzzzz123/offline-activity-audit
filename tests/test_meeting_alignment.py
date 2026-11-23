"""Offline checks for the adopted meeting decisions and real request routing."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import json
import unittest
from unittest.mock import patch

from tests.reader_test_support import new_test_dir, remove_test_dir
from audit_core import pdf_policy as policy
from audit_core.agent_orchestration import WORKERS, worker_provider, scoped_case
from audit_core.model_settings import ModelSettingsStore, api_selection, selection_from_environment, SNAPSHOT_ENV
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pos_arithmetic import prepare_pos_arithmetic
from audit_core.pdf_evidence import calculate, document_map
from audit_core.submitted_source_checks import submitted_source_checks
from audit_core.native_pos_fields import native_pos_field_check
from audit_core.run_conversation import current, current_agent_id, isolated


def connection(model='deepseek-v4-1-flash', key='fake-doc-key', visual=None):
    value = {'provider': 'zimaocloud', 'model': model, 'api_key': key, 'reasoning_effort': 'max'}
    if visual is not None:
        value['visual'] = json.loads(visual.subprocess_value())
    return api_selection(value)


class MeetingPolicyTests(unittest.TestCase):
    def test_eight_types_have_specific_seal_and_pdf_rules(self):
        catalogue = policy.catalogue()
        self.assertEqual(catalogue['version'], '2026-11-17.xlsx.53')
        self.assertEqual(len(catalogue['types']), 8)
        for scenario in catalogue['types']:
            rules = {r.id: r for r in policy.rules(scenario)}
            self.assertIn('stamped_document_pdf', rules)
            if 'contract_signed' in rules:
                self.assertIn('经销商公章', rules['contract_signed'].text)
                self.assertNotIn('公章或财务专用章', rules['contract_signed'].text)
                self.assertNotIn('任何实际印章即视为已盖章', rules['contract_signed'].text)
        self.assertNotIn('还允许合同章', next(r.text for r in policy.rules('entry_fee') if r.id == 'entry_agreement'))

    def test_entry_pos_alternative_preserves_channel_thresholds(self):
        item = next(m for m in policy.requirements('entry_fee') if m.id == 'entry_system')
        self.assertEqual(item.when, 'cvs_otc')
        self.assertIn('二选一', item.text)
        self.assertNotIn('POS不能替代', json.dumps(policy.catalogue(), ensure_ascii=False))
        self.assertEqual(policy.entry_photo_requirement(10, True), ('stores', 10))
        self.assertEqual(policy.entry_photo_requirement(50, True), ('stores', 40))
        self.assertEqual(policy.entry_photo_requirement(100, True), ('stores', 50))
        self.assertEqual(policy.entry_photo_requirement(101, True), ('photos', 50))
        self.assertEqual(policy.entry_photo_requirement(101, False), ('stores', 101))

    def test_new_workbook_requires_gift_pos_and_photos_and_invoice(self):
        materials = {m.id: m for m in policy.requirements('self_procured_gift_material')}
        for flags, optional in [({'uses_pos': True, 'photo_evidence': False}, 'gift_store_photos'),
                                ({'uses_pos': False, 'photo_evidence': True}, 'pos')]:
            self.assertTrue(policy.applicability(materials[optional].when, 'self_procured_gift_material', flags))
            self.assertTrue(policy.applicability(materials['invoice'].when, 'self_procured_gift_material', flags))
        self.assertTrue(policy.applicability('required_pos', 'self_procured_gift_material', {'uses_pos': False}))

    def test_documents_worker_can_see_alternative_sources(self):
        for scenario, roles in [('entry_fee', ['pos_excel', 'entry_system']),
                                ('self_procured_gift_material', ['pos_excel', 'gift_store_photos'])]:
            case = {'archive_id': 'a1', 'scenario': scenario, 'flags': {},
                    'materials': [{'id': r, 'state': 'present', 'source_ids': [r]} for r in roles],
                    'documents': [{'unit_id': r, 'roles': [r], 'facts': [r], 'limitations': []} for r in roles],
                    'units': [{'unit_id': r, 'source_file': r, 'locator': r} for r in roles]}
            packet = scoped_case(case, 'documents')
            self.assertEqual({d['unit_id'] for d in packet['documents']}, set(roles))

    def test_sku_still_belongs_to_existing_photo_agent(self):
        from audit_core.agent_orchestration import owner
        self.assertEqual(WORKERS, ('visual', 'documents', 'numbers'))
        self.assertEqual(owner('entry_sku'), 'visual')
        text = next(r.text for r in policy.rules('entry_fee') if r.id == 'entry_sku')
        self.assertIn('逐店', text)
        self.assertIn('不能以另店照片补齐', text)


class MeetingPosTests(unittest.TestCase):
    def fixture(self, total='100'):
        facts = ['Sheet1!R1: C1=门店 | C2=销售金额', 'Sheet1!R2: C1=甲店 | C2=40',
                 'Sheet1!R3: C1=乙店 | C2=60', f'Sheet1!R4: C1=合计 | C2={total}']
        # Use the native reader's exact cell serialization contract.
        from audit_core.spreadsheet_rows import ROW
        self.assertIsNotNone(ROW.fullmatch(facts[0]))
        docs = [{'unit_id': 'u1', 'roles': ['spreadsheet'], 'facts': facts, 'limitations': []}]
        materials = [{'id': 'pos_excel', 'state': 'present', 'source_ids': ['u1']}]
        units = [{'unit_id': 'u1', 'file_id': 'f1', 'source_file': 'pos.xlsx', 'source_kind': 'spreadsheet'}]
        return docs, materials, units

    def test_amount_only_table_is_complete_without_quantity_or_price(self):
        docs, materials, units = self.fixture()
        plan = prepare_pos_arithmetic(docs, materials, units, amount_only=True)
        self.assertTrue(plan['complete'], plan['limitations'])
        self.assertEqual(len(plan['comparisons']), 1)
        self.assertTrue(calculate(plan['comparisons'][0], document_map(docs))['matches'])
        self.assertFalse(prepare_pos_arithmetic(docs, materials, units)['complete'])

    def test_amount_shortfall_is_preserved(self):
        docs, materials, units = self.fixture('99.99')
        plan = prepare_pos_arithmetic(docs, materials, units, amount_only=True)
        self.assertFalse(calculate(plan['comparisons'][0], document_map(docs))['matches'])

    def test_target_restores_full_fields_and_native_path(self):
        docs, materials, units = self.fixture()
        self.assertEqual(submitted_source_checks('pos_target_incentive', {'uses_pos': True}, docs, materials, units), [])
        check, proof = native_pos_field_check({'scenario': 'pos_target_incentive'})
        self.assertIsNone(check)
        self.assertIn('5+2', proof['scope'])
        self.assertNotEqual(proof.get('fallback_reason'), 'amount_only_policy_uses_original_sources')


class MeetingRoutingTests(unittest.TestCase):
    def setUp(self):
        self.folder = new_test_dir('reader-test-meeting-routing')
        self.visual = connection('qwen3.8-max', 'fake-visual-key')
        self.primary = connection(visual=self.visual)
        self.provider = PdfEvidenceProvider(self.primary.model, 'max', connection=self.primary)

    def tearDown(self):
        remove_test_dir(self.folder)

    def test_snapshot_retains_separate_keys_without_repr_exposure(self):
        with patch.dict('os.environ', {SNAPSHOT_ENV: self.primary.subprocess_value()}):
            frozen = selection_from_environment()
        self.assertEqual(frozen, self.primary)
        self.assertNotEqual(frozen.api_key, frozen.visual.api_key)
        self.assertNotIn('fake-', repr(frozen))
        self.assertNotIn('fake-', repr(frozen.visual))

    def test_new_visual_key_does_not_replace_deepseek_profile(self):
        store = ModelSettingsStore(self.folder)
        store.save(connection(), expected_revision=0)
        original = store._read()['profiles']
        store.save_visual(self.visual, expected_revision=1)
        self.assertEqual(store._read()['profiles'], original)
        self.assertEqual(store.snapshot(), self.primary)
        self.assertNotIn('fake-', store.path.read_text('utf-8'))
        store.save(connection(), expected_revision=2)
        self.assertEqual(store.snapshot().visual, self.visual)

    def test_real_call_boundary_routes_all_four_existing_roles(self):
        def capture(provider, *args, **kwargs):
            return (provider.connection.model, provider.connection.api_key,
                    current_agent_id(), current().profile['model'] if current() else None)
        def call(role, images=()):
            provider = worker_provider(self.provider, role)
            return provider._call(self.folder / role, self.folder, {}, None,
                                  images=list(images), label=role, validator=lambda x: x)
        with patch.object(PdfEvidenceProvider, '_call_stage', capture):
            with ThreadPoolExecutor(max_workers=3) as pool:
                answers = dict(zip(WORKERS, pool.map(call, WORKERS)))
            self.assertEqual(answers['visual'], ('qwen3.8-max', 'fake-visual-key', 'visual', 'qwen3.8-max'))
            for role in ('documents', 'numbers'):
                self.assertEqual(answers[role], ('deepseek-v4-1-flash', 'fake-doc-key', role, 'deepseek-v4-1-flash'))
            self.assertEqual(call('main')[0], 'deepseek-v4-1-flash')
        self.assertEqual(self.provider.connection, self.primary)

    def test_nonvisual_image_request_delegates_pixels_and_resumes_own_text_model(self):
        from audit_core.independent_image_review import run
        from audit_core.prompts import bound_prompt
        from tests.pdf_test_support import image_bytes
        picture = self.folder / 'scan.png'
        picture.write_bytes(image_bytes())
        calls = []

        def capture(provider, root, skill, schema, prompt, *, images, label, validator, **options):
            if options.pop('original_reinspection', False) and images:
                return run(provider, root, skill, schema, prompt, images=images, label=label,
                           validator=validator, **options)
            calls.append((provider.connection.model, current_agent_id(), len(images), current() is None))
            if options.get('transcription'):
                return {'facts': ['可见本页财务专用章'], 'limitations': []}
            self.assertIn('可见本页财务专用章', prompt.format())
            return {'checks': []}

        for role in ('documents', 'numbers'):
            calls.clear()
            worker = worker_provider(self.provider, role)
            with patch.object(PdfEvidenceProvider, '_call_stage', capture):
                worker._call(self.folder / role, self.folder, {}, bound_prompt('只完成本职任务'),
                             images=[picture], label=role, validator=lambda x: x)
            self.assertEqual(calls, [('qwen3.8-max', 'visual', 1, True),
                                    ('deepseek-v4-1-flash', role, 0, False)])

    def test_transcription_is_isolated_and_cache_names_qwen(self):
        def capture(provider, *args, **kwargs):
            self.assertIsNone(current())
            self.assertEqual(current_agent_id(), 'visual')
            return provider.connection
        with patch.object(PdfEvidenceProvider, '_call_stage', capture), isolated():
            actual = self.provider._call(self.folder, self.folder, {}, None, images=[Path('p.png')],
                                         label='read', validator=lambda x: x, transcription=True)
        self.assertEqual(actual, self.visual)
        identity, persistent = self.provider._reading_identity()
        self.assertEqual(identity['model'], 'qwen3.8-max')
        self.assertEqual(identity['effort'], 'max')
        self.assertTrue(persistent)


if __name__ == '__main__':
    unittest.main()
