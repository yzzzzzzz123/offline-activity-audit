"""Large joint-image requests retain every original and keep final audit sources."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.model_settings import ModelSelection
from audit_core.pdf_materials import PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.product_reference_batches import bounded_product_references, image_request_bytes
from tests.pdf_test_support import unknown_checks, image_bytes
from tests.test_optional_staff_and_display_photos import review
from tests.test_photo_product_references import case_for, photo_context

MODULE = 'audit_core.product_reference_batches'


class ProductReferenceBatchTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scene = self.root / 'scene.png'
        self.scene.write_bytes(b'scene' * 12)
        self.units = [{'unit_id': 'photo', 'image': self.scene, 'source_file': 'scene.png', 'locator': '原图'}]
        self.images = []
        for index in range(4):
            path = self.root / f'ref-{index}.png'
            path.write_bytes(bytes([index]) * 400)
            self.images.append(path)
        self.references = {'reference_only': True, 'rule_ids': ['price_evidence'], 'resolutions': [],
            'limitations': [], 'references': [{'product_code': str(i), 'image': p.name} for i, p in enumerate(self.images)]}
        self.calls = []

    def model(self, root, skill, schema, prompt, *, images, label, validator):
        scenes = json.loads(prompt.partial_variables['scenes'])
        refs = json.loads(prompt.partial_variables['references'])
        value = {'scene_observations': [{'unit_id': row['unit_id'], 'facts': ['本图可见包装，部分细节无法确认'], 'limitations': []} for row in scenes],
                 'reference_observations': [{'reference_id': row['reference_id'], 'facts': ['仅本批参考外观'], 'limitations': []} for row in refs]}
        self.calls.append({'images': images, 'schema': schema, 'prompt': prompt.format(), 'value': deepcopy(value)})
        validator(value)
        return value

    def invoke(self, **kwargs):
        args = dict(root=self.root / 'batches', skill=self.root, scene_units=self.units,
                    fixed_images=[self.scene], references=self.references,
                    reference_images=self.images, call=self.model, label='test')
        args.update(kwargs)
        return bounded_product_references(**args)

    def test_small_request_keeps_original_single_call_path(self):
        meta, images = self.invoke()
        self.assertIs(meta, self.references)
        self.assertIs(images, self.images)
        self.assertEqual(self.calls, [])

    def test_large_references_all_appear_once_with_same_scene_and_unchanged_bytes(self):
        originals = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in [self.scene, *self.images]}
        before = deepcopy(self.references)
        with patch(MODULE + '.IMAGE_REQUEST_BUDGET', 2000), patch(MODULE + '.TEXT_HEADROOM', 100), \
                patch(MODULE + '.save_model_observations') as saved:
            meta, final_references = self.invoke()
            self.assertGreater(len(self.calls), 1)
            for call in self.calls:
                self.assertLessEqual(image_request_bytes(call['images']), 2000)
                self.assertEqual(call['images'][0], self.scene)
            self.assertEqual([p for c in self.calls for p in c['images'][1:]], self.images)
            self.assertEqual(final_references, [])
            self.assertEqual(len(meta['joint_visual_observations']), len(self.calls))
            self.assertTrue(meta['image_delivery']['all_original_reference_images_included'])
            saved.assert_called_once()
        self.assertEqual(self.references, before)
        self.assertEqual(originals, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in originals})

    def test_wrong_or_duplicate_current_sources_still_fail_without_using_partial_facts(self):
        for error in ('other_scene', 'other_reference', 'duplicate_reference', 'empty_facts'):
            def bad(root, skill, schema, prompt, **kwargs):
                refs = json.loads(prompt.partial_variables['references'])
                value = {'scene_observations': [{'unit_id': 'photo', 'facts': ['current scene'], 'limitations': []}],
                    'reference_observations': [{'reference_id': r['reference_id'], 'facts': ['reference'], 'limitations': []} for r in refs]}
                if error == 'other_scene':
                    value['scene_observations'][0]['unit_id'] = 'other-invoice'
                elif error == 'other_reference':
                    value['reference_observations'][0]['reference_id'] = 'unissued-reference'
                elif error == 'duplicate_reference':
                    value['reference_observations'][1]['reference_id'] = value['reference_observations'][0]['reference_id']
                else:
                    value['scene_observations'][0]['facts'] = []
                kwargs['validator'](value)
            with self.subTest(error=error), patch(MODULE + '.IMAGE_REQUEST_BUDGET', 2200), patch(MODULE + '.TEXT_HEADROOM', 100), \
                    patch(MODULE + '.save_model_observations') as saved, self.assertRaises(AuditError):
                self.invoke(call=bad)
            saved.assert_not_called()

    def test_one_image_that_cannot_fit_is_not_dropped_or_resized(self):
        before = self.images[0].read_bytes()
        with patch(MODULE + '.IMAGE_REQUEST_BUDGET', 500), patch(MODULE + '.TEXT_HEADROOM', 100), self.assertRaises(AuditError):
            self.invoke()
        self.assertEqual(self.images[0].read_bytes(), before)
        self.assertEqual(self.calls, [])

    def test_large_scenes_compare_every_reference_group_and_only_omit_reviewed_scenes(self):
        units = []
        for index in range(5):
            path = self.root / f'scene-{index}.png'
            path.write_bytes(bytes([index + 20]) * 420)
            units.append({'unit_id': f'photo-{index}', 'image': path, 'source_file': path.name})
        protected = units[0]['image']
        originals = {p: p.read_bytes() for p in [*(u['image'] for u in units), *self.images]}
        with patch(MODULE + '.IMAGE_REQUEST_BUDGET', 2400), patch(MODULE + '.TEXT_HEADROOM', 100), \
                patch(MODULE + '.save_model_observations'):
            metadata, refs = self.invoke(scene_units=units, fixed_images=[u['image'] for u in units],
                                         protected_images=[protected])
            self.assertEqual(refs, [])
            expected_pairs = {(u['image'], p) for u in units for p in self.images}
            pairs = []
            for call in self.calls:
                self.assertLessEqual(image_request_bytes(call['images']), 2400)
                scenes = [p for p in call['images'] if p in {u['image'] for u in units}]
                references = [p for p in call['images'] if p in self.images]
                pairs.extend((s, r) for s in scenes for r in references)
            self.assertEqual(set(pairs), expected_pairs)
            self.assertEqual(len(pairs), len(expected_pairs))
            delivery = metadata['image_delivery']
            self.assertEqual(delivery['omitted_scene_source_ids'], [u['unit_id'] for u in units[1:]])
            self.assertGreater(delivery['scene_groups'], 1)
            self.assertGreater(delivery['reference_groups'], 1)
        self.assertEqual(originals, {p: p.read_bytes() for p in originals})

    def test_large_scene_only_request_preserves_every_source_without_reference_invention(self):
        units = []
        for index in range(4):
            path = self.root / f'only-scene-{index}.png'
            path.write_bytes(bytes([index]) * 550)
            units.append({'unit_id': f'only-{index}', 'image': path})
        with patch(MODULE + '.IMAGE_REQUEST_BUDGET', 2300), patch(MODULE + '.TEXT_HEADROOM', 100), \
                patch(MODULE + '.save_model_observations'):
            metadata, refs = self.invoke(scene_units=units, fixed_images=[u['image'] for u in units],
                references={**self.references, 'references': []}, reference_images=[])
            self.assertEqual(refs, [])
            self.assertEqual([p for call in self.calls for p in call['images']], [u['image'] for u in units])
            self.assertTrue(all(image_request_bytes(c['images']) <= 2300 for c in self.calls))
            self.assertEqual(metadata['image_delivery']['omitted_scene_source_ids'], [u['unit_id'] for u in units])
            self.assertTrue(all(not row['reference_observations'] for row in metadata['joint_visual_observations']))

    def test_protected_payment_or_template_images_are_never_silently_dropped(self):
        large = self.root / 'payment.png'
        large.write_bytes(b'payment' * 1000)
        with patch(MODULE + '.IMAGE_REQUEST_BUDGET', 2000), patch(MODULE + '.TEXT_HEADROOM', 100), \
                self.assertRaises(AuditError):
            self.invoke(fixed_images=[self.scene, large], protected_images=[large])
        self.assertEqual(self.calls, [])

    def test_normal_audit_uses_reading_facts_without_preloading_original_images(self):
        for provider_name in ('ppio','deepseek'):
            for scenario in ('entry_fee','price_difference_support','promotional_display'):
                with self.subTest(provider=provider_name,scenario=scenario):
                    case=case_for(self.root,scenario)
                    for unit in case['units']:unit['image'].write_bytes(image_bytes())
                    original=deepcopy(case);output=unknown_checks(scenario,case['flags'],materials=case['materials'],units=case['units'])
                    provider=PdfEvidenceProvider(None,None,connection=ModelSelection(provider_name,'mock-model','default','https://mock.invalid/v1','fake-key'),stage_summary_reviewer=isolated_stage_summary)
                    with patch.object(provider,'_call',return_value=output) as model, \
                         patch('audit_core.pdf_materials.template_comparison',return_value=({},[])), \
                         patch('audit_core.pdf_materials.prepare_pos_identities',return_value={}), \
                         patch('audit_core.pdf_materials.prepare_product_references',return_value=(self.references,self.images)):
                        result=provider.audit(case,self.root)
                    self.assertEqual(model.call_count,1)
                    request=json.loads(model.call_args.args[3].partial_variables['request'])
                    self.assertEqual(request['documents'],case['documents'])
                    self.assertEqual(request['product_references'],self.references)
                    self.assertEqual(request['original_images'],
                                     [{'unit_id':u['unit_id'],'image':u['image'].name} for u in case['units']])
                    self.assertEqual(model.call_args.kwargs['images'],[])
                    self.assertEqual(result,output);self.assertEqual(case,original)

    def test_completed_joint_reference_observations_are_available_without_resending_scene(self):
        case=case_for(self.root,'entry_fee')
        refs={**self.references,'joint_visual_observations':[{'facts':['本单已完成的原图与参考图联合观察。']} ]}
        provider=PdfEvidenceProvider(None,None,stage_summary_reviewer=isolated_stage_summary)
        value=unknown_checks(case['scenario'],case['flags'],materials=case['materials'],units=case['units'])
        with patch.object(provider,'_call',return_value=value) as model, \
             patch('audit_core.pdf_materials.prepare_product_references',return_value=(refs,[])), \
             patch('audit_core.pdf_materials.prepare_pos_identities',return_value={}), \
             patch('audit_core.pdf_materials.template_comparison',return_value=({},[])):
            provider.audit(case,self.root)
        self.assertNotIn(self.root/'photo.png',model.call_args.kwargs['images'])
        self.assertIn('本单已完成的原图与参考图联合观察',model.call_args.args[3].format())


if __name__ == '__main__':
    unittest.main()
