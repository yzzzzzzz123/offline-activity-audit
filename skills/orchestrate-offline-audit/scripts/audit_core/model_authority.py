"""The upload runner delegates business decisions to its executing model.

Legacy policy functions remain available to explicit calculation/inspection
tools. They are not acceptance tests for model answers in a live workflow.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

from .common import AuditError

_EXECUTION = ContextVar('audit_model_business_authority', default=False)


def enabled():
    return _EXECUTION.get()


@contextmanager
def execution():
    token = _EXECUTION.set(True)
    try:
        yield
    finally:
        _EXECUTION.reset(token)


# Serialization spine that stays demanded for an object carrying an audit
# judgment. Everything else on such an object is advisory.
ADVISORY_REQUIRED = frozenset({
    'checks', 'ready', 'findings', 'notes', 'items', 'issues', 'documents',
    'unit_id', 'facts', 'roles', 'limitations', 'rule_id', 'rule_ids', 'status',
    'reason', 'source_ids', 'candidate_scenarios', 'flags', 'materials',
    'material_matches', 'source_problem_ids', 'target_ids', 'text'})

# A property set that marks an object as a judgment/report rather than a value
# contract. Value contracts (``{fact_index, quote, value}``) keep their exact
# ``required`` members so the model knows what to write; judgment objects keep
# only the spine above so an incomplete answer is still read, not gated.
BUSINESS_MARKERS = frozenset({
    'checks', 'comparisons', 'source_assertions', 'status', 'rule_id',
    'rule_ids', 'reason', 'reason_code', 'source_ids', 'findings', 'notes',
    'limitations', 'candidate_scenarios', 'material_matches', 'flags',
    'materials', 'documents', 'ready', 'issues', 'problems'})

# A value object may legitimately contain a ``reason`` field (for example POS
# ``other_facts`` also has ``fact_index`` and ``kind``).  These structural
# members make it provenance/data, not a business judgment whose constraints
# should be relaxed.
VALUE_OBJECT_MARKERS = frozenset({
    'fact_index', 'fact_indexes', 'quote', 'value', 'kind'})


def transport_schema(schema):
    """Keep JSON shapes, removing business predicates and evidence formulas.

    Shape means more than ``type/properties``: a nested ``anyOf`` (for example
    the POS product name and barcode objects ``{fact_index, quote, value}``)
    and nested ``required`` members are how the model learns what to write.
    Dropping them produced an empty ``{}`` contract, so the model answered with
    a plain string, the strict extraction validator rejected it, and the POS
    product-database lookup was abandoned before it queried anything.

    An object that carries an audit judgment keeps its nested ``required``
    advisory: only its serialization spine is demanded, so an incomplete
    judgment is still read by the execution model instead of being gated by
    transport validation. Pure value objects keep their full contract.

    Value constraints such as ``enum`` and ``const`` are also part of the
    serialization contract. Dropping them let a model return a human-readable
    label (for example POS ``other_facts.kind = "表头"``), pass transport
    validation, and then fail the strict extraction validator after the whole
    page had already been read. Keep value constraints for value objects, but
    continue to relax business-judgment objects and the business ``status``
    field so this projection never becomes a new business gate.
    """
    def shape(value, name='', root=False, *, business_context=False):
        if not isinstance(value, dict):
            return {}
        if name in {'comparisons', 'source_assertions', 'reference_evidence'}:
            return {'type': value.get('type', 'object')}
        result = {k: deepcopy(value[k]) for k in ('type', 'description', '$ref') if k in value}
        for keyword in ('allOf', 'anyOf', 'oneOf'):
            branches = value.get(keyword)
            if isinstance(branches, list):
                result[keyword] = [shape(branch, name, business_context=business_context)
                                   for branch in branches]
        properties = set(value.get('properties', {}))
        judgment = bool(BUSINESS_MARKERS & properties) and not bool(
            VALUE_OBJECT_MARKERS & properties)
        relaxed = business_context or judgment
        if not relaxed and name != 'status':
            # These are value contracts, not business predicates. In particular
            # POS ``other_facts.kind`` must remain visible to the model and to
            # transport validation; strict source validation runs afterwards.
            for keyword in ('enum', 'const'):
                if keyword in value:
                    result[keyword] = deepcopy(value[keyword])
        if 'properties' in value:
            result['type'] = 'object'
            result['properties'] = {key: shape(item, key, business_context=relaxed)
                                    for key, item in value['properties'].items()}
            # These are serialization fields, not proof or business conditions.
            required = set(value.get('required', []))
            readable = required if (root or not judgment) else required & ADVISORY_REQUIRED
            if readable:
                result['required'] = sorted(readable)
        if 'items' in value:
            result['items'] = shape(value['items'], business_context=relaxed)
        if '$defs' in value:
            result['$defs'] = {key: shape(item, business_context=business_context)
                               for key, item in value['$defs'].items()}
        if name == 'status':
            result = {'type': 'string'}
        return result
    return shape(schema,root=True)


def validate_transport(value, schema):
    """Only reject an unreadable wire object, never a business judgment."""
    from jsonschema import Draft202012Validator, ValidationError
    if not isinstance(value, dict):
        raise AuditError('模型返回须为可读取的JSON对象。')
    try:
        Draft202012Validator(schema).validate(value)
    except ValidationError as error:
        location = '/'.join(map(str, error.absolute_path)) or '$'
        raise AuditError('模型JSON字段无法读取：' + location + '；' + error.message) from error
    return value


def classification_route(value, selected):
    from .pdf_policy import POLICY_VERSION
    from .scenario_registry import SCENARIO_ORDER
    candidates = value.get('candidate_scenarios', [])
    scenario = selected or (candidates[0] if len(candidates) == 1 else None)
    if scenario not in SCENARIO_ORDER:
        scenario = None
    # Missing/unclear material is a fact for the AI to assess, not an execution gate.
    return {'scenario': scenario, 'matched': scenario is not None,
            'reasons': [] if scenario else [value.get('reason') or '模型尚未确认本单核销类型。'],
            'classification': value, 'policy_version': POLICY_VERSION,
            'business_authority': 'executing_model', 'deterministic_material_gate': False}


def decision(scenario, evidence, documents):
    from .pdf_policy import POLICY_VERSION, rules, SUBMITTED_SOURCE_CHECKS
    registered = {r.id: r for r in rules(scenario)}
    checks = []
    for original in evidence.get('checks', []):
        check = deepcopy(original)
        rule = registered.get(check.get('rule_id'))
        auxiliary = SUBMITTED_SOURCE_CHECKS.get(check.get('rule_id'), {})
        check.setdefault('comparisons', [])
        check.setdefault('source_ids', [])
        check.setdefault('reason', '')
        check.update(requirement=rule.text if rule else auxiliary.get('requirement', check.get('requirement', '')),
                     effect=rule.effect if rule else auxiliary.get('effect', ''))
        # Calculations already requested by the model remain evidence. Do not
        # recalculate, change a status, force applicability or synthesize checks.
        check.setdefault('calculations', [])
        checks.append(check)
    problems = [c for c in checks if c.get('status') in {'fail', 'unknown'}]
    return {**deepcopy(evidence), 'policy_version': POLICY_VERSION, 'scenario': scenario,
            'business_authority': 'executing_model', 'checks': checks,
            'summary': {'conclusion': 'issues_found' if problems else 'no_issues_found',
                        'error_count': len(problems)},
            'noncompliance_registration': [c['reason'] for c in problems]}


def business_guidance(scenario):
    """Deliver business standards without depending on a program's rule grouping."""
    from . import pdf_policy as policy
    from .qince_context import POLICY
    from .customer_language import CUSTOMER_PROBLEM_STYLE
    from .fee_policy_checks import WORKBOOK_EVIDENCE_PROMPT
    from .fee_policy_source import APPLICATION_CLARIFICATION
    common = ('AUDIT_SCOPE_CLARIFICATION', 'SUBMITTED_SOURCE_CLARIFICATION',
        'AMOUNT_CLARIFICATION', 'UNIT_PRICE_CLARIFICATION', 'SEAL_CLARIFICATION', 'COMPANY_TEXT_READING_GUIDANCE',
        'TEMPLATE_CLARIFICATION', 'BRAND_CONTRACT_CLARIFICATION', 'SKU_CONTRACT_CLARIFICATION',
        'POS_COMMON_CLARIFICATION', 'POS_CLARIFICATION', 'PRODUCT_PHOTO_CLARIFICATION',
        'PHOTO_WATERMARK_CLARIFICATION', 'PHOTO_TIME_READING_GUIDANCE', 'LOCATION_REFERENCE_CLARIFICATION')
    scoped = {
        'personnel_incentive': ('PERSONNEL_CONTRACT_CLARIFICATION', 'STAFF_PHOTO_CLARIFICATION',
            'PAYMENT_COMPANY_CLARIFICATION', 'PAYMENT_CLARIFICATION'),
        'entry_fee': ('ENTRY_CHANNEL_CLARIFICATION', 'ENTRY_SKU_CLARIFICATION',
            'ENTRY_ADDRESS_DUPLICATE_CLARIFICATION'),
        'promotional_display': ('DISPLAY_TABLE_PHOTO_CLARIFICATION', 'DISPLAY_SIZE_CLARIFICATION',
            'DISPLAY_SIZE_ESTIMATION_CLARIFICATION', 'DISPLAY_BRAND_CLARIFICATION'),
        'poster_material': ('INVOICE_DETAILS_CLARIFICATION',),
        'price_difference_support': ('PRICE_SUPPORT_CLARIFICATION',),
        'self_procured_gift_material': ('GIFT_MATERIAL_CLARIFICATION', 'GIFT_CONTRACT_CLARIFICATION',
            'INVOICE_DETAILS_CLARIFICATION'),
        'giveaway_promotion': ('GIVEAWAY_EVIDENCE_CLARIFICATION',),
    }
    return '\n'.join([*(getattr(policy, name) for name in (*common, *scoped.get(scenario, ()))),
        POLICY, APPLICATION_CLARIFICATION, WORKBOOK_EVIDENCE_PROMPT, CUSTOMER_PROBLEM_STYLE])


def material_prompt(scenario, documents, *, large_venue_fee=None, cvs_otc_channel=False, compact_inventory=False):
    from dataclasses import asdict
    import json
    from .prompts import bound_prompt
    from .pdf_policy import requirements
    from .pdf_materials import _prompt_documents, _gate_documents, _business_background
    from .scenario_registry import SKILL_BY_SCENARIO
    from .template_references import template_guide
    from .pdf_policy import STAFF_PHOTO_CLARIFICATION,ENTRY_CHANNEL_CLARIFICATION,INVOICE_DETAILS_CLARIFICATION
    from .pdf_materials import GIFT_RULE_GATE_CLARIFICATION
    skill = SKILL_BY_SCENARIO[scenario]
    guidance = (STAFF_PHOTO_CLARIFICATION if scenario=='personnel_incentive' else
                ENTRY_CHANNEL_CLARIFICATION if scenario=='entry_fee' else
                INVOICE_DETAILS_CLARIFICATION if scenario=='poster_material' else
                GIFT_RULE_GATE_CLARIFICATION
                if scenario=='self_procured_gift_material' else '')
    return bound_prompt('继续由你盘点本单资料，业务类型为{scenario}。依据本类Skill和实际原文判断资料角色及适用条件。'
        '这一步只整理后续审核要用的材料，不设置资料门禁；缺件、多交、字段或条件不明均不停止业务审核。'
        '保留已有资料并如实说明限制；后续由你结合完整资料判断具体问题，不为满足程序匹配而改写事实。'
        'candidate_scenarios保留本次类型；flags、materials及material_matches沿用接口形状，supported只作你的盘点记录。'
        '顶层与各类别内的flags/materials是同一份盘点的两份镜像，只写一份即可，程序按本单类型复用；两份都写时必须一致。'
        '接口事实largeVenueFee={large}、cvsOtcChannel={channel}；它们是本单已知业务输入，不是程序审核结论。'
        '按需使用资料和同单关联明细，不把系统明细冒充独立上传原件。'
        '条件性资料可用condition_evidence记录原文依据，不为填齐字段臆断适用。'
        '参考样张只说明资料角色和样式，不计本包来源，不补齐本次上传资料。'
        '\n资料要求：{requirements}\n本类业务说明：{guide}\n本类条件及资料角色：{guidance}'
        '\n指定样张说明：{template_guide}\n业务背景：{background}\n全部已读事实：{documents}',
        scenario=scenario, large=json.dumps(large_venue_fee), channel=json.dumps(cvs_otc_channel),
        requirements=json.dumps([asdict(r) for r in requirements(scenario)], ensure_ascii=False),
        guide=(skill/'references/business-guide.md').read_text('utf-8'),guidance=guidance,
        template_guide=template_guide(skill),background=_business_background(),
        documents=(_gate_documents(documents) if compact_inventory else _prompt_documents(documents)))
