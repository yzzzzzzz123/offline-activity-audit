"""Shared meanings of settlement row arithmetic and the overall claim ceiling.

No case identities or model-dependent retries. Evidence is never rewritten.
"""
import re

from .common import AuditError, CheckEvidenceError


CLAIM_AMOUNT_LABEL = (r'(?:(?:实际|本次|本期|申报)?核销(?:总)?金额|实际结算金额|本次结算金额|'
                      r'实报金额|(?:本次)?申请(?:进行)?核销)(?:合计|总计)?')
CLAIM_VALUE_SEPARATOR = r'[\s_＿]*(?:[:：=]|为|是)?[\s_＿]*[¥￥]?[\s_＿]*'
TOTAL_LABEL = r'合计|总计|总额|本次产生费用'
ORIGINAL_NUMBER = r'[-+]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?'
AMOUNT_FIELD = r'(?<!核销)(?:补差|采购|制作|销售|奖励|本行)?金额\s*[:：]?\s*[¥￥]?\s*('
AMOUNT_FIELD += ORIGINAL_NUMBER + r')(?![\d.,，])'
SETTLEMENT_OPERATORS = {
    'line_amount': {'operation': 'product', 'operator': 'eq',
                    'meaning': '本行列示金额等于本行数量与对应单价等真实计费因子的乘积'},
    'declared_total': {'operation': 'sum', 'operator': 'eq',
                       'meaning': '表内原始合计金额等于全部对应明细金额之和'},
    'claim_ceiling': {'operations': ['sum', 'value', 'product'], 'operator': 'le',
                      'meaning': '实际核销金额不得超过完整对应明细或真实计费因子复算的支撑金额'},
    'claim_ceiling_reverse': {'operation': 'value', 'operator': 'ge',
                              'meaning': '仅真实左侧支撑金额、右侧实际核销金额时接受等价反向表达，保留原算式'},
}
SETTLEMENT_SCOPE = (
    '结算自身复算的所有操作数只取本次结算来源，合同计划、申请预算和POS由各自要点核对。'
    '先按原字段确认金额对象：同一笔费用的行金额或唯一合计按真实因子product/eq，'
    '多行表内合计按完整明细sum/eq；只有另列的真实核销金额才增加上限比较。'
    '单笔费用的因子可分行列示，但不能跨费用拼数；不将同一合计换名为另一核销金额，'
    '不为凑比较数量重复生成算式。真实独立核销金额仍须核对，真实金额差异仍须报告。'
)
_PARTIAL = r'未完整|截断|缺行|(?:部分|局部).*(?:未读|无法|看不清)'
_ROW = r'(?:第\s*\d+\s*行|!R\d+|表格行\d+)'
_FIELD_PREFIX = r'''^\s*(?:(?:明细表|表格|表内|明细)(?:行)?\s*)?[“"‘']?'''
_FIELD_SEPARATOR = r'''[”"’']?[\s_＿]*(?:[:：=]|为|是)[\s_＿]*'''
_FIELD_ROW = re.compile(r'^\s*(?:明细表|表格|表内|明细)(?:第\s*(\d+)\s*行|行(?:\s*(\d+))?)\s*[:：]\s*(.+)$')
_NUMERIC_FIELD_LABEL = r'''[^:：=“”"‘’']{0,12}(?:金额|费用|工资|单价|合计|总计|人数|数量|天数)'''
_FACTOR_PROFILES = (
    (r'(?:临促)?人数|人员数量|临促数量', r'(?:执行|工作)?天数', r'单日工资|日工资|每日工资|日薪'),
    (r'(?:采购|制作|销售|结算|堆头|陈列|奖励)?数量', r'(?:采购|制作|销售|结算|奖励)?单价'),
)


def _field_text(fact):
    row = _FIELD_ROW.fullmatch(fact)
    return row[3] if row else fact


def _original_fields(document, labels):
    """Read standalone, explicitly labelled fields; never infer from a model label."""
    fields = []
    pattern = re.compile(_FIELD_PREFIX + r'(?:' + labels + r')\s*' + _FIELD_SEPARATOR + r'[“"‘\']?[\s_＿]*[¥￥]?[\s_＿]*('
                         + ORIGINAL_NUMBER + r')(?![\d.,，])(.*)$')
    for fact in document['facts']:
        match = pattern.fullmatch(_field_text(fact))
        if match and re.fullmatch(r'[\s_＿元人天日件箱套个瓶袋店堆头列组纵位平方米平米（）()/／。．.“”"‘’\']*', match[2]):
            fields.append({'unit_id': document['unit_id'], 'quote': fact, 'number': match[1]})
    return fields


def _unit_priced_fields(document):
    """A labelled amount is a rate only when its original unit says per item."""
    pattern=re.compile(_FIELD_PREFIX + r'''[^:：=\d“”"‘’']{0,12}(?:金额|费用)\s*''' + _FIELD_SEPARATOR +
                       r'[“"‘\']?[\s_＿]*[¥￥]?[\s_＿]*('+ORIGINAL_NUMBER+r')[\s_＿]*元\s*[/／每]\s*'
                       r'[\u4e00-\u9fff]{1,8}\s*[”"’\']?[。．.]?\s*$')
    return [{'unit_id':document['unit_id'],'quote':fact,'number':match[1]}
            for fact in document['facts'] if (match:=pattern.fullmatch(_field_text(fact)))]


def _vertical_field_rows(document):
    """Explicit field rows may describe one fee, never separate fee lines."""
    indices = []
    for fact in document['facts']:
        row = _FIELD_ROW.fullmatch(fact)
        if row is None:
            if re.search(_ROW, fact):
                return False
            continue
        field = {'unit_id': document['unit_id'], 'facts': [fact]}
        if not (_original_fields(field, _NUMERIC_FIELD_LABEL) or _unit_priced_fields(field)
                or re.fullmatch(r'''活动(?:时间|日期|周期)\s*[:：]\s*[0-9０-９年月日至到\-—－/.\s_＿（）()“”"‘’]+[。．.]?''', row[3])):
            return False
        index = row[1] or row[2]
        if index is not None:
            indices.append(int(index))
    return len(indices) == len(set(indices))


def _explicit_claim_fields(document):
    pattern=re.compile(r'(?:'+CLAIM_AMOUNT_LABEL+r')'+CLAIM_VALUE_SEPARATOR+r'('
                       +ORIGINAL_NUMBER+r')(?![\d.,，])')
    fields=[]
    for fact in document['facts']:
        matches=list(pattern.finditer(fact))
        if len(matches)==1:
            fields.append({'unit_id':document['unit_id'],'quote':fact,'number':matches[0][1]})
        elif matches:
            return None
    return fields


def _repeats_staff_factors(fact, factors, numeric_field):
    """A complete prose restatement is not an additional expense field.

    Require all three original roles and units, not just an equal wage number.
    Explicit other/multiple expenses or an additional amount remain ambiguous.
    """
    if (len(factors) != 3 or re.search(r'另|其他|第二|第三|分别|多笔', fact)
            or len(re.findall(numeric_field, fact)) != 1):
        return False
    patterns = [r'(?:人数|人员)\s*(?:[:：=]|为|是)?\s*{n}\s*[（(]?\s*(?:名|人)',
                r'(?:聘请|执行|工作|持续|天数)\s*(?:[:：=]|为|是)?\s*{n}\s*天',
                r'(?:单日|每日|日)?工资\s*(?:[:：=]|为|是)\s*{n}\s*[（(]?\s*元\s*[）)]?\s*[/／]\s*(?:天|日)']
    return all(len(re.findall(pattern.format(n=re.escape(field['number'])), fact)) == 1
               for pattern, field in zip(patterns, factors))


def single_settlement_expense(documents):
    """Prove one vertical expense, with no row/table or amount ambiguity.

    This is a layout compatibility proof, not a fallback for missing evidence.
    Unknown layouts retain the ordinary same-row and full-detail safeguards.
    """
    from .runtime_evidence_repair import basis_for
    repaired = basis_for(documents)
    if repaired is not None:
        return repaired
    if len(documents) != 1:
        return None
    document = documents[0]
    if (any(re.search(_PARTIAL, text) for text in document.get('limitations', []))
            or not _vertical_field_rows(document)
            or sum(bool(re.match(r'^\s*(?:明细表|表格)(?:标题|表头|项目)\s*[:：]', fact))
                   for fact in document['facts']) > 1):
        return None
    totals = _original_fields(document, r'(?:' + TOTAL_LABEL + r')(?:金额)?')
    claims = _explicit_claim_fields(document)
    if len(totals) != 1 or claims is None or len(claims) > 1:
        return None
    candidates = []
    for profile in _FACTOR_PROFILES:
        fields = [_original_fields(document, labels) for labels in profile]
        if len(profile)==2 and not fields[1]:
            fields[1]=_unit_priced_fields(document)
        if all(len(items) == 1 for items in fields):
            candidates.append([items[0] for items in fields])
    if len(candidates) != 1:
        return None
    factors = candidates[0]
    known = {o['quote'] for o in [totals[0], *factors, *claims]}
    # An extra factor/amount or another table block prevents a single-expense
    # proof, even if its number happens to equal one of the selected fields.
    all_labels = '|'.join(label for profile in _FACTOR_PROFILES for label in profile)
    if any(o['quote'] not in known for o in _original_fields(document, all_labels)):
        return None
    # Quoted vertical field names are layout metadata, not different expenses.
    # Still reject extra numeric fields, including unfamiliar fee names, rather
    # than allowing them to disappear just because their labels were quoted.
    if any(field['quote'] not in known for field in _original_fields(document, _NUMERIC_FIELD_LABEL)):
        return None
    numeric_field = (r'(?:金额|费用|工资|单价|合计|总计|人数|数量|天数)\s*(?:[:：=]|为|是)\s*[¥￥]?\s*'
                     + ORIGINAL_NUMBER)
    if any(fact not in known and (re.search(AMOUNT_FIELD, fact)
           or (re.search(numeric_field, fact) and not _repeats_staff_factors(fact, factors, numeric_field)))
           for fact in document['facts']):
        return None
    return {'total': totals[0], 'factors': factors, 'claims': claims}


def _matches_field(operand, field, documents):
    from .pdf_evidence import _number
    facts = _facts_for(operand, documents)
    return (operand['unit_id'] == field['unit_id']
            and _number(operand['number']) == _number(field['number'])
            and operand['quote'] in field['quote']
            and bool(facts) and all(fact == field['quote'] for fact in facts))


def _matches_factors(operands, basis, documents):
    fields = basis['factors']
    matches = [[i for i, field in enumerate(fields) if _matches_field(o, field, documents)]
               for o in operands]
    return (len(matches) == len(fields) and all(len(m) == 1 for m in matches)
            and {m[0] for m in matches} == set(range(len(fields))))


def _claim_operand(operand, documents=()):
    from .runtime_evidence_repair import claim_operand
    if claim_operand(operand, documents):
        return True
    # Bind the label to this operand's own original number, not another amount
    # elsewhere in a long quote. The normal operand validator proves the quote.
    number = re.escape(operand['number'])
    pattern = re.compile(r'(?:' + CLAIM_AMOUNT_LABEL + r')'+CLAIM_VALUE_SEPARATOR+r'('
                         + number + r')(?![\d.])')
    if pattern.search(operand['quote']):
        return True
    # A short numeric quote may use its containing fact only when the quoted
    # occurrence itself is the claim field. Equal numbers elsewhere do not
    # establish a role; no model label participates in this decision.
    roles = []
    for fact in _facts_for(operand, documents):
        claim_spans = [m.span(1) for m in pattern.finditer(fact)]
        for quoted in re.finditer(re.escape(operand['quote']), fact):
            spans = [(quoted.start()+m.start(), quoted.start()+m.end()) for m in
                     re.finditer(r'(?<![\d.])'+number+r'(?![\d.])', quoted.group())]
            roles.extend(span in claim_spans for span in spans)
    return bool(roles) and all(roles)


def _facts_for(operand, documents):
    return [fact for d in documents if d['unit_id'] == operand['unit_id']
            for fact in d['facts'] if operand['quote'] in fact]


def settlement_purpose(comparison, documents=()):
    if _claim_operand(comparison['left'], documents):
        return 'claim_ceiling'
    if (comparison['operation'] == 'value' and len(comparison['right']) == 1
            and _claim_operand(comparison['right'][0], documents)):
        return 'claim_ceiling_reverse'
    if comparison['operation'] == 'product':
        return 'line_amount'
    return 'declared_total'


def expected_operator(scenario, rule_id, comparison, documents=(), *, comparison_index=0):
    from .pdf_policy import comparison_operators
    permitted = comparison_operators(scenario, rule_id)
    policy = permitted.get(comparison['value_kind'])
    field = f'comparisons/{comparison_index}'
    if policy is None:
        raise CheckEvidenceError('数值类别必须符合本条审核要点，不能混用金额、数量或单价',
                                 rule_id, 'comparison_value_kind', field+'/value_kind')
    if isinstance(policy, dict):
        purpose = settlement_purpose(comparison, documents)
        branch = policy[purpose]
        if comparison['operation'] not in branch.get('operations', [branch.get('operation')]):
            raise CheckEvidenceError('结算行金额仅按真实计费因子相乘复算；整单核销上限使用完整金额依据。',
                                     rule_id, 'comparison_operation', field+'/operation')
        expected = branch['operator']
    else:
        purpose = comparison['value_kind']
        expected = policy
    if comparison['operator'] != expected:
        raise CheckEvidenceError(f'数量金额比较方向错误：本项{purpose}应使用{expected}，实际为{comparison["operator"]}。',
                                 rule_id, 'comparison_operator', field+'/operator')
    return expected


def _detail_amounts(documents):
    result = []
    for document in documents:
        for index, fact in enumerate(document['facts']):
            if (not re.search(r'(?:第\s*\d+\s*行|!R\d+|表格行\d+)', fact)
                    or re.search(r'合计|总计|表头', fact)):
                continue
            amounts = re.findall(AMOUNT_FIELD, fact)
            if len(amounts) == 1:
                result.append({'unit_id': document['unit_id'], 'quote': fact, 'number': amounts[0]})
    return result


def _complete_detail_amounts(documents):
    rows = _detail_amounts(documents)
    originals = [fact for d in documents for fact in d['facts'] if re.search(_ROW, fact)
                 and not re.search(r'合计|总计|表头', fact)]
    # A row with unreadable/ambiguous amount cannot disappear from a program sum.
    if not rows or len(rows) != len(originals):
        return None
    numbers = [re.search(r'(?:第\s*|!R|表格行)(\d+)', fact)[1] for fact in originals]
    return rows if len(set(numbers)) == len(numbers) else None


def complete_settlement_comparisons(check, documents):
    """Verify an explicitly written table total by code, using original rows.

    A model may compare a claim directly with a written total and omit the
    table's own sum. Recover only an unambiguous, source-labelled full table;
    multi-table or partially read documents keep the existing model evidence.
    """
    if check['rule_id'] != 'settlement_calculation' or not check['comparisons']:
        return check
    from .pdf_evidence import _number
    comparisons = list(check['comparisons'])
    basis = single_settlement_expense(documents)
    additions = []
    if basis:
        # Preserve and validate every supplied comparison. Add omitted checks
        # only from a unique original expense; never remove an invalid extra.
        if not any(c['operation'] == 'product' and _matches_field(c['left'], basis['total'], documents)
                   for c in comparisons):
            additions.append({'label': '单笔结算原始金额与全部计费因子复算', 'value_kind': 'amount',
                'left': basis['total'], 'right': basis['factors'], 'operation': 'product', 'operator': 'eq'})
        for claim in basis['claims']:
            if not any(_matches_field(c['left'], claim, documents)
                       or (c['operation'] == 'value' and len(c['right']) == 1
                           and _matches_field(c['right'][0], claim, documents)) for c in comparisons):
                additions.append({'label': '独立核销金额与本笔完整计费支撑金额核对', 'value_kind': 'amount',
                    'left': claim, 'right': basis['factors'], 'operation': 'product', 'operator': 'le'})
        comparisons.extend(additions)
    for document in documents:
        if any(re.search(_PARTIAL, text)
               for text in document.get('limitations', [])):
            continue
        rows = _detail_amounts([document])
        totals = [fact for fact in document['facts'] if re.search(TOTAL_LABEL, fact)
                  and re.search(AMOUNT_FIELD, fact)]
        if not rows or len(totals) != 1:
            continue
        total = totals[0]
        numbers = re.findall(AMOUNT_FIELD, total)
        if len(numbers) != 1:
            continue
        if not any(o['unit_id'] == document['unit_id'] and o['quote'] in total
                   and _number(o['number']) == _number(numbers[0])
                   for c in comparisons for o in [c['left'], *c['right']]):
            continue
        if any(c['operation'] == 'sum' and c['left']['unit_id'] == document['unit_id']
               and c['left']['quote'] in total for c in comparisons):
            continue
        comparisons.append({'label': '结算表原始合计与全部明细金额复算', 'value_kind': 'amount',
            'left': {'unit_id': document['unit_id'], 'quote': total, 'number': numbers[0]},
            'right': rows, 'operation': 'sum', 'operator': 'eq'})
    # A fully identified single table can also prove an independent claim's
    # ceiling. Do not let a model omit that field while reporting only its rows.
    table_basis = None
    if not basis and len(documents) == 1:
        document = documents[0]
        rows = _complete_detail_amounts(documents)
        claims = _original_fields(document, CLAIM_AMOUNT_LABEL)
        if (rows and len(claims) == 1
                and not any(re.search(_PARTIAL, text) for text in document.get('limitations', []))):
            claim = claims[0]
            if not any(_matches_field(c['left'], claim, documents)
                       or (c['operation'] == 'value' and len(c['right']) == 1
                           and _matches_field(c['right'][0], claim, documents)) for c in comparisons):
                added = {'label': '独立核销金额与全部结算明细金额核对', 'value_kind': 'amount',
                         'left': claim, 'right': rows, 'operation': 'sum', 'operator': 'le'}
                additions.append(added)
                comparisons.append(added)
                table_basis = {'detail_amounts': rows, 'claim': claim}
    result = {**check, 'comparisons': comparisons}
    if additions:
        result['settlement_basis_completion'] = {'original_basis': basis or table_basis,
                                                'added_comparisons': additions}
    return result


def validate_settlement_claim_coverage(check, documents):
    if check['rule_id'] != 'settlement_calculation' or check['status'] not in {'pass', 'fail'}:
        return
    for document in documents:
        for claim in _original_fields(document, CLAIM_AMOUNT_LABEL):
            if not any(_matches_field(c['left'], claim, documents)
                       or (c['operation'] == 'value' and len(c['right']) == 1
                           and _matches_field(c['right'][0], claim, documents)) for c in check['comparisons']):
                raise CheckEvidenceError('结算原文另列独立核销金额，须以完整对应计费依据核对其上限；'
                                         '依据不完整时不能仅凭已核对的部分费用宣称整项通过。',
                    'settlement_calculation', 'comparison_scope', 'comparisons')


def validate_settlement_sources(comparison, documents, *, comparison_index=0):
    """Prove one row's factors; never allow cross-row multiplication.

    Only identifiable detail rows are used for the completeness check; an
    unparseable source cannot create a synthetic total or invented row.
    """
    purpose = settlement_purpose(comparison, documents)
    if comparison['operation'] == 'product':
        operands = ([comparison['left'], *comparison['right']] if purpose == 'line_amount'
                    else comparison['right'])
        if len({o['unit_id'] for o in operands}) != 1:
            raise AuditError('本行金额与数量、单价须来自同一结算行，不能跨页或跨行混乘。')
        facts = next((d['facts'] for d in documents if d['unit_id'] == operands[0]['unit_id']), [])
        if not any(all(o['quote'] in fact for o in operands) for fact in facts):
            basis = single_settlement_expense(documents)
            if not (basis and _matches_factors(comparison['right'], basis, documents)
                    and (purpose == 'claim_ceiling'
                         or _matches_field(comparison['left'], basis['total'], documents))):
                raise CheckEvidenceError('本行金额与计费因子没有同一原始结算行或唯一单笔费用依据，不能跨行混乘。',
                    'settlement_calculation', 'comparison_scope', f'comparisons/{comparison_index}/right')
    elif comparison['operation'] == 'sum':
        # If the source explicitly distinguishes a detail amount from a claim,
        # it cannot be relabelled as the whole claim by selecting sum/le.
        total_facts = _facts_for(comparison['left'], documents)
        if purpose == 'declared_total' and (not total_facts or not all(re.search(TOTAL_LABEL, fact)
                                                                     for fact in total_facts)):
            raise AuditError('表内合计复算左侧须引用真实合计字段，不能将某行金额当作整单合计。')
        right = comparison['right']
        from .pdf_evidence import _number
        for row in _detail_amounts(documents):
            if not any(o['unit_id'] == row['unit_id'] and o['quote'] in row['quote']
                       and _number(o['number']) == _number(row['number']) for o in right):
                raise AuditError('结算合计须覆盖全部对应结算明细金额，不能用部分行求和。')


def settlement_schema(documents=None, source_ids=None):
    """The same source-purpose split guides every applicable model schema."""
    schema = {'properties': {'value_kind': {'const': 'amount'},
                           'operation': {'type': 'string', 'enum': ['product', 'sum', 'value']}},
            'description': '按原始字段用途校验：行金额=本行计费因子乘积，product/eq；'
                           '表内合计=全部对应明细金额，sum/eq；实际核销金额<=完整支撑金额，le。'
                           '仅明确左侧支撑、右侧核销的value/ge可表示等价上限。不能仅按运算类型选方向；'
                           '引用保留原始字段及原值，用途与来源由程序再次核验。' + SETTLEMENT_SCOPE}
    if source_ids:
        allowed = sorted(set(source_ids))
        source = {'properties': {'unit_id': {'enum': allowed}}}
        schema['properties'].update(left=source, right={'items': source})
        schema['description'] += ('本项所有操作数只取本次结算来源，不取合同计划、申请预算或POS。'
                                  '只核对实际列示的行金额、合计和独立核销金额，不为凑算式虚构金额对象。')
        if documents is not None:
            settlement = [d for d in documents if d['unit_id'] in allowed]
            # Absence of an independent claim field does not create an upper
            # bound comparison for the same written row/total. Keep eq for its
            # actual arithmetic, including genuine under/over-stated amounts.
            if not any(re.search(CLAIM_AMOUNT_LABEL, fact) for d in settlement for fact in d['facts']):
                schema['properties'].update(operation={'enum': ['product','sum']}, operator={'const':'eq'})
                schema['description'] += ('本批结算事实没有另列独立核销金额，只复算实际计费行或完整表内合计，'
                                          '不把同一合计换称核销额并附加le/ge。')
    return schema


def settlement_comparisons_schema(documents=None, source_ids=None):
    schema = {'items': settlement_schema(documents, source_ids)}
    if documents is not None and source_ids:
        settlement = [d for d in documents if d['unit_id'] in set(source_ids)]
        basis = single_settlement_expense(settlement)
        if basis and not basis['claims'] and len(set(source_ids)) == len(settlement):
            schema.update(maxItems=1, description='原始来源只证明一笔费用及一个金额对象，只需一条完整product/eq。')
            schema['items']['properties'].update(operation={'const': 'product'}, operator={'const': 'eq'})
        if basis and len(set(source_ids)) == len(settlement):
            # Preserve field identity in generation: the same bare number can
            # occur in both the actual claim and the written table total.
            # These are original complete facts, not model-authored labels.
            def branch(left,right,operation,operator):
                return {'properties':{'left':{'enum':left},'right':right,
                    'operation':{'const':operation},'operator':{'const':operator}}}
            factors={'items':{'enum':basis['factors']},'minItems':len(basis['factors']),
                     'maxItems':len(basis['factors']),'uniqueItems':True}
            choices=[branch([basis['total']],factors,'product','eq')]
            if basis['claims']:
                choices.extend([
                    branch(basis['claims'],factors,'product','le'),
                    branch(basis['claims'],{'items':{'enum':[basis['total']]},'minItems':1,'maxItems':1},'value','le'),
                    branch([basis['total']],{'items':{'enum':basis['claims']},'minItems':1,'maxItems':1},'value','ge')])
            schema['items']['anyOf']=choices
            schema['description']=schema.get('description','')+'同一来源有同值金额时，按完整原始字段引用区分合计和核销，不能只摘取裸数字。'
    return schema
