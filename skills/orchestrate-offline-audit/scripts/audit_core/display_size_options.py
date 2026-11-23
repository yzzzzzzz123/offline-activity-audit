"""Source-bound alternatives for a complete endcap OR an area display.

This validates the relationship between visual judgements; it does not measure
photos, decide whether a fixture is complete, or rewrite historical decisions.
"""
from __future__ import annotations

import re
from copy import deepcopy

from .common import AuditError
from .pdf_policy import DISPLAY_SIZE_ESTIMATION_CLARIFICATION


def _object(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


_TEXT = {"type": "string", "minLength": 1, "maxLength": 2400}
_QUOTE = _object({"unit_id": _TEXT, "quote": _TEXT})
SIZE_OPTIONS_SCHEMA = _object({
    "contract_source": _QUOTE,
    "selected_option": {"type": "string", "enum": ["either", "whole_endcap", "area"]},
    "restriction_source": {"anyOf": [_QUOTE, {"type": "null"}]},
    "options": {"type": "array", "minItems": 2, "maxItems": 2, "items": _object({
        "kind": {"type": "string", "enum": ["whole_endcap", "area"]},
        "status": {"type": "string", "enum": ["pass", "fail", "unknown"]},
        "reason": _TEXT,
        "source_ids": {"type": "array", "minItems": 2, "uniqueItems": True,
                       "items": {"type": "string", "minLength": 1}},
    })},
})

_AREA = r"(?:\d+(?:\.\d+)?|[一二三四五六七八九十]+)\s*(?:平方米|平米|㎡|m²|m2)\s*(?:堆头|地堆)?"
_EITHER = re.compile(r"整组端架\s*(?:或|或者)\s*" + _AREA
                     + r"|" + _AREA + r"\s*(?:或|或者)\s*整组端架", re.I)


def alternative_contract_sources(documents, materials):
    contracts = {uid for item in materials if item["id"] == "promotion_contract"
                 and item["state"] == "present" for uid in item["source_ids"]}
    return {doc["unit_id"] for doc in documents if doc["unit_id"] in contracts
            and any(_EITHER.search(fact) for fact in doc.get("facts", []))}


def alternative_status(statuses):
    """An unknown alternative cannot cancel an already proven alternative."""
    if "pass" in statuses:
        return "pass"
    return "fail" if statuses and all(s == "fail" for s in statuses) else "unknown"


def validate_size_options(check, photo_id, documents, materials, *, required=False):
    comparison = check.get("size_options")
    contract_ids = alternative_contract_sources(documents, materials)
    if comparison is None:
        if required and contract_ids and check["rule_id"] == "display_size":
            raise AuditError("合同为整组端架或面积二选一，须分别提供两种规格的判断，不能只报无面积参照")
        return
    if check["rule_id"] != "display_size":
        raise AuditError("规格分支仅用于陈列大小，不改变水印或品牌检查")
    sources = {doc["unit_id"]: doc for doc in documents}
    contracts = {uid for item in materials if item["id"] == "promotion_contract"
                 and item["state"] == "present" for uid in item["source_ids"]}

    def quoted(source):
        return source["unit_id"] in contracts and any(
            source["quote"] in fact for fact in sources.get(source["unit_id"], {}).get("facts", []))

    basis = comparison["contract_source"]
    if not quoted(basis) or not _EITHER.search(basis["quote"]):
        raise AuditError("规格二选一须逐字引用本次合同的或关系原文，申请字段和读取限制不能代替合同")
    if basis["unit_id"] not in check["source_ids"]:
        raise AuditError("陈列大小须保留实际引用的合同来源")
    options = comparison["options"]
    if {item["kind"] for item in options} != {"whole_endcap", "area"}:
        raise AuditError("整组端架和面积须各核对一次，不能重复一种或遗漏另一种")
    for item in options:
        if (photo_id not in item["source_ids"] or basis["unit_id"] not in item["source_ids"]
                or not set(item["source_ids"]).issubset(check["source_ids"])):
            raise AuditError("每种规格的判断须保留本图和合同来源，不借其他照片代替")
    selected = comparison["selected_option"]
    restriction = comparison["restriction_source"]
    if selected == "either":
        if restriction is not None:
            raise AuditError("未收窄合同二选一时不得附加排他性约定")
        expected = alternative_status([item["status"] for item in options])
    else:
        marker = "整组端架" if selected == "whole_endcap" else _AREA
        if (restriction is None or not quoted(restriction)
                or restriction["unit_id"] not in check["source_ids"]
                or _EITHER.search(restriction["quote"])
                or re.search(r"不得|不能|不应|不允许|不采用|无需|未限定|不限", restriction["quote"])
                or not re.search(r"(?:仅|只|限定为|限于|指定为|采用|执行)[^，。；;\n]{0,18}" + marker,
                                 restriction["quote"], re.I)):
            raise AuditError("收窄二选一须引用本次合同明确的执行约定，不能仅凭勤策1平堆字段排除端架")
        expected = next(item["status"] for item in options if item["kind"] == selected)
    if check["status"] != expected:
        raise AuditError("陈列大小结论与合同适用分支不一致，已满足一个可选规格时不能因另一项未知而判未知")


def size_options_issue(check, photo_id, documents, materials, *, required=False):
    """Only an auxiliary specification failure is recoverable, never source/amount validation."""
    try:
        validate_size_options(check, photo_id, documents, materials, required=required)
    except AuditError as exc:
        return str(exc)
    return None


def complete_size_check(check, photo_id, documents, materials, *, required=False):
    """Keep the visual judgement; invalid auxiliary metadata is diagnostic only.

    Counts, packaging estimates and the actual contract are judged by the model.
    A legacy size_options shape is not an additional business approval gate.
    Primary photo/source validation remains in validate_photo_reviews.
    """
    result = deepcopy(check)
    issue = size_options_issue(check, photo_id, documents, materials, required=required)
    if issue is None:
        return result
    result.pop("size_options", None)
    return result


SIZE_OPTIONS_PROMPT = (
    "合同明确为整组端架或面积二选一时，display_size另填size_options："
    "contract_source逐字引用本次合同或关系原文；options分别列whole_endcap、area各一次，"
    "各自返回status、具体reason及包含本图和合同的source_ids。"
    "整组端架先看架体结构及完整陈列边界，不要求面积、尺子或占地标识；完整范围可确认时，"
    "不能仅因无面积参照将端架分支记unknown。边缘截断、背侧遮挡或无法判断是否完整端架时，具体说明该限制。"
    "面积分支允许按商品包装、可见排列及边界合理估算，说明参照、方向、数量及不确定范围，不能凭规模猜平方米。"
    "没有本次合同明确的逐店限定时selected_option=either、restriction_source=null；"
    "本单关联申请字段仅写1平堆，不自行取消合同允许的端架选项。"
    "确有本次合同明确的逐店执行约定才选whole_endcap或area，并在restriction_source逐字引用该限定，"
    "不可借用其他门店的限定；不同来源约定确实冲突时具体说明，不自行决定合同被替换。"
    "either时任一分支pass则本图display_size为pass，两分支均fail才为fail，"
    "没有pass且至少一项unknown则为unknown；限定单一规格时仅按被限定分支的结果。"
    "只把仍无法满足或无法确认的适用规格写入问题reason，不把另一未选分支的尺寸未知继续当成问题。"
    "没有上述二选一约定时不输出size_options，沿用合同实际规格核对，不新增资料或审核项。"
) + DISPLAY_SIZE_ESTIMATION_CLARIFICATION
