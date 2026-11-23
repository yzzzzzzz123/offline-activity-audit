"""Synthetic Qince responses only; no real external services or callbacks."""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from audit_core.common import AuditError
from audit_core.qince_context import (
    APPLICATIONS, PLANS, VERIFICATION, POLICY, QinceClient, context_sources, load_context,
)


def fixture():
    return {
        VERIFICATION: [{"verify_code": "HX_TEST", "creator_name": "不需要的员工", "details": [
            {"id": "V1", "activity_code": "SQ_TEST", "activity_cm_id": "9000000000000000001",
             "actual_cm_id": "C1", "actual_cm": "测试甲店", "capital_account_name": "不需要的账户"}]}],
        APPLICATIONS: [{"applyCode": "SQ_TEST", "planCode": "CX_TEST", "activityStatus": "0", "status": "0",
            "applicant": "不需要的员工", "subList": [
                {"subId": 9000000000000000001, "cusId": "C1", "cusName": "测试甲店", "startDate": "2026-10-01",
                 "endDate": "2026-10-10", "itemName": "地堆", "pdRangeType": "0", "applyAmount": "999999",
                 "dealerName": "本单费用代垫经销商", "dealerCode": "DEALER_TEST", "remark": "本次门店明细", "exeRequire": "1平米堆头"},
                {"subId": "OTHER", "cusId": "OTHER", "cusName": "其他申请行门店"}]}],
        PLANS: [{"code": "CX_TEST", "pd_range_type": "PRODUCT", "product_list": [
            {"id": "P1", "code": "SKU1", "name": "测试商品", "supplier": "不需要的供应商"}]}],
    }


class ContextTests(unittest.TestCase):
    def test_only_current_verification_and_exact_application_row_are_visible(self):
        raw = fixture()
        raw[VERIFICATION].append({"verify_code": "OTHER", "details": [{"activity_code": "SQ_OTHER", "activity_cm_id": "OTHER"}]})
        units, docs = context_sources(raw, "HX_TEST", "a001")
        self.assertEqual(len(units), 3)
        facts = json.dumps(docs, ensure_ascii=False)
        self.assertIn("测试甲店", facts)
        self.assertIn("测试商品", facts)
        self.assertIn('本单费用代垫经销商', facts)
        self.assertIn('dealerCode', facts)
        for unwanted in ("其他申请行门店", "不需要", "activityStatus", "supplier"):
            self.assertNotIn(unwanted, facts)
        self.assertTrue(all(u["source_kind"] == "qince" and u["image"] is None for u in units))
        self.assertEqual([u["unit_id"] for u in units], [d["unit_id"] for d in docs])

    def test_no_approval_or_deleted_status_becomes_an_extra_audit_gate(self):
        raw = fixture()
        original = deepcopy(raw)
        units, _ = context_sources(raw, "HX_TEST", "a001")
        self.assertEqual(len(units), 3)
        self.assertEqual(raw, original)

    def test_missing_optional_values_do_not_create_material_requirements(self):
        raw = fixture()
        row = raw[APPLICATIONS][0]["subList"][0]
        row.clear()
        row.update(subId="9000000000000000001", cusId="C1", cusName="测试甲店")
        units, docs = context_sources(raw, "HX_TEST", "a001")
        self.assertEqual(len(units), 2)
        self.assertNotIn("缺少", json.dumps(docs, ensure_ascii=False))

    def test_actual_enterprise_product_and_display_columns_are_selected_without_contact_fields(self):
        raw = fixture()
        raw[APPLICATIONS][0]["subList"][0]["extColumns"] = [
            {"ext_key": "活动SKU", "ext_col": "sku", "ext_value": "测试商品", "ext_value_code": "SKU1"},
            {"ext_key": "陈列、堆头类型", "ext_col": "display", "ext_value": "1平堆"},
            {"ext_key": "收货电话", "ext_value": "PRIVATE_PHONE"},
            {"ext_key": "预计POS数量", "ext_value": "PRIVATE_PLAN"},
        ]
        _, docs = context_sources(raw, "HX_TEST", "a001")
        text = json.dumps(docs, ensure_ascii=False)
        self.assertIn("1平堆", text)
        self.assertIn("活动SKU", text)
        self.assertNotIn("PRIVATE_PHONE", text)
        self.assertNotIn("PRIVATE_PLAN", text)

    def test_wrong_verification_or_row_does_not_borrow_other_customers(self):
        self.assertEqual(context_sources(fixture(), "HX_OTHER", "a001"), ([], []))
        raw = fixture()
        raw[VERIFICATION][0]["details"][0]["activity_cm_id"] = "MISSING"
        _, docs = context_sources(raw, "HX_TEST", "a001")
        self.assertNotIn("cusName", json.dumps(docs))
        self.assertNotIn("测试商品", json.dumps(docs, ensure_ascii=False))

    def test_envelope_and_string_response_data_are_supported(self):
        raw = {key: {"return_code": "0", "response_data": json.dumps(rows)} for key, rows in fixture().items()}
        self.assertEqual(context_sources(raw, "HX_TEST", "a001"), context_sources(fixture(), "HX_TEST", "a001"))

    def test_query_error_is_not_interpreted_as_empty_materials(self):
        raw = {VERIFICATION: {"return_code": "1", "return_msg": "secret raw diagnostic"}}
        with self.assertRaisesRegex(AuditError, "未成功") as caught:
            context_sources(raw, "HX_TEST", "a001")
        self.assertNotIn("secret", str(caught.exception))

    def test_conflicting_duplicate_application_lines_are_not_overwritten(self):
        raw = fixture()
        conflict = deepcopy(raw[APPLICATIONS][0]["subList"][0])
        conflict["cusName"] = "冲突的店"
        raw[APPLICATIONS][0]["subList"].append(conflict)
        with self.assertRaisesRegex(AuditError, "冲突"):
            context_sources(raw, "HX_TEST", "a001")

    def test_empty_results_remain_optional(self):
        self.assertEqual(context_sources({}, "HX_TEST", "a001"), ([], []))

    def test_contract_referenced_details_keep_exact_rows_and_do_not_borrow_other_applications(self):
        raw = fixture()
        raw[APPLICATIONS].append({"applyCode": "SQ_OTHER", "planCode": "CX_OTHER", "subList": [
            {"subId": "9000000000000000001", "cusName": "UNRELATED_TERMINAL", "pdRangeType": "0"}]})
        raw[PLANS].append({"code": "CX_OTHER", "product_list": [{"name": "UNRELATED_PRODUCT"}]})
        before = deepcopy(raw)
        units, documents = context_sources(raw, "HX_TEST", "a001")
        self.assertEqual([u["source_file"] for u in units], ["勤策核销明细", "勤策申请明细", "勤策活动方案"])
        self.assertEqual([u["unit_id"] for u in units], [d["unit_id"] for d in documents])
        self.assertIn("核销行V1", units[0]["locator"])
        self.assertIn("申请单SQ_TEST · 申请行9000000000000000001", units[1]["locator"])
        self.assertEqual(units[2]["locator"], "方案CX_TEST")
        self.assertNotIn("UNRELATED_", json.dumps(documents, ensure_ascii=False))
        self.assertEqual(raw, before)
        self.assertIn("不能仅以合同图未列附件为由报告缺件或疑点", POLICY)
        self.assertIn("对应勤策核销行/申请行/方案以及相关照片的unit_id", POLICY)
        self.assertIn("不能借用其他核销单、其他申请行或未关联方案补齐", POLICY)

    def test_customer_and_one_square_display_remain_reference_fields_not_observed_photo_facts(self):
        raw = fixture()
        row = raw[APPLICATIONS][0]["subList"][0]
        row["cusName"] = "合成经销商客户"
        row["extColumns"] = [{"ext_key": "陈列、堆头类型", "ext_value": "1平堆"}]
        units, documents = context_sources(raw, "HX_TEST", "a001")
        application = documents[1]
        self.assertIn('cusName="合成经销商客户"', application["facts"])
        self.assertTrue(any('"ext_value":"1平堆"' in fact for fact in application["facts"]))
        self.assertTrue(all(unit["image"] is None and unit["source_kind"] == "qince" for unit in units))
        self.assertTrue(all(document["roles"] == ["勤策本次业务补充明细（独立来源）"] for document in documents))
        self.assertFalse(any("照片实际面积" in fact or "终端门店=" in fact for fact in application["facts"]))
        self.assertIn("申请行的客户也不自动等于终端门店", POLICY)
        self.assertIn("只表示本次约定，不能证明照片实际面积已达到", POLICY)

    def test_missing_link_keeps_only_available_source_without_inventing_attachment_or_scope(self):
        raw = fixture()
        raw[VERIFICATION][0]["details"][0]["activity_cm_id"] = "MISSING"
        units, documents = context_sources(raw, "HX_TEST", "a001")
        self.assertEqual(len(units), 1)
        self.assertEqual(units[0]["source_file"], "勤策核销明细")
        self.assertFalse(any("planCode=" in fact or "cusName=" in fact or "product_list=" in fact
                             for document in documents for fact in document["facts"]))
        self.assertIn("勤策空字段或尚未形成的批复只留技术记录，不列客户问题", POLICY)
        self.assertIn("真实来源冲突须具体说明", POLICY)
        self.assertIn("不新增API必填项", POLICY)


class ClientTests(unittest.TestCase):
    def test_v2_signs_exact_utf8_body_and_uses_the_three_known_query_paths(self):
        client = QinceClient("1234567890123456789", "private-test-key")
        response = MagicMock()
        response.iter_bytes.return_value = [b'{"return_code":"0","response_data":[]}']
        transport = MagicMock()
        transport.stream.return_value.__enter__.return_value = response
        with patch("audit_core.qince_context.httpx.Client") as factory, patch(
                "audit_core.qince_context.time.time", return_value=1234), patch(
                "audit_core.qince_context.secrets.token_hex", return_value="abc123"):
            factory.return_value.__enter__.return_value = transport
            client.query(VERIFICATION, {"verify_code": "测试"})
            args, kwargs = transport.stream.call_args
            body = kwargs["content"].decode("utf-8")
            expected = hashlib.sha256(f"data={body}&appkey=private-test-key&nonce=abc123&timestamp=1234000".encode()).hexdigest()
            self.assertEqual(kwargs["headers"]["digest"], expected)
            self.assertEqual(kwargs["headers"]["encrypt-version"], "v2")
            self.assertIn("/queryVerificationRegular/1234567890123456789/1234000/abc123/", args[1])
            self.assertNotIn("private-test-key", args[1])
            self.assertFalse(factory.call_args.kwargs["trust_env"])
            self.assertFalse(factory.call_args.kwargs["follow_redirects"])

    def test_fetch_queries_only_linked_application_and_needed_scheme_without_date_window(self):
        raw = fixture()
        calls = []
        def pages(method, params):
            calls.append((method, params))
            return raw[method]
        with patch.object(QinceClient, "pages", side_effect=pages):
            result = QinceClient("1234567890123456789", "test").fetch("HX_TEST")
        self.assertEqual(calls, [(VERIFICATION, {"verify_code": "HX_TEST"}),
                                 (APPLICATIONS, {"activity_code": "SQ_TEST"}), (PLANS, {"plan_code": "CX_TEST"})])
        self.assertEqual(result, raw)

    def test_scheme_not_queried_when_application_has_its_own_product_scope(self):
        raw = fixture()
        raw[APPLICATIONS][0]["subList"][0]["pdRangeType"] = "2"
        with patch.object(QinceClient, "pages", side_effect=lambda method, params: raw[method]) as call:
            QinceClient("1234567890123456789", "test").fetch("HX_TEST")
        self.assertEqual(call.call_count, 2)

    def test_pagination_reads_all_pages_and_rejects_repeated_page(self):
        client = QinceClient("1234567890123456789", "test")
        first = [{"id": str(i)} for i in range(100)]
        with patch.object(client, "query", side_effect=[first, [{"id": "last"}]]) as call:
            self.assertEqual(len(client.pages(VERIFICATION, {"verify_code": "HX_TEST"})), 101)
            self.assertEqual(call.call_args.args[1]["page"], "2")
        with patch.object(client, "query", side_effect=[first, first]):
            with self.assertRaisesRegex(AuditError, "分页重复"):
                client.pages(APPLICATIONS, {})

    def test_absent_configuration_and_absent_identity_do_not_trigger_network(self):
        with tempfile.TemporaryDirectory() as directory, patch("audit_core.qince_context.PROJECT_ROOT", Path(directory)), patch.dict(os.environ, {}, clear=True), patch.object(QinceClient, "query", side_effect=AssertionError("no network")):
            self.assertIsNone(load_context("HX_TEST"))
            self.assertIsNone(load_context(None))

    def test_explicit_file_requires_identity_and_invalid_configuration_is_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "details.json"
            path.write_text(json.dumps(fixture()), encoding="utf-8")
            self.assertEqual(load_context("HX_TEST", path), fixture())
            with self.assertRaisesRegex(AuditError, "核销单号"):
                load_context(None, path)
        with patch.dict(os.environ, {"OFFLINE_AUDIT_QINCE_OPENID": "123"}, clear=True):
            with self.assertRaisesRegex(AuditError, "配置不完整"):
                load_context("HX_TEST")

    def test_private_json_host_is_used_without_exposing_credentials(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {}, clear=True):
            root = Path(directory)
            config = root / "config/qince.json"
            config.parent.mkdir()
            config.write_text(json.dumps({"host": "https://fx.canban.cn", "openid": "1234567890123456789", "appkey": "test-only"}), encoding="utf-8")
            config.chmod(0o600)
            with patch("audit_core.qince_context.PROJECT_ROOT", root):
                client = QinceClient.from_environment()
            self.assertEqual(client.host, "https://fx.canban.cn")
            self.assertNotIn("test-only", repr(client))


class WorkflowTests(unittest.TestCase):
    def test_injected_provider_never_uses_real_qince_configuration(self):
        from tests.pdf_test_support import PolicyProvider, bundle
        from audit_core.workbench_runtime import run_persistent_audit
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = bundle(root / "input")
            with patch.object(QinceClient, "from_environment", side_effect=AssertionError("fixture cannot load real credentials")):
                result = run_persistent_audit("20261103-isolated-qince", producer_model="ppio", input_dir=archive,
                    worktrees_root=root / "runs", evidence_provider=PolicyProvider(scenarios=("promotional_display",)),
                    biz_type="陈列堆头", verify_code="HX_TEST", input_source="oss")
            self.assertEqual(result["status"], "completed")

    def test_external_details_are_auditable_sources_but_do_not_change_zip_material_gate(self):
        from tests.pdf_test_support import PolicyProvider, bundle
        from audit_core.orchestrator import run_audit
        provider = PolicyProvider(scenarios=("promotional_display",))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = bundle(root / "input")
            result = run_audit("20261103-qince-fixture", producer_model="ppio", input_dir=archive,
                               output_dir=root / "output", evidence_provider=provider, biz_type="陈列堆头",
                               verify_code="HX_TEST", qince_data=fixture())
        gate, audit = provider.calls
        self.assertFalse(any(u.get("source_kind") == "qince" for u in gate["units"]))
        self.assertTrue(any(u.get("source_kind") == "qince" for u in audit["units"]))
        self.assertTrue(any("测试甲店" in str(d["facts"]) for d in audit["documents"]))
        self.assertEqual(result["scenarios"], ["promotional_display"])


if __name__ == "__main__":
    unittest.main()
