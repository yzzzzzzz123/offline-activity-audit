"""Extraction failures stay technical; exact source barcodes can be queried."""
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.audit_tools import dispatch_requests, execute_tool, tool_protocol
from audit_core.common import AuditError
from audit_core.model_authority import execution
from audit_core.model_metrics import collect_model_artifacts
from audit_core.pos_product_identity import lookup_pos_products, prepare_pos_identities
from audit_core.product_database import catalog_from_rows
from audit_core.tool_observations import observations


def fixture():
    document={'unit_id':'excel','facts':['Sheet!R1: 商品名称 | 国际条码',
        'Sheet!R2: 合成甲商品 | 6970356164296','Sheet!R3: 合成乙商品 | 6970356164289'], 'limitations':[]}
    rows=[{'fact_indexes':[i],'product_name':{'fact_index':i,'quote':name,'value':name},
           'barcode_69':{'fact_index':i,'quote':code,'value':code}}
          for i,name,code in [(1,'合成甲商品','6970356164296'),(2,'合成乙商品','6970356164289')]]
    case={'archive_id':'a001','scenario':'pos_target_incentive','flags':{'uses_pos':True},'units':[],
          'materials':[{'id':'pos_excel','state':'present','source_ids':['excel']}], 'documents':[document]}
    products=[{'unit_id':'excel',**row} for row in rows]
    catalog=catalog_from_rows([{'product_code':str(i),'product_name':'库内名称不同不影响准确条码',
                               'barcode_69':r['barcode_69']['value']} for i,r in enumerate(rows)])
    return case,products,catalog


class PosLookupRecoveryTests(TestCase):
    def test_chinese_metadata_kinds_do_not_discard_valid_rows_before_lookup(self):
        case,products,catalog=fixture();before=deepcopy(case)
        answer={'rows':[{k:v for k,v in p.items() if k!='unit_id'} for p in products],
                'other_facts':[{'fact_index':0,'kind':'表头','reason':'原表头'}]}
        with TemporaryDirectory() as folder,patch('audit_core.pos_product_identity.load_product_catalog',return_value=catalog) as db:
            result=prepare_pos_identities(case,Path(folder),lambda *a,**k:deepcopy(answer))
        db.assert_called_once()
        self.assertEqual([r['status'] for r in result['rows']],['pass','pass'])
        self.assertEqual(result['limitations'],[])
        self.assertEqual(case,before)

    def test_failed_extraction_records_actual_error_and_allows_source_bound_lookup(self):
        case,products,catalog=fixture();before=deepcopy(case)
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'):
            with patch('audit_core.pos_product_identity.load_product_catalog') as db:
                def broken(*args,**kwargs):raise AuditError('fixture: other_facts/0/kind invalid')
                partial=prepare_pos_identities(case,Path(folder),broken)
            db.assert_not_called()
            self.assertEqual(partial['limitations'],[])
            self.assertFalse(partial['complete'])
            self.assertFalse(partial['system_issues'][0]['database_queried'])
            self.assertFalse(partial['system_issues'][0]['customer_actionable'])
            self.assertIn('kind invalid',partial['system_issues'][0]['error'])
            request={'tool':'pos_product_lookup','rule_ids':['pos_product_identity'],'fields':[],
                     'source_ids':['excel'],'products':products}
            with execution(),patch('audit_core.pos_product_identity.load_product_catalog',return_value=catalog) as db:
                result=dispatch_requests(case,[request],['pos_product_identity'])[0]['result']
            db.assert_called_once()
            self.assertTrue(result['complete'])
            self.assertEqual([r['status'] for r in result['rows']],['pass','pass'])
            self.assertEqual(observations(case)[0]['result'],result)
            self.assertIn('pos_product_lookup',tool_protocol(['pos_product_identity']))
        self.assertEqual(case,before)

    def test_distinct_partial_lookups_of_same_sheet_keep_both_proofs(self):
        case,products,catalog=fixture()
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'),\
                patch('audit_core.pos_product_identity.load_product_catalog',return_value=catalog):
            for product in products:execute_tool(case,'pos_product_lookup',products=[product])
            proofs=[r for r in observations(case) if r['request']['tool']=='pos_product_lookup']
            self.assertEqual(len(proofs),2)
            self.assertEqual({r['result']['rows'][0]['source']['fact_indexes'][0] for r in proofs},{1,2})
            self.assertTrue(all(r['result']['coverage']=='requested_rows_only' for r in proofs))

    def test_foreign_or_truncated_barcode_is_rejected_before_database_access(self):
        case,products,_=fixture()
        variants=[]
        for change in ('foreign-source','short-code','borrowed-quote'):
            value=deepcopy(products[:1])
            if change=='foreign-source':value[0]['unit_id']='other-case'
            if change=='short-code':value[0]['barcode_69']['value']='697035616429'
            if change=='borrowed-quote':value[0]['barcode_69']['fact_index']=2
            variants.append(value)
        for value in variants:
            with self.subTest(value=value),patch('audit_core.pos_product_identity.load_product_catalog') as db:
                with self.assertRaises(AuditError):lookup_pos_products(case,value)
                db.assert_not_called()

    def test_database_failure_remains_system_failure_and_no_match_stays_distinct(self):
        case,products,catalog=fixture()
        with patch('audit_core.pos_product_identity.load_product_catalog',side_effect=AuditError('fixture DB offline')):
            result=execute_tool(case,'pos_product_lookup',products=products)
        self.assertEqual(result['status'],'model_takeover')
        self.assertFalse(result['complete'])
        self.assertNotIn('库内无参考商品',str(result))
        catalog['products']=catalog['products'][1:]
        with patch('audit_core.pos_product_identity.load_product_catalog',return_value=catalog):
            result=lookup_pos_products(case,products)
        self.assertEqual(result['rows'][0]['reason_code'],'catalog_product_missing')
        self.assertEqual(result['rows'][1]['status'],'pass')
