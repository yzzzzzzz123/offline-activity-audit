"""Synthetic display provenance checks; no model or business service is used."""
from copy import deepcopy
import json
import unittest

from audit_core.display_source_evidence import (DisplaySourceError, validate_display_sources,
    display_source_options, MAX_SOURCE_OPTIONS, MAX_SOURCE_OPTIONS_BYTES)
from audit_core.pdf_evidence import audit_schema, photo_review_schema, validate_photo_reviews


def fixture():
    records = [
        ("v", "勤策核销明细", {"verify_code": "HX-SYNTHETIC", "activity_code": "A1", "activity_cm_id": "123", "actual_cm": "核销客户甲"}),
        ("a", "勤策申请明细", {"verify_code": "HX-SYNTHETIC", "applyCode": "A1", "subId": 123, "cusName": "甲店",
                              "startDate": "2026-10-01", "endDate": "2026-10-10",
                              "extColumns": [{"ext_key": "陈列、堆头类型", "ext_value": "1平堆"}]}),
    ]
    units, docs = [], []
    for uid, label, fields in records:
        facts = [key + "=" + json.dumps(value, ensure_ascii=False, separators=(",", ":")) for key, value in fields.items()]
        units.append({"unit_id": uid, "source_kind": "qince", "source_file": label, "native_facts": facts})
        docs.append({"unit_id": uid, "roles": ["independent"], "facts": deepcopy(facts), "limitations": []})
    facts = ["活动门店：甲店（甲路1号）、乙店（乙路2号）。", "表2 | 甲店 | 甲路1号 | 1平米", "具体规格详见附件。"]
    units += [{"unit_id": "c", "source_file": "合同.pdf", "image": None},
              {"unit_id": "p", "source_file": "照片.xlsx", "image": "synthetic.png"}]
    docs += [{"unit_id": "c", "roles": ["contract"], "facts": facts, "limitations": ["本图未展示完整附件"]},
             {"unit_id": "p", "roles": ["photo"], "facts": ["水印可读甲店，陈列边界被遮挡。"],
              "limitations": ["照片未显示合同门店清单"]}]
    materials = [{"id": "promotion_contract", "state": "present", "source_ids": ["c"]},
                 {"id": "display_photos", "state": "present", "source_ids": ["p"]}]
    return docs, materials, units


def application_assertion(docs):
    quote = next(f for d in docs if d["unit_id"] == "a" for f in d["facts"] if f.startswith("cusName="))
    return {"kind": "application_customer", "statement": "本单关联申请明细的客户名称为“甲店”，仅作名称参照。",
            "references": [{"unit_id": "a", "field": "cusName", "quote": quote}]}


def check(reason="本图没有可核验的实际面积尺度，暂时无法确认面积。", assertions=None):
    return {"rule_id": "display_size", "status": "unknown", "reason": reason,
            "source_ids": ["p", "c", "a"], "source_assertions": assertions or []}


class DisplaySourceOptionsTests(unittest.TestCase):
    def case(self):
        docs, materials, units = fixture()
        return {"scenario": "promotional_display", "documents": docs, "materials": materials, "units": units}

    def options(self, case):
        text = display_source_options(case)
        return text, json.loads(text.split("选项：", 1)[1]) if text else []

    def test_complete_contract_punctuation_is_copyable_without_relaxing_validator(self):
        case = self.case()
        quote = "执行周期：2026 年 10 月 01 日-2026 年 10 月 10 日。"
        case["documents"][2]["facts"] = [quote]
        text, options = self.options(case)
        option = next(a for a in options if a["kind"] == "contract_reference")
        self.assertEqual(option["references"][0]["quote"], quote)
        self.assertTrue(option["statement"].endswith("。”。"))
        value = check(option["statement"], [option])
        original = deepcopy((case, value))
        validate_display_sources(value, case["documents"], case["materials"], case["units"], path="checks/0")
        self.assertEqual((case, value), original)
        bad = deepcopy(value)
        bad["source_assertions"][0]["statement"] = option["statement"][:-1]
        bad["reason"] = bad["source_assertions"][0]["statement"]
        with self.assertRaises(DisplaySourceError):
            validate_display_sources(bad, case["documents"], case["materials"], case["units"], path="checks/0")
        self.assertIn("未列出不代表资料缺少", text)

    def test_every_option_passes_original_ref_and_reason_checks_without_changing_state(self):
        case = self.case(); before = deepcopy(case)
        _, options = self.options(case)
        self.assertTrue(options)
        for option in options:
            value = check(option["statement"] + "本图边界被遮挡，实际面积仍无法确认。", [option])
            value["source_ids"] = ["p", *[r["unit_id"] for r in option["references"]]]
            validate_display_sources(value, case["documents"], case["materials"], case["units"], path="checks/0")
            self.assertEqual(value["status"], "unknown")
        self.assertEqual(case, before)

    def test_limitations_wrong_role_and_unlinked_or_other_claim_fields_never_become_options(self):
        for mutation in ("limits", "source_kind", "label", "link", "verify"):
            case = self.case()
            if mutation == "limits":
                case["documents"][2]["limitations"] = case["documents"][2]["facts"]
                case["documents"][2]["facts"] = []
            elif mutation == "source_kind": case["units"][1]["source_kind"] = "photo"
            elif mutation == "label": case["units"][1]["source_file"] = "普通材料"
            else:
                index = 2 if mutation == "link" else 0
                fact = 'activity_cm_id="different"' if mutation == "link" else 'verify_code="OTHER"'
                case["units"][0]["native_facts"][index] = fact
                case["documents"][0]["facts"][index] = fact
            _, options = self.options(case)
            with self.subTest(mutation=mutation):
                if mutation == "limits": self.assertFalse(any(a["kind"] == "contract_reference" for a in options))
                else: self.assertFalse(any(a["kind"] == "application_customer" for a in options))

    def test_complete_values_are_not_truncated_to_fit_count_or_utf8_budget(self):
        case = self.case()
        facts = [f"第{i}条活动原文：" + "原" * 700 + "。" for i in range(80)]
        case["documents"][2]["facts"] = facts
        text, options = self.options(case)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_SOURCE_OPTIONS_BYTES)
        self.assertLessEqual(len(options), MAX_SOURCE_OPTIONS)
        self.assertTrue(options)
        for option in options:
            if option["kind"] == "contract_reference": self.assertIn(option["references"][0]["quote"], facts)
        case["documents"][2]["facts"] = [f"活动{i}。" for i in range(100)]
        text, options = self.options(case)
        self.assertLessEqual(len(options), MAX_SOURCE_OPTIONS)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_SOURCE_OPTIONS_BYTES)

    def test_exact_duplicate_options_are_deduplicated_and_order_is_stable(self):
        case = self.case(); quote = case["documents"][2]["facts"][0]
        case["documents"][2]["facts"] = [quote, quote, quote]
        first, options = self.options(case)
        self.assertEqual(sum(a["kind"] == "contract_reference" for a in options), 1)
        self.assertEqual(first, display_source_options(deepcopy(case)))

    def test_legal_copied_option_does_not_authorize_uncovered_attachment_claim(self):
        case = self.case(); _, options = self.options(case)
        option = next(a for a in options if a["kind"] == "application_customer")
        value = check(option["statement"] + "合同门店清单经该申请行取得。", [option])
        with self.assertRaises(DisplaySourceError) as caught:
            validate_display_sources(value, case["documents"], case["materials"], case["units"], path="checks/0")
        self.assertEqual(caught.exception.validation_paths, ["checks/0/reason"])

    def test_only_display_provider_prompt_gets_options_and_other_scenarios_stay_empty(self):
        from audit_core.pdf_materials import _display_source_prompt
        case = self.case()
        self.assertIn(display_source_options(case), _display_source_prompt(case))
        from audit_core.scenario_registry import SCENARIO_LABELS
        for scenario in SCENARIO_LABELS:
            if scenario != "promotional_display":
                other = dict(case, scenario=scenario)
                self.assertEqual(display_source_options(other), "")
                self.assertEqual(_display_source_prompt(other), "")

    def test_legacy_case_without_optional_units_gets_no_unverified_options(self):
        from audit_core.pdf_materials import _display_source_prompt
        case = self.case()
        del case["units"]
        self.assertEqual(display_source_options(case), "")
        self.assertNotIn("可复制声明选项", _display_source_prompt(case))

    def test_short_complete_options_balance_roles_after_long_contract_facts(self):
        case = self.case()
        long_facts = [f"合同段落{i}：" + "原文" * 450 + "。" for i in range(60)]
        short_fact = "执行周期：2026年10月1日至2026年10月10日。"
        case["documents"][2]["facts"] = long_facts + [short_fact]
        original = deepcopy(case)
        text, options = self.options(case)
        self.assertLessEqual(len(text.encode("utf-8")), MAX_SOURCE_OPTIONS_BYTES)
        self.assertLessEqual(len(options), MAX_SOURCE_OPTIONS)
        self.assertEqual({a["kind"] for a in options}, {
            "contract_reference", "application_customer", "verification_customer",
            "activity_period", "agreed_specification"})
        contracts = [a for a in options if a["kind"] == "contract_reference"]
        self.assertEqual(contracts[0]["references"][0]["quote"], short_fact)
        for assertion in options:
            value = check(assertion["statement"] + "本图实际尺度无法确认。", [assertion])
            value["source_ids"] = ["p", *[r["unit_id"] for r in assertion["references"]]]
            validate_display_sources(value, case["documents"], case["materials"], case["units"], path="checks/0")
        self.assertEqual(case, original)

    def test_long_first_fact_does_not_hide_later_short_contract_or_linked_spec(self):
        case = self.case()
        facts = ["合同说明：" + "大段原文" * 230 + "。", "约定规格：1平堆。"]
        case["documents"][2]["facts"] = facts
        first, options = self.options(case)
        case["documents"][2]["facts"] = list(reversed(facts))
        second, reordered = self.options(case)
        self.assertEqual(first, second)
        self.assertEqual(options, reordered)
        self.assertTrue(any(a["kind"] == "agreed_specification" for a in options))
        self.assertTrue(any(a["references"][0]["quote"] == facts[1] for a in options))
        self.assertTrue(all(a["references"][0]["quote"] in facts for a in options if a["kind"] == "contract_reference"))

    def test_copied_name_statement_does_not_allow_free_source_reinterpretation(self):
        case = self.case()
        assertion = application_assertion(case["documents"])
        for status, extra in (("unknown", "合同涉及门店只见附件，本批来源无门店清单。"),
                              ("pass", "申请客户作为本图门店对应依据，日期与店名齐备。")):
            value = check("本图水印可见甲店。" + assertion["statement"] + extra, [assertion])
            value["status"] = status
            before = deepcopy(value)
            with self.subTest(status=status), self.assertRaises(DisplaySourceError) as caught:
                validate_display_sources(value, case["documents"], case["materials"], case["units"], path="checks/0")
            self.assertEqual(caught.exception.validation_paths, ["checks/0/reason"])
            self.assertEqual(value, before)

    def test_visual_fact_plus_supported_statement_preserves_genuine_unknown(self):
        case = self.case()
        assertion = application_assertion(case["documents"])
        value = check("本图水印可见店名“甲店”。" + assertion["statement"]
                      + "现有依据尚不能确认本图门店的身份或位置。", [assertion])
        value["rule_id"] = "display_watermark"
        before = deepcopy(value)
        validate_display_sources(value, case["documents"], case["materials"], case["units"], path="checks/0")
        self.assertEqual(value, before)

    def test_display_prompt_examples_follow_same_statement_contract(self):
        from audit_core.pdf_materials import _display_source_prompt
        prompt = _display_source_prompt(self.case())
        self.assertIn("本单关联申请明细的客户名称为“甲店”，仅作名称参照。", prompt)
        self.assertIn("本单关联申请明细的规格原文为“1平堆”，不代表照片实际面积。", prompt)
        self.assertIn("statement和reason可自然表达", prompt)
        self.assertIn("名称参照不自动证明终端身份或完整地址", prompt)
        self.assertNotIn("可表述为‘结合本单关联的勤策申请明细", prompt)
        self.assertNotIn("可写‘水印名称与关联申请客户名称对应", prompt)
        self.assertIn("不新增街道门牌必填", prompt)


class DisplaySourceEvidenceTests(unittest.TestCase):
    def address_fixture(self, contract="示例路1号甲店", photo="示例路1号甲店"):
        docs, materials, units = fixture()
        cquote, pquote = "活动门店地址：" + contract, "照片水印地址：" + photo
        docs[2]["facts"] = [cquote]
        docs[3]["facts"] = [pquote]
        same = "".join(contract.split()) == "".join(photo.split())
        statement = (f"所引照片水印地址为“{photo}”，本次合同地址为“{contract}”，"
                     f"两处完整地址文本{'一致' if same else '不一致'}。")
        assertion = {"kind": "address_compare", "statement": statement, "references": [
            {"unit_id": "c", "field": "contract_address", "quote": cquote},
            {"unit_id": "p", "field": "watermark_address", "quote": pquote}]}
        value = dict(check(statement, [assertion]), rule_id="display_watermark", status="pass" if same else "fail")
        return value, docs, materials, units

    def test_complete_address_same_pass_and_different_fail_are_not_rewritten(self):
        for contract, photo in (("示例路1号甲店", "示例路1号甲店"),
                                ("示例路1号甲店", "示例路2号乙店"),
                                ("示例路 1 号甲店", "示例路1号甲店")):
            args = self.address_fixture(contract, photo)
            before = deepcopy(args)
            validate_display_sources(*args, path="photo_reviews/0/checks/0", photo_unit_id="p")
            self.assertEqual(args, before)

    def test_common_city_or_street_prefix_cannot_replace_complete_address(self):
        for prefix in ("上海市", "上海市示例路"):
            value, docs, materials, units = self.address_fixture("上海市示例路1号甲店", "上海市示例路2号乙店")
            assertion = value["source_assertions"][0]
            assertion["statement"] = (f"所引照片水印地址为“{prefix}”，本次合同地址为“{prefix}”，两处完整地址文本一致。")
            value.update(reason=assertion["statement"], status="pass")
            with self.assertRaises(DisplaySourceError):
                validate_display_sources(value, docs, materials, units, path="checks/0")

    def test_natural_address_facts_keep_full_field_and_ignore_later_timestamp(self):
        value, docs, materials, units = self.address_fixture("示例路1号", "示例路1号")
        cquote = "本次活动门店为测试甲店，门店地址为示例路1号。"
        pquote = "本图水印显示测试甲店，地址为示例路1号，拍摄时间2026年10月5日。"
        docs[2]["facts"], docs[3]["facts"] = [cquote], [pquote]
        value["source_assertions"][0]["references"][0]["quote"] = cquote
        value["source_assertions"][0]["references"][1]["quote"] = pquote
        before = deepcopy((value, docs, materials, units))
        validate_display_sources(value, docs, materials, units,
                                 path="photo_reviews/0/checks/0", photo_unit_id="p")
        self.assertEqual((value, docs, materials, units), before)
        for prefix in ("甲方通讯地址", "商品收货地址", "发货地址", "开票地址"):
            bad = deepcopy(docs)
            quote = prefix + "为示例路1号。"
            bad[2]["facts"] = [quote]
            changed = deepcopy(value)
            changed["source_assertions"][0]["references"][0]["quote"] = quote
            with self.subTest(prefix=prefix), self.assertRaises(DisplaySourceError):
                validate_display_sources(changed, bad, materials, units, path="checks/0")

    def test_address_cannot_borrow_another_photo_or_application_customer(self):
        for mutation in ("other_photo", "application", "limitations", "ambiguous", "unclear", "no_watermark"):
            value, docs, materials, units = self.address_fixture()
            a = value["source_assertions"][0]
            if mutation == "other_photo":
                docs.append(dict(deepcopy(docs[3]), unit_id="p2"))
                units.append(dict(deepcopy(units[3]), unit_id="p2"))
                materials[1]["source_ids"].append("p2")
                value["source_ids"].append("p2")
                a["references"][1]["unit_id"] = "p2"
            elif mutation == "application":
                a["references"][0] = dict(application_assertion(docs)["references"][0])
            elif mutation == "limitations":
                docs[3]["limitations"] = docs[3]["facts"]
                docs[3]["facts"] = []
            else:
                text = {"ambiguous": "甲店地址：示例路1号；乙店地址：示例路2号",
                        "unclear": "照片水印地址：疑似示例路1号甲店",
                        "no_watermark": "包装收货地址：示例路1号甲店"}[mutation]
                docs[3]["facts"] = [text]
                a["references"][1]["quote"] = text
            with self.subTest(mutation=mutation), self.assertRaises(DisplaySourceError):
                validate_display_sources(value, docs, materials, units,
                                         path="photo_reviews/0/checks/0", photo_unit_id="p")

    def test_valid_address_comparison_cannot_hide_old_attachment_claim(self):
        value, docs, materials, units = self.address_fixture()
        value["reason"] += "合同门店清单经该申请行取得。"
        with self.assertRaises(DisplaySourceError):
            validate_display_sources(value, docs, materials, units, path="checks/0")

    def test_address_negative_and_unknown_keep_original_status_at_real_entry(self):
        value, docs, materials, units = self.address_fixture("示例路1号甲店", "示例路2号乙店")
        checks = [value] + [dict(check(), rule_id=rule) for rule in
                           ("display_brand", "display_material_brand", "display_size")]
        result = {"photo_reviews": [{"unit_id": "p", "checks": checks}]}
        before = deepcopy(result)
        validate_photo_reviews(result, ["p"], docs, materials, units)
        self.assertEqual(result, before)
        value.update(status="unknown", reason="水印地址无法确认，暂不能核对。", source_assertions=[])
        validate_photo_reviews(result, ["p"], docs, materials, units)
        self.assertEqual(value["status"], "unknown")

    def test_address_uncertainty_before_or_after_comparison_preserves_unknown(self):
        docs, materials, units = fixture()
        for reason in (
            "无法确认地址与合同活动门店的对应。",
            "地址与合同活动门店的对应无法确认。",
            "水印地址与活动门店一致不能确认。",
            "水印地址与活动门店相符尚未确认。",
            "水印地址与活动门店对应仍无法核实。",
            "水印地址与本次活动门店的对应关系无法确认。",
            "水印也无其他地址文字可与合同门店对应，门店对应关系无法确认。",
            "本图无地址信息可与本次活动门店对应。",
        ):
            with self.subTest(reason=reason):
                checks = [dict(check(), rule_id=rule) for rule in
                          ("display_watermark", "display_brand", "display_material_brand", "display_size")]
                checks[0]["reason"] = reason
                value = {"photo_reviews": [{"unit_id": "p", "checks": checks}]}
                original = deepcopy((value, docs, materials, units))
                validate_photo_reviews(value, ["p"], docs, materials, units)
                self.assertEqual((value, docs, materials, units), original)
                self.assertEqual(checks[0]["status"], "unknown")

    def test_address_affirmation_is_not_excused_by_uncertainty_about_other_fields(self):
        docs, materials, units = fixture()
        for reason in (
            "地址对应，但无法确认面积。",
            "无法确认面积，地址一致。",
            "地址对应无法确认面积。",
            "地址一致；无法确认尺寸。",
            "地址对应。无法确认面积。",
            "地址对应无法确认，但另一处地址一致。",
            "地址与合同活动门店一致。",
            "水印也无其他地址文字，但地址与合同门店对应。",
            "水印无其他地址文字可与合同门店对应，但另一处地址一致。",
            "水印无其他地址文字可与合同门店对应且已经确认。",
            "没有其他资料，地址与合同门店对应。",
            "水印无其他地址文字可与合同门店一致。",
            "水印无其他地址文字可与合同门店对应但另一地址对应。",
            "地址对应关系无法确认面积。",
        ):
            with self.subTest(reason=reason), self.assertRaises(DisplaySourceError) as error:
                validate_display_sources(check(reason), docs, materials, units, path="checks/0")
            self.assertEqual(error.exception.validation_paths, ["checks/0/reason"])

    def test_trailing_uncertainty_does_not_authorize_uncovered_sources_or_bad_reference(self):
        docs, materials, units = fixture()
        reason = "地址与合同活动门店的对应无法确认。"
        for extra in ("合同门店清单经该申请行取得。", "附件未提供。", "本单全部终端范围已确认。",
                      "合同与申请明细仅有面积约定。"):
            with self.subTest(extra=extra), self.assertRaises(DisplaySourceError):
                validate_display_sources(check(reason + extra), docs, materials, units, path="checks/0")
        assertion = application_assertion(docs)
        assertion["references"][0]["quote"] = 'cusName="不属于本次来源"'
        value = check(assertion["statement"] + reason, [assertion])
        with self.assertRaises(DisplaySourceError) as error:
            validate_display_sources(value, docs, materials, units, path="checks/0")
        self.assertEqual(error.exception.validation_paths, ["checks/0/source_assertions/0/references/0/quote"])

    def test_realistic_linked_native_field_preserves_unknown_and_raw_input(self):
        docs, materials, units = fixture()
        assertion = application_assertion(docs)
        value = check(assertion["statement"] + "本图未显示可核验面积，无法确认实际面积。", [assertion])
        original = deepcopy((value, docs, materials, units))
        validate_display_sources(value, docs, materials, units, path="checks/0")
        self.assertEqual((value, docs, materials, units), original)
        self.assertEqual(value["status"], "unknown")

    def test_legal_assertion_does_not_hide_bad_remaining_reason(self):
        docs, materials, units = fixture()
        assertion = application_assertion(docs)
        for text in ("合同门店清单经该申请行取得。", "附件未在合同图出现，无法核对。",
                     "附件内容经申请行取得。", "地址与活动门店对应。", "逐店规格详见清单而清单未见。"):
            for assertions in ([], [assertion]):
                with self.subTest(text=text, assertion=bool(assertions)):
                    value = check((assertion["statement"] if assertions else "") + text, assertions)
                    with self.assertRaises(DisplaySourceError) as error:
                        validate_display_sources(value, docs, materials, units, path="checks/2")
                    self.assertEqual(error.exception.validation_paths, ["checks/2/reason"])
                    self.assertNotIn(text, str(error.exception))

    def test_native_contract_lists_and_table_rows_do_not_need_special_prefixes(self):
        docs, materials, units = fixture()
        for quote in docs[2]["facts"]:
            statement = f"本次合同参照原文：“{quote}”。"
            assertion = {"kind": "contract_reference", "statement": statement,
                         "references": [{"unit_id": "c", "field": "fact", "quote": quote}]}
            validate_display_sources(check(statement + "本图边界受遮挡，无法确认实际面积。", [assertion]),
                                     docs, materials, units, path="checks/0")

    def test_contract_quote_does_not_authorize_extra_completeness_claim(self):
        docs, materials, units = fixture()
        quote = docs[2]["facts"][0]
        assertion = {"kind": "contract_reference", "statement": f"本次合同参照原文：“{quote}”。",
                     "references": [{"unit_id": "c", "field": "fact", "quote": quote}]}
        for suffix in ("完整终端名单已经取得。", "因此地址与活动门店对应。", "合同附件字段没有地址。"):
            with self.subTest(suffix=suffix), self.assertRaises(DisplaySourceError):
                validate_display_sources(check(assertion["statement"] + suffix, [assertion]),
                                         docs, materials, units, path="checks/0")

    def test_limitations_roles_and_wrong_source_cannot_authorize_contract_claim(self):
        docs, materials, units = fixture()
        for uid, quote in (("p", docs[3]["limitations"][0]), ("c", docs[2]["limitations"][0]),
                           ("a", docs[1]["facts"][3])):
            statement = f"本次合同参照原文：“{quote}”。"
            assertion = {"kind": "contract_reference", "statement": statement,
                         "references": [{"unit_id": uid, "field": "fact", "quote": quote}]}
            with self.subTest(uid=uid), self.assertRaises(DisplaySourceError):
                validate_display_sources(check(statement, [assertion]), docs, materials, units, path="checks/0")

    def test_field_quote_role_and_same_claim_link_are_strict(self):
        for mutation in ("quote", "field", "link", "verify", "native", "source_kind"):
            docs, materials, units = fixture()
            assertion = application_assertion(docs)
            if mutation == "quote":
                assertion["references"][0]["quote"] = 'cusName = "甲店"'
            elif mutation == "field":
                assertion["references"][0]["field"] = "terminal_name"
            elif mutation in {"link", "verify"}:
                index = 2 if mutation == "link" else 0
                value = 'activity_cm_id="999"' if mutation == "link" else 'verify_code="OTHER"'
                docs[0]["facts"][index] = value
                units[0]["native_facts"][index] = value
            elif mutation == "native":
                units[1]["native_facts"] = []
            else:
                units[1]["source_kind"] = "photo"
            with self.subTest(mutation=mutation), self.assertRaises(DisplaySourceError):
                validate_display_sources(check(assertion["statement"], [assertion]), docs, materials, units, path="checks/0")

    def test_specification_is_not_visible_area_and_list_is_read_without_rewrite(self):
        docs, materials, units = fixture()
        quote = docs[1]["facts"][-1]
        statement = "本单关联申请明细的规格原文为“1平堆”，不代表照片实际面积。"
        assertion = {"kind": "agreed_specification", "statement": statement,
                     "references": [{"unit_id": "a", "field": "extColumns", "quote": quote}]}
        value = check(statement + "本图边界不清，仍无法确认实际面积。", [assertion])
        validate_display_sources(value, docs, materials, units, path="checks/0")
        self.assertEqual(value["status"], "unknown")
        assertion["statement"] = "本单关联申请明细的规格原文为“1平堆”，本图实际面积已达标。"
        with self.assertRaises(DisplaySourceError):
            validate_display_sources(check(assertion["statement"], [assertion]), docs, materials, units, path="checks/0")

    def test_ordinary_brand_size_and_material_checklist_are_not_source_claims(self):
        docs, materials, units = fixture()
        for reason in ("实物品牌模糊，无法确认。", "本图边界被遮挡，不能确认1平方米。",
                       "宣传物料上的字样模糊，不能确认品牌。", "已按资料清单逐项核对。",
                       "模板清单中的签章位置已核对。", "本图水印地址不能与已有合同参照对应。",
                       "无法确认水印地址与本次活动门店一致。", "该照片水印没有地址。",
                       "本图只有店名，现有资料不足以确认地址对应。"):
            validate_display_sources(check(reason), docs, materials, units, path="checks/0")

    def test_statements_must_be_complete_and_no_untrusted_path_leaks(self):
        docs, materials, units = fixture()
        assertion = application_assertion(docs)
        for value in (check("并非" + assertion["statement"], [assertion]),
                      check(assertion["statement"] * 2, [assertion]),
                      check(assertion["statement"], [assertion] * 9)):
            with self.assertRaises(DisplaySourceError):
                validate_display_sources(value, docs, materials, units, path="checks/0")
        error = DisplaySourceError("PRIVATE_SOURCE " * 1000)
        self.assertEqual(error.validation_paths, ["$"])
        self.assertNotIn("PRIVATE_SOURCE", str(error))
        self.assertLessEqual(len(str(error).encode("utf-8")), 1000)

    def test_both_display_generation_schemas_require_bounded_assertions(self):
        for schema in (photo_review_schema(["p"], ["p", "c"], ["c"]),
                       audit_schema("promotional_display")):
            item = (schema["properties"]["photo_reviews"]["items"]["properties"]["checks"]["items"]
                    if "photo_reviews" in schema["properties"] and "checks" not in schema["properties"]
                    else schema["properties"]["checks"]["items"])
            self.assertIn("source_assertions", item["required"])
            self.assertEqual(item["properties"]["source_assertions"]["maxItems"], 8)
        for scenario in ("entry_fee", "personnel_incentive", "price_difference_support", "poster_material",
                         "pos_target_incentive", "giveaway_promotion", "self_procured_gift_material"):
            self.assertNotIn("source_assertions", audit_schema(scenario)["properties"]["checks"]["items"]["properties"])

    def test_per_photo_entry_records_prose_warning_without_changing_model_result(self):
        docs, materials, units = fixture()
        checks = [dict(check(), rule_id=rule) for rule in
                  ("display_watermark", "display_brand", "display_material_brand", "display_size")]
        checks[0]["reason"] = "合同门店清单经该申请行取得。"
        value = {"photo_reviews": [{"unit_id": "p", "checks": checks}]}
        original = deepcopy(value)
        validate_photo_reviews(value, ["p"], docs, materials, units)
        self.assertEqual(value, original)


class DisplayLocalScopeFeedbackTests(unittest.TestCase):
    def test_local_picture_absence_remains_rejected_without_rewriting_input(self):
        docs, materials, units = fixture()
        assertion = application_assertion(docs)
        for suffix in (
            "合同涉及门店仅写清单见附件而门店清单未在本图出现。",
            "本图水印店名与申请客户名称仅名称相近。",
        ):
            value = check(assertion["statement"] + suffix, [assertion])
            original = deepcopy((value, docs, materials, units))
            with self.subTest(suffix=suffix), self.assertRaises(DisplaySourceError) as caught:
                validate_display_sources(value, docs, materials, units, path="checks/0")
            self.assertEqual(caught.exception.validation_paths, ["checks/0/reason"])
            self.assertEqual((value, docs, materials, units), original)
            self.assertEqual(value["status"], "unknown")

    def test_supported_name_statement_and_actual_unknown_are_preserved(self):
        docs, materials, units = fixture()
        assertion = application_assertion(docs)
        value = check(assertion["statement"] + "本图水印可见店名甲店。现有依据尚不能确认本图门店的身份或位置；边界被遮挡，实际面积仍无法确认。", [assertion])
        original = deepcopy((value, docs, materials, units))
        validate_display_sources(value, docs, materials, units, path="checks/0")
        self.assertEqual((value, docs, materials, units), original)
        self.assertEqual(value["status"], "unknown")

    def test_prompt_explains_local_scope_without_new_address_requirements(self):
        from audit_core.pdf_materials import _display_source_prompt
        docs, materials, units = fixture()
        case = dict(scenario="promotional_display", documents=docs, materials=materials, units=units)
        original = deepcopy(case)
        text = _display_source_prompt(case)
        self.assertIn("不能作为缺资料或门店对应不明的理由", text)
        self.assertIn("读取限制保留在facts/limitations", text)
        self.assertIn("可如实比较本图店名与本单关联名称", text)
        self.assertIn("不要因未写省市区或街道门牌就新增缺件或门店无法对应结论", text)
        self.assertIn("不为满足声明格式改变status", text)
        self.assertEqual(case, original)
        for scenario in ("price_difference_support", "self_procured_gift_material", "entry_fee", "personnel_incentive", "poster_material", "pos_target_incentive", "giveaway_promotion"):
            self.assertEqual(_display_source_prompt(dict(case, scenario=scenario)), "")

    def test_retry_feedback_is_bounded_fixed_text_and_keeps_role_guard(self):
        for path in ("checks/0/reason", "photo_reviews/0/checks/0/source_assertions/0/statement", "PRIVATE_SECRET" * 100):
            error = DisplaySourceError(path)
            message = str(error)
            self.assertIn("本图/合同图未展示附件或清单", message)
            self.assertIn("即使未说“整份未提供”也不例外", message)
            self.assertIn("申请客户名称不能改称终端地址", message)
            self.assertIn("不新增省市区或街道门牌条件", message)
            self.assertIn("不改变pass/fail/unknown", message)
            self.assertNotIn("PRIVATE_SECRET", message)
            self.assertLessEqual(len(message.encode("utf-8")), 1000)


if __name__ == "__main__":
    unittest.main()
