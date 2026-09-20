"""Closed extraction contracts and source-bound PDF-policy decisions."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import re
from typing import Any

import jsonschema

from .common import AuditError
from .pdf_policy import FLAGS, MATERIALS, MATERIAL_RULE_IDS, POLICY_VERSION, applicability, requirements, rules


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
    "material_matches": {"type": "array", "minItems": len(MATERIALS),
                         "maxItems": len(MATERIALS), "items": MATERIAL_MATCH},
})
OPERAND = obj({"unit_id": TEXT, "quote": TEXT, "number": TEXT})
COMPARISON = obj({"label": TEXT, "left": OPERAND,
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
        if applies is False and item["state"] != "not_applicable" and not (optional_training_pos and item["state"] == "present"):
            raise AuditError("不适用的条件资料不能并入匹配清单，须列清单外资料；仅培训POS和Excel明文可选")
        if applies is None and item["state"] != "unclear":
            raise AuditError("资料适用条件无法确定时不能声称已满足或已豁免")


def validate_classification(value: dict, documents: list[dict]) -> None:
    validate(value, CLASSIFICATION_SCHEMA)
    sources = document_map(documents)
    matches = value["material_matches"]
    compared = [item["scenario"] for item in matches]
    if set(compared) != set(MATERIALS) or len(compared) != len(set(compared)):
        raise AuditError("确定核销类型前必须逐一对照PDF八套资料清单，不得遗漏或重复类型")
    for item in matches:
        _sources(item["source_ids"], sources, required=item["supported"])
        _validate_material_list(item["scenario"], item["flags"], item["materials"], sources)
        covered = {uid for material in item["materials"]
                   if material["state"] in {"present", "unclear"} for uid in material["source_ids"]}
        for extra in item["extra_materials"]:
            _sources(extra["source_ids"], sources, required=True)
            covered.update(extra["source_ids"])
        if covered != set(sources):
            raise AuditError("每类比对必须将全部已读来源归入对应资料项或清单外资料，不得忽略多余资料")
        if item["supported"] != _exact_material_match(item):
            raise AuditError("资料项必须不多不少才能匹配类型；缺项、额外资料或资料适用条件不明均不能支持该类型")
    candidates = value["candidate_scenarios"]
    supported = {item["scenario"] for item in matches if item["supported"]}
    if set(candidates) != supported:
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
    if not has_pos_source and selected["flags"]["uses_pos"] is not False:
        raise AuditError("严格匹配清单内没有POS来源时不得额外启动POS通用审核")


def _exact_material_match(match: dict) -> bool:
    return (not match["extra_materials"]
            and all(item["state"] in {"present", "not_applicable"} for item in match["materials"]))


def _classification_reasons(value: dict) -> list[str]:
    from .scenario_registry import SCENARIO_LABELS
    candidates = value["candidate_scenarios"]
    if candidates:
        labels = "、".join(SCENARIO_LABELS[scenario] for scenario in candidates)
        return [f"资料项比对仍同时匹配{labels}，无法唯一确定核销类型。", value["reason"]]
    reasons = []
    for match in value["material_matches"]:
        labels = {r.id: r.text for r in requirements(match["scenario"])}
        gaps = []
        for material in match["materials"]:
            if material["state"] in {"missing", "unclear"}:
                state = "缺少资料项" if material["state"] == "missing" else "资料项或适用条件无法确认"
                gaps.append(f"{state}：{labels[material['id']]}（{material['reason']}）")
        for extra in match["extra_materials"]:
            gaps.append(f"清单外资料项：{extra['description']}（{'、'.join(extra['source_ids'])}；{extra['reason']}）")
        reasons.append(f"{SCENARIO_LABELS[match['scenario']]}资料清单不匹配：" + "；".join(gaps))
    return reasons


def classification_decision(value: dict, documents: list[dict]) -> dict:
    validate_classification(value, documents)
    candidates = [item["scenario"] for item in value["material_matches"] if _exact_material_match(item)]
    scenario = candidates[0] if len(candidates) == 1 else None
    # Compare material items, not physical file counts. Content compliance is
    # audited only after one complete list accounts for all submitted material.
    matched = scenario is not None
    reasons = [] if matched else _classification_reasons(value)
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


def audit_decision(scenario: str, flags: dict, evidence: dict, documents: list[dict],
                   materials: list[dict] | None = None) -> dict:
    validate(evidence, AUDIT_SCHEMA)
    allowed = {r.id: r for r in rules(scenario)}
    ids = [c["rule_id"] for c in evidence["checks"]]
    if set(ids) != set(allowed) or len(ids) != len(allowed):
        raise AuditError("审核必须覆盖全部且仅覆盖PDF登记要点，不得增加旧规则或遗漏要点")
    sources = document_map(documents)
    material_checks = {MATERIAL_RULE_IDS[m["id"]]: m for m in (materials or [])}
    material_labels = {r.id: r.text for r in requirements(scenario)}
    checks = []
    for observation in evidence["checks"]:
        rule = allowed[observation["rule_id"]]
        check = dict(observation)
        material = material_checks.get(rule.id)
        missing = material is not None and material["state"] == "missing"
        _sources(check["source_ids"], sources,
                 required=check["status"] in {"pass", "fail"} and not (missing and check["status"] == "fail"))
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
        expected_operator = {"claim_ceiling": "le", "payment_application_amount": "ge"}.get(rule.id, "eq")
        for comparison in check["comparisons"]:
            if comparison["operator"] != expected_operator:
                raise AuditError("数量金额比较方向必须符合PDF要点，不能改写相等或金额上限要求")
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
    zero = any(c["effect"] == "zero" and c["status"] == "fail" for c in checks)
    return {"policy_version": POLICY_VERSION, "scenario": scenario, "checks": checks,
            "summary": {"conclusion": "zero_reimbursement" if zero else "failed" if problems else "pass",
                        "error_count": len(problems)},
            "noncompliance_registration": [c["reason"] for c in problems]}
