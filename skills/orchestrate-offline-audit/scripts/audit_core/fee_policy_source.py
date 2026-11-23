"""User-approved 2026-11-04 workbook rules; never current-case evidence."""
from __future__ import annotations
from dataclasses import replace

SOURCE_TITLE = "附件：费用分类及申请核销资料表-2026.11.4.xlsx"
POLICY_VERSION = "2026-11-16.xlsx.51"
SOURCE_SHEET = "Sheet1"
SOURCE_ROWS = {
    "promotional_display": "5:8", "personnel_incentive": "9:11",
    "poster_material": "12:15", "self_procured_gift_material": "16",
    "price_difference_support": "19:20", "giveaway_promotion": "21",
    "pos_target_incentive": "22", "entry_fee": "24:25",
}
WORKBOOK_CLARIFICATION = (
    "用户2026-11-06指定2026.11.4费用分类及申请核销资料表为本轮规则来源。"
    "核销资料按E列盘点，审核按H列执行；D列申请资料与A1背景不机械增加为核销包必交项。"
    "新表明确调整的比较方向、签章类型、付款品牌、日期及资料分支覆盖旧口径；不冲突的已确认补充保留。"
    "本类型已有审核确实使用且本单已取得的费用代垫经销商、活动起止日、数量、单价、商品及金额，分别按原字段含义使用；不因接口返回就新增逐字段对齐。"
    "勤策字段不是必填前置：本单对应审核项有有效值时必须使用，并以勤策值为准；字段空白则只跳过该字段的勤策对齐，不报客户缺资料，不影响原有资料审核。"
    "数字按同一对象、含义和单位精确比较，0是有效值；文字允许合理模糊匹配，线上/线下后缀及轻微写法差异不单独报错，不能合并真实不同分店或数字编号。"
    "申请金额优先读取实际总额，也可由程序对本单精确关联且完整覆盖的申请行去重求和；完整范围可以只有1行，但任意单行或部分行不能冒充合计。"
    "审批前的AI核销不以最终审批完成为前置，未形成的最终批复不列客户资料问题。"
    "只引用本单精确关联的勤策申请/方案或本次实际提交且能确认关联的系统申请原件，保留独立来源；"
    "合同、结算单、核销行均不能自证系统申请或批复值。辅助来源缺失只记技术档案，不新增API必填项、必交资料或把内部取数限制写进客户AI总结。"
    "实际照片、POS、支付数据仍分别证明执行情况；不将申请计划数量当已销售数量。"
    "2026-11-06用户确认H13中“多场培训”为复制笔误，按多场物料制作活动理解。"
    "仅八类规范bizType及既有精确别名进入AI；表中公司赠品物料、维护费用及其他仍无需AI核销，不新增第九类。"
    "不自动决定0核销、扣款、罚款或人事考核，不改写历史结果。"
)
APPLICATION_CLARIFICATION = (
    "仅执行本类型原有审核项；需要补充且实际已取得的申请参照须保留本单关联，不把全部系统字段变成必审对齐项。"
    "没有对应辅助参照时不执行该项额外系统对齐，不把字段未取得写成客户问题；合同、结算单、POS及照片自身的原有审核继续。"
    "商品身份优先使用项目商品数据库，勤策未提供69码不能据此否定已经完成的商品库匹配。合同仍提供本次真实赠送/奖励规则与执行约定，不用合同总额冒充勤策原始字段。"
)
PERSONNEL_CLARIFICATION = (
    "人员激励分临促工资、销售提成、培训费用，按本次合同/结算实际内容确定，可包含多个分支，不能从缺POS或缺照片反推豁免。"
    "temporary_staff仅可靠肯定涉及临促时为true，training仅培训费用，sales_commission表示本单实际有按SKU销量计提的销售提成。"
    "纯临促工资或纯培训不强制POS；实际提供POS仍完整核验5+2和原值。临促工资须每日现场水印照片，含临促人员及公司产品；"
    "培训每场至少一张培训现场水印照片。销售提成不因无临促说明额外要求临促照片。"
    "临促单店人数=申请数量，申请单价=单日工资×执行天数，不把人数/天数当商品销量。"
    "门店/区域达成奖励按人/家实际含义核对，不把人数、门店数与SKU销量比较；混合SKU计提只核对真实SKU项目。"
    "销售提成红包截图需包含参半、小箭头、重点任一品牌字样，不再仅对大额红包检查公司名；"
    "付款总额去重后≥结算实际核销金额，付款时间在活动期间或结束后不超过3个自然月；"
    "培训付款同样检查金额和时间，批量微信账单还需经销商实际盖章（不区分章种）及与申请一致的经销商名称。"
)
POSTER_CLARIFICATION = (
    "物料制作分海报张贴与制作（海报、宣传单页、展示道具）。发票/收据/打款截图三选一，不能误要求同时交三种；"
    "前两者抬头或后者付款方分别与申请费用代垫经销商核对，金额≥结算实际核销金额。"
    "张贴：同一地址更换前后各一张水印照片，两图地点一致，至少一图含参半/小箭头/重点产品。"
    "制作：活动周期内各次制作成品水印照片，含品牌元素即可；另须制作清单电子版，包含数量、单价及总金额，清单合计≥实际核销金额。"
    "H13多场培训已由用户确认为多场物料制作活动，不将培训材料加给物料制作。"
)


def apply_workbook_policy(namespace: dict) -> None:
    """Update the one authoritative catalogue, retaining compatible user decisions."""
    R, M = namespace["Rule"], namespace["Requirement"]
    base, optional_pos = namespace["BASE_MATERIALS"], namespace["OPTIONAL_POS"]
    pos, excel = namespace["POS_MATERIAL"], namespace["POS_EXCEL"]
    pos_rules = namespace["POS_RULES"]
    old = namespace["RULES"]
    old_watermark = namespace["PHOTO_WATERMARK_CLARIFICATION"]
    old_price = namespace["PRICE_SUPPORT_CLARIFICATION"]
    materials = namespace["MATERIALS"]
    namespace.update(POLICY_VERSION=POLICY_VERSION, SOURCE_TITLE=SOURCE_TITLE)
    namespace["FLAGS"] += ("sales_commission", "poster_posting", "batch_payment")
    namespace["MATERIAL_LABELS"].update(activity_application="本次系统活动申请（独立比较依据）", approved_claim="本单系统核销批复（独立比较依据）",
        training_photos="培训现场水印照片", material_list="物料制作清单电子版", invoice="发票或收据")
    optional_application = M("activity_application", "本单精确关联的系统活动申请原件或勤策申请明细，仅实际取得时引用；不要求另造申请表", "optional")
    materials["personnel_incentive"] = (
        M("pos", pos.text + "；仅销售提成/达成奖励需要，纯临促工资、培训可免", "personnel_pos"),
        M("pos_excel", excel.text + "；仅销售提成/达成奖励需要，纯临促工资、培训可免", "personnel_pos"),
        M("payment", "临促工资打款截图、激励红包截图或培训付款凭证；批量微信账单按实际内容归本项"),
        M("staff_photos", "明确涉及临促工资时，活动期每天的现场水印照片，含人员和公司产品", "confirmed_temporary_staff"),
        M("training_photos", "培训现场水印照片；同一周期多场培训每场至少一张", "training"), *base)
    materials["poster_material"] = (*optional_pos,
        M("invoice", "发票、收据或打款截图三选一，抬头/付款方与费用代垫经销商一致"),
        M("finished_photos", "张贴为同址更换前后水印照片；制作为活动期成品水印照片"),
        M("material_list", "物料制作清单电子版，列数量、单价、总金额；海报张贴不要求", "material_production"), *base)
    for scenario in materials:
        materials[scenario] = (*materials[scenario], optional_application,
            M("approved_claim", "本单系统核销批复金额合计，实际取得时独立引用；不能用申请预算或结算单自证", "optional"))

    app_rules = set()
    comparison_rules = set()
    points: dict[str, dict[str, str]] = {}
    def rule(scenario, id, text, point, *, when="always", numeric=False, application=False):
        points.setdefault(scenario,{})[id] = point
        if application:app_rules.add((scenario,id))
        if numeric and application:comparison_rules.add((scenario,id))
        return R(id,text,when=when,numeric=numeric,
                 source=f"{SOURCE_TITLE}·{SOURCE_SHEET}!H{point}")
    def common(scenario, row):
        seal_id = "settlement_template_seal" if scenario in {"promotional_display","poster_material"} else "settlement_seal"
        out = [rule(scenario,seal_id,"结算单须有本次经销商实际盖章，不区分公章、财务章、合同章、结算单章或其他章种；只要文件上有任何实际印章即视为已盖章。",row),
            rule(scenario,"settlement_dealer","结算单落章经销商名称及填写名称分别与系统活动申请【费用代垫经销商】一致，逐字核对错别字和漏字。",row,application=True),
            rule(scenario,"settlement_activity_dates","结算单执行周期须与本次促销合同一致，分别引用双方原文。",row),
            rule(scenario,"settlement_application_dates","结算单执行周期与本次系统活动申请一致；合同及结算单相同不能掩盖与申请不一致。",row,application=True),
            rule(scenario,"contract_signed","促销合同须有经销商实际盖章，不区分公章、财务章、合同章、结算单章或其他章种；只要文件上有任何实际印章即视为已盖章。",row),
            rule(scenario,"contract_dealer","促销合同落章名称及填写的经销商名称分别与系统活动申请【费用代垫经销商】一致，核对错别字、漏字。",row,application=True),
            rule(scenario,"contract_budget_amount","促销合同活动预算金额与本次申请金额核对。优先用实际总额；没有独立总额字段时，允许程序对本单精确关联且完整覆盖的申请行去重求和，保留全部原行引用，不再额外要求单头总额。完整关联范围只有1行仍按完整合计；不得用任意单行、部分行或核销金额代替申请合计。",row,numeric=True,application=True),
            rule(scenario,"contract_activity_dates","促销合同执行周期与系统活动申请执行周期一致，逐字引用双方日期。",row,application=True)]
        if scenario not in {"promotional_display","pos_target_incentive"}:
            out.append(rule(scenario,"claim_ceiling","结算单【实际核销金额】等于本单系统核销【批复金额合计】，不能用申请额度或合同总额代替。",row,
                when="recorded_approval_applicable",numeric=True,application=True))
        return out

    # Keep the explicitly retained product/size checks and largeVenueFee contract.
    s="promotional_display"
    kept=[r for r in old[s] if r.id in {"display_brand","display_material_brand","display_size","atrium_agreement"}]
    for r in kept: points.setdefault(s,{})[r.id]="5:8（产品按原设定及已确认补充）"
    namespace["RULES"][s] = (*pos_rules,
        rule(s,"display_watermark","逐张照片水印含日期、地址、拍摄时间，清晰展示全貌、摆放及促销标识；日期位于系统申请周期内，地址对应申请活动门店。", "5:8", application=True),
        *kept, *common(s,"5:8"),
        rule(s,"display_quantity_price","结算堆头数量、单价分别等于系统活动申请对应项目的数量、单价；按同一项目和单位核对。", "5:8", numeric=True,application=True),
        rule(s,"settlement_calculation","核销金额按本次真实数量、单价复算；结算本次核销金额不得高于计算金额，不自定多余公式。", "5:8",numeric=True),
        rule(s,"application_claim_ceiling","结算核销金额不得超过本次系统活动申请金额，与预算字段分别对应。", "5:8",numeric=True,application=True))

    s="personnel_incentive"
    namespace["RULES"][s]=(*pos_rules,*common(s,"9:11"),
        rule(s,"reward_unit_price","销售提成结算中69码和奖励单价分别对应系统活动申请【条形码、单价】，单价原精度相等。门店奖励按人/家原义核对，临促工资另核。", "10",when="personnel_sku",numeric=True,application=True),
        rule(s,"settlement_pos_quantity","仅真实按SKU销量计提的结算行，逐69码销量须等于POS Excel全量对应销量。临促人数/天数和门店达成人数不得与SKU销量比较。", "10",when="personnel_sku",numeric=True),
        rule(s,"application_quantity_price","销售/门店激励按本次实际奖励对象核对系统申请对应数量及单价，SKU按69码对应，不将预计名额当实际达成人数。", "10",when="personnel_sales",numeric=True,application=True),
        rule(s,"staff_headcount","临促结算的单店临促人数等于系统活动申请数量；人数不是商品销量。", "9",when="confirmed_temporary_staff",numeric=True,application=True),
        rule(s,"staff_total_unit_price","系统申请单价等于结算单日工资×执行日期天数；左侧申请单价，右侧实际日工资及天数。", "9",when="confirmed_temporary_staff",numeric=True,application=True),
        rule(s,"application_claim_ceiling","临促工资结算实际核销金额不得超过本次系统活动申请金额；申请预算与销售提成/培训核销批复分别对应。", "9",when="confirmed_temporary_staff",numeric=True,application=True),
        rule(s,"settlement_calculation","销售提成为逐行销售数量×奖励单价，临促为人数×天数×日工资；按实际项目及单位复算后，合计不得小于结算实际核销金额。培训不套用SKU公式。", "9:10",when="personnel_not_training",numeric=True),
        rule(s,"payment_company","销售提成红包/微信激励截图须含参半、小箭头或重点品牌字样；不再按1000元门槛检查经销商公司名称。", "10",when="non_store_red_packet"),
        rule(s,"payment_claim_amount","本次有效红包或培训付款逐笔去重后合计≥结算实际核销金额；付款归属不明或金额不清具体说明。", "10:11",when="incentive_or_training_payment",numeric=True),
        rule(s,"payment_activity_dates","销售提成或培训付款须在活动期间，或活动结束后不超过3个自然月，按真实付款日期逐笔核对。", "10:11",when="incentive_or_training_payment",application=True),
        rule(s,"payment_statement_seal","采用批量导出微信账单时，须有本次经销商实际盖章，不区分章种；只要文件上有任何实际印章即视为已盖章。", "10:11",when="batch_payment"),
        rule(s,"payment_statement_dealer","批量微信账单落章经销商名称与申请费用代垫经销商一致，核对错字漏字。", "10:11",when="batch_payment",application=True),
        rule(s,"staff_photo_content","临促照片含日期、地址、拍摄时间水印；活动执行期每一天至少一张，地址匹配系统申请门店，画面同时含临促人员和公司产品，不能跨店跨日借图。", "9",when="confirmed_temporary_staff",application=True),
        rule(s,"training_photo_content","培训现场水印照片含日期、地址、拍摄时间；日期在系统申请周期内、地址匹配，且各场培训分别有现场照片。", "11",when="training",application=True),
        rule(s,"pos_seal","销售提成盖章版POS须有经销商实际盖章，不区分章种；数据仍以Excel为准，不以盖章版补Excel缺项。", "10",when="personnel_sales"),
        rule(s,"pos_dealer","销售提成POS落章经销商名称与系统申请费用代垫经销商一致，核对错字漏字。", "10",when="personnel_sales",application=True),
        rule(s,"application_products","销售提成POS和促销合同涉及产品的69码分别与本次系统申请条形码对应，不能把合同列有商品当作已销售。", "10",when="personnel_sku",application=True))

    s="poster_material"
    namespace["RULES"][s]=(*pos_rules,*common(s,"12:15"),
        rule(s,"invoice_details","发票抬头、收据抬头或打款截图付款方分别与系统申请费用代垫经销商一致，三选一；各自原义不能混作收款方。", "12:15",application=True),
        rule(s,"invoice_amount","有效发票/收据/打款金额合计≥实际核销金额；同一交易的多种凭证不重复求和。", "12:15",numeric=True),
        rule(s,"finished_photo","成品或张贴前后照片须含日期、地址、拍摄时间水印，日期在申请周期内，地址匹配申请门店；制作照片含品牌元素即可，同一周期每场物料制作活动至少一张。", "12:15",application=True),
        rule(s,"posting_before_after","海报张贴同一地址更换前后各一张照片，地点一致，至少一张含参半、小箭头或重点产品，不能拿两张不同地点的照片拼成前后对照。", "12",when="poster_posting"),
        rule(s,"material_list_amount","物料制作清单电子版须有详细数量、单价及总金额，其金额合计≥结算实际核销金额；海报张贴不要求制作清单。", "13:15",when="material_production",numeric=True))

    s="self_procured_gift_material"
    kept=[r for r in old[s] if r.id in {"gift_rule_present","gift_rule_quantity","gift_finished_photos"}]
    points[s]={r.id:"16" for r in kept}
    namespace["RULES"][s]=(*pos_rules,*common(s,"16"),*kept,
        rule(s,"invoice_details","发票或收据抬头与系统申请费用代垫经销商一致；采购转账仅辅助，不能替代本类票据。", "16",application=True),
        rule(s,"invoice_amount","有效发票/收据金额≥结算实际核销金额，重复凭证不重复计。", "16",numeric=True),
        rule(s,"application_quantity_price","结算采购/制作数量合计及单价与系统申请对应数量合计及单价一致，不与POS零售价或赠品应赠量混淆。", "16",numeric=True,application=True),
        rule(s,"settlement_calculation","结算采购/制作数量×单价=各行金额；对应金额合计≥实际核销金额，按原值复算。", "16",numeric=True),
        rule(s,"gift_store_photo_content","实际活动门店水印照片应在申请周期内、地址匹配，清晰展示促销内容及自采物料；同一张可同时证明赠品及门店用途。", "16",application=True),
        rule(s,"pos_seal","本次POS盖章版须有经销商实际盖章，不区分章种；只要文件上有任何实际印章即视为已盖章。", "16"),
        rule(s,"pos_dealer","POS落章经销商名称与申请费用代垫经销商一致，核对错字漏字。", "16",application=True))

    for s,row in (("price_difference_support","19:20"),("giveaway_promotion","21")):
        ids={"price_evidence","photo_activity_dates","full_reduction_evidence"} if s=="price_difference_support" else {"giveaway_evidence"}
        kept=[r for r in old[s] if r.id in ids]
        points[s]={r.id:row for r in kept}
        namespace["RULES"][s]=(*pos_rules,*common(s,row),*kept,
            rule(s,"reward_unit_price","结算对应69码的奖励/补差/搭赠单价与系统活动申请条形码和单价一致，按原精度比较；搭赠申请69码指赠送商品，不与购买商品混淆。",row,numeric=True,application=True),
            rule(s,"settlement_calculation","结算各行数量×对应单价=本行金额，金额合计≥实际核销金额；搭赠数量不是购买商品销量。",row,numeric=True),
            rule(s,"pos_seal","POS盖章版须有经销商实际盖章，不区分章种，实际销量和金额仍读取Excel原值。",row),
            rule(s,"pos_dealer","POS落章经销商名称与申请费用代垫经销商一致，核对错字漏字。",row,application=True),
            rule(s,"application_products","本次POS、对应结算行及适用的合同涉及产品须与系统活动申请的69码对应；搭赠须区分购买商品和赠送商品的不同角色。",row,application=True),
            *( [rule(s,"settlement_pos_quantity","结算每个69码申报销量与POS Excel全量对应69码销量相等，新表覆盖此前补差只检查上限的口径。",row,numeric=True),
                rule(s,"application_quantity_price","结算逐69码补差单价与系统申请对应单价相等，按原值及同一单位比较。",row,numeric=True,application=True),
                rule(s,"price_matches_contract","逐张照片活动价格标签与促销合同促销价一致；日期和价格独立检查，看不清定位具体图片。",row)] if s=="price_difference_support" else [
                rule(s,"gift_rule_quantity","按真实搭赠规则，用POS实际购买商品销量或金额计算应搭赠数量；应搭赠数量≥结算申报搭赠数量，不能把赠品69码当购买商品69码，不自定取整。",row,numeric=True)] ),
            rule(s,"activity_photo_content","活动照片须含日期、地址、拍摄时间水印，日期在申请周期内、地址匹配申请门店。搭赠允许各店照片或含购买/赠送商品、购买时间及门店信息的小票二选一，混合提交共同覆盖全量。",row,application=True))

    s="pos_target_incentive"
    namespace["RULES"][s]=(*pos_rules,*common(s,"22"),
        rule(s,"pos_seal","POS盖章版须有经销商实际盖章，不区分章种；只要文件上有任何实际印章即视为已盖章。","22"),
        rule(s,"pos_dealer","POS落章经销商名称与系统申请费用代垫经销商一致，核对错字漏字。","22",application=True),
        rule(s,"pos_sales_amount","POS Excel全部销售明细金额合计≥结算单【POS销售金额】，不得只取局部明细。","22",numeric=True),
        rule(s,"target_reward","根据本次奖励规则与POS实际销售金额选择达标档位，整笔销售金额×对应比例计算奖励；不能把目标金额当实际销量，未达高档不能套高比例，也不逐SKU比较奖励单价。","22",numeric=True))

    # Entry-fee retains the explicitly approved channel thresholds and inventory
    # supplement; the workbook adds watermark fields and application references.
    s="entry_fee"
    points[s]={r.id:"24:25及已确认渠道/库存补充" for r in old[s]}
    namespace["RULES"][s]=tuple(r for r in old[s] if r.id not in {"entry_watermark","entry_sku"})+(
        rule(s,"entry_watermark","逐张上架照片水印含日期、地址、拍摄时间，清晰展示陈列及产品摆放，地址与系统活动申请门店匹配；本表未要求进场照片日期落在活动周期内，不自设该时间范围。","24:25",application=True),
        rule(s,"entry_sku","按本次应交照片门店逐店核对清晰可辨的SKU与申请名称所指条形码一致；允许同店多图补齐，不借其他门店照片。","24:25",application=True),
        rule(s,"entry_application_products","产品推广协议中的69码与本次系统申请条形码一致；仅有勤策已确认进场约定替代协议时依原条件使用，不伪造盖章协议。","24:25",application=True))
    namespace["RULE_POINTS"]={s:{r.id:points.get(s,{}).get(r.id,SOURCE_ROWS[s]) for r in rs if r.id not in {p.id for p in pos_rules}} for s,rs in namespace["RULES"].items()}
    namespace["APPLICATION_RULES"]=frozenset(app_rules)
    namespace["APPLICATION_COMPARISON_RULES"]=frozenset(comparison_rules)
    namespace["CONTRACT_COMPARISON_RULES"]={s:frozenset({"gift_rule_quantity"}) for s in ("self_procured_gift_material","giveaway_promotion")}
    from .comparison_policy import SETTLEMENT_OPERATORS
    namespace["COMPARISON_OPERATORS"].update(
        claim_ceiling={"amount":"eq"}, contract_budget_amount={"amount":"eq"},
        application_claim_ceiling={"amount":"le"}, settlement_calculation={"amount":SETTLEMENT_OPERATORS},
        invoice_amount={"amount":"le"}, material_list_amount={"amount":"le"},
        staff_headcount={"quantity":"eq"}, staff_total_unit_price={"unit_price":"eq"},
        gift_rule_quantity={"quantity":"le"}, pos_sales_amount={"amount":"le"}, target_reward={"amount":"eq"})
    namespace["PERSONNEL_CONTRACT_COMPARISON_RULES"]=frozenset()
    namespace['UNIT_PRICE_CLARIFICATION']='新表指定与系统申请对照的申报单价，逐商品/项目及单位按原精度相等比较，不能拿合同或POS零售价自证申请。临促申请单价=结算单日工资×执行天数，单独核对，不把人数当商品销量；缺申请字段须说明无法核对，不设容差。'
    for name in ("BRAND_CONTRACT_CLARIFICATION","PERSONNEL_CONTRACT_CLARIFICATION","SKU_CONTRACT_CLARIFICATION"):
        namespace[name]=APPLICATION_CLARIFICATION
    namespace["STAFF_PHOTO_CLARIFICATION"]=PERSONNEL_CLARIFICATION
    namespace["PAYMENT_COMPANY_CLARIFICATION"]="销售提成红包截图包含参半/小箭头/重点品牌字样，取消大额红包经销商公司名称的旧门槛；批量账单的经销商名称与申请另行核对。"
    namespace["PAYMENT_CLARIFICATION"]=PERSONNEL_CLARIFICATION + (
        "同一笔付款的发出记录、收款确认、重叠截图只计一次；先联合查看全部相关截图，不按照片张数计算笔数。"
        "不同付款即使金额相同也分别计算；逐笔原值引用，由程序求和，不任意挑选红包凑数。归属或金额不能确认时具体说明。")
    namespace["AMOUNT_CLARIFICATION"]=(
        "本次有效付款、票据、制作清单、结算计算金额合计须大于等于实际核销金额，100元核销对应100、100.1、101元支撑金额可通过，99.99元不足。"
        "申请金额与批复金额不混用。合同预算可与实际申请总额或程序完整复算的本次关联申请合计核对，绝不要求另造独立单头字段。批复金额仅实际有效取得时再核对，不把审批前尚未形成的最终金额列为客户问题。"
        "POS列示金额≥数量×销售单价、金额合计≥明细金额之和，数量合计相等。"
        "原值用十进制精度比较，无一分钱或比例容差；单价相等和批复相等不因抹零放宽，金额支撑项允许少报但不允许超过原精度支撑金额。")
    from .qince_amount_basis import APPLICATION_TOTAL_CLARIFICATION
    namespace["AMOUNT_CLARIFICATION"] += APPLICATION_TOTAL_CLARIFICATION
    old_size_tail="用户2026-09-22进一步确认：本类已有审核需要的数量、单价、门店和执行期间同样从品牌方本次合同读取。"
    namespace["DISPLAY_SIZE_CLARIFICATION"]=namespace["DISPLAY_SIZE_CLARIFICATION"].replace(old_size_tail,"")
    namespace["RULES"]["promotional_display"]=tuple(
        replace(r,text=r.text.replace(old_size_tail,"")) if r.id=="display_size" else r
        for r in namespace["RULES"]["promotional_display"])
    from .qince_amount_basis import OPTIONAL_REFERENCE_RULES
    for scenario, registered in namespace['RULES'].items():
        namespace['RULES'][scenario] = tuple(
            replace(r, when=f'available_reference:{r.id}:{r.when}')
            if r.id in OPTIONAL_REFERENCE_RULES else r for r in registered)
    from .qince_amount_basis import CONTRACT_STANDARD_RULES
    for scenario, registered in namespace['RULES'].items():
        namespace['RULES'][scenario] = tuple(replace(r, text=r.text +
            ' 勤策本单该字段有有效值时以其为准精确比对；该字段为空时跳过勤策对齐，继续按本次实际合同约定核对结算，不要求补勤策字段。'
            ' 计划销量、预计名额不能当作实际销量或实际达成人数，只有相同含义、对象和单位的数值才比较。')
            if r.id in CONTRACT_STANDARD_RULES else r for r in registered)
    namespace["SEAL_CLARIFICATION"]=(
        "新表H列所列结算单、促销合同、POS及批量微信账单须有本次经销商实际盖章，"
        "不区分公章、财务章、合同章、结算单章、业务章、部门章或其他章种；"
        "只要文件上有任何实际印章即视为已盖章，不因无法区分章种认定不合格或无法核验。"
        "仍须核对印章主体能否对应本单经销商。整份文件没有任何实际印章或签字图形时，"
        "直接记录未盖章/未签章，不能把印刷公司名称、标题、已填写内容或另一份已盖章文件当作本文件已盖章。"
        "原文明写的客户盖章、双方盖章仍按原要求核对，不能把双方盖章改为任意一方盖章。")
    namespace["AUDIT_SCOPE_CLARIFICATION"]=WORKBOOK_CLARIFICATION
    namespace["PRICE_SUPPORT_CLARIFICATION"]="补差以新表H19:H20执行，结算SKU销量与POS Excel销量相等，覆盖旧上限口径；活动价格须逐张核对，与合同促销价一致，不能用其他照片清楚的价格自动豁免该图；照片日期和活动价独立判断，整数价格不等于不清楚，无价签或遮挡定位具体照片；照片及结算周期分别对照系统申请，日期含起止当日，不能将另一张照片的日期移用为本图日期；销售预估数量与实际补差计费数量可以不同，分别按字段含义比较，原值与全部来源保留。"
    namespace["INVOICE_DETAILS_CLARIFICATION"]=POSTER_CLARIFICATION
    namespace["POS_CLARIFICATION"]="须交POS的分支同时提供盖章版及Excel版；纯临促工资、培训免POS。数据以Excel原值为准，盖章版另按新表核对实际盖章（不区分章种）及经销商名称，不用它补Excel缺项或另立两版数据冲突。"
    namespace["GIFT_CONTRACT_CLARIFICATION"]="赠送/搭赠规则从本次实际约定读取，POS提供实际销售数量/金额，结算提供申报赠送数量；按应赠数量≥申报数量比较，不用采购发票数量冒充实际赠出数量，不自定取整公式。"
    namespace["PHOTO_WATERMARK_CLARIFICATION"]=(
        "按新表各类已有照片要求核对日期、地址、拍摄时间，活动周期及门店分别引用系统申请；不能从文件名补写水印。"
        "日期、价格、画面分别判断，照片模糊或部分被遮挡具体定位；小票不套照片水印要求，补差线上小象截图例外保留。"
        "条码费不新增照片日期必须落在活动周期内的要求；临促每日、培训每场、物料制作每场照片分别执行，不跨分支套用。"
        + namespace["PHOTO_TIME_PRESENCE_CLARIFICATION"])
    # Kept visual checks were constructed with the previous clarifications.
    # Replace embedded superseded wording as well as exported constants.
    for scenario, scenario_rules in namespace["RULES"].items():
        namespace["RULES"][scenario] = tuple(replace(r, text=r.text
            .replace(old_watermark,namespace["PHOTO_WATERMARK_CLARIFICATION"])
            .replace(old_price,namespace["PRICE_SUPPORT_CLARIFICATION"])
            .replace("不从资料栏另加促销内容、客户自采物料或活动期间检查。", "活动门店现场内容与申请周期另按本类对应要点核对。")) for r in scenario_rules)


def extra_applicability(when: str, scenario: str, flags: dict):
    from .fee_policy_nov17 import extra_applicability as latest_applicability
    latest = latest_applicability(when, scenario, flags)
    if latest is not NotImplemented:
        return latest
    if when.startswith('available_reference:'):
        _, rule_id, original = when.split(':', 2)
        if flags.get('_reference_available', {}).get(rule_id) is False:
            return False
        from .pdf_policy import applicability
        return applicability(original, scenario, flags)
    if when == 'recorded_approval_applicable':
        if flags.get('_recorded_approval') is False:
            return False
        return extra_applicability('approved_claim_applicable', scenario, flags) if scenario == 'personnel_incentive' else True
    if when in {"personnel_pos","personnel_sales"}:
        if scenario != "personnel_incentive":return False
        if flags.get("sales_commission") is True or flags.get("store_incentive") is True:return True
        if flags.get("training") is True or flags.get("temporary_staff") is True:return False
        # Only a confirmed wage/training branch waives the ordinary POS list.
        # A generic personnel incentive must not waive POS by setting all flags false.
        return None if flags.get('sales_commission') is None else True
    if when == "personnel_sku":
        if flags.get("store_incentive") is True and flags.get("sales_commission") is not True:return False
        return extra_applicability("personnel_sales",scenario,flags)
    if when == "personnel_not_training":
        if flags.get("sales_commission") is True or flags.get("temporary_staff") is True or flags.get("store_incentive") is True:return True
        return False if flags.get("training") is True else None
    if when == "approved_claim_applicable":
        if flags.get("training") is True or extra_applicability("personnel_sales",scenario,flags) is True:return True
        return False if flags.get("temporary_staff") is True else None
    if when == "incentive_or_training_payment":
        if flags.get("red_packet") is True or flags.get("training") is True:return True
        return False if flags.get("temporary_staff") is True else flags.get("red_packet")
    if when == "material_production":
        value=flags.get("poster_posting")
        return not value if value is not None else None
    return NotImplemented
