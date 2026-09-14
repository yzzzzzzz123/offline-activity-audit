from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from audit_core import codex_runner as api
from audit_core.common import AuditError, validate_json
from audit_core.document_pipeline import extract_maintenance_documents, maintenance_document_batches
from audit_core.model_metrics import collect_model_artifacts
from test_maintenance_fee import _document, EVIDENCE_SCHEMA


class DocumentPipelineTests(unittest.TestCase):
    def test_batches_keep_authority_documents_and_each_pdf_intact(self):
        roles = ["activity_photo"] * 4 + ["signed_promotional_contract", "stamped_pos_data", "settlement"] + ["supporting_document"] * 5
        documents = [{"path": f"source-{i}.pdf", "role": role} for i, role in enumerate(roles)]
        batches = maintenance_document_batches(documents)
        self.assertEqual([len(batch) for batch in batches], [3, 1, 1, 1, 1, 3, 2])
        self.assertEqual([document for batch in batches for document in batch], documents)

    def run_chunks(self, *, corrupt=False, fail_after=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            docs = []
            for i in range(7):
                path = sources / f"source-{i}.png"
                Image.new("RGB", (8, 8), (i, 0, 0)).save(path)
                docs.append({"path": str(path), "role": "supporting_document"})
            case = {"scenario": "maintenance_fee", "document_roles": docs}
            calls = []

            def model(**kwargs):
                calls.append(kwargs)
                index = len(calls)
                if fail_after is not None and index > fail_after:
                    raise api.CodexExtractionError("当前块失败")
                visible = [p.name for p in kwargs["images"]]
                staged = sorted(p.name for p in kwargs["model_root"].glob("*.png"))
                self.assertEqual(sorted(visible), staged)
                self.assertLessEqual(len(staged), 3)
                value = {"schema_version": "1.0", "scenario": "maintenance_fee",
                         "documents": [_document(name, "supporting_document") for name in reversed(visible)],
                         "extraction_notes": []}
                if corrupt:
                    value["documents"][0]["source_file"] = "source-outside-current-batch.png"
                validate_json(value, EVIDENCE_SCHEMA)
                kwargs["post_validate"](value)
                return value

            with collect_model_artifacts(root / "analysis"), patch.object(api, "_run_codex_json", side_effect=model):
                try:
                    result = extract_maintenance_documents(
                        case, root, codex="mock", skill_dir=EVIDENCE_SCHEMA.parents[1],
                        full_schema=EVIDENCE_SCHEMA, selected_model="gpt-6-astra",
                        selected_reasoning_effort="medium", model_catalog=root / "catalog.json",
                        max_attempts=3, attempt_timeout_seconds=3600,
                    )
                except api.CodexExtractionError:
                    self.assertEqual(len(calls), fail_after + 1)
                    checkpoints = sorted((root / "analysis/model-observations").glob("*.json"))
                    self.assertEqual(len(checkpoints), fail_after)
                    self.assertEqual(len(json.loads(checkpoints[0].read_text(encoding="utf-8"))["documents"]), 3)
                    return
            self.assertEqual(len(calls), 3)
            self.assertEqual([d["source_file"] for d in result["documents"]], [Path(d["path"]).name for d in docs])
            self.assertEqual([p.name for call in calls for p in call["images"]], [Path(d["path"]).name for d in docs])

    def test_sources_are_isolated_complete_and_returned_in_original_order(self):
        self.run_chunks()

    def test_cross_batch_source_is_rejected(self):
        with self.assertRaises(AuditError):
            self.run_chunks(corrupt=True)

    def test_later_failure_preserves_successful_batch_without_resubmission(self):
        self.run_chunks(fail_after=1)


if __name__ == "__main__":
    unittest.main()
