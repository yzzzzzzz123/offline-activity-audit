"""Source identities stay exact while names can be compared fuzzily elsewhere."""
from copy import deepcopy
import json
from unittest import TestCase
import jsonschema

from audit_core.display_source_evidence import (DisplaySourceError,ASSERTIONS_SCHEMA,
    bind_display_source_schema,validate_display_sources,display_source_options)
from audit_core.pdf_evidence import audit_schema,photo_review_schema
from tests.test_display_source_evidence import fixture,check,application_assertion


class DisplayFieldReferenceContractTests(TestCase):
    def case(self):
        docs,mats,units=fixture()
        return {'scenario':'promotional_display','documents':docs,'materials':mats,'units':units}

    def assertion_schema(self,case):
        schema=photo_review_schema(['p'],[d['unit_id'] for d in case['documents']])
        return bind_display_source_schema(schema,case)['properties']['photo_reviews']['items']['properties']['checks']['items']['properties']['source_assertions']

    def test_exact_projected_extension_item_and_full_original_field_are_both_supported(self):
        case=self.case();full=next(f for f in case['documents'][1]['facts'] if f.startswith('extColumns='))
        component=json.dumps(json.loads(full.split('=',1)[1])[0],ensure_ascii=False,separators=(',',':'))
        for quote in (full,component):
            assertion={'kind':'agreed_specification','statement':'申请规格为1平堆，照片边界仍未完整展示。',
                       'references':[{'unit_id':'a','field':'extColumns','quote':quote}]}
            value=check(assertion['statement'],[assertion]);before=deepcopy((case,value))
            jsonschema.validate([assertion],self.assertion_schema(case))
            validate_display_sources(value,case['documents'],case['materials'],case['units'],path='checks/0',strict_prose=False)
            self.assertEqual((case,value),before)
            self.assertEqual(value['status'],'unknown')

    def test_fuzzy_business_name_does_not_authorize_rewriting_qince_quote(self):
        case=self.case()
        for item in (case['documents'][1],case['units'][1]):
            key='facts' if 'facts' in item else 'native_facts'
            item[key]=[f.replace('甲店','华联四颗树店') for f in item[key]]
        good=application_assertion(case['documents']);good['statement']='照片写四棵树店，与系统四颗树店为近似写法。'
        bad=deepcopy(good);bad['references'][0]['quote']=bad['references'][0]['quote'].replace('四颗树','四棵树')
        schema=self.assertion_schema(case)
        jsonschema.validate([good],schema)
        validate_display_sources(check(good['statement'],[good]),case['documents'],case['materials'],case['units'],path='checks/0',strict_prose=False)
        with self.assertRaises(jsonschema.ValidationError):jsonschema.validate([bad],schema)
        with self.assertRaises(DisplaySourceError):validate_display_sources(check(bad['statement'],[bad]),case['documents'],case['materials'],case['units'],path='checks/0',strict_prose=False)

    def test_unit_field_and_quote_are_bound_together_and_other_verification_is_not_eligible(self):
        case=self.case();good=application_assertion(case['documents']);schema=self.assertion_schema(case)
        for key,value in [('unit_id','v'),('field','actual_cm'),('quote','cusName="其他店"')]:
            bad=deepcopy(good);bad['references'][0][key]=value
            with self.subTest(key=key),self.assertRaises(jsonschema.ValidationError):jsonschema.validate([bad],schema)
        case['units'][0]['native_facts'][0]='verify_code="OTHER"'
        case['documents'][0]['facts'][0]='verify_code="OTHER"'
        with self.assertRaises(jsonschema.ValidationError):jsonschema.validate([good],self.assertion_schema(case))

    def test_all_same_specification_rows_remain_distinct_not_one_representative(self):
        case=self.case()
        for number in range(2,25):
            uid=f'a{number}';v=f'v{number}'
            app=deepcopy(case['units'][1]);app['unit_id']=uid
            app['native_facts']=[f.replace('subId=123',f'subId={number}').replace('cusName="甲店"',f'cusName="门店{number}"') for f in app['native_facts']]
            ver=deepcopy(case['units'][0]);ver['unit_id']=v
            ver['native_facts']=[f.replace('activity_cm_id="123"',f'activity_cm_id="{number}"') for f in ver['native_facts']]
            case['units'] += [app,ver]
            case['documents'] += [{'unit_id':uid,'facts':deepcopy(app['native_facts'])}, {'unit_id':v,'facts':deepcopy(ver['native_facts'])}]
        schema=self.assertion_schema(case)
        branch=next(b for b in schema['items']['allOf'] if b['if']['properties']['kind']['const']=='agreed_specification')
        refs=branch['then']['properties']['references']['items']['enum']
        self.assertEqual({r['unit_id'] for r in refs},{'a',*[f'a{i}' for i in range(2,25)]})
        self.assertTrue(all(r['field']=='extColumns' for r in refs))

    def test_photo_and_final_generation_both_use_the_closed_field_contract(self):
        case=self.case()
        photo=bind_display_source_schema(photo_review_schema(['p'],['p','a','v','c']),case)
        final=audit_schema('promotional_display',materials=case['materials'],documents=case['documents'],units=case['units'])
        for schema in (photo,final):
            assertions=schema['properties']['photo_reviews']['items']['properties']['checks']['items']['properties']['source_assertions']
            self.assertTrue(assertions['items']['allOf'])
        subset=bind_display_source_schema(photo,case,['p','c'])
        assertions=subset['properties']['photo_reviews']['items']['properties']['checks']['items']['properties']['source_assertions']
        self.assertTrue(all(branch['then']['properties']['references']['items'] is False for branch in assertions['items']['allOf']))
