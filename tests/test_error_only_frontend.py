from __future__ import annotations

from copy import deepcopy
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "skills/orchestrate-offline-audit/assets/error-only.js"
NODE = shutil.which("node")
SCENARIOS = (
    "personnel_incentive", "promotional_display", "poster_material",
    "other_expense", "maintenance_fee", "giveaway_promotion",
    "price_difference_support", "pos_target_incentive", "entry_fee",
    "self_procured_gift_material",
)
HARNESS = r"""
const input = JSON.parse(require('fs').readFileSync(0, 'utf8'));
global.window = { location: { search: '' } };
global.document = {
  title: '', head: { appendChild() {} }, querySelector() { return null; },
  body: { className: '', innerHTML: '' },
  getElementById(id) { return id === 'audit-data' ? { textContent: JSON.stringify(input.view) } : { addEventListener() {} }; }
};
const end = input.script.indexOf(input.fullPage ? '      const renderFacetOptions = ' : '      const total = projections.reduce(');
if (end < 0) throw new Error('Missing projection boundary');
eval(input.script.slice(0, end) + `
  globalThis.rendered = {
    projections,
    html: document.body.innerHTML,
    evidence: (globalThis.fixtureEvidence || []).map(item => evidenceHtml(item.evidence, item.fallback || []))
  };
})();`);
process.stdout.write(JSON.stringify(globalThis.rendered));
"""


class Node:
    def __init__(self, tag: str = "", attrs: dict | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | str] = []

    def text(self) -> str:
        return "".join(child.text() if isinstance(child, Node) else child for child in self.children)

    def find(self, class_name: str) -> list[Node]:
        found = [self] if class_name in self.attrs.get("class", "").split() else []
        for child in self.children:
            if isinstance(child, Node):
                found.extend(child.find(class_name))
        return found


class Markup(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.root = Node()
        self.stack = [self.root]
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        node = Node(tag, dict(attrs))
        self.stack[-1].children.append(node)
        if tag not in {"br", "img", "input", "meta", "link", "hr"}:
            self.stack.append(node)

    def handle_endtag(self, tag: str) -> None:
        if len(self.stack) > 1:
            self.stack.pop()

    def handle_data(self, value: str) -> None:
        self.stack[-1].children.append(value)


def render(view: dict, *, script: str | None = None) -> list[dict]:
    completed = subprocess.run(
        [NODE, "-e", HARNESS],
        input=json.dumps({"view": view, "script": script or ASSET.read_text(encoding="utf-8")}, ensure_ascii=False),
        encoding="utf-8", capture_output=True, check=True, timeout=15,
    )
    return json.loads(completed.stdout)["projections"]


def row(scenario: str, **overrides) -> dict:
    value = {
        "heading": "门店甲" if scenario == "promotional_display" else "问题：金额不一致",
        "status": "issue", "section": "detail", "confidence": "medium",
        "values": [
            "文件：上传目录/合同.pdf", "识别结果：金额不一致。已通过身份核验。",
            "材料要求：需要完整材料。", "审核结论：待人工确认。", "核销影响：暂不能自动核销。",
            "主要问题：活动日期\n处理方式：补交清晰合同。\n要重新提交：补交清晰合同。",
        ],
        "error_reasons": ["合同.pdf：申请金额 100 元，转账金额 90 元，相差 10 元。"],
        "error_reason": "这个低优先级字段不应覆盖列表。",
        "card_evidence": {
            "source_files": ["上传目录/合同.pdf"],
            "sources": [{"file": "临时/crop.png", "original_file": "上传目录/合同.pdf", "kind": "derived", "role": "合同", "locator": "第 1 页", "facts": ["识别了完整标题。"]}],
            "references": [{"name": "知识库", "facts": ["参考内容"]}],
            "comparisons": ["金额比较详情"], "limitations": ["材料范围说明"], "derived_file_count": 1,
        },
    }
    value.update(overrides)
    return value


@unittest.skipUnless(NODE, "Node.js is required for the frontend projection test")
class ErrorOnlyFrontendTests(unittest.TestCase):
    def test_display_store_gap_requests_visible_identity_without_watermark_format(self) -> None:
        value = row("promotional_display", error_reasons=["门店甲：现场照片未显示可与合同门店对应的名称或地址。"])
        value["values"][0] = "门店：门店甲"
        value["values"][1] = "文件：现场.jpg\n识别地点：未识别"
        value["values"][5] = "主要问题：门店水印缺失或无法核对"
        card = Markup(render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]["html"]).root
        self.assertIn("门店无法确认", [node.text() for node in card.find("eo-chip")])
        action = card.find("action")[0].text()
        self.assertIn("补交能显示门店名称或地址", action)
        self.assertNotIn("水印", action)
        self.assertEqual([node.text() for node in card.find("eo-line")], value["error_reasons"])

    def test_type_diagnostic_does_not_become_amount_error_from_material_words(self) -> None:
        value = row("maintenance_fee", heading="问题：核销类型待确认", error_reasons=[
            "资料包.zip：文件名未注明核销方式。",
        ])
        value["values"][1] = "合同原价39.9元，活动价29.9元，人员激励15%，核销类型待确认。"
        sheet = {
            "scenario": None, "projection_kind": "material_diagnostic",
            "audit_type_label": "核销方式无法确认", "rows": [value],
            "diagnostic_issues": [{"code": "scenario_unconfirmed"}],
        }
        projected = render({"sheets": [sheet]})[0]
        chips = [node.text() for node in Markup(projected["html"]).root.find("eo-chip")]
        self.assertIn("费用类型", chips)
        self.assertNotIn("金额复算", chips)

    def test_classification_rejection_renders_reason_action_zip_and_zero_counts(self) -> None:
        from audit_core.material_diagnostic_output import (
            build_classification_rejection_result, build_classification_rejection_view,
        )
        from audit_core.pass_check_log import attach_pass_check_log

        result = build_classification_rejection_result([{
            "archive_id": "a001", "source_archive": "资料包.zip", "archive_sha256": "0" * 64,
            "scenario": None, "reason_code": "missing_scenario_marker", "matched_scenarios": [],
        }])
        view = attach_pass_check_log(build_classification_rejection_view(result), {})
        completed = subprocess.run(
            [NODE, "-e", HARNESS], input=json.dumps({
                "view": view, "script": ASSET.read_text(encoding="utf-8"), "fullPage": True,
            }, ensure_ascii=False), encoding="utf-8", capture_output=True, check=True, timeout=15,
        )
        rendered = json.loads(completed.stdout)
        self.assertNotIn('id="eoErrorFacetSummary"', rendered["html"])
        document = Markup(rendered["html"]).root
        cards = document.find("eo-error-card")
        self.assertEqual(len(cards), 1)
        self.assertIn("核销方式无法确认", cards[0].text())
        self.assertNotIn("其他费用", document.text())
        self.assertEqual([node.text() for node in cards[0].find("eo-line")], [result["archives"][0]["reason"]])
        self.assertEqual(cards[0].find("action")[0].text(), "处理方式" + result["archives"][0]["action"])
        self.assertEqual([node.text() for node in cards[0].find("eo-evidence-source")], ["资料包.zip"])
        self.assertEqual(document.find("eo-cockpit-hero"), [])
        self.assertEqual(document.find("eo-gauge-stat"), [])
        self.assertIn("0 种核销类型", document.find("eo-rail-foot")[0].text())
        self.assertIn("0 项通过", document.find("eo-rail-foot")[0].text())
        self.assertEqual(document.find("eo-pass-card"), [])

    def test_all_ten_scenarios_and_diagnostic_use_shared_reasons(self) -> None:
        sheets = [{"scenario": scenario, "rows": [row(scenario)], "headers": []} for scenario in SCENARIOS]
        sheets.append({"scenario": "maintenance_fee", "projection_kind": "material_diagnostic", "audit_type_label": "维护费用", "rows": [row("maintenance_fee")]})
        for projected in render({"sheets": sheets}):
            with self.subTest(scenario=projected["sheet"]["scenario"], diagnostic=projected["sheet"].get("projection_kind")):
                markup = Markup(projected["html"]).root
                self.assertEqual([node.text() for node in markup.find("eo-line")], ["合同.pdf：申请金额 100 元，转账金额 90 元，相差 10 元。"])
                self.assertEqual(len(markup.find("action")), 1)
                self.assertIn("补交", markup.find("action")[0].text())
                self.assertNotIn("置信度", markup.text())
                self.assertEqual(markup.find("eo-error-confidence"), [])
                self.assertNotIn("；置信度：", markup.find("eo-error-card")[0].attrs["aria-label"])
                files = markup.find("eo-evidence")
                self.assertEqual(len(files), 1)
                self.assertEqual(files[0].text(), "涉及的业务文件合同.pdf")
                for unwanted in ("材料要求", "审核结论", "核销影响", "已通过身份核验", "临时/crop", "第 1 页", "参考内容", "材料范围说明"):
                    self.assertNotIn(unwanted, markup.text())

    def test_string_field_and_explicit_empty_list_are_authoritative(self) -> None:
        first = row("maintenance_fee", error_reason="结算单.jpg：缺少签章。")
        del first["error_reasons"]
        second = row("maintenance_fee", error_reasons=[])
        result = render({"sheets": [{"scenario": "maintenance_fee", "rows": [first, second]}]})[0]
        self.assertEqual([node.text() for node in Markup(result["html"]).root.find("eo-line")], ["结算单.jpg：缺少签章。"])

    def test_display_aggregate_cards_take_reasons_from_their_saved_rows(self) -> None:
        value = row("promotional_display", heading="合同销售附件第1行｜PDF第2页")
        value["values"] = [
            "商品编码：A\n商品名称：示例商品", "", "销售Excel第2行",
            "知识库商品：未确认\n知识库商品编码：B\n条形码：精确匹配\n\n合同附件 → 销售Excel\n商品编码：不一致", "", "",
        ]
        value["error_reasons"] = ["合同.pdf 第 2 页：商品编码 A 与销售表 B 不一致。"]
        value["error_reasons_by_scope"] = {
            "knowledge": ["合同.pdf 第 2 页：合同商品编码 A 未在商品资料中登记。"],
            "sales": ["合同.pdf 第 2 页：商品编码 A 与销售表 B 不一致。"],
        }
        result = render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]
        cards = Markup(result["html"]).root.find("eo-error-card")
        self.assertEqual(len(cards), 2)
        for card, scope in zip(cards, ("knowledge", "sales")):
            reason_field = next(field for field in card.find("eo-field") if field.text().startswith("错误原因"))
            self.assertEqual([node.text() for node in reason_field.find("eo-line")], value["error_reasons_by_scope"][scope])
            self.assertNotIn("69码已命中", reason_field.text())
            self.assertNotIn("需要处理", reason_field.text())
        del value["error_reasons_by_scope"]
        legacy_cards = Markup(render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]["html"]).root.find("eo-error-card")
        self.assertIn("未在商品资料中登记", legacy_cards[0].text())
        self.assertNotIn("商品编码：不一致", legacy_cards[0].text())
        self.assertIn("商品编码：不一致", legacy_cards[1].text())
        self.assertNotIn("未在商品资料中登记", legacy_cards[1].text())

    def test_legacy_contract_reason_keeps_actual_business_code_and_locator(self) -> None:
        value = row("promotional_display", heading="合同销售附件第7行｜PDF第3页")
        del value["error_reason"], value["error_reasons"]
        value["values"] = [
            "商品编码：CP-REAL-01\n商品名称：合同牙膏", "", "",
            "知识库商品：未确认\n知识库商品编码：CP-KQ-INTERNAL\n条形码：精确匹配\n本项结果：商品存在条件未满足", "", "",
        ]
        markup = Markup(render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]["html"]).root
        reasons = [node.text() for node in markup.find("eo-line")]
        self.assertEqual(reasons, ["合同第3页第7行，商品“合同牙膏”：合同商品编码“CP-REAL-01”未在商品资料中登记"])
        for internal in ("CP-KQ-INTERNAL", "product_code_aliases", "知识库主编码"):
            self.assertNotIn(internal, markup.text())

    def test_incomplete_display_review_uses_system_operation_and_keeps_other_actions(self) -> None:
        cases = (
            ("现场照片复核未完成，尚未确认陈列列数和堆头面积。", "重新复核现场原图"),
            ("系统无法读取现场原图，陈列列数和堆头面积尚未核验。", "恢复现场原图读取后重新核验"),
        )
        for reason, operation in cases:
            with self.subTest(reason=reason):
                value = row("promotional_display", error_reasons=[reason])
                value["values"][5] = "主要问题：陈列标准、活动日期"
                markup = Markup(render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]["html"]).root
                self.assertEqual([node.text() for node in markup.find("eo-line")], [reason])
                action = markup.find("action")[0].text()
                self.assertIn(operation, action)
                self.assertIn("补交能看清完整拍摄日期的现场照片", action)
                self.assertNotIn("补一张完整堆头全景", action)

    def test_legacy_review_failure_does_not_blame_customer_photos(self) -> None:
        cases = (
            ("必做复核受文件访问限制阻断，未能原始分辨率重新打开照片，无法按指定流程确认实体列数或占地面积。", "系统无法读取现场原图", "恢复现场原图读取后重新核验"),
            ("本次无法完成要求的原始分辨率独立复核，未形成实体列数或占地面积的有效复核结论。", "现场照片复核未完成", "重新复核现场原图"),
        )
        for basis, reason, operation in cases:
            with self.subTest(basis=basis):
                value = row("promotional_display")
                del value["error_reason"], value["error_reasons"]
                value["values"][1] = "陈列标准核验：无法确认（照片不足以判断1平米堆头或4纵陈列）\n视觉依据：" + basis
                value["values"][5] = "主要问题：陈列标准"
                markup = Markup(render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]["html"]).root
                self.assertIn(reason, markup.text())
                self.assertEqual(markup.find("action")[0].text(), "处理方式" + operation)
                for blame in ("照片不足", "补一张", "补交", "补拍"):
                    self.assertNotIn(blame, markup.text())

    def test_saved_confirmed_product_removes_stale_action_but_low_candidate_remains(self) -> None:
        value = row("promotional_display", error_reasons=["系统无法读取现场原图，陈列列数和堆头面积尚未核验。"])
        value["values"][1] = "现场商品：已保存商品身份（置信度：中）\n知识库结论：置信度：中"
        value["values"][5] = "主要问题：销售Excel对合同附件、陈列标准、现场商品知识库、现场商品与合同"
        markup = Markup(render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]["html"]).root
        self.assertEqual(markup.find("action")[0].text(), "处理方式恢复现场原图读取后重新核验")
        self.assertNotIn("现场商品知识库", markup.text())
        value["values"][1] = "现场商品：一个候选置信度：中；另一个候选置信度：低\n知识库结论：置信度：低"
        value["error_reasons"].append("现场商品尚未确认。")
        markup = Markup(render({"sheets": [{"scenario": "promotional_display", "rows": [value]}]})[0]["html"]).root
        self.assertIn("提供能辨认商品文字和完整包装的现场照片", markup.find("action")[0].text())

    def test_legacy_diagnostic_only_retains_error_facts_and_separate_action(self) -> None:
        value = row("maintenance_fee")
        del value["error_reason"], value["error_reasons"]
        value["values"][1] = "识别结果：陈列销售.jpg：印章文字不清晰，无法完整确认盖章主体。审核结论：待人工确认；建议：补交清晰照片。"
        sheet = {"scenario": "maintenance_fee", "projection_kind": "material_diagnostic", "rows": [value]}
        markup = Markup(render({"sheets": [sheet]})[0]["html"]).root
        self.assertEqual([node.text() for node in markup.find("eo-line")], ["陈列销售.jpg：印章文字不清晰，无法完整确认盖章主体"])
        self.assertIn("补交清晰合同", markup.text())

    def test_original_files_only_preserve_distinct_same_name_sources(self) -> None:
        value = row("maintenance_fee", card_evidence={
            "source_files": ["甲/合同.pdf", "乙\\合同.pdf", "甲/合同.pdf", "crop-1.png", "crop-orphan.png"],
            "sources": [
                {"file": "crop-1.png", "original_file": "甲/合同.pdf", "kind": "derived"},
                {"file": "crop-orphan.png", "kind": "derived"},
            ],
        })
        markup = Markup(render({"sheets": [{"scenario": "maintenance_fee", "rows": [value]}]})[0]["html"]).root
        self.assertEqual([node.text() for node in markup.find("eo-evidence-source")], ["合同.pdf", "合同.pdf"])
        self.assertNotIn("crop", markup.text())

    def test_structured_unsupported_and_ocr_named_originals_are_not_hidden(self) -> None:
        value = row("maintenance_fee", card_evidence={
            "source_files": ["说明.docx", "ocr/盖章POS.jpg"],
            "sources": [
                {"file": "说明.docx", "original_file": "说明.docx", "kind": "submitted"},
                {"file": "ocr/盖章POS.jpg", "original_file": "ocr/盖章POS.jpg", "kind": "submitted"},
            ],
        })
        markup = Markup(render({"sheets": [{"scenario": "maintenance_fee", "rows": [value]}]})[0]["html"]).root
        self.assertEqual([node.text() for node in markup.find("eo-evidence-source")], ["说明.docx", "盖章POS.jpg"])

    def test_reference_and_generated_sources_are_excluded_by_provenance(self) -> None:
        value = row("maintenance_fee", card_evidence={
            "source_files": ["参考图.jpg", "生成图.jpg", "crop.png", "无原件crop.png"],
            "sources": [
                {"file": "参考图.jpg", "original_file": "知识库/参考图.jpg", "kind": "reference"},
                {"file": "生成图.jpg", "kind": "generated"},
                {"file": "crop.png", "original_file": "提交/活动核销.xls", "kind": "derived"},
                {"file": "无原件crop.png", "kind": "derived"},
            ],
        })
        markup = Markup(render({"sheets": [{"scenario": "maintenance_fee", "rows": [value]}]})[0]["html"]).root
        self.assertEqual([node.text() for node in markup.find("eo-evidence-source")], ["活动核销.xls"])

    def test_known_empty_source_list_hides_file_area_without_invented_fallback(self) -> None:
        value = row("maintenance_fee", card_evidence={"source_files": [], "sources": [], "limitations": ["长范围说明"]})
        markup = Markup(render({"sheets": [{"scenario": "maintenance_fee", "rows": [value]}]})[0]["html"]).root
        self.assertEqual(markup.find("eo-evidence"), [])
        self.assertNotIn("长范围说明", markup.text())

    def test_actions_keep_only_recorded_operations_without_generic_fallback(self) -> None:
        explicit = row("maintenance_fee")
        explicit["values"][5] = "处理方式：补交盖章清晰的结算单。；错误原因：印章不清晰。核销影响：不能自动核销。"
        missing = row("maintenance_fee")
        missing["values"][5] = "审核结论：待人工确认。"
        cards = Markup(render({"sheets": [{"scenario": "maintenance_fee", "rows": [explicit, missing]}]})[0]["html"]).root.find("eo-error-card")
        self.assertEqual(cards[0].find("action")[0].text(), "处理方式补交盖章清晰的结算单。")
        self.assertEqual(cards[1].find("action"), [])
        self.assertNotIn("补充能够直接核验", cards[1].text())

    def test_reason_and_filename_html_are_escaped(self) -> None:
        value = row("maintenance_fee", error_reasons=["合同 <script>：缺少签章。"], card_evidence={"source_files": ["目录/<img>.jpg"]})
        html = render({"sheets": [{"scenario": "maintenance_fee", "rows": [value]}]})[0]["html"]
        self.assertIn("&lt;script&gt;", html)
        self.assertIn("&lt;img&gt;.jpg", html)
        self.assertNotIn("<script>", html)
        self.assertNotIn("<img>", html)

    def test_root_and_asset_share_exact_renderer_and_keep_other_controls(self) -> None:
        root = (ROOT / "offline-activity-audit.html").read_text(encoding="utf-8")
        script = root.split('<script id="error-only-preview-script">', 1)[1].split("</script>", 1)[0].strip()
        self.assertEqual(script, ASSET.read_text(encoding="utf-8").strip())
        result = render({"sheets": [{"scenario": "maintenance_fee", "rows": [row("maintenance_fee")]}]}, script=script)
        self.assertEqual(result[0]["count"], 1)
        for control in ("eoErrorCategory", "eoErrorType", "eoPassList", "buildPassGroupHtml", "return evidenceHtml(item.evidence, item.source_files || [])"):
            self.assertIn(control, script)

    def test_current_nanxiong_snapshot_read_only_projection(self) -> None:
        path = ROOT / "worktrees/HX202601160053-广州南雄维护费用申请-9-20260911_0910_02/snapshot.json"
        if not path.is_file():
            self.skipTest("The saved customer snapshot is not in this checkout")
        from audit_core.error_reason import attach_error_reasons

        before = path.read_bytes()
        saved = json.loads(before)
        original_view = deepcopy(saved["view"])
        projected = attach_error_reasons(saved["view"])
        cards = Markup(render(projected)[0]["html"]).root
        reasons = [node.text() for node in cards.find("eo-line")]
        self.assertEqual(len(reasons), 4)
        for fact in ("盖章主体", "POS 销售电子表", "服务费结算单.jpg", "结算单.jpg"):
            self.assertIn(fact, "\n".join(reasons))
        for extra in ("材料要求", "审核结论", "核销影响", "暂不能自动核销", "置信度", "补交", "已盘点本包全部材料"):
            self.assertNotIn(extra, "\n".join(reasons))
        self.assertEqual([node.text() for node in cards.find("eo-evidence-source")], ["陈列销售.jpg", "陈列销售1.jpg", "服务费结算单.jpg", "结算单.jpg"])
        self.assertEqual(saved["view"], original_view)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), hashlib.sha256(before).hexdigest())


if __name__ == "__main__":
    unittest.main()
