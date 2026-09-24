"""Closed extraction contracts and source-bound PDF-policy decisions."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import re
from typing import Any

import jsonschema

from .common import AuditError
from .pdf_policy import (FLAGS, MATERIALS, MATERIAL_LABELS, MATERIAL_RULE_IDS, POLICY_VERSION,
                         COMPARISON_OPERATORS, CONTRACT_COMPARISON_RULES, CONTRACT_REFERENCE_RULES,
                         applicability, requirements, rules)


def obj(properties: dict) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


TEXT = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": TEXT}
IDS = {**STRINGS, "uniqueItems": True}
DOCUMENT = obj({"unit_id": TEXT, "roles": STRINGS, "facts": STRINGS, "limitations": STRINGS})
DOCUMENT_SCHEMA = obj({"documents": {"type": "array", "items": DOCUMENT}})
MATERIAL = obj({"id": TEXT, "state": {"type": "string", "enum": ["present", "missing", "unclear", "not_applicable"]},
                "reason": TEXT, "source_ids": IDS})
FLAGS_SCHEMA = obj({name: {"type": ["boolean", "null"]} for name in FLAGS})
EXTRA_MATERIAL = obj({"description": TEXT, "reason": TEXT, "source_ids": IDS})
MATERIAL_MATCH = obj({
    "scenario": {"type": "string", "enum": list(MATERIALS)},
    "supported": {"type": "boolean"}, "reason": TEXT, "source_ids": IDS,
    "flags": FLAGS_SCHEMA, "materials": {"type": "array", "items": MATERIAL},
    "extra_materials": {"type": "array", "items": EXTRA_MATERIAL},
})
CLASSIFICATION_SCHEMA = obj({
    "candidate_scenarios": {"type": "array", "uniqueItems": True,
                            "items": {"type": "string", "enum": list(MATERIALS)}},
    "reason": TEXT, "source_ids": IDS,
    "flags": FLAGS_SCHEMA,
    "materials": {"type": "array", "items": MATERIAL},
    "background_materials": {"type": "array", "items": EXTRA_MATERIAL},
    "material_matches": {"type": "array", "minItems": len(MATERIALS),
                         "maxItems": len(MATERIALS), "items": MATERIAL_MATCH},
})
BIZ_TYPE_SCHEMA = obj({
    "scenario": {"type": ["string", "null"], "enum": [*MATERIALS, None]},
    "reason": TEXT,
})


def classification_schema(selected_scenario: str | None = None, *, large_venue_fee: bool | None = None) -> dict:
    if large_venue_fee is not None and not isinstance(large_venue_fee, bool):
        raise AuditError("largeVenueFee 必须是布尔值 true 或 false")
    schema = deepcopy(CLASSIFICATION_SCHEMA)
    if selected_scenario is not None:
        if selected_scenario not in MATERIALS:
            raise AuditError("业务类型未对应已登记Skill")
        props = schema["properties"]
        props["candidate_scenarios"].update(minItems=1, maxItems=1)
        props["candidate_scenarios"]["items"]["enum"] = [selected_scenario]
        props["material_matches"].update(minItems=1, maxItems=1)
        props["material_matches"]["items"]["properties"]["scenario"]["enum"] = [selected_scenario]
        if selected_scenario == "promotional_display" and large_venue_fee is not None:
            for flags in (props["flags"], props["material_matches"]["items"]["properties"]["flags"]):
                flags["properties"]["atrium"] = {"type": "boolean", "enum": [large_venue_fee]}
    return schema


OPERAND = obj({"unit_id": TEXT, "quote": TEXT, "number": TEXT})
COMPARISON = obj({"label": TEXT,
                  "value_kind": {"type": "string", "enum": ["amount", "quantity", "unit_price"]},
                  "left": OPERAND,
                  "right": {"type": "array", "minItems": 1, "items": OPERAND},
                  "operation": {"type": "string", "enum": ["value", "sum", "product", "ratio", "floor_ratio"]},
                  "operator": {"type": "string", "enum": ["eq", "le", "ge"]}})
CHECK = obj({"rule_id": TEXT,
             "status": {"type": "string", "enum": ["pass", "fail", "unknown", "not_applicable"]},
             "reason": TEXT, "source_ids": IDS,
             "comparisons": {"type": "array", "items": COMPARISON}})
AUDIT_SCHEMA = obj({"checks": {"type": "array", "items": CHECK}})


def audit_schema(scenario: str) -> dict:
    """Constrain model output to this type's complete PDF audit list."""
    schema = deepcopy(AUDIT_SCHEMA)
    ids = [rule.id for rule in rules(scenario)]
    schema["properties"]["checks"].update(minItems=len(ids), maxItems=len(ids))
    schema["properties"]["checks"]["items"]["properties"]["rule_id"] = {"type": "string", "enum": ids}
    return schema


def validate(value: dict, schema: dict) -> None:
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except jsonschema.ValidationError as exc:
        # Avoid putting private documents/model output into public exception logs.
        raise AuditError("PDF规则证据结构无效：" + "/".join(map(str, exc.absolute_path))) from None


def validate_documents(value: dict, units: list[dict]) -> None:
    validate(value, DOCUMENT_SCHEMA)
    expected = [u["unit_id"] for u in units]
    actual = [d["unit_id"] for d in value["documents"]]
    if actual != expected:
        raise AuditError("材料识别必须按顺序完整覆盖每个原始文件/页面，不得遗漏、重复或增加来源")
    for document, unit in zip(value["documents"], units):
        if not unit.get("image"):
            if document["facts"] != unit["native_facts"] or document["limitations"] != unit["limitations"]:
                raise AuditError("程序读取的原始单元格及读取限制不得由模型改写")


def document_map(documents: list[dict]) -> dict[str, dict]:
    result = {d["unit_id"]: d for d in documents}
    if len(result) != len(documents):
        raise AuditError("材料事实包含重复来源")
    return result


def _sources(ids: list[str], documents: dict, *, required: bool = False) -> None:
    if set(ids) - set(documents):
        raise AuditError("证据引用了未读取的材料来源")
    if required and not ids:
        raise AuditError("确定的分类、材料或审核事实必须引用本次材料来源")


def _validate_material_list(scenario: str, flags: dict, materials: list[dict], sources: dict) -> None:
    wanted = {r.id for r in requirements(scenario)}
    actual = [r["id"] for r in materials]
    if set(actual) != wanted or len(actual) != len(wanted):
        raise AuditError("必须逐项覆盖每一类型的PDF资料清单，不能增加或省略资料要求")
    if scenario != "personnel_incentive" and (flags["training"] or flags["temporary_staff"]):
        raise AuditError("培训/临促例外只能用于人员激励")
    present_pos = any(m["id"] in {"pos", "pos_excel"} and m["state"] == "present" for m in materials)
    if present_pos and flags["uses_pos"] is not True:
        raise AuditError("已经提供POS资料时必须核验PDF的POS通用标准，不能自行免除")
    for item in materials:
        _sources(item["source_ids"], sources, required=item["state"] == "present")
        requirement = next(r for r in requirements(scenario) if r.id == item["id"])
        applies = applicability(requirement.when, scenario, flags)
        if item["state"] == "not_applicable" and applies is not False:
            raise AuditError("资料例外必须符合PDF明文条件，不能自行免交")
        optional_training_pos = requirement.when == "not_training" and scenario == "personnel_incentive"
        optional_pos_submission = optional_training_pos and (
            item["state"] == "present" or (present_pos and item["state"] in {"missing", "unclear"}))
        if applies is False and item["state"] != "not_applicable" and not optional_pos_submission:
            raise AuditError("不适用的条件资料不能并入匹配清单，须列清单外资料；仅培训POS和Excel明文可选")
        if applies is None and item["state"] != "unclear":
            raise AuditError("资料适用条件无法确定时不能声称已满足或已豁免")


def validate_classification(value: dict, documents: list[dict], selected_scenario: str | None = None,
                            *, large_venue_fee: bool | None = None) -> None:
    validate(value, classification_schema(selected_scenario, large_venue_fee=large_venue_fee))
    sources = document_map(documents)
    background_sources = set()
    for background in value["background_materials"]:
        _sources(background["source_ids"], sources, required=True)
        background_sources.update(background["source_ids"])
    matches = value["material_matches"]
    compared = [item["scenario"] for item in matches]
    expected = {selected_scenario} if selected_scenario else set(MATERIALS)
    if set(compared) != expected or len(compared) != len(set(compared)):
        raise AuditError("确定核销类型前必须逐一对照PDF八套资料清单，不得遗漏或重复类型")
    for item in matches:
        if (item["scenario"] == "promotional_display" and large_venue_fee is not None
                and item["flags"]["atrium"] is not large_venue_fee):
            raise AuditError("商场入场协议的适用条件必须采用接口largeVenueFee，不能由AI改写")
        _sources(item["source_ids"], sources, required=item["supported"])
        _validate_material_list(item["scenario"], item["flags"], item["materials"], sources)
        covered = {uid for material in item["materials"]
                   if material["state"] in {"present", "unclear"} for uid in material["source_ids"]}
        covered.update(background_sources)
        for extra in item["extra_materials"]:
            _sources(extra["source_ids"], sources, required=True)
            covered.update(extra["source_ids"])
        if covered != set(sources):
            raise AuditError("每类比对必须将全部已读来源归入对应资料项、可选业务背景或清单外资料，不得忽略多余资料")
        if item["supported"] != _exact_material_match(item):
            raise AuditError("资料项必须不多不少才能匹配类型；缺项、额外资料或资料适用条件不明均不能支持该类型")
    candidates = value["candidate_scenarios"]
    supported = {item["scenario"] for item in matches if item["supported"]}
    if set(candidates) != ({selected_scenario} if selected_scenario else supported):
        raise AuditError("核销类型候选必须与PDF资料清单逐类比对结果一致")
    _sources(value["source_ids"], sources, required=bool(candidates))
    if set(value["source_ids"]) != set(sources):
        raise AuditError("类型比对必须保留全部已读来源，包括未匹配及清单外资料")
    if len(candidates) != 1:
        if value["materials"]:
            raise AuditError("核销类型未唯一确定时不得虚构某一类型的资料核验结果")
        return
    selected = next(item for item in matches if item["scenario"] == candidates[0])
    if value["flags"] != selected["flags"] or value["materials"] != selected["materials"]:
        raise AuditError("所选类型的资料和适用条件必须沿用该类型已保存的清单比对结果")
    has_pos_source = any(m["id"] in {"pos", "pos_excel", "entry_system"} and m["state"] == "present"
                         for m in selected["materials"])
    if _exact_material_match(selected) and not has_pos_source and selected["flags"]["uses_pos"] is not False:
        raise AuditError("严格匹配清单内没有POS来源时不得额外启动POS通用审核")


def _exact_material_match(match: dict) -> bool:
    by_id = {item["id"]: item for item in match["materials"]}
    pos_pair = [by_id.get(name, {}).get("state") for name in ("pos", "pos_excel")]
    # Training may omit both versions, but cannot submit one and exempt the other.
    incomplete_pos_pair = "present" in pos_pair and pos_pair != ["present", "present"]
    return (not incomplete_pos_pair and not match["extra_materials"]
            and all(item["state"] in {"present", "not_applicable"} for item in match["materials"]))


def _classification_reasons(value: dict, selected_scenario: str | None = None) -> list[str]:
    from .scenario_registry import SCENARIO_LABELS
    candidates = value["candidate_scenarios"]
    if candidates and selected_scenario is None:
        labels = "、".join(SCENARIO_LABELS[scenario] for scenario in candidates)
        return [f"资料项比对仍同时匹配{labels}，无法唯一确定核销类型。", value["reason"]]
    reasons = []
    # Describe conditional gaps for every list supported by some actual material;
    # never pick a type by the fewest missing items. The full eight-list evidence
    # remains in material_matches even when a list has no related material.
    relevant = [match for match in value["material_matches"]
                if any(m["state"] == "present" or (m["state"] == "unclear" and m["source_ids"])
                       for m in match["materials"])]
    for match in relevant or value["material_matches"]:
        labels = MATERIAL_LABELS
        gaps = []
        for material in match["materials"]:
            if material["state"] in {"missing", "unclear"}:
                state = "缺少资料" if material["state"] == "missing" else "尚不能确认的资料或适用情况"
                detail = f"（{material['reason']}）" if material["state"] == "unclear" else ""
                gaps.append(f"{state}：{labels[material['id']]}{detail}")
        for extra in match["extra_materials"]:
            gaps.append(f"多出的资料：{extra['description']}（{'、'.join(extra['source_ids'])}；{extra['reason']}）")
        pair = [m["state"] for m in match["materials"] if m["id"] in {"pos", "pos_excel"}]
        if "present" in pair and pair != ["present", "present"]:
            gaps.append("POS明细须成套提供Excel版和盖章版，已提供一版时不能免交另一版")
        prefix = (f"{SCENARIO_LABELS[match['scenario']]}：" if selected_scenario
                  else f"如果申报的是{SCENARIO_LABELS[match['scenario']]}，")
        if selected_scenario:
            reasons.extend(prefix + gap for gap in gaps)
        else:
            reasons.append(prefix + "；".join(gaps))
    return reasons


def classification_decision(value: dict, documents: list[dict], selected_scenario: str | None = None,
                            *, large_venue_fee: bool | None = None) -> dict:
    validate_classification(value, documents, selected_scenario, large_venue_fee=large_venue_fee)
    candidates = [item["scenario"] for item in value["material_matches"] if _exact_material_match(item)]
    scenario = selected_scenario or (candidates[0] if len(candidates) == 1 else None)
    # Compare material items, not physical file counts. Content compliance is
    # audited only after one complete list accounts for all submitted material.
    matched = scenario is not None and scenario in candidates
    reasons = [] if matched else _classification_reasons(value, selected_scenario)
    return {"scenario": scenario, "matched": matched, "reasons": reasons,
            "classification": value, "policy_version": POLICY_VERSION}


def _number(raw: str) -> Decimal:
    text = str(raw).strip().replace(",", "").replace("，", "").replace("￥", "").replace("¥", "")
    percent = text.endswith("%") or text.endswith("％")
    text = text.rstrip("%％").removesuffix("元").strip()
    if len(text) > 100 or not re.fullmatch(r"[+-]?\d+(?:\.\d+)?", text):
        raise AuditError("数值证据不是可计算的十进制数")
    try:
        value = Decimal(text)
    except InvalidOperation:
        raise AuditError("数值证据不是可计算的十进制数") from None
    if not value.is_finite():
        raise AuditError("数值证据必须是有限数")
    return value / 100 if percent else value


def _operand(item: dict, documents: dict) -> Decimal:
    _sources([item["unit_id"]], documents, required=True)
    text = "\n".join(documents[item["unit_id"]]["facts"])
    quote = item["quote"]
    if quote not in text:
        raise AuditError("数值引用必须逐字来自本次已提取材料，不能补造申请值或计算依据")
    number = _number(item["number"])
    tokens = re.findall(r"(?<![A-Za-z0-9])[-+]?\d[\d,，]*(?:\.\d+)?\s*[%％]?", quote)
    if not any(_number(token) == number for token in tokens):
        raise AuditError("数值与引用的原文不一致")
    return number


def calculate(comparison: dict, documents: dict) -> dict:
    left = _operand(comparison["left"], documents)
    terms = [_operand(item, documents) for item in comparison["right"]]
    op = comparison["operation"]
    if op == "value":
        if len(terms) != 1:
            raise AuditError("直接比较只能引用一个对应值")
        right = terms[0]
    elif op == "sum":
        right = sum(terms, Decimal(0))
    elif op == "product":
        right = Decimal(1)
        for term in terms:
            right *= term
    else:
        if len(terms) != 3 or terms[1] <= 0 or any(term < 0 for term in terms):
            raise AuditError("赠送比例比较须提供非负实际数量、正数购买门槛及非负赠送数量")
        quotient = terms[0] / terms[1]
        if op == "floor_ratio":
            quotient = quotient.to_integral_value(rounding=ROUND_FLOOR)
        right = quotient * terms[2]
    matches = {"eq": left == right, "le": left <= right, "ge": left >= right}[comparison["operator"]]
    return {**comparison, "left_value": str(left), "right_value": str(right), "matches": matches}


def _validate_contract_comparison(rule_id: str, comparison: dict, materials: list[dict]) -> None:
    contract_sources = {uid for item in materials
                        if item["id"] == "promotion_contract" and item["state"] == "present"
                        for uid in item["source_ids"]}
    terms = comparison["right"]
    if rule_id == "gift_rule_quantity" and comparison["operation"] in {"ratio", "floor_ratio", "product"}:
        pos_sources = {uid for item in materials
                       if item["id"] == "pos_excel" and item["state"] == "present"
                       for uid in item["source_ids"]}
        if comparison["operation"] in {"ratio", "floor_ratio"}:
            if (len(terms) != 3 or terms[0]["unit_id"] not in pos_sources
                    or any(term["unit_id"] not in contract_sources for term in terms[1:])):
                raise AuditError("赠品数量的实际销量须引用本次POS Excel版，购买门槛和赠送数量须引用品牌方合同")
        elif (not any(term["unit_id"] in contract_sources for term in terms)
              or terms[0]["unit_id"] not in contract_sources | pos_sources
              or any(term["unit_id"] not in contract_sources for term in terms[1:])):
            raise AuditError("赠品乘积只允许首项引用POS Excel实际销量，其余参数必须来自品牌方合同")
    elif not contract_sources or any(term["unit_id"] not in contract_sources for term in terms):
        raise AuditError("本类已确认的申请参照值必须引用品牌方本次促销合同，不能以结算单或其他资料代替")
    if comparison["value_kind"] == "unit_price":
        claim_sources = {uid for item in materials
                         if item["id"] == "settlement" and item["state"] == "present"
                         for uid in item["source_ids"]}
        if comparison["left"]["unit_id"] not in claim_sources:
            raise AuditError("单价比较左侧须引用结算单申报单价，不能以合同约定或POS销售单价替代申报值")
    if rule_id == "gift_rule_quantity":
        invoice_sources = {uid for item in materials if item["id"] == "invoice"
                           for uid in item["source_ids"]}
        submitted_sources = {uid for item in materials
                             if item["id"] in {"settlement", "pos_excel"} and item["state"] == "present"
                             for uid in item["source_ids"]}
        left_source = comparison["left"]["unit_id"]
        stamped_sources = {uid for item in materials if item["id"] == "pos" and item["state"] == "present"
                           for uid in item["source_ids"]}
        if left_source in stamped_sources - submitted_sources:
            raise AuditError("赠品核对引用POS实际数据时以Excel版为准，不能引用盖章版替代")
        if left_source in (contract_sources | invoice_sources) - submitted_sources:
            raise AuditError("合同约定数或发票采购数不能当作经销商申报或实际赠出数量")


def _validate_payment_comparison(comparison: dict, materials: list[dict] | None) -> None:
    if comparison["operation"] != "sum":
        raise AuditError("本次红包金额须逐笔引用，由程序求和后与本次核销金额比较")
    if materials is None:
        return
    claim_sources = {uid for item in materials
                     if item["id"] == "settlement" and item["state"] == "present"
                     for uid in item["source_ids"]}
    payment_sources = {uid for item in materials
                       if item["id"] == "payment" and item["state"] == "present"
                       for uid in item["source_ids"]}
    if (comparison["left"]["unit_id"] not in claim_sources
            or any(term["unit_id"] not in payment_sources for term in comparison["right"])):
        raise AuditError("红包金额比较左侧须引用结算单本次核销金额，右侧须逐笔引用本次支付凭证，不能互换或改用合同额度")


def _validate_pos_comparison(comparison: dict, materials: list[dict] | None) -> None:
    operations = {"amount": {"product", "sum"}, "quantity": {"sum"}}
    if comparison["operation"] not in operations[comparison["value_kind"]]:
        raise AuditError("POS金额须引用逐行乘算或金额合计，数量须引用数量合计，不能用其他比较替代复算")
    if materials is None:
        return
    pos_sources = {uid for item in materials
                   if item["id"] == "pos_excel" and item["state"] == "present"
                   for uid in item["source_ids"]}
    if any(term["unit_id"] not in pos_sources for term in [comparison["left"], *comparison["right"]]):
        raise AuditError("POS复算的列示值及各项原值须来自本次Excel销售明细，不能以盖章版、结算单或合同金额替代")


def _validate_settlement_pos_quantity(comparison: dict, materials: list[dict] | None) -> None:
    if materials is None:
        return
    settlement = {uid for item in materials if item["id"] == "settlement" and item["state"] == "present"
                  for uid in item["source_ids"]}
    excel = {uid for item in materials if item["id"] == "pos_excel" and item["state"] == "present"
             for uid in item["source_ids"]}
    if (comparison["left"]["unit_id"] not in settlement
            or any(term["unit_id"] not in excel for term in comparison["right"])):
        raise AuditError("结算销量须以POS Excel版为准比较，左侧引用结算单，右侧引用Excel，不得以盖章版替代")


def audit_decision(scenario: str, flags: dict, evidence: dict, documents: list[dict],
                   materials: list[dict] | None = None) -> dict:
    validate(evidence, AUDIT_SCHEMA)
    allowed = {r.id: r for r in rules(scenario)}
    ids = [c["rule_id"] for c in evidence["checks"]]
    if set(ids) != set(allowed) or len(ids) != len(allowed):
        raise AuditError("审核必须覆盖全部且仅覆盖PDF登记要点，不得增加旧规则或遗漏要点")
    sources = document_map(documents)
    material_rule_ids = {**MATERIAL_RULE_IDS,
                         "settlement": "settlement_template_seal" if "settlement_template_seal" in allowed else "settlement_seal",
                         "promotion_contract": "contract_signed"}
    material_checks = {material_rule_ids[m["id"]]: m for m in (materials or [])
                       if material_rule_ids.get(m["id"]) in allowed}
    material_labels = {r.id: r.text for r in requirements(scenario)}
    checks = []
    for observation in evidence["checks"]:
        rule = allowed[observation["rule_id"]]
        check = dict(observation)
        material = material_checks.get(rule.id)
        missing = material is not None and material["state"] == "missing"
        _sources(check["source_ids"], sources,
                 required=check["status"] in {"pass", "fail"} and not (missing and check["status"] == "fail"))
        if (scenario == "poster_material" and rule.id == "invoice_details" and check["status"] == "pass"
                and material is not None and material["state"] == "present"
                and not set(material["source_ids"]).intersection(check["source_ids"])):
            raise AuditError("票据信息检查通过时须引用本次发票或收据；合同、结算单可补充明细，不能代替票据")
        if rule.id in {"pos_fields", "pos_arithmetic", "pos_product_identity"} and materials is not None:
            excel = {uid for item in materials if item["id"] == "pos_excel" and item["state"] == "present"
                     for uid in item["source_ids"]}
            if not set(check["source_ids"]).issubset(excel):
                raise AuditError("POS通用字段、复算及商品核对只引用Excel版数据，不能以盖章版或其他资料代替")
        applies = applicability(rule.when, scenario, flags)
        if applies is False:
            if check["status"] != "not_applicable":
                raise AuditError("审核没有遵守PDF条件例外")
        elif applies is None and check["status"] != "unknown":
            raise AuditError("审核条件不明时不得自动通过或免审")
        elif check["status"] == "not_applicable":
            raise AuditError("不适用仅允许PDF明文的条件例外，不能免除必审要点")
        if (not rule.numeric or applies is not True or check["status"] == "not_applicable") and check["comparisons"]:
            raise AuditError("非数值要点或适用条件未确认的项目不得夹带金额比较")
        if rule.numeric and check["status"] in {"pass", "fail"} and not check["comparisons"]:
            raise AuditError("数量金额要点必须提供原文数值，由程序复算，AI不能只给通过/不通过")
        if (check["status"] == "pass" and rule.id in CONTRACT_REFERENCE_RULES.get(scenario, ())
                and materials is not None):
            contract_id = "entry_agreement" if scenario == "entry_fee" else "promotion_contract"
            contract_sources = {uid for item in materials
                                if item["id"] == contract_id and item["state"] == "present"
                                for uid in item["source_ids"]}
            if not contract_sources.intersection(check["source_ids"]):
                raise AuditError("本类活动约定比较通过时须引用品牌方本次合同或产品推广协议，不能只引用申报或现场资料")
            if rule.id == "staff_daily_photos":
                photo_sources = {uid for item in materials
                                 if item["id"] == "staff_photos" and item["state"] == "present"
                                 for uid in item["source_ids"]}
                if not photo_sources.intersection(check["source_ids"]):
                    raise AuditError("临促逐店逐日照片检查通过时须引用本次临促照片，合同安排不能证明实际照片已提供")
        for comparison in check["comparisons"]:
            expected_operator = COMPARISON_OPERATORS.get(rule.id, {}).get(comparison["value_kind"])
            if expected_operator is None:
                raise AuditError("数值类别必须符合本条审核要点，不能混用金额、数量或单价")
            if (rule.id in CONTRACT_COMPARISON_RULES.get(scenario, ())
                    and materials is not None):
                _validate_contract_comparison(rule.id, comparison, materials)
            if comparison["operator"] != expected_operator:
                raise AuditError("数量金额比较方向必须符合本类审核要点及用户已确认补充，不能放宽数量相等或颠倒金额支持关系")
            if rule.id == "payment_claim_amount":
                _validate_payment_comparison(comparison, materials)
            elif rule.id == "pos_arithmetic":
                _validate_pos_comparison(comparison, materials)
            elif rule.id == "settlement_pos_quantity":
                _validate_settlement_pos_quantity(comparison, materials)
            if comparison["operation"] in {"ratio", "floor_ratio"} and rule.id != "gift_rule_quantity":
                raise AuditError("赠送比例公式只能用于按实际赠送规则核验赠品数量")
            if comparison["left"] in comparison["right"]:
                raise AuditError("数值比对不能用同一原值证明自身，须引用独立的对应依据")
        calculations = [calculate(c, sources) for c in check["comparisons"]]
        cited = {operand["unit_id"] for c in check["comparisons"]
                 for operand in [c["left"], *c["right"]]}
        if not cited.issubset(check["source_ids"]):
            raise AuditError("数量金额比较的来源必须完整列入该要点证据")
        if calculations and any(not c["matches"] for c in calculations):
            check["status"] = "fail"
            differences = [f"{c['label']}：实际 {c['left_value']}，对照 {c['right_value']}"
                           for c in calculations if not c["matches"]]
            check["reason"] = "；".join(differences)
        elif rule.numeric and calculations and check["status"] == "fail":
            raise AuditError("数量金额比较全部相符，不能凭额外理由判该数值要点失败")
        check.update(effect=rule.effect, requirement=rule.text, calculations=calculations)
        if material and material["state"] in {"missing", "unclear"}:
            label = material_labels[material["id"]]
            check.update(status="fail" if missing else "unknown", effect="missing_material",
                         reason=("未提供" if missing else "未能确认") + label + "；" + material["reason"],
                         source_ids=list(material["source_ids"]), comparisons=[], calculations=[])
        checks.append(check)
    problems = [c for c in checks if c["status"] in {"fail", "unknown"}]
    return {"policy_version": POLICY_VERSION, "scenario": scenario, "checks": checks,
            "summary": {"conclusion": "issues_found" if problems else "no_issues_found",
                        "error_count": len(problems)},
            "noncompliance_registration": [c["reason"] for c in problems]}
