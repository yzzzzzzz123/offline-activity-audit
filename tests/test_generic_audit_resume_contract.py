"""The full visual RAG prompt obeys the same source-bound resume contract."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.failed_upload_resume import ResumeEvidenceProvider
from audit_core.resume_request_identity import request_identity
from tests.test_failed_upload_resume import CONNECTION, SCHEMA, request, result, pending, trace_prefix


def photo_prompt(*, policy='old', fact='原图水印与活动价格', unit='photo-1', role='price_photo',
                 rule='price_evidence', reference='same', materials=None, flags=None,
                 auxiliary='reference-a', rule_text='核对本图活动价格'):
    materials = materials if materials is not None else [{'kind': 'price_photo', 'source_ids': ['photo-1']}]
    focus = {'scope': '本批完整对应资料', 'rule_ids': [rule],
             'reference_fields': {'contract': reference}, 'material_inventory': materials,
             'omitted_other_roles_are_not_missing': True}
    sections = {
        '本批索引：': focus,
        '本次现场照片及unit_id：': [{'unit_id': unit, 'image': 'photo.jpg'}],
        '本次对应OSS细节图记录（reference_only，不是本次证据）：': {'reference': auxiliary},
        '已经识别的完整事实：': [{'unit_id': unit, 'roles': [role], 'facts': [fact], 'limitations': []}],
        '已盘点的资料清单：': materials,
        '适用条件：': flags if flags is not None else {'online': False},
        '唯一审核要点：': [{'id': rule, 'text': rule_text, 'numeric': False, 'applicable': True}],
    }
    return '公共政策说明=' + policy + '\n' + '\n'.join(
        name + json.dumps(value, ensure_ascii=False) for name, value in sections.items())


def payload(text, *, schema=None, images=None):
    return {**request(2, 'photo', '照片审核', text)['data'],
            'output_schema': schema or SCHEMA, 'images': images or []}


class GenericAuditResumeContractTests(unittest.TestCase):
    def provider(self, original, *, completed=False, copies=1):
        traces = trace_prefix()
        if completed:
            traces += [request(2, 'target', '商品', 'source A'), result(3, 'target', '商品', {'ok': True})]
        for index in range(copies):
            sequence = 4 + index * 2
            event = request(sequence, 'photo-' + str(index), '照片审核', original['input_text'])
            event['data'] = original | {'attempt': 1}
            traces += [event, result(sequence + 1, event['call_id'], '照片审核', {'ok': True})]
        if not completed:
            traces += pending()
        return ResumeEvidenceProvider(CONNECTION, traces,
            revalidate_from_call_id='target' if completed else None)

    def call(self, provider, current, *, validator=lambda value: None):
        with patch('audit_core.failed_upload_resume.current_request', return_value=current):
            return provider._call(Path('unused'), Path('skill'), current['output_schema'],
                current['input_text'], images=[], label='照片审核', validator=validator)

    def test_full_visual_prompt_reuses_identical_sources_after_policy_and_schema_repair(self):
        old = payload(photo_prompt())
        current = payload(photo_prompt(policy='repaired'), schema={**SCHEMA, 'additionalProperties': False})
        identities = [request_identity(value, '照片审核') for value in (old, current)]
        self.assertNotEqual(identities[0]['request'], identities[1]['request'])
        self.assertEqual(identities[0]['source'], identities[1]['source'])
        self.assertIsNotNone(identities[0]['lineage'])
        checked = []
        provider = self.provider(old)
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call', side_effect=AssertionError('Model forbidden')):
            answer = self.call(provider, current, validator=lambda value: checked.append(value['ok']))
        self.assertEqual(answer, {'ok': True})
        self.assertEqual(checked, [True])
        self.assertTrue(provider.reused_checkpoints[0]['same_source_current_contract'])

    def test_every_source_dimension_and_actual_rule_remains_part_of_identity(self):
        old = payload(photo_prompt())
        identity = request_identity(old, '照片审核')
        variants = [photo_prompt(**change) for change in (
            {'fact': '另一原始价格'}, {'unit': 'other-unit'}, {'role': 'promotion_contract'},
            {'rule': 'other_rule'}, {'rule_text': '不同的实际审核标准'},
            {'materials': [{'kind': 'other', 'source_ids': ['other-unit']}]},
            {'flags': {'online': True}}, {'auxiliary': 'reference-b'})]
        for text in variants:
            with self.subTest(text=text[-100:]):
                changed = request_identity(payload(text), '照片审核')
                self.assertNotEqual(identity['source'], changed['source'])
                self.assertNotEqual(identity['lineage'], changed['lineage'])
                with patch('audit_core.pdf_materials.PdfEvidenceProvider._call', side_effect=AssertionError('Model forbidden')):
                    with self.assertRaises(AuditError): self.call(self.provider(old), payload(text))

    def test_reference_index_only_changes_known_dependency_not_original_source_lineage(self):
        old, current = payload(photo_prompt()), payload(photo_prompt(reference='corrected'))
        a, b = [request_identity(value, '照片审核') for value in (old, current)]
        self.assertNotEqual(a['source'], b['source'])
        self.assertEqual(a['lineage'], b['lineage'])
        provider = self.provider(old, completed=True)
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call', return_value={'new': True}) as normal:
            with self.assertRaises(AuditError): self.call(provider, current)
            self.assertEqual(normal.call_count, 0)
            provider._call(Path('unused'), Path('skill'), SCHEMA, 'source A', images=[], label='商品', validator=lambda _: None)
            self.assertEqual(self.call(provider, current), {'new': True})
            self.assertEqual(normal.call_count, 1)

    def test_completed_candidate_reuses_original_photo_without_new_model_call(self):
        old, current = payload(photo_prompt()), payload(photo_prompt(policy='repaired'))
        provider = self.provider(old, completed=True)
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call', side_effect=AssertionError('Model forbidden')):
            provider._call(Path('unused'), Path('skill'), SCHEMA, 'source A', images=[], label='商品', validator=lambda _: None)
            self.assertEqual(self.call(provider, current), {'ok': True})
        self.assertTrue(provider.revalidation_target_seen)
        self.assertFalse(provider.revalidated_stages)

    def test_failed_current_validation_does_not_consume_or_replace_saved_answer(self):
        old, current = payload(photo_prompt()), payload(photo_prompt(policy='repaired'))
        for mode in ('schema', 'source'):
            provider = self.provider(old)
            value = deepcopy(current)
            if mode == 'schema': value['output_schema'] = {**SCHEMA, 'required': ['missing']}
            def check(_):
                if mode == 'source': raise AuditError('Current source rejected')
            with self.subTest(mode=mode), patch('audit_core.pdf_materials.PdfEvidenceProvider._call', side_effect=AssertionError('Model forbidden')):
                with self.assertRaises(AuditError): self.call(provider, value, validator=check)
            self.assertFalse(provider.reused)
            self.assertFalse(provider._consumed)
            self.assertTrue(next(iter(provider.saved.values()))[0]['value']['ok'])

    def test_missing_duplicate_malformed_or_inconsistent_sections_fail_closed(self):
        text = photo_prompt()
        variants = [text.replace('适用条件：', 'unknown_flags：'),
                    text + '\n已经识别的完整事实：[]',
                    text.replace('适用条件：{"online": false}', '适用条件：not-json'),
                    text.replace('已盘点的资料清单：[{', '已盘点的资料清单：[{}, {'),
                    text.replace('唯一审核要点：[{"id": "price_evidence"', '唯一审核要点：[{"id": "different"')]
        for changed in variants:
            with self.subTest(text=changed[-120:]), self.assertRaises(AuditError):
                request_identity(payload(changed), '照片审核')
        untyped = payload('本批索引：{}\nunknown full source')
        self.assertIsNone(request_identity(untyped, '照片审核')['lineage'])

    def test_same_label_multiple_checkpoints_are_still_ambiguous(self):
        old, current = payload(photo_prompt()), payload(photo_prompt(policy='repaired'))
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call', side_effect=AssertionError('Model forbidden')):
            with self.assertRaisesRegex(AuditError, '多个'): self.call(self.provider(old, copies=2), current)

    def test_model_skill_image_order_and_image_bytes_cannot_cross_source_identity(self):
        old = payload(photo_prompt(), images=[{'file': 'a.jpg', 'size_bytes': 1, 'sha256': 'a'},
                                             {'file': 'b.jpg', 'size_bytes': 2, 'sha256': 'b'}])
        original = request_identity(old, '照片审核')
        for key, value in [('provider', 'other'), ('model', 'other'), ('skill', 'other'),
                           ('images', list(reversed(old['images']))),
                           ('images', [{'file': 'a.jpg', 'size_bytes': 1, 'sha256': 'changed'}, old['images'][1]])]:
            with self.subTest(key=key):
                changed = request_identity(old | {key: value}, '照片审核')
                self.assertNotEqual(original['source'], changed['source'])
                self.assertNotEqual(original['lineage'], changed['lineage'])


if __name__ == '__main__':
    unittest.main()
