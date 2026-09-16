"""Bounded, source-preserving visual calls behind the single official runner."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from typing import Any, Callable

from langchain_core.prompts import PromptTemplate
from langchain_core.runnables import RunnableLambda

from .prompts import bound_prompt
from .common import AuditError, validate_json
from .contract_pipeline import extract_contract_chunks
from .display_chunks import (
    chunk_attachment_records, chunk_sequence, merge_contract_product_cells,
    merge_photo_assignments, merge_product_queries, merge_routed_results,
)
from .model_metrics import save_model_observations
from .workflow import batch_local

PHOTO_BATCH_SIZE = 3
ATTACHMENT_ROW_BATCH_SIZE = 8
VISUAL_WORKERS = 2


def _map_chunks(function: Callable, chunks: list) -> list:
    return batch_local(RunnableLambda(function, name="visual_material_batch"),
                       chunks, max_concurrency=VISUAL_WORKERS)


def _assignment_schema() -> dict[str, Any]:
    return {
        "type": "object", "additionalProperties": False,
        "required": ["photo_assignments", "extraction_notes"],
        "properties": {
            "photo_assignments": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["photo_file", "store_line_no", "visible_location", "location_basis"],
                "properties": {
                    "photo_file": {"type": "string", "minLength": 1},
                    "store_line_no": {"type": ["integer", "null"], "minimum": 1},
                    "visible_location": {"type": ["string", "null"]},
                    "location_basis": {"type": "string", "minLength": 1},
                },
            }},
            "extraction_notes": {"type": "array", "items": {"type": "string"}},
        },
    }


def extract_display_chunks(
    case: dict[str, Any], root: Path, *, codex: str, skill_dir: Path,
    full_schema: Path, selected_model: str, selected_reasoning_effort: str,
    model_catalog: Path, max_attempts: int, attempt_timeout_seconds: int,
) -> dict[str, Any]:
    # Import lazily to keep the legacy runner helpers and their validation authoritative.
    from . import codex_runner as api

    pipeline_root = root / "model-promotional_display"
    pipeline_root.mkdir(parents=True, exist_ok=False)

    def stage(name: str) -> tuple[Path, Path]:
        stage_root = pipeline_root / name
        stage_skill = stage_root / "skill"
        stage_skill.mkdir(parents=True, exist_ok=False)
        shutil.copy2(skill_dir / "references" / "visual-extraction.md", stage_skill / "SKILL.md")
        return stage_root, stage_skill

    def run(stage_root: Path, stage_skill: Path, schema: Path, prompt: PromptTemplate,
            images: list[Path], label: str, validator: Callable | None = None,
            effort: str | None = None) -> dict[str, Any]:
        # Neither production calibration labels nor prior model results enter a stage.
        return api._run_codex_json(
            codex=codex, model_root=stage_root, skill_dir=stage_skill, schema=schema,
            raw_output=stage_root / "result.json", prompt=prompt, images=images,
            selected_model=selected_model, model_catalog=model_catalog, label=label,
            max_attempts=max_attempts, attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=effort or selected_reasoning_effort, post_validate=validator,
        )

    contract_root, contract_skill = stage("contract")
    contract_pdf = contract_root / Path(case["contract_pdf"]).name
    shutil.copy2(case["contract_pdf"], contract_pdf)
    page_images = api._extract_scanned_pdf_pages(contract_pdf, contract_root)
    contract_result = extract_contract_chunks(
        original_pdf=contract_pdf, page_images=page_images, full_schema=full_schema,
        stage=stage, run=run, map_chunks=_map_chunks,
    )
    attachment_records = contract_result["contract"]["sales_attachment"].get("records") or []
    row_chunks = chunk_attachment_records(attachment_records, ATTACHMENT_ROW_BATCH_SIZE)

    def read_cells(indexed: tuple[int, list], split_tag: str = "") -> dict:
        index, records = indexed
        cell_root, cell_skill = stage(f"cells-{index:03d}{split_tag}")
        # Output is row-bounded; original page density still determines the lossless
        # crop resolution. A smaller output block must not make dense text blurrier.
        page_records = [record for record in attachment_records
                        if record["source_page"] == records[0]["source_page"]]
        views = api._prepare_contract_product_cell_views(page_images, page_records, cell_root / "views")
        schema = cell_root / "cells.schema.json"
        shutil.copy2(skill_dir / "references" / "contract-product-cells.schema.json", schema)
        try:
            return run(
                cell_root, cell_skill, schema,
                api._contract_product_cells_prompt(cell_skill, contract_pdf, views, schema, records),
                views, f"合同附件商品格块 {index}{split_tag}/{len(row_chunks)}（{len(records)}行）",
                lambda value: api._validate_contract_product_cells(records, value),
            )
        except api.CodexRequestConfigurationError:
            raise
        except api.CodexExtractionError:
            if len(records) <= 1:
                raise
            print(f"附件块 {index}{split_tag} 缩小到逐行子块重新复读，不复用失败输出。", flush=True)
            halves = chunk_sequence(records, (len(records) + 1) // 2)
            return merge_contract_product_cells(records, [
                read_cells((index, part), split_tag + f"-s{part_no}")
                for part_no, part in enumerate(halves, 1)
            ])

    if row_chunks:
        cell_results = _map_chunks(read_cells, list(enumerate(row_chunks, 1)))
        api._apply_contract_product_cells(
            contract_result, merge_contract_product_cells(attachment_records, cell_results),
        )
        api._validate_contract_result(case, contract_result, source_page_count=len(page_images))

    photo_sources = [Path(value) for value in case["photo_files"]]
    photo_batches = chunk_sequence(photo_sources, PHOTO_BATCH_SIZE)
    stores = contract_result["contract"]["stores"]
    store_ids = {int(store["line_no"]) for store in stores}
    route_table = [{key: store.get(key) for key in ("line_no", "store_name", "address")} for store in stores]

    def route_photos(indexed: tuple[int, list], split_tag: str = "") -> dict:
        index, sources = indexed
        route_root, route_skill = stage(f"route-{index:03d}{split_tag}")
        images = api._copy_images(sources, route_root)
        schema = route_root / "route.schema.json"
        schema.write_text(json.dumps(_assignment_schema(), ensure_ascii=False), encoding="utf-8")
        prompt = (
            bound_prompt("""完整读取 `{skill_path}`。仅为当前 {image_count} 张现场照片建立门店路由，使用 `{schema}`。
逐张查看原图水印/门头可见地点，原样保留 photo_file；每图恰好一条。以下名单仅作路由索引，不是现场证据。
门店索引：{route_table_json}
照片：{image_names_json}
可用文件名编号及名称作为路由线索，但不能将其写为 visible_location 或证明同址；水印/门头不可读时 visible_location=null，location_basis明确标注仅按文件名路由。能唯一归属才返回 store_line_no；无法唯一归属返回null并说明。地点文字不同不在本轮判错，后续代码独立地图核验。不识别商品/日期/陈列，不读合同PDF、Excel、其他照片、参考图片或历史结果。""", skill_path=route_skill / 'SKILL.md', image_count=len(images), schema=schema, route_table_json=json.dumps(route_table, ensure_ascii=False), image_names_json=json.dumps([p.name for p in images], ensure_ascii=False))
        )

        def validate(value: dict) -> None:
            merge_photo_assignments(images, [value])
            if any(item["store_line_no"] is None for item in value["photo_assignments"]):
                raise AuditError("仍有现场照片未能唯一路由；必须复查当前块，不得漏图后发布通过结果")
            if any(item["store_line_no"] is not None and item["store_line_no"] not in store_ids
                   for item in value["photo_assignments"]):
                raise AuditError("照片路由引用不存在的合同门店行")
        try:
            return run(route_root, route_skill, schema, prompt, images,
                       f"现场照片路由块 {index}{split_tag}/{len(photo_batches)}", validate)
        except api.CodexContextCapacityError:
            if len(sources) <= 1:
                raise
            return merge_photo_assignments(sources, [
                route_photos((index, part), split_tag + f"-s{part_no}")
                for part_no, part in enumerate(chunk_sequence(sources, (len(sources) + 1) // 2), 1)
            ])

    assignments = merge_photo_assignments(
        photo_sources, _map_chunks(route_photos, list(enumerate(photo_batches, 1))),
    )

    def read_queries(indexed: tuple[int, list], split_tag: str = "") -> dict:
        index, sources = indexed
        query_root, query_skill = stage(f"query-{index:03d}{split_tag}")
        images = api._copy_images(sources, query_root)
        schema = query_root / "query.schema.json"
        shutil.copy2(skill_dir / "references" / "product-query.schema.json", schema)
        try:
            return run(query_root, query_skill, schema,
                       api._product_query_prompt(query_skill, images, schema), images,
                       f"现场商品文字块 {index}{split_tag}/{len(photo_batches)}",
                       lambda value: api._validate_product_query_result(images, value),
                       effort=api.PRODUCT_QUERY_REASONING_EFFORT)
        except api.CodexContextCapacityError:
            if len(sources) <= 1:
                raise
            return merge_product_queries(sources, [
                read_queries((index, part), split_tag + f"-s{part_no}")
                for part_no, part in enumerate(chunk_sequence(sources, (len(sources) + 1) // 2), 1)
            ])

    query_result = merge_product_queries(
        photo_sources, _map_chunks(read_queries, list(enumerate(photo_batches, 1))),
    )
    by_name = {path.name: path for path in photo_sources}
    assigned = assignments["photo_assignments"]
    routes = [{
        "store_line_no": int(store["line_no"]), "contract_store_name": store["store_name"],
        "photo_files": [item["photo_file"] for item in assigned if item["store_line_no"] == store["line_no"]],
    } for store in stores]
    full_rag = api.load_product_catalog()

    def read_store(route: dict) -> dict:
        number = route["store_line_no"]
        photo_root, photo_skill = stage(f"store-{number:03d}")
        images = api._copy_images([by_name[name] for name in route["photo_files"]], photo_root)
        local_query = {**query_result, "photo_queries": [
            q for q in query_result["photo_queries"] if q["photo_file"] in route["photo_files"]
        ]}
        rag = api._select_product_rag_candidates(full_rag, local_query)
        rag = api.attach_product_reference_images(rag)
        references = api._copy_product_reference_images(photo_root, rag)
        rules = photo_root / "shared-product-rag-rules.md"
        shutil.copy2(api.PRODUCT_KNOWLEDGE_RULES, rules)
        local_contract = {"contract": {**contract_result["contract"], "stores": [
            store for store in stores if store["line_no"] == number
        ]}}
        schema = api._write_subset_schema(full_schema, photo_root / "photo.schema.json", "photo_reviews", "单店现场照片事实")
        prompt = api._photo_prompt(photo_skill, rules, images, schema, local_contract, rag, references)
        prompt += bound_prompt(
            "\n本轮不可变路由：{route_json}"
            "\n必须原样保留此路由的三个字段；路由仅用于分组，可见地点和日期仍须独立从照片读取，不得继承索引内容。",
            route_json=json.dumps([route], ensure_ascii=False),
        )
        local_case = {**case, "photo_files": [str(p) for p in images]}

        def validate(value: dict) -> None:
            api._validate_photo_result(local_case, local_contract, value, rag)
            merge_routed_results([route], [value], "photo_reviews")

        return run(photo_root, photo_skill, schema, prompt,
                   [*images, *(item["path"] for item in references)],
                   f"堆头现场门店 {number}/{len(stores)}", validate)

    photo_result = merge_routed_results(routes, _map_chunks(read_store, routes), "photo_reviews")
    first_pass = deepcopy(photo_result["photo_reviews"])

    def review_store(route: dict) -> dict:
        number = route["store_line_no"]
        review_root, review_skill = stage(f"review-{number:03d}")
        images = api._copy_images([by_name[name] for name in route["photo_files"]], review_root)
        schema = review_root / "review.schema.json"
        shutil.copy2(skill_dir / "references" / "display-standard-review.schema.json", schema)
        return run(review_root, review_skill, schema,
                   api._display_standard_review_prompt(review_skill, images, schema, [route]),
                   images, f"独立陈列复核门店 {number}/{len(stores)}",
                   lambda value: api._validate_display_standard_review([route], value))

    focused_result = merge_routed_results(routes, _map_chunks(review_store, routes), "display_reviews")
    api._apply_display_standard_review(photo_result, focused_result)
    focused_reviews = deepcopy(photo_result["photo_reviews"])
    api._apply_display_standard_calibrations(
        photo_result, photo_sources, skill_dir / "references" / "display-standard-calibrations.json",
    )
    api._validate_photo_result(case, contract_result, photo_result, full_rag)
    unassigned = [item["photo_file"] for item in assigned if item["store_line_no"] is None]
    notes = [*contract_result.get("extraction_notes", []), *assignments.get("extraction_notes", []),
             *query_result.get("extraction_notes", []), *photo_result.get("extraction_notes", [])]
    if unassigned:
        notes.append("已逐张识别但无法唯一路由到合同门店的照片：" + "、".join(unassigned))
    merged = {"schema_version": "2.5", "scenario": "promotional_display",
              "contract": contract_result["contract"], "photo_reviews": photo_result["photo_reviews"],
              "extraction_notes": list(dict.fromkeys(notes))}
    validate_json(merged, full_schema)
    api._validate_source_names(case, merged, full_rag)
    save_model_observations("promotional-display", {
        "version": 1, "photo_inventory": [
            {"file_name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in photo_sources
        ], "first_pass_reviews": first_pass, "focused_reviews": focused_reviews,
        "calibrated_reviews": deepcopy(photo_result["photo_reviews"]),
        "contract": contract_result["contract"], "photo_assignments": assigned,
        "unassigned_photo_files": unassigned,
        "partition": {"photo_batch_size": PHOTO_BATCH_SIZE, "attachment_row_batch_size": ATTACHMENT_ROW_BATCH_SIZE,
                      "workers": VISUAL_WORKERS, "contract_page_count": len(page_images),
                      "attachment_chunks": len(row_chunks), "photo_chunks": len(photo_batches)},
    })
    return merged
