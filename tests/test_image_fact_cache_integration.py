"""Cache only transcription; keep batching, current source binding and audit inputs."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import jsonschema

from audit_core.common import AuditError
from audit_core.image_fact_cache import fact_key
from audit_core.model_settings import ModelSelection
from audit_core.pdf_evidence import DOCUMENT_SCHEMA
from audit_core.pdf_evidence import DISPLAY_PHOTO_RULES
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import catalogue
from audit_core.prompts import bound_prompt
from tests.pdf_test_support import classification


class ImageFactIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.selection = ModelSelection("ppio", "qwen/qwen3.8-max", "default", "https://api.ppinfra.com/v3/openai", "never-persist-this-key")

    def unit(self, name, shade=10, *, native=None, limitations=None):
        path = self.root / (name + ".png")
        Image.new("RGB", (12, 12), (shade, 1, 2)).save(path)
        return {"unit_id": name, "image": path, "source_file": name + ".xlsx", "locator": "照片!R2C3",
                "native_facts": native or [], "limitations": limitations or []}

    def run_read(self, units, run="run-1", selection=None, policy=None, persistent=True, invalid=False, reverse=False, alter=None):
        provider = PdfEvidenceProvider(None, None, connection=selection or self.selection, stage_summary_reviewer=isolated_stage_summary)
        calls, observations = [], []
        def call(root, skill, schema, prompt, *, images, label, validator, **transport_options):
            manifest = json.loads(prompt.partial_variables["manifest"])
            calls.append({"manifest": manifest, "images": list(images), "prompt": prompt.format(), "schema": deepcopy(schema),
                          "transport_options": transport_options})
            result = {"documents": [{"unit_id": row["unit_id"], "roles": ["photo"],
                                      "facts": ["实际可见的商品和日期" + row["unit_id"][-12:]], "limitations": []} for row in manifest]}
            if reverse:
                result["documents"].reverse()
            if invalid:
                result["documents"][0]["unit_id"] = "wrong-source"
            if alter:
                alter(result)
            validator(result)
            return result
        destination = self.root / ("request-" + str(len(list(self.root.glob("request-*")))))
        destination.mkdir()
        case = {"archive_id": "a001", "units": units, "workspace_id": run}
        if persistent:
            case["worktrees_root"] = self.root / "worktrees"
        with patch.object(provider, "_call", side_effect=call), patch("audit_core.pdf_materials.save_model_observations", side_effect=lambda name, data: observations.append((name, deepcopy(data)))):
            documents = provider._read_documents(case, destination, policy or catalogue())
        return documents, calls, observations[-1][1]

    def test_cold_images_send_one_at_a_time_then_cross_run_hits_rebind_sources(self):
        units = [self.unit(f"u{i}", i) for i in range(7)]
        documents, calls, counts = self.run_read(units)
        self.assertEqual([len(call["images"]) for call in calls], [1] * 7)
        self.assertEqual((counts["model_read_images"], counts["read_calls"]), (7, 7))
        renamed = [{**unit, "unit_id": "new-" + unit["unit_id"], "locator": "新表!R9C4", "source_file": "新原件.xlsx"} for unit in units]
        after, calls, counts = self.run_read(renamed, "run-2")
        self.assertEqual(calls, [])
        self.assertEqual(counts["persistent_hits"], 7)
        self.assertEqual([d["unit_id"] for d in after], [u["unit_id"] for u in renamed])
        self.assertEqual([d["facts"] for d in after], [d["facts"] for d in documents])
        cache_text = "".join(p.read_text(encoding="utf-8") for p in (self.root / "worktrees/.cache/image-facts-v1").glob("*.json"))
        self.assertNotIn(self.selection.api_key, cache_text)
        self.assertNotIn("new-u", cache_text)
        self.assertFalse(counts["token_usage_estimated"])

    def test_short_model_labels_keep_full_cache_identity_and_rebind_for_both_api_providers(self):
        selections = [self.selection, replace(self.selection, provider='deepseek', model='deepseek-flash',
                                              base_url='https://api.deepseek.com/v1')]
        for index, selection in enumerate(selections):
            with self.subTest(provider=selection.provider):
                units = [self.unit(f'p{index}-one', 30 + index), self.unit(f'p{index}-two', 60 + index)]
                documents, calls, _ = self.run_read(units, selection=selection)
                labels = [r['unit_id'] for call in calls for r in call['manifest']]
                self.assertTrue(all(len(label) == len('image-') + 24 for label in labels))
                self.assertEqual(len(set(labels)), 2)
                self.assertEqual([d['unit_id'] for d in documents], [u['unit_id'] for u in units])
                self.assertNotEqual(documents[0]['facts'], documents[1]['facts'])
                renamed = [{**u, 'unit_id': 'next-' + u['unit_id']} for u in units]
                after, hits, counts = self.run_read(renamed, run='next', selection=selection)
                self.assertEqual(hits, [])
                self.assertEqual(counts['persistent_hits'], 2)
                self.assertEqual([d['facts'] for d in after], [d['facts'] for d in documents])
                self.assertEqual([d['unit_id'] for d in after], [u['unit_id'] for u in renamed])
        for entry in (self.root / 'worktrees/.cache/image-facts-v1').glob('*.json'):
            self.assertRegex(entry.name, r'^[0-9a-f]{64}\.json$')

    def test_equal_label_prefixes_do_not_deduplicate_different_images(self):
        units = [self.unit('collision-one', 31), self.unit('collision-two', 81)]
        first_sha = hashlib.sha256(units[0]['image'].read_bytes()).hexdigest()
        def collide_prefix(image_sha, context):
            if 'suffix' in context:
                return 'a' * 24 + ('0' if image_sha == first_sha else '1') * 40
            return fact_key(image_sha, context)
        with patch('audit_core.pdf_materials.fact_key', side_effect=collide_prefix):
            documents, calls, counts = self.run_read(units, persistent=False)
        labels = [r['unit_id'] for call in calls for r in call['manifest']]
        self.assertEqual([len(call['images']) for call in calls], [1, 1])
        self.assertEqual(len(set(labels)), 2)
        self.assertEqual(counts['batch_duplicate_hits'], 0)
        self.assertNotEqual(documents[0]['facts'], documents[1]['facts'])
        self.assertEqual([d['unit_id'] for d in documents], ['collision-one', 'collision-two'])

    def test_batch_duplicates_send_once_and_positions_do_not_supply_visual_facts(self):
        first = self.unit("one")
        second = self.unit("two")
        for unit in (first, second):
            unit["photo_reference"] = True
            unit["native_facts"] = ["表格图片来源位置：" + unit["locator"]]
        second["locator"] = "第二表!R10C9"
        second["native_facts"] = ["表格图片来源位置：" + second["locator"]]
        documents, calls, counts = self.run_read([first, second], persistent=False)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(calls[0]["images"]), 1)
        self.assertEqual([d["unit_id"] for d in documents], ["one", "two"])
        self.assertEqual(counts["batch_duplicate_hits"], 1)
        self.assertNotIn("第二表", calls[0]["prompt"])
        self.assertNotIn("one.xlsx", calls[0]["prompt"])
        self.assertIn("不得从同批其他图片", calls[0]["prompt"])

    def test_model_endpoint_effort_policy_native_limits_and_bytes_invalidate(self):
        unit = self.unit("image")
        self.run_read([unit])
        for selection in [replace(self.selection, model="qwen/other"), replace(self.selection, provider="qwen"),
                          replace(self.selection, base_url="https://another.example/v1")]:
            with self.subTest(selection=selection.model + selection.provider + selection.base_url + selection.reasoning_effort):
                self.assertEqual(len(self.run_read([unit], selection=selection)[1]), 1)
        # Reading uses the selected effort. Old none facts cannot stand in for
        # a requested max read, while matching max facts remain reusable.
        self.assertEqual(len(self.run_read([unit], selection=replace(self.selection, reasoning_effort="none"))[1]), 1)
        maximum = replace(self.selection, reasoning_effort="max")
        self.assertEqual(len(self.run_read([unit], selection=maximum)[1]), 1)
        self.assertEqual(self.run_read([unit], selection=maximum)[1], [])
        for changed in [{**unit, "native_facts": ["原生PDF文字不同"]}, {**unit, "limitations": ["页面存在遮挡"]}, self.unit("new-pixels", 99)]:
            self.assertEqual(len(self.run_read([changed])[1]), 1)
        self.assertEqual(len(self.run_read([unit], policy={**catalogue(), "reading_revision": "changed"})[1]), 1)
        # A separate image-account credential must not inherit another account's
        # recognition cache. Only an opaque connection scope is persisted.
        self.assertEqual(len(self.run_read([unit], selection=replace(self.selection, api_key="new-secret"))[1]), 1)

    def test_unvalidated_reply_is_not_cached(self):
        unit = self.unit("bad")
        with self.assertRaises(AuditError):
            self.run_read([unit], invalid=True)
        self.assertEqual(list((self.root / "worktrees/.cache/image-facts-v1").glob("*.json")), [])
        self.assertEqual(len(self.run_read([unit])[1]), 1)

    def test_batch_schema_binds_only_pending_unique_images_without_mutating_stable_schema(self):
        baseline_schema = deepcopy(DOCUMENT_SCHEMA)
        cached = self.unit("cached", 5)
        self.run_read([cached], run="old-run")
        fresh, duplicate, other = self.unit("fresh", 20), self.unit("duplicate", 20), self.unit("other", 40)
        native = {"unit_id": "native", "image": None, "native_facts": ["原始表格事实"], "limitations": []}
        units = [cached, fresh, duplicate, native, other]
        documents, calls, counts = self.run_read(units, run="next-run")
        self.assertEqual(len(calls), 2)
        call = calls[0]
        constraint = call["schema"]["properties"]["documents"]
        self.assertEqual((constraint["minItems"], constraint["maxItems"]), (1, 1))
        self.assertEqual(constraint["items"]["properties"]["unit_id"]["enum"],
                         [item["unit_id"] for item in call["manifest"]])
        for field in ("facts", "roles", "limitations"):
            self.assertNotIn("enum", constraint["items"]["properties"][field]["items"])
        self.assertTrue(all(len(c["images"]) == 1 for c in calls))
        self.assertEqual(counts["persistent_hits"], 1)
        self.assertEqual([d["unit_id"] for d in documents], [u["unit_id"] for u in units])
        self.assertEqual(documents[3]["facts"], native["native_facts"])
        self.assertEqual(DOCUMENT_SCHEMA, baseline_schema)
        schema_again = self.run_read([self.unit("last", 99)])[1][0]["schema"]["properties"]["documents"]
        self.assertEqual((schema_again["minItems"], schema_again["maxItems"]), (1, 1))
        self.assertNotEqual(schema_again["items"]["properties"]["unit_id"]["enum"],
                            constraint["items"]["properties"]["unit_id"]["enum"])

    def test_missing_extra_and_duplicate_ids_are_rejected_without_cache_writes(self):
        for error in ("missing", "extra", "duplicate"):
            with self.subTest(error=error):
                units = [self.unit(error + "-a", 10), self.unit(error + "-b", 11)]
                def alter(result):
                    if error == "missing":
                        result["documents"].pop()
                    elif error == "extra":
                        result["documents"].append({**deepcopy(result["documents"][0]), "unit_id": "PRIVATE_UNKNOWN_FACT"})
                    else:
                        result["documents"].append(deepcopy(result["documents"][0]))
                with self.assertRaises(AuditError) as raised:
                    self.run_read(units, alter=alter)
                self.assertNotIn("PRIVATE_UNKNOWN_FACT", str(raised.exception))
                self.assertLessEqual(len(str(raised.exception).encode("utf-8")), 1000)
                self.assertEqual(list((self.root / "worktrees/.cache/image-facts-v1").glob("*.json")), [])

    def test_failed_later_batch_preserves_only_prior_validated_cache_for_retry(self):
        units = [self.unit(f"u{i}", i) for i in range(7)]
        count = 0
        def alter(result):
            nonlocal count
            count += 1
            if count == 2:
                result["documents"][0]["unit_id"] = "PRIVATE_UNKNOWN_FACT"
        with self.assertRaises(AuditError):
            self.run_read(units, alter=alter)
        documents, calls, counts = self.run_read(units, run="retry-run")
        self.assertEqual(len(documents), 7)
        self.assertEqual([len(call["images"]) for call in calls], [1] * 6)
        self.assertEqual(counts["persistent_hits"], 1)
        constraint = calls[0]["schema"]["properties"]["documents"]
        self.assertEqual((constraint["minItems"], constraint["maxItems"]), (1, 1))

    def test_dynamic_schema_rejects_unknown_or_wrong_count_and_uniqueness_guard_still_applies(self):
        units = [self.unit("one", 1), self.unit("two", 2)]
        _, calls, _ = self.run_read(units, persistent=False)
        schema = calls[0]["schema"]
        value = {"documents": [{"unit_id": item["unit_id"], "roles": ["photo"], "facts": ["本图事实"], "limitations": []}
                               for item in calls[0]["manifest"]]}
        value['field_inventory'] = [{'unit_id': item['unit_id'], **{
            key: {'state': 'absent', 'fact_indexes': []}
            for key in ('written_dealer_name', 'seal_name', 'activity_period')}}
            for item in calls[0]['manifest']]
        for change in ("unknown", "missing", "extra"):
            invalid = deepcopy(value)
            if change == "unknown":
                invalid["documents"][0]["unit_id"] = "wrong"
            elif change == "missing":
                invalid["documents"].pop()
            else:
                invalid["documents"].append(deepcopy(invalid["documents"][0]))
            with self.subTest(change=change), self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid, schema)
        duplicate = deepcopy(value)
        duplicate["documents"].append(deepcopy(duplicate["documents"][0]))
        with self.assertRaises(jsonschema.ValidationError):
            jsonschema.validate(duplicate, schema)
        with self.assertRaises(AuditError):
            self.run_read(units, persistent=False,
                          alter=lambda result: result["documents"].append(deepcopy(result["documents"][0])))

    def test_no_scope_does_not_persist_between_calls(self):
        unit = self.unit("isolated")
        self.assertEqual(len(self.run_read([unit], persistent=False)[1]), 1)
        self.assertEqual(len(self.run_read([unit], persistent=False)[1]), 1)
        self.assertFalse((self.root / "worktrees").exists())

    def test_reordered_model_documents_bind_by_uid_not_array_position(self):
        units = [self.unit("first", 20), self.unit("second", 40)]
        documents, _, _ = self.run_read(units, reverse=True)
        self.assertNotEqual(documents[0]["facts"], documents[1]["facts"])
        renamed = [{**u, "unit_id": "next-" + u["unit_id"]} for u in units]
        next_documents, calls, _ = self.run_read(renamed, "next-run")
        self.assertEqual(calls, [])
        self.assertEqual([d["facts"] for d in next_documents], [d["facts"] for d in documents])

    def test_large_context_is_read_normally_without_retaining_cache_record(self):
        unit = self.unit("large-context", native=["x" * (2 * 1024 * 1024 + 1)])
        for _ in range(2):
            documents, calls, _ = self.run_read([unit])
            self.assertEqual(len(documents), 1)
            self.assertEqual(len(calls), 1)
        self.assertEqual(list((self.root / "worktrees/.cache/image-facts-v1").glob("*.json")), [])

    def test_cache_hit_still_runs_new_gate_and_visual_photo_review_against_changed_contract(self):
        photo = self.unit("current-photo")
        provider = PdfEvidenceProvider(None, None, connection=self.selection, stage_summary_reviewer=isolated_stage_summary)
        counters = {"read": 0, "gate": 0, "photo_review": 0}
        compared = []
        active = {}
        def transport(**kwargs):
            name = kwargs["root"].name
            prompt = kwargs["prompt"]
            if name.startswith("read-"):
                counters["read"] += 1
                rows = json.loads(prompt.partial_variables["manifest"])
                value = {"documents": [{"unit_id": row["unit_id"], "roles": ["photo"],
                                         "facts": ["拍摄日期2026年10月1日"], "limitations": []} for row in rows]}
            elif name == "classify":
                counters["gate"] += 1
                self.assertEqual(kwargs["images"], [])
                self.assertIn(active["contract"], prompt.format())
                value = classification("promotional_display", ["contract", photo["unit_id"]])
                selected = next(m for m in value["material_matches"] if m["scenario"] == "promotional_display")
                value.update(material_matches=[selected], materials=selected["materials"], flags=selected["flags"])
            else:
                counters["photo_review"] += 1
                self.assertEqual(len(kwargs["images"]), 1)
                self.assertEqual(kwargs["images"][0].read_bytes(), photo["image"].read_bytes())
                self.assertIn(active["contract"], prompt.format())
                compared.append(active["contract"])
                value = {"photo_reviews": [{"unit_id": photo["unit_id"], "checks": [
                    {"rule_id": rule_id, "status": "unknown", "reason": active["contract"] + "，仍需核对本图实际情况",
                     "source_ids": [photo["unit_id"], "contract"]} for rule_id in DISPLAY_PHOTO_RULES]}]}
            kwargs["validator"](value)
            return value
        with patch("audit_core.qwen_model.extract_qwen_json", side_effect=transport):
            for index, contract in enumerate(("合同甲约定10月", "合同乙改为11月")):
                active["contract"] = contract
                temporary = self.root / ("full-" + str(index))
                temporary.mkdir()
                units = [{"unit_id": "contract", "image": None, "native_facts": [contract], "limitations": []}, photo]
                case = {"archive_id": "a001", "selected_scenario": "promotional_display", "units": units,
                        "worktrees_root": self.root / "worktrees", "workspace_id": f"run-{index}"}
                result = provider.classify(case, temporary)
                current = {**case, "scenario": "promotional_display", "documents": result["documents"],
                           "materials": [{"id": "promotion_contract", "state": "present", "source_ids": ["contract"]},
                                         {"id": "display_photos", "state": "present", "source_ids": [photo["unit_id"]]}]}
                provider._audit_display_photos(current, temporary, [], [])
        self.assertEqual(counters, {"read": 1, "gate": 2, "photo_review": 2})
        self.assertEqual(compared, ["合同甲约定10月", "合同乙改为11月"])

    def test_real_call_deduplicates_only_exact_bytes_and_explains_alias_to_transport(self):
        a, b, c = self.unit("first"), self.unit("copy"), self.unit("different", 90)
        provider = PdfEvidenceProvider(None, None, connection=self.selection, stage_summary_reviewer=isolated_stage_summary)
        recorded = []
        def transport(**kwargs):
            recorded.append(kwargs)
            if kwargs['images']:
                self.assertEqual(len(kwargs['images']), 1)
                return {'facts': ['本张实际可见内容'], 'limitations': []}
            return {"documents": []}
        with patch("audit_core.qwen_model.extract_qwen_json", side_effect=transport):
            provider._call(self.root / "transport", self.root, DOCUMENT_SCHEMA,
                           bound_prompt("Review both current placements first and copy against the new contract."),
                           images=[a["image"], b["image"], c["image"]], label="test", validator=lambda _: None)
        self.assertEqual([len(call['images']) for call in recorded], [1, 1, 0])
        self.assertEqual(recorded[0]['images'][0].read_bytes(), a['image'].read_bytes())
        self.assertEqual(recorded[1]['images'][0].read_bytes(), c['image'].read_bytes())
        sent = recorded[-1]["prompt"].format()
        self.assertIn('"image":"copy.png"', sent)
        self.assertIn('"same_bytes_as":"first.png"', sent)
        self.assertIn("不得因此合并门店、付款记录或审核结论", sent)


if __name__ == "__main__":
    unittest.main()
