from __future__ import annotations

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import importlib.util
import io
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from PIL import Image

from audit_core.common import AuditError
from audit_core.product_oss import OWNER, json_bytes, manifest_key
from tests.test_product_oss import FakeOSS, ROW, fixture


class PublishTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        folder = Path(__file__).resolve().parents[1] / "shared/product-database"
        spec = importlib.util.spec_from_file_location("image_publisher_test", folder / "publish_product_images.py")
        cls.publisher = importlib.util.module_from_spec(spec)
        with patch.object(sys, "path", [str(folder), *sys.path]):
            spec.loader.exec_module(cls.publisher)

    def test_plan_preserves_view_ids_and_requires_complete_fixed_names(self):
        picture, old, catalog = fixture()
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "view-1.png"
            source.write_bytes(picture)
            old["views"][0]["view_id"] = "stable-business-view"
            plan, paths = self.publisher.make_plan(catalog["products"][0], Path(temporary), old)
            self.assertEqual(plan["views"][0]["view_id"], "stable-business-view")
            self.assertEqual(paths, [source])
            source.rename(Path(temporary) / "new-version.png")
            with self.assertRaisesRegex(AuditError, "沿用现有视图文件名"):
                self.publisher.make_plan(catalog["products"][0], Path(temporary), old)

    def test_plan_rejects_mixed_files_and_undecodable_images(self):
        picture, old, catalog = fixture()
        with TemporaryDirectory() as temporary:
            source = Path(temporary) / "view-1.png"
            source.write_bytes(picture)
            foreign = Path(temporary) / "business.txt"
            foreign.write_text("不能上传其他材料", encoding="utf-8")
            with self.assertRaises(AuditError):
                self.publisher.make_plan(catalog["products"][0], Path(temporary), old)
            foreign.unlink()
            source.write_bytes(b"bad image")
            with self.assertRaises(OSError):
                self.publisher.make_plan(catalog["products"][0], Path(temporary), old)

    def test_preview_no_writes_and_apply_updates_only_same_product_keys(self):
        picture, old, catalog = fixture()
        key = old["views"][0]["object_key"]
        manifest = manifest_key(ROW["product_code"])
        class Storage(FakeOSS):
            writes = []
            def head(self, key):
                return SimpleNamespace(metadata={"managed-by": OWNER}) if key in self.objects else None
            def put(self, key, body, **kwargs):
                self.writes.append(key)
                self.objects[key] = body if isinstance(body, bytes) else body.read()
        storage = Storage({key: picture, manifest: json_bytes(old)})
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            Image.new("RGB", (8, 8), "red").save(source / "view-1.png")
            args = ["publisher", "--product-code", ROW["product_code"], "--source-dir", str(source)]
            with patch.object(self.publisher, "ROOT", root), \
                 patch.object(self.publisher, "load_product_catalog", side_effect=lambda: deepcopy(catalog)), \
                 patch.object(self.publisher, "ProductOSS", return_value=storage), \
                 redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                with patch.object(sys, "argv", args):
                    self.assertEqual(self.publisher.main(), 0)
                self.assertEqual(storage.writes, [])
                with patch.object(sys, "argv", [*args, "--apply"]):
                    self.assertEqual(self.publisher.main(), 0)
                self.assertEqual(storage.writes, [key, manifest])
                self.assertEqual(storage.objects[key], (source / "view-1.png").read_bytes())
                self.assertEqual(set(storage.objects), {key, manifest})
                self.assertEqual(list(root.rglob("*.lock")), [])


if __name__ == "__main__":
    unittest.main()
