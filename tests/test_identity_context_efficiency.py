"""Keep every source while sharing repeated text in identity preparation."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from audit_core.photo_product_references import prepare_product_references, _name_matcher
from audit_core.model_self_check import review_stage
from audit_core.model_metrics import collect_model_artifacts
from tests.pdf_test_support import flags_for
from tests.test_final_logic_stage import fixture


def corpus():
    return [{'unit_id': f'q{i}', 'roles': ['本单精确关联明细'], 'limitations': [],
        'facts': ['本单完整关联事实及原字段：' + '保留商品规格及数量，不以名称相似替代身份。' * 12,
                  f'原始行={i}；商品SKU={i % 7}；原始数量=4']} for i in range(300)]


def expanded(value):
    if isinstance(value, list):
        return value
    fragments = value['fragments']
    def fact(item):
        if isinstance(item, int): return fragments[item]
        if isinstance(item, list):
            return fragments[item[0]] + str(item[1]) + ': ' + ' | '.join(fragments[i] for i in item[2])
        return item
    return [{**value['document_metadata'][d['metadata']], 'unit_id': d['unit_id'],
             'facts': [fact(f) for f in d['facts']]} for d in value['documents']]


class IdentityContextEfficiencyTests(TestCase):
    def test_product_scope_shares_repetitions_without_losing_any_source_or_quote(self):
        with TemporaryDirectory() as folder:
            documents = corpus(); original = deepcopy(documents)
            case = {'archive_id':'a001','scenario':'entry_fee','flags':flags_for('entry_fee'),
                'materials':[{'id':'entry_agreement','source_ids':['q0']},
                             {'id':'shelf_photos','source_ids':['photo']}], 'documents':documents,
                'units':[{'unit_id':d['unit_id'],'source_kind':'qince'} for d in documents]
                    + [{'unit_id':'photo','image':Path(folder)/'original.png'}]}
            def call(root,skill,schema,prompt,**kwargs):
                wire=json.loads(prompt.partial_variables['documents'])
                self.assertEqual(expanded(wire),original)
                self.assertLess(len(json.dumps(wire,ensure_ascii=False)),len(json.dumps(original,ensure_ascii=False))//2)
                self.assertEqual(prompt.partial_variables['_audit_stage_context']['max_output_tokens'],None)
                self.assertEqual(kwargs['images'],[])
                return {'requests':[],'limitations':['本单原始商品标识确实不能确定。']}
            with patch('audit_core.photo_product_references.load_product_catalog',side_effect=AssertionError('no lookup')):
                result,images=prepare_product_references(case,Path(folder),call)
            self.assertEqual(documents,original)
            self.assertEqual(images,[])
            self.assertEqual(result['resolutions'],[])

    def test_name_matching_keeps_every_lightweight_candidate_in_its_actual_task_context(self):
        candidates=[{'product_code':str(i),'product_name':f'商品{i}','barcode_69':str(6900000000000+i)} for i in range(201)]
        request={'product_code':None,'product_name':{'unit_id':'q0','quote':'商品简称','value':'商品简称'},'barcode_69':None}
        seen=[]
        def call(root,skill,schema,prompt,**kwargs):
            seen.extend(json.loads(prompt.partial_variables['candidates']))
            self.assertEqual(prompt.partial_variables['_audit_stage_context']['rules'],[])
            self.assertEqual(kwargs['images'],[])
            return {'compatible_product_codes':['0'] if len(seen)==200 else [],'reason':'按实际规格逐项核对。'}
        with TemporaryDirectory() as folder:
            match=_name_matcher({'archive_id':'a001','scenario':'entry_fee'},Path(folder),call)
            self.assertEqual(match(request,candidates),['0'])
            self.assertEqual(match(request,candidates),['0'])
        self.assertEqual(seen,candidates)

    def test_self_check_sends_a_lossless_corpus_but_keeps_unmodified_observations(self):
        case,packet,provider=fixture()
        documents=corpus();case['documents']=documents
        check=next(c for c in packet['audit_evidence']['checks'] if c['rule_id']=='contract_signed')
        value={'checks':[deepcopy(check)]}
        def call(root,skill,schema,prompt,**kwargs):
            wire=json.loads(prompt.partial_variables['request'])
            self.assertEqual(expanded(wire['documents']),documents)
            self.assertEqual(len(expanded(wire['documents'])),300)
            return {'ready':True,'checks':value['checks'],'notes':[]}
        with TemporaryDirectory() as folder,collect_model_artifacts(Path(folder)/'analysis'), \
             patch('audit_core.audit_self_check.repair_documents',return_value=documents), \
             patch.object(provider,'_call',side_effect=call):
            result=review_stage(provider,case,value,value,Path(folder)/'one-stage',['contract_signed'],lambda v,ids:v)
            saved=json.loads((Path(folder)/'analysis/model-observations/model-self-check-input-one-stage.json').read_text('utf-8'))
        self.assertEqual(saved['documents'],documents)
        self.assertEqual(result,value)
