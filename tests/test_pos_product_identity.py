"""Common POS field identity checks use real query results, never OSS photos."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_materials import PdfEvidenceProvider, _cell
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.pdf_policy import MATERIALS, applicability, rules
from audit_core.pos_product_identity import (
    prepare_pos_identities, project_pos_identity_reporting, validate_extraction, validate_pos_identity_reporting, verify_identity,
)
from audit_core.product_database import catalog_from_rows
from tests.pdf_test_support import flags_for, unknown_checks


MODULE = "audit_core.pos_product_identity"


def catalog():
    return catalog_from_rows([
        {"product_code": "CP-A-001", "product_name": "参半清新口腔喷雾沁润蜜桃20ml胶盒装",
         "barcode_69": "6970356168997"},
        {"product_code": "CP-A-002", "product_name": "小箭头柔顺洗发水500ml",
         "barcode_69": "6970356167341"},
    ])


def field(value, index=1):
    return {"fact_index": index, "quote": value, "value": value} if value is not None else None


def row(code="CP-A-001", name="参半清新口腔喷雾沁润蜜桃20ml胶盒装", barcode="6970356168997", kind=None):
    return {"fact_indexes": [1], "product_code": field(code), "product_name": field(name),
            "barcode_69": field(barcode), "code_kind": kind or ("brand" if code else "absent")}


def document(uid="pos", data=None):
    data = data or row()
    return {"unit_id": uid, "facts": ["产品编码 | 商品名称 | 商品条码",
            " | ".join(data[k]["value"] for k in ("product_code", "product_name", "barcode_69") if data[k])],
            "limitations": []}


def extraction(data=None):
    data = deepcopy(data or row())
    return {"rows": [{k: data[k] for k in ("fact_indexes", "product_name", "barcode_69")}], "other_facts": [
        {"fact_index": 0, "kind": "header", "reason": "本页表头"}]}


def case_for(scenario="pos_target_incentive", data=None):
    return {"archive_id": "pos-case", "scenario": scenario, "flags": flags_for(scenario, uses_pos=True),
            "materials": [{"id": "pos", "state": "present", "source_ids": ["pos"]},
                          {"id": "pos_excel", "state": "present", "source_ids": ["excel"]}],
            "documents": [document("pos", data), document("excel", data)], "units": []}


class PosIdentityTests(unittest.TestCase):
    def test_grouped_missing_barcodes_keep_every_verified_name_without_mutating_model_observation(self):
        value = unknown_checks("personnel_incentive", flags_for("personnel_incentive", uses_pos=True))
        check = next(c for c in value["checks"] if c["rule_id"] == "pos_product_identity")
        check.update(status="unknown", reason="奖励明细中的10个商品未填写69码。", source_ids=[])
        verified = {"applicable": True, "limitations": [], "rows": [
            {"unit_id": "excel", "status": "unknown", "reason_code": "missing_barcode",
             "submitted": {"product_name": f"合成商品{number}", "barcode_69": None},
             "reason": "Excel销售明细缺少可核对的69码。"} for number in range(10)]}
        original = deepcopy(value)
        projected = project_pos_identity_reporting(value, verified)
        validate_pos_identity_reporting(projected, verified)
        self.assertEqual(value, original)
        actual = next(c for c in projected["checks"] if c["rule_id"] == "pos_product_identity")
        self.assertEqual(actual["status"], "unknown")
        self.assertEqual(actual["comparisons"], [])
        self.assertEqual(actual["source_ids"], ["excel"])
        self.assertTrue(all(f"合成商品{number}" in actual["reason"] for number in range(10)))
        self.assertIs(project_pos_identity_reporting(projected, verified), projected)

    def test_projection_never_turns_wrong_pass_or_unknown_status_into_a_valid_judgment(self):
        for row_status, check_status in (("unknown", "pass"), ("fail", "pass"), ("fail", "unknown")):
            value = unknown_checks("personnel_incentive", flags_for("personnel_incentive", uses_pos=True))
            check = next(c for c in value["checks"] if c["rule_id"] == "pos_product_identity")
            check.update(status=check_status, source_ids=[], reason="合并的商品说明。")
            verified = {"applicable": True, "limitations": [], "rows": [{
                "unit_id": "excel", "status": row_status, "reason_code": "product_name_mismatch",
                "submitted": {"product_name": "合成商品", "barcode_69": "6900000000000"},
                "reason": "商品名称与所填69码对应的库内商品不一致。"}]}
            projected = project_pos_identity_reporting(value, verified)
            self.assertEqual(next(c for c in projected["checks"] if c["rule_id"] == "pos_product_identity")["status"], check_status)
            with self.assertRaises(AuditError):
                validate_pos_identity_reporting(projected, verified)

    def test_projection_deduplicates_identical_descriptions_but_keeps_all_source_units(self):
        value = unknown_checks("personnel_incentive", flags_for("personnel_incentive", uses_pos=True))
        check = next(c for c in value["checks"] if c["rule_id"] == "pos_product_identity")
        check.update(status="unknown", source_ids=[], reason="库内未找到对应商品。")
        row = {"status": "unknown", "reason_code": "catalog_product_missing",
               "submitted": {"product_name": "合成外部商品", "barcode_69": "6900000000000"},
               "reason": "库内无参考商品。"}
        verified = {"applicable": True, "limitations": [],
                    "rows": [{"unit_id": unit, **deepcopy(row)} for unit in ["excel-1", "excel-2"]]}
        projected = project_pos_identity_reporting(value, verified)
        validate_pos_identity_reporting(projected, verified)
        actual = next(c for c in projected["checks"] if c["rule_id"] == "pos_product_identity")
        self.assertEqual(actual["source_ids"], ["excel-1", "excel-2"])
        self.assertEqual(actual["reason"].count("合成外部商品"), 1)
        self.assertIn("库内无参考商品", actual["reason"])

    def test_numeric_excel_barcode_does_not_gain_a_suffix_but_literal_text_is_preserved(self):
        self.assertEqual(_cell(6970356168997.0), "6970356168997")
        self.assertEqual(_cell("6970356168997.0"), "6970356168997.0")
        self.assertEqual(_cell("06970356168997"), "06970356168997")
        self.assertEqual(_cell(1.005), "1.005")

    def test_exact_identity_and_unique_abbreviation_are_supported(self):
        match = Mock(return_value=["CP-A-001"])
        exact = verify_identity(row(), catalog(), match)
        self.assertEqual(exact["status"], "pass")
        match.assert_not_called()
        for code in (None, "CP-A-001"):
            result = verify_identity(row(code=code, name="参半蜜桃喷雾20ml"), catalog(), match)
            self.assertEqual(result["status"], "pass")
            self.assertEqual(result["reference"]["product_code"], "CP-A-001")
        match.assert_not_called()

    def test_dealer_name_difference_does_not_override_exact_barcode(self):
        matcher = Mock(side_effect=AssertionError('POS身份不再调用名称语义模型'))
        result = verify_identity(row(name="小箭头洗发水500ml"), catalog(), matcher)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reference"]["product_code"], "CP-A-001")
        matcher.assert_not_called()

    def test_extra_product_code_cannot_override_name_and_barcode_selection(self):
        result = verify_identity(row(code="CP-A-001", name="小箭头柔顺洗发水500ml", barcode="6970356167341"), catalog(), Mock())
        self.assertEqual(result["status"], "pass")
        self.assertEqual(result["reference"]["product_code"], "CP-A-002")

    def test_extra_codes_missing_conflicting_or_unattributed_do_not_affect_result(self):
        expected = verify_identity(row(code=None), catalog(), Mock())
        for code in ("CP-A-001", "CP-A-002", "CP-MISSING", "cp-a-001", "4362235", None):
            for kind in ("brand", "retailer", "unclear", "absent"):
                with self.subTest(code=code, kind=kind):
                    result = verify_identity(row(code=code, kind=kind), catalog(), Mock())
                    self.assertEqual(result, expected)
                    self.assertNotIn("product_code", result["submitted"])

    def test_barcode_must_still_match_exactly(self):
        for barcode in ("6970356168997.0", "06970356168997", "697035616899"):
            with self.subTest(barcode=barcode):
                result = verify_identity(row(barcode=barcode), catalog(), Mock())
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason_code"], "catalog_product_missing")

    def test_missing_catalog_is_not_misreported_as_a_bad_customer_product(self):
        result = verify_identity(row(code="CP-NONE", name="库中没有的商品", barcode="6900000000000"), catalog(), Mock(return_value=[]))
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "库内无参考商品。")

    def test_name_cannot_replace_an_unmatched_original_barcode(self):
        result = verify_identity(row(code=None, barcode="6900000000000"), catalog(), Mock())
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason_code"], "catalog_product_missing")
        self.assertIn("库内无参考商品", result["reason"])
        self.assertEqual(verify_identity(row(kind="unclear"), catalog(), Mock())["status"], "pass")

    def test_same_barcode_keeps_all_internal_records_and_never_picks_a_code(self):
        data = catalog()
        data["products"][1]["barcode_69"] = data["products"][0]["barcode_69"]
        matcher = Mock(side_effect=AssertionError('不按名称挑选内部编码'))
        result = verify_identity(row(code=None, name="日化产品"), data, matcher)
        self.assertEqual(result["status"], "pass")
        self.assertEqual(len(result["references"]), 2)
        self.assertNotIn("reference", result)
        self.assertEqual(verify_identity(row(code="CP-A-001", name="日化产品"), data, matcher), result)
        matcher.assert_not_called()

    def test_required_name_or_barcode_is_not_filled_from_catalog_or_extra_code(self):
        result = verify_identity(row(name=None), catalog(), Mock())
        self.assertEqual(result["reason_code"], "product_name_missing")
        self.assertIsNone(result["submitted"]["product_name"])
        missing_barcode = verify_identity(row(barcode=None), catalog(), Mock())
        self.assertEqual(missing_barcode["reason_code"], "barcode_missing")
        self.assertIsNone(missing_barcode["submitted"]["barcode_69"])

    def test_grounding_covers_every_fact_and_rejects_fabricated_or_truncated_codes(self):
        doc = document()
        validate_extraction(extraction(), doc)
        missing = deepcopy(doc)
        missing["facts"].append("CP-A-002 | 小箭头洗发水500ml | 6970356167341")
        with self.assertRaises(AuditError):
            validate_extraction(extraction(), missing)
        for barcode in ("697035616899", "6970356167341"):
            with self.subTest(barcode=barcode), self.assertRaises(AuditError):
                validate_extraction(extraction(row(barcode=barcode)), doc)
        duplicated = extraction()
        duplicated["other_facts"].append({"fact_index": 1, "kind": "note", "reason": "试图隐藏商品"})
        with self.assertRaises(AuditError):
            validate_extraction(duplicated, doc)
        decimal_text = document(data=row(barcode="6970356168997.0"))
        with self.assertRaises(AuditError):
            validate_extraction(extraction(), decimal_text)

    def test_every_pos_type_queries_only_excel_and_deduplicates_repeated_identities(self):
        for scenario in MATERIALS:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                call = Mock(return_value=extraction())
                case = case_for(scenario)
                case["documents"].append(document("excel-2"))
                case["materials"][1]["source_ids"].append("excel-2")
                with patch(MODULE + ".load_product_catalog", return_value=catalog()) as db, \
                     patch(MODULE + ".verify_identity", wraps=verify_identity) as verify, \
                     patch("audit_core.product_images.attach_product_reference_images") as oss, \
                     patch("audit_core.product_image_runtime.copy_oss_image") as image:
                    result = prepare_pos_identities(case, Path(temporary), call)
                self.assertEqual(call.call_count, 2)
                self.assertEqual(db.call_count, 1)
                self.assertEqual(verify.call_count, 1)
                self.assertEqual([r["unit_id"] for r in result["rows"]], ["excel", "excel-2"])
                self.assertTrue(all(r["status"] == "pass" for r in result["rows"]))
                oss.assert_not_called()
                image.assert_not_called()
                self.assertTrue(all(c.kwargs["images"] == [] for c in call.call_args_list))

    def test_absent_pos_does_not_query_database_or_start_extraction(self):
        case = case_for("poster_material")
        case["flags"]["uses_pos"] = False
        with patch(MODULE + ".load_product_catalog") as db:
            call = Mock()
            result = prepare_pos_identities(case, Path("unused"), call)
        self.assertFalse(result["applicable"])
        db.assert_not_called()
        call.assert_not_called()

    def test_database_error_propagates_instead_of_guessing_a_result(self):
        with tempfile.TemporaryDirectory() as temporary, \
             patch(MODULE + ".load_product_catalog", side_effect=AuditError("数据库无法连接")), \
             self.assertRaisesRegex(AuditError, "数据库无法连接"):
            prepare_pos_identities(case_for(), Path(temporary), Mock(return_value=extraction()))

    def test_model_cannot_pass_or_omit_conflicting_and_missing_products(self):
        case = case_for()
        unknown = verify_identity(row(code="CP-NONE", name="库中没有的商品", barcode="6900000000000"), catalog(), Mock(return_value=[]))
        verified = {"applicable": True, "rows": [{"unit_id": "excel", **unknown}], "limitations": []}
        value = unknown_checks(case["scenario"], case["flags"])
        check = next(c for c in value["checks"] if c["rule_id"] == "pos_product_identity")
        check.update(status="pass", source_ids=["excel"], reason="库中没有的商品：库内无参考商品。")
        with self.assertRaises(AuditError):
            validate_pos_identity_reporting(value, verified)
        check["status"] = "unknown"
        validate_pos_identity_reporting(value, verified)
        for reason in ("库内无参考商品。", "库中没有的商品：无法核对。"):
            check["reason"] = reason
            with self.assertRaises(AuditError):
                validate_pos_identity_reporting(value, verified)
        conflict = verify_identity(row(barcode="6900000000000"), catalog(), Mock())
        verified["rows"] = [{"unit_id": "excel", **conflict}]
        check.update(status="pass", reason="6900000000000条码不一致。")
        with self.assertRaises(AuditError):
            validate_pos_identity_reporting(value, verified)
        check.update(status="unknown", reason="6900000000000：库内无参考商品。")
        validate_pos_identity_reporting(value, verified)

    def test_unreadable_pos_cannot_silently_pass_identity_check(self):
        case = case_for()
        case["documents"][1]["facts"] = []
        with tempfile.TemporaryDirectory() as temporary, patch(MODULE + ".load_product_catalog", return_value=catalog()):
            verified = prepare_pos_identities(case, Path(temporary), Mock(return_value=extraction()))
        self.assertTrue(verified["limitations"])
        value = unknown_checks(case["scenario"], case["flags"])
        check = next(c for c in value["checks"] if c["rule_id"] == "pos_product_identity")
        check.update(status="pass", source_ids=["excel"], reason="全部一致")
        with self.assertRaises(AuditError):
            validate_pos_identity_reporting(value, verified)

    def test_extra_column_reading_limitation_does_not_become_a_product_problem(self):
        case = case_for()
        case["documents"][1]["limitations"] = ["额外产品编码模糊，其他字段清晰。"]
        with tempfile.TemporaryDirectory() as temporary, patch(MODULE + ".load_product_catalog", return_value=catalog()):
            verified = prepare_pos_identities(case, Path(temporary), Mock(return_value=extraction()))
        self.assertEqual(verified["limitations"], [])
        value = unknown_checks(case["scenario"], case["flags"])
        check = next(c for c in value["checks"] if c["rule_id"] == "pos_product_identity")
        check.update(status="pass", source_ids=["excel"], reason="商品名称和69码唯一对应")
        validate_pos_identity_reporting(value, verified)
        check.update(status="fail", reason="产品编码不一致")
        with self.assertRaises(AuditError):
            validate_pos_identity_reporting(value, verified)

    def test_common_rule_ids_and_conditions_cover_all_eight_types(self):
        for scenario in MATERIALS:
            common = [r for r in rules(scenario) if r.id in {"pos_fields", "pos_arithmetic", "pos_product_identity"}]
            self.assertEqual({r.id for r in common}, {"pos_fields", "pos_arithmetic", "pos_product_identity"})
            for uses_pos in (False, True):
                self.assertTrue(all(applicability(r.when, scenario, flags_for(scenario, uses_pos=uses_pos)) is uses_pos for r in common))

    def test_provider_passes_verified_identity_to_audit_and_enforces_it(self):
        data = row(barcode="6900000000000")
        case = case_for(data=data)
        evidence = unknown_checks(case["scenario"], case["flags"])
        check = next(c for c in evidence["checks"] if c["rule_id"] == "pos_product_identity")
        check.update(status="unknown", reason="69码6900000000000：库内无参考商品。", source_ids=["excel"])
        seen = []

        def call(root, skill, schema, prompt, **kwargs):
            if root.name.startswith("pdf-pos-identities-"):
                value = extraction(data)
            else:
                text = render_prompt(prompt)
                seen.append(text)
                self.assertIn('"reason_code": "catalog_product_missing"', text)
                self.assertIn("准确69码", text)
                value = evidence
            kwargs["validator"](value)
            return value

        provider = PdfEvidenceProvider("gpt-6-astra", "medium", stage_summary_reviewer=isolated_stage_summary)
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", side_effect=call), \
             patch(MODULE + ".load_product_catalog", return_value=catalog()):
            result = provider.audit(case, Path(temporary))
        self.assertIs(result, evidence)
        self.assertEqual(len(seen), 1)


if __name__ == "__main__":
    unittest.main()
