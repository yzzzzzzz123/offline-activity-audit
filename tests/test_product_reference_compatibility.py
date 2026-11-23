"""Optional literal-reference compaction must preserve existing input forms."""
from copy import deepcopy
from unittest import TestCase
from audit_core.common import AuditError
from audit_core.product_reference_batches import compact_product_references, references_for_rules


class ProductReferenceCompatibilityTests(TestCase):
    def test_product_visual_facts_are_not_resent_to_watermark_or_date_checks(self):
        references = {'rule_ids': ['entry_sku'], 'references': [{'product_code': 'p1'}],
            'joint_visual_observations': [{'facts': ['完整原始外观事实']}], 'limitations': ['原有限制']}
        before = deepcopy(references)
        scoped = references_for_rules(references, ['entry_watermark', 'entry_address'])
        self.assertNotIn('joint_visual_observations', scoped)
        self.assertIs(references_for_rules(references, ['entry_sku']), references)
        self.assertEqual(references, before)

    def test_legacy_unbound_references_are_never_silently_discarded(self):
        for references in ([], {'references': [{'facts': ['原始事实']}]}, ['legacy']):
            self.assertIs(references_for_rules(references, ['entry_watermark']), references)

    def test_empty_and_legacy_lists_preserve_every_original_value(self):
        for references in ([],[{'product_name':'原名','source_ids':['u'],'facts':['原始事实']}],['原有引用']):
            before=deepcopy(references)
            self.assertIs(compact_product_references(references),references)
            self.assertEqual(references,before)

    def test_non_joint_reference_dict_keeps_original_shape(self):
        references={'references':[],'limitations':['原有限制']}
        self.assertIs(compact_product_references(references),references)

    def test_unexpected_shapes_are_not_silently_turned_into_empty_references(self):
        for references in (None,'invalid',0):
            with self.subTest(references=references),self.assertRaises(AuditError):compact_product_references(references)
