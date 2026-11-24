"""Closed extraction contracts and source-bound PDF-policy decisions."""
from __future__ import annotations
from .fee_policy_checks import (application_sources, approval_sources, constrain_application_check, validate_workbook_comparison,
    separate_settlement_product_cells,
                               PAYMENT_DATES_SCHEMA, target_tiers_schema, payment_date_check, target_tier_comparison)


from copy import deepcopy
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
import json
import re
from typing import Any

import jsonschema

from .common import AuditError
from .comparison_policy import (expected_operator, settlement_comparisons_schema, validate_settlement_sources,
                                complete_settlement_comparisons, validate_settlement_claim_coverage)
from .audit_rag import FIELD_RULES, FIELD_STATES, REFERENCE_EVIDENCE_SCHEMA, compact_explanations
from .display_source_evidence import ASSERTIONS_SCHEMA, validate_display_sources
from .display_size_options import (SIZE_OPTIONS_SCHEMA, validate_size_options,
                                   size_options_issue, complete_size_check)
from .giveaway_coverage import giveaway_coverage_check, linked_activity_customer_names
from .pos_arithmetic import prepare_pos_arithmetic
from .price_support_dates import PRICE_ACTIVITY_DATES_SCHEMA, price_activity_date_checks
from .pdf_policy import (FLAGS, MATERIALS, MATERIAL_LABELS, MATERIAL_RULE_IDS, POLICY_VERSION,
                         COMPARISON_OPERATORS, comparison_operators, CONTRACT_COMPARISON_RULES, CONTRACT_REFERENCE_RULES,
                         applicability, requirements, rules, entry_photo_requirement)


def obj(properties: dict, required: tuple[str, ...] | None = None) -> dict:
    return {"type": "object", "additionalProperties": False,
            "properties": properties,
            "required": list(properties) if required is None else list(required)}


TEXT = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": TEXT}
IDS = {**STRINGS, "uniqueItems": True}
DOCUMENT = obj({"unit_id": TEXT, "roles": STRINGS, "facts": STRINGS, "limitations": STRINGS})
SHEET_LAYOUT = obj({'sheet': TEXT, 'state': {'enum': ['visible', 'hidden', 'veryHidden']},
    'merged_ranges': STRINGS,
    'hidden_rows': {'type': 'array', 'items': {'type': 'integer', 'minimum': 1}},
    'hidden_columns': {'type': 'array', 'items': obj({
        'min': {'type': 'integer', 'minimum': 1}, 'max': {'type': 'integer', 'minimum': 1}})}})
# Program-owned layout is optional on historical/visual documents. It never
# enters fact indexes, numeric operands or image transcription schema.
DOCUMENT['properties']['sheet_layout'] = SHEET_LAYOUT
DOCUMENT_SCHEMA = obj({"documents": {"type": "array", "items": DOCUMENT}})
MATERIAL = obj({"id": TEXT, "state": {"type": "string", "enum": ["present", "missing", "unclear", "not_applicable"]},
                "reason": TEXT, "source_ids": IDS})
# Optional internal evidence preserves the readability of sealed, older material
# records. Only a new affirmative staff condition requires this provenance.
MATERIAL["properties"]["condition_evidence"] = {
    "type": "array", "maxItems": 16, "items": obj({
        "unit_id": TEXT, "quote": TEXT,
        "stance": {"type": "string", "enum": ["affirmed", "denied", "unclear"]},
    }),
}
FLAGS_SCHEMA = obj({name: {"type": ["boolean", "null"]} for name in FLAGS})
EXTRA_MATERIAL = obj({"description": TEXT, "reason": TEXT, "source_ids": IDS})
# The answer carries the same material list twice: once for the whole claim and
# once inside each type comparison. Both copies are mirrors of one judgement, so
# a model that writes only one of them is still readable; the program reuses the
# model's own copy (see complete_material_mirrors) and never invents a material,
# a state or a source. Requiring both copies turned a cosmetic shape difference
# into a whole-claim failure (2026-11-12, HX202610180011 核销资料盘点).
MATERIAL_MATCH = obj({
    "scenario": {"type": "string", "enum": list(MATERIALS)},
    "supported": {"type": "boolean"}, "reason": TEXT, "source_ids": IDS,
    "flags": FLAGS_SCHEMA, "materials": {"type": "array", "items": MATERIAL},
    "extra_materials": {"type": "array", "items": EXTRA_MATERIAL},
}, required=("scenario", "supported", "reason", "source_ids"))
CLASSIFICATION_SCHEMA = obj({
    "candidate_scenarios": {"type": "array", "uniqueItems": True,
                            "items": {"type": "string", "enum": list(MATERIALS)}},
    "reason": TEXT, "source_ids": IDS,
    "flags": FLAGS_SCHEMA,
    "materials": {"type": "array", "items": MATERIAL},
    "background_materials": {"type": "array", "items": EXTRA_MATERIAL},
    "material_matches": {"type": "array", "minItems": len(MATERIALS),
                         "maxItems": len(MATERIALS), "items": MATERIAL_MATCH},
}, required=("candidate_scenarios", "reason", "source_ids", "material_matches"))
BIZ_TYPE_SCHEMA = obj({
    "scenario": {"type": ["string", "null"], "enum": [*MATERIALS, None]},
    "reason": TEXT,
})


def classification_schema(selected_scenario: str | None = None, *, large_venue_fee: bool | None = None,
                          cvs_otc_channel: bool | None = None) -> dict:
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
        if selected_scenario == "entry_fee":
            for flags in (props["flags"], props["material_matches"]["items"]["properties"]["flags"]):
                flags["properties"]["cvs_otc"] = {"type": "boolean"}
                if cvs_otc_channel is not None:
                    flags["properties"]["cvs_otc"]["enum"] = [cvs_otc_channel is True]
            if cvs_otc_channel is not None:
                for materials in (props["materials"], props["material_matches"]["items"]["properties"]["materials"]):
                    state = {"type": "string", "enum": ["present", "missing", "unclear"] if cvs_otc_channel is True else ["not_applicable"]}
                    materials["items"]["allOf"] = [{
                        "if": {"properties": {"id": {"const": "entry_system"}}, "required": ["id"]},
                        "then": {"properties": {"state": state}},
                    }]
        if selected_scenario == "promotional_display" and large_venue_fee is not None:
            for flags in (props["flags"], props["material_matches"]["items"]["properties"]["flags"]):
                flags["properties"]["atrium"] = {"type": "boolean", "enum": [large_venue_fee]}
            for materials in (props["materials"], props["material_matches"]["items"]["properties"]["materials"]):
                state = {"type": "string", "enum": ["present", "missing", "unclear"] if large_venue_fee else ["not_applicable"]}
                materials["items"]["allOf"] = [{
                    "if": {"properties": {"id": {"const": "atrium_agreement"}}, "required": ["id"]},
                    "then": {"properties": {"state": state}},
                }]
    return schema


OPERAND = obj({"unit_id": TEXT, "quote": TEXT, "number": TEXT})
SOURCE_QUOTE = obj({"unit_id": TEXT, "quote": TEXT})
ENTRY_COVERAGE = obj({
    "total_stores": {"anyOf": [OPERAND, {"type": "null"}]},
    "scope_complete": {"type": "boolean"},
    "scope_source_ids": IDS,
    "stores": {"type": "array", "items": obj({"name": TEXT, "source": SOURCE_QUOTE})},
    "photos": {"type": "array", "items": obj({"unit_id": TEXT, "store_name": {"type": ["string", "null"]}})},
    "limitations": STRINGS,
})
GIVEAWAY_COVERAGE = obj({
    "scope_complete": {"type": "boolean"}, "scope_source_ids": IDS,
    "stores": {"type": "array", "items": obj({"name": TEXT, "source": SOURCE_QUOTE})},
    "evidence": {"type": "array", "items": obj({"unit_id": TEXT,
        "store_name": {"type": ["string", "null"]}, "store_quote": {"type": "string"},
        "valid": {"type": "boolean"}, "reason": TEXT})}, "limitations": STRINGS,
})
COMPARISON = obj({"label": TEXT,
                  "value_kind": {"type": "string", "enum": ["amount", "quantity", "unit_price"]},
                  "left": OPERAND,
                  "right": {"type": "array", "minItems": 1, "items": OPERAND},
                  "operation": {"type": "string", "enum": ["value", "sum", "product", "ratio", "floor_ratio"]},
                  "operator": {"type": "string", "enum": ["eq", "le", "ge"]}})
COMPARISON["allOf"] = [{
    "if": {"properties": {"operation": {"const": "value"}}, "required": ["operation"]},
    "then": {"properties": {"right": {
        "maxItems": 1,
        "description": "直接比较每次只对应一个原值；多个来源须分别列出比较，不改为求和或任意只取一个。",
    }}},
}]
CHECK = obj({"rule_id": TEXT,
             "status": {"type": "string", "enum": ["pass", "fail", "unknown", "not_applicable"]},
             "reason": TEXT, "source_ids": IDS,
             "comparisons": {"type": "array", "items": COMPARISON}})
CHECK['properties']['reference_evidence'] = REFERENCE_EVIDENCE_SCHEMA
AUDIT_SCHEMA = obj({"checks": {"type": "array", "items": CHECK}})
from .audit_tools import TOOL_REQUEST_SCHEMA
AUDIT_SCHEMA['properties']['tool_requests'] = deepcopy(TOOL_REQUEST_SCHEMA)
AUDIT_SCHEMA['properties']['reference_reads'] = {'type': 'array', 'items': obj({
    'field': {'type': 'string', 'enum': list(FIELD_RULES)},
    'state': {'type': 'string', 'enum': FIELD_STATES}, 'source_ids': IDS})}
AUDIT_SCHEMA['properties']['payment_dates']={'anyOf':[PAYMENT_DATES_SCHEMA,{'type':'null'}]}
AUDIT_SCHEMA['properties']['target_tiers']={'anyOf':[target_tiers_schema(OPERAND),{'type':'null'}]}
AUDIT_SCHEMA["properties"]["entry_coverage"] = ENTRY_COVERAGE
AUDIT_SCHEMA["properties"]["giveaway_coverage"] = GIVEAWAY_COVERAGE
DISPLAY_PHOTO_RULES = ("display_watermark", "display_brand", "display_material_brand", "display_size")
PHOTO_CHECK = obj({"rule_id": {"type": "string", "enum": list(DISPLAY_PHOTO_RULES)},
                   "status": {"type": "string", "enum": ["pass", "fail", "unknown"]},
                   "reason": TEXT, "source_ids": IDS})
PHOTO_CHECK["properties"]["source_assertions"] = deepcopy(ASSERTIONS_SCHEMA)
PHOTO_CHECK["properties"]["size_options"] = deepcopy(SIZE_OPTIONS_SCHEMA)
PHOTO_REVIEW = obj({"unit_id": TEXT, "checks": {"type": "array", "minItems": len(DISPLAY_PHOTO_RULES),
                                                "maxItems": len(DISPLAY_PHOTO_RULES), "items": PHOTO_CHECK}})
PHOTO_REVIEW_SCHEMA = obj({"photo_reviews": {"type": "array", "items": PHOTO_REVIEW}})
AUDIT_SCHEMA["properties"].update(deepcopy(PHOTO_REVIEW_SCHEMA["properties"]))
PRICE_PHOTO_REVIEW = obj({"unit_id": TEXT,
                          "status": {"type": "string", "enum": ["pass", "fail", "unknown"]},
                          "reason": TEXT, "source_ids": IDS})
AUDIT_SCHEMA["properties"]["price_photo_reviews"] = {"type": "array", "items": PRICE_PHOTO_REVIEW}
AUDIT_SCHEMA["properties"]["activity_dates"] = PRICE_ACTIVITY_DATES_SCHEMA


def price_photo_units(materials: list[dict], units: list[dict]) -> list[dict]:
    ids = {uid for item in materials if item["id"] == "price_evidence"
           and item["state"] in {"present", "unclear"} for uid in item["source_ids"]}
    return [unit for unit in units if unit["unit_id"] in ids
            and (unit.get("image") or unit.get("photo_reference"))]


def _price_photo_check(reviews: list[dict], documents: list[dict], materials: list[dict],
                       units: list[dict]) -> dict:
    expected = [unit["unit_id"] for unit in price_photo_units(materials, units)]
    if [item["unit_id"] for item in reviews] != expected:
        raise AuditError("补差价格证明须逐张按原始顺序分析，不能遗漏、重复或用其他图片代替")
    sources = document_map(documents)
    for review in reviews:
        _sources(review["source_ids"], sources, required=True)
        if review["unit_id"] not in review["source_ids"] or any(
                uid in expected and uid != review["unit_id"] for uid in review["source_ids"]):
            raise AuditError("每张补差价格照片的判断须引用本图，不能用另一图的价签或水印补齐")
        if (any(unit["unit_id"] == review["unit_id"] and not unit.get("image") for unit in units)
                and review["status"] != "unknown"):
            raise AuditError("未能读取的价格证明图片须说明无法确认，不能猜测通过或不符")
    problems = [item for item in reviews if item["status"] != "pass"]
    return {"status": "fail" if any(item["status"] == "fail" for item in reviews)
            else "unknown" if problems else "pass",
            "reason": "；".join(item["reason"] for item in problems) or "已逐张核对本次价格证明照片。",
            "source_ids": list(dict.fromkeys(uid for item in (problems or reviews) for uid in item["source_ids"])),
            "comparisons": [], "photo_reviews": deepcopy(reviews)}


def photo_review_schema(expected: list[str], source_ids: list[str], contract_sources: list[str] | None = None,
                        *, size_alternatives: bool = False, allow_unverified_size_options: bool = False,
                        application_source_ids: list[str] | None = None) -> dict:
    """Bind each batch and its citations to actual program-issued source IDs."""
    schema = deepcopy(PHOTO_REVIEW_SCHEMA)
    reviews = schema["properties"]["photo_reviews"]
    reviews.update(minItems=len(expected), maxItems=len(expected))
    review = reviews["items"]
    review["properties"]["checks"]["items"]["required"].append("source_assertions")
    review["properties"]["unit_id"] = {"type": "string", "enum": expected}
    review["properties"]["checks"]["items"]["properties"]["source_ids"]["items"] = {"type": "string", "enum": source_ids}
    if contract_sources:
        review["properties"]["checks"]["items"]["allOf"] = [{
            "if": {"properties": {"status": {"const": "pass"},
                                  "rule_id": {"enum": sorted(CONTRACT_REFERENCE_RULES["promotional_display"])}},
                   "required": ["status", "rule_id"]},
            "then": {"properties": {"source_ids": {"contains": {"enum": contract_sources}}}},
        }]
    if application_source_ids:
        review["properties"]["checks"]["items"].setdefault("allOf", []).append({
            "if": {"properties": {"status": {"const": "pass"}, "rule_id": {"const": "display_watermark"}},
                   "required": ["status", "rule_id"]},
            "then": {"properties": {"source_ids": {"contains": {"enum": application_source_ids}}}},
        })
    if not size_alternatives:
        # Only the literal, source-bound whole-endcap/area alternative has this
        # auxiliary structure. A plain endcap must not be upgraded to a whole
        # fixture, nor should an invented branch turn a valid check unknown.
        review["properties"]["checks"]["items"]["properties"]["size_options"] = {"type": "null"}
    if size_alternatives and not allow_unverified_size_options:
        review["properties"]["checks"]["items"].setdefault("allOf", []).append({
            "if": {"properties": {"rule_id": {"const": "display_size"}}, "required": ["rule_id"]},
            "then": {"required": ["size_options"]},
        })
    review["allOf"] = [{
        "if": {"properties": {"unit_id": {"const": uid}}, "required": ["unit_id"]},
        "then": {"properties": {"checks": {"items": {"properties": {"source_ids": {
            "contains": {"const": uid},
            "items": {"type": "string", "enum": [source for source in source_ids if source not in expected or source == uid]},
        }}}}}},
    } for uid in expected]
    return compact_explanations(schema)


def display_photo_units(scenario: str, materials: list[dict], units: list[dict]) -> list[dict]:
    ids = {uid for material in materials if material["id"] == "display_photos"
           and material["state"] in {"present", "unclear"} for uid in material["source_ids"]}
    return [unit for unit in units if scenario == "promotional_display" and unit["unit_id"] in ids
            and (unit.get("image") or unit.get("photo_reference"))]


def validate_photo_reviews(value: dict, expected: list[str], documents: list[dict], materials: list[dict],
                           units: list[dict] | None = None, *, require_size_options: bool = False,
                           allow_unverified_size_options: bool = False) -> None:
    validate(value, PHOTO_REVIEW_SCHEMA)
    if [review["unit_id"] for review in value["photo_reviews"]] != expected:
        raise AuditError("陈列照片必须逐张按顺序给出分析情况，不得遗漏、重复或增加照片")
    sources = document_map(documents)
    contract = {uid for material in materials if material["id"] == "promotion_contract"
                and material["state"] == "present" for uid in material["source_ids"]}
    for review_index, review in enumerate(value["photo_reviews"]):
        allowed = {uid for material in materials if material["id"] == "display_photos"
                   and material["state"] in {"present", "unclear"} for uid in material["source_ids"]}
        if review["unit_id"] not in allowed:
            raise AuditError("逐张陈列照片分析只能使用本次陈列照片来源")
        checks = review["checks"]
        if {check["rule_id"] for check in checks} != set(DISPLAY_PHOTO_RULES):
            raise AuditError("每张陈列照片须完整分析水印、实物品牌、宣传物料品牌及陈列大小")
        for check_index, check in enumerate(checks):
            _sources(check["source_ids"], sources, required=True)
            if allow_unverified_size_options:
                issue = size_options_issue(check, review["unit_id"], documents, materials, required=require_size_options)
                if issue:
                    from .execution_trace import record_step
                    record_step("validation.size_options_unverified", "规格辅助结构未通过校验，保留实际照片判断并继续核销", {
                        "photo_unit_id": review["unit_id"], "rule_id": check["rule_id"],
                        "validation_issue": issue, "model_status": check["status"],
                        "decision_status": check["status"],
                        "diagnostic_only": True,
                        "original_model_evidence_unchanged": True, "extra_model_calls": 0,
                    }, status="completed")
            else:
                validate_size_options(check, review["unit_id"], documents, materials, required=require_size_options)
            validate_display_sources(check, documents, materials, units or [],
                                     path=f"photo_reviews/{review_index}/checks/{check_index}",
                                     photo_unit_id=review["unit_id"], strict_prose=False)
            if review["unit_id"] not in check["source_ids"]:
                raise AuditError("逐张照片的分析必须引用该照片，不能用其他照片代替")
            if check["status"] == "pass" and check["rule_id"] in CONTRACT_REFERENCE_RULES["promotional_display"]:
                if not contract.intersection(check["source_ids"]):
                    raise AuditError("照片日期地址、申请区域品牌及大小比对通过时须保留本次合同参照")


def _merge_photo_reviews(checks: list[dict], reviews: list[dict]) -> None:
    for check in checks:
        if check["rule_id"] not in DISPLAY_PHOTO_RULES or not reviews:
            continue
        observations = [item for review in reviews for item in review["checks"] if item["rule_id"] == check["rule_id"]]
        status = "fail" if any(item["status"] == "fail" for item in observations) else "unknown" if any(item["status"] == "unknown" for item in observations) else "pass"
        check.update(status=status,
                     source_ids=list(dict.fromkeys(uid for item in observations for uid in item["source_ids"])),
                     reason="；".join(item["reason"] for item in observations if item["status"] != "pass")
                     or f"已逐张分析全部{len(reviews)}张陈列照片，具体情况见每张照片的分析说明。")


def audit_schema(scenario: str, *, materials: list[dict] | None = None, include_photo_reviews: bool = True,
                 units=None, model_output: bool = True, documents=None) -> dict:
    """Constrain model output to this type's complete PDF audit list."""
    schema = deepcopy(AUDIT_SCHEMA)
    if scenario == "promotional_display":
        display_check = schema["properties"]["checks"]["items"]
        display_check["properties"]["source_assertions"] = deepcopy(ASSERTIONS_SCHEMA)
        display_check["required"].append("source_assertions")
    if scenario == "price_difference_support":
        schema["required"].extend(["price_photo_reviews", "activity_dates"])
    else:
        schema["properties"].pop("price_photo_reviews", None)
        schema["properties"].pop("activity_dates", None)
    if scenario == "entry_fee":
        schema["required"].append("entry_coverage")
    else:
        schema["properties"].pop("entry_coverage", None)
    if scenario == "giveaway_promotion":
        schema["required"].append("giveaway_coverage")
    else:
        schema["properties"].pop("giveaway_coverage", None)
    if scenario == "promotional_display" and include_photo_reviews:
        schema["properties"].update(deepcopy(PHOTO_REVIEW_SCHEMA["properties"]))
        schema["properties"]["photo_reviews"]["items"]["properties"]["checks"]["items"]["required"].append("source_assertions")
        schema["required"].append("photo_reviews")
    else:
        schema["properties"].pop("photo_reviews", None)
    for field,applicable_scenario in (('payment_dates','personnel_incentive'),('target_tiers','pos_target_incentive')):
        if scenario==applicable_scenario:schema['required'].append(field)
        else:schema['properties'].pop(field,None)
    ids = [rule.id for rule in rules(scenario)]
    schema["properties"]["checks"].update(minItems=len(ids), maxItems=len(ids))
    schema["properties"]["checks"]["items"]["properties"]["rule_id"] = {"type": "string", "enum": ids}
    # Put the existing contract/settlement source rules in the generation
    # contract as well as the decision validator. Missing original values can
    # still be unknown with no comparisons; never substitute another document.
    contract = sorted({uid for item in materials or []
                       if item["id"] == "promotion_contract" and item["state"] == "present"
                       for uid in item["source_ids"]})
    settlement = sorted({uid for item in materials or []
                         if item["id"] == "settlement" and item["state"] == "present"
                         for uid in item["source_ids"]})
    constrained = CONTRACT_COMPARISON_RULES.get(scenario, frozenset()) - {"gift_rule_quantity"}
    if contract and constrained:
        conditions = []
        for rule_id in sorted(constrained):
            comparison = {"properties": {"right": {"items": {"properties": {
                "unit_id": {"type": "string", "enum": contract}}}}}}
            if settlement:
                comparison["allOf"] = [{
                    "if": {"properties": {"value_kind": {"const": "unit_price"}}, "required": ["value_kind"]},
                    "then": {"properties": {"left": {"properties": {
                        "unit_id": {"type": "string", "enum": settlement}}}}},
                }]
            conditions.append({
                "if": {"properties": {"rule_id": {"const": rule_id}}, "required": ["rule_id"]},
                "then": {"properties": {"comparisons": {"items": comparison}}},
            })
        schema["properties"]["checks"]["items"]["allOf"] = conditions
    from .pdf_policy import APPLICATION_COMPARISON_RULES
    ids = {uid for m in materials or [] for uid in m['source_ids']} | {u['unit_id'] for u in units or []}
    role_documents = [{'unit_id':uid} for uid in ids]
    app = sorted(application_sources(role_documents, materials, units))
    approval = sorted(approval_sources(role_documents, materials, units))
    conditions = schema['properties']['checks']['items'].setdefault('allOf', [])
    if 'gift_rule_quantity' in {r.id for r in rules(scenario)} and materials is not None:
        from .gift_quantity_contract import gift_quantity_schema
        gift_contract=gift_quantity_schema(materials)
        if gift_contract is not None:
            conditions.append({'if':{'properties':{'rule_id':{'const':'gift_rule_quantity'}},'required':['rule_id']},
                               'then':{'properties':{'comparisons':{'items':gift_contract}}}})
    if 'settlement_calculation' in {r.id for r in rules(scenario)}:
        conditions.append({'if': {'properties': {'rule_id': {'const': 'settlement_calculation'}},
                                  'required': ['rule_id']},
                           'then': {'properties': {'comparisons': settlement_comparisons_schema(documents, settlement)}}})
    for s, rule_id in sorted(APPLICATION_COMPARISON_RULES):
        # A reusable Skill contract has no case yet. Missing case arguments
        # must not turn its application checks into a permanent unknown-only
        # schema; actual requests pass materials (including an empty list).
        if materials is None and units is None:continue
        if s != scenario:continue
        left, right = (app, settlement) if rule_id=='staff_total_unit_price' else (
            contract if rule_id=='contract_budget_amount' else settlement,
            approval if rule_id=='claim_ceiling' else app)
        from .qince_amount_basis import CONTRACT_STANDARD_RULES
        if rule_id in CONTRACT_STANDARD_RULES:
            if rule_id == 'staff_total_unit_price':left = sorted(set(left) | set(contract))
            else:right = sorted(set(right) | set(contract))
        if not left or not right:continue
        comparison={'properties':{'left':{'properties':{'unit_id':{'enum':left}}},
                                  'right':{'items':{'properties':{'unit_id':{'enum':right}}}}}}
        if rule_id == 'claim_ceiling':
            comparison['properties']['operation'] = {'const': 'value'}
            comparison['properties']['right'].update(minItems=1, maxItems=1)
        elif rule_id in {'contract_budget_amount', 'application_claim_ceiling'}:
            comparison['properties']['operation'] = {'enum': ['value', 'sum']}
            comparison['properties']['right'].update(minItems=1)
            if documents is not None:
                from .qince_amount_basis import application_total_comparison_schema
                comparison['allOf'] = [application_total_comparison_schema(documents, units, app)]
        conditions.append({'if':{'properties':{'rule_id':{'const':rule_id}},'required':['rule_id']},
                           'then':{'properties':{'comparisons':{'items':comparison}}}})
    if not conditions:
        schema['properties']['checks']['items'].pop('allOf', None)
    if materials is not None:
        # The observation belongs to the document being checked. Qince's
        # authoritative values are already read by code, not another observed
        # document period. Prevent the model mixing both sides in one array.
        for key, allowed in [('contract_activity_dates', contract),
                             ('settlement_application_dates', settlement),
                             ('contract_dealer', contract), ('settlement_dealer', settlement)]:
            if not allowed or key not in {r.id for r in rules(scenario)}:
                continue
            props = {'periods': {'items': {'properties': {'unit_id': {'enum': allowed}}}}}
            for name in ('written_name', 'seal_name'):
                props[name] = {'anyOf': [{'type':'null'},
                    {'type':'object','properties':{'unit_id':{'enum':allowed}}}]}
            conditions.append({'if':{'properties':{'rule_id':{'const':key}},'required':['rule_id']},
                'then':{'properties':{'reference_evidence':{'properties':props}}}})
        if conditions:
            schema['properties']['checks']['items']['allOf'] = conditions
        if scenario == 'price_difference_support':
            for role, source_ids in (('contract', contract), ('settlement', settlement)):
                periods = schema['properties']['activity_dates']['properties']
                periods[role] = deepcopy(periods[role])
                period = periods[role]['properties']
                period['source_ids'].update(minItems=len(source_ids), maxItems=len(source_ids))
                if source_ids:
                    period['source_ids']['items'] = {'type': 'string', 'enum': source_ids}
                    period['periods']['items']['properties']['unit_id'] = {'type': 'string', 'enum': source_ids}
                else:
                    period['periods']['maxItems'] = 0
        # Mirror existing source/duplicate checks in this request's schema so
        # the model sees the concrete Excel IDs and distinct-row requirement.
        excel = sorted({uid for m in materials if m['id'] == 'pos_excel' and m['state'] == 'present'
                        for uid in m['source_ids']})
        # Native arithmetic can replace the unused model aggregate later;
        # do not reject that discarded aggregate at generation time.
        pos_rules = sorted({'pos_fields', 'pos_product_identity'} &
                           {rule.id for rule in rules(scenario)})
        if pos_rules:
            conditions.append({
                'if': {'properties': {'rule_id': {'enum': pos_rules}}, 'required': ['rule_id']},
                'then': {'properties': {'source_ids': {'items': {'enum': excel}} if excel else {'maxItems': 0}}},
            })
        if any(rule.id == 'settlement_pos_quantity' for rule in rules(scenario)):
            conditions.append({
                'if': {'properties': {'rule_id': {'const': 'settlement_pos_quantity'}}, 'required': ['rule_id']},
                'then': {'properties': {'comparisons': {'items': {'properties': {'right': {'uniqueItems': True}}}}}},
            })
    if not conditions:schema['properties']['checks']['items'].pop('allOf',None)
    if scenario == 'promotional_display' and documents is not None and units is not None:
        from .display_source_evidence import bind_display_source_schema
        schema=bind_display_source_schema(schema,{'scenario':scenario,'documents':documents,
            'materials':materials or [],'units':units})
    return compact_explanations(schema) if model_output else schema


def validate(value: dict, schema: dict) -> None:
    try:
        jsonschema.Draft202012Validator(schema).validate(value)
    except jsonschema.ValidationError as exc:
        # Avoid putting private documents/model output into public exception logs.
        from .program_advice import validation_detail
        error = AuditError("PDF规则证据结构无效：" + "/".join(map(str, exc.absolute_path)))
        error.validation_detail = validation_detail(exc, value)
        error.validation_paths = [error.validation_detail['path']]
        raise error from None


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
        if document.get('sheet_layout') != unit.get('sheet_layout'):
            raise AuditError('工作表版式只能来自本份原始电子表，不能由模型改写或借用其他表')


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


CLASSIFICATION_ERROR_CODES = {
    '销售提成/批量付款条件仅适用于人员激励':'gate_material_state',
    '张贴与制作分支仅适用于物料制作':'gate_material_state',
    '海报张贴须有本次合同或结算正文依据，不能根据缺少制作清单反推张贴豁免':'gate_material_state',
    "证据引用了未读取的材料来源": "unknown_source",
    "材料事实包含重复来源": "gate_source_reference",
    "确定的分类、材料或审核事实必须引用本次材料来源": "gate_source_reference",
    "必须逐项覆盖每一类型的PDF资料清单，不能增加或省略资料要求": "gate_material_list",
    "培训/临促例外只能用于人员激励": "gate_material_state",
    "已经提供POS资料时必须核验PDF的POS通用标准，不能自行免除": "gate_material_state",
    "仅勤策资料替代协议时须使用已验证客户与申请商品逐行精确关联的进场明细；只有编号、客户总数或不完整明细不能认定约定资料齐全": "gate_material_state",
    "资料例外必须符合PDF明文条件，不能自行免交": "gate_material_state",
    "不适用的条件资料不能并入匹配清单，须列清单外资料；仅培训POS和Excel明文可选": "gate_material_state",
    "资料适用条件无法确定时不能声称已满足或已豁免": "gate_material_state",
    "临促条件证据只能用于人员激励的临促资料项": "gate_staff_condition",
    "临促条件只能引用本次结算单或促销合同，不能引用付款或照片资料": "gate_staff_condition",
    "临促条件原文必须逐字来自本次结算单或促销合同的已读事实": "gate_staff_condition",
    "临促证据的肯否判断须与完整原文一致，否定或空白模板不能作为肯定条件": "gate_staff_condition",
    "结算单或促销合同已明确涉及临促，不能省略肯定原文证据或改为未涉及": "gate_staff_condition",
    "临促标记必须与结算单或促销合同的肯定、否定或冲突证据一致": "gate_staff_condition",
    "明确涉及临促时必须引用本次结算单或促销合同的肯定原文": "gate_staff_condition",
    "门店达成奖励例外只能用于人员激励": "gate_material_state",
    "门店达成奖励例外须有本次合同或结算单的明确依据，不能依据文件名、付款截图、人数或缺少SKU推断": "gate_material_state",
    "每类比对必须将全部已读来源归入对应资料项、可选业务背景或清单外资料，不得忽略多余资料": "gate_source_coverage",
    "类型比对必须保留全部已读来源，包括未匹配及清单外资料": "gate_source_coverage",
    "确定核销类型前必须逐一对照PDF八套资料清单，不得遗漏或重复类型": "gate_consistency",
    "CVS/OTC渠道条件必须采用接口cvsOtcChannel，仅true为是，其余情况为否，不能由AI改写": "gate_consistency",
    "商场入场协议的适用条件必须采用接口largeVenueFee，不能由AI改写": "gate_consistency",
    "资料项必须不多不少才能匹配类型；缺项、额外资料或资料适用条件不明均不能支持该类型": "gate_consistency",
    "核销类型候选必须与PDF资料清单逐类比对结果一致": "gate_consistency",
    "核销类型未唯一确定时不得虚构某一类型的资料核验结果": "gate_consistency",
    "所选类型的资料和适用条件必须沿用该类型已保存的清单比对结果": "gate_consistency",
    "严格匹配清单内没有POS来源时不得额外启动POS通用审核": "gate_material_state",
}


def _gate_field(value: dict, path: str):
    current = value
    for part in path.split("/"):
        current = current[int(part)] if part.isdecimal() else current[part]
    return current


def _gate_small_value(value) -> bool:
    if isinstance(value, str):
        return len(value.encode("utf-8")) <= 2048
    if value is None or type(value) is bool:
        return True
    return (isinstance(value, list) and len(value) <= 16
            and all(isinstance(item, str) and len(item.encode("utf-8")) <= 256 for item in value))


class ClassificationEvidenceError(AuditError):
    """Unchanged gate rejection with bounded, private same-request diagnostics."""

    def __init__(self, message: str, path: tuple, value: dict, sources: dict,
                 *, source_ids=(), related_paths=()):
        super().__init__(message)
        self.validation_code = CLASSIFICATION_ERROR_CODES[message]
        self.validation_paths = ["/".join(map(str, path)) or "$"]
        self.retry_context = {}
        context = {"path": self.validation_paths[0], "fields": [], "source_ids": [],
                   "sources": [], "omitted": False}
        for parts in (path, *related_paths):
            position = "/".join(map(str, parts))
            try:
                field = _gate_field(value, position)
            except (KeyError, IndexError, TypeError, ValueError):
                context["omitted"] = True
                continue
            if not _gate_small_value(field) or len(context["fields"]) == 4:
                context["omitted"] = True
                continue
            if not any(item["path"] == position for item in context["fields"]):
                context["fields"].append({"path": position, "value": deepcopy(field)})
        for uid in source_ids:
            if not isinstance(uid, str) or len(uid.encode("utf-8")) > 256:
                context["omitted"] = True
                continue
            if uid in context["source_ids"]:
                continue
            if len(context["source_ids"]) == 16:
                context["omitted"] = True
                break
            context["source_ids"].append(uid)
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 8192:
            return
        count = 0
        for uid in context["source_ids"]:
            document = sources.get(uid)
            if not isinstance(document, dict):
                continue
            if len(context["sources"]) == 3:
                context["omitted"] = True
                break
            original = document.get("facts") or []
            item = {"unit_id": uid, "facts": []}
            context["sources"].append(item)
            # Prefer relevant staff rows only for a staff rejection. Selection
            # does not change facts, infer a condition, or relax the validator.
            for preferred in (True, False):
                for fact in original:
                    if (not isinstance(fact, str) or fact in item["facts"]
                            or len(fact.encode("utf-8")) > 2048
                            or (preferred and (self.validation_code != "gate_staff_condition"
                                               or not re.search(rf"临促|临时促销|{_STAFF_TEMPORARY_HIRE}", fact)))):
                        continue
                    if count == 3:
                        break
                    item["facts"].append(fact)
                    if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 8192:
                        item["facts"].pop()
                        continue
                    count += 1
                if count == 3:
                    break
            if any(fact not in item["facts"] for fact in original):
                context["omitted"] = True
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) <= 8192:
            self.retry_context = context


def _gate_raise(message: str, path: tuple, gate, *, source_ids=(), related_paths=()) -> None:
    if gate is None:
        raise AuditError(message)
    value, sources = gate
    raise ClassificationEvidenceError(message, path, value, sources,
        source_ids=source_ids, related_paths=related_paths) from None


def _gate_sources(ids, sources, path, gate, *, required=False):
    try:
        _sources(ids, sources, required=required)
    except AuditError as error:
        _gate_raise(str(error), path, gate, source_ids=ids)


# Explicit temporary hiring is the same condition; ordinary promoters are not.
_STAFF_TEMPORARY_HIRE = r"临时\s*聘请\s*促销员"
_STAFF_WORD = r"(?:临促|临时促销(?:人员)?)"
_STAFF_NEGATION = re.compile(
    rf"(?<!有)(?:无|没有|不是|非|不|未|无需|不需要)(?:任何)?(?:涉及|包含|含|安排|使用|聘请|雇佣|申请|申报|支付|发放|发生|产生|设置|提及|提供)?(?:任何|额外)?\s*(?:{_STAFF_WORD}|{_STAFF_TEMPORARY_HIRE})"
    rf"|{_STAFF_WORD}(?:人员|安排|工资|费用|补贴|人数|天数)?\s*(?:[：:=为]\s*)?(?:无|否|不涉及|不安排|未安排|0(?:\.0+)?(?:人|天|元)?(?![0-9.]))(?:\b|[，。；;]|$)"
    rf"|(?:未曾|未有|不再|不会)\s*{_STAFF_TEMPORARY_HIRE}"
    rf"|{_STAFF_TEMPORARY_HIRE}\s*[：:=]\s*(?:无|否)(?:\b|[，。；;]|$)"
    rf"|{_STAFF_TEMPORARY_HIRE}\s*(?:人数\s*[：:=为]?\s*0(?:\.0+)?\s*人?|0(?:\.0+)?\s*人)(?![0-9.])(?:\b|[，。；;]|$)"
)
_STAFF_EMPTY = re.compile(
    rf"^(?:(?:是否|有无)(?:涉及|安排|使用)?)?{_STAFF_WORD}"
    r"(?:人员|安排|工资|费用|补贴|人数|天数|资料|照片)?[\s：:=?？□☐_]*$"
    rf"|(?:本次\s*)?{_STAFF_TEMPORARY_HIRE}[\s：:=?？□☐_]+$"
)
_STAFF_UNCERTAIN = re.compile(
    r"待确认|待定|不确定|无法确认|尚未确定|是否.{0,6}临促.{0,4}[?？]$"
    rf"|(?:是否|有无|拟|计划|打算|可能|考虑|如果|若)\s*(?:需要|安排)?\s*{_STAFF_TEMPORARY_HIRE}"
    rf"|{_STAFF_TEMPORARY_HIRE}\s*[?？]"
)
_STAFF_AFFIRMATIVE = re.compile(
    rf"(?:安排|聘请|雇佣|使用|涉及|包含|申请|申报|支付|发放|存在|有)\s*(?:[1-9一二三四五六七八九十]+\s*[名个人]*)?{_STAFF_WORD}"
    rf"|(?:是否(?:涉及|安排|使用)?|有无){_STAFF_WORD}\s*[：:=]?\s*(?:是|有|已安排)(?:\b|$|[，。；;])"
    rf"|{_STAFF_WORD}(?:人员|安排|工资|费用|补贴|人数|天数)?\s*[：:=为]?\s*(?:[1-9][0-9]*(?:\.[0-9]+)?|0\.[0-9]*[1-9][0-9]*)\s*(?:名|人|天|元)"
    rf"|{_STAFF_TEMPORARY_HIRE}"
)


def _staff_text(text: str) -> str:
    # Remove only native spreadsheet location syntax, never business wording.
    from .spreadsheet_rows import ROW, parse_cells
    match = ROW.fullmatch(text)
    cells = parse_cells(match[3]) if match else parse_cells('{' + text + '}')
    if cells and (not match or match[3].startswith('{')):
        return ' '.join(cell['raw'] for cell in cells.values())
    lines = []
    for line in text.splitlines():
        match = ROW.fullmatch(line)
        cells = parse_cells(match[3]) if match else None
        if cells:
            lines.append(' '.join(cell['raw'] for cell in cells.values()))
            continue
        line = re.sub(r"^.+!R[1-9][0-9]*: ", "", line)
        lines.append(re.sub(r"(?:^| \| )C[1-9][0-9]*=", " ", line).strip())
    return "\n".join(lines)


def _staff_stance(text: str) -> str:
    wording = _staff_text(text)
    if (_STAFF_EMPTY.fullmatch(wording) or _STAFF_UNCERTAIN.search(wording)
            or re.search(r"(?:□|☐)\s*是\s*(?:□|☐)\s*否|_{2,}|[□☐]\s*$", wording)):
        return "unclear"
    denied = bool(_STAFF_NEGATION.search(wording))
    affirmed = bool(_STAFF_AFFIRMATIVE.search(wording))
    if denied:
        # A positive phrase contained inside a negative phrase is not support.
        affirmed = bool(_STAFF_AFFIRMATIVE.search(_STAFF_NEGATION.sub("", wording)))
        return "unclear" if affirmed else "denied"
    return "affirmed" if affirmed else "unclear"


def _staff_quoted_facts(quote: str, facts: list[str]) -> list[str]:
    matching = [fact for fact in facts if quote in fact]
    if matching:
        return matching
    # A vertically filled native field may need its original heading and value.
    # Only verbatim consecutive rows in the same sheet and same single column
    # can be combined; never join unrelated inventory values or different cells.
    from .spreadsheet_rows import ROW, parse_cells
    for index in range(len(facts) - 1):
        parts = facts[index:index + 2]
        if '\n'.join(parts) != quote:
            continue
        rows = [ROW.fullmatch(part) for part in parts]
        cells = [parse_cells(row[3]) if row else None for row in rows]
        if (all(rows) and rows[0][1] == rows[1][1]
                and int(rows[1][2]) == int(rows[0][2]) + 1
                and all(cells) and len(cells[0]) == len(cells[1]) == 1
                and cells[0].keys() == cells[1].keys()):
            return [quote]
    return []


def _validate_staff_condition(scenario: str, flags: dict, materials: list[dict], sources: dict,
                              *, gate=None, path: tuple = ()) -> None:
    staff = next((item for item in materials if item["id"] == "staff_photos"), {})
    staff_index = next((index for index, item in enumerate(materials) if item["id"] == "staff_photos"), 0)
    staff_path = (*path, "materials", staff_index)
    for material_index, item in enumerate(materials):
        if item.get("condition_evidence") and (scenario != "personnel_incentive" or item["id"] != "staff_photos"):
            _gate_raise("临促条件证据只能用于人员激励的临促资料项",
                        (*path, "materials", material_index, "condition_evidence"), gate)
    if scenario != "personnel_incentive":
        return
    evidence = staff.get("condition_evidence", [])
    allowed = {uid for item in materials if item["id"] in {"settlement", "promotion_contract"}
               and item["state"] in {"present", "unclear"} for uid in item["source_ids"]}
    stances = set()
    for evidence_index, item in enumerate(evidence):
        uid, quote, stance = item["unit_id"], item["quote"], item["stance"]
        evidence_path = (*staff_path, "condition_evidence", evidence_index)
        _gate_sources([uid], sources, (*evidence_path, "unit_id"), gate, required=True)
        if uid not in allowed:
            _gate_raise("临促条件只能引用本次结算单或促销合同，不能引用付款或照片资料",
                        (*evidence_path, "unit_id"), gate, source_ids=[uid])
        facts = sources[uid].get("facts", [])
        quoted_facts = _staff_quoted_facts(quote, facts)
        if not quoted_facts:
            _gate_raise("临促条件原文必须逐字来自本次结算单或促销合同的已读事实",
                        (*evidence_path, "quote"), gate, source_ids=[uid],
                        related_paths=[(*evidence_path, "unit_id"), (*evidence_path, "stance")])
        actual_stance = _staff_stance(quote)
        if stance != actual_stance or (stance == "affirmed"
                and any(_staff_stance(fact) == "denied" for fact in quoted_facts)):
            _gate_raise("临促证据的肯否判断须与完整原文一致，否定或空白模板不能作为肯定条件",
                        (*evidence_path, "stance"), gate, source_ids=[uid],
                        related_paths=[(*evidence_path, "unit_id"), (*evidence_path, "quote")])
        stances.add(stance)
    # Validate omissions as well as submitted quotes. An explicit affirmative
    # fact in an approved source cannot disappear through an empty evidence
    # array or an unrelated "unclear" quote. Unmentioned fields are not denials.
    source_stances = set()
    for uid in allowed:
        facts = sources.get(uid, {}).get("facts", [])
        source_stances.update(_staff_stance(fact) for fact in facts)
        for index in range(len(facts) - 1):
            quote = "\n".join(facts[index:index + 2])
            if _staff_quoted_facts(quote, facts[index:index + 2]):
                source_stances.add(_staff_stance(quote))
    if ("affirmed" in source_stances and "denied" not in source_stances
            and ("affirmed" not in stances or flags.get("temporary_staff") is not True)):
        _gate_raise("结算单或促销合同已明确涉及临促，不能省略肯定原文证据或改为未涉及",
                    (*path, "flags", "temporary_staff"), gate, source_ids=sorted(allowed),
                    related_paths=[(*staff_path, "state")])
    # A contrary statement elsewhere in either approved source cannot be hidden
    # by quoting only the affirmative document or a fragment of the same line.
    if "affirmed" in stances and "denied" in source_stances:
        stances.add("denied")
    flag = flags.get("temporary_staff")
    if stances:
        expected = True if stances == {"affirmed"} else False if stances == {"denied"} else None
        if flag is not expected:
            _gate_raise("临促标记必须与结算单或促销合同的肯定、否定或冲突证据一致",
                        (*path, "flags", "temporary_staff"), gate, source_ids=sorted(allowed),
                        related_paths=[(*staff_path, "state")])
    elif flag is True:
        _gate_raise("明确涉及临促时必须引用本次结算单或促销合同的肯定原文",
                    (*path, "flags", "temporary_staff"), gate, source_ids=sorted(allowed),
                    related_paths=[(*staff_path, "state")])


def _has_sku_reward(text: str) -> bool:
    """Distinguish an actual SKU reward from an explicit absence observation."""
    pattern = r"(?:每(?:支|件|盒|瓶|个SKU)|按(?:单品|SKU|商品销量|产品销量))[^，。；;\n]{0,24}(?:奖励|激励|提成)"
    for match in re.finditer(pattern, text, flags=re.I):
        prefix = re.split(r"但是|不过|然而|而是|但|而|另有|另外|并且|同时", text[:match.start()])[-1]
        # Extracted facts may say “未出现临促工资、培训或按SKU销量计提的提成表述”.
        # Such a negative statement does not establish a mixed SKU activity.
        absent = re.search(r"(?:未出现|未包含|未涉及|未列(?:示|出|明)?|未见|没有|不涉及|不包含)[^，。；;\n]*$|(?:无|不|未)$", prefix)
        double_negative = re.search(r"(?:并非|不是|不代表|不能认为|不能说).*(?:没有|未见|未出现|不涉及)", prefix)
        if not absent or double_negative:
            return True
    return False


def _validate_store_incentive(scenario: str, flags: dict, materials: list[dict], sources: dict,
                              *, gate=None, path: tuple = ()) -> None:
    """Bind the optional personnel subtype to actual business facts.

    Unknown/mixed activities need not claim this whole-case exemption; their
    actual SKU rows and other payments retain their own applicable checks.
    """
    if flags.get("store_incentive") is not True:
        return
    if scenario != "personnel_incentive":
        _gate_raise("门店达成奖励例外只能用于人员激励", (*path, "flags", "store_incentive"), gate)
    ids = {uid for item in materials if item["id"] in {"promotion_contract", "settlement"}
           and item["state"] in {"present", "unclear"} for uid in item["source_ids"]}
    marker = r"(?:门店|单店|店铺|区域)(?:月度|月|单月)?(?:达成(?:任务|指标)?)?(?:激励|奖励|奖)"
    store_price = r"(?:奖励|激励|奖金)[^，。；;\n]{0,24}元\s*[/／]\s*(?:家|店)"
    positive = negative = sku_reward = False
    for uid in ids:
        for fact in sources.get(uid, {}).get("facts", []):
            for clause in re.split(r"[，。；;\n]", fact):
                text = re.sub(r"\s+", "", clause)
                if re.search(r"(?:不是|不属于|不涉及|无)" + marker, text):
                    negative = True
                    continue
                if re.search(r"是否|有无|待确认|不确定|未明确|示例|模板|[?？□☐]", text):
                    continue
                positive |= bool(re.search(marker + "|" + store_price, text))
                sku_reward |= _has_sku_reward(text)
    if not positive or negative or sku_reward:
        _gate_raise("门店达成奖励例外须有本次合同或结算单的明确依据，不能依据文件名、付款截图、人数或缺少SKU推断",
                    (*path, "flags", "store_incentive"), gate, source_ids=sorted(ids))


def _optional_material_ids(scenario: str, flags: dict) -> set[str]:
    return {r.id for r in requirements(scenario) if r.when == "optional"
            or (r.when == "confirmed_temporary_staff" and flags.get("temporary_staff") is not True)
            or (r.when == "confirmed_full_reduction" and flags.get("full_reduction") is not True)}


def _validate_material_list(scenario: str, flags: dict, materials: list[dict], sources: dict,
                            *, gate=None, path: tuple = ()) -> None:
    wanted = {r.id for r in requirements(scenario)}
    actual = [r["id"] for r in materials]
    if set(actual) != wanted or len(actual) != len(wanted):
        _gate_raise("必须逐项覆盖每一类型的PDF资料清单，不能增加或省略资料要求",
                    (*path, "materials"), gate,
                    related_paths=[(*path, "materials", index, "id") for index in range(min(len(materials), 4))])
    if scenario != "personnel_incentive" and (flags["training"] or flags["temporary_staff"]):
        _gate_raise("培训/临促例外只能用于人员激励", (*path, "flags"), gate,
                    related_paths=[(*path, "flags", "training"), (*path, "flags", "temporary_staff")])
    _validate_staff_condition(scenario, flags, materials, sources, gate=gate, path=path)
    if scenario!='personnel_incentive' and any(flags.get(k) for k in ('sales_commission','batch_payment')):
        _gate_raise('销售提成/批量付款条件仅适用于人员激励',(*path,'flags'),gate)
    if scenario!='poster_material' and flags.get('poster_posting'):
        _gate_raise('张贴与制作分支仅适用于物料制作',(*path,'flags','poster_posting'),gate)
    if scenario=='poster_material' and flags.get('poster_posting') is True:
        ids={uid for m in materials if m['id'] in {'settlement','promotion_contract'} and m['state']=='present' for uid in m['source_ids']}
        text='\n'.join(f for uid in ids for f in sources[uid]['facts'])
        if not re.search(r'海报(?:张贴|更换)|(?:张贴|更换)(?:海报|费用|费)',text):
            _gate_raise('海报张贴须有本次合同或结算正文依据，不能根据缺少制作清单反推张贴豁免',(*path,'flags','poster_posting'),gate,source_ids=sorted(ids))
    _validate_store_incentive(scenario, flags, materials, sources, gate=gate, path=path)
    present_pos = any(m["id"] in {"pos", "pos_excel"} and m["state"] == "present" for m in materials)
    if present_pos and flags["uses_pos"] is not True:
        _gate_raise("已经提供POS资料时必须核验PDF的POS通用标准，不能自行免除", (*path, "flags", "uses_pos"), gate)
    for material_index, item in enumerate(materials):
        material_path = (*path, "materials", material_index)
        _gate_sources(item["source_ids"], sources, (*material_path, "source_ids"), gate, required=item["state"] == "present")
        if scenario == "entry_fee" and item["id"] == "entry_agreement" and item["state"] == "present":
            cited = [sources[uid] for uid in item["source_ids"]]
            qince_only = cited and all("勤策本次业务补充明细（独立来源）" in d.get("roles", []) for d in cited)
            verified_scope = {uid for uid, document in sources.items()
                              if "勤策本次业务补充明细（独立来源）" in document.get("roles", [])
                              and any(f.startswith("linked_customer=") for f in document["facts"])}
            if qince_only and (not verified_scope or not verified_scope.issubset(item["source_ids"])):
                _gate_raise("仅勤策资料替代协议时须使用已验证客户与申请商品逐行精确关联的进场明细；只有编号、客户总数或不完整明细不能认定约定资料齐全",
                            (*material_path, "state"), gate, source_ids=item["source_ids"])
        requirement = next(r for r in requirements(scenario) if r.id == item["id"])
        if item["id"] in _optional_material_ids(scenario, flags):
            continue
        applies = applicability(requirement.when, scenario, flags)
        if item["state"] == "not_applicable" and applies is not False:
            _gate_raise("资料例外必须符合PDF明文条件，不能自行免交", (*material_path, "state"), gate,
                        source_ids=item["source_ids"], related_paths=[(*material_path, "id")])
        optional_training_pos = requirement.when in {"not_training", "personnel_pos"} and scenario == "personnel_incentive"
        optional_pos_submission = optional_training_pos and (
            item["state"] == "present" or (present_pos and item["state"] in {"missing", "unclear"}))
        if applies is False and item["state"] != "not_applicable" and not optional_pos_submission:
            _gate_raise("不适用的条件资料不能并入匹配清单，须列清单外资料；仅培训POS和Excel明文可选",
                        (*material_path, "state"), gate, source_ids=item["source_ids"], related_paths=[(*material_path, "id")])
        if applies is None and item["state"] != "unclear":
            _gate_raise("资料适用条件无法确定时不能声称已满足或已豁免", (*material_path, "state"), gate,
                        source_ids=item["source_ids"], related_paths=[(*material_path, "id")])


def validate_classification(value: dict, documents: list[dict], selected_scenario: str | None = None,
                            *, large_venue_fee: bool | None = None, cvs_otc_channel: bool = False) -> None:
    validate(value, classification_schema(selected_scenario, large_venue_fee=large_venue_fee,
                                          cvs_otc_channel=cvs_otc_channel))
    try:
        sources = document_map(documents)
    except AuditError as error:
        _gate_raise(str(error), (), (value, {}))
    gate = (value, sources)
    background_sources = set()
    for background_index, background in enumerate(value["background_materials"]):
        _gate_sources(background["source_ids"], sources, ("background_materials", background_index, "source_ids"), gate, required=True)
        background_sources.update(background["source_ids"])
    matches = value["material_matches"]
    compared = [item["scenario"] for item in matches]
    expected = {selected_scenario} if selected_scenario else set(MATERIALS)
    if set(compared) != expected or len(compared) != len(set(compared)):
        _gate_raise("确定核销类型前必须逐一对照PDF八套资料清单，不得遗漏或重复类型", ("material_matches",), gate,
                    related_paths=[("material_matches", index, "scenario") for index in range(min(len(matches), 4))])
    for match_index, item in enumerate(matches):
        match_path = ("material_matches", match_index)
        if item["scenario"] == "entry_fee" and item["flags"]["cvs_otc"] is not (cvs_otc_channel is True):
            _gate_raise("CVS/OTC渠道条件必须采用接口cvsOtcChannel，仅true为是，其余情况为否，不能由AI改写",
                        (*match_path, "flags", "cvs_otc"), gate)
        if (item["scenario"] == "promotional_display" and large_venue_fee is not None
                and item["flags"]["atrium"] is not large_venue_fee):
            _gate_raise("商场入场协议的适用条件必须采用接口largeVenueFee，不能由AI改写",
                        (*match_path, "flags", "atrium"), gate)
        _gate_sources(item["source_ids"], sources, (*match_path, "source_ids"), gate, required=item["supported"])
        _validate_material_list(item["scenario"], item["flags"], item["materials"], sources, gate=gate, path=match_path)
        covered = {uid for material in item["materials"]
                   if material["state"] in {"present", "unclear"} for uid in material["source_ids"]}
        covered.update(background_sources)
        for extra_index, extra in enumerate(item["extra_materials"]):
            _gate_sources(extra["source_ids"], sources, (*match_path, "extra_materials", extra_index, "source_ids"), gate, required=True)
            covered.update(extra["source_ids"])
        if covered != set(sources):
            _gate_raise("每类比对必须将全部已读来源归入对应资料项、可选业务背景或清单外资料，不得忽略多余资料",
                        (*match_path, "materials"), gate, source_ids=sorted(set(sources) - covered))
        if item["supported"] != _exact_material_match(item):
            _gate_raise("资料项必须不多不少才能匹配类型；缺项、额外资料或资料适用条件不明均不能支持该类型",
                        (*match_path, "supported"), gate,
                        related_paths=[(*match_path, "materials", index, "state") for index, material in enumerate(item["materials"])
                                       if material["state"] not in {"present", "not_applicable"}][:3])
    candidates = value["candidate_scenarios"]
    supported = {item["scenario"] for item in matches if item["supported"]}
    if set(candidates) != ({selected_scenario} if selected_scenario else supported):
        _gate_raise("核销类型候选必须与PDF资料清单逐类比对结果一致", ("candidate_scenarios",), gate)
    _gate_sources(value["source_ids"], sources, ("source_ids",), gate, required=bool(candidates))
    if set(value["source_ids"]) != set(sources):
        _gate_raise("类型比对必须保留全部已读来源，包括未匹配及清单外资料", ("source_ids",), gate,
                    source_ids=sorted(set(sources) - set(value["source_ids"])))
    if len(candidates) != 1:
        if value["materials"]:
            _gate_raise("核销类型未唯一确定时不得虚构某一类型的资料核验结果", ("materials",), gate)
        return
    selected = next(item for item in matches if item["scenario"] == candidates[0])
    if value["flags"] != selected["flags"] or value["materials"] != selected["materials"]:
        # Choose the first actual difference without rewriting either mirror.
        mismatch = ("flags",) if value["flags"] != selected["flags"] else ("materials",)
        related = []
        selected_index = matches.index(selected)
        if mismatch == ("flags",):
            flag = next(name for name in FLAGS if value["flags"][name] != selected["flags"][name])
            mismatch = ("flags", flag)
            related = [("material_matches", selected_index, "flags", flag)]
        elif len(value["materials"]) == len(selected["materials"]):
            index = next(i for i, material in enumerate(value["materials"]) if material != selected["materials"][i])
            field = next(name for name in ("id", "state", "reason", "source_ids", "condition_evidence")
                         if value["materials"][index].get(name) != selected["materials"][index].get(name))
            mismatch = ("materials", index, field) if field in value["materials"][index] else ("materials", index, "id")
            related = [("material_matches", selected_index, *mismatch)]
        _gate_raise("所选类型的资料和适用条件必须沿用该类型已保存的清单比对结果", mismatch, gate, related_paths=related)
    has_pos_source = any(m["id"] in {"pos", "pos_excel", "entry_system"} and m["state"] == "present"
                         for m in selected["materials"])
    if _exact_material_match(selected) and not has_pos_source and selected["flags"]["uses_pos"] is not False:
        _gate_raise("严格匹配清单内没有POS来源时不得额外启动POS通用审核",
                    ("material_matches", matches.index(selected), "flags", "uses_pos"), gate)


def _exact_material_match(match: dict) -> bool:
    by_id = {item["id"]: item for item in match["materials"]}
    pos_pair = [by_id.get(name, {}).get("state") for name in ("pos", "pos_excel")]
    # Training may omit both versions, but cannot submit one and exempt the other.
    incomplete_pos_pair = (match["scenario"] != "self_procured_gift_material"
                           and "present" in pos_pair and pos_pair != ["present", "present"])
    optional = _optional_material_ids(match["scenario"], match["flags"])
    return (not incomplete_pos_pair and not match["extra_materials"]
            and all(item["id"] in optional or item["state"] in {"present", "not_applicable"}
                    for item in match["materials"]))


def classification_issues(value: dict, selected_scenario: str | None = None) -> list[dict]:
    from .scenario_registry import SCENARIO_LABELS
    candidates = value["candidate_scenarios"]
    if candidates and selected_scenario is None:
        labels = "、".join(SCENARIO_LABELS[scenario] for scenario in candidates)
        return [{"reason": reason, "source_ids": []} for reason in
                (f"资料项比对仍同时匹配{labels}，无法唯一确定核销类型。", value["reason"])]
    reasons = []
    # Describe conditional gaps for every list supported by some actual material;
    # never pick a type by the fewest missing items. The full eight-list evidence
    # remains in material_matches even when a list has no related material.
    relevant = [match for match in value["material_matches"]
                if any(m["state"] == "present" or (m["state"] == "unclear" and m["source_ids"])
                       for m in match["materials"])]
    for match in relevant or value["material_matches"]:
        labels = {**MATERIAL_LABELS, **({"invoice": "发票、收据或打款截图"} if match["scenario"] == "poster_material" else {})}
        gaps = []
        # Legacy read-only projections may predate flags; do not add a new staff requirement.
        optional = _optional_material_ids(match["scenario"], match.get("flags") or {})
        missing_gift_photos = (match["scenario"] == "self_procured_gift_material" and
                              all(any(item["id"] == role and item["state"] == "missing"
                                      for item in match["materials"])
                                  for role in ("gift_finished_photos", "gift_store_photos")))
        for material in match["materials"]:
            if material["id"] in optional:
                continue
            if missing_gift_photos and material["id"] == "gift_store_photos":
                continue
            if material["state"] in {"missing", "unclear"}:
                state = "缺少资料" if material["state"] == "missing" else "尚不能确认的资料或适用情况"
                detail = f"（{material['reason']}）" if material["state"] == "unclear" else ""
                ids = material["source_ids"] if material["state"] == "unclear" else []
                # A missing counterpart is corrected from the submitted POS version.
                # An entirely absent contract or other item has no submitted file.
                counterpart = {"pos": "pos_excel", "pos_excel": "pos"}.get(material["id"])
                if material["state"] == "missing" and counterpart:
                    ids = [uid for item in match["materials"]
                           if item["id"] == counterpart and item["state"] == "present"
                           for uid in item["source_ids"]]
                label = ("外采赠品及活动门店现场照片" if missing_gift_photos and material["id"] == "gift_finished_photos"
                         else labels[material["id"]])
                gaps.append({"reason": f"{state}：{label}{detail}",
                             "source_ids": list(dict.fromkeys(ids))})
        for extra in match["extra_materials"]:
            gaps.append({"reason": f"多出的资料：{extra['description']}（{'、'.join(extra['source_ids'])}；{extra['reason']}）",
                         "source_ids": list(extra["source_ids"])})
        pair = [m["state"] for m in match["materials"] if m["id"] in {"pos", "pos_excel"}]
        if "present" in pair and pair != ["present", "present"]:
            gaps.append({"reason": "POS明细须成套提供Excel版和盖章版，已提供一版时不能免交另一版",
                         "source_ids": list(dict.fromkeys(uid for item in match["materials"]
                             if item["id"] in {"pos", "pos_excel"} and item["state"] == "present"
                             for uid in item["source_ids"]))})
        prefix = (f"{SCENARIO_LABELS[match['scenario']]}：" if selected_scenario
                  else f"如果申报的是{SCENARIO_LABELS[match['scenario']]}，")
        if selected_scenario:
            reasons.extend({**gap, "reason": prefix + gap["reason"]} for gap in gaps)
        else:
            reasons.append({"reason": prefix + "；".join(gap["reason"] for gap in gaps),
                            "source_ids": list(dict.fromkeys(uid for gap in gaps for uid in gap["source_ids"]))})
    return reasons


def _classification_reasons(value: dict, selected_scenario: str | None = None) -> list[str]:
    return [issue["reason"] for issue in classification_issues(value, selected_scenario)]


_MATERIAL_STATE_SYNONYMS = {
    "present": "present", "provided": "present", "supplied": "present",
    "submitted": "present", "available": "present", "included": "present",
    "yes": "present", "true": "present",
    "missing": "missing", "absent": "missing", "not_provided": "missing",
    "unprovided": "missing", "optional_not_provided": "missing",
    "not_submitted": "missing", "no": "missing", "false": "missing",
    "unclear": "unclear", "uncertain": "unclear", "unknown": "unclear",
    "partial": "unclear",
    "not_applicable": "not_applicable", "n/a": "not_applicable",
    "na": "not_applicable", "none": "not_applicable",
}


def complete_material_mirrors(value):
    """Fill a missing mirror copy from the copy the model actually wrote.

    `flags`, `materials` and `extra_materials` exist once for the whole claim and
    once inside every type comparison. Only absent keys are filled, and only
    from the same answer, so a contradiction the model really wrote (two
    different lists) stays visible to the caller instead of being silently
    resolved. Nothing is invented: when the answer carries neither copy the
    defaults stay empty/unknown and the business rules treat them as unknown.
    """
    if not isinstance(value, dict):
        return value
    matches = [item for item in value.get("material_matches") or [] if isinstance(item, dict)]
    top_flags = value.get("flags")
    top_materials = value.get("materials")
    for match in matches:
        if match.get("flags") is None and isinstance(top_flags, dict):
            match["flags"] = deepcopy(top_flags)
        if match.get("materials") is None and isinstance(top_materials, list):
            match["materials"] = deepcopy(top_materials)
        if match.get("extra_materials") is None:
            match["extra_materials"] = []
    if value.get("flags") is None:
        donor = next((item["flags"] for item in matches if isinstance(item.get("flags"), dict)), None)
        value["flags"] = {name: None for name in FLAGS} if donor is None else deepcopy(donor)
    if value.get("materials") is None:
        donor = next((item["materials"] for item in matches if isinstance(item.get("materials"), list)), None)
        value["materials"] = [] if donor is None else deepcopy(donor)
    if value.get("background_materials") is None:
        value["background_materials"] = []
    return value


def normalize_material_states(value):
    """Map the audit model's own state wording onto the program's four states.

    The executing model may answer with natural vocabulary ("provided",
    "optional_not_provided"). Every program-side material rule keys on
    present/missing/unclear/not_applicable, so unmapped wording silently
    disables those rules — e.g. scene-photo indexing skipped a whole claim and
    photo-reuse inspection never ran. Unknown wording becomes "unclear": it is
    neither a pass nor a failure, and it never blocks the run.
    """
    if not isinstance(value, dict):
        return value
    complete_material_mirrors(value)
    groups = [value.get("materials")]
    groups += [item.get("materials") for item in value.get("material_matches") or []
               if isinstance(item, dict)]
    for materials in groups:
        for material in materials or []:
            if not isinstance(material, dict):
                continue
            raw = material.get("state")
            if not isinstance(raw, str):
                continue
            key = raw.strip().casefold().replace("-", "_").replace(" ", "_")
            material["state"] = _MATERIAL_STATE_SYNONYMS.get(key, "unclear")
    return value


def classification_decision(value: dict, documents: list[dict], selected_scenario: str | None = None,
                            *, large_venue_fee: bool | None = None, cvs_otc_channel: bool = False) -> dict:
    normalize_material_states(value)
    from .model_authority import enabled, classification_route
    if enabled():
        return classification_route(value, selected_scenario)
    validate_classification(value, documents, selected_scenario, large_venue_fee=large_venue_fee,
                            cvs_otc_channel=cvs_otc_channel)
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


class OperandEvidenceError(AuditError):
    """Strict rejection with bounded, private context for the same request's retry."""

    def __init__(self, message: str, item: dict, facts: list[str], path: tuple, field: str):
        super().__init__(message)
        self.validation_paths = ["/".join(map(str, (*path, field)))]
        self.retry_context = {}
        limits = {"unit_id": 256, "quote": 2048, **({"number": 128} if "number" in item else {})}
        if any(not isinstance(item.get(key), str) or len(item[key].encode("utf-8")) > limit
               for key, limit in limits.items()):
            return
        context = {"path": self.validation_paths[0], **{key: item[key] for key in limits},
                   "facts": [], "facts_omitted": True}
        # A copied table row can contain a typo even when its locator is exact.
        # Prefer that same original row, including when its number differs;
        # unrelated rows containing a substring such as 67 in 1674 are no help.
        # These hints never establish a match or alter the rejected evidence.
        row = re.match(r'.+!R[1-9][0-9]*:', item['quote'])
        locator = row.group() if row else None
        located = bool(locator and any(isinstance(fact, str) and fact.startswith(locator) for fact in facts))
        literal_number = (re.compile(r'(?<![0-9.])' + re.escape(item['number']) + r'(?![0-9.])')
                          if "number" in item else None)
        for mode in (('row',) if located else ('number', 'any') if literal_number else ('any',)):
            for fact in facts:
                if (not isinstance(fact, str) or fact in context["facts"]
                        or len(fact.encode("utf-8")) > 2048
                        or (mode == 'row' and not fact.startswith(locator))
                        or (mode == 'number' and not literal_number.search(fact))):
                    continue
                context["facts"].append(fact)
                if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > 8192:
                    context["facts"].pop()
                    continue
                if len(context["facts"]) == 3:
                    break
            if len(context["facts"]) == 3:
                break
        context["facts_omitted"] = any(fact not in context["facts"] for fact in facts)
        if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) <= 8192:
            self.retry_context = context


def _quote_number_tokens(quote: str) -> list[str]:
    """Keep valid thousands groups; commas between fields never join values."""
    tokens = []
    for candidate in re.findall(r"(?<![A-Za-z0-9])[-+]?\d[\d,，]*(?:\.\d+)?\s*[%％]?", quote):
        candidate = candidate.strip().rstrip(',，')
        if not re.search('[,，]', candidate) or re.fullmatch(
                r'[-+]?\d{1,3}(?:[,，]\d{3})+(?:\.\d+)?\s*[%％]?', candidate):
            tokens.append(candidate)
        else:
            tokens.extend(part for part in re.split('[,，]', candidate) if part)
    return tokens


def _operand(item: dict, documents: dict, *, path: tuple = ()) -> Decimal:
    _sources([item["unit_id"]], documents, required=True)
    text = "\n".join(documents[item["unit_id"]]["facts"])
    quote = item["quote"]
    if quote not in text:
        raise OperandEvidenceError("数值引用必须逐字来自本次已提取材料，不能补造申请值或计算依据",
                                   item, documents[item["unit_id"]]["facts"], path, "quote")
    number = _number(item["number"])
    tokens = _quote_number_tokens(quote)
    if not any(_number(token) == number for token in tokens):
        raise OperandEvidenceError("数值与引用的原文不一致", item,
                                   documents[item["unit_id"]]["facts"], path, "number")
    return number


def _payment_operand(item: dict, documents: dict, *, path: tuple = ()) -> tuple[Decimal, dict | None]:
    """Interpret a proven outgoing debit as paid money, retaining its sign.

    This is only used on the payment side of the reimbursement comparison.
    A negative number alone is insufficient: its original fact line must say
    money went out, without a reversal, incoming or unsuccessful qualifier.
    All other numeric evidence continues through the strict signed validator.
    """
    _sources([item["unit_id"]], documents, required=True)
    facts = documents[item["unit_id"]]["facts"]
    quote = item["quote"]
    if quote not in "\n".join(facts):
        return _operand(item, documents, path=path), None
    number = _number(item["number"])
    debits = {token.strip() for token in _quote_number_tokens(quote)
              if not token.strip().endswith(("%", "％"))
              and _number(token) < 0 and abs(_number(token)) == abs(number)}
    if len(debits) == 1:
        debit = next(iter(debits))
        # Short quotes (e.g. just "-1103.00") inherit direction only from
        # every original matching fact, never another line on the statement.
        contexts = []
        for fact in facts:
            if quote in fact:
                contexts.extend(line for line in fact.splitlines()
                                if debit in {value.strip() for value in _quote_number_tokens(line)})
        outgoing = r"转给|转出|支出|付款|支付|发出红包|红包已发出"
        ambiguous = (r"退款|退回|退还|冲正|撤销|收款(?!人|方|账户|账号)|转入|收入|入账|失败|已取消|未成功|"
                     r"(?:未|待|取消|拒绝).{0,3}(?:转给|转出|支出|付款|支付)")
        if contexts and all(re.search(outgoing, line) and not re.search(ambiguous, line)
                            for line in contexts):
            paid = -_number(debit)
            return paid, {"unit_id": item["unit_id"], "quote": quote,
                          "source_number": debit, "payment_amount": str(paid),
                          "direction_quotes": list(dict.fromkeys(contexts))}
    return _operand(item, documents, path=path), None


def calculate(comparison: dict, documents: dict, *, payment_amounts: bool = False,
              path: tuple = ()) -> dict:
    left = _operand(comparison["left"], documents, path=(*path, "left"))
    if payment_amounts:
        if (comparison["operation"] != "sum" or comparison["value_kind"] != "amount"
                or comparison["operator"] != "le"):
            raise AuditError("支付支出金额仅用于本次核销金额与逐笔支付合计比较")
        payments = [_payment_operand(item, documents, path=(*path, "right", index))
                    for index, item in enumerate(comparison["right"])]
        terms = [value for value, _ in payments]
        outflows = [{"term_index": index, **source} for index, (_, source) in enumerate(payments)
                    if source is not None]
    else:
        terms = [_operand(item, documents, path=(*path, "right", index))
                 for index, item in enumerate(comparison["right"])]
        outflows = []
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
    return {**comparison, "left_value": str(left), "right_value": str(right), "matches": matches,
            **({"payment_outflows": outflows} if outflows else {})}


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


def _entry_photo_coverage(evidence: dict | None, flags: dict, documents: list[dict],
                          materials: list[dict], units: list[dict] | None) -> dict:
    """Recompute channel thresholds from source-bound store and photo facts."""
    unknown = {"status": "unknown", "reason": "暂时不能确认本次进场门店总数及上架照片覆盖范围。",
               "source_ids": [], "comparisons": []}
    if evidence is None:
        return unknown
    validate(evidence, ENTRY_COVERAGE)
    sources = document_map(documents)
    contract = {uid for item in materials if item["id"] == "entry_agreement"
                and item["state"] == "present" for uid in item["source_ids"]}
    related = {unit["unit_id"] for unit in units or [] if unit.get("source_kind") == "qince"}
    allowed_scope = contract | related
    scope = evidence["scope_source_ids"]
    _sources(scope, sources)
    if not set(scope).issubset(allowed_scope):
        raise AuditError("进场门店范围只能引用本次产品推广协议及其关联明细，不能用申报或文件名自证")
    names = set()
    for store in evidence["stores"]:
        name, reference = store["name"].strip(), store["source"]
        uid, quote = reference["unit_id"], reference["quote"]
        if (uid not in allowed_scope or uid not in scope or not name or name in names
                or quote not in "\n".join(sources.get(uid, {}).get("facts", [])) or name not in quote):
            raise AuditError("进场门店须逐字对应本次协议明细且按同一门店去重，不能编造、重复或遗漏来源")
        names.add(name)
    linked_names = linked_activity_customer_names(units)
    if (contract and contract.issubset(related) and evidence["scope_complete"]
            and isinstance(linked_names, set) and names != linked_names):
        raise AuditError("勤策进场完整门店清单须保留本单全部已验证关联客户，不能遗漏门店或把SKU行当门店")
    total = None
    total_operand = evidence["total_stores"]
    if total_operand is not None:
        if total_operand["unit_id"] not in allowed_scope or total_operand["unit_id"] not in scope:
            raise AuditError("进场门店总数必须引用本次产品推广协议或实际关联明细")
        number = _operand(total_operand, sources, path=("entry_coverage", "total_stores"))
        if number <= 0 or number != number.to_integral_value():
            raise AuditError("进场门店总数须为原文可确认的正整数")
        total = int(number)
        if len(names) > total or (evidence["scope_complete"] and len(names) != total):
            raise AuditError("完整门店清单与引用的门店总数不一致，不能通过遗漏或重复门店改变比例")
    elif evidence["scope_complete"] and names and contract and contract.issubset(scope):
        total = len(names)
    photo_sources = {uid for item in materials if item["id"] == "shelf_photos"
                     and item["state"] in {"present", "unclear"} for uid in item["source_ids"]}
    photo_ids = [photo["unit_id"] for photo in evidence["photos"]]
    if len(photo_ids) != len(set(photo_ids)) or not set(photo_ids).issubset(photo_sources):
        raise AuditError("上架照片覆盖只能使用本次上架照片，不能重复、虚构或拿库存表充当照片")
    if units is not None:
        expected = [unit["unit_id"] for unit in units if unit["unit_id"] in photo_sources
                    and (unit.get("image") or unit.get("photo_reference"))]
        if photo_ids != expected:
            raise AuditError("上架照片覆盖须按原始顺序盘点全部实际照片，不得抽样、遗漏或增加来源")
    for photo in evidence["photos"]:
        if photo["store_name"] is not None and photo["store_name"] not in names:
            raise AuditError("照片门店须对应本次协议中实际列明的门店，无法对应时保留null")
    cited = list(dict.fromkeys([*scope, *photo_ids]))
    unknown["source_ids"] = cited
    if total is None:
        return {**unknown, "reason": "暂时不能确认本次进场门店总数，无法核对上架照片应覆盖的范围。"}
    unit, minimum = entry_photo_requirement(total, flags.get("cvs_otc") is True)
    covered = {photo["store_name"] for photo in evidence["photos"] if photo["store_name"] is not None}
    unresolved = sum(photo["store_name"] is None for photo in evidence["photos"])
    actual = len(photo_ids) if unit == "photos" else len(covered)
    status = "pass" if actual >= minimum else "fail"
    if evidence["limitations"] or (unit == "stores" and actual < minimum <= actual + unresolved):
        status = "unknown"
    label = "张上架照片" if unit == "photos" else "家门店的上架照片"
    reason = (f"本次进场{total}家，至少需{minimum}{label}，已确认{actual}{label}。")
    if status == "unknown":
        reason = "暂时不能确认上架照片覆盖是否齐全；" + reason + "；".join(evidence["limitations"])
    return {"status": status, "reason": reason, "source_ids": cited, "comparisons": [],
            "coverage": {"total_stores": total, "unit": unit, "minimum": minimum, "actual": actual,
                         "unresolved_photos": unresolved}}


def audit_decision(scenario: str, flags: dict, evidence: dict, documents: list[dict],
                   materials: list[dict] | None = None, *, units: list[dict] | None = None,
                   selected_rule_ids: list[str] | None = None, defer_native_pos: bool = False) -> dict:
    from .model_authority import enabled, decision
    if enabled():
        return decision(scenario,evidence,documents)
    from .model_judgment import apply_judgments
    reviewed = apply_judgments(scenario, flags, evidence, documents, materials, units,
        selected_rule_ids, defer_native_pos, _program_audit_decision)
    if reviewed is not None:
        return reviewed
    return _program_audit_decision(scenario, flags, evidence, documents, materials,
        units=units, selected_rule_ids=selected_rule_ids, defer_native_pos=defer_native_pos)


def _program_audit_decision(scenario: str, flags: dict, evidence: dict, documents: list[dict],
                   materials: list[dict] | None = None, *, units: list[dict] | None = None,
                   selected_rule_ids: list[str] | None = None, defer_native_pos: bool = False) -> dict:
    from .runtime_evidence_repair import load_saved_bindings
    load_saved_bindings(documents,materials)
    from .qince_amount_basis import (approval_scope_flags, program_amount_check,
                                    constrain_commission_plan_quantities, normalize_application_total_check)
    flags = approval_scope_flags(flags, documents, materials, units)
    if defer_native_pos and (selected_rule_ids is None or
            not set(selected_rule_ids).issubset({'pos_fields', 'pos_arithmetic', 'pos_product_identity'})):
        raise AuditError('仅独立POS资料分块可以推迟原生全量复算，最终审核不能跳过。')
    base = {key: value for key, value in evidence.items() if key != "photo_reviews"}
    base_schema = AUDIT_SCHEMA
    if scenario == "promotional_display":
        base_schema = deepcopy(AUDIT_SCHEMA)
        base_schema["properties"]["checks"]["items"]["properties"]["source_assertions"] = deepcopy(ASSERTIONS_SCHEMA)
    validate(base, base_schema)
    if 'payment_dates' in evidence and scenario!='personnel_incentive':raise AuditError('付款日期证据仅用于人员激励分支')
    if 'target_tiers' in evidence and scenario!='pos_target_incentive':raise AuditError('达标阶梯证据仅用于POS达标激励')
    _validate_store_incentive(scenario, flags, materials or [], document_map(documents))
    if scenario != "price_difference_support" and "price_photo_reviews" in evidence:
        raise AuditError("逐张价格证明分析仅适用于补差，不能扩展其他核销类型")
    if scenario != "price_difference_support" and "activity_dates" in evidence:
        raise AuditError("补差活动日期证据不能扩展其他核销类型")
    if scenario != "entry_fee" and "entry_coverage" in evidence:
        raise AuditError("进场门店覆盖证据只适用于条码费，不能扩展其他核销类型")
    if scenario != "giveaway_promotion" and "giveaway_coverage" in evidence:
        raise AuditError("搭赠门店照片小票覆盖证据只适用于搭赠")
    reviews = deepcopy(evidence.get("photo_reviews", []))
    if scenario != "promotional_display" and "photo_reviews" in evidence:
        raise AuditError("逐张陈列照片分析只适用于陈列堆头")
    if scenario == "promotional_display" and (selected_rule_ids is None or set(selected_rule_ids).intersection(DISPLAY_PHOTO_RULES)):
        expected = ([unit["unit_id"] for unit in display_photo_units(scenario, materials or [], units)]
                    if units is not None else [review["unit_id"] for review in reviews])
        validate_photo_reviews({"photo_reviews": reviews}, expected, documents, materials or [], units,
                               allow_unverified_size_options=True)
        for review in reviews:
            review["checks"] = [complete_size_check(check, review["unit_id"], documents, materials or [])
                                for check in review["checks"]]
        for check_index, check in enumerate(evidence["checks"]):
            if check['rule_id'] in DISPLAY_PHOTO_RULES:
                validate_display_sources(check, documents, materials or [], units or [],
                                         path=f"checks/{check_index}", strict_prose=False)
        unreadable = {unit["unit_id"] for unit in units or []
                      if unit.get("photo_reference") and not unit.get("image")}
        if any(check["status"] != "unknown" for review in reviews if review["unit_id"] in unreadable
               for check in review["checks"]):
            raise AuditError("图片未能读取时须逐项说明无法确认，不能猜测照片符合或不符")
    allowed = {r.id: r for r in rules(scenario)}
    if selected_rule_ids is not None:
        if len(set(selected_rule_ids)) != len(selected_rule_ids) or not set(selected_rule_ids).issubset(allowed):
            raise AuditError('分块审核范围必须来自本类完整规则，不能新增或重复要点。')
        allowed = {key: allowed[key] for key in selected_rule_ids}
    ids = [c["rule_id"] for c in evidence["checks"]]
    if set(ids) != set(allowed) or len(ids) != len(allowed):
        raise AuditError("审核必须覆盖全部且仅覆盖PDF登记要点，不得增加旧规则或遗漏要点")
    sources = document_map(documents)
    if 'reference_reads' in evidence:
        from .audit_rag import reference_fields, reference_fold, needed_fields, validate_reads
        indexed = reference_fields(documents, units)
        # The model receives one entry per distinct value (reference_view); the
        # same folded grouping is validated here so a grouped entry record is
        # not forced to restate one unit_id per linked row. Every real binding
        # is still present, and ids outside this batch are still rejected.
        read_fields = reference_fold({key: indexed[key] for key in needed_fields(allowed)})
        validate_reads(evidence, read_fields, require_all=False)
    price_check = (_price_photo_check(evidence.get("price_photo_reviews", []), documents,
                                     materials or [], units)
                   if scenario == "price_difference_support" and 'price_evidence' in allowed and units is not None
                   and (price_photo_units(materials or [], units) or evidence.get("price_photo_reviews")) else None)
    date_checks = (price_activity_date_checks(evidence.get("activity_dates"), documents,
                                             materials or [], units or [], online=flags.get("online") is True)
                   if scenario == "price_difference_support" and
                   {'settlement_activity_dates', 'photo_activity_dates'}.intersection(allowed) else {})
    entry_coverage = (_entry_photo_coverage(evidence.get("entry_coverage"), flags, documents,
                                          materials or [], units) if scenario == "entry_fee" and 'entry_photo_coverage' in allowed else None)
    giveaway_coverage = None
    if scenario == "giveaway_promotion" and "giveaway_coverage" in evidence:
        validate(evidence["giveaway_coverage"], GIVEAWAY_COVERAGE)
        giveaway_coverage = giveaway_coverage_check(evidence["giveaway_coverage"], documents, materials or [], units)
    material_rule_ids = {**MATERIAL_RULE_IDS,
                         "settlement": "settlement_template_seal" if "settlement_template_seal" in allowed else "settlement_seal",
                         "promotion_contract": "contract_signed"}
    material_checks = {material_rule_ids[m["id"]]: m for m in (materials or [])
                       if material_rule_ids.get(m["id"]) in allowed}
    material_labels = {r.id: r.text for r in requirements(scenario)}
    checks = []
    pos_arithmetic = (prepare_pos_arithmetic(documents, materials, units, scenario=scenario)
                      if flags.get("uses_pos") is True and not defer_native_pos else None)
    for check_index, observation in enumerate(evidence["checks"]):
        rule = allowed[observation["rule_id"]]
        check = dict(observation)
        if rule.when.startswith('available_reference:') and applicability(rule.when, scenario, flags) is False:
            check.update(status='not_applicable', comparisons=[],
                         reason='本项辅助参照不适用于本次实际审核范围；已交材料的自身审核继续。')
        check = normalize_application_total_check(scenario, check, documents, materials, units,
                                                  path=('checks', check_index))
        programmed_amount = (program_amount_check(check, documents, materials, units)
                             if applicability(rule.when, scenario, flags) is True else None)
        if programmed_amount is not None:
            check = programmed_amount
        settlement_ids = {uid for m in materials or [] if m['id'] == 'settlement' for uid in m['source_ids']}
        settlement_documents = [d for d in documents if d['unit_id'] in settlement_ids]
        check = complete_settlement_comparisons(check, settlement_documents)
        # Even an unavailable independent reference cannot legitimize fabricated
        # numbers, a forbidden operator, or numeric payload on a visual check.
        for comparison_index, comparison in enumerate(check['comparisons']):
            if not rule.numeric:raise AuditError('非数值要点不得夹带金额比较')
            validate_workbook_comparison(scenario,rule.id,comparison,documents,materials,units,
                                         comparison_index=comparison_index)
            expected_operator(scenario, rule.id, comparison, settlement_documents, comparison_index=comparison_index)
        validate_settlement_claim_coverage(check, settlement_documents)
        check = constrain_commission_plan_quantities(scenario, flags, check, sources, units=units or [])
        deterministic_target=False
        if rule.id=='target_reward':
            target=target_tier_comparison(evidence.get('target_tiers'),documents,materials,units)
            deterministic_target=target is not None
            check.update(target or {'status':'unknown','comparisons':[],
                'reason':'暂时不能确认完整奖励档位及POS实际销售总金额，不能选择比例或计算奖励。'})
        if rule.id=='payment_activity_dates' and applicability(rule.when,scenario,flags) is True:
            check.update(payment_date_check(evidence.get('payment_dates'),documents,materials,units,original=check))
        if rule.id == "price_evidence" and price_check is not None:
            check.update(price_check)
        if rule.id in date_checks:
            check.update(date_checks[rule.id])
        if rule.id == "entry_photo_coverage" and entry_coverage is not None:
            check.update(entry_coverage)
        if rule.id == "giveaway_evidence" and giveaway_coverage is not None:
            check.update(giveaway_coverage)
        if rule.id == "pos_arithmetic" and pos_arithmetic is not None:
            # Raw model observations remain intact. The published decision uses
            # every native row instead of trusting a sampled comparison array.
            scope = '逐行金额及金额合计' if scenario == 'pos_target_incentive' else '逐行金额、数量合计及金额合计'
            check.update(comparisons=pos_arithmetic["comparisons"],
                         source_ids=pos_arithmetic["source_ids"],
                         status="pass" if pos_arithmetic["complete"] else "unknown",
                         reason=(f"POS全部销售明细的{scope}已按原精度复算。"
                                 if pos_arithmetic["complete"] else "；".join(pos_arithmetic["limitations"])))
        check = constrain_application_check(scenario,rule,check,documents,materials,units)
        material = material_checks.get(rule.id)
        missing = material is not None and material["state"] == "missing"
        _sources(check["source_ids"], sources,
                 required=check["status"] in {"pass", "fail"} and not (missing and check["status"] == "fail"))
        if (scenario == "poster_material" and rule.id == "invoice_details" and check["status"] == "pass"
                and material is not None and material["state"] == "present"
                and not set(material["source_ids"]).intersection(check["source_ids"])):
            raise AuditError("票据信息检查通过时须引用本次发票或收据；合同、结算单可补充明细，不能代替票据")
        if (scenario == "entry_fee" and rule.id == "entry_inventory" and check["status"] == "pass"
                and material is not None and material["state"] == "present"
                and not set(material["source_ids"]).intersection(check["source_ids"])):
            raise AuditError("全量终端库存盖章检查通过须引用本次实际库存明细，不能只引用协议或照片")
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
            raise AuditError(f"审核条件不明时不得自动通过或免审；要点{rule.id}的{rule.when}=null，只能返回unknown。")
        elif check["status"] == "not_applicable":
            raise AuditError(f"不适用仅允许PDF明文的条件例外，不能免除必审要点；要点{rule.id}的{rule.when}=true。"
                             "未取得本单独立参照值时返回unknown及具体缺项，不能因缺数据返回not_applicable。")
        if (not rule.numeric or applies is not True or check["status"] == "not_applicable") and check["comparisons"]:
            raise AuditError("非数值要点或适用条件未确认的项目不得夹带金额比较")
        if rule.numeric and check["status"] in {"pass", "fail"} and not check["comparisons"] and not deterministic_target:
            raise AuditError("数量金额要点必须提供原文数值，由程序复算，AI不能只给通过/不通过")
        if (check["status"] == "pass" and rule.id in CONTRACT_REFERENCE_RULES.get(scenario, ())
                and materials is not None
                and not (scenario == 'promotional_display' and reviews and rule.id in DISPLAY_PHOTO_RULES)):
            # Per-photo sources have already been validated above. The final
            # aggregate is rebuilt from those rows below, so an unused model
            # aggregate must not force a repeat of the complete final review.
            contract_id = "entry_agreement" if scenario == "entry_fee" else "promotion_contract"
            contract_sources = {uid for item in materials
                                if item["id"] == contract_id and item["state"] == "present"
                                for uid in item["source_ids"]}
            if not contract_sources.intersection(check["source_ids"]):
                raise AuditError("本类活动约定比较通过时须引用品牌方本次合同或产品推广协议，不能只引用申报或现场资料")
        for comparison_index, comparison in enumerate(check["comparisons"]):
            validate_workbook_comparison(scenario,rule.id,comparison,documents,materials,units,
                                         comparison_index=comparison_index)
            expected_operator(scenario, rule.id, comparison, settlement_documents, comparison_index=comparison_index)
            if (rule.id in CONTRACT_COMPARISON_RULES.get(scenario, ())
                    and materials is not None):
                _validate_contract_comparison(rule.id, comparison, materials)
            if rule.id == 'settlement_calculation':
                validate_settlement_sources(comparison, settlement_documents, comparison_index=comparison_index)
            if rule.id == "payment_claim_amount":
                _validate_payment_comparison(comparison, materials)
            elif rule.id == "pos_arithmetic":
                _validate_pos_comparison(comparison, materials)
            elif rule.id == "settlement_pos_quantity":
                _validate_settlement_pos_quantity(comparison, materials)
            if comparison["operation"] in {"ratio", "floor_ratio"} and rule.id != "gift_rule_quantity":
                raise AuditError("赠送比例公式只能用于按实际赠送规则核验赠品数量")
            if (comparison["left"] in comparison["right"]
                    and not separate_settlement_product_cells(rule.id, comparison, documents)):
                raise AuditError("数值比对不能用同一原值证明自身，须引用独立的对应依据")
        calculations = [calculate(c, sources, payment_amounts=rule.id == "payment_claim_amount",
                                  path=("checks", check_index, "comparisons", comparison_index))
                        for comparison_index, c in enumerate(check["comparisons"])]
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
        if rule.id == "pos_arithmetic" and pos_arithmetic is not None and not pos_arithmetic["complete"]:
            if check["status"] == "fail":
                check["reason"] += "；" + "；".join(pos_arithmetic["limitations"])
        if rule.id == "pos_arithmetic" and pos_arithmetic is not None:
            check["coverage"] = {key: pos_arithmetic[key] for key in ("complete", "tables", "limitations")}
        check.update(effect=rule.effect, requirement=rule.text, calculations=calculations)
        if material and applies is not False and material["state"] in {"missing", "unclear"}:
            label = material_labels[material["id"]]
            check.update(status="fail" if missing else "unknown", effect="missing_material",
                         reason=("未提供" if missing else "未能确认") + label + "；" + material["reason"],
                         source_ids=list(material["source_ids"]), comparisons=[], calculations=[])
        checks.append(check)
    _merge_photo_reviews(checks, reviews)
    checks=[constrain_application_check(scenario,allowed[c['rule_id']],c,documents,materials,units) for c in checks]
    if 'reference_reads' in evidence:
        from .audit_rag import constrain_read_check
        checks = [constrain_read_check(check, read_fields) for check in checks]
    for review in reviews:
            review['checks']=[constrain_application_check(scenario,allowed[c['rule_id']],c,documents,materials,units)
                              if c['rule_id'] in allowed else c for c in review['checks']]
    problems = [c for c in checks if c["status"] in {"fail", "unknown"}]
    return {"policy_version": POLICY_VERSION, "scenario": scenario, "checks": checks,
            **({'payment_dates':deepcopy(evidence['payment_dates'])} if scenario=='personnel_incentive' and 'payment_dates' in evidence else {}),
            **({'target_tiers':deepcopy(evidence['target_tiers'])} if scenario=='pos_target_incentive' and 'target_tiers' in evidence else {}),
            **({"price_photo_reviews": deepcopy(evidence.get("price_photo_reviews", []))}
               if scenario == "price_difference_support" else {}),
            **({"activity_dates": deepcopy(evidence["activity_dates"])}
               if scenario == "price_difference_support" and "activity_dates" in evidence else {}),
            **({"entry_coverage": deepcopy(evidence["entry_coverage"])} if scenario == "entry_fee" and "entry_coverage" in evidence else {}),
            **({"giveaway_coverage": deepcopy(evidence["giveaway_coverage"])}
               if scenario == "giveaway_promotion" and "giveaway_coverage" in evidence else {}),
            **({"photo_reviews": deepcopy(reviews)} if scenario == "promotional_display" else {}),
            "summary": {"conclusion": "issues_found" if problems else "no_issues_found",
                        "error_count": len(problems)},
            "noncompliance_registration": [c["reason"] for c in problems]}
