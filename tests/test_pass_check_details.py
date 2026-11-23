from copy import deepcopy
import unittest
from audit_core.pass_check_log import build_pass_check_log


class PassCheckDetailsTests(unittest.TestCase):
    def fixture(self):
        row={'status':'pass','rule_id':'pos_product_identity','check_category':'销售明细商品信息与商品库',
             'values':['POS.xlsx','已核对准确69码','名称必含，按原始69码查库'],'card_evidence':{'source_files':['POS.xlsx']}}
        sheet={'scenario':'personnel_incentive','projection_kind':'pdf_policy','archive_id':'a001','title':'人员激励','audit_type_label':'人员激励','rows':[row]}
        packet={'archive_id':'a001','result':{'scenario':'personnel_incentive'},'units':[{'unit_id':'a001-f001','source_file':'目录/POS.xlsx','locator':'销售表，第7行'}],
            '_presentation_observations':{'pos-product-identity':{'verification':{'applicable':True,'limitations':[], 'rows':[
                {'unit_id':'a001-f001','status':'pass','submitted':{'product_name':'经销商简称','barcode_69':'6900000000001'},
                 'references':[{'product_code':'P1','product_name':'正式商品','barcode_69':'6900000000001'}, {'product_code':'P2','product_name':'渠道商品','barcode_69':'6900000000001'}]}]}}}}
        return {'sheets':[sheet]},packet

    def test_exact_barcode_all_candidates_and_source_row_without_mutating_evidence(self):
        view,packet=self.fixture();before=deepcopy((view,packet))
        log=build_pass_check_log(view,{},material_packets=[packet]);item=log['groups'][0]['items'][0]
        self.assertIn('精确查询',item['verification_method']);self.assertEqual(item['requirement'],'名称必含，按原始69码查库')
        detail=item['verification_details'][0];self.assertIn('第7行',detail['subject']);self.assertIn('P1',detail['compared']);self.assertIn('P2',detail['compared'])
        self.assertEqual((view,packet),before)

    def test_wrong_archive_failed_lookup_or_conflicting_barcode_cannot_claim_db_query(self):
        for kind in ['archive','status','barcode','missing']:
            with self.subTest(kind=kind):
                view,packet=self.fixture();row=packet['_presentation_observations']['pos-product-identity']['verification']['rows'][0]
                if kind=='archive':packet['archive_id']='a002'
                elif kind=='status':row['status']='unknown'
                elif kind=='barcode':row['references'][0]['barcode_69']='6900000000002'
                else:packet.pop('_presentation_observations')
                item=build_pass_check_log(view,{},material_packets=[packet])['groups'][0]['items'][0]
                self.assertNotIn('verification_details',item)

    def test_passed_photo_subcheck_survives_other_failed_checks_on_same_image(self):
        row={'status':'issue','photo_unit_id':'a001-photo','rule_id':'display_photo_review','values':['照片.jpg','大小无法确认'],
             'card_evidence':{'source_files':['照片.jpg'],'sources':[{'file':'照片.jpg','locator':'原图1'}]}}
        sheet={'scenario':'promotional_display','projection_kind':'pdf_policy','archive_id':'a001','title':'陈列','audit_type_label':'陈列','rows':[row]}
        packet={'archive_id':'a001','units':[{'unit_id':'a001-photo','source_file':'照片.jpg','locator':'原图1'}],
            'result':{'scenario':'promotional_display','photo_reviews':[{'unit_id':'a001-photo','checks':[
                {'rule_id':'display_brand','status':'pass','reason':'现场清晰可见参半商品','requirement':'陈列参半商品'},
                {'rule_id':'display_size','status':'unknown','reason':'无法确认大小'}]}]}}
        view={'sheets':[sheet]};before=deepcopy(view);log=build_pass_check_log(view,{},material_packets=[packet])
        self.assertEqual(log['business_total'],1);item=log['groups'][0]['items'][0];self.assertEqual(item['control_id'],'display_brand')
        self.assertIn('其他不符合或无法确认',item['verification_method']);self.assertEqual(view,before)


if __name__=='__main__':unittest.main()
