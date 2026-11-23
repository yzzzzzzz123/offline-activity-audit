"""Cross-claim source facts reach new results without rewriting older audits."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.pdf_workflow import _reuse_rows, _scene_units
from audit_core.cross_claim_photos import model_advice, same_claim_candidates
from audit_core.model_owned_audit import audit_policy
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import SEAL_CLARIFICATION
from audit_core.workbench_delete import prepare_run_deletion
from audit_core.workbench_runtime import run_persistent_audit
from tests.pdf_test_support import PolicyProvider, bundle, image_bytes


class SceneProvider(PolicyProvider):
    def __init__(self):
        super().__init__(scenarios=("poster_material",))

    def __call__(self, case, temporary):
        result = super().__call__(case, temporary)
        if case["kind"] != "pdf_material_classification":
            return result
        roles = {"settlement.png": "settlement", "contract.png": "promotion_contract",
                 "invoice.png": "invoice", "scene.png": "finished_photos"}
        ids = {roles[Path(unit["source_file"]).name]: unit["unit_id"] for unit in case["units"]}
        for document in result["documents"]:
            document["roles"] = ["photo"] if document["unit_id"] == ids["finished_photos"] else ["document"]
        for match in result["classification"]["material_matches"]:
            for material in match["materials"]:
                if material["state"] in {"present", "unclear"}:
                    material["source_ids"] = [ids.get(material["id"],ids["settlement"]) ]
        return result


class PhotoReuseWorkflowTests(unittest.TestCase):
    def _run(self, root, serial, claim):
        source = root / ("input-" + serial)
        bundle(source, files={"settlement.png": image_bytes("white"),
                              "contract.png": image_bytes("blue" if serial == "a" else "green"),
                              "invoice.png": image_bytes("yellow"), "scene.png": image_bytes("red")})
        provider = SceneProvider()
        with patch("audit_core.qince_context.load_context", return_value=None):
            result = run_persistent_audit("20261103-" + serial, producer_model="fixture",
                        input_dir=source, worktrees_root=root / "worktrees", evidence_provider=provider,
                        biz_type="KT板等物料制作", verify_code=claim)
        workspace = root / "worktrees" / result["workspace_id"]
        snapshot = json.loads((workspace / "snapshot.json").read_text(encoding="utf8"))
        return workspace, snapshot, provider

    def test_same_claim_retry_is_excluded_cross_claim_exact_match_is_published_and_delete_revokes_it(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first, first_snapshot, _ = self._run(root, "a", "HX-A")
            frozen = (first / "snapshot.json").read_bytes()
            same, same_snapshot, _ = self._run(root, "same", "HX-A")
            current, snapshot, provider = self._run(root, "b", "HX-B")
            reuse = lambda value: [row for sheet in value["view"]["sheets"] for row in sheet["rows"]
                                  if row.get("rule_id") == "cross_claim_photo_reuse"]
            self.assertEqual(reuse(first_snapshot), [])
            self.assertEqual(reuse(same_snapshot), [])
            self.assertEqual(len(reuse(snapshot)), 1)
            row = reuse(snapshot)[0]
            self.assertIn("原图文件完全相同", row["error_reason"])
            self.assertEqual(row["values"][0], "scene.png")
            self.assertIn("的scene.png原图文件完全相同", row["error_reason"])
            self.assertNotIn("scene.png · scene.png", row["error_reason"])
            self.assertTrue(all(m["verify_code"] == "HX-A" for m in row["historical_photo_matches"]))
            self.assertEqual((first / "snapshot.json").read_bytes(), frozen)
            # The new contract and the complete current audit are still executed.
            calls = [call for call in provider.calls if call["kind"] == "pdf_policy_audit"]
            self.assertEqual(len(calls), 1)
            self.assertTrue(all(u["image"].exists() is False for u in calls[0]["units"] if u.get("image")))
            self.assertEqual(calls[0]["workspace_id"], current.name)
            self.assertEqual(calls[0]["worktrees_root"], root / "worktrees")
            # Deleting one source preserves the other owner's occurrence.
            plan = prepare_run_deletion(root / "worktrees", first.name, input_root=root / "input-oss")
            plan.execute()
            later, last, _ = self._run(root, "c", "HX-C")
            self.assertTrue(reuse(last))
            self.assertTrue(all(m["workspace_id"] != first.name for row in reuse(last)
                                for m in row["historical_photo_matches"]))
            self.assertTrue(same.is_dir())

    def test_mixed_receipt_and_contract_sources_are_not_treated_as_scene_photos(self):
        units = [{"unit_id": "receipt", "image": Path("receipt.png"), "source_file": "receipt.png", "locator": "p1"},
                 {"unit_id": "mixed", "image": Path("mixed.png"), "source_file": "mixed.png", "locator": "p1"}]
        materials = [{"id": "giveaway_evidence", "state": "present", "source_ids": ["receipt", "mixed"]},
                     {"id": "promotion_contract", "state": "present", "source_ids": ["mixed"]}]
        documents = [{"unit_id": "receipt", "roles": ["receipt"]}, {"unit_id": "mixed", "roles": ["photo"]}]
        sources = _scene_units(units, materials, documents)
        self.assertEqual([u["unit_id"] for u in sources], ["receipt"])
        self.assertFalse(sources[0]["scene_photo"])

    def test_near_candidates_do_not_create_customer_problems_or_manual_tasks(self):
        packet = {"units": [{"unit_id": "p", "source_file": "photo.png", "locator": "Sheet1!R2C2"}],
                  "documents": [], "photo_reuse": {"photos": [{"unit_id": "p", "exact_reuse": [],
                  "near_candidates": [{"verify_code": "OTHER", "distance": 2}]}]}}
        original = deepcopy(packet)
        self.assertEqual(_reuse_rows(packet, 4), [])
        self.assertEqual(packet, original)

    def test_same_claim_near_duplicates_are_reported_as_technical_candidates(self):
        prepared = [
            ({"unit_id": "a"}, "a.png", "", {"phash": "0000000000000000"}, {}),
            ({"unit_id": "b"}, "b.png", "", {"phash": "0000000000000003"}, {}),
            ({"unit_id": "c"}, "c.png", "", {"phash": "ffffffffffffffff"}, {}),
            ({"unit_id": "d"}, "d.png", "", {"phash": None}, {}),
        ]
        self.assertEqual(same_claim_candidates(prepared),
                         [{"unit_ids": ["a", "b"], "distance": 2}])

    def test_same_claim_near_duplicates_never_become_program_failures(self):
        packet = {"units": [{"unit_id": "a", "source_file": "图片 4.png", "locator": "图片 4.png"},
                            {"unit_id": "b", "source_file": "图片 5.png", "locator": "图片 5.png"}],
                  "documents": [],
                  "photo_reuse": {"status": "ready", "same_claim_candidates": [
                      {"unit_ids": ["a", "b"], "distance": 3}], "photos": [
                      {"unit_id": "a", "exact_reuse": [], "same_claim_near": [{"unit_id": "b", "distance": 3}]},
                      {"unit_id": "b", "exact_reuse": [], "same_claim_near": [{"unit_id": "a", "distance": 3}]}]}}
        original = deepcopy(packet)
        self.assertEqual(_reuse_rows(packet, 4), [])
        self.assertEqual(packet, original)

    def test_same_claim_model_context_binds_file_and_visible_facts_without_verdict(self):
        advice = {"status": "ready", "same_claim_candidates": [
            {"unit_ids": ["a", "b"], "distance": 3}]}
        original = deepcopy(advice)
        units = [
            {"unit_id": "a", "source_file": "陈列照片(4).png", "locator": "图片 4.png"},
            {"unit_id": "b", "source_file": "陈列照片(6).png", "locator": "图片 6.png"},
        ]
        documents = [
            {"unit_id": "a", "roles": ["photo"], "facts": [
                "本图水印显示鹤山互惠百货（赵庄店），拍摄日期2026年10月3日。"], "limitations": []},
            {"unit_id": "b", "roles": ["photo"], "facts": [
                "本图水印显示凌云大厦名仕店，拍摄日期2026年10月3日。"], "limitations": []},
        ]
        context = model_advice(advice, units, documents)
        pair = context["same_claim_candidates"][0]
        self.assertEqual(pair["distance"], 3)
        self.assertEqual([photo["source_file"] for photo in pair["photos"]],
                         ["陈列照片(4).png", "陈列照片(6).png"])
        self.assertIn("鹤山互惠百货（赵庄店）", pair["photos"][0]["facts"][0])
        self.assertIn("凌云大厦名仕店", pair["photos"][1]["facts"][0])
        self.assertNotIn("status", pair)
        self.assertEqual(advice, original)

    def test_contract_without_any_seal_is_explicit_in_reading_and_audit_policy(self):
        manifest = [{"unit_id": "contract", "image": "contract.png", "pdf_text": [],
                     "reading_limitations": [], "reader_skills": ["read-audit-document"]}]
        reading = PdfEvidenceProvider._reading_prompt(manifest, {}).format()
        policy = audit_policy()
        for text in (reading, policy, SEAL_CLARIFICATION):
            self.assertIn("没有任何实际印章或签字图形", text)
        self.assertIn("本页未见印章或签字", reading)
        self.assertNotIn("是否为公章或财务章", reading)
        self.assertIn("不区分公章、财务章、合同章、结算单章", reading)
        self.assertIn("contract_signed不符", policy)
        self.assertIn("photo_reuse_advice.same_claim_candidates", policy)
        self.assertIn("不得直接写“已确认造假”", policy)


if __name__ == "__main__":
    unittest.main()
