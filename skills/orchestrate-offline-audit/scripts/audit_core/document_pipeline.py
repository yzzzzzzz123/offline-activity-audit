"""Bound maintenance-fee extraction without sharing evidence between source batches."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import validate_json
from .model_metrics import save_model_observations


DOCUMENT_BATCH_SIZE = 3


def maintenance_document_batches(documents: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Keep each source/PDF intact, and isolate dense authoritative documents."""
    batches: list[list[dict[str, Any]]] = []
    pending: list[dict[str, Any]] = []
    for document in documents:
        role = document["role"]
        if role in {"stamped_pos_data", "signed_promotional_contract", "settlement"}:
            if pending:
                batches.append(pending)
                pending = []
            batches.append([document])
        else:
            if pending and (pending[0]["role"] != role or len(pending) >= DOCUMENT_BATCH_SIZE):
                batches.append(pending)
                pending = []
            pending.append(document)
    if pending:
        batches.append(pending)
    return batches


def extract_maintenance_documents(case: dict[str, Any], root: Path, *, codex: str,
                                  skill_dir: Path, full_schema: Path, selected_model: str,
                                  selected_reasoning_effort: str, model_catalog: Path,
                                  max_attempts: int, attempt_timeout_seconds: int) -> dict[str, Any]:
    from . import codex_runner as api

    pipeline_root = root / "model-maintenance_fee"
    pipeline_root.mkdir(parents=True, exist_ok=False)
    batches = maintenance_document_batches(case["document_roles"])
    results: list[dict[str, Any]] = []
    for index, documents in enumerate(batches, 1):
        model_root = pipeline_root / f"documents-{index:03d}"
        model_root.mkdir()
        chunk_case = {**case, "document_roles": documents}
        images, manifest = api._prepare_other_expense_sources(chunk_case, model_root, label="维护费用")
        value = api._run_codex_json(
            codex=codex, model_root=model_root, skill_dir=skill_dir, schema=full_schema,
            raw_output=model_root / "evidence.json",
            prompt=api._maintenance_fee_prompt(skill_dir, full_schema, manifest), images=images,
            selected_model=selected_model, model_catalog=model_catalog,
            label=f"维护费用材料块 {index}/{len(batches)}（{len(documents)}份）",
            max_attempts=max_attempts, attempt_timeout_seconds=attempt_timeout_seconds,
            reasoning_effort=selected_reasoning_effort,
            post_validate=lambda value, scoped=chunk_case: api._validate_maintenance_fee_sources(scoped, value),
        )
        # A failed later batch never invalidates or resubmits an earlier successful call.
        save_model_observations(f"maintenance-documents-{index:03d}", value)
        results.append(value)
    merged = {
        "schema_version": "1.0", "scenario": "maintenance_fee",
        "documents": [document for result in results for document in result["documents"]],
        "extraction_notes": [note for result in results for note in result["extraction_notes"]],
    }
    # Coverage checks remain global, including duplicates and immutable source roles.
    validate_json(merged, full_schema)
    api._validate_maintenance_fee_sources(case, merged)
    by_source = {item["source_file"]: item for item in merged["documents"]}
    merged["documents"] = [by_source[Path(item["path"]).name] for item in case["document_roles"]]
    return merged
