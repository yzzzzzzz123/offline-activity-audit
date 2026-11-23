"""Source-bound identity for saved requests, independent of display labels.

Uses existing trace data and transport projections. No network or model calls.
"""
import hashlib
import json
import math
from pathlib import Path
import re

from .common import AuditError


class _Number(str):
    """Keep a JSON number's original decimal spelling without float rounding."""


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


_DECODER = json.JSONDecoder(object_pairs_hook=_object, parse_int=_Number, parse_float=_Number,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def _identity_tree(value):
    # Object key order is transport formatting; array order, exact numbers and
    # source strings (including JSON-looking quotations) remain evidence.
    if isinstance(value, dict):
        return ['object', [[key, _identity_tree(value[key])] for key in sorted(value)]]
    if isinstance(value, (list, tuple)):
        return ['array', [_identity_tree(item) for item in value]]
    if isinstance(value, _Number):
        return ['number', str(value)]
    if value is None or isinstance(value, bool):
        return ['literal', value]
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise AuditError('断点来源包含非有限数值')
        return ['number', str(value)]
    if isinstance(value, str):
        return ['string', value]
    raise AuditError('断点来源包含不可序列化的数据')


def _digest(value):
    raw = json.dumps(_identity_tree(value), ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _prompt_parts(text):
    """Normalize embedded JSON objects, never the evidence strings inside them.

    Persistent fact caches serialize keys in a different order from fresh
    reads. Parse whole outer JSON values once; keep all other prompt text exact.
    No field is removed, no list sorted, and no source number is rounded.
    """
    parts, previous, cursor = [], 0, 0
    opening = re.compile(r'[\[{]')
    while cursor < len(text):
        match = opening.search(text, cursor)
        if match is None:
            break
        start = match.start()
        try:
            value, end = _DECODER.raw_decode(text, start)
        except ValueError:
            cursor = start + 1
            continue
        parts.extend([text[previous:start], value])
        previous = cursor = end
    parts.append(text[previous:])
    return parts


def _audit_source(text):
    # Existing audit chunks carry a complete typed source packet. Separate it
    # from instructions so the *failed* stage can use corrected rules, while
    # its scope, actual facts and references must still be exactly the same.
    markers = [re.search(pattern, text) for pattern in
               (r'(?m)^本批索引：', r'(?m)^本批完整对应原始事实：', r'(?m)(?:^|；)本单flags：')]
    if all(markers):
        try:
            focus, _ = _DECODER.raw_decode(text[markers[0].end():].lstrip())
            documents, _ = _DECODER.raw_decode(text[markers[1].end():].lstrip())
            flags, _ = _DECODER.raw_decode(text[markers[2].end():].lstrip())
            if not isinstance(focus, dict) or not isinstance(documents, (dict, list)) or not isinstance(flags, dict):
                raise ValueError('invalid typed source packet')
            return {'facts': documents, 'flags': flags,
                    'scope': {key: focus[key] for key in ('rule_ids','reference_fields','material_inventory')}}
        except (KeyError, ValueError, IndexError, TypeError):
            raise AuditError('断点请求的原始来源包不完整，禁止按阶段名称猜测复用') from None
    return None


def _source_payload(text, *, summary_review=False):
    # Extraction, name matching and other generic calls keep their complete
    # input: unit IDs, file/chunk context, all fact indexes and every raw value.
    source = ((_summary_source(text) if summary_review else None) or _audit_source(text) or _generic_audit_source(text)
              or _photo_source(text) or _reading_source(text))
    return source if source is not None else _prompt_parts(text)


def _summary_source(text):
    """Preserve the entire review input while allowing corrected presentation instructions."""
    markers = list(re.finditer('待核验完整内容：', text))
    if not markers:
        return None
    try:
        if len(markers) != 1:
            raise ValueError('ambiguous summary request')
        value, _ = _DECODER.raw_decode(text[markers[0].end():].lstrip())
        if (not isinstance(value, dict) or value.get('phase') not in {'stage', 'final'}
                or not isinstance(value.get('problems'), list)
                or any(not isinstance(p, dict) or not isinstance(p.get('id'), str)
                       or not isinstance(p.get('text'), str) for p in value['problems'])):
            raise ValueError('invalid summary request')
        # Only this known instruction field is not evidence. All source IDs,
        # flags, statuses, text, sections, draft and unexpected data remain bound.
        return {'summary_review': {key: item for key, item in value.items() if key != 'instruction'}}
    except (ValueError, TypeError):
        raise AuditError('总结断点缺少完整本单审核结果，禁止猜测复用') from None


def _generic_audit_source(text):
    """The existing full RAG prompt's evidence, including visual stages.

    These stages predate the compact audit packet and still use its original
    five headings. Keep the complete typed sources and actual selected rules;
    unrelated shared policy prose is checked by the current schema/validator,
    rather than becoming part of an original document's identity.
    """
    names = ('本批索引：', '已经识别的完整事实：', '已盘点的资料清单：',
             '适用条件：', '唯一审核要点：')
    markers = [list(re.finditer(r'(?m)^' + re.escape(name), text)) for name in names]
    if not markers[0] or not markers[1]:
        return None
    try:
        if any(len(matches) != 1 for matches in markers):
            raise ValueError('missing or ambiguous source heading')
        focus, documents, materials, flags, rules = [
            _DECODER.raw_decode(text[matches[0].end():].lstrip())[0] for matches in markers]
        if (not isinstance(focus, dict) or not isinstance(documents, (dict, list))
                or not isinstance(materials, (dict, list)) or not isinstance(flags, dict)
                or not isinstance(rules, list) or not rules):
            raise ValueError('invalid full audit source packet')
        rule_ids = focus['rule_ids']
        if (not isinstance(rule_ids, list) or not rule_ids
                or not all(isinstance(value, str) and value for value in rule_ids)
                or len(rule_ids) != len(set(rule_ids))
                or any(not isinstance(rule, dict) or not isinstance(rule.get('id'), str) for rule in rules)
                or len(rules) != len(rule_ids) or {rule['id'] for rule in rules} != set(rule_ids)
                or not isinstance(focus['reference_fields'], dict)
                or _digest(focus['material_inventory']) != _digest(materials)):
            raise ValueError('scope disagrees with complete evidence')
        auxiliary = {}
        # Preserve existing case-dependent reference/manifest sections too.
        # Their payloads are single rendered lines; JSON key order alone is
        # formatting, while original values, text and list order stay exact.
        for name in (
                '本次补差价格证明图片来源（其他类型忽略）：', '本次现场照片及unit_id：',
                '本次对应OSS细节图记录（reference_only，不是本次证据）：',
                '附带的公司指定模板页面（reference_only，不是本次核销来源）：',
                '附带的本次待核对文件页面与unit_id：',
                '程序已仅从本次POS Excel提取商品名称和69码并实际查库，结果：',
                '程序按原始工作表行列完整复算的结果（null表示当前格式尚未建立原生全量计划）：',
                '附件字节相同的来源对应表：'):
            matches = list(re.finditer(r'(?m)^' + re.escape(name), text))
            if len(matches) > 1:
                raise ValueError('ambiguous auxiliary source heading')
            if matches:
                payload = text[matches[0].end():].split('\n', 1)[0].rstrip('\r')
                auxiliary[name] = _prompt_parts(payload)
        return {'facts': documents, 'flags': flags, 'scope': {
            'rule_ids': rule_ids, 'reference_fields': focus['reference_fields'],
            'material_inventory': materials, 'audit_rules': rules,
            'retrieval_scope': {key: value for key, value in focus.items()
                                if key not in ('rule_ids', 'reference_fields', 'material_inventory')},
            'auxiliary_sources': auxiliary}}
    except (KeyError, ValueError, IndexError, TypeError):
        raise AuditError('通用审核断点的完整事实、资料盘点或审核范围不一致，禁止猜测复用') from None


def _photo_source(text):
    """The photo stage's complete typed evidence, independent of ref wording."""
    names = ('本次已读全部原文及表格行关系：','本批逐张照片来源：',
             '本单已有独立系统活动申请来源：','本次合同来源unit_id：',
             'source_ids必须逐字复制程序给出的真实unit_id，只能使用本批允许的来源：',
             '本次商品参考（仅辨认外观，不是现场来源）：','只按下方四项已有规则，不增加检查：')
    markers=[re.search(r'(?m)^'+re.escape(name),text) for name in names]
    if not all(markers):
        return None
    try:
        values=[_DECODER.raw_decode(text[m.end():].lstrip())[0] for m in markers]
        docs,manifest,applications,contracts,allowed,references,rules=values
        if (not isinstance(docs,(dict,list)) or not isinstance(manifest,list)
                or not all(isinstance(v,list) for v in (applications,contracts,allowed,rules))
                or not isinstance(references,(dict,list))):
            raise ValueError('invalid photo source packet')
        return {'facts':{'documents':docs,'manifest':manifest},'flags':{},
                'scope':{'rule_ids':rules,'reference_fields':references,
                         'material_inventory':{'applications':applications,'contracts':contracts,'allowed':allowed}}}
    except (ValueError,TypeError,KeyError):
        raise AuditError('照片断点原始来源包不完整，禁止猜测复用') from None


def _reading_source(text):
    # Historical readers received the full catalogue including audit-only
    # clarifications. Match the same positive reading contract as new readers,
    # retaining every instruction outside that typed downstream metadata.
    from .reading_contract import reading_policy_context
    policy=re.search(r'(?m)^适用规则（只用于决定需要提取哪些事实，不在此选型）：',text)
    manifest=re.search(r'(?m)^本批资料：',text)
    if policy is None or manifest is None:
        return None
    try:
        def decode_after(marker):
            start=marker.end()
            while start<len(text) and text[start].isspace():start+=1
            value,end=_DECODER.raw_decode(text,start)
            return value,start,end
        catalogue,p_start,p_end=decode_after(policy)
        sources,m_start,m_end=decode_after(manifest)
        if not isinstance(catalogue,dict) or not isinstance(sources,list) or p_end>manifest.start():
            raise ValueError('invalid reader source packet')
        return {'facts':sources,'flags':{},'scope':{
            'reading_policy':reading_policy_context(catalogue),
            'prefix':text[:p_start],'between':text[p_end:m_start],'suffix':text[m_end:]}}
    except (ValueError,TypeError):
        raise AuditError('原始读取的来源或读取契约不完整，禁止猜测复用') from None


def reading_manifest_identity(manifest):
    """A full reader source descriptor, never just its visible alias."""
    if (not isinstance(manifest,dict)
            or any(not isinstance(manifest.get(key),str) or not manifest[key] for key in ('unit_id','image'))
            or any(not isinstance(manifest.get(key),list)
                   or any(not isinstance(value,str) for value in manifest[key])
                   for key in ('pdf_text','reading_limitations'))):
        raise AuditError('原读取断点缺少完整图片、原生文字或读取限制来源')
    return _digest(manifest)


def reading_manifest_identities(text):
    source=_reading_source(text)
    return None if source is None else tuple(reading_manifest_identity(item) for item in source['facts'])


def request_identity(request, label):
    required = ('provider','model','reasoning_effort','skill','input_text','output_schema','images')
    if (any(key not in request for key in required)
            or any(not isinstance(request[key], str) or not request[key]
                   for key in required[:5])
            or not isinstance(request['output_schema'], dict)
            or not isinstance(request['images'], list)):
        raise AuditError('断点缺少完整原始请求，禁止仅凭展示名称复用')
    images = []
    for item in request['images']:
        if not isinstance(item, dict) or any(key not in item for key in ('file','size_bytes','sha256')):
            raise AuditError('断点缺少原始图片指纹')
        images.append({key:item[key] for key in ('file','size_bytes','sha256')})
    purpose = {key:request[key] for key in ('provider','model','reasoning_effort','skill')}
    purpose.update(label=label, images=images)
    full = {**purpose, 'input':_prompt_parts(request['input_text']), 'schema':request['output_schema']}
    source = (_audit_source(request['input_text']) or _generic_audit_source(request['input_text'])
              or _photo_source(request['input_text'])
              or _reading_source(request['input_text']))
    lineage = None
    if source is not None:
        # Completed-record correction may only reconsider a known audit block
        # with the same original documents/rules. Corrected derived references
        # may change; arbitrary extraction or newly introduced stages may not.
        lineage_source = {**source, 'scope': {k:v for k,v in source['scope'].items() if k != 'reference_fields'}}
        lineage = _digest({**purpose, 'source':lineage_source})
    is_summary = {'ready', 'issues', 'items'} <= set(request['output_schema'].get('properties', {}))
    return {'request':_digest(full),
            'source':_digest({**purpose, 'source':_source_payload(request['input_text'], summary_review=is_summary)}),
            'lineage':lineage}


def current_request(connection, skill, schema, prompt, images, *, transcription=False):
    """Match the normal API adapter's deterministic delivery transformations."""
    from .extraction_chain import render_prompt
    from .pdf_materials import ORCHESTRATOR_SKILL
    from .prompt_delivery import share_schema_definitions, share_system_business_guide
    from .prompts import bound_prompt
    from .stage_context import reading_context, share_stage_prompt
    if not transcription:
        from .model_authority import transport_schema
        schema = transport_schema(schema)

    context = reading_context() if transcription else getattr(prompt, 'partial_variables', {}).get('_audit_stage_context')
    prompt = share_stage_prompt(prompt, context)
    delivered = share_schema_definitions(schema) if not transcription and skill != ORCHESTRATOR_SKILL else schema
    if context is None:
        prompt, _ = share_system_business_guide(prompt, skill)
    sent, aliases, seen = [], [], {}
    for path in images:
        raw = Path(path).read_bytes()
        sha = hashlib.sha256(raw).hexdigest()
        if sha in seen:
            aliases.append({'image':path.name,'same_bytes_as':seen[sha],'sha256':sha})
        else:
            seen[sha] = path.name
            sent.append({'file':path.name,'size_bytes':len(raw),'sha256':sha})
    if aliases:
        prompt = bound_prompt('{original}\n附件字节相同的来源对应表：{aliases}\n'
            '上述image使用same_bytes_as的同一原图；每个来源unit_id、位置和本次核验要求仍分别保留，'
            '不得因此合并门店、付款记录或审核结论。', original=render_prompt(prompt),
            aliases=json.dumps(aliases, ensure_ascii=False))
    effort = connection.reasoning_effort
    return {'provider':connection.provider,'model':connection.model,'reasoning_effort':effort,
            'skill':Path(skill).name,'input_text':render_prompt(prompt),'output_schema':delivered,'images':sent}
