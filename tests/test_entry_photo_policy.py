"""Pass all current-case photos to the store-aware address comparison."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.extraction_chain import render_prompt
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import ENTRY_ADDRESS_DUPLICATE_CLARIFICATION
from audit_core.scenario_registry import SKILL_BY_SCENARIO
from audit_core.template_references import template_comparison
from tests.pdf_test_support import flags_for, unknown_checks


class EntryPhotoPolicyTests(unittest.TestCase):
    def test_all_current_shelf_pages_are_available_across_reading_batches(self):
        # More than the six-page transcription batch; keep case images separate
        # from the designated agreement reference images and unrelated pages.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = [{"unit_id": f"photo-{i}", "image": root / f"photo-{i}.png"} for i in range(9)]
            agreement = {"unit_id": "agreement", "image": root / "agreement.png"}
            unrelated = {"unit_id": "other", "image": root / "other.png"}
            flags = flags_for("entry_fee")
            case = {"scenario": "entry_fee", "archive_id": "a001", "flags": flags,
                    "units": [agreement, *units, unrelated],
                    "documents": [{"unit_id": u["unit_id"], "facts": ["本次协议或照片可见原文"]}
                                  for u in [agreement, *units, unrelated]],
                    "materials": [{"id": "entry_agreement", "state": "present", "source_ids": ["agreement"]},
                                  {"id": "shelf_photos", "state": "present", "source_ids": [u["unit_id"] for u in units]}]}
            original = deepcopy(case)
            response = unknown_checks("entry_fee", flags)
            provider = PdfEvidenceProvider("gpt-6-astra", "medium")
            def model(*args, **kwargs):
                return ({"requests": [], "limitations": ["合成资料未列具体商品标识"]}
                        if "requests" in args[2]["properties"] else response)
            with patch.object(provider, "_call", side_effect=model) as call:
                provider.audit(case, root)
            prompt = render_prompt(call.call_args.args[3])
            references = template_comparison(SKILL_BY_SCENARIO["entry_fee"], "entry_fee")[1]
            self.assertEqual(call.call_args.kwargs["images"], [agreement["image"], *[u["image"] for u in units], *references])
            self.assertNotIn(unrelated["image"], call.call_args.kwargs["images"])
            self.assertIn(ENTRY_ADDRESS_DUPLICATE_CLARIFICATION, prompt)
            for unit in units:
                self.assertIn(f'"unit_id": "{unit["unit_id"]}", "image": "{unit["image"].name}"', prompt)
            self.assertEqual(case, original)

    def test_mixed_agreement_photo_page_is_attached_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            flags = flags_for("entry_fee")
            unit = {"unit_id": "mixed", "image": root / "mixed.png"}
            case = {"scenario": "entry_fee", "archive_id": "a001", "flags": flags,
                    "units": [unit], "documents": [{"unit_id": "mixed", "facts": ["本次协议附上架照片"]}],
                    "materials": [{"id": mid, "state": "present", "source_ids": ["mixed"]}
                                  for mid in ("entry_agreement", "shelf_photos")]}
            provider = PdfEvidenceProvider("gpt-6-astra", "medium")
            def model(*args, **kwargs):
                return ({"requests": [], "limitations": ["合成资料未列具体商品标识"]}
                        if "requests" in args[2]["properties"] else unknown_checks("entry_fee", flags))
            with patch.object(provider, "_call", side_effect=model) as call:
                provider.audit(case, root)
            self.assertEqual(call.call_args.kwargs["images"].count(unit["image"]), 1)

    def test_auditing_another_case_cannot_reuse_previous_case_photos(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            provider = PdfEvidenceProvider("gpt-6-astra", "medium")
            flags = flags_for("entry_fee")
            cases = []
            for archive_id in ("a001", "a002"):
                uid = archive_id + "-photo"
                cases.append({"scenario": "entry_fee", "archive_id": archive_id, "flags": flags,
                              "units": [{"unit_id": uid, "image": root / f"{uid}.png"}],
                              "documents": [{"unit_id": uid, "facts": ["申报甲店，水印地址为示例路18号"]}],
                              "materials": [{"id": "shelf_photos", "state": "present", "source_ids": [uid]}]})
            with patch.object(provider, "_call", return_value=unknown_checks("entry_fee", flags)) as call:
                for case in cases:
                    provider.audit(case, root)
            for index, case in enumerate(cases):
                other_image = cases[1-index]["units"][0]["image"]
                self.assertIn(case["units"][0]["image"], call.call_args_list[index].kwargs["images"])
                self.assertNotIn(other_image, call.call_args_list[index].kwargs["images"])
                self.assertNotIn(other_image.name, render_prompt(call.call_args_list[index].args[3]))


if __name__ == "__main__":
    unittest.main()
