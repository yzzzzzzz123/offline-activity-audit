"""Read real container layouts without Excel, converters or customer fixtures."""
import hashlib
import io
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage

from audit_core.common import AuditError
from audit_core.pdf_materials import prepare_units
from tests.pdf_test_support import image_bytes


REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def biff_workbook():
    """Small BIFF8 sheet with text-cached DISPIMG values, as exported by WPS."""
    def record(code, body=b""):
        return struct.pack("<HH", code, len(body)) + body

    def bof(kind):
        return record(0x0809, struct.pack("<HHHHII", 0x0600, kind, 0x0DBB, 0x07CC, 0, 6))

    values = ["门店", "照片", "甲店", '=DISPIMG("ID_A1",1)', "乙店"]
    strings = b"".join(struct.pack("<HB", len(value), 1) + value.encode("utf-16le") for value in values)
    sst = record(0x00FC, struct.pack("<II", 6, len(values)) + strings)
    sheet_name = "陈列".encode("utf-16le")

    def globals_at(offset):
        return (bof(5) + record(0x0042, struct.pack("<H", 1200))
                + record(0x0085, struct.pack("<IBBBB", offset, 0, 0, 2, 1) + sheet_name)
                + sst + record(0x000A))

    sheet = bof(0x10) + record(0x0200, struct.pack("<IIHHH", 0, 3, 0, 2, 0))
    for row, col, string in ((0, 0, 0), (0, 1, 1), (1, 0, 2), (1, 1, 3), (2, 0, 4), (2, 1, 3)):
        sheet += record(0x00FD, struct.pack("<HHHI", row, col, 0, string))
    return globals_at(len(globals_at(0))) + sheet + record(0x000A)


def ole_workbook(image_package=None):
    """CFB v3 with interleaved sectors: raw ZIP offsets cannot address it."""
    streams = [("Workbook", biff_workbook())]
    if image_package is not None:
        streams.append(("ETCellImageData", image_package))
    chunks = [[body.ljust(max(4096, (len(body) + 511) // 512 * 512), b"\0")[i:i + 512]
               for i in range(0, max(4096, (len(body) + 511) // 512 * 512), 512)]
              for _, body in streams]
    chains = [[] for _ in streams]
    sectors = []
    for index in range(max(map(len, chunks))):
        for stream, parts in enumerate(chunks):
            if index < len(parts):
                chains[stream].append(len(sectors) + 2)
                sectors.append(parts[index])
    end, free = 0xFFFFFFFE, 0xFFFFFFFF
    fat = [free] * 128
    fat[0], fat[1] = end, 0xFFFFFFFD
    for chain in chains:
        for index, sector in enumerate(chain):
            fat[sector] = chain[index + 1] if index + 1 < len(chain) else end

    def directory(name, kind, start=end, size=0, right=free, child=free):
        entry = bytearray(128)
        encoded = (name + "\0").encode("utf-16le")
        entry[:len(encoded)] = encoded
        struct.pack_into("<HBBIII", entry, 64, len(encoded), kind, 1, free, right, child)
        struct.pack_into("<IQ", entry, 116, start, size)
        return entry

    entries = [directory("Root Entry", 5, child=1)]
    for index, ((name, _), parts) in enumerate(zip(streams, chunks)):
        entries.append(directory(name, 2, chains[index][0], len(parts) * 512,
                                 right=index + 2 if index + 1 < len(streams) else free))
    header = bytearray(512)
    header[:8] = bytes.fromhex("d0cf11e0a1b11ae1")
    struct.pack_into("<HHHHH", header, 24, 0x003E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 0, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 1, *([free] * 108))
    return bytes(header) + b"".join(entries).ljust(512, b"\0") + struct.pack("<128I", *fat) + b"".join(sectors)


def cell_package(*, media=True, target="media/photo.png", external=False, orphan=False):
    stream = io.BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("xl/cellImages.xml", f'<cellImages><cellImage><cNvPr name="ID_A1"/>'
                         f'<blip xmlns:r="{REL}" r:embed="rId1"/></cellImage></cellImages>')
        mode = ' TargetMode="External"' if external else ""
        archive.writestr("xl/_rels/cellImages.xml.rels", f'<Relationships><Relationship Id="rId1" '
                         f'Type="{REL}/image" Target="{target}"{mode}/></Relationships>')
        if media:
            archive.writestr("xl/media/photo.png", image_bytes("blue"))
        if orphan:
            archive.writestr("xl/media/orphan.png", image_bytes("red"))
    return stream.getvalue()


class SpreadsheetContainerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.counter = 0

    def read(self, payload, name="任意名称.xls"):
        self.counter += 1
        source = self.root / name
        source.write_bytes(payload)
        units = prepare_units({"inventory": [{"path": source, "source_file": name,
                               "file_id": "f001", "kind": "spreadsheet"}]}, self.root / f"units-{self.counter}")
        self.assertEqual(source.read_bytes(), payload)
        return units

    def test_ole_wps_stream_keeps_each_cell_and_reused_photo_without_converter(self):
        with patch("audit_core.spreadsheet_photos._convert_legacy_workbook",
                   side_effect=AssertionError("native WPS stream must not require Excel")):
            units = self.read(ole_workbook(cell_package()))
        photos = [unit for unit in units if unit.get("photo_reference")]
        self.assertEqual([unit["locator"] for unit in photos],
                         ["陈列!R2C2（单元格图片）", "陈列!R3C2（单元格图片）"])
        self.assertTrue(all(not unit["limitations"] for unit in units))
        self.assertEqual([hashlib.sha256(unit["image"].read_bytes()).digest() for unit in photos],
                         [hashlib.sha256(image_bytes("blue")).digest()] * 2)
        facts = "\n".join(fact for unit in units for fact in unit["native_facts"])
        self.assertIn("C1=甲店", facts)
        self.assertIn("C1=乙店", facts)

    def test_plain_biff_still_reads_tables_and_keeps_missing_photo_sources(self):
        units = self.read(ole_workbook())
        photos = [unit for unit in units if unit.get("photo_reference")]
        self.assertEqual(len(photos), 2)
        self.assertTrue(all(unit["image"] is None for unit in photos))
        self.assertTrue(all("图片未随表格提供" in "".join(unit["limitations"]) for unit in photos))

    def test_embedded_archive_preserves_missing_and_orphan_photos(self):
        units = self.read(ole_workbook(cell_package(media=False, orphan=True)))
        photos = [unit for unit in units if unit.get("photo_reference")]
        self.assertEqual(len(photos), 3)
        self.assertTrue(all(unit["image"] is None for unit in photos[:2]))
        self.assertTrue(photos[2]["image"].is_file())
        self.assertIn("未能对应到工作表行列", "".join(photos[2]["limitations"]))

    def test_embedded_image_relationship_traversal_is_still_rejected(self):
        for target in ("../../outside.png", "/outside.png", "%2e%2e/%2e%2e/outside.png", "C:/outside.png"):
            with self.subTest(target=target), self.assertRaisesRegex(AuditError, "路径不安全"):
                self.read(ole_workbook(cell_package(target=target)))

    def test_external_image_is_unreadable_and_never_fetched(self):
        units = self.read(ole_workbook(cell_package(target="https://example.invalid/p.png", external=True)))
        photos = [unit for unit in units if unit.get("photo_reference") and "!R" in unit["locator"]]
        self.assertEqual(len(photos), 2)
        self.assertTrue(all(unit["image"] is None for unit in photos))

    def test_embedded_zip_member_traversal_is_still_rejected(self):
        stream = io.BytesIO(cell_package())
        with ZipFile(stream, "a") as archive:
            archive.writestr("../outside.png", image_bytes())
        with self.assertRaises(AuditError):
            self.read(ole_workbook(stream.getvalue()))

    def test_embedded_zip_limits_and_case_collisions_are_still_rejected(self):
        with patch("audit_core.archive_input.MAX_MEMBER_BYTES", 8), self.assertRaises(AuditError):
            self.read(ole_workbook(cell_package()))
        stream = io.BytesIO(cell_package())
        with ZipFile(stream, "a") as archive:
            archive.writestr("xl/cellimages.xml", "<cellImages/>")
        with self.assertRaisesRegex(AuditError, "大小写冲突"):
            self.read(ole_workbook(stream.getvalue()))

    def xlsx_with_custom_xml(self):
        book = Workbook()
        book.active.append(["金额", 5480])
        book.active.add_image(SheetImage(io.BytesIO(image_bytes())), "C2")
        stream = io.BytesIO()
        book.save(stream)
        book.close()
        output = io.BytesIO()
        with ZipFile(stream) as original, ZipFile(output, "w", ZIP_DEFLATED) as archive:
            for info in original.infolist():
                payload = original.read(info)
                if info.filename == "xl/_rels/workbook.xml.rels":
                    root = ET.fromstring(payload)
                    ET.SubElement(root, "Relationship", {"Id": "custom1", "Type": f"{REL}/customXml",
                                                          "Target": "../customXml/item1.xml"})
                    payload = ET.tostring(root)
                archive.writestr(info.filename, payload)
            archive.writestr("customXml/item1.xml", "<data/>")
        return output.getvalue()

    def test_valid_non_image_custom_xml_does_not_block_tables_or_floating_photos(self):
        units = self.read(self.xlsx_with_custom_xml(), "正常工作簿.xlsx")
        photos = [unit for unit in units if unit.get("photo_reference")]
        self.assertEqual(len(photos), 1)
        self.assertTrue(photos[0]["image"].is_file())
        self.assertIn("R2C3", photos[0]["locator"])
        self.assertIn("C2=5480", "\n".join(fact for unit in units for fact in unit["native_facts"]))

    def test_relevant_workbook_path_and_duplicate_relationships_remain_rejected(self):
        for duplicate in (False, True):
            original = io.BytesIO(self.xlsx_with_custom_xml())
            output = io.BytesIO()
            with ZipFile(original) as before, ZipFile(output, "w") as after:
                for info in before.infolist():
                    payload = before.read(info)
                    if info.filename == "xl/_rels/workbook.xml.rels":
                        root = ET.fromstring(payload)
                        worksheet = next(item for item in root if item.get("Type") == f"{REL}/worksheet")
                        if duplicate:
                            ET.SubElement(root, "Relationship", {**worksheet.attrib, "Type": f"{REL}/customXml"})
                        else:
                            worksheet.set("Target", "../../outside.xml")
                        payload = ET.tostring(root)
                    after.writestr(info.filename, payload)
            with self.subTest(duplicate=duplicate), self.assertRaises(AuditError):
                self.read(output.getvalue(), "异常工作簿.xlsx")


if __name__ == "__main__":
    unittest.main()
