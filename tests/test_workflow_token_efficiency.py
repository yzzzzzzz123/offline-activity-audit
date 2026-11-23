"""Remove duplicate delivery, while proving every source value/check remains."""
from tests.pdf_test_support import isolated_stage_summary
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.model_settings import api_selection
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.prompt_delivery import share_system_business_guide
from audit_core.prompts import bound_prompt
from audit_core.qince_references import *
from tests.test_qince_references import references


def multi_photo_case(root, count=7, products=100):
    raw = references()
    images, units, digests = [], [], {}
    for index in range(count):
        path = root / f'photo-{index}.png'; path.write_bytes(f'original-photo-{index}'.encode())
        url = f'https://image.qince.com/{index}.png'
        images.append({'imageId':f'I{index}','imageUrl':url})
        units.append({'unit_id':f'current-{index}','image':path})
        digests[url] = hashlib.sha256(path.read_bytes()).hexdigest()
    raw[PRIMITIVE_DETAIL][0]['images'] = images
    raw[PRIMITIVE_DETAIL][0]['products'] = [{'productId':f'P{i}','productCode':f'SKU{i}',
        'productNum':str(i+1),'layers':'1,2,3','bjpType':'0','type':'0'} for i in range(products)]
    raw[CHECKED_DETAIL][0]['images'] = deepcopy(images)
    raw[CHECKED_DETAIL][0]['products'] = [{**p,'productNum':str(index+2),'changeType':'3'}
        for index,p in enumerate(raw[PRIMITIVE_DETAIL][0]['products'])]
    raw[ORIGINAL] = [{'imageId':f'I{index}','imageUrl':images[index]['imageUrl'],
        'products':[{'productId':'P1','productNum':str(index+10)}]} for index in range(count)]
    return raw, units, digests


def payloads(documents):
    return {d['unit_id']:json.loads(d['facts'][0].split('=',1)[1]) for d in documents}


def expanded_photos(documents):
    values = payloads(documents)
    result = []
    for value in values.values():
        if 'imageId' not in value: continue
        parent = value.get('business_reference_unit_id')
        result.append({**(values[parent] if parent else {}),
                       **{k:v for k,v in value.items() if k!='business_reference_unit_id'}})
    return result


class GuideDeliveryTests(unittest.TestCase):
    def skill(self,root):
        skill=root/'skill';(skill/'references').mkdir(parents=True)
        guide='原审核项、条件、全部标准与例外逐字保留。\n'*80
        (skill/'references/business-guide.md').write_text(guide,encoding='utf-8')
        (skill/'SKILL.md').write_text('原Skill完整内容',encoding='utf-8')
        (skill/'references/audit-rules.md').write_text('原逐项审核规则',encoding='utf-8')
        return skill,guide

    def test_exact_instruction_binding_is_shared_without_touching_evidence_or_dynamic_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            skill,guide=self.skill(Path(directory))
            prompt=bound_prompt('说明:{business_guide}\n完整证据:{documents}',
                business_guide=guide+'本次单据的额外来源约束',documents=guide)
            original=prompt.format();projected,saved=share_system_business_guide(prompt,skill)
            self.assertGreater(saved,0)
            self.assertEqual(prompt.format(),original)
            self.assertEqual(projected.partial_variables['documents'],guide)
            self.assertTrue(projected.partial_variables['business_guide'].endswith('本次单据的额外来源约束'))
            self.assertIn('system',projected.partial_variables['business_guide'])

    def test_real_api_adapter_still_receives_full_guide_and_all_schema_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);skill,guide=self.skill(root)
            selection=api_selection({'provider':'ppio','api_key':'sk_test_fixture_only'})
            provider=PdfEvidenceProvider(None,None,connection=selection, stage_summary_reviewer=isolated_stage_summary)
            calls=[]
            def complete(connection,content,*,system,**kwargs):
                calls.append((system,content))
                self.assertEqual(system.count(guide),1)
                self.assertIn('原逐项审核规则',system)
                self.assertIn('本批 JSON Schema',system)
                return '{"ok":true}',{'input_tokens':0,'output_tokens':0}
            with patch('audit_core.qwen_model.complete_json',side_effect=complete):
                result=provider._call(root/'request',skill,{'type':'object','properties':{'ok':{'const':True}},'required':['ok']},
                    bound_prompt('本类说明:{guide}\n本次事实:{documents}',guide=guide,documents='原始事实逐字保留'),
                    images=[],label='隔离输入等价验证',validator=lambda value:value)
            self.assertEqual(result,{'ok':True});self.assertEqual(len(calls),1)
            self.assertNotIn(guide,json.dumps(calls[0][1],ensure_ascii=False))
            self.assertIn('原始事实逐字保留',json.dumps(calls[0][1],ensure_ascii=False))

    def test_transcription_and_unknown_bindings_are_not_rewritten(self):
        with tempfile.TemporaryDirectory() as directory:
            skill,guide=self.skill(Path(directory))
            for prompt in (bound_prompt('{manifest}',manifest=guide),bound_prompt('{business_guide}',business_guide='其他原文'+guide)):
                projected,saved=share_system_business_guide(prompt,skill)
                self.assertIs(projected,prompt);self.assertEqual(saved,0)


class SharedPhotoSourceTests(unittest.TestCase):
    def test_each_api_response_is_parsed_once_for_all_seven_photos(self):
        from collections import Counter
        with tempfile.TemporaryDirectory() as directory, patch(
                'audit_core.qince_references.reference_rows', wraps=reference_rows) as reads:
            self.project(Path(directory))
        counts=Counter(c.args[1] for c in reads.call_args_list)
        self.assertEqual(len(counts),9)
        self.assertTrue(all(count==1 for count in counts.values()))

    def project(self,root,*,count=7,products=100,alter=None):
        raw,units,digests=multi_photo_case(root,count,products)
        if alter:alter(raw)
        original=deepcopy(raw);events=[]
        with patch('audit_core.qince_references.photo_digest',side_effect=lambda raw,url:digests.get(url)), \
             patch('audit_core.qince_references.record_step',side_effect=lambda kind,label,data,**kw:events.append((kind,deepcopy(data)))):
            extra,docs=photo_reference_sources(raw,'HX_TEST','a',units)
        self.assertEqual(raw,original)
        before=[data for kind,data in events if kind=='qince.photo_linked']
        return extra,docs,before

    def test_seven_photos_keep_every_original_and_checked_field_via_shared_source(self):
        with tempfile.TemporaryDirectory() as directory:
            units,docs,before=self.project(Path(directory))
        after=expanded_photos(docs)
        self.assertEqual(after,before)
        self.assertEqual(len(after),7)
        self.assertEqual({r['imageId'] for r in after},{f'I{i}' for i in range(7)})
        self.assertEqual({p for r in after for p in r['photo_unit_ids']},{f'current-{i}' for i in range(7)})
        self.assertEqual(len([u for u in units if u['source_file']=='勤策本图共用业务参照']),1)
        self.assertEqual(len([u for u in units if u['source_file']=='勤策本图识别与校对参照']),7)
        raw_bytes=len(json.dumps(before,ensure_ascii=False).encode())
        delivered=len(json.dumps(list(payloads(docs).values()),ensure_ascii=False).encode())
        self.assertLess(delivered,raw_bytes//3)

    def test_single_photo_keeps_plain_shape_without_extra_shared_record(self):
        with tempfile.TemporaryDirectory() as directory:
            units,docs,before=self.project(Path(directory),count=1)
        self.assertEqual(len(units),1)
        self.assertEqual(list(payloads(docs).values()),before)

    def test_checked_variants_with_different_image_bindings_stay_per_photo(self):
        def alter(raw):
            base=raw[CHECKED_DETAIL][0]
            raw[CHECKED_DETAIL]=[{**deepcopy(base),'images':[image],'approvalStatus':str(index),
                'products':[{'productId':'P1','productNum':str(index),'changeType':'2'}]}
                for index,image in enumerate(base['images'])]
        with tempfile.TemporaryDirectory() as directory:
            _,docs,before=self.project(Path(directory),alter=alter)
        self.assertEqual(expanded_photos(docs),before)
        self.assertEqual([r['checked_details'][0]['approvalStatus'] for r in expanded_photos(docs)],[str(i) for i in range(7)])

    def test_business_or_report_version_conflict_never_merges_common_values(self):
        def alter(raw):
            alternate=deepcopy(raw[PRIMITIVE_REPORT][0]);alternate['imageArea']='2.00000001'
            raw[PRIMITIVE_REPORT].append(alternate)
        with tempfile.TemporaryDirectory() as directory:
            units,docs,before=self.project(Path(directory),count=2,alter=alter)
        self.assertEqual(expanded_photos(docs),before)
        self.assertEqual(len([u for u in units if u['source_file']=='勤策本图共用业务参照']),2)
        self.assertEqual({r['report']['imageArea'] for r in expanded_photos(docs)},{'1.00','2.00000001'})


class DateWindowTests(unittest.TestCase):
    def test_merged_window_avoids_requerying_same_business_but_keeps_all_application_rows(self):
        from unittest.mock import MagicMock
        from audit_core.qince_context import _linked_applications,VERIFICATION,APPLICATIONS
        raw=references();app=raw[APPLICATIONS][0];row=app['subList'][0]
        source=[('2026-10-01','2026-10-05'),('2026-10-03','2026-10-07'),('2026-10-08','2026-10-09')]
        app['subList']=[];raw[VERIFICATION][0]['details']=[]
        for index,(start,end) in enumerate(source):
            app['subList'].append({**row,'subId':f'S{index}','startDate':start,'endDate':end})
            raw[VERIFICATION][0]['details'].append({'id':f'V{index}','activity_code':app['applyCode'],
                'activity_cm_id':f'S{index}','customer_id':'C1','customer_code':'STORE1'})
        original=deepcopy(raw);client=MagicMock();client.host='https://enterprise.test'
        client.pages.side_effect=lambda method,params:raw.get(method,[])
        linked=_linked_applications(raw[VERIFICATION],raw[APPLICATIONS])
        result=fetch_references(client,raw,'HX_TEST',linked,needs={'historical_photos'})
        calls=[c for c in client.pages.call_args_list if c.args[0]==PRIMITIVE_REPORT]
        self.assertEqual(len(calls),1)
        self.assertEqual(calls[0].args[1],{'startDate':'2026-10-01','endDate':'2026-10-09'})
        self.assertEqual(result[APPLICATIONS],original[APPLICATIONS]);self.assertEqual(raw,original)

    def test_overlap_and_adjacent_ranges_merge_without_adding_or_losing_a_day(self):
        source=[('2026-10-01','2026-10-05'),('2026-10-03','2026-10-07'),
                ('2026-10-08','2026-10-09'),('2026-10-12','2026-10-13')]
        linked=[({},dict(startDate=start,endDate=end)) for start,end in source]
        self.assertEqual(windows(linked),[('2026-10-01','2026-10-09'),('2026-10-12','2026-10-13')])
        def days(ranges):
            return {date.fromisoformat(start)+timedelta(days=i) for start,end in ranges
                    for i in range((date.fromisoformat(end)-date.fromisoformat(start)).days+1)}
        self.assertEqual(days(windows(linked)),days(source))
        self.assertEqual([row for _,row in linked],[dict(startDate=start,endDate=end) for start,end in source])


class ApplicationLinkDeliveryTests(unittest.TestCase):
    def test_large_shared_link_table_is_restored_exactly_for_each_product_reference(self):
        from audit_core.qince_context import context_sources,_linked_applications,VERIFICATION,APPLICATIONS
        raw=references();app=raw[APPLICATIONS][0];base=deepcopy(app['subList'][0])
        app['subList']=[];raw[VERIFICATION][0]['details']=[]
        for index in range(40):
            app['subList'].append({**deepcopy(base),'subId':str(index)})
            raw[VERIFICATION][0]['details'].append({'id':str(index),'activity_code':app['applyCode'],
                'activity_cm_id':str(index),'customer_id':'C1','customer_code':'STORE1'})
        linked=_linked_applications(raw[VERIFICATION],raw[APPLICATIONS])
        expected=[row[2] for row in reference_sources(raw,'HX_TEST',linked)]
        before=deepcopy(raw);units,docs=context_sources(raw,'HX_TEST','a')
        values={d['unit_id']:{k:json.loads(v) for k,v in (fact.split('=',1) for fact in d['facts'])}
                for u,d in zip(units,docs) if u.get('qince_reference')}
        shared=[value for value in values.values() if 'application_rows' in value and 'method' not in value]
        self.assertEqual(len(shared),1);self.assertEqual(len(shared[0]['application_rows']),40)
        actual=[]
        for value in values.values():
            if 'method' not in value:continue
            value=deepcopy(value);parent=value.pop('application_reference_unit_id',None)
            if parent:value['application_rows']=values[parent]['application_rows']
            actual.append(value)
        self.assertEqual(actual,expected)
        self.assertEqual(raw,before)


if __name__=='__main__':unittest.main()
