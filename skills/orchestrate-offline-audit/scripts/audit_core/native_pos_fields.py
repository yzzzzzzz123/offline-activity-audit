"""Prove complete native POS fields without sending every row to a model.

This is a positive proof, not a second business standard or a layout guesser.
Unsupported/ambiguous layouts return to the existing model stage. Original
facts, exact arithmetic and product identity checks remain independent.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime
import re

from .native_pos_identity import NAME_HEADERS, BARCODE_HEADERS
from .pos_arithmetic import _columns, _header_label, _cell_operand, TOTAL, prepare_pos_arithmetic
from .price_support_dates import _period
from .spreadsheet_rows import ROW, parse_cells


_TIME_HEADERS = frozenset({'活动时间', '活动日期', '活动周期', '活动期间', '销售日期', '销售时间',
                           '销售周期', '交易日期', 'POS日期', '日期'})
_START_HEADERS = frozenset({'销售开始时间', '销售开始日期', '活动开始时间', '活动开始日期', '开始日期', '开始时间'})
_END_HEADERS = frozenset({'销售结束时间', '销售结束日期', '活动结束时间', '活动结束日期', '结束日期', '结束时间'})
_TITLE = re.compile(r'^(?:活动|销售|促销|执行)(?:时间|日期|周期|期间)\s*[:：]\s*(.+)$')
_REPORT_TITLE = re.compile(r'^[^\d\s]{1,80}销售(?:明细|数据|汇总)?表?$')
_DATE_CHARACTERS = re.compile(r'[\d\s年月日号至到./—–~～\-]+')
_MONTH_DAY = r'(\d{1,2})(?:月|[./-])(\d{1,2})[日号]?'
_SHORT_TIME = re.compile(_MONTH_DAY + r'(?:\s*(?:至|到|[-—–~～])\s*' + _MONTH_DAY + r')?')


def _time_value(raw):
    """Prove a printed time field, without inventing a year or comparing it.

    Month/day shorthand is a present POS field. Exact application/contract
    period checks remain separate and must establish their own real years.
    """
    text = raw.strip()
    try:
        # Native readers spell actual Excel date cells as ISO dates/datetimes.
        datetime.fromisoformat(text)
        if re.match(r'^\d{4}-\d{2}-\d{2}(?:$|[ T])', text):
            return True
    except ValueError:
        pass
    if _DATE_CHARACTERS.fullmatch(text) and _period(text):
        return True
    short = _SHORT_TIME.fullmatch(text)
    if short:
        try:
            # 2000 only validates possible calendar month/day (including 2/29),
            # is never returned, stored or used as the activity's actual year.
            date(2000, int(short[1]), int(short[2]))
            if short[3]:
                date(2000, int(short[3]), int(short[4]))
            return True
        except ValueError:
            pass
    return False


def _time_columns_present(values, columns):
    if 'time' in columns:
        return _time_value(values.get(columns['time'], ''))
    if 'time_start' not in columns:
        return False
    start, end = (values.get(columns[key], '') for key in ('time_start', 'time_end'))
    if not _time_value(start) or not _time_value(end):
        return False
    parsed_start, parsed_end = _period(start), _period(end)
    return not (parsed_start and parsed_end) or parsed_start[0] <= parsed_end[1]


def native_pos_field_check(case, plan=None):
    """Return (check or None, source-indexed receipt), with zero model calls.

    The full arithmetic plan must already establish every numeric detail and
    both totals. Independently scan every original fact for same-sheet column
    bindings, original names/barcodes and activity time. A single unresolved
    fact/row/source retains the normal model path; it does not fail the bill.
    """
    proof = {'tool': 'native_pos_fields', 'model_calls': 0, 'complete': False,
             'scope': '仅POS电子版5+2字段完整性；不代替商品查库、周期对齐或金额复算。',
             'sources': [], 'tables': []}

    def fallback(reason):
        return None, {**proof, 'fallback_reason': reason}

    if case.get('flags', {}).get('uses_pos') is not True:
        return fallback('pos_not_applicable_or_not_confirmed')
    materials = [m for m in case['materials'] if m['id'] == 'pos_excel']
    if not materials or any(m['state'] != 'present' for m in materials):
        return fallback('pos_source_not_complete')
    ids = {uid for m in materials for uid in m['source_ids']}
    if not ids:
        return fallback('pos_source_not_complete')
    docs = [d for d in case['documents'] if d['unit_id'] in ids]
    units = [u for u in case.get('units', []) if u['unit_id'] in ids]
    if (len(docs) != len(ids) or len(units) != len(ids)
            or {d['unit_id'] for d in docs} != ids or {u['unit_id'] for u in units} != ids):
        return fallback('native_source_identity_not_complete')
    metadata = {u['unit_id']: u for u in units}
    source_facts = {d['unit_id']: d['facts'] for d in docs}
    if plan is None:
        plan = prepare_pos_arithmetic(case['documents'], case['materials'], case.get('units'))
    if not plan or not plan['complete']:
        return fallback('native_numeric_coverage_not_complete')
    sheets = defaultdict(dict)
    for document in docs:
        uid = document['unit_id']
        unit = metadata[uid]
        if (not unit.get('file_id') or unit.get('image') or unit.get('source_kind')
                or unit.get('photo_reference') or document.get('roles') != ['spreadsheet']
                or not document['facts'] or unit.get('native_facts') != document['facts']
                or unit.get('limitations') or document.get('limitations')):
            return fallback('source_is_not_complete_unchanged_native_cells')
        proof['sources'].append({'unit_id': uid, 'fact_count': len(document['facts'])})
        for index, fact in enumerate(document['facts']):
            matched = ROW.fullmatch(fact)
            if matched is None:
                return fallback('native_row_unresolved')
            key = (unit['file_id'], matched[1])
            row = int(matched[2])
            if row in sheets[key]:
                return fallback('duplicate_native_row')
            # Index only original positions. Do not retain a second parsed
            # copy of every cell in a tens-of-thousands-row workbook.
            sheets[key][row] = (uid, index)
    detail_count = 0
    for (file_id, sheet), rows in sheets.items():
        columns = None
        time_title = None
        table = None
        for row, (uid, index) in sorted(rows.items()):
            cells = parse_cells(ROW.fullmatch(source_facts[uid][index])[3])
            if not cells:
                return fallback('native_row_unresolved')
            pointer = {'unit_id': uid, 'fact_index': index}
            values = {col: cell['raw'].strip() for col, cell in cells.items() if cell['raw'].strip()}
            if len(values) == 1 and (title := _TITLE.fullmatch(next(iter(values.values())))):
                if not _time_value(title[1]) or columns is not None:
                    return fallback('activity_title_scope_ambiguous')
                if time_title and time_title['value'] != title[1].strip():
                    return fallback('multiple_activity_titles')
                time_title = {'value': title[1].strip(), 'source': pointer}
                continue
            if (columns is None and len(values) == 1
                    and _REPORT_TITLE.fullmatch(next(iter(values.values())))):
                # A narrow report heading, not arbitrary notes or extra data.
                # It cannot supply missing field values, time or identifiers.
                continue
            numeric = _columns(cells)
            if numeric:
                # Generic 名称 is unambiguous only inside a POS header with
                # explicit barcode and all three sale numeric columns.
                names = [col for col, v in values.items() if _header_label(v) in NAME_HEADERS | {'名称'}]
                codes = [col for col, v in values.items() if _header_label(v) in BARCODE_HEADERS]
                times = [col for col, v in values.items() if _header_label(v) in _TIME_HEADERS]
                starts = [col for col, v in values.items() if _header_label(v) in _START_HEADERS]
                ends = [col for col, v in values.items() if _header_label(v) in _END_HEADERS]
                if (len(names) != 1 or len(codes) != 1 or len(times) > 1
                        or (bool(starts or ends) and (len(starts) != 1 or len(ends) != 1 or times))):
                    return fallback('native_identity_or_time_columns_ambiguous')
                found = {**numeric, 'name': names[0], 'barcode': codes[0]}
                if times:
                    found['time'] = times[0]
                elif starts:
                    found.update(time_start=starts[0], time_end=ends[0])
                # The arithmetic reader already proves this export's date
                # header is its sales-amount column, not a value to invent.
                amount_time = (_time_value(values[numeric['amount']])
                               and _period(values[numeric['amount']]) is not None)
                if not times and not starts and time_title is None and not amount_time:
                    return fallback('native_activity_time_not_proven')
                if columns != found or table is None or table['total_sources']:
                    table = {'file_id': file_id, 'sheet': sheet, 'columns': found,
                             'header_sources': [], 'detail_rows': 0, 'first_row': None,
                             'last_row': None, 'total_sources': [],
                             'activity_title_source': time_title['source'] if time_title else None,
                             'activity_header_source': pointer if amount_time else None}
                    proof['tables'].append(table)
                columns = found
                table['header_sources'].append(pointer)
                continue
            if columns is None:
                return fallback('native_prefix_or_header_not_resolved')
            # A total has its own explicit label and no product barcode. Native
            # arithmetic independently proves its complete operand coverage.
            if any(TOTAL.fullmatch(v) for v in values.values()) and not values.get(columns['barcode']):
                native = {'unit_id': uid, 'index': row, 'cells': cells}
                if any(_cell_operand(native, columns[k]) is None for k in ('quantity', 'amount')):
                    return fallback('native_totals_not_resolved')
                table['total_sources'].append(pointer)
                continue
            native = {'unit_id': uid, 'index': row, 'cells': cells}
            if (not values.get(columns['name'])
                    or not re.fullmatch(r'\d{13}', values.get(columns['barcode'], ''))
                    or any(_cell_operand(native, columns[k]) is None for k in ('quantity', 'price', 'amount'))
                    or (time_title is None and table['activity_header_source'] is None
                        and not _time_columns_present(values, columns))
                    or table['total_sources']):
                return fallback('native_detail_field_not_proven')
            table['first_row'] = row if table['first_row'] is None else table['first_row']
            table['last_row'] = row
            table['detail_rows'] += 1
            detail_count += 1
    if (not proof['tables'] or any(not t['detail_rows'] or not t['total_sources'] for t in proof['tables'])
            or detail_count != sum(t['detail_rows'] for t in plan['tables'])):
        return fallback('native_full_table_coverage_not_proven')
    proof.update(complete=True, detail_rows=detail_count,
                 fields=['活动时间', '69码', '商品名称', '销售数量', '销售单价', '销售金额', '数量及金额合计'])
    return {'rule_id': 'pos_fields', 'status': 'pass',
            'reason': 'POS电子版的活动时间、69码、商品名称、销售数量、销售单价、销售金额及数量和金额合计齐全。',
            'source_ids': [d['unit_id'] for d in docs], 'comparisons': []}, proof
