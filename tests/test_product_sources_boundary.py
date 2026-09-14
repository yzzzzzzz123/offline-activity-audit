"""数据库/OSS 唯一来源及新品只读规划的回归保护。"""
from __future__ import annotations

import argparse
from contextlib import redirect_stdout
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.product_database import catalog_from_rows
from audit_core.product_rag import catalog_product_name_values
from audit_core.codex_runner import _product_candidate_score, _copy_product_reference_images
from tests.product_test_support import png_bytes


ROOT = Path(__file__).resolve().parents[1]


class ProductSourceBoundaryTests(unittest.TestCase):
    def test_runtime_skills_and_maintenance_have_no_backup_dependency(self):
        forbidden = "canban-product-" + "multimodal-knowledge-base"
        failures = []
        for directory in ("audit_core", "scripts", "skills", "shared/product-database"):
            for path in (ROOT / directory).rglob("*"):
                if "runtime" in path.parts or "__pycache__" in path.parts:
                    continue
                if path.is_file() and path.suffix in {".py", ".md", ".json", ".yaml", ".ps1", ".sh"}:
                    if forbidden in path.read_text(encoding="utf-8-sig"):
                        failures.append(path.relative_to(ROOT).as_posix())
        self.assertEqual(failures, [])

    def test_legacy_loaders_are_not_exposed(self):
        from audit_core import product_rag, product_images
        for module, attributes in (
            (product_rag, ("load_legacy_image_catalog", "load_pending_product_rag", "SHARED_PRODUCT_RAG_DIR")),
            (product_images, ("attach_local_product_reference_images",)),
        ):
            for attribute in attributes:
                self.assertFalse(hasattr(module, attribute))

    def test_legacy_aliases_and_view_descriptions_never_supply_identity(self):
        product = {"product_name": "测试牙刷", "product_code": "TEST-CODE", "barcode_69": "6970356167341",
                   "aliases": ["独特无关商品"], "product_code_aliases": ["SP-1"],
                   "specification": "历史款式", "sources": [{"observed_product_name": "伪造观察"}],
                   "views": [{"visible_anchors": ["旧文字"]}]}
        self.assertEqual(catalog_product_name_values(product), ["测试牙刷"])
        for visible in ("独特无关商品", "SP-1", "历史款式", "伪造观察", "旧文字"):
            self.assertEqual(_product_candidate_score(product, {"visible_text": [visible]}), 0)

    def test_local_file_view_cannot_be_copied(self):
        with TemporaryDirectory() as temporary:
            product = {"product_id": "db-test", "views": [{"view_id": "v1", "image_file": "existing.png"}]}
            with self.assertRaisesRegex(AuditError, "OSS Object Key"):
                _copy_product_reference_images(Path(temporary), {"products": [product]})
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_onboarding_plan_only_uses_database_and_explicit_new_inputs(self):
        spec = importlib.util.spec_from_file_location("isolated_onboarding", ROOT / "skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py")
        onboarding = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(onboarding)
        row = {"product_name": "测试玫瑰清茶牙膏180g", "product_code": "CP-TEST-001", "barcode_69": "6970356167341"}
        catalog = catalog_from_rows([row])
        with TemporaryDirectory() as temporary, patch.object(onboarding, "load_product_catalog", return_value=catalog):
            root = Path(temporary)
            source = root / "new-inputs"
            folder = source / "one"
            folder.mkdir(parents=True)
            image = folder / "front.png"
            image.write_bytes(png_bytes())
            observation = {"source_folder": "one", "source_id": "test-001", "brand": "测试",
                           "observed_product_name": row["product_name"], "observed_specification": "180g",
                           "observed_variant": None, "barcode_69": row["barcode_69"],
                           "name_evidence_files": ["front.png"], "barcode_evidence_files": ["front.png"]}
            observations = root / "observations.json"
            observations.write_text(json.dumps({"schema_version": "1.0", "observations": [observation]}), encoding="utf-8")
            output = root / "plan.json"
            args = argparse.Namespace(source_root=str(source), observations=str(observations), output=str(output))
            with redirect_stdout(io.StringIO()):
                self.assertEqual(onboarding._match(args), 0)
            plan = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(plan["schema_version"], "2.0")
            operations = onboarding._prepare_publish(plan)
            self.assertEqual(operations[0]["product_code"], row["product_code"])
            changed = deepcopy(plan)
            changed["database"]["image_manifest_keys_sha256"] = "changed"
            with self.assertRaisesRegex(AuditError, "重新 match"):
                onboarding._prepare_publish(changed)
            image.write_bytes(png_bytes("red"))
            with self.assertRaisesRegex(AuditError, "来源图片"):
                onboarding._prepare_publish(plan)
