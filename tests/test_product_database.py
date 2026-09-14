from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from PIL import Image
from tests.product_test_support import MemoryOSS, image_objects
from audit_core.product_oss import manifest_key
from audit_core import product_image_runtime as runtime

from audit_core.common import AuditError
from audit_core import product_database as db
from audit_core.product_images import attach_product_reference_images
from audit_core.product_rag import load_product_rag
from audit_core.codex_runner import _select_product_rag_candidates
from audit_core.display import _contract_attachment_knowledge_reconciliation
from audit_core.personnel import _personnel_sales_knowledge_reconciliation


ROWS = [{"product_code": "CP-TEST-001", "product_name": "测试玫瑰清茶牙膏180g", "barcode_69": "6970356167341"}]


class ProductDatabaseTests(unittest.TestCase):
    def test_only_database_fields_are_accepted_and_ids_are_stable(self):
        first = db.catalog_from_rows(ROWS)
        renamed = db.catalog_from_rows([{**ROWS[0], "product_name": "数据库的新名称"}])
        self.assertEqual(first["products"][0]["product_id"], renamed["products"][0]["product_id"])
        self.assertNotEqual(first["data_source"]["sha256"], renamed["data_source"]["sha256"])
        self.assertEqual(first["products"][0]["product_code_aliases"], [])
        self.assertEqual(first["products"][0]["views"], [])
        for bad in ([], ROWS + ROWS, [{**ROWS[0], "aliases": ["SP-1"]}],
                    [{**ROWS[0], "barcode_69": "6970356167342"}], [{**ROWS[0], "product_name": ""}]):
            with self.subTest(bad=bad), self.assertRaises(AuditError):
                db.catalog_from_rows(bad)

    def test_fresh_per_run_snapshot_and_no_mutable_or_cross_run_cache(self):
        newer = [{**ROWS[0], "product_name": "下次查询的名称"}]
        with patch.object(db, "_read_database_rows", side_effect=[ROWS, newer]) as query:
            with db.product_catalog_scope():
                first = db.load_product_catalog()
                first["products"][0]["product_name"] = "调用方改值"
                self.assertEqual(load_product_rag()["products"][0]["product_name"], ROWS[0]["product_name"])
            with db.product_catalog_scope():
                self.assertEqual(db.load_product_catalog()["products"][0]["product_name"], newer[0]["product_name"])
            self.assertEqual(query.call_count, 2)

    def test_database_failure_never_reads_old_catalog(self):
        with patch.object(db, "_read_database_rows", side_effect=AuditError("数据库不可用")), \
             patch("audit_core.common.load_json", side_effect=AssertionError("不能回退JSON")):
            with self.assertRaisesRegex(AuditError, "数据库不可用"):
                load_product_rag()

    def test_reader_uses_tls_viewer_and_rejects_truncated_response(self):
        body = (json.dumps({"row_count": 1}) + "\n" + json.dumps(ROWS[0]) + "\n").encode()
        result = subprocess.CompletedProcess([], 0, stdout=body, stderr=b"")
        with patch.object(db.shutil, "which", return_value="docker"), \
             patch.object(Path, "is_file", return_value=True), patch.object(db.subprocess, "run", return_value=result) as run:
            self.assertEqual(db._read_database_rows(), ROWS)
            args = run.call_args.args[0]
            self.assertIn("--user=product_viewer", args[-1])
            self.assertIn("--ssl-mode=REQUIRED", args[-1])
            self.assertNotIn("root_password", args[-1])
            self.assertIn(b"READ ONLY", run.call_args.kwargs["input"])
            result.stdout = b'{"row_count":2}\n' + body.split(b"\n", 1)[1]
            with self.assertRaises(AuditError):
                db._read_database_rows()

    def test_contract_codes_cannot_use_old_image_library_aliases(self):
        catalog = db.catalog_from_rows(ROWS)
        record = {"line_no": 1, "source_page": 1, "product_code": "SP-1",
                  "product_name": ROWS[0]["product_name"], "barcode_69": ROWS[0]["barcode_69"]}
        contract = {"sales_attachment": {"present": True, "records": [record]}}
        result = _contract_attachment_knowledge_reconciliation(contract, catalog)
        self.assertEqual(result["status"], "fail")
        record["product_code"] = ROWS[0]["product_code"]
        self.assertEqual(_contract_attachment_knowledge_reconciliation(contract, catalog)["status"], "pass")

    def test_personnel_uses_database_without_touching_image_library(self):
        catalog = db.catalog_from_rows(ROWS)
        with patch("audit_core.product_images.attach_product_reference_images", side_effect=AssertionError("人员不查图")):
            result = _personnel_sales_knowledge_reconciliation([
                {"barcode": ROWS[0]["barcode_69"], "product_name": ROWS[0]["product_name"], "quantity": 3, "excel_rows": [2]}
            ], catalog)
        self.assertEqual(result["status"], "pass")


class DatabaseImageBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.catalog = db.catalog_from_rows([{**ROWS[0], "image_manifest_key": manifest_key(ROWS[0]["product_code"])}])
        self.catalog["products"][0]["views"] = [{"view_id": "view-1"}]
        self.remote = MemoryOSS(image_objects(self.catalog))
        self.catalog["products"][0]["views"] = []
        mocked = patch.object(runtime, "ProductOSS", return_value=self.remote)
        mocked.start()
        self.addCleanup(mocked.stop)

    def test_image_lookup_preserves_database_identity_and_has_no_legacy_text(self):
        product = attach_product_reference_images(self.catalog)["products"][0]
        self.assertEqual(product["product_name"], ROWS[0]["product_name"])
        self.assertTrue(product["product_id"].startswith("db-"))
        self.assertEqual(product["product_code_aliases"], [])
        self.assertEqual(product["views"][0]["visible_anchors"], [])
        self.assertEqual(self.catalog["products"][0]["views"], [])

    def test_manifest_requires_exact_pair_and_absent_pointer_keeps_identity(self):
        key = self.catalog["products"][0]["image_manifest_key"]
        wrong = json.loads(self.remote.objects[key])
        wrong["barcode_69"] = "6970356161042"
        self.remote.objects[key] = json.dumps(wrong).encode()
        with self.assertRaises(AuditError):
            attach_product_reference_images(self.catalog)
        result = attach_product_reference_images(db.catalog_from_rows(ROWS))
        self.assertEqual(result["products"][0]["product_name"], ROWS[0]["product_name"])
        self.assertEqual(result["products"][0]["views"], [])
        self.assertEqual(result["products"][0]["match_policy"], "candidate_only")

    def test_local_catalog_is_not_an_accepted_image_source(self):
        without_source = deepcopy(self.catalog)
        without_source.pop("data_source")
        with self.assertRaisesRegex(AuditError, "MySQL"):
            attach_product_reference_images(without_source)


class DatabaseOnboardingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = Path(__file__).resolve().parents[1] / "skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py"
        spec = importlib.util.spec_from_file_location("database_onboarding", path)
        cls.onboarding = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.onboarding)

    def test_matching_keeps_database_code_case_and_same_barcode_candidates(self):
        candidates = [{"product_code": code, "product_name": ROWS[0]["product_name"], "barcode": ROWS[0]["barcode_69"]}
                      for code in ("CP-001", "cp-001")]
        found = self.onboarding._valid_exact_candidates({"list": candidates}, ROWS[0]["barcode_69"])
        self.assertEqual([p["product_code"] for p in found], ["CP-001", "cp-001"])

    def test_publish_validation_rechecks_database_and_rejects_stale_or_forged_identity(self):
        catalog = db.catalog_from_rows(ROWS)
        plan = {"schema_version": "2.0", "kind": "product-oss-onboarding-plan",
                "source_root": str(Path(__file__).resolve().parent),
                "database": catalog["data_source"], "products": [{"selected": dict(ROWS[0])}]}
        with patch.object(self.onboarding, "sha256_file", return_value="image-index"), \
             patch.object(self.onboarding, "_validate_source_root"), \
             patch.object(self.onboarding, "load_product_catalog", return_value=catalog):
            stale = deepcopy(plan)
            stale["database"]["sha256"] = "stale"
            with self.assertRaisesRegex(AuditError, "重新 match"):
                self.onboarding._prepare_publish(stale)
            forged = deepcopy(plan)
            forged["products"][0]["selected"]["product_name"] = "旧JSON中的名称"
            with self.assertRaisesRegex(AuditError, "当前数据库核验"):
                self.onboarding._prepare_publish(forged)


if __name__ == "__main__":
    unittest.main()
