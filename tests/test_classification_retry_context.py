"""Gate failures remain strict; only bounded private correction data is added."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.pdf_evidence import (ClassificationEvidenceError, classification_decision,
    classification_schema, validate_classification)
from tests.pdf_test_support import classification
from tests.test_optional_staff_and_display_photos import selected_staff


class ClassificationRetryContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        schema = Path(temporary.name) / "schema.json"
        schema.write_text(json.dumps(classification_schema("personnel_incentive")), encoding="utf-8")
        self.documents = [{"unit_id": "contract", "facts": ["PRIVATE_CONTRACT原文不改"]},
                          {"unit_id": "claim", "facts": ["PRIVATE_CLAIM原文不改"]}]
        self.value = classification("personnel_incentive", ["contract", "claim"])
        selected = next(m for m in self.value["material_matches"] if m["scenario"] == "personnel_incentive")
        self.value.update(material_matches=[selected], candidate_scenarios=["personnel_incentive"],
                          flags=deepcopy(selected["flags"]), materials=deepcopy(selected["materials"]))
        self.parser = EvidenceOutputParser(schema_path=schema, post_validate=lambda value:
            validate_classification(value, self.documents, "personnel_incentive"))

    def reject(self, value, code, path):
        before = deepcopy((value, self.documents))
        with self.assertRaises(AuditError) as caught:
            self.parser.parse(json.dumps(value, ensure_ascii=False))
        self.assertEqual((value, self.documents), before)
        self.assertEqual(self.parser.last_diagnostics, {"validation_stage": "evidence",
            "validation_code": code, "validation_paths": [path]})
        self.assertNotIn("PRIVATE_", str(caught.exception))
        self.assertNotIn("PRIVATE_", json.dumps(self.parser.last_diagnostics))
        self.assertLessEqual(len(self.parser.last_retry_context.encode("utf-8")), 8192)
        return caught.exception

    def test_source_errors_preserve_unknown_code_and_actual_location(self):
        for ids, code in ((["outside"], "unknown_source"), ([], "gate_source_reference")):
            value = deepcopy(self.value)
            value["material_matches"][0]["materials"][0]["source_ids"] = ids
            with self.subTest(ids=ids):
                self.reject(value, code, "material_matches/0/materials/0/source_ids")
                context = json.loads(self.parser.last_retry_context)
                self.assertEqual(context["fields"][0]["value"], ids)
                self.assertEqual(context["sources"], [])

    def test_missing_coverage_is_reported_without_assigning_a_material(self):
        value = deepcopy(self.value)
        value["source_ids"] = ["contract"]
        self.reject(value, "gate_source_coverage", "source_ids")
        context = json.loads(self.parser.last_retry_context)
        self.assertEqual(context["source_ids"], ["claim"])
        self.assertEqual(context["sources"], [self.documents[1]])
        value = deepcopy(self.value)
        for item in value["material_matches"][0]["materials"]:
            item["source_ids"] = [uid for uid in item["source_ids"] if uid != "claim"]
        self.reject(value, "gate_source_coverage", "material_matches/0/materials")
        self.assertEqual(json.loads(self.parser.last_retry_context)["source_ids"], ["claim"])

    def test_missing_list_invalid_state_and_mirror_remain_distinct_failures(self):
        value = deepcopy(self.value)
        value["material_matches"][0]["materials"].pop()
        self.reject(value, "gate_material_list", "material_matches/0/materials")
        value = deepcopy(self.value)
        value["material_matches"][0]["materials"][0]["state"] = "not_applicable"
        self.reject(value, "gate_material_state", "material_matches/0/materials/0/state")
        value = deepcopy(self.value)
        value["flags"]["red_packet"] = True
        self.reject(value, "gate_consistency", "flags/red_packet")
        context = json.loads(self.parser.last_retry_context)
        self.assertEqual(context["fields"], [{"path": "flags/red_packet", "value": True},
            {"path": "material_matches/0/flags/red_packet", "value": False}])
        value = deepcopy(self.value)
        value["materials"][0]["reason"] = "PRIVATE_WRONG_MIRROR"
        self.reject(value, "gate_consistency", "materials/0/reason")

    def test_true_missing_material_is_a_normal_business_issue_not_a_validation_failure(self):
        value = deepcopy(self.value)
        match = value["material_matches"][0]
        match["materials"][0].update(state="missing", source_ids=[], reason="未提交对应材料")
        match["supported"] = False
        value["materials"] = deepcopy(match["materials"])
        before = deepcopy(value)
        self.assertEqual(self.parser.parse(json.dumps(value)), value)
        result = classification_decision(value, self.documents, "personnel_incentive")
        self.assertFalse(result["matched"])
        self.assertTrue(result["reasons"])
        self.assertEqual(value, before)
        self.assertEqual(self.parser.last_retry_context, "")

    def test_affirmative_staff_cannot_be_hidden_by_false_or_omitted_quote(self):
        self.documents = [{"unit_id": "u", "facts": ["本次安排临促2人", "PRIVATE_STAFF本次事实"]}]
        for flag in (False, None):
            for evidence in (None, [], [{"unit_id": "u", "quote": "PRIVATE_STAFF本次事实", "stance": "unclear"}]):
                value = selected_staff("missing", flag)
                staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
                if evidence is not None:
                    staff["condition_evidence"] = evidence
                with self.subTest(flag=flag, evidence=evidence):
                    error = self.reject(value, "gate_staff_condition", "material_matches/0/flags/temporary_staff")
                    self.assertIn("不能省略肯定原文证据", str(error))
                    self.assertIn("本次安排临促2人", self.parser.last_retry_context)

    def test_staff_quote_remains_verbatim_and_has_a_specific_position(self):
        self.documents = [{"unit_id": "u", "facts": ["本次安排临促2人"]}]
        value = selected_staff("missing", True)
        index = next(i for i, m in enumerate(value["materials"]) if m["id"] == "staff_photos")
        value["materials"][index]["condition_evidence"][0]["quote"] = "本次安排 临促2人"
        self.reject(value, "gate_staff_condition", f"material_matches/0/materials/{index}/condition_evidence/0/quote")

    def test_private_context_is_bounded_complete_and_only_same_sources(self):
        value = deepcopy(self.value)
        value["source_ids"] = ["contract"]
        self.documents[0]["facts"] = ["UNRELATED_PRIVATE_MUST_NOT_COPY"]
        self.documents[1]["facts"] = ["大" * 3000, "\x01" * 1800] + [f"完整原文{i}" for i in range(100)]
        self.reject(value, "gate_source_coverage", "source_ids")
        context = json.loads(self.parser.last_retry_context)
        facts = [fact for source in context["sources"] for fact in source["facts"]]
        self.assertLessEqual(len(facts), 3)
        self.assertTrue(context["omitted"])
        self.assertTrue(all(fact in self.documents[1]["facts"] for fact in facts))
        self.assertNotIn("UNRELATED_PRIVATE", self.parser.last_retry_context)
        self.assertNotIn("大", self.parser.last_retry_context)

    def test_source_id_context_limit_never_becomes_a_full_missing_source_inventory(self):
        self.documents.extend({"unit_id": f"extra{i}", "facts": [f"原文{i}"]} for i in range(100))
        self.reject(self.value, "gate_source_coverage", "material_matches/0/materials")
        context = json.loads(self.parser.last_retry_context)
        self.assertEqual(len(context["source_ids"]), 16)
        self.assertLessEqual(sum(len(s["facts"]) for s in context["sources"]), 3)
        self.assertTrue(context["omitted"])

    def test_untrusted_metadata_and_mismatched_payload_cannot_enter_retry(self):
        message = "类型比对必须保留全部已读来源，包括未匹配及清单外资料"
        for mode in ("ordinary", "path", "field", "code", "facts"):
            error = ClassificationEvidenceError(message, ("source_ids",), self.value,
                {d["unit_id"]: d for d in self.documents}, source_ids=["claim"])
            if mode == "ordinary":
                error = AuditError(message)
                error.validation_paths = ["source_ids"]
                error.retry_context = {"PRIVATE": "arbitrary"}
            elif mode == "path":
                error.validation_paths = ["material_matches/999/source_ids"]
            elif mode == "field":
                error.retry_context["fields"][0]["value"] = ["invented"]
            elif mode == "code":
                error.validation_code = "gate_staff_condition"
            else:
                error.retry_context["sources"][0]["facts"] = ["whole"] * 4
            def reject(_):
                raise error
            self.parser.post_validate = reject
            with self.subTest(mode=mode), self.assertRaises(AuditError):
                self.parser.parse(json.dumps(self.value))
            self.assertEqual(self.parser.last_retry_context, "")
            if mode in ("ordinary", "path", "code"):
                self.assertEqual(self.parser.last_diagnostics["validation_paths"], ["$"])

    def test_success_json_and_schema_failure_clear_previous_private_context(self):
        bad = deepcopy(self.value)
        bad["source_ids"] = ["contract"]
        for next_value in (json.dumps(self.value), "not json", json.dumps({"reasoning": "PRIVATE_BAD"})):
            self.reject(bad, "gate_source_coverage", "source_ids")
            self.assertTrue(self.parser.last_retry_context)
            try:
                self.parser.parse(next_value)
            except (AuditError, json.JSONDecodeError):
                pass
            self.assertEqual(self.parser.last_retry_context, "")


if __name__ == "__main__":
    unittest.main()
