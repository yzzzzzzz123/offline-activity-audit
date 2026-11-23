"""Endcap/area alternatives: isolated sources and model substitutes, no customer run."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from itertools import product
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.display_size_options import alternative_contract_sources, alternative_status
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import (audit_decision, photo_review_schema, validate,
                                     validate_photo_reviews)
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_workflow import build_view
from tests.pdf_test_support import unknown_checks
from tests.test_optional_staff_and_display_photos import photo_case, review


CONTRACT = "本次陈列整组端架或1平米堆头。"


def fixture(root, count=1):
    case = photo_case(root, count)
    case["documents"][0]["facts"] = [CONTRACT]
    for doc in case["documents"][1:]:
        doc["facts"] = ["独立端架的完整结构及四周边界清晰可见，未提供占地面积依据。"]
    return case


def size_review(uid, endcap="pass", area="unknown", *, selected="either", restriction=None):
    row = review(uid)
    size = row["checks"][3]
    status = alternative_status([endcap, area]) if selected == "either" else endcap if selected == "whole_endcap" else area
    reason = {"pass": "完整端架的四周边界清晰，可确认满足整组端架规格。",
              "unknown": "端架右侧超出画面，无法确认整组范围，实际面积也没有可验证的参照。",
              "fail": "完整范围可见且与适用规格有明确差异。"}[status]
    size.update(status=status, reason=reason, size_options={
        "contract_source": {"unit_id": "contract", "quote": CONTRACT},
        "selected_option": selected, "restriction_source": restriction,
        "options": [
            {"kind": "whole_endcap", "status": endcap,
             "reason": "架体完整范围清晰可见。" if endcap == "pass" else "右侧边界超出本图。",
             "source_ids": [uid, "contract"]},
            {"kind": "area", "status": area, "reason": "本图未提供有真实尺寸依据的面积参照。",
             "source_ids": [uid, "contract"]},
        ],
    })
    return row


class DisplaySizeOptionsTests(unittest.TestCase):
    def validate(self, case, rows, *, require=True):
        validate_photo_reviews({"photo_reviews": rows}, [r["unit_id"] for r in rows],
                               case["documents"], case["materials"], case["units"],
                               require_size_options=require)

    def test_all_nine_or_combinations_and_contradictory_overall_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            for endcap, area in product(("pass", "fail", "unknown"), repeat=2):
                with self.subTest(endcap=endcap, area=area):
                    row = size_review("p01", endcap, area)
                    before = deepcopy(row)
                    self.validate(case, [row])
                    self.assertEqual(row, before)
                    expected = "pass" if "pass" in (endcap, area) else "fail" if endcap == area == "fail" else "unknown"
                    self.assertEqual(row["checks"][3]["status"], expected)
                    row["checks"][3]["status"] = "unknown" if expected != "unknown" else "pass"
                    with self.assertRaisesRegex(AuditError, "适用分支不一致"):
                        self.validate(case, [row])

    def test_whole_endcap_pass_area_unknown_no_longer_becomes_a_size_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), 7)
            rows = [size_review(f"p{i:02d}") for i in range(1, 8)]
            evidence = unknown_checks("promotional_display", case["flags"])
            evidence["photo_reviews"] = rows
            before = deepcopy(evidence)
            result = audit_decision("promotional_display", case["flags"], evidence,
                                    case["documents"], case["materials"], units=case["units"])
            size = next(c for c in result["checks"] if c["rule_id"] == "display_size")
            self.assertEqual(size["status"], "pass")
            packet = {**case, "source_archive": "synthetic-endcap-alternatives.zip", "result": result}
            output = build_view([packet], None)
            photo_rows = [r for s in output["sheets"] for r in s["rows"] if r.get("photo_unit_id")]
            self.assertEqual(len(photo_rows), 7)
            self.assertTrue(all(not r["summary_items"] for r in photo_rows))
            self.assertEqual(evidence, before)
            self.assertEqual(len(result["photo_reviews"]), 7)
            self.assertEqual(result["photo_reviews"][0]["checks"][3]["size_options"]["options"][1]["status"], "unknown")

    def test_real_boundary_and_area_uncertainty_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            row = size_review("p01", "unknown", "unknown")
            self.validate(case, [row])
            self.assertEqual(row["checks"][3]["status"], "unknown")
            self.assertIn("超出画面", row["checks"][3]["reason"])

    def test_explicit_contract_store_restriction_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            quote = "甲店仅执行1平米堆头。"
            case["documents"][0]["facts"].append(quote)
            row = size_review("p01", selected="area", restriction={"unit_id": "contract", "quote": quote})
            self.validate(case, [row])
            self.assertEqual(row["checks"][3]["status"], "unknown")
            row["checks"][3]["status"] = "pass"
            with self.assertRaisesRegex(AuditError, "适用分支不一致"):
                self.validate(case, [row])

    def test_bare_application_label_fabricated_quote_and_negative_restriction_cannot_remove_option(self):
        with tempfile.TemporaryDirectory() as tmp:
            for uid, quote in (("qince", "陈列、堆头类型：1平堆"),
                               ("contract", "甲店仅执行1平米堆头。"),
                               ("contract", "甲店不得仅执行1平米堆头。"),
                               ("contract", "甲店执行整组端架或1平米堆头。")):
                with self.subTest(quote=quote):
                    case = fixture(Path(tmp))
                    case["documents"].append({"unit_id": "qince", "facts": [quote], "limitations": []})
                    if "不得" in quote or "或" in quote:
                        case["documents"][0]["facts"].append(quote)
                    row = size_review("p01", selected="area", restriction={"unit_id": uid, "quote": quote})
                    row["checks"][3]["source_ids"].append("qince")
                    with self.assertRaisesRegex(AuditError, "收窄二选一"):
                        self.validate(case, [row])

    def test_every_branch_has_its_own_photo_and_contract_citations(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), 2)
            for bad in (["p02", "contract"], ["p01", "missing"], ["contract"]):
                row = size_review("p01")
                row["checks"][3]["size_options"]["options"][0]["source_ids"] = bad
                with self.subTest(sources=bad), self.assertRaises(AuditError):
                    self.validate(case, [row])

    def test_no_fabricated_contract_and_no_reusing_other_checks(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            for change in ("quote", "duplicate", "brand"):
                row = size_review("p01")
                if change == "quote":
                    row["checks"][3]["size_options"]["contract_source"]["quote"] = "合同允许免查尺寸。"
                elif change == "duplicate":
                    row["checks"][3]["size_options"]["options"][1]["kind"] = "whole_endcap"
                else:
                    row["checks"][1]["size_options"] = row["checks"][3].pop("size_options")
                with self.subTest(change=change), self.assertRaises(AuditError):
                    self.validate(case, [row])

    def test_new_model_schema_and_validator_require_both_alternatives_but_old_records_remain_readable(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            old = {"photo_reviews": [review("p01")]}
            schema = photo_review_schema(["p01"], ["p01", "contract", "application"], ["contract"], size_alternatives=True)
            with self.assertRaises(AuditError):
                validate(old, schema)
            with self.assertRaisesRegex(AuditError, "分别提供"):
                self.validate(case, old["photo_reviews"])
            self.validate(case, old["photo_reviews"], require=False)
            validate({"photo_reviews": [size_review("p01")]}, schema)

    def test_non_alternative_contracts_and_negated_reading_limitations_do_not_activate_new_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp))
            for text in ("本次3纵位陈列。", "本次1平米堆头。", "本次整组端架。", "本次整组端架和1平米堆头。"):
                case["documents"][0]["facts"] = [text]
                case["documents"][0]["limitations"] = [CONTRACT]
                self.assertEqual(alternative_contract_sources(case["documents"], case["materials"]), set())
                self.validate(case, [review("p01")])

    def test_real_photo_provider_wires_schema_prompt_and_branch_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            case = fixture(Path(tmp), 2)
            provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
            def call(root, skill, schema, prompt, *, images, label, validator):
                self.assertEqual(len(images), 2)
                text = render_prompt(prompt)
                self.assertIn("先直接数本图可见的横向列数、前后排数或纵位", text)
                self.assertIn("直接在display_size.reason中说明本图对各适用规格的判断", text)
                self.assertIn("不要求另外填写size_options", text)
                response = {"photo_reviews": [size_review("p01"), size_review("p02", "unknown", "unknown")]}
                validate(response, schema)
                validator(response)
                ordinary = {"photo_reviews": [review("p01"), review("p02")]}
                validate(ordinary, schema)
                validator(ordinary)
                wrong = deepcopy(response)
                wrong["photo_reviews"][0]["checks"][3]["status"] = "unknown"
                before = deepcopy(wrong)
                validator(wrong)
                self.assertEqual(wrong, before)
                return response
            with patch.object(provider, "_call", side_effect=call):
                rows = provider._audit_display_photos(case, Path(tmp), [], [])
            self.assertEqual([row["checks"][3]["status"] for row in rows], ["pass", "unknown"])


if __name__ == "__main__":
    unittest.main()
