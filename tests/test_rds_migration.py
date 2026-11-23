"""Exercise RDS migration refusal and transaction boundaries without a live database."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import importlib.util
import json
from pathlib import Path
import ssl
import sys
import tempfile
import types
import unittest
from unittest.mock import MagicMock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("delivery_rds_migration", ROOT / "deploy/rds.py")
rds = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(rds)

SCHEMA = [
    ("barcode_69", "char", "NO", 13, "ascii_bin", "MUL"),
    ("product_name", "varchar", "NO", 512, "utf8mb4_bin", ""),
    ("product_code", "varchar", "NO", 64, "ascii_bin", "PRI"),
    ("image_manifest_key", "varchar", "YES", 1024, "utf8mb4_bin", ""),
]


class RDSMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.password = self.root / "password"
        self.password.write_text("fixture-password\n", encoding="utf-8")
        self.password.chmod(0o600)
        self.rows = [{"barcode_69": "6970356167341", "product_name": "测试商品",
                      "product_code": "TEST-1", "image_manifest_key": None}]
        self.sql = self.root / "products.sql"
        self.sql.write_text("INSERT INTO `products` VALUES ('6970356167341','测试商品','TEST-1',NULL);\n",
                            encoding="utf-8")
        self.manifest = {"rows": 1, "sha256": hashlib.sha256(json.dumps(
            self.rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()}
        self.write_checksums()

    def write_checksums(self):
        digest = hashlib.sha256(self.sql.read_bytes()).hexdigest()
        self.sql.with_suffix(".sql.sha256").write_text(digest + "  products.sql\n", encoding="utf-8")
        self.sql.with_suffix(".manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def arguments(self, command="import"):
        return types.SimpleNamespace(command=command, host="customer-rds.example", port=3306,
            database="customer_catalog", user="migration_fixture", ca=str(self.root / "ca.pem"),
            password_file=str(self.password), file=str(self.sql), manifest=None,
            read_only=False, apply=True)

    @contextmanager
    def database(self, *, existing=0, restored_rows=None, schema=SCHEMA):
        cursor = MagicMock()
        cursor.__enter__.return_value = cursor

        def fetchone():
            statement = cursor.execute.call_args.args[0]
            if statement.startswith("SHOW SESSION"): return ("Ssl_cipher", "TLS_AES_256_GCM_SHA384")
            if statement == "SELECT VERSION()": return ("8.4.0",)
            if "information_schema.TABLES" in statement: return (existing,)
            if statement == "SELECT COUNT(*) FROM products": return (existing,)
            raise AssertionError("Unexpected fixture query: " + statement)

        def fetchall():
            if "information_schema.COLUMNS" in cursor.execute.call_args.args[0]: return schema
            return [(json.dumps(row),) for row in (self.rows if restored_rows is None else restored_rows)]

        cursor.fetchone.side_effect = fetchone
        cursor.fetchall.side_effect = fetchall
        cursor.nextset.return_value = None
        connection = MagicMock()
        connection.__enter__.return_value = connection
        connection.cursor.return_value = cursor
        module = types.SimpleNamespace(connect=MagicMock(return_value=connection),
            constants=types.SimpleNamespace(CLIENT=types.SimpleNamespace(MULTI_STATEMENTS=65536)))
        with patch.dict(sys.modules, {"pymysql": module}), patch.object(
            rds.ssl, "create_default_context", return_value=ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ):
            yield module, connection, cursor

    def test_bad_sql_checksum_is_rejected_before_connecting(self):
        self.sql.write_text("changed after handover", encoding="utf-8")
        with self.database() as (module, connection, cursor):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                rds.run(self.arguments())
            module.connect.assert_not_called()

    def test_existing_table_is_preserved_by_schema_command(self):
        with self.database(existing=1) as (_, connection, cursor):
            with self.assertRaisesRegex(RuntimeError, "already exists"):
                rds.run(self.arguments("schema"))
            self.assertFalse(any("CREATE TABLE" in call.args[0] for call in cursor.execute.call_args_list))
            connection.commit.assert_not_called()

    def test_nonempty_products_table_is_preserved(self):
        with self.database(existing=3) as (_, connection, cursor):
            with self.assertRaisesRegex(RuntimeError, "empty products table"):
                rds.run(self.arguments())
            self.assertFalse(any(call.args[0].startswith("INSERT") for call in cursor.execute.call_args_list))
            connection.commit.assert_not_called()

    def test_schema_mismatch_prevents_import(self):
        with self.database(schema=[]) as (_, connection, cursor):
            with self.assertRaisesRegex(RuntimeError, "schema differs"):
                rds.run(self.arguments())
            self.assertFalse(any(call.args[0].startswith("INSERT") for call in cursor.execute.call_args_list))
            connection.commit.assert_not_called()

    def test_any_restored_field_difference_rolls_back(self):
        changed = [{**self.rows[0], "image_manifest_key": "unexpected-object-key"}]
        with self.database(restored_rows=changed) as (_, connection, cursor):
            with self.assertRaisesRegex(RuntimeError, "rolled back"):
                rds.run(self.arguments())
            connection.rollback.assert_called_once()
            connection.commit.assert_not_called()

    def test_matching_full_field_manifest_commits_once(self):
        with self.database() as (module, connection, cursor):
            rds.run(self.arguments())
            self.assertEqual(module.connect.call_args.kwargs["database"], "customer_catalog")
            connection.commit.assert_called_once()
            connection.rollback.assert_not_called()


if __name__ == "__main__":
    unittest.main()
