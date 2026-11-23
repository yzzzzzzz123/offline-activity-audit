"""Plain Chinese projections of source-backed results, shared by UI and callback.

This layer changes wording only. Decisions and original evidence remain intact.
"""
from __future__ import annotations

from decimal import Decimal
import re
from collections.abc import Sequence

from .pdf_evidence import _number as evidence_number
from .pdf_policy import MATERIAL_LABELS

SYSTEM_FAILURE_MESSAGE = "这次系统处理没有完成，暂时不能提供完整核销结果。请联系工作人员查看。"

CUSTOMER_PROBLEM_STYLE = (
    '\n客户问题写法：reason是给客户直接看的话，通常一两句，先说哪个对象有什么问题。'
    '例如“照片里的甲店，暂时对不上本次活动门店名单。”'
    '不要接着罗列合同分店、申请字段、候选排除过程、已通过的日期，也不要补“问题在……不是……”等自我解释。'
    '确有两家分店无法区分时，只保留帮助客户辨认的必要店名；完整候选、来源和对应过程放独立证据或技术记录。'
    '名称差异能由本单资料合理对应且没有冲突时，不因简称或详细地址不全列问题。'
    '日期、数量、单价或金额确有差异时，保留必要的双方原值与具体问题；看不清、未取得和确实不符不能混写。'
    '用日常中文，不用“现有字段”“对应关系未建立”“分支依据”“证据闭环”等程序说法。'
    '通过项不进入问题摘要，同一照片已通过的检查不要夹在问题说明中。'
    '一条只讲一件事：同一问题的现象、原因和影响一两句说清，不换说法反复解释，'
    '不把同一问题按照片、商品或表格行逐个重复写；同类问题可概括范围，必要时举例，完整明细留技术记录。'
    '不同问题用换行分开，不能用分号把不同检查项拼成大段；同一问题的必要原因和双方原值保留在一起。'
    '不同对象有不同日期、金额、规格或处理要求时须分别说清，不能仅因同属一项规则就合并或漏掉。'
    '不按字数截断，不为篇幅触发失败或新增模型润色调用。'
    '不省略任何实际审核项、照片或证据，不改原值与业务结论，不额外要求人工复核或补交资料。\n'
)


def basename(value: str) -> str:
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


POS_PAIR_REASON = "销售明细的Excel版和盖章版未交齐。"


def concise_reason(value: str, *, archive: str = "", label: str = "", files: Sequence[str] = ()) -> str:
    """Remove report wrappers, retaining the actual problem and business locators."""
    value = str(value or "").strip()
    prefixes = [name for name in (archive, basename(archive), label) if name]
    while True:
        previous = value
        for prefix in prefixes:
            value = re.sub(r"^" + re.escape(prefix) + r"\s*[：:]\s*", "", value)
        if value == previous:
            break
    # Filenames are facts, even when they contain one of the words simplified below.
    protected = {}
    for name in sorted({name for source in files for name in (source, basename(source)) if name}, key=len, reverse=True):
        if name in value:
            token = f"\ue100{len(protected)}\ue101"
            protected[token] = basename(name)
            value = value.replace(name, token)
    value = re.sub(r"(^|[；，。])缺少资料\s*[：:]\s*", r"\1缺少", value)
    value = re.sub(r"(^|[；，。])多出的资料\s*[：:]\s*", r"\1多交了", value)
    # Older snapshots kept this wrapper after simplifying nested POS parentheses.
    value = re.sub(r"^POS）（", "", value)
    value = value.replace("盖章版销售明细（POS）", "盖章版销售明细")
    value = value.replace("销售明细（POS）", "销售明细")
    unclear = re.match(r"^尚不能确认的资料或适用情况[：:]\s*(.*?)（(.*)）[。\s]*$", value, re.S)
    if unclear:
        value = unclear.group(2).strip()
        if re.fullmatch(r"(?:待确认|无法确认|不能确认|条件不明|资料不清楚)[。\s]*", value):
            value = "无法确认" + unclear.group(1).strip()
    else:
        value = re.sub(r"^尚不能确认的资料或适用情况[：:]\s*", "无法确认", value)
    if re.fullmatch(r"(?:POS明细|销售明细)须成套提供Excel版和盖章版，已提供一版时不能免交另一版[。\s]*", value):
        value = POS_PAIR_REASON
    # This saved gate explanation states a condition is unknown, not that photos
    # are missing. Retain that distinction without repeating audit instructions.
    if value == ("未见临促现场照片；本次促销合同未提供，结算单未列活动申请说明或临促安排，"
                 "无法确认有无临促及照片是否必交，不能推定豁免或具体缺少的门店日期。"):
        value = "未提供促销合同，无法确认是否安排临促、是否需要临促照片。"
    for token, name in protected.items():
        value = value.replace(token, name)
    value = value.strip(" ，,；;：:。")
    return value + "。" if value else ""


def plain_text(value: str, units: list[dict]) -> str:
    """Keep submitted names intact while translating known internal vocabulary."""
    names = {}
    for unit in units:
        source = unit["source_file"]
        for name in (source, basename(source)):
            names[name] = basename(source)
    for unit in units:
        # Use only the actual render filename, not guessed extensions. Original
        # submitted names take precedence if a render alias happens to collide.
        display = basename(unit["source_file"])
        names.setdefault(unit["unit_id"], display)
        if unit.get("image"):
            names.setdefault(basename(unit["image"]), display)
    protected = []

    def protect(match):
        protected.append(names[match.group(0)])
        return f"\ue000{len(protected) - 1}\ue001"

    # Material reasons contain source-ID lists. Several IDs may be reading
    # chunks of one original workbook; display that file once, preserving the
    # complete source-ID list and all evidence outside this wording projection.
    by_id = {unit["unit_id"]: unit["source_file"] for unit in units}
    if by_id:
        ids = "(?:" + "|".join(re.escape(uid) for uid in sorted(by_id, key=len, reverse=True)) + ")"

        def protect_file_list(match):
            sources = dict.fromkeys(by_id[uid] for uid in re.findall(ids, match.group(0)))
            tokens = []
            for source in sources:
                protected.append(basename(source))
                tokens.append(f"\ue000{len(protected) - 1}\ue001")
            return "、".join(tokens)

        value = re.sub(r"(?<![A-Za-z0-9_.-])" + ids + r"(?:\s*、\s*" + ids + r")+(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])",
                       protect_file_list, value)

    if names:
        pattern = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
        value = re.sub(r"(?<![A-Za-z0-9_.-])(?:" + pattern + r")(?![A-Za-z0-9_-]|\.[A-Za-z0-9_])", protect, value)

    def protect_pos_label(match):
        # A quoted source header such as “含税pos” is not the internal `pos`
        # material ID. Keep the observed wording instead of inventing a label.
        label = match.group(0)
        if re.search(r"[\u4e00-\u9fff]", label) and re.search(r"(?<![A-Za-z0-9_])pos(?![A-Za-z0-9_])", label, re.I):
            protected.append(label)
            return f"\ue000{len(protected) - 1}\ue001"
        return label

    value = re.sub(r'“[^”\n]{1,120}”|「[^」\n]{1,120}」|"[^"\n]{1,120}"', protect_pos_label, value)
    conditions = {
        "uses_pos": "这次是否使用销售明细", "training": "这次是否属于培训费",
        "store_incentive": "这次是否属于门店或区域达成奖励",
        "temporary_staff": "申请是否说明有临时促销人员", "atrium": "这次是否属于商场中庭大型活动",
        "cvs_otc": "这次是否属于CVS/OTC渠道", "online": "这次是否为线上补差",
        "full_reduction": "这次是否涉及满减返还", "photo_evidence": "这次是否用活动照片证明",
        "red_packet": "这次是否用红包截图证明",
    }
    for field, label in conditions.items():
        value = re.sub(rf'(?<![A-Za-z0-9_]){field}["\s]*(?:为|=|:|：|是)\s*(?:null|None)(?![A-Za-z0-9_])',
                       label + "还不清楚", value)
        value = re.sub(rf"(?<![A-Za-z0-9_]){field}(?![A-Za-z0-9_.])", label, value)
    for field, label in MATERIAL_LABELS.items():
        value = re.sub(rf"(?<![A-Za-z0-9_]){field}(?![A-Za-z0-9_.])", label, value)
    value = re.sub(r"(?<![A-Za-z0-9_])(?:null|None)(?![A-Za-z0-9_.])", "未确认", value)
    replacements = {
        "无法核验": "暂时不能确认", "未能核验": "暂时不能确认", "核验": "核对",
        "字段完整性": "信息是否齐全", "非数值内容": "文字内容", "数值一致性": "数字是否相同",
        "内容一致性": "内容是否相同", "口径不一致": "比较的内容或单位不同",
        "口径不清": "比较的内容或单位还不清楚", "同口径": "同一范围和单位",
        "证据不足": "目前资料还不能说明", "核对依据": "比较所需的信息",
        "水印字段": "照片上标注的信息", "SKU销售数量": "各商品的销售数量",
        "POS两版": "销售明细的Excel版和盖章版", "POS盖章版": "销售明细盖章版",
        "POS电子表": "销售明细Excel表", "POS明细": "销售明细（POS）",
        "POS数据": "销售明细（POS）", "直接0核销": "记录具体问题，交由人工复核",
        "不予核销": "需要人工复核", "核销失败": "发现需人工复核的问题",
    }
    for old, new in replacements.items():
        value = value.replace(old, new)
    # Translate the term, preserving product identifiers such as SKU-9999.
    value = re.sub(r"(?<![A-Za-z0-9_-])SKU(?![A-Za-z0-9_-])", "商品", value)
    for index, name in enumerate(protected):
        value = value.replace(f"\ue000{index}\ue001", name)
    return value.strip()


_UNCERTAIN_SUBJECTS = {
    "settlement_template_seal": "结算单是否使用公司统一模板，并盖有本次客户的章",
    "settlement_seal": "结算单是否盖有本次客户的章",
    "contract_signed": "促销合同是否已经签章",
    "pos_fields": "销售明细需要的信息是否写全",
    "pos_arithmetic": "销售明细的每行金额和合计是否算对",
    "pos_product_identity": "Excel销售明细中的商品名称和69码能否与商品库对应",
    "application_quantity_price": "本次申报数量和单价是否与系统活动申请相同",
    "reward_unit_price": "结算单的奖励或补差单价是否与系统活动申请相同",
    "settlement_pos_quantity": "结算单各商品的销量是否与销售明细相同",
    "claim_ceiling": "实际核销金额是否等于系统核销批复金额合计",
    "display_watermark": "陈列照片的水印日期、地址是否符合本次活动要求",
    "display_brand": "本次申请的陈列区域内是否只摆放参半商品",
    "display_material_brand": "陈列物料上的品牌是否符合要求",
    "atrium_agreement": "本次包含大型活动场地费时是否有对应的商场入场协议",
    "display_quantity_price": "结算单中的陈列数量和单价是否与系统活动申请相同",
    "display_size": "现场陈列大小是否符合申请中的规格",
    "payment_company": "激励付款截图是否有参半、小箭头或重点品牌字样",
    "payment_claim_amount": "本次有效付款合计是否足够支持实际核销金额",
    "staff_daily_photos": "每家临促门店每天是否都有带水印并展示临促人员和产品的照片",
    "invoice_details": "本次票据抬头或付款方是否对应申请经销商",
    "finished_photo": "是否提供了成品照片",
    "gift_rule_present": "本次活动赠送规则是否写清楚",
    "gift_rule_quantity": "按真实赠送规则计算的应赠数量是否不少于申报赠品数量",
    "gift_finished_photos": "各门店成品水印照片是否完整展示外采赠品",
    "price_evidence": "本次门店照片是否有水印且活动价格清楚，或线上截图的价格是否清楚",
    "full_reduction_evidence": "所交照片或小票等资料是否能证明本次满减返还活动",
    "giveaway_evidence": "本次活动照片或小票是否齐全，并显示搭赠所需的信息",
    "entry_agreement": "产品推广协议是否使用公司统一模板，并有双方盖章",
    "entry_sku": "现场照片中的商品是否与申请的进场商品相同",
    "entry_watermark": "进场照片水印中的地址是否对应本次门店",
    "entry_photo_coverage": "上架照片是否达到本次渠道要求的门店覆盖或照片数量",
    "entry_inventory": "终端库存明细是否覆盖全部进场门店及商品，并有盖章",
    "entry_duplicate": "本次核销单不同申报门店的照片水印是否使用了相同地址；已确认同店的多张照片不算地址重复",
}


def customer_reason(check: dict, units: list[dict]) -> str:
    raw = check["reason"]
    # Some outputs only wrap the policy sentence in “no evidence”, without a
    # single observed fact. Keep that uncertainty, but do not show the policy's
    # conditional penalties as though the customer had actually triggered them.
    first_requirement = str(check.get("requirement") or "").split("。")[0]
    if check["status"] == "unknown" and len(first_requirement) > 8 and first_requirement in raw:
        rest = raw.replace(first_requirement, "", 1)
        filler = re.sub(r"未提供|缺少|核验|核对|依据|证据|无法|不能|确认|的|[\s，。；：、]", "", rest)
        subject = _UNCERTAIN_SUBJECTS.get(check["rule_id"])
        if not filler and subject:
            return "目前资料还不能确认" + subject + "。"
    return plain_text(raw, units)


def _number(value: str | Decimal) -> str:
    text = format(Decimal(value), "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def numeric_problem(check: dict, units: list[dict]) -> str | None:
    """Explain only program-confirmed mismatches, never infer missing evidence."""
    by_id = {unit["unit_id"]: unit for unit in units}
    sentences = []
    cache_groups = {}
    for item in check.get("calculations") or []:
        if item["matches"]:
            continue
        if check['rule_id'] == 'pos_arithmetic':
            from .pos_arithmetic import is_binary_formula_cache_difference
            if is_binary_formula_cache_difference(item, units):
                source = by_id[item['left']['unit_id']]
                cache_groups.setdefault(basename(source['source_file']), []).append(item)
                continue
        left, right = _number(item["left_value"]), _number(item["right_value"])
        gap = _number(abs(Decimal(left) - Decimal(right)))
        label = plain_text(item["label"], units)
        source = by_id[item["left"]["unit_id"]]
        left_file = basename(source["source_file"])
        right_files = list(dict.fromkeys(basename(by_id[term["unit_id"]]["source_file"])
                                       for term in item["right"]))
        if item["operation"] == "value":
            right_file = right_files[0]
            if check["rule_id"] == "pos_numeric_consistency":
                left_label, right_label = f"盖章版《{left_file}》", f"Excel版《{right_file}》"
            elif source["source_file"] != by_id[item["right"][0]["unit_id"]]["source_file"]:
                left_label, right_label = f"《{left_file}》", f"《{right_file}》"
                if left_file == right_file:
                    left_label += f"中“{item['left']['quote']}”"
                    right_label = f"另一份《{right_file}》中“{item['right'][0]['quote']}”"
            else:
                # Same-file comparisons need the actual locators/quotes, not two
                # indistinguishable names or guessed business roles.
                left_label = f"《{left_file}》中“{item['left']['quote']}”"
                right_label = f"同一文件中“{item['right'][0]['quote']}”"
            detail = f"{left_label}写的是{left}，{right_label}写的是{right}"
        else:
            action = {"sum": "加总", "product": "相乘", "ratio": "按赠送比例计算",
                      "floor_ratio": "按规则中的完整赠送次数计算"}[item["operation"]]
            files = "、".join(f"《{name}》" for name in right_files)
            values = "、".join(_number(evidence_number(term["number"]))
                              if not re.search(r"[%％]", term["number"]) else term["number"]
                              for term in item["right"])
            if len(item["right"]) > 6:
                values = "各项数值"
            detail = f"《{left_file}》写的是{left}；根据{files}中的{values}{action}，结果应为{right}"
        if check["rule_id"] == "payment_claim_amount" and item["operator"] == "le":
            conclusion = f"本次付款合计不能少于本次核销金额，少了{gap}"
        elif item.get('value_kind') == 'quantity' and item['operator'] == 'le':
            subject = ('结算申报销量超过POS实际销量' if check['rule_id']=='settlement_pos_quantity' else
                       '申报赠品数量超过按规则计算的应赠数量' if check['rule_id']=='gift_rule_quantity' else
                       '前项数量超过后项数量')
            conclusion = f'{subject}，两处相差{gap}'
        elif item.get("value_kind") == "unit_price" and item["operator"] == "eq":
            reference = "系统申请" if "系统" in check.get("requirement", "") else "合同约定"
            subject = '合同奖励单价' if check['rule_id']=='contract_reward_unit_price' else '申报单价'
            conclusion = f"{subject}须与{reference}单价一致，相差{gap}"
        elif item["operator"] == "ge" and check["rule_id"] == "pos_arithmetic":
            conclusion = f"列示金额不能少于复算金额，少了{gap}"
        elif item["operator"] == "ge":
            conclusion = f"前项金额不能少于后项金额，少了{gap}"
        elif item["operator"] == "le":
            conclusion = f"本次核销金额不能超过申请金额，超出{gap}"
        else:
            conclusion = f"两处应一致，相差{gap}"
        sentences.append(f"{label}：{detail}。{conclusion}。")
    for filename, items in cache_groups.items():
        example = items[0]
        sentences.append(f'《{filename}》有{len(items)}处原生乘法公式的缓存金额与精确复算结果存在数值表示差异'
            f'（如{plain_text(example["label"], units)}：缓存{_number(example["left_value"])}，'
            f'精确复算{_number(example["right_value"])}）。这些原值差异及全部行号、公式仍保留在技术档案中，'
            '未仅据此认定客户少算金额；金额比较规则未放宽。')
    return "\n".join(sentences) or None
