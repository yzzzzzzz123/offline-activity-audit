"""Private OSS handover uses fake keys in tests and never contacts external services."""
from contextlib import redirect_stdout
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import runpy
import ssl
import sys
import tarfile
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "deploy"))
import deployment_credentials as credentials
import build_release
import manage
import configuration
from delivery_configuration import APPLICATION_MEMBER,DRAFT_MEMBER,CALLBACK_URL,prepared_configuration

FAKE = {"accessKeyId": "FixtureAccessKey123456789", "accessKeySecret": "FixtureAccessSecret123456789"}
DATA = credentials.checked_product_oss_bytes(json.dumps(FAKE))


class OSSCredentialDeliveryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.addCleanup(os.chdir,Path.cwd())
        self.root = Path(temporary.name)
        seed = self.root / "deploy/seed/product-catalog.sql"
        seed.parent.mkdir(parents=True)
        seed.write_text("-- isolated fixture\n", encoding="utf-8")
        seed.with_suffix(".sql.sha256").write_text(hashlib.sha256(seed.read_bytes()).hexdigest(), encoding="utf-8")
        seed.with_suffix(".manifest.json").write_text(json.dumps({"rows": 1, "sha256": "a" * 64}), encoding="utf-8")
        (self.root / "README.md").write_text("fixture", encoding="utf-8")
        self.source = self.root / credentials.PRODUCT_OSS_MEMBER
        self.source.parent.mkdir(parents=True)
        self.source.write_bytes(DATA)
        self.source.chmod(0o600)

    def build(self, included, prepared=False):
        output = self.root / "release"
        if prepared:
            (self.root/'deploy/application.env.example').write_bytes((ROOT/'deploy/application.env.example').read_bytes())
        with patch.object(build_release, "ROOT", self.root), patch.object(
            build_release, "current_product_oss_bytes", return_value=DATA
        ) as exporter, redirect_stdout(io.StringIO()) as messages:
            build_release.build(output, include_product_oss_credentials=included,prepare_application_config=prepared)
        self.assertNotIn(FAKE["accessKeySecret"], messages.getvalue())
        exporter.assert_called_once() if included else exporter.assert_not_called()
        return next(output.glob("*.tar.gz"))

    def prepared_config(self):
        (self.root/'deploy/application.env.example').write_bytes((ROOT/'deploy/application.env.example').read_bytes())
        directory=self.root/'config'
        directory.mkdir(mode=0o700)
        for name,data in prepared_configuration(self.root).items():
            path=self.root/name
            path.write_bytes(data)
            path.chmod(0o600)

    def configure(self):
        template = ROOT / "deploy/application.env.example"
        (self.root / "deploy/application.env.example").write_bytes(template.read_bytes())
        ca = self.root / "ca.pem"
        ca.write_text("fixture", encoding="utf-8")
        args = SimpleNamespace(bind="127.0.0.1", port=18084, rds_host="rds.example.invalid",
            rds_port=3306, rds_database="customer_catalog", rds_user="customer_viewer", rds_ca=str(ca))
        with patch.object(manage, "ROOT", self.root), patch.object(
            manage.ssl, "create_default_context", return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ), redirect_stdout(io.StringIO()) as messages:
            manage.configure(args)
        self.assertNotIn(FAKE["accessKeySecret"], messages.getvalue())
        return self.root / "config/secrets/product-oss.json"

    def test_default_build_excludes_private_credentials_even_when_present(self):
        archive = self.build(False)
        with tarfile.open(archive) as package:
            self.assertNotIn(credentials.PRODUCT_OSS_MEMBER, package.getnames())
            manifest = json.load(package.extractfile("DELIVERY-MANIFEST.json"))
            self.assertFalse(manifest["includes_product_oss_credentials"])

    def test_authorized_build_contains_exact_key_and_private_mode(self):
        archive = self.build(True)
        with tarfile.open(archive) as package:
            member = package.getmember(credentials.PRODUCT_OSS_MEMBER)
            self.assertEqual(member.mode, 0o600)
            self.assertEqual(package.extractfile(member).read(), DATA)
            manifest = json.load(package.extractfile("DELIVERY-MANIFEST.json"))
            self.assertTrue(manifest["includes_product_oss_credentials"])
        self.assertIn("with-oss-", archive.name)

    def test_prepared_package_contains_actual_config_and_known_callback(self):
        archive=self.build(True,prepared=True)
        with tarfile.open(archive) as package:
            text=package.extractfile(APPLICATION_MEMBER).read().decode('utf-8')
            self.assertIn('OFFLINE_AUDIT_OSS_CALLBACK_URL='+CALLBACK_URL,text)
            self.assertIn('OFFLINE_AUDIT_OSS_ENABLED=1',text)
            for name in (APPLICATION_MEMBER,DRAFT_MEMBER):
                self.assertEqual(package.getmember(name).mode,0o600)
            self.assertNotIn('config/secrets/rds_password.txt',package.getnames())

    def test_prepared_config_is_completed_without_losing_callback_or_oss_key(self):
        self.prepared_config()
        target=self.configure()
        self.assertEqual(target.read_bytes(),DATA)
        text=(self.root/APPLICATION_MEMBER).read_text(encoding='utf-8')
        self.assertIn('OFFLINE_AUDIT_PRODUCT_DB_HOST=rds.example.invalid',text)
        self.assertIn('OFFLINE_AUDIT_OSS_CALLBACK_URL='+CALLBACK_URL,text)
        self.assertIn('OFFLINE_AUDIT_OSS_ENABLED=1',text)
        self.assertFalse((self.root/DRAFT_MEMBER).exists())
        with self.assertRaisesRegex(RuntimeError,'already exists'):
            self.configure()

    def test_changed_prepared_config_is_not_overwritten(self):
        self.prepared_config()
        path=self.root/APPLICATION_MEMBER
        changed=path.read_bytes()+b'# customer edit\n'
        path.write_bytes(changed)
        with self.assertRaisesRegex(RuntimeError,'modified'):
            self.configure()
        self.assertEqual(path.read_bytes(),changed)
        self.assertFalse((self.root/'config/secrets').exists())

    def test_service_does_not_start_with_uninitialized_delivered_config(self):
        self.prepared_config()
        with patch.object(configuration,'ROOT',self.root),self.assertRaisesRegex(RuntimeError,'README 6.1'):
            runpy.run_path(str(ROOT/'deploy/service.py'),run_name='__main__')

    def test_enabled_intake_requires_rds_password_before_service_start(self):
        self.prepared_config()
        self.configure()
        with patch.object(configuration,'ROOT',self.root),patch.dict(os.environ,{},clear=True),self.assertRaisesRegex(RuntimeError,'RDS'):
            runpy.run_path(str(ROOT/'deploy/service.py'),run_name='__main__')

    def test_enabled_intake_requires_model_key_before_service_start(self):
        from audit_core.model_settings import ModelSettingsError
        self.prepared_config()
        self.configure()
        (self.root/'config/secrets/rds_password.txt').write_text('fixture-only-password\n',encoding='utf-8')
        with patch.object(configuration,'ROOT',self.root),patch.dict(os.environ,{},clear=True),self.assertRaises(ModelSettingsError):
            runpy.run_path(str(ROOT/'deploy/service.py'),run_name='__main__')

    def test_configuration_imports_key_and_blank_interactive_input_preserves_it(self):
        target = self.configure()
        self.assertEqual(target.read_bytes(), DATA)
        if os.name != "nt":
            self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        with patch.object(manage, "ROOT", self.root), patch.object(
            manage.getpass, "getpass", return_value=""
        ), redirect_stdout(io.StringIO()):
            manage.secret_configuration(None)
        self.assertEqual(target.read_bytes(), DATA)
        with self.assertRaisesRegex(RuntimeError, "already exists"):
            self.configure()
        self.assertEqual(target.read_bytes(), DATA)

    def test_invalid_bundle_fails_before_creating_configuration(self):
        self.source.write_text('{"accessKeyId":"invalid"}', encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Invalid product OSS"):
            self.configure()
        self.assertFalse((self.root / "config").exists())

    @unittest.skipIf(os.name == "nt", "POSIX file permissions are checked on Linux.")
    def test_world_readable_bundle_is_rejected(self):
        self.source.chmod(0o644)
        with self.assertRaises(Exception):
            self.configure()
        self.assertFalse((self.root / "config").exists())

    def test_configuration_without_private_bundle_still_creates_empty_template(self):
        self.source.unlink()
        target = self.configure()
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {})

    def test_package_verification_rejects_broadened_private_file_permissions(self):
        archive = self.build(True)
        altered = self.root / "altered.tar.gz"
        with tarfile.open(archive) as original, tarfile.open(altered, "w:gz") as output:
            for member in original.getmembers():
                data = original.extractfile(member).read()
                if member.name == credentials.PRODUCT_OSS_MEMBER:
                    member.mode = 0o644
                output.addfile(member, io.BytesIO(data))
        with self.assertRaisesRegex(RuntimeError, "incorrectly protected"):
            build_release.verify(altered)


if __name__ == "__main__":
    unittest.main()
