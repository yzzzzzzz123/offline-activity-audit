"""用合成来源和真实结构校验检查切分管线，绝不调用外部模型。"""

from __future__ import annotations

from audit_core.extraction_chain import render_prompt

from collections import Counter
from contextlib import ExitStack
from contextvars import ContextVar
from copy import deepcopy
import hashlib
from tests.product_test_support import image_objects, MemoryOSS
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Barrier, Lock, get_ident
from typing import Any, Callable
import unittest
from unittest.mock import patch

from PIL import Image
from pypdf import PdfWriter

from audit_core import codex_runner as api
from audit_core.common import AuditError, validate_json
from audit_core.display_chunks import chunk_attachment_records
from audit_core.display_pipeline import extract_display_chunks
from audit_core.model_metrics import collect_model_artifacts, record_model_attempt


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SOURCE_SKILL = PROJECT_ROOT / "skills/create-offline-audit-scenario/references" / "legacy" / "promotional_display"
CORE_SENTINEL = "CONTRACT_CORE_SKU_MUST_NOT_IDENTIFY_PHOTO"
ATTACHMENT_SENTINEL = "ATTACHMENT_SKU_MUST_NOT_IDENTIFY_PHOTO"
_CALL_CONTEXT: ContextVar[str] = ContextVar("display_pipeline_test_context", default="absent")


def _observation(*, count: int | None, marker: str) -> dict[str, Any]:
    four = count is not None and count >= 4
    return {
        "standard_evidence": "meets" if four else "unclear",
        "matched_standard": "four_vertical" if four else "unclear",
        "vertical_facing_count": count,
        "vertical_facing_basis": [] if count is None else [f"独立第{index}列" for index in range(1, count + 1)],
        "stack_1sqm_basis": None,
        "description": marker,
        "limitations": [] if four else ["未由面积依据或四个独立包装列证明标准"],
    }


class DisplayPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory(prefix="display-pipeline-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input_root = self.root / "input"
        self.input_root.mkdir()
        self.skill = self.root / "skill"
        references = self.skill / "references"
        references.mkdir(parents=True)
        for name in (
            "visual-extraction.md", "evidence.schema.json", "contract-product-cells.schema.json",
            "product-query.schema.json", "display-standard-review.schema.json",
        ):
            shutil.copy2(SOURCE_SKILL / "references" / name, references / name)
        (references / "display-standard-calibrations.json").write_text(
            json.dumps({"schema_version": "1.0", "calibrations": []}), encoding="utf-8",
        )
        self.full_schema = references / "evidence.schema.json"
        self.pdf = self.input_root / "本次合成合同.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=160, height=80)
        writer.add_blank_page(width=160, height=80)
        writer.write(self.pdf)
        self.assignments: dict[str, int | None] = {
            "a1.jpg": 1, "a2.jpg": 1, "a3.jpg": 1, "a4.jpg": 1, "b1.jpg": 2,
        }
        self.photos = []
        for index, name in enumerate(self.assignments):
            image = self.input_root / name
            Image.new("RGB", (64, 48), (index * 31, 70, 140)).save(image)
            self.photos.append(image)
        self.case = {
            "scenario": "promotional_display", "contract_pdf": str(self.pdf),
            "photo_files": [str(path) for path in self.photos],
            "sales_excel": str(self.input_root / "SALES_EXCEL_MUST_NOT_REACH_MODEL.xlsx"),
        }
        stores = [
            {"line_no": number, "store_name": f"合成门店{number}", "address": f"路由索引地址{number}", "stack_count": 1}
            for number in range(1, 4)
        ]
        rows = [
            {
                "line_no": number, "source_page": 2, "customer_name": "合成客户",
                "business_date": "2026-04", "product_code": f"OLD-CODE-{number}",
                "product_name": f"{ATTACHMENT_SENTINEL}-{number}", "barcode_69": "6970356167341",
                "unit": "支", "quantity": 1, "retail_price": 10, "total_amount": 10,
            }
            for number in range(1, 10)
        ]
        self.contract_result = {
            "schema_version": "2.5", "scenario": "promotional_display",
            "contract": {
                "source_file": self.pdf.name, "customer_name": "合成客户",
                "contract_parties": ["合成品牌", "合成客户"],
                "activity_start": "2026-04-01", "activity_end": "2026-04-30",
                "activity_budget": 300, "activity_content": "三家门店开展陈列",
                "settlement_method": "每店一百元", "display_standard": "一平方米或四纵陈列",
                "fee_per_store": 100, "fee_basis": "per_store", "claimed_amount": 300,
                "contract_stack_count": 3, "watermark_visible": None, "seal_visible": True,
                "requires_specific_products": True, "required_products": [CORE_SENTINEL],
                "required_product_identities": [{
                    "visible_text": CORE_SENTINEL, "product_code": "CONTRACT-SECRET-CODE",
                    "product_name": CORE_SENTINEL, "barcode_69": "6970356167341",
                }],
                "requires_promotion": False, "required_promotion": None,
                "sales_attachment": {
                    "present": True, "source_pages": [2], "records": rows,
                    "total_quantity": 9, "total_amount": 90,
                },
                "stores": stores, "notes": [],
            },
            "extraction_notes": ["合成输入仅供切分及边界测试"],
        }
        products = []
        for letter, code in (("a", "ALPHA-UNIQUE-9301"), ("b", "BETA-UNIQUE-7429")):
            products.append({
                "product_id": f"product-{letter}", "product_name": f"DB_NAME_{letter.upper()}",
                "product_code": code, "product_code_aliases": [], "barcode_69": "6970356167341",
                "specification": "规格未载明", "variant": None, "aliases": [], "sources": [],
                "views": [{
                    "view_id": f"view-{letter}",
                    "source_id": f"source-{letter}", "face": "front", "identity_strength": "strong",
                    "visible_anchors": [],
                }],
            })
        self.catalog = {"schema_version": "1.0", "data_source": {"type": "mysql"}, "products": products}
        self.remote = MemoryOSS(image_objects(self.catalog))
        self.calls: list[dict[str, Any]] = []
        self.call_lock = Lock()
        self.run_counter = 0
        self.mutate: Callable[[str, dict[str, Any]], None] | None = None
        self.route_barrier: Barrier | None = None

    def _pages(self, source: Path, destination: Path) -> list[Path]:
        self.assertEqual(source.name, self.pdf.name)
        pages = []
        for number in (1, 2):
            path = destination / f"contract-page-{number:02d}.png"
            Image.new("RGB", (160, 80), "white").save(path)
            pages.append(path)
        return pages

    def _route(self, number: int) -> dict[str, Any]:
        return {
            "store_line_no": number, "contract_store_name": f"合成门店{number}",
            "photo_files": [name for name, assigned in self.assignments.items() if assigned == number],
        }

    def _photo_result(self, number: int) -> dict[str, Any]:
        route = self._route(number)
        has_photos = bool(route["photo_files"])
        product = self.catalog["products"][number - 1] if number <= 2 else None
        return {
            "schema_version": "2.5", "scenario": "promotional_display",
            "photo_reviews": [{
                **route,
                "visible_date": "2026-04-02" if has_photos else None,
                "visible_location": f"照片独立地点{number}" if has_photos else None,
                "location_basis": "独立读取水印，不以路由索引替代现场地点",
                "display_observation": _observation(
                    count=4 if has_photos else None, marker=f"FIRST_PASS_SENTINEL_{number}",
                ),
                "visible_text": [product["product_code"]] if product and has_photos else [],
                "recognized_products": [product["product_name"] if product and has_photos else "无法识别商品"],
                "product_reference_hits": [{
                    "reference_product_id": product["product_id"], "confidence": "exact",
                    "matched_view_ids": [product["views"][0]["view_id"]],
                    "visible_basis": [product["product_code"]], "limitations": [],
                }] if product and has_photos else [],
                "promotion_evidence": {"signals": [], "visible_prices": [], "limitations": []},
                "risk_notes": [], "supplement_advice": [],
            }],
            "extraction_notes": [f"门店{number}独立观察"],
        }

    def _fake_run(self, **kwargs: Any) -> dict[str, Any]:
        name = kwargs["model_root"].name
        with self.call_lock:
            self.calls.append({
                "stage": name, "images": list(kwargs["images"]), "prompt": render_prompt(kwargs["prompt"]),
                "skill": kwargs["skill_dir"], "root": kwargs["model_root"],
                "thread": get_ident(), "context": _CALL_CONTEXT.get(),
                "effort": kwargs["reasoning_effort"],
            })
        if name.startswith("route-") and self.route_barrier is not None:
            self.route_barrier.wait(timeout=10)
        if name == "contract-core":
            result = deepcopy(self.contract_result)
            result["contract"].pop("stores")
            result["contract"]["sales_attachment"].pop("records")
            result["page_inventory"] = [
                {"source_page": 1, "tables": [{"table_no": 1, "kind": "stores", "row_count": 3}]},
                {"source_page": 2, "tables": [{"table_no": 1, "kind": "sales_attachment", "row_count": 9}]},
            ]
        elif name.startswith("contract-layout-"):
            page = int(name.rsplit("-", 1)[-1])
            result = {"source_file": self.pdf.name, "source_page": page, "tables": [{
                "table_no": 1, "kind": "stores" if page == 1 else "sales_attachment",
                "row_count": 3 if page == 1 else 9, "clockwise_rotation": 0,
                "body_bounds": {"left": .1, "top": .1, "right": .9, "bottom": .9},
            }], "extraction_notes": []}
        elif name.startswith("contract-table-"):
            schema = json.loads(kwargs["schema"].read_text(encoding="utf-8"))["properties"]
            kind = schema["kind"]["const"]
            row_ids = schema["records"]["items"]["properties"]["page_local_row_no"]["enum"]
            rows = self.contract_result["contract"]["stores"] if kind == "stores" else self.contract_result["contract"]["sales_attachment"]["records"]
            result = {"source_file": self.pdf.name, "source_page": schema["source_page"]["const"],
                      "table_no": 1, "kind": kind, "records": [], "extraction_notes": []}
            for row_no in row_ids:
                row = deepcopy(rows[row_no - 1])
                row.pop("line_no")
                row["page_local_row_no"] = row_no
                result["records"].append(row)
        elif name.startswith("cells-"):
            parts = name.split("-")
            index = int(parts[1]) - 1
            records = chunk_attachment_records(self.contract_result["contract"]["sales_attachment"]["records"])[index]
            for part in parts[2:]:
                midpoint = (len(records) + 1) // 2
                records = records[:midpoint] if part == "s1" else records[midpoint:]
            result = {"records": [{
                "line_no": row["line_no"], "source_page": row["source_page"],
                "product_code": f"REREAD-CODE-{row['line_no']}",
                "product_name": f"{ATTACHMENT_SENTINEL}-REREAD-{row['line_no']}",
                "barcode_69": row["barcode_69"],
            } for row in records], "extraction_notes": ["保留原始行序独立复读"]}
        elif name.startswith("route-"):
            result = {"photo_assignments": [{
                "photo_file": path.name, "store_line_no": self.assignments[path.name],
                "visible_location": f"照片地点{self.assignments[path.name]}",
                "location_basis": "原图可见地点；仅用于路由索引",
            } for path in kwargs["images"]], "extraction_notes": ["逐张完成路由"]}
        elif name.startswith("query-"):
            queries = []
            for path in kwargs["images"]:
                number = self.assignments[path.name]
                code = self.catalog["products"][number - 1]["product_code"] if number in (1, 2) else "UNROUTED-CODE"
                queries.append({
                    "photo_file": path.name, "visible_product_names": [],
                    "visible_product_codes": [code], "visible_barcodes_69": [],
                    "visible_text": [code], "packaging_terms": [], "limitations": [],
                })
            result = {"schema_version": "1.0", "photo_queries": queries, "extraction_notes": ["逐图独立提取文字"]}
        elif name.startswith("store-"):
            result = self._photo_result(int(name.rsplit("-", 1)[-1]))
        elif name.startswith("review-"):
            number = int(name.rsplit("-", 1)[-1])
            route = self._route(number)
            result = {"schema_version": "1.0", "display_reviews": [{
                **route, "display_observation": _observation(
                    count=3 if route["photo_files"] else None, marker=f"FOCUSED_SENTINEL_{number}",
                ),
            }], "extraction_notes": ["本次聚焦复核独立完成"]}
        else:
            self.fail(f"未识别测试阶段 {name}")
        if self.mutate:
            self.mutate(name, result)
        validate_json(result, kwargs["schema"])
        if kwargs.get("post_validate"):
            kwargs["post_validate"](result)
        record_model_attempt({
            "label": name, "model": kwargs["selected_model"], "attempt": 1,
            "reasoning_effort": kwargs["reasoning_effort"], "status": "success",
            "image_count": len(kwargs["images"]), "usage": {"input_tokens": 1, "output_tokens": 1},
        })
        return result

    def _execute(self) -> tuple[dict[str, Any], Path]:
        self.run_counter += 1
        analysis = self.root / f"analysis-{self.run_counter}"
        working = self.root / f"working-{self.run_counter}"
        with ExitStack() as stack:
            stack.enter_context(patch.object(api, "_run_codex_json", side_effect=self._fake_run))
            stack.enter_context(patch.object(api, "_extract_scanned_pdf_pages", side_effect=self._pages))
            stack.enter_context(patch.object(api, "load_product_catalog", side_effect=lambda: deepcopy(self.catalog)))
            stack.enter_context(patch("audit_core.product_image_runtime.ProductOSS", return_value=self.remote))
            stack.enter_context(collect_model_artifacts(analysis))
            token = _CALL_CONTEXT.set("current-run-marker")
            stack.callback(_CALL_CONTEXT.reset, token)
            result = extract_display_chunks(
                self.case, working, codex="never-executed-codex",
                skill_dir=self.skill, full_schema=self.full_schema,
                selected_model="gpt-6-astra", selected_reasoning_effort="high",
                model_catalog=self.root / "unused-model-catalog.json",
                max_attempts=1, attempt_timeout_seconds=1,
            )
        return result, analysis

    def test_complete_pipeline_keeps_source_coverage_oss_references_and_independent_review(self) -> None:
        self.route_barrier = Barrier(2)
        result, analysis = self._execute()
        expected_photos = [path.name for path in self.photos]
        for prefix in ("route-", "query-"):
            calls = [call for call in self.calls if call["stage"].startswith(prefix)]
            self.assertEqual(Counter(path.name for call in calls for path in call["images"]), Counter(expected_photos))
            self.assertTrue(all(len(call["images"]) <= 3 for call in calls))
        by_stage = {call["stage"]: call for call in self.calls}
        for number in (1, 2, 3):
            store = by_stage[f"store-{number:03d}"]
            review = by_stage[f"review-{number:03d}"]
            route = self._route(number)
            submitted = [path.name for path in store["images"] if not path.name.startswith("rag-reference--")]
            self.assertEqual(submitted, route["photo_files"])
            self.assertEqual([path.name for path in review["images"]], route["photo_files"])
            self.assertNotEqual(store["root"], review["root"])
            for call in (store, review):
                for marker in (CORE_SENTINEL, ATTACHMENT_SENTINEL, "CONTRACT-SECRET-CODE", "SALES_EXCEL_MUST_NOT_REACH_MODEL"):
                    self.assertNotIn(marker, call["prompt"])
                self.assertTrue(all(path.parent == call["root"] for path in call["images"]))
                self.assertEqual(call["skill"].parent, call["root"])
            self.assertNotIn("FIRST_PASS_SENTINEL", review["prompt"])
            self.assertNotIn("DB_NAME_", review["prompt"])
            self.assertNotIn("rag-reference--", review["prompt"])
        self.assertIn("DB_NAME_A", by_stage["store-001"]["prompt"])
        self.assertNotIn("DB_NAME_B", by_stage["store-001"]["prompt"])
        self.assertIn("DB_NAME_B", by_stage["store-002"]["prompt"])
        self.assertNotIn("DB_NAME_A", by_stage["store-002"]["prompt"])
        self.assertEqual(by_stage["store-003"]["images"], [])
        self.assertNotIn("DB_NAME_", by_stage["store-003"]["prompt"])
        self.assertEqual(len([path for path in by_stage["store-001"]["images"] if not path.name.startswith("rag-reference--")]), 4)
        self.assertEqual([row["store_line_no"] for row in result["photo_reviews"]], [1, 2, 3])
        self.assertEqual([row["display_observation"]["vertical_facing_count"] for row in result["photo_reviews"]], [3, 3, None])
        observations = json.loads((analysis / "model-observations" / "promotional-display.json").read_text(encoding="utf-8"))
        self.assertEqual(observations["first_pass_reviews"][0]["display_observation"]["vertical_facing_count"], 4)
        self.assertEqual(observations["focused_reviews"][0]["display_observation"]["vertical_facing_count"], 3)
        self.assertEqual(observations["calibrated_reviews"], result["photo_reviews"])
        self.assertEqual([row["file_name"] for row in observations["photo_inventory"]], expected_photos)
        self.assertEqual(observations["unassigned_photo_files"], [])
        self.assertEqual({call["context"] for call in self.calls}, {"current-run-marker"})
        self.assertEqual(len({call["thread"] for call in self.calls if call["stage"].startswith("route-")}), 2)
        metrics = [json.loads(line) for line in (analysis / "model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(metrics), len(self.calls))
        self.assertEqual(Counter(row["label"] for row in metrics), Counter(call["stage"] for call in self.calls))
        self.assertEqual(_CALL_CONTEXT.get(), "absent")

    def test_contract_cells_are_bounded_complete_and_never_receive_prior_identity(self) -> None:
        original = deepcopy(self.contract_result)
        result, analysis = self._execute()
        cell_calls = sorted((call for call in self.calls if call["stage"].startswith("cells-")), key=lambda call: call["stage"])
        self.assertEqual(len(cell_calls), 2)
        for call in cell_calls:
            self.assertNotIn("OLD-CODE-", call["prompt"])
            self.assertNotIn(ATTACHMENT_SENTINEL, call["prompt"])
            self.assertNotIn(CORE_SENTINEL, call["prompt"])
            self.assertTrue(all("page-02" in path.name for path in call["images"]))
        records = result["contract"]["sales_attachment"]["records"]
        self.assertEqual([row["line_no"] for row in records], list(range(1, 10)))
        self.assertEqual([row["product_code"] for row in records], [f"REREAD-CODE-{number}" for number in range(1, 10)])
        self.assertEqual(self.contract_result, original)
        observations = json.loads((analysis / "model-observations" / "promotional-display.json").read_text(encoding="utf-8"))
        self.assertEqual(observations["partition"]["attachment_chunks"], 2)

    def test_first_contract_pass_is_bounded_and_uses_independent_page_inventory(self) -> None:
        result, analysis = self._execute()
        partitions = json.loads((analysis / "model-observations" / "promotional-display-contract.json").read_text(encoding="utf-8"))
        self.assertEqual(partitions["row_batch_size"], 8)
        self.assertEqual([len(fragment["records"]) for fragment in partitions["fragments"]], [3, 8, 1])
        by_stage = {call["stage"]: call for call in self.calls}
        core_root = by_stage["contract-core"]["root"]
        core_contract = json.loads((core_root / "schema.json").read_text(encoding="utf-8"))["properties"]["contract"]["properties"]
        self.assertNotIn("stores", core_contract)
        self.assertNotIn("records", core_contract["sales_attachment"]["properties"])
        for call in self.calls:
            if call["stage"].startswith("contract-layout-"):
                self.assertEqual(len(call["images"]), 1)
                self.assertNotIn(CORE_SENTINEL, call["prompt"])
                self.assertNotIn(ATTACHMENT_SENTINEL, call["prompt"])
            if call["stage"].startswith("contract-table-"):
                self.assertTrue(all(path.parent == call["root"] for path in call["images"]))
                self.assertNotIn("OLD-CODE-", call["prompt"])
                self.assertNotIn("SALES_EXCEL_MUST_NOT_REACH_MODEL", call["prompt"])
        self.assertEqual(len(result["contract"]["stores"]), 3)
        self.assertEqual(len(result["contract"]["sales_attachment"]["records"]), 9)

    def test_first_pass_rejects_independent_page_count_disagreement(self) -> None:
        def mutate(stage: str, value: dict) -> None:
            if stage == "contract-layout-002":
                value["tables"][0]["row_count"] = 8
        self.mutate = mutate
        with self.assertRaisesRegex(AuditError, "独立整页盘点"):
            self._execute()
        self.assertFalse(any(call["stage"].startswith("contract-table-") for call in self.calls))

    def test_first_pass_rejects_missing_duplicate_or_wrong_page_rows(self) -> None:
        for fault in ("missing", "duplicate", "wrong_page"):
            with self.subTest(fault=fault):
                def mutate(stage: str, value: dict) -> None:
                    if stage != "contract-table-002-001-001":
                        return
                    if fault == "missing":
                        value["records"].pop()
                    elif fault == "duplicate":
                        value["records"][1] = deepcopy(value["records"][0])
                    else:
                        value["records"][0]["source_page"] = 1
                self.mutate = mutate
                with self.assertRaises(AuditError):
                    self._execute()

    def test_first_pass_capacity_failure_splits_only_the_failed_row_block(self) -> None:
        def mutate(stage: str, value: dict) -> None:
            if stage == "contract-table-002-001-001":
                raise api.CodexContextCapacityError("context_limit")
        self.mutate = mutate
        result, analysis = self._execute()
        self.assertEqual(len(result["contract"]["sales_attachment"]["records"]), 9)
        calls = Counter(call["stage"] for call in self.calls)
        self.assertEqual(calls["contract-core"], 1)
        self.assertEqual(calls["contract-layout-002"], 1)
        self.assertEqual(calls["contract-table-001-001-001"], 1)
        partitions = json.loads((analysis / "model-observations" / "promotional-display-contract.json").read_text(encoding="utf-8"))
        self.assertEqual([len(fragment["records"]) for fragment in partitions["fragments"]], [3, 4, 4, 1])

    def test_route_rejects_missing_duplicate_unknown_and_unassigned_photos(self) -> None:
        for failure in ("missing", "duplicate", "unknown-store", "unassigned"):
            def mutate(stage: str, result: dict[str, Any]) -> None:
                if stage != "route-001":
                    return
                rows = result["photo_assignments"]
                if failure == "missing":
                    rows.pop()
                elif failure == "duplicate":
                    rows.append(deepcopy(rows[0]))
                elif failure == "unknown-store":
                    rows[0]["store_line_no"] = 99
                else:
                    rows[0]["store_line_no"] = None
            self.mutate = mutate
            with self.subTest(failure=failure), self.assertRaises(AuditError):
                self._execute()

    def test_query_rejects_missing_or_duplicate_source_photos(self) -> None:
        for failure in ("missing", "duplicate"):
            def mutate(stage: str, result: dict[str, Any]) -> None:
                if stage == "query-001":
                    rows = result["photo_queries"]
                    if failure == "missing":
                        rows.pop()
                    else:
                        rows.append(deepcopy(rows[0]))
            self.mutate = mutate
            with self.subTest(failure=failure), self.assertRaises(AuditError):
                self._execute()

    def test_store_rejects_photo_reorder_omission_and_cross_store_rag_identity(self) -> None:
        for failure in ("reorder", "missing", "cross-store-rag"):
            def mutate(stage: str, result: dict[str, Any]) -> None:
                if stage != "store-001":
                    return
                row = result["photo_reviews"][0]
                if failure == "reorder":
                    row["photo_files"].reverse()
                elif failure == "missing":
                    row["photo_files"].pop()
                else:
                    row["product_reference_hits"][0].update({
                        "reference_product_id": "product-b", "matched_view_ids": ["view-b"],
                    })
            self.mutate = mutate
            with self.subTest(failure=failure), self.assertRaises(AuditError):
                self._execute()

    def test_focused_review_rejects_reordered_route_and_photo_omission(self) -> None:
        for failure in ("reorder", "missing"):
            def mutate(stage: str, result: dict[str, Any]) -> None:
                if stage == "review-001":
                    photos = result["display_reviews"][0]["photo_files"]
                    if failure == "reorder":
                        photos.reverse()
                    else:
                        photos.pop()
            self.mutate = mutate
            with self.subTest(failure=failure), self.assertRaises(AuditError):
                self._execute()

    def test_cells_reject_reorder_missing_line_and_wrong_source_page(self) -> None:
        for failure in ("reorder", "missing", "wrong-page"):
            def mutate(stage: str, result: dict[str, Any]) -> None:
                if stage != "cells-001":
                    return
                rows = result["records"]
                if failure == "reorder":
                    rows.reverse()
                elif failure == "missing":
                    rows.pop()
                else:
                    rows[0]["source_page"] = 1
            self.mutate = mutate
            with self.subTest(failure=failure), self.assertRaises(AuditError):
                self._execute()

    def test_capacity_failure_splits_only_failed_photo_batches_without_losing_sources(self) -> None:
        def mutate(stage: str, result: dict[str, Any]) -> None:
            key = "photo_assignments" if stage.startswith("route-") else "photo_queries"
            if stage.startswith(("route-", "query-")) and len(result[key]) > 1:
                raise api.CodexContextCapacityError("合成容量上限")
        self.mutate = mutate
        result, analysis = self._execute()
        for prefix in ("route-", "query-"):
            calls = [call for call in self.calls if call["stage"].startswith(prefix)]
            leaf_calls = [call for call in calls if len(call["images"]) == 1]
            self.assertEqual(Counter(path.name for call in leaf_calls for path in call["images"]), Counter(path.name for path in self.photos))
            self.assertEqual(len({call["root"] for call in calls}), len(calls))
        self.assertEqual(result["photo_reviews"][0]["photo_files"], self._route(1)["photo_files"])
        observations = json.loads((analysis / "model-observations" / "promotional-display.json").read_text(encoding="utf-8"))
        self.assertEqual(len(observations["photo_assignments"]), len(self.photos))

    def test_attachment_retry_splits_rows_and_preserves_all_original_source_keys(self) -> None:
        def mutate(stage: str, result: dict[str, Any]) -> None:
            if stage.startswith("cells-") and len(result["records"]) > 2:
                raise api.CodexExtractionError("合成密集行复读失败")
        self.mutate = mutate
        result, _ = self._execute()
        rows = result["contract"]["sales_attachment"]["records"]
        self.assertEqual([(row["source_page"], row["line_no"]) for row in rows], [(2, number) for number in range(1, 10)])
        self.assertTrue(any(call["stage"].count("-s") > 1 for call in self.calls if call["stage"].startswith("cells-")))
        self.assertTrue(all(row["product_code"] == f"REREAD-CODE-{row['line_no']}" for row in rows))

    def test_configuration_error_does_not_trigger_attachment_resplitting(self) -> None:
        def mutate(stage: str, result: dict[str, Any]) -> None:
            if stage == "cells-001":
                raise api.CodexRequestConfigurationError("合成配置错误")
        self.mutate = mutate
        with self.assertRaises(api.CodexRequestConfigurationError):
            self._execute()
        self.assertFalse(any(call["stage"].startswith("cells-001-s") for call in self.calls))


if __name__ == "__main__":
    unittest.main()
