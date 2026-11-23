"""Model-facing shape keeps the POS identity object the database lookup needs.

The 2026-11-11 feedback runs showed ``pos_product_identity`` aborting with
``PDF规则证据结构无效：rows/0/product_name`` in every attempt: the transport
schema had collapsed the identity object to ``{}``, the model answered with a
plain string, the strict validator rejected it, and the product database was
never queried. These tests pin the wire shape and the per-sheet fallback.
"""
from pathlib import Path
import unittest
from unittest.mock import patch

import jsonschema

from audit_core.common import AuditError
from audit_core.model_authority import transport_schema
from audit_core.pos_product_identity import (extraction_schema, prepare_pos_identities,
                                             validate_extraction, OTHER_FACT_KINDS)
from audit_core.product_database import catalog_from_rows
from tests.pdf_test_support import flags_for
from tests.reader_test_support import test_dir


MODULE = "audit_core.pos_product_identity"


def quoted(index, fact, value):
    return {"fact_index": index, "quote": fact, "value": value}


def pos_document(uid, name, barcode):
    header = "Sheet1!R1: 商品名称 | 商品条码"
    row = f"Sheet1!R2: {name} | {barcode}"
    return {"unit_id": uid, "facts": [header, row], "limitations": []}


def pos_extraction(document, name, barcode):
    header, row = document["facts"]
    return {"rows": [{"fact_indexes": [1], "product_name": quoted(1, row, name),
                      "barcode_69": quoted(1, row, barcode)}],
            "other_facts": [{"fact_index": 0, "kind": "header", "reason": "本页表头"}]}


def catalog():
    return catalog_from_rows([{"product_code": "CP-A-001", "product_name": "参半清新口腔喷雾沁润蜜桃20ml胶盒装",
                               "barcode_69": "6970356168997"}])


class TransportShapeTests(unittest.TestCase):
    def test_pos_identity_fields_keep_the_quoted_object_contract(self):
        schema = transport_schema(extraction_schema(2))
        row = schema["properties"]["rows"]["items"]
        for key in ("product_name", "barcode_69"):
            with self.subTest(key=key):
                field = row["properties"][key]
                self.assertIn("anyOf", field)
                branch = next(item for item in field["anyOf"] if item.get("type") == "object")
                self.assertEqual(set(branch["properties"]), {"fact_index", "quote", "value"})
                self.assertEqual(set(branch["required"]), {"fact_index", "quote", "value"})
                self.assertTrue(any(item.get("type") == "null" for item in field["anyOf"]))

    def test_extraction_fields_are_required_on_the_wire(self):
        schema = transport_schema(extraction_schema(2))
        row = schema["properties"]["rows"]["items"]
        for key in ("fact_indexes", "product_name", "barcode_69"):
            self.assertIn(key, row["required"])

    def test_value_enums_survive_transport_projection(self):
        schema = transport_schema(extraction_schema(2))
        kind = schema["properties"]["other_facts"]["items"]["properties"]["kind"]
        self.assertEqual(set(kind["enum"]), set(OTHER_FACT_KINDS))
        self.assertEqual(kind["type"], "string")

    def test_observed_chinese_kinds_are_normalized_before_strict_validation(self):
        # Real runs also classified report metadata as a non-product note.
        document = {"unit_id": "excel", "facts": ["报表参数：门店 | 日期", "商品 | 69码 | 销量"],
                    "limitations": []}
        value = {"rows": [], "other_facts": [
            {"fact_index": 0, "kind": "报表参数", "reason": "报表参数"},
            {"fact_index": 1, "kind": "表头", "reason": "表头"},
        ]}
        validate_extraction(value, document)
        self.assertEqual([item["kind"] for item in value["other_facts"]], ["note", "header"])

    def test_unknown_kind_still_fails_strict_validation(self):
        document = {"unit_id": "excel", "facts": ["无法归类的一行"], "limitations": []}
        value = {"rows": [], "other_facts": [
            {"fact_index": 0, "kind": "商品行", "reason": "未经登记的分类"}]}
        with self.assertRaises(AuditError):
            validate_extraction(value, document)

    def test_plain_string_identity_cannot_satisfy_the_delivered_contract(self):
        schema = transport_schema(extraction_schema(2))
        document = pos_document("excel", "参半清新口腔喷雾沁润蜜桃20ml胶盒装", "6970356168997")
        good = pos_extraction(document, "参半清新口腔喷雾沁润蜜桃20ml胶盒装", "6970356168997")
        validate_extraction(good, document)
        jsonschema.Draft202012Validator(schema).validate(good)
        broken = {"rows": [{"fact_indexes": [1], "product_name": "参半清新口腔喷雾沁润蜜桃20ml胶盒装",
                            "barcode_69": "6970356168997"}],
                  "other_facts": [{"fact_index": 0, "kind": "header", "reason": "本页表头"}]}
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.Draft202012Validator(schema).validate(broken)

    def test_business_answer_keeps_nested_required_advisory(self):
        schema = {"type": "object", "required": ["checks"], "properties": {"checks": {
            "type": "array", "items": {"type": "object",
                "required": ["rule_id", "status", "reason", "comparisons"],
                "properties": {"rule_id": {"type": "string"}, "status": {"type": "string"},
                               "reason": {"type": "string"},
                               "comparisons": {"type": "array", "items": {"type": "object",
                                   "required": ["left", "right"],
                                   "properties": {"left": {"type": "string"},
                                                  "right": {"type": "string"}}}}}}}}}
        projected = transport_schema(schema)
        item = projected["properties"]["checks"]["items"]
        self.assertEqual(set(item["required"]), {"rule_id", "status", "reason"})
        jsonschema.Draft202012Validator(projected).validate({"checks": [
            {"rule_id": "pos_product_identity", "status": "unknown", "reason": "无法确认。",
             "comparisons": [{"left": "1"}]}]})


class PosIdentityPerSheetFallbackTests(unittest.TestCase):
    def test_one_unreadable_page_does_not_discard_the_other_page_lookup(self):
        first = pos_document("excel-1", "参半牙膏", "6900000000000")
        second = pos_document("excel-2", "参半清新口腔喷雾沁润蜜桃20ml胶盒装", "6970356168997")
        case = {"archive_id": "pos-shape-case", "scenario": "pos_target_incentive",
                "flags": flags_for("pos_target_incentive", uses_pos=True),
                "materials": [{"id": "pos_excel", "state": "present",
                               "source_ids": ["excel-1", "excel-2"]}],
                "documents": [first, second], "units": []}
        responses = [AuditError("PDF规则证据结构无效：rows/0/product_name"),
                     pos_extraction(second, "参半清新口腔喷雾沁润蜜桃20ml胶盒装", "6970356168997")]

        def call(root, skill, schema, prompt, *, images, label, validator):
            value = responses.pop(0)
            if isinstance(value, Exception):
                raise value
            validator(value)
            return value

        with test_dir("pos-shape-") as temporary, \
                patch(MODULE + ".load_product_catalog", return_value=catalog()):
            result = prepare_pos_identities(case, Path(temporary), call)
        self.assertEqual([row["unit_id"] for row in result["rows"]], ["excel-2"])
        self.assertEqual(result["rows"][0]["status"], "pass")
        self.assertEqual(result['limitations'], [])
        self.assertEqual([item['unit_id'] for item in result['system_issues']], ['excel-1'])
        self.assertFalse(result['system_issues'][0]['database_queried'])
        self.assertFalse(responses)


if __name__ == "__main__":
    unittest.main()
