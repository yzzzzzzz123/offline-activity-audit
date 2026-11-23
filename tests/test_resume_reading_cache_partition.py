"""Deployment cache changes must not resize an existing same-run read batch."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from contextlib import nullcontext
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock, patch
import json

from PIL import Image

from audit_core.common import AuditError
from audit_core.failed_upload_resume import ResumeEvidenceProvider
from audit_core.image_fact_cache import ImageFactCache
from audit_core.model_settings import ModelSelection
from audit_core.pdf_evidence import validate
from audit_core.pdf_materials import PdfEvidenceProvider, material_catalogue
from audit_core.resume_request_identity import current_request, reading_manifest_identity
from tests.test_failed_upload_resume import trace_prefix, request, result, pending


class CapturedRequest(Exception):
    pass


class ResumeReadingCachePartitionTests(TestCase):
    def setUp(self):
        self.temporary=TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.connection=ModelSelection('ppio','fixture-model','default','https://example.invalid/v1','fixture-unused')
        self.units=[]
        for i in range(3):
            path=self.root/f'photo-{i}.png'
            Image.new('RGB',(12,12),(20+i,30,40)).save(path)
            self.units.append({'unit_id':f'local-{i}','image':path,'source_file':path.name,
                'locator':path.name,'native_facts':[],'limitations':[]})
        self.policy=material_catalogue()

    def directory(self,name):
        path=self.root/name;path.mkdir();return path

    def original(self,units=None,*,legacy=False):
        entries=[];calls=[]
        class RecordingCache(ImageFactCache):
            def put(self,key,value):
                entries.append((key,deepcopy(value)))
                return super().put(key,value)
        provider=PdfEvidenceProvider(None,None,connection=self.connection, stage_summary_reviewer=isolated_stage_summary)
        def fake(root,skill,schema,prompt,*,images,label,validator,**kwargs):
            data=current_request(self.connection,skill,schema,prompt,images,transcription=True)
            manifest=json.loads(prompt.partial_variables['manifest'])
            value={'documents':[{'unit_id':item['unit_id'],'roles':['photo'],
                'facts':['本图实际文字'+str(i)],'limitations':[]} for i,item in enumerate(manifest)]}
            validate(value,schema);validator(value)
            calls.append({'data':data,'value':deepcopy(value),'label':label})
            return value
        case={'archive_id':'a001','units':units or self.units,'workspace_id':'same-run'}
        legacy_group=(patch.object(provider,'_reading_requests',side_effect=lambda entries,label:[(list(entries),label)])
                      if legacy else nullcontext())
        with patch('audit_core.pdf_materials.ImageFactCache',RecordingCache),patch.object(provider,'_call',side_effect=fake),legacy_group:
            docs=provider._read_documents(case,self.directory('original'),self.policy)
        saved=[]
        for index,call in enumerate(calls):
            call_id='reader' if index==0 else f'reader-{index}'
            event=request(2+index*2,call_id,call['label'],call['data']['input_text'])
            event['data']=call['data']|{'attempt':1}
            saved.extend([event,result(3+index*2,call_id,call['label'],call['value'])])
        wait=pending()
        for index,event in enumerate(wait):event['sequence']=2+len(saved)+index
        wait[0]['data']=current_request(self.connection,Path('skill'),{'type':'object'},'pending',[])|{'attempt':1}
        traces=trace_prefix()+saved+wait
        return case,docs,calls,entries,traces

    def resume(self,case,traces,name='resumed',*,revalidation=False):
        provider=ResumeEvidenceProvider(self.connection,traces,
            revalidate_from_call_id='reader' if revalidation else None)
        observations=[]
        with (patch('audit_core.pdf_materials.PdfEvidenceProvider._call',side_effect=AssertionError('Real model forbidden')),
              patch('audit_core.pdf_materials.save_model_observations',side_effect=lambda _,data:observations.append(deepcopy(data)))):
            docs=provider._read_documents(case,self.directory(name),self.policy)
        return provider,docs,observations[-1]

    def test_one_new_cache_hit_keeps_original_three_source_request_and_answer(self):
        case,original,calls,entries,traces=self.original()
        store=self.root/'isolated-cache';warm=ImageFactCache(store,owner='other-run')
        warm.put(*entries[-1]);case['worktrees_root']=store
        fresh=PdfEvidenceProvider(None,None,connection=self.connection, stage_summary_reviewer=isolated_stage_summary)
        pending_sizes=[]
        def stop(root,skill,schema,prompt,*,images,**kwargs):
            pending_sizes.append(len(images));raise CapturedRequest()
        with patch.object(fresh,'_call',side_effect=stop),self.assertRaises(CapturedRequest):
            fresh._read_documents(case,self.directory('normal-mixed-cache'),self.policy)
        self.assertEqual(pending_sizes,[1])
        preserved=deepcopy(traces)
        provider,docs,counts=self.resume(case,traces)
        self.assertEqual(docs,original)
        self.assertEqual(provider.reused_checkpoints[0]['source_sequence'],3)
        self.assertEqual(counts['same_run_layout_images'],3)
        self.assertEqual(traces,preserved)
        self.assertFalse(provider.pending_started)

    def test_old_validated_multi_image_checkpoint_is_replayed_intact_without_sending_images(self):
        case,original,calls,entries,traces=self.original(legacy=True)
        self.assertEqual(len(calls),1)
        self.assertEqual(len(calls[0]['data']['images']),3)
        store=self.root/'legacy-cache';warm=ImageFactCache(store,owner='other-run')
        warm.put(*entries[-1]);case['worktrees_root']=store
        provider,docs,counts=self.resume(case,traces)
        self.assertEqual(docs,original)
        self.assertEqual(len(provider.reused_checkpoints),1)
        self.assertEqual(counts['same_run_layout_images'],3)
        self.assertFalse(provider.pending_started)

    def test_all_hits_keep_resume_contract_while_fresh_runs_still_use_cache(self):
        case,original,_,entries,traces=self.original()
        store=self.root/'isolated-cache';warm=ImageFactCache(store,owner='other-run')
        for key,value in entries:warm.put(key,value)
        case['worktrees_root']=store
        provider,docs,counts=self.resume(case,traces)
        self.assertEqual(docs,original)
        self.assertEqual(counts['same_run_layout_images'],3)
        fresh=PdfEvidenceProvider(None,None,connection=self.connection, stage_summary_reviewer=isolated_stage_summary)
        with patch.object(fresh,'_call',side_effect=AssertionError('All sources should be cached')):
            self.assertEqual(fresh._read_documents(case,self.directory('fresh'),self.policy),original)
        self.assertEqual(len(provider.reused_checkpoints),3)

    def test_changed_image_never_reuses_old_batch_or_falls_back_to_model(self):
        case,_,_,entries,traces=self.original()
        store=self.root/'isolated-cache';warm=ImageFactCache(store,owner='other-run')
        warm.put(*entries[-1]);case['worktrees_root']=store
        Image.new('RGB',(12,12),(200,10,20)).save(self.units[0]['image'])
        with self.assertRaises(AuditError):self.resume(case,traces)

    def test_native_text_limitations_and_batch_label_are_not_wildcards(self):
        _,_,calls,_,traces=self.original()
        provider=ResumeEvidenceProvider(self.connection,traces)
        manifest=json.JSONDecoder().raw_decode(calls[0]['data']['input_text'].split('本批资料：',1)[1].lstrip())[0][0]
        cache=Mock();cache.get.return_value=({'facts':['cached']},'persistent_hit')
        for changed,label in [(manifest|{'pdf_text':['changed']},calls[0]['label']),
                              (manifest|{'reading_limitations':['new']},calls[0]['label']),
                              (manifest,'other batch')]:
            with self.subTest(label=label):
                self.assertEqual(provider._reading_cache_lookup(cache,'key',changed,label=label),cache.get.return_value)
        self.assertEqual(cache.get.call_count,3)
        cache_label=calls[0]['label'].split(' · 单图',1)[0]
        self.assertEqual(provider._reading_cache_lookup(cache,'key',manifest,label=cache_label),
                         (None,'same_run_checkpoint_layout'))
        self.assertEqual(cache.get.call_count,3)

    def test_normal_post_pending_work_restores_default_cache_behavior(self):
        _,_,calls,_,traces=self.original()
        provider=ResumeEvidenceProvider(self.connection,traces)
        manifest=json.JSONDecoder().raw_decode(calls[0]['data']['input_text'].split('本批资料：',1)[1].lstrip())[0][0]
        cache=Mock();cache.get.return_value=({'facts':['cached']},'persistent_hit')
        provider.pending_started=True
        self.assertEqual(provider._reading_cache_lookup(cache,'key',manifest,label=calls[0]['label']),cache.get.return_value)
        cache.get.assert_called_once_with('key')

    def test_duplicate_image_bindings_remain_separate_local_sources(self):
        units=[self.units[0],self.units[0]|{'unit_id':'duplicate-source'},self.units[2]]
        case,original,calls,entries,traces=self.original(units)
        self.assertEqual([len(call['data']['images']) for call in calls],[1,1])
        store=self.root/'isolated-cache';warm=ImageFactCache(store,owner='other-run')
        warm.put(*entries[-1]);case['worktrees_root']=store
        _,docs,_=self.resume(case,traces)
        self.assertEqual(docs,original)
        self.assertEqual([d['unit_id'] for d in docs],['local-0','duplicate-source','local-2'])

    def test_completed_candidate_mode_preserves_the_same_original_reader_batch(self):
        case,original,_,entries,traces=self.original()
        store=self.root/'isolated-cache';warm=ImageFactCache(store,owner='other-run')
        for key,value in entries:warm.put(key,value)
        case['worktrees_root']=store
        provider,docs,counts=self.resume(case,traces,revalidation=True)
        self.assertEqual(docs,original)
        self.assertTrue(provider.revalidation_target_seen)
        self.assertFalse(provider.revalidated_stages)
        self.assertEqual(counts['same_run_layout_images'],3)

    def test_incomplete_reader_descriptor_is_never_a_batch_membership_wildcard(self):
        base={'unit_id':'image-source','image':'image-source.png','pdf_text':[],'reading_limitations':[]}
        for changed in ({'pdf_text':None},{'image':''},{'reading_limitations':[None]}):
            with self.subTest(changed=changed),self.assertRaises(AuditError):
                reading_manifest_identity(base|changed)
