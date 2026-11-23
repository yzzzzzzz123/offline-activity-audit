"""Confirmed field integration with synthetic records and isolated local images."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock
import shutil
import subprocess

from audit_core.common import AuditError
from audit_core.qince_context import (QinceClient, context_sources, entry_context_sources,
                                      _linked_applications, VERIFICATION, APPLICATIONS, business_columns)
from audit_core.qince_references import *
from tests.test_qince_context import fixture


def references():
    raw = fixture()
    row = raw[APPLICATIONS][0]['subList'][0]
    row.update(cusCode='STORE1', pdCodes='SKU1', pdIds='P1')
    raw[VERIFICATION][0]['details'][0].update(customer_id='C1', customer_code='STORE1')
    raw[CUSTOMERS] = [{'id':'C1', 'cm_code':'STORE1', 'cm_name':'本单甲店', 'cm_addr':'甲路1号',
        'cm_mss_city':'甲市', 'cm_status':'1', 'cm_approval_status':'1',
        'deliverys':[{'cm_delivery_addr':'仓库地址', 'cm_delivery_phone':'PRIVATE_PHONE'}],
        'linkmans':[{'cm_linkman_mobile':'PRIVATE_PHONE'}], 'exts':[{'cm_ext_key':'unknown','cm_ext_value':'PRIVATE_DYNAMIC'}]}]
    raw[PRODUCTS] = [{'prd_waiqin365_id':'P1','prd_code':'SKU1','prd_name':'本单商品',
        'prd_barcode':'6900000000001','prd_units':[{'prd_unit_waiqin365_id':'U1','prd_unit_name':'盒',
        'prd_barcode':'6900000000002','prd_is_base':'0','prd_ratio':'12.50000000','length':'99'}]}]
    raw[SKU_CODES] = [{'productId':'P1','productCode':'SKU1','skuCode':'AI1','unitId':'U1',
        'unitName':'盒','bjpType':'0','lengthWidthAndHeight':'5,5,20'}]
    raw[PRIMITIVE_REPORT] = [{'bizId':'B1','customCode':'STORE1','imageArea':'1.00','displayType':'1',
        'position':'经理','agentJudgeResult':'existing-reference','createTime':'2026-10-02 10:00:00'}]
    raw[PRIMITIVE_DETAIL] = [{'bizId':'B1','images':[{'imageId':'I1','imageUrl':'https://image.qince.com/original.png'}],
        'products':[{'productId':'P1','productNum':'12','layers':'1,2,3'}]}]
    raw[ORIGINAL] = [{'imageId':'I1','products':[{'productId':'P1','productNum':'9'}]}]
    raw[CHECKED_DETAIL] = [{'bizId':'B1','images':[{'imageId':'I1'}],
        'products':[{'productId':'P1','productNum':'10','changeType':'3'}], 'approvalStatus':'99'}]
    raw[COMPARE] = [{'productDetailList':[{'bizId':'B1','productId':'P1','originalCount':9,'totalCount':10}]}]
    raw[ACTUAL] = [{'master_id':'R1','cm_code':'STORE1','product_code':'SKU1','biz_id':'B1,B2', 'row_num':'3'}]
    raw[RESULTS] = [{'setting_id':'S1','report_id':'R1','compare_id':'CMP1','cm_code':'STORE1',
                     'must_products':[{'target_id':'P1','row_num':'3','actual_row_num':'2'}]}]
    raw[STANDARDS] = [{'master_id':'S1','cus_range_type':'1','cus_range_id':'CR1','start_date':'2026-10-01',
        'end_date':'2026-10-10','must_products':[{'target_id':'P1','condition':'1'}]}]
    return raw


class FieldProjectionTests(unittest.TestCase):
    def test_real_workflow_passes_new_independent_references_to_existing_audit(self):
        from tests.pdf_test_support import PolicyProvider, bundle, image_bytes
        from audit_core.orchestrator import run_audit
        provider=PolicyProvider(scenarios=('promotional_display',))
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);archive=bundle(root/'input')
            with patch('audit_core.qince_references.photo_digest',return_value=hashlib.sha256(image_bytes()).hexdigest()):
                result=run_audit('20261106-qince-references',producer_model='ppio',input_dir=archive,
                    output_dir=root/'output',evidence_provider=provider,biz_type='陈列堆头',
                    verify_code='HX_TEST',qince_data=references())
        audit=provider.calls[-1]
        self.assertTrue(any(u['source_file']=='勤策终端主档' for u in audit['units']))
        self.assertTrue(any(u['source_file']=='勤策本图识别与校对参照' for u in audit['units']))
        self.assertEqual(result['scenarios'],['promotional_display'])

    def test_current_master_and_packaging_are_independent_sources_with_exact_ids(self):
        raw = references(); before = deepcopy(raw)
        units, docs = context_sources(raw,'HX_TEST','a')
        data = json.dumps(docs,ensure_ascii=False)
        for value in ('甲路1号','6900000000001','6900000000002','12.50000000','5,5,20','unitId'):
            self.assertIn(value,data)
        for value in ('PRIVATE_PHONE','PRIVATE_DYNAMIC','"length"','经理','existing-reference'):
            self.assertNotIn(value,data)
        self.assertTrue(all(u['source_kind']=='qince' and u['image'] is None for u in units))
        self.assertEqual(raw,before)
        self.assertIn('申请行',units[1]['locator'])

    def test_same_name_and_wrong_stable_id_or_code_cannot_borrow_terminal(self):
        for field in ('id','cm_code'):
            raw=references();raw[CUSTOMERS][0][field]='OTHER'
            self.assertNotIn('甲路1号',json.dumps(context_sources(raw,'HX_TEST','a'),ensure_ascii=False))

    def test_wrong_verification_does_not_receive_any_reference(self):
        self.assertEqual(context_sources(references(),'OTHER','a'),([],[]))

    def test_conflicting_verification_customer_blocks_all_extra_references(self):
        raw=references();raw[VERIFICATION][0]['details'][0]['customer_id']='DIFFERENT'
        units,docs=context_sources(raw,'HX_TEST','a')
        self.assertFalse(any(u.get('qince_reference') for u in units))

    def test_customer_missing_type_does_not_assert_terminal_status(self):
        units,docs=context_sources(references(),'HX_TEST','a')
        doc=next(d for u,d in zip(units,docs) if u['source_file']=='勤策终端主档')
        self.assertIn('不自动证明',''.join(doc['limitations']))
        self.assertNotIn('执行终端=',str(doc['facts']))

    def test_other_skus_and_customers_are_excluded_even_from_offline_data(self):
        raw=references();raw[PRODUCTS].append({'prd_code':'OTHER','prd_name':'UNRELATED'})
        raw[SKU_CODES].append({'productId':'OTHER','productCode':'OTHER','skuCode':'UNRELATED'})
        raw[CUSTOMERS].append({'id':'OTHER','cm_name':'UNRELATED'})
        self.assertNotIn('UNRELATED',json.dumps(context_sources(raw,'HX_TEST','a'),ensure_ascii=False))

    def test_terminal_status_is_not_audit_input_but_endpoint_provenance_is_kept(self):
        raw=references();raw[CUSTOMERS_V1]=raw.pop(CUSTOMERS)
        data=json.dumps(context_sources(raw,'HX_TEST','a'),ensure_ascii=False)
        self.assertIn('/api/customer/v1/queryCustomer',data)
        self.assertNotIn('cm_approval_status',data)
        self.assertNotIn('cm_status',data)

    def test_known_extension_ids_work_after_label_change_and_preserve_explicit_empty_value(self):
        row={'extColumns':[{'ext_col':'ext_2412110257','ext_key':'changed','ext_value_code':'SKU1'},
                           {'ext_col':'ext_2412110258','ext_key':'changed','ext_value':''}]}
        selected=business_columns(row)
        self.assertEqual(len(selected),2)
        self.assertEqual(selected[1]['ext_value'],'')

    def test_compact_entry_scope_keeps_new_master_sources(self):
        raw=references();r=raw[APPLICATIONS][0]['subList'][0]
        r.update(cusName='测试甲店',pdNames='本单商品')
        raw[VERIFICATION][0]['details'][0]['customer_name']='测试甲店'
        units,docs=entry_context_sources(raw,'HX_TEST','a')
        self.assertEqual(units[0]['source_file'],'勤策进场关联明细')
        self.assertTrue(any(u['source_file']=='勤策终端主档' for u in units))
        self.assertEqual(len({u['unit_id'] for u in units}),len(units))


class TransportAndFetchTests(unittest.TestCase):
    def test_runtime_load_requests_references_without_loading_a_second_connection(self):
        from audit_core.qince_context import load_context
        with patch.object(QinceClient,'from_environment') as factory:
            factory.return_value.fetch.return_value=references()
            self.assertEqual(load_context('HX_TEST'),references())
            factory.return_value.fetch.assert_called_once_with('HX_TEST',include_references=True)

    def test_object_and_string_envelopes_preserve_twenty_digit_ids_and_decimal_text(self):
        data=response_rows({'return_code':'0','response_data':'{"total":1,"rows":[{"bizId":90000000000000000001,"imageArea":1.23000000}]} '})
        self.assertEqual(data[0],{'bizId':'90000000000000000001','imageArea':'1.23000000'})
        self.assertEqual(data.total,1)

    def test_missing_rows_invalid_total_and_floating_id_fail_instead_of_empty_success(self):
        for data in ({'total':0},{'rows':[],'total':True},{'rows':[{'bizId':1.1}]}):
            with self.subTest(data=data), self.assertRaises(AuditError):response_rows(data)

    def test_pages_follow_documented_family_size_and_detect_incomplete_total(self):
        client=QinceClient('1234567890123456789','test')
        first=PageRows([{'bizId':str(i)} for i in range(100)],101)
        last=PageRows([{'bizId':'last'}],101)
        with patch.object(client,'query',side_effect=[first,last]) as query:
            self.assertEqual(len(client.pages(PRIMITIVE_REPORT,{'startDate':'2026-10-01'})),101)
            self.assertEqual(query.call_args.args[1]['page'],'2')
            self.assertEqual(query.call_args.args[1]['rows'],'100')
        with patch.object(client,'query',return_value=PageRows([{'bizId':'1'}],2)):
            with self.assertRaisesRegex(AuditError,'完整'):client.pages(PRIMITIVE_REPORT,{})
        with patch.object(client,'query',side_effect=[PageRows(first,200),PageRows(last,101)]):
            with self.assertRaisesRegex(AuditError,'发生变化'):client.pages(PRIMITIVE_REPORT,{})

    def test_fixed_page_size_and_unpaged_sku_code_do_not_send_fake_parameters(self):
        client=QinceClient('1234567890123456789','test')
        for method,page in ((CUSTOMERS,'page_number'),(ORIGINAL,'page'),(PICTURES,'page')):
            with patch.object(client,'query',return_value=[]) as query:
                client.pages(method,{'filter':'x'})
                self.assertEqual(query.call_args.args[1],{'filter':'x',page:'1'})
        with patch.object(client,'query',return_value=[]) as query:
            client.pages(SKU_CODES,{})
            query.assert_called_once_with(SKU_CODES,{})

    def test_fetch_is_scoped_by_customer_product_and_dates_and_keeps_every_original_record(self):
        raw=references(); client=MagicMock()
        client.pages.side_effect=lambda method,params:raw.get(method,[])
        linked=_linked_applications(raw[VERIFICATION],raw[APPLICATIONS])
        data=fetch_references(client,{k:raw[k] for k in (VERIFICATION,APPLICATIONS,'queryRegularSale')},'HX_TEST',linked,
                              needs={'terminals','products','dimensions','historical_photos'})
        calls=client.pages.call_args_list
        terminal=next(c.args[1] for c in calls if c.args[0]==CUSTOMERS)
        self.assertEqual(terminal['id'],'C1');self.assertIn('sorts',terminal);self.assertIn('query_group',terminal)
        self.assertEqual(data[CHECKED_DETAIL][0]['products'][0]['productNum'],'10')
        self.assertEqual(data[PRIMITIVE_DETAIL][0]['products'][0]['productNum'],'12')
        for call in calls:
            method,params=call.args
            if method in (PRIMITIVE_REPORT,PRIMITIVE_DETAIL,CHECKED_REPORT,CHECKED_DETAIL):
                self.assertEqual(params['startDate'],'2026-10-01');self.assertEqual(params['endDate'],'2026-10-10')
        self.assertTrue(all('query' in REFERENCE_ENDPOINTS[c.args[0]] for c in calls))

    def test_unavailable_optional_interface_is_recorded_without_customer_missing_material(self):
        raw=references();client=MagicMock();client.pages.side_effect=AuditError('Unavailable')
        linked=_linked_applications(raw[VERIFICATION],raw[APPLICATIONS])
        data=fetch_references(client,raw,'HX_TEST',linked,needs={'terminals'})
        self.assertTrue(data['reference_status'])
        self.assertTrue(all(r['status']=='query_failed' for r in data['reference_status']))

    def test_no_link_or_customer_conflict_causes_no_reference_network(self):
        raw=references();raw[VERIFICATION][0]['details'][0]['customer_id']='OTHER'
        client=MagicMock(); linked=_linked_applications(raw[VERIFICATION],raw[APPLICATIONS])
        fetch_references(client,raw,'HX_TEST',linked)
        client.pages.assert_not_called()

    def test_query_uses_new_path_and_retains_signed_utf8_transport(self):
        client=QinceClient('1234567890123456789','test')
        response=MagicMock();response.iter_bytes.return_value=[b'{"return_code":"0","response_data":{"rows":[],"total":0}}']
        transport=MagicMock();transport.stream.return_value.__enter__.return_value=response
        with patch('audit_core.qince_context.httpx.Client') as factory:
            factory.return_value.__enter__.return_value=transport
            self.assertEqual(client.query(PRIMITIVE_REPORT,{'startDate':'2026-10-01'}),[])
        self.assertIn('/api/ai/v2/queryPrimitiveAiSkuReport/',transport.stream.call_args.args[1])
        self.assertIn('digest',transport.stream.call_args.kwargs['headers'])


class PhotoBindingTests(unittest.TestCase):
    def run_photo(self,raw,digest):
        with tempfile.TemporaryDirectory() as directory:
            image=Path(directory)/'photo.png';image.write_bytes(b'current-original-photo')
            unit={'unit_id':'current-photo','image':str(image)}
            with patch('audit_core.qince_references.photo_digest',return_value=digest):
                return photo_reference_sources(raw,'HX_TEST','a',[unit])

    def test_same_store_same_filename_or_wrong_bytes_never_attach_existing_result(self):
        self.assertEqual(self.run_photo(references(),'different'),([],[]))

    def test_matching_original_bytes_keep_image_unit_business_quantity_and_both_versions(self):
        units,docs=self.run_photo(references(),hashlib.sha256(b'current-original-photo').hexdigest())
        self.assertEqual(len(units),1)
        data=json.dumps(docs,ensure_ascii=False)
        for value in ('current-photo','I1','B1','productNum','12','9','10','changeType','CMP1','S1','cus_range_id'):
            self.assertIn(value,data)
        self.assertNotIn('经理',data)
        self.assertIn('业务级数量不能分摊',''.join(docs[0]['limitations']))

    def test_unrelated_image_business_customer_or_rule_is_not_attached(self):
        raw=references()
        raw[CHECKED_DETAIL].append({'bizId':'OTHER','products':[{'productName':'UNRELATED'}]})
        raw[ORIGINAL].append({'imageId':'OTHER','products':[{'productName':'UNRELATED'}]})
        raw[STANDARDS].append({'master_id':'OTHER','must_products':[{'target_id':'UNRELATED'}]})
        self.assertNotIn('UNRELATED',json.dumps(self.run_photo(raw,hashlib.sha256(b'current-original-photo').hexdigest())))

    def test_offline_json_does_not_retrieve_any_remote_picture(self):
        with patch('audit_core.qince_references.httpx.Client',side_effect=AssertionError('offline network')):
            self.assertIsNone(photo_digest(references(),'https://image.qince.com/x.png'))

    def test_optional_api_errors_do_not_turn_into_missing_materials(self):
        raw=references();raw[CUSTOMERS]={'return_code':'205','return_msg':'sensitive raw message'}
        units,docs=context_sources(raw,'HX_TEST','a')
        self.assertFalse(any(u['source_file']=='勤策终端主档' for u in units))
        self.assertNotIn('sensitive',json.dumps(docs))

    def test_signed_image_urls_do_not_leak_signature_into_model_sources(self):
        raw=references();raw[PRIMITIVE_DETAIL][0]['images'][0]['imageUrl']+='?signature=PRIVATE_SIGNATURE'
        _,docs=self.run_photo(raw,hashlib.sha256(b'current-original-photo').hexdigest())
        self.assertNotIn('PRIVATE_SIGNATURE',json.dumps(docs))

    def test_same_enterprise_image_port_is_supported_without_changing_service_config(self):
        raw=LiveReferences({},'https://company.test')
        response=MagicMock();response.status_code=200;response.headers={'content-type':'image/png'}
        response.iter_bytes.return_value=[b'original']
        transport=MagicMock();transport.stream.return_value.__enter__.return_value=response
        with patch('audit_core.qince_references.httpx.Client') as factory:
            factory.return_value.__enter__.return_value=transport
            self.assertEqual(photo_digest(raw,'https://company.test:8020/x.png'),hashlib.sha256(b'original').hexdigest())

    def test_image_reads_reject_http_credentials_ip_and_unapproved_hosts(self):
        for url in ('http://image.qince.com/x','https://evil.test/x','https://127.0.0.1/x',
                    'https://user:password@image.qince.com/x','https://image.qince.com:8080/x',
                    'https://image.qince.com:invalid/x'):
            with self.subTest(url=url),patch('audit_core.qince_references.httpx.Client',side_effect=AssertionError('invalid image network')):
                self.assertIsNone(photo_digest(LiveReferences({}),url))

    def test_live_image_digest_is_computed_from_actual_returned_bytes_once(self):
        raw=LiveReferences({});response=MagicMock();response.status_code=200
        response.headers={'content-type':'image/png'};response.iter_bytes.return_value=[b'original']
        transport=MagicMock();transport.stream.return_value.__enter__.return_value=response
        with patch('audit_core.qince_references.httpx.Client') as factory:
            factory.return_value.__enter__.return_value=transport
            first=photo_digest(raw,'https://image.qince.com/x.png')
            self.assertEqual(first,hashlib.sha256(b'original').hexdigest())
            self.assertEqual(photo_digest(raw,'https://image.qince.com/x.png'),first)
            self.assertEqual(factory.call_count,1)


@unittest.skipUnless(shutil.which('node'),'Node.js is required for frontend data projection')
class FrontendFieldTests(unittest.TestCase):
    def test_actual_frontend_labels_nested_reference_fields_and_preserves_before_after(self):
        html=(Path(__file__).resolve().parents[1]/'skills/orchestrate-offline-audit/assets/offline-activity-audit.html').read_text(encoding='utf-8')
        script=html.split('      const customerFields = ',1)[1].split('      const customerChecks = ',1)[0]
        script='const customerFields = '+script
        sample={'queryCustomerV2':[{'id':'90000000000000000001','cm_addr':'甲路1号'}],
            'queryProductV2':[{'prd_units':[{'prd_unit_name':'盒','prd_ratio':'12.5000'}]}],
            'original_photo':[{'imageId':'I1','products':[{'productNum':'9'}]}],
            'checked_details':[{'products':[{'productNum':'10','layers':'1,2,3'}]}],
            'imageIds':['90000000000000000001','90000000000000000002']}
        result=subprocess.run([shutil.which('node'),'-e',script+'console.log(JSON.stringify(customerBusinessRows('+json.dumps(sample,ensure_ascii=False)+')));'],
            capture_output=True,text=True,encoding='utf-8',check=True,timeout=15)
        rows=json.loads(result.stdout)
        self.assertTrue(any(r[1]=='详细地址' and r[2]=='甲路1号' for r in rows))
        self.assertTrue(any(r[1]=='包装换算比' and r[2]=='12.5000' for r in rows))
        self.assertTrue(any('逐图原始识别' in r[0] and r[2]=='9' for r in rows))
        self.assertTrue(any('校对后业务明细' in r[0] and r[2]=='10' for r in rows))
        self.assertTrue(any(r[1]=='该业务全部照片编号' and '90000000000000000002' in r[2] for r in rows))


if __name__=='__main__':unittest.main()
