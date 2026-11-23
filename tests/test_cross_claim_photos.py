"""Synthetic-only local photo index regressions; never call providers or services."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

from audit_core import cross_claim_photos as index


class CrossClaimPhotoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scope = self.root / "worktrees"
        self.scope.mkdir()
        self.image = self.root / "scene.png"
        picture = Image.new("RGB", (96, 80), "white")
        draw = ImageDraw.Draw(picture)
        draw.rectangle((8, 9, 60, 50), fill="red")
        draw.ellipse((25, 30, 80, 70), fill="blue")
        picture.save(self.image)
        self.archive = "a" * 64

    def workspace(self, run, *, scope=None, status="completed", workspace=None):
        scope = scope or self.scope
        path = scope / (workspace or run)
        path.mkdir(exist_ok=True)
        (path / "manifest.json").write_text(json.dumps({"run_id": run, "workspace_id": path.name, "status": status}), encoding="utf-8")
        return path

    def record(self, claim="CLAIM-A", run="run-a", *, scope=None, role="display_photos", image=None, **kwargs):
        unit = {"unit_id": "a001-f0001", "source_file": "photos/scene.png", "locator": "Sheet1!R2C3", "image": image or self.image, "role": role}
        unit.update(kwargs.pop("unit_fields", {}))
        return index.inspect_and_record(scope or self.scope, verify_code=claim, run_id=run,
            archive_sha256=self.archive, units=[unit], recognition_version=kwargs.pop("recognition_version", "reader-v1"), **kwargs)

    def database(self):
        return index.index_directory(self.scope) / "index.sqlite"

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.database())
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def test_identical_bytes_cross_claim_retains_provenance_without_copying_original(self):
        self.workspace("run-a", workspace="archive-a")
        before = self.image.read_bytes()
        first = self.record(workspace_id="archive-a")
        self.assertEqual(first["status"], "ready")
        self.assertFalse(first["photos"][0]["exact_reuse"])
        second = self.record("CLAIM-B", "run-b", recognition_version="reader-v2")
        photo = second["photos"][0]
        self.assertEqual(photo["status"], "exact_reuse")
        source = photo["exact_reuse"][0]
        self.assertEqual((source["run_id"], source["workspace_id"], source["verify_code"]), ("run-a", "archive-a", "CLAIM-A"))
        self.assertEqual(source["recognition_version"], "reader-v1")
        self.assertEqual(source["locator"], "Sheet1!R2C3")
        self.assertEqual(source["sha256"], hashlib.sha256(before).hexdigest())
        self.assertNotIn("image", source)
        self.assertEqual(self.image.read_bytes(), before)
        self.assertFalse(list(index.index_directory(self.scope).rglob("*.png")))
        self.assertFalse(second["coverage"]["historical_backfill"])
        with self.connection() as db:
            self.assertEqual(db.execute("PRAGMA journal_mode").fetchone()[0], "wal")
            self.assertEqual(db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 2)

    def test_same_business_retries_and_unknown_identity_never_report_cross_claim(self):
        self.workspace("run-a")
        self.record()
        self.workspace("retry")
        same = self.record("CLAIM-A", "retry")
        unknown = self.record(None, "manual")
        for result in [same, unknown]:
            self.assertFalse(result["photos"][0]["exact_reuse"])
            self.assertFalse(result["photos"][0]["near_candidates"])
        # Unknown source cannot be exposed to a future known claim either.
        index.remove_run(self.scope, "run-a")
        index.remove_run(self.scope, "retry")
        self.workspace("manual")
        self.assertFalse(self.record("CLAIM-B", "run-b")["photos"][0]["exact_reuse"])

    def test_scopes_are_isolated(self):
        self.workspace("run-a")
        self.record()
        other = self.root / "another-customer"
        other.mkdir()
        self.assertFalse(self.record("CLAIM-B", "run-b", scope=other)["photos"][0]["exact_reuse"])

    def test_contract_payment_reference_and_unconfirmed_mixed_roles_are_not_indexed(self):
        for role in ["promotion_contract", "settlement", "payment", "reference", "receipt", "photos", "giveaway_evidence", "staff_photos", "price_evidence", "full_reduction_evidence"]:
            with self.subTest(role=role):
                result = self.record(role=role)
                self.assertEqual(result["status"], "skipped")
        self.assertFalse(index.index_directory(self.scope).exists())
        self.assertEqual(self.record(role="giveaway_evidence", unit_fields={"scene_photo": True})["coverage"]["indexed"], 1)
        self.assertEqual(self.record(role="photos", unit_fields={"scene_photo": True, "roles": ["payment"]})["status"], "skipped")
        self.assertEqual(self.record(unit_fields={"reference": True})["status"], "skipped")
        for role in ["price_evidence", "full_reduction_evidence"]:
            self.assertEqual(self.record(role=role, unit_fields={"scene_photo": True})["coverage"]["indexed"], 1)
            self.assertEqual(self.record(role=role, unit_fields={"scene_photo": True, "roles": ["receipt"]})["status"], "skipped")

    def test_pixel_identical_but_changed_encoding_is_near_only_and_never_exact(self):
        self.workspace("run-a")
        self.record()
        alternative = self.root / "reencoded.bmp"
        with Image.open(self.image) as picture:
            picture.save(alternative)
        result = self.record("CLAIM-B", "run-b", image=alternative)
        photo = result["photos"][0]
        self.assertFalse(photo["exact_reuse"])
        self.assertEqual(photo["status"], "indexed")
        self.assertTrue(photo["near_candidates"])
        self.assertTrue(all(item["technical_only"] for item in photo["near_candidates"]))
        self.assertFalse(result["coverage"]["near_search_exhaustive"])

    def test_deleted_running_failed_and_mismatched_manifest_sources_are_filtered(self):
        for status in ["running", "failed"]:
            self.workspace("run-a", status=status)
            self.record()
            self.assertFalse(self.record("CLAIM-B", "run-b")["photos"][0]["exact_reuse"])
        workspace = self.workspace("run-a")
        (workspace / "manifest.json").write_text(json.dumps({"run_id": "other", "workspace_id": "run-a", "status": "completed"}), encoding="utf-8")
        self.assertFalse(self.record("CLAIM-B", "run-b")["photos"][0]["exact_reuse"])
        shutil.rmtree(workspace)
        self.assertFalse(self.record("CLAIM-B", "run-b")["photos"][0]["exact_reuse"])

    def test_idempotent_recording_and_remove_only_one_run_keep_other_references(self):
        for run, claim in [("run-a", "CLAIM-A"), ("run-b", "CLAIM-B")]:
            self.workspace(run)
            self.record(claim, run)
            self.record(claim, run)
        with self.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 2)
        removed = index.remove_run(self.scope, "run-a")
        self.assertTrue(removed["source_hidden"])
        self.assertEqual(removed["removed_occurrences"], 1)
        self.assertEqual(index.remove_run(self.scope, "run-a")["removed_occurrences"], 0)
        result = self.record("CLAIM-C", "run-c")
        self.assertEqual([m["run_id"] for m in result["photos"][0]["exact_reuse"]], ["run-b"])
        # A delayed writer cannot resurrect a deleted run.
        self.assertEqual(self.record()["status"], "degraded")
        with self.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM occurrences WHERE run_id='run-a'").fetchone()[0], 0)

    def test_remove_run_absent_database_does_not_create_cache_or_database(self):
        result = index.remove_run(self.scope, "never-indexed")
        self.assertTrue(result["source_hidden"])
        self.assertEqual(result["status"], "ready")
        self.assertFalse((self.scope / ".cache").exists())

    def test_corrupt_database_degrades_without_claiming_no_duplicates_or_resetting(self):
        folder = index.index_directory(self.scope)
        folder.mkdir(parents=True)
        self.database().write_bytes(b"invalid-private-photo-index")
        before = self.database().read_bytes()
        result = self.record()
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["photos"][0]["status"], "unavailable")
        self.assertFalse(result["photos"][0]["exact_reuse"])
        self.assertNotIn("invalid-private-photo-index", json.dumps(result))
        self.assertEqual(self.database().read_bytes(), before)
        removed = index.remove_run(self.scope, "run-a")
        self.assertEqual(removed["status"], "degraded")
        self.assertFalse(removed["source_hidden"])

    def test_locked_database_uses_bounded_failure_and_deletion_tombstone(self):
        self.workspace("run-a")
        self.record()
        with self.connection() as blocker:
            blocker.execute("BEGIN IMMEDIATE")
            blocker.execute("UPDATE occurrences SET recognition_version='locked'")
            result = self.record("CLAIM-B", "run-b")
            self.assertEqual(result["status"], "degraded")
            removed = index.remove_run(self.scope, "run-a")
            self.assertFalse(removed["source_hidden"])
            self.assertEqual(removed["status"], "degraded")
        self.assertTrue(self.record("CLAIM-B", "run-b")["photos"][0]["exact_reuse"])
        self.assertTrue(index.remove_run(self.scope, "run-a")["source_hidden"])
        self.assertFalse(self.record("CLAIM-B", "run-b")["photos"][0]["exact_reuse"])

    def test_permission_error_is_diagnostic_and_contains_no_absolute_image_path(self):
        with patch.object(index, "_directory", side_effect=PermissionError("secret/path/key")):
            result = self.record()
        self.assertEqual(result["status"], "degraded")
        encoded = json.dumps(result)
        self.assertNotIn("secret/path/key", encoded)
        self.assertNotIn(str(self.image), encoded)

    def test_index_hardlink_is_rejected(self):
        folder = index.index_directory(self.scope)
        folder.mkdir(parents=True)
        unrelated = self.root / "unrelated.txt"
        unrelated.write_bytes(b"preserve")
        try:
            os.link(unrelated, self.database())
        except OSError as exc:
            self.skipTest(f"hardlink unavailable: {type(exc).__name__}")
        self.assertEqual(self.record()["status"], "degraded")
        self.assertEqual(unrelated.read_bytes(), b"preserve")

    def test_symlinked_cache_directory_is_rejected_without_writing_external_target(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.scope / ".cache").mkdir()
        try:
            index.index_directory(self.scope).symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {type(exc).__name__}")
        self.assertEqual(self.record()["status"], "degraded")
        self.assertEqual(list(outside.iterdir()), [])

    def test_image_symlink_is_rejected(self):
        alias = self.root / "alias.png"
        try:
            alias.symlink_to(self.image)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {type(exc).__name__}")
        self.assertEqual(self.record(image=alias)["status"], "degraded")
        self.assertFalse(index.index_directory(self.scope).exists())

    def test_foreign_sqlite_database_is_not_mutated_or_adopted(self):
        index.index_directory(self.scope).mkdir(parents=True)
        with self.connection() as db:
            db.execute("CREATE TABLE other(value TEXT)")
            db.execute("INSERT INTO other VALUES ('keep')")
        before = self.database().read_bytes()
        self.assertEqual(self.record()["status"], "degraded")
        self.assertEqual(self.database().read_bytes(), before)

    def test_source_manifest_symlink_is_never_followed(self):
        source = self.workspace("run-a")
        self.record()
        original = source / "manifest.json"
        saved = self.root / "saved-manifest.json"
        original.replace(saved)
        try:
            original.symlink_to(saved)
        except OSError as exc:
            self.skipTest(f"symlink unavailable: {type(exc).__name__}")
        self.assertFalse(self.record("CLAIM-B", "run-b")["photos"][0]["exact_reuse"])

    def test_unreadable_and_oversized_images_are_not_silently_indexed(self):
        result = self.record(image=self.root / "missing.png")
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["diagnostics"][0]["code"], "PHOTO_IMAGE_UNAVAILABLE")
        with patch.object(index, "MAX_IMAGE_BYTES", 1):
            result = self.record()
        self.assertEqual(result["photos"][0]["status"], "unavailable")
        self.assertFalse(index.index_directory(self.scope).exists())

    def test_supported_older_pillow_getdata_fallback_is_equivalent(self):
        expected = index._fingerprint(self.image)
        # Pillow before 12.1 has only getdata; emulate that supported API.
        with patch.object(Image.Image, "get_flattened_data", None):
            actual = index._fingerprint(self.image)
        self.assertEqual(actual, expected)

    def test_pixel_budget_keeps_stream_sha_exact_without_decoding_or_phash(self):
        self.workspace("run-a")
        with patch.object(index, "MAX_IMAGE_PIXELS", 1), patch.object(Image.Image, "thumbnail", side_effect=AssertionError("must not decode")):
            first = self.record()
            second = self.record("CLAIM-B", "run-b")
        self.assertEqual(first["status"], "ready")
        self.assertEqual(second["photos"][0]["status"], "exact_reuse")
        self.assertEqual(second["coverage"]["status"], "partial")
        self.assertEqual(second["photos"][0]["near_skipped_reason"], "image_pixels_or_frames")
        self.assertFalse(second["photos"][0]["near_candidates"])
        with self.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM bands").fetchone()[0], 0)

    def test_image_and_occurrence_budgets_stop_new_rows_without_claiming_full_coverage(self):
        self.workspace("run-a")
        self.record()
        alternative = self.root / "new-encoding.bmp"
        with Image.open(self.image) as picture:
            picture.save(alternative)
        with patch.object(index, "MAX_IMAGES", 1):
            result = self.record("CLAIM-B", "run-b", image=alternative)
        self.assertEqual(result["status"], "partial")
        self.assertIn("images", result["coverage"]["limit_reached"])
        self.assertEqual(result["coverage"]["indexed"], 0)
        with patch.object(index, "MAX_OCCURRENCES", 1):
            result = self.record("CLAIM-B", "run-b")
        self.assertEqual(result["status"], "partial")
        self.assertTrue(result["photos"][0]["exact_reuse"])
        self.assertFalse(result["photos"][0]["indexed"])
        with self.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT images,occurrences FROM counters").fetchone()), (1, 1))

    def test_whole_batch_output_budget_bounds_source_matches_not_only_per_photo(self):
        self.workspace("run-a")
        self.record()
        units = [{"unit_id": f"photo-{n}", "source_file": "scene.png", "locator": str(n), "image": self.image, "role": "display_photos"} for n in range(25)]
        with patch.object(index, "MAX_TOTAL_MATCHES", 5), patch.object(index, "_matches", wraps=index._matches) as match:
            result = index.inspect_and_record(self.scope, verify_code="CLAIM-B", run_id="run-b", archive_sha256=self.archive, units=units)
        self.assertEqual(result["coverage"]["indexed"], 25)
        self.assertEqual(sum(len(p["exact_reuse"]) + len(p["near_candidates"]) for p in result["photos"]), 5)
        self.assertLessEqual(match.call_count, 5)
        self.assertIn("output_matches", result["coverage"]["limit_reached"])

    def test_sqlite_memory_page_and_wal_settings_are_bounded(self):
        self.record()
        with index._database(index.index_directory(self.scope), create=False) as db:
            self.assertEqual(db.execute("PRAGMA cache_size").fetchone()[0], -4096)
            self.assertEqual(db.execute("PRAGMA temp_store").fetchone()[0], 1)
            self.assertLessEqual(db.execute("PRAGMA max_page_count").fetchone()[0] * db.execute("PRAGMA page_size").fetchone()[0], index.MAX_DATABASE_BYTES)
            self.assertEqual(db.execute("PRAGMA journal_size_limit").fetchone()[0], index.JOURNAL_SIZE_BYTES)
            self.assertEqual(db.execute("PRAGMA wal_autocheckpoint").fetchone()[0], 256)

    def test_large_run_removal_batches_rows_and_tombstone_set_has_a_hard_limit(self):
        units = [{"unit_id": f"photo-{n}", "source_file": "scene.png", "locator": str(n), "image": self.image, "role": "display_photos"} for n in range(260)]
        fingerprint = index._fingerprint(self.image)
        with patch.object(index, "_fingerprint", return_value=fingerprint):
            result = index.inspect_and_record(self.scope, verify_code="CLAIM-A", run_id="run-a", archive_sha256=self.archive, units=units)
        self.assertEqual(result["coverage"]["indexed"], 260)
        self.assertEqual(index.remove_run(self.scope, "run-a")["removed_occurrences"], 260)
        with patch.object(index, "MAX_REMOVED_RUNS", 1):
            rejected = index.remove_run(self.scope, "run-b")
        self.assertFalse(rejected["source_hidden"])
        with self.connection() as db:
            self.assertEqual(tuple(db.execute("SELECT images,occurrences,removed_runs FROM counters").fetchone()), (0, 0, 1))
        self.assertFalse((index.index_directory(self.scope) / "removed-runs").exists())

    def test_concurrent_writers_keep_all_occurrences_without_duplicate_upserts(self):
        for n in range(4):
            self.workspace(f"run-{n}")
        with ThreadPoolExecutor(max_workers=4) as workers:
            results = list(workers.map(lambda n: self.record(f"CLAIM-{n}", f"run-{n}"), range(4)))
        self.assertTrue(all(value["status"] == "ready" for value in results), results)
        with self.connection() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM occurrences").fetchone()[0], 4)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM images").fetchone()[0], 1)

    def test_band_candidate_queries_are_bounded_and_use_the_index(self):
        self.workspace("run-a")
        self.record()
        fingerprint = index._fingerprint(self.image)
        with self.connection() as db:
            for n in range(180):
                digest = hashlib.sha256(str(n).encode()).hexdigest()
                db.execute("INSERT INTO images VALUES (?,?,?,?,?,?)", (digest, index.ALGORITHM_VERSION, fingerprint["phash"], 96, 80, 1))
                for band in range(4):
                    db.execute("INSERT INTO bands VALUES (?,?,?,?)", (index.ALGORITHM_VERSION, band, fingerprint["phash"][band * 4:band * 4 + 4], digest))
            plan = db.execute("EXPLAIN QUERY PLAN SELECT sha256 FROM bands WHERE algorithm=? AND band=? AND value=? ORDER BY sha256 LIMIT ?", (index.ALGORITHM_VERSION, 0, fingerprint["phash"][:4], index.MAX_BAND_ROWS + 1)).fetchall()
        self.assertTrue(any("SEARCH" in row[3] for row in plan))
        with patch.object(index, "_sources", wraps=index._sources) as sources:
            result = self.record("CLAIM-B", "run-b")
        self.assertLessEqual(sources.call_count, 1 + 4 * index.MAX_BAND_ROWS)
        self.assertTrue(result["photos"][0]["candidates_limited"])


if __name__ == "__main__":
    unittest.main()
