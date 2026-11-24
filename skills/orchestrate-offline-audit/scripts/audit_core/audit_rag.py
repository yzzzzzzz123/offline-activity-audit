"""Run-local, role-based evidence retrieval. No vector DB, network or old answers.

Retrieve complete sources for each rule, never similarity top-k or a prefix of
the file. Raw facts/IDs remain the authority; a partial packet cannot establish
that a material is missing. Cross-source arithmetic keeps its full operand set.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import json
import re

from .common import AuditError

CONTEXT_BYTES = 48_000
APPLICATION_LABELS = {'勤策申请明细', '勤策申请主档', '勤策进场关联明细'}
EXTRA_OWNER = {
    'payment_dates': {'payment_activity_dates'}, 'target_tiers': {'target_reward'},
    'activity_dates': {'settlement_activity_dates', 'photo_activity_dates'},
    'price_photo_reviews': {'price_evidence'}, 'entry_coverage': {'entry_photo_coverage'},
    'giveaway_coverage': {'giveaway_evidence'},
}
# Explicit dependencies are conservative: an unregistered rule receives the
# entire case. Shared/multi-role pages remain whole, not sentence-classified.
ROLE_GROUPS = {
    'stamped_formats': ({'promotion_contract', 'settlement', 'pos', 'payment', 'entry_agreement', 'entry_system'},
        {'stamped_document_pdf'}),
    'gift_execution': ({'gift_activity_evidence', 'gift_finished_photos', 'gift_store_photos', 'pos', 'pos_excel', 'activity_application'},
        {'gift_activity_evidence'}),
    'documents': ({'promotion_contract', 'settlement', 'pos', 'activity_application', 'payment', 'invoice'},
        {'submitted_material_scope', 'contract_signed', 'contract_dealer', 'settlement_seal', 'settlement_template_seal', 'receipt_finance_seal',
         'settlement_dealer', 'pos_seal', 'pos_dealer', 'payment_statement_seal', 'payment_statement_dealer'}),
    'dates': ({'promotion_contract', 'settlement', 'activity_application', 'price_evidence'},
        {'contract_activity_dates', 'settlement_activity_dates', 'settlement_application_dates', 'photo_activity_dates'}),
    'budget': ({'promotion_contract', 'settlement', 'activity_application', 'approved_claim'},
        {'contract_budget_amount', 'claim_ceiling', 'application_claim_ceiling'}),
    'pos': ({'pos_excel'}, {'pos_fields', 'pos_arithmetic', 'pos_product_identity'}),
    'settlement': ({'promotion_contract', 'settlement', 'activity_application', 'gift_rule'},
        {'settlement_calculation', 'reward_unit_price', 'contract_reward_unit_price', 'application_quantity_price',
         'staff_headcount', 'staff_total_unit_price',
         'gift_rule_present', 'display_quantity_price'}),
    'pos_settlement': ({'promotion_contract', 'settlement', 'activity_application', 'pos_excel', 'gift_rule'},
        {'application_products', 'gift_rule_quantity', 'target_reward', 'pos_sales_amount', 'pos_activity_dates'}),
    'pos_quantity': ({'settlement', 'pos_excel'}, {'settlement_pos_quantity'}),
    'payment': ({'payment', 'promotion_contract', 'settlement', 'activity_application'},
        {'payment_company', 'payment_claim_amount', 'payment_activity_dates', 'payment_attribution'}),
    'invoice': ({'invoice', 'promotion_contract', 'settlement', 'material_list', 'activity_application'},
        {'invoice_details', 'invoice_amount', 'material_list_amount', 'poster_payment_dates'}),
    'photos': ({'promotion_contract', 'settlement', 'activity_application', 'display_photos', 'atrium_agreement',
                'finished_photos', 'giveaway_evidence', 'price_evidence', 'full_reduction_evidence',
                'gift_finished_photos', 'gift_store_photos', 'staff_photos', 'training_photos'},
        {'display_watermark', 'display_brand', 'display_material_brand', 'display_size', 'atrium_agreement',
         'finished_photo', 'posting_before_after', 'giveaway_evidence', 'activity_photo_content',
         'full_reduction_evidence', 'price_evidence', 'price_matches_contract', 'gift_finished_photos',
         'gift_store_photo_content', 'staff_photo_content', 'training_photo_content'}),
    'entry': ({'entry_agreement', 'entry_system', 'shelf_photos', 'activity_application', 'pos', 'pos_excel'},
        {'entry_agreement', 'entry_duplicate', 'entry_photo_coverage', 'entry_inventory', 'entry_watermark',
         'entry_sku', 'entry_application_products'}),
}
DEALER_RULES = {'contract_dealer', 'settlement_dealer', 'pos_dealer', 'payment_statement_dealer'}
PERIOD_RULES = {'contract_activity_dates', 'settlement_application_dates', 'payment_activity_dates',
                'display_watermark', 'entry_watermark'}
FIELD_STATES = ['available', 'multiple', 'missing', 'empty', 'incomplete', 'invalid']
FIELD_RULES = {
    'advance_dealer': DEALER_RULES | {'invoice_details'},
    'activity_period': PERIOD_RULES,
    'application_quantity': {'application_quantity_price', 'staff_headcount', 'display_quantity_price'},
    'application_unit_price': {'application_quantity_price', 'reward_unit_price', 'contract_reward_unit_price', 'staff_total_unit_price', 'display_quantity_price'},
    'application_products': {'application_products', 'entry_application_products', 'entry_sku'},
    'application_total': {'contract_budget_amount', 'application_claim_ceiling'},
    'approved_total': {'claim_ceiling'},
}
REFERENCE_PROMPT = (
    '\n结构化来源核对：reference_reads逐项返回程序索引的field/state/source_ids，已返回但空值不是missing；'
    'multiple只说明不同参照并存，不自动表示冲突或不合格。每个不同值都须有对应来源，不能只选择一个值。'
    'contract_dealer、settlement_dealer、pos_dealer、payment_statement_dealer、contract_activity_dates、'
    'settlement_application_dates各自填写reference_evidence。written_name为该资料填写的经销商名称，'
    'seal_name为该资料印章里的经销商名称，各含unit_id/quote/value；quote从本张facts逐字复制，'
    'value为其中真实完整名称，不改错字、不丢后缀；印章短引用须能定位到同一条原始印章事实。'
    '只看到填写名称不能推定落章名称，不能用申请名称补写原图；无法辨认为null。'
    'pos_dealer只核对seal_name，written_name=null。日期项的periods逐条列该资料全部活动周期的unit_id/quote，'
    '不能选签章日、签约日或提交日代替活动周期，不漏掉不一致的另一页；名称项periods=[]，日期项两个名称均null。'
    '本单申请名称和日期由程序按真实字段、来源角色及原文核对；最终比较仍执行原标准。'
    '其他审核项不需要reference_evidence，不新增必交资料。\n'
    '勤策核销审批状态是已取得的独立事实：verify_status为0待审批、1已审批、2被打回、3审批中、4已作废、5已撤回；'
    'verify_amount是最近审批节点金额，不因金额与申报一致就称最终批复。只有已审批且无红冲才可作最终节点金额；'
    '审批中应说明当前流程状态和当前节点金额，不把尚未形成最终批复写成客户未提供资料。\n')
TEXT = {'type': 'string', 'minLength': 1}
_VISIBLE_FIELD = {'type': 'object', 'additionalProperties': False, 'required': ['state', 'fact_indexes'],
    'properties': {'state': {'enum': ['present', 'absent', 'unclear', 'not_applicable']},
                   'fact_indexes': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'integer', 'minimum': 0}}},
    'allOf': [{'if': {'properties': {'state': {'const': 'present'}}, 'required': ['state']},
               'then': {'properties': {'fact_indexes': {'minItems': 1}}}},
              {'if': {'properties': {'state': {'enum': ['absent', 'not_applicable']}}, 'required': ['state']},
               'then': {'properties': {'fact_indexes': {'maxItems': 0}}}}]}
VISIBLE_FIELD_INVENTORY_SCHEMA = {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
    'required': ['unit_id', 'written_dealer_name', 'seal_name', 'activity_period'],
    'properties': {'unit_id': TEXT, **{key: _VISIBLE_FIELD for key in
        ('written_dealer_name', 'seal_name', 'activity_period')}}}}


def validate_visible_inventory(value):
    """The visible fields must survive into the source facts, not just a sidecar."""
    import jsonschema
    inventory = value['field_inventory']
    jsonschema.Draft202012Validator(VISIBLE_FIELD_INVENTORY_SCHEMA).validate(inventory)
    documents = {d['unit_id']: d for d in value['documents']}
    if len(inventory) != len(documents) or {r['unit_id'] for r in inventory} != set(documents):
        raise AuditError('读图关键字段盘点必须完整覆盖本批每张原图，不得漏页、重复或混用。')
    for row in inventory:
        facts = documents[row['unit_id']]['facts']
        beyond = []
        for key in ('written_dealer_name', 'seal_name', 'activity_period'):
            bad = [index for index in row[key]['fact_indexes'] if index >= len(facts)]
            if bad:
                beyond.append({'unit_id': row['unit_id'], 'field': key,
                               'facts_count': len(facts), 'invalid_indexes': bad[:3]})
        if beyond:
            detail = dumps({'out_of_range': beyond[:4]})
            raise AuditError('读图关键字段未完整进入本张原始事实；fact_indexes必须指向本张facts中真实存在的条目，'
                             '请把对应名称、印章或活动周期的原文补进facts，或将state改为absent/unclear并清空indexes。'
                             + detail)


_INVENTORY_FIELD_MARKER = {
    'written_dealer_name': re.compile(r'(经销商|客户名称|购货单位|收货单位|门店名称|单位名称)'),
    'seal_name': re.compile(
        r'(印章|印文|盖章|公章|财务(?:专用)?章|合同(?:专用)?章|结算单(?:专用)?章|'
        r'业务(?:专用)?章|部门(?:专用)?章|专用章|落章|章内|章面|章样|圆章|方章|五角星)'
    ),
    'activity_period': re.compile(
        r'(\d{4}\s*[年./-]\s*\d{1,2}|\d{1,2}\s*月\s*\d{1,2}\s*日).{0,60}?(至|到|—|–|-|~)'
    ),
}


def repair_visible_inventory(value):
    """把越界的 fact_indexes 按原文定位纠正，返回纠错说明。

    该盘点只用于证明关键字段原文确实进入本张 facts；下标算错属于记账错误，
    不应让整单核销失败。无法定位时不臆造来源：把 'present' 降为 'unclear'
    并清空下标，由调用方把说明写进 limitations 留痕。
    """
    notes = []
    documents = {d.get('unit_id'): d for d in value.get('documents') or [] if isinstance(d, dict)}
    for row in value.get('field_inventory') or []:
        if not isinstance(row, dict):
            continue
        document = documents.get(row.get('unit_id'))
        if document is None:
            continue
        facts = document.get('facts') or []
        for key in ('written_dealer_name', 'seal_name', 'activity_period'):
            entry = row.get(key)
            if not isinstance(entry, dict):
                continue
            indexes = [i for i in entry.get('fact_indexes') or []
                       if isinstance(i, int) and not isinstance(i, bool)]
            invalid = [i for i in indexes if not 0 <= i < len(facts)]
            if not invalid:
                continue
            marker = _INVENTORY_FIELD_MARKER[key]
            repaired = [i for i in indexes if 0 <= i < len(facts)]
            unresolved = []
            for index in invalid:
                candidate = None
                for shift in (-1, 1, -2, 2):
                    shifted = index + shift
                    if 0 <= shifted < len(facts) and shifted not in repaired \
                            and marker.search(facts[shifted]):
                        candidate = shifted
                        break
                if candidate is None:
                    matched = [j for j, text in enumerate(facts)
                               if j not in repaired and marker.search(text)]
                    if len(matched) == 1:
                        candidate = matched[0]
                if candidate is None:
                    unresolved.append(index)
                else:
                    repaired.append(candidate)
            entry['fact_indexes'] = sorted(set(repaired))
            if not entry['fact_indexes'] and entry.get('state') == 'present':
                entry['state'] = 'unclear'
            notes.append({
                'unit_id': row.get('unit_id'),
                'field': key,
                'facts_count': len(facts),
                'invalid_indexes': invalid,
                'repaired_indexes': entry['fact_indexes'],
                'unresolved_indexes': unresolved,
            })
    return notes


QUOTED_NAME = {'type': 'object', 'additionalProperties': False,
    'required': ['unit_id', 'quote', 'value'], 'properties': {'unit_id': TEXT, 'quote': TEXT, 'value': TEXT}}
QUOTED_PERIOD = {'type': 'object', 'additionalProperties': False,
    'required': ['unit_id', 'quote'], 'properties': {'unit_id': TEXT, 'quote': TEXT}}
REFERENCE_EVIDENCE_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['written_name', 'seal_name', 'periods'], 'properties': {
        'written_name': {'anyOf': [QUOTED_NAME, {'type': 'null'}]},
        'seal_name': {'anyOf': [QUOTED_NAME, {'type': 'null'}]},
        'periods': {'type': 'array', 'items': QUOTED_PERIOD}}}
_REFERENCE_VALUE = {'type': 'object', 'additionalProperties': False,
    'required': ['value', 'source_fields', 'source_ids'], 'properties': {
        'value': {}, 'source_fields': {'type': 'object', 'minProperties': 1},
        'source_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': TEXT}}}
_REFERENCE_FIELD = {'type': 'object', 'additionalProperties': False, 'required': ['state', 'values'],
    'properties': {'state': {'type': 'string', 'enum': FIELD_STATES},
                   'values': {'type': 'array', 'items': _REFERENCE_VALUE}}}
REFERENCE_INPUT_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema', 'type': 'object', 'additionalProperties': False,
    'required': list(FIELD_RULES), 'properties': {field: _REFERENCE_FIELD for field in FIELD_RULES}}
_FIELD_KEYS = {
    'advance_dealer': ['dealerName', 'advanceDealerName', 'dealerCode', 'dealerId', 'extColumns'],
    'activity_period': ['startDate', 'endDate', 'linked_application_rows'],
    'application_quantity': ['quantity', 'extColumns'], 'application_unit_price': ['unitPrice', 'extColumns'],
    'application_products': ['pdCodes', 'pdNames', 'pdIds', 'barcode', 'extColumns'],
    'application_total': ['applyAmount', 'totalAmount', 'budgetAmount', 'extColumns', 'linked_application_rows'],
    'approved_total': ['approvedAmount', 'approvalAmount', 'approvedTotalAmount', '批复金额合计', 'extColumns',
                       'verify_amount', 'verify_status', 'reverse_status', 'audit_time']}
for _field, _keys in _FIELD_KEYS.items():
    REFERENCE_INPUT_SCHEMA['properties'][_field] = deepcopy(_REFERENCE_FIELD)
    REFERENCE_INPUT_SCHEMA['properties'][_field]['properties']['values']['items']['properties']['source_fields'].update(
        additionalProperties=False, properties={key: {} for key in _keys})


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _fields(document):
    result = {}
    for fact in document.get('facts', []):
        key, sep, raw = fact.partition('=')
        if sep:
            try:
                value = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if key in result and result[key] != value:
                return {}  # Never resolve a conflict by overwriting it.
            result[key] = value
    return result


def reference_fields(documents, units):
    """Small typed index, preserving every distinct value and source binding."""
    by_id = {u['unit_id']: u for u in units or []}
    groups = {field: OrderedDict() for field in FIELD_RULES}
    def add(field, value, original, uid):
        entry = groups[field].setdefault(dumps({'value': value, 'source_fields': original}),
            {'value': value, 'source_fields': original, 'source_ids': []})
        if uid not in entry['source_ids']:
            entry['source_ids'].append(uid)
    for document in documents:
        unit = by_id.get(document['unit_id'], {})
        if unit.get('source_kind') != 'qince':
            continue
        values = _fields(document)
        uid = document['unit_id']
        if unit.get('source_file') == '勤策核销批复':
            for key in ('approvedAmount', 'approvalAmount', 'approvedTotalAmount', '批复金额合计', 'verify_amount'):
                if key in values:
                    if key == 'verify_amount' and (str(values.get('verify_status')) != '1'
                                                   or str(values.get('reverse_status')) != '0'):
                        continue
                    original = {key: values[key]}
                    if key == 'verify_amount':
                        original.update({k: values[k] for k in ('verify_status', 'reverse_status', 'audit_time') if k in values})
                    add('approved_total', values[key], original, uid)
            for column in values.get('extColumns', []):
                if column.get('ext_key') == '批复金额合计':
                    add('approved_total', column.get('ext_value'), {'extColumns': [column]}, uid)
            continue
        if unit.get('source_file') not in APPLICATION_LABELS:
            continue
        # A real API name is the advance dealer, not the applicant/customer.
        for key in ('dealerName', 'advanceDealerName'):
            if key in values:
                add('advance_dealer', values[key], {k: values[k] for k in
                    ('dealerName', 'advanceDealerName', 'dealerCode', 'dealerId') if k in values}, uid)
        if 'startDate' in values or 'endDate' in values:
            original = {k: values[k] for k in ('startDate', 'endDate') if k in values}
            add('activity_period', {'start': values.get('startDate'), 'end': values.get('endDate')}, original, uid)
        for field, keys in (('application_quantity', ('quantity',)), ('application_unit_price', ('unitPrice',)),
                            ('application_products', ('pdCodes', 'pdNames', 'pdIds', 'barcode'))):
            for key in keys:
                if key in values:
                    add(field, values[key], {key: values[key]}, uid)
        # A line amount is not an independently recorded application total.
        if unit.get('source_file') == '勤策申请主档':
            for key in ('applyAmount', 'totalAmount', 'budgetAmount'):
                if key in values:
                    add('application_total', values[key], {key: values[key]}, uid)
        for column in values.get('extColumns', []):
            label = column.get('ext_key')
            field = {'数量': 'application_quantity', '单价': 'application_unit_price',
                     '活动SKU': 'application_products', '条形码': 'application_products',
                     '费用代垫经销商': 'advance_dealer', '申请总金额': 'application_total',
                     '活动申请总金额': 'application_total'}.get(label)
            if field:
                value = {k: column[k] for k in ('ext_value', 'ext_value_name', 'ext_value_code') if k in column}
                add(field, value, {'extColumns': [column]}, uid)
        # Grouped entry records retain their original application row links.
        for row in values.get('linked_customer', {}).get('linked_application_rows', []):
            if row.get('startDate') and row.get('endDate'):
                add('activity_period', {'start': row['startDate'], 'end': row['endDate']},
                    {'linked_application_rows': [row]}, uid)
    if not groups['application_total'] or all(e['value'] in (None, '', [])
                                             for e in groups['application_total'].values()):
        from .qince_amount_basis import linked_application_basis
        basis = linked_application_basis(documents, units)
        if basis is not None:
            groups['application_total'].clear()
            derived = {'linked_application_rows': {k:v for k,v in basis.items() if k != 'terms'}}
            for term in basis['terms']:
                add('application_total', basis['total'], derived, term['unit_id'])
    def populated(value):
        if isinstance(value, dict):
            return any(populated(v) for v in value.values())
        return value not in (None, '', [])
    def state(field, entries):
        from datetime import date
        if not entries:
            return 'missing'
        nonempty = [e for e in entries if populated(e['value'])]
        if not nonempty:
            return 'empty'
        if field == 'activity_period':
            for e in nonempty:
                if not e['value'].get('start') or not e['value'].get('end'):
                    return 'incomplete'
                try:
                    start, end = (date.fromisoformat(e['value'][key][:10]) for key in ('start', 'end'))
                    if end < start:
                        return 'invalid'
                except (ValueError, TypeError):
                    return 'invalid'
        if len(nonempty) != len(entries):
            return 'incomplete'
        return 'available' if len({dumps(e['value']) for e in entries}) == 1 else 'multiple'
    result = {field: {'state': state(field, list(values.values())), 'values': list(values.values())}
              for field, values in groups.items()}
    import jsonschema
    jsonschema.Draft202012Validator(REFERENCE_INPUT_SCHEMA).validate(result)
    return result


def needed_fields(rule_ids):
    return [field for field, members in FIELD_RULES.items() if set(rule_ids) & members]


def reference_prompt(fields):
    """Only describe the reference fields this stage actually uses."""
    if not fields:
        return ''
    text = ('\n结构化来源核对：reference_reads只返回本批登记的field/state/source_ids，实际空值不是missing；'
            '每个不同值使用真实对应来源，不根据字段缺失新增客户资料要求。已有参照但未用必须返工；'
            '没有对应辅助字段不额外对齐，客户总结只写资料自身的具体问题。')
    if set(fields) & {'advance_dealer','activity_period'}:
        text += ('仅本批已登记的经销商/周期比较项填写reference_evidence。written_name、seal_name各为unit_id/quote/value或null；'
                 'periods为unit_id/quote数组，只引用当前被检查文书自己的活动周期：合同项只填合同来源，结算项只填结算来源。'
                 '不要在periods里重复填写勤策startDate/endDate；勤策参照由程序从字段索引读取。名称项periods=[]，周期项两个名称为null。'
                 '原文须逐字连续复制本张资料，不用申请名称补写原图印章。POS只核对seal_name，written_name=null。'
                 'seal_name.quote可为同一条原始印章事实的连续短引用，程序结合其所在事实判断来源；填写名称不能冒充印章证据。'
                 '不能用签约日期代替活动周期，不漏掉同份资料另一处不同周期。')
    if 'application_total' in fields:
        from .qince_amount_basis import APPLICATION_TOTAL_CLARIFICATION
        text += ('申请金额可为真实原始总额，或明确标注的程序完整关联行合计。后者由程序回填全部原行算式，'
                 '不能拿派生值伪造原始quote，也不能因没有独立单头就说无法核对。')
        text += APPLICATION_TOTAL_CLARIFICATION
    if 'approved_total' in fields:
        text += '只核对本阶段已实际取得的有效批复，不以最终审批完成为核销前置。'
    if set(fields) & {'application_quantity','application_unit_price','application_products'}:
        text += ('数量、单价、商品字段分别按实际含义和同一项目/单位使用；商品基础身份用项目商品库，'
                 '不因勤策少辅助字段否定既有查库结果，不把计划销量当作实际POS销量。')
    return text


_ROW_IDENTITY = ('verification_row_id', 'activity_code', 'activity_cm_id', 'startDate', 'endDate')


def _fold_values(values):
    """One entry per distinct value, keeping every real source binding."""
    folded = OrderedDict()
    for item in values:
        signature = dumps(item['value'])
        entry = folded.get(signature)
        if entry is None:
            entry = folded[signature] = {'value': item['value'],
                'source_fields': item['source_fields'], 'source_ids': []}
        for uid in item['source_ids']:
            if uid not in entry['source_ids']:
                entry['source_ids'].append(uid)
    return list(folded.values())


def _row_identity(source_fields):
    """Keep which original row is meant, drop the copy of the same row body."""
    rows = source_fields.get('linked_application_rows')
    if not isinstance(rows, list):
        return source_fields
    return {'linked_application_rows': [{key: row[key] for key in _ROW_IDENTITY if key in row}
                                        for row in rows]}


def reference_fold(fields):
    """Same values folded once, with every source binding kept for validation."""
    return {key: {'state': info['state'], 'values': _fold_values(info['values'])}
            for key, info in fields.items()}


def reference_view(fields):
    """Model-side projection: one entry per distinct value, not per source row.

    A grouped entry record repeats the same application period (and the same
    amounts) for every linked row; sending each row body again adds no distinct
    value. ``equivalent_source_count`` states how many original bindings the
    entry stands for, so one row is never presented as the only source. Rows,
    numbers and unit_ids all stay in the case documents and in the program-side
    ``reference_fields`` index used for validation.
    """
    view = {}
    for key, info in fields.items():
        view[key] = {'state': info['state'], 'values': [
            {'value': item['value'],
             'source_fields': _row_identity(item['source_fields']),
             'source_ids': item['source_ids'][:1],
             'equivalent_source_count': len(item['source_ids'])}
            for item in _fold_values(info['values'])]}
    return view


def reference_schema(fields):
    """Request-specific read receipts: present values cannot be called missing."""
    choices = []
    for field, info in reference_fold(fields).items():
        source_ids = sorted({uid for item in info['values'] for uid in item['source_ids']})
        required_values = [{'contains': {'enum': row['source_ids']}} for row in info['values']]
        choices.append({'type': 'object', 'additionalProperties': False,
            'required': ['field', 'state', 'source_ids'], 'properties': {
                'field': {'const': field}, 'state': {'const': info['state']},
                'source_ids': {'type': 'array', 'uniqueItems': True,
                    **({'minItems': 1, 'items': {'type': 'string', 'enum': source_ids}, 'allOf': required_values} if source_ids else
                       {'maxItems': 0, 'items': {'type': 'string'}})}}})
    return {'type': 'array', 'maxItems': len(fields),
            'items': {'anyOf': choices} if choices else {'type': 'object'}}


def validate_reads(value, fields, *, require_all=True):
    reads = value.get('reference_reads', [])
    ids = [item['field'] for item in reads]
    if (len(ids) != len(set(ids)) or not set(ids).issubset(fields) or
            (require_all and set(ids) != set(fields))):
        raise AuditError('结构化参照字段漏读或重复；须逐项返回本批字段状态及真实来源。')
    for item in reads:
        info = fields[item['field']]
        allowed = {uid for row in info['values'] for uid in row['source_ids']}
        if (item['state'] != info['state'] or not set(item['source_ids']).issubset(allowed)
                or (bool(allowed) != bool(item['source_ids']))
                or any(not set(row['source_ids']).intersection(item['source_ids']) for row in info['values'])):
            raise AuditError('已有勤策字段不能写为未取得；请按本批字段索引核对并引用对应申请来源。')


def constrain_read_check(check, fields):
    """Do not turn an unfinished comparison into a false client data omission.

    Never invent a pass. This is a source-state inconsistency, kept unknown and
    distinguished from missing material; the unmodified model answer is saved.
    """
    import re
    for field in needed_fields([check['rule_id']]):
        info = fields.get(field, {})
        label = {'advance_dealer': '费用代垫经销商', 'activity_period': '活动起止日期',
            'application_quantity': '申请数量', 'application_unit_price': '申请单价',
            'application_products': '申请商品字段', 'application_total': '本次申请金额',
            'approved_total': '本单已有核销批复金额'}[field]
        if check['status'] != 'unknown':
            continue
        if field in {'application_total', 'approved_total'} and info.get('state') == 'missing':
            # Preserve the specific observed limit; do not overwrite actual
            # linked totals or workflow facts with a generic missing-header text.
            continue
        if info.get('state') not in {'available', 'multiple'}:
            continue
        if re.search(r'(?:未取得|未获取|未提供|没有取得|缺少).{0,16}(?:系统|勤策|申请|费用代垫)', check['reason']):
            return {**check, 'reason': f'已取得本单的{label}，但本项尚未完成与对应已交资料的核对，暂时不能确认核对结果。'}
    return check


def _project_system(document, group):
    if group not in {'documents', 'invoice', 'dates', 'budget', 'payment', 'settlement', 'pos_settlement', 'photos'}:
        return document
    keep = {'verify_code', 'applyCode', 'subId', 'id', 'activity_code', 'activity_cm_id'}
    if group in {'documents', 'invoice'}:
        keep |= {'dealerId', 'dealerCode', 'dealerName', 'advanceDealerName', 'updealer_code', 'updealer_name',
                 'actual_dealer_code', 'actual_dealer_name', 'extColumns'}
    if group in {'dates', 'payment'}:
        keep |= {'startDate', 'endDate', 'execute_date'}
    if group == 'budget':
        keep |= {'applyAmount', 'totalAmount', 'budgetAmount', 'approvedAmount', 'approvalAmount',
                 'approvedTotalAmount', '批复金额合计', 'extColumns',
                 'verify_status', 'reverse_status', 'verify_amount', 'audit_time'}
    if group == 'photos':
        keep |= {'cusId', 'cusCode', 'cusName', 'customer_id', 'customer_code', 'customer_name',
                 'startDate', 'endDate', 'pdIds', 'pdCodes', 'pdNames', 'barcode', 'extColumns', 'itemName', 'exeRequire'}
    if group in {'settlement', 'pos_settlement'}:
        keep |= {'cusId', 'cusCode', 'cusName', 'quantity', 'unitPrice', 'barcode',
                 'pdIds', 'pdCodes', 'pdNames', 'extColumns'}
    if group == 'settlement':
        # Submitted and latest-node line amounts keep their original semantics;
        # neither is an actual headcount or an independent final approval total.
        keep |= {'customer_id', 'customer_code', 'customer_name', 'init_amount', 'audit_amount'}
    facts = [fact for fact in document['facts'] if fact.partition('=')[0] in keep]
    if group in {'documents', 'invoice', 'budget'}:
        allowed = {'费用代垫经销商'} if group in {'documents', 'invoice'} else {'申请总金额', '活动申请总金额', '批复金额合计'}
        facts = [fact for fact in facts if not fact.startswith('extColumns=') or
                 any(c.get('ext_key') in allowed for c in json.loads(fact.partition('=')[2]))]
        facts = [fact if not fact.startswith('extColumns=') else 'extColumns=' + dumps([
            c for c in json.loads(fact.partition('=')[2]) if c.get('ext_key') in allowed]) for fact in facts]
    if group in {'settlement', 'pos_settlement', 'photos'}:
        filtered = []
        for fact in facts:
            if fact.startswith('extColumns='):
                columns = json.loads(fact.partition('=')[2])
                allowed = ({'活动SKU', '条形码', '陈列、堆头类型'} if group == 'photos' else
                           {'活动SKU', '条形码', '数量', '单价', '奖励规则', '赠品方案', '搭赠规则'})
                columns = [c for c in columns if c.get('ext_key') in allowed or
                           (group == 'photos' and c.get('ext_col') in {'ext_2412110257', 'ext_2412110258'})]
                # Each canonical object is a continuous substring of the native
                # extColumns fact. Never invent a shorter extColumns=[...] quote.
                filtered.extend(dumps(column) for column in columns)
            else:
                filtered.append(fact)
        facts = filtered
    # Preserve non-flat/grouped reference records in full rather than dropping
    # business data whose role the index does not understand.
    if not _fields(document) or 'linked_customer' in _fields(document) or any(
            f.startswith('fields=') for f in document['facts']):
        return document
    return {**document, 'facts': facts}


def retrieve(documents, materials, units, group, roles):
    ids = {uid for material in materials if material['id'] in roles for uid in material['source_ids']}
    by_id = {u['unit_id']: u for u in units or []}
    result = []
    for document in documents:
        unit = by_id.get(document['unit_id'], {})
        if unit.get('source_kind') == 'qince':
            # All these sources were already filtered by exact verify/app row.
            if group in {'pos', 'pos_quantity'}:
                continue
            label = unit.get('source_file')
            if group == 'budget' and label not in {'勤策申请主档', '勤策申请明细', '勤策核销批复', '勤策核销审批状态'}:
                continue
            if group in {'documents', 'invoice', 'dates', 'payment'} and label not in APPLICATION_LABELS:
                continue
            if group == 'settlement' and label not in APPLICATION_LABELS | {'勤策核销明细'}:
                continue
            if group == 'pos_settlement' and label not in APPLICATION_LABELS:
                continue
            if group == 'photos' and label not in APPLICATION_LABELS | {
                    '勤策核销明细', '勤策终端主档', '勤策商品包装参照', '勤策商品识别码与尺寸', '勤策本图识别与校对参照'}:
                continue
            result.append(_project_system(document, group))
        elif group == 'all' or document['unit_id'] in ids:
            result.append(document)
    if group in {'documents', 'invoice', 'dates', 'payment', 'photos'}:
        # Names/periods repeated across linked SKU/store rows are compared once
        # per exact value, not read 140 times. Every distinct value survives;
        # raw rows and all provenance stay in reference_fields/the full case.
        seen, compact = set(), []
        for document in result:
            unit = by_id.get(document['unit_id'], {})
            if unit.get('source_kind') == 'qince':
                signature = (unit.get('source_file'), tuple(f for f in document['facts']
                    if f.partition('=')[0] not in {'subId', 'id'}))
                if signature in seen:continue
                seen.add(signature)
            compact.append(document)
        return compact
    return result


def batches(case, *, force_split=False, provider_limits_only=False):
    """Do not add calls for a small case. Large cases split by audit purpose."""
    from .pdf_policy import rules, applicability
    ids = [rule.id for rule in rules(case['scenario'])]
    size = len(dumps(case['documents']).encode('utf-8'))
    if not force_split and (provider_limits_only or size <= CONTEXT_BYTES):
        return [{'name': 'all', 'rule_ids': ids, 'documents': case['documents']}]
    groups = OrderedDict()
    for rule_id in ids:
        name = next((name for name, (_, members) in ROLE_GROUPS.items() if rule_id in members), 'all')
        groups.setdefault(name, []).append(rule_id)
    by_id = {rule.id: rule for rule in rules(case['scenario'])}
    result = []
    for name, members in groups.items():
        inactive = [key for key in members if applicability(by_id[key].when, case['scenario'], case.get('flags', {})) is False
                    and not any(key in owners for extra, owners in EXTRA_OWNER.items()
                                if extra not in {'payment_dates', 'target_tiers'})]
        if inactive:
            result.append({'name': name, 'rule_ids': inactive, 'program_not_applicable': True, 'documents': []})
            members = [key for key in members if key not in inactive]
        if not members:continue
        not_applicable = all(applicability(by_id[key].when, case['scenario'], case.get('flags', {})) is False
                             for key in members)
        if any(set(members) & owners for key, owners in EXTRA_OWNER.items() if key not in {'payment_dates', 'target_tiers'}):
            not_applicable = False
        documents = [] if not_applicable else retrieve(case['documents'], case['materials'], case.get('units'), name,
                ROLE_GROUPS[name][0] if name in ROLE_GROUPS else set())
        if name == 'pos' and not not_applicable:
            chunks = pos_chunks(documents, case.get('units', []))
            for chunk in chunks:
                result.append({'name': name, 'rule_ids': members, **chunk})
        else:
            # Numeric settlement rules can require many independent joins and
            # comparisons even when their input fits the text budget. Keep one
            # rule per request to avoid mixing unrelated numeric derivations.
            width = 1 if name == 'settlement' else 4
            for start in range(0, len(members), width):
                selected = members[start:start+width]
                packet = documents
                if set(selected) <= {'settlement_calculation'}:
                    # Multiplication of submitted settlement items does not
                    # require repeating the entire linked application table.
                    qince = {u['unit_id'] for u in case.get('units', []) if u.get('source_kind') == 'qince'}
                    packet = [d for d in documents if d['unit_id'] not in qince]
                elif set(selected) <= {'reward_unit_price', 'application_quantity_price'}:
                    # Verification-node amounts do not establish the requested
                    # quantity or reward unit price. Keep every application
                    # row and its product/store identity, not these extra rows.
                    verification = {u['unit_id'] for u in case.get('units', [])
                                    if u.get('source_kind') == 'qince' and u.get('source_file') == '勤策核销明细'}
                    packet = [d for d in documents if d['unit_id'] not in verification]
                result.append({'name': name, 'rule_ids': selected,
                               'program_not_applicable': not_applicable, 'documents': packet})
    return result


def program_pos_checks(plan, identities):
    """Project this run's validated DB results; do not ask AI to repeat them."""
    result = {}
    if identities.get('applicable'):
        from .pos_product_identity import project_pos_identity_reporting, validate_pos_identity_reporting
        rows = identities['rows']
        status = ('fail' if any(r['status'] == 'fail' for r in rows) else
                  'unknown' if not rows or identities.get('limitations') or any(r['status'] == 'unknown' for r in rows)
                  else 'pass')
        value = {'checks': [{'rule_id': 'pos_product_identity', 'status': status,
            'reason': '商品条码已逐项查库匹配。' if status == 'pass' else '部分商品未能完成匹配。',
            'source_ids': list(dict.fromkeys(r['unit_id'] for r in rows)), 'comparisons': []}]}
        value = project_pos_identity_reporting(value, identities)
        validate_pos_identity_reporting(value, identities)
        result['pos_product_identity'] = value['checks'][0]
    if plan and plan['complete']:
        # This is an intermediate placeholder; the existing final native POS
        # validator calculates every original row and determines real status.
        result['pos_arithmetic'] = {'rule_id': 'pos_arithmetic', 'status': 'unknown',
            'reason': '按全部原始销售行由程序复算，具体结果随完整核对记录生成。',
            'source_ids': list(plan['source_ids']), 'comparisons': []}
    return result


def pos_chunks(documents, units):
    """Cover every native fact once; repeat genuine same-sheet headers as context.

    Only local POS field checks are reduced across chunks. Global arithmetic is
    still recomputed from the untouched full case by prepare_pos_arithmetic.
    """
    from .pos_arithmetic import ROW, _columns, _formula_column_candidate
    from .spreadsheet_rows import parse_cells
    by_id = {u['unit_id']: u for u in units}
    headers = {}
    for document in documents:
        source = by_id.get(document['unit_id'], {}).get('source_file', document['unit_id'])
        for fact in document['facts']:
            match = ROW.fullmatch(fact)
            if match:
                cells = parse_cells(match[3])
                if cells is None:
                    continue
                if _columns(cells) or _formula_column_candidate(cells):
                    headers.setdefault((source, match[1]), []).append((document, fact))
    chunks, current, ranges, size = [], [], [], 0
    def emit():
        if not current:
            return
        packed = {d['unit_id']: {**d, 'facts': list(d['facts'])} for d in current}
        for document in current:
            source = by_id.get(document['unit_id'], {}).get('source_file', document['unit_id'])
            sheets = {match[1] for f in document['facts'] if (match := ROW.fullmatch(f))}
            for sheet in sheets:
                for header_doc, fact in headers.get((source, sheet), []):
                    target = packed.setdefault(header_doc['unit_id'], {**header_doc, 'facts': []})
                    if fact not in target['facts']:
                        target['facts'].insert(0, fact)
        chunks.append({'documents': list(packed.values()), 'pos_ranges': list(ranges)})
    for document in documents:
        for index, fact in enumerate(document['facts']):
            cost = len(fact.encode('utf-8')) + 32
            if current and size + cost > CONTEXT_BYTES // 2:
                emit(); current, ranges, size = [], [], 0
            if not current or current[-1]['unit_id'] != document['unit_id']:
                current.append({**document, 'facts': []})
                ranges.append([document['unit_id'], index, index])
            current[-1]['facts'].append(fact)
            ranges[-1][2] = index + 1
            size += cost
    emit()
    # Empty documents still carry explicit limitations and cannot disappear.
    empty = [d for d in documents if not d['facts']]
    if empty:
        chunks.append({'documents': empty, 'pos_ranges': [[d['unit_id'], 0, 0] for d in empty]})
    return chunks or [{'documents': documents, 'pos_ranges': []}]


def pos_quantity_index(documents, plan, identities):
    """Exact per-69 totals with complete original operands, never model sums.

    The compact RAG view is a program derivation, explicitly not an original
    voucher value. Full-row operands stay here and are restored before the
    ordinary quote/numeric/direction validators. Ambiguity uses the full path.
    """
    from .pos_arithmetic import ROW
    from decimal import Decimal, localcontext
    import re
    if not plan or not plan['complete'] or identities.get('limitations'):
        return None
    products = {}
    for comparison in plan['comparisons']:
        if comparison['operation'] != 'product':
            continue
        match = re.fullmatch(r'(.*)第(\d+)行销售金额', comparison['label'])
        if not match:
            return None
        products[(comparison['left']['unit_id'], match[1], int(match[2]))] = comparison['right'][0]
    facts = {d['unit_id']: d['facts'] for d in documents}
    groups, consumed = OrderedDict(), set()
    for row in identities.get('rows', []):
        source = row['source']
        barcode = source.get('barcode_69', {}).get('value') if source.get('barcode_69') else None
        name = source.get('product_name', {}).get('value') if source.get('product_name') else None
        if not isinstance(barcode, str) or not re.fullmatch(r'69\d{11}', barcode) or not name:
            return None
        for index in source['fact_indexes']:
            fact = facts[row['unit_id']][index]
            match = ROW.fullmatch(fact)
            if not match or barcode not in fact:
                return None
            key = (row['unit_id'], match[1], int(match[2]))
            if key not in products or key in consumed:
                return None
            consumed.add(key)
            group = groups.setdefault(barcode, {'id': f'pos-q-{len(groups)+1:04d}',
                'barcode_69': barcode, 'names': [], 'terms': []})
            if name not in group['names']:
                group['names'].append(name)
            group['terms'].append({'unit_id': row['unit_id'], 'quote': fact, 'number': products[key]['number']})
    if consumed != set(products) or not consumed:
        return None  # No partial grouping may claim full POS coverage.
    for group in groups.values():
        values = [Decimal(t['number'].replace(',', '').replace('，', '')) for t in group['terms']]
        with localcontext() as context:
            context.prec = max(28, sum(len(v.as_tuple().digits) for v in values))
            group['quantity'] = format(sum(values), 'f')
        group['row_count'] = len(values)
        group['source_ids'] = list(dict.fromkeys(t['unit_id'] for t in group['terms']))
    return list(groups.values())


def quantity_wire_schema(schema, groups):
    """A distinct derivation reference; never fake a subtotal as an operand."""
    original = schema['properties']['checks']['items']['properties']['comparisons']['items']
    reference = deepcopy(original)
    reference['required'] = list(reference['required']) + ['right_group_ref']
    reference['properties'].update(right_group_ref={'type': 'string', 'enum': [g['id'] for g in groups]},
        right={'type': 'array', 'maxItems': 0}, operation={'const': 'sum'}, value_kind={'const': 'quantity'})
    schema['properties']['checks']['items']['properties']['comparisons']['items'] = reference
    schema['properties']['quantity_group_reads'] = {'type': 'array', 'uniqueItems': True,
        'minItems': len(groups), 'maxItems': len(groups), 'items': {'enum': [g['id'] for g in groups]}}
    schema['required'].append('quantity_group_reads')


def expand_quantity_refs(value, groups):
    result = deepcopy(value)
    by_id = {g['id']: g for g in groups or []}
    read = result.pop('quantity_group_reads', [])
    if len(read) != len(by_id) or set(read) != set(by_id):
        raise AuditError('完整POS数量索引的商品组不得遗漏或重复。')
    for check in result['checks']:
        for comparison in check['comparisons']:
            if 'right_group_ref' not in comparison:
                continue
            if check['rule_id'] != 'settlement_pos_quantity' or comparison['right_group_ref'] not in by_id:
                raise AuditError('数量汇总引用不属于本单完整POS索引。')
            group = by_id[comparison.pop('right_group_ref')]
            comparison['right'] = deepcopy(group['terms'])
            check['source_ids'] = list(dict.fromkeys(check['source_ids'] + group['source_ids']))
    return result


def part_schema(full, rule_ids, fields):
    schema = deepcopy(full)
    check = schema['properties']['checks']
    check.update(minItems=len(rule_ids), maxItems=len(rule_ids))
    check['items']['properties']['rule_id']['enum'] = list(rule_ids)
    # The typed companion is requested in the prompt and validated when
    # present. Older valid responses must not fail solely for omitting new
    # auxiliary metadata; existing source/role/numeric checks still execute.
    for key, owners in EXTRA_OWNER.items():
        if not set(rule_ids) & owners:
            schema['properties'].pop(key, None)
            if key in schema['required']:
                schema['required'].remove(key)
    schema['properties']['reference_reads'] = reference_schema(fields)
    if 'tool_requests' in schema['properties']:
        request = schema['properties']['tool_requests']['items']['properties']
        request['rule_ids']['items'] = {'enum': list(rule_ids)}
        request['fields']['items'] = {'enum': list(fields)} if fields else {'type': 'string'}
    return schema


def compact_explanations(schema):
    """Guide concise prose without adding a new audit-failure condition.

    Length advice is deliberately not maxLength: a verbose but otherwise valid
    response must still finish. Original facts and merged problems stay whole.
    """
    def visit(node):
        if isinstance(node, dict):
            properties = node.get('properties', {})
            if 'reason' in properties and 'status' in properties:
                properties['reason'] = {**properties['reason'], 'description':
                    '给客户看的简短结论：直接说哪个对象有什么问题。通过项建议120字以内，问题或无法确认项建议480字以内；'
                    '不复述核对过程、候选名单和已通过内容。必要原值与差异保留，完整引用和算式写在对应证据字段。'
                    '字数只作写法引导，不是审核失败或资料缺失的依据。'}
            if {'statement', 'references', 'kind'}.issubset(properties):
                properties['statement'] = {**properties['statement'], 'description':
                    '保留本项实际证据、关联和判断依据，避免重复长篇原文；必要细节不能因长度省略。'}
            for value in list(node.values()):
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)
    visit(schema)
    return schema


def _seal_fact_contains_name(fact, item):
    """Resolve the name within its own source clause, not unrelated page text."""
    import re
    seal = (r'印章|印文|盖章|公章|财务(?:专用)?章|合同(?:专用)?章|结算单(?:专用)?章|'
            r'业务(?:专用)?章|部门(?:专用)?章|专用章|落章|章内|章面|章样|圆章|方章')
    written = r'填写|打印|抬头|客户名称|经销商名称|付款方'
    outcomes = []
    for quoted in re.finditer(re.escape(item['quote']), fact):
        for occurrence in re.finditer(re.escape(item['value']), quoted.group()):
            position = quoted.start() + occurrence.start()
            prefix = re.split(r'[。；;\n]', fact[:position])[-1]
            seal_marks = list(re.finditer(seal, prefix))
            written_marks = list(re.finditer(written, prefix))
            if not seal_marks:
                outcomes.append(False)
                continue
            mark = seal_marks[-1]
            if written_marks and written_marks[-1].start() > mark.start():
                outcomes.append(False)
                continue
            if re.search(r'未见|没有|无|非|不是', prefix[max(0, mark.start()-6):mark.start()]):
                outcomes.append(False)
                continue
            if re.search(r'无法(?:辨认|识别|确认)|看不清|不可辨', prefix[mark.end():]):
                outcomes.append(False)
                continue
            outcomes.append(True)
    return bool(outcomes) and all(outcomes)


def reference_comparison(check, documents, materials, units):
    """Compute the two known high-risk comparisons from typed, quoted evidence.

    Missing document/seal text is distinct from a missing API field. Does not
    reuse company names for stores or row amounts for header approval totals.
    """
    import re
    evidence = check.get('reference_evidence')
    rule = check['rule_id']
    if evidence is None or rule not in DEALER_RULES | {'contract_activity_dates', 'settlement_application_dates'}:
        return check
    role = ('promotion_contract' if rule.startswith('contract_') else 'pos' if rule == 'pos_dealer'
            else 'payment' if rule == 'payment_statement_dealer' else 'settlement')
    source_ids = {uid for m in materials or [] if m['id'] == role and m['state'] in {'present', 'unclear'}
                  for uid in m['source_ids']}
    facts = {d['unit_id']: d['facts'] for d in documents}
    from .common import CheckEvidenceError
    def fail(message, code, field):
        raise CheckEvidenceError(message, rule, code, 'reference_evidence/'+field)
    def quoted(item, field):
        if item['unit_id'] not in source_ids:
            fail('reference_evidence必须引用当前被检查文书，不能混入其他材料角色。',
                 'reference_source_role', field+'/unit_id')
        matched = [f for f in facts.get(item['unit_id'], []) if item['quote'] in f]
        if not matched:
            fail('reference_evidence必须逐字引用当前被检查文书；合同项只引合同、结算项只引结算。'
                 'periods不填写勤策startDate/endDate，勤策参照由程序读取字段索引，不与文书原文混填。',
                 'reference_quote', field+'/quote')
        if 'value' in item and item['value'] not in item['quote']:
            fail('经销商名称必须实际出现在所引用原文中，不能改写错字或漏字。',
                 'reference_name_value', field+'/value')
        return matched
    for key in ('written_name', 'seal_name'):
        if evidence[key] is not None:
            matched = quoted(evidence[key], key)
            if key == 'seal_name' and not all(_seal_fact_contains_name(fact, evidence[key]) for fact in matched):
                fail('落章名称须来自同一条可辨认的实际印章事实；填写名称、无关印章或多种来源歧义不能代替。',
                     'seal_source', key+'/quote')
    for index, item in enumerate(evidence['periods']):
        quoted(item, f'periods/{index}')
    refs = reference_fields(documents, units)
    field = 'advance_dealer' if rule in DEALER_RULES else 'activity_period'
    info = refs[field]
    reference_ids = {uid for e in info['values'] for uid in e['source_ids']}
    used = set(check['source_ids'])
    # All distinct candidate values must remain visible, not choose one to pass.
    values = {dumps(e['value']): e['value'] for e in info['values']}
    if info['state'] != 'available' or len(values) != 1:
        if info['state'] in {'missing','empty','incomplete'}:
            return check  # Available uploaded materials retain their own checks.
        return {**check, 'status': 'unknown', 'comparisons': [],
            'reason': '本单申请的对应参照字段' + ('未返回。' if info['state'] == 'missing' else
                '已返回但为空、不完整、格式有误或有多个不同值，暂时不能确认对应关系。')}
    used.update(next(iter(e['source_ids'])) for e in info['values'])
    expected = next(iter(values.values()))
    if rule in DEALER_RULES:
        observed = []
        for key in ('written_name', 'seal_name') if rule != 'pos_dealer' else ('seal_name',):
            item = evidence[key]
            if item is None:
                return {**check, 'status': 'unknown', 'comparisons': [], 'source_ids': sorted(used),
                    'reason': '已取得费用代垫经销商，但对应资料的' + ('填写名称' if key == 'written_name' else '落章名称') + '未能辨认，暂时不能核对。'}
            observed.append(item['value'])
            used.add(item['unit_id'])
        if not isinstance(expected, str):
            return check  # Custom dictionaries still require their own exact interpretation.
        from .reference_matching import name_matches
        matches = all(name_matches(value, expected) for value in observed)
        reason = (f'资料中的对应经销商名称与本单申请费用代垫经销商“{expected}”一致。' if matches else
                  f'资料中的经销商名称“{"、".join(observed)}”与本单申请费用代垫经销商“{expected}”不一致。')
    else:
        from datetime import date
        from .activity_periods import case_activity_periods, period_problem
        expected_period = tuple(date.fromisoformat(expected[key][:10]) for key in ('start', 'end'))
        for index, item in enumerate(evidence['periods']):
            quoted(item, f'periods/{index}')
            used.add(item['unit_id'])
        label = '促销合同' if role == 'promotion_contract' else '结算单'
        # The program reads every relevant source fact, including activity
        # narrative and explicit phases. A model's short quote cannot hide a
        # conflicting page or make an already present period disappear.
        reading = case_activity_periods(facts, materials)[role]
        used.update(s['unit_id'] for p in reading['periods'] for s in p['sources'])
        conflicting = reading['state'] == 'unknown' and not reading['limitations'] and reading['periods']
        if reading['state'] != 'available' and not conflicting:
            return {**check, 'status': 'unknown', 'comparisons': [], 'source_ids': sorted(used),
                    'reason': period_problem(label, reading)}
        periods = ([tuple(date.fromisoformat(v) for v in p) for p in reading['coverage']] if not conflicting else
                   sorted({(date.fromisoformat(p['start']), date.fromisoformat(p['end'])) for p in reading['periods']}))
        matches = periods == [expected_period]
        reason = (f'{label}执行周期与本单申请的{expected_period[0]}至{expected_period[1]}一致。' if matches else
                  f'{label}执行周期' + '、'.join(f'{a}至{b}' for a, b in periods) +
                  f'与本单申请的{expected_period[0]}至{expected_period[1]}不一致。')
    return {**check, 'status': 'pass' if matches else 'fail', 'reason': reason,
            'source_ids': sorted(used), 'comparisons': []}


def merge(parts, expected, *, pos_fact_counts=None):
    """No final model call: all original validated rule results exactly once."""
    result, checks, reads = {}, {}, {}
    seen_ranges = {}
    for part in parts:
        for uid, start, end in part.get('_pos_ranges', []):
            previous = seen_ranges.setdefault(uid, [])
            if any(max(start, a) < min(end, b) for a, b in previous):
                raise AuditError('POS分块重复审核相同原始事实范围。')
            previous.append((start, end))
        for item in part.get('reference_reads', []):
            previous = reads.setdefault(item['field'], {**item, 'source_ids': []})
            if previous['state'] != item['state']:
                raise AuditError('分块核销对同一系统字段的取得状态不一致。')
            previous['source_ids'] = list(dict.fromkeys(previous['source_ids'] + item['source_ids']))
        for check in part['checks']:
            if check['rule_id'] in checks:
                if not part.get('_pos_ranges') or check['rule_id'] not in {'pos_fields', 'pos_arithmetic', 'pos_product_identity'}:
                    raise AuditError('分块核销重复返回同一审核项，不能覆盖前一块。')
                old = checks[check['rule_id']]
                statuses = {old['status'], check['status']}
                old.update(status=('fail' if 'fail' in statuses else 'unknown' if 'unknown' in statuses else
                    'pass' if 'pass' in statuses else 'not_applicable'),
                    reason='；'.join(dict.fromkeys([old['reason'], check['reason']])),
                    source_ids=list(dict.fromkeys(old['source_ids'] + check['source_ids'])),
                    comparisons=old['comparisons'] + check['comparisons'])
                continue
            checks[check['rule_id']] = check
        for key, value in part.items():
            if key in {'checks', 'reference_reads', '_pos_ranges'}:
                continue
            if key in result and result[key] != value:
                raise AuditError('分块核销返回冲突的跨项证据，不能任意选择。')
            result[key] = value
    if set(checks) != set(expected):
        raise AuditError('分块核销漏项或混入其他审核项。')
    positions = {}
    for uid, ranges in seen_ranges.items():
        position = 0
        for start, end in sorted(ranges):
            if start != position:
                raise AuditError('POS分块事实覆盖中间存在缺口。')
            position = end
        positions[uid] = position
    if pos_fact_counts is not None and positions != pos_fact_counts:
        raise AuditError('POS分块未完整覆盖全部来源及末尾事实，不能按已收到部分判整表完成。')
    result['checks'] = [checks[rule_id] for rule_id in expected]
    result['reference_reads'] = list(reads.values())
    return result
