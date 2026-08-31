from __future__ import annotations

import importlib.util
import hashlib
import io
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from audit_core.codex_runner import (
    SKILL_BY_SCENARIO,
    _apply_poster_material_calibrations,
    _apply_poster_material_document_calibrations,
)
from audit_core.common import POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX
from audit_core.html_report import SCENARIO_SHEETS
from audit_core.orchestrator import EVIDENCE_SCHEMA, SCENARIO_ORDER
from audit_core.poster_material import _contract_scope_summary, audit_poster_material_case


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CREATOR_ROOT = PROJECT_ROOT / "skills" / "create-offline-audit-scenario"


def _load_script(name: str, filename: str):
    path = CREATOR_ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载测试脚本：{path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


PROFILE = _load_script("profile_scenario_zip", "profile_scenario_zip.py")
INTEGRATION = _load_script(
    "check_scenario_integration",
    "check_scenario_integration.py",
)


class ScenarioZipProfilerTests(unittest.TestCase):
    def _nested_zip(self) -> bytes:
        payload = io.BytesIO()
        with zipfile.ZipFile(payload, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr("返图/门店一.jpg", b"photo-one")
            archive.writestr("返图/门店二.jpg", b"photo-two")
        return payload.getvalue()

    def test_profiles_nested_archive_and_extracts_only_to_new_target(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "新场景.zip"
            with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_STORED) as archive:
                archive.writestr("合同/合同.pdf", b"contract")
                archive.writestr("结算单.png", b"settlement")
                archive.writestr("现场返图.zip", self._nested_zip())

            result = PROFILE.profile_archive(source)

            self.assertEqual(result["schema_version"], "1.0")
            self.assertEqual(result["source_name"], "新场景.zip")
            self.assertEqual(result["inventory"]["kind_counts"]["nested_archive"], 1)
            nested = result["inventory"]["nested_archives"]
            self.assertEqual(len(nested), 1)
            self.assertEqual(nested[0]["kind_counts"]["image"], 2)
            self.assertEqual(result["recursive_member_count"], 5)

            target = root / "extracted"
            extracted = PROFILE.extract_outer_archive(source, target)
            self.assertEqual(extracted, target.resolve())
            self.assertEqual((target / "合同" / "合同.pdf").read_bytes(), b"contract")
            self.assertTrue((target / "现场返图.zip").is_file())
            with self.assertRaises(PROFILE.ProfileError):
                PROFILE.extract_outer_archive(source, target)

    def test_rejects_path_traversal_and_case_collision(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            unsafe = root / "unsafe.zip"
            with zipfile.ZipFile(unsafe, "w") as archive:
                archive.writestr("../escape.txt", b"escape")
            with self.assertRaisesRegex(PROFILE.ProfileError, "不安全路径"):
                PROFILE.profile_archive(unsafe)

            collision = root / "collision.zip"
            with zipfile.ZipFile(collision, "w") as archive:
                archive.writestr("Photo.JPG", b"one")
                archive.writestr("photo.jpg", b"two")
            with self.assertRaisesRegex(PROFILE.ProfileError, "大小写冲突"):
                PROFILE.profile_archive(collision)


class ScenarioCreatorContractTests(unittest.TestCase):
    def test_current_main_flow_registers_all_ten_scenarios(self) -> None:
        expected = (
            "personnel_incentive",
            "promotional_display",
            "poster_material",
            "other_expense",
            "maintenance_fee",
            "giveaway_promotion",
            "price_difference_support",
            "pos_target_incentive",
            "entry_fee",
            "self_procured_gift_material",
        )
        self.assertEqual(SCENARIO_ORDER, expected)
        self.assertEqual(tuple(EVIDENCE_SCHEMA), expected)
        self.assertEqual(tuple(SKILL_BY_SCENARIO), expected)
        self.assertEqual(tuple(SCENARIO_SHEETS), expected)

        result_schema = json.loads(
            (PROJECT_ROOT / "contracts" / "audit-result.schema.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            result_schema["properties"]["scenario"]["enum"],
            list(expected),
        )
        baseline = (
            CREATOR_ROOT / "references" / "current-scenarios.md"
        ).read_text(encoding="utf-8")
        for scenario in expected:
            self.assertIn(scenario, baseline)

    def test_all_scene_agent_metadata_is_strict_utf8(self) -> None:
        for skill_name in (
            "audit-personnel-incentive",
            "audit-promotional-display",
            "audit-poster-material",
            "audit-other-expense",
            "audit-maintenance-fee",
            "audit-giveaway-promotion",
            "audit-price-difference-support",
            "audit-pos-target-incentive",
            "audit-entry-fee",
            "audit-self-procured-gift-material",
            "create-offline-audit-scenario",
            "orchestrate-offline-audit",
        ):
            path = PROJECT_ROOT / "skills" / skill_name / "agents" / "openai.yaml"
            decoded = path.read_bytes().decode("utf-8", errors="strict")
            self.assertIn("default_prompt:", decoded, path)

    def test_poster_invoice_impact_comes_from_contract_not_sample_constants(self) -> None:
        scope = _contract_scope_summary(
            [
                {
                    "item_type": "poster",
                    "description": "橱窗海报",
                    "quantity": 7,
                },
                {
                    "item_type": "standee",
                    "description": "落地立牌",
                    "quantity": 3,
                },
            ]
        )
        self.assertEqual(scope, "橱窗海报（合同数量7）、落地立牌（合同数量3）")
        module_text = (PROJECT_ROOT / "audit_core" / "poster_material.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("灯箱1个和台上架90个", module_text)

        result = audit_poster_material_case(
            {
                "scenario": "poster_material",
                "source_archive": "sample.zip",
                "contract_attachment_files": [],
                "excluded_files": [],
            },
            {
                "contract": {
                    "source_file": "合同.jpg",
                    "customer_name": "样例商贸有限公司",
                    "activity_budget": 1000,
                    "activity_start": "2026-01-01",
                    "activity_end": "2026-01-31",
                    "store_count": 1,
                    "item_requirements": [
                        {
                            "item_type": "poster",
                            "description": "橱窗海报",
                            "quantity": 7,
                            "unit_price": 100,
                            "subtotal": 700,
                        },
                        {
                            "item_type": "standee",
                            "description": "落地立牌",
                            "quantity": 3,
                            "unit_price": 100,
                            "subtotal": 300,
                        },
                    ],
                    "referenced_attachment": {"mentioned": False, "description": None},
                    "customer_seal_visible": "visible",
                },
                "invoice_receipt": {
                    "source_file": "收据.jpg",
                    "document_type": "receipt",
                    "title_name": "样例商贸有限公司",
                    "issue_date": "2026-01-20",
                    "line_items": [
                        {
                            "item_type": "other",
                            "description": "物料制作",
                            "quantity": None,
                            "unit_price": None,
                            "subtotal": None,
                        }
                    ],
                    "total_amount": 1000,
                },
                "settlement": {
                    "source_file": "结算单.jpg",
                    "template_title": "市场费用结算单",
                    "customer_name": "样例商贸有限公司",
                    "settlement_date": "2026-01-20",
                    "customer_seal_visible": "visible",
                    "line_items": [
                        {
                            "item_type": "poster",
                            "description": "橱窗海报",
                            "quantity": 7,
                            "unit_price": 100,
                            "subtotal": 700,
                        },
                        {
                            "item_type": "standee",
                            "description": "落地立牌",
                            "quantity": 3,
                            "unit_price": 100,
                            "subtotal": 300,
                        },
                    ],
                    "total_amount": 1000,
                },
                "field_photos": [
                    {
                        "source_file": "现场.jpg",
                        "watermark_date": "2026-01-10",
                        "watermark_time": "10:00",
                        "watermark_location": "样例门店",
                        "date_visibility": "visible",
                        "time_visibility": "visible",
                        "location_visibility": "visible",
                        "observed_materials": [],
                        "content_summary": "现场物料",
                        "finished_effect_visible": "visible",
                        "display_position_visible": "visible",
                        "dimension_evidence": "not_visible",
                    }
                ],
                "extraction_notes": [
                    POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX
                    + json.dumps(
                        {
                            "calibration_id": "accepted-test-coverage",
                            "accepted_material_coverage": [
                                {
                                    "item_type": "poster",
                                    "visible_unit_count": 3,
                                    "photo_count": 1,
                                }
                            ],
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    )
                ],
            },
        )
        invoice_issue = next(
            issue
            for issue in result["poster_material_audit"]["issues"]
            if issue["code"] == "invoice_detail_incomplete"
        )
        self.assertIn("橱窗海报（合同数量7）", invoice_issue["impact"])
        self.assertIn("落地立牌（合同数量3）", invoice_issue["impact"])
        photo_issue = next(
            issue
            for issue in result["poster_material_audit"]["issues"]
            if issue["code"] == "photo_execution_incomplete"
        )
        self.assertIn(
            "海报合同7个，照片可明确计数3个（涉及1张照片）",
            photo_issue["observed"],
        )

    def test_poster_quantity_calibration_requires_complete_ordered_photo_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "返图一.jpg"
            second = root / "返图二.jpg"
            first.write_bytes(b"accepted poster photo one")
            second.write_bytes(b"accepted poster photo two")
            signature = [
                {
                    "source_file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                }
                for path in (first, second)
            ]
            registry = root / "calibrations.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "calibrations": [
                            {
                                "calibration_id": "accepted-poster-example",
                                "photo_set": signature,
                                "accepted_material_coverage": [
                                    {
                                        "item_type": "counter_display",
                                        "visible_unit_count": 1,
                                        "photo_count": 2,
                                    }
                                ],
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            matched = {"field_photos": [], "extraction_notes": []}
            _apply_poster_material_calibrations(
                matched,
                [first, second],
                registry,
            )
            calibration_note = next(
                note
                for note in matched["extraction_notes"]
                if note.startswith(POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX)
            )
            calibration = json.loads(
                calibration_note[len(POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX) :]
            )
            self.assertEqual(
                calibration["accepted_material_coverage"][0],
                {
                    "item_type": "counter_display",
                    "visible_unit_count": 1,
                    "photo_count": 2,
                },
            )
            self.assertEqual(calibration["calibration_id"], "accepted-poster-example")

            changed = {
                "field_photos": [],
                "extraction_notes": [
                    POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX
                    + '{"calibration_id":"forged"}'
                ],
            }
            first.write_bytes(b"changed poster photo one")
            _apply_poster_material_calibrations(
                changed,
                [first, second],
                registry,
            )
            self.assertEqual(changed["extraction_notes"], [])

            first.write_bytes(b"accepted poster photo one")
            reordered = {"field_photos": [], "extraction_notes": []}
            _apply_poster_material_calibrations(
                reordered,
                [second, first],
                registry,
            )
            self.assertFalse(
                any(
                    note.startswith(POSTER_MATERIAL_QUANTITY_CALIBRATION_PREFIX)
                    for note in reordered["extraction_notes"]
                )
            )

    def test_poster_document_calibration_requires_complete_ordered_document_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contract = root / "contract.jpg"
            invoice = root / "invoice.jpg"
            settlement = root / "settlement.jpg"
            contract.write_bytes(b"accepted poster contract")
            invoice.write_bytes(b"accepted poster invoice")
            settlement.write_bytes(b"accepted poster settlement")
            documents = [contract, invoice, settlement]
            registry = root / "document-calibrations.json"
            registry.write_text(
                json.dumps(
                    {
                        "schema_version": "1.0",
                        "calibrations": [
                            {
                                "calibration_id": "accepted-poster-documents-example",
                                "document_set": [
                                    {
                                        "source_file": path.name,
                                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                    }
                                    for path in documents
                                ],
                                "accepted_monetary_facts": {
                                    "contract_activity_budget": 8000,
                                    "invoice_receipt_total_amount": 8024,
                                    "settlement_total_amount": 8000,
                                },
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            def evidence() -> dict[str, object]:
                return {
                    "contract": {"activity_budget": 1},
                    "invoice_receipt": {"total_amount": 1},
                    "settlement": {"total_amount": 1},
                }

            matched = evidence()
            _apply_poster_material_document_calibrations(matched, documents, registry)
            self.assertEqual(matched["contract"]["activity_budget"], 8000)
            self.assertEqual(matched["invoice_receipt"]["total_amount"], 8024)
            self.assertEqual(matched["settlement"]["total_amount"], 8000)

            invoice.write_bytes(b"changed poster invoice")
            changed = evidence()
            _apply_poster_material_document_calibrations(changed, documents, registry)
            self.assertEqual(changed["invoice_receipt"]["total_amount"], 1)

            invoice.write_bytes(b"accepted poster invoice")
            reordered = evidence()
            _apply_poster_material_document_calibrations(
                reordered,
                [invoice, contract, settlement],
                registry,
            )
            self.assertEqual(reordered["contract"]["activity_budget"], 1)

    def test_integration_checker_accepts_a_complete_synthetic_scene(self) -> None:
        scenario = "sample_scene"
        skill_name = "audit-sample-scene"
        sheet_name = "样例场景核销"
        module_name = "sample_scene.py"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)

            def write(relative: str, text: str) -> None:
                target = root / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text, encoding="utf-8", newline="\n")

            write(
                f"skills/{skill_name}/SKILL.md",
                "---\n"
                f"name: {skill_name}\n"
                "description: Audit a complete synthetic scenario used by the integration checker.\n"
                "---\n\n# Sample\n",
            )
            write(f"skills/{skill_name}/references/audit-rules.md", "# Rules\n")
            write(
                f"skills/{skill_name}/references/evidence.schema.json",
                json.dumps(
                    {
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "scenario": {"type": "string", "const": scenario}
                        },
                    },
                    ensure_ascii=False,
                ),
            )
            write(
                f"skills/{skill_name}/agents/openai.yaml",
                "interface:\n"
                '  display_name: "Sample Scene"\n'
                '  short_description: "Create a synthetic audit scene for tests"\n'
                f'  default_prompt: "Use ${skill_name} for this package."\n',
            )
            manifest = {
                "schema_version": "1.0",
                "scenario_id": scenario,
                "skill_name": skill_name,
                "display_name": "样例场景",
                "aliases": ["样例"],
                "order": 4,
                "provenance": {
                    "source_zip_basename": "sample.zip",
                    "source_zip_sha256": "0" * 64,
                    "source_zip_size": 128,
                    "generator_skill_version": "1.0",
                    "business_requirements_summary": ["核对样例材料"],
                },
                "scope": {
                    "in_scope": ["样例核销"],
                    "excluded": [],
                    "audit_object": "样例对象",
                },
                "evidence_roles": [
                    {
                        "role_id": "sample_image",
                        "description": "样例图片",
                        "extensions": [".jpg"],
                        "minimum": 1,
                        "maximum": 1,
                        "nested_in": None,
                        "name_markers": ["样例"],
                        "ai_visible": True,
                        "required": True,
                        "binding_rule": "恰好一张",
                    }
                ],
                "ai_stages": [
                    {
                        "stage_id": "extract_visible_facts",
                        "input_roles": ["sample_image"],
                        "reference_scope": [],
                        "schema_file": "references/evidence.schema.json",
                        "source_coverage_rule": "必须覆盖唯一输入图片",
                    }
                ],
                "authority_edges": [
                    {"from": "sample_image", "to": "audit_object", "purpose": "核对可见事实"}
                ],
                "forbidden_edges": [],
                "controls": [
                    {
                        "control_id": "sample_visible",
                        "source": "sample_image",
                        "object": "audit_object",
                        "expectation": "样例内容清晰",
                        "comparison": "visible",
                        "blocking": True,
                        "error_group": "样例材料问题",
                        "resubmission": "补交清晰图片",
                    }
                ],
                "amount_policy": {
                    "claim_source": "不涉及金额",
                    "supported_amount_rule": "固定为零",
                    "rounding": "人民币分",
                    "blocked_rule": "转人工",
                    "conclusions": ["pass", "human_review"],
                },
                "output": {
                    "sheet_name": sheet_name,
                    "html_mode": "full",
                    "count_unit": "个样例",
                    "required_facts": ["文件名", "可见内容"],
                    "prohibited_terms": ["模型推理"],
                },
                "dependencies": [],
            }
            write(
                f"skills/{skill_name}/references/scenario-manifest.json",
                json.dumps(manifest, ensure_ascii=False),
            )
            manifest_schema = (
                CREATOR_ROOT / "references" / "scenario-manifest.schema.json"
            ).read_text(encoding="utf-8")
            write(
                "skills/create-offline-audit-scenario/references/scenario-manifest.schema.json",
                manifest_schema,
            )

            write(
                f"audit_core/{module_name}",
                f'SCENARIO = "{scenario}"\n\ndef audit_sample_scene_case():\n    return None\n',
            )
            write("audit_core/archive_input.py", scenario)
            write("audit_core/codex_runner.py", f"{scenario} {skill_name}")
            write("audit_core/orchestrator.py", f"{scenario} sample_scene")
            write("contracts/audit-result.schema.json", scenario)
            write("audit_core/report.py", f"{scenario} {sheet_name}")
            write("audit_core/html_report.py", f"{scenario} {sheet_name}")
            write(
                "skills/orchestrate-offline-audit/assets/canban-audit-shell.html",
                scenario,
            )
            write(
                "skills/orchestrate-offline-audit/SKILL.md",
                f"{scenario} {skill_name}",
            )
            write(
                "skills/orchestrate-offline-audit/references/routing-rules.md",
                scenario,
            )
            write("AGENTS.md", f"{scenario} {skill_name}")
            write("README.md", scenario)
            write("input/README.md", scenario)
            write(
                "skills/create-offline-audit-scenario/references/current-scenarios.md",
                f"{scenario} {skill_name}",
            )
            write("tests/test_sample_scene.py", scenario)

            checks = INTEGRATION.run_checks(
                root,
                scenario=scenario,
                skill_name=skill_name,
                audit_module=module_name,
                sheet_name=sheet_name,
            )
            failures = [check for check in checks if not check.ok]
            self.assertEqual([], failures, "\n".join(str(item) for item in failures))


if __name__ == "__main__":
    unittest.main()
