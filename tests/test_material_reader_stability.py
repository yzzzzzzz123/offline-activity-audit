"""End-to-end material reader stability contract, without model/network calls."""
from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import patch
from xml.etree.ElementTree import ParseError
from zipfile import ZipFile

from openpyxl import Workbook
from PIL import Image, UnidentifiedImageError
import pymupdf

from audit_core.material_reader import read_material
from audit_core.reading_contract import (
    CUSTOMER_UNREADABLE,
    PARTIALLY_READ,
    READ_OK,
    SYSTEM_FAILURE,
    ReadingSystemError,
)
from audit_core.workbook_reading import spreadsheet_units
from tests.reader_test_support import new_test_dir, remove_test_dir


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 12), "green").save(output, format="PNG")
    return output.getvalue()


def jpeg_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (16, 12), "blue").save(output, format="JPEG")
    return output.getvalue()


def xlsx_bytes() -> bytes:
    output = BytesIO()
    book = Workbook()
    sheet = book.active
    sheet.title = "销售明细"
    sheet.append(["商品名称", "69码"])
    sheet.append(["测试商品", "6900000000000"])
    book.save(output)
    book.close()
    return output.getvalue()


def docx_bytes() -> bytes:
    output = BytesIO()
    with ZipFile(output, "w") as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>合成合同 5000 元</w:t></w:r></w:p></w:body></w:document>",
        )
    return output.getvalue()


def pdf_bytes() -> bytes:
    with pymupdf.open() as document:
        for index in range(2):
            page = document.new_page(width=180, height=240)
            page.insert_text((12, 36), f"PAGE {index + 1}")
        return document.tobytes()


def write_bundle(path: Path, files: dict[str, bytes]) -> Path:
    with ZipFile(path, "w") as archive:
        for name, payload in files.items():
            archive.writestr(name, payload)
    return path


def write_pdf(path: Path) -> Path:
    path.write_bytes(pdf_bytes())
    return path


def fake_render(source: Path, destination: Path, *, inspected=None) -> dict:
    destination.mkdir(parents=True)
    target = destination / "document.pdf"
    write_pdf(target)
    return {
        "pdf": target,
        "pages": 2,
        "tool": "synthetic-renderer",
        "pdf_sha256": "synthetic",
        "source_sha256": "synthetic",
    }


class MaterialReaderStabilityTests(unittest.TestCase):
    def setUp(self):
        self.root = new_test_dir("reader-stability-")
        self.addCleanup(remove_test_dir, self.root)

    def source(self, name: str, payload: bytes) -> Path:
        path = self.root / name
        path.write_bytes(payload)
        return path

    def output(self, name: str) -> Path:
        return self.root / f"output-{name}"

    def test_single_file_types_all_return_read_receipts(self):
        cases = [
            ("photo.jpg", jpeg_bytes(), None),
            ("photo.png", png_bytes(), None),
            ("scan.pdf", pdf_bytes(), None),
            ("book.xlsx", xlsx_bytes(), None),
        ]
        for name, payload, _ in cases:
            with self.subTest(name=name):
                result = read_material(self.source(name, payload), self.output(name))
                self.assertEqual(result["model_calls"], 0)
                self.assertEqual(result["reading_status"], READ_OK)
                self.assertEqual(result["source_count"], 1)
                self.assertGreater(result["unit_count"], 0)
                self.assertEqual(result["file_receipts"][0]["status"], READ_OK)

        with patch("audit_core.material_intake.render_word", side_effect=fake_render):
            result = read_material(
                self.source("contract.docx", docx_bytes()),
                self.output("contract.docx"),
            )
        self.assertEqual(result["reading_status"], READ_OK)
        self.assertEqual(result["file_receipts"][0]["adapter"], "word")
        self.assertGreaterEqual(result["file_receipts"][0]["pages_read"], 1)

    def test_ooxml_with_xls_extension_is_read_by_content(self):
        result = read_material(
            self.source("legacy-looking.xls", xlsx_bytes()),
            self.output("legacy-looking.xls"),
        )
        receipt = result["file_receipts"][0]
        self.assertEqual(receipt["status"], READ_OK)
        self.assertEqual(receipt["sniffed_format"], "xlsx")
        self.assertTrue(receipt["declared_format"].startswith("xls"))
        self.assertGreaterEqual(receipt["sheet_count"], 1)

    def test_docx_with_pdf_extension_is_read_by_content(self):
        with patch("audit_core.material_intake.render_word", side_effect=fake_render):
            result = read_material(
                self.source("misnamed.pdf", docx_bytes()),
                self.output("misnamed.pdf"),
            )
        receipt = result["file_receipts"][0]
        self.assertEqual(receipt["status"], READ_OK)
        self.assertEqual(receipt["sniffed_format"], "docx")
        self.assertEqual(receipt["adapter"], "word")

    def test_one_bad_attachment_does_not_discard_good_attachments(self):
        bundle = write_bundle(self.root / "mixed.zip", {
            "good.png": png_bytes(),
            "bad.jpg": b"this is not an image",
            "book.xlsx": xlsx_bytes(),
        })
        result = read_material(bundle, self.output("mixed"))
        receipts = {item["source_file"]: item for item in result["file_receipts"]}
        self.assertEqual(result["reading_status"], PARTIALLY_READ)
        self.assertEqual(receipts["good.png"]["status"], READ_OK)
        self.assertEqual(receipts["book.xlsx"]["status"], READ_OK)
        self.assertEqual(receipts["bad.jpg"]["status"], CUSTOMER_UNREADABLE)
        self.assertTrue(receipts["bad.jpg"]["customer_actionable"])

    def test_missing_renderer_is_file_scoped_system_failure(self):
        bundle = write_bundle(self.root / "word-and-image.zip", {
            "contract.docx": docx_bytes(),
            "evidence.png": png_bytes(),
        })
        failure = ReadingSystemError(
            "系统缺少 LibreOffice Writer", code="renderer_missing", retryable=False)
        with patch("audit_core.material_intake.render_word", side_effect=failure):
            result = read_material(bundle, self.output("word-and-image"))
        receipts = {item["source_file"]: item for item in result["file_receipts"]}
        self.assertEqual(receipts["evidence.png"]["status"], READ_OK)
        self.assertEqual(receipts["contract.docx"]["status"], SYSTEM_FAILURE)
        self.assertFalse(receipts["contract.docx"]["customer_actionable"])
        self.assertEqual(result["reading_status"], SYSTEM_FAILURE)

    def test_damaged_pdf_page_keeps_later_pages(self):
        source = self.source("pages.pdf", pdf_bytes())
        real_get_pixmap = pymupdf.Page.get_pixmap
        calls = {"count": 0}

        def fail_first_page(self, *args, **kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                raise pymupdf.FileDataError("damaged page stream")
            return real_get_pixmap(self, *args, **kwargs)

        with patch.object(pymupdf.Page, "get_pixmap", fail_first_page):
            result = read_material(source, self.output("damaged-page"))
        units = result["file_receipts"][0]
        self.assertEqual(units["status"], PARTIALLY_READ)
        self.assertEqual(units["pages_total"], 2)
        self.assertEqual(units["pages_read"], 1)
        self.assertFalse(units["customer_actionable"])
        archive = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))["archives"][0]
        self.assertEqual([unit["read_status"] for unit in archive["units"]],
                         [CUSTOMER_UNREADABLE, READ_OK])

    def test_spreadsheet_decode_error_keeps_rows_already_read(self):
        source = self.source("rows.xlsx", xlsx_bytes())
        root = self.root / "spreadsheet-units"
        root.mkdir()

        def broken_rows(sheet, layout=None):
            yield 2, [{"column": 1, "value": "已读取行", "data_type": "s"}]
            raise ParseError("damaged sheet XML")

        with patch("audit_core.workbook_reading.stored_rows", new=broken_rows):
            units = spreadsheet_units(
                {"file_id": "f1", "source_file": "rows.xlsx", "path": source},
                root,
            )
        self.assertGreaterEqual(len(units), 2)
        self.assertTrue(units[0]["native_facts"])
        self.assertIn("已读取行", "".join(units[0]["native_facts"]))
        self.assertEqual(units[-1]["read_status"], CUSTOMER_UNREADABLE)

    def test_staging_cleanup_failure_does_not_hide_published_manifest(self):
        source = self.source("cleanup.png", png_bytes())
        real_rmtree = shutil.rmtree

        def cleanup(path, *args, **kwargs):
            if Path(path).name == ".staging":
                raise OSError("staging is busy")
            return real_rmtree(path, *args, **kwargs)

        output = self.output("cleanup")
        with patch("audit_core.material_reader.shutil.rmtree", side_effect=cleanup):
            result = read_material(source, output)
        manifest = json.loads(Path(result["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(result["reading_status"], READ_OK)
        self.assertEqual(manifest["staging_cleanup_deferred"]["error_code"],
                         "staging_cleanup_deferred")

    def test_posix_branches_do_not_require_powershell_or_com(self):
        from audit_core import legacy_activity_workbook, word_materials

        legacy = self.source("legacy.xls", bytes.fromhex("d0cf11e0a1b11ae1") + b"x")
        with patch.object(legacy_activity_workbook.os, "name", "posix"), \
                patch.object(legacy_activity_workbook.shutil, "which", return_value=None), \
                patch.object(legacy_activity_workbook, "run_command") as command:
            with self.assertRaises(ReadingSystemError) as caught:
                legacy_activity_workbook._convert_legacy_workbook(
                    legacy, self.root / "converted.xlsx")
        command.assert_not_called()
        self.assertEqual(caught.exception.code, "renderer_missing")

        word = self.source("word.docx", docx_bytes())
        with patch.object(word_materials.os, "name", "posix"), \
                patch.object(word_materials, "libreoffice_path", return_value=None), \
                patch.object(word_materials, "run_command") as command:
            with self.assertRaises(ReadingSystemError) as caught:
                word_materials.render_word(word, self.root / "word-render")
        command.assert_not_called()
        self.assertEqual(caught.exception.code, "renderer_missing")


if __name__ == "__main__":
    unittest.main()
