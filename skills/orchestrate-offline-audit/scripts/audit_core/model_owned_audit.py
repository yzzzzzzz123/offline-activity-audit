"""Business audit performed by the run's continuing AI, with optional tools."""
from copy import deepcopy
from dataclasses import asdict
import json

from .model_authority import decision, business_guidance


AUDIT_POLICY = (
    '继续由你完成本单业务审核和判断。程序不设置业务门禁，也不替你判定适用、通过、不符或无法确认。'
    '根据本类实际业务规则和本单完整原始资料自主核对，资料盘点有缺项仍继续审核可判断的内容。'
    '计算、日期解析、字段识别、资料匹配、商品查询及复用提示均是辅助依据；你可以采纳、纠正，'
    '或读取原事实、自行推理及使用case_python完成步骤，不要求改到任何程序检查通过。'
    '保留真实问题和真实不确定性，不把内部工具故障、未返回或不会解析写成客户缺件。'
    'POS缺少单价或其他金额字段不影响用已有原始名称和69码独立查商品库；'
    '商品查询必须依据实际查库结果，case_python只能分析本单原文，不能代替数据库访问。'
    '来源未分发或查询未执行属于内部待完成步骤，请补取本单原文并请求pos_product_lookup；'
    '不把“尚未查库”单独列作客户商品资料问题，也不虚构命中或通过。'
    'checks用rule_id、status(pass/fail/unknown/not_applicable)、reason、source_ids保存你的逐项判断；'
    '可以按需要补充comparisons等证据，不要求为计算器固定字段或引用句式反复重写答案。'
    '材料、数字和来源保持原样，准确69码与实际商品身份、赠品和购买品、申请与实际销量分清。'
    '先核对已有工具结果，再决定是否需要补查，已完成结果不重复请求。'
    '按已读取的逐张照片事实核对；同店多图共同判断实际覆盖，确有疑问再按文件名回看原图。'
    '同一单照片复用候选必须逐对核对，候选仅是技术提示，不能直接写成造假或拒付。'
    '陈列照片用photo_reviews分别保存逐图检查，'
    '总体checks仍说明对应规则结论，不借其他门店的画面补事实。'
    '商品及模板参考不是本次上传凭证，不用其数值、印章或商品图证明现场事实。'
    '需要工具时以tool_requests请求，并保留当前checks；收到结果后由你继续决定。'
    '需要回看图片以image_requests给原图文件名，不必重复整包图片。'
    '原规则未要求的字段、附件或额外资料不新增为客户问题。'
    '自行确认本类规则、所有原始行和图片的审核覆盖，不按规则清单长度或程序比较结果自动通过。'
    '理由用普通人易懂的中文，定位实际对象、原值差异及具体限制，同一件事只说一次。'
)


def audit_policy():
    from .pdf_policy import CONTRACT_SIGNATURE_CLARIFICATION, PHOTO_REUSE_CLARIFICATION
    return AUDIT_POLICY + CONTRACT_SIGNATURE_CLARIFICATION + PHOTO_REUSE_CLARIFICATION


def run(provider, case, root):
    if getattr(provider, 'agent_mode', 'single') == 'main_three':
        from .agent_orchestration import run as run_team
        return run_team(provider, case, root)
    return run_single(provider, case, root)


def run_single(provider, case, root):
    from contextlib import nullcontext
    from .audit_tools import assist, dispatch_requests, tool_protocol
    from .cross_claim_photos import model_advice
    from .execution_trace import record_step
    from .model_metrics import save_model_observations
    from .model_judgment import digest
    from .material_sources import material_source_ids
    from .pdf_materials import (_prompt_documents, _prompt_pos_identities, _display_source_prompt,
                               GIFT_PHOTO_AUDIT_CLARIFICATION, ORCHESTRATOR_SKILL, _business_background,
                               prepare_product_references, prepare_pos_identities, template_comparison)
    from .pdf_policy import rules, requirements, SUBMITTED_SOURCE_CHECKS
    from .product_reference_batches import product_identity_support
    from .prompts import bound_prompt
    from .scenario_registry import SKILL_BY_SCENARIO, SCENARIO_LABELS
    from .template_references import template_guide
    from .tool_observations import observations, remember

    scenario = case['scenario']; skill = SKILL_BY_SCENARIO[scenario]
    def helper(operation):
        # Model audits run concurrently; mutable per-run DB/image snapshots
        # and authorized source-maintenance tools are accessed one at a time.
        with getattr(provider, '_agent_tool_lock', nullcontext()):
            return operation()
    def failed(error):
        from .execution_diagnostics import _text
        return {'complete':False,'status':'model_takeover','error':_text(error),
            'instruction':'辅助读取未完成，请结合本单原始资料自主处理，不据此报客户缺件。'}
    agent_id = case.get('_agent_id')
    if agent_id is None:
        case = dict(case)
    references, _ = assist('product_references',
        lambda: helper(lambda: prepare_product_references(case, root, provider._call, include_images=False)),
        fallback=lambda e:(failed(e), [])) if agent_id in {None, 'visual'} else ({}, [])
    identities = assist('pos_product_identity',
        lambda: helper(lambda: prepare_pos_identities(case, root, provider._call)), fallback=failed) if agent_id in {None, 'numbers'} else {}
    if identities:
        remember(case, 'pos_product_identity', fields=[], code=None,
                 source_ids=material_source_ids(case, 'pos_excel'), result=identities)
    case['_product_identity_support'] = product_identity_support(references)
    if case['_product_identity_support']:
        remember(case, 'product_identity', fields=[], code=None, source_ids=[], result=case['_product_identity_support'])
    provider._audit_cases[case['archive_id']] = case
    templates, template_images = assist('template_reference',
        lambda: template_comparison(skill, scenario), fallback=lambda e:(failed(e), [])) if agent_id in {None, 'documents'} else ({}, [])
    specs = case.get('_agent_rule_specs', [asdict(r) for r in rules(scenario)])
    selected = [r['id'] for r in specs]
    check = {'type': 'object', 'required': ['rule_id','status','reason','source_ids'], 'properties': {
        'rule_id': {'type':'string'}, 'status': {'type':'string'}, 'reason': {'type':'string'},
        'source_ids': {'type':'array','items':{'type':'string'}}, 'comparisons': {'type':'array'}}}
    schema = {'type':'object','required':['checks'], 'properties': {
        'checks': {'type':'array','items':check},
        'photo_reviews': {'type':'array','items':{'type':'object','required':['unit_id','checks'],'properties': {
            'unit_id':{'type':'string'},'checks':{'type':'array','items':check}}}},
        'tool_requests': {'type':'array'}, 'image_requests': {'type':'array','items':{'type':'string'}},
        'source_requests': {'type':'array','items':{'type':'string'}}}}
    if agent_id in {'documents', 'numbers'}:
        schema['properties'].pop('photo_reviews')
    request = {'scenario': scenario, 'rules': specs,
        'material_requirements': [asdict(r) for r in requirements(scenario)],
        'materials': case['materials'], 'flags': case['flags'],
        'original_file_formats': [{k: u.get(k) for k in ('unit_id', 'source_file', 'locator', 'detected_format', 'declared_format')}
                                  for u in case.get('units', [])],
        'documents': json.loads(_prompt_documents(case['documents'])),
        'photo_agent_report': case.get('_photo_agent_report'),
        'pos_product_identity': json.loads(_prompt_pos_identities(identities)) if identities else {},
        'product_references': references, 'template_references': templates,
        'tool_observations': observations(case),
        'submitted_source_advice': case.get('_submitted_source_advice', []),
        'submitted_source_rules': SUBMITTED_SOURCE_CHECKS,
        'photo_reuse_advice': model_advice(case.get('_photo_reuse_advice', {}),
                                          case.get('units', []), case.get('documents', []))}
    available = {u['image'].name:u['image'] for u in case.get('units',[]) if u.get('image')}
    # Reading stages already turned each original image into source-bound
    # facts. The final business audit starts from those facts, catalogs and
    # document text; it may request a specific original by image_requests if
    # something still needs a visual check. Never preload the whole case here.
    images = []
    # Designated template pages are reference-only; keep them reachable by
    # filename for an explicit request instead of sending them up front.
    for path in template_images or []:
        if path.is_file() and path.name not in available:
            available[path.name] = path
    request['original_images'] = [{'unit_id':u['unit_id'],'image':u['image'].name}
                                  for u in case.get('units',[]) if u.get('image')]
    policy = audit_policy()
    if agent_id:
        from .agent_orchestration import LABELS, photo_policy
        request['agent_id'] = agent_id
        request['source_catalog'] = case['_agent_source_catalog']
        policy += ('本次角色为'+LABELS[agent_id]+'。只返回本次rules分配的审核项，不能改写其他角色结论。'
            '当前documents是本职完整来源，不是整单全部资料；source_catalog是整单来源目录。'
            '需要其他已有来源时以source_requests列unit_id，程序交付该来源完整原文；'
            '未在当前分工上下文中出现不表示客户缺件，不按片段推断全单缺失。'
            '每项结果保留状态、具体理由与来源，包含通过、不适用和真实无法确认项。'
            '主Agent仅收到这些结果与来源引用，不会替你读取原件或完成遗漏项。')
        if agent_id == 'visual':
            policy += ('你是唯一的图片/照片判断负责人。图片识别、水印、日期、门店、商品、尺寸、'
                       '陈列、复用和篡改等照片结论均由你完成；合同和表格文字仅作判断图片的依据，'
                       '不接管文档合规、原生POS复算或整单金额任务。\n通用照片造假判断：\n'+photo_policy())
        else:
            policy += ('photo_reviews及所有照片内容判断只由照片Agent返回，本角色仅返回分配的checks。'
                       '已有图片facts是照片Agent读取的来源绑定事实，不是让你再次独立判断照片。'
                       '需要看像素时以image_requests委托照片Agent独立读取，你只依据返回事实完成本职文字或金额逻辑。'
                       '外采赠品POS及活动照片的汇总须引用photo_agent_report中的照片有效性和覆盖结论，'
                       '不得根据照片存在、文件名或自己重看照片事实宣称有效；不明确时保留该具体限制。')
    business_policy = business_guidance(scenario)
    if scenario=='self_procured_gift_material':business_policy += GIFT_PHOTO_AUDIT_CLARIFICATION
    business_policy += '\n' + (ORCHESTRATOR_SKILL/'references/customer-language.md').read_text('utf-8')
    business_policy += '\n' + _business_background()
    prompt = bound_prompt('{policy}\n本类业务说明：{guide}\n既有业务补充（实现格式不构成门禁）：{business_policy}'
        '\n指定样张的资料角色及使用范围：{template_guide}\n工具：{tools}\n本单完整资料及已有结果：{request}',
        policy=policy, guide=(skill/'references/business-guide.md').read_text('utf-8')+_display_source_prompt(case),
        business_policy=business_policy,template_guide=template_guide(skill),
        tools=tool_protocol(selected), request=json.dumps(request,ensure_ascii=False,default=str),
        _audit_stage_context={'version':1,'phase':'audit','scenario':scenario,'rule_ids':selected,
            'rules':specs,'policies':{'business_authority':'executing_model',
                'business_policy':business_policy},
            'max_text_bytes':None,'max_output_tokens':None})
    iteration = 0; seen = set()
    while True:
        stage_root = root/f'model-audit-{case["archive_id"]}-{iteration:03d}'
        artifact_id = case['archive_id']+('-'+agent_id if agent_id else '')+'-'+str(iteration)
        save_model_observations('model-owned-audit-input-'+artifact_id, request)
        value = provider._call(stage_root, skill, schema, prompt, images=images,
            label=SCENARIO_LABELS[scenario]+' '+(LABELS[agent_id] if agent_id else '模型自主审核'), validator=lambda v:v,
            original_reinspection=bool(images))
        save_model_observations('model-owned-audit-result-'+artifact_id, value)
        tools = value.get('tool_requests', [])
        source_requests = value.get('source_requests', [])
        added = provider._agent_expand_sources(case, source_requests) if source_requests and agent_id else []
        if tools and agent_id:
            from .agent_orchestration import expand_tool_sources
            added = [*added, *expand_tool_sources(provider, case, tools)]
        if added:
            request['documents'] = json.loads(_prompt_documents(case['documents']))
            available.update({u['image'].name:u['image'] for u in case.get('units', []) if u.get('image')})
            request['original_images'] = [{'unit_id':u['unit_id'],'image':u['image'].name}
                                         for u in case.get('units', []) if u.get('image')]
        wanted = [name for name in value.get('image_requests',[]) if name in available]
        if not tools and not wanted and not added:
            break
        signature = digest({'requests':tools,'images':wanted,'sources':added,'checks':value.get('checks',[])})
        if signature in seen:
            record_step('audit.tool_requests_reused','已有工具与图片结果留给同一模型自查',
                {'archive_id':case['archive_id'],'requests':tools,'images':wanted,'business_conclusion':None})
            break
        seen.add(signature)
        results = helper(lambda: dispatch_requests(case, tools, selected)) if tools else []
        request['tools'] = [*request.get('tools',[]), *results]
        request['tool_observations'] = observations(case)
        request['current_model_result'] = deepcopy(value)
        request['provided_images'] = wanted
        images = [available[name] for name in wanted]
        prompt = prompt.partial(request=json.dumps(request,ensure_ascii=False,default=str))
        record_step('audit.tool_result','执行模型选择工具后继续本单审核',{'results':results,'images':wanted})
        iteration += 1
    value.pop('tool_requests',None);value.pop('image_requests',None);value.pop('source_requests',None)
    return provider._review_stage_summary(case,value,decision(scenario,value,case['documents']),
        root/f'model-audit-self-check-{case["archive_id"]}',selected,
        lambda v,ids:decision(scenario,v,case['documents']),images=list(available.values()))
