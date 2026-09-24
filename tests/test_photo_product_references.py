"""Contract-scoped DB/OSS retrieval, shared photo rules and evidence isolation."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.extraction_chain import render_prompt
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.photo_product_references import (
    PHOTO_CHECKS, photo_context, prepare_product_references, resolve_requests, validate_selection,
    validate_reference_reporting,
)
from audit_core.product_database import catalog_from_rows, product_catalog_scope
from audit_core.pdf_policy import ENTRY_SKU_CLARIFICATION, PRODUCT_PHOTO_CLARIFICATION
from tests.pdf_test_support import flags_for, unknown_checks
from tests.product_test_support import MemoryOSS, image_objects


MODULE = "audit_core.photo_product_references"


def catalog(count=3):
    result = catalog_from_rows([{"product_code": f"SKU-{i:04d}", "product_name": f"测试商品{i}规格100g",
                                "barcode_69": "6970356167341"} for i in range(count)])
    for product in result["products"]:
        product["views"] = [{"view_id": "front"}, {"view_id": "detail"}]
    return result


def field(value, quote=None, unit_id="contract"):
    return {"unit_id": unit_id, "quote": quote or value, "value": value}


def request(*, code=None, name=None, barcode=None, rule="entry_sku"):
    return {"rule_ids": [rule], "product_code": field(code) if code else None,
            "product_name": field(name) if name else None, "barcode_69": field(barcode) if barcode else None}


def case_for(root, scenario="entry_fee"):
    flags = flags_for(scenario)
    flags.update(temporary_staff=scenario == "personnel_incentive", photo_evidence=True, online=False,
                 full_reduction=True)
    contract_id = "entry_agreement" if scenario == "entry_fee" else "promotion_contract"
    photo_ids = sorted({mid for mids in PHOTO_CHECKS.get(scenario, {}).values() for mid in mids})
    return {"archive_id": "a001", "scenario": scenario, "flags": flags,
            "documents": [{"unit_id": "contract", "facts": ["商品编码：SKU-0001；名称：测试商品1规格100g；69码：6970356167341"]},
                          {"unit_id": "photo", "facts": ["本次现场照片，部分包装模糊"]},
                          {"unit_id": "settlement", "facts": ["经销商自报SKU-0002"]}],
            "materials": [{"id": contract_id, "state": "present", "source_ids": ["contract"]},
                          {"id": "settlement", "state": "present", "source_ids": ["settlement"]},
                          *[{"id": mid, "state": "present", "source_ids": ["photo"]} for mid in photo_ids]],
            "units": [{"unit_id": uid, "image": root / (uid + ".png")} for uid in ("contract", "photo", "settlement")]}


class ProductSelectionTests(unittest.TestCase):
    def test_direct_code_or_full_name_and_barcode_resolves_unique_code(self):
        c = catalog()
        requests = [request(code="SKU-0001"), request(name="测试商品1规格100g", barcode="6970356167341")]
        products, resolved = resolve_requests({"requests": requests}, c)
        self.assertEqual([p["product_code"] for p in products], ["SKU-0001"])
        self.assertEqual([r["status"] for r in resolved], ["resolved", "resolved"])

    def test_ambiguous_partial_and_conflicting_identifiers_never_expand_candidates(self):
        c = catalog()
        c["products"][2]["product_name"] = c["products"][1]["product_name"]
        requests = [request(name="测试商品1规格100g"), request(barcode="6970356167341"),
                    request(name="测试商品1规格100g", barcode="6970356167341"),
                    request(name="测试商品", barcode="6970356167341"),
                    request(code="SKU-0001", barcode="6970356167358"), request(code="absent")]
        selected, resolutions = resolve_requests({"requests": requests}, c)
        self.assertEqual(selected, [])
        self.assertTrue(all(r["status"] == "unresolved" for r in resolutions))

    def test_existing_identifiers_can_resolve_without_another_required_field(self):
        c = catalog(1)
        for req in (request(name=c["products"][0]["product_name"]),
                    request(barcode=c["products"][0]["barcode_69"])):
            with self.subTest(req=req):
                selected, resolutions = resolve_requests({"requests": [req]}, c)
                self.assertEqual([p["product_code"] for p in selected], ["SKU-0000"])
                self.assertEqual(resolutions[0]["status"], "resolved")

    def test_missing_product_wording_is_distinct_from_conflict_or_ambiguity(self):
        c = catalog()
        requests = [request(code="SKU-9999"), request(name="未登记商品", barcode="6900000000001"),
                    request(code="SKU-0001", barcode="6900000000001"), request(barcode="6970356167341")]
        _, resolutions = resolve_requests({"requests": requests}, c)
        self.assertEqual([r.get("reason_code") for r in resolutions],
                         ["catalog_product_missing", "catalog_product_missing", None, None])
        self.assertEqual(resolutions[0]["reason"], "库内无参考商品。")

    def test_missing_reference_is_reported_for_the_specific_product(self):
        flags = flags_for("entry_fee")
        value = unknown_checks("entry_fee", flags)
        _, resolutions = resolve_requests({"requests": [request(code="SKU-9999")]}, catalog())
        references = {"resolutions": resolutions}
        with self.assertRaises(AuditError):
            validate_reference_reporting(value, references)
        check = next(c for c in value["checks"] if c["rule_id"] == "entry_sku")
        check.update(reason="SKU-9999：库内无参考商品。", status="unknown")
        validate_reference_reporting(value, references)
        from audit_core.customer_language import customer_reason
        self.assertEqual(customer_reason(check, []), "SKU-9999：库内无参考商品。")
        check["status"] = "pass"
        with self.assertRaises(AuditError):
            validate_reference_reporting(value, references)

    def test_selection_rejects_other_sources_and_truncated_codes(self):
        docs = [{"unit_id": "contract", "facts": ["编号SKU-0001-X；编码SKU-0002"]}]
        for value in (request(code="invented"), request(code="SKU-0001"), request(code="SKU-0002")):
            if value["product_code"]["value"] == "SKU-0002":
                value["product_code"]["unit_id"] = "settlement"
            # Preserve full surrounding quote so a partial identifier cannot hide
            # the suffix by submitting a shorter quote.
            value["product_code"]["quote"] = docs[0]["facts"][0]
            with self.subTest(value=value), self.assertRaises(AuditError):
                validate_selection({"requests": [value], "limitations": []}, docs, ["entry_sku"])

    def test_no_photo_rule_or_inactive_branch_does_not_touch_database_or_oss(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cases = [case_for(root, s) for s in ("poster_material", "pos_target_incentive", "personnel_incentive", "giveaway_promotion")]
            cases[2]["flags"]["temporary_staff"] = False
            cases[3]["flags"]["photo_evidence"] = False
            with patch(MODULE + ".load_product_catalog") as db, patch(MODULE + ".attach_product_reference_images") as oss:
                for case in cases:
                    self.assertEqual(photo_context(case)[1], [])
                    meta, images = prepare_product_references(case, root, lambda *a, **k: self.fail("unexpected model call"))
                    self.assertEqual(images, [])
                db.assert_not_called()
                oss.assert_not_called()


class ProductRetrievalTests(unittest.TestCase):
    def test_thousand_product_catalog_reads_only_selected_manifests_and_unique_images(self):
        c = catalog(1200)
        objects = image_objects(c)
        client = MemoryOSS(objects)
        selection = {"requests": [request(code="SKU-0001"), request(code="SKU-0701"),
                                   request(code="SKU-0001")], "limitations": []}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = case_for(root)
            case["documents"][0]["facts"].append("乙店及丙店商品编码：SKU-0701；SKU-0001")
            original = deepcopy(case)
            def select(*args, **kwargs):
                prompt = render_prompt(args[3])
                self.assertNotIn("经销商自报", prompt)
                self.assertNotIn("SKU-1199", prompt)
                kwargs["validator"](selection)
                return selection
            with product_catalog_scope(), patch(MODULE + ".load_product_catalog", return_value=c), \
                    patch("audit_core.product_image_runtime.ProductOSS", return_value=client):
                meta, images = prepare_product_references(case, root, select)
            manifests = [key for key in client.calls if key.endswith("manifest.json")]
            expected = [c["products"][i]["image_manifest_key"] for i in (1, 701)]
            self.assertCountEqual(manifests, expected)
            self.assertEqual(len(client.calls), len(set(client.calls)))
            self.assertEqual(len(images), 1)  # fixture views share identical bytes
            self.assertTrue(all(path.is_file() for path in images))
            self.assertEqual({r["product_code"] for r in meta["references"]}, {"SKU-0001", "SKU-0701"})
            self.assertEqual(case, original)

    def test_ambiguous_name_with_or_without_barcode_downloads_no_manifest(self):
        c = catalog()
        c["products"][2]["product_name"] = c["products"][1]["product_name"]
        for req, db_calls in ((request(name="测试商品1规格100g", barcode="6970356167341"), 1),
                              (request(name="测试商品1规格100g"), 1)):
            with self.subTest(req=req), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                selection = {"requests": [req], "limitations": []}
                with patch(MODULE + ".load_product_catalog", return_value=c) as db, \
                        patch(MODULE + ".attach_product_reference_images") as oss:
                    meta, images = prepare_product_references(case_for(root), root, lambda *a, **k: selection)
                self.assertEqual(db.call_count, db_calls)
                oss.assert_not_called()
                self.assertEqual(images, [])
                self.assertEqual(meta["resolutions"][0]["status"], "unresolved")

    def test_abbreviated_name_is_checked_against_text_before_only_its_images_are_read(self):
        c = catalog()
        client = MemoryOSS(image_objects(c))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = case_for(root)
            case["documents"][0]["facts"] = ["商品简称：测试商品1；69码：6970356167341"]
            selection = {"requests": [request(name="测试商品1", barcode="6970356167341")], "limitations": []}
            calls = []
            def model(*args, **kwargs):
                calls.append(args[2])
                if "requests" in args[2]["properties"]:
                    return selection
                self.assertEqual(client.calls, [])
                self.assertEqual(kwargs["images"], [])
                prompt = render_prompt(args[3])
                self.assertIn("不要求名称逐字相同", prompt)
                self.assertIn("本批只有一个候选也不代表必然匹配", prompt)
                return {"compatible_product_codes": ["SKU-0001"], "reason": "简称及条码仅对应候选商品1"}
            with product_catalog_scope(), patch(MODULE + ".load_product_catalog", return_value=c), \
                    patch("audit_core.product_image_runtime.ProductOSS", return_value=client):
                meta, images = prepare_product_references(case, root, model)
            self.assertEqual(len(calls), 2)
            self.assertEqual(meta["resolutions"][0]["product_code"], "SKU-0001")
            self.assertTrue(images)
            self.assertEqual([key for key in client.calls if key.endswith("manifest.json")],
                             [c["products"][1]["image_manifest_key"]])

    def test_later_text_batch_can_disprove_uniqueness_without_any_oss_download(self):
        c = catalog(450)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            case = case_for(root)
            case["documents"][0]["facts"] = ["商品名称：待确认简称"]
            selection = {"requests": [request(name="待确认简称")], "limitations": []}
            batches = []
            def model(*args, **kwargs):
                if "requests" in args[2]["properties"]:
                    return selection
                candidates = args[2]["properties"]["compatible_product_codes"]["items"]["enum"]
                batches.append(candidates)
                self.assertEqual(kwargs["images"], [])
                codes = [p for p in ("SKU-0001", "SKU-0401") if p in candidates]
                return {"compatible_product_codes": codes, "reason": "保留本批全部可能商品"}
            with patch(MODULE + ".load_product_catalog", return_value=c), \
                    patch(MODULE + ".attach_product_reference_images") as oss:
                meta, images = prepare_product_references(case, root, model)
            self.assertEqual([len(b) for b in batches], [200, 200, 50])
            self.assertEqual(images, [])
            self.assertEqual(meta["resolutions"][0]["status"], "unresolved")
            self.assertNotIn("reason_code", meta["resolutions"][0])
            oss.assert_not_called()

    def test_name_match_cannot_select_a_code_outside_the_barcode_candidates(self):
        c = catalog(1)
        selection = {"requests": [request(name="简称", barcode="6970356167341")]}
        with self.assertRaises(AuditError):
            resolve_requests(selection, c, lambda request, candidates: ["invented"])

    def test_single_barcode_candidate_with_conflicting_name_is_not_auto_matched(self):
        c = catalog(1)
        selection = {"requests": [request(name="不同规格的商品", barcode="6970356167341")]}
        selected, resolutions = resolve_requests(selection, c, lambda request, candidates: [])
        self.assertEqual(selected, [])
        self.assertEqual(resolutions[0]["status"], "unresolved")
        self.assertNotIn("reason_code", resolutions[0])

    def test_all_six_existing_photo_paths_receive_only_current_scope_references(self):
        for scenario in PHOTO_CHECKS:
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                case = case_for(root, scenario)
                c = catalog()
                client = MemoryOSS(image_objects(c))
                original = deepcopy(case)
                output = unknown_checks(scenario, case["flags"])
                calls = []
                def model(*args, **kwargs):
                    prompt = render_prompt(args[3])
                    calls.append((prompt, kwargs["images"]))
                    if "requests" in args[2]["properties"]:
                        value = {"requests": [request(code="SKU-0001", rule=photo_context(case)[0][0])], "limitations": []}
                    else:
                        value = output
                    kwargs["validator"](value)
                    return value
                provider = PdfEvidenceProvider("gpt-6-astra", "medium")
                with patch.object(provider, "_call", side_effect=model), \
                        patch(MODULE + ".load_product_catalog", return_value=c), \
                        patch("audit_core.product_image_runtime.ProductOSS", return_value=client):
                    provider.audit(case, root)
                self.assertEqual(len(calls), 2)
                prompt, images = calls[-1]
                self.assertIn(PRODUCT_PHOTO_CLARIFICATION, prompt)
                self.assertIn(root / "photo.png", images)
                self.assertEqual(sum(p.name.startswith("product-reference-") for p in images), 1)
                self.assertIn('"product_code": "SKU-0001"', prompt)
                self.assertNotIn('"product_code": "SKU-0002"', prompt)
                if scenario == "entry_fee":
                    self.assertIn(ENTRY_SKU_CLARIFICATION, prompt)
                self.assertEqual(case, original)

    def test_database_failure_never_falls_back_to_full_gallery(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            selection = {"requests": [request(code="SKU-0001")], "limitations": []}
            with patch(MODULE + ".load_product_catalog", side_effect=AuditError("数据库无法连接")), \
                    patch(MODULE + ".attach_product_reference_images") as oss, self.assertRaises(AuditError):
                prepare_product_references(case_for(root), root, lambda *a, **k: selection)
            oss.assert_not_called()

    def test_reference_images_cannot_become_current_photo_evidence(self):
        from audit_core.pdf_evidence import audit_decision
        case = case_for(Path("/unused"))
        value = unknown_checks("entry_fee", case["flags"])
        check = next(c for c in value["checks"] if c["rule_id"] == "entry_sku")
        check.update(status="pass", source_ids=["product-reference-123.png"], reason="参考图有该商品")
        with self.assertRaises(AuditError):
            audit_decision("entry_fee", case["flags"], value, case["documents"], case["materials"])


if __name__ == "__main__":
    unittest.main()
