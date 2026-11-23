"""Isolated Qince line-amount retrieval; no models, services or real case writes."""
from copy import deepcopy
from decimal import Decimal
import json
import unittest

from audit_core.audit_rag import reference_fields, retrieve, ROLE_GROUPS
from audit_core.qince_context import APPLICATIONS, VERIFICATION, context_sources


def settlement_fixture():
    # These are synthetic line identities. Their order/amounts do not establish
    # actual recipients or distinguish store awards from regional awards.
    details, application_rows = [], []
    for index in range(40):
        row_id = str(9000000000000000001 + index)
        amount = '0.00' if 23 <= index < 30 else '500.00'
        details.append({
            'id': f'VERIFY-{index + 1:02}', 'activity_code': 'SQ_SYNTHETIC',
            'activity_cm_id': row_id, 'customer_id': f'CUSTOMER-{index + 1:02}',
            'customer_code': f'CODE-{index + 1:02}', 'customer_name': f'合成客户{index + 1:02}',
            'init_amount': amount, 'audit_amount': amount,
        })
        application_rows.append({
            'subId': row_id, 'cusId': details[-1]['customer_id'],
            'cusCode': details[-1]['customer_code'], 'cusName': details[-1]['customer_name'],
            'quantity': '1', 'unitPrice': '500.00', 'applyAmount': '500.00',
        })
    return {
        VERIFICATION: [{
            'verify_code': 'HX_SYNTHETIC', 'verify_status': '3', 'reverse_status': '0',
            'verify_amount': '16500.00', 'details': details,
        }],
        APPLICATIONS: [{'applyCode': 'SQ_SYNTHETIC', 'subList': application_rows}],
    }


def fields(document):
    return {key: json.loads(value) for fact in document['facts']
            for key, _, value in [fact.partition('=')]}


class SettlementVerificationRetrievalTests(unittest.TestCase):
    def setUp(self):
        self.raw = settlement_fixture()
        self.units, self.documents = context_sources(self.raw, 'HX_SYNTHETIC', 'synthetic')
        self.labels = {unit['unit_id']: unit['source_file'] for unit in self.units}

    def select(self, group):
        return retrieve(self.documents, [], self.units, group, ROLE_GROUPS[group][0])

    def verification_lines(self, documents):
        return [document for document in documents
                if self.labels[document['unit_id']] == '勤策核销明细']

    def test_all_forty_lines_keep_exact_identity_and_original_amount_facts(self):
        before_raw, before_docs, before_units = map(deepcopy, (self.raw, self.documents, self.units))
        originals = {document['unit_id']: document for document in self.documents}
        lines = self.verification_lines(self.select('settlement'))
        self.assertEqual(len(lines), 40)
        for document, row in zip(lines, self.raw[VERIFICATION][0]['details']):
            self.assertEqual(fields(document), {'verify_code': 'HX_SYNTHETIC', **row})
            self.assertEqual(document['facts'], originals[document['unit_id']]['facts'])
            self.assertEqual(document['unit_id'], originals[document['unit_id']]['unit_id'])
        first, last = [fields(document) for document in lines[:30]], [fields(document) for document in lines[30:]]
        self.assertEqual([row['init_amount'] for row in first], ['500.00'] * 23 + ['0.00'] * 7)
        self.assertEqual([row['init_amount'] for row in last], ['500.00'] * 10)
        self.assertEqual(sum(Decimal(row['init_amount']) for row in first), Decimal('11500.00'))
        self.assertEqual(sum(Decimal(row['init_amount']) for row in last), Decimal('5000.00'))
        self.assertEqual((self.raw, self.documents, self.units), (before_raw, before_docs, before_units))

    def test_application_quantities_remain_separate_from_claim_line_amounts(self):
        selected = self.select('settlement')
        application_rows = [fields(document) for document in selected
                            if self.labels[document['unit_id']] == '勤策申请明细']
        self.assertEqual(len(application_rows), 40)
        self.assertTrue(all(row['quantity'] == '1' and row['unitPrice'] == '500.00'
                            for row in application_rows))
        # No derived persons, award categories or replacement quantities are
        # created merely because some line amounts equal one application unit.
        for document in self.verification_lines(selected):
            self.assertEqual(set(fields(document)), {
                'verify_code', 'id', 'activity_code', 'activity_cm_id',
                'customer_id', 'customer_code', 'customer_name', 'init_amount', 'audit_amount',
            })
        index = reference_fields(selected, self.units)
        self.assertEqual(index['application_quantity']['state'], 'available')
        self.assertEqual([entry['value'] for entry in index['application_quantity']['values']], ['1'])
        self.assertEqual(index['approved_total']['state'], 'missing')

    def test_latest_node_amount_is_not_replaced_by_submission_or_final_approval(self):
        row = self.raw[VERIFICATION][0]['details'][0]
        row['audit_amount'] = '450.1230'
        self.units, self.documents = context_sources(self.raw, 'HX_SYNTHETIC', 'synthetic')
        selected = self.select('settlement')
        first = fields(self.verification_lines(selected)[0])
        self.assertEqual(first['init_amount'], '500.00')
        self.assertEqual(first['audit_amount'], '450.1230')
        self.assertEqual(reference_fields(selected, self.units)['approved_total']['state'], 'missing')

    def test_budget_and_pos_groups_do_not_receive_verification_line_amounts(self):
        for group in ('budget', 'pos', 'pos_quantity', 'pos_settlement'):
            with self.subTest(group=group):
                selected = self.select(group)
                self.assertEqual(self.verification_lines(selected), [])
                self.assertFalse(any(fact.startswith(('init_amount=', 'audit_amount='))
                                     for document in selected for fact in document['facts']))
        budget = self.select('budget')
        workflow = next(fields(document) for document in budget
                        if self.labels[document['unit_id']] == '勤策核销审批状态')
        self.assertEqual(workflow['verify_status'], '3')
        self.assertEqual(workflow['verify_amount'], '16500.00')
        self.assertEqual(reference_fields(budget, self.units)['approved_total']['state'], 'missing')

    def test_current_case_scope_still_excludes_foreign_verification_lines(self):
        foreign = deepcopy(self.raw[VERIFICATION][0])
        foreign.update(verify_code='HX_FOREIGN')
        for row in foreign['details']:
            row.update(id='FOREIGN-' + row['id'], customer_name='OTHER_CASE_CUSTOMER', init_amount='999.99')
        self.raw[VERIFICATION].append(foreign)
        self.units, self.documents = context_sources(self.raw, 'HX_SYNTHETIC', 'synthetic')
        selected = self.select('settlement')
        self.assertEqual(len(self.verification_lines(selected)), 40)
        content = json.dumps(selected, ensure_ascii=False)
        for forbidden in ('HX_FOREIGN', 'FOREIGN-', 'OTHER_CASE_CUSTOMER', '999.99'):
            self.assertNotIn(forbidden, content)


if __name__ == '__main__':
    unittest.main()
