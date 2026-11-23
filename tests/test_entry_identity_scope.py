"""API product codes resolve identity without inventing application barcodes."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.qince_amount_basis import approval_scope_flags
from audit_core.model_metrics import collect_model_artifacts
from audit_core.tool_observations import observations,remember,identity_support
from audit_core.product_reference_batches import product_identity_support
from audit_core.final_logic_review import context
from audit_core.model_self_check import review_stage
from tests.test_final_logic_stage import fixture


def api_case(barcode=None):
    body={'linked_application_rows':[{'verification_row_id':'v1','activity_code':'SQ-ONE',
        'activity_cm_id':'row1','活动SKU':[{'ext_key':'活动SKU','ext_value':'商品甲',
        'ext_value_name':'商品甲','ext_value_code':'SKU-A'}]}]}
    raw=json.dumps(body,ensure_ascii=False,separators=(',',':'))
    facts=['linked_customer='+raw]
    if barcode:facts.append('barcode='+json.dumps(barcode))
    return {'archive_id':'a001','scenario':'entry_fee','flags':{},
        'documents':[{'unit_id':'api-application','facts':facts,'roles':[],'limitations':[]}],
        'units':[{'unit_id':'api-application','source_kind':'qince','source_file':'勤策进场关联明细'}],
        'materials':[{'id':key,'state':'present','source_ids':['api-application']}
                     for key in ('activity_application','entry_agreement')]}


def flags(case):
    return approval_scope_flags(case['flags'],case['documents'],case['materials'],case['units'])


def identity_proofs(root,case):
    quote='"ext_value":"商品甲","ext_value_name":"商品甲","ext_value_code":"SKU-A"'
    request={'rule_ids':['entry_sku'],'product_code':{'unit_id':'api-application','quote':quote,'value':'SKU-A'},
        'product_name':{'unit_id':'api-application','quote':quote,'value':'商品甲'},'barcode_69':None}
    product={'product_code':'SKU-A','product_name':'商品甲','barcode_69':'6970000000001'}
    saved={'reference_only':True,'rule_ids':['entry_sku'],'references':[product],
        'resolutions':[{'request':request,'status':'resolved','product_code':'SKU-A'}],'limitations':[]}
    folder=root/'model-observations';folder.mkdir(parents=True,exist_ok=True)
    selection=folder/'photo-product-selection-a001.json';database=folder/'photo-product-database-a001.json'
    selection.write_text(json.dumps(saved,ensure_ascii=False),encoding='utf-8')
    database.write_text(json.dumps({'selected_products':[product]},ensure_ascii=False),encoding='utf-8')
    return selection,database,saved


class EntryIdentityScopeTests(TestCase):
    def test_linked_api_rows_marked_as_application_are_not_independent_originals(self):
        case=api_case();before=deepcopy(case)
        result=flags(case)
        self.assertFalse(result['_reference_available']['entry_application_products'])
        self.assertFalse(result['_reference_available']['application_products'])
        self.assertEqual(case,before)

    def test_actual_application_barcode_remains_a_comparison_reference(self):
        self.assertTrue(flags(api_case('6970000000001'))['_reference_available']['entry_application_products'])

    def test_a_separately_submitted_application_original_remains_usable(self):
        case=api_case()
        case['units'].append({'unit_id':'original','source_kind':'image','source_file':'申请原件.jpg'})
        case['documents'].append({'unit_id':'original','facts':['本次申请原件的商品条形码及门店约定。'],'roles':[]})
        case['materials'][0]['source_ids'].append('original')
        self.assertTrue(flags(case)['_reference_available']['entry_application_products'])

    def test_old_database_match_is_rebuilt_from_source_bound_requests_without_network(self):
        case=api_case()
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            selection,database,saved=identity_proofs(Path(folder)/'analysis',case)
            before=(selection.read_bytes(),database.read_bytes())
            with patch('httpx.Client',side_effect=AssertionError('no network')):
                proof=next(r for r in observations(case) if r['request']['tool']=='product_identity')
            self.assertTrue(proof['revalidated_against_current_sources'])
            self.assertEqual(proof['result']['identities'][0]['barcode_69'],'6970000000001')
            self.assertIsNone(proof['result']['resolutions'][0]['request']['barcode_69'])
            self.assertEqual(before,(selection.read_bytes(),database.read_bytes()))

    def test_source_changes_or_a_conflicting_database_identity_invalidate_old_match(self):
        for changed in ('source','database'):
            with self.subTest(changed=changed),TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
                case=api_case();_,database,_=identity_proofs(Path(folder)/'analysis',case)
                if changed=='source':case['documents'][0]['facts'][0]=case['documents'][0]['facts'][0].replace('SKU-A','SKU-B')
                else:
                    value=json.loads(database.read_text('utf-8'));value['selected_products'][0]['barcode_69']='6970000000002'
                    database.write_text(json.dumps(value),encoding='utf-8')
                self.assertFalse(any(r['request']['tool']=='product_identity' for r in observations(case)))

    def test_identity_support_survives_case_recreation_in_the_same_run(self):
        case=api_case();reference={'rule_ids':['entry_sku'],'references':[{
            'product_code':'SKU-A','product_name':'商品甲','barcode_69':'6970000000001'}],'resolutions':[]}
        support=product_identity_support(reference)
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            remember(case,'product_identity',fields=[],code=None,source_ids=[],result=support)
            restored=deepcopy(case)
            self.assertNotIn('_product_identity_support',restored)
            self.assertEqual(identity_support(restored,observations(restored)),support)

    def test_final_review_and_targeted_check_get_identity_and_correct_optional_scope(self):
        api=api_case()
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            _,_,provider=fixture('entry_fee');case,packet,_=fixture('entry_fee')
            case.update(documents=api['documents'],units=api['units'],materials=api['materials'])
            case['flags']=flags(case)
            packet.update(documents=case['documents'],units=case['units'])
            packet['classification'].update(flags=case['flags'],materials=case['materials'])
            for check in packet['audit_evidence']['checks']:check['source_ids']=[]
            check=next(c for c in packet['audit_evidence']['checks'] if c['rule_id']=='entry_application_products')
            check.update(status='unknown',reason='没有申请条形码，无法核对69码。',source_ids=['api-application'])
            packet['result']={'checks':packet['audit_evidence']['checks']}
            provider._audit_cases={}
            identity_proofs(Path(folder)/'analysis',case)
            request,_,_=context([packet],provider)
            self.assertEqual(len(request['archives'][0]['product_identity_support']['identities']),1)
            target=next(r for r in request['archives'][0]['rules']['rules'] if r['id']=='entry_application_products')
            self.assertFalse(target['applicable'])
            def model(root,skill,schema,prompt,**kwargs):
                payload=json.loads(prompt.partial_variables['request'])
                self.assertEqual(len(payload['product_identity_support']['identities']),1)
                rule=prompt.partial_variables['_audit_stage_context']['rules'][0]
                self.assertFalse(rule['applicable'])
                return {'ready':True,'notes':[],'checks':[{**check,'status':'not_applicable',
                    'reason':'独立申请条码参照未提供，不新增对齐；商品身份已由内部编码对应商品库。'}]}
            with patch.object(provider,'_call',side_effect=model):
                result=review_stage(provider,case,{'checks':[check]},{'checks':[check]},Path(folder)/'target',
                    ['entry_application_products'],lambda value,ids:{'checks':value['checks']})
            self.assertEqual(result['checks'][0]['status'],'not_applicable')
            self.assertEqual(check['status'],'unknown')
