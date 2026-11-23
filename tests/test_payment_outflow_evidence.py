"""Outgoing statement signs must not turn paid money into a quote failure."""
from copy import deepcopy
from decimal import Decimal
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import calculate
from tests.test_brand_amount_policy import numeric_case, decide


def outgoing_case(amounts=("1103.00", "973.00", "1240.00", "935.00", "1229.00"), *, signed=False):
    case, evidence, check = numeric_case("payment_claim_amount", "5480", list(amounts))
    for index, (document, operand, amount) in enumerate(zip(
            case["documents"][1:], check["comparisons"][0]["right"], amounts), 1):
        document["facts"] = [f"第{index}条：转账 -{amount}；23:59 转账-转给收款人{index}"]
        operand.update(quote="-" + amount, number=("-" if signed else "") + amount)
    return case, evidence, check


class PaymentOutflowEvidenceTests(unittest.TestCase):
    def test_each_outgoing_debit_counts_as_paid_and_preserves_signed_source(self):
        for signed in (False, True):
            with self.subTest(signed=signed):
                case, evidence, check = outgoing_case(signed=signed)
                before = deepcopy(evidence)
                result = decide(case, evidence, "payment_claim_amount")
                calculation = result["calculations"][0]
                self.assertEqual(result["status"], "pass")
                self.assertEqual(Decimal(calculation["right_value"]), Decimal("5480"))
                self.assertEqual([t["source_number"] for t in calculation["payment_outflows"]],
                                 ["-1103.00", "-973.00", "-1240.00", "-935.00", "-1229.00"])
                self.assertTrue(all("转给" in t["direction_quotes"][0]
                                    for t in calculation["payment_outflows"]))
                self.assertEqual(evidence, before, "原始模型证据不得被改写")

    def test_negative_numbers_are_still_strict_outside_payment_comparison(self):
        case, _, check = outgoing_case()
        documents = {d["unit_id"]: d for d in case["documents"]}
        with self.assertRaisesRegex(AuditError, "数值与引用的原文不一致"):
            calculate(check["comparisons"][0], documents)
        for operand in check["comparisons"][0]["right"]:
            operand["number"] = "-" + operand["number"]
        result = calculate(check["comparisons"][0], documents)
        self.assertEqual(result["right_value"], "-5480.00")
        self.assertNotIn("payment_outflows", result)

    def test_refund_incoming_pending_and_unclear_debits_are_not_converted(self):
        for context in ("退款转给对方", "转入", "收入", "入账", "收款", "付款已取消",
                        "未付款", "待支付", "转出失败", "转给对方未成功", "转账"):
            for signed in (False, True):
                with self.subTest(context=context, signed=signed):
                    case, evidence, check = outgoing_case(("100.00",), signed=signed)
                    case["documents"][1]["facts"] = [f"{context} -100.00"]
                    if signed:
                        result = decide(case, evidence, "payment_claim_amount")
                        self.assertEqual(result["calculations"][0]["right_value"], "-100.00")
                        self.assertNotIn("payment_outflows", result["calculations"][0])
                    else:
                        with self.assertRaisesRegex(AuditError, "数值与引用的原文不一致"):
                            decide(case, evidence, "payment_claim_amount")

    def test_direction_cannot_be_borrowed_from_another_statement_line(self):
        case, evidence, check = outgoing_case(("100.00",))
        case["documents"][1]["facts"] = ["转给甲 -99.00", "转账 -100.00"]
        with self.assertRaises(AuditError):
            decide(case, evidence, "payment_claim_amount")
        case["documents"][1]["facts"] = ["转给甲 -100.00", "退款 -100.00"]
        with self.assertRaises(AuditError):
            decide(case, evidence, "payment_claim_amount")

    def test_forged_quotes_unknown_sources_and_changed_amounts_remain_rejected(self):
        for mutation in ({"quote": "-5480.00"}, {"unit_id": "absent"}, {"number": "1103.01"}):
            with self.subTest(mutation=mutation):
                case, evidence, check = outgoing_case()
                check["comparisons"][0]["right"][0].update(mutation)
                with self.assertRaises(AuditError):
                    decide(case, evidence, "payment_claim_amount")

    def test_signed_outflow_keeps_exact_decimal_and_does_not_round_shortfall(self):
        case, evidence, check = outgoing_case(("99.999",))
        check["comparisons"][0]["left"].update(number="100", quote="本次核销金额=100")
        case["documents"][0]["facts"] = ["本次核销金额=100"]
        result = decide(case, evidence, "payment_claim_amount")
        self.assertEqual(result["status"], "fail")
        self.assertEqual(result["calculations"][0]["right_value"], "99.999")

    def test_source_roles_and_payment_operator_cannot_be_bypassed(self):
        for mutation in ("source", "operator", "operation", "value_kind"):
            with self.subTest(mutation=mutation):
                case, evidence, check = outgoing_case()
                if mutation == "source":
                    case["materials"][1]["id"] = "promotion_contract"
                else:
                    check["comparisons"][0][mutation] = {
                        "operator": "ge", "operation": "product", "value_kind": "quantity"}[mutation]
                with self.assertRaises(AuditError):
                    decide(case, evidence, "payment_claim_amount")


if __name__ == "__main__":
    unittest.main()
