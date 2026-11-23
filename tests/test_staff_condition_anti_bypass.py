"""Unambiguous source evidence cannot be downgraded by a redundant unclear quote."""
from copy import deepcopy
import unittest

from audit_core.pdf_evidence import ClassificationEvidenceError, classification_decision
from tests.test_optional_staff_and_display_photos import selected_staff


AFFIRMATIVE = "本次临时聘请促销员对产品进行推广。"
NEUTRAL = "本次聘请促销人员2名，工资按天计算。"
DENIED = "本次没有临时聘请促销员"
GUARD_MESSAGE = "结算单或促销合同已明确涉及临促，不能省略肯定原文证据或改为未涉及"


def fixture(flag, *, redundant=True):
    value = selected_staff("missing", flag)
    staff = next(item for item in value["materials"] if item["id"] == "staff_photos")
    staff["condition_evidence"] = [{"unit_id": "u", "quote": AFFIRMATIVE, "stance": "affirmed"}]
    if redundant:
        staff["condition_evidence"].append({"unit_id": "u", "quote": NEUTRAL, "stance": "unclear"})
    return value, [{"unit_id": "u", "facts": [AFFIRMATIVE, NEUTRAL]}]


class StaffConditionAntiBypassTests(unittest.TestCase):
    def assert_rejected_without_mutation(self, value, documents, message):
        before = deepcopy((value, documents))
        with self.assertRaises(ClassificationEvidenceError) as caught:
            classification_decision(value, documents, "personnel_incentive")
        self.assertEqual(str(caught.exception), message)
        self.assertEqual((value, documents), before)
        return caught.exception

    def test_null_and_false_cannot_hide_unambiguous_affirmative_source(self):
        for flag in (None, False):
            with self.subTest(flag=flag):
                value, documents = fixture(flag)
                error = self.assert_rejected_without_mutation(value, documents, GUARD_MESSAGE)
                self.assertEqual(error.validation_code, "gate_staff_condition")
                self.assertEqual(error.validation_paths, ["material_matches/0/flags/temporary_staff"])

    def test_true_and_only_affirmative_evidence_retains_genuine_missing_material(self):
        value, documents = fixture(True, redundant=False)
        before = deepcopy((value, documents))
        result = classification_decision(value, documents, "personnel_incentive")
        self.assertFalse(result["matched"])
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("临促资料或照片", result["reasons"][0])
        self.assertIs(value["flags"]["temporary_staff"], True)
        self.assertEqual((value, documents), before)

    def test_true_with_redundant_unclear_quote_still_fails_existing_consistency_rule(self):
        value, documents = fixture(True)
        self.assert_rejected_without_mutation(
            value, documents, "临促标记必须与结算单或促销合同的肯定、否定或冲突证据一致")

    def test_affirmative_and_denied_authorized_sources_remain_unconfirmed(self):
        value, documents = fixture(None, redundant=False)
        settlement = next(item for item in value["materials"] if item["id"] == "settlement")
        settlement["source_ids"] = ["settlement"]
        value["source_ids"].append("settlement")
        value["material_matches"][0]["source_ids"].append("settlement")
        documents.append({"unit_id": "settlement", "facts": [DENIED]})
        before = deepcopy((value, documents))
        result = classification_decision(value, documents, "personnel_incentive")
        self.assertTrue(result["matched"])
        self.assertEqual(result["reasons"], [])
        self.assertIsNone(value["flags"]["temporary_staff"])
        self.assertEqual((value, documents), before)

    def test_only_unclear_source_does_not_create_a_staff_requirement(self):
        value = selected_staff("missing", None)
        staff = next(item for item in value["materials"] if item["id"] == "staff_photos")
        staff["condition_evidence"] = [{"unit_id": "u", "quote": NEUTRAL, "stance": "unclear"}]
        documents = [{"unit_id": "u", "facts": [NEUTRAL]}]
        before = deepcopy((value, documents))
        result = classification_decision(value, documents, "personnel_incentive")
        self.assertTrue(result["matched"])
        self.assertEqual(result["reasons"], [])
        self.assertIsNone(value["flags"]["temporary_staff"])
        self.assertEqual((value, documents), before)


if __name__ == "__main__":
    unittest.main()
