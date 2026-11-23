"""Source-bound identity reading for native horizontal POS matrices.

Only a complete table with adjacent barcode/name header rows qualifies. This
reads identities once per explicit SKU column, not once per store or sale.
It does not supply missing prices, amounts, dates or arithmetic evidence.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import re

from .pos_arithmetic import NUMBER, TOTAL, _cell_operand
from .spreadsheet_rows import ROW, parse_cells

_BARCODE = re.compile(r'69\d{11}')
_BARCODE_LABELS = {'条码', '69码', '商品条码', '国际条码', 'SKU条码'}
_META = {'门店', '门店名称', '店名', '销售门店', '店号', '店号2', '门店编号', '门店编码',
         '区域', '促销名字', '促销员', '活动周期', '活动日期', '活动时间', '日期', '系统'}
_SUMMARY = {'合计', '总计', '销量总计', '销售总计', '销售数量合计', '销售金额合计', '含税pos', '含税POS'}


def _barcode_columns(cells):
    codes = {col: cell for col, cell in cells.items() if _BARCODE.fullmatch(cell['raw'].strip())}
    if len(codes) < 2:
        return None  # A one-SKU list has no unambiguous matrix structure.
    if any(c['raw'].strip() and col not in codes and c['raw'].strip() not in _BARCODE_LABELS
           for col, c in cells.items()):
        return None
    return codes


def _header(codes, names, contexts=()):
    if any(col not in names or not names[col]['raw'].strip()
           or NUMBER.fullmatch(names[col]['raw'].strip()) or names[col]['raw'].startswith('公式')
           or _BARCODE.search(names[col]['raw']) for col in codes):
        return None
    other = {col: c['raw'].strip() for col, c in names.items() if col not in codes and c['raw'].strip()}
    summary_sources = []
    for uid, index, cells in contexts:
        for col, cell in cells.items():
            label = cell['raw'].strip()
            if label in _SUMMARY and col not in codes and not names.get(col, {}).get('raw', '').strip():
                if col in other and other[col] != label:
                    return None
                other[col] = label
                summary_sources.append({'unit_id': uid, 'fact_index': index, 'column': col})
    if (not other or not any(label in _META for label in other.values())
            or any(label not in _META | _SUMMARY for label in other.values())):
        return None
    # Plain product columns must not overlap store/date/summary columns.
    return {'product_columns': sorted(codes),
            'metadata_columns': sorted(col for col, label in other.items() if label in _META),
            'summary_columns': sorted(col for col, label in other.items() if label in _SUMMARY),
            'summary_header_sources': summary_sources}


def native_matrix_extractions(documents, units):
    """Return compatible extraction candidates and a compact coverage receipt.

    Unknown files/sheets/rows remain on the original model path. Bindings do
    not cross files or sheets; split header pairs also retain that path because
    the existing extraction contract requires each field in its original unit.
    Positions, not a second complete parsed-cell table, are kept in memory.
    """
    metadata = {u['unit_id']: u for u in units}
    counts = Counter(u['unit_id'] for u in units)
    files = defaultdict(list)
    for document in documents:
        unit = metadata.get(document['unit_id'], {})
        if unit.get('file_id'):
            files[unit['file_id']].append(document)
    result = {}
    receipt = {'tool': 'pos_matrix_identity', 'model_calls': 0, 'files': [],
               'scope': '仅原生横向矩阵商品名称与69码；不补写价格、金额、日期或数量。'}
    for file_id, docs in files.items():
        if any(counts[d['unit_id']] != 1 for d in docs):
            candidate, proof = None, {'file_id': file_id, 'complete': False,
                                      'fallback_reason': 'duplicate_source_unit_metadata'}
        else:
            candidate, proof = _file_extractions(file_id, docs, metadata)
        receipt['files'].append(proof)
        if candidate is not None:
            result.update(candidate)
    return result, receipt


def _file_extractions(file_id, docs, metadata):
    proof = {'file_id': file_id, 'complete': False, 'source_ids': [], 'tables': []}
    def fallback(reason):
        return None, {**proof, 'fallback_reason': reason}
    if len({d['unit_id'] for d in docs}) != len(docs):
        return fallback('duplicate_source_unit')
    facts = {d['unit_id']: d['facts'] for d in docs}
    sheets = defaultdict(dict)
    outputs = {}
    for d in docs:
        uid = d['unit_id']; unit = metadata[uid]
        if (unit.get('image') or unit.get('source_kind') or unit.get('photo_reference')
                or d.get('roles') != ['spreadsheet'] or not d['facts']
                or d['facts'] != unit.get('native_facts') or unit.get('limitations') or d.get('limitations')):
            return fallback('source_not_complete_unchanged_native_cells')
        outputs[uid] = {'extracted': {'rows': [], 'other_facts': []},
                        'headers': [], 'method': 'native_matrix_cells'}
        proof['source_ids'].append(uid)
        for index, fact in enumerate(d['facts']):
            row = ROW.fullmatch(fact)
            if row is None or int(row[2]) in sheets[row[1]]:
                return fallback('invalid_or_duplicate_native_row')
            sheets[row[1]][int(row[2])] = (uid, index)
    for sheet, rows in sheets.items():
        def read(position):
            uid, index = rows[position]
            return parse_cells(ROW.fullmatch(facts[uid][index])[3])
        ordered = sorted(rows)
        current = None
        pending = []
        consumed = set()
        for offset, position in enumerate(ordered):
            if position in consumed:
                continue
            uid, index = rows[position]
            cells = read(position)
            if not cells:
                return fallback('unresolved_native_cells')
            adjacent = (ordered[offset+1] if offset+1 < len(ordered) else None)
            codes = _barcode_columns(cells)
            next_cells = read(adjacent) if adjacent == position+1 else None
            next_codes = _barcode_columns(next_cells) if next_cells else None
            name_cells = next_cells if codes else cells if next_codes else None
            code_cells = codes or next_codes
            header = _header(code_cells, name_cells, pending) if code_cells and name_cells else None
            if header is not None:
                if current is not None and not current['data_rows']:
                    return fallback('matrix_has_no_body')
                if adjacent is None or rows[adjacent][0] != uid:
                    return fallback('header_pair_crosses_source_units')
                code_index = index if codes else rows[adjacent][1]
                name_index = rows[adjacent][1] if codes else index
                current = {'file_id': file_id, 'sheet': sheet, **header, 'unit_id': uid,
                           'barcode_fact_index': code_index, 'name_fact_index': name_index,
                           'header_rows': [position, adjacent], 'data_rows': 0, 'total_rows': 0}
                # Prefixes may describe a brand/report or a numeric header
                # context row, but cannot contain another unbound identifier.
                brand_context = any([c['raw'].strip() for c in pcells.values()][:1] == ['品牌']
                                    for _, _, pcells in pending)
                for puid, pi, pcells in pending:
                    if not _prefix(pcells, header, brand_context=brand_context):
                        return fallback('unresolved_matrix_prefix')
                    outputs[puid]['extracted']['other_facts'].append({
                        'fact_index': pi, 'kind': 'note', 'reason': '原表标题或矩阵表头辅助信息，未据此补商品字段。'})
                pending = []
                proof['tables'].append(current)
                for col in header['product_columns']:
                    outputs[uid]['extracted']['rows'].append({'fact_indexes': sorted([code_index, name_index]),
                        'product_name': {'fact_index': name_index, 'quote': name_cells[col]['quote'],
                                         'value': name_cells[col]['raw'].strip()},
                        'barcode_69': {'fact_index': code_index, 'quote': code_cells[col]['quote'],
                                       'value': code_cells[col]['raw'].strip()}})
                outputs[uid]['headers'].append(current)
                consumed.add(adjacent)
                continue
            # A barcode-like header that could not bind in full is ambiguous,
            # never an extra note or a quantity row.
            if codes or any(_BARCODE.search(c['raw']) for c in cells.values()):
                return fallback('unbound_or_ambiguous_matrix_identifier')
            if current is None:
                # Prefix rows are a small header region, not an unbounded
                # copy of the workbook. A long unrecognized region falls back.
                if len(pending) >= 8:
                    return fallback('matrix_header_not_established')
                pending.append((uid, index, cells))
                continue
            all_columns = set(current['product_columns'] + current['metadata_columns'] + current['summary_columns'])
            if any(c['raw'].strip() and col not in all_columns for col, c in cells.items()):
                return fallback('unbound_matrix_column')
            numeric_columns = current['product_columns'] + current['summary_columns']
            native_row = {'unit_id': uid, 'index': position, 'cells': cells}
            if any(cells.get(col, {}).get('raw', '').strip() and _cell_operand(native_row, col) is None
                   for col in numeric_columns):
                return fallback('unresolved_matrix_value')
            labels = [cells.get(col, {}).get('raw', '').strip() for col in current['metadata_columns']]
            if not any(labels) or not any(_cell_operand(native_row, col) is not None for col in numeric_columns):
                return fallback('matrix_body_not_identified')
            total = any(TOTAL.fullmatch(label) for label in labels)
            current['total_rows' if total else 'data_rows'] += 1
            if current not in outputs[uid]['headers']:
                outputs[uid]['headers'].append(current)
            outputs[uid]['extracted']['other_facts'].append({'fact_index': index,
                'kind': 'total' if total else 'note',
                'reason': '矩阵合计行，商品身份见原列头。' if total else
                          '矩阵明细行，商品身份见原列头。'})
        if current is None or not current['data_rows'] or pending:
            return fallback('matrix_table_not_complete')
    proof.update(complete=True, product_bindings=sum(len(t['product_columns']) for t in proof['tables']))
    return outputs, proof


def _prefix(cells, header, *, brand_context=False):
    populated = {col: c['raw'].strip() for col, c in cells.items() if c['raw'].strip()}
    if any(_BARCODE.search(v) for v in populated.values()):
        return False
    if len(populated) == 1:
        text = next(iter(populated.values()))
        return bool(not text.startswith('公式') and (
            re.search(r'销售|汇总|数据|明细|报表', text) or
            (brand_context and re.fullmatch(r'[^\d]{1,40}\d{1,2}[./月]\d{1,2}[日号]?\s*[-—至~～]\s*'
                                           r'\d{1,2}[./月]\d{1,2}[日号]?', text))))
    if list(populated.values())[:1] == ['品牌'] and len(populated) == 2:
        return True
    product_cols = set(header['product_columns'])
    return (product_cols.issubset(populated)
            and all(NUMBER.fullmatch(populated[col]) for col in product_cols)
            and all(col in product_cols or col in header['metadata_columns'] or col in header['summary_columns']
                    for col in populated))
