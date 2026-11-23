"""One material inventory written once must not fail the claim (no shape gate)."""
from copy import deepcopy
import unittest

import jsonschema

from audit_core.model_authority import transport_schema
from audit_core.pdf_evidence import (classification_schema, complete_material_mirrors,
    normalize_material_states, validate_classification)
from tests.pdf_test_support import classification


SCENARIO = "giveaway_promotion"
UNITS = ["claim", "settlement"]


def selected(scenario=SCENARIO, units=UNITS):
    """A complete single-type answer shaped like the live selected-scenario call."""
    value = classification(scenario, units)
    match = next(item for item in value["material_matches"] if item["scenario"] == scenario)
    return {"candidate_scenarios": [scenario], "reason": value["reason"], "source_ids": list(units),
            "flags": deepcopy(match["flags"]), "materials": deepcopy(match["materials"]),
            "background_materials": [], "material_matches": [deepcopy(match)]}


def documents(units=UNITS):
    return [{"unit_id": unit, "roles": [], "facts": [], "limitations": []} for unit in units]


class ClassificationMaterialMirrorTests(unittest.TestCase):
    def test_single_copy_answer_satisfies_both_schemas(self):
        value = selected()
        del value["material_matches"][0]["flags"]
        del value["material_matches"][0]["materials"]
        del value["material_matches"][0]["extra_materials"]
        for schema in (classification_schema(SCENARIO), transport_schema(classification_schema(SCENARIO))):
            with self.subTest(schema=schema["required"]):
                jsonschema.Draft202012Validator(schema).validate(value)

    def test_top_level_only_answer_passes_claim_validation(self):
        value = selected()
        del value["material_matches"][0]["flags"]
        del value["material_matches"][0]["materials"]
        del value["material_matches"][0]["extra_materials"]
        normalize_material_states(value)
        validate_classification(value, documents(), SCENARIO)
        self.assertEqual(value["material_matches"][0]["flags"], value["flags"])
        self.assertEqual(value["material_matches"][0]["materials"], value["materials"])
        self.assertEqual(value["material_matches"][0]["extra_materials"], [])

    def test_eight_list_answer_with_one_copy_of_each_mirror_is_readable(self):
        """The 2026-11-12 HX202610180011 shape: every list comparison returned,
        but flags/materials written once at the top level."""
        value = classification(SCENARIO, UNITS)
        for match in value["material_matches"]:
            match.pop("flags", None)
            match.pop("materials", None)
            match.pop("extra_materials", None)
        for schema in (classification_schema(), transport_schema(classification_schema())):
            with self.subTest(required=schema["required"]):
                jsonschema.Draft202012Validator(schema).validate(value)
        normalize_material_states(value)
        for match in value["material_matches"]:
            self.assertEqual(match["extra_materials"], [])
            self.assertTrue(match["flags"])

    def test_nested_only_answer_is_reused_for_the_claim_copy(self):
        value = selected()
        del value["flags"]
        del value["materials"]
        del value["background_materials"]
        normalize_material_states(value)
        validate_classification(value, documents(), SCENARIO)
        self.assertEqual(value["flags"], value["material_matches"][0]["flags"])
        self.assertEqual(value["materials"], value["material_matches"][0]["materials"])
        self.assertEqual(value["background_materials"], [])

    def test_two_different_copies_stay_visible(self):
        value = selected()
        value["flags"] = {**value["flags"], "uses_pos": not value["flags"]["uses_pos"]}
        before = deepcopy(value["flags"])
        complete_material_mirrors(value)
        self.assertEqual(value["flags"], before)
        self.assertNotEqual(value["flags"], value["material_matches"][0]["flags"])

    def test_absent_mirrors_become_unknown_without_inventing_materials(self):
        value = {"candidate_scenarios": [SCENARIO], "reason": "资料可读", "source_ids": list(UNITS),
                 "material_matches": [{"scenario": SCENARIO, "supported": False,
                                       "reason": "仅盘点", "source_ids": list(UNITS)}]}
        normalize_material_states(value)
        self.assertEqual(value["materials"], [])
        self.assertTrue(all(flag is None for flag in value["flags"].values()))
        self.assertEqual(value["material_matches"][0]["extra_materials"], [])


if __name__ == "__main__":
    unittest.main()
