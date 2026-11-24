"""Current 2026-11-17 workbook, adopted by the user on 2026-11-18.

Only the current catalogue is changed. Recorded runs retain their frozen policy.
The two ambiguous cells were explicitly clarified by the user: commission POS
quantity may exceed settlement quantity; training photos do not need an address.
"""
from dataclasses import replace

SOURCE_TITLE = '附件：费用分类及申请核销资料表-2026.11.17.xlsx'
SOURCE_SHA256 = '91aaa4dd6f0dc490a3ccd88f475d51025c8905ca0ee6567a1925dcf89d709c8b'
POLICY_VERSION = '2026-11-24.xlsx.54'
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
STAMPED_FORMATS = (
    '促销合同和结算单接受PDF、JPG/JPEG、PNG。逐页或逐图核对实际签章、主体、清晰度和内容；'
    '图片只要能核对就无须补PDF。其他实际要求验章的协议、盖章POS、盖章库存明细及批量微信账单仍须PDF原件。'
    '依据已解析的原始格式和资料用途核对，不能用扩展名或渲染出的预览格式代替。'
    '活动照片和原生POS Excel按各自格式提交，已交但不清晰时标明对应文件与具体位置。'
)
STAFF_UNITS = (
    '按H11常规人头报价，数量为单店人数，申请单价为日工资×执行天数。先读申请说明、合同和结算的单位。'
    '申请明细未标单位而申请说明与原件明确按天报价时，分别核对人数、执行天数、元/天以及人数×天数×日工资的总额。'
    '说明该单的按天填报方式，天数不与人数比较，日单价不与全期工资比较。单位不能确定时保留原字段供核对。'
)
REWARD_PRECISION = (
    '分别列出原精度理论奖励、结算奖励及实际核销金额，核对各自用途、合同上限和批复。'
    '奖励按整元列示，与理论值按元四舍五入一致且未超过理论值时，忽略该取整尾差；'
    '保留真实超额、错误比例、跨档及实际核销与批复的独立检查。'
)
POS = (
    '实际使用POS的各类核对原生Excel活动时间、69码、商品名称、逐行销售数量、销售单价、销售金额及金额合计。'
    '仅POS达标激励免查销售数量合计；其他类型仍核对数量合计相等。各类均按原值复算逐行金额和金额合计，'
    '不以金额除数量补造单价，盖章PDF不能补Excel缺项。'
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
PAYMENT_ATTRIBUTION = (
    '按每笔付款金额、时间和来源，核对经销商发给本次临促或激励对象的用途证据。'
    '可结合同一交易的备注、名单、领款确认或工资账单；其他交易的备注不能代用。'
    '逐笔区分已有用途备注、尚无用途佐证和付款双方身份无法对应，定位具体交易，'
    '分开说明到账总额及其中用途待核部分。备注并非唯一证据，不统一追要双方全名。'
)
SUBMITTED_MATERIAL_SCOPE = (
    '核对上传的合同、结算、POS等凭证是否属于本单。引用本单申请和附件中的签约主体、活动内容、'
    '门店、执行期等相互印证的事实，列出疑似其他活动的具体原件和理由。资料盘点中的额外文件是复核线索。'
    '已明确指定的样张、模板、参考件只作为参考。相似错字、同案多页或多份同类材料本身不足以认定混单。'
    '按正文认定实际材料角色；仅取得明确提交字段或目录时提示传错位置，未取得字段时不推断操作。'
    '合同不能代替应提交的POS，继续核对实际收到的POS。'
)
ACTUAL_PERIOD_NOTE = (
    '申请期与资料中另行注明的实际执行期并存时，分别引用两个区间及备注来源，核对合同、结算与POS的相互印证。'
    '已明确解释且材料相互印证的特殊执行安排可交人工复核，不因两个区间不同直接判资料不合格。'
    '资料本身的年份错误、未解释的期间矛盾及计算条款所用月份与合同不符仍需说明。'
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
        + TRAINING + SEAL + POS + STAMPED_FORMATS)
    ns['POS_CLARIFICATION'] = POS + '要求POS的分支同时提供公章版和Excel；纯临促工资、培训免强制POS，实际提供则完整核对。'
    ns['POS_COMMON_CLARIFICATION'] = (
        '通用POS检查以原生Excel全量明细为准：5+2字段、十进制原值复算和准确69码商品查库。'
        '金额支撑项允许少申报；数量合计按各类型要求核对。商品名称保留原文，同码名称/颜色/包装描述差异不报商品不符。'
        '准确69码查无参考与数据库故障分别说明；不以库内字段补POS原值。' + POS)
    ns['SUBMITTED_SOURCE_CHECKS']['pos_fields']['scope'] = 'uses_pos'
    ns['SUBMITTED_SOURCE_CHECKS']['pos_fields']['requirement'] = POS
    ns['SUBMITTED_SOURCE_CLARIFICATION'] = (
        '已有POS Excel即继续核对字段，不因另一份资料缺少而跳过；POS达标激励同样检查数量及销售单价。'
        '人员激励的合同、结算周期分别与本单实际关联申请周期核对，原有资料独立检查。')
    ns['PAYMENT_CLARIFICATION'] = PAYMENT + PAYMENT_ATTRIBUTION
    ns['PAYMENT_COMPANY_CLARIFICATION'] = PAYMENT
    ns['PRODUCT_PHOTO_CLARIFICATION'] += (
        ' 新品、改码或包装更新仅使用本次实际取得的当前商品资料、版本及包装尺寸；'
        '缺尺寸或参考图时列具体参照缺口，不凭旧包装推定新商品不符。')
    ns['STAFF_PHOTO_CLARIFICATION'] = (
        '纯临促工资及培训免强制POS；销售提成及门店/区域达成奖励按实际分支要求POS。'
        + STAFF_UNITS +
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
    ns['AMOUNT_CLARIFICATION'] = ns['AMOUNT_CLARIFICATION'].replace(
        '数量合计相等。', '除POS达标激励外，数量合计相等。') + (
        ' 新表要求的陈列堆头、临促工资、POS达标激励也核对实际核销金额=本单系统批复合计，'
        '不能把申请预算上限检查当成批复相等。销售提成销量允许少申报，结算销量≤对应POS全量销量；补差销量仍相等。')
    ns['UNIT_PRICE_CLARIFICATION'] = ns['UNIT_PRICE_CLARIFICATION'].replace(
        '临促申请单价=结算单日工资×执行天数，单独核对，不把人数当商品销量；', STAFF_UNITS)
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
                if item.id in {'settlement', 'promotion_contract'}:
                    text += '；须公章，接受PDF、JPG/JPEG或PNG，按原件内容核对印章'
                else:
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
            elif r.id in {'contract_activity_dates', 'settlement_activity_dates', 'settlement_application_dates'}:
                text += ACTUAL_PERIOD_NOTE
            elif r.id == 'payment_activity_dates': text = '临促工资、销售提成、培训的实际付款日期在结算单活动期间或结束后不超过3个自然月，逐笔核对。'
            elif r.id == 'training_photo_content': text = TRAINING
            elif r.id == 'entry_inventory': text = ENTRY
            elif r.id == 'entry_sku': text = ns['ENTRY_SKU_CLARIFICATION'] + ' 逐门店逐申请69码列出已识别、明确缺失及无法确认；不能以另店照片补齐。'
            elif r.id == 'settlement_pos_quantity' and scenario == 'personnel_incentive':
                text = '销售提成逐69码结算申报销量≤POS Excel全量对应销量；高于才不足，低于或等于均可。临促人数、天数及门店达成人数不与SKU销量比较。'
            elif r.id in {'staff_headcount', 'staff_total_unit_price'}:
                text = STAFF_UNITS + (' 本项核对人数。' if r.id == 'staff_headcount' else ' 本项核对工资计价单位与金额。')
            elif scenario == 'pos_target_incentive' and r.id == 'target_reward':
                text += REWARD_PRECISION
            elif scenario == 'pos_target_incentive' and r.id == 'pos_fields':
                text = '原生POS Excel逐项核对活动时间、69码、商品名称、销售数量、销售单价、销售金额及金额合计。本类免查销售数量合计。'
            elif scenario == 'pos_target_incentive' and r.id == 'pos_arithmetic':
                text = '逐行核对销售金额≥数量×销售单价，列示销售金额合计≥全量明细金额之和。本类免查销售数量合计。'
            elif r.id == 'invoice_details': text += ' 若本次提供收据，另核对收款方公司财务专用章。'
            source = ('用户确认的所有涉及POS类型通用5+2及原值复算标准'
                      if r.id in {'pos_fields','pos_arithmetic','pos_product_identity'} else
                      f'{SOURCE_TITLE}·Sheet1!H{SOURCE_ROWS[scenario]}')
            if scenario == 'pos_target_incentive' and r.id in {'pos_fields','pos_arithmetic'}:
                source += '，2026-11-24确认POS达标激励免查数量合计'
            out.append(replace(r, text=text, source=source))
        ns['RULES'][scenario] = tuple(out)
        ns['RULE_POINTS'][scenario]['submitted_material_scope'] = 'A1及本次资料归属复核'
        ns['RULES'][scenario] += (R('submitted_material_scope', SUBMITTED_MATERIAL_SCOPE,
                                    source=f'{SOURCE_TITLE}·Sheet1!A1及销售支持资料归属反馈'),)
        if scenario in {'promotional_display', 'pos_target_incentive'}:
            ns['RULES'][scenario] += (new_rule(scenario, 'claim_ceiling', '结算单实际核销金额等于本单系统批复金额合计，不能以申请预算代替。',
                when='available_reference:claim_ceiling:recorded_approval_applicable', numeric=True, application=True),)
        ns['RULES'][scenario] += (new_rule(scenario, 'stamped_document_pdf', STAMPED_FORMATS),)
        if scenario in {'personnel_incentive', 'self_procured_gift_material', 'price_difference_support', 'giveaway_promotion', 'pos_target_incentive'}:
            ns['RULES'][scenario] += (new_rule(scenario, 'pos_activity_dates',
                '逐笔或逐完整日期范围核对POS Excel活动时间落在结算单活动周期闭区间内；覆盖全部明细，字段存在不等于周期对齐，不猜年份。', when='required_pos'),)
        if scenario in {'poster_material', 'self_procured_gift_material'}:
            ns['RULES'][scenario] += (new_rule(scenario, 'receipt_finance_seal',
                '实际提供收据时，核对收款方公司财务专用章及收款方主体；非收据不适用，不要求经销商代盖收款方章。', when='uses_receipt'),)
    s = 'personnel_incentive'
    ns['RULES'][s] += (new_rule(s, 'payment_attribution', PAYMENT_ATTRIBUTION,
                                when='personal_incentive_payment'),)
    ns['RULES'][s] += (new_rule(s, 'contract_reward_unit_price',
        '销售提成促销合同活动内容中每个69码的奖励单价与本次系统申请对应单价相等；分别引用合同与申请，不用结算单单价代替合同单价。',
        when='available_reference:contract_reward_unit_price:personnel_sku', numeric=True, application=True),)
    s = 'poster_material'
    ns['RULES'][s] += (new_rule(s, 'poster_payment_dates',
        '海报张贴费用的付款/票据开具日期在结算单活动期间或结束后不超过3个自然月，保留真实日期与来源，不用照片日期替代。', when='poster_posting'),)
    s = 'self_procured_gift_material'
    ns['RULES'][s] += (new_rule(s, 'gift_activity_evidence', GIFT),)
    ns['POLICY_INTERPRETATIONS'] = {'commission_quantity': 'settlement_le_pos', 'training_address': 'not_required',
        'fifty_stores': '80_percent', 'non_ai_fee_pool': '客户费用池-赠品',
        'settlement_and_promotion_contract_formats': ['PDF', 'JPEG', 'PNG'],
        'pos_target_quantity_total': 'not_required'}


def extra_applicability(when, scenario, flags):
    if when == 'personal_incentive_payment' and scenario == 'personnel_incentive':
        relevant = [flags.get(k) for k in ('temporary_staff', 'sales_commission', 'training')]
        if True in relevant:
            return True
        if flags.get('store_incentive') is True or all(value is False for value in relevant):
            return False
        return None
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
