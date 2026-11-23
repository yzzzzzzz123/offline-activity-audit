"""Material state wording from the audit model must reach program rules."""
import unittest

from audit_core.pdf_evidence import normalize_material_states
from audit_core.pdf_workflow import _scene_units


def classification(**states):
    return {"materials": [{"id": name, "state": state, "reason": "", "source_ids": ["u1"]}
                          for name, state in states.items()],
            "material_matches": [{"scenario": "entry_fee", "materials": [
                {"id": name, "state": state, "reason": "", "source_ids": ["u1"]}
                for name, state in states.items()]}]}


class MaterialStateNormalizationTests(unittest.TestCase):
    def test_model_vocabulary_maps_to_program_states(self):
        value = normalize_material_states(classification(
            shelf_photos="provided", pos="not_applicable",
            approved_claim="optional_not_provided", entry_system="Missing",
            odd_wording="maybe"))
        states = {item["id"]: item["state"] for item in value["materials"]}
        self.assertEqual(states, {"shelf_photos": "present", "pos": "not_applicable",
                                  "approved_claim": "missing", "entry_system": "missing",
                                  "odd_wording": "unclear"})
        for match in value["material_matches"]:
            self.assertEqual(match["materials"], value["materials"])

    def test_canonical_states_are_untouched(self):
        value = classification(shelf_photos="present", entry_system="unclear", pos="missing")
        before = [dict(item) for item in value["materials"]]
        normalize_material_states(value)
        self.assertEqual(value["materials"], before)

    def test_provided_photos_are_indexed_as_scene_units(self):
        documents = [{"unit_id": "u1", "roles": ["photo"], "facts": [], "limitations": []}]
        units = [{"unit_id": "u1", "image": "photo.png", "source_file": "photo.png", "locator": ""}]
        value = normalize_material_states(classification(shelf_photos="provided"))
        self.assertEqual([unit["unit_id"] for unit in
                          _scene_units(units, value["materials"], documents)], ["u1"])


if __name__ == "__main__":
    unittest.main()
