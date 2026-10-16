"""Material corrections list only the affected submitted files."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from audit_core.material_issue_files import attach_material_issue_files
from audit_core.error_reason import attach_error_reasons
from audit_core.pdf_evidence import classification_issues
from audit_core.pdf_workflow import build_view
from audit_core.workbench_server import WorkbenchCatalog
from tests.pdf_test_support import classification
from tests.test_error_only_frontend import Markup, NODE, render


SALES_FILE = "2026年4月销售明细20260509164207765.xlsx"


def material_packet(archive_id="a001", source_archive="本次资料.zip"):
    names = ["1.厚街汉邦66广场家家欣.jpg", "2.松岗百佳华.jpg", SALES_FILE,
             "3.公明百佳华.jpg", "郑州雨瑶5.1-5.20人员激励活动结算单.jpg",
             "雨瑶人员激励参半销售数据2.xlsx", "雨瑶人员激励红包图片.jpg", "雨瑶人员激励红包图片2.jpg"]
    units = [{"unit_id": f"{archive_id}-{index}", "source_file": name, "locator": "原始资料"}
             for index, name in enumerate(names)]
    value = classification("personnel_incentive", [u["unit_id"] for u in units], missing=("pos", "promotion_contract"))
    match = next(m for m in value["material_matches"] if m["scenario"] == "personnel_incentive")
    next(m for m in match["materials"] if m["id"] == "pos_excel")["source_ids"] = [units[2]["unit_id"]]
    value.update(material_matches=[match], materials=deepcopy(match["materials"]))
    return {"archive_id": archive_id, "source_archive": source_archive, "classification": value,
            "units": units, "documents": [{"unit_id": u["unit_id"], "facts": ["本次已保存的材料事实"]} for u in units],
            "route": {"scenario": "personnel_incentive", "matched": False,
                      "reasons": [i["reason"] for i in classification_issues(value, "personnel_incentive")]}}


def material_view(packets):
    rejection = {"decision_source": "biz_type", "archives": [
        {"archive_id": p["archive_id"], "source_archive": p["source_archive"], "scenario": "personnel_incentive"}
        for p in packets]}
    return build_view(packets, rejection)


class MaterialIssueFileTests(unittest.TestCase):
    def test_missing_stamp_lists_only_its_excel_and_missing_contract_lists_nothing(self):
        packet = material_packet()
        original = deepcopy(packet)
        rows = material_view([packet])["sheets"][0]["rows"]
        self.assertEqual([r["error_reason"] for r in rows], ["缺少盖章版销售明细。", "缺少促销合同。"])
        self.assertEqual([r["business_source_files"] for r in rows], [[SALES_FILE], []])
        self.assertEqual(len(rows[0]["card_evidence"]["source_files"]), 8)
        self.assertEqual(packet, original)

    def test_both_pos_versions_missing_do_not_invent_a_file(self):
        packet = material_packet()
        match = packet["classification"]["material_matches"][0]
        next(m for m in match["materials"] if m["id"] == "pos_excel").update(state="missing", source_ids=[])
        packet["route"]["reasons"] = [i["reason"] for i in classification_issues(packet["classification"], "personnel_incentive")]
        rows = material_view([packet])["sheets"][0]["rows"]
        self.assertTrue(all(r["business_source_files"] == [] for r in rows))

    def test_unclear_material_and_extra_material_use_their_own_sources(self):
        packet = material_packet()
        match = packet["classification"]["material_matches"][0]
        next(m for m in match["materials"] if m["id"] == "pos").update(
            state="unclear", reason="照片上的印章看不清。", source_ids=[packet["units"][0]["unit_id"]])
        match["extra_materials"] = [{"description": "其他活动销售数据", "reason": "不属于本次活动",
                                    "source_ids": [packet["units"][5]["unit_id"]]}]
        packet["route"]["reasons"] = [i["reason"] for i in classification_issues(packet["classification"], "personnel_incentive")]
        rows = material_view([packet])["sheets"][0]["rows"]
        by_reason = {r["error_reason"]: r["business_source_files"] for r in rows}
        self.assertEqual(by_reason["照片上的印章看不清。"], [packet["units"][0]["source_file"]])
        extra = next(row for row in rows if row["error_reason"].startswith("多交了"))
        self.assertEqual(extra["business_source_files"], [packet["units"][5]["source_file"]])

    def test_historical_projection_is_immutable_idempotent_and_separates_archives(self):
        packets = [material_packet(), material_packet("a002", "另一个资料包.zip")]
        packets[1]["units"][2]["source_file"] = "另一份销售表.xlsx"
        view = material_view(packets)
        for sheet in view["sheets"]:
            del sheet["archive_id"]  # Older snapshots only stored the archive name.
            for row in sheet["rows"]:
                del row["business_source_files"]
        original = deepcopy(view)
        projected = attach_material_issue_files(view, packets)
        self.assertEqual(projected["sheets"][0]["rows"][0]["business_source_files"], [SALES_FILE])
        self.assertEqual(projected["sheets"][1]["rows"][0]["business_source_files"], ["另一份销售表.xlsx"])
        self.assertEqual(view, original)
        self.assertEqual(attach_material_issue_files(projected, packets), projected)

    def test_distinct_same_name_sources_are_not_merged(self):
        packet = material_packet()
        packet["units"][5]["source_file"] = "第二目录/" + SALES_FILE
        packet["units"][2]["source_file"] = "第一目录/" + SALES_FILE
        match = packet["classification"]["material_matches"][0]
        next(m for m in match["materials"] if m["id"] == "pos_excel")["source_ids"].append(packet["units"][5]["unit_id"])
        files = material_view([packet])["sheets"][0]["rows"][0]["business_source_files"]
        self.assertEqual(files, ["第一目录/" + SALES_FILE, "第二目录/" + SALES_FILE])

    def test_old_nested_pos_wrapper_still_matches_saved_material_sources(self):
        packet = material_packet()
        match = packet["classification"]["material_matches"][0]
        next(m for m in match["materials"] if m["id"] == "pos").update(
            state="unclear", reason="照片上的印章看不清。", source_ids=[packet["units"][0]["unit_id"]])
        packet["route"]["reasons"] = [i["reason"] for i in classification_issues(packet["classification"], "personnel_incentive")]
        view = material_view([packet])
        row = view["sheets"][0]["rows"][0]
        row.update(error_reason="POS）（照片上的印章看不清。", error_reasons=["POS）（照片上的印章看不清。"])
        del row["business_source_files"]
        projected = attach_material_issue_files(attach_error_reasons(view), [packet])
        self.assertEqual(projected["sheets"][0]["rows"][0]["business_source_files"], [packet["units"][0]["source_file"]])

    def test_saved_evidence_changes_refresh_live_projection_without_rewriting_snapshot(self):
        packet = material_packet()
        view = material_view([packet])
        for row in view["sheets"][0]["rows"]:
            del row["business_source_files"]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "20261015-material-files"
            evidence = workspace / "analysis/pdf-policy/evidence.json"
            evidence.parent.mkdir(parents=True)
            (workspace / "manifest.json").write_text(json.dumps({"status": "completed", "workspace_id": workspace.name}), encoding="utf-8")
            snapshot = workspace / "snapshot.json"
            snapshot.write_text(json.dumps({"view": view}), encoding="utf-8")
            before = snapshot.read_bytes()
            evidence.write_text(json.dumps({"packets": [packet]}), encoding="utf-8")
            catalog = WorkbenchCatalog(root)
            first_signature = catalog.html_signature(workspace.name)
            first, first_etag = catalog.snapshot_document(workspace.name)
            self.assertEqual(json.loads(first)["view"]["sheets"][0]["rows"][0]["business_source_files"], [SALES_FILE])
            packet["units"][2]["source_file"] = "修改后的销售表.xlsx"
            evidence.write_text(json.dumps({"packets": [packet]}), encoding="utf-8")
            second, second_etag = catalog.snapshot_document(workspace.name)
            self.assertNotEqual(first_signature, catalog.html_signature(workspace.name))
            self.assertNotEqual(first_etag, second_etag)
            self.assertEqual(json.loads(second)["view"]["sheets"][0]["rows"][0]["business_source_files"], ["修改后的销售表.xlsx"])
            self.assertEqual(before, snapshot.read_bytes())

    @unittest.skipUnless(NODE, "Node.js is required for frontend projection")
    def test_render_only_relevant_excel_and_hide_whole_archive_file_area(self):
        projected = render(material_view([material_packet()]))[0]
        cards = Markup(projected["html"]).root.find("eo-error-card")
        self.assertEqual([node.text() for node in cards[0].find("eo-evidence-source")], [SALES_FILE])
        self.assertEqual(cards[1].find("eo-evidence"), [])
        self.assertNotIn(".jpg", "".join(node.text() for node in cards))
