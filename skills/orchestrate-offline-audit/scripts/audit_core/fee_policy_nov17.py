"""Current 2026-11-17 workbook, adopted by the user on 2026-11-18.

Only the current catalogue is changed. Recorded runs retain their frozen policy.
The two ambiguous cells were explicitly clarified by the user: commission POS
quantity may exceed settlement quantity; training photos do not need an address.
"""
from dataclasses import replace

SOURCE_TITLE = '附件：费用分类及申请核销资料表-2026.11.17.xlsx'
SOURCE_SHA256 = '91aaa4dd6f0dc490a3ccd88f475d51025c8905ca0ee6567a1925dcf89d709c8b'
POLICY_VERSION = '2026-11-17.xlsx.53'
SOURCE_ROWS = {
    'entry_fee': '5:6', 'promotional_display': '7:10',
    'personnel_incentive': '11:13', 'poster_material': '14:17',
    'self_procured_gift_material': '18', 'price_difference_support': '19:20',
    'giveaway_promotion': '21', 'pos_target_incentive': '22',
}
SEAL = (
    '按2026.11.17新表：收据必须有收款方公司财务专用章；其他本类实际要求验章的资料须使用公章，'
    '包括结算单、促销合同、产品推广协议、盖章POS、库存明细及批量工资单/微信账单。'
    '其他资料的财务专用章、合同章、业务章、人事章等不能代替公章。'
    '收据核对收款方的财务专用章，不把费用代垫经销商印章当作收款方印章。'
    '其他印章主体对应本单实际要求的经销商或签约方；原文明写双方盖章时仍须双方。'
    '章印须清晰完整、不重叠遮挡；章种或主体不能确认时定位原件交人工复核，不猜为合格或未盖章。'
    '整份文件没有任何实际印章或签字图形时明确记录未盖章/未签章，不能把印刷名称当实际印章。'
    '签字、印刷名称、标题或另一文件的印章不能代替本文件的必需印章。'
)
TRAINING = (
    '用户2026-11-18确认新表H13末尾“地址不要”：培训现场照片只核对活动日期、拍摄时间及培训现场内容，'
    '不要求地址水印，不进行门店地址匹配；即使地址为空也不报缺件、无法确认或不符。'
    '同一周期多场培训每场至少一张。该例外仅限培训，不放宽临促或其他类型照片。'
)
POS = (
    '所有实际使用POS的类型均按完整POS标准核对，包括POS达标激励：活动时间、69码、商品名称、销售数量、'
    '销售单价、销售金额及销售数量和金额合计。数据以原生Excel原值为准，盖章PDF不能补Excel缺项。'
    '逐行金额及金额合计按已确认的金额支撑方向复算，数量合计相等，不以金额除数量补造单价。'
    'POS达标激励恢复上述字段及复算，不再适用会议纪要的仅金额豁免。'
    '销售提成、外采赠品、补差、搭赠及POS达标激励均核对POS明细活动时间落在结算单执行周期内。'
    '时间字段存在不等于周期已对齐；逐笔日期、日期范围及所有对应明细均保留来源，不能只核对部分行。'
    '缺年或范围不清时不得猜年；原明细未提供的字段不从合同、结算或商品库补写。'
)
PAYMENT = (
    '人员激励按本次实际内容区分临促工资、销售提成、培训，可同时包含多个分支。'
    '临促工资、销售提成及培训的实际付款逐笔去重后合计均须≥结算实际核销金额；'
    '付款日期须在结算单活动期间或活动结束后不超过3个自然月，包含最后一天，不按90天计算。'
    '工资银行转账、微信账单和红包均纳入相应金额与日期核对，不能因临促工资不是红包而跳过。'
    '实际使用的红包/微信激励截图须有参半、小箭头或重点品牌字样，不按1000元门槛检查经销商公司名称。'
    '批量导出的工资单/微信账单须有本次经销商公章，名称与申请费用代垫经销商对应。'
    '同一笔付款的发出、收款确认及重叠截图只计一次；不同交易即使金额相同也分别计算。'
)
GIFT = (
    '新表E18/H18覆盖会议纪要二选一口径：外采赠品必须同时提供采购发票或收据、经销商公章POS及原生Excel、'
    '全部活动门店现场水印照片、结算单和签章促销合同。POS与照片各自必需，不能相互替代。'
    '照片展示促销活动内容及自采赠品；同一图可以同时承担成品与门店现场用途，不跨店借图。'
    '赠送规则从本次真实合同读取，以POS实际销售数量或金额计算应赠数量，应赠数量≥结算申报数量。'
    '采购数量、合同预计数量和发票金额不能冒充实际销量或实际赠出数量。'
    '采购转账仅为可选辅助，不能替代采购发票/收据；不额外强制终端销售小票。'
    '采购收据须盖收款方公司财务专用章。资料已交但不清或覆盖不足，定位具体来源与门店。'
)
ENTRY = (
    'CVS/OTC进场执行资料二选一：覆盖全部本次进场门店及商品的POS销售资料，或全量终端库存明细。'
    '两条路径均保留公章盖章版及对应原生Excel；库存路径不能只交盖章PDF而缺库存Excel，'
    'POS路径也保留公章PDF及POS Excel。选择已完整满足的一条路径，不重复追要另一条。'
    '门店与69码须与本次申请范围一致且齐全，印章名称对应申请费用代垫经销商。'
    '已交盖章版但缺Excel时具体说明缺少对应Excel，不能写成盖章版未交。'
)
POSTER = (
    '物料分海报张贴和制作（海报、宣传单页、展示道具）。发票/收据/打款截图三选一，金额≥实际核销金额；'
    '发票或收据抬头、打款截图付款方分别与申请费用代垫经销商对应。'
    '收据必须有收款方公司财务专用章。uses_receipt按原件正文是否为收据识别，缺章的收据仍为收据，不能靠该标记豁免验章。'
    '海报张贴费用凭证的实际付款/开具日期在活动期间或活动结束后不超过3个自然月；'
    '独立识别付款时间或票据开具时间，不用现场照片日期补造。'
    '张贴同址更换前后各一张水印照片，两图地点一致，至少一张含参半/小箭头/重点产品。'
    '制作照片在活动期间、含品牌元素；另有含数量、单价、总金额的电子制作清单，清单合计≥核销金额。'
    '新表已删除物料制作中的“多场培训”笔误，不将培训要求加给物料制作。'
)


def apply_nov17_policy(ns, legacy_seal):
    R, M = ns['Rule'], ns['Requirement']
    from .meeting_policy import PDF
    old_seal = ns['SEAL_CLARIFICATION']
    old_watermark = ns['PHOTO_WATERMARK_CLARIFICATION']
    old_entry = ns['ENTRY_CHANNEL_CLARIFICATION']
    ns.update(POLICY_VERSION=POLICY_VERSION, SOURCE_TITLE=SOURCE_TITLE,
              SOURCE_SHA256=SOURCE_SHA256, SOURCE_ROWS=SOURCE_ROWS)
    ns['FLAGS'] += ('uses_receipt',)
    ns['SEAL_CLARIFICATION'] = SEAL
    ns['AUDIT_SCOPE_CLARIFICATION'] = (
        '用户2026-11-18要求按2026.11.17新表修改。明确变化覆盖11月4日表及11月16日会议纪要的冲突口径，'
        '不冲突的已确认规则保留。E列盘点核销资料，H列执行审核；新表明确的收据章种、付款时限及F列POS字段同时执行。'
        'D列申请资料和A1管理后果不机械扩为核销包附件或自动拒付、罚款、人事通知。'
        '系统申请、批复、合同、结算、POS及付款分别保留独立来源，不相互自证。'
        '本单实际取得的勤策字段按原义核对，空字段沿用既有参照边界，不能伪造系统值。'
        '审批前尚未形成最终批复不列客户缺件；已有有效批复时应核对本表要求的金额相等。'
        '公司赠品物料、维护费用及其他仍不进入八类AI审核；公司赠品费用池应为客户费用池-赠品，'
        '该项属于非AI费用池管理，不新增第九类或伪造当前接口未提供的费用池字段。'
        + TRAINING + SEAL + POS + PDF)
    ns['POS_CLARIFICATION'] = POS + '要求POS的分支同时提供公章版和Excel；纯临促工资、培训免强制POS，实际提供则完整核对。'
    ns['POS_COMMON_CLARIFICATION'] = (
        '通用POS检查以原生Excel全量明细为准：5+2字段、十进制原值复算和准确69码商品查库。'
        '金额支撑项允许少申报；数量合计相等。商品名称保留原文，同码名称/颜色/包装描述差异不报商品不符。'
        '准确69码查无参考与数据库故障分别说明；不以库内字段补POS原值。' + POS)
    ns['SUBMITTED_SOURCE_CHECKS']['pos_fields']['scope'] = 'uses_pos'
    ns['SUBMITTED_SOURCE_CHECKS']['pos_fields']['requirement'] = '所有使用POS的类型均检查原生Excel完整5+2字段，不以金额除数量补造单价。'
    ns['SUBMITTED_SOURCE_CLARIFICATION'] = (
        '已有POS Excel即继续核对字段，不因另一份资料缺少而跳过；POS达标激励同样检查数量及销售单价。'
        '人员激励的合同、结算周期分别与本单实际关联申请周期核对，原有资料独立检查。')
    ns['PAYMENT_CLARIFICATION'] = PAYMENT
    ns['PAYMENT_COMPANY_CLARIFICATION'] = PAYMENT
    ns['PRODUCT_PHOTO_CLARIFICATION'] += (
        ' 新品、改码或包装更新仅使用本次实际取得的当前商品资料、版本及包装尺寸；'
        '缺尺寸或参考图时列具体参照缺口，不凭旧包装推定新商品不符。')
    ns['STAFF_PHOTO_CLARIFICATION'] = (
        '纯临促工资及培训免强制POS；销售提成及门店/区域达成奖励按实际分支要求POS。'
        '临促人数等于申请数量，申请单价等于日工资×执行天数。'
        '临促活动期每天至少一张含日期、地址、拍摄时间、临促人员及公司产品的照片。'
        '销售提成结算对应69码销量≤POS全量对应销量；申请计划销量不当实际销量。' + TRAINING + PAYMENT)
    ns['INVOICE_DETAILS_CLARIFICATION'] = POSTER
    ns['GIFT_MATERIAL_CLARIFICATION'] = GIFT
    ns['GIFT_CONTRACT_CLARIFICATION'] = GIFT
    # Retain the already confirmed integer thresholds, including 50 stores at 80%.
    start = old_entry.index('另须提供覆盖全部本次进场门店')
    end = old_entry.index('少于20家', start)
    ns['ENTRY_CHANNEL_CLARIFICATION'] = old_entry[:start] + ENTRY + old_entry[end:]
    ns['PHOTO_WATERMARK_CLARIFICATION'] = (
        '按各类型核对照片日期、地址、拍摄时间及现场内容，活动与门店引用本单真实来源；'
        '临促每日、培训每场，进场照片不新增活动周期限制。' + TRAINING + ns['PHOTO_TIME_PRESENCE_CLARIFICATION'])
    ns['AMOUNT_CLARIFICATION'] += (
        ' 新表要求的陈列堆头、临促工资、POS达标激励也核对实际核销金额=本单系统批复合计，'
        '不能把申请预算上限检查当成批复相等。销售提成销量允许少申报，结算销量≤对应POS全量销量；补差销量仍相等。')
    ns['SCENARIO_COMPARISON_OPERATORS'] = {'personnel_incentive': {'settlement_pos_quantity': {'quantity': 'le'}}}
    ns['COMPARISON_OPERATORS']['contract_reward_unit_price'] = {'unit_price': 'eq'}
    ns['MATERIAL_LABELS']['entry_system'] = '公章版及对应Excel的终端库存明细或POS销售资料'

    def new_rule(scenario, id, text, *, when='always', numeric=False, application=False):
        row = SOURCE_ROWS[scenario]
        if application:
            ns['APPLICATION_RULES'] |= {(scenario, id)}
            if numeric: ns['APPLICATION_COMPARISON_RULES'] |= {(scenario, id)}
        ns['RULE_POINTS'][scenario][id] = row
        return R(id, text, when=when, numeric=numeric, source=f'{SOURCE_TITLE}·Sheet1!H{row}')

    for scenario, materials in ns['MATERIALS'].items():
        out = []
        for item in materials:
            if item.id in {'settlement', 'promotion_contract', 'pos', 'entry_agreement'}:
                text = item.text.replace('不区分章种，实际盖章即可', '须公章')
                text += '；本资料要求的印章须为公章，验章原件提交PDF，POS原生Excel另保留'
                item = replace(item, text=text)
            if item.id == 'entry_system': item = replace(item, text=ENTRY)
            out.append(item)
        ns['MATERIALS'][scenario] = tuple(out)

    for scenario, registered in ns['RULES'].items():
        out = []
        for r in registered:
            text = r.text.replace(old_seal, SEAL).replace(legacy_seal, SEAL).replace(old_entry, ns['ENTRY_CHANNEL_CLARIFICATION'])
            text = text.replace(old_watermark, ns['PHOTO_WATERMARK_CLARIFICATION'])
            if r.id in {'settlement_seal', 'settlement_template_seal'}:
                text = '本次结算单须有经销商公章，名称与本单对应。' + ('须使用公司统一模板。' if r.id == 'settlement_template_seal' else '') + SEAL
            elif r.id == 'contract_signed': text = '本次促销合同须有相应经销商公章，原双方签章条件保留。' + SEAL
            elif r.id == 'pos_seal': text = 'POS盖章版须有本次经销商公章，实际数据仍以原生Excel为准。' + SEAL
            elif r.id == 'payment_statement_seal': text = '批量导出的工资单/微信账单须有本次经销商公章，不能用其他章种替代。'
            elif r.id == 'payment_company': text = '临促及销售提成等实际红包/微信激励截图须有参半、小箭头或重点品牌字样，不按金额门槛检查经销商公司名称。'
            elif r.id == 'payment_claim_amount': text = '临促工资、销售提成、培训的本次实际付款逐笔去重合计≥结算实际核销金额；工资转账与红包均核对。'
            elif r.id == 'payment_activity_dates': text = '临促工资、销售提成、培训的实际付款日期在结算单活动期间或结束后不超过3个自然月，逐笔核对。'
            elif r.id == 'training_photo_content': text = TRAINING
            elif r.id == 'entry_inventory': text = ENTRY
            elif r.id == 'entry_sku': text = ns['ENTRY_SKU_CLARIFICATION'] + ' 逐门店逐申请69码列出已识别、明确缺失及无法确认；不能以另店照片补齐。'
            elif r.id == 'settlement_pos_quantity' and scenario == 'personnel_incentive':
                text = '销售提成逐69码结算申报销量≤POS Excel全量对应销量；高于才不足，低于或等于均可。临促人数、天数及门店达成人数不与SKU销量比较。'
            elif r.id == 'invoice_details': text += ' 若本次提供收据，另核对收款方公司财务专用章。'
            source = ('用户确认的所有涉及POS类型通用5+2及原值复算标准'
                      if r.id in {'pos_fields','pos_arithmetic','pos_product_identity'} else
                      f'{SOURCE_TITLE}·Sheet1!H{SOURCE_ROWS[scenario]}')
            out.append(replace(r, text=text, source=source))
        ns['RULES'][scenario] = tuple(out)
        if scenario in {'promotional_display', 'pos_target_incentive'}:
            ns['RULES'][scenario] += (new_rule(scenario, 'claim_ceiling', '结算单实际核销金额等于本单系统批复金额合计，不能以申请预算代替。',
                when='available_reference:claim_ceiling:recorded_approval_applicable', numeric=True, application=True),)
        ns['RULES'][scenario] += (new_rule(scenario, 'stamped_document_pdf', PDF),)
        if scenario in {'personnel_incentive', 'self_procured_gift_material', 'price_difference_support', 'giveaway_promotion', 'pos_target_incentive'}:
            ns['RULES'][scenario] += (new_rule(scenario, 'pos_activity_dates',
                '逐笔或逐完整日期范围核对POS Excel活动时间落在结算单活动周期闭区间内；覆盖全部明细，字段存在不等于周期对齐，不猜年份。', when='required_pos'),)
        if scenario in {'poster_material', 'self_procured_gift_material'}:
            ns['RULES'][scenario] += (new_rule(scenario, 'receipt_finance_seal',
                '实际提供收据时，核对收款方公司财务专用章及收款方主体；非收据不适用，不要求经销商代盖收款方章。', when='uses_receipt'),)
    s = 'personnel_incentive'
    ns['RULES'][s] += (new_rule(s, 'contract_reward_unit_price',
        '销售提成促销合同活动内容中每个69码的奖励单价与本次系统申请对应单价相等；分别引用合同与申请，不用结算单单价代替合同单价。',
        when='available_reference:contract_reward_unit_price:personnel_sku', numeric=True, application=True),)
    s = 'poster_material'
    ns['RULES'][s] += (new_rule(s, 'poster_payment_dates',
        '海报张贴费用的付款/票据开具日期在结算单活动期间或结束后不超过3个自然月，保留真实日期与来源，不用照片日期替代。', when='poster_posting'),)
    s = 'self_procured_gift_material'
    ns['RULES'][s] += (new_rule(s, 'gift_activity_evidence', GIFT),)
    ns['POLICY_INTERPRETATIONS'] = {'commission_quantity': 'settlement_le_pos', 'training_address': 'not_required',
        'fifty_stores': '80_percent', 'non_ai_fee_pool': '客户费用池-赠品'}


def extra_applicability(when, scenario, flags):
    if when == 'non_store_red_packet' and scenario == 'personnel_incentive':
        if flags.get('store_incentive') is True:
            return False
        if flags.get('training') is True and not any(flags.get(k) is True for k in ('temporary_staff','sales_commission')):
            return False
        return flags.get('red_packet')
    if when == 'approved_claim_applicable':
        return True if scenario == 'personnel_incentive' else NotImplemented
    if when == 'incentive_or_training_payment':
        if scenario == 'personnel_incentive' and any(flags.get(k) is True for k in ('temporary_staff','sales_commission','training','store_incentive','red_packet')):
            return True
        return None
    return NotImplemented
