"""Source-bound activity dates for price-difference support only."""
from __future__ import annotations

from datetime import date
from pathlib import PurePosixPath
import re
import unicodedata

from .common import AuditError


def _object(properties):
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


_TEXT = {"type": "string", "minLength": 1}
_QUOTE = _object({"unit_id": _TEXT, "quote": _TEXT})
_PERIOD = _object({
    "source_ids": {"type": "array", "uniqueItems": True, "items": _TEXT},
    "periods": {"type": "array", "items": _QUOTE},
    "limitations": {"type": "array", "items": _TEXT},
})
PRICE_ACTIVITY_DATES_SCHEMA = _object({
    "contract": _PERIOD, "settlement": _PERIOD,
    "photos": {"type": "array", "items": _object({
        "unit_id": _TEXT, "quote": {"type": ["string", "null"], "minLength": 1}})},
})
_FULL_DATE = re.compile(r"(?<!\d)(\d{4})\s*(?:年|[-./])\s*(\d{1,2})\s*(?:月|[-./])\s*(\d{1,2})\s*[日号]?(?!\d)")
_PHOTO_DATE = re.compile(_FULL_DATE.pattern + r"|(?<!\d)(\d{4})[ \t]+(\d{1,2})[ \t]+(\d{1,2})(?!\d)")
_CONNECTOR = re.compile(r"\s*(?:至|到|[-—–~～]{1,2})\s*")
_SHORT_END = re.compile(r"\s*(?:至|到|[-—–~～]{1,2})\s*(\d{1,2})\s*(?:月|[./-])\s*(\d{1,2})\s*[日号]?(?!\d)")
_ACTIVITY_LABEL = re.compile(r"(?:活动|促销|执行).{0,12}(?:日期|时间|期间|周期|起止)|(?:日期|时间|期间|周期|起止).{0,12}(?:活动|促销|执行)")
_UNCERTAIN = re.compile(r"疑似|可能|似乎|大概|看不清|无法辨认|无法确认|不能确认|模糊|遮挡|待定|不详|未填|未明确|待确认")
_OTHER_DATE = re.compile(r"签订|签署|签章|付款|打款|支付|开票|制单|打印|有效期|效期|生产日期|保质期|落款|收货|到货|发货|出库|入库|物流")


def _require(value, message):
    if not value:
        raise AuditError(message)


def _keys(value, expected):
    _require(isinstance(value, dict) and set(value) == set(expected), "补差日期证据结构不完整或含额外字段")


def _strings(value):
    _require(isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value),
             "补差日期证据来源或限制须为非空文字列表")


def _parsed_dates(quote, *, spaced=False):
    text = unicodedata.normalize("NFKC", quote)
    matches = list((_PHOTO_DATE if spaced else _FULL_DATE).finditer(text))
    try:
        values = [date(*(int(part) for part in (match.groups()[:3] if match[1] else match.groups()[3:])))
                  for match in matches]
    except ValueError:
        return text, matches, None
    return text, matches, values


def _period(quote):
    text, matches, values = _parsed_dates(quote)
    if _UNCERTAIN.search(text) or (_OTHER_DATE.search(text) and not _ACTIVITY_LABEL.search(text)):
        return None
    if not values or len(values) > 2:
        return None
    if len(values) == 2:
        between = text[matches[0].end():matches[1].start()]
        labeled = (re.search(r"开始|起始|起期", text[:matches[0].start()])
                   and re.search(r"结束|截止|终止|止期", between))
        if not (_CONNECTOR.fullmatch(between) or labeled) or values[1] < values[0]:
            return None
        return values[0], values[1]
    tail = text[matches[0].end():]
    short = _SHORT_END.match(tail)
    if short:
        try:
            end = date(values[0].year, int(short[1]), int(short[2]))
        except ValueError:
            return None
        # Only this explicit range supplies a year. Never infer a rollover.
        return (values[0], end) if end >= values[0] else None
    if _CONNECTOR.match(tail) or re.search(r"\d{1,2}\s*月\s*\d{1,2}\s*日", text[:matches[0].start()]):
        return None
    if re.search(r"开始|起始|起期|截止|结束|终止|止期", text):
        return None  # A lone endpoint is not an explicitly single-day activity.
    return values[0], values[0]


def _photo_date(quote):
    text, matches, values = _parsed_dates(quote, spaced=True)
    if (_UNCERTAIN.search(text) or _OTHER_DATE.search(text) or not values or len(values) != 1
            or _CONNECTOR.match(text[matches[0].end():])):
        return None
    return values[0]


def _source_quote(item, allowed, documents, *, path=()):
    _keys(item, ("unit_id", "quote"))
    uid, quote = item["unit_id"], item["quote"]
    _require(isinstance(uid, str) and uid in allowed and uid in documents, "补差日期证据引用了错误资料角色或未知来源")
    if not (isinstance(quote, str) and quote.strip()
            and any(quote in fact for fact in documents[uid])):
        from .pdf_evidence import OperandEvidenceError
        # Hint complete original date lines from this proven unit first. No
        # date normalization or cross-unit substitution can establish a match.
        facts = sorted(documents[uid], key=lambda fact: not _FULL_DATE.search(unicodedata.normalize('NFKC', fact)))
        raise OperandEvidenceError('补差日期必须逐字引用该来源原文，不能拼接或改写日期',
                                   item, facts, path, 'quote')
    return uid, quote


def _uncertain_or_other_context(uid, quote, documents, *, activity):
    """Keep the source clause when a model quotes only a clean-looking date."""
    for fact in documents[uid]:
        if quote not in fact:
            continue
        clauses = re.split(r"[；;。\n，,]", fact)
        indexes = [i for i, clause in enumerate(clauses) if quote in clause]
        contexts = [clauses[i] for i in indexes] or [fact]
        for index in indexes:
            if index + 1 < len(clauses) and _UNCERTAIN.match(clauses[index + 1].strip()):
                contexts.append(clauses[index + 1])
        for context in contexts:
            if _UNCERTAIN.search(context):
                return True
            if _OTHER_DATE.search(context) and (not activity or not _ACTIVITY_LABEL.search(context)):
                return True
    return False


def _verified_signing_date_note(note, allowed, documents):
    """A source-backed exclusion of a signing date is not an activity-date gap.

    Accept only a standalone, unambiguous explanation with a matching signing
    page in this material role. Mixed or unsupported limitations still block.
    The original note remains in the saved evidence, unchanged.
    """
    text, _, values = _parsed_dates(note)
    clauses = [part.strip() for part in re.split(r"[；;。\n，,]", text) if part.strip()]
    if (not values or len(values) != 1 or len(clauses) not in {2, 3}
            or len(clauses) == 3 and not re.fullmatch(r"属(?:签订|签署|签章|落款)日期", clauses[1])
            or _UNCERTAIN.search(text) or _ACTIVITY_LABEL.search(clauses[0])
            or not re.search(r"签订|签署|签章|落款", clauses[0])
            or not re.fullmatch(r"不(?:能|应)?(?:作为|用作|用于)(?:本次)?(?:活动|促销|执行)"
                                r"(?:期间|周期|日期|时间)(?:使用|依据)?", clauses[-1])):
        return False
    for uid in allowed:
        facts = documents[uid]
        # A real activity statement (even an ambiguous one) cannot be dismissed
        # as a signing-only page by a model-supplied note.
        if any(_ACTIVITY_LABEL.search(fact) for fact in facts):
            continue
        if not any(re.search(r"签章确认|(?:签订|签署|签章|落款)(?:日期|时间)", fact) for fact in facts):
            continue
        source_dates = []
        valid = True
        for fact in facts:
            _, matches, parsed = _parsed_dates(fact)
            if not matches:
                continue
            if (not parsed or _UNCERTAIN.search(fact)
                    or re.search(r"付款|打款|支付|开票|有效期|效期|生产|保质|收货|到货|发货|出库|入库|物流", fact)):
                valid = False
                break
            source_dates.extend(parsed)
        if valid and source_dates and set(source_dates) == set(values):
            return True
    return False


def _verified_single_period_note(note, allowed, documents):
    """A source-backed statement that only one page states a period is not a gap.

    This recognizes an affirmative coverage note, not an arbitrary limitation.
    Actual missing, ambiguous or conflicting activity dates still block above.
    """
    text, _, values = _parsed_dates(note)
    clauses = [part.strip() for part in re.split(r"[；;。\n，,]", text) if part.strip()]
    if (values or len(clauses) != 2 or _UNCERTAIN.search(text)
            or re.search(r"缺|遗漏|未|不完整|不足|冲突|不一致", text)
            or not re.fullmatch(r"(?:合同|结算单)[^；;。\n，,]{0,60}仅(?:第[一二三四五六七八九十0-9]+页)?"
                                r"(?:写有|载有|列有|明确写有)(?:执行|活动|促销)(?:周期|期间|日期|时间)", clauses[0])
            or not re.fullmatch(r"无其他(?:可比较的)?(?:活动|促销|执行)(?:期间|周期|日期|时间)", clauses[1])):
        return False
    periods, sources = set(), set()
    for uid in allowed:
        for fact in documents[uid]:
            if not _ACTIVITY_LABEL.search(fact):
                continue
            period = _period(fact)
            if period is None:
                return False
            periods.add(period)
            sources.add(uid)
    return len(periods) == 1 and len(sources) == 1


def _resolve_period(value, allowed, documents, label, *, path=()):
    _keys(value, ("source_ids", "periods", "limitations"))
    _strings(value["source_ids"]); _strings(value["limitations"])
    _require(len(value["source_ids"]) == len(set(value["source_ids"]))
             and set(value["source_ids"]) == allowed, "补差活动周期须完整覆盖本次合同或结算单来源，不能挑选单页")
    _require(isinstance(value["periods"], list), "补差活动周期引用须为列表")
    resolved, uncertain = set(), []
    for index, item in enumerate(value["periods"]):
        uid, quote = _source_quote(item, allowed, documents, path=(*path, 'periods', index))
        period = None if _uncertain_or_other_context(uid, quote, documents, activity=True) else _period(quote)
        if period is None:
            _, _, values = _parsed_dates(quote)
            if values and len(values) == 2 and values[1] < values[0]:
                uncertain.append(f"{label}活动日期原文为{values[0]}至{values[1]}，结束日期早于开始日期，暂时不能确认")
            else:
                uncertain.append(f"{label}活动周期原文缺少明确年份、日期不完整或无法确认")
        else:
            resolved.add(period)
    # A model cannot cite one page and hide an explicitly conflicting activity
    # period on another covered page. Do not scan signing/invoice dates as scope.
    for uid in allowed:
        for fact in documents[uid]:
            if _ACTIVITY_LABEL.search(fact) and _FULL_DATE.search(unicodedata.normalize("NFKC", fact)):
                period = _period(fact)
                if period is None:
                    uncertain.append(f"{label}中活动日期原文存在歧义，暂时不能确认完整周期")
                else:
                    resolved.add(period)
    if len(resolved) > 1:
        uncertain.append(f"{label}不同原文中的活动周期不一致，暂时不能确认本次适用周期")
    if value["limitations"]:
        uncertain.extend(f"{label}：{reason}" for reason in value["limitations"]
                         if not (len(resolved) == 1 and not uncertain
                                 and (_verified_signing_date_note(reason, allowed, documents)
                                      or _verified_single_period_note(reason, allowed, documents))))
    if not value["periods"] or not allowed or not resolved:
        uncertain.append(f"暂时不能确认{label}本次活动起止日期")
    return (next(iter(resolved)) if len(resolved) == 1 and not uncertain else None,
            list(dict.fromkeys(uncertain)))


def _check(status, reason, sources):
    return {"status": status, "reason": reason, "source_ids": sorted(set(sources)), "comparisons": []}


def _display(unit):
    name = PurePosixPath(str(unit.get("source_file") or unit["unit_id"]).replace("\\", "/")).name
    locator = unit.get("locator")
    return f"《{name}》" + (f"（{locator}）" if isinstance(locator, str) and locator and locator != name else "")


def price_activity_date_checks(evidence, documents, materials, units, *, online=False):
    """Return two nonnumeric checks. Only the caller's trusted online flag exempts photos."""
    _require(type(online) is bool, "补差线上例外须来自已确认条件，不能由日期证据自行声明")
    source_map = {}
    for document in documents:
        uid = document["unit_id"]
        _require(uid not in source_map, "补差日期事实包含重复来源")
        _strings(document["facts"])
        source_map[uid] = document["facts"]
    def role(name, states=("present",)):
        ids = {uid for item in materials or [] if item["id"] == name and item["state"] in states
               for uid in item["source_ids"]}
        _require(ids.issubset(source_map), "补差日期资料包含未读取来源")
        return ids
    contract_ids, settlement_ids = role("promotion_contract"), role("settlement")
    # Unclear price content does not remove an actually supplied photo from date coverage.
    photo_ids = role("price_evidence", ("present", "unclear"))
    expected = [unit for unit in units or [] if unit["unit_id"] in photo_ids
                and (unit.get("image") or unit.get("photo_reference"))]
    expected_ids = [unit["unit_id"] for unit in expected]
    _require(len(expected_ids) == len(set(expected_ids)), "补差照片来源重复，不能确认逐张日期")
    if evidence is None:
        return {"settlement_activity_dates": _check("unknown", "暂时不能确认结算单与促销合同的活动周期是否一致", contract_ids | settlement_ids),
                "photo_activity_dates": _check("not_applicable" if online else "unknown",
                    "线上截图不套用现场照片日期检查" if online else "暂时不能确认照片水印日期是否在促销合同活动周期内", [])}
    _keys(evidence, ("contract", "settlement", "photos"))
    contract, contract_unknown = _resolve_period(evidence["contract"], contract_ids, source_map, "促销合同", path=('activity_dates', 'contract'))
    settlement, settlement_unknown = _resolve_period(evidence["settlement"], settlement_ids, source_map, "结算单", path=('activity_dates', 'settlement'))
    if contract is None or settlement is None:
        originals = [f"{label}活动周期原文：{'；'.join(dict.fromkeys(item['quote'] for item in evidence[key]['periods']))}"
                     for key, label in (("settlement", "结算单"), ("contract", "促销合同"))
                     if evidence[key]["periods"]]
        settlement_check = _check("unknown", "；".join(dict.fromkeys(originals + contract_unknown + settlement_unknown)), contract_ids | settlement_ids)
    elif contract != settlement:
        settlement_check = _check("fail", f"结算单活动周期{settlement[0]}至{settlement[1]}与促销合同约定的{contract[0]}至{contract[1]}不一致", contract_ids | settlement_ids)
    else:
        settlement_check = _check("pass", f"结算单活动周期与促销合同一致（{contract[0]}至{contract[1]}）", contract_ids | settlement_ids)
    _require(isinstance(evidence["photos"], list), "补差照片日期证据须逐张列出")
    if online:
        _require(not evidence["photos"], "线上截图不应填入现场照片日期检查")
        photo_check = _check("not_applicable", "线上截图不套用现场照片日期检查", [])
    else:
        by_id = {}
        photo_indexes = {}
        for index, item in enumerate(evidence["photos"]):
            _keys(item, ("unit_id", "quote"))
            _require(isinstance(item["unit_id"], str) and item["unit_id"] not in by_id,
                     "补差照片日期来源重复或不合法")
            by_id[item["unit_id"]] = item
            photo_indexes[item['unit_id']] = index
        _require(set(by_id) == set(expected_ids), "补差照片日期必须完整覆盖本次实际价格证明图片，不能漏图或增加来源")
        failures, unknowns, problem_ids = [], list(contract_unknown), set()
        if not expected:
            unknowns.append("暂时不能确认可核对日期的现场价格证明照片")
        elif contract is None:
            unknowns.append(f"本次全部{len(expected)}张现场价格证明照片均因合同活动周期无法确认，暂时不能判断拍摄日期是否在活动期内")
            problem_ids.update(expected_ids)
        for unit in expected:
            item = by_id[unit["unit_id"]]
            value = None
            if item["quote"] is not None:
                uid, quote = _source_quote(item, {unit["unit_id"]}, source_map,
                                           path=('activity_dates', 'photos', photo_indexes[unit['unit_id']]))
                value = None if _uncertain_or_other_context(uid, quote, source_map, activity=False) else _photo_date(quote)
            if value is None or not unit.get("image"):
                unknowns.append(f"{_display(unit)}拍摄日期缺失、看不清或缺少明确年份，暂时不能确认")
                problem_ids.add(unit["unit_id"])
            elif contract is not None and not contract[0] <= value <= contract[1]:
                failures.append(f"{_display(unit)}水印拍摄日期{value}不在促销合同活动周期{contract[0]}至{contract[1]}内")
                problem_ids.add(unit["unit_id"])
        status = "fail" if failures else "unknown" if unknowns else "pass"
        reason = "；".join(dict.fromkeys(failures + unknowns)) or f"全部现场价格证明照片的拍摄日期均在促销合同活动周期{contract[0]}至{contract[1]}内"
        photo_check = _check(status, reason, contract_ids | (set(expected_ids) if status == "pass" else problem_ids))
    return {"settlement_activity_dates": settlement_check, "photo_activity_dates": photo_check}
