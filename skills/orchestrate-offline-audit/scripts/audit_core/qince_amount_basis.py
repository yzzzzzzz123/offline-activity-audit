"""Case-linked amount evidence, derived with code and without extra API calls."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, localcontext
import json
import re

OPTIONAL_REFERENCE_RULES = frozenset({
    'contract_activity_dates', 'settlement_application_dates', 'contract_budget_amount',
    'application_claim_ceiling', 'claim_ceiling',
    'application_products', 'entry_application_products', 'contract_reward_unit_price',
})
CONTRACT_STANDARD_RULES = frozenset({'application_quantity_price', 'reward_unit_price',
    'staff_headcount', 'staff_total_unit_price', 'display_quantity_price'})
APPLICATION_TOTAL_RULES = frozenset({'contract_budget_amount', 'application_claim_ceiling'})
APPLICATION_TOTAL_CLARIFICATION = (
    '独立申请主档总额与本单完整关联申请行合计是不同来源；根据实际取得的来源及其完整范围核对，不另造单头字段。'
    '完整范围只有1行时仍保留该行来源及原值，不能为了工具格式补造多行。'
    '工具比较可使用value表示原始总额、sum表示原行合计；这只是辅助表达，不是要求模型改写证据直至程序通过的业务门禁。'
    '任意单行、部分行、重复冲突行、其他申请或核销金额不能代替完整合计；0是有效金额。'
)


def constrain_commission_plan_quantities(scenario, flags, check, sources, units=None):
    """Keep commission prices strict; application plans are not POS actuals.

    Validate and retain every excluded operand/calculation for technical review.
    This does not exempt settlement/POS quantities or staff headcount checks.
    A usable price comparison is still mandatory; incomplete evidence goes
    through the existing finite stage retry rather than becoming a client fault.
    """
    if (scenario != 'personnel_incentive' or flags.get('sales_commission') is not True
            or check['rule_id'] != 'application_quantity_price'):
        return check
    plan_ids = None if units is None else {u['unit_id'] for u in units
        if u.get('source_kind') == 'qince' and u.get('source_file') == '勤策申请明细'}
    excluded = [c for c in check['comparisons'] if c['value_kind'] == 'quantity'
                and (plan_ids is None or all(v['unit_id'] in plan_ids for v in c['right']))]
    if not excluded:
        return check
    from .pdf_evidence import calculate
    from .common import AuditError
    archive = [{'reason': '申请计划数量不作为销售提成实际销量的一致性参照；实销量由结算与POS原值核对。',
                'comparison': deepcopy(c), 'calculation': calculate(c, sources)} for c in excluded]
    retained = [c for c in check['comparisons'] if c not in excluded]
    if not any(c['value_kind'] == 'unit_price' for c in retained) or check['status'] not in {'pass', 'fail'}:
        raise AuditError('销售提成不能用申请计划数量与实际销量比较代替本项核对；'
                         '请保留对应奖励单价的原始比较，实际销量按结算与POS核对。')
    return {**check, 'comparisons': retained, 'status': 'pass',
            'reason': '本次申报奖励单价已按对应申请原值逐项核对。',
            'excluded_plan_quantity_comparisons': archive}


def fields(document):
    result = {}
    for fact in document.get('facts', []):
        key, sep, raw = fact.partition('=')
        if not sep:
            continue
        try:
            value = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if key in result and result[key] != value:
            return {}
        result[key] = value
    return result


def linked_application_basis(documents, units):
    """Sum only a complete bijection of this verification's linked rows.

    A derived scope is labelled as such, never as a made-up header field.
    Missing rows/amounts, inconsistent identities and conflicting duplicates
    disable this derivation; partial totals cannot certify a whole scope.
    """
    kinds = {u['unit_id']: u for u in units or []}
    applications, links, codes = {}, {}, set()
    for document in documents:
        unit = kinds.get(document['unit_id'], {})
        if unit.get('source_kind') != 'qince':
            continue
        label = unit.get('source_file')
        if label not in {'勤策申请明细', '勤策核销明细'}:
            continue
        data = fields(document)
        code = str(data.get('verify_code') or '')
        if not code:
            return None
        codes.add(code)
        keys = ('applyCode', 'subId') if label == '勤策申请明细' else ('activity_code', 'activity_cm_id')
        key = tuple(str(data.get(k) or '') for k in keys)
        if not all(key):
            return None
        if label == '勤策核销明细':
            customer = tuple(str(data.get(k) or '') for k in ('customer_id', 'customer_code'))
            if key in links and links[key] != customer:
                return None
            links[key] = customer
            continue
        raw = data.get('applyAmount')
        try:
            amount = Decimal(str(raw).replace(',', ''))
            if isinstance(raw, bool) or not amount.is_finite():
                return None
        except InvalidOperation:
            return None
        customer = tuple(str(data.get(k) or '') for k in ('cusId', 'cusCode'))
        entry = {'amount': amount, 'customer': customer, 'unit_id': document['unit_id'],
                 'quote': next(f for f in document['facts'] if f.startswith('applyAmount=')), 'number': str(raw)}
        if key in applications and any(applications[key][k] != entry[k] for k in ('amount', 'customer')):
            return None
        applications.setdefault(key, entry)
    if len(codes) != 1 or not links or set(links) != set(applications):
        return None
    for key, customer in links.items():
        if any(a and b and a != b for a, b in zip(customer, applications[key]['customer'])):
            return None
    values = list(applications.values())
    with localcontext() as context:
        context.prec = max(28, sum(len(v['amount'].as_tuple().digits) + abs(v['amount'].as_tuple().exponent) for v in values))
        total = format(sum(v['amount'] for v in values), 'f')
    return {'kind': 'program_derived_complete_linked_application_sum',
            'scope': '本次核销精确关联且完整覆盖的申请明细', 'total': total,
            'row_count': len(values), 'application_codes': sorted({k[0] for k in applications}),
            'terms': [{k: v[k] for k in ('unit_id', 'quote', 'number')} for v in values]}


def complete_application_sum(comparison, documents, units):
    """Prove exact complete coverage for sum, or its one-row value equivalent."""
    if comparison['operation'] not in {'sum', 'value'} or comparison['value_kind'] != 'amount':
        return None
    basis = linked_application_basis(documents, units)
    if basis is None or (comparison['operation'] == 'value' and basis['row_count'] != 1):
        return None
    supplied = [(v['unit_id'], v['quote'], v['number']) for v in comparison['right']]
    expected = [(v['unit_id'], v['quote'], v['number']) for v in basis['terms']]
    if len(supplied) != len(expected) or len(set(supplied)) != len(supplied) or set(supplied) != set(expected):
        return None
    return basis


def application_total_comparison_schema(documents, units, application_ids):
    """Use the same complete-row proof for the concrete generation contract."""
    row_ids = {u['unit_id'] for u in units or []
               if u.get('source_kind') == 'qince' and u.get('source_file') == '勤策申请明细'}
    originals = sorted(set(application_ids) - row_ids)
    choices = []
    if originals:
        choices.append({'properties': {'operation': {'const': 'value'}, 'right': {
            'minItems': 1, 'maxItems': 1, 'items': {'properties': {'unit_id': {'enum': originals}}}}}})
    basis = linked_application_basis(documents, units)
    if basis is not None:
        choices.append({'properties': {
            'operation': {'enum': ['sum', 'value'] if basis['row_count'] == 1 else ['sum']},
            'right': {'minItems': basis['row_count'], 'maxItems': basis['row_count'],
                      'uniqueItems': True, 'items': {'enum': deepcopy(basis['terms'])}},
        }})
    return {'description': APPLICATION_TOTAL_CLARIFICATION, 'anyOf': choices} if choices else False


def normalize_application_total_check(scenario, check, documents, materials, units, *, path=()):
    """Validate original evidence before deterministic work, archiving one-row normalization.

    Never overwrite a bad original quote with a newly computed comparison.
    The raw model check remains untouched; the decision carries its conversion.
    """
    from .pdf_policy import APPLICATION_COMPARISON_RULES
    if check['rule_id'] not in APPLICATION_TOTAL_RULES or (scenario, check['rule_id']) not in APPLICATION_COMPARISON_RULES:
        return check
    from .fee_policy_checks import validate_workbook_comparison
    from .comparison_policy import expected_operator
    from .pdf_evidence import calculate, document_map
    sources = document_map(documents)
    comparisons, normalized = [], []
    for index, comparison in enumerate(check['comparisons']):
        expected_operator(scenario, check['rule_id'], comparison, [], comparison_index=index)
        validate_workbook_comparison(scenario, check['rule_id'], comparison, documents, materials, units)
        calculation = calculate(comparison, sources, path=(*path, 'comparisons', index))
        basis = complete_application_sum(comparison, documents, units)
        if comparison['operation'] == 'value' and basis is not None:
            converted = {**comparison, 'operation': 'sum'}
            normalized.append({'reason': '完整关联申请恰好一行；已验证原来源、逐字引用及原值，按完整合计规范化。',
                'original_comparison': deepcopy(comparison), 'normalized_comparison': deepcopy(converted),
                'complete_application_basis': deepcopy(basis), 'original_calculation': calculation})
            comparisons.append(converted)
        else:
            comparisons.append(comparison)
    if not normalized:
        return check
    return {**check, 'comparisons': comparisons, 'normalized_application_totals': normalized}


def approval_scope_flags(flags, documents, materials, units):
    """Recorded approval is optional in a pre-approval AI audit, never a gate."""
    from .fee_policy_checks import approval_sources, application_sources, material_sources
    from .qince_approval_state import constrain_approval_state
    candidates = approval_sources(documents, materials, units)
    state = constrain_approval_state({'rule_id': 'claim_ceiling', 'status': 'unknown',
        'reason': '', 'source_ids': [], 'comparisons': []}, documents, units)
    from .audit_rag import reference_fields, needed_fields
    indexed = reference_fields(documents, units)
    available = {key: all(indexed[field]['state'] in {'available', 'multiple'} for field in needed_fields([key]))
                 for key in OPTIONAL_REFERENCE_RULES}
    # A separately submitted original application/approval remains valid
    # evidence even when it has no API-style JSON fields. Never give this role
    # to a contract or settlement merely because a classifier listed it twice.
    native_app = material_sources(materials, 'activity_application') & application_sources(documents, materials, units)
    qince_sources = {u['unit_id'] for u in units or [] if u.get('source_kind') == 'qince'}
    qince_sources.update(d['unit_id'] for d in documents if d.get('source_kind') == 'qince')
    native_app -= qince_sources  # Linked API rows are not separately submitted originals.
    if native_app:
        for key in OPTIONAL_REFERENCE_RULES - {'claim_ceiling'}:
            available[key] = True
    if candidates and state is None:
        available['claim_ceiling'] = True
    # Product identity is already checked against the project's product DB.
    # A Qince SKU/name is not an independently supplied retail barcode.
    barcodes = []
    for entry in indexed['application_products']['values']:
        original = entry['source_fields']
        if original.get('barcode'):
            barcodes.append(original['barcode'])
        barcodes.extend(c.get('ext_value') for c in original.get('extColumns', [])
                        if c.get('ext_key') == '条形码')
    has_barcodes = any(re.fullmatch(r'69\d{11}', str(value or '')) for value in barcodes)
    for key in ('application_products', 'entry_application_products'):
        available[key] = available[key] and (has_barcodes or bool(native_app))
    return {**flags, '_recorded_approval': bool(candidates) and state is None,
            '_reference_available': available}


def program_amount_check(check, documents, materials, units):
    """Bind an unambiguous original contract/claim amount to a complete sum.

    This supplies original operands for the existing Decimal validator; it
    neither rounds values nor invents an original quote for a computed total.
    Ambiguous headings/amounts retain the model path and original evidence.
    """
    key = check['rule_id']
    if key not in {'contract_budget_amount', 'application_claim_ceiling'}:
        return None
    basis = linked_application_basis(documents, units)
    if basis is None:
        return None
    # Explicit independently recorded totals take precedence, including
    # empty/conflicting originals; a derivation must not conceal them.
    kinds = {u['unit_id']: u for u in units or []}
    for document in documents:
        if kinds.get(document['unit_id'], {}).get('source_file') != '勤策申请主档':
            continue
        data = fields(document)
        if any(data.get(k) not in (None, '') for k in ('applyAmount', 'totalAmount', 'budgetAmount')) or any(
                c.get('ext_key') in {'申请总金额', '活动申请总金额'} and c.get('ext_value') not in (None, '')
                for c in data.get('extColumns', [])):
            return None
    role = 'promotion_contract' if key == 'contract_budget_amount' else 'settlement'
    allowed = {uid for m in materials or [] if m['id'] == role and m['state'] in {'present', 'unclear'}
               for uid in m['source_ids']}
    label = (r'(?:活动预算金额|活动预算|费用预算|预算金额)' if key == 'contract_budget_amount'
             else r'(?:实际核销金额|堆头核销金额|陈列核销金额|本次核销金额)')
    pattern = re.compile(label + r'\s*(?:[（(](?:元|人民币)[）)])?\s*(?:[:：=]|为)?\s*[￥¥]?\s*'
                         r'(?P<number>[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)(?![\d.,])')
    originals = []
    for document in documents:
        if document['unit_id'] not in allowed:
            continue
        for fact in document['facts']:
            for match in pattern.finditer(fact):
                # Do not reinterpret a formula, unit multiplier or written
                # "万元" value as a different original numeric operand.
                if re.match(r'\s*(?:万|千|亿|[×*+÷/=])', fact[match.end():]):
                    return None
                originals.append({'unit_id': document['unit_id'], 'quote': fact, 'number': match['number']})
    if len({Decimal(v['number'].replace(',', '')) for v in originals}) != 1:
        return None
    left = originals[0]
    operator = 'eq' if key == 'contract_budget_amount' else 'le'
    return {**check, 'status': 'pass',
            'reason': f'本次合同预算与完整关联申请明细合计{basis["total"]}元按原值核对。' if key == 'contract_budget_amount'
                      else f'本次实际核销金额与完整关联申请明细合计{basis["total"]}元按原值核对。',
            'source_ids': list(dict.fromkeys([v['unit_id'] for v in originals + basis['terms']])),
            'comparisons': [{'label': '合同预算与本次关联申请金额' if key == 'contract_budget_amount' else '实际核销金额与本次关联申请金额',
                'value_kind': 'amount', 'left': left, 'right': basis['terms'], 'operation': 'sum', 'operator': operator}]}
