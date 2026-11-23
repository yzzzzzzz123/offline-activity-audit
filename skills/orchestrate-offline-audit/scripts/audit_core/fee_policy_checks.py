"""Source constraints for the workbook's independent application/claim values."""
from __future__ import annotations
from .common import AuditError
from calendar import monthrange
from datetime import date
from decimal import Decimal
import re

def _object(properties):
    return {'type':'object','additionalProperties':False,'properties':properties,'required':list(properties)}
_text={'type':'string','minLength':1}
DATE_SOURCE=_object({'unit_id':_text,'quote':_text,'date':_text})
PAYMENT_DATES_SCHEMA=_object({'start':DATE_SOURCE,'end':DATE_SOURCE,'payments':{'type':'array','items':DATE_SOURCE},'complete':{'type':'boolean'}})
def target_tiers_schema(operand):
    return _object({'sales':operand,'reward':operand,'tiers':{'type':'array','minItems':1,'items':_object({'threshold':operand,'rate':operand,'rate_unit':{'type':'string','enum':['percent','ratio']}})},'complete':{'type':'boolean'}})

WORKBOOK_EVIDENCE_PROMPT=(
    '\n新表补充证据：人员激励payment_dates逐笔列结算单活动起止日期和本次付款原文，每项unit_id/quote/date(ISO)，complete声明是否覆盖所有实际付款；'
    '结算周期与本次真实申请周期一致时也可引用申请起止日期，不从文件名猜日期。临促工资、销售提成及培训均核对，3个月按自然月闭区间计算，不按90天。'
    'POS达标target_tiers列POS实际销售总金额sales、结算奖励金额reward、全部真实奖励档tiers及complete。'
    '每档threshold和rate均用原文数值operand，rate_unit区分percent(原文10%)与ratio(原文0.1)，不要改写原数。'
    '程序按实际销售额选择可达的最高门槛，全额乘对应比例，不能把目标额当实际销售额，也不能漏掉更高或更低档来迎合申报；依据不足为null/complete=false。'
)

def add_months(value,months):
    index=value.year*12+value.month-1+months
    year,month=divmod(index,12);month+=1
    return date(year,month,min(value.day,monthrange(year,month)[1]))

def _quoted_date(item,allowed,documents):
    uid=item['unit_id'];text=item['quote']
    if uid not in allowed or not any(text in f for f in documents.get(uid,{}).get('facts',[])):
        raise AuditError('付款日期须逐字引用本次正确角色的来源，不能借其他文件或拼写日期')
    raw=re.findall(r'(?<!\d)(\d{4})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})\s*日?(?!\d)',text)
    try:values=[date(*map(int,v)) for v in raw];selected=date.fromisoformat(item['date'])
    except ValueError:raise AuditError('付款日期证据含无效日期') from None
    if selected not in values or len(set(values))!=1:
        raise AuditError('每项付款起止日期须有唯一原文日期，不猜年份或从多个日期中挑选')
    return selected

def payment_date_check(evidence,documents,materials,units=None,original=None):
    unknown={'status':'unknown','reason':'暂时不能确认本次活动结算周期与全部付款日期，无法核对活动结束后3个月期限。','source_ids':[],'comparisons':[]}
    if not evidence or not evidence.get('complete'):
        if original and original['status']=='unknown':
            return {**original, 'comparisons':[]}
        return unknown
    refs={d['unit_id']:d for d in documents};settlement=material_sources(materials,'settlement');app=application_sources(documents,materials,units)|settlement;payment=material_sources(materials,'payment')
    if (evidence['start']['unit_id'] in settlement) != (evidence['end']['unit_id'] in settlement):
        raise AuditError('付款活动起止日期须引用同一资料角色，不能混用结算单与系统申请拼接周期')
    start=_quoted_date(evidence['start'],app,refs);end=_quoted_date(evidence['end'],app,refs)
    if end<start:raise AuditError('活动结束日期不能早于开始日期')
    if not evidence['payments'] or {p['unit_id'] for p in evidence['payments']}!=payment:return unknown
    latest=add_months(end,3);bad=[]
    for p in evidence['payments']:
        day=_quoted_date(p,payment,refs)
        if not start<=day<=latest:bad.append(f'{day.isoformat()}不在{start.isoformat()}至{latest.isoformat()}内')
    return {'status':'fail' if bad else 'pass','reason':'；'.join(bad) if bad else f'全部本次付款日期位于活动开始日至结束后3个自然月（含{latest.isoformat()}）内。',
        'source_ids':list(dict.fromkeys([evidence['start']['unit_id'],evidence['end']['unit_id'],*[p['unit_id'] for p in evidence['payments']]])),'comparisons':[]}

def target_tier_comparison(evidence,documents,materials,units=None):
    if not evidence or not evidence.get('complete'):return None
    from .pdf_evidence import _operand
    refs={d['unit_id']:d for d in documents};excel=material_sources(materials,'pos_excel');claim=material_sources(materials,'settlement')
    standard=application_sources(documents,materials,units)|material_sources(materials,'promotion_contract')
    if evidence['sales']['unit_id'] not in excel or evidence['reward']['unit_id'] not in claim:
        raise AuditError('达标阶梯只能读取POS Excel实际销售总金额和结算奖励金额')
    sales=_operand(evidence['sales'],refs);_operand(evidence['reward'],refs)
    tiers={}
    for tier in evidence['tiers']:
        if any(tier[k]['unit_id'] not in standard for k in ('threshold','rate')):raise AuditError('达标门槛和比例须引用本次真实奖励规则，不能引用结算自填值')
        threshold=_operand(tier['threshold'],refs);raw_rate=_operand(tier['rate'],refs)
        explicit_percent=str(tier['rate']['number']).strip().endswith(('%','％'))
        if tier['rate_unit']=='ratio' and explicit_percent:raise AuditError('比例单位与原文百分数冲突')
        rate=raw_rate/100 if tier['rate_unit']=='percent' and not explicit_percent else raw_rate
        if threshold<0 or rate<0 or rate>1 or threshold in tiers:raise AuditError('达标门槛须非负且不重复，比例须在0至100%之间')
        tiers[threshold]=(rate,tier)
    eligible=[t for t in tiers if sales>=t]
    if not eligible:
        return {'status':'fail','reason':f'POS实际销售金额{sales}未达到本次最低奖励门槛。','comparisons':[],
                'source_ids':list(dict.fromkeys([evidence['sales']['unit_id'],*[v['threshold']['unit_id'] for _,v in tiers.values()]]))}
    rate,tier=tiers[max(eligible)]
    # The percentage operand is still the original number; append an exact 100
    # denominator through a deterministic calculation rather than a fake source.
    expected=sales*rate;actual=_operand(evidence['reward'],refs)
    return {'status':'pass' if actual<=expected else 'fail','reason':f'POS实际销售金额{sales}，达到{max(eligible)}档，比例{rate*100}%，可支持奖励上限{expected}，结算奖励{actual}。',
        'source_ids':list(dict.fromkeys([evidence['sales']['unit_id'],evidence['reward']['unit_id'],*[t[k]['unit_id'] for t in evidence['tiers'] for k in ('threshold','rate')]])),
        'comparisons':[], 'tier_calculation':{'sales':str(sales),'threshold':str(max(eligible)),'rate':str(rate),'expected':str(expected),'actual':str(actual)}}


def material_sources(materials, *ids):
    return {uid for m in materials or [] if m['id'] in ids and m['state']=='present' for uid in m['source_ids']}


def application_sources(documents, materials=None, units=None):
    """Program-linked application rows or a distinct submitted application original.

    A verification row, contract, settlement, POS or payment cannot certify itself
    as the approved application. Never infer this role from a ZIP/file name.
    """
    blocked=material_sources(materials,'settlement','promotion_contract','pos','pos_excel','payment','invoice')
    linked={u['unit_id'] for u in units or [] if u.get('source_kind')=='qince' and u.get('source_file') in {'勤策申请明细','勤策申请主档','勤策进场关联明细'}}
    originals=material_sources(materials,'activity_application')-blocked
    return (linked|originals)&{d['unit_id'] for d in documents}


def approval_sources(documents,materials=None,units=None):
    blocked=material_sources(materials,'settlement','promotion_contract','pos','pos_excel','payment','invoice')
    linked={u['unit_id'] for u in units or [] if u.get('source_kind')=='qince' and u.get('source_file')=='勤策核销批复'}
    return (linked|(material_sources(materials,'approved_claim')-blocked))&{d['unit_id'] for d in documents}


def constrain_application_check(scenario, rule, check, documents, materials, units=None):
    from .pdf_policy import APPLICATION_RULES, applicability
    if (scenario,rule.id) not in APPLICATION_RULES or check['status']=='not_applicable':return check
    from .qince_approval_state import constrain_approval_state
    from .qince_amount_basis import OPTIONAL_REFERENCE_RULES
    from .audit_rag import reference_comparison
    check = reference_comparison(check, documents, materials, units)
    if rule.id not in OPTIONAL_REFERENCE_RULES:
        # Photo/payment facts remain independently auditable. Missing a Qince
        # citation must not overwrite a real watermark/date/material finding.
        return check
    workflow = constrain_approval_state(check, documents, units)
    if workflow is not None:
        return workflow
    if rule.id=='invoice_details' and any(m['id']=='invoice' and m['state']=='missing' for m in materials or []):
        return check  # A definite missing required receipt remains a material issue.
    app=(approval_sources if rule.id=='claim_ceiling' else application_sources)(documents,materials,units)
    if not app.intersection(check['source_ids']):
        reference='系统核销批复金额合计' if rule.id=='claim_ceiling' else '系统活动申请字段'
        if app:
            return {**check,'status':'unknown',
                    'reason':f'本单已取得独立的{reference}来源，但本检查未引用对应记录，暂时不能确认该项核对结果。',
                    **({'comparisons':[]} if 'comparisons' in check else {})}
        return {**check,'status':'unknown','reason':f'暂时不能核对{rule.text.split("；")[0]}：未取得可确认对应本单的{reference}，不能用合同或结算单自填值代替。',
                **({'comparisons':[]} if 'comparisons' in check else {})}
    return check


def _equal_gift_ratio_parameters(rule_id, comparison, terms):
    """A quoted n:n supplies two distinct positions even when their values match.

    This only exempts the two ratio parameters from duplicate-count protection.
    The normal operand and contract-role checks still prove every source/value.
    """
    if (rule_id != 'gift_rule_quantity' or comparison['operation'] not in {'ratio', 'floor_ratio'}
            or len(terms) != 3 or terms[0] == terms[1] or terms[1] != terms[2]):
        return False
    from .pdf_evidence import _number
    value = _number(terms[1][2])
    return value > 0 and any(
        Decimal(buy) == value == Decimal(gift)
        for buy, gift in re.findall(r'(?<![\d.+-])(\d+(?:\.\d+)?)\s*[:：]\s*(\d+(?:\.\d+)?)(?![\d.])', terms[1][1]))


def separate_settlement_product_cells(rule_id, comparison, documents):
    """Prove equal values occupy independent quantity/price/amount cells.

    Only one complete original visual row and its nearest preceding original
    header qualify. This never legitimizes a value/sum self-reference, a
    partial quote, a different material, or an invented number.
    """
    if (rule_id != 'settlement_calculation' or comparison['operation'] != 'product'
            or comparison['value_kind'] != 'amount' or len(comparison['right']) != 2):
        return False
    left, first, second = comparison['left'], *comparison['right']
    if any(v['unit_id'] != left['unit_id'] or v['quote'] != left['quote'] for v in (first, second)):
        return False
    facts = next((d['facts'] for d in documents if d['unit_id'] == left['unit_id']), [])
    if left['quote'] not in facts:
        return False
    index = facts.index(left['quote'])
    cells = re.split(r'[|｜\t]', left['quote'])
    header = next((re.split(r'[|｜\t]', fact) for fact in reversed(facts[:index])
                   if '表头' in fact and re.search(r'[|｜\t]', fact)), [])
    if len(cells) < 4 or len(header) != len(cells):
        return False
    if any(re.search(r'[%％]', cell) for cell in cells[-3:]):
        return False
    patterns = (r'(?:销售|结算|申报|搭赠|赠品|补差|奖励)?(?:数量|销量)',
                r'(?:销售|结算|补差|奖励|搭赠|赠品)?单价',
                r'(?:销售|结算|补差|奖励|搭赠|赠品)?金额')
    if not all(re.fullmatch(pattern, cell.strip()) for pattern, cell in zip(patterns, header[-3:])):
        return False
    from .pdf_evidence import _number
    try:
        return [_number(cell) for cell in cells[-3:]] == [_number(v['number']) for v in (first, second, left)]
    except AuditError:
        return False


def validate_workbook_comparison(scenario,rule_id,comparison,documents,materials,units=None,*,comparison_index=0):
    from .pdf_policy import APPLICATION_COMPARISON_RULES
    settlement=material_sources(materials,'settlement')
    contract=material_sources(materials,'promotion_contract')
    application=application_sources(documents,materials,units)
    left=comparison['left']['unit_id'];right={v['unit_id'] for v in comparison['right']}
    if rule_id == 'settlement_calculation':
        from .common import CheckEvidenceError
        for position, operand in [('left', comparison['left']),
                                  *((f'right/{i}', value) for i,value in enumerate(comparison['right']))]:
            if operand['unit_id'] not in settlement:
                raise CheckEvidenceError('本项费用复算只使用本次结算单实际计费来源，不能混入合同计划、申请预算或POS；'
                                         '先核实金额对象及来源，不通过改运算或方向代替来源。',
                                         rule_id,'comparison_source',f'comparisons/{comparison_index}/{position}/unit_id')
    if (scenario,rule_id) in APPLICATION_COMPARISON_RULES:
        from .qince_amount_basis import CONTRACT_STANDARD_RULES
        if rule_id in CONTRACT_STANDARD_RULES:
            from .audit_rag import reference_fields
            field = 'application_unit_price' if comparison['value_kind'] == 'unit_price' else 'application_quantity'
            info = reference_fields(documents, units)[field]
            def nonempty(value):
                raw = value['value']
                if isinstance(raw, dict):
                    raw = raw.get('ext_value')
                return raw is not None and not isinstance(raw, bool) and str(raw).strip() != ''
            supplied = {uid for value in info['values'] if nonempty(value)
                        for uid in value['source_ids']}
            # Effective Qince values are authoritative. A contract can only
            # supply the original standard when that particular field is empty.
            # Keep distinct values/objects; source binding does not match SKUs.
            application = supplied if supplied else application | contract
        if rule_id=='claim_ceiling':application=approval_sources(documents,materials,units)
        complete_linked_sum = False
        if rule_id in {'contract_budget_amount','application_claim_ceiling'}:
            from .qince_amount_basis import complete_application_sum
            if complete_application_sum(comparison, documents, units) is not None:
                complete_linked_sum = True
            elif comparison['operation'] == 'sum':
                raise AuditError('本次申请金额合计必须完整覆盖本单精确关联申请行，不能用部分行、重复行或其他申请代替。')
            else:
                row_ids={u['unit_id'] for u in units or [] if u.get('source_kind')=='qince' and u.get('source_file')=='勤策申请明细'}
                application-=row_ids
        if rule_id=='staff_total_unit_price':
            if left not in application or not right.issubset(settlement) or comparison['operation']!='product':
                raise AuditError('临促申请单价须独立引用系统申请，右侧为结算单日工资×天数，不用同一原值自证')
        elif not right or not right.issubset(application) or left not in (contract if rule_id in {'contract_budget_amount','contract_reward_unit_price'} else settlement):
            raise AuditError('新表申请/批复比较须引用本单独立系统申请；左侧合同预算或结算申报，右侧申请/批复，不能互换或用合同代替申请')
        if rule_id in {'contract_budget_amount','claim_ceiling','application_claim_ceiling'} and not complete_linked_sum and (comparison['operation']!='value' or len(comparison['right'])!=1):
            raise AuditError('系统总金额或批复合计须引用一次原始总额，不能按申请行或SKU重复求和')
    terms=[(v['unit_id'],v['quote'],v['number']) for v in comparison['right']]
    if (rule_id!='pos_arithmetic' and len(terms)!=len(set(terms))
            and not _equal_gift_ratio_parameters(rule_id, comparison, terms)
            and not separate_settlement_product_cells(rule_id, comparison, documents)):
        raise AuditError(f'数值合计不能重复引用同一个原始数值；要点{rule_id}的right含完全相同的来源引用。'
                         '不同销售行数量相同仍是不同来源，quote须从原facts逐字引用并保留工作表和行定位，'
                         '不能只截取重复的列号与数值；不重复计数，也不改动原facts。')
    if rule_id=='invoice_amount':
        if left not in settlement or not right.issubset(material_sources(materials,'invoice')):
            raise AuditError('票据金额比较左侧须为结算实际核销金额，右侧须为本次有效票据，不能拿预算或收款记录补值')
    if rule_id=='material_list_amount':
        if left not in settlement or not right.issubset(material_sources(materials,'material_list')):
            raise AuditError('物料清单金额左侧须为结算实际核销金额，右侧须为本次制作清单电子版')
    if rule_id=='pos_sales_amount':
        if left not in settlement or not right.issubset(material_sources(materials,'pos_excel')):
            raise AuditError('POS达标销售金额须左侧结算POS销售金额、右侧本次POS Excel全量实际金额')
    if rule_id=='target_reward':
        excel=material_sources(materials,'pos_excel')
        if left not in settlement or not right.intersection(excel) or not right.intersection(contract|application):
            raise AuditError('达标奖励须使用POS Excel实际销售金额及本次真实奖励规则，不能用目标金额或结算单自填比例自证')
