"""Read explicit native POS identity columns, with no model or inferred cells.

Only unchanged program-read spreadsheet facts qualify. Ambiguous documents use
the existing model extractor; header bindings never cross files or worksheets.
"""
from collections import defaultdict

from .pos_arithmetic import ROW, TOTAL
from .spreadsheet_rows import parse_cells


NAME_HEADERS = frozenset({"商品名称", "产品名称", "品名"})
BARCODE_HEADERS = frozenset({"69码", "条码", "商品条码", "国际条码", "商品69码", "SKU条码"})


def _cells(content):
    parsed = parse_cells(content)
    return {col: c['raw'] for col, c in parsed.items()} if parsed else None


def native_identity_extractions(documents, units):
    """Return complete per-unit extractions only where every fact is resolved.

    Unknown prefix/footer facts keep that unit on the model path. A later unit
    can use an explicit earlier header from the same native file and sheet.
    An unknown row after a header clears its binding until a new full header.
    """
    metadata = {unit["unit_id"]: unit for unit in units}
    facts_by_id = {d['unit_id']: d['facts'] for d in documents}
    files = defaultdict(list)
    for document in documents:
        unit = metadata.get(document["unit_id"], {})
        if unit.get("file_id"):
            files[unit["file_id"]].append(document)
    result = {}
    for file_documents in files.values():
        from .native_pos_matrix import native_matrix_extractions
        matrix, _ = native_matrix_extractions(file_documents, units)
        if matrix:
            result.update(matrix)
            continue
        sheets, outputs, rejected = defaultdict(dict), {}, set()
        valid_file = True
        for document in file_documents:
            uid = document["unit_id"]
            unit = metadata[uid]
            if (unit.get("image") or unit.get("source_kind") or unit.get("photo_reference")
                    or document.get("roles") != ["spreadsheet"]
                    or not document.get("facts") or unit.get("native_facts") != document["facts"]
                    or unit.get("limitations") or document.get("limitations")):
                valid_file = False
                break
            outputs[uid] = {"extracted": {"rows": [], "other_facts": []}, "headers": []}
            for index, fact in enumerate(document["facts"]):
                matched = ROW.fullmatch(fact)
                if matched is None:
                    valid_file = False
                    break
                sheet, row_number, content = matched.groups()
                cells = _cells(content)
                row_number = int(row_number)
                if cells is None or row_number in sheets[sheet]:
                    valid_file = False
                    break
                sheets[sheet][row_number] = (uid, index, cells)
            if not valid_file:
                break
        if not valid_file:
            continue
        for sheet, rows in sheets.items():
            columns = header = None
            for row_number, (uid, index, cells) in sorted(rows.items()):
                output = outputs[uid]
                names = [col for col, raw in cells.items() if raw.strip() in NAME_HEADERS]
                barcodes = [col for col, raw in cells.items() if raw.strip() in BARCODE_HEADERS]
                if names or barcodes:
                    if len(names) == len(barcodes) == 1:
                        columns = {"product_name": names[0], "barcode_69": barcodes[0]}
                        header = {"unit_id": uid, "fact_index": index, "sheet": sheet,
                                  "row": row_number, "columns": columns}
                        output["extracted"]["other_facts"].append({"fact_index": index,
                            "kind": "header", "reason": "原生单元格明确列出商品名称与69码表头。"})
                    else:
                        columns = header = None
                        rejected.add(uid)
                    continue
                if columns is None:
                    rejected.add(uid)
                    continue
                name = cells.get(columns["product_name"], "").strip()
                barcode = cells.get(columns["barcode_69"], "").strip()
                total = not barcode and (not name or TOTAL.fullmatch(name)) and any(
                    col <= min(columns.values()) and TOTAL.fullmatch(raw.strip())
                    for col, raw in cells.items())
                if total:
                    output["extracted"]["other_facts"].append({"fact_index": index,
                        "kind": "total", "reason": "原行明确标注合计，未列商品69码。"})
                    continue
                # Multiple identities in one cell need the original model's
                # split/association logic, never an invented combined barcode.
                multiple_codes = any(char.isspace() or char in ",，;；、/|" for char in barcode)
                if multiple_codes or (not name and not barcode) or any(
                        "\n" in raw or "\r" in raw or raw.startswith("公式")
                        for raw in (cells.get(col, "") for col in columns.values())):
                    rejected.add(uid)
                    columns = header = None
                    continue
                row = {"fact_indexes": [index]}
                quoted_cells = parse_cells(ROW.fullmatch(facts_by_id[uid][index])[3])
                for key, col in columns.items():
                    raw = cells.get(col, "")
                    row[key] = {"fact_index": index, "quote": quoted_cells[col]['quote'],
                                "value": raw.strip()} if raw.strip() else None
                output["extracted"]["rows"].append(row)
                if header not in output["headers"]:
                    output["headers"].append(header)
        result.update({uid: output for uid, output in outputs.items() if uid not in rejected})
    return result
