"""Unambiguous native-cell wire format, with exact source quote fragments.

Ordinary rows retain the existing spelling/checkpoint identity. Only ambiguous
text uses a canonical JSON object. Decoding restores original text, never a
globally unescaped or normalized approximation of a source quotation.
"""
import json
import re
from .common import AuditError

ROW = re.compile(r'^(.*?)!R([1-9]\d*): (.*)$', re.DOTALL)
CELL = re.compile(r'(?:^| \| )C([1-9]\d*)=(.*?)(?= \| C[1-9]\d*=|$)', re.DOTALL)
AMBIGUOUS = re.compile(r' \| C[1-9]\d*=|!R[1-9]\d*:|[\x00-\x1f]')


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def encode_row(sheet, row, cells):
    ordered = sorted(cells, key=lambda cell: cell[0])
    columns = [column for column, _ in ordered]
    if (len(columns) != len(set(columns))
            or any(type(column) is not int or column < 1 for column in columns)):
        raise AuditError('电子表同一行的单元格坐标重复或无效，未覆盖或丢弃原始值')
    values = {f'C{column}': raw for column, raw in ordered}
    if any(AMBIGUOUS.search(raw) for raw in values.values()):
        body = _json(values)
    else:
        body = ' | '.join(f'{key}={raw}' for key, raw in values.items())
    return f'{sheet}!R{row}: {body}'


def parse_cells(content):
    """Return decoded cells and contiguous original quote fragments, or None."""
    if content.startswith('{'):
        try:
            pairs = json.loads(content, object_pairs_hook=list)
        except (ValueError, TypeError):
            return None
        if not isinstance(pairs, list) or not pairs or any(
                not isinstance(pair, tuple) or len(pair) != 2
                or not isinstance(pair[0], str) or not re.fullmatch(r'C[1-9]\d*', pair[0])
                or not isinstance(pair[1], str) for pair in pairs):
            return None
        columns = [int(key[1:]) for key, _ in pairs]
        if columns != sorted(set(columns)) or _json(dict(pairs)) != content:
            return None
        return {int(key[1:]): {'raw': raw, 'quote': _json(key) + ':' + _json(raw)} for key, raw in pairs}
    matches = list(CELL.finditer(content))
    columns = [int(m[1]) for m in matches]
    if (not matches or columns != sorted(set(columns))
            or ' | '.join(m[0].removeprefix(' | ') for m in matches) != content):
        return None
    return {int(m[1]): {'raw': m[2], 'quote': f'C{m[1]}={m[2]}'} for m in matches}


def exact_cell_value(fact, quote, value):
    """Prove a decoded field against a single canonical native source cell."""
    matched = ROW.fullmatch(fact)
    cells = parse_cells(matched[3]) if matched else None
    return bool(cells and any(c['quote'] == quote and c['raw'].strip() == value for c in cells.values()))
