"""One activity-period assertion can cite its own complete start and end fields."""
from copy import deepcopy
import unittest

from audit_core.display_source_evidence import DisplaySourceError, validate_display_sources
from tests.test_display_source_evidence import fixture, check


class DisplayPeriodReferencesTests(unittest.TestCase):
    def setUp(self):
        self.docs, self.materials, self.units = fixture()
        self.assertion = {'kind': 'activity_period',
            'statement': '本单申请周期为2026-10-01至2026-10-10。',
            'references': [{'unit_id': 'a', 'field': field,
                'quote': next(f for d in self.docs if d['unit_id'] == 'a' for f in d['facts'] if f.startswith(field + '='))}
                for field in ('startDate', 'endDate')]}

    def validate(self, assertion=None):
        value = check('本图日期在本单申请周期内。', [assertion or self.assertion])
        before = deepcopy((value, self.docs, self.materials, self.units))
        validate_display_sources(value, self.docs, self.materials, self.units,
            path='photo_reviews/0/checks/0', photo_unit_id='p', strict_prose=False)
        self.assertEqual((value, self.docs, self.materials, self.units), before)

    def test_same_record_start_and_end_validate_without_rewriting_reason_or_state(self):
        self.validate()
        self.assertEqual(len(self.assertion['references']), 2)

    def test_reverse_reference_order_still_keeps_the_original_field_bindings(self):
        assertion = deepcopy(self.assertion)
        assertion['references'].reverse()
        self.validate(assertion)

    def test_duplicate_field_mixed_record_and_unknown_source_are_rejected(self):
        for error in ('duplicate', 'other_unit', 'unknown'):
            assertion = deepcopy(self.assertion)
            if error == 'duplicate':
                assertion['references'][1] = deepcopy(assertion['references'][0])
            else:
                assertion['references'][1]['unit_id'] = 'v' if error == 'other_unit' else 'unread'
            with self.subTest(error=error), self.assertRaises(DisplaySourceError):
                self.validate(assertion)

    def test_bad_second_quote_and_unlinked_record_keep_strict_source_checks(self):
        assertion = deepcopy(self.assertion)
        assertion['references'][1]['quote'] = 'endDate="2026-10-11"'
        with self.assertRaises(DisplaySourceError) as raised:
            self.validate(assertion)
        self.assertEqual(raised.exception.validation_paths,
            ['photo_reviews/0/checks/0/source_assertions/0/references/1/quote'])
        self.units[0]['native_facts'][1] = 'activity_code="OTHER"'
        self.docs[0]['facts'][1] = 'activity_code="OTHER"'
        with self.assertRaises(DisplaySourceError):
            self.validate()

    def test_other_assertion_kinds_do_not_gain_mixed_field_reference_permission(self):
        assertion = deepcopy(self.assertion)
        assertion['kind'] = 'application_customer'
        with self.assertRaises(DisplaySourceError):
            self.validate(assertion)


if __name__ == '__main__':
    unittest.main()
