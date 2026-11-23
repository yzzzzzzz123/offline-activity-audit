"""Preserve readable scanner PDFs without weakening unreadable-file checks."""
from __future__ import annotations

import hashlib
import io
from pathlib import Path
import re
import tempfile
import unittest
import zipfile
from unittest.mock import patch

from PIL import Image
import pymupdf
from pypdf.errors import DependencyError, PdfReadError

from audit_core.material_intake import MAX_VISUAL_PAGES, _is_content_decode_error, _visual_limitations, prepare_material_diagnosis
from audit_core.pdf_materials import prepare_units
from audit_core.common import AuditError


def _pdf(*, scans: bool = False, pages: int = 2) -> bytes:
    with pymupdf.open() as document:
        for index in range(1, pages + 1):
            page = document.new_page(width=240, height=320)
            if scans:
                image = io.BytesIO()
                Image.new("RGB", (240, 320), (index * 40, 100, 180)).save(image, format="PNG")
                page.insert_image(page.rect, stream=image.getvalue())
            else:
                page.insert_text((15, 40), f"PAGE {index}: QTY 12 PRICE 10 AMOUNT 120")
        return document.tobytes()


def _scanner_incremental_update(raw: bytes) -> bytes:
    """Scanner-compatible final xref has no entries and points to /Prev.

    This is the structural failure found in the four original claim PDFs;
    all test pages and business-looking text here are synthetic.
    """
    previous = int(re.findall(rb"startxref\s+(\d+)", raw)[-1])
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        catalog, size = document.pdf_catalog(), document.xref_length()
    position = len(raw) + 1
    return raw + (
        f"\nxref\ntrailer\n<</Root {catalog} 0 R /Size {size}/Prev {previous}>>"
        f"\nstartxref\n{position}\n%%EOF\n"
    ).encode("ascii")


class ScannerPdfCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def source(self, raw: bytes, name: str = "source.pdf") -> Path:
        path = self.root / name
        path.write_bytes(raw)
        return path

    def test_empty_incremental_xref_keeps_every_original_page_and_source_bytes(self) -> None:
        reference = _pdf(pages=3)
        raw = _scanner_incremental_update(reference)
        source = self.source(raw)
        before = hashlib.sha256(raw).hexdigest()
        self.assertEqual(_visual_limitations(source), [])
        inventory = {"file_id": "a001-f0001", "source_file": source.name,
                     "kind": "visual", "path": str(source), "suffix": ".pdf", "limitations": []}
        units = prepare_units({"inventory": [inventory]}, self.root / "rendered")
        self.assertEqual([unit["locator"] for unit in units], ["第1页", "第2页", "第3页"])
        self.assertEqual(len({unit["unit_id"] for unit in units}), 3)
        with pymupdf.open(stream=reference, filetype="pdf") as document:
            for index, unit in enumerate(units):
                expected = self.root / f"reference-{index}.jpg"
                document[index].get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False).save(expected, jpg_quality=95)
                self.assertEqual(unit["image"].read_bytes(), expected.read_bytes())
                self.assertIn(f"PAGE {index + 1}", "".join(unit["native_facts"]))
                self.assertEqual(unit["limitations"], [])
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), before)

    def test_scanned_contract_settlement_pos_and_payment_reach_page_images(self) -> None:
        inputs = self.root / "input"
        inputs.mkdir()
        raw = _scanner_incremental_update(_pdf(scans=True))
        names = ("促销合同.pdf", "结算单.pdf", "POS盖章.pdf", "转账记录.pdf")
        archive = inputs / "HX202606250043-人员激励.zip"
        with zipfile.ZipFile(archive, "w") as container:
            for name in names:
                container.writestr(name, raw)
        diagnosis = prepare_material_diagnosis(inputs, self.root / "diagnosis", original_error="")
        result = diagnosis["archives"][0]
        self.assertEqual(len(result["inventory"]), 4)
        self.assertTrue(all(item["limitations"] == [] for item in result["inventory"]))
        units = prepare_units(result, self.root / "rendered")
        self.assertEqual(len(units), 8)
        self.assertEqual({unit["source_file"] for unit in units}, set(names))
        for unit in units:
            self.assertTrue(unit["image"].is_file())
            self.assertEqual(unit["native_facts"], [])
            self.assertEqual(unit["limitations"], [])
            with Image.open(unit["image"]) as image:
                self.assertEqual(image.size, (480, 640))

    def test_recoverable_startxref_offset_is_not_unreadable(self) -> None:
        raw = _pdf()
        match = re.search(rb"startxref\s+(\d+)", raw)
        assert match
        raw = raw[:match.start(1)] + str(int(match[1]) + 1).encode() + raw[match.end(1):]
        self.assertEqual(_visual_limitations(self.source(raw)), [])

    def test_password_required_remains_unreadable(self) -> None:
        with pymupdf.open(stream=_pdf(), filetype="pdf") as document:
            raw = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                                   owner_pw="synthetic-owner", user_pw="synthetic-open-password")
        self.assertEqual(_visual_limitations(self.source(raw)), ["PDF 已加密，无法读取其中的材料内容"])

    def test_viewable_owner_permission_pdf_does_not_require_open_password(self) -> None:
        with pymupdf.open(stream=_pdf(), filetype="pdf") as document:
            raw = document.tobytes(encryption=pymupdf.PDF_ENCRYPT_AES_256,
                                   owner_pw="synthetic-owner", user_pw="",
                                   permissions=pymupdf.PDF_PERM_ACCESSIBILITY)
        self.assertEqual(_visual_limitations(self.source(raw)), [])

    def test_malformed_and_empty_files_are_reported(self) -> None:
        for index, raw in enumerate((b"", b"not a PDF", b"%PDF-1.7\nxref\ntrailer\n")):
            with self.subTest(index=index):
                self.assertEqual(_visual_limitations(self.source(raw, f"bad-{index}.pdf")),
                                 ["PDF 无法读取，无法识别其中的材料内容"])

    def test_page_limit_still_applies_to_recoverable_scanner_pdf(self) -> None:
        source = self.source(_scanner_incremental_update(_pdf(pages=3)))
        with patch("audit_core.material_intake.MAX_VISUAL_PAGES", 2):
            with self.assertRaisesRegex(AuditError, 'PDF 超过单份材料可读取的 2 页上限'):
                _visual_limitations(source)
        self.assertEqual(MAX_VISUAL_PAGES, 500)

    def test_missing_files_are_execution_faults(self) -> None:
        with self.assertRaises(FileNotFoundError):
            _visual_limitations(self.root / "missing.pdf")

    def test_shared_error_classifier_preserves_both_pdf_decoders_and_dependency_faults(self) -> None:
        self.assertTrue(_is_content_decode_error(pymupdf.FileDataError("invalid PDF")))
        self.assertTrue(_is_content_decode_error(PdfReadError("invalid PDF")))
        self.assertFalse(_is_content_decode_error(DependencyError("missing decoder dependency")))


if __name__ == "__main__":
    unittest.main()
