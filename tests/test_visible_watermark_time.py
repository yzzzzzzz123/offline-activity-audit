"""Time-watermark reading and targeted review contracts; no real model calls."""
from copy import deepcopy
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
from audit_core.pdf_policy import PHOTO_TIME_READING_GUIDANCE, PHOTO_TIME_PRESENCE_CLARIFICATION
from audit_core.scenario_registry import SCENARIO_ORDER
from tests.pdf_test_support import flags_for


class VisibleWatermarkTimeTests(TestCase):
    def test_relaxed_presence_policy_is_published_to_all_generated_business_guides(self):
        import importlib.util
        from audit_core.paths import PROJECT_ROOT
        from audit_core.pdf_policy import catalogue, POLICY_VERSION
        source = PROJECT_ROOT/'skills/create-offline-audit-scenario/scripts/sync_pdf_policy.py'
        spec = importlib.util.spec_from_file_location('watermark_policy_sync', source)
        module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
        generated = module.generated_files()
        self.assertEqual(POLICY_VERSION, '2026-11-24.xlsx.54')
        self.assertIn(PHOTO_TIME_PRESENCE_CLARIFICATION, catalogue()['clarifications'])
        from audit_core.scenario_registry import SCENARIO_SPECS
        for scenario in SCENARIO_SPECS:
            for name in ('SKILL.md', 'references/audit-rules.md', 'references/business-guide.md'):
                with self.subTest(scenario=scenario.scenario, document=name):
                    self.assertIn(PHOTO_TIME_PRESENCE_CLARIFICATION, generated[scenario.skill_dir/name])
        self.assertIn('不补造完整时间', PHOTO_TIME_PRESENCE_CLARIFICATION)
        self.assertIn('价签、商品规格、条码', PHOTO_TIME_PRESENCE_CLARIFICATION)

    def test_time_digits_can_finish_without_reread_and_keep_independent_date_problem(self):
        for digits in ('15', '17', '16'):
            with self.subTest(digits=digits), TemporaryDirectory() as folder:
                root=Path(folder)
                documents=[{'unit_id':'one','roles':['display_photos'],
                    'facts':['水印时间位置读到'+digits,'日期2026.10.9，门店可对应。'],
                    'limitations':['分钟字形未辨认。']},
                    {'unit_id':'two','roles':['display_photos'],
                    'facts':['日期2026.10.20，超出活动周期。'],'limitations':[]}]
                case={'archive_id':'a001','scenario':'promotional_display',
                    'flags':flags_for('promotional_display',uses_pos=False),
                    'materials':[{'id':'display_photos','state':'present','source_ids':['one','two']}],
                    'documents':documents,'units':[]}
                value={'checks':[{'rule_id':'display_watermark','status':'fail',
                    'reason':'照片二日期2026.10.20超出活动期。','source_ids':['two']}],
                    'photo_reviews':[{'unit_id':'one','checks':[{'rule_id':'display_watermark',
                        'status':'unknown','reason':'分钟未辨认。','source_ids':['one']}]},
                        {'unit_id':'two','checks':[{'rule_id':'display_watermark','status':'fail',
                            'reason':'日期2026.10.20超出活动期。','source_ids':['two']}]}]}
                original=deepcopy((case,value))
                answer=deepcopy(value)
                answer['photo_reviews'][0]['checks'][0].update(status='pass',reason='已显示拍摄时间，日期和门店可对应。')
                provider=PdfEvidenceProvider('fixture','max')
                def call(call_root,skill,schema,prompt,**kwargs):
                    self.assertIn(PHOTO_TIME_PRESENCE_CLARIFICATION,prompt.format())
                    self.assertIn('不为补齐分钟反复回看',prompt.format())
                    self.assertEqual(kwargs['images'],[])
                    result={'ready':True,'notes':[],**deepcopy(answer)}
                    kwargs['validator'](result)
                    return result
                with collect_model_artifacts(root/'analysis'),patch.object(provider,'_call',side_effect=call) as model:
                    result=review_stage(provider,case,value,{},root/'review',
                        ['display_watermark'],lambda *args:value,images=[])
                self.assertEqual(model.call_count,1)
                self.assertEqual(result['photo_reviews'][0]['checks'][0]['reason'],'已显示拍摄时间，日期和门店可对应。')
                self.assertEqual(result['photo_reviews'][1],value['photo_reviews'][1])
                self.assertEqual(result['checks'],value['checks'])
                self.assertEqual((case,value),original)

    def test_complete_time_guidance_reaches_photo_and_document_transcription_and_all_business_types(self):
        for reader in ('read-audit-image', 'read-audit-document'):
            manifest = [{'unit_id': 'one', 'image': 'one.png', 'pdf_text': [],
                         'reading_limitations': [], 'reader_skills': [reader]}]
            prompt = PdfEvidenceProvider._reading_prompt(manifest, {}).format()
            self.assertIn(PHOTO_TIME_READING_GUIDANCE, prompt)
        for scenario in SCENARIO_ORDER:
            with self.subTest(scenario=scenario):
                self.assertIn(PHOTO_TIME_READING_GUIDANCE, business_guidance(scenario))

    def test_changed_time_reading_contract_does_not_reuse_old_image_facts(self):
        from tests.test_image_fact_cache_integration import ImageFactIntegrationTests
        helper = ImageFactIntegrationTests('runTest')
        helper.setUp()
        self.addCleanup(helper.doCleanups)
        unit = helper.unit('time-watermark', 93)
        unit['source_file'] = 'actual-photo.png'
        with patch('audit_core.pdf_policy.PHOTO_TIME_READING_GUIDANCE', '旧版读取约定'):
            _, old_calls, _ = helper.run_read([unit], run='old')
        _, new_calls, _ = helper.run_read([unit], run='new')
        _, unchanged_calls, counts = helper.run_read([unit], run='same-fixed-contract')
        self.assertEqual(len(old_calls), 1)
        self.assertEqual(len(new_calls), 1)
        self.assertIn(PHOTO_TIME_READING_GUIDANCE, new_calls[0]['prompt'])
        self.assertEqual(unchanged_calls, [])
        self.assertEqual(counts['persistent_hits'], 1)

    def test_model_can_recheck_only_ambiguous_time_without_rewriting_unrelated_results(self):
        for visible_time in ('15:11', '17:16'):
            with self.subTest(time=visible_time), TemporaryDirectory() as folder:
                root = Path(folder)
                pictures = [root / 'ambiguous-time.png', root / 'other-photo.png']
                for picture in pictures:
                    Image.new('RGB', (12, 12), 'white').save(picture)
                originals = [picture.read_bytes() for picture in pictures]
                docs = [
                    {'unit_id': 'photo-one', 'roles': ['display_photos'],
                     'facts': ['水印日期2026.10.9及门店；大号数字被初读为编号。'], 'limitations': []},
                    {'unit_id': 'photo-two', 'roles': ['display_photos'],
                     'facts': ['本图日期2026.10.20超出活动周期。'], 'limitations': []},
                ]
                case = {'archive_id': 'a001', 'scenario': 'promotional_display',
                        'flags': flags_for('promotional_display', uses_pos=False),
                        'materials': [{'id': 'display_photos', 'state': 'present',
                                       'source_ids': ['photo-one', 'photo-two']}],
                        'documents': docs,
                        'units': [{'unit_id': doc['unit_id'], 'source_file': picture.name,
                                   'locator': '整张原图', 'native_facts': [], 'limitations': [],
                                   'image': picture} for doc, picture in zip(docs, pictures)]}
                watermark = {'rule_id': 'display_watermark', 'status': 'fail',
                             'reason': '照片一未见拍摄时间；照片二日期超出活动期。',
                             'source_ids': ['photo-one', 'photo-two']}
                brand = {'rule_id': 'display_brand', 'status': 'unknown',
                         'reason': '另一处商品品牌字样看不清。', 'source_ids': ['photo-two']}
                before = {'checks': [watermark, brand], 'photo_reviews': [
                    {'unit_id': 'photo-one', 'checks': [dict(watermark, status='unknown',
                        reason='初读称叠加编号，未见拍摄时间。', source_ids=['photo-one'])]},
                    {'unit_id': 'photo-two', 'checks': [dict(watermark,
                        reason='日期2026.10.20超出活动期。', source_ids=['photo-two'])]}]}
                original = deepcopy(before)
                corrected = deepcopy(before)
                corrected['checks'][0]['reason'] = '照片二日期2026.10.20超出活动期。'
                corrected['photo_reviews'][0]['checks'][0].update(
                    status='pass', reason=f'回看本图原图，水印完整时分为{visible_time}，日期及门店均可对应。')
                provider = PdfEvidenceProvider('fixture', 'medium')
                calls = []

                def call(call_root, skill, schema, prompt, **kwargs):
                    self.assertIn(PHOTO_TIME_READING_GUIDANCE, prompt.format())
                    self.assertIn('数字是否属于水印时间位置确有疑问', prompt.format())
                    calls.append(list(kwargs['images']))
                    answer = {'ready': len(calls) > 1, 'notes': [],
                              **deepcopy(corrected if len(calls) > 1 else before),
                              'image_requests': [pictures[0].name] if len(calls) == 1 else []}
                    kwargs['validator'](answer)
                    return answer

                with collect_model_artifacts(root / 'analysis'), patch.object(provider, '_call', side_effect=call):
                    result = review_stage(provider, case, before, before, root / 'review',
                                          ['display_watermark', 'display_brand'], lambda *args: before,
                                          images=pictures)
                self.assertEqual(calls, [[], [pictures[0]]])
                self.assertEqual(result['photo_reviews'][0]['checks'][0]['status'], 'pass')
                self.assertIn(visible_time, result['photo_reviews'][0]['checks'][0]['reason'])
                self.assertEqual(result['photo_reviews'][1], original['photo_reviews'][1])
                self.assertEqual(result['checks'][1], original['checks'][1])
                self.assertEqual(result['checks'][0]['status'], 'fail')
                self.assertEqual(before, original)
                self.assertEqual([picture.read_bytes() for picture in pictures], originals)
