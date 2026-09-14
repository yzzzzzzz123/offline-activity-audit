from __future__ import annotations

import unittest
from copy import deepcopy

from audit_core.pass_check_log import attach_pass_check_log, build_pass_check_log


def _view(*scenarios: str) -> dict:
    return {
        "schema_version": "1.1",
        "title": "线下活动核销结果",
        "sheets": [
            {
                "name": scenario,
                "scenario": scenario,
                "rows": [],
                "audit_counts": {"error_count": 1},
            }
            for scenario in scenarios
        ],
    }


class PassCheckLogTests(unittest.TestCase):
    def test_display_keeps_passed_subchecks_when_store_overall_needs_review(self) -> None:
        view = _view("promotional_display")
        result = {
            "scenario": "promotional_display",
            "contract": {
                "source_file": "D:/temporary/促销合同.pdf",
                "activity_start": "2026-04-01",
                "activity_end": "2026-04-30",
            },
            "sales": {"source_file": "D:/temporary/销售明细.xlsx"},
            "summary": {
                "sales_internal_status": "pass",
                "sales_line_amount_status": "pass",
                "sales_printed_quantity_status": "exact",
                "sales_printed_amount_status": "exact",
            },
            "contract_core_reconciliation": {
                "source_file": "D:/temporary/促销合同.pdf",
                "field_checks": [
                    {
                        "label": "执行期间",
                        "contract_value": "2026-04-01 至 2026-04-30",
                        "status": "pass",
                        "basis": "销售业务日期全部位于合同执行期间内。",
                    },
                    {
                        "label": "印章",
                        "contract_value": "不可见",
                        "status": "fail",
                        "basis": "印章不可见。",
                    },
                ],
            },
            "contract_attachment_sales_reconciliation": {
                "record_count": 2,
                "matched_count": 2,
                "unmatched_sales_rows": [9],
            },
            "store_reconciliation": [
                {
                    "contract_store_name": "挺拇指生活超市（横沥店）",
                    "photo_files": [
                        "D:/temporary/10-1挺拇指生活超市(横沥店).jpg",
                        "10-2挺拇指生活超市(横沥店).jpg",
                    ],
                    "photo_count": 2,
                    "visible_date": "2026-04-30",
                    "visible_location": "东莞市·南铭购物乐园",
                    "period_match": "match",
                    "store_match": "compatible",
                    "location_resolution": {
                        "confidence": "high",
                        "basis": "百度地图 MCP确认两处直线距离37.4米，属于商场与店内门店关系。",
                    },
                    "display_match": "pass",
                    "display_description": "单张照片可分辨4个独立包装列，达到4纵陈列。",
                    "duplicate_check": "none",
                    "photo_knowledge_match": "exact",
                    "photo_knowledge_match_basis": "现场商品文字与包装特征均已确认。",
                    "photo_contract_product_status": "unmatched",
                    "amount_rule_status": "pass",
                    "amount_rule_basis": "合同明确按店核销：1店×1000元。",
                    "status": "supplement",
                }
            ],
        }

        log = build_pass_check_log(view, {"promotional_display": result})
        self.assertEqual(log["audit_type_count"], 1)
        self.assertEqual(log["types_with_pass"], 1)
        group = log["groups"][0]
        self.assertEqual(group["audit_type"], "堆头/陈列")
        titles = [item["title"] for item in group["items"]]
        self.assertIn("门店地点核验通过", titles)
        self.assertIn("现场日期核验通过", titles)
        self.assertIn("现场陈列核验通过", titles)
        self.assertIn("现场商品识别通过", titles)
        self.assertIn("单店金额规则核验通过", titles)
        self.assertNotIn("现场商品合同范围核验通过", titles)
        self.assertNotIn("印章核验通过", titles)

        location = next(item for item in group["items"] if item["title"] == "门店地点核验通过")
        self.assertEqual(location["confidence"], "high")
        self.assertIn("37.4米", location["basis"])
        self.assertEqual(
            location["source_files"],
            [
                "10-1挺拇指生活超市(横沥店).jpg",
                "10-2挺拇指生活超市(横沥店).jpg",
                "促销合同.pdf",
            ],
        )
        paired = next(item for item in group["items"] if item["title"] == "已配对明细逐行核验通过")
        self.assertIn("本项只记录已配对行", paired["basis"])
        self.assertIn("仍在错误总览中处理", paired["basis"])

    def test_generic_controls_are_grouped_by_audit_type_and_failures_are_excluded(self) -> None:
        view = _view("maintenance_fee", "entry_fee")
        results = {
            "maintenance_fee": {
                "scenario": "maintenance_fee",
                "case_name": "维护费用.zip",
                "maintenance_fee_audit": {
                    "controls": [
                        {
                            "control_id": "pos_visual_seal",
                            "status": "pass",
                            "basis": "全部POS可视资料盖章清楚",
                            "confidence_score": 0.96,
                        },
                        {"control_id": "amount_recalculation", "status": "fail", "basis": "金额无法复算"},
                    ],
                    "documents": [
                        {"source_file": "D:/temporary/盖章POS.jpg", "role": "stamped_pos_data"}
                    ],
                },
            },
            "entry_fee": {
                "scenario": "entry_fee",
                "case_name": "进场费.zip",
                "entry_fee_audit": {
                    "controls": [
                        {"control_id": "contract_authority", "status": "pass", "basis": "合同双方签章且门店清单完整"},
                        {"control_id": "system_deduction_proof", "status": "fail", "basis": "缺少扣款凭证"},
                    ],
                    "contract": {"source_file": "D:/temporary/进场合同.pdf"},
                },
            },
        }

        log = build_pass_check_log(view, results)
        self.assertEqual([group["audit_type"] for group in log["groups"]], ["维护费用", "进场费"])
        self.assertEqual([group["item_count"] for group in log["groups"]], [1, 1])
        titles = [item["title"] for group in log["groups"] for item in group["items"]]
        self.assertEqual(titles, ["盖章POS核验通过", "合同权威性核验通过"])
        self.assertEqual(log["groups"][0]["items"][0]["confidence_score"], 0.96)
        self.assertNotIn("申报金额复算通过", titles)
        self.assertNotIn("系统扣款凭证核验通过", titles)

    def test_attach_is_read_only_and_legacy_pass_rows_have_a_safe_fallback(self) -> None:
        view = _view("personnel_incentive")
        view["sheets"][0]["rows"] = [
            {
                "excel_row": 4,
                "status": "pass",
                "section": "detail",
                "confidence": "medium",
                "heading": "示例商品",
                "values": ["销售数量一致", "奖励金额一致"],
            }
        ]
        before = deepcopy(view)
        enriched = attach_pass_check_log(view, {})
        self.assertEqual(view, before)
        self.assertIsNotNone(enriched)
        item = enriched["pass_check_log"]["groups"][0]["items"][0]
        self.assertEqual(item["subject"], "示例商品")
        self.assertEqual(item["basis"], "奖励金额一致")
        self.assertEqual(item["confidence"], "medium")


if __name__ == "__main__":
    unittest.main()
