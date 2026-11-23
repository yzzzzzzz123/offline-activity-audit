"""Read activity periods from the complete received document, without AI.

Year inheritance uses explicit activity periods in the same source unit, or
the exactly corresponding period of this case's unique promotion contract.
Application, signing, payment and filename dates never supply a missing year.
Original facts/quotes stay unchanged; every derived date keeps its sources.
"""
from __future__ import annotations

from datetime import date
import re
import unicodedata

from .price_support_dates import _ACTIVITY_LABEL, _FULL_DATE, _OTHER_DATE, _UNCERTAIN, _period

_NARRATIVE = re.compile(r'(?:于|自|从)\s*\d{4}\s*(?:年|[-./]).{0,90}?(?:进行|开展|举办).{0,60}?(?:促销|活动|激励|陈列)')
_FIELD = re.compile(r'(?:活动|执行|促销)(?:起止日期|开始日期|结束日期|时间|日期|周期|期间|起止)\s*[：:|｜]')
_PHASE = re.compile(r'(?:第\s*)?(?:\d{2,6}|[一二三四五六七八九十]+)\s*(?:档(?:期)?|阶段)\s*[:：]')
_MD = r'(\d{1,2})\s*(?:月|[./-])\s*(\d{1,2})\s*[日号]?'
_SHORT_RANGE = re.compile(r'(?<![\d.])' + _MD + r'\s*(?:至|到|[-—–~～]{1,2})\s*' + _MD + r'(?!\d)')


def _records(uid, facts, year_context=()):
    records, pending, limitations = [], [], []
    for index, fact in enumerate(facts):
        active = False
        for raw in re.split(r'[；;。\n]', fact):
            text = unicodedata.normalize('NFKC', raw).strip()
            if not text:
                continue
            labelled = bool(_ACTIVITY_LABEL.search(text))
            narrative = bool(_NARRATIVE.search(text))
            phase = _PHASE.search(text)
            if not (labelled or narrative or active and phase):
                continue
            active = labelled or narrative or bool(active and phase)
            # A prose mention of "during this activity" with no date field is
            # not an additional blank date field.
            has_date = bool(_FULL_DATE.search(text) or _SHORT_RANGE.search(text))
            if not has_date and not _FIELD.search(text):
                continue
            source = {'unit_id': uid, 'fact_index': index, 'quote': fact}
            if _OTHER_DATE.search(text) and not labelled:
                continue
            period = _period(text)
            item = {'sources': [source], 'phase': phase[0] if phase else None}
            if period is not None:
                records.append({**item, 'start': period[0].isoformat(), 'end': period[1].isoformat(),
                                'basis': 'explicit_activity_period'})
                continue
            short = _SHORT_RANGE.search(text)
            if short and not _FULL_DATE.search(text) and not _UNCERTAIN.search(text):
                pending.append((item, tuple(map(int, short.groups()))))
            else:
                limitations.append({**source, 'code': 'activity_date_unclear' if _UNCERTAIN.search(text)
                                    else 'activity_date_incomplete'})
    # Resolve the printed shorthand only against an independently stated full
    # activity period in this same page/image/native unit, not expected dates.
    for item, month_days in pending:
        candidates = {}
        for anchor in records:
            if anchor['basis'] != 'explicit_activity_period':
                continue
            start, end = date.fromisoformat(anchor['start']), date.fromisoformat(anchor['end'])
            try:
                if start.year == end.year:
                    candidate = (date(start.year, *month_days[:2]), date(start.year, *month_days[2:]))
                elif month_days == (start.month, start.day, end.month, end.day):
                    candidate = start, end
                else:
                    continue
            except ValueError:
                continue
            if candidate[1] >= candidate[0]:
                candidates.setdefault(candidate, []).extend(anchor['sources'])
        external = False
        if not candidates:
            for anchor in year_context:
                start, end = date.fromisoformat(anchor['start']), date.fromisoformat(anchor['end'])
                # A related contract supplies a year only for the exact same
                # printed month/day pair, not an expected value to overwrite.
                if month_days == (start.month, start.day, end.month, end.day):
                    candidates.setdefault((start, end), []).extend(anchor['sources'])
                    external = True
        if len(candidates) == 1:
            (start, end), anchors = next(iter(candidates.items()))
            sources = item['sources'] + [s for s in anchors if s not in item['sources']]
            records.append({**item, 'sources': sources, 'start': start.isoformat(), 'end': end.isoformat(),
                            'basis': 'related_contract_activity_year' if external else 'same_source_activity_year'})
        else:
            limitations.append({**item['sources'][0], 'code': 'activity_year_ambiguous' if candidates
                                else 'activity_year_not_stated'})
    return records, limitations


def read_activity_periods(documents, source_ids, *, year_context=()):
    """Return complete dates and source proof, with safe phase coverage only."""
    records, limitations = [], []
    for uid in sorted(set(source_ids)):
        found, problems = _records(uid, documents.get(uid, []), year_context)
        records.extend(found)
        limitations.extend(problems)
    intervals = sorted({(r['start'], r['end']) for r in records})
    phase_ranges = {}
    for record in records:
        if record['phase']:
            phase_ranges.setdefault(record['phase'], set()).add((record['start'], record['end']))
    coverage = []
    if len(intervals) <= 1:
        coverage = intervals
    elif all(r['phase'] for r in records) and all(len(values) == 1 for values in phase_ranges.values()):
        # Only explicitly named phases can form a union. Never take min/max
        # across different versions or hide a real gap between phases.
        for start, end in intervals:
            if coverage and (date.fromisoformat(start) - date.fromisoformat(coverage[-1][1])).days <= 1:
                coverage[-1] = (coverage[-1][0], max(coverage[-1][1], end))
            else:
                coverage.append((start, end))
    state = ('unknown' if limitations or intervals and not coverage else
             'available' if coverage else 'missing')
    return {'state': state, 'periods': records, 'coverage': [list(p) for p in coverage],
            'limitations': limitations, 'model_calls': 0,
            'scope': '日期按原值核对；简写年份仅由同一来源，或本单唯一合同中月日完全对应的明确活动周期证明，保留两方原文。'}


def case_activity_periods(documents, materials):
    """Two existing material roles only; no query or unrelated source search."""
    ids = {role: {uid for material in materials or [] if material['id'] == role
                  and material['state'] in {'present', 'unclear'} for uid in material['source_ids']}
           for role in ('promotion_contract', 'settlement')}
    contract = read_activity_periods(documents, ids['promotion_contract'])
    context = (contract['periods'] if contract['state'] == 'available' and len(contract['coverage']) == 1
               and not ids['promotion_contract'].intersection(ids['settlement']) else ())
    return {'promotion_contract': contract,
            'settlement': read_activity_periods(documents, ids['settlement'], year_context=context)}


def program_activity_checks(case):
    """Skip model date reasoning only when the full sources resolve exactly."""
    from .audit_rag import reference_fields, reference_comparison
    from .pdf_policy import rules
    indexed = reference_fields(case['documents'], case.get('units'))['activity_period']
    if indexed['state'] != 'available':
        return {}, None
    facts = {d['unit_id']: d['facts'] for d in case['documents']}
    readings = case_activity_periods(facts, case['materials'])
    applicable = {r.id for r in rules(case['scenario'])}
    checks = {}
    for key, role in [('contract_activity_dates', 'promotion_contract'), ('settlement_application_dates', 'settlement')]:
        reading = readings[role]
        if key not in applicable or reading['state'] != 'available':
            continue  # Unresolved reading retains the ordinary model stage.
        ids = {uid for m in case['materials'] if m['id'] == role
               and m['state'] in {'present', 'unclear'} for uid in m['source_ids']}
        quotes = []
        for period in reading['periods']:
            for source in period['sources']:
                item = {key: source[key] for key in ('unit_id', 'quote')}
                if source['unit_id'] in ids and item not in quotes:
                    quotes.append(item)
        if not quotes:
            continue
        check = {'rule_id': key, 'status': 'unknown', 'reason': '按本单完整文书原文核对活动周期。',
                 'source_ids': sorted(ids), 'comparisons': [],
                 'reference_evidence': {'written_name': None, 'seal_name': None, 'periods': quotes}}
        computed = reference_comparison(check, case['documents'], case['materials'], case.get('units'))
        if computed['status'] in {'pass', 'fail'}:
            checks[key] = computed
    return checks, readings


def period_problem(label, reading):
    codes = {item['code'] for item in reading['limitations']}
    if 'activity_year_not_stated' in codes or 'activity_year_ambiguous' in codes:
        return f'{label}活动日期未写明年份，资料自身也没有唯一的活动年份可对应。'
    if 'activity_date_unclear' in codes:
        return f'{label}活动日期存在模糊或待定内容，无法确定完整起止日期。'
    if reading['periods'] and not reading['coverage']:
        return f'{label}写有多个不同的活动周期，无法确定本次适用哪一段。'
    return f'{label}未见完整、可辨认的活动起止日期。'
