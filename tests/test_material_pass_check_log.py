from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from audit_core.pass_check_log import (
    attach_pass_check_log,
    build_pass_check_log,
    load_workspace_material_packets,
)


def fixture(archive_id="a001", scenario="promotional_display"):
    materials = [
        {"id": "promotion_contract", "state": "present", "source_ids": [archive_id + "-f001"], "reason": "资料清单中已找到合同"},
        {"id": "settlement", "state": "missing", "source_ids": [], "reason": "缺少结算单"},
        {"id": "pos", "state": "unclear", "source_ids": [archive_id + "-f001"], "reason": "内容不清楚"},
        {"id": "atrium_agreement", "state": "not_applicable", "source_ids": [], "reason": "本次不适用"},
    ]
    packet = {
        "archive_id": archive_id, "source_archive": archive_id + ".zip",
        "route": {"scenario": scenario, "matched": False},
        "classification": {
            "candidate_scenarios": [scenario], "materials": materials,
            "material_matches": [{"scenario": scenario, "materials": deepcopy(materials), "supported": False}],
        },
        "units": [{"unit_id": archive_id + "-f001", "source_file": "材料/合同.pdf", "locator": "第 2 页"}],
        "documents": [{"unit_id": archive_id + "-f001", "facts": ["页面含合同文字"], "limitations": []}],
    }
    sheet = {
        "scenario": scenario, "archive_id": archive_id, "source_archive": archive_id + ".zip",
        "projection_kind": "classification_rejection", "confirmed_scenario": True,
        "decision_source": "biz_type", "audit_type_label": "陈列堆头",
        "rows": [{"status": "issue", "error_reason": "缺少结算单", "values": ["缺少结算单"]}],
        "audit_counts": {"error_count": 1},
    }
    return {"sheets": [sheet]}, packet


def sync_materials(packet):
    packet["classification"]["material_matches"][0]["materials"] = deepcopy(packet["classification"]["materials"])


class MaterialPassCheckLogTests(unittest.TestCase):
    def test_presence_has_distinct_stage_label_counts_and_bound_sources_without_mutation(self):
        view, packet = fixture()
        before = deepcopy((view, packet))
        projected = attach_pass_check_log(view, {}, material_packets=[packet])
        log = projected["pass_check_log"]
        self.assertEqual((log["total"], log["material_total"], log["business_total"]), (1, 1, 0))
        self.assertEqual(log["notice"], "本次仅完成资料检查，业务内容尚未审核。")
        group = log["groups"][0]
        self.assertEqual((group["material_total"], group["business_total"]), (1, 0))
        item = group["items"][0]
        self.assertEqual(item["category"], "资料完整性")
        self.assertEqual(item["status_label"], "已提供")
        self.assertEqual(item["check_stage"], "material_gate")
        self.assertTrue(item["title"].endswith("已提供"))
        self.assertNotIn("核验通过", item["title"])
        self.assertIn("业务内容尚未审核", item["basis"])
        self.assertEqual(item["source_files"], ["合同.pdf"])
        self.assertEqual(item["evidence"]["sources"][0]["locator"], "第 2 页")
        self.assertEqual(item["evidence"]["sources"][0]["facts"], [], "资料盘点无需复制整表提取正文")
        self.assertEqual(projected["sheets"], view["sheets"], "原问题、来源及错误计数不可改变")
        self.assertEqual((view, packet), before)

    def test_only_present_material_with_unambiguous_complete_source_chain_is_shown(self):
        mutations = {
            "missing": lambda p: p["classification"]["materials"][0].update(state="missing"),
            "unclear": lambda p: p["classification"]["materials"][0].update(state="unclear"),
            "not_applicable": lambda p: p["classification"]["materials"][0].update(state="not_applicable"),
            "no_source": lambda p: p["classification"]["materials"][0].update(source_ids=[]),
            "unknown_source": lambda p: p["classification"]["materials"][0].update(source_ids=["other-f001"]),
            "source_not_list": lambda p: p["classification"]["materials"][0].update(source_ids="a001-f001"),
            "duplicate_source": lambda p: p["classification"]["materials"][0].update(source_ids=["a001-f001"] * 2),
            "missing_unit": lambda p: p.update(units=[]),
            "missing_document": lambda p: p.update(documents=[]),
            "duplicate_unit": lambda p: p["units"].append(deepcopy(p["units"][0])),
            "duplicate_document": lambda p: p["documents"].append(deepcopy(p["documents"][0])),
            "invalid_facts": lambda p: p["documents"][0].update(facts="不是事实数组"),
            "missing_source_file": lambda p: p["units"][0].pop("source_file"),
            "unknown_material": lambda p: p["classification"]["materials"][0].update(id="invented_material"),
            "other_type_material": lambda p: p["classification"]["materials"][0].update(id="invoice"),
            "duplicate_material": lambda p: p["classification"]["materials"].append(deepcopy(p["classification"]["materials"][0])),
        }
        for name, mutate in mutations.items():
            with self.subTest(name=name):
                view, packet = fixture()
                mutate(packet)
                sync_materials(packet)
                log = build_pass_check_log(view, {}, material_packets=[packet])
                self.assertEqual(log["total"], 0)
                self.assertEqual(log["business_total"], 0)

    def test_archive_and_confirmed_scenario_must_match_exactly_without_filename_guessing(self):
        cases = {
            "archive_missing": lambda v, p: v["sheets"][0].pop("archive_id"),
            "archive_other": lambda v, p: p.update(archive_id="other"),
            "scenario_not_confirmed": lambda v, p: v["sheets"][0].update(confirmed_scenario=False),
            "other_route": lambda v, p: p["route"].update(scenario="personnel_incentive"),
            "already_audited_route": lambda v, p: p["route"].update(matched=True),
            "other_material_match": lambda v, p: p["classification"]["material_matches"][0].update(scenario="personnel_incentive"),
            "contradictory_materials": lambda v, p: p["classification"]["material_matches"][0]["materials"][0].update(state="missing"),
            "duplicate_material_match": lambda v, p: p["classification"]["material_matches"].append(deepcopy(p["classification"]["material_matches"][0])),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                view, packet = fixture()
                mutate(view, packet)
                self.assertEqual(build_pass_check_log(view, {}, material_packets=[packet])["total"], 0)
        view, packet = fixture()
        self.assertEqual(build_pass_check_log(view, {}, material_packets=[packet, deepcopy(packet)])["total"], 0)
        self.assertEqual(build_pass_check_log(view, {})["total"], 0)
        view, packet = fixture(scenario="other_expense")
        self.assertEqual(build_pass_check_log(view, {}, material_packets=[packet])["total"], 0)

    def test_same_type_multiple_packets_are_retained_and_duplicate_sheet_does_not_duplicate_materials(self):
        first, packet_a = fixture("a001")
        second, packet_b = fixture("a002")
        view = {"sheets": first["sheets"] + second["sheets"] + deepcopy(first["sheets"])}
        log = build_pass_check_log(view, {}, material_packets=[packet_a, packet_b])
        self.assertEqual((log["total"], log["material_total"], log["audit_type_count"]), (2, 2, 1))
        self.assertEqual({i["archive_id"] for i in log["groups"][0]["items"]}, {"a001", "a002"})
        self.assertEqual(len({i["check_id"] for i in log["groups"][0]["items"]}), 2)

    def test_mixed_same_type_packets_keep_business_passes_in_either_sheet_order(self):
        view, packet = fixture()
        business_sheet = {
            "scenario": "promotional_display", "archive_id": "a002", "source_archive": "a002.zip",
            "projection_kind": "pdf_policy", "audit_type_label": "陈列堆头", "title": "陈列堆头核销",
            "rows": [{"status": "pass", "check_category": "促销合同签章", "rule_id": "contract_signed",
                      "values": ["合同.pdf", "已明确核对签章"], "card_evidence": {"source_files": ["合同.pdf"]}}],
        }
        for sheets in [[*view["sheets"], business_sheet], [business_sheet, *view["sheets"]]]:
            with self.subTest(first=sheets[0]["projection_kind"]):
                log = build_pass_check_log({"sheets": sheets}, {}, material_packets=[packet])
                self.assertEqual((log["total"], log["material_total"], log["business_total"]), (2, 1, 1))
                self.assertNotIn("本次仅完成", log["notice"])
                self.assertIn("不代表", log["notice"])
                business = next(i for i in log["groups"][0]["items"] if i["check_stage"] == "business")
                self.assertEqual(business["status_label"], "通过")
                self.assertEqual(business["basis"], "已明确核对签章")

    def test_presence_is_never_added_to_business_or_unconfirmed_diagnostic_sheets(self):
        for kind in ["pdf_policy", "material_diagnostic"]:
            view, packet = fixture()
            view["sheets"][0].update(projection_kind=kind, title="测试", audit_type_label="陈列堆头")
            log = build_pass_check_log(view, {}, material_packets=[packet])
            self.assertEqual(log["material_total"], 0)

    def test_loader_is_read_only_and_tolerates_missing_invalid_or_wrong_shape_evidence(self):
        with tempfile.TemporaryDirectory(prefix="material-pass-log-") as directory:
            root = Path(directory)
            self.assertEqual(load_workspace_material_packets(root), [])
            path = root / "analysis/pdf-policy/evidence.json"
            path.parent.mkdir(parents=True)
            _, packet = fixture()
            path.write_text(json.dumps({"packets": [packet]}, ensure_ascii=False), encoding="utf-8")
            original, timestamp = path.read_bytes(), path.stat().st_mtime_ns
            loaded = load_workspace_material_packets(root)
            self.assertEqual(loaded, [packet])
            loaded[0]["route"]["matched"] = True
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(path.stat().st_mtime_ns, timestamp)
            self.assertEqual(load_workspace_material_packets(root), [packet])
            for raw in ["invalid JSON", "[]", "null", '{"packets":{}}', '{"packets":[null,1,"x"]}']:
                path.write_text(raw, encoding="utf-8")
                self.assertEqual(load_workspace_material_packets(root), [])


if __name__ == "__main__":
    unittest.main()
