"""Customer clarifications: optional staff and an explanation for every photo."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from openpyxl import Workbook
from openpyxl.drawing.image import Image as SheetImage

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import (DISPLAY_PHOTO_RULES, audit_decision, classification_decision,
                                     classification_issues, display_photo_units, validate_documents,
                                     photo_review_schema, validate)
from audit_core.pdf_materials import PdfEvidenceProvider, _spreadsheet_units, _gate_documents
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.pdf_policy import STAFF_PHOTO_CLARIFICATION, requirements, rules
from audit_core.qince_context import POLICY as QINCE_CONTEXT_POLICY
from audit_core.qince_context import context_sources
from audit_core.pdf_workflow import build_view
from audit_core.workbench_store import render_analysis_summary_markdown
from tests.pdf_test_support import classification, flags_for, image_bytes, unknown_checks


def selected_staff(state="not_applicable", temporary_staff=None):
    flags = flags_for("personnel_incentive", temporary_staff=temporary_staff)
    value = classification("personnel_incentive", ["u"], flags=flags)
    value["source_ids"] = list(value["source_ids"])
    for candidate in value["material_matches"]:
        candidate["source_ids"] = list(candidate["source_ids"])
        for item in candidate["materials"]:
            item["source_ids"] = list(item["source_ids"])
    match = next(match for match in value["material_matches"] if match["scenario"] == "personnel_incentive")
    staff = next(material for material in match["materials"] if material["id"] == "staff_photos")
    staff.update(state=state, source_ids=["u"] if state in {"present", "unclear"} else [], reason="按本次临促条件盘点")
    if temporary_staff is True:
        staff["condition_evidence"] = [{"unit_id": "u", "quote": "本次安排临促2人", "stance": "affirmed"}]
    match["supported"] = temporary_staff is not True or state == "present"
    value.update(candidate_scenarios=["personnel_incentive"], material_matches=[match],
                 flags=flags, materials=match["materials"])
    return value


def review(uid, *, status="pass", problem="水印日期被遮挡，无法确认是否在活动期内"):
    return {"unit_id": uid, "checks": [
        {"rule_id": rule_id, "status": status if rule_id == "display_watermark" else "pass",
         "reason": problem if rule_id == "display_watermark" and status != "pass" else {
             "display_watermark": "水印日期为2026年10月1日，与合同活动期间一致",
             "display_brand": "申请区域内可见的货品均为参半，右侧其他品牌位于区域外",
             "display_material_brand": "围挡宣传物料可见参半品牌，没有其他品牌物料",
             "display_size": "完整陈列边界可见，与合同约定的陈列范围对应"}[rule_id],
         "source_ids": [uid, "contract", "application"] if rule_id == "display_watermark" else [uid, "contract"] if rule_id in {"display_brand", "display_size"} else [uid],
         "source_assertions": []}
        for rule_id in DISPLAY_PHOTO_RULES]}


def photo_case(root, count=2):
    units = [{"unit_id": "contract", "source_file": "合同.pdf", "locator": "第1页", "image": None,
              "native_facts": ["甲店2026年10月1日，陈列边界见合同"], "limitations": []}]
    units.append({"unit_id":"application","source_file":"系统申请原件.json","locator":"本次申请","image":None, "native_facts":["甲店2026年10月1日活动申请"],"limitations":[]})
    for index in range(count):
        uid = f"p{index+1:02d}"
        path = root / (uid + ".png")
        path.write_bytes(image_bytes())
        units.append({"unit_id": uid, "source_file": "照片收集.xlsx", "locator": f"甲店!R{index+2}C3（单元格图片）",
                      "image": path, "photo_reference": True, "native_facts": [], "limitations": []})
    documents = [{"unit_id": unit["unit_id"], "roles": ["photo"] if unit["image"] else ["contract"],
                  "facts": ["照片实际内容"], "limitations": unit["limitations"]} for unit in units]
    materials = [{"id": "promotion_contract", "state": "present", "source_ids": ["contract"], "reason": "合同原件"},
                 {"id": "display_photos", "state": "present", "source_ids": [u["unit_id"] for u in units if u.get("photo_reference")], "reason": "表格逐张照片"}]
    materials.append({"id":"activity_application","state":"present","source_ids":["application"],"reason":"独立本次申请"})
    return {"archive_id": "a001", "scenario": "promotional_display", "flags": flags_for("promotional_display"),
            "units": units, "documents": documents, "materials": materials}


class OptionalStaffTests(unittest.TestCase):
    def test_absent_unclear_and_submitted_staff_are_optional_without_an_affirmative_condition(self):
        for flag in (False, None):
            for state in ("missing", "unclear", "not_applicable", "present"):
                with self.subTest(flag=flag, state=state):
                    value = selected_staff(state, flag)
                    route = classification_decision(value, [{"unit_id": "u"}], "personnel_incentive")
                    self.assertTrue(route["matched"])
                    self.assertEqual(classification_issues(value, "personnel_incentive"), [])
                    result = audit_decision("personnel_incentive", value["flags"],
                                            unknown_checks("personnel_incentive", value["flags"]), [], value["materials"])
                    self.assertNotIn("staff_daily_photos", [c["rule_id"] for c in result["checks"]])

    def test_confirmed_staff_requires_material_but_does_not_restore_daily_photo_checks(self):
        documents = [{"unit_id": "u", "facts": ["本次安排临促2人"]}]
        for state in ("missing", "unclear", "present"):
            with self.subTest(state=state):
                value = selected_staff(state, True)
                route = classification_decision(value, documents, "personnel_incentive")
                self.assertEqual(route["matched"], state == "present")
                issues = classification_issues(value, "personnel_incentive")
                self.assertEqual(len(issues), 0 if state == "present" else 1)
                if issues:
                    self.assertIn("临促资料或照片", issues[0]["reason"])
                self.assertNotIn("staff_daily_photos", [r.id for r in rules("personnel_incentive")])
        with self.assertRaisesRegex(AuditError, "不能自行免交"):
            classification_decision(selected_staff("not_applicable", True), documents, "personnel_incentive")

    def test_affirmative_condition_needs_its_own_authorized_verbatim_source(self):
        for fault in ("missing", "payment", "fabricated", "unknown-source"):
            with self.subTest(fault=fault):
                value = selected_staff("missing", True)
                staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
                documents = [{"unit_id": "u", "facts": ["本次安排临促2人"]}]
                if fault == "missing":
                    staff.pop("condition_evidence")
                elif fault == "fabricated":
                    documents[0]["facts"] = ["本次按销售数量发放奖励"]
                elif fault == "unknown-source":
                    staff["condition_evidence"][0]["unit_id"] = "outside"
                else:
                    documents.append({"unit_id": "payment", "facts": ["本次安排临促2人"]})
                    next(m for m in value["materials"] if m["id"] == "payment")["source_ids"] = ["payment"]
                    value["source_ids"].append("payment")
                    value["material_matches"][0]["source_ids"].append("payment")
                    staff["condition_evidence"][0]["unit_id"] = "payment"
                with self.assertRaises(AuditError):
                    classification_decision(value, documents, "personnel_incentive")

    def test_denials_empty_templates_and_uncertainty_cannot_be_mislabeled_affirmative(self):
        for quote in ("无临促", "本次不涉及临促", "本次没有安排临促", "本次不申请临促费用",
                      "临促：否", "临促人数：0", "临促工资为0.00元", "本次不是临促工资",
                      "无需提供临促资料", "有无临促：", "临促工资：", "临促",
                      "是否涉及临促？", "临促安排待确认", "结算!R1: C1=是否临促 | C2=否",
                      "结算!R1: C1=有无临促 | C2=", "是否临促：□是 □否",
                      "临促安排：人员____ 日期____", "临促资料.zip"):
            with self.subTest(quote=quote):
                value = selected_staff("missing", True)
                staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
                staff["condition_evidence"][0]["quote"] = quote
                facts = [] if quote.endswith(".zip") else [quote]
                with self.assertRaises(AuditError):
                    classification_decision(value, [{"unit_id": "u", "facts": facts}], "personnel_incentive")

    def test_valid_yes_field_and_either_contract_or_settlement_can_establish_condition(self):
        for material_id in ("promotion_contract", "settlement"):
            for quote in ("本次安排临促2人", "有无临促：是", "结算!R1: C1=是否临促 | C2=是", "临促工资：0.50元"):
                with self.subTest(material=material_id, quote=quote):
                    value = selected_staff("missing", True)
                    documents = [{"unit_id": "u", "facts": ["本次按销售数量发放奖励"]},
                                 {"unit_id": "basis", "facts": [quote]}]
                    next(m for m in value["materials"] if m["id"] == material_id)["source_ids"] = ["basis"]
                    staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
                    staff["condition_evidence"] = [{"unit_id": "basis", "quote": quote, "stance": "affirmed"}]
                    value["source_ids"].append("basis")
                    value["material_matches"][0]["source_ids"].append("basis")
                    route = classification_decision(value, documents, "personnel_incentive")
                    self.assertFalse(route["matched"])
                    self.assertEqual(len(route["reasons"]), 1)
                    self.assertIn("临促资料或照片", route["reasons"][0])

    def test_flag_cannot_bypass_affirmative_evidence_or_override_denied_unclear_evidence(self):
        for stance, quote, bad_flags in (("affirmed", "本次安排临促2人", (False, None)),
                                        ("denied", "本次无临促", (True, None)),
                                        ("unclear", "临促安排待确认", (True, False))):
            for flag in bad_flags:
                with self.subTest(stance=stance, flag=flag):
                    value = selected_staff("missing", flag)
                    staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
                    staff["condition_evidence"] = [{"unit_id": "u", "quote": quote, "stance": stance}]
                    with self.assertRaises(AuditError):
                        classification_decision(value, [{"unit_id": "u", "facts": [quote]}], "personnel_incentive")

    def test_affirmative_original_cannot_be_disguised_as_denied_or_unclear(self):
        for stance, flag in (("denied", False), ("unclear", None)):
            value = selected_staff("missing", flag)
            staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
            staff["condition_evidence"] = [{"unit_id": "u", "quote": "本次安排临促2人", "stance": stance}]
            with self.subTest(stance=stance), self.assertRaisesRegex(AuditError, "肯否判断须与完整原文一致"):
                classification_decision(value, [{"unit_id": "u", "facts": ["本次安排临促2人"]}], "personnel_incentive")

    def test_known_affirmative_source_cannot_be_hidden_by_omitting_condition_evidence(self):
        for flag in (False, None):
            for omission in ("absent", "empty", "unrelated"):
                for facts in (["本次安排临促2人", "资料备注待确认"],
                              ["结算!R1: C1=是否临促", "结算!R2: C1=是", "资料备注待确认"]):
                    value = selected_staff("missing", flag)
                    staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
                    if omission == "empty":
                        staff["condition_evidence"] = []
                    elif omission == "unrelated":
                        staff["condition_evidence"] = [{"unit_id": "u", "quote": "资料备注待确认", "stance": "unclear"}]
                    with self.subTest(flag=flag, omission=omission, facts=facts), self.assertRaisesRegex(AuditError, "不能省略肯定原文证据"):
                        classification_decision(value, [{"unit_id": "u", "facts": facts}], "personnel_incentive")

    def test_unmentioned_staff_observations_and_non_contract_facts_do_not_trigger_requirement(self):
        for text in ("合同表格内未见‘临促’字段或临促安排记载", "未见临促字段", "本次按销售数量计算奖励"):
            value = selected_staff("missing", None)
            next(m for m in value["materials"] if m["id"] == "payment")["source_ids"] = ["payment"]
            value["source_ids"].append("payment")
            value["material_matches"][0]["source_ids"].append("payment")
            documents = [{"unit_id": "u", "facts": [text]},
                         {"unit_id": "payment", "facts": ["本次安排临促2人"]}]
            with self.subTest(text=text):
                result = classification_decision(value, documents, "personnel_incentive")
                self.assertTrue(result["matched"])
                self.assertEqual(result["reasons"], [])

    def test_adjacent_vertical_native_field_can_be_cited_without_inventing_a_row(self):
        for answer, flag, stance, missing in (("是", True, "affirmed", True),
                                             ("否", False, "denied", False),
                                             ("", None, "unclear", False)):
            facts = ["结算!R1: C1=是否临促", f"结算!R2: C1={answer}"]
            value = selected_staff("missing", flag)
            staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
            staff["condition_evidence"] = [{"unit_id": "u", "quote": "\n".join(facts), "stance": stance}]
            with self.subTest(answer=answer):
                result = classification_decision(value, [{"unit_id": "u", "facts": facts}], "personnel_incentive")
                self.assertEqual(result["matched"], not missing)
        for second in ("结算!R2: C2=是", "其他表!R2: C1=是", "结算!R3: C1=是"):
            facts = ["结算!R1: C1=是否临促", second]
            value = selected_staff("missing", True)
            staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
            staff["condition_evidence"] = [{"unit_id": "u", "quote": "\n".join(facts), "stance": "affirmed"}]
            with self.subTest(second=second), self.assertRaisesRegex(AuditError, "原文必须逐字来自"):
                classification_decision(value, [{"unit_id": "u", "facts": facts}], "personnel_incentive")

    def test_conflicting_contract_and_settlement_do_not_create_a_missing_photo_issue(self):
        value = selected_staff("missing", None)
        next(m for m in value["materials"] if m["id"] == "settlement")["source_ids"] = ["settlement"]
        staff = next(m for m in value["materials"] if m["id"] == "staff_photos")
        staff["condition_evidence"] = [{"unit_id": "u", "quote": "本次安排临促2人", "stance": "affirmed"}]
        value["source_ids"].append("settlement")
        value["material_matches"][0]["source_ids"].append("settlement")
        documents = [{"unit_id": "u", "facts": ["本次安排临促2人"]},
                     {"unit_id": "settlement", "facts": ["本次无临促"]}]
        self.assertTrue(classification_decision(value, documents, "personnel_incentive")["matched"])
        self.assertEqual(classification_issues(value, "personnel_incentive"), [])
        value["flags"]["temporary_staff"] = True
        with self.assertRaisesRegex(AuditError, "冲突证据一致"):
            classification_decision(value, documents, "personnel_incentive")

    def test_old_records_remain_schema_readable_and_names_never_supply_condition_evidence(self):
        from audit_core.pdf_evidence import classification_schema
        old = selected_staff("missing", True)
        next(m for m in old["materials"] if m["id"] == "staff_photos").pop("condition_evidence")
        validate(old, classification_schema("personnel_incentive"))
        for name in ("普通资料.xlsx", "临促工资照片.zip"):
            value = selected_staff("missing", None)
            documents = [{"unit_id": "u", "source_file": name, "facts": ["按销售数量计算奖励"]}]
            self.assertTrue(classification_decision(value, documents, "personnel_incentive")["matched"])

    def test_gate_projection_preserves_original_staff_rows_without_joining_column_values(self):
        facts = ["结算!R1: C1=本次活动", "结算!R2: C1=有无临促 | C2=否",
                 "结算!R3: C1=仅按销量激励", "结算!R4: C1=其他金额"]
        documents = [{"unit_id": "u", "roles": ["spreadsheet"], "facts": list(facts), "limitations": []}]
        projected = json.loads(_gate_documents(documents))[0]
        self.assertEqual(projected["staff_condition_facts"], facts[:3])
        self.assertEqual(documents[0]["facts"], facts)

    def test_both_classification_entries_use_the_conditional_policy_and_verbatim_evidence(self):
        for scenario in ("personnel_incentive", None):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
                unit = {"unit_id": "u", "image": None, "native_facts": ["结算!R1: C1=有无临促 | C2=否"],
                        "limitations": [], "source_file": "临促照片.xlsx", "locator": "结算表"}
                with patch.object(provider, "_call", return_value=selected_staff()) as call:
                    provider.classify({"archive_id": "a001", "selected_scenario": scenario, "units": [unit]},
                                      Path(temporary))
                prompt = render_prompt(call.call_args.args[3])
                self.assertIn(STAFF_PHOTO_CLARIFICATION, prompt)
                self.assertIn("condition_evidence", prompt)
                self.assertIn("条件性资料", prompt)
                self.assertIn("结算!R1: C1=有无临促 | C2=否", prompt)
                self.assertNotIn("temporary_staff仅记录可选临促事实", prompt)

    def test_optional_staff_does_not_exempt_payment_or_other_required_material(self):
        for material_id in ("payment", "pos", "pos_excel", "settlement", "promotion_contract"):
            with self.subTest(material=material_id):
                value = selected_staff()
                next(m for m in value["materials"] if m["id"] == material_id).update(state="missing", source_ids=[])
                value["material_matches"][0]["supported"] = False
                route = classification_decision(value, [{"unit_id": "u"}], "personnel_incentive")
                self.assertFalse(route["matched"])
                self.assertTrue(route["reasons"])
                self.assertNotIn("临促", "；".join(route["reasons"]))

    def test_superseded_staff_check_is_rejected_and_prompt_uses_latest_confirmation(self):
        flags = flags_for("personnel_incentive", temporary_staff=True)
        value = unknown_checks("personnel_incentive", flags)
        value["checks"].append({"rule_id": "staff_daily_photos", "status": "fail", "reason": "缺每日临促照",
                                "source_ids": [], "comparisons": []})
        with self.assertRaisesRegex(AuditError, "全部且仅覆盖"):
            audit_decision("personnel_incentive", flags, value, [])
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", return_value=unknown_checks("personnel_incentive", flags)) as call:
            provider.audit({"scenario": "personnel_incentive", "archive_id": "a001", "flags": flags,
                            "documents": [], "materials": []}, Path(temporary))
        prompt = render_prompt(call.call_args.args[3])
        self.assertIn(STAFF_PHOTO_CLARIFICATION, prompt)
        self.assertNotIn("不能只看整场日期齐全或照片总数足够就通过", prompt)


class WorkbookPhotoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.counter = 0

    def read(self, payload, name="返图.xlsx"):
        self.counter += 1
        path = self.root / name
        path.write_bytes(payload)
        output = self.root / f"units-{self.counter}"
        output.mkdir()
        return _spreadsheet_units({"path": path, "source_file": name, "file_id": "f001"}, output)

    def workbook(self, *, floating=False, cells=False, missing=False):
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "甲店"
        sheet.append(["活动周期", "客户名称", "陈列照片"])
        sheet.append(["10月1日", "甲店", '=_xlfn.DISPIMG("ID_A1",1)' if cells else "门店原文"])
        if cells:
            sheet.append(["10月2日", "甲店", '=DISPIMG("ID_A1",1)'])
        if floating:
            sheet.add_image(SheetImage(io.BytesIO(image_bytes())), "D2")
            sheet.add_image(SheetImage(io.BytesIO(image_bytes("blue"))), "D2")
            hidden = workbook.create_sheet("乙店")
            hidden.sheet_state = "hidden"
            hidden.add_image(SheetImage(io.BytesIO(image_bytes("red"))), "B7")
        stream = io.BytesIO()
        workbook.save(stream)
        workbook.close()
        if cells:
            main = "http://schemas.openxmlformats.org/drawingml/2006/main"
            rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
            with ZipFile(stream, "a") as archive:
                archive.writestr("xl/cellimages.xml", f'<cellImages><cellImage><cNvPr name="ID_A1"/><blip xmlns="{main}" r:embed="rId1" xmlns:r="{rel}"/></cellImage></cellImages>')
                archive.writestr("xl/_rels/cellimages.xml.rels", '<Relationships><Relationship Id="rId1" Target="media/wps.png"/></Relationships>')
                if not missing:
                    archive.writestr("xl/media/wps.png", image_bytes())
        return stream.getvalue()

    def test_multiple_floating_photos_same_cell_and_hidden_sheets_keep_all_placements(self):
        units = self.read(self.workbook(floating=True))
        photos = [unit for unit in units if unit.get("photo_reference")]
        self.assertEqual(len(photos), 3)
        self.assertTrue(all(unit["image"].is_file() for unit in photos))
        self.assertEqual(sum("甲店!R2C4" in unit["locator"] for unit in photos), 2)
        self.assertIn("乙店!R7C2", photos[2]["locator"])
        self.assertEqual(len({unit["unit_id"] for unit in photos}), 3)

    def test_wps_cells_in_xls_suffix_are_read_with_each_reused_placement_and_rename_invariance(self):
        original = self.workbook(cells=True)
        first = self.read(original, "任意名称.xls")
        second = self.read(original, "HX202610110012.xlsx")
        for units in (first, second):
            photos = [unit for unit in units if unit.get("photo_reference")]
            self.assertEqual(len(photos), 2)
            self.assertTrue(all(unit["image"] for unit in photos))
            self.assertIn("甲店!R2C3", photos[0]["locator"])
            self.assertIn("甲店!R3C3", photos[1]["locator"])
        self.assertEqual([unit["native_facts"] for unit in first], [unit["native_facts"] for unit in second])
        self.assertEqual((self.root / "任意名称.xls").read_bytes(), original)

    def test_missing_wps_media_is_an_explicit_photo_source_with_row_specific_limitations(self):
        units = self.read(self.workbook(cells=True, missing=True))
        photos = [unit for unit in units if unit.get("photo_reference")]
        self.assertEqual(len(photos), 2)
        for index, unit in enumerate(photos, 2):
            self.assertIsNone(unit["image"])
            self.assertIn(f"R{index}C3", unit["locator"])
            self.assertIn("图片未随表格提供", "".join(unit["limitations"]))

    def test_real_legacy_workbook_uses_existing_converter_and_detects_lost_pictures(self):
        source = self.root / "converted.xlsx"
        for payload, expected in ((self.workbook(floating=True), 3), (self.workbook(), 1)):
            with self.subTest(expected=expected):
                source.write_bytes(payload)
                with patch("audit_core.spreadsheet_photos._legacy_has_pictures", return_value=True), patch(
                        "audit_core.spreadsheet_photos._convert_legacy_workbook", side_effect=lambda _s, target: shutil.copyfile(source, target)):
                    if expected == 1:
                        with self.assertRaisesRegex(AuditError, "未完整保留图片"):
                            self.read(b"legacy-fixture", "原表.xls")
                    else:
                        units = self.read(b"legacy-fixture", "原表.xls")
                        photos = [unit for unit in units if unit.get("photo_reference")]
                        self.assertEqual(len(photos), expected)


class IndividualPhotoAnalysisTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def decide(self, case, reviews):
        evidence = {**unknown_checks(case["scenario"], case["flags"]), "photo_reviews": reviews}
        return audit_decision(case["scenario"], case["flags"], evidence, case["documents"], case["materials"], units=case["units"])

    def test_generation_schema_rejects_filenames_and_other_photo_citations(self):
        schema = photo_review_schema(["p01", "p02"], ["contract", "application", "p01", "p02"], ["contract"])
        value = {"photo_reviews": [review("p01"), review("p02")]}
        validate(value, schema)
        for source in ("照片收集.xlsx", "p01.png", "p02", "unread-source"):
            with self.subTest(source=source):
                invalid = deepcopy(value)
                invalid["photo_reviews"][0]["checks"][0]["source_ids"] = [source, "contract"]
                with self.assertRaises(AuditError):
                    validate(invalid, schema)
        invalid = deepcopy(value)
        invalid["photo_reviews"][0]["checks"][1]["source_ids"] = ["p01"]
        with self.assertRaises(AuditError):
            validate(invalid, schema)

    def test_every_photo_has_its_own_customer_row_including_passed_photos(self):
        case = photo_case(self.root)
        result = self.decide(case, [review("p01"), review("p02", status="unknown")])
        photo_check = next(check for check in result["checks"] if check["rule_id"] == "display_watermark")
        self.assertEqual(photo_check["status"], "unknown")
        packet = {**case, "source_archive": "HX202610110012.zip", "result": result}
        rows = build_view([packet], None)["sheets"][0]["rows"]
        photos = [row for row in rows if row.get("photo_unit_id")]
        self.assertEqual(len(photos), 2)
        self.assertEqual([row["status"] for row in photos], ["pass", "issue"])
        self.assertIn("甲店!R2C3", photos[0]["values"][0])
        self.assertIn("围挡宣传物料可见参半", photos[0]["values"][1])
        self.assertIn("水印日期被遮挡", photos[1]["error_reason"])
        self.assertIn("甲店!R3C3", photos[1]["error_reason"])
        self.assertEqual([r["unit_id"] for r in result["photo_reviews"]], ["p01", "p02"])
        summary = render_analysis_summary_markdown(build_view([packet], None), {"status": "completed"})
        self.assertNotIn("逐张陈列照片分析", summary)
        self.assertNotIn("甲店!R2C3", summary)
        self.assertIn("甲店!R3C3", summary)
        self.assertNotIn("围挡宣传物料可见参半", summary)
        self.assertIn("水印日期被遮挡", summary)

    def test_omitted_repeated_reordered_extra_or_borrowed_photos_are_rejected(self):
        case = photo_case(self.root)
        first, second = review("p01"), review("p02")
        for values in ([], [first], [first, first], [second, first], [first, second, review("contract")]):
            with self.subTest(values=values), self.assertRaises(AuditError):
                self.decide(case, values)
        wrong = deepcopy(second)
        wrong["checks"][1]["source_ids"] = ["p01"]
        with self.assertRaisesRegex(AuditError, "引用该照片"):
            self.decide(case, [first, wrong])
        incomplete = deepcopy(second)
        incomplete["checks"][1] = deepcopy(incomplete["checks"][0])
        with self.assertRaisesRegex(AuditError, "完整分析"):
            self.decide(case, [first, incomplete])

    def test_single_bad_photo_prevents_aggregate_pass_and_missing_image_cannot_be_guessed(self):
        case = photo_case(self.root)
        result = self.decide(case, [review("p01"), review("p02", status="fail", problem="该照片水印没有地址")])
        self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == "display_watermark")["status"], "fail")
        case["units"][2]["image"] = None
        with self.assertRaisesRegex(AuditError, "图片未能读取"):
            self.decide(case, [review("p01"), review("p02")])

    def test_fourteen_photos_are_analyzed_in_batches_without_sampling(self):
        case = photo_case(self.root, 14)
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        sizes = []
        def fake_call(root, skill, schema, prompt, *, images, label, validator, **kwargs):
            sizes.append(len(images))
            rendered = render_prompt(prompt)
            self.assertIn("即使符合也写清照片可见的实际情况", rendered)
            self.assertIn("甲店!R", rendered)
            value = {"photo_reviews": [review(image.stem) for image in images]}
            validator(value)
            return value
        with patch.object(provider, "_call", side_effect=fake_call):
            values = provider._audit_display_photos(case, self.root, [], [])
        self.assertEqual(sizes, [6, 6, 2])
        self.assertEqual([r["unit_id"] for r in values], [unit["unit_id"] for unit in case["units"] if unit.get("photo_reference")])
        self.decide(case, values)

    def test_current_qince_policy_and_independent_facts_reach_every_photo_batch_and_final_audit(self):
        case = photo_case(self.root, 7)
        raw = {
            "queryVerificationRegular": [{"verify_code": "HX_SYNTHETIC", "details": [
                {"id": str(index), "activity_code": "SQ_SYNTHETIC", "activity_cm_id": str(index)}
                for index in range(1, 9)]}],
            "queryRegularSaleActivities": [{"applyCode": "SQ_SYNTHETIC", "planCode": "FA_ONLY_REFERENCE",
                "subList": [{"subId": str(index), "cusId": "C" + str(index), "cusName": "合成申请客户" + str(index),
                    "startDate": "2026-10-01", "endDate": "2026-10-31", "pdRangeType": "1",
                    "extColumns": [{"ext_key": "陈列、堆头类型", "ext_value": "1平堆"}]}
                    for index in range(1, 9)]}],
            "queryRegularSale": [],
        }
        units, documents = context_sources(raw, "HX_SYNTHETIC", "a001")
        self.assertEqual([unit["source_file"] for unit in units], ["勤策核销明细"] * 8 + ["勤策申请明细"] * 8)
        case["units"].extend(units)
        case["documents"].extend(documents)
        original = deepcopy(case)
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        calls = []
        expected_reviews = []

        def fake_call(root, skill, schema, prompt, *, images, label, validator, **kwargs):
            rendered = render_prompt(prompt)
            calls.append({"label": label, "image_count": len(images)})
            self.assertIn(QINCE_CONTEXT_POLICY, rendered)
            self.assertIn("合成申请客户1", rendered)
            self.assertIn("a001-qince-0016", rendered)
            self.assertIn("contract", rendered)
            self.assertIn('"source_file": "勤策核销明细"', rendered)
            self.assertIn('"source_file": "勤策申请明细"', rendered)
            self.assertNotIn('"source_file": "勤策活动方案"', rendered)
            self.assertIn("cusId/cusName是该申请行的客户标识/名称", rendered)
            self.assertIn("planCode只是引用的方案编号", rendered)
            self.assertIn("申请行数、客户数或照片数不能推出完整终端名单及门店总数", rendered)
            self.assertIn("引用客户名称时说明是本单关联申请的客户名称", rendered)
            self.assertIn("来源名称解释不预设pass/fail/unknown结论", rendered)
            self.assertIn("只逐字段说明本单实际取得什么", rendered)
            self.assertIn("不评判附件是否随合同图、是否提供或是否整体取得", rendered)
            self.assertIn("不把申请行改称独立附件或终端名单", rendered)
            self.assertIn("本单关联申请明细的规格原文为“1平堆”，不代表照片实际面积。", rendered)
            self.assertIn("关联申请规格为1平堆", rendered)
            self.assertIn("本图实际面积能否确认", rendered)
            self.assertIn("不要求街道门牌完整作为新增条件", rendered)
            self.assertIn("不能仅因水印只有店名就判地址缺失", rendered)
            self.assertIn("不因缺少完整地址继续报身份或位置无法确认", rendered)
            self.assertIn("已有关联规格时，该例子不能用于报规格缺失", rendered)
            source_policy_at = rendered.index("陈列核对的来源名称与字段含义须准确")
            if label.startswith("陈列照片逐张分析"):
                self.assertGreater(source_policy_at, rendered.index("只按下方四项已有规则"))
                value = {"photo_reviews": [review(image.stem) for image in images]}
                for reviewed in value["photo_reviews"]:
                    for check in reviewed["checks"]:
                        if check['rule_id'] == 'display_watermark':
                            check['source_ids'].append('a001-qince-0009')
                        if check["rule_id"] in {"display_brand", "display_size"}:
                            check.update(status="unknown", reason="本图包装遮挡，品牌不能确认" if check["rule_id"] == "display_brand"
                                         else "本图没有可比尺度，实际面积不能确认")
                expected_reviews.extend(deepcopy(value["photo_reviews"]))
                self.assertIn("本次合同来源unit_id", rendered)
                self.assertIn("本批逐张照片来源", rendered)
            else:
                self.assertTrue(label.endswith("模型自主审核"))
                self.assertGreater(source_policy_at, rendered.index("本类业务说明"))
                self.assertIn("p07", rendered)
                self.assertEqual(images, [])
                value = unknown_checks(case["scenario"], case["flags"])
                originals=[unit['image'] for unit in case['units'] if unit.get('image')]
                value['photo_reviews']=[review(image.stem,status='unknown') for image in originals]
                for row in value['photo_reviews']:
                    for check in row['checks']:
                        if check['rule_id'] in {'display_brand','display_size'}:check.update(status='unknown',reason='本图遮挡，具体范围不能确认')
                expected_reviews.extend(deepcopy(value['photo_reviews']))
                from audit_core.audit_rag import needed_fields
                for check in value['checks']:
                    if needed_fields([check['rule_id']]):
                        check['source_ids'] = list(dict.fromkeys(check['source_ids'] +
                            [u['unit_id'] for u in units]))
            validator(value)
            return value

        with patch.object(provider, "_call", side_effect=fake_call), patch(
                "audit_core.pdf_materials.prepare_product_references", return_value=({
                    "reference_only": True, "rule_ids": [], "references": [],
                    "resolutions": [], "limitations": []}, [])), patch(
                "httpx.AsyncClient", side_effect=AssertionError("No external model or catalogue calls")):
            result = provider.audit(case, self.root)
        self.assertEqual([call["image_count"] for call in calls], [0])
        self.assertEqual(len(calls), 1)
        self.assertEqual([reviewed["unit_id"] for reviewed in result["photo_reviews"]],
                         [f"p{index:02d}" for index in range(1, 8)])
        self.assertEqual(result["photo_reviews"], expected_reviews)
        self.assertTrue(all(check["status"] == "unknown" for reviewed in result["photo_reviews"]
                            for check in reviewed["checks"] if check["rule_id"] in {"display_brand", "display_size"}))
        self.assertEqual(case, original)

    def test_display_source_clarification_preserves_original_attachment_text_and_specific_unknowns(self):
        case = photo_case(self.root, 1)
        case["documents"][0]["facts"] = ["合同约定1平米堆头，具体门店清单见附件"]
        original = deepcopy(case)
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        specific_unknowns = {
            "display_watermark": "本图水印定位被遮挡，现有资料不能唯一对应具体门店",
            "display_brand": "本图右侧包装被遮挡，不能确认其品牌",
            "display_material_brand": "本图宣传物料文字模糊，不能确认品牌",
            "display_size": "本图未见有真实尺寸依据的参照，无法确认实际面积",
        }
        def fake_call(root, skill, schema, prompt, *, images, label, validator):
            rendered = render_prompt(prompt)
            self.assertIn("合同约定1平米堆头，具体门店清单见附件", rendered)
            self.assertIn("只说明该具体字段仍不能确认", rendered)
            self.assertIn("有该规格不代表本图面积达标", rendered)
            self.assertIn("不预设pass/fail/unknown结论", rendered)
            value = {"photo_reviews": [review("p01")]}
            for check in value["photo_reviews"][0]["checks"]:
                check.update(status="unknown", reason=specific_unknowns[check["rule_id"]])
            validator(value)
            return value
        with patch.object(provider, "_call", side_effect=fake_call):
            result = provider._audit_display_photos(case, self.root, [], [])
        self.assertEqual({check["rule_id"]: check["reason"] for check in result[0]["checks"]}, specific_unknowns)
        self.assertTrue(all(check["status"] == "unknown" for check in result[0]["checks"]))
        self.assertEqual(case, original)

    def test_display_without_qince_does_not_invent_an_independent_source_or_requirement(self):
        case = photo_case(self.root, 1)
        original = deepcopy(case)
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        def fake_call(root, skill, schema, prompt, *, images, label, validator):
            rendered = render_prompt(prompt)
            self.assertIn("没有勤策来源时只使用本次实际材料，不新增必交要求", rendered)
            self.assertIn("本次实际勤策来源名称与定位（各项内容仍以对应documents原文为准）：[]", rendered)
            value = {"photo_reviews": [review("p01", status="unknown", problem="本图水印地址不能与已有合同参照对应")]}
            validator(value)
            return value
        with patch.object(provider, "_call", side_effect=fake_call):
            result = provider._audit_display_photos(case, self.root, [], [])
        self.assertEqual(result[0]["checks"][0]["status"], "unknown")
        self.assertEqual(case, original)

    def test_other_scenarios_do_not_receive_display_source_instructions(self):
        class Captured(Exception): pass
        for scenario in ("entry_fee", "personnel_incentive", "pos_target_incentive", "giveaway_promotion",
                         "poster_material", "price_difference_support", "self_procured_gift_material"):
            with self.subTest(scenario=scenario):
                case = {"archive_id": "a001", "scenario": scenario, "flags": flags_for(scenario, uses_pos=False),
                        "documents": [], "materials": [], "units": []}
                original = deepcopy(case)
                provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
                seen = []
                def fake_call(root, skill, schema, prompt, **kwargs):
                    seen.append(render_prompt(prompt))
                    raise Captured()
                with patch.object(provider, "_call", side_effect=fake_call), patch(
                        "audit_core.pdf_materials.prepare_product_references", return_value=({}, [])), patch(
                        "audit_core.pdf_materials.prepare_pos_identities", return_value={}), patch(
                        "audit_core.pdf_materials.template_comparison", return_value=([], [])), patch(
                        "httpx.AsyncClient", side_effect=AssertionError("No external model or catalogue calls")):
                    with self.assertRaises(Captured): provider.audit(case, self.root)
                self.assertEqual(len(seen), 1)
                self.assertIn(QINCE_CONTEXT_POLICY, seen[0])
                self.assertNotIn("陈列核对的来源名称与字段含义须准确", seen[0])
                self.assertNotIn("本次实际勤策来源名称与定位", seen[0])
                self.assertEqual(case, original)


if __name__ == "__main__":
    unittest.main()
