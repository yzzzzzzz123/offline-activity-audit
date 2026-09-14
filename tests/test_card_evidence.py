from __future__ import annotations

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from audit_core.card_evidence import EvidenceContext, enrich_pass_item
from audit_core.pass_check_log import attach_pass_check_log, build_pass_check_log, load_workspace_results


def view(scenario):
    return {"sheets": [{"scenario": scenario, "rows": []}]}


class CardEvidenceTests(unittest.TestCase):
    def test_equal_execution_period_retains_both_files_values_and_exact_row_scope(self):
        scenario = "promotional_display"
        period = "2026-04-01 至 2026-04-30"
        result = {"scenario": scenario, "contract": {"source_file": "合同.pdf"},
                  "sales": {"source_file": "销售.xlsx", "sheet": "Sheet1", "records": [
                      {"excel_row": row, "period_text": period} for row in range(2, 38)]},
                  "contract_core_reconciliation": {"field_checks": [
                      {"field": "execution_period", "label": "执行周期", "contract_value": period,
                       "comparison_value": period, "status": "pass", "basis": "全部位于合同期内。"}]}}
        before = deepcopy(result)
        item = build_pass_check_log(view(scenario), {scenario: result})["groups"][0]["items"][0]
        self.assertEqual(result, before)
        self.assertEqual(item["source_files"], ["合同.pdf", "销售.xlsx"])
        self.assertEqual(item["source_file_count"], 2)
        self.assertEqual(item["subject"].count(period), 2)
        self.assertIn("不是逐笔实际交易日期核验", item["basis"])
        evidence = item["evidence"]
        self.assertIn("第 2–37 行", evidence["sources"][1]["locator"])
        self.assertIn("36 行", evidence["sources"][1]["facts"][0])
        self.assertIn("未保留页码", evidence["sources"][0]["locator"])
        result["sales"]["records"].append({"excel_row": 39, "period_text": ""})
        item = build_pass_check_log(view(scenario), {scenario: result})["groups"][0]["items"][0]
        self.assertIn("另有 1 行业务日期为空", item["evidence"]["limitations"][0])

    def test_explicit_roles_no_filename_guess_no_eight_file_cap(self):
        scenario = "giveaway_promotion"
        docs = [{"source_file": f"小票{i}.jpg", "role": "visual_document", "document_type": "store_receipt"} for i in range(12)]
        docs += [{"source_file": "合同.jpg", "role": "signed_promotional_contract"},
                 {"source_file": "文件名叫POS但其实是结算.jpg", "role": "settlement"}]
        result = {"scenario": scenario, f"{scenario}_audit": {"documents": docs, "controls": [
            {"control_id": "receipt_execution", "status": "pass"},
            {"control_id": "pos_visual_seal", "status": "pass"}]}}
        items = build_pass_check_log(view(scenario), {scenario: result})["groups"][0]["items"]
        self.assertEqual(items[0]["source_file_count"], 13)
        self.assertEqual(len(items[0]["evidence"]["sources"]), 13)
        self.assertNotIn(docs[-1]["source_file"], items[0]["source_files"])
        self.assertEqual(items[1]["source_files"], [])
        self.assertFalse(items[1]["evidence"]["file_count_complete"])

    def test_amount_rule_has_five_sources_not_unrelated_pos_or_photos(self):
        scenario = "self_procured_gift_material"
        roles = [("合同.jpg", "signed_promotional_contract"), ("结算.jpg", "settlement"),
                 ("票据.jpg", "purchase_invoice_or_receipt"), ("定金.jpg", "purchase_payment_record"),
                 ("尾款.jpg", "purchase_payment_record"), ("POS.jpg", "stamped_pos_data"), ("返图.png", "activity_photo")]
        result = {f"{scenario}_audit": {"documents": [{"source_file": name, "role": role} for name, role in roles],
                                      "controls": [{"control_id": "amount_recalculation", "status": "pass"}]}}
        item = build_pass_check_log(view(scenario), {scenario: result})["groups"][0]["items"][0]
        self.assertEqual(item["source_files"], [name for name, _ in roles[:5]])

    def test_transfer_includes_expected_reward_sources_and_identity_limit(self):
        result = {"sales": {"source_file": "销售.xlsx", "sheet": "明细", "records": [{"excel_row": 2, "store_name": "甲店"}]},
                  "settlement": {"source_file": "结算.jpg"},
                  "store_transfer_reconciliation": [{"store_name": "甲店", "expected_reward_amount": 30, "transfer_amount": 30,
                      "transfer_id": "t1", "excel_quantity": 10, "status": "amount_match_identity_unverified"}],
                  "transfer_evidence": [{"transfer_id": "t1", "amount": 30, "source_files": ["红包1.jpg", "红包2.jpg"]}]}
        item = build_pass_check_log(view("personnel_incentive"), {"personnel_incentive": result})["groups"][0]["items"][0]
        self.assertEqual(item["source_files"], ["销售.xlsx", "结算.jpg", "红包1.jpg", "红包2.jpg"])
        self.assertIn("不是收款身份匹配", "".join(item["evidence"]["limitations"]))

    def test_derived_images_count_workbook_once_and_preserve_all_image_names(self):
        result = {"_presentation_case": {"activity_return": {"source_file": "返图.xls", "sheet": "返图", "records": [
            {"photo_file": "图1.png", "excel_row": 2}, {"photo_file": "图2.png", "excel_row": 3}]}}}
        evidence = EvidenceContext("self_procured_gift_material", result).make(["合同.jpg", "图1.png", "图2.png", "返图.xls"])
        self.assertEqual(evidence["source_file_count"], 2)
        self.assertEqual(evidence["derived_file_count"], 2)
        self.assertEqual(len(evidence["sources"]), 4)

    def test_saved_companion_recovers_omitted_contract_without_writing_any_input(self):
        scenario = "entry_fee"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = {"scenario": scenario, "entry_fee_audit": {"controls": [{"control_id": "contract_authority", "status": "pass"}],
                       "issues": [{"title": "缺少扣款", "code": "system_deduction_proof_missing", "source_files": [], "observed": "未提交", "expected": "按合同提交"}]}}
            values = {"results/entry_fee.json": result, "evidence/entry_fee.json": {"documents": [
                {"source_file": "协议.pdf", "role": "entry_fee_contract", "party_a": "甲", "party_b": "乙"}]},
                "input-cases.json": {scenario: {"scenario": scenario}}}
            for name, value in values.items():
                path = root / "analysis" / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
            hashes = {path: path.read_bytes() for path in root.rglob("*.json")}
            original_view = view(scenario)
            original_view["sheets"][0]["rows"] = [{"heading": "问题：缺少扣款", "status": "issue"}]
            projection = attach_pass_check_log(original_view, load_workspace_results(root))
            self.assertEqual(projection["pass_check_log"]["groups"][0]["items"][0]["source_files"], ["协议.pdf"])
            self.assertEqual(projection["sheets"][0]["rows"][0]["card_evidence"]["source_files"], ["协议.pdf"])
            self.assertNotIn("card_evidence", original_view["sheets"][0]["rows"][0])
            self.assertEqual(hashes, {path: path.read_bytes() for path in root.rglob("*.json")})

    def test_missing_contract_period_does_not_claim_photo_is_within_it(self):
        scenario = "poster_material"
        result = {f"{scenario}_audit": {"contract": {}, "field_photos": [{"source_file": "照片.jpg", "date_visibility": "visible",
                  "time_visibility": "visible", "location_visibility": "visible", "watermark_date": "2026-04-01", "watermark_time": "10:00", "watermark_location": "门店"}]}}
        item = build_pass_check_log(view(scenario), {scenario: result})["groups"][0]["items"][0]
        self.assertNotIn("日期位于合同执行期内", item["basis"])
        self.assertIn("未完成期间核对", item["basis"])


if __name__ == "__main__":
    unittest.main()
