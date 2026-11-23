from __future__ import annotations

from copy import deepcopy
import json
import subprocess
import unittest

from tests.test_error_only_frontend import HARNESS, Markup, NODE, renderer_script


PASS_HARNESS = HARNESS.replace(
    "    projections,",
    "    projections, passHtml: buildPassGroupHtml() || passEmptyHtml(), materialPassTotal, businessPassTotal,",
)


def fixture(*, material: bool = True, business: bool = False, empty_groups: bool = False) -> dict:
    sheets = [{"scenario": "entry_fee", "projection_kind": "classification_rejection", "audit_type_label": "条码费", "rows": []}]
    items = []
    if material:
        items.append({"check_id": "material-contract", "check_stage": "material_gate", "status_label": "已提供", "category": "资料完整性", "title": "合同已提供", "subject": "合同", "basis": "已识别提交的合同文件。", "source_files": ["合同.pdf"]})
    if business:
        sheets.append({"scenario": "entry_fee", "projection_kind": "pdf_policy", "audit_type_label": "条码费", "rows": []})
        items.append({"check_id": "business-amount", "check_stage": "business", "status_label": "通过", "category": "金额核对", "title": "金额一致", "subject": "费用", "basis": "合同与结算金额一致。", "source_files": ["结算单.pdf"]})
    notice = "资料完整性项仅确认资料已提供，不代表对应资料包的业务内容审核通过。" if business else "本次仅完成资料检查，业务内容尚未审核。"
    groups = [{"scenario": "entry_fee", "audit_type": "条码费", "title": "条码费", "notice": notice, "items": items}] if items or empty_groups else []
    return {"sheets": sheets, "pass_check_log": {"total": len(items), "groups": groups, "notice": notice if items else ""}}


def render_pass(view: dict) -> dict:
    result = subprocess.run([NODE, "-e", PASS_HARNESS], input=json.dumps({"view": view, "script": renderer_script(), "fullPage": True}, ensure_ascii=False), encoding="utf-8", capture_output=True, check=True, timeout=15)
    return json.loads(result.stdout)


@unittest.skipUnless(NODE, "Node.js is required for the frontend projection test")
class PassCheckFrontendTests(unittest.TestCase):
    def test_material_only_is_provided_not_business_pass(self) -> None:
        rendered = render_pass(fixture())
        cards = Markup(rendered["passHtml"]).root.find("eo-pass-card")
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0].find("eo-pass-index")[0].text(), "已提供0001")
        self.assertIn("资料已提供 1 项 · 业务检查通过 0 项", rendered["html"])
        self.assertIn("本次仅完成资料检查，业务内容尚未审核。", rendered["passHtml"])
        self.assertEqual((rendered["materialPassTotal"], rendered["businessPassTotal"]), (1, 0))

    def test_mixed_stages_count_items_and_keep_business_label(self) -> None:
        view = fixture(business=True)
        view["pass_check_log"].update(total=999, material_total=999, business_total=999)
        rendered = render_pass(view)
        self.assertEqual((rendered["materialPassTotal"], rendered["businessPassTotal"]), (1, 1))
        self.assertIn("资料已提供 1 项 · 业务检查通过 1 项", rendered["html"])
        self.assertEqual([node.text() for node in Markup(rendered["passHtml"]).root.find("eo-pass-index")], ["已提供0001", "通过0002"])

    def test_rejection_with_empty_groups_has_visible_stage_empty_state(self) -> None:
        for groups in (False, True):
            with self.subTest(empty_group_present=groups):
                rendered = render_pass(fixture(material=False, empty_groups=groups))
                markup = Markup(rendered["passHtml"]).root
                self.assertEqual(markup.find("eo-pass-group"), [])
                self.assertEqual(len(markup.find("eo-pass-empty")), 1)
                self.assertIn("尚未进入业务审核", markup.text())
                self.assertIn("没有可确认已提供的资料项", markup.text())
                self.assertIn("资料已提供 0 项 · 业务检查通过 0 项", rendered["html"])

    def test_pdf_without_pass_has_business_empty_state(self) -> None:
        view = fixture(material=False, empty_groups=True)
        view["sheets"][0]["projection_kind"] = "pdf_policy"
        rendered = render_pass(view)
        self.assertIn("暂无明确通过的业务检查项", rendered["passHtml"])
        self.assertNotIn("尚未进入业务审核", rendered["passHtml"])

    def test_legacy_business_pass_defaults_to_business(self) -> None:
        view = {"sheets": [{"scenario": "entry_fee", "projection_kind": "pdf_policy", "name": "条码费", "rows": [{"status": "pass", "heading": "金额一致", "values": ["金额一致"], "section": "detail"}]}]}
        rendered = render_pass(view)
        self.assertEqual((rendered["materialPassTotal"], rendered["businessPassTotal"]), (0, 1))
        self.assertIn("通过0001", Markup(rendered["passHtml"]).root.text())

    def test_labels_are_stage_bound_and_all_other_text_escaped(self) -> None:
        view = fixture(business=True)
        group = view["pass_check_log"]["groups"][0]
        group["notice"] = '<img src=x onerror="alert(1)">'
        group["items"][0].update(status_label="通过", title="<script>bad()</script>", source_files=["<img>.pdf"])
        group["items"][1]["status_label"] = '<svg onload="alert(1)">'
        rendered = render_pass(view)
        self.assertEqual([node.text() for node in Markup(rendered["passHtml"]).root.find("eo-pass-index")], ["已提供0001", "通过0002"])
        self.assertNotIn("<img", rendered["passHtml"])
        self.assertNotIn("<script", rendered["passHtml"])
        self.assertNotIn("<svg", rendered["passHtml"])
        self.assertIn("&lt;img", rendered["passHtml"])

    def test_render_does_not_change_payload_or_error_projection(self) -> None:
        view = fixture(business=True)
        original = deepcopy(view)
        rendered = render_pass(view)
        self.assertEqual(view, original)
        self.assertEqual([part["count"] for part in rendered["projections"]], [0, 0])


if __name__ == "__main__":
    unittest.main()
