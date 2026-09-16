from __future__ import annotations

from audit_core.extraction_chain import render_prompt

from copy import deepcopy
import errno
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from pypdf.errors import DependencyError, PdfReadError

from audit_core.common import AuditError
from audit_core import material_diagnostic_ai as diagnostic


def _document(file_id: str, document_type: str = "settlement", **changes) -> dict:
    value = {
        "file_id": file_id, "document_type": document_type, "confidence": "high",
        "title": "可见结算单", "document_number": "JS001", "page_number": None,
        "total_pages": None, "visible_facts": ["可见结算用途"], "limitations": [],
    }
    value.update(changes)
    return value


def _evidence(file_ids: list[str], archive_id: str = "a001") -> dict:
    return {
        "schema_version": "1.0",
        "archives": [{
            "archive_id": archive_id, "scenario_candidates": [],
            "documents": [_document(file_id) for file_id in file_ids],
        }],
    }


def _inventory(file_id: str, path: Path, *, kind: str = "visual", limitations=()) -> dict:
    return {
        "file_id": file_id, "source_file": "客户原始目录/" + path.name,
        "path": path, "suffix": path.suffix.lower(), "kind": kind,
        "limitations": list(limitations),
    }


def _case(inventory: list[dict]) -> dict:
    return {"archives": [{"archive_id": "a001", "inventory": inventory}]}


class MaterialDiagnosticAITests(unittest.TestCase):
    def test_generation_schema_declares_types_for_enum_and_const_fields(self) -> None:
        schema = json.loads(diagnostic.SCHEMA.read_text(encoding="utf-8"))

        def check(node):
            if isinstance(node, dict):
                if "enum" in node or "const" in node:
                    self.assertIn("type", node, "Structured output generation requires an explicit field type")
                for value in node.values():
                    check(value)
            elif isinstance(node, list):
                for value in node:
                    check(value)

        check(schema)

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_root = self.root / "private-submitted-input"
        self.input_root.mkdir()
        self.staging = self.root / "staging"
        self.staging.mkdir()

    def photo(self, name: str) -> Path:
        path = self.input_root / name
        Image.new("RGB", (8, 8), "blue").save(path)
        return path

    def test_known_unreadable_visual_is_never_opened_or_attached(self) -> None:
        for suffix in (".jpg", ".pdf"):
            with self.subTest(suffix=suffix):
                item = _inventory("a001-f0001", self.input_root / ("不能读取" + suffix),
                                  limitations=["来源已标记为损坏", "无法安全读取"])
                with (
                    patch.object(diagnostic.Image, "open") as image_open,
                    patch.object(diagnostic, "PdfReader") as pdf_reader,
                    patch.object(diagnostic.shutil, "copy2") as copy,
                ):
                    units = diagnostic._visual_units(item, self.staging)
                image_open.assert_not_called()
                pdf_reader.assert_not_called()
                copy.assert_not_called()
                self.assertEqual(units[0]["images"], [])
                self.assertEqual(units[0]["limitation"], "来源已标记为损坏；无法安全读取")

    def test_readable_picture_is_copied_with_stable_id_and_original_is_unchanged(self) -> None:
        source = self.photo("客户错误命名.png")
        before = source.read_bytes()
        units = diagnostic._visual_units(_inventory("a001-f0001", source), self.staging)
        target = self.staging / "a001-f0001.png"
        self.assertEqual(units[0]["images"], [target])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(source.read_bytes(), before)
        self.assertNotIn("path", units[0])

    def test_newly_detected_broken_picture_has_no_attachment(self) -> None:
        source = self.input_root / "看似照片.jpg"
        source.write_bytes(b"not an encoded image")
        with patch.object(diagnostic.shutil, "copy2") as copy:
            units = diagnostic._visual_units(_inventory("a001-f0001", source), self.staging)
        copy.assert_not_called()
        self.assertEqual(units[0]["images"], [])
        self.assertIn("图片编码损坏", units[0]["limitation"])

    def test_model_staging_preserves_source_access_dependency_and_resource_failures(self) -> None:
        for suffix, target in ((".jpg", "Image.open"), (".pdf", "PdfReader")):
            for error in (
                PermissionError("access denied"), FileNotFoundError("source disappeared"),
                OSError(errno.EIO, "disk error"), OSError(errno.ENOSPC, "disk full"),
                TimeoutError("storage timeout"), IsADirectoryError("source replaced by directory"),
                MemoryError("memory exhausted"), RecursionError("recursion exhausted"),
                ModuleNotFoundError("decoder dependency missing"), DependencyError("cryptography missing"),
                RuntimeError("unexpected decoder failure"),
            ):
                with self.subTest(suffix=suffix, error=type(error).__name__):
                    item = _inventory("a001-f0001", self.input_root / ("source" + suffix))
                    with patch("audit_core.material_diagnostic_ai." + target, side_effect=error):
                        with self.assertRaises(type(error)) as caught:
                            diagnostic._visual_units(item, self.staging)
                    self.assertIs(caught.exception, error)

    def test_pdf_render_disk_failure_is_execution_failure_not_customer_corruption(self) -> None:
        failure = OSError(errno.ENOSPC, "temporary disk full")
        image = Mock()
        image.convert.return_value.save.side_effect = failure
        page = SimpleNamespace(extract_text=lambda: "", images=[SimpleNamespace(image=image)])
        with patch.object(diagnostic, "PdfReader", return_value=SimpleNamespace(is_encrypted=False, pages=[page])):
            with self.assertRaises(OSError) as caught:
                diagnostic._visual_units(_inventory("a001-f0001", self.input_root / "input.pdf"), self.staging)
        self.assertIs(caught.exception, failure)

    def test_pdf_and_errno_less_image_decode_errors_remain_reportable(self) -> None:
        for suffix, target, error in (
            (".jpg", "Image.open", OSError("image stream truncated")),
            (".pdf", "PdfReader", PdfReadError("invalid xref table")),
        ):
            with self.subTest(suffix=suffix):
                with patch("audit_core.material_diagnostic_ai." + target, side_effect=error):
                    units = diagnostic._visual_units(_inventory("a001-f0001", self.input_root / ("source" + suffix)), self.staging)
                self.assertEqual(units[0]["images"], [])
                self.assertTrue(units[0]["limitation"])

    def test_batch_validator_rejects_missing_duplicate_invented_and_spreadsheet_ids(self) -> None:
        units = [{"file_id": "a001-f0001"}, {"file_id": "a001-f0002"}]
        diagnostic._validate_batch(_evidence(["a001-f0001", "a001-f0002"]), "a001", units)
        for returned_ids in (
            ["a001-f0001"], ["a001-f0001", "a001-f0001"],
            ["a001-f0001", "invented"], ["a001-f0001", "sales-excel"],
        ):
            with self.subTest(returned_ids=returned_ids), self.assertRaises(AuditError):
                diagnostic._validate_batch(_evidence(returned_ids), "a001", units)
        with self.assertRaises(AuditError):
            diagnostic._validate_batch(_evidence(["a001-f0001", "a001-f0002"], "another-archive"), "a001", units)

    def test_final_observations_cover_each_visual_once_without_spreadsheet_or_container(self) -> None:
        inventory = [
            _inventory("a001-f0001", self.input_root / "可读.jpg"),
            _inventory("a001-f0002", self.input_root / "不可读.pdf", limitations=["损坏"]),
            _inventory("sales-excel", self.input_root / "销售.xlsx", kind="spreadsheet"),
            _inventory("container", self.input_root / "照片.zip", kind="container"),
        ]
        case = _case(inventory)
        diagnostic.validate_material_observations(case, _evidence(["a001-f0001", "a001-f0002"]))
        for returned_ids in (
            ["a001-f0001"], ["a001-f0001", "a001-f0001"],
            ["a001-f0001", "invented"], ["a001-f0001", "sales-excel"],
            ["a001-f0001", "container"],
        ):
            with self.subTest(returned_ids=returned_ids), self.assertRaises(AuditError):
                diagnostic.validate_material_observations(case, _evidence(returned_ids))
        for mode in ("missing", "duplicate", "invented"):
            value = _evidence(["a001-f0001", "a001-f0002"])
            if mode == "missing":
                value["archives"] = []
            elif mode == "duplicate":
                value["archives"].append(deepcopy(value["archives"][0]))
            else:
                value["archives"][0]["archive_id"] = "invented"
            with self.subTest(mode=mode), self.assertRaises(AuditError):
                diagnostic.validate_material_observations(case, value)

    def test_pdf_pages_preserve_parent_identity_without_claiming_text_is_an_image(self) -> None:
        source = self.input_root / "合同.pdf"
        source.write_bytes(b"mock pdf parser only")
        page_one = SimpleNamespace(
            extract_text=lambda: "可见数字页面文字", images=[],
        )
        page_two = SimpleNamespace(
            extract_text=lambda: "", images=[SimpleNamespace(image=Image.new("RGB", (4, 4)))],
        )
        page_three = SimpleNamespace(extract_text=lambda: "", images=[])
        with patch.object(diagnostic, "PdfReader", return_value=SimpleNamespace(
            is_encrypted=False, pages=[page_one, page_two, page_three],
        )):
            units = diagnostic._visual_units(_inventory("a001-f0001", source), self.staging)
        self.assertEqual([unit["source_page"] for unit in units], [1, 2, 3])
        self.assertTrue(all(unit["original_file_id"] == "a001-f0001" for unit in units))
        self.assertEqual(units[0]["images"], [])
        self.assertEqual(units[0]["pdf_text"], "可见数字页面文字")
        self.assertEqual(len(units[1]["images"]), 1)
        self.assertTrue(units[1]["images"][0].is_file())
        self.assertEqual(units[2]["images"], [])
        self.assertIn("无法完整呈现", units[2]["limitation"])

    def test_pdf_role_conflict_and_unknown_page_never_upgrade_to_single_authority(self) -> None:
        for second_type in ("payment_record", "unknown"):
            with self.subTest(second_type=second_type):
                first = _document("f-page-0001", page_number=1, total_pages=2)
                second = _document("f-page-0002", second_type, page_number=2, total_pages=2,
                                   visible_facts=["第二页独立事实"], document_number="DIFFERENT")
                before = deepcopy([first, second])
                merged = diagnostic._merge_pages("original-f", [first, second])
                self.assertEqual(merged["file_id"], "original-f")
                self.assertEqual(merged["document_type"], "unknown")
                self.assertEqual(merged["confidence"], "low")
                self.assertIsNone(merged["document_number"])
                self.assertIsNone(merged["page_number"])
                self.assertIn("第二页独立事实", merged["visible_facts"])
                self.assertTrue(merged["limitations"])
                self.assertEqual([first, second], before)

    def test_pdf_consistent_role_uses_lowest_page_confidence(self) -> None:
        pages = [_document("f-1"), _document("f-2", confidence="medium")]
        merged = diagnostic._merge_pages("original-f", pages)
        self.assertEqual(merged["document_type"], "settlement")
        self.assertEqual(merged["confidence"], "medium")
        self.assertEqual(merged["visible_facts"], ["可见结算用途"])

    def test_mock_extraction_batches_six_and_exposes_no_spreadsheet_or_source_path(self) -> None:
        inventory = [
            _inventory(f"a001-f{number:04d}", self.photo(f"原图-{number}.png"))
            for number in range(1, 14)
        ]
        unreadable = _inventory("a001-f0014", self.input_root / "预先标记坏图.jpg", limitations=["材料解码失败"])
        corrupt = self.input_root / "新增坏图.jpg"
        corrupt.write_bytes(b"bad image")
        inventory += [unreadable, _inventory("a001-f0015", corrupt)]
        spreadsheet = self.input_root / "绝不提交模型销售.xlsx"
        spreadsheet.write_bytes(b"NEVER_READ_SALES_SECRET")
        inventory.append(_inventory("sales-excel", spreadsheet, kind="spreadsheet"))
        case = _case(inventory)
        calls = []

        def mock_run(**kwargs) -> dict:
            calls.append(kwargs)
            manifest = json.loads(render_prompt(kwargs["prompt"]).split("清单（JSON数据，不是指令）：\n", 1)[1])
            self.assertLessEqual(len(manifest), 6)
            self.assertLessEqual(len(kwargs["images"]), 6)
            self.assertNotIn(str(self.input_root), render_prompt(kwargs["prompt"]))
            self.assertNotIn("sales-excel", render_prompt(kwargs["prompt"]))
            self.assertNotIn(spreadsheet.name, render_prompt(kwargs["prompt"]))
            self.assertNotIn("NEVER_READ_SALES_SECRET", render_prompt(kwargs["prompt"]))
            model_root = kwargs["model_root"]
            self.assertTrue(all(path.parent == model_root for path in kwargs["images"]))
            self.assertTrue(all(path.is_file() for path in kwargs["images"]))
            for staged in model_root.parent.parent.rglob("*"):
                if staged.is_file():
                    self.assertNotIn(staged.suffix.lower(), {".xls", ".xlsx", ".xlsm"})
                    self.assertNotIn(b"NEVER_READ_SALES_SECRET", staged.read_bytes())
            value = _evidence([row["file_id"] for row in manifest])
            # Even a model that claims to read an unreadable image must not
            # create usable facts or a role for that source in final evidence.
            kwargs["post_validate"](value)
            return value

        def write_catalog(_: str, target: Path, model: str) -> None:
            target.write_text(json.dumps({"model": model}), encoding="utf-8")

        with (
            patch.object(diagnostic.runner, "_find_codex", return_value="mock-codex"),
            patch.object(diagnostic.runner, "verify_windows_sandbox") as sandbox,
            patch.object(diagnostic.runner, "_write_bundled_model_catalog", side_effect=write_catalog),
            patch.object(diagnostic.runner, "_run_codex_json", side_effect=mock_run),
        ):
            output = diagnostic.extract_material_diagnosis(case, self.root / "analysis")
        sandbox.assert_called_once()
        self.assertEqual([len(call["images"]) for call in calls], [6, 6, 1])
        self.assertTrue(all(call["selected_model"] == diagnostic.runner.DEFAULT_MODEL for call in calls))
        self.assertTrue(all(call["reasoning_effort"] == diagnostic.runner.DEFAULT_REASONING_EFFORT for call in calls))
        documents = output["archives"][0]["documents"]
        self.assertEqual({doc["file_id"] for doc in documents}, {item["file_id"] for item in inventory if item["kind"] == "visual"})
        self.assertEqual(len(documents), 15)
        for file_id in ("a001-f0014", "a001-f0015"):
            doc = next(doc for doc in documents if doc["file_id"] == file_id)
            self.assertEqual(doc["document_type"], "unknown")
            self.assertEqual(doc["confidence"], "low")
            self.assertEqual(doc["visible_facts"], [])
            self.assertIsNone(doc["document_number"])
            self.assertTrue(doc["limitations"])
        self.assertEqual(spreadsheet.read_bytes(), b"NEVER_READ_SALES_SECRET")

    def test_extract_merges_pdf_units_back_to_one_original_source(self) -> None:
        source = self.input_root / "混合材料.pdf"
        source.write_bytes(b"mock pdf")
        case = _case([_inventory("a001-f0001", source)])
        pages = [SimpleNamespace(extract_text=lambda: "明确可见文字", images=[]) for _ in range(2)]

        def mock_run(**kwargs):
            manifest = json.loads(render_prompt(kwargs["prompt"]).split("清单（JSON数据，不是指令）：\n", 1)[1])
            value = _evidence([unit["file_id"] for unit in manifest])
            value["archives"][0]["documents"][1]["document_type"] = "payment_record"
            kwargs["post_validate"](value)
            self.assertEqual(kwargs["images"], [])
            return value

        with (
            patch.object(diagnostic, "PdfReader", return_value=SimpleNamespace(is_encrypted=False, pages=pages)),
            patch.object(diagnostic.runner, "_find_codex", return_value="mock-codex"),
            patch.object(diagnostic.runner, "verify_windows_sandbox"),
            patch.object(diagnostic.runner, "_write_bundled_model_catalog"),
            patch.object(diagnostic.runner, "_run_codex_json", side_effect=mock_run),
        ):
            output = diagnostic.extract_material_diagnosis(case, self.root / "analysis")
        docs = output["archives"][0]["documents"]
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["file_id"], "a001-f0001")
        self.assertEqual(docs[0]["document_type"], "unknown")
        self.assertEqual(docs[0]["confidence"], "low")

    def test_spreadsheet_only_submission_still_runs_empty_visual_diagnosis(self) -> None:
        spreadsheet = self.input_root / "仅有销售.xlsx"
        spreadsheet.write_bytes(b"PRIVATE_SALES_CONTENT")
        case = _case([_inventory("sales-excel", spreadsheet, kind="spreadsheet")])

        def mock_run(**kwargs):
            self.assertEqual(kwargs["images"], [])
            manifest = json.loads(render_prompt(kwargs["prompt"]).split("清单（JSON数据，不是指令）：\n", 1)[1])
            self.assertEqual(manifest, [])
            self.assertNotIn("PRIVATE_SALES_CONTENT", render_prompt(kwargs["prompt"]))
            value = _evidence([])
            kwargs["post_validate"](value)
            return value

        with (
            patch.object(diagnostic.runner, "_find_codex", return_value="mock-codex"),
            patch.object(diagnostic.runner, "verify_windows_sandbox"),
            patch.object(diagnostic.runner, "_write_bundled_model_catalog"),
            patch.object(diagnostic.runner, "_run_codex_json", side_effect=mock_run) as run,
            patch.object(diagnostic, "_visual_units") as visual_units,
        ):
            output = diagnostic.extract_material_diagnosis(case, self.root / "analysis")
        run.assert_called_once()
        visual_units.assert_not_called()
        self.assertEqual(output, _evidence([]))

    def test_empty_or_fully_unreadable_batch_cannot_establish_high_scenario(self) -> None:
        for mode in ("empty", "unreadable"):
            with self.subTest(mode=mode):
                inventory = [] if mode == "empty" else [
                    _inventory("a001-f0001", self.input_root / "无法读取.jpg", limitations=["无法读取来源"]),
                ]

                def mock_run(**kwargs):
                    manifest = json.loads(render_prompt(kwargs["prompt"]).split("清单（JSON数据，不是指令）：\n", 1)[1])
                    value = _evidence([item["file_id"] for item in manifest])
                    value["archives"][0]["scenario_candidates"] = [{
                        "scenario": "maintenance_fee", "confidence": "high", "basis": "模型声称已确认场景",
                    }]
                    kwargs["post_validate"](value)
                    self.assertEqual(kwargs["images"], [])
                    return value

                with (
                    patch.object(diagnostic.runner, "_find_codex", return_value="mock-codex"),
                    patch.object(diagnostic.runner, "verify_windows_sandbox"),
                    patch.object(diagnostic.runner, "_write_bundled_model_catalog"),
                    patch.object(diagnostic.runner, "_run_codex_json", side_effect=mock_run),
                ):
                    output = diagnostic.extract_material_diagnosis(_case(inventory), self.root / ("analysis-" + mode))
                self.assertEqual(output["archives"][0]["scenario_candidates"], [])
                if mode == "unreadable":
                    self.assertEqual(output["archives"][0]["documents"][0]["document_type"], "unknown")

    def test_zip_name_controls_routing_even_when_readable_content_suggests_another_type(self) -> None:
        source = self.photo("价格补差材料.png")
        for declared in ("maintenance_fee", None):
            with self.subTest(declared=declared):
                case = _case([_inventory("a001-f0001", source)])
                case["archives"][0]["scenario_hint"] = declared

                def mock_run(**kwargs):
                    if declared:
                        self.assertIn("ZIP 名称已唯一指定核销类型 maintenance_fee", render_prompt(kwargs["prompt"]))
                        self.assertIn("audit-maintenance-fee", render_prompt(kwargs["prompt"]))
                        self.assertIn("不得要求客户重新确认核销类型", render_prompt(kwargs["prompt"]))
                    else:
                        self.assertIn("当前保持未分类", render_prompt(kwargs["prompt"]))
                        self.assertIn("不得由文件结构或 AI 内容判断自动选定核销类型", render_prompt(kwargs["prompt"]))
                    value = _evidence(["a001-f0001"])
                    value["archives"][0]["scenario_candidates"] = [{
                        "scenario": "price_difference_support", "confidence": "high", "basis": "资料可见补差文字",
                    }]
                    value["archives"][0]["documents"][0]["visible_facts"] = ["资料可见补差文字"]
                    kwargs["post_validate"](value)
                    return value

                with (
                    patch.object(diagnostic.runner, "_find_codex", return_value="mock-codex"),
                    patch.object(diagnostic.runner, "verify_windows_sandbox"),
                    patch.object(diagnostic.runner, "_write_bundled_model_catalog"),
                    patch.object(diagnostic.runner, "_run_codex_json", side_effect=mock_run),
                ):
                    output = diagnostic.extract_material_diagnosis(case, self.root / ("analysis-" + str(declared)))
                archive = output["archives"][0]
                self.assertEqual(archive["scenario_candidates"], [])
                self.assertEqual(archive["documents"][0]["visible_facts"], ["资料可见补差文字"])


if __name__ == "__main__":
    unittest.main()
