"""Problem-only summary projections; every fixture and callback is isolated."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from audit_core.callback_delivery import deliver_completed_callback
from audit_core.oss_intake import OSSIntakeConfig, build_callback_result
from audit_core.pdf_evidence import audit_decision
from audit_core.pdf_workflow import build_view
from audit_core.workbench_store import (
    ANALYSIS_SUMMARY_FILENAME, WorkbenchRunStore, render_analysis_summary_markdown,
)
from tests.pdf_test_support import unknown_checks
from tests.test_optional_staff_and_display_photos import photo_case, review


def summary_packet(reviews, checks=None):
    """Synthetic cells, never the customer's actual HX audit result."""
    units = [{"unit_id": "contract", "source_file": "合同.pdf", "locator": "第1页"}]
    for index, item in enumerate(reviews):
        units.append({"unit_id": item["unit_id"], "source_file": "附件/照片收集.xlsx",
                      "locator": f"甲店!R{index + 6}C2（单元格图片）"})
    units.append({"unit_id":"application","source_file":"系统申请.json","locator":"申请"})
    return {"archive_id": "synthetic-problem-summary", "source_archive": "隔离摘要样例.zip",
            "units": units, "documents": [{"unit_id": unit["unit_id"], "facts": ["原始完整证据保留"]}
                                          for unit in units],
            "result": {"scenario": "promotional_display", "checks": checks or [],
                       "photo_reviews": reviews, "summary": {"conclusion": "issues_found"}}}


def screenshot_like_fixture():
    """The screenshot's three problem shapes, using synthetic store/file names."""
    first = review("p01", status="unknown", problem=(
        "水印日期2026-08-27，日期在执行周期内；合同门店清单未提供，无法确认甲店地址是否对应合同门店。"))
    second = review("p02")
    second["checks"][3].update(status="unknown", reason=(
        "甲店照片只展示牙膏盒，未展示完整陈列边界，无法确认合同约定的2米×2米地堆尺寸。"))
    third = review("p03")
    third["checks"][3].update(status="unknown", reason=(
        "甲店照片仅为牙膏货架近景，未显示地面陈列尺寸，无法确认2米×2米申请规格。"))
    return summary_packet([first, second, third, review("p04")])


class ProblemOnlySummaryTests(unittest.TestCase):
    def render(self, packet):
        view = build_view([packet], None)
        return view, render_analysis_summary_markdown(view, {"status": "completed"})

    def test_mixed_photo_keeps_fail_unknown_and_not_pass_or_not_applicable(self):
        mixed = review("p01", status="unknown", problem="甲店2026-08-27水印地址被遮挡，无法确认门店地址。")
        mixed["checks"][1].update(status="fail", reason="甲店申请区域内混放其他品牌牙膏3盒。")
        mixed["checks"][2].update(status="pass", reason="未见其他品牌物料。")
        mixed["checks"][3].update(status="not_applicable", reason="本项不适用的完整说明。")
        packet = summary_packet([mixed, review("p02")])
        original = deepcopy(packet)
        view, summary = self.render(packet)
        self.assertIn("无法确认门店地址", summary)
        self.assertIn("混放其他品牌牙膏3盒", summary)
        self.assertIn("甲店!R6C2", summary)
        self.assertNotIn("甲店!R7C2", summary)
        self.assertNotIn("未见其他品牌物料", summary)
        self.assertNotIn("本项不适用", summary)
        self.assertNotIn("逐张陈列照片分析", summary)
        self.assertEqual(summary.count("\n1. "), 1)
        self.assertEqual(summary.count("\n2. "), 1)
        self.assertNotIn("\n3. ", summary)
        self.assertIn("1. 照片收集.xlsx · 甲店!R6C2（单元格图片）：甲店2026-08-27水印地址被遮挡", summary)
        self.assertIn("2. 照片收集.xlsx · 甲店!R6C2（单元格图片）：甲店申请区域内混放其他品牌牙膏3盒", summary)
        rows = view["sheets"][0]["rows"]
        self.assertEqual([item["status"] for item in rows[0]["summary_items"]], ["unknown", "fail"])
        self.assertEqual(rows[1]["summary_items"], [])
        self.assertIn("未见其他品牌物料", rows[0]["values"][1])
        self.assertIn("本项不适用", rows[0]["values"][1])
        self.assertIn("无法确认门店地址", rows[0]["error_reason"])
        self.assertIn("混放其他品牌牙膏3盒", rows[0]["error_reason"])
        self.assertNotIn("未见其他品牌物料", rows[0]["error_reason"])
        self.assertNotIn("本项不适用", rows[0]["error_reason"])
        self.assertEqual(packet, original)
        self.assertEqual(rows[0]["summary_items"][0]["source_ids"], ["p01", "contract", "application"])
        self.assertEqual(rows[0]["card_evidence"]["sources"][0]["facts"], ["原始完整证据保留"])

    def test_qince_matched_store_is_not_a_photo_error_or_file_to_resubmit(self):
        photo = review("p01")
        photo["checks"][0].update(reason="合同门店清单见附件；勤策明细已对应千子言甄选，日期在执行期内。",
                                   source_ids=["p01", "contract", "qince-store"])
        photo["checks"][3].update(status="unknown", reason="合同及勤策约定1平米，照片没有尺寸参照，无法确认堆头面积。",
                                   source_ids=["p01", "contract", "qince-store"])
        packet = summary_packet([photo])
        packet["units"][1].update(source_file="陈列图片 (1).png", locator="陈列图片 (1).png")
        packet["units"].append({"unit_id": "qince-store", "source_file": "勤策关联门店明细",
                                "locator": "本次申请行", "source_kind": "qince"})
        packet["documents"].append({"unit_id": "qince-store", "roles": ["business_context"],
                                    "facts": ["门店千子言甄选，陈列规格1平堆"], "limitations": []})
        before = deepcopy(packet)
        view, summary = self.render(packet)
        row = view["sheets"][0]["rows"][0]
        self.assertIn("无法确认堆头面积", row["error_reason"])
        self.assertNotIn("合同门店清单见附件", row["error_reason"])
        self.assertNotIn("勤策明细已对应", row["error_reason"])
        self.assertEqual(row["business_source_files"], ["陈列图片 (1).png"])
        self.assertIn("勤策明细已对应", row["values"][1])
        self.assertIn("勤策关联门店明细", row["card_evidence"]["source_files"])
        self.assertIn("无法确认堆头面积", summary)
        self.assertNotIn("合同门店清单见附件", summary)
        self.assertEqual(packet, before)

    def test_same_problem_on_different_cells_is_grouped_without_losing_photo_records(self):
        packet = summary_packet([review("p01", status="unknown"), review("p02", status="unknown")])
        original = deepcopy(packet)
        view, summary = self.render(packet)
        self.assertIn("1. 照片收集.xlsx · 甲店!R6C2", summary)
        self.assertIn("照片收集.xlsx · 甲店!R7C2", summary)
        self.assertNotIn("\n2. ", summary)
        self.assertEqual(summary.count("水印日期被遮挡"), 1)
        self.assertEqual(len(view["sheets"][0]["rows"]), 2)
        self.assertEqual(packet, original)

    def test_standalone_photo_name_is_shown_once_without_changing_original_names(self):
        for source_file, locator, filename in [
            ("1.jpg", "1.jpg", "1.jpg"),
            ("附件/10.jpg", "附件/10.jpg", "10.jpg"),
            ("附件/2.jpg", "2.jpg", "2.jpg"),
            (r"附件\3.jpg", "附件/3.jpg", "3.jpg"),
            ("附件/4.jpg.jpg", "附件/4.jpg.jpg", "4.jpg.jpg"),
        ]:
            with self.subTest(source_file=source_file, locator=locator):
                packet = summary_packet([review("p01", status="unknown")])
                packet["units"][1].update(source_file=source_file, locator=locator)
                original = deepcopy(packet)
                view, summary = self.render(packet)
                row = view["sheets"][0]["rows"][0]
                self.assertEqual(row["values"][0], filename)
                self.assertTrue(row["error_reason"].startswith(filename + "："))
                self.assertIn("1. " + filename + "：", summary)
                self.assertNotIn(filename + " · ", summary)
                self.assertEqual(packet, original)
                self.assertEqual(row["card_evidence"]["sources"][0]["locator"], locator)

    def test_pdf_page_location_and_same_named_distinct_photos_are_preserved(self):
        packet = summary_packet([review("p01", status="unknown"), review("p02", status="unknown"),
                                 review("p03", status="unknown")])
        for unit, source_file, locator in zip(packet["units"][1:],
                ["附件/陈列照片.pdf", "甲店/1.jpg", "乙店/1.jpg"], ["第2页", "甲店/1.jpg", "乙店/1.jpg"]):
            unit.update(source_file=source_file, locator=locator)
        view, summary = self.render(packet)
        self.assertIn("1. 陈列照片.pdf · 第2页", summary)
        self.assertIn("甲店/1.jpg", summary)
        self.assertIn("乙店/1.jpg", summary)
        self.assertNotIn("\n2. ", summary)
        self.assertEqual(summary.count("水印日期被遮挡"), 1)
        self.assertEqual([row["photo_unit_id"] for row in view["sheets"][0]["rows"]], ["p01", "p02", "p03"])

    def test_all_pass_photos_return_only_no_error_even_if_their_reason_contains_negative_words(self):
        photos = [review("p01"), review("p02")]
        photos[0]["checks"][1]["reason"] = "未见其他品牌，未发现跨门店复用，符合本次要求。"
        _, summary = self.render(summary_packet(photos))
        self.assertEqual(summary, "未发现错误。")

    def test_all_unknown_keeps_objects_and_uncertainty_without_claiming_noncompliance(self):
        photo = review("p01")
        for item, reason in zip(photo["checks"], [
            "甲店2026-08-27水印地址被遮挡", "牙膏商品标识看不清", "围挡物料文字看不清", "地堆边界未显示",
        ]):
            item.update(status="unknown", reason=reason)
        _, summary = self.render(summary_packet([photo]))
        for item in photo["checks"]:
            self.assertIn(item["reason"], summary)
        self.assertIn("暂时不能确认陈列水印", summary)
        self.assertIn("暂时不能确认申请陈列大小", summary)
        for value in ["不合格", "明确违规", "拒付", "批准", "建议", "共4"]:
            self.assertNotIn(value, summary)

    def test_actual_audit_decision_does_not_relabel_brand_failure_as_missing_material(self):
        with tempfile.TemporaryDirectory() as temporary:
            case = photo_case(Path(temporary))
            photos = [review("p01"), review("p02")]
            photos[0]["checks"][1].update(status="fail", reason="甲店申请区域内混放其他品牌牙膏3盒。")
            result = audit_decision(case["scenario"], case["flags"],
                                    {**unknown_checks(case["scenario"], case["flags"]), "photo_reviews": photos},
                                    case["documents"], case["materials"], units=case["units"])
            self.assertEqual(next(item for item in result["checks"] if item["rule_id"] == "display_brand")["effect"], "missing_material")
            packet = {**case, "source_archive": "隔离摘要样例.zip", "result": result}
            view, summary = self.render(packet)
            self.assertIn("混放其他品牌牙膏3盒", summary)
            self.assertNotIn("陈列产品品牌所需资料不足", summary)
            item = view["sheets"][0]["rows"][0]["summary_items"][0]
            self.assertEqual(item["status"], "fail")
            self.assertEqual(item["effect"], "")

    def test_missing_material_fail_stays_a_missing_document_problem(self):
        check = {"rule_id": "atrium_agreement", "status": "fail", "effect": "missing_material",
                 "reason": "未提供甲店2026-08-27商场入场协议，无法确认活动场地约定。",
                 "source_ids": [], "requirement": "本次申请的原有资料要求"}
        _, summary = self.render(summary_packet([], [check]))
        self.assertIn(check["reason"], summary)
        for value in ["不合格", "明确违规", "拒付", "费用不通过"]:
            self.assertNotIn(value, summary)

    def test_summary_removes_only_safe_compliance_clauses_and_keeps_dates_skus_sizes_and_money(self):
        packet = screenshot_like_fixture()
        before = deepcopy(packet)
        view, summary = self.render(packet)
        self.assertNotIn("日期在执行周期内", summary)
        self.assertIn("水印日期2026-08-27", summary)
        self.assertIn("合同门店清单未提供", summary)
        self.assertIn("牙膏盒", summary)
        self.assertIn("2米×2米", summary)
        self.assertIn("甲店!R6C2", summary)
        self.assertIn("甲店!R7C2", summary)
        self.assertIn("甲店!R8C2", summary)
        self.assertNotIn("甲店!R9C2", summary)
        self.assertEqual(summary.count("\n1. "), 1)
        self.assertEqual(summary.count("\n2. "), 1)
        self.assertEqual(summary.count("\n3. "), 1)
        self.assertIn("日期在执行周期内", view["sheets"][0]["rows"][0]["values"][1])
        self.assertEqual(packet, before)
        numeric = {"rule_id": "display_quantity_price", "status": "fail", "source_ids": ["contract"],
                   "effect": "missing_material", "requirement": "保持原数量和单价比较", "reason":
                   "甲店2026-08-27牙膏A，合同单价1.50元，申报单价1.51元，相差0.01元；陈列2米×2米。"}
        _, numeric_summary = self.render(summary_packet([], [numeric]))
        self.assertIn(numeric["reason"], numeric_summary)

    def test_legacy_photo_rows_use_saved_issue_reasons_but_never_full_findings_or_passed_rows(self):
        view = {"sheets": [{"name": "陈列堆头", "projection_kind": "pdf_policy", "rows": [
            {"photo_unit_id": "old-issue", "status": "issue", "error_reason": "水印地址被遮挡，无法确认甲店。",
             "values": ["照片收集.xlsx · 甲店!R7C2", "水印日期正确；品牌和尺寸全部符合要求。", "原规则", "需要处理"]},
            {"photo_unit_id": "old-pass", "status": "pass", "error_reason": "不应展示的旧残留",
             "values": ["照片收集.xlsx · 甲店!R8C2", "未见其他品牌，符合要求。", "原规则", "已核对，符合要求"]},
        ]}]}
        original = deepcopy(view)
        summary = render_analysis_summary_markdown(view, {"status": "completed", "error_count": 1})
        self.assertIn("甲店!R7C2", summary)
        self.assertIn("无法确认甲店", summary)
        self.assertNotIn("甲店!R8C2", summary)
        self.assertNotIn("全部符合要求", summary)
        self.assertNotIn("不应展示的旧残留", summary)
        self.assertEqual(view, original)

    def test_price_photo_summary_removes_only_other_photos_complete_pass_clause(self):
        problem = "《合成3.jpg》的价签上批发价格栏被水印遮挡，活动价格看不清，暂时不能确认本图活动价格"
        check = {"rule_id": "price_evidence", "status": "fail", "effect": "missing_material",
                 "source_ids": ["contract"], "requirement": "活动价格清晰可读",
                 "reason": problem + "；其余照片水印含拍摄时间和门店地址、价格可读。"}
        packet = summary_packet([], [check])
        original = deepcopy(packet)
        view, summary = self.render(packet)
        self.assertIn(problem, summary)
        self.assertNotIn("其余照片", summary)
        self.assertIn("其余照片", view["sheets"][0]["rows"][0]["values"][1])
        self.assertEqual(packet, original)

    def test_price_photo_summary_preserves_other_problems_and_specific_price_facts(self):
        problem = "《合成3.jpg》活动价被水印遮挡，暂时不能确认价格"
        retained = [
            "其他照片的水印拍摄时间、门店地址和价格部分仍看不清",
            "《合成4.jpg》门店地址被遮挡，无法确认地址",
            "其余照片水印含拍摄时间和门店地址、价格可读但商品无法对应",
            "其余照片2025-12-12的SKU6970356164289活动价26.90元可读",
            "合同价格26.90元，照片价格29.90元，相差3.00元",
            "其余照片水印含拍摄时间和门店地址、价格可读（仅限一张）",
        ]
        for tail in retained:
            with self.subTest(tail=tail):
                check = {"rule_id": "price_evidence", "status": "unknown", "effect": "",
                         "source_ids": ["contract"], "requirement": "活动价格清晰可读",
                         "reason": problem + "；" + tail + "。"}
                _, summary = self.render(summary_packet([], [check]))
                self.assertIn(problem, summary)
                self.assertIn(tail, summary)

    def test_material_gate_keeps_specific_problems_without_counts_suggestions_or_pass_materials(self):
        view = {"sheets": [{"name": "陈列堆头", "scenario": "promotional_display",
                            "projection_kind": "classification_rejection", "rows": [
            {"status": "issue", "error_reason": "未提供促销合同门店附件。", "values": []},
            {"status": "issue", "error_reason": "无法确认甲店是否属于商场大型活动。", "values": []},
            {"status": "pass", "error_reason": "结算单已提供。", "values": []},
        ]}]}
        summary = render_analysis_summary_markdown(view, {"status": "completed"})
        self.assertEqual(summary, "## 陈列堆头\n\n1. 未提供促销合同门店附件。\n2. 无法确认甲店是否属于商场大型活动。")

    def test_saved_summary_callback_and_fake_delivery_are_identical_without_rewriting_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workspace = root / "isolated-problem-summary"
            store = WorkbenchRunStore(workspace, run_id="20261102-summary-fixture", producer_model="codex",
                                      root_html=Path("offline-activity-audit.html"), workbench_url="http://127.0.0.1:1/")
            view, expected = self.render(screenshot_like_fixture())
            store.complete(view_payload=view, scenarios=["promotional_display"], verification={"fixture": True})
            path = workspace / ANALYSIS_SUMMARY_FILENAME
            self.assertEqual(path.read_text(encoding="utf-8"), expected)
            manifest = json.loads((workspace / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["analysis_summary"]["sha256"], hashlib.sha256(path.read_bytes()).hexdigest())
            result = {"status": "completed", "worktree": str(workspace), "analysis_summary": str(path),
                      "snapshot": str(workspace / "snapshot.json"), "scenarios": ["promotional_display"]}
            before = {file.relative_to(workspace).as_posix(): file.read_bytes() for file in workspace.rglob("*") if file.is_file()}
            # A later renderer change must not replace an already sealed summary.
            with patch("audit_core.oss_intake.render_analysis_summary_markdown", side_effect=AssertionError("saved text is authoritative")):
                self.assertEqual(build_callback_result(result), expected)
            sender = Mock(return_value={"status": "delivered", "http_status": 204, "attempts": 1, "delivered_at": "2026-11-02T00:00:00Z"})
            config = OSSIntakeConfig(allowed_hosts=("unused.invalid",), callback_url="https://callback.invalid/api/v1/ai/analyze/callback")
            with patch("socket.create_connection", side_effect=AssertionError("network is forbidden in this fixture")):
                delivered = deliver_completed_callback(root, workspace_id=workspace.name, verify_code="SYNTHETIC_SUMMARY", analyze_id=1,
                                                        config=config, apply=True, sender=sender)
            self.assertEqual(delivered["status"], "delivered")
            sender.assert_called_once()
            self.assertEqual(sender.call_args.args[0], {"verifyCode": "SYNTHETIC_SUMMARY", "analyzeId": 1, "result": expected})
            self.assertEqual({file.relative_to(workspace).as_posix(): file.read_bytes() for file in workspace.rglob("*") if file.is_file()}, before)


if __name__ == "__main__":
    unittest.main()
