"""Lossless visual delivery: all original pairs, scoped facts and paid checkpoints."""
from copy import deepcopy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from audit_core.product_reference_batches import bounded_product_references, compact_product_references
from audit_core.qwen_model import system_prompt
from audit_core.stage_context import encode_context

MODULE='audit_core.product_reference_batches'


class VisualPathEfficiencyTests(unittest.TestCase):
    def test_larger_groups_reduce_requests_and_repeat_images_without_losing_a_pair(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary); scenes=[]; refs=[]
            for i in range(26):
                path=root/f'scene-{i}.png';path.write_bytes(b's'*100)
                scenes.append({'unit_id':f'scene-{i}','image':path})
            for i in range(66):
                path=root/f'ref-{i}.png';path.write_bytes(b'r'*100)
                refs.append(path)
            metadata={'rule_ids':['entry_sku'],'references':[
                {'product_code':str(i),'image':p.name} for i,p in enumerate(refs)]}
            def run(width):
                calls=[];pairs=[]
                def model(root,skill,schema,prompt,**kwargs):
                    a=json.loads(prompt.partial_variables['scenes']);b=json.loads(prompt.partial_variables['references'])
                    pairs.extend((s['unit_id'],r['reference_id']) for s in a for r in b)
                    self.assertEqual(prompt.partial_variables['_audit_stage_context']['phase'],'transcription')
                    encode_context(prompt.partial_variables['_audit_stage_context'])
                    value={'scene_observations':[{'unit_id':s['unit_id'],'facts':['literal scene'],'limitations':[]} for s in a],
                           'reference_observations':[{'reference_id':r['reference_id'],'facts':['literal reference'],'limitations':[]} for r in b]}
                    kwargs['validator'](value);calls.append(len(kwargs['images']));return value
                with patch(MODULE+'.JOINT_SOURCE_BUDGET',width),patch(MODULE+'.IMAGE_REQUEST_BUDGET',10000),\
                        patch(MODULE+'.TEXT_HEADROOM',100),patch(MODULE+'.save_model_observations'):
                    bounded_product_references(root=root/f'batches-{width}',skill=root,scene_units=scenes,
                        fixed_images=[u['image'] for u in scenes],references=metadata,reference_images=refs,
                        call=model,label='isolated')
                self.assertEqual(len(pairs),26*66)
                self.assertEqual(len(set(pairs)),26*66)
                return calls
            old=run(12);new=run(32)
            self.assertLess(len(new),len(old)//2)
            self.assertLess(sum(new),sum(old))

    def test_fact_pool_expands_to_every_original_word_order_and_batch_scope(self):
        common='本图可见文字；69码6970356166450；金额1.2300'
        groups=[]
        for batch in range(15):
            groups.append({'batch':batch,'reference_only':True,'reference_manifest':[
                {'reference_id':f'ref-{batch%3}','image':f'reference-{batch%3}.png',
                 'metadata':[{'product_code':f'CP-{batch%3}'}]}],
                'scene_observations':[{'unit_id':'p1','facts':[common,common,'仅本批未见某款'],
                    'limitations':['不能由另一图补写']}],
                'reference_observations':[{'reference_id':f'ref-{batch%3}',
                    'facts':[common],'limitations':[]}]})
        original={'joint_visual_observations':groups,'references':[{'image':'original.png'}]}
        before=deepcopy(original);after=compact_product_references(original)
        pool=after['joint_visual_observations']
        for saved,scope in zip(groups,pool['batch_scopes']):
            self.assertEqual(scope['batch'],saved['batch'])
            manifests={r['reference_id']:r for r in pool['reference_manifest']}
            self.assertEqual([manifests[uid] for uid in scope['reference_manifest_ids']],saved['reference_manifest'])
            for field,key in (('scene_observations','unit_id'),('reference_observations','reference_id')):
                by_id={row[key]:row for row in pool[field]}
                expanded=[{key:row[key],**{name:[by_id[row[key]][name][i] for i in row[name+'_indexes']]
                    for name in ('facts','limitations')}} for row in scope[field]]
                self.assertEqual(expanded,saved[field])
        self.assertEqual(before,original)
        self.assertIs(compact_product_references(after),after)
        self.assertLess(len(json.dumps(after,ensure_ascii=False)),len(json.dumps(before,ensure_ascii=False)))

    def test_conflicting_reference_binding_cannot_be_compacted(self):
        from audit_core.common import AuditError
        group={'batch':1,'reference_only':True,'scene_observations':[],'reference_observations':[],
               'reference_manifest':[{'reference_id':'ref-1','image':'a.png'}]}
        other=deepcopy(group);other['batch']=2;other['reference_manifest'][0]['image']='b.png'
        with self.assertRaises(AuditError):
            compact_product_references({'joint_visual_observations':[group,other]})

    def test_source_only_unique_facts_do_not_gain_pool_index_overhead(self):
        value={'joint_visual_observations':[{'batch':1,'reference_only':True,'reference_manifest':[],
            'reference_observations':[], 'scene_observations':[
                {'unit_id':'scene','facts':['only once'],'limitations':[]}]}]}
        self.assertIs(compact_product_references(value),value)

    def test_visual_stage_system_does_not_include_other_business_guides(self):
        from audit_core.stage_context import reading_context
        with TemporaryDirectory() as temporary:
            skill=Path(temporary);(skill/'references').mkdir()
            for relative in ('SKILL.md','references/audit-rules.md','references/business-guide.md'):
                (skill/relative).write_text('UNRELATED_FULL_GUIDE'*500,encoding='utf-8')
            old=system_prompt(skill,'{}');new=system_prompt(skill,'{}',reading_context())
            self.assertNotIn('UNRELATED_FULL_GUIDE',new)
            self.assertLess(len(new.encode()),len(old.encode())//20)
            self.assertIn('原图',new)


if __name__=='__main__':unittest.main()
