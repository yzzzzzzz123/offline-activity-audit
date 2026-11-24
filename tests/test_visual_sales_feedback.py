"""Regression cases for per-store coverage, stamp evidence and image fallback."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import Mock, patch

from PIL import Image, ImageDraw

from audit_core.agent_orchestration import owner, rule_specs
from audit_core.independent_image_review import _red_ink_view, run
from audit_core.model_authority import decision, execution
from audit_core.model_self_check import merge_self_check_result
from audit_core.pdf_workflow import build_view
from audit_core.prompts import bound_prompt
from audit_core.summary_review import final_request
from tests.test_optional_staff_and_display_photos import photo_case, review


class VisualFeedbackTests(TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def report(self, checks, photos=()):
        case = photo_case(self.root)
        with execution():
            result = decision(case['scenario'], {'checks': checks, 'photo_reviews': photos}, case['documents'])
            view = build_view([{**case, 'source_archive': 'case.zip', 'result': result}], None)
        return view['sheets'][0]['rows'], final_request(view)

    def test_missing_store_reaches_both_customer_summary_and_main_agent(self):
        missing = '本单申请的凌云大厦复圣店没有对应的陈列照片。'
        late = '锦绣城店拍摄日期超期。'
        photos = [review('p01', status='fail', problem=late), review('p02')]
        check = {'rule_id': 'display_store_coverage', 'status': 'fail',
                 'reason': missing, 'source_ids': ['application']}
        self.assertIn('display_store_coverage', {r['id'] for r in rule_specs('promotional_display')})
        self.assertEqual(owner('display_store_coverage'), 'visual')
        for rule, reason in [('display_store_coverage', missing),
                             ('display_watermark', late + missing)]:
            with self.subTest(rule=rule):
                rows, details = self.report([{**check, 'rule_id': rule, 'reason': reason}], photos)
                self.assertIn('复圣店', details['draft'])
                self.assertTrue(any('复圣店' in issue['text'] for issue in details['problems']))
                self.assertIn(late, details['draft'])
                # A missing or late photo never changes observations about the physical fixture.
                photo = next(r for r in rows if r.get('photo_unit_id') == 'p01')
                self.assertIn('陈列范围对应', photo['values'][1])
                self.assertNotIn('陈列范围对应', details['draft'])
        _, rollup = self.report([{**check, 'rule_id': 'display_watermark', 'reason': late}], photos)
        self.assertEqual(rollup['draft'].count(late), 1)

    def test_two_checks_about_one_stamp_do_not_hide_separate_name_discrepancies(self):
        uncertain = {'status': 'unknown', 'source_ids': ['contract'], 'unreadable_seal_unit_id': 'contract'}
        seal = {**uncertain, 'rule_id': 'contract_signed', 'reason': '本页圆章核心字号叠印，主体暂时不能确认。'}
        dealer = {**uncertain, 'rule_id': 'contract_dealer', 'reason': '同页圆章的经销商名称无法确认。'}
        other = {'rule_id': 'contract_dealer', 'reason': '合同正文填写的经销商名称与申请相差一个字。',
                 'source_ids': ['contract', 'application'], 'status': 'fail'}
        rows, details = self.report([seal, dealer, other])
        self.assertEqual(len(rows), 3)
        self.assertEqual([x['text'] for x in details['problems']], [seal['reason'], other['reason']])
        for replacement in ({**dealer, 'source_ids': ['application']},
                            {**dealer, 'unreadable_seal_unit_id': 'application', 'source_ids': ['application']}):
            _, separate = self.report([seal, replacement])
            self.assertEqual(len(separate['problems']), 2)
        changed = deepcopy(dealer)
        changed.pop('unreadable_seal_unit_id')
        changed['reason'] = '合同正文手写公司名称无法确认。'
        updated = merge_self_check_result({'checks': [dealer]}, {'checks': [changed]})['checks'][0]
        self.assertNotIn('unreadable_seal_unit_id', updated)

    def test_multi_store_rollups_publish_each_missing_store_once(self):
        late = '中心店水印日期超出本次活动周期。'
        photos = [review('p01', status='fail', problem=late), review('p02')]
        reasons = {
            'display_watermark': late + '北街店没有对应的陈列照片。南街店未提交陈列照片。',
            'display_brand': '中心店照片品牌模糊；北街店也没有对应照片，无法核实货品。',
            'display_size': '北街店没有对应照片，其他照片可见围挡。南街店没有对应照片。',
            'display_material_brand': '已提交的照片所示物料均为参半。'}
        checks = [{'rule_id': rule, 'source_ids': ['application', 'p01'],
                   'status': 'pass' if rule == 'display_material_brand' else 'fail',
                   'reason': reason} for rule, reason in reasons.items()]
        independent = {'rule_id': 'display_store_coverage', 'status': 'fail',
                       'reason': '北街店和南街店未上传本次陈列照片。', 'source_ids': ['application']}
        for current in (False, True):
            with self.subTest(current=current):
                case = photo_case(self.root)
                for name in ('北街店', '南街店'):
                    uid = 'store-' + name
                    case['units'].append({**case['units'][1], 'unit_id': uid})
                    case['documents'].append({**case['documents'][1], 'unit_id': uid,
                                              'facts': ['cusName=' + json.dumps(name, ensure_ascii=False)]})
                inputs = [{**c, 'source_ids': c['source_ids'] + ['store-北街店', 'store-南街店']} for c in checks]
                with execution():
                    result = decision(case['scenario'], {'checks': inputs + ([independent] if current else []),
                                                         'photo_reviews': photos}, case['documents'])
                    view = build_view([{**case, 'source_archive': 'case.zip', 'result': result}], None)
                sheet = view['sheets'][0]
                saved_reasons = {c['rule_id']: c['reason'] for c in sheet['photo_rollup_evidence']}
                self.assertEqual(saved_reasons, reasons)
                report = final_request(view)
                for text in (report['draft'], '\n'.join(p['text'] for p in report['problems'])):
                    self.assertEqual(text.count('北街店'), 1)
                    self.assertEqual(text.count('南街店'), 1)
                    self.assertEqual(text.count(late), 1)
                    self.assertNotIn('中心店照片品牌模糊', text)
                coverage = [r for r in sheet['rows'] if r['rule_id'] == 'display_store_coverage']
                self.assertEqual(len(coverage), 1 if current else 2)
                self.assertTrue(all('application' in r['summary_items'][0]['source_ids'] for r in coverage))

    def scan(self):
        path = self.root / 'page.png'
        with Image.new('RGB', (110, 90), 'white') as page:
            draw = ImageDraw.Draw(page)
            draw.rectangle((20, 20, 79, 49), outline='red', width=3)
            draw.line((15, 35, 85, 35), fill='black', width=2)
            page.save(path)
        return path

    def test_red_ink_view_preserves_source_and_suppresses_black_grid(self):
        path = self.scan()
        original = path.read_bytes()
        view = self.root / 'detail' / 'red.png'
        self.assertEqual(_red_ink_view(path, view), (20, 20, 80, 50))
        with Image.open(view) as img:
            self.assertEqual(img.size, (60, 30))
            self.assertEqual(img.getpixel((1, 4)), 0)
            self.assertEqual(img.getpixel((18, 15)), 255)
        _red_ink_view(path, view, 90)
        with Image.open(view) as img:
            self.assertEqual(img.size, (30, 60))
        self.assertEqual(path.read_bytes(), original)
        with Image.new('L', (50, 50), color=50) as mono:
            mono.save(path)
        self.assertIsNone(_red_ink_view(path, self.root / 'unneeded.png'))
        self.assertFalse((self.root / 'unneeded.png').exists())

    def test_optional_view_failure_or_no_request_keeps_original_and_finishes(self):
        source = self.scan()
        for mode in ('success', 'not_requested', 'view_error', 'reader_error'):
            with self.subTest(mode=mode):
                provider = SimpleNamespace(connection=SimpleNamespace(visual=None))
                original = {'facts':['右下可见红色章印。'], 'limitations':['与表格叠印。'],
                            'inspect_red_ink':mode != 'not_requested', 'red_ink_clockwise':90}
                def read(root, skill, schema, prompt, *, images, **kwargs):
                    self.assertEqual(len(images), 1)
                    if images == [source]:
                        return deepcopy(original)
                    if mode == 'reader_error':
                        raise TimeoutError('test')
                    return {'facts':['可辨昆明夏云商贸。'], 'limitations':[]}
                provider._call = Mock(side_effect=read)
                provider._call_stage = lambda root, skill, schema, prompt, **kwargs: json.loads(
                    prompt.partial_variables['readings'])
                with patch('audit_core.independent_image_review._red_ink_view',
                           side_effect=OSError('test') if mode == 'view_error' else _red_ink_view):
                    readings = run(provider, self.root / mode, self.root, {}, bound_prompt('核对页码'),
                                   images=[source, source], label='原图复看', validator=lambda x:x)
                self.assertEqual(provider._call.call_count, 2 if mode in {'success','reader_error'} else 1)
                self.assertEqual(readings[1]['same_bytes_as'], source.name)
                self.assertEqual(readings[0]['facts'], original['facts'])
                self.assertEqual('seal_detail' in readings[0], mode == 'success')
                if mode == 'success':
                    self.assertEqual(readings[0]['seal_detail'], readings[1]['seal_detail'])
                    self.assertEqual(readings[0]['seal_detail']['source_sha256'], hashlib.sha256(source.read_bytes()).hexdigest())
