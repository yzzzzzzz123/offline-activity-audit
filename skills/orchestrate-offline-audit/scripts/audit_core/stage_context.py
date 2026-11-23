"""Compile source-exact rules for one stage, rather than three whole guides."""
from __future__ import annotations

from dataclasses import asdict
import json

# A planning target only: it can split multi-rule work, never rejects input or
# caps a provider response. User removed application model I/O limits.
STAGE_SPLIT_TARGET_BYTES = 128_000

STAGE_CONTEXT_SCHEMA = {
    'type': 'object', 'additionalProperties': False,
    'required': ['version', 'phase', 'scenario', 'rule_ids', 'rules', 'policies', 'max_text_bytes', 'max_output_tokens'],
    'properties': {
        'version': {'const': 1}, 'phase': {'enum': ['audit', 'transcription', 'material_gate']},
        'scenario': {'type': 'string'},
        'rule_ids': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
        'rules': {'type': 'array', 'items': {'type': 'object'}},
        'policies': {'type': 'object', 'additionalProperties': {'type': 'string'}},
        'max_text_bytes': {'const': None},
        'max_output_tokens': {'const': None},
    },
}


def compile_context(scenario, rule_ids, flags):
    from . import pdf_policy as policy
    from .audit_rag import ROLE_GROUPS
    from .qince_context import POLICY
    from .qince_references import REFERENCE_POLICY
    from .customer_language import CUSTOMER_PROBLEM_STYLE
    from .fee_policy_checks import WORKBOOK_EVIDENCE_PROMPT
    from .fee_policy_source import APPLICATION_CLARIFICATION
    from .audit_tools import tool_protocol

    by_id = {r.id: r for r in policy.rules(scenario)}
    if len(set(rule_ids)) != len(rule_ids) or not set(rule_ids).issubset(by_id):
        from .common import AuditError
        raise AuditError('阶段规则包包含未登记或重复要点。')
    groups = {name for name, (_, ids) in ROLE_GROUPS.items() if set(rule_ids) & ids}
    if not set(rule_ids).issubset(set().union(*(ids for _, ids in ROLE_GROUPS.values()))):
        return None  # Unregistered dependencies retain the original full guide.
    names = {'RESULT_CLARIFICATION', 'SUBMITTED_SOURCE_CLARIFICATION'}
    if any(by_id[key].numeric for key in rule_ids):
        names |= {'AMOUNT_CLARIFICATION', 'UNIT_PRICE_CLARIFICATION'}
    if groups & {'documents'}:
        names |= {'SEAL_CLARIFICATION', 'TEMPLATE_CLARIFICATION'}
    if groups & {'pos', 'pos_settlement', 'pos_quantity'}:
        names |= {'POS_COMMON_CLARIFICATION', 'POS_CLARIFICATION'}
    if groups & {'settlement', 'pos_settlement', 'pos_quantity', 'budget'}:
        names |= {'BRAND_CONTRACT_CLARIFICATION', 'SKU_CONTRACT_CLARIFICATION'}
        if scenario == 'personnel_incentive':
            names |= {'PERSONNEL_CONTRACT_CLARIFICATION', 'STAFF_PHOTO_CLARIFICATION'}
    if 'payment' in groups:
        names |= {'PAYMENT_COMPANY_CLARIFICATION', 'PAYMENT_CLARIFICATION'}
    if 'invoice' in groups:
        names.add('INVOICE_DETAILS_CLARIFICATION')
    if 'photos' in groups:
        names |= {'PRODUCT_PHOTO_CLARIFICATION', 'PHOTO_WATERMARK_CLARIFICATION', 'LOCATION_REFERENCE_CLARIFICATION'}
        if scenario == 'promotional_display':
            names |= {'DISPLAY_TABLE_PHOTO_CLARIFICATION', 'DISPLAY_SIZE_CLARIFICATION',
                      'DISPLAY_SIZE_ESTIMATION_CLARIFICATION', 'DISPLAY_BRAND_CLARIFICATION'}
        if scenario == 'personnel_incentive':
            names.add('STAFF_PHOTO_CLARIFICATION')
    if scenario == 'entry_fee' and 'entry' in groups:
        names |= {'ENTRY_CHANNEL_CLARIFICATION', 'ENTRY_SKU_CLARIFICATION', 'ENTRY_ADDRESS_DUPLICATE_CLARIFICATION'}
    if scenario == 'price_difference_support':
        names.add('PRICE_SUPPORT_CLARIFICATION')
    if scenario == 'self_procured_gift_material':
        names |= {'GIFT_MATERIAL_CLARIFICATION', 'GIFT_CONTRACT_CLARIFICATION'}
    if scenario == 'giveaway_promotion':
        names.add('GIVEAWAY_EVIDENCE_CLARIFICATION')
    policies = {name: getattr(policy, name) for name in sorted(names)}
    from .audit_rag import needed_fields
    source_policy = (POLICY.removesuffix(REFERENCE_POLICY) if needed_fields(rule_ids) or groups & {'photos', 'entry'}
                     else '本阶段仅使用对应上传材料及已验证程序事实，不要求对齐其他阶段的勤策字段，不新增资料要求。')
    policies.update(source_policy=source_policy,
                    customer_language=CUSTOMER_PROBLEM_STYLE, tool_protocol=tool_protocol(rule_ids))
    if needed_fields(rule_ids):
        policies['application_scope'] = APPLICATION_CLARIFICATION
    if groups & {'photos', 'entry'}:
        policies['reference_scope'] = ('门店主档仅作名称和位置辅助；商品尺寸须对应本单同一商品及包装单位，'
            '单位不同不能混用。申报规格不是现场已达标证据。历史识别或铺货结论不代替本次逐图审核。')
    if set(rule_ids) & {'payment_activity_dates', 'target_reward'}:
        policies['structured_workbook_evidence'] = WORKBOOK_EVIDENCE_PROMPT
    from .model_authority import enabled
    return {'version': 1, 'phase': 'audit', 'scenario': scenario, 'rule_ids': list(rule_ids),
            'max_text_bytes': None, 'max_output_tokens': None,
            'rules': [asdict(by_id[key]) if enabled() else
                      {**asdict(by_id[key]), 'applicable': policy.applicability(by_id[key].when, scenario, flags)}
                      for key in rule_ids], 'policies': policies}


def attach_context(prompt, scenario, rule_ids, flags):
    context = compile_context(scenario, rule_ids, flags)
    return prompt.partial(_audit_stage_context=context) if context is not None else prompt


def share_stage_prompt(prompt, context):
    """Remove only bound static instruction copies already in this system.

    Never search/replace material text, quotes, numeric operands or source data.
    The compiled rule catalogue/policy dependencies are the authoritative text.
    """
    if not context or context['phase'] != 'audit':return prompt
    replacements = {}
    policies = set(context['policies'].values())
    for name, value in getattr(prompt, 'partial_variables', {}).items():
        if name == 'business_guide':
            replacements[name] = '按system内本阶段完整规则、适用条件和补充口径执行。'
        elif (name.endswith('_policy') or name in {'amount_clarification', 'pos_clarification', 'customer_language'}) and value in policies:
            replacements[name] = '本项完整口径见system内本阶段规则。'
    return prompt.partial(**replacements) if replacements else prompt


def reading_context():
    return {'version': 1, 'phase': 'transcription', 'scenario': '', 'rule_ids': [], 'rules': [],
            'max_text_bytes': None, 'max_output_tokens': None,
            'policies': {'scope': '只读取本批每张图片的全部可见事实及真实读取限制，不执行资料门禁或核销判断。'
                         '完整保留表格、数字、经销商填写名称及落章名称、日期、水印和来源；不借其他文件补写原图。'
                         '只按本次Schema输出，不读取其他核销单、历史回答或其他阶段的业务标准。'}}


def material_context(scenario):
    return {'version': 1, 'phase': 'material_gate', 'scenario': scenario, 'rule_ids': [], 'rules': [],
            'max_text_bytes': None, 'max_output_tokens': None,
            'policies': {'scope': '仅执行本次提示中本类完整资料清单及已确认适用条件的盘点，所有unit_id须归属。'
                         '不执行其他阶段的金额、日期、印章、模板、照片合规审核，不读其他类别全文。'
                         '资料不足及条件不明均如实记录，继续审核可判断的内容；由模型判断适用条件，程序不设业务门禁。'}}


def encode_context(context):
    import jsonschema
    jsonschema.Draft202012Validator(STAGE_CONTEXT_SCHEMA).validate(context)
    return json.dumps(context, ensure_ascii=False, separators=(',', ':'))


def stage_plan(scenario):
    """Generated standard; grouping targets never act as model I/O caps."""
    from .audit_rag import ROLE_GROUPS, needed_fields
    from .pdf_policy import rules
    from .qince_references import reference_needs
    ordered = [r.id for r in rules(scenario)]
    stages = []
    for name, (roles, members) in ROLE_GROUPS.items():
        ids = [key for key in ordered if key in members]
        width = 1 if name == 'settlement' else 4
        for offset in range(0, len(ids), width):
            selected = ids[offset:offset+width]
            stages.append({'name': name, 'rule_ids': selected, 'material_roles': sorted(roles),
                           'reference_fields': needed_fields(selected)})
    return {'version': 1, 'scenario': scenario, 'qince_auxiliaries': sorted(reference_needs(scenario)),
            'stages': stages, 'max_rules_per_split_stage': 4, 'max_text_bytes': None,
            'max_output_tokens': None, 'maximum_rework_calls_per_stage': None,
            'small_packet_policy': '用途分组仅作模型选择资料和工具的导航，不固定拆分审核或设置上下文及输出上限。',
            'advance_when': ['同一执行模型结合本单原始资料自主完成判断和自查；工具、引用匹配及证据结构不设业务门禁'],
            'business_limitations': '真实缺件、原图不清或来源冲突按原业务规则完成，不伪造通过；技术漏读不得冒充业务缺件。'}
