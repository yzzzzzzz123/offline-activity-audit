"""Safety checks for backup and restore; does not connect to a real database."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import dbctl


class BackupRestoreTests(unittest.TestCase):
    def test_backup_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "backup.sql"
            target.write_bytes(b"existing backup")
            with patch.object(dbctl, "execute") as execute:
                with self.assertRaises(FileExistsError):
                    dbctl.backup(argparse.Namespace(file=str(target)))
                execute.assert_not_called()
            self.assertEqual(target.read_bytes(), b"existing backup")

    def test_failed_backup_removes_only_new_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "backup.sql"
            def fail(*args, **kwargs):
                kwargs["stdout"].write(b"partial dump")
                raise subprocess.CalledProcessError(1, "mysqldump")
            with patch.object(dbctl, "execute", side_effect=fail):
                with self.assertRaises(subprocess.CalledProcessError):
                    dbctl.backup(argparse.Namespace(file=str(target)))
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".sql.sha256").exists())

    def test_restore_refuses_nonempty_database(self):
        with patch.object(dbctl, "query", return_value="130"), patch.object(dbctl, "execute") as execute:
            with self.assertRaisesRegex(RuntimeError, "empty products table"):
                dbctl.restore(argparse.Namespace(file="unused.sql"))
            execute.assert_not_called()

    def test_restore_rejects_missing_invalid_or_mismatched_checksum(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "backup.sql"
            source.write_bytes(b"SELECT 1;")
            checksum = source.with_suffix(".sql.sha256")
            for content in (None, "", "invalid", "0" * 64):
                with self.subTest(content=content):
                    if content is not None:
                        checksum.write_text(content, encoding="utf-8")
                    with patch.object(dbctl, "query", return_value="0"), patch.object(dbctl, "execute") as execute:
                        with self.assertRaises(RuntimeError):
                            dbctl.restore(argparse.Namespace(file=str(source)))
                        execute.assert_not_called()

    def test_restore_uses_transaction_for_verified_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "backup.sql"
            data = b"INSERT INTO products VALUES ('6900000000000', 'example', 'P1');"
            source.write_bytes(data)
            source.with_suffix(".sql.sha256").write_text(hashlib.sha256(data).hexdigest(), encoding="utf-8")
            with patch.object(dbctl, "query", side_effect=["0", "1"]), patch.object(dbctl, "execute") as execute:
                dbctl.restore(argparse.Namespace(file=str(source)))
            self.assertEqual(execute.call_args.kwargs["input"], b"START TRANSACTION;\n" + data + b"\nCOMMIT;\n")


class ImageManifestMigrationTests(unittest.TestCase):
    def test_preview_never_writes_or_loads_products(self):
        with patch.object(dbctl, "image_manifest_definition", return_value=None), \
                patch.object(dbctl, "product_identity_summary") as identity, \
                patch.object(dbctl, "query") as query:
            dbctl.migrate_image_manifest(argparse.Namespace(apply=False))
        identity.assert_not_called()
        query.assert_not_called()

    def test_existing_correct_column_is_a_noop(self):
        with patch.object(dbctl, "image_manifest_definition", return_value=dbctl.IMAGE_MANIFEST_DEFINITION), \
                patch.object(dbctl, "query") as query:
            dbctl.migrate_image_manifest(argparse.Namespace(apply=True))
        query.assert_not_called()

    def test_incompatible_existing_column_is_preserved(self):
        definition = {**dbctl.IMAGE_MANIFEST_DEFINITION, "length": 255}
        with patch.object(dbctl, "image_manifest_definition", return_value=definition), \
                patch.object(dbctl, "query") as query:
            with self.assertRaisesRegex(RuntimeError, "未修改或覆盖"):
                dbctl.migrate_image_manifest(argparse.Namespace(apply=True))
        query.assert_not_called()

    def test_apply_adds_only_nullable_column_and_verifies_existing_data(self):
        identity = {"row_count": 130, "sha256": "a" * 64}
        with patch.object(dbctl, "image_manifest_definition", side_effect=[None, dbctl.IMAGE_MANIFEST_DEFINITION]), \
                patch.object(dbctl, "product_identity_summary", side_effect=[identity, identity]), \
                patch.object(dbctl, "query", side_effect=["", "0"]) as query:
            dbctl.migrate_image_manifest(argparse.Namespace(apply=True))
        sql = query.call_args_list[0].args[0]
        self.assertIn("ADD COLUMN image_manifest_key", sql)
        self.assertIn("NULL DEFAULT NULL", sql)
        self.assertIn("ALGORITHM=INSTANT", sql)
        self.assertIn("lock_wait_timeout = 5", sql)
        for forbidden in ("DROP", "UPDATE", "INSERT", "DELETE", "TRUNCATE"):
            self.assertNotIn(forbidden, sql.upper())
        self.assertIn("image_manifest_key IS NOT NULL", query.call_args_list[1].args[0])

    def test_identity_change_is_reported_without_rollback_or_overwrite(self):
        with patch.object(dbctl, "image_manifest_definition", side_effect=[None, dbctl.IMAGE_MANIFEST_DEFINITION]), \
                patch.object(dbctl, "product_identity_summary", side_effect=[{"sha256": "a"}, {"sha256": "b"}]), \
                patch.object(dbctl, "query", return_value="") as query:
            with self.assertRaisesRegex(RuntimeError, "商品身份数据发生变化"):
                dbctl.migrate_image_manifest(argparse.Namespace(apply=True))
        self.assertEqual(query.call_count, 1)

    def test_unexpected_nonempty_new_field_is_not_cleared(self):
        with patch.object(dbctl, "image_manifest_definition", side_effect=[None, dbctl.IMAGE_MANIFEST_DEFINITION]), \
                patch.object(dbctl, "product_identity_summary", return_value={"row_count": 130}), \
                patch.object(dbctl, "query", side_effect=["", "1"]) as query:
            with self.assertRaisesRegex(RuntimeError, "未写入或清除"):
                dbctl.migrate_image_manifest(argparse.Namespace(apply=True))
        self.assertEqual(query.call_count, 2)

    def test_identity_hash_preserves_three_field_contract(self):
        rows = [{"product_code": "P2", "product_name": "商品二", "barcode_69": "6900000000002"},
                {"product_code": "P1", "product_name": "商品一", "barcode_69": "6900000000001"}]
        with patch.object(dbctl, "query", return_value="\n".join(json.dumps(row) for row in rows)) as query:
            result = dbctl.product_identity_summary()
        expected = hashlib.sha256(json.dumps(list(reversed(rows)), ensure_ascii=False, sort_keys=True,
                                            separators=(",", ":")).encode("utf-8")).hexdigest()
        self.assertEqual(result, {"row_count": 2, "sha256": expected})
        self.assertNotIn("image_manifest_key", query.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
