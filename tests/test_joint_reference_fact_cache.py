"""Exact current-image fact reuse and closed shape correction, without real APIs."""
from contextlib import redirect_stdout
from collections import Counter
from copy import deepcopy
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.evidence_parser import EvidenceOutputParser
from audit_core.image_fact_cache import ImageFactCache
from audit_core.model_settings import ModelSelection
from audit_core.product_reference_batches import bounded_product_references, JOINT_TASK_CLARIFICATION, JOINT_SOURCE_BUDGET
from audit_core.qwen_model import extract_qwen_json

MODULE = 'audit_core.product_reference_batches'


class JointReferenceFactCacheTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.scene = self.root / 'scene.png'
        self.scene.write_bytes(b'scene' * 12)
        self.units = [{'unit_id': 'photo', 'image': self.scene, 'source_file': self.scene.name, 'locator': 'original'}]
        self.refs = []
        for index in range(4):
            path = self.root / f'ref-{index}.png'
            path.write_bytes(bytes([index]) * 400)
            self.refs.append(path)
        self.references = {'rule_ids': ['price_evidence'], 'references': [
            {'product_code': str(i), 'image': path.name} for i, path in enumerate(self.refs)]}
        self.identity = {'provider': 'ppio', 'model': 'mock-model', 'effort': 'default', 'endpoint': 'https://mock.invalid/v1'}
        self.calls = []

    def model(self, root, skill, schema, prompt, **kwargs):
        scenes = json.loads(prompt.partial_variables['scenes'])
        refs = json.loads(prompt.partial_variables['references'])
        value = {'scene_observations': [{'unit_id': r['unit_id'], 'facts': ['literal visible '+r['unit_id']], 'limitations': []} for r in scenes],
                 'reference_observations': [{'reference_id': r['reference_id'], 'facts': ['literal reference '+r['reference_id']], 'limitations': []} for r in refs]}
        self.calls.append({'schema': schema, 'prompt': prompt.format(), 'value': deepcopy(value)})
        kwargs['validator'](value)
        return value

    def invoke(self, **overrides):
        args = dict(root=self.root/'batches', skill=self.root, scene_units=self.units,
                    fixed_images=[self.scene], references=self.references, reference_images=self.refs,
                    call=self.model, label='isolated', model_identity=self.identity)
        args.update(overrides)
        with patch(MODULE+'.IMAGE_REQUEST_BUDGET', 2200), patch(MODULE+'.TEXT_HEADROOM', 100), \
                patch(MODULE+'.save_model_observations'):
            return bounded_product_references(**args)

    def test_all_current_sources_reuse_exact_facts_with_existing_cache_limits(self):
        before = {p: p.read_bytes() for p in [self.scene, *self.refs]}
        a, _ = self.invoke(fact_cache=ImageFactCache(self.root/'worktrees', owner='first'))
        sent = len(self.calls)
        self.assertGreater(sent, 1)
        self.assertTrue(all(c['prompt'].startswith(JOINT_TASK_CLARIFICATION) for c in self.calls))
        with patch('audit_core.execution_trace.record_step') as trace:
            b, _ = self.invoke(fact_cache=ImageFactCache(self.root/'worktrees', owner='second'))
        self.assertEqual(trace.call_count, sent)
        for call, original in zip(trace.call_args_list, self.calls):
            self.assertEqual(call.args[0], 'image.reference_facts.cache_hit')
            self.assertEqual(call.args[2]['fact_value_sha256'], hashlib.sha256(
                json.dumps(original['value'],ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest())
        self.assertEqual(len(self.calls), sent)
        self.assertEqual(a['joint_visual_observations'], b['joint_visual_observations'])
        self.assertEqual(b['image_delivery']['cached_fact_batches'], sent)
        self.assertEqual(b['image_delivery']['model_fact_batches'], 0)
        self.assertFalse(b['image_delivery']['audit_verdicts_reused'])
        self.assertEqual(before, {p: p.read_bytes() for p in before})

    def test_partial_failure_keeps_only_previously_validated_batch_facts(self):
        cache = ImageFactCache(self.root/'worktrees', owner='failed')
        count = 0
        def failing(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise AuditError('synthetic transport exhausted')
            return self.model(*args, **kwargs)
        with self.assertRaises(AuditError):
            self.invoke(fact_cache=cache, call=failing)
        sent = len(self.calls)
        result, _ = self.invoke(fact_cache=ImageFactCache(self.root/'worktrees', owner='retry'))
        self.assertEqual(result['image_delivery']['cached_fact_batches'], 1)
        self.assertEqual(len(self.calls)-sent, result['image_delivery']['batches']-1)

    def test_different_provider_model_endpoint_or_reasoning_does_not_hit(self):
        self.invoke(fact_cache=ImageFactCache(self.root/'worktrees', owner='base'))
        for field, value in (('provider','deepseek'), ('model','other-model'), ('endpoint','https://other.invalid/v1'), ('effort','high')):
            with self.subTest(field=field):
                result, _ = self.invoke(model_identity={**self.identity,field:value},
                    fact_cache=ImageFactCache(self.root/'worktrees', owner=field))
                self.assertEqual(result['image_delivery']['cached_fact_batches'], 0)

    def test_changed_image_source_binding_and_skill_are_cache_misses(self):
        self.invoke(fact_cache=ImageFactCache(self.root/'worktrees', owner='base'))
        for kind in ('image','source_id','skill'):
            with self.subTest(kind=kind):
                units = deepcopy(self.units)
                if kind == 'image': self.scene.write_bytes(b'other'*12)
                if kind == 'source_id': units[0]['unit_id']='another-photo'
                if kind == 'skill': (self.root/'SKILL.md').write_text('Different source-reading contract',encoding='utf-8')
                result, _ = self.invoke(scene_units=units,fact_cache=ImageFactCache(self.root/'worktrees', owner=kind))
                self.assertEqual(result['image_delivery']['cached_fact_batches'], 0)
                self.scene.write_bytes(b'scene'*12)
                (self.root/'SKILL.md').unlink(missing_ok=True)

    def test_same_image_bytes_with_distinct_source_ids_keep_distinct_saved_rows(self):
        second=self.root/'same-scene.png';second.write_bytes(self.scene.read_bytes())
        units=[*self.units,{'unit_id':'photo2','image':second,'source_file':second.name}]
        a,_=self.invoke(scene_units=units,fixed_images=[self.scene,second],fact_cache=ImageFactCache(self.root/'worktrees',owner='a'))
        sent=len(self.calls)
        b,_=self.invoke(scene_units=units,fixed_images=[self.scene,second],fact_cache=ImageFactCache(self.root/'worktrees',owner='b'))
        self.assertEqual(len(self.calls),sent)
        self.assertEqual(a['joint_visual_observations'],b['joint_visual_observations'])
        self.assertNotEqual(b['joint_visual_observations'][0]['scene_observations'][0]['facts'],b['joint_visual_observations'][0]['scene_observations'][1]['facts'])

    def test_final_audit_response_is_rejected_and_never_cached(self):
        def wrong(*args, **kwargs):return {'bizType':'entry_fee','gate':{},'checks':[]}
        with self.assertRaises(AuditError):
            self.invoke(call=wrong,fact_cache=ImageFactCache(self.root/'worktrees',owner='bad'))
        result,_=self.invoke(fact_cache=ImageFactCache(self.root/'worktrees',owner='good'))
        self.assertEqual(result['image_delivery']['cached_fact_batches'],0)

    def test_existing_small_cache_budget_is_respected_without_execution_failure(self):
        cache=ImageFactCache(self.root/'worktrees',owner='limited',max_entries=1,max_bytes=1024)
        result,_=self.invoke(fact_cache=cache)
        self.assertEqual(result['image_delivery']['model_fact_batches'],result['image_delivery']['batches'])
        self.assertLessEqual(len(cache.memory),1)
        self.assertLessEqual(cache.memory_bytes,cache.max_memory_bytes)
        self.assertLessEqual(len(list(cache.root.glob('*.json'))),1)

    def large_invoke(self, **overrides):
        units = []
        for i in range(20):
            image = self.root / f'large-scene-{i}.png'
            image.write_bytes(bytes([i]) * 5)
            units.append({'unit_id':f'scene-{i}', 'image':image, 'source_file':image.name})
        images = []
        for i in range(40):
            image = self.root / f'large-reference-{i}.png'
            image.write_bytes(bytes([i]) * 400)
            images.append(image)
        args = dict(root=self.root/'large-batches', skill=self.root, scene_units=units,
                    fixed_images=[u['image'] for u in units], reference_images=images,
                    references={'rule_ids':['price_evidence'], 'references':[
                        {'product_code':str(i), 'image':p.name} for i,p in enumerate(images)]},
                    call=self.model, label='isolated', model_identity=self.identity)
        args.update(overrides)
        with patch(MODULE+'.IMAGE_REQUEST_BUDGET',14000), patch(MODULE+'.TEXT_HEADROOM',100), \
                patch(MODULE+'.save_model_observations'):
            return bounded_product_references(**args)

    def test_uncached_groups_use_transport_budget_and_preserve_every_original_pair(self):
        result,_=self.large_invoke()
        pairs=Counter((s['unit_id'],r['reference_id']) for group in result['joint_visual_observations']
                      for s in group['scene_observations'] for r in group['reference_observations'])
        self.assertEqual(len(pairs),20*40)
        self.assertEqual(set(pairs.values()),{1})
        self.assertIsNone(JOINT_SOURCE_BUDGET)  # No arbitrary source-count cap.
        self.assertEqual(result['image_delivery']['batches'],len(self.calls))
        self.assertGreaterEqual(len(self.calls),result['image_delivery']['planned_parent_batches'])
        self.assertTrue(result['image_delivery']['original_bytes_unchanged'])

    def test_new_partition_keeps_validated_large_parent_and_splits_only_uncached_groups(self):
        calls=0
        def interrupted(*args,**kwargs):
            nonlocal calls
            calls+=1
            if calls==2:raise AuditError('synthetic later batch failed')
            return self.model(*args,**kwargs)
        with patch(MODULE+'.JOINT_SOURCE_BUDGET',1000), self.assertRaises(AuditError):
            self.large_invoke(fact_cache=ImageFactCache(self.root/'worktrees',owner='old'),call=interrupted)
        parent=deepcopy(self.calls[0]['value'])
        self.calls.clear()
        result,_=self.large_invoke(fact_cache=ImageFactCache(self.root/'worktrees',owner='retry'))
        first=result['joint_visual_observations'][0]
        self.assertEqual({k:first[k] for k in parent},parent)
        self.assertEqual(result['image_delivery']['cached_fact_batches'],1)
        pairs=Counter((s['unit_id'],r['reference_id']) for group in result['joint_visual_observations']
                      for s in group['scene_observations'] for r in group['reference_observations'])
        self.assertEqual(len(pairs),800)
        self.assertEqual(set(pairs.values()),{1})
        self.assertIsNone(JOINT_SOURCE_BUDGET)  # No arbitrary source-count cap.

    def test_both_providers_receive_closed_shape_hint_without_business_response_values(self):
        self.invoke()
        schema=self.calls[0]['schema'];good=self.calls[0]['value']
        path=self.root/'schema.json';path.write_text(json.dumps(schema),encoding='utf-8')
        bad={'bizType':'PRIVATE_BAD_RESPONSE','gate':{'missing':['PRIVATE_NO_AGREEMENT']},'checks':[]}
        parser=EvidenceOutputParser(schema_path=path)
        with self.assertRaises(AuditError):parser.parse(json.dumps(bad))
        hint=parser.last_retry_context
        self.assertIn('scene_observations',hint)
        self.assertIn('reference_observations',hint)
        self.assertIn('photo',hint)
        self.assertLessEqual(len(hint.encode('utf-8')),4096)
        self.assertNotIn('PRIVATE_',hint)
        for provider in ('ppio','deepseek'):
            prompts=[]
            def respond(selection, content, **kwargs):
                prompts.append(content[0]['text'])
                return json.dumps(bad if len(prompts)==1 else good), {'input_tokens':12,'output_tokens':5}
            with self.subTest(provider=provider), redirect_stdout(io.StringIO()), \
                    patch('audit_core.qwen_model.complete_json',side_effect=respond), \
                    patch('audit_core.qwen_model.time.sleep'), \
                    patch('httpx.AsyncClient',side_effect=AssertionError('Real APIs forbidden')):
                result=extract_qwen_json(connection=ModelSelection(provider,'mock-model','default','https://mock.invalid/v1','fake'),
                    root=self.root,skill=self.root,schema_path=path,prompt='Isolated visible facts only',
                    images=[],label='isolated shape repair',validator=None)
            self.assertEqual(result,good)
            self.assertEqual(len(prompts),2)
            self.assertIn(hint,prompts[1])
            self.assertNotIn('PRIVATE_',prompts[1])

    def test_chinese_root_response_gets_actual_array_shape_for_every_current_source(self):
        self.invoke()
        schema=self.calls[0]['schema']
        # Match the real failing batch size without copying its business facts.
        expected={'scene_observations':('unit_id',[f'scene-{i}' for i in range(7)]),
                  'reference_observations':('reference_id',[f'ref-{i}' for i in range(4)])}
        for field,(key,ids) in expected.items():
            array=schema['properties'][field]
            array['minItems']=array['maxItems']=len(ids)
            array['items']['properties'][key]['enum']=ids
        path=self.root/'schema.json';path.write_text(json.dumps(schema),encoding='utf-8')
        parser=EvidenceOutputParser(schema_path=path)
        with self.assertRaises(AuditError):
            parser.parse(json.dumps({'现场照片事实':['PRIVATE_BUSINESS_VALUE'],'商品参考图事实':[]}))
        hint=parser.last_retry_context
        shape=json.loads(hint.split('结构：',1)[1])
        self.assertEqual(set(shape),set(expected))
        for field,(key,ids) in expected.items():
            self.assertIsInstance(shape[field],list)
            self.assertEqual([row[key] for row in shape[field]],ids)
            self.assertTrue(all(row=={key:uid,'facts':[],'limitations':[]}
                                for row,uid in zip(shape[field],ids)))
        self.assertIn('不能照抄空值',hint)
        self.assertNotIn('PRIVATE_',hint)
        self.assertLessEqual(len(hint),1100)
        self.assertLessEqual(len(hint.encode('utf-8')),4096)


if __name__=='__main__':unittest.main()
