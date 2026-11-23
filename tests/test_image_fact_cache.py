"""Private exact-byte fact storage, corruption fallback and run ownership."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.image_fact_cache import (ImageFactCache, fact_key, remove_run_facts,
                                        MAX_MEMORY_BYTES, MAX_MEMORY_ENTRIES, MAX_ENTRY_BYTES, MAX_CONTEXT_BYTES)


FACTS = {"roles": ["现场照片"], "facts": ["照片可见日期2026-11-03"], "limitations": []}


class ImageFactCacheTests(unittest.TestCase):
    def key(self, text="image", context=None):
        return fact_key(hashlib.sha256(text.encode()).hexdigest(), context or {"prompt": "facts only"})

    def test_without_owner_or_root_uses_only_bounded_copy_safe_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            for cache in (ImageFactCache(), ImageFactCache(Path(temporary))):
                self.assertFalse(cache.put(self.key(), FACTS))
                value, source = cache.get(self.key())
                self.assertEqual(source, "memory_hit")
                value["facts"].append("caller mutation")
                self.assertEqual(cache.get(self.key())[0], FACTS)
            self.assertEqual(list(Path(temporary).iterdir()), [])

    def test_persistent_hit_records_second_owner_and_deletion_keeps_other_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = ImageFactCache(root, owner="run-a")
            self.assertTrue(first.put(self.key(), FACTS))
            second = ImageFactCache(root, owner="run-b")
            self.assertEqual(second.get(self.key()), (FACTS, "persistent_hit"))
            record = json.loads((first.root / (self.key() + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(record["owners"], ["run-a", "run-b"])
            self.assertEqual(remove_run_facts(root, "run-a"), {"removed_entries": 0, "remaining_entries": 1, "deferred": 0})
            self.assertEqual(ImageFactCache(root, owner="run-b").get(self.key())[0], FACTS)
            self.assertEqual(remove_run_facts(root, "run-b")["removed_entries"], 1)
            self.assertIsNone(ImageFactCache(root, owner="run-c").get(self.key())[0])

    def test_corruption_wrong_key_version_missing_owner_or_decision_fields_are_misses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = ImageFactCache(root, owner="run")
            cache.put(self.key(), FACTS)
            path = cache.root / (self.key() + ".json")
            original = json.loads(path.read_text(encoding="utf-8"))
            variants = ["not-json", [], {**original, "version": 0}, {**original, "key": "0" * 64},
                        {k: v for k, v in original.items() if k != "owners"},
                        {**original, "value": {**FACTS, "approved": True}},
                        {**original, "value": {**FACTS, "facts": ["tampered"]}}]
            for variant in variants:
                with self.subTest(variant=type(variant).__name__):
                    path.write_text(json.dumps(variant), encoding="utf-8")
                    self.assertEqual(ImageFactCache(root, owner="run").get(self.key()), (None, None))
                    self.assertFalse(path.exists())
            with self.assertRaises(ValueError):
                cache.put(self.key(), {**FACTS, "source_ids": ["old-unit"]})

    def test_capacity_evicts_old_records_and_never_stores_oversize(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = ImageFactCache(root, owner="run", max_entries=2, max_bytes=4096)
            for name in ("one", "two", "three"):
                cache.put(self.key(name), FACTS)
            self.assertEqual(len(list(cache.root.glob("*.json"))), 2)
            self.assertIsNone(ImageFactCache(root, owner="run").get(self.key("one"))[0])
            self.assertFalse(cache.put(self.key("large"), {**FACTS, "facts": ["x" * 5000]}))
            self.assertLessEqual(sum(p.stat().st_size for p in cache.root.glob("*.json")), 4096)

    def test_filesystem_failure_is_a_miss_and_write_failure_keeps_run_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = ImageFactCache(Path(temporary), owner="run")
            with patch("audit_core.image_fact_cache._prepare", side_effect=PermissionError("denied")):
                self.assertEqual(cache.get(self.key()), (None, None))
                self.assertFalse(cache.put(self.key(), FACTS))
                self.assertEqual(cache.get(self.key()), (FACTS, "memory_hit"))

    def test_hard_link_is_neither_read_overwritten_nor_deleted(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = ImageFactCache(root, owner="run")
            cache.get(self.key())
            outside = root / "protected.json"
            outside.write_text("protected", encoding="utf-8")
            link = cache.root / (self.key() + ".json")
            os.link(outside, link)
            self.assertEqual(cache.get(self.key()), (None, None))
            self.assertFalse(cache.put(self.key(), FACTS))
            self.assertEqual(remove_run_facts(root, "run")["deferred"], 1)
            self.assertEqual(outside.read_text(encoding="utf-8"), "protected")
            self.assertTrue(link.exists())

    def test_shared_concurrent_writes_are_atomic_and_owners_are_not_lost(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            def store(index):
                return ImageFactCache(root, owner=f"run-{index}").put(self.key(), FACTS)
            with ThreadPoolExecutor(max_workers=6) as executor:
                self.assertTrue(all(executor.map(store, range(12))))
            cache = ImageFactCache(root, owner="reader")
            self.assertEqual(cache.get(self.key()), (FACTS, "persistent_hit"))
            record = json.loads((cache.root / (self.key() + ".json")).read_text(encoding="utf-8"))
            self.assertEqual(set(record["owners"]), {"reader", *(f"run-{i}" for i in range(12))})
            self.assertEqual(list(cache.root.glob("*.tmp")), [])
            if os.name != "nt":
                self.assertEqual(cache.root.stat().st_mode & 0o777, 0o700)
                self.assertEqual((cache.root / (self.key() + ".json")).stat().st_mode & 0o777, 0o600)

    def test_keys_are_exact_and_context_sensitive(self):
        base = {"model": "m1", "provider": "p1", "endpoint": "https://example.com/v1", "effort": "default",
                "schema": {}, "prompt": "facts", "native_facts": [], "limitations": [], "version": 1}
        for field in base:
            self.assertNotEqual(self.key(context=base), self.key(context={**base, field: "changed"}))
        self.assertNotEqual(self.key("original", base), self.key("slightly-changed", base))

    def test_thousand_entries_never_exceed_memory_lru_hard_limits(self):
        cache = ImageFactCache()
        large = {**FACTS, "facts": ["x" * (80 * 1024)]}
        for index in range(1100):
            cache.put(self.key(str(index)), large)
            self.assertLessEqual(len(cache.memory), MAX_MEMORY_ENTRIES)
            self.assertLessEqual(cache.memory_bytes, MAX_MEMORY_BYTES)
        self.assertEqual(cache.get(self.key("0")), (None, None))
        self.assertEqual(cache.get(self.key("1099"))[1], "memory_hit")
        cache.put(self.key("oversize"), {**FACTS, "facts": ["x" * (MAX_ENTRY_BYTES + 1)]})
        self.assertEqual(cache.get(self.key("oversize")), (None, None))
        self.assertIsNone(self.key(context={"prompt": "x" * (MAX_CONTEXT_BYTES + 1)}))

    def test_owner_hit_growth_respects_same_disk_budget_as_put(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = ImageFactCache(root, owner="a", max_bytes=512)
            self.assertTrue(cache.put(self.key(), FACTS))
            before = (cache.root / (self.key() + ".json")).read_bytes()
            second = ImageFactCache(root, owner="b" * 512, max_bytes=512)
            self.assertEqual(second.get(self.key()), (None, None))
            self.assertEqual((cache.root / (self.key() + ".json")).read_bytes(), before)
            self.assertLessEqual(sum(p.stat().st_size for p in cache.root.glob("*.json")), 512)

    def test_many_short_strings_count_python_overhead_and_skip_memory_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = ImageFactCache(Path(temporary), owner="run")
            facts = {**FACTS, "facts": ["x"] * 180_000}
            self.assertTrue(cache.put(self.key(), facts))  # serialized record remains below 1 MiB
            self.assertEqual(cache.memory_bytes, 0)
            self.assertEqual(len(cache.memory), 0)
            self.assertEqual(cache.get(self.key())[1], "persistent_hit")
            self.assertEqual(len(cache.memory), 0)

    def test_overfull_directory_is_bounded_and_falls_back_without_reading_every_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = ImageFactCache(root, owner="run")
            cache.get(self.key())
            for index in range(5):
                (cache.root / (f"{index:064x}" + ".json")).write_text("{}", encoding="utf-8")
            with patch("audit_core.image_fact_cache.MAX_ENTRIES", 3):
                self.assertFalse(cache.put(self.key(), FACTS))
                self.assertEqual(remove_run_facts(root, "run")["deferred"], 1)
            self.assertEqual(len(list(cache.root.glob("*.json"))), 5)


if __name__ == "__main__":
    unittest.main()
