"""Stage facts and read-only tool requests for model-led self-checks."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
import json
import re

from .common import AuditError

SELF_CHECK_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['version', 'stage', 'rule_ids', 'field_checks', 'rework', 'can_advance', 'checker_model_calls'],
    'properties': {
        'version': {'const': 1}, 'stage': {'type': 'string'},
        'rule_ids': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
        'field_checks': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False,
            'required': ['rule_id', 'field', 'input_state', 'result_status', 'source_ids', 'state'],
            'properties': {'rule_id': {'type': 'string'}, 'field': {'type': 'string'},
                'input_state': {'type': 'string'},
                'result_status': {'enum': ['pass', 'fail', 'unknown', 'not_applicable']},
                'source_ids': {'type': 'array', 'items': {'type': 'string'}},
                'state': {'enum': ['checked', 'needs_rework', 'not_applicable', 'limited']}}}},
        'rework': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
            'required': ['rule_id', 'code', 'instruction'], 'properties': {
                'rule_id': {'type': 'string'}, 'code': {'enum': ['field_not_used', 'amount_scope', 'language', 'tool_request']},
                'instruction': {'type': 'string'}}}},
        'can_advance': {'type': 'boolean'}, 'checker_model_calls': {'const': 0},
    },
}

class StageGatePending(AuditError):
    """A quality stage must be reworked, not silently marked as passed."""
    code = 'stage_rework_pending'

    def __init__(self, stage, rule_ids):
        self.stage = stage
        self.rule_ids = list(rule_ids)
        super().__init__('本阶段自测尚未通过，定向返工后仍有待处理条目，已保留断点，未继续下一阶段。')


def money_context(case):
    """All linked original line amounts, distinctly labelled from header totals.

    This sum proves only the selected linked rows. It is never a made-up
    application header amount or final approved amount.
    """
    units = {u['unit_id']: u for u in case.get('units', [])}
    rows, states, conflicts = {}, [], False
    for document in case['documents']:
        unit = units.get(document['unit_id'], {})
        if unit.get('source_kind') != 'qince':
            continue
        fields = {}
        for fact in document['facts']:
            key, sep, raw = fact.partition('=')
            if sep:
                try: fields[key] = json.loads(raw)
                except (TypeError, ValueError): pass
        if unit.get('source_file') == '勤策核销审批状态':
            states.append({'unit_id': document['unit_id'], 'fields': fields})
        if unit.get('source_file') != '勤策申请明细' or fields.get('applyAmount') in (None, ''):
            continue
        key = (str(fields.get('applyCode') or ''), str(fields.get('subId') or ''))
        if not all(key):
            continue
        try:
            amount = Decimal(str(fields['applyAmount']).replace(',', ''))
            if not amount.is_finite():continue
        except InvalidOperation:
            continue
        entry = {'unit_id': document['unit_id'], 'applyCode': key[0], 'subId': key[1],
                 'number': str(fields['applyAmount']),
                 'quote': next(f for f in document['facts'] if f.startswith('applyAmount='))}
        if key in rows:
            if Decimal(rows[key]['number'].replace(',', '')) != amount:
                conflicts = True
            else:
                aliases = rows[key].setdefault('source_ids', [rows[key]['unit_id']])
                if entry['unit_id'] not in aliases:
                    aliases.append(entry['unit_id'])
        else:
            rows[key] = entry
    values = [Decimal(r['number'].replace(',', '')) for r in rows.values()]
    with localcontext() as context:
        context.prec = max(28, sum(len(v.as_tuple().digits) for v in values))
        total = format(sum(values), 'f') if values and not conflicts else None
    return {'scope': '仅本单已精确关联申请行，不能冒充独立主档总额或最终批复',
            'linked_row_count': len(rows), 'linked_rows_sum': total,
            'conflicting_duplicate_rows': conflicts, 'rows': list(rows.values()), 'approval_states': states}


def stage_self_check(value, decision, case, rule_ids, *, stage):
    """Collect facts for the model; prose heuristics never gate a stage.

    Explicit tool requests are actions to fulfil, not quality judgments. Source
    integrity and rule coverage remain transport boundaries. The model owns the
    interpretation of available fields, amount scope and customer language.
    """
    from .audit_rag import reference_fields, needed_fields
    actual = [c['rule_id'] for c in value['checks']]
    if len(actual) != len(set(actual)) or set(actual) != set(rule_ids):
        raise AuditError('阶段审核项遗漏、重复或越界。')
    indexed = reference_fields(case['documents'], case.get('units'))
    evaluated = {c['rule_id']: c for c in decision['checks']}
    fields = []
    for key in rule_ids:
        check = evaluated[key]
        for field in needed_fields([key]):
            fields.append({'rule_id': key, 'field': field, 'input_state': indexed[field]['state'],
                'result_status': check['status'], 'source_ids': list(check['source_ids']),
                'state': 'not_applicable' if check['status'] == 'not_applicable' else
                         'limited' if check['status'] == 'unknown' else 'checked'})
    requests = []
    for tool in value.get('tool_requests', []):
        for key in tool['rule_ids']:
            if key not in rule_ids:
                raise AuditError('工具补查不能混入其他阶段。')
            if not any(item['rule_id'] == key for item in requests):
                requests.append({'rule_id': key, 'code': 'tool_request',
                    'instruction': '读取本次申请的只读工具结果，自行判断是否修正本项。'})
    return {'version': 1, 'stage': stage, 'rule_ids': list(rule_ids),
        'field_checks': fields, 'rework': requests, 'can_advance': not requests,
        'checker_model_calls': 0}


def repair_documents(case, rule_ids):
    from .audit_rag import ROLE_GROUPS, retrieve
    from collections import OrderedDict
    documents = OrderedDict()
    groups = [name for name, (_, ids) in ROLE_GROUPS.items() if set(rule_ids) & ids]
    if not groups:
        return case['documents']
    for group in groups:
        for d in retrieve(case['documents'], case['materials'], case.get('units'), group, ROLE_GROUPS[group][0]):
            old = documents.setdefault(d['unit_id'], {**d, 'facts': []})
            old['facts'] = list(dict.fromkeys(old['facts'] + d['facts']))
    return list(documents.values())
