"""Price-photo coverage and source boundaries without model calls or real files."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from pathlib import Path
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_decision, audit_schema, validate
from tests.pdf_test_support import flags_for, unknown_checks


SCENARIO = "price_difference_support"


def fixture():
    flags = flags_for(SCENARIO)
    documents = [
        {"unit_id": "clear", "facts": ["本图活动价19元，水印日期和地址清楚"]},
        {"unit_id": "missing-price", "facts": ["本图没有展示价格，水印日期和地址清楚"]},
    ]
    materials = [{"id": "price_evidence", "state": "present", "reason": "两张已交现场图",
                  "source_ids": ["clear", "missing-price"]}]
    units = [{"unit_id": row["unit_id"], "image": Path(row["unit_id"] + ".png"),
              "source_file": row["unit_id"] + ".png", "locator": "原图"} for row in documents]
    evidence = unknown_checks(SCENARIO, flags)
    evidence["price_photo_reviews"] = [
        {"unit_id": "clear", "status": "pass", "reason": "本图活动价19元可读", "source_ids": ["clear"]},
        {"unit_id": "missing-price", "status": "fail", "reason": "本图没有展示活动价格", "source_ids": ["missing-price"]},
    ]
    return flags, documents, materials, units, evidence


def decide(value):
    flags, documents, materials, units, evidence = value
    return audit_decision(SCENARIO, flags, evidence, documents, materials, units=units)


class PriceSupportPhotoReviewTests(unittest.TestCase):
    def test_all_readable_photos_keep_a_pass_and_all_technical_reviews(self):
        value = fixture()
        value[-1]["price_photo_reviews"][1].update(status="pass", reason="本图活动价29元可读")
        value[1][1]["facts"] = ["本图活动价29元，水印日期和地址清楚"]
        before = deepcopy(value)
        result = decide(value)
        check = next(row for row in result["checks"] if row["rule_id"] == "price_evidence")
        self.assertEqual(check["status"], "pass")
        self.assertEqual(check["source_ids"], ["clear", "missing-price"])
        self.assertEqual(len(result["price_photo_reviews"]), 2)
        self.assertEqual(value, before)

    def test_one_missing_price_is_not_waived_by_readable_other_photo(self):
        value = fixture()
        before = deepcopy(value)
        result = decide(value)
        check = next(row for row in result["checks"] if row["rule_id"] == "price_evidence")
        self.assertEqual(check["status"], "fail")
        self.assertEqual(check["source_ids"], ["missing-price"])
        self.assertEqual(check["reason"], "本图没有展示活动价格")
        self.assertEqual(result["price_photo_reviews"], value[-1]["price_photo_reviews"])
        self.assertEqual(value, before)

    def test_all_photos_must_be_reviewed_in_original_order(self):
        for fault in ("missing", "duplicate", "reordered", "extra"):
            with self.subTest(fault=fault):
                value = fixture()
                rows = value[-1]["price_photo_reviews"]
                if fault == "missing":
                    rows.pop()
                elif fault == "duplicate":
                    rows[1] = deepcopy(rows[0])
                elif fault == "reordered":
                    rows.reverse()
                else:
                    rows.append({"unit_id": "not-present", "status": "pass", "reason": "额外图片", "source_ids": ["not-present"]})
                with self.assertRaisesRegex(AuditError, "逐张按原始顺序"):
                    decide(value)

    def test_another_photo_cannot_supply_the_price_or_watermark(self):
        for ids in (["clear"], ["missing-price", "clear"]):
            with self.subTest(ids=ids):
                value = fixture()
                value[-1]["price_photo_reviews"][1]["source_ids"] = ids
                with self.assertRaisesRegex(AuditError, "本图"):
                    decide(value)

    def test_unreadable_photo_can_only_be_unknown(self):
        for status in ("pass", "fail", "unknown"):
            with self.subTest(status=status):
                value = fixture()
                value[3][1].update(image=None, photo_reference=True)
                value[-1]["price_photo_reviews"][1].update(status=status, reason="本图未能解码，暂时不能确认")
                if status == "unknown":
                    check = next(row for row in decide(value)["checks"] if row["rule_id"] == "price_evidence")
                    self.assertEqual(check["status"], "unknown")
                else:
                    with self.assertRaisesRegex(AuditError, "未能读取"):
                        decide(value)

    def test_empty_photo_list_does_not_turn_unknown_into_verified_pass(self):
        flags = flags_for(SCENARIO)
        evidence = unknown_checks(SCENARIO, flags)
        evidence["price_photo_reviews"] = []
        result = audit_decision(SCENARIO, flags, evidence, [], [], units=[])
        check = next(row for row in result["checks"] if row["rule_id"] == "price_evidence")
        self.assertEqual(check["status"], "unknown")

    def test_price_review_schema_is_required_only_for_price_support(self):
        flags, documents, materials, units, evidence = fixture()
        evidence["activity_dates"] = {
            "contract": {"source_ids": [], "periods": [], "limitations": []},
            "settlement": {"source_ids": [], "periods": [], "limitations": []},
            "photos": [],
        }
        validate(evidence, audit_schema(SCENARIO))
        del evidence["price_photo_reviews"]
        with self.assertRaises(AuditError):
            validate(evidence, audit_schema(SCENARIO))
        other = unknown_checks("personnel_incentive", flags_for("personnel_incentive"))
        other["price_photo_reviews"] = []
        with self.assertRaises(AuditError):
            validate(other, audit_schema("personnel_incentive"))


class PricePhotoRecheckTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        from PIL import Image
        from audit_core.pdf_materials import PdfEvidenceProvider
        from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.provider = PdfEvidenceProvider(None, None, stage_summary_reviewer=isolated_stage_summary)
        self.case = {"scenario": SCENARIO, "archive_id": "synthetic", "flags": flags_for(SCENARIO),
                     "documents": [], "materials": [], "units": []}
        for index in range(20):
            uid = f"photo-{index:02d}"
            image = self.root / (uid + ".png")
            Image.new("RGB", (12, 12), "white").save(image)
            self.case["units"].append({"unit_id": uid, "image": image, "source_file": image.name,
                                       "locator": "原图"})
            self.case["documents"].append({"unit_id": uid, "roles": ["photo"],
                                           "facts": [f"本图{index}活动价与商品位置须查看原图"], "limitations": []})
        self.case["materials"] = [{"id": "price_evidence", "state": "present", "reason": "已收到照片",
                                   "source_ids": [u["unit_id"] for u in self.case["units"]]}]
        self.evidence = unknown_checks(SCENARIO, self.case["flags"])
        self.evidence["price_photo_reviews"] = [
            {"unit_id": u["unit_id"], "source_ids": [u["unit_id"]], "status": "pass",
             "reason": "本图商品活动价可读，水印清楚"} for u in self.case["units"]]

    def reply(self, uid, *, status="pass", correspondence="yes"):
        return {"review": {"unit_id": uid, "source_ids": [uid], "status": status,
                           "reason": "本图目标商品下方活动价19元可读，水印清楚"},
                "visual_evidence": {"watermark_readable": "yes", "activity_price_readable": "yes",
                                    "product_visible": "yes", "price_product_correspondence": correspondence,
                                    "activity_price": "19", "product_location": "左上目标商品箱体",
                                    "price_tag_location": "该箱体正下方价签"}}

    def run_recheck(self, respond):
        from unittest.mock import patch
        from audit_core.extraction_chain import render_prompt
        calls, observations = [], []
        def fake_call(root, skill, schema, prompt, **kwargs):
            uid = schema["properties"]["review"]["properties"]["unit_id"]["const"]
            calls.append({"uid": uid, "images": kwargs["images"], "prompt": render_prompt(prompt), "schema": deepcopy(schema)})
            value = respond(uid)
            kwargs["validator"](value)
            return value
        with patch.object(self.provider, "_call", side_effect=fake_call), patch(
                "audit_core.pdf_materials.save_model_observations", side_effect=lambda name, value: observations.append(deepcopy(value))):
            result = self.provider._recheck_unclear_price_photos(self.case, self.evidence, self.root)
        return result, calls, observations

    def test_all_pass_fail_and_watermark_unknown_do_not_call(self):
        self.evidence["price_photo_reviews"][0].update(status="fail", reason="本图价签被水印完全遮挡")
        self.evidence["price_photo_reviews"][1].update(status="unknown", reason="活动价清楚但水印日期模糊")
        result, calls, observations = self.run_recheck(lambda uid: self.fail("Must skip"))
        self.assertIs(result, self.evidence)
        self.assertEqual(calls, [])
        self.assertEqual(observations, [])

    def test_non_price_scenario_never_enters_price_recheck(self):
        self.case["scenario"] = "self_procured_gift_material"
        self.evidence["price_photo_reviews"][0].update(status="unknown", reason="活动价签数字看不清")
        result, calls, _ = self.run_recheck(lambda uid: self.fail("Must skip other scenarios"))
        self.assertIs(result, self.evidence)
        self.assertEqual(calls, [])

    def test_only_first_two_unknown_prices_and_one_image_per_chain(self):
        for index in range(3):
            self.evidence["price_photo_reviews"][index].update(status="unknown", reason="活动价签数字看不清")
        before = deepcopy(self.evidence)
        result, calls, observations = self.run_recheck(self.reply)
        self.assertEqual([c["uid"] for c in calls], ["photo-00", "photo-01"])
        self.assertEqual([len(c["images"]) for c in calls], [1, 1])
        self.assertEqual(result["price_photo_reviews"][2:], before["price_photo_reviews"][2:])
        self.assertEqual(result["checks"], before["checks"])
        self.assertEqual(self.evidence, before)
        self.assertEqual(observations[0]["logical_chains"], 2)
        self.assertNotIn("本图19活动价", calls[0]["prompt"])
        self.assertIn("不要求原价", calls[0]["prompt"])
        self.assertIn("其他品牌", calls[0]["prompt"])

    def test_one_correct_same_product_price_preserves_other_nineteen_and_checks(self):
        self.evidence["price_photo_reviews"][7].update(status="unknown", reason="无法确认活动价格")
        before = deepcopy(self.evidence)
        before_decision = audit_decision(SCENARIO, self.case["flags"], before, self.case["documents"],
                                         self.case["materials"], units=self.case["units"])
        result, calls, observations = self.run_recheck(self.reply)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result["price_photo_reviews"][7]["status"], "pass")
        self.assertEqual([r for i, r in enumerate(result["price_photo_reviews"]) if i != 7],
                         [r for i, r in enumerate(before["price_photo_reviews"]) if i != 7])
        self.assertEqual({k:v for k,v in result.items() if k != "price_photo_reviews"},
                         {k:v for k,v in before.items() if k != "price_photo_reviews"})
        after_decision = audit_decision(SCENARIO, self.case["flags"], result, self.case["documents"],
                                        self.case["materials"], units=self.case["units"])
        self.assertEqual([r for r in before_decision["checks"] if r["rule_id"] != "price_evidence"],
                         [r for r in after_decision["checks"] if r["rule_id"] != "price_evidence"])
        self.assertEqual(observations[0]["observations"][0]["before"], before["price_photo_reviews"][7])

    def test_other_brand_price_or_unclear_relation_cannot_pass(self):
        for correspondence in ("no", "unclear"):
            with self.subTest(correspondence=correspondence):
                self.evidence["price_photo_reviews"][0].update(status="unknown", reason="价格看不清")
                before = deepcopy(self.evidence)
                result, _, observations = self.run_recheck(lambda uid: self.reply(uid, correspondence=correspondence))
                self.assertEqual(result, before)
                self.assertEqual(observations[0]["observations"][0]["status"], "failed_retained_unknown")

    def test_unknown_remains_unknown_and_true_obstruction_fail_is_untouched(self):
        self.evidence["price_photo_reviews"][0].update(status="fail", reason="本图价签被完全遮挡")
        self.evidence["price_photo_reviews"][1].update(status="unknown", reason="活动价格无法辨认")
        original_fail = deepcopy(self.evidence["price_photo_reviews"][0])
        def reply(uid):
            value = self.reply(uid, status="unknown", correspondence="unclear")
            value["review"]["reason"] = "本图反光位置仍无法辨认活动价"
            value["visual_evidence"].update(activity_price_readable="unclear", activity_price=None)
            return value
        result, calls, _ = self.run_recheck(reply)
        self.assertEqual([c["uid"] for c in calls], ["photo-01"])
        self.assertEqual(result["price_photo_reviews"][0], original_fail)
        self.assertEqual(result["price_photo_reviews"][1]["status"], "unknown")

    def test_wrong_source_extra_field_and_missing_visual_proof_are_rejected(self):
        for fault in ("source", "extra", "proof", "watermark", "product"):
            with self.subTest(fault=fault):
                self.evidence["price_photo_reviews"][0].update(status="unknown", reason="活动价看不清")
                before = deepcopy(self.evidence)
                def reply(uid):
                    value = self.reply(uid)
                    if fault == "source": value["review"]["source_ids"] = ["photo-01"]
                    elif fault == "extra": value["checks"] = []
                    elif fault == "proof": value["visual_evidence"]["price_tag_location"] = None
                    elif fault == "watermark": value["visual_evidence"]["watermark_readable"] = "unclear"
                    else: value["visual_evidence"]["product_visible"] = "no"
                    return value
                result, _, observation = self.run_recheck(reply)
                self.assertEqual(result, before)
                self.assertEqual(observation[0]["observations"][0]["status"], "failed_retained_unknown")

    def test_transport_failure_retains_unknown_and_records_failure(self):
        self.evidence["price_photo_reviews"][0].update(status="unknown", reason="价签数字看不清")
        before = deepcopy(self.evidence)
        def failure(uid): raise TimeoutError("private message must not be copied")
        result, calls, observations = self.run_recheck(failure)
        self.assertEqual(result, before)
        self.assertEqual(len(calls), 1)
        row = observations[0]["observations"][0]
        self.assertEqual(row["error_type"], "TimeoutError")
        self.assertNotIn("private message", str(observations))

    def test_related_context_is_complete_without_size_caps_and_cancellation_is_not_swallowed(self):
        self.evidence["price_photo_reviews"][0].update(status="unknown", reason="价签数字看不清")
        self.case["documents"][0]["facts"].append("界" * 7000)
        def long_reply(uid):
            value = self.reply(uid)
            value['review']['reason'] = '本图目标商品下方活动价19元可读。' * 300
            value['visual_evidence']['product_location'] = '左上目标商品箱体。' * 40
            return value
        result, calls, observations = self.run_recheck(long_reply)
        self.assertFalse(observations[0]["observations"][0]["context_omitted"])
        self.assertIn("界" * 7000, calls[0]["prompt"])
        self.assertEqual(result['price_photo_reviews'][0]['status'], 'pass')
        self.assertGreater(len(result['price_photo_reviews'][0]['reason']), 800)
        def cancelled(uid): raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt): self.run_recheck(cancelled)

    def test_real_qwen_parser_retries_keep_usage_and_never_accept_unbound_price(self):
        import io
        import json
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from audit_core.model_metrics import collect_model_artifacts
        from audit_core.model_settings import api_selection
        from audit_core.pdf_materials import PdfEvidenceProvider
        from tests.pdf_test_support import SingleAgentEvidenceProvider as PdfEvidenceProvider
        self.provider.connection = api_selection({"provider": "ppio", "api_key": "sk_synthetic_not_sent"})
        self.evidence["price_photo_reviews"][0].update(status="unknown", reason="活动价签数字看不清")
        for correct in (False, True):
            with self.subTest(correct=correct):
                transport = []
                self.provider = PdfEvidenceProvider(None,None,connection=self.provider.connection,stage_summary_reviewer=isolated_stage_summary)
                def respond(selection, content, **kwargs):
                    transport.append(content)
                    value = self.reply("photo-00", correspondence="yes" if correct else "no")
                    return json.dumps(value, ensure_ascii=False), {"input_tokens": 12, "output_tokens": 5}
                run_root = self.root / ("corrected" if correct else "retained")
                run_root.mkdir()
                with collect_model_artifacts(run_root / "analysis"), redirect_stdout(io.StringIO()), patch(
                        "audit_core.qwen_model.complete_json", side_effect=respond), patch(
                        "audit_core.qwen_model.time.sleep"), patch(
                        "httpx.AsyncClient", side_effect=AssertionError("External model forbidden")):
                    result = self.provider._recheck_unclear_price_photos(self.case, self.evidence, run_root)
                self.assertEqual(len(transport), 1)
                self.assertTrue(all(sum(block.get("type") == "image_url" for block in content) == 1 for content in transport))
                metrics = [json.loads(line) for line in (run_root / "analysis/model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]
                self.assertEqual([m["status"] for m in metrics], ["success"])
                self.assertEqual(sum(m["usage"]["input_tokens"] for m in metrics), 12 * len(transport))
                self.assertEqual(result["price_photo_reviews"][0]["status"], "pass" if correct else "unknown")
                self.assertEqual(result["price_photo_reviews"][1:], self.evidence["price_photo_reviews"][1:])
                self.assertEqual(result["checks"], self.evidence["checks"])

    def test_normal_audit_leaves_price_uncertainty_to_model_self_check(self):
        from unittest.mock import patch
        self.evidence['price_photo_reviews'][3].update(status='unknown',reason='活动价签数字看不清')
        with patch.object(self.provider,'_call',return_value=deepcopy(self.evidence)) as model, \
             patch.object(self.provider,'_recheck_unclear_price_photos',side_effect=AssertionError('automatic correction')), \
             patch('audit_core.pdf_materials.prepare_product_references',return_value=({},[])), \
             patch('audit_core.pdf_materials.prepare_pos_identities',return_value={}):
            result=self.provider.audit(self.case,self.root)
        self.assertEqual(model.call_count,1)
        self.assertEqual(result,self.evidence)


if __name__ == "__main__":
    unittest.main()
