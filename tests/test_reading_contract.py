"""Format routing, owner classification and per-file receipts."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
import unittest
from zipfile import ZipFile

from openpyxl import Workbook
from PIL import Image, UnidentifiedImageError
import pymupdf

from audit_core.reading_contract import (
    CUSTOMER_UNREADABLE,
    INTERNAL_BUG,
    PARTIALLY_READ,
    READ_OK,
    SYSTEM_FAILURE,
    ReadingSystemError,
    aggregate_reading_status,
    classify_reading_error,
    derive_receipt,
    normalize_reading_path,
    normalize_visual_orientation,
    reader_for_format,
    sniff_format,
)
from tests.reader_test_support import test_dir


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 10), "red").save(output, format="PNG")
    return output.getvalue()


def jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (12, 10), "blue").save(output, format="JPEG")
    return output.getvalue()


def docx_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>contract</w:t></w:r></w:p></w:body></w:document>",
        )
    return output.getvalue()


def xlsx_bytes() -> bytes:
    output = BytesIO()
    book = Workbook()
    book.active.append(["product", "barcode"])
    book.active.append(["sample", "6900000000000"])
    book.save(output)
    book.close()
    return output.getvalue()


def pdf_bytes() -> bytes:
    with pymupdf.open() as document:
        page = document.new_page(width=120, height=160)
        page.insert_text((10, 20), "sample page")
        return document.tobytes()


class FormatRoutingTests(unittest.TestCase):
    def test_declared_extension_aliases_are_not_format_mismatches(self):
        for declared, fmt in (
            (".jpg", "jpeg"),
            (".jpeg", "jpeg"),
            (".jfif", "jpeg"),
            (".tif", "tiff"),
            (".xlsm", "xlsx"),
        ):
            with self.subTest(declared=declared):
                selected = reader_for_format(fmt, declared)
                self.assertFalse(selected["format_mismatch"])
                self.assertTrue(selected["reading_suffix"].startswith("."))

    def test_content_signature_wins_over_wrong_suffix(self):
        with test_dir("reader-contract-") as root:
            cases = {
                "photo.pdf": (png_bytes(), "png", ".png"),
                "contract.doc": (b"{\\rtf1\\ansi contract}", "rtf", ".rtf"),
                "spreadsheet.xls": (xlsx_bytes(), "xlsx", ".xlsx"),
                "word.pdf": (docx_bytes(), "docx", ".docx"),
            }
            for name, (payload, expected_format, expected_suffix) in cases.items():
                path = root / name
                path.write_bytes(payload)
                with self.subTest(name=name):
                    fmt = sniff_format(path, path.suffix)
                    selected = reader_for_format(fmt, path.suffix)
                    self.assertEqual(fmt, expected_format)
                    self.assertEqual(selected["reading_suffix"], expected_suffix)
                    self.assertTrue(selected["format_mismatch"])

    def test_ole_signature_is_not_treated_as_a_normal_xlsx(self):
        with test_dir("reader-contract-") as root:
            path = root / "legacy.xlsx"
            path.write_bytes(bytes.fromhex("d0cf11e0a1b11ae1") + b"not-a-real-cfb")
            self.assertEqual(sniff_format(path, ".xlsx"), "xls")
            selected = reader_for_format("xls", ".xlsx")
            self.assertEqual(selected["reading_suffix"], ".xls")
            self.assertTrue(selected["format_mismatch"])

    def test_normalization_copies_inside_the_output_root(self):
        with test_dir("reader-contract-") as root:
            source = root / "declared.pdf"
            source.write_bytes(png_bytes())
            item = {"path": source, "file_id": "f1", "reading_suffix": ".png"}
            target = normalize_reading_path(item, root / "normalized")
            self.assertTrue(target.is_file())
            self.assertEqual(target.read_bytes(), source.read_bytes())
            self.assertEqual(target.parent, (root / "normalized").resolve())
            self.assertEqual(source.read_bytes(), png_bytes())

    def test_exif_orientation_is_applied_only_to_the_task_copy(self):
        with test_dir("reader-contract-") as root:
            source = root / "source.jpg"
            exif = Image.Exif()
            exif[274] = 6
            Image.new("RGB", (30, 20), "white").save(
                source, format="JPEG", quality=95, exif=exif)
            source_bytes = source.read_bytes()

            task_copy = root / "task.jpg"
            task_copy.write_bytes(source_bytes)
            result = normalize_visual_orientation(task_copy)

            self.assertTrue(result["applied"])
            self.assertEqual(result["orientation"], 6)
            self.assertEqual(result["original_size"], [30, 20])
            self.assertEqual(result["normalized_size"], [20, 30])
            with Image.open(task_copy) as image:
                self.assertEqual(image.size, (20, 30))
            self.assertEqual(source.read_bytes(), source_bytes)

    def test_image_without_orientation_remains_byte_identical(self):
        with test_dir("reader-contract-") as root:
            image = root / "plain.png"
            image.write_bytes(png_bytes())
            before = image.read_bytes()
            result = normalize_visual_orientation(image)
            self.assertFalse(result["applied"])
            self.assertEqual(image.read_bytes(), before)


class ReceiptAndOwnershipTests(unittest.TestCase):
    def test_receipt_reports_pages_sheets_and_units(self):
        item = {
            "file_id": "f1",
            "source_file": "book.xlsx",
            "declared_format": "xlsx",
            "sniffed_format": "xlsx",
            "adapter": "spreadsheet",
            "kind": "spreadsheet",
            "reading_suffix": ".xlsx",
            "elapsed_seconds": 1.23456,
        }
        units = [
            {"file_id": "f1", "locator": "Sheet1", "read_status": READ_OK},
            {"file_id": "f1", "locator": "Sheet2", "read_status": READ_OK},
        ]
        receipt = derive_receipt(item, units)
        self.assertEqual(receipt["status"], READ_OK)
        self.assertEqual(receipt["sheet_count"], 2)
        self.assertEqual(receipt["unit_count"], 2)
        self.assertEqual(receipt["elapsed_seconds"], 1.235)

    def test_receipt_keeps_partial_success_and_failure_ownership(self):
        item = {
            "file_id": "f1",
            "source_file": "scan.pdf",
            "declared_format": "pdf",
            "sniffed_format": "pdf",
            "adapter": "pdf",
            "kind": "visual",
            "reading_suffix": ".pdf",
            "pages_total": 2,
        }
        units = [
            {"file_id": "f1", "locator": "第1页", "read_status": READ_OK},
            {"file_id": "f1", "locator": "第2页", "read_status": SYSTEM_FAILURE,
             "error_code": "storage_full", "retryable": True,
             "customer_actionable": False, "limitations": ["磁盘空间不足"]},
        ]
        receipt = derive_receipt(item, units)
        self.assertEqual(receipt["status"], PARTIALLY_READ)
        self.assertEqual(receipt["pages_read"], 1)
        self.assertEqual(receipt["pages_total"], 2)
        self.assertFalse(receipt["customer_actionable"])
        self.assertEqual(aggregate_reading_status([receipt]), PARTIALLY_READ)

    def test_decoder_damage_is_customer_unreadable_but_os_failure_is_system(self):
        self.assertEqual(
            classify_reading_error(UnidentifiedImageError("bad image"))["status"],
            CUSTOMER_UNREADABLE,
        )
        self.assertEqual(
            classify_reading_error(pymupdf.FileDataError("bad page"))["status"],
            CUSTOMER_UNREADABLE,
        )
        permission = classify_reading_error(PermissionError("denied"))
        self.assertEqual(permission["status"], SYSTEM_FAILURE)
        self.assertFalse(permission["customer_actionable"])
        missing_renderer = classify_reading_error(
            ReadingSystemError("LibreOffice missing", code="renderer_missing", retryable=False))
        self.assertEqual(missing_renderer["error_code"], "renderer_missing")
        self.assertFalse(missing_renderer["customer_actionable"])

    def test_unknown_exception_is_internal_bug_not_missing_material(self):
        self.assertEqual(classify_reading_error(RuntimeError("unexpected"))["status"], INTERNAL_BUG)


if __name__ == "__main__":
    unittest.main()
