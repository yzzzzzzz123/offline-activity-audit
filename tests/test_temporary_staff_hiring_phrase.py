"""Explicit temporary hiring wording, with no broad promoter inference."""
from copy import deepcopy
import json
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import ClassificationEvidenceError, _staff_stance, classification_decision
from tests.test_optional_staff_and_display_photos import selected_staff


PHRASE = "临时聘请促销员"
SENTENCE = "本次门店于活动期间开档，临时聘请促销员对我司产品进行推广。"


def gate(quote, stance, flag, state="missing", facts=None):
    value = selected_staff(state, flag)
    staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
    staff["condition_evidence"] = [{"unit_id": "u", "quote": quote, "stance": stance}]
    documents = [{"unit_id": "u", "facts": facts if facts is not None else [quote]}]
    return value, documents


class TemporaryStaffHiringPhraseTests(unittest.TestCase):
    def test_explicit_action_and_word_spacing_are_affirmative(self):
        for text in (PHRASE, SENTENCE, "临时 聘请 促销员", "临时\n聘请\t促销员"):
            with self.subTest(text=text):
                self.assertEqual(_staff_stance(text), "affirmed")
                for state in ("missing", "present"):
                    value, documents = gate(text, "affirmed", True, state)
                    before = deepcopy((value, documents))
                    result = classification_decision(value, documents, "personnel_incentive")
                    self.assertEqual(result["matched"], state == "present")
                    self.assertEqual((value, documents), before)
                    if state == "missing":
                        self.assertTrue(any("临促资料或照片" in reason for reason in result["reasons"]))

    def test_negation_never_uses_the_positive_substring_as_support(self):
        for text in ("本次未临时聘请促销员", "没有临时聘请促销员", "不需要临时聘请促销员",
                     "无需临时聘请促销员", "不是临时聘请促销员", "未曾临时聘请促销员",
                     "本次不再临时聘请促销员", "不会临时聘请促销员", "临时聘请促销员：否"):
            with self.subTest(text=text):
                self.assertEqual(_staff_stance(text), "denied")
                value, documents = gate(text, "denied", False)
                self.assertTrue(classification_decision(value, documents, "personnel_incentive")["matched"])
                value, documents = gate(text, "affirmed", True)
                with self.assertRaisesRegex(AuditError, "肯否判断须与完整原文一致"):
                    classification_decision(value, documents, "personnel_incentive")

    def test_zero_people_and_zero_cost_are_not_conflated(self):
        for text in ("临时聘请促销员人数：0", "临时聘请促销员人数为0人", "临时聘请促销员0人"):
            with self.subTest(text=text):
                self.assertEqual(_staff_stance(text), "denied")
                value, documents = gate(text, "denied", False)
                self.assertTrue(classification_decision(value, documents, "personnel_incentive")["matched"])
        for text in ("临时聘请促销员费用0元", "临时聘请促销员人数：0.5", "临时聘请促销员人数：10"):
            with self.subTest(text=text):
                self.assertEqual(_staff_stance(text), "affirmed")

    def test_questions_future_plans_blank_templates_and_uncertainty_remain_unknown(self):
        for text in ("是否临时聘请促销员", "本次是否临时聘请促销员？", "是否临时聘请促销员：□是□否",
                     "是否需要临时聘请促销员？", "是否安排临时聘请促销员？", "本次临时聘请促销员？",
                     "本次临时聘请促销员：",
                     "临时聘请促销员____", "临时聘请促销员：", "拟临时聘请促销员",
                     "计划临时聘请促销员", "可能临时聘请促销员", "如果临时聘请促销员",
                     "临时聘请促销员安排待确认", "临时聘请促销员无法确认"):
            with self.subTest(text=text):
                self.assertEqual(_staff_stance(text), "unclear")
                value, documents = gate(text, "unclear", None)
                self.assertTrue(classification_decision(value, documents, "personnel_incentive")["matched"])
                value, documents = gate(text, "affirmed", True)
                with self.assertRaisesRegex(AuditError, "肯否判断须与完整原文一致"):
                    classification_decision(value, documents, "personnel_incentive")

    def test_ordinary_promoters_and_unapproved_hiring_synonyms_are_not_inferred(self):
        for text in ("促销员", "聘请促销员", "安排促销员推广产品", "临时聘用促销员",
                     "临时雇佣促销员", "促销员工资", "本次促销活动"):
            with self.subTest(text=text):
                self.assertEqual(_staff_stance(text), "unclear")
                value = selected_staff("missing", None)
                self.assertTrue(classification_decision(value, [{"unit_id": "u", "facts": [text]}],
                                                       "personnel_incentive")["matched"])

    def test_false_flag_and_omitted_condition_cannot_hide_the_explicit_action(self):
        for flag in (False, None):
            for evidence in (None, [], [{"unit_id": "u", "quote": "资料备注", "stance": "unclear"}]):
                value = selected_staff("missing", flag)
                staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
                if evidence is not None:
                    staff["condition_evidence"] = evidence
                with self.subTest(flag=flag, evidence=evidence), self.assertRaisesRegex(AuditError, "不能省略肯定原文证据"):
                    classification_decision(value, [{"unit_id": "u", "facts": [SENTENCE, "资料备注"]}], "personnel_incentive")

    def test_quote_still_must_be_verbatim_and_from_contract_or_settlement(self):
        value, documents = gate("临时 聘请促销员", "affirmed", True, facts=[SENTENCE])
        with self.assertRaisesRegex(AuditError, "原文必须逐字来自"):
            classification_decision(value, documents, "personnel_incentive")
        value, documents = gate(PHRASE, "affirmed", True, facts=["没有" + PHRASE])
        with self.assertRaisesRegex(AuditError, "肯否判断须与完整原文一致"):
            classification_decision(value, documents, "personnel_incentive")
        value, documents = gate(SENTENCE, "affirmed", True)
        value["materials"][3]["condition_evidence"][0]["unit_id"] = "payment"
        next(m for m in value["materials"] if m["id"] == "payment")["source_ids"] = ["payment"]
        value["source_ids"].append("payment")
        value["material_matches"][0]["source_ids"].append("payment")
        documents.append({"unit_id": "payment", "facts": [SENTENCE]})
        with self.assertRaisesRegex(AuditError, "不能引用付款或照片资料"):
            classification_decision(value, documents, "personnel_incentive")

    def test_conflicting_sources_do_not_become_a_confirmed_staff_requirement(self):
        value, documents = gate(SENTENCE, "affirmed", None)
        next(m for m in value["materials"] if m["id"] == "settlement")["source_ids"] = ["settlement"]
        value["source_ids"].append("settlement")
        value["material_matches"][0]["source_ids"].append("settlement")
        documents.append({"unit_id": "settlement", "facts": ["本次没有临时聘请促销员"]})
        result = classification_decision(value, documents, "personnel_incentive")
        self.assertTrue(result["matched"])
        value["flags"]["temporary_staff"] = True
        with self.assertRaisesRegex(AuditError, "冲突证据一致"):
            classification_decision(value, documents, "personnel_incentive")
        self.assertEqual(_staff_stance(SENTENCE + "但本次无临促。"), "unclear")

    def test_filename_does_not_supply_a_temporary_hiring_condition(self):
        value = selected_staff("missing", None)
        documents = [{"unit_id": "u", "source_file": PHRASE + ".pdf", "facts": ["本次按销售数量计算奖励"]}]
        self.assertTrue(classification_decision(value, documents, "personnel_incentive")["matched"])

    def test_retry_context_includes_complete_phrase_without_changing_its_limits(self):
        value, documents = gate(SENTENCE, "denied", False,
            facts=["抬头", "标题", "普通表格项目", SENTENCE, "其他备注"])
        with self.assertRaises(ClassificationEvidenceError) as caught:
            classification_decision(value, documents, "personnel_incentive")
        context = caught.exception.retry_context
        facts = [fact for source in context["sources"] for fact in source["facts"]]
        self.assertIn(SENTENCE, facts)
        self.assertLessEqual(len(facts), 3)
        self.assertLessEqual(len(json.dumps(context, ensure_ascii=False).encode("utf-8")), 8192)
        self.assertTrue(all(fact in documents[0]["facts"] for fact in facts))


if __name__ == "__main__":
    unittest.main()
