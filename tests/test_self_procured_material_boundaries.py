"""Purchased gifts keep required receipts separate from optional payment records."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_evidence import classification_decision, classification_issues, validate
from audit_core.pdf_materials import GIFT_PHOTO_AUDIT_CLARIFICATION, GIFT_RULE_GATE_CLARIFICATION, PdfEvidenceProvider
from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
from audit_core.pdf_policy import MATERIALS, requirements, rules
from tests.pdf_test_support import classification, flags_for, image_bytes, unknown_checks


SCENARIO = "self_procured_gift_material"
AUXILIARY = "purchase_transaction_record"
PHOTO_IDS = {"gift_finished_photos", "gift_store_photos"}


def gift_packet(*, absent=(), auxiliary_state="not_applicable", same_photo=False,
                extra=False, unclear=()):
    """Each required role has its own synthetic source unless explicitly shared."""
    materials, documents = [], {}
    absent, unclear = set(absent), set(unclear)
    for requirement in requirements(SCENARIO):
        material_id = requirement.id
        if material_id == 'gift_activity_evidence':
            continue
        state = (auxiliary_state if material_id == AUXILIARY else
                 "missing" if material_id in absent else
                 "unclear" if material_id in unclear else "present")
        unit_id = "shared-photo" if same_photo and material_id in PHOTO_IDS else "u-" + material_id
        source_ids = [unit_id] if state in {"present", "unclear"} else []
        if source_ids:
            documents[unit_id] = {"unit_id": unit_id, "roles": ["submitted"],
                "facts": ["本次活动门店现场可见外采赠品及促销内容" if unit_id == "shared-photo"
                          else "本次采购的付款交易记录，仅证明转账" if material_id == AUXILIARY
                          else "本次已提交的" + material_id], "limitations": []}
        materials.append({"id": material_id, "state": state,
            "reason": "原图部分遮挡，暂不能确认对应内容" if state == "unclear" else
                      "本次未提供" if state in {"missing", "not_applicable"} else "本次已提交",
            "source_ids": source_ids})
    extras = []
    if extra:
        documents["unrelated"] = {"unit_id": "unrelated", "roles": ["receipt"],
            "facts": ["与本次赠品采购无关的独立费用凭证"], "limitations": []}
        extras = [{"description": "其他费用凭证", "reason": "与本次采购无关",
                   "source_ids": ["unrelated"]}]
    by_id = {item['id']: item for item in materials}
    proof_ids = {'pos', 'pos_excel', *PHOTO_IDS}
    ids = list(documents)
    flags = flags_for(SCENARIO, uses_pos=any(by_id[key]['state'] == 'present' for key in ('pos', 'pos_excel')),
                      photo_evidence=any(by_id[key]['state'] in {'present', 'unclear'} for key in PHOTO_IDS))
    optional = {item.id for item in requirements(SCENARIO) if item.when == 'optional'}
    supported = not extras and all(item['id'] in optional or item['state'] == 'present' for item in materials)
    match = {"scenario": SCENARIO, "supported": supported, "reason": "盘点本次资料",
             "source_ids": ids, "flags": flags, "materials": materials, "extra_materials": extras}
    return {"candidate_scenarios": [SCENARIO], "reason": "上游已指定外采赠品",
            "source_ids": ids, "flags": flags, "materials": materials,
            "background_materials": [], "material_matches": [match]}, list(documents.values())


class SelfProcuredMaterialBoundaryTests(unittest.TestCase):
    def test_invoice_remains_required_and_auxiliary_is_scoped_without_new_payment_audit(self):
        materials = {item.id: item for item in requirements(SCENARIO)}
        self.assertEqual(materials["invoice"].when, "always")
        self.assertEqual(materials[AUXILIARY].when, "optional")
        for scenario in set(MATERIALS) - {SCENARIO}:
            self.assertNotIn(AUXILIARY, {item.id for item in requirements(scenario)}, scenario)
        self.assertFalse(any("payment" in rule.id for rule in rules(SCENARIO)))
        self.assertIn("invoice_amount", {r.id for r in rules(SCENARIO)})

    def test_auxiliary_absent_or_submitted_does_not_block_or_lose_its_source(self):
        for state in ("not_applicable", "missing", "unclear", "present"):
            with self.subTest(state=state):
                value, documents = gift_packet(auxiliary_state=state)
                original = deepcopy((value, documents))
                result = classification_decision(value, documents, SCENARIO)
                self.assertTrue(result["matched"])
                self.assertEqual(result["reasons"], [])
                self.assertEqual((value, documents), original)
                if state in {"present", "unclear"}:
                    self.assertIn("u-" + AUXILIARY, result["classification"]["source_ids"])
                    self.assertEqual(result["classification"]["background_materials"], [])

    def test_transfer_record_cannot_waive_missing_invoice_or_receipt(self):
        value, documents = gift_packet(absent=["invoice"], auxiliary_state="present")
        result = classification_decision(value, documents, SCENARIO)
        self.assertFalse(result["matched"])
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("缺少资料：发票或收据", result["reasons"][0])
        self.assertNotIn("多出", result["reasons"][0])

    def test_same_actual_photo_can_cover_both_roles_without_duplicate_upload(self):
        value, documents = gift_packet(same_photo=True)
        result = classification_decision(value, documents, SCENARIO)
        self.assertTrue(result["matched"])
        photo_roles = [item for item in result["classification"]["materials"] if item["id"] in PHOTO_IDS]
        self.assertEqual(len(photo_roles), 2)
        self.assertTrue(all(item["source_ids"] == ["shared-photo"] for item in photo_roles))
        self.assertEqual(sum(document["unit_id"] == "shared-photo" for document in documents), 1)

    def test_complete_pos_does_not_waive_required_photos_or_rewrite_inventory(self):
        value, documents = gift_packet(absent=PHOTO_IDS, auxiliary_state="present")
        original = deepcopy(value)
        result = classification_decision(value, documents, SCENARIO)
        self.assertFalse(result["matched"])
        self.assertTrue(result["reasons"])
        self.assertEqual(value, original)
        self.assertEqual(sum(item["state"] == "missing" for item in value["materials"]), 2)
        self.assertTrue(classification_issues(value, SCENARIO))

    def test_missing_or_unclear_photos_remain_required_even_with_pos(self):
        for missing_id in PHOTO_IDS:
            with self.subTest(missing_id=missing_id):
                value, documents = gift_packet(absent=[missing_id])
                result = classification_decision(value, documents, SCENARIO)
                self.assertFalse(result["matched"])
                self.assertTrue(result["reasons"])
        value, documents = gift_packet(absent=['pos', 'pos_excel', 'gift_finished_photos'], unclear=["gift_store_photos"])
        result = classification_decision(value, documents, SCENARIO)
        self.assertFalse(result["matched"])
        issues = classification_issues(value, SCENARIO)
        uncertain = [issue for issue in issues if "尚不能确认" in issue["reason"]]
        self.assertEqual(len(uncertain), 1)
        self.assertIn("原图部分遮挡", uncertain[0]["reason"])
        self.assertEqual(uncertain[0]["source_ids"], ["u-gift_store_photos"])

    def test_complete_photos_do_not_cover_missing_or_partial_pos(self):
        for absent in ({'pos', 'pos_excel'}, {'pos'}):
            with self.subTest(absent=absent):
                value, documents = gift_packet(absent=absent)
                self.assertFalse(classification_decision(value, documents, SCENARIO)['matched'])

    def test_missing_pos_and_photos_preserve_each_required_role_gap(self):
        value, documents = gift_packet(absent={'pos', 'pos_excel', *PHOTO_IDS})
        result = classification_decision(value, documents, SCENARIO)
        self.assertFalse(result['matched'])
        self.assertTrue(result['reasons'])
        self.assertNotIn('gift_activity_evidence',{m['id'] for m in value['materials']})
        self.assertTrue(any('照片' in reason for reason in result['reasons']))

    def test_auxiliary_neither_hides_unrelated_expense_nor_accepts_unread_source(self):
        value, documents = gift_packet(auxiliary_state="present", extra=True)
        result = classification_decision(value, documents, SCENARIO)
        self.assertFalse(result["matched"])
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("多出的资料：其他费用凭证", result["reasons"][0])
        value, documents = gift_packet(auxiliary_state="present")
        auxiliary = next(item for item in value["materials"] if item["id"] == AUXILIARY)
        auxiliary["source_ids"] = ["not-read"]
        with self.assertRaises(AuditError):
            classification_decision(value, documents, SCENARIO)

    def test_provider_gate_receives_boundary_and_preserves_the_real_material_decision(self):
        value, documents = gift_packet(auxiliary_state="present", same_photo=True)
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        prompts = []

        def gate_call(root, skill, schema, prompt, **kwargs):
            prompts.append(render_prompt(prompt))
            self.assertEqual(kwargs["images"], [])
            kwargs["validator"](value)
            return value

        case = {"archive_id": "a001", "selected_scenario": SCENARIO,
                "units": [{"unit_id": document["unit_id"], "image": None,
                           "native_facts": document["facts"], "limitations": []}
                          for document in documents]}
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_call", side_effect=gate_call):
            packet = provider.classify(case, Path(temporary))
        self.assertEqual(len(prompts), 1)
        self.assertTrue(AUXILIARY in prompts[0], "门禁提示应包含可选采购交易记录角色")
        self.assertTrue("不能用辅助资料顶替必交凭证" in prompts[0], "门禁应保留发票或收据必交边界")
        self.assertTrue("不要求重复提交两套照片" in prompts[0], "门禁应允许同一现场图覆盖两种实际用途")
        self.assertTrue(classification_decision(packet["classification"], packet["documents"], SCENARIO)["matched"])


class GiftRuleInformationBoundaryTests(unittest.TestCase):
    """Model doubles exercise the real gate; they are not real-model acceptance."""

    def packet(self, *, rule=None, absent=()):
        value, documents = gift_packet(absent=absent)
        materials = value["materials"]
        gift = next(item for item in materials if item["id"] == "gift_rule")
        documents = [doc for doc in documents if doc["unit_id"] != "u-gift_rule"]
        contract = next(doc for doc in documents if doc["unit_id"] == "u-promotion_contract")
        settlement = next(doc for doc in documents if doc["unit_id"] == "u-settlement")
        contract["roles"] = ["促销合同（本次品牌方约定）"]
        contract["facts"] = ["活动说明：采购赠品杯子200个，每个6元，计算规则200×6=1200元。"]
        settlement["roles"] = ["经销商结算申报"]
        settlement["facts"] = ["本次采购赠品200个，单价6元，申报采购费用1200元。"]
        if rule is not None:
            contract["facts"].append(rule)
        gift.update(state="present" if rule is not None else "unclear",
            reason="本次合同已明确赠送约定，执行数量仍需业务核对" if rule is not None else
                   "已看到采购数量、采购单价及费用计算，尚不能确认本次实际赠送规则",
            source_ids=[contract["unit_id"], settlement["unit_id"]])
        match = value["material_matches"][0]
        value["source_ids"] = match["source_ids"] = [doc["unit_id"] for doc in documents]
        match["supported"] = rule is not None and not absent
        return value, documents

    def through_provider(self, value, documents, *, selected=True):
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        original = deepcopy((value, documents))
        if selected:
            response = deepcopy(value)
        else:
            response = classification(SCENARIO, [doc["unit_id"] for doc in documents])
            response["material_matches"] = [deepcopy(value["material_matches"][0]) if row["scenario"] == SCENARIO
                                             else row for row in response["material_matches"]]
            response["candidate_scenarios"] = [SCENARIO] if value["material_matches"][0]["supported"] else []
            response["materials"] = deepcopy(value["materials"]) if response["candidate_scenarios"] else []
        prompts = []

        def fake_gate(root, skill, schema, prompt, **kwargs):
            text = render_prompt(prompt)
            prompts.append(text)
            self.assertIn(GIFT_RULE_GATE_CLARIFICATION, text)
            self.assertIn("采购成本算式", text)
            self.assertIn("不因缺件停止后续业务审核", text)
            self.assertEqual(kwargs["images"], [])
            kwargs["validator"](response)
            return deepcopy(response)

        case = {"archive_id": "a001", "units": []}
        if selected:
            case["selected_scenario"] = SCENARIO
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_read_documents", return_value=deepcopy(documents)), \
                patch.object(provider, "_call", side_effect=fake_gate) as call:
            packet = provider.classify(case, Path(temporary))
        self.assertEqual(call.call_count, 1)
        self.assertEqual((value, documents), original)
        self.assertEqual(packet["documents"], documents)
        return packet, prompts[0]

    def test_purchase_only_unclear_remains_visible_despite_missing_pos_and_photos(self):
        value, documents = self.packet(absent={"pos", "pos_excel", *PHOTO_IDS})
        packet, prompt = self.through_provider(value, documents)
        self.assertIn("采购赠品杯子200个", prompt)
        result = classification_decision(packet["classification"], packet["documents"], SCENARIO)
        self.assertFalse(result["matched"])
        self.assertEqual(len(result["reasons"]), 4)
        issues = classification_issues(packet["classification"], SCENARIO)
        gift_issue = next(item for item in issues if "活动赠送规则" in item["reason"])
        self.assertIn("已看到采购数量", gift_issue["reason"])
        self.assertEqual(gift_issue["source_ids"], ["u-promotion_contract", "u-settlement"])
        states = {m["id"]: m["state"] for m in packet["classification"]["materials"]}
        self.assertEqual(states["promotion_contract"], "present")
        self.assertEqual(states["settlement"], "present")
        self.assertEqual(states["gift_rule"], "unclear")

    def test_purchase_only_unclear_is_not_silently_repaired_when_other_materials_exist(self):
        value, documents = self.packet()
        packet, _ = self.through_provider(value, documents)
        result = classification_decision(packet["classification"], packet["documents"], SCENARIO)
        self.assertFalse(result["matched"])
        self.assertEqual(len(result["reasons"]), 1)
        self.assertIn("活动赠送规则", result["reasons"][0])

    def test_explicit_rule_and_direct_giving_quantity_remain_valid_without_all_three_fields(self):
        for rule in ("本次活动每购买2瓶指定商品赠杯子1个。", "本次活动约定赠送杯子200个。"):
            with self.subTest(rule=rule):
                value, documents = self.packet(rule=rule)
                packet, prompt = self.through_provider(value, documents)
                self.assertTrue(classification_decision(packet["classification"], packet["documents"], SCENARIO)["matched"])
                self.assertIn(rule, prompt)
                self.assertIn("不强制购买门槛、赠送比例、赠送对象三个字段同时必填", prompt)
                self.assertIn("不要求另交赠送规则文件或附件", prompt)
                self.assertIn("不代表已实际赠出", prompt)

    def test_legacy_eight_type_gate_receives_same_scoped_instruction_and_preserves_unclear(self):
        value, documents = self.packet(absent={"pos", "pos_excel", *PHOTO_IDS})
        packet, _ = self.through_provider(value, documents, selected=False)
        self.assertEqual(len(packet["classification"]["material_matches"]), 8)
        gift_match = next(row for row in packet["classification"]["material_matches"] if row["scenario"] == SCENARIO)
        self.assertEqual(next(m for m in gift_match["materials"] if m["id"] == "gift_rule")["state"], "unclear")
        self.assertEqual(packet["classification"]["candidate_scenarios"], [])

    def test_other_seven_selected_gates_do_not_receive_gift_instruction(self):
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        for scenario in set(MATERIALS) - {SCENARIO}:
            with self.subTest(scenario=scenario):
                prompt = render_prompt(provider._gate_prompt(scenario, []))
                self.assertNotIn(GIFT_RULE_GATE_CLARIFICATION, prompt)
                self.assertNotIn("采购成本算式", prompt)

    def test_missing_rule_still_missing_and_sources_cannot_be_invented(self):
        value, documents = gift_packet(absent={"gift_rule"})
        result = classification_decision(value, documents, SCENARIO)
        self.assertFalse(result["matched"])
        self.assertIn("缺少资料：活动赠送规则", result["reasons"][0])
        value, documents = self.packet()
        next(m for m in value["materials"] if m["id"] == "gift_rule")["source_ids"] = ["not-read"]
        with self.assertRaises(AuditError):
            classification_decision(value, documents, SCENARIO)


class GiftPhotoIdentityBoundaryTests(unittest.TestCase):
    """Final-audit instructions do not invent identity requirements or rewrite answers."""

    def through_audit(self, *, contract, photo_facts, status, reason):
        value, documents = gift_packet()
        materials = value["materials"]
        old_photos = {uid for item in materials if item["id"] in PHOTO_IDS for uid in item["source_ids"]}
        documents = [doc for doc in documents if doc["unit_id"] not in old_photos]
        next(doc for doc in documents if doc["unit_id"] == "u-promotion_contract")["facts"] = [contract]
        next(doc for doc in documents if doc["unit_id"] == "u-invoice")["facts"] = ["收据：收纳筐6000个，每个4.5元。"]
        documents.extend({"unit_id": f"photo-{index}", "roles": ["活动现场照片"],
                          "facts": [fact], "limitations": []}
                         for index, fact in enumerate(photo_facts, 1))
        photo_ids = [doc["unit_id"] for doc in documents if doc["unit_id"].startswith("photo-")]
        for item in materials:
            if item["id"] in PHOTO_IDS:
                item["source_ids"] = photo_ids[:]
            elif item['id'] == 'gift_activity_evidence':
                item['source_ids'] = [uid for uid in item['source_ids'] if uid not in old_photos] + photo_ids
        response = unknown_checks(SCENARIO, value["flags"])
        check = next(item for item in response["checks"] if item["rule_id"] == "gift_finished_photos")
        check.update(status=status, reason=reason, source_ids=photo_ids + ["u-promotion_contract"])
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        captured = []
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            units = []
            for doc in documents:
                path = root / (doc["unit_id"] + ".png") if doc["unit_id"] in photo_ids else None
                if path:
                    path.write_bytes(image_bytes())
                units.append({"unit_id": doc["unit_id"], "image": path,
                              "native_facts": [] if path else doc["facts"], "limitations": []})
            case = {"archive_id": "a001", "scenario": SCENARIO, "flags": value["flags"],
                    "documents": documents, "materials": materials, "units": units}
            original = deepcopy(case)

            def fake_call(directory, skill, schema, prompt, *, images, label, validator, **kwargs):
                captured.append(render_prompt(prompt))
                self.assertTrue(label.endswith("模型自主审核"))
                # Reading facts carry each photo; originals are only listed by
                # name and fetched when the audit explicitly asks for one.
                self.assertEqual(images, [])
                for unit in units:
                    if unit["image"]:
                        self.assertIn(unit["image"].name, captured[0])
                validate(response, schema)
                validator(response)
                return deepcopy(response)


            with patch.object(provider, "_call", side_effect=fake_call), patch(
                    "audit_core.pdf_materials.prepare_product_references", return_value=({
                        "reference_only": True, "rule_ids": [], "references": [],
                        "resolutions": [], "limitations": []}, [])), patch(
                    "audit_core.pdf_materials.prepare_pos_identities", return_value={"applicable": False}), patch(
                    "httpx.AsyncClient", side_effect=AssertionError("No external model or catalogue calls")):
                result = provider.audit(case, root)
            self.assertEqual(case, original)
        self.assertEqual(result, response, "Prompt clarification must not rewrite model facts or conclusions")
        self.assertEqual(len(captured), 1)
        self.assertIn(GIFT_PHOTO_AUDIT_CLARIFICATION, captured[0])
        self.assertIn(contract, captured[0])
        for fact in photo_facts:
            self.assertIn(fact, captured[0])
        return result, captured[0]

    def test_generic_basket_names_and_floor_position_do_not_create_extra_identity_fields(self):
        _, prompt = self.through_audit(
            contract="本次活动赠送收纳筐6000个，每购买指定产品满29.9元赠送1个收纳筐。",
            photo_facts=["甲店水印日期与地址可见，活动陈列旁地面有2个米色塑料购物篮。",
                         "乙店水印日期与地址可见，活动架旁有浅色收纳篮。"],
            status="pass", reason="两张照片都可见本次活动现场的筐篮，水印拍摄时间和门店地址可见。")
        self.assertIn("未约定能区分不同款式的具体特征", prompt)
        self.assertIn("名称的差别及摆在地面本身", prompt)
        self.assertIn("不把采购来源身份、尺寸、颜色、型号或SKU新增为必须证明的条件", prompt)
        self.assertIn("不表示凡有篮筐都自动通过", prompt)

    def test_actual_missing_gift_remains_problem_even_if_promotion_sign_mentions_it(self):
        result, prompt = self.through_audit(
            contract="本次活动赠品为收纳筐。",
            photo_facts=["甲店地面可见购物篮。", "乙店照片只有牙膏及满29.9赠收纳筐的促销牌，未见筐篮实物。"],
            status="fail", reason="乙店照片没有看到赠品筐篮实物，促销牌文字不能代替现场实物。")
        self.assertIn("不能用促销牌写有赠品、合同列有赠品或另一门店照片代替本图实物", prompt)
        self.assertEqual(next(c for c in result["checks"] if c["rule_id"] == "gift_finished_photos")["status"], "fail")

    def test_specific_visual_uncertainty_and_explicit_feature_conflict_are_not_forced_to_pass(self):
        for status, contract, fact, reason in (
            ("unknown", "本次赠送收纳筐。", "甲店物体被纸箱遮挡，只能看见一小段提手。",
             "甲店照片中的物体被纸箱遮挡，无法辨认是否为筐篮。"),
            ("fail", "本次约定赠送红色带盖收纳筐。", "甲店照片清楚可见蓝色无盖筐篮。",
             "甲店照片为蓝色无盖筐篮，与本次合同明确约定的红色带盖收纳筐不符。")):
            with self.subTest(status=status):
                result, prompt = self.through_audit(contract=contract, photo_facts=[fact], status=status, reason=reason)
                self.assertIn("本单若明确约定具体特征，仍按该真实约定核对", prompt)
                check = next(c for c in result["checks"] if c["rule_id"] == "gift_finished_photos")
                self.assertEqual((check["status"], check["reason"]), (status, reason))

    def test_photo_clarification_is_absent_from_all_material_gates_and_other_final_audits(self):
        provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        for scenario in MATERIALS:
            self.assertNotIn(GIFT_PHOTO_AUDIT_CLARIFICATION, render_prompt(provider._gate_prompt(scenario, [])))
        _, documents = gift_packet()
        response = classification(SCENARIO, [doc["unit_id"] for doc in documents])
        gate_prompts = []
        def legacy_gate(directory, skill, schema, prompt, **kwargs):
            gate_prompts.append(render_prompt(prompt))
            validate(response, schema)
            kwargs["validator"](response)
            return deepcopy(response)
        with tempfile.TemporaryDirectory() as temporary, patch.object(provider, "_read_documents", return_value=documents), \
                patch.object(provider, "_call", side_effect=legacy_gate):
            provider.classify({"archive_id": "a001", "units": []}, Path(temporary))
        self.assertEqual(len(gate_prompts), 1)
        self.assertNotIn(GIFT_PHOTO_AUDIT_CLARIFICATION, gate_prompts[0])

        class Captured(Exception):
            pass

        for scenario in set(MATERIALS) - {SCENARIO}:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                case = {"archive_id": "a001", "scenario": scenario, "flags": flags_for(scenario, uses_pos=False),
                        "documents": [], "materials": [], "units": []}
                prompts = []
                def capture(directory, skill, schema, prompt, **kwargs):
                    prompts.append(render_prompt(prompt))
                    raise Captured()
                with patch.object(provider, "_call", side_effect=capture), patch(
                        "audit_core.pdf_materials.prepare_product_references", return_value=({}, [])), patch(
                        "audit_core.pdf_materials.prepare_pos_identities", return_value={"applicable": False}), patch(
                        "audit_core.pdf_materials.template_comparison", return_value=([], [])), patch(
                        "httpx.AsyncClient", side_effect=AssertionError("No external model or catalogue calls")):
                    with self.assertRaises(Captured):
                        provider.audit(case, Path(temporary))
                self.assertEqual(len(prompts), 1)
                self.assertNotIn(GIFT_PHOTO_AUDIT_CLARIFICATION, prompts[0])


if __name__ == "__main__":
    unittest.main()
