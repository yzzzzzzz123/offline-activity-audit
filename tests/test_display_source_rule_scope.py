"""Photo field choices must not override document-specific reference roles."""
from copy import deepcopy
from unittest import TestCase
import jsonschema

from audit_core.display_source_evidence import ASSERTIONS_SCHEMA, bind_display_source_schema
from audit_core.pdf_evidence import DISPLAY_PHOTO_RULES, photo_review_schema
from tests.test_display_source_evidence import fixture, application_assertion


class DisplaySourceRuleScopeTests(TestCase):
    def case(self):
        docs,mats,units=fixture()
        quote='dealerName="经销商甲有限公司"'
        docs[1]['facts'].append(quote)
        units[1]['native_facts'].append(quote)
        return {'scenario':'promotional_display','documents':docs,'materials':mats,'units':units}

    def checks_schema(self,case):
        base={'type':'object','properties':{'checks':{'type':'array','items':{
            'type':'object','required':['rule_id','source_assertions'],'properties':{
                'rule_id':{'type':'string'},'source_assertions':deepcopy(ASSERTIONS_SCHEMA)}}}}}
        return bind_display_source_schema(base,case)

    def assertion(self):
        return {'kind':'application_customer','statement':'本单经销商按原文核对。',
                'references':[{'unit_id':'a','field':'dealerName','quote':'dealerName="经销商甲有限公司"'}]}

    def test_dealer_and_document_date_assertions_keep_their_existing_generation_contract(self):
        case=self.case();schema=self.checks_schema(case)
        for rule in ('contract_dealer','settlement_dealer','application_activity_dates','contract_period'):
            with self.subTest(rule=rule):
                value={'checks':[{'rule_id':rule,'source_assertions':[self.assertion()]}]}
                before=deepcopy((case,value))
                jsonschema.validate(value,schema)
                self.assertEqual((case,value),before)

    def test_all_four_photo_rules_still_reject_dealer_as_store_customer(self):
        schema=self.checks_schema(self.case())
        for rule in DISPLAY_PHOTO_RULES:
            with self.subTest(rule=rule),self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate({'checks':[{'rule_id':rule,'source_assertions':[self.assertion()]}]},schema)

    def test_valid_photo_customer_and_period_source_remain_closed_to_actual_field(self):
        case=self.case();schema=self.checks_schema(case)
        good=application_assertion(case['documents'])
        value={'checks':[{'rule_id':'display_size','source_assertions':[good]}]}
        jsonschema.validate(value,schema)
        for changed in ({'field':'dealerName'},{'unit_id':'v'},{'quote':'cusName="改写门店"'}):
            invalid=deepcopy(value)
            invalid['checks'][0]['source_assertions'][0]['references'][0].update(changed)
            with self.subTest(changed=changed),self.assertRaises(jsonschema.ValidationError):
                jsonschema.validate(invalid,schema)

    def test_per_photo_contract_remains_strict_and_non_photo_constraint_is_not_mutated(self):
        case=self.case()
        schema=photo_review_schema(['p'],[d['unit_id'] for d in case['documents']])
        bound=bind_display_source_schema(schema,case)
        assertions=bound['properties']['photo_reviews']['items']['properties']['checks']['items']['properties']['source_assertions']
        with self.assertRaises(jsonschema.ValidationError):jsonschema.validate([self.assertion()],assertions)
        jsonschema.validate([application_assertion(case['documents'])],assertions)
        self.assertNotEqual(schema,bound)
        before=self.checks_schema(case)
        self.assertEqual(before['properties']['checks']['items']['properties']['source_assertions'],ASSERTIONS_SCHEMA)
