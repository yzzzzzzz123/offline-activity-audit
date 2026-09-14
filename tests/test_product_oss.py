from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image

from audit_core.common import AuditError
from audit_core import product_oss as oss
from audit_core import product_image_runtime as runtime
from audit_core.product_database import catalog_from_rows, product_catalog_scope
from audit_core.product_images import attach_product_reference_images
from audit_core.codex_runner import _copy_product_reference_images, _select_product_rag_candidates


ROW = {"product_code": "CP-TEST-001", "barcode_69": "6970356167341", "product_name": "测试商品"}


def fixture():
    output = io.BytesIO()
    Image.new("RGB", (8, 8), "blue").save(output, format="PNG")
    picture = output.getvalue()
    view = {"view_id": "view-1", "object_key": oss.product_prefix(ROW["product_code"]) + "view-1.png",
            "sha256": hashlib.sha256(picture).hexdigest(), "size_bytes": len(picture),
            "content_type": "image/png", "width": 8, "height": 8}
    value = {"schema_version": "1.0", "product_code": ROW["product_code"], "barcode_69": ROW["barcode_69"], "views": [view]}
    catalog = catalog_from_rows([{**ROW, "image_manifest_key": oss.manifest_key(ROW["product_code"])}])
    return picture, value, catalog


class FakeOSS:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def read(self, key, *, limit, output=None, expected_sha=None, expected_size=None):
        self.calls.append(key)
        data = self.objects[key]
        if len(data) > limit or expected_size not in (None, len(data)):
            raise AuditError("长度不一致")
        if expected_sha and hashlib.sha256(data).hexdigest() != expected_sha:
            raise AuditError("SHA-256 校验失败")
        if output is not None:
            output.write(data)
            return hashlib.sha256(data).hexdigest()
        return data


class ProductOSSBoundaryTests(unittest.TestCase):
    def test_only_approved_product_keys_no_urls_traversal_or_other_modules(self):
        oss.check_key(oss.manifest_key(ROW["product_code"]))
        for value in ("offline-verify/test/a.zip", "product-reference/CP/a.png",
                      "https://example.com/a.png?secret=x", oss.PREFIX + "CP/../a.png",
                      oss.PREFIX + "CP/a.png?token=x", oss.PREFIX + "CP\\a.png",
                      oss.PREFIX + "../manifest.json", oss.PREFIX + "CP/a%2f.png"):
            with self.subTest(key=value), self.assertRaises(AuditError):
                oss.check_key(value)

    def test_manifest_strict_identity_not_legacy_text_or_arbitrary_url(self):
        _, value, _ = fixture()
        oss.validate_manifest(oss.json_bytes(value), ROW)
        mutations = [dict(product_code="CP-OTHER"), dict(barcode_69="6970356161042"),
                     dict(product_name="外部商品名称"), dict(views=[])]
        for change in mutations:
            with self.subTest(change=change), self.assertRaises(AuditError):
                oss.validate_manifest(oss.json_bytes({**value, **change}), ROW)
        for change in ({"object_key": oss.PREFIX + "CP-OTHER/a.png"}, {"sha256": "bad"},
                       {"size_bytes": True}, {"size_bytes": oss.MAX_IMAGE_BYTES + 1},
                       {"content_type": "text/html"}, {"width": 0}, {"image_url": "https://untrusted/"}):
            changed = deepcopy(value)
            changed["views"][0].update(change)
            with self.subTest(change=change), self.assertRaises(AuditError):
                oss.validate_manifest(oss.json_bytes(changed), ROW)
        value["views"].append(deepcopy(value["views"][0]))
        with self.assertRaises(AuditError):
            oss.validate_manifest(oss.json_bytes(value), ROW)

    def test_database_pointer_separate_hash_and_strict_identity_unchanged(self):
        before = catalog_from_rows([ROW])
        after = catalog_from_rows([{**ROW, "image_manifest_key": oss.manifest_key(ROW["product_code"])}])
        self.assertEqual(before["data_source"]["sha256"], after["data_source"]["sha256"])
        self.assertNotEqual(before["data_source"]["image_manifest_keys_sha256"], after["data_source"]["image_manifest_keys_sha256"])
        for key in ("https://example.com/manifest.json?token=x", oss.manifest_key("CP-OTHER"), ""):
            with self.assertRaises(AuditError):
                catalog_from_rows([{**ROW, "image_manifest_key": key}])

    def test_no_silent_local_fallback_when_configured(self):
        with patch.object(runtime, "ProductOSS", side_effect=AuditError("网络不可用")):
            _, _, catalog = fixture()
            with self.assertRaisesRegex(AuditError, "网络不可用"):
                attach_product_reference_images(catalog)
            missing = attach_product_reference_images(catalog_from_rows([ROW]))
            self.assertEqual(missing["products"][0]["views"], [])
            self.assertEqual(missing["products"][0]["match_policy"], "candidate_only")

    def test_missing_or_invalid_oss_configuration_fails_closed(self):
        with TemporaryDirectory() as folder:
            configuration = Path(folder) / "product-images.json"
            with patch.object(oss, "SETTINGS", configuration):
                with self.assertRaisesRegex(AuditError, "配置缺失"):
                    attach_product_reference_images(fixture()[2])
                configuration.write_text('{"mode":"local"}', encoding="utf-8")
                with self.assertRaises(AuditError):
                    oss.ProductOSS()

    def test_benchmark_fingerprint_streams_remote_bytes_and_detects_corruption(self):
        picture, manifest, catalog = fixture()
        key = manifest["views"][0]["object_key"]
        remote = FakeOSS({oss.manifest_key(ROW["product_code"]): oss.json_bytes(manifest), key: picture})
        with patch.object(oss, "ProductOSS", return_value=remote):
            result = oss.reference_fingerprint(catalog)
            self.assertEqual(result["oss-images/" + key]["sha256"], hashlib.sha256(picture).hexdigest())
            remote.objects[key] += b"corrupt"
            with self.assertRaisesRegex(AuditError, "长度|SHA-256"):
                oss.reference_fingerprint(catalog)

    def test_dpapi_roundtrip_not_plaintext(self):
        if os.name != "nt":
            self.skipTest("Windows DPAPI 专用测试")
        value = b"synthetic-non-secret-test-value"
        encrypted = oss._crypt(value)
        self.assertNotIn(value, encrypted)
        self.assertEqual(oss._crypt(encrypted, decrypt=True), value)

    def test_service_error_never_exposes_original_auth_response(self):
        service = SimpleNamespace()
        class Error(Exception):
            code = "AccessDenied"
        operation = Mock(side_effect=Error("SECRET_SENTINEL in signed URL"))
        with self.assertRaises(AuditError) as caught:
            oss.ProductOSS._call(service, operation, object())
        self.assertIn("AccessDenied", str(caught.exception))
        self.assertNotIn("SECRET_SENTINEL", str(caught.exception))


class ProductImageRuntimeTests(unittest.TestCase):
    def test_database_candidate_selected_before_any_manifest_or_image_is_loaded(self):
        picture, value, catalog = fixture()
        manifest = oss.manifest_key(ROW["product_code"])
        key = value["views"][0]["object_key"]
        fake = FakeOSS({manifest: oss.json_bytes(value), key: picture})
        self.assertEqual(catalog["products"][0]["views"], [])
        query = {"photo_queries": [{"visible_barcodes_69": [ROW["barcode_69"]]}]}
        with TemporaryDirectory() as folder, patch.object(runtime, "ProductOSS", return_value=fake):
            with product_catalog_scope():
                selected = _select_product_rag_candidates(catalog, query)
                self.assertEqual(len(selected["products"]), 1)
                self.assertEqual(selected["data_source"], catalog["data_source"])
                self.assertEqual(fake.calls, [])
                enriched = runtime.attach_oss_images(selected)
                self.assertEqual(fake.calls, [manifest])
                copied = _copy_product_reference_images(Path(folder), enriched)
                self.assertEqual(len(copied), 1)
                self.assertEqual(copied[0]["path"].read_bytes(), picture)
                self.assertEqual(fake.calls, [manifest, key])
                generic = _select_product_rag_candidates(catalog, {"photo_queries": [{"visible_text": ["品牌"]}]})
                self.assertEqual(generic["products"], [])
                cache_root = Path(runtime._STATE.get().temporary.name)
            self.assertFalse(cache_root.exists())

    def test_metadata_only_then_selected_download_cached_within_run_and_cleaned(self):
        picture, value, catalog = fixture()
        manifest = oss.manifest_key(ROW["product_code"])
        key = value["views"][0]["object_key"]
        fake = FakeOSS({manifest: oss.json_bytes(value), key: picture})
        with TemporaryDirectory() as folder, patch.object(runtime, "ProductOSS", return_value=fake):
            with product_catalog_scope():
                enriched = runtime.attach_oss_images(catalog)
                self.assertEqual(fake.calls, [manifest])
                for name in ("one", "two"):
                    destination = Path(folder) / name
                    destination.mkdir()
                    copied = _copy_product_reference_images(destination, enriched)
                    self.assertEqual(len(copied), 1)
                    self.assertEqual(copied[0]["path"].read_bytes(), picture)
                self.assertEqual(fake.calls, [manifest, key])
                cache_root = Path(runtime._STATE.get().temporary.name)
                self.assertTrue(cache_root.is_dir())
                runtime.attach_oss_images(catalog)
                self.assertEqual(fake.calls, [manifest, key])
            self.assertFalse(cache_root.exists())
            with product_catalog_scope():
                runtime.attach_oss_images(catalog)
            self.assertEqual(fake.calls, [manifest, key, manifest])

    def test_exception_also_cleans_temp_and_partial_bytes(self):
        picture, value, catalog = fixture()
        key = value["views"][0]["object_key"]
        fake = FakeOSS({oss.manifest_key(ROW["product_code"]): oss.json_bytes(value), key: picture})
        with TemporaryDirectory() as folder, patch.object(runtime, "ProductOSS", return_value=fake):
            with self.assertRaisesRegex(AuditError, "后续步骤失败"):
                with product_catalog_scope():
                    enriched = runtime.attach_oss_images(catalog)
                    runtime.copy_oss_image(enriched["products"][0], enriched["products"][0]["views"][0], Path(folder) / "image.png")
                    cache_root = Path(runtime._STATE.get().temporary.name)
                    raise AuditError("后续步骤失败")
            self.assertFalse(cache_root.exists())

    def test_corrupted_or_updated_images_fail_closed_no_output(self):
        picture, value, catalog = fixture()
        manifest = oss.manifest_key(ROW["product_code"])
        key = value["views"][0]["object_key"]
        fake = FakeOSS({manifest: oss.json_bytes(value), key: picture + b"bad"})
        with TemporaryDirectory() as folder, patch.object(runtime, "ProductOSS", return_value=fake):
            with product_catalog_scope():
                enriched = runtime.attach_oss_images(catalog)
                target = Path(folder) / "image.png"
                with self.assertRaises(AuditError):
                    runtime.copy_oss_image(enriched["products"][0], enriched["products"][0]["views"][0], target)
                self.assertFalse(target.exists())
                self.assertEqual(list(Path(runtime._STATE.get().temporary.name).iterdir()), [])
                self.assertEqual(fake.calls.count(key), 2)

    def test_decode_and_size_metadata_cannot_be_faked(self):
        picture, value, _ = fixture()
        view = value["views"][0]
        for data, metadata in ((b"not an image", {}), (picture, {"width": 9}),
                               (picture, {"content_type": "image/jpeg"})):
            fake_view = {**view, **metadata, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
            fake = FakeOSS({view["object_key"]: data})
            with TemporaryDirectory() as folder, patch.object(runtime, "ProductOSS", return_value=fake):
                target = Path(folder) / "image.png"
                with self.assertRaises(AuditError):
                    runtime._download(fake_view, target)
                self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
