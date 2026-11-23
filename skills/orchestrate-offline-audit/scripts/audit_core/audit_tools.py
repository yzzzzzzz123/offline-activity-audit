"""Case tools and separately authorized local deterministic source maintenance."""
from __future__ import annotations

from copy import deepcopy

from .common import AuditError

_TOOL_DETAILS = {
    'reference_fields': '本阶段实际参照字段',
    'linked_application_amounts': '本单关联申请行去重合计与审批状态，派生合计不是独立主档总额或最终批复',
    'pos_calculation': '原生POS全量精确复算，保留全部原始算式',
    'source_inventory': '本单已准备来源盘点，不按文件扩展名猜用途',
    'activity_periods': '合同/结算单正文、表格、简写与分档日期，只按实际来源解析',
    'pos_field_inventory': '原生POS5+2字段全量盘点，未完整证明仍核对原资料，不据此报缺件',
    'pos_matrix_identity': '横向POS矩阵名称与69码同列绑定，原销量行保留，不补价格或金额',
    'pos_product_lookup': '按本单Excel原行准确69码实际查商品库，products须含unit_id、fact_indexes及product_name/barcode_69的fact_index/quote/value；不下载商品图',
    'case_python': '执行模型生成本单临时Python分析工具，读取本单原始facts并返回可复核结果',
    'project_source': '仅本机授权进程修复项目已有确定性函数；隔离验证后写入真实源码并立即生效',
}

TOOL_REQUEST_SCHEMA = {'type': 'array', 'items': {
    'type': 'object', 'additionalProperties': False, 'required': ['tool', 'rule_ids', 'fields'],
    'properties': {
        'tool': {'type': 'string', 'enum': list(_TOOL_DETAILS)},
        'code': {'type': 'string'},
        'source_ids': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string'}},
        'products': {'type': 'array', 'items': {'type': 'object'}},
        'rule_ids': {'type': 'array', 'minItems': 1, 'uniqueItems': True, 'items': {'type': 'string'}},
        'fields': {'type': 'array', 'uniqueItems': True, 'items': {'type': 'string', 'enum': [
            'advance_dealer', 'activity_period', 'application_quantity', 'application_unit_price',
            'application_products', 'application_total', 'approved_total']}}}}}

def tool_protocol(rule_ids=None):
    """Advertise only useful tools, keeping the saved-response contract open.

    This is instruction selection, not a new rejection of old valid replies.
    All existing source/schema/request validation continues unchanged.
    """
    from .audit_rag import needed_fields
    from .local_source_repair import authorized, protocol
    local_source = authorized()
    selected = set(rule_ids or ())
    names = {'source_inventory', 'case_python'}
    if rule_ids is None:
        names = set(_TOOL_DETAILS)
    else:
        if needed_fields(selected):names.add('reference_fields')
        if selected & {'contract_budget_amount','claim_ceiling','application_claim_ceiling'}:
            names.add('linked_application_amounts')
        if selected & {'pos_fields','pos_arithmetic','settlement_pos_quantity','gift_rule_quantity','pos_sales_amount','target_reward'}:
            names.add('pos_calculation')
        if selected & {'contract_activity_dates','settlement_application_dates','settlement_activity_dates',
                       'photo_activity_dates','display_watermark','entry_watermark','payment_activity_dates'}:
            names.add('activity_periods')
        if 'pos_fields' in selected:names.add('pos_field_inventory')
        if selected & {'pos_product_identity','application_products'}:
            names.update({'pos_matrix_identity', 'pos_product_lookup'})
    if local_source:names.add('project_source')
    else:names.discard('project_source')
    return ('已有工具优先用于节省Token，其结果和成败均不决定业务结论。'
            '需要补查时保留checks，并以可选tool_requests请求tool/rule_ids/fields；'
            'rule_ids仅本阶段待处理项，fields仅所需参照字段，不需要时[]。本阶段按需工具：'
            + '；'.join(name+'（'+description+'）' for name,description in _TOOL_DETAILS.items() if name in names)
            + '。工具仅为节省Token的辅助，模型具有最终判断权。工具不支持、失败、不完整或与资料冲突时，'
              '执行模型可直接读取原始资料完成，或用case_python生成临时解析、计算与核对工具，不受原工具固定路径约束。'
              'case_python请求附code与source_ids（本单原始unit_id，[]表示当前本单全部来源）；fields=[]。'
              '代码可用documents/materials/flags、source(unit_id)、Decimal、json、re、math、statistics及常用Python内置函数，'
              '将结果放在result变量。无需导入模块；可以编写函数、循环和新的解析逻辑，不读取宿主文件、网络或其他单据。'
              '程序只读执行后交回本阶段，已有结果不重复请求。工具统计或派生值不冒充原始operand；'
              '不循环请求，不执行任意路径、命令、SQL、密钥或其他核销单。' + protocol())


TOOL_PROTOCOL = tool_protocol()


def assist(name, operation, *, fallback=None):
    """An optimization failure hands control back to the execution model."""
    try:
        return operation()
    except (AuditError, ValueError, KeyError, TypeError, ArithmeticError, OSError) as error:
        from .execution_trace import record_step
        from .execution_diagnostics import _text
        record_step('audit.tool_model_takeover', '辅助工具未完成，交由模型读取原资料处理', {
            'tool': name, 'error_type': type(error).__name__, 'error': _text(error),
            'model_takeover': True, 'business_conclusion': None}, status='needs_model')
        return fallback(error) if callable(fallback) else deepcopy(fallback)


def execute_tool(case, name, *, fields=(), code=None, source_ids=(), products=()):
    # Validate authority before entering the recoverable implementation.
    if name not in _TOOL_DETAILS:
        raise AuditError('未登记的核销工具，不执行。')
    if name == 'project_source':
        from .local_source_repair import run
        return run(case, code)
    if name == 'case_python':
        from .case_python import run
        value = run(case, code, source_ids)
        from .tool_observations import remember
        remember(case,name,fields=fields,code=code,source_ids=source_ids,result=value)
        return value
    if name == 'reference_fields':
        from .audit_rag import FIELD_RULES
        if not set(fields).issubset(FIELD_RULES):
            raise AuditError('工具只允许本单已登记参照字段。')
    def failed(error):
        from .execution_diagnostics import _text
        return {'tool': name, 'model_calls': 0, 'complete': False,
            'status': 'model_takeover', 'error_type': type(error).__name__,
            'error': _text(error), 'fallback': 'read_original_stage_sources',
            'instruction': '本工具仅为辅助。请执行模型读取本阶段原始资料，自行完成该步骤并继续；工具失败不构成客户资料问题。'}
    value = assist(name, lambda: _execute_tool(case, name, fields=fields, products=products), fallback=failed)
    from .tool_observations import remember
    remember(case,name,fields=fields,code=code,source_ids=source_ids,result=value,
             **({'parameters': {'products': products}} if name == 'pos_product_lookup' else {}))
    return value


def _execute_tool(case, name, *, fields=(), products=()):
    """Read-only case tools; only the catalog lookup may read the product DB."""
    if name == 'pos_product_lookup':
        from .pos_product_identity import lookup_pos_products
        return lookup_pos_products(case, products)
    from .audit_rag import reference_fields, reference_view
    from .audit_self_check import money_context
    if name == 'reference_fields':
        indexed = reference_fields(case['documents'], case.get('units'))
        if not set(fields).issubset(indexed):
            raise AuditError('工具只允许本单已登记参照字段。')
        return {'tool': name, 'model_calls': 0, 'fields': reference_view(
            {key: indexed[key] for key in (fields or indexed)})}
    if name == 'linked_application_amounts':
        return {'tool': name, 'model_calls': 0, **money_context(case)}
    if name == 'pos_calculation':
        from .pos_arithmetic import prepare_pos_arithmetic
        from .pdf_evidence import calculate, document_map
        plan = case.get('_native_pos_plan')
        if plan is None:
            plan = prepare_pos_arithmetic(case['documents'], case['materials'], case.get('units'))
        if plan is None:
            return {'tool': name, 'model_calls': 0, 'applicable': False, 'calculation_count': 0,
                    'scope': '本单没有适用的原生POS明细；不据此新增必交资料。'}
        sources = document_map(case['documents'])
        calculations = [calculate(c, sources) for c in plan['comparisons']]
        from .model_metrics import save_model_observations
        save_model_observations('tool-pos-calculation-' + case.get('archive_id','case'),
                                {'plan': plan, 'calculations': calculations})
        return {'tool': name, 'model_calls': 0, 'complete': plan['complete'],
                'source_ids': plan['source_ids'], 'calculation_count': len(calculations),
                'difference_count': sum(not c['matches'] for c in calculations),
                'limitations': plan['limitations'],
                'full_original_calculations_saved': True,
                'scope': '原值及全部算式由既有程序核对；本摘要不是原凭据，不将统计数用作operand。'}
    if name == 'source_inventory':
        return {'tool': name, 'model_calls': 0, 'materials': deepcopy(case['materials']),
                'sources': [{key: u.get(key) for key in ('unit_id','source_file','locator','source_kind','detected_format','row_range','sheet_layout')}
                            for u in case.get('units',[])]}
    if name == 'pos_field_inventory':
        from .native_pos_fields import native_pos_field_check
        check, proof = native_pos_field_check(case, case.get('_native_pos_plan'))
        from .model_metrics import save_model_observations
        save_model_observations('tool-native-pos-fields-' + case.get('archive_id', 'case'), proof)
        return {**proof, 'check': check}
    if name == 'pos_matrix_identity':
        from .material_sources import material_source_ids
        from .native_pos_matrix import native_matrix_extractions
        from .pos_product_identity import validate_extraction
        ids = set(material_source_ids(case, 'pos_excel'))
        docs = [d for d in case['documents'] if d['unit_id'] in ids]
        extracted, proof = native_matrix_extractions(docs, case.get('units', []))
        for doc in docs:
            if doc['unit_id'] in extracted:
                validate_extraction(extracted[doc['unit_id']]['extracted'], doc)
        from .model_metrics import save_model_observations
        save_model_observations('tool-pos-matrix-identity-' + case.get('archive_id', 'case'),
                                {'receipt': proof, 'extractions': extracted})
        return {**proof, 'complete': bool(ids) and set(extracted) == ids,
                'products': [{'unit_id': uid, **row} for uid, value in extracted.items()
                             for row in value['extracted']['rows']],
                'source_proof': '原文见本单原生facts及所列unit_id/fact_index；正常流程另存完整工具观察。'}
    if name == 'activity_periods':
        from .activity_periods import case_activity_periods
        facts = {d['unit_id']: d['facts'] for d in case['documents']}
        readings = case_activity_periods(facts, case['materials'])
        from .model_metrics import save_model_observations
        save_model_observations('tool-activity-periods-' + case.get('archive_id', 'case'), readings)
        # Source facts are already in this stage's documents. Return pointers
        # rather than another copy of long whole-paragraph quotations.
        return {'tool': name, 'model_calls': 0, 'documents': {role: {
            **reading, 'periods': [{**p, 'sources': [{k:s[k] for k in ('unit_id','fact_index')} for s in p['sources']]}
                                  for p in reading['periods']],
            'limitations': [{k:v for k,v in p.items() if k != 'quote'} for p in reading['limitations']]}
            for role, reading in readings.items()}, 'full_source_proof_saved': True}
    raise AuditError('未登记的核销工具，不执行。')


def dispatch_requests(case, requests, rule_ids):
    from .model_authority import enabled
    if enabled():
        results = []
        for request in requests:
            try:
                if not isinstance(request,dict):
                    raise AuditError('工具请求须为JSON对象。')
                value = execute_tool(case, request.get('tool'), fields=request.get('fields',[]),
                    code=request.get('code'), source_ids=request.get('source_ids',[]),
                    products=request.get('products', []))
            except (AuditError,ValueError,KeyError,TypeError,ArithmeticError,OSError) as error:
                from .execution_diagnostics import _text
                value = {'complete':False,'status':'model_takeover','error':_text(error),
                    'instruction':'请求未完成；由你选择其他工具或结合原事实继续判断，不影响业务结论。'}
            results.append({'request':deepcopy(request),'result':value})
        return results
    import jsonschema
    from .audit_rag import needed_fields
    jsonschema.Draft202012Validator(TOOL_REQUEST_SCHEMA).validate(requests)
    results = []
    for request in requests:
        if not set(request['rule_ids']).issubset(rule_ids) or not set(request['fields']).issubset(needed_fields(request['rule_ids'])):
            raise AuditError('工具请求不能越过当前阶段或核对其他无关字段。')
        results.append({'request': request, 'result': execute_tool(case, request['tool'], fields=request['fields'],
            code=request.get('code'), source_ids=request.get('source_ids', []), products=request.get('products', []))})
    return results
