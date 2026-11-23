"""2026-11-16 meeting decisions adopted by the user on 2026-11-17.

This is a policy supplement, never evidence for an individual claim. Apply it
after the workbook so generated skills and the actual model see one catalogue.
"""
from dataclasses import replace

POLICY_VERSION = '2026-11-17.meeting.52'
SOURCE = '核销自动化上线沟通V2.0·2026-11-16会议纪要（用户2026-11-17确认执行）'
SEAL = (
    '按会议纪要区分文件：结算单、促销合同须使用公章或财务专用章；'
    '进场/产品推广协议还允许合同章。出库章、人事章等无关部门印章不能替代上述章种。'
    '印章须清晰、完整，不能重叠遮挡；无法辨明章种或主体时定位原文件及页码，交人工复核，不猜为合格或未盖章。'
    '印章主体仍须对应本单经销商；原文明写双方盖章时仍核对双方。'
    '印刷公司名称、签字、标题、已填写内容或另一文件的印章不等于本文件盖章。'
    '整份文件没有任何实际印章或签字图形时，明确记未盖章/未签章；仅有签字仍不能替代必需印章。'
    '本次未改变POS及批量微信账单原有章种口径，仍核对实际印章和对应主体。'
)
PDF = (
    '促销合同及本类型实际要求验章的结算单、协议、盖章POS、盖章库存明细、批量微信账单须提交PDF原文件，'
    '逐页查看实际印章；仅改扩展名、截图或把其他文件的印章借来均不能证明符合。'
    '以解析得到的真实文件格式及本单资料角色判断，不以渲染后的页面PNG认定原文件不是PDF。'
    '这不要求活动照片、原生POS Excel、其他无需验章的明细转PDF，POS Excel仍按本类要求保留。'
    '格式问题与文件是否已交、能否读取、印章是否合规分别说明；已交非PDF不得写成文件未交，仍完成可判断的内容。'
)
ENTRY = (
    'CVS/OTC进场执行资料二选一：覆盖本次全部进场门店及公司进场商品的终端库存明细盖章版，或POS销售数据。'
    'POS可满足本项，不再同时追要库存明细；选择库存路径仍检查实际盖章。'
    'POS路径按本次实际门店、商品、销售期间及完整明细核对覆盖，不能以部分门店/商品替代全部进场范围。'
    '渠道只以接口cvsOtcChannel为准，照片比例和逐店SKU规则不因选择POS而改变。'
)
GIFT = (
    '按会议纪要最后的关键决策，外采赠品执行证明为POS数据与活动照片二选一，可按门店混合提供并共同覆盖实际活动范围。'
    '不强制终端销售小票；有POS证明的门店不另要求成品/现场照片，有合格活动照片的门店不另要求POS。'
    '两种证明都没有时合并列一项执行证明缺口；已交但无效、模糊或覆盖不全时定位具体门店/来源，不写成未交。'
    '选择照片时需展示赠品成品及活动现场，允许同店多图和同图同时承担两种用途；不跨店借图。'
    '选择POS时仍按原规则核对相应POS明细、盖章PDF及Excel；两种都交则审核各自内容，不要求另一种补齐已有充分证明的门店。'
    '采购发票或收据仍必交，不能与取消的终端销售小票混淆；采购转账仅为可选辅助，不替代票据。'
    '合同赠送规则仍保留；仅照片路径不强制补POS复算实际销量，不把采购数量或合同预计数量当实际赠出数量。'
    '手写金额、抬头或赠送数量无法辨明时引用具体原件并交人工复核，不能凭上下文补字或猜数。'
)
POS_TARGET = (
    '仅POS达标激励：核对POS的真实归属、活动期间、销售金额及金额合计；不强制逐SKU数量、销售单价、数量合计，'
    '不以数量×单价复算POS，也不把缺数量汇总或缺单价列成缺件。已提供商品/69码仍保留原文并核对实际身份。'
    '全量实际POS销售金额≥结算单对应的POS销售金额即可；高于或等于不报差异，低于才说明不足。'
    '先确认两侧范围、期间、字段含义一致，不拿销售总额与奖励核销金额混比。'
    '奖励仍按本次合同/申请真实达成规则计算，预算限额导致少申报不因金额不相等报错；不取消其他类型数量、单价和金额要求。'
)


def apply_meeting_policy(ns, legacy_seal):
    R, M = ns['Rule'], ns['Requirement']
    old_entry, old_seal = ns['ENTRY_CHANNEL_CLARIFICATION'], ns['SEAL_CLARIFICATION']
    ns['POLICY_VERSION'] = POLICY_VERSION
    ns['SEAL_CLARIFICATION'] = SEAL
    ns['SUBMITTED_SOURCE_CHECKS']['pos_fields'] = {
        'scope': 'uses_pos_except_pos_target_incentive',
        'requirement': '除POS达标激励外，POS电子版须有每个SKU销售单价，不以金额除数量补造单价。' + POS_TARGET}
    ns['COMPARISON_OPERATORS']['target_reward'] = {'amount': 'le'}
    ns['ENTRY_CHANNEL_CLARIFICATION'] = old_entry.replace(
        '另须提供覆盖全部本次进场门店及公司进场商品的终端库存明细盖章版，POS不能替代该库存明细。', ENTRY)
    ns['GIFT_MATERIAL_CLARIFICATION'] = GIFT
    ns['GIFT_CONTRACT_CLARIFICATION'] += ' 外采赠品按POS或照片二选一，不因选择照片而追要POS；缺执行数量时具体交人工复核，不补造数量。'
    ns['AUDIT_SCOPE_CLARIFICATION'] += SOURCE + '优先覆盖冲突的旧口径；其他已确认规则保留。' + SEAL + PDF + ENTRY + GIFT + POS_TARGET
    # Shared text must not re-introduce mandatory 5+2 checks into the exception.
    for key in ('POS_COMMON_CLARIFICATION', 'POS_CLARIFICATION', 'AMOUNT_CLARIFICATION', 'SUBMITTED_SOURCE_CLARIFICATION'):
        ns[key] = '以下通用POS字段及数量/单价运算仅适用于POS达标激励以外的类型；POS达标激励按本段末尾专项规则执行。' + ns[key] + POS_TARGET
    ns['RESULT_CLARIFICATION'] += '会议确认AI初筛、人工最终复核；手写及照片真实性疑点不自动作拒付、扣款或人事处罚。'
    ns['PRODUCT_PHOTO_CLARIFICATION'] += '新品、改码或包装更新仅使用实际取得的当前商品资料、版本及包装尺寸；缺尺寸或参考图时列具体参照缺口，供维护商品资料，不凭旧包装推定新商品不符。'
    ns['MATERIAL_LABELS'].update(entry_system='终端库存明细盖章PDF或POS销售数据', gift_activity_evidence='POS或活动照片执行证明')
    ns['MATERIAL_RULE_IDS']['gift_activity_evidence'] = 'gift_activity_evidence'
    for scenario, materials in ns['MATERIALS'].items():
        updated = []
        for item in materials:
            if item.id in {'settlement', 'promotion_contract'}:
                item = replace(item, text=item.text + '；提交PDF，须公章或财务专用章，印章清晰不重叠')
            elif item.id == 'entry_system':
                item = replace(item, text=ENTRY + '库存盖章版提交PDF；POS路径保留Excel及盖章PDF。')
            elif item.id == 'entry_agreement':
                item = replace(item, text=item.text + '；实际使用协议时提交PDF，双方印章限公章、财务专用章或合同章')
            elif item.id == 'pos':
                item = replace(item, text=item.text + '；盖章版为PDF，Excel原始数据另保留')
            if scenario == 'self_procured_gift_material':
                if item.id in {'pos', 'pos_excel'}:
                    item = replace(item, when='optional', text=item.text + '；作为执行证明的可选来源，按实际门店覆盖判断二选一是否满足，不单独强制两条路径齐交')
                elif item.id in {'gift_finished_photos', 'gift_store_photos'}:
                    item = replace(item, when='optional', text=item.text + '；作为执行证明的可选来源，有POS覆盖的门店不重复要求，照片内容仍按实际来源核验')
            updated.append(item)
        if scenario == 'self_procured_gift_material':
            updated.append(M('gift_activity_evidence', GIFT))
        ns['MATERIALS'][scenario] = tuple(updated)

    for scenario, registered in ns['RULES'].items():
        updated = []
        for r in registered:
            text = r.text.replace(legacy_seal, SEAL).replace(old_seal, SEAL).replace(old_entry, ns['ENTRY_CHANNEL_CLARIFICATION'])
            when = r.when
            if r.id in {'settlement_seal', 'settlement_template_seal'}:
                text = '结算单须有本次经销商公章或财务专用章，印章清晰完整、不重叠；实际主体与本单对应。' + SEAL
            elif r.id == 'contract_signed':
                text = '促销合同须有本次经销商公章或财务专用章，签字不能替代所需印章。' + SEAL
            elif r.id == 'entry_inventory':
                text = ENTRY
            elif r.id == 'entry_sku':
                text = ns['ENTRY_SKU_CLARIFICATION'] + '输出按门店×本次申请69码逐项列出已识别、明确缺失及无法确认，并列对应照片/页码；同一条码不能重复计数，无法看清不能当未提交。'
            if scenario == 'self_procured_gift_material':
                if r.id in {'gift_finished_photos', 'gift_store_photo_content'}:
                    when = 'photo_evidence'
                    text = GIFT + '照片路径逐张核对日期、地址、拍摄时间和现场内容。' + ns['PHOTO_TIME_PRESENCE_CLARIFICATION']
                elif r.id in {'pos_seal', 'pos_dealer', 'gift_rule_quantity'}:
                    when = 'uses_pos'
                    text += ' ' + GIFT
            if scenario == 'pos_target_incentive' and r.id in {'pos_fields', 'pos_arithmetic', 'pos_sales_amount', 'target_reward'}:
                text = POS_TARGET if r.id in {'pos_fields', 'pos_arithmetic'} else text + ' ' + POS_TARGET
            if scenario == 'pos_target_incentive' and r.id == 'pos_product_identity':
                text += '仅核对本次实际提供的商品/69码；金额口径POS没有逐SKU明细时，不单独追要商品、69码、数量或单价。'
            updated.append(replace(r, text=text, when=when, source=SOURCE if text != r.text or when != r.when else r.source))
        updated.append(R('stamped_document_pdf', PDF, source=SOURCE))
        ns['RULE_POINTS'][scenario]['stamped_document_pdf'] = '会议纪要·盖章资料PDF'
        if scenario == 'self_procured_gift_material':
            updated.append(R('gift_activity_evidence', GIFT, source=SOURCE))
            ns['RULE_POINTS'][scenario]['gift_activity_evidence'] = '会议纪要·外采赠品关键决策'
        ns['RULES'][scenario] = tuple(updated)
