"""The eight material lists and audit points in the user-approved one-page PDF.

This catalogue is the allow-list for new audits. Historical processors are not
part of this policy. Presence of a file is distinct from validity of its contents.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


POLICY_VERSION = "2026-09-18.pdf.3"
SOURCE_TITLE = "费用核销类型-资料与标准清单-20260918.pdf"


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    when: str = "always"


@dataclass(frozen=True)
class Rule:
    id: str
    text: str
    effect: str = "missing_material"
    when: str = "always"
    numeric: bool = False


FLAGS = (
    "training", "temporary_staff", "atrium", "cvs_otc", "online",
    "full_reduction", "uses_pos", "photo_evidence", "red_packet",
)

BASE_MATERIALS = (
    Requirement("settlement", "结算单（公司统一模板，客户盖章）"),
    Requirement("promotion_contract", "签章后的促销合同"),
)
POS_MATERIAL = Requirement("pos", "经销商盖章 POS 数据")
GIFT_RULE = Requirement("gift_rule", "核销时备注清楚活动赠送规则")
INVOICE = Requirement("invoice", "发票或收据，抬头为公司名称，含明细、金额、开票时间")

MATERIALS = {
    "promotional_display": (
        Requirement("display_photos", "活动期间带地址、拍摄时间水印的照片，清晰展示全貌、产品摆放、促销标识"),
        Requirement("atrium_agreement", "商场中庭大型活动现场展示的商场入场协议（仅大型活动现场需要）", "atrium"),
        *BASE_MATERIALS,
    ),
    "personnel_incentive": (
        Requirement("pos", "经销商盖章 POS 数据", "not_training"),
        Requirement("pos_excel", "POS 数据 Excel（培训费可不提供）", "not_training"),
        Requirement("payment", "红包截图或临促工资打款截图（有无临促依据活动申请说明字段确认）"),
        Requirement("staff_photos", "临促活动期内每一天的水印照片，含临促人员和产品（有无临促依据活动申请说明字段确认）", "temporary_staff"),
        *BASE_MATERIALS,
    ),
    "poster_material": (
        INVOICE,
        Requirement("finished_photos", "成品水印照片，地址和拍摄时间，展示尺寸、内容、展示位置"),
        *BASE_MATERIALS,
    ),
    "self_procured_gift_material": (
        INVOICE, GIFT_RULE, POS_MATERIAL,
        Requirement("gift_finished_photos", "包含外采赠品的成品水印照片"),
        Requirement("gift_store_photos", "活动期间所有门店现场水印照片，清晰展示促销内容和客户自采物料"),
        *BASE_MATERIALS,
    ),
    "price_difference_support": (
        POS_MATERIAL,
        Requirement("price_evidence", "所有活动门店清晰展示活动价格的现场水印照片，或线上小象超市截图"),
        Requirement("full_reduction_evidence", "满减返还活动存在证明（现场水印照片、小票等，须对应本次费用，可与价格证明共用同一来源）"),
        *BASE_MATERIALS,
    ),
    "giveaway_promotion": (
        POS_MATERIAL, GIFT_RULE,
        Requirement("giveaway_evidence", "全量活动水印照片或小票二选一；照片展示搭赠现场或消费者参与；小票显示购买产品、搭赠产品、购买时间、门店"),
        *BASE_MATERIALS,
    ),
    "entry_fee": (
        Requirement("entry_agreement", "产品推广协议（公司统一标准模板，双方盖章）"),
        Requirement("entry_system", "带公司进场产品的 POS 或终端库存表（适用于 CVS/OTC 渠道）", "cvs_otc"),
        Requirement("shelf_photos", "日期、地址、店名水印照片，清晰可辨具体 SKU"),
    ),
    "pos_target_incentive": (
        POS_MATERIAL,
        Requirement("pos_excel", "POS 数据 Excel（POS达标激励必交，无培训豁免）"),
        *BASE_MATERIALS,
    ),
}

COMMON_RULES = (
    Rule("submission_deadline", "核销自活动结束日起3个月内提交，超期不再核销。月份按日历月；必须有真实活动结束与提交日期，不能拿文件名或分析日期代替。进场费按原文“无时间要求”不作活动时限拒付，仍检查水印日期是否存在。", "reject", "activity_deadline"),
    Rule("review_deadline", "财务一审已超2个月且资料有误或缺失，不再补，直接0核销；只有同时具备时间和错误/缺失证据才触发。起算事件不清不能猜测。", "zero"),
    Rule("scene_authenticity", "确保费用场景真实；张冠李戴不予核销，额度不返还。仅有模糊或读不清不能认定张冠李戴。", "reject"),
    Rule("fraud", "有充分证据确认核销造假时0核销并记录罚款要求（先费用池后经销商额度）；不推测造假，不执行罚款、扣款或通知。", "zero"),
    Rule("bi_quota", "BI额度不足不允许先做活动后补申请；以业务发生时的额度与申请、活动时间证据判断，不能用当前余额代替。", "reject"),
)
BASE_RULES = (
    Rule("settlement_template_seal", "结算单须为公司统一模板并由客户盖章。"),
    Rule("contract_signed", "促销合同已签章。"),
)
POS_RULES = (
    Rule("pos_fields", "POS必含5+2：活动时间、SKU/69码、商品名称、销售数量、销售单价、销售金额、销售金额及数量合计；缺项即不完整。", when="uses_pos"),
    Rule("pos_seal", "要求提交的POS数据有经销商盖章。", when="required_pos"),
    Rule("pos_arithmetic", "销售金额=数量×销售单价，并核对销售金额、数量合计。", when="uses_pos", numeric=True),
    Rule("application_quantity_price", "数量、单价与申请不符或填错，直接0核销。必须区分销售单价与奖励/补差单价，不把实际销量低于计划自行解释为填错；比较口径不清记无法核验。", "zero", "uses_pos", True),
)
SKU_RULES = (
    Rule("reward_unit_price", "活动申请对应69码的奖励/补差单价与结算单奖励单价一致；不是将POS零售价与奖励单价比较。", "zero", "not_training", True),
    Rule("settlement_pos_quantity", "结算单每个SKU销售数量与POS对应销售数量一致。", "zero", "not_training", True),
    Rule("claim_ceiling", "结算单本次核销金额小于等于申请金额。", "missing_material", "always", True),
)

RULES = {
    "promotional_display": (
        *BASE_RULES, *POS_RULES,
        Rule("display_watermark", "照片拍摄日期在执行周期内，地址与活动门店一致，日期和地址缺一即不完整。"),
        Rule("display_brand", "申请的堆头或陈列大小范围内只能存在参半品牌，不能有其他产品；不得把申请区域外相邻货架算为混摆。"),
        Rule("display_material_brand", "堆头陈列物料元素为小阔集团产品（参半、重点、小箭头）。"),
        Rule("atrium_agreement", "商场中庭大型活动现场展示有商场入场协议；没有则缺资料。仅大型活动现场要求此项。", when="atrium"),
        Rule("display_quantity_price", "数量、单价等明细与申请填错，直接0核销。", "zero", numeric=True),
        Rule("display_size", "按照申请的堆头大小与现场照片判断是否符合；没有可用规格或尺度时明确无法核验，不使用旧固定面积、列数标准。"),
    ),
    "personnel_incentive": (
        *BASE_RULES, *POS_RULES, *SKU_RULES,
        Rule("pos_excel_present", "非培训人员激励提供POS数据Excel；未提供为缺资料。", when="not_training"),
        Rule("payment_evidence", "提供红包截图或临促工资打款截图；无对应支付证明为缺资料。"),
        Rule("payment_company", "大额红包>1000元要求写公司名称；缺名称为缺资料。工资打款不套红包公司备注条件。", when="red_packet"),
        Rule("payment_application_amount", "红包截图资料要求金额>=活动申请金额；按原文核验，不把该要求改成核销金额。与另一金额要求不能同时成立时明确列出原文口径冲突。", when="red_packet", numeric=True),
        Rule("payment_claim_amount", "红包金额与本次核销金额一致，否则缺资料。", when="red_packet", numeric=True),
        Rule("staff_daily_photos", "依据活动申请说明有无临促字段确认适用；临促活动期间每一天均有含临促人员和产品的水印照片，少任意一天即缺资料。", when="temporary_staff"),
    ),
    "poster_material": (
        *BASE_RULES,
        Rule("invoice_details", "发票/收据抬头为经销商公司，包含明细、金额和开票时间。"),
        Rule("finished_photo", "有成品水印照片（地址+拍摄时间），展示尺寸、内容、展示位置；无成品照为缺资料。"),
        *POS_RULES,
    ),
    "self_procured_gift_material": (
        *BASE_RULES, *POS_RULES,
        Rule("invoice_details", "发票或收据抬头为公司名称，包含明细、金额、开票时间。"),
        Rule("gift_rule_present", "核销时备注清楚活动赠送规则，未备注为缺资料。"),
        Rule("gift_rule_quantity", "按照赠送规则核验赠送赠品数量；按单或累计等计数口径必须来自实际规则，不能自行设置。", numeric=True),
        Rule("gift_finished_photos", "按门店提供全量成品水印照片，包含外采赠品。"),
        Rule("gift_all_store_photos", "活动期间所有门店现场水印照片清晰展示促销内容和客户自采物料。"),
    ),
    "price_difference_support": (
        *BASE_RULES, *POS_RULES, *SKU_RULES,
        Rule("price_evidence", "所有活动门店现场照片有水印且活动价格清晰，或提供线上小象超市截图；线上证据不套线下照片水印格式。"),
        Rule("full_reduction_evidence", "核验满减返还的现场水印照片、小票等活动存在证明，须对应本次费用；无对应证明为缺资料。此项必须核验，不能以是否满减无法确定为由跳过或中止审核。"),
    ),
    "giveaway_promotion": (
        *BASE_RULES, *POS_RULES, *SKU_RULES,
        Rule("gift_rule_present", "核销时备注清楚活动赠送规则。"),
        Rule("giveaway_evidence", "全量活动水印照片或小票二选一。照片展示搭赠现场或消费者参与；小票显示购买产品、搭赠产品、购买时间、门店。两者均无或关键信息缺失为缺资料；不要求同时提供照片和小票，不增加零金额赠品行、出库单和阶梯资格要求。"),
    ),
    "entry_fee": (
        *POS_RULES,
        Rule("entry_agreement", "产品推广协议为公司统一标准模板且双方盖章，否则缺资料。"),
        Rule("entry_sku", "水印照片清晰可辨具体SKU，并与活动申请一致；无法辨识则缺资料。"),
        Rule("entry_watermark", "水印包含日期、地址、店名，门店地址一致。原文无时间要求，不额外要求照片处于活动周期。"),
        Rule("entry_system", "CVS/OTC渠道系统性资料含公司进场产品的POS或终端库存表。", when="cvs_otc"),
        Rule("entry_duplicate", "存量进场记录检查是否重复；只在具有历史业务依据时判断，不凭同商品多店上架认定重复。"),
    ),
    "pos_target_incentive": (
        *BASE_RULES, *POS_RULES, *SKU_RULES,
        Rule("pos_excel_present", "POS达标激励须提供POS数据Excel；未提供为缺资料，无培训豁免。"),
    ),
}


MATERIAL_RULE_IDS = {
    "settlement": "settlement_template_seal", "promotion_contract": "contract_signed",
    "pos": "pos_fields", "pos_excel": "pos_excel_present", "payment": "payment_evidence",
    "staff_photos": "staff_daily_photos", "display_photos": "display_watermark",
    "atrium_agreement": "atrium_agreement", "invoice": "invoice_details",
    "finished_photos": "finished_photo", "gift_rule": "gift_rule_present",
    "gift_finished_photos": "gift_finished_photos", "gift_store_photos": "gift_all_store_photos",
    "price_evidence": "price_evidence", "full_reduction_evidence": "full_reduction_evidence",
    "giveaway_evidence": "giveaway_evidence", "entry_agreement": "entry_agreement",
    "entry_system": "entry_system", "shelf_photos": "entry_watermark",
}


def applicability(when: str, scenario: str, flags: dict) -> bool | None:
    if when == "always":
        return True
    if when == "activity_deadline":
        return scenario != "entry_fee"
    if when == "not_training":
        if scenario != "personnel_incentive":
            return True
        return not flags["training"] if flags.get("training") is not None else None
    if when == "required_pos":
        if scenario == "personnel_incentive":
            return applicability("not_training", scenario, flags)
        return scenario in {"self_procured_gift_material", "price_difference_support", "giveaway_promotion", "pos_target_incentive"}
    return flags.get(when)


def requirements(scenario: str) -> tuple[Requirement, ...]:
    return MATERIALS[scenario]


def rules(scenario: str) -> tuple[Rule, ...]:
    return (*COMMON_RULES, *RULES[scenario])


def catalogue() -> dict:
    from .scenario_registry import SCENARIO_LABELS
    return {"version": POLICY_VERSION, "source": SOURCE_TITLE, "page": 1,
            "types": {key: {"label": SCENARIO_LABELS[key],
                            "materials": [asdict(r) for r in value],
                            "audit_points": [asdict(r) for r in rules(key)]}
                      for key, value in MATERIALS.items()}}


def material_catalogue() -> dict:
    """Only the PDF material column is used to compare candidate types."""
    policy = catalogue()
    return {**policy, "types": {
        scenario: {"label": entry["label"], "materials": entry["materials"]}
        for scenario, entry in policy["types"].items()
    }}
