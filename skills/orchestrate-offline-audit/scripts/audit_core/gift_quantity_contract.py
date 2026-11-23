"""Generate the same gift-quantity source roles enforced by the validator."""


def gift_quantity_schema(materials):
    def sources(*roles):
        return sorted({uid for m in materials or [] if m['id'] in roles and m['state']=='present'
                       for uid in m['source_ids']})
    contract=sources('promotion_contract');pos=sources('pos_excel')
    submitted=sources('settlement','pos_excel')
    if not contract:
        return None
    def operand(ids):return {'properties':{'unit_id':{'enum':ids}}} if ids else False
    choices=[{'properties':{'operation':{'const':'value'},
                            'right':{'minItems':1,'maxItems':1,'items':operand(contract)}}}]
    if pos:
        choices.append({'properties':{'operation':{'enum':['ratio','floor_ratio']},
            'right':{'minItems':3,'maxItems':3,'prefixItems':[operand(pos),operand(contract),operand(contract)],'items':False}}})
    choices.append({'properties':{'operation':{'const':'product'},
        'right':{'minItems':1,'prefixItems':[operand(sorted(set(pos)|set(contract)))],
                 'items':operand(contract),'contains':operand(contract)}}})
    schema={'description':'赠品核对：直接数量约定来自本次合同；按销量计算时，POS购买依据与合同赠送参数共同参与。'
                          '单独POS赠品数量不是合同计算的应赠量，不以改运算或反复引用POS替代合同规则。',
            'anyOf':choices}
    if submitted:schema['properties']={'left':operand(submitted)}
    return schema
