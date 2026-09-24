"""Designated sample provenance, runtime comparison, and evidence isolation."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from zipfile import ZipFile

import pymupdf

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.paths import PROJECT_ROOT
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.scenario_registry import SKILL_BY_SCENARIO
from audit_core.template_references import TEMPLATE_CHECKS, template_comparison
from tests.pdf_test_support import flags_for, unknown_checks
from tests.test_brand_amount_policy import numeric_case


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def manifest_for(skill):
    return json.loads((skill / "references/template-manifest.json").read_text(encoding="utf-8"))


class TemplateAssetTests(unittest.TestCase):
    def test_all_eight_skills_have_traceable_original_assets(self):
        source_zips = set()
        for scenario, skill in SKILL_BY_SCENARIO.items():
            with self.subTest(scenario=scenario):
                manifest = manifest_for(skill)
                self.assertEqual(manifest["scenario"], scenario)
                source_zips.add(manifest["source_zip"])
                self.assertTrue(manifest["documents"])
                for record in manifest["documents"]:
                    self.assertEqual(digest(skill / record["file"]), record["sha256"])
                    self.assertTrue(record["source_member"])
                    self.assertEqual(len(record["source_sha256"]), 64)
                    if record["usage"] == "template_check":
                        rule_id, material_id = TEMPLATE_CHECKS[scenario]
                        self.assertEqual(record["rule_ids"], [rule_id])
                        self.assertEqual(record["role"], material_id)
                    else:
                        self.assertEqual(record["usage"], "format_example")
                        self.assertEqual(record["rule_ids"], [])
        self.assertEqual(len(source_zips), 8)

    def test_selected_originals_and_split_pdf_pages_match_input_archives(self):
        manifests = [(skill, manifest_for(skill)) for skill in SKILL_BY_SCENARIO.values()]
        if any(not (PROJECT_ROOT / manifest["source_zip"]).is_file() for _, manifest in manifests):
            self.skipTest("User input ZIPs are not distributed in this checkout")
        for skill, manifest in manifests:
            archive = PROJECT_ROOT / manifest["source_zip"]
            with self.subTest(scenario=manifest["scenario"]), ZipFile(archive, metadata_encoding="gbk") as zipped:
                self.assertEqual(digest(archive), manifest["source_zip_sha256"])
                for record in manifest["documents"]:
                    original = zipped.read(record["source_member"])
                    self.assertEqual(hashlib.sha256(original).hexdigest(), record["source_sha256"])
                    extracted = skill / record["file"]
                    if extracted.suffix == ".pdf":
                        with pymupdf.open(stream=original, filetype="pdf") as source, pymupdf.open(extracted) as target:
                            self.assertEqual(len(target), len(record["source_pages"]))
                            for page, original_number in zip(target, record["source_pages"]):
                                self.assertEqual(page.get_pixmap().samples,
                                                 source[original_number-1].get_pixmap().samples)
                    else:
                        self.assertEqual(extracted.read_bytes(), original)

    def test_comparison_previews_match_selected_pages(self):
        for scenario in TEMPLATE_CHECKS:
            skill = SKILL_BY_SCENARIO[scenario]
            for record in manifest_for(skill)["documents"]:
                if record["usage"] != "template_check":
                    continue
                for preview in record["preview_images"]:
                    with self.subTest(scenario=scenario, page=preview["page"]):
                        asset = skill / preview["file"]
                        self.assertEqual(digest(asset), preview["sha256"])
                        original = skill / record["file"]
                        if original.suffix == ".pdf":
                            with pymupdf.open(original) as doc:
                                expected = doc[preview["page"]-1].get_pixmap(matrix=pymupdf.Matrix(1.6, 1.6), alpha=False)
                                self.assertEqual(pymupdf.Pixmap(str(asset)).samples, expected.samples)
                        else:
                            self.assertEqual(asset.read_bytes(), original.read_bytes())

    def test_only_existing_three_template_rules_receive_references(self):
        for scenario, skill in SKILL_BY_SCENARIO.items():
            references, images = template_comparison(skill, scenario)
            with self.subTest(scenario=scenario):
                self.assertEqual(len(images), {"entry_fee": 4, "poster_material": 1,
                                               "promotional_display": 1}.get(scenario, 0))
                self.assertEqual(len(references), len(images))
                for reference in references:
                    self.assertTrue(reference["reference_only"])
                    self.assertNotIn("unit_id", reference)
                    self.assertNotIn("source_ids", reference)

    def test_missing_tampered_or_escaped_assets_are_not_silently_used(self):
        for failure in ("missing_manifest", "missing_image", "tampered_image", "escaped_path"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temporary:
                skill = Path(temporary)
                (skill / "references").mkdir()
                (skill / "assets/templates").mkdir(parents=True)
                asset = skill / "assets/templates/reference.jpg"
                asset.write_bytes(b"approved-reference")
                record = {"usage": "template_check", "role": "settlement",
                          "rule_ids": ["settlement_template_seal"], "title": "指定结算单",
                          "file": "assets/templates/reference.jpg", "sha256": digest(asset)}
                record["preview_images"] = [{"file": record["file"], "sha256": record["sha256"], "page": 1}]
                if failure == "missing_image":
                    asset.unlink()
                elif failure == "tampered_image":
                    asset.write_bytes(b"changed-reference")
                elif failure == "escaped_path":
                    outside = skill / "outside.jpg"
                    outside.write_bytes(b"approved-reference")
                    record["file"] = "assets/templates/../../outside.jpg"
                if failure != "missing_manifest":
                    (skill / "references/template-manifest.json").write_text(
                        json.dumps({"scenario": "poster_material", "documents": [record]}), encoding="utf-8")
                references, images = template_comparison(skill, "poster_material")
                self.assertEqual(images, [])
                self.assertTrue(references[0]["unavailable"])
                self.assertIn("不算客户缺交资料", references[0]["reason"])


class TemplateRuntimeTests(unittest.TestCase):
    def test_runtime_attaches_current_files_and_references_without_mutating_evidence(self):
        for scenario, skill in SKILL_BY_SCENARIO.items():
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                current_image = root / "current-case-page.png"
                current_image.write_bytes(b"current case image")
                flags = flags_for(scenario)
                response = unknown_checks(scenario, flags)
                material_id = TEMPLATE_CHECKS.get(scenario, (None, "settlement"))[1]
                case = {"scenario": scenario, "archive_id": "a001", "flags": flags,
                        "documents": [{"unit_id": "current", "facts": ["本次上传资料"]}],
                        "materials": [{"id": material_id, "state": "present", "source_ids": ["current"]}],
                        "units": [{"unit_id": "current", "image": current_image}]}
                original = deepcopy(case)
                calls = []

                def fake_call(model_root, used_skill, schema, prompt, **kwargs):
                    calls.append((used_skill, render_prompt(prompt), kwargs["images"]))
                    kwargs["validator"](response)
                    return response

                provider = PdfEvidenceProvider("gpt-6-astra", "medium")
                with patch.object(provider, "_call", side_effect=fake_call):
                    provider.audit(case, root)
                self.assertEqual(case, original)
                self.assertEqual(calls[0][0], skill)
                self.assertIn((skill / "references/template-reference.md").read_text(encoding="utf-8"), calls[0][1])
                reference_images = template_comparison(skill, scenario)[1]
                self.assertEqual(calls[0][2], [current_image] + reference_images if scenario in TEMPLATE_CHECKS else [])

    def test_reference_id_cannot_replace_a_current_source_in_visual_or_numeric_checks(self):
        scenario = "poster_material"
        flags = flags_for(scenario)
        evidence = unknown_checks(scenario, flags)
        template_id = manifest_for(SKILL_BY_SCENARIO[scenario])["documents"][1]["id"]
        check = next(c for c in evidence["checks"] if c["rule_id"] == "settlement_template_seal")
        check.update(status="pass", source_ids=[template_id], reason="参考样张有盖章")
        with self.assertRaises(AuditError):
            audit_decision(scenario, flags, evidence, [], [])
        case, evidence, check = numeric_case("payment_claim_amount", "100", ["100"])
        check["comparisons"][0]["right"][0]["unit_id"] = template_id
        check["source_ids"].append(template_id)
        with self.assertRaises(AuditError):
            audit_decision(case["scenario"], case["flags"], evidence, case["documents"], case["materials"])

    def test_fixed_gates_receive_role_guides_without_adding_sample_documents(self):
        provider = PdfEvidenceProvider("gpt-6-astra", "medium")
        for scenario, skill in SKILL_BY_SCENARIO.items():
            with self.subTest(scenario=scenario):
                docs = [{"unit_id": "actual", "facts": ["来自本次资料"]}]
                original = deepcopy(docs)
                prompt = render_prompt(provider._gate_prompt(scenario, docs))
                self.assertEqual(docs, original)
                self.assertIn("参考样张不计本包来源，不补齐门禁", prompt)
                self.assertIn((skill / "references/template-reference.md").read_text(encoding="utf-8"), prompt)


if __name__ == "__main__":
    unittest.main()
