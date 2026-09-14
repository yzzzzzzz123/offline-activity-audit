from __future__ import annotations

import io
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree

from openpyxl import Workbook
from PIL import Image

from audit_core.archive_input import MaterialInputError, prepare_cases
from audit_core.common import AuditError
from audit_core.legacy_activity_workbook import (
    LegacyWorkbookMaterialError,
    extract_activity_return_workbook,
)


HEADERS = ["活动周期", "陈列图片", "客户编码", "客户名称", "费用代垫经销商", "费用代垫经销商编码"]
FORMULA = '=DISPIMG("ID_A1",1)'
DRAWING = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
MAIN = "http://schemas.openxmlformats.org/drawingml/2006/main"
RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


class LegacyActivityWorkbookTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "活动返图.xls"
        self.source.write_bytes(b"legacy-fixture")
        self.converted = self.root / "converted.xlsx"
        self.counter = 0

    def converted_workbook(
        self, *, formulas: list[str] | None = None, tables: int = 1,
        targets: list[tuple[str, str]] | None = None,
        include_relationships: bool = True, include_media: bool = True,
    ) -> None:
        workbook = Workbook()
        for index in range(max(tables, 1)):
            sheet = workbook.active if index == 0 else workbook.create_sheet()
            if tables:
                sheet.append(HEADERS)
                for formula in [FORMULA] if formulas is None else formulas:
                    sheet.append(["工作簿正文不可进入错误", formula, "C001", "客户名称不可进入错误", "经销商甲", "D001"])
            else:
                sheet.append(["错误表头"])
        workbook.save(self.converted)
        workbook.close()
        if not include_relationships:
            return
        targets = [("ID_A1", "media/photo.png")] if targets is None else targets
        images = ElementTree.Element("cellImages")
        relationships = ElementTree.Element("Relationships")
        for index, (image_id, target) in enumerate(targets, 1):
            cell = ElementTree.SubElement(images, "cellImage")
            ElementTree.SubElement(cell, f"{{{DRAWING}}}cNvPr", {"name": image_id})
            ElementTree.SubElement(cell, f"{{{MAIN}}}blip", {f"{{{RELATIONSHIP}}}embed": f"rId{index}"})
            ElementTree.SubElement(relationships, "Relationship", {"Id": f"rId{index}", "Target": target})
        with zipfile.ZipFile(self.converted, "a") as archive:
            archive.writestr("xl/cellimages.xml", ElementTree.tostring(images))
            archive.writestr("xl/_rels/cellimages.xml.rels", ElementTree.tostring(relationships))
            if include_media:
                stream = io.BytesIO()
                Image.new("RGB", (6, 6), "white").save(stream, format="PNG")
                archive.writestr("xl/media/photo.png", stream.getvalue())

    def extract(self) -> dict:
        self.counter += 1
        with patch("audit_core.legacy_activity_workbook._convert_legacy_workbook",
                   side_effect=lambda _source, target: shutil.copyfile(self.converted, target)):
            return extract_activity_return_workbook(self.source, self.root / f"photos-{self.counter}")

    def assert_material_error(self, pattern: str) -> None:
        with self.assertRaisesRegex(LegacyWorkbookMaterialError, pattern) as caught:
            self.extract()
        for sensitive in ("工作簿正文不可进入错误", "客户名称不可进入错误", "ID_A1"):
            self.assertNotIn(sensitive, str(caught.exception))

    def test_valid_converted_workbook_keeps_photo_row_binding(self) -> None:
        self.converted_workbook()
        result = self.extract()
        self.assertEqual(result["record_count"], 1)
        self.assertEqual(result["records"][0]["excel_row"], 2)
        self.assertTrue(Path(result["records"][0]["photo_file"]).is_file())

    def test_missing_or_multiple_detail_tables_are_material_errors(self) -> None:
        for count in (0, 2):
            with self.subTest(tables=count):
                self.converted_workbook(tables=count)
                self.assert_material_error(f"明细表应唯一，实际{count}个")

    def test_invalid_photo_formula_reports_row_without_cell_body(self) -> None:
        formula = '=SECRET("私密工作簿正文")'
        self.converted_workbook(formulas=[formula])
        with self.assertRaisesRegex(LegacyWorkbookMaterialError, "第2行图片公式不可核验") as caught:
            self.extract()
        self.assertNotIn(formula, str(caught.exception))
        self.assertNotIn("私密工作簿正文", str(caught.exception))

    def test_reused_photo_id_reports_row_without_id_body(self) -> None:
        self.converted_workbook(formulas=[FORMULA, FORMULA])
        self.assert_material_error("第3行重复使用图片ID")

    def test_missing_wps_relationships_is_a_material_error(self) -> None:
        self.converted_workbook(include_relationships=False)
        self.assert_material_error("缺少WPS单元格图片关系")

    def test_unmapped_photo_id_is_a_material_error(self) -> None:
        self.converted_workbook(targets=[("ID_B2", "media/photo.png")])
        self.assert_material_error("第2行图片ID没有媒体文件")

    def test_relationship_with_missing_media_file_is_a_material_error(self) -> None:
        self.converted_workbook(include_media=False)
        self.assert_material_error("第2行引用的媒体文件未提交")

    def test_duplicate_media_ids_are_material_errors(self) -> None:
        self.converted_workbook(targets=[("ID_A1", "media/photo.png"), ("ID_A1", "media/photo.png")])
        self.assert_material_error("图片ID重复")

    def test_unbound_extra_images_are_a_material_error(self) -> None:
        self.converted_workbook(targets=[("ID_A1", "media/photo.png"), ("ID_B2", "media/photo.png")])
        self.assert_material_error("图片未逐行完整绑定：明细1行，媒体2份")

    def test_unsafe_relationship_paths_remain_fatal(self) -> None:
        for target in ("../outside.png", "/tmp/outside.png", "media/../outside.png"):
            with self.subTest(target=target):
                self.converted_workbook(targets=[("ID_A1", target)])
                with self.assertRaisesRegex(AuditError, "图片路径不安全") as caught:
                    self.extract()
                self.assertNotIsInstance(caught.exception, LegacyWorkbookMaterialError)

    def test_image_size_limit_remains_fatal(self) -> None:
        self.converted_workbook()
        with patch("audit_core.legacy_activity_workbook.MAX_IMAGE_BYTES", 1):
            with self.assertRaisesRegex(AuditError, "图片尺寸异常") as caught:
                self.extract()
        self.assertNotIsInstance(caught.exception, LegacyWorkbookMaterialError)

    def test_conversion_infrastructure_error_remains_fatal(self) -> None:
        failure = AuditError("读取活动返图.xls需要 Windows PowerShell 和 Microsoft Excel")
        with patch("audit_core.legacy_activity_workbook._convert_legacy_workbook", side_effect=failure):
            with self.assertRaises(AuditError) as caught:
                extract_activity_return_workbook(self.source, self.root / "photos")
        self.assertIs(caught.exception, failure)
        self.assertNotIsInstance(caught.exception, LegacyWorkbookMaterialError)

    def test_archive_routing_only_converts_the_typed_material_error(self) -> None:
        inputs = self.root / "input"
        inputs.mkdir()
        with zipfile.ZipFile(inputs / "SQ-自采赠品物料.zip", "w") as archive:
            for name in ("促销协议.jpg", "结算单.jpg", "物料购买凭证.jpg", "销售pos.jpg", "活动返图.xls"):
                archive.writestr(name, b"fixture")
        material = LegacyWorkbookMaterialError("活动返图工作簿第2行图片公式不可核验")
        with patch("audit_core.archive_input.extract_activity_return_workbook", side_effect=material):
            with self.assertRaisesRegex(MaterialInputError, "第2行图片公式不可核验") as caught:
                prepare_cases(inputs, self.root / "material-prepared")
        self.assertIs(caught.exception.__cause__, material)
        fatal = AuditError("活动返图工作簿图片路径不安全")
        with patch("audit_core.archive_input.extract_activity_return_workbook", side_effect=fatal):
            with self.assertRaises(AuditError) as caught:
                prepare_cases(inputs, self.root / "fatal-prepared")
        self.assertIs(caught.exception, fatal)
        self.assertNotIsInstance(caught.exception, MaterialInputError)


if __name__ == "__main__":
    unittest.main()
