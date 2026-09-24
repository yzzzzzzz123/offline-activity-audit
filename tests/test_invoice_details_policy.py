"""KT receipt details can be supported by the submitted contract and settlement."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import INVOICE_DETAILS_CLARIFICATION, requirements, rules
from tests.pdf_test_support import flags_for, unknown_checks


def invoice_fixture():
    flags = flags_for("poster_material")
    documents = [
        {"unit_id": "receipt", "facts": ["收据抬头为本次经销商，日期2025年12月10日，金额8024元，事由物料制作"]},
        {"unit_id": "contract", "facts": ["本次合同约定制作台上架90个、灯箱1个"]},
        {"unit_id": "settlement", "facts": ["本次结算明细为台上架90个、灯箱1个，使用指定模板且客户印章可见"]},
        {"unit_id": "photos", "facts": ["本次已提交制作完成的台上架和灯箱照片"]},
    ]
    sources = {"invoice": "receipt", "promotion_contract": "contract",
               "settlement": "settlement", "finished_photos": "photos"}
    materials = [{"id": item.id, "state": "present" if item.id in sources else "not_applicable",
                  "source_ids": [sources[item.id]] if item.id in sources else [],
                  "reason": "已提供本次资料" if item.id in sources else "本次不涉及POS"}
                 for item in requirements("poster_material")]
    evidence = unknown_checks("poster_material", flags)
    observations = {
        "invoice_details": (["receipt", "contract", "settlement"],
                            "收据抬头、金额和日期可见，具体制作明细结合本次合同、结算单确认齐全"),
        "finished_photo": (["photos"], "已提供物料成品照片"),
        "settlement_template_seal": (["settlement"], "结算单使用指定模板且客户印章可见"),
    }
    for check in evidence["checks"]:
        if check["rule_id"] in observations:
            ids, reason = observations[check["rule_id"]]
            check.update(status="pass", source_ids=ids, reason=reason)
    return flags, documents, materials, evidence


def invoice_check(evidence):
    return next(check for check in evidence["checks"] if check["rule_id"] == "invoice_details")


class InvoiceDetailsPolicyTests(unittest.TestCase):
    def test_combined_details_pass_and_preserve_each_document_source(self):
        flags, documents, materials, evidence = invoice_fixture()
        original = deepcopy(documents)
        result = audit_decision("poster_material", flags, evidence, documents, materials)
        self.assertEqual(result["summary"]["conclusion"], "no_issues_found")
        self.assertEqual(invoice_check(result)["source_ids"], ["receipt", "contract", "settlement"])
        self.assertEqual(invoice_check(result)["calculations"], [])
        self.assertEqual(documents, original)

    def test_original_receipt_with_its_own_details_needs_no_extra_detail_source(self):
        flags, documents, materials, evidence = invoice_fixture()
        documents[0]["facts"].append("收据明细：台上架90个、灯箱1个")
        invoice_check(evidence).update(source_ids=["receipt"], reason="收据抬头、制作明细、金额和日期齐全")
        result = audit_decision("poster_material", flags, evidence, documents, materials)
        self.assertEqual(invoice_check(result)["status"], "pass")

    def test_contract_and_settlement_cannot_replace_receipt_evidence(self):
        flags, documents, materials, evidence = invoice_fixture()
        invoice_check(evidence)["source_ids"] = ["contract", "settlement"]
        with self.assertRaisesRegex(AuditError, "须引用本次发票或收据"):
            audit_decision("poster_material", flags, evidence, documents, materials)

    def test_supplementary_details_do_not_clear_missing_or_unclear_receipt(self):
        for state, expected in (("missing", "fail"), ("unclear", "unknown")):
            with self.subTest(state=state):
                flags, documents, materials, evidence = invoice_fixture()
                material = next(item for item in materials if item["id"] == "invoice")
                material.update(state=state, source_ids=[] if state == "missing" else ["receipt"],
                                reason="未找到票据" if state == "missing" else "该图片无法确认是否为本次票据")
                invoice_check(evidence)["source_ids"] = ["contract", "settlement"]
                result = audit_decision("poster_material", flags, evidence, documents, materials)
                self.assertEqual(invoice_check(result)["status"], expected)

    def test_template_reference_cannot_supply_missing_case_details(self):
        flags, documents, materials, evidence = invoice_fixture()
        invoice_check(evidence)["source_ids"] = ["receipt", "template-reference-contract"]
        with self.assertRaises(AuditError):
            audit_decision("poster_material", flags, evidence, documents, materials)

    def test_missing_receipt_date_remains_an_issue_after_details_are_supplied(self):
        flags, documents, materials, evidence = invoice_fixture()
        documents[0]["facts"] = ["收据抬头为本次经销商，金额8024元，事由物料制作，日期未填"]
        invoice_check(evidence).update(status="fail", reason="收据没有填写开具日期。")
        result = audit_decision("poster_material", flags, evidence, documents, materials)
        self.assertEqual(invoice_check(result)["status"], "fail")
        self.assertEqual(result["noncompliance_registration"], ["收据没有填写开具日期。"])

    def test_actual_audit_prompt_and_validation_accept_combined_details(self):
        flags, documents, materials, evidence = invoice_fixture()
        provider = PdfEvidenceProvider("gpt-6-astra", "medium")
        captured = []

        def fake_call(root, skill, schema, prompt, **kwargs):
            captured.append(render_prompt(prompt))
            kwargs["validator"](evidence)
            return evidence

        case = {"scenario": "poster_material", "archive_id": "a001", "flags": flags,
                "documents": documents, "materials": materials}
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", side_effect=fake_call):
            actual = provider.audit(case, Path(temporary))
        self.assertEqual(invoice_check(actual)["status"], "pass")
        self.assertEqual(len(captured), 1)
        self.assertIn(INVOICE_DETAILS_CLARIFICATION, captured[0])
        self.assertFalse(any(rule.id == "invoice_details" for rule in rules("self_procured_gift_material")))


if __name__ == "__main__":
    unittest.main()
