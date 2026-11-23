"""Strict source validation and bounded retry context, without a remote model."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.pdf_evidence import OperandEvidenceError, audit_decision, audit_schema
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_payment_outflow_evidence import outgoing_case


class OperandRetryContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.flags = flags_for("personnel_incentive")
        self.materials = [
            {"id": "activity_application", "state": "present", "source_ids": ["contract"], "reason": "fixture"},
            {"id": "settlement", "state": "present", "source_ids": ["claim"], "reason": "fixture"},
        ]
        self.documents = [
            {"unit_id": "contract", "facts": ["合同奖励单价=2元", "合同数量=20"]},
            {"unit_id": "claim", "facts": ["结算奖励单价=2元", "本次数量=20"]},
            {"unit_id": "other", "facts": ["UNRELATED_PRIVATE_SOURCE=2"]},
        ]
        self.value = unknown_checks("personnel_incentive", self.flags)
        self.index = next(i for i, check in enumerate(self.value["checks"])
                          if check["rule_id"] == "reward_unit_price")
        check = self.value["checks"][self.index]
        self.comparison = {"label": "fixture", "value_kind": "unit_price", "operation": "value", "operator": "eq",
                           "left": {"unit_id": "claim", "quote": "结算奖励单价=2元", "number": "2"},
                           "right": [{"unit_id": "contract", "quote": "合同奖励单价=2元", "number": "2"}]}
        check.update(status="pass", source_ids=["claim", "contract"], comparisons=[deepcopy(self.comparison)])
        path = self.root / "schema.json"
        path.write_text(json.dumps(audit_schema("personnel_incentive", materials=self.materials)), encoding="utf-8")
        self.parser = EvidenceOutputParser(schema_path=path, post_validate=lambda value:
            audit_decision("personnel_incentive", self.flags, value, self.documents, self.materials))

    def reject(self, value):
        before = deepcopy((value, self.documents))
        with self.assertRaises(AuditError) as caught:
            self.parser.parse(json.dumps(value))
        self.assertEqual((value, self.documents), before)
        self.assertNotIn("UNRELATED_PRIVATE_SOURCE", self.parser.last_retry_context)
        return caught.exception

    def test_each_numeric_side_reports_exact_check_comparison_and_operand_path(self):
        for side in ("left", "right"):
            with self.subTest(side=side):
                value = deepcopy(self.value)
                comparisons = value["checks"][self.index]["comparisons"]
                comparisons.append(deepcopy(self.comparison))
                operand = comparisons[1]["left"] if side == "left" else comparisons[1]["right"][0]
                operand["quote"] = operand["quote"].replace("=", " = ")
                error = self.reject(value)
                path = f"checks/{self.index}/comparisons/1/{side}" + ("/0" if side == "right" else "") + "/quote"
                self.assertEqual(str(error), "数值引用必须逐字来自本次已提取材料，不能补造申请值或计算依据")
                self.assertEqual(self.parser.last_diagnostics, {"validation_stage": "evidence",
                    "validation_code": "quote_not_in_source", "validation_paths": [path]})
                context = json.loads(self.parser.last_retry_context)
                self.assertEqual(context["path"], path)
                self.assertEqual(context["quote"], operand["quote"])
                source = next(d for d in self.documents if d["unit_id"] == operand["unit_id"])
                self.assertTrue(all(fact in source["facts"] for fact in context["facts"]))

    def test_number_mismatch_keeps_its_distinct_code_and_number_path(self):
        self.value["checks"][self.index]["comparisons"][0]["right"][0]["number"] = "3"
        error = self.reject(self.value)
        self.assertEqual(str(error), "数值与引用的原文不一致")
        self.assertEqual(self.parser.last_diagnostics["validation_code"], "number_quote_mismatch")
        self.assertEqual(self.parser.last_diagnostics["validation_paths"],
                         [f"checks/{self.index}/comparisons/0/right/0/number"])

    def test_later_payment_term_is_located_without_changing_signed_payment_rules(self):
        case, value, check = outgoing_case()
        check_index = value["checks"].index(check)
        check["comparisons"][0]["right"][3]["quote"] = "not a source -935.00"
        path = self.root / "payment-schema.json"
        path.write_text(json.dumps(audit_schema(case["scenario"], materials=case["materials"])), encoding="utf-8")
        parser = EvidenceOutputParser(schema_path=path, post_validate=lambda result:
            audit_decision(case["scenario"], case["flags"], result, case["documents"], case["materials"]))
        with self.assertRaises(AuditError):
            parser.parse(json.dumps(value))
        self.assertEqual(parser.last_diagnostics["validation_paths"],
                         [f"checks/{check_index}/comparisons/0/right/3/quote"])
        context = json.loads(parser.last_retry_context)
        self.assertEqual(context["unit_id"], "right4")
        self.assertEqual(context["number"], "935.00")
        self.assertIn("-935.00", context["facts"][0])
        check["comparisons"][0]["right"][3]["quote"] = "-935.00"
        self.assertEqual(parser.parse(json.dumps(value)), value)
        self.assertEqual(parser.last_retry_context, "")

    def test_changed_whitespace_joined_facts_and_fabricated_values_remain_rejected(self):
        for quote in ("合同奖励单价 =2元", "合同奖励单价=2元；合同数量=20", "合同奖励单价=200元"):
            with self.subTest(quote=quote):
                value = deepcopy(self.value)
                value["checks"][self.index]["comparisons"][0]["right"][0]["quote"] = quote
                self.reject(value)
                self.assertEqual(self.parser.last_diagnostics["validation_code"], "quote_not_in_source")
        self.parser.parse(json.dumps(self.value))
        self.assertEqual(self.parser.last_diagnostics, {})
        self.assertEqual(self.parser.last_retry_context, "")

    def test_context_is_small_and_preserves_only_complete_original_facts(self):
        self.documents[0]["facts"] = ["2" + "密" * 9000] + ["2" + "\x01" * 1800] + [f"原文{i}金额=2" for i in range(100)]
        self.reject(self.value)
        context = json.loads(self.parser.last_retry_context)
        self.assertLessEqual(len(self.parser.last_retry_context.encode("utf-8")), 8192)
        self.assertLessEqual(len(context["facts"]), 3)
        self.assertTrue(context["facts_omitted"])
        self.assertTrue(all(fact in self.documents[0]["facts"] for fact in context["facts"]))
        self.assertTrue(all(len(fact.encode("utf-8")) <= 2048 for fact in context["facts"]))
        self.assertNotIn("密", self.parser.last_retry_context)
        public = json.dumps(self.parser.last_diagnostics, ensure_ascii=False)
        self.assertNotIn("原文", public)
        self.assertNotIn("合同奖励", public)

    def test_oversized_operand_has_position_but_no_truncated_retry_text(self):
        self.value["checks"][self.index]["comparisons"][0]["right"][0]["quote"] = "原" * 1000
        self.reject(self.value)
        self.assertNotEqual(self.parser.last_diagnostics["validation_paths"], ["$"])
        self.assertEqual(self.parser.last_retry_context, "")

    def test_arbitrary_exception_metadata_and_invalid_paths_are_not_exposed(self):
        for typed, path in ((False, f"checks/{self.index}/comparisons/0/right/0"),
                            (True, "checks/0/../../PRIVATE"), (True, "checks/999/comparisons/0/right/0")):
            with self.subTest(typed=typed, path=path):
                operand = self.comparison["right"][0]
                error = (OperandEvidenceError("数值引用必须逐字来自本次已提取材料，不能补造申请值或计算依据",
                         operand, ["PRIVATE_FACT"], tuple(path.split("/")), "quote") if typed else AuditError("private"))
                if not typed:
                    error.validation_paths = [path + "/quote"]
                    error.retry_context = {"private": "PRIVATE_FACT"}
                def reject(_):
                    raise error
                self.parser.post_validate = reject
                with self.assertRaises(AuditError):
                    self.parser.parse(json.dumps(self.value))
                self.assertEqual(self.parser.last_diagnostics["validation_paths"], ["$"])
                self.assertEqual(self.parser.last_retry_context, "")

    def test_json_failure_clears_previous_private_context(self):
        value = deepcopy(self.value)
        value["checks"][self.index]["comparisons"][0]["left"]["quote"] = "wrong=2"
        self.reject(value)
        self.assertTrue(self.parser.last_retry_context)
        with self.assertRaises(json.JSONDecodeError):
            self.parser.parse("not json")
        self.assertEqual(self.parser.last_retry_context, "")

    def test_table_locator_prefers_its_complete_original_row_over_numeric_substrings(self):
        row = 'Sheet2!R54: C1=甲店（西城店） | C5=67 | C6=24.9 | C7=1668.3'
        self.documents[0]['facts'] = [
            'Sheet2!R10: C5=56 | C7=1674.3999999999999',
            'Sheet3!R54: C5=67', 'Sheet2!R540: C5=67', row,
        ]
        operand = self.value['checks'][self.index]['comparisons'][0]['right'][0]
        operand.update(quote='Sheet2!R54: C1=甲店（西城店 | C5=67', number='67')
        self.reject(self.value)
        context = json.loads(self.parser.last_retry_context)
        self.assertEqual(context['facts'], [row])
        self.assertTrue(context['facts_omitted'])
        self.assertNotIn('1674', self.parser.last_retry_context)

    def test_located_row_is_shown_even_when_it_disproves_the_rejected_number(self):
        row = 'Sheet2!R54: C1=甲店 | C5=68'
        self.documents[0]['facts'] = ['Sheet2!R53: C5=67', row]
        operand = self.value['checks'][self.index]['comparisons'][0]['right'][0]
        operand.update(quote='Sheet2!R54: C1=甲店 | C5 = 67', number='67')
        self.reject(self.value)
        self.assertEqual(json.loads(self.parser.last_retry_context)['facts'], [row])
        operand['quote'] = row
        self.reject(self.value)
        self.assertEqual(self.parser.last_diagnostics['validation_code'], 'number_quote_mismatch')

    def test_without_a_locator_whole_literal_numbers_rank_before_substrings(self):
        self.documents[0]['facts'] = ['金额=1674.40', '条码=6970356166700', '数量=167',
                                      '数量=67.5', '实际数量=67']
        operand = self.value['checks'][self.index]['comparisons'][0]['right'][0]
        operand.update(quote='错误改写的数量 67', number='67')
        self.reject(self.value)
        context = json.loads(self.parser.last_retry_context)
        self.assertEqual(context['facts'][0], '实际数量=67')
        self.assertLessEqual(len(context['facts']), 3)

    def test_oversized_exact_row_does_not_borrow_an_unrelated_row_or_truncate_original(self):
        self.documents[0]['facts'] = ['Sheet2!R54: C1=' + '大' * 9000, 'Sheet2!R55: C5=67']
        operand = self.value['checks'][self.index]['comparisons'][0]['right'][0]
        operand.update(quote='Sheet2!R54: C5=67', number='67')
        self.reject(self.value)
        context = json.loads(self.parser.last_retry_context)
        self.assertEqual(context['facts'], [])
        self.assertTrue(context['facts_omitted'])
        self.assertLessEqual(len(self.parser.last_retry_context.encode('utf-8')), 8192)


if __name__ == "__main__":
    unittest.main()
