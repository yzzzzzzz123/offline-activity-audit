from __future__ import annotations

import io
import errno
import stat
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch

from PIL import Image
from pypdf.errors import DependencyError, PdfReadError

from audit_core.archive_input import (
    ArchiveClassificationError,
    ArchiveInputError,
    MaterialInputError,
    _classify_archive,
    discover_archives,
    prepare_cases,
    scenario_from_archive_name,
    scenarios_from_archive_name,
)
from audit_core.common import AuditError
from audit_core.legacy_activity_workbook import LegacyWorkbookMaterialError
from audit_core.material_intake import _scenario_hint, _visual_limitations, prepare_material_diagnosis


def _image_bytes(color: str = "white") -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(output, format="PNG")
    return output.getvalue()


def _zip_bytes(files: dict[str, bytes]) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    return output.getvalue()


class MaterialIntakeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.inputs = self.root / "input"
        self.inputs.mkdir()
        self.prepared = self.root / "prepared"

    def bundle(self, files: dict[str, bytes], name: str = "ai-pack-8.zip") -> Path:
        path = self.inputs / name
        path.write_bytes(_zip_bytes(files))
        return path

    def diagnose(self) -> dict:
        return prepare_material_diagnosis(
            self.inputs, self.prepared, original_error="原始材料角色无法绑定",
        )

    def test_unknown_name_has_classification_error_and_explicit_low_level_inventory_remains_available(self) -> None:
        self.bundle({"IMG_1.jpg": _image_bytes()})
        with self.assertRaises(ArchiveClassificationError):
            discover_archives(self.inputs)
        result = self.diagnose()
        self.assertEqual(result["kind"], "material_diagnostic")
        self.assertEqual(result["source_archives"], ["ai-pack-8.zip"])
        self.assertIsNone(result["archives"][0]["scenario_hint"])
        self.assertEqual(result["archives"][0]["inventory"][0]["file_id"], "a001-f0001")

    def test_normal_and_diagnostic_routing_share_all_ten_zip_name_types(self) -> None:
        for label, scenario in (
            ("人员激励", "personnel_incentive"), ("堆头陈列", "promotional_display"),
            ("海报物料", "poster_material"), ("其他费用", "other_expense"),
            ("维护费用", "maintenance_fee"), ("额外搭赠", "giveaway_promotion"),
            ("价格补差", "price_difference_support"), ("POS达标激励", "pos_target_incentive"),
            ("条码费", "entry_fee"), ("进场费", "entry_fee"), ("自采赠品物料", "self_procured_gift_material"),
        ):
            with self.subTest(label=label):
                name = f"HX202601160053-客户-{label}-9.zip"
                self.assertEqual(scenario_from_archive_name(name), scenario)
                self.assertEqual(_scenario_hint(name), scenario)

    def test_unmarked_complete_shapes_cannot_select_personnel_or_display(self) -> None:
        for name, files in (
            ("ai-pack-display.zip", {"销售.xlsx": b"xlsx", "合同.pdf": b"pdf", "现场.png": _image_bytes()}),
            ("ai-pack-personnel.zip", {"销售.xlsx": b"xlsx", "结算单.png": _image_bytes(), "转账.png": _image_bytes()}),
        ):
            with self.subTest(name=name):
                source = self.bundle(files, name)
                with self.assertRaisesRegex(ArchiveClassificationError, "ZIP 名称缺少唯一分类标记"):
                    _classify_archive(source)
                self.assertIsNone(_scenario_hint(name))

    def test_generic_expense_names_select_maintenance_in_normal_and_diagnostic_intake(self) -> None:
        source = self.bundle({"POS数据.jpg": _image_bytes()}, "客户费用申请.zip")
        self.assertEqual(discover_archives(self.inputs)["maintenance_fee"]["path"], source)
        case = prepare_cases(self.inputs, self.root / "strict")["maintenance_fee"]
        self.assertEqual(case["scenario"], "maintenance_fee")
        self.assertEqual(self.diagnose()["archives"][0]["scenario_hint"], "maintenance_fee")

    def test_filename_keywords_preserve_specific_types_and_ambiguous_names(self) -> None:
        for name, expected in (
            ("费用.zip", "maintenance_fee"),
            ("客户活动费用申请.ZIP", "maintenance_fee"),
            ("客户物料申请.zip", "poster_material"),
            ("物料制作费用.zip", "poster_material"),
            ("其他费用.zip", "other_expense"),
            ("自采赠品物料费用.zip", "self_procured_gift_material"),
            ("人员激励费用.zip", "personnel_incentive"),
            ("堆头费用.zip", "promotional_display"),
            ("人员激励-堆头费用.zip", None),
            ("维护费用-物料.zip", None),
            ("资料包.zip", None),
            ("费用/资料包.zip", None),
        ):
            with self.subTest(name=name):
                self.assertEqual(scenario_from_archive_name(name), expected)
                self.assertEqual(_scenario_hint(name), expected)

    def test_named_maintenance_cannot_be_reclassified_from_display_shape(self) -> None:
        source = self.bundle({
            "销售.xlsx": b"xlsx", "合同.pdf": b"pdf", "现场.png": _image_bytes(),
        }, "HX202601160053-广州南雄维护费用申请-9.zip")
        with self.assertRaisesRegex(MaterialInputError, "材料组成不满足 ZIP 名称指定") as caught:
            _classify_archive(source)
        self.assertNotIsInstance(caught.exception, ArchiveClassificationError)
        archive = self.diagnose()["archives"][0]
        self.assertEqual(archive["scenario_hint"], "maintenance_fee")
        self.assertEqual(len(archive["inventory"]), 3)

    def test_named_personnel_and_display_do_not_route_to_the_other_shape(self) -> None:
        for name, files in (
            ("人员激励.zip", {"销售.xlsx": b"xlsx", "合同.pdf": b"pdf", "现场.png": _image_bytes()}),
            ("堆头陈列.zip", {"销售.xlsx": b"xlsx", "结算单.png": _image_bytes(), "转账.png": _image_bytes()}),
        ):
            with self.subTest(name=name), self.assertRaises(MaterialInputError):
                _classify_archive(self.bundle(files, name))

    def test_multiple_zip_name_markers_are_not_resolved_by_document_shape(self) -> None:
        source = self.bundle({
            "销售.xlsx": b"xlsx", "合同.pdf": b"pdf", "现场.png": _image_bytes(),
        }, "维护费用-堆头陈列.zip")
        with self.assertRaisesRegex(ArchiveClassificationError, "ZIP 名称缺少唯一分类标记"):
            _classify_archive(source)
        self.assertIsNone(self.diagnose()["archives"][0]["scenario_hint"])
        self.assertEqual(set(scenarios_from_archive_name(source.name)), {"maintenance_fee", "promotional_display"})

    def test_self_procured_name_only_suppresses_its_embedded_generic_material_marker(self) -> None:
        self.assertEqual(scenario_from_archive_name("自采赠品物料.zip"), "self_procured_gift_material")
        self.assertIsNone(scenario_from_archive_name("自采赠品物料-海报.zip"))
        self.assertIsNone(scenario_from_archive_name("自采物料-展示道具.zip"))

    def test_two_settlements_preserve_both_candidates(self) -> None:
        self.bundle({
            "结算单.jpg": _image_bytes(),
            "服务费结算单.jpg": _image_bytes("blue"),
            "POS数据.jpg": _image_bytes("red"),
        }, "HX202601160053-广州南雄维护费用申请-9.zip")
        with self.assertRaisesRegex(MaterialInputError, "结算单应最多1份"):
            prepare_cases(self.inputs, self.root / "strict")
        archive = self.diagnose()["archives"][0]
        self.assertEqual(archive["scenario_hint"], "maintenance_fee")
        names = {item["source_file"] for item in archive["inventory"]}
        self.assertEqual(names, {"结算单.jpg", "服务费结算单.jpg", "POS数据.jpg"})
        self.assertNotIn("settlement_document", archive)

    def test_empty_archive_still_provides_diagnostic_inventory(self) -> None:
        self.bundle({})
        self.assertEqual(self.diagnose()["archives"][0]["inventory"], [])

    def test_same_basename_in_different_directories_and_same_bytes_are_retained(self) -> None:
        content = _image_bytes()
        self.bundle({"店甲/结算单.jpg": content, "店乙/结算单.jpg": content})
        files = self.diagnose()["archives"][0]["inventory"]
        self.assertEqual({item["source_file"] for item in files}, {"店甲/结算单.jpg", "店乙/结算单.jpg"})
        self.assertEqual(len({item["file_id"] for item in files}), 2)
        self.assertEqual(len({item["sha256"] for item in files}), 1)

    def test_nested_zip_preserves_source_chain_and_global_ids(self) -> None:
        self.bundle({"附件.zip": _zip_bytes({"照片/IMG.png": _image_bytes()})}, "1.zip")
        self.bundle({"IMG.png": _image_bytes()}, "2.zip")
        result = self.diagnose()
        files = [item for archive in result["archives"] for item in archive["inventory"]]
        self.assertEqual(len({item["file_id"] for item in files}), 3)
        nested = next(item for item in files if item["source_file"] == "附件.zip/照片/IMG.png")
        self.assertEqual(nested["parent_source"], "附件.zip")
        self.assertEqual(nested["kind"], "visual")
        self.assertEqual(nested["limitations"], [])

    def test_unreadable_visuals_and_unsupported_files_are_reportable(self) -> None:
        self.bundle({"坏图片.jpg": b"invalid", "坏文档.pdf": b"invalid", "证明.docx": b"document"})
        files = self.diagnose()["archives"][0]["inventory"]
        self.assertEqual(len(files), 3)
        self.assertTrue(all(item["limitations"] for item in files))
        self.assertEqual(next(item for item in files if item["suffix"] == ".docx")["kind"], "unsupported")

    def test_visual_validation_preserves_environment_and_resource_failures(self) -> None:
        for suffix, target in (
            (".jpg", "audit_core.material_intake.Image.open"),
            (".pdf", "audit_core.material_intake.PdfReader"),
        ):
            for error in (
                PermissionError("access denied"), FileNotFoundError("source disappeared"),
                OSError(errno.EIO, "disk error"), OSError(errno.ENOSPC, "disk full"),
                TimeoutError("storage timeout"), IsADirectoryError("source replaced by directory"),
                MemoryError("memory exhausted"), RecursionError("recursion exhausted"),
                ModuleNotFoundError("decoder dependency missing"), DependencyError("cryptography missing"),
                RuntimeError("unexpected decoder failure"),
            ):
                with self.subTest(suffix=suffix, error=type(error).__name__):
                    with patch(target, side_effect=error), self.assertRaises(type(error)) as caught:
                        _visual_limitations(self.root / ("source" + suffix))
                    self.assertIs(caught.exception, error)

    def test_visual_format_errors_remain_reportable(self) -> None:
        for suffix, target, error in (
            (".jpg", "audit_core.material_intake.Image.open", OSError("image file is truncated")),
            (".jpg", "audit_core.material_intake.Image.open", SyntaxError("bad PNG chunk")),
            (".pdf", "audit_core.material_intake.PdfReader", PdfReadError("damaged PDF")),
        ):
            with self.subTest(suffix=suffix, error=type(error).__name__), patch(target, side_effect=error):
                self.assertTrue(_visual_limitations(self.root / ("source" + suffix)))

    def test_spreadsheets_are_not_passed_as_visual_evidence(self) -> None:
        self.bundle({"销售.xlsx": b"spreadsheet", "活动返图.xls": b"not confirmed legacy structure"})
        with patch("audit_core.material_intake.extract_activity_return_workbook") as extractor:
            files = self.diagnose()["archives"][0]["inventory"]
        extractor.assert_not_called()
        self.assertTrue(all(item["kind"] == "spreadsheet" for item in files))
        self.assertTrue(next(item for item in files if item["suffix"] == ".xls")["limitations"])

    def test_confirmed_activity_workbook_exposes_only_derived_photos(self) -> None:
        structure = bytes.fromhex("d0cf11e0a1b11ae1") + "|".join((
            "活动周期", "陈列图片", "客户编码", "客户名称",
            "费用代垫经销商", "费用代垫经销商编码", "DISPIMG",
        )).encode("utf-16le")
        self.bundle({"客户误命名.xls": structure})

        def extract(_: Path, destination: Path) -> dict:
            destination.mkdir(parents=True)
            photo = destination / "活动返图-row-02.png"
            photo.write_bytes(_image_bytes())
            return {"records": [{"photo_file": photo}]}

        with patch("audit_core.material_intake.extract_activity_return_workbook", side_effect=extract):
            files = self.diagnose()["archives"][0]["inventory"]
        self.assertEqual([item["kind"] for item in files], ["spreadsheet", "visual"])
        self.assertEqual(files[1]["source_file"], "客户误命名.xls/活动返图-row-02.png")
        self.assertEqual(files[1]["parent_source"], "客户误命名.xls")

    def test_legacy_workbook_material_error_keeps_other_visuals_and_removes_partial_conversion(self) -> None:
        self.bundle({
            "活动返图.xls": b"mock confirmed activity workbook",
            "合同.png": _image_bytes(), "结算单.png": _image_bytes("red"),
        })
        attempted_roots = []

        def broken_material(_: Path, destination: Path) -> dict:
            attempted_roots.append(destination)
            destination.mkdir(parents=True)
            (destination / "partial.xlsx").write_bytes(b"partial converted workbook")
            (destination / "partial-photo.png").write_bytes(_image_bytes())
            raise LegacyWorkbookMaterialError("活动返图工作簿第3行重复使用图片ID")

        with (
            patch("audit_core.material_intake._is_activity_return_workbook", return_value=True),
            patch("audit_core.material_intake.extract_activity_return_workbook", side_effect=broken_material),
        ):
            files = self.diagnose()["archives"][0]["inventory"]
        self.assertEqual(len(files), 3)
        visual_files = [item for item in files if item["kind"] == "visual"]
        self.assertEqual({item["source_file"] for item in visual_files}, {"合同.png", "结算单.png"})
        self.assertTrue(all(item["path"].is_file() and not item["limitations"] for item in visual_files))
        workbook = next(item for item in files if item["kind"] == "spreadsheet")
        self.assertEqual(workbook["source_file"], "活动返图.xls")
        self.assertTrue(workbook["path"].is_file())
        self.assertIn("第3行重复使用图片ID", "；".join(workbook["limitations"]))
        self.assertIn("照片无法确认完整绑定", "；".join(workbook["limitations"]))
        self.assertEqual(len(attempted_roots), 1)
        self.assertFalse(attempted_roots[0].exists())
        self.assertFalse(list((self.prepared / "material_diagnostic").rglob("partial*")))

    def test_legacy_workbook_dependency_and_security_errors_abort_and_clean_diagnosis(self) -> None:
        self.bundle({"活动返图.xls": b"mock confirmed activity workbook", "合同.png": _image_bytes()})
        for message in ("读取活动返图.xls需要 Windows PowerShell 和 Microsoft Excel", "活动返图工作簿图片路径不安全"):
            with self.subTest(message=message):
                failure = AuditError(message)

                def broken_execution(_: Path, destination: Path) -> dict:
                    destination.mkdir(parents=True)
                    (destination / "partial.xlsx").write_bytes(b"partial converted workbook")
                    raise failure

                with (
                    patch("audit_core.material_intake._is_activity_return_workbook", return_value=True),
                    patch("audit_core.material_intake.extract_activity_return_workbook", side_effect=broken_execution),
                    self.assertRaises(AuditError) as caught,
                ):
                    self.diagnose()
                self.assertIs(caught.exception, failure)
                self.assertFalse((self.prepared / "material_diagnostic").exists())

    def test_unsafe_nested_archive_blocks_before_visual_parsing(self) -> None:
        self.bundle({"IMG.png": _image_bytes(), "恶意.zip": _zip_bytes({"../逃逸.jpg": _image_bytes()})})
        with patch("audit_core.material_intake._visual_limitations") as read_visual:
            with self.assertRaises(ArchiveInputError) as caught:
                self.diagnose()
        self.assertNotIsInstance(caught.exception, MaterialInputError)
        read_visual.assert_not_called()
        self.assertFalse((self.prepared / "material_diagnostic").exists())
        self.assertFalse((self.root / "逃逸.jpg").exists())

    def test_all_archives_are_safe_before_any_visual_parsing(self) -> None:
        self.bundle({"IMG.png": _image_bytes()}, "1.zip")
        self.bundle({"../escape.png": _image_bytes()}, "2.zip")
        with patch("audit_core.material_intake._visual_limitations") as read_visual:
            with self.assertRaises(ArchiveInputError):
                self.diagnose()
        read_visual.assert_not_called()

    def test_symbolic_link_member_is_a_security_error(self) -> None:
        path = self.inputs / "unsafe.zip"
        with zipfile.ZipFile(path, "w") as archive:
            member = zipfile.ZipInfo("link.jpg")
            member.create_system = 3
            member.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(member, "../../outside")
        with self.assertRaises(ArchiveInputError) as caught:
            self.diagnose()
        self.assertNotIsInstance(caught.exception, MaterialInputError)

    def test_same_archive_path_collision_is_a_security_error(self) -> None:
        path = self.inputs / "unsafe.zip"
        with zipfile.ZipFile(path, "w") as archive, warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            archive.writestr("same.jpg", _image_bytes())
            archive.writestr("same.jpg", _image_bytes("red"))
        with self.assertRaisesRegex(ArchiveInputError, "路径重复") as caught:
            self.diagnose()
        self.assertNotIsInstance(caught.exception, MaterialInputError)

    def test_case_colliding_archive_paths_are_not_material_role_duplicates(self) -> None:
        self.bundle({"IMG.jpg": _image_bytes(), "img.JPG": _image_bytes()})
        with self.assertRaisesRegex(ArchiveInputError, "大小写冲突"):
            self.diagnose()

    def test_corrupt_archive_never_becomes_a_business_diagnosis(self) -> None:
        (self.inputs / "corrupt.zip").write_bytes(b"not a zip")
        with self.assertRaises(ArchiveInputError) as caught:
            self.diagnose()
        self.assertNotIsInstance(caught.exception, MaterialInputError)

    def test_crc_failure_never_reaches_visual_reader(self) -> None:
        path = self.bundle({"IMG.png": _image_bytes()})
        payload = bytearray(path.read_bytes())
        with zipfile.ZipFile(path) as archive:
            info = archive.infolist()[0]
            offset = info.header_offset + 30 + len(info.filename.encode()) + len(info.extra)
        payload[offset] ^= 0xFF
        path.write_bytes(payload)
        with patch("audit_core.material_intake._visual_limitations") as read_visual:
            with self.assertRaises(zipfile.BadZipFile):
                self.diagnose()
        read_visual.assert_not_called()

    def test_member_budget_is_cumulative_across_nested_archives(self) -> None:
        self.bundle({"nested.zip": _zip_bytes({"1.png": _image_bytes(), "2.png": _image_bytes()})})
        with patch("audit_core.material_intake.MAX_DIAGNOSTIC_FILES", 2):
            with self.assertRaisesRegex(ArchiveInputError, "总成员数超限"):
                self.diagnose()

    def test_size_budget_is_cumulative_across_archives(self) -> None:
        self.bundle({"1.txt": b"123456"}, "1.zip")
        self.bundle({"2.txt": b"123456"}, "2.zip")
        with patch("audit_core.material_intake.MAX_DIAGNOSTIC_BYTES", 10):
            with self.assertRaisesRegex(ArchiveInputError, "解压总量超限"):
                self.diagnose()

    def test_nested_archive_depth_is_bounded(self) -> None:
        nested = _zip_bytes({"photo.jpg": _image_bytes()})
        for _ in range(5):
            nested = _zip_bytes({"nested.zip": nested})
        (self.inputs / "nested.zip").write_bytes(nested)
        with self.assertRaisesRegex(ArchiveInputError, "嵌套归档层数超限"):
            self.diagnose()

    def test_rar_dependency_failure_is_not_recoverable(self) -> None:
        self.bundle({"照片.rar": b"rar"})
        with patch("audit_core.material_intake.shutil.which", return_value=None):
            with self.assertRaisesRegex(ArchiveInputError, "系统未提供") as caught:
                self.diagnose()
        self.assertNotIsInstance(caught.exception, MaterialInputError)

    def test_regular_preparation_remains_unchanged(self) -> None:
        self.bundle({
            "销售.xlsx": b"xlsx", "结算单.png": _image_bytes(), "转账.png": _image_bytes(),
        }, "人员激励.zip")
        case = prepare_cases(self.inputs, self.root / "strict")["personnel_incentive"]
        self.assertEqual(case["sales_excel"].name, "销售.xlsx")
        self.assertEqual(case["settlement_image"].name, "结算单.png")
        self.assertEqual([path.name for path in case["transfer_images"]], ["转账.png"])

    def test_retained_maintenance_package_can_be_prepared_read_only(self) -> None:
        project = Path(__file__).resolve().parents[1]
        original = project / "input/8c3abb5ea4d2261de03d140b/HX202601160053-广州南雄维护费用申请-9.zip"
        if not original.is_file():
            self.skipTest("本机没有保留的客户维护费用包")
        before = original.read_bytes()
        with self.assertRaisesRegex(MaterialInputError, "结算单应最多1份") as caught:
            prepare_cases(original.parent, self.root / "strict")
        result = prepare_material_diagnosis(
            original.parent, self.prepared, original_error=caught.exception,
        )
        archive = result["archives"][0]
        self.assertEqual(archive["scenario_hint"], "maintenance_fee")
        names = [item["source_file"] for item in archive["inventory"]]
        self.assertTrue(any(name.endswith("服务费结算单.jpg") for name in names))
        self.assertTrue(any(name.endswith("/结算单.jpg") or name == "结算单.jpg" for name in names))
        with zipfile.ZipFile(original, metadata_encoding="gbk") as source:
            self.assertEqual(
                len([item for item in archive["inventory"] if item["parent_source"] is None]),
                len([item for item in source.infolist() if not item.is_dir()]),
            )
        self.assertTrue(any(item["parent_source"] and ".rar/" in item["source_file"] for item in archive["inventory"]))
        self.assertEqual(original.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
