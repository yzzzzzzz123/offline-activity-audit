"""Resume matching is request-scoped, never a display-label lookup."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from audit_core.common import AuditError
from audit_core.failed_upload_resume import ResumeEvidenceProvider
from audit_core.resume_request_identity import current_request, request_identity
from tests.test_failed_upload_resume import CONNECTION, SCHEMA, request, result, pending, trace_prefix


class ResumeRequestIdentityTests(unittest.TestCase):
    def provider(self, pairs, connection=CONNECTION):
        return ResumeEvidenceProvider(connection,trace_prefix()+pairs+pending())

    def call(self, provider, text, schema=SCHEMA, images=None, validator=lambda _:None, label='同名提取'):
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',
                   side_effect=AssertionError('No real model allowed')):
            return provider._call(Path('fresh-temp'),Path('skill'),schema,text,
                                  images=images or [],label=label,validator=validator)

    def test_same_label_different_units_and_segments_keep_all_saved_answers(self):
        texts=['unit=a table=1 facts=["name A"]','unit=b table=1 facts=["name B"]',
               'unit=a table=1 segment=2 facts=["name C"]']
        pairs=[]
        for i,text in enumerate(texts):
            pairs += [request(2+i*2,f'c{i}','同名提取',text),
                      result(3+i*2,f'c{i}','同名提取',{'ok':True,'source':text})]
        provider=self.provider(pairs)
        # Selection uses source identity, including when calls arrive in a
        # different order; it does not take the first or last same-label row.
        for text in reversed(texts):
            self.assertEqual(self.call(provider,text)['source'],text)
        self.assertEqual(len(provider.reused_checkpoints),3)

    def test_another_file_another_fact_or_changed_schema_never_uses_a_saved_answer(self):
        provider=self.provider([request(2,'c','同名提取','source A'),result(3,'c','同名提取',{'ok':True})])
        for text,schema in [('source B',SCHEMA),('source A changed facts',SCHEMA),
                            ('source A',{**SCHEMA,'required':['missing']})]:
            with self.subTest(text=text),self.assertRaises(AuditError):self.call(provider,text,schema)
        self.assertEqual(provider.reused,[])

    def test_same_source_can_use_a_repaired_generation_schema_only_after_current_validation(self):
        pairs=[request(2,'c','同名提取','source A'),result(3,'c','同名提取',{'ok':True})]
        # additionalProperties alone is not sent by the advisory transport.
        # Add an optional value field so the delivered generation shape really
        # differs, while the original response still satisfies this contract.
        current={**SCHEMA,'additionalProperties':False,
                 'properties':{**SCHEMA['properties'],'optional_note':{'type':'string'}}}
        provider=self.provider(pairs)
        self.assertEqual(self.call(provider,'source A',schema=current),{'ok':True})
        self.assertTrue(provider.reused_checkpoints[0]['same_source_current_contract'])
        bad=[request(2,'c','同名提取','source A'),result(3,'c','同名提取',{'ok':True,'old_extra':True})]
        with self.assertRaises(AuditError):self.call(self.provider(bad),'source A',schema=current)

    def test_photo_source_packet_keeps_images_facts_scope_and_actual_rules_when_reference_format_changes(self):
        def photo(fact='original',reference='same',rule='actual'):
            return ('本次已读全部原文及表格行关系：'+json.dumps([{'unit_id':'u','facts':[fact]}])+'\n'
                    '本批逐张照片来源：[{"unit_id":"p"}]\n本单已有独立系统活动申请来源：["u"]\n'
                    '本次合同来源unit_id：["c"]\nsource_ids必须逐字复制程序给出的真实unit_id，只能使用本批允许的来源：["u","p","c"]\n'
                    '本次商品参考（仅辨认外观，不是现场来源）：'+json.dumps({'product':reference})+'\n'
                    '只按下方四项已有规则，不增加检查：'+json.dumps([{'id':'display_size','text':rule}])+'\nsource-format-policy=old')
        a=current_request(CONNECTION,Path('skill'),SCHEMA,photo(),[])
        b={**a,'input_text':photo().replace('source-format-policy=old','source-format-policy=fixed')}
        self.assertNotEqual(request_identity(a,'photo')['request'],request_identity(b,'photo')['request'])
        self.assertEqual(request_identity(a,'photo')['source'],request_identity(b,'photo')['source'])
        for key in ('fact','reference','rule'):
            changed={**a,'input_text':photo(**{key:'different'})}
            self.assertNotEqual(request_identity(a,'photo')['source'],request_identity(changed,'photo')['source'])

    def test_reading_contract_excludes_only_downstream_audit_clarifications(self):
        from audit_core.pdf_materials import PdfEvidenceProvider,ORCHESTRATOR_SKILL
        from audit_core.extraction_chain import render_prompt
        from audit_core.reading_contract import reading_policy_context
        policy={'version':'same','types':{'one':{'materials':[{'id':'contract','text':'合同事实'}]}},
                'clarifications':['后续预算必须按完整申请求和']}
        manifest=[{'unit_id':'image-one','image':'one.png','pdf_text':['raw'], 'reading_limitations':[]}]
        prompt=render_prompt(PdfEvidenceProvider._reading_prompt(manifest,policy))
        self.assertNotIn(policy['clarifications'][0],prompt)
        legacy=prompt.replace(json.dumps(reading_policy_context(policy),ensure_ascii=False),json.dumps(policy,ensure_ascii=False))
        a=current_request(CONNECTION,ORCHESTRATOR_SKILL,SCHEMA,legacy,[],transcription=True)
        b=current_request(CONNECTION,ORCHESTRATOR_SKILL,SCHEMA,prompt,[],transcription=True)
        self.assertNotEqual(request_identity(a,'read')['request'],request_identity(b,'read')['request'])
        self.assertEqual(request_identity(a,'read')['source'],request_identity(b,'read')['source'])
        for text in (prompt.replace('只提取本批原始页面','忽略部分原始页面'),
                     prompt.replace('"raw"','"changed raw"'),prompt.replace('合同事实','新增序列号事实')):
            with self.subTest(text=text[:50]):
                c={**b,'input_text':text}
                self.assertNotEqual(request_identity(b,'read')['source'],request_identity(c,'read')['source'])

    def test_embedded_json_key_order_is_formatting_but_values_arrays_and_quotes_are_not(self):
        original = 'Instructions\n事实：[{"unit_id":"u","facts":["raw", "{\\"a\\":1,\\"b\\":2}"],"metadata":{"b":2,"a":1}}]\nEnd'
        reordered = 'Instructions\n事实：[ {"metadata":{"a":1,"b":2}, "facts":["raw", "{\\"a\\":1,\\"b\\":2}"],"unit_id":"u"} ]\nEnd'
        provider = self.provider([request(2,'c','同名提取',original),result(3,'c','同名提取',{'ok':True})])
        self.assertEqual(self.call(provider,reordered),{'ok':True})
        variants = [reordered.replace('"raw"','"changed"'), reordered.replace(',"unit_id":"u"',''),
                    reordered.replace('Instructions','Different instruction'),
                    reordered.replace('{\\"a\\":1,\\"b\\":2}','{\\"b\\":2,\\"a\\":1}'),
                    'Instructions\n事实：[{"unit_id":"u","facts":["second","first"]}]\nEnd']
        for text in variants:
            with self.subTest(text=text),self.assertRaises(AuditError):
                self.call(self.provider([request(2,'c','同名提取',original),result(3,'c','同名提取',{'ok':True})]),text)

    def test_prompt_numbers_are_not_float_rounded_or_confused_with_strings(self):
        def identity(value):
            return request_identity(current_request(CONNECTION,Path('skill'),SCHEMA,'原值：{"n":'+value+'}',[]),'same')['request']
        values=['1','"1"','1.0','1.00000000000000001','9007199254740993','9007199254740992']
        self.assertEqual(len({identity(v) for v in values}),len(values))

    def test_disk_cache_hit_builds_the_current_gate_prompt_before_checkpoint_lookup(self):
        from audit_core.image_fact_cache import ImageFactCache
        from audit_core.pdf_materials import SKILL_BY_SCENARIO
        from audit_core.stage_context import material_context
        scenario='price_difference_support'
        documents=[{'unit_id':'u','roles':['promotion_contract'],'facts':['合同预算600元'], 'limitations':[]}]
        with TemporaryDirectory() as temporary:
            root=Path(temporary); key=hashlib.sha256(b'fixture-image').hexdigest()
            first=ImageFactCache(root,owner='same-run')
            first.put(key,{k:documents[0][k] for k in ('roles','facts','limitations')})
            cached,status=ImageFactCache(root,owner='same-run').get(key)
            self.assertEqual(status,'persistent_hit')
            current=[{'unit_id':'u',**cached}]
            self.assertNotEqual(list(current[0]),list(documents[0]))
            normal=ResumeEvidenceProvider(CONNECTION,trace_prefix()+pending())
            def prompt(d):return normal._gate_prompt(scenario,d).partial(_audit_stage_context=material_context(scenario))
            skill=SKILL_BY_SCENARIO[scenario]
            saved=current_request(CONNECTION,skill,SCHEMA,prompt(documents),[])
            pair=request(2,'gate','门禁',saved['input_text']);pair['data']=saved|{'attempt':1}
            provider=self.provider([pair,result(3,'gate','门禁',{'ok':True})])
            with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',side_effect=AssertionError('Model forbidden')):
                answer=provider._call(root/'unused',skill,SCHEMA,prompt(current),images=[],label='门禁',validator=lambda v:self.assertTrue(v['ok']))
            self.assertEqual(answer,{'ok':True})
            self.assertEqual(provider.reused_checkpoints[0]['source_sequence'],3)

    def test_retries_pair_by_call_id_and_attempt_using_the_original_input(self):
        pairs=[request(2,'c','同名提取','source A'),
               {'type':'ai.validation_failed','sequence':3,'call_id':'c',
                'label':'同名提取 · 第1次结果未通过校验','data':{'attempt':1}},
               request(4,'c','同名提取','source A\nexisting retry hint',attempt=2),
               result(5,'c','同名提取',{'ok':True},attempt=2)]
        provider=self.provider(pairs)
        self.assertEqual(self.call(provider,'source A'),{'ok':True})
        self.assertEqual(provider.reused_checkpoints[0]['source_sequence'],5)

    def test_retry_changed_schema_or_source_is_not_a_checkpoint(self):
        for mode in ('schema','input','call'):
            pairs=[request(2,'c','同名提取','source A'),
                   request(4,'c','同名提取','source A\nretry',attempt=2),
                   result(5,'c','同名提取',{'ok':True},attempt=2)]
            if mode=='schema':pairs[1]['data']['output_schema']={'type':'object'}
            elif mode=='input':pairs[1]['data']['input_text']='OTHER FACTS'
            else:pairs[2]['call_id']='unpaired'
            with self.subTest(mode=mode),self.assertRaises(AuditError):self.provider(pairs)

    def test_ambiguous_identical_requests_do_not_choose_last_answer(self):
        pairs=[request(2,'c1','同名提取','same'),result(3,'c1','同名提取',{'ok':True}),
               request(4,'c2','同名提取','same'),result(5,'c2','同名提取',{'ok':True})]
        with self.assertRaisesRegex(AuditError,'多个'):self.call(self.provider(pairs),'same')

    def test_a_checkpoint_is_consumed_once_and_revalidated_without_mutating_it(self):
        provider=self.provider([request(2,'c','同名提取','same'),result(3,'c','同名提取',{'ok':True})])
        seen=[]
        value=self.call(provider,'same',validator=lambda v:seen.append(v['ok']))
        value['ok']=False
        self.assertEqual(seen,[True])
        self.assertTrue(next(iter(provider.saved.values()))[0]['value']['ok'])
        with self.assertRaises(AuditError):self.call(provider,'same')

    def test_current_source_validator_failure_never_falls_back_to_the_model(self):
        provider=self.provider([request(2,'c','同名提取','same'),result(3,'c','同名提取',{'ok':True})])
        def reject(_):raise AuditError('Current source rejected')
        with self.assertRaisesRegex(AuditError,'Current source'):self.call(provider,'same',validator=reject)
        self.assertFalse(provider.reused)

    def test_run_and_model_scope_are_enforced(self):
        trace=trace_prefix()+[request(2,'c','同名提取','same'),result(3,'c','同名提取',{'ok':True})]+pending()
        for connection,run in [(CONNECTION,'another-run'),
                               (SimpleNamespace(provider='ppio',model=CONNECTION.model,reasoning_effort='default'),'same-run'),
                               (SimpleNamespace(provider=CONNECTION.provider,model='different',reasoning_effort='default'),'same-run')]:
            with self.subTest(run=run),self.assertRaises(AuditError):
                ResumeEvidenceProvider(connection,trace,run_id=run)
        with self.assertRaises(AuditError):ResumeEvidenceProvider(CONNECTION,trace+[
            {'type':'run.started','sequence':102,'data':{'run_id':'another-run'}}])

    def test_missing_original_request_cannot_be_recovered_from_label_only(self):
        with self.assertRaises(AuditError):self.provider([result(3,'c','同名提取',{'ok':True})])

    def test_image_bytes_are_identity_while_temporary_parent_paths_are_not(self):
        with TemporaryDirectory() as temporary:
            base=Path(temporary);(base/'old').mkdir();(base/'new').mkdir()
            old=base/'old'/'picture.png';new=base/'new'/'picture.png'
            old.write_bytes(b'original');new.write_bytes(b'original')
            descriptor={'file':old.name,'size_bytes':8,'sha256':hashlib.sha256(b'original').hexdigest()}
            pairs=[request(2,'c','同名提取','same',images=[descriptor]),result(3,'c','同名提取',{'ok':True})]
            self.assertEqual(self.call(self.provider(pairs),'same',images=[new]),{'ok':True})
            new.write_bytes(b'changed!')
            with self.assertRaises(AuditError):self.call(self.provider(pairs),'same',images=[new])

    def test_both_api_models_have_the_same_request_contract(self):
        for provider in ('deepseek','ppio','zimaocloud'):
            connection=SimpleNamespace(provider=provider,model='same-model',reasoning_effort='default')
            data=current_request(connection,Path('skill'),SCHEMA,'unit=X facts=["raw"]',[])
            self.assertEqual(request_identity(data,'extract'),request_identity(deepcopy(data),'extract'))
            data['input_text']='unit=Y facts=["raw"]'
            self.assertNotEqual(request_identity(data,'extract')['source'],
                                request_identity(current_request(connection,Path('skill'),SCHEMA,'unit=X facts=["raw"]',[]),'extract')['source'])

    def test_pending_stage_requires_same_source_even_when_schema_changes(self):
        provider=self.provider([])
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',return_value={'normal':True}) as call:
            self.assertEqual(provider._call(Path('temp'),Path('skill'),{'type':'object'},'pending',
                images=[],label='日期',validator=lambda _:None),{'normal':True})
            self.assertEqual(call.call_count,1)
        provider=self.provider([])
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',side_effect=AssertionError('Model forbidden')):
            with self.assertRaises(AuditError):provider._call(Path('temp'),Path('skill'),SCHEMA,'another source',
                images=[],label='日期',validator=lambda _:None)

    def test_failed_audit_chunk_can_change_policy_but_not_sources(self):
        focus={'rule_ids':['one'],'reference_fields':{},'material_inventory':[]}
        def text(facts,policy):return '本批索引：'+json.dumps(focus)+'\n本批完整对应原始事实：'+json.dumps(facts)+'\n本单flags：{}\npolicy='+policy
        data=current_request(CONNECTION,Path('skill'),SCHEMA,text([{'unit_id':'u','facts':['raw']}],'old'),[])
        changed={**data,'input_text':text([{'unit_id':'u','facts':['raw']}],'fixed')}
        self.assertNotEqual(request_identity(data,'audit')['request'],request_identity(changed,'audit')['request'])
        self.assertEqual(request_identity(data,'audit')['source'],request_identity(changed,'audit')['source'])
        changed['input_text']=text([{'unit_id':'u','facts':['different']}],'fixed')
        self.assertNotEqual(request_identity(data,'audit')['source'],request_identity(changed,'audit')['source'])

    def test_completed_revalidation_reuses_target_and_only_reconsiders_known_same_source_dependencies(self):
        def audit(reference='old',fact='actual raw',rule='check'):
            focus={'rule_ids':[rule],'reference_fields':{'product':reference},'material_inventory':[]}
            return ('本批索引：'+json.dumps(focus)+'\nSkill：skill；本单flags：{}。\n'
                    '本批完整对应原始事实：'+json.dumps([{'unit_id':'u','facts':[fact]}]))
        trace=trace_prefix()+[request(2,'target','商品','request A'),result(3,'target','商品',{'ok':True}),
            request(4,'dependent','审核',audit()),result(5,'dependent','审核',{'ok':True}),
            request(6,'later','其他','stable'),result(7,'later','其他',{'ok':True})]
        def provider():return ResumeEvidenceProvider(CONNECTION,trace,revalidate_from_call_id='target')
        p=provider()
        with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',return_value={'ok':True}) as call:
            with self.assertRaises(AuditError):p._call(Path('temp'),Path('skill'),SCHEMA,audit('fixed'),images=[],label='审核',validator=lambda _:None)
            self.assertEqual(call.call_count,0)
            p._call(Path('temp'),Path('skill'),SCHEMA,'request A',images=[],label='商品',validator=lambda _:None)
            p._call(Path('temp'),Path('skill'),SCHEMA,audit('fixed'),images=[],label='审核',validator=lambda _:None)
            p._call(Path('temp'),Path('skill'),SCHEMA,'stable',images=[],label='其他',validator=lambda _:None)
            self.assertEqual(call.call_count,1)
        self.assertTrue(p.revalidation_target_seen)
        self.assertEqual([v['source_sequence'] for v in p.reused_checkpoints],[3,7])
        for text,label in [(audit('fixed',fact='different raw'),'审核'),(audit('fixed',rule='other'),'审核'),('unknown source','其他')]:
            p=provider()
            with patch('audit_core.pdf_materials.PdfEvidenceProvider._call',side_effect=AssertionError('Model forbidden')):
                p._call(Path('temp'),Path('skill'),SCHEMA,'request A',images=[],label='商品',validator=lambda _:None)
                with self.assertRaises(AuditError):p._call(Path('temp'),Path('skill'),SCHEMA,text,images=[],label=label,validator=lambda _:None)

    def test_completed_revalidation_requires_an_explicit_unique_validated_original_call(self):
        trace=trace_prefix()+[request(2,'known','商品','source'),result(3,'known','商品',{'ok':True})]
        for target in ('absent',''):
            with self.subTest(target=target),self.assertRaises(AuditError):
                ResumeEvidenceProvider(CONNECTION,trace,revalidate_from_call_id=target)

    def test_identity_uses_the_normal_adapter_schema_image_and_transcription_projection(self):
        from audit_core.pdf_materials import PdfEvidenceProvider, ORCHESTRATOR_SKILL
        from audit_core.model_settings import api_selection
        from audit_core.extraction_chain import render_prompt
        for provider,transcription in [('ppio',False),('deepseek',True),('zimaocloud',True)]:
            with self.subTest(provider=provider),TemporaryDirectory() as temporary:
                base=Path(temporary)
                images=[base/'first.png',base/'duplicate.png']
                for p in images:p.write_bytes(b'same image bytes')
                selection=api_selection({'provider':provider,'api_key':
                    'sk_synthetic_never_send' if provider=='ppio' else 'sk-synthetic-never-send',
                    **({'reasoning_effort':'max'} if transcription else {})})
                skill=ORCHESTRATOR_SKILL if transcription else Path('skill')
                predicted=current_request(selection,skill,SCHEMA,'raw source',images,transcription=transcription)
                captured=[]
                def fake_extract(**kwargs):
                    captured.append({'provider':kwargs['connection'].provider,'model':kwargs['connection'].model,
                        'reasoning_effort':kwargs['connection'].reasoning_effort,'skill':kwargs['skill'].name,
                        'input_text':render_prompt(kwargs['prompt']),
                        'output_schema':json.loads(kwargs['schema_path'].read_text(encoding='utf-8')),
                        'images':[{'file':p.name,'size_bytes':p.stat().st_size,
                                   'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in kwargs['images']]})
                    return {'ok':True}
                normal=PdfEvidenceProvider(selection.model,selection.reasoning_effort,connection=selection, stage_summary_reviewer=isolated_stage_summary)
                with patch('audit_core.qwen_model.extract_qwen_json',side_effect=fake_extract):
                    normal._call(base/'normal',skill,SCHEMA,'raw source',images=images,label='same',
                                 validator=lambda _:None,transcription=transcription)
                self.assertEqual(predicted,captured[0])
                self.assertEqual(len(predicted['images']),1)
                if transcription:self.assertEqual(predicted['reasoning_effort'],'max')

    def test_old_none_transcription_cannot_match_max_even_with_identical_image_and_prompt(self):
        from audit_core.model_settings import api_selection
        from audit_core.pdf_materials import ORCHESTRATOR_SKILL
        selection=api_selection({'provider':'zimaocloud','api_key':'sk-synthetic-never-send',
                                 'reasoning_effort':'max'})
        prompt='identical original facts and request'
        maximum=current_request(selection,ORCHESTRATOR_SKILL,SCHEMA,prompt,[],transcription=True)
        old={**maximum,'reasoning_effort':'none'}
        before=request_identity(old,'same-reading')
        after=request_identity(maximum,'same-reading')
        self.assertNotEqual(before['request'],after['request'])
        self.assertNotEqual(before['source'],after['source'])


if __name__=='__main__':unittest.main()
