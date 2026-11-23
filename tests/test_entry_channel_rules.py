"""Source-bound photo coverage uses the user's channel table, including 50 stores."""
from copy import deepcopy
import unittest

from audit_core.common import AuditError
from audit_core.pdf_evidence import audit_decision, audit_schema
from audit_core.pdf_policy import entry_photo_requirement
from tests.pdf_test_support import classification, flags_for, unknown_checks


def case(total, photo_stores, *, channel=True):
    flags = flags_for('entry_fee', cvs_otc=channel)
    documents = [{'unit_id': 'agreement', 'roles': ['entry_agreement'],
                  'facts': [f'本次进场门店合计{total}家', *[f'本次协议门店：门店{i}' for i in range(1, total + 1)]],
                  'limitations': []},
                 {'unit_id': 'inventory', 'roles': ['entry_system'],
                  'facts': ['全部进场门店及公司进场商品的库存明细，盖章可见'], 'limitations': []}]
    photo_ids = [f'photo{i}' for i in range(len(photo_stores))]
    documents += [{'unit_id': uid, 'roles': ['shelf_photos'], 'facts': [f'{name or "门店不明"}上架照片'],
                   'limitations': []} for uid, name in zip(photo_ids, photo_stores)]
    value = classification('entry_fee', [d['unit_id'] for d in documents], flags=flags)
    materials = next(item['materials'] for item in value['material_matches'] if item['scenario'] == 'entry_fee')
    for item in materials:
        item['source_ids'] = ({'entry_agreement': ['agreement'], 'entry_system': ['inventory'],
                               'shelf_photos': photo_ids}.get(item['id'], [])
                              if item['state'] == 'present' else [])
    evidence = unknown_checks('entry_fee', flags)
    evidence['entry_coverage'] = {
        'total_stores': {'unit_id': 'agreement', 'quote': f'本次进场门店合计{total}家', 'number': str(total)},
        'scope_complete': True, 'scope_source_ids': ['agreement'],
        'stores': [{'name': f'门店{i}', 'source': {'unit_id': 'agreement', 'quote': f'本次协议门店：门店{i}'}}
                   for i in range(1, total + 1)],
        'photos': [{'unit_id': uid, 'store_name': name} for uid, name in zip(photo_ids, photo_stores)],
        'limitations': [],
    }
    units = [{'unit_id': uid, 'image': f'{uid}.png'} for uid in photo_ids]
    return flags, evidence, documents, materials, units


def coverage_decision(*args):
    result = audit_decision('entry_fee', *args[:-1], units=args[-1])
    return next(check for check in result['checks'] if check['rule_id'] == 'entry_photo_coverage')


class EntryChannelRulesTests(unittest.TestCase):
    def test_thresholds_follow_original_image_and_confirmed_50_store_boundary(self):
        expected = {1: ('stores', 1), 19: ('stores', 19), 20: ('stores', 16),
                    21: ('stores', 17), 49: ('stores', 40), 50: ('stores', 40),
                    51: ('stores', 26), 99: ('stores', 50), 100: ('stores', 50),
                    101: ('photos', 50), 1000: ('photos', 50)}
        for total, quota in expected.items():
            with self.subTest(total=total):
                self.assertEqual(entry_photo_requirement(total, True), quota)
                self.assertEqual(entry_photo_requirement(total, False), ('stores', total))
        for total in (0, -1, True, 1.5, '30', None):
            with self.subTest(total=total), self.assertRaises(ValueError):
                entry_photo_requirement(total, True)

    def test_exact_quota_passes_and_one_store_short_fails_despite_model_pass(self):
        for total, minimum in ((19, 19), (20, 16), (30, 24), (50, 40), (51, 26), (100, 50)):
            for supplied in (minimum - 1, minimum):
                values = case(total, [f'门店{i}' for i in range(1, supplied + 1)])
                model_check = next(c for c in values[1]['checks'] if c['rule_id'] == 'entry_photo_coverage')
                model_check.update(status='pass', reason='模型声称数量足够', source_ids=['agreement'])
                with self.subTest(total=total, supplied=supplied):
                    check = coverage_decision(*values)
                    self.assertEqual(check['status'], 'pass' if supplied == minimum else 'fail')
                    self.assertEqual(check['coverage']['minimum'], minimum)

    def test_non_channel_does_not_get_sampling_exemption_even_above_100_stores(self):
        values = case(101, [f'门店{i}' for i in range(1, 51)], channel=False)
        check = coverage_decision(*values)
        self.assertEqual(check['status'], 'fail')
        self.assertEqual(check['coverage']['minimum'], 101)
        inventory = next(c for c in audit_decision('entry_fee', *values[:-1], units=values[-1])['checks']
                         if c['rule_id'] == 'entry_inventory')
        self.assertEqual(inventory['status'], 'not_applicable')

    def test_same_store_multiple_photos_are_one_store_but_above_100_uses_photo_count(self):
        check = coverage_decision(*case(30, ['门店1'] * 24))
        self.assertEqual(check['status'], 'fail')
        self.assertEqual(check['coverage']['actual'], 1)
        for photos in (49, 50):
            check = coverage_decision(*case(101, ['门店1'] * photos))
            self.assertEqual(check['status'], 'pass' if photos == 50 else 'fail')
            self.assertEqual(check['coverage']['unit'], 'photos')

    def test_complete_cited_list_can_be_counted_without_new_required_total_field(self):
        values = case(30, [f'门店{i}' for i in range(1, 25)])
        values[1]['entry_coverage']['total_stores'] = None
        self.assertEqual(coverage_decision(*values)['status'], 'pass')
        values[1]['entry_coverage']['scope_complete'] = False
        self.assertEqual(coverage_decision(*values)['status'], 'unknown')

    def test_unmapped_photos_are_unknown_when_they_could_satisfy_the_quota(self):
        values = case(30, [f'门店{i}' for i in range(1, 24)] + [None])
        self.assertEqual(coverage_decision(*values)['status'], 'unknown')
        values[1]['entry_coverage']['photos'][-1]['store_name'] = '门店24'
        self.assertEqual(coverage_decision(*values)['status'], 'pass')

    def test_counts_cannot_use_inventory_claims_duplicate_stores_or_omit_photos(self):
        original = case(30, [f'门店{i}' for i in range(1, 25)])
        for change in ('claim_count', 'duplicate_store', 'omit_photo', 'invent_store'):
            values = deepcopy(original)
            evidence = values[1]['entry_coverage']
            if change == 'claim_count':
                evidence['total_stores']['unit_id'] = 'inventory'
            elif change == 'duplicate_store':
                evidence['stores'][-1] = evidence['stores'][0]
            elif change == 'omit_photo':
                evidence['photos'].pop()
            else:
                evidence['photos'][0]['store_name'] = '文件名推测门店'
            with self.subTest(change=change), self.assertRaises(AuditError):
                coverage_decision(*values)

    def test_inventory_pass_cannot_cite_only_photos_or_only_agreement(self):
        original = case(30, [f'门店{i}' for i in range(1, 25)])
        for cited in (['agreement'], ['inventory'], ['agreement', 'inventory']):
            values = deepcopy(original)
            check = next(c for c in values[1]['checks'] if c['rule_id'] == 'entry_inventory')
            check.update(status='pass', reason='本次全部门店及公司商品库存明细与协议对应，盖章可见', source_ids=cited)
            if len(cited) == 1:
                with self.subTest(cited=cited), self.assertRaises(AuditError):
                    audit_decision('entry_fee', *values[:-1], units=values[-1])
            else:
                result = audit_decision('entry_fee', *values[:-1], units=values[-1])
                self.assertEqual(next(c for c in result['checks'] if c['rule_id'] == 'entry_inventory')['status'], 'pass')

    def test_production_schema_requires_coverage_only_for_entry_fee(self):
        self.assertIn('entry_coverage', audit_schema('entry_fee')['required'])
        self.assertNotIn('entry_coverage', audit_schema('poster_material')['properties'])
