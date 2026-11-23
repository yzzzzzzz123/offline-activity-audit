"""Exact authored-row delivery, without photo interpretation or model calls."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.extraction_chain import render_prompt
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.photo_row_context import photo_row_context
from tests.test_optional_staff_and_display_photos import photo_case, review


class PhotoRowContextTests(unittest.TestCase):
    def setUp(self):
        self.fact = "Sheet1!R6: C1=5 | C2=美道家灵宝时代文化店"
        self.photo = {"unit_id": "a001-f0001-photo-0005", "file_id": "a001-f0001",
                      "source_file": "照片.xlsx", "locator": "Sheet1!R6C3（浮动图片5）"}
        self.table = {"unit_id": "a001-f0001-table-0001", "file_id": "a001-f0001",
                      "source_file": "照片.xlsx", "locator": "Sheet1", "image": None,
                      "native_facts": ["Sheet1!R5: C2=另一家店", self.fact, "Sheet1!R7: C2=后一家店"]}
        self.document = {"unit_id": self.table["unit_id"], "roles": ["spreadsheet"],
                         "facts": list(self.table["native_facts"]), "limitations": []}

    def bind(self, photo=None, tables=None, documents=None, table_ids=None):
        return photo_row_context(photo or self.photo,
                                 documents if documents is not None else [self.document],
                                 tables if tables is not None else [self.table, self.photo],
                                 table_ids if table_ids is not None else {self.table["unit_id"]})

    def test_exact_row_preserves_original_text_source_and_inputs(self):
        original = deepcopy((self.photo, self.table, self.document))
        rows = self.bind()
        self.assertEqual(rows, [{"unit_id": self.table["unit_id"], "source_file": "照片.xlsx",
                                 "locator": "Sheet1!R6", "fact": self.fact,
                                 "meaning": "表格作者标注，不是照片水印或照片可见内容"}])
        self.assertEqual((self.photo, self.table, self.document), original)

    def test_file_id_original_name_and_sheet_must_all_match(self):
        for key, value in (("file_id", "a002-f0001"), ("source_file", "别的照片.xlsx"),
                           ("locator", "Sheet2")):
            with self.subTest(key=key):
                table = dict(self.table, **{key: value})
                self.assertEqual(self.bind(tables=[table]), [])

    def test_no_neighbour_row_or_partial_row_number_match(self):
        for locator in ("Sheet1!R5C3（浮动图片5）", "Sheet1!R60C3（浮动图片5）"):
            document = dict(self.document, facts=[self.fact])
            table = dict(self.table, native_facts=[self.fact])
            self.assertEqual(self.bind(photo=dict(self.photo, locator=locator),
                                       tables=[table], documents=[document]), [])

    def test_missing_or_unresolved_placement_does_not_guess(self):
        for locator in ("xl/media/image5.jpeg", "Sheet1!位置未绑定行列（浮动图片5）", "Sheet1!R6C3", ""):
            self.assertEqual(self.bind(photo=dict(self.photo, locator=locator)), [])
        self.assertEqual(self.bind(photo=dict(self.photo, file_id="")), [])
        self.assertEqual(self.bind(table_ids=set()), [])

    def test_native_row_must_also_be_present_in_allowed_document(self):
        self.assertEqual(self.bind(documents=[]), [])
        self.assertEqual(self.bind(documents=[dict(self.document, facts=["改写后的店名"])]), [])
        self.assertEqual(self.bind(tables=[dict(self.table, native_facts=[])]), [])

    def test_duplicate_ownership_or_conflicting_rows_do_not_guess(self):
        self.assertEqual(self.bind(tables=[self.table, deepcopy(self.table)]), [])
        self.assertEqual(self.bind(documents=[self.document, deepcopy(self.document)]), [])
        other = dict(self.table, unit_id="a001-f0001-table-0002", native_facts=["Sheet1!R6: C2=冲突店"])
        other_doc = dict(self.document, unit_id=other["unit_id"], facts=other["native_facts"])
        self.assertEqual(self.bind(tables=[self.table, other], documents=[self.document, other_doc],
                                   table_ids={self.table["unit_id"], other["unit_id"]}), [])

    def test_cell_photo_and_sheet_name_containing_separator_remain_exact(self):
        sheet = "城区!门店"
        fact = sheet + "!R6: C2=甲店"
        self.assertEqual(self.bind(photo=dict(self.photo, locator=sheet + "!R6C3（单元格图片）"),
                                   tables=[dict(self.table, locator=sheet, native_facts=[fact])],
                                   documents=[dict(self.document, facts=[fact])])[0]["fact"], fact)

    def test_provider_binds_row_and_keeps_full_context_without_calling_a_model(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = photo_case(root, 1)
            photo = next(unit for unit in case["units"] if unit.get("photo_reference"))
            photo.update(file_id=self.photo["file_id"], source_file=self.photo["source_file"], locator=self.photo["locator"])
            case["units"].append(deepcopy(self.table))
            case["documents"].append(deepcopy(self.document))
            next(m for m in case["materials"] if m["id"] == "display_photos")["source_ids"].append(self.table["unit_id"])
            provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
            captured = []
            expected_binding = True

            def call(root, skill, schema, prompt, *, images, label, validator):
                text = render_prompt(prompt)
                captured.append(text)
                manifest = json.loads(text.split("本批逐张照片来源：", 1)[1].split("\n", 1)[0])
                if expected_binding:
                    self.assertEqual(manifest[0]["table_row_context"][0]["fact"], self.fact)
                else:
                    self.assertEqual(manifest[0]["table_row_context"], [])
                self.assertIn("Sheet1!R5: C2=另一家店", text)
                self.assertIn("Sheet1!R7: C2=后一家店", text)
                self.assertIn("本次已读全部原文及表格行关系", text)
                self.assertIn("不是照片可见内容或水印", text)
                result = {"photo_reviews": [review("p01")]}
                validator(result)
                return result

            with patch.object(provider, "_call", side_effect=call):
                provider._audit_display_photos(case, root, [], [])
                expected_binding = False
                case["units"][-1]["native_facts"].remove(self.fact)
                case["documents"][-1]["facts"].remove(self.fact)
                provider._audit_display_photos(case, root, [], [])
            self.assertEqual(len(captured), 2)


if __name__ == "__main__":
    unittest.main()
