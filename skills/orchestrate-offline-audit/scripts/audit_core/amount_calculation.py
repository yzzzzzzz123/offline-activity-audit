"""Optional exact arithmetic for the current amount worker's source materials."""
from decimal import Decimal, Inexact, ROUND_HALF_UP, localcontext
import re
from time import monotonic

from .common import AuditError
from .material_sources import material_source_ids
from .pdf_evidence import _number, calculate, document_map
from .pos_arithmetic import prepare_pos_arithmetic


_NUM = r'[+-]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?'
_SALES = re.compile(
    rf'(?:^|[：:；;|，,])\s*[【\[]?POS\s*销售(?:总金额|金额|额)[】\]]?'
    rf'\s*(?:[（(]元[）)])?\s*[:：=]?\s*[¥￥]?\s*({_NUM})'
    r'\s*(?:元|[（(]元[）)])?\s*(?=$|[；;|。])', re.I)
_PRODUCT = re.compile(
    rf'(?<![\d.*/×÷%％+\-])({_NUM}[%％]?)\s*(?:元|支|份|天|人)?'
    rf'(?:[（(][^()（）*×=＝]{{1,30}}(?:价|含税|不含税)[）)])?\s*[×*]\s*'
    rf'({_NUM}[%％]?)\s*(?:元|支|份|天|人)?\s*[=＝]\s*'
    rf'({_NUM})(?![\d.%％*/×÷+\-万亿千美eE]|[,，]\d)')
_OTHER_CURRENCY = re.compile(
    r'(?:金额|销售额|币种|金额单位|单位)\s*[:：（(=]?\s*(?:万元|千元|亿元|美元|港元|欧元|USD|HKD|EUR|\$)', re.I)
_GIFT_ROW = re.compile(
    rf'(?:^|[|｜])\s*搭赠数量\s*[:：]?\s*({_NUM})\s*(?:支|件|份)?\s*[|｜]\s*'
    rf'搭赠单价\s*[:：]?\s*({_NUM})\s*(?:元)?\s*[|｜]\s*搭赠金额\s*[:：]?\s*({_NUM})\s*(?:元)?\s*$')
_REWARD = re.compile(rf'(?:表格行\d*[:：]\s*)?奖励标准\s*[:：]\s*({_NUM})\s*[*×]\s*({_NUM}[%％]?)\s*$')
_STAFF = {
    'people': re.compile(r'(?:表格行\d*[:：]\s*)?(?:临促)?人数\s*[:：]?\s*(\d+)\s*[（(]?(?:人|名)[）)]?\s*$'),
    'days': re.compile(r'(?:表格行\d*[:：]\s*)?(?:执行)?天数\s*[:：]?\s*(\d+)\s*[（(]?天[）)]?\s*$'),
    'daily_wage': re.compile(rf'(?:表格行\d*[:：]\s*)?(?:单日工资|日工资)\s*[:：]?\s*({_NUM})\s*[（(]?元[）)]?\s*/\s*天\s*$'),
}


def _document_amounts(doc, role, scenario, check_time):
    readings, staff = [], {}
    for index, fact in enumerate(doc['facts']):
        check_time()
        source = {'role': role, 'source_id': doc['unit_id'], 'fact_index': index}
        for m in _PRODUCT.finditer(fact):
            prefix, suffix = fact[:m.start()].rstrip(), fact[m.end():].lstrip()
            if ((prefix and (prefix[-1] in '*/×÷+-.0123456789%％' or
                             (prefix[-1].isascii() and prefix[-1].isalpha()))) or
                    (suffix and suffix[0] in '*/×÷+-万亿千%％')):
                continue
            a, b, stated = map(_number, m.groups())
            readings.append({**source, 'kind': 'printed_product', 'quote': m[0], 'calculated': str(a * b),
                             'stated': str(stated), 'stated_minus_calculated': str(stated - a * b)})
        if role != 'settlement':
            continue
        gift = _GIFT_ROW.search(fact)
        if gift:
            quantity, price, stated = map(_number, gift.groups())
            readings.append({**source, 'kind': 'gift_line_amount', 'quote': gift[0], 'stated': str(stated),
                             'calculated': str(quantity * price), 'stated_minus_calculated': str(stated - quantity * price)})
        reward = _REWARD.fullmatch(fact.strip())
        if reward:
            sales, rate = map(_number, reward.groups())
            if 0 <= rate <= 1:
                theoretical = sales * rate
                readings.append({**source, 'kind': 'printed_reward_basis', 'quote': reward[0],
                                 'sales': str(sales), 'rate': str(rate), 'unrounded_reward': str(theoretical),
                                 'nearest_whole_yuan': str(theoretical.to_integral_value(rounding=ROUND_HALF_UP))})
        if scenario == 'personnel_incentive':
            for key, pattern in _STAFF.items():
                match = pattern.fullmatch(fact.strip())
                if match:
                    staff.setdefault(key, []).append({'fact_index': index, 'quote': fact, 'number': match[1]})
    if set(staff) == set(_STAFF) and all(len(hits) == 1 for hits in staff.values()):
        operands = {key: hits[0] for key, hits in staff.items()}
        pay = _number(operands['people']['number']) * _number(operands['days']['number']) * _number(operands['daily_wage']['number'])
        readings.append({'kind': 'staff_wage', 'role': role, 'source_id': doc['unit_id'],
                         'operands': operands, 'wages': str(pay)})
    return readings


def _native_sales_difference(case, documents, check_time):
    excel = material_source_ids(case, 'pos_excel', states=('present',))
    settlement = material_source_ids(case, 'settlement', states=('present',))
    if not excel or not settlement or set(excel).intersection(settlement):
        return None
    source_files = {u['unit_id']: u.get('file_id') or u['unit_id'] for u in case.get('units', [])}
    if len({source_files.get(uid, uid) for uid in settlement}) != 1:
        return None
    # An unlabeled reimbursement/reward figure must never stand in for POS sales.
    values = []
    for uid in [*excel, *settlement]:
        for fact in documents[uid]['facts']:
            check_time()
            if _OTHER_CURRENCY.search(fact):
                return None
            if uid not in settlement:
                continue
            for found in _SALES.finditer(fact):
                values.append((uid, found.group().strip(), _number(found[1])))
    if not values or len({number for _, _, number in values}) != 1:
        return None
    plan = prepare_pos_arithmetic(case['documents'],
        [{'id': 'pos_excel', 'state': 'present', 'source_ids': excel}], case.get('units'),
        amount_only=True, check_progress=check_time)
    if not plan or not plan['complete'] or set(plan['source_ids']) != set(excel):
        return None
    totals = [c for c in plan['comparisons'] if c['operation'] == 'sum' and c['value_kind'] == 'amount']
    # Two separate sheets/totals may overlap; their scope belongs to the agent.
    if len(totals) != 1:
        return None
    total = totals[0]
    actual = sum((_number(term['number']) for term in total['right']), Decimal(0))
    uid, quote, claimed = values[0]
    return {'kind': 'pos_sales_difference', 'unit': '元',
            'pos_detail_total': str(actual), 'pos_stated_total': total['left']['number'],
            'settlement_pos_sales': str(claimed), 'settlement_minus_pos': str(claimed - actual),
            'row_count': len(total['right']), 'pos_source_ids': plan['source_ids'],
            'pos_locator': total['label'], 'settlement_source_id': uid, 'settlement_quote': quote}


def _check_quote_boundary(operand, documents, check_time):
    quote = operand['quote']
    if not isinstance(quote, str) or not quote or len(quote) > 32_768:
        raise AuditError('请引用一项完整原始金额')
    # A substring such as "单价12" in "单价120" is not evidence for 12.
    left = r'(?<![\d.,，+\-])' if quote[0].isdigit() else ''
    right = r'(?![\d.%％]|[,，]\d)' if quote[-1].isdigit() else ''
    pattern = re.compile(left + re.escape(quote) + right)
    for fact in documents[operand['unit_id']]['facts']:
        check_time()
        if pattern.search(fact):
            return
    raise AuditError('金额引用不完整，请结合对应原件自行核对')


def source_amounts(case, comparisons=None):
    """Return source-bound observations only. Unsupported layouts retain their raw facts."""
    documents = document_map(case['documents'])
    deadline = monotonic() + 3.0

    def check_time():
        if monotonic() > deadline:
            raise TimeoutError('金额辅助复算超时，请按本单原件继续计算')

    # Limit only the optimization. The agent still receives every original fact.
    characters = 0
    if comparisons is None:
        relevant = {uid for role in ('pos_excel', 'settlement', 'promotion_contract')
                    for uid in material_source_ids(case, role, states=('present',))}
        documents = {uid: doc for uid, doc in documents.items() if uid in relevant}
    for doc in documents.values():
        for fact in doc['facts']:
            check_time()
            size = len(fact)
            characters += size
            if size > 32_768 or characters > 2_000_000:
                raise ValueError('本次原文超出快速复算范围，请按原件分项核对')
    facts = []
    with localcontext() as ctx:
        ctx.prec = 128
        ctx.traps[Inexact] = True
        if comparisons is not None:
            if not isinstance(comparisons, list) or not comparisons:
                raise AuditError('需要本单原文中的金额及其来源')
            for c in comparisons:
                check_time()
                if (not isinstance(c, dict) or c.get('value_kind') != 'amount' or
                        c.get('operation') not in ('sum', 'product', 'value') or
                        c.get('operator') not in ('eq', 'ge', 'le') or
                        not isinstance(c.get('right'), list) or not 1 <= len(c['right']) <= 256):
                    raise AuditError('此算式超出金额辅助工具支持范围，请依据原件自行计算')
                for operand in [c['left'], *c['right']]:
                    _check_quote_boundary(operand, documents, check_time)
                result = calculate(c, documents)
                facts.append({'kind': 'quoted_comparison', 'operation': c['operation'],
                              'left': c['left'], 'right': c['right'], 'left_value': result['left_value'],
                              'calculated': result['right_value'],
                              'left_minus_calculated': str(Decimal(result['left_value']) - Decimal(result['right_value']))})
        else:
            difference = _native_sales_difference(case, documents, check_time)
            check_time()
            if difference is not None:
                facts.append(difference)
            for role in ('promotion_contract', 'settlement'):
                for uid in material_source_ids(case, role, states=('present',)):
                    check_time()
                    facts.extend(_document_amounts(documents[uid], role, case.get('scenario'), check_time))
    check_time()
    return {'tool': 'amount_calculation', 'calculations': facts, 'model_calls': 0,
            'status': 'observed' if facts else 'model_takeover',
            'instruction': '仅所列来源和算式已复算。请核对字段用途；其他算式或原件布局请直接依原文自行计算并继续。'}
