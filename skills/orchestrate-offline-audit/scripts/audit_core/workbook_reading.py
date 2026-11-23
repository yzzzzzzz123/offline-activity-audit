"""Lossless, streaming Excel facts shared by the upload and inspection tools.

Read stored cells, never the formatting rectangle; preserve XML numeric text
before a float can round it. Layout is provenance, not invented cell content.
"""
from __future__ import annotations

from decimal import Decimal
from bisect import bisect_left, bisect_right
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from zipfile import BadZipFile
from xml.etree.ElementTree import ParseError
from xml.etree.ElementTree import tostring

import openpyxl
import xlrd

from .common import AuditError
from .reading_contract import CUSTOMER_UNREADABLE, ReadingSystemError
from .spreadsheet_rows import encode_row
from .spreadsheet_photos import (
    DISPIMG,
    spreadsheet_source,
    validate_spreadsheet_archive,
    workbook_photo_units,
)
from openpyxl.worksheet._reader import WorkSheetParser, VALUE_TAG, FORMULA_TAG
from openpyxl.utils.cell import range_boundaries
from openpyxl.utils.cell import get_column_letter


class NativeCellParser(WorkSheetParser):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.hidden_rows = []

    def parse_row(self, element):
        index, cells = super().parse_row(element)
        attrs = self.row_dimensions.pop(str(index), {})
        if str(attrs.get('hidden', '')).lower() in {'1', 'true'}:
            self.hidden_rows.append(index)
        return index, cells

    def parse_cell(self, element):
        cell = super().parse_cell(element)
        number = element.findtext(VALUE_TAG)
        # Dates retain the library's epoch/style decoding. Text and error cells
        # are never reinterpreted as numbers (including text barcodes).
        if element.get('t', 'n') == 'n' and cell['data_type'] != 'd' and number:
            cell['raw_number'] = number
        if element.find(FORMULA_TAG) is not None:
            if not isinstance(cell['value'], str):
                cell['value'] = (getattr(cell['value'], 'text', None)
                                 or tostring(element.find(FORMULA_TAG), encoding='unicode'))
            column = self.col_counter
            self.col_counter = cell['column'] - 1
            self.data_only = True
            try:
                cached = super().parse_cell(element)
            finally:
                self.data_only = False
                self.col_counter = column
            cell['cached_value'] = cached['value']
            cell['cached_type'] = cached['data_type']
            if cached['data_type'] == 'd':
                cell.pop('raw_number', None)
        return cell


def stored_rows(sheet, layout=None):
    """Yield every stored row, even beyond an incorrect worksheet dimension."""
    with sheet._get_source() as source:
        parser = NativeCellParser(
            source, sheet._shared_strings, data_only=False,
            epoch=sheet.parent.epoch, date_formats=sheet.parent._date_formats,
            timedelta_formats=sheet.parent._timedelta_formats)
        yield from parser.parse()
    if layout is not None:
        layout.update(sheet=sheet.title, state=sheet.sheet_state,
            merged_ranges=[item.ref for item in parser.merged_cells.mergeCell] if parser.merged_cells else [],
            hidden_rows=parser.hidden_rows,
            hidden_columns=[{'min': int(attrs['min']), 'max': int(attrs['max'])}
                            for attrs in parser.column_dimensions.values()
                            if str(attrs.get('hidden', '')).lower() in {'1', 'true'}])


def numeric_text(cell, fallback):
    """Keep exact decimal operands; expand scientific notation without rounding."""
    raw = cell.get('raw_number')
    if raw is None:
        return fallback
    value = Decimal(raw)
    if not value.is_finite():
        return fallback
    if value.copy_abs() < 2 ** 53 and value == value.to_integral_value():
        return format(value, 'f').split('.')[0]
    # Expand ordinary scientific cells, but retain an extreme exponent's exact
    # original spelling rather than allocating millions of zeroes.
    return format(value, 'f') if 'e' in raw.lower() and -324 <= value.adjusted() <= 308 else raw


class SequentialWorksheet:
    """Compatibility for legacy row-ordered readers without repeated XML scans.

    Keep only the current stored row. A deliberate backward seek starts one new
    scan; ordinary adjacent .cell calls never reopen the worksheet. Dimensions
    come from actual content, including data beyond a stale declared range.
    """
    def __init__(self, sheet, *, cached=False):
        self.raw = sheet
        self.title, self.sheet_state = sheet.title, sheet.sheet_state
        self.cached = cached
        self._extent = None
        self._rows = None
        self._requested = self._index = 0
        self._cells = {}
        self._ended = False

    def _measure(self):
        if self._extent is None:
            last_row = last_column = 1
            for row, cells in stored_rows(self.raw):
                populated = [c for c in cells if c['value'] is not None]
                if populated:
                    last_row = max(last_row, row)
                    last_column = max(last_column, max(c['column'] for c in populated))
            self._extent = last_row, last_column
        return self._extent

    @property
    def max_row(self): return self._measure()[0]

    @property
    def max_column(self): return self._measure()[1]

    def cell(self, row, column):
        if row < 1 or column < 1:
            raise ValueError('Row or column values must be at least 1')
        if row < self._requested:
            self.close()
        self._requested = row
        if self._rows is None and not self._ended:
            self._rows = stored_rows(self.raw)
        while self._index < row and not self._ended:
            try:
                self._index, values = next(self._rows)
                self._cells = {c['column']: c for c in values}
            except StopIteration:
                self._ended = True
                self._cells = {}
        cell = self._cells.get(column, {}) if self._index == row else {}
        value = cell.get('cached_value') if self.cached and cell.get('data_type') == 'f' else cell.get('value')
        return SimpleNamespace(value=value, row=row, column=column,
                               coordinate=f'{get_column_letter(column)}{row}')

    def close(self):
        if self._rows is not None:
            self._rows.close()
        self._rows = None
        self._requested = self._index = 0
        self._cells = {}
        self._ended = False


class SequentialWorkbook:
    def __init__(self, raw, *, cached=False):
        self.raw = raw
        self.sheetnames = raw.sheetnames
        self.worksheets = [SequentialWorksheet(s, cached=cached) for s in raw.worksheets]
        self._by_name = {s.title: s for s in self.worksheets}

    def __getitem__(self, name): return self._by_name[name]

    def close(self):
        for sheet in self.worksheets:
            sheet.close()
        self.raw.close()


def sequential_workbook(path, **kwargs):
    cached = kwargs.pop('data_only', False)
    kwargs.pop('read_only', None)
    kwargs['keep_links'] = False
    return SequentialWorkbook(openpyxl.load_workbook(path, read_only=True, data_only=False, **kwargs), cached=cached)


def _cell(value) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    # xlrd represents numeric EAN-13 cells as float. Preserve the exact integer
    # value without inventing a literal '.0' suffix in the source barcode.
    # Actual text cells, including zeros or a typed suffix, stay untouched.
    if isinstance(value, float) and value.is_integer() and abs(value) < 2 ** 53:
        return str(int(value))
    return str(value) if value is not None else ""


def spreadsheet_units(item: dict, root: Path, *, max_cells=1_000_000) -> list[dict]:
    path = Path(item["path"])
    units = []
    rows = []
    count = 0
    photo_cells = []
    layouts = []
    first_row = last_row = None
    used_columns = set()
    active_sheet = None

    def flush(sheet: str):
        nonlocal first_row, last_row
        if rows:
            units.append({"unit_id": f"{item['file_id']}-table-{len(units) + 1:04d}",
                          "file_id": item["file_id"], "source_file": item["source_file"],
                          "locator": sheet, "image": None, "native_facts": list(rows),
                          "limitations": [], "row_range": [first_row, last_row],
                          'column_range': [min(used_columns), max(used_columns)]})
            rows.clear()
            used_columns.clear()
            first_row = last_row = None

    try:
        path, legacy_pictures = spreadsheet_source(path, root, item["file_id"])
        if path.suffix.lower() == ".xls":
            book = xlrd.open_workbook(path, on_demand=True)
            try:
                for sheet in book.sheets():
                    active_sheet = sheet.name
                    layouts.append({'sheet': sheet.name, 'state': ['visible', 'hidden', 'veryHidden'][sheet.visibility],
                                    'merged_ranges': [], 'hidden_rows': [], 'hidden_columns': []})
                    for index in range(sheet.nrows):
                        cells = []
                        for col, cell in enumerate(sheet.row(index)):
                            value = cell.value
                            if cell.ctype == xlrd.XL_CELL_DATE:
                                value = xlrd.xldate_as_datetime(value, book.datemode)
                            if value != "":
                                used_columns.add(col + 1)
                                photo = DISPIMG.match(str(value))
                                if photo:
                                    photo_cells.append({"sheet": sheet.name, "row": index + 1,
                                                        "column": col + 1, "image_id": photo.group(1)})
                                cells.append((col + 1, _cell(value)))
                        count += sheet.ncols
                        if count > max_cells:
                            raise ReadingSystemError(
                                "电子表格单元格总量超出安全读取范围",
                                code="resource_limit", retryable=True)
                        if cells:
                            first_row = first_row or index + 1
                            last_row = index + 1
                            rows.append(encode_row(sheet.name, index + 1, cells))
                        if len(rows) >= 150:
                            flush(sheet.name)
                    flush(sheet.name)
            finally:
                book.release_resources()
        else:
            validate_spreadsheet_archive(path)
            book = openpyxl.load_workbook(path, read_only=True, data_only=False, keep_links=False)
            try:
                for sheet in book.worksheets:
                    active_sheet = sheet.title
                    layout = {}
                    for index, values in stored_rows(sheet, layout):
                        count += len(values)
                        if count > max_cells:
                            raise ReadingSystemError(
                                "电子表格单元格总量超出安全读取范围",
                                code="resource_limit", retryable=True)
                        cells = []
                        for cell in values:
                            col = cell["column"]
                            cached_value = cell.get('cached_value')
                            if cell["data_type"] == "f":
                                photo = DISPIMG.match(str(cell["value"]))
                                if photo:
                                    photo_cells.append({"sheet": sheet.title, "row": index,
                                                        "column": col, "image_id": photo.group(1)})
                                displayed = numeric_text(cell, _cell(cached_value)) if cached_value is not None else "无缓存结果，无法确认数值"
                                cells.append((col, f"公式{cell['value']}；缓存值={displayed}"))
                            elif cell["value"] is not None:
                                cells.append((col, numeric_text(cell, _cell(cell['value']))))
                            if cell['value'] is not None:
                                used_columns.add(col)
                        if cells:
                            first_row = first_row or index
                            last_row = index
                            rows.append(encode_row(sheet.title, index, cells))
                        if len(rows) >= 150:
                            flush(sheet.title)
                    flush(sheet.title)
                    layouts.append(layout)
            finally:
                book.close()
        photos = workbook_photo_units(path, item, root, photo_cells)
        if legacy_pictures and (not photos or any(
                not photo["image"] or "!R" not in photo["locator"] for photo in photos)):
            raise AuditError("电子表格转换未完整保留图片及工作表行列对应关系，已停止读取，不能用无图表格继续核验")
        units.extend(photos)
        # A read receipt is code-generated and file scoped. Keep it out of raw
        # cell facts so arithmetic and source quotation retain their contract.
        for layout in layouts:
            tables = [u for u in units if not u.get('photo_reference') and u['locator'] == layout['sheet']]
            sheet_photos = [u for u in photos if u['locator'].startswith(layout['sheet'] + '!')]
            layout['table_unit_ids'] = [u['unit_id'] for u in tables]
            layout['photo_unit_ids'] = [u['unit_id'] for u in sheet_photos]
            layout['row_ranges'] = [u['row_range'] for u in tables]
            # Keep full layout once in the receipt; a 150-row packet carries
            # only intersecting layout, not every merge on a large workbook.
            merged = sorted(((range_boundaries(address), address) for address in layout['merged_ranges']),
                            key=lambda item: item[0][1])
            hidden_rows = sorted(layout['hidden_rows'])
            cursor, active = 0, []
            for unit in tables:
                first, last = unit['row_range']
                left, right = unit['column_range']
                active = [item for item in active if item[0][3] >= first - 1]
                while cursor < len(merged) and merged[cursor][0][1] <= last:
                    merge_entry = merged[cursor]
                    if merge_entry[0][3] >= first - 1:
                        active.append(merge_entry)
                    cursor += 1
                unit['sheet_layout'] = {'sheet': layout['sheet'], 'state': layout['state'],
                    'merged_ranges': [address for bounds, address in active if bounds[0] <= right and bounds[2] >= left],
                    'hidden_rows': hidden_rows[bisect_left(hidden_rows, first):bisect_right(hidden_rows, last)],
                    'hidden_columns': [c for c in layout['hidden_columns'] if int(c['min']) <= right and int(c['max']) >= left]}
        from .workbench_store import atomic_write_json
        atomic_write_json(root / (item['file_id'] + '-workbook-reading.json'),
                          {'version': 1, 'file_id': item['file_id'], 'source_file': item['source_file'],
                           'sheets': layouts, 'stored_cell_count': count,
                           'photo_unit_count': len(photos), 'model_calls': 0})
    except (xlrd.XLRDError, xlrd.compdoc.CompDocError, BadZipFile, ParseError, openpyxl.utils.exceptions.InvalidFileException):
        # Row batching normally flushes at 150 rows. If the decoder fails
        # before that boundary, publish the rows already reconstructed as
        # successful units and add a separate unreadable receipt for the gap.
        if rows and active_sheet:
            flush(active_sheet)
        # Do not pass decoder messages containing private source values to logs.
        failure = {"unit_id": item["file_id"] + "-unreadable", "file_id": item["file_id"],
                   "source_file": item["source_file"], "locator": "电子表格",
                   "image": None, "native_facts": [],
                   "limitations": ["电子表格内容无法读取"],
                   "read_status": CUSTOMER_UNREADABLE, "error_code": "unreadable_content",
                   "retryable": False, "customer_actionable": True}
        # A later sheet or picture relationship may fail after earlier cells
        # were already read. Keep those successful units and add a visible
        # failure receipt instead of replacing the whole workbook with one gap.
        return [*units, failure] if units else [failure]
    return units or [{"unit_id": item["file_id"], "file_id": item["file_id"],
                      "source_file": item["source_file"], "locator": "电子表格",
                      "image": None, "native_facts": [], "limitations": ["电子表格没有可读取的数据"],
                      "read_status": CUSTOMER_UNREADABLE, "error_code": "empty_content",
                      "retryable": False, "customer_actionable": True}]
