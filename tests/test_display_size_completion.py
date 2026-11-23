"""A bad auxiliary size branch must not abort unrelated photo checks or rewrite raw AI."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_decision, photo_review_schema, validate, validate_photo_reviews
from tests.pdf_test_support import unknown_checks
from tests.test_display_size_options import fixture, size_review
from tests.test_optional_staff_and_display_photos import review


class DisplaySizeCompletionTests(unittest.TestCase):
    def decision(self, case, rows):
        evidence = unknown_checks('promotional_display', case['flags'])
        evidence['photo_reviews'] = rows
        before = deepcopy(evidence)
        result = audit_decision('promotional_display', case['flags'], evidence,
                                case['documents'], case['materials'], units=case['units'])
        self.assertEqual(evidence, before)
        return result

    def test_auxiliary_branch_does_not_replace_actual_photo_judgement(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), 2)
            case['documents'][0]['facts'] = ['每店1个堆头或陈列；本次8个1平米堆头。']
            bad, ordinary = size_review('p01'), review('p02')
            bad['checks'][3]['size_options']['contract_source']['quote'] = case['documents'][0]['facts'][0]
            result = self.decision(case, [bad, ordinary])
            projected = result['photo_reviews'][0]['checks']
            self.assertEqual(projected[3]['status'], bad['checks'][3]['status'])
            self.assertNotIn('size_options', projected[3])
            self.assertEqual(projected[3]['reason'], bad['checks'][3]['reason'])
            self.assertEqual([c['status'] for c in projected[:3]], ['pass'] * 3)
            self.assertEqual(result['photo_reviews'][1]['checks'][3]['status'], 'pass')
            self.assertEqual(next(c for c in result['checks'] if c['rule_id'] == 'display_size')['status'], 'pass')

    def test_missing_auxiliary_detail_preserves_counted_and_estimated_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            row = review('p01')
            row['checks'][3]['reason'] = '本图约4列、前后4排，包装边长约25厘米，估算宽深各1米，基本满足合同1平米。'
            schema = photo_review_schema(['p01'], ['p01', 'contract', 'application'], ['contract'],
                                         size_alternatives=True, allow_unverified_size_options=True)
            validate({'photo_reviews': [row]}, schema)
            result = self.decision(case, [row])
            actual = result['photo_reviews'][0]['checks'][3]
            self.assertEqual((actual['status'], actual['reason']), ('pass', row['checks'][3]['reason']))

    def test_auxiliary_issue_preserves_true_shortfall_and_visual_uncertainty(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            for status, reason in [('fail', '本图2列2排，包装宽深约25厘米，估算约0.25平米，明显不足1平米。'),
                                   ('unknown', '可见4列，侧面完全遮挡，无法估计前后排数或柜体深度。')]:
                with self.subTest(status=status):
                    row = size_review('p01')
                    row['checks'][3].update(status=status, reason=reason)
                    row['checks'][3]['size_options']['contract_source']['quote'] = '不存在的规格分支'
                    actual = self.decision(case, [row])['photo_reviews'][0]['checks'][3]
                    self.assertEqual((actual['status'], actual['reason']), (status, reason))

    def test_valid_endcap_or_area_evidence_still_passes(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            result = self.decision(case, [size_review('p01')])
            self.assertEqual(result['photo_reviews'][0]['checks'][3]['status'], 'pass')
            self.assertIn('size_options', result['photo_reviews'][0]['checks'][3])

    def test_irrelevant_auxiliary_does_not_change_brand_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            row = size_review('p01')
            row['checks'][1]['size_options'] = deepcopy(row['checks'][3]['size_options'])
            result = self.decision(case, [row])
            self.assertEqual(result['photo_reviews'][0]['checks'][1]['status'], 'pass')
            self.assertNotIn('size_options', result['photo_reviews'][0]['checks'][1])

    def test_normal_mode_still_rejects_missing_photo_or_unissued_primary_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), 2)
            for rows in ([size_review('p01')], [size_review('p01'), size_review('p02')]):
                if len(rows) == 2:
                    rows[0]['checks'][3]['source_ids'].append('other-invoice-photo')
                with self.subTest(rows=len(rows)), self.assertRaises(AuditError):
                    validate_photo_reviews({'photo_reviews': rows}, ['p01', 'p02'], case['documents'],
                                           case['materials'], case['units'], require_size_options=True,
                                           allow_unverified_size_options=True)

    def test_strict_diagnostic_validator_remains_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            row = size_review('p01')
            row['checks'][3]['size_options']['contract_source']['quote'] = '合同没有提供的规格分支'
            with self.assertRaises(AuditError):
                validate_photo_reviews({'photo_reviews': [row]}, ['p01'], case['documents'], case['materials'])


if __name__ == '__main__':
    unittest.main()
