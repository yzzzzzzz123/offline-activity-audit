"""Reference learning, cache isolation and targeted OCR review; no paid calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from PIL import Image

from audit_core.model_authority import business_guidance
from audit_core.model_metrics import collect_model_artifacts
from audit_core.model_self_check import review_stage
from audit_core.pdf_materials import PdfEvidenceProvider
from audit_core.pdf_policy import COMPANY_TEXT_READING_GUIDANCE, PHOTO_TIME_READING_GUIDANCE
from audit_core.recognition_examples import load_examples, study_prompt
from audit_core.scenario_registry import SCENARIO_ORDER
from tests.pdf_test_support import flags_for


class RecognitionExampleTests(TestCase):
    def test_only_human_verified_examples_are_loaded_and_precede_current_source(self):
        sample = load_examples()
        self.assertEqual(len(sample['examples']), 6)
        self.assertIn('2025.12.12 星期五', study_prompt([], sample))
        unverified = deepcopy(sample['examples'][0])
        unverified.update(approval='unverified', confirmed_reading='UNVERIFIED-COMPANY')
        payload = {'revision': 'fixture', 'examples': [sample['examples'][0], unverified]}
        with TemporaryDirectory() as folder:
            path=Path(folder)/'catalog.json';path.write_text(json.dumps(payload), encoding='utf-8')
            with patch('audit_core.recognition_examples.CATALOG', path):
                loaded=load_examples()
        self.assertEqual(len(loaded['examples']), 1)
        self.assertNotIn('UNVERIFIED-COMPANY', study_prompt([], loaded))
        manifest=[{'unit_id':'current-source','image':'current.png','pdf_text':['CURRENT FACTS'],
                   'reading_limitations':[],'reader_skills':['read-audit-image']}]
        prompt=PdfEvidenceProvider._reading_prompt(manifest,{},study=sample).format()
        self.assertLess(prompt.index('人工核验'),prompt.index('CURRENT FACTS'))
        self.assertIn('不能把示例公司、时间、金额或审核结论复制到当前facts', prompt)
        self.assertIn('深圳市山三古月科技有限公司',prompt)
        self.assertIn('张家口传奇伟业商贸有限公司',prompt)
        self.assertIn('奔象（广州）供应链管理有限公司',prompt)

    def test_company_and_year_reading_guidance_reaches_every_business_scenario(self):
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario):
                prompt=business_guidance(scenario)
                self.assertIn(COMPANY_TEXT_READING_GUIDANCE,prompt)
                self.assertIn(PHOTO_TIME_READING_GUIDANCE,prompt)

    def test_changed_examples_invalidate_source_fact_cache_without_extra_study_calls(self):
        from tests.test_image_fact_cache_integration import ImageFactIntegrationTests
        helper=ImageFactIntegrationTests('runTest');helper.setUp();self.addCleanup(helper.doCleanups)
        unit=helper.unit('year-watermark',57)
        _, first, _=helper.run_read([unit],run='before')
        changed=load_examples();changed['revision']='fixture-new-revision'
        changed['examples'][0]['reading_lesson'] += ' NEW HUMAN VERIFIED LESSON'
        with patch('audit_core.recognition_examples.load_examples',return_value=changed):
            _, after, _=helper.run_read([unit],run='after')
            _, same, counts=helper.run_read([unit],run='same-revision')
        self.assertEqual([len(first),len(after),len(same)],[1,1,0])
        self.assertEqual(len(after[0]['images']),1)
        self.assertIn('NEW HUMAN VERIFIED LESSON',after[0]['prompt'])
        self.assertEqual(counts['persistent_hits'],1)

    def test_date_weekday_disagreement_can_reopen_only_the_affected_original(self):
        with TemporaryDirectory() as folder:
            root=Path(folder);pictures=[root/'year.png',root/'unrelated.png']
            for picture in pictures:Image.new('RGB',(12,12),'white').save(picture)
            original_bytes=[picture.read_bytes() for picture in pictures]
            documents=[{'unit_id':'one','roles':['photo'],'facts':['初读水印为2026.12.12 星期五'],'limitations':[]},
                       {'unit_id':'two','roles':['photo'],'facts':['水印2025.11.10，早于活动开始'],'limitations':[]}]
            case={'archive_id':'a001','scenario':'price_difference_support',
                  'flags':flags_for('price_difference_support',uses_pos=False),
                  'materials':[], 'documents':documents,
                  'units':[{'unit_id':d['unit_id'],'image':p,'source_file':p.name,
                            'locator':'原图','native_facts':[],'limitations':[]} for d,p in zip(documents,pictures)]}
            before={'checks':[{'rule_id':'photo_activity_dates','status':'fail',
                'reason':'初读称全部照片无法确认在活动期内。','source_ids':['one','two']}],
                'photo_reviews':[{'unit_id':'one','checks':[{'rule_id':'photo_activity_dates','status':'unknown',
                    'reason':'初读年份与星期不符。','source_ids':['one']}]},
                    {'unit_id':'two','checks':[{'rule_id':'photo_activity_dates','status':'fail',
                    'reason':'2025.11.10早于活动期。','source_ids':['two']}]}]}
            original=deepcopy(before);corrected=deepcopy(before)
            corrected['photo_reviews'][0]['checks'][0].update(status='pass',reason='回看原图为2025.12.12 星期五，14:32，在活动期内。')
            corrected['checks'][0]['reason']='仅照片二的2025.11.10早于活动期。'
            provider=PdfEvidenceProvider('fixture','max');seen=[]
            def call(call_root,skill,schema,prompt,**kwargs):
                self.assertIn('先以image_requests请求该张原图',prompt.format())
                self.assertIn('watermark-year-2025-not-2026',prompt.format())
                seen.append(list(kwargs['images']))
                answer={'ready':len(seen)>1,'notes':[],**deepcopy(corrected if len(seen)>1 else before),
                        'image_requests':[pictures[0].name] if len(seen)==1 else []}
                kwargs['validator'](answer);return answer
            with collect_model_artifacts(root/'analysis'),patch.object(provider,'_call',side_effect=call):
                result=review_stage(provider,case,before,before,root/'review',['photo_activity_dates'],
                                    lambda *args:before,images=pictures)
            self.assertEqual(seen,[[],[pictures[0]]])
            self.assertEqual(result['photo_reviews'][0]['checks'][0]['status'],'pass')
            self.assertEqual(result['photo_reviews'][1],original['photo_reviews'][1])
            self.assertNotIn('全部照片',result['checks'][0]['reason'])
            self.assertEqual(result['checks'][0]['status'],'fail')
            self.assertEqual(before,original)
            self.assertEqual([p.read_bytes() for p in pictures],original_bytes)
