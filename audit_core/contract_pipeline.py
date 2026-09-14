"""Bound the first contract transcription before the independent product-cell reread."""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
import shutil
from typing import Any, Callable

from PIL import Image

from .common import AuditError
from .display_chunks import chunk_sequence
from .model_metrics import save_model_observations

CONTRACT_ROW_BATCH_SIZE = 8


def _object(properties: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False,
            "properties": properties, "required": list(properties)}


def _table_identity() -> dict[str, Any]:
    return _object({
        "table_no": {"type": "integer", "minimum": 1},
        "kind": {"type": "string", "enum": ["stores", "sales_attachment"]},
        "row_count": {"type": "integer", "minimum": 1, "maximum": 1000},
    })


def core_schema(full: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(full)
    result.pop("$id", None)
    result["properties"].pop("photo_reviews")
    result["required"].remove("photo_reviews")
    contract = result["properties"]["contract"]
    contract["properties"].pop("stores")
    contract["required"].remove("stores")
    sales = contract["properties"]["sales_attachment"]
    sales["properties"].pop("records")
    sales["required"].remove("records")
    result["properties"]["page_inventory"] = {"type": "array", "items": _object({
        "source_page": {"type": "integer", "minimum": 1},
        "tables": {"type": "array", "items": _table_identity()},
    })}
    result["required"].append("page_inventory")
    return result


def layout_schema(source_file: str, page_no: int) -> dict[str, Any]:
    bounds = _object({key: {"type": "number", "minimum": 0, "maximum": 1}
                      for key in ("left", "top", "right", "bottom")})
    table = _table_identity()
    table["properties"].update({
        "clockwise_rotation": {"type": "integer", "enum": [0, 90, 180, 270]},
        "body_bounds": bounds,
    })
    table["required"].extend(["clockwise_rotation", "body_bounds"])
    return _object({
        "source_file": {"type": "string", "const": source_file},
        "source_page": {"type": "integer", "const": page_no},
        "tables": {"type": "array", "items": table},
        "extraction_notes": {"type": "array", "items": {"type": "string"}},
    })


def validate_inventory(inventory: list[dict], page_count: int) -> None:
    if [page["source_page"] for page in inventory] != list(range(1, page_count + 1)):
        raise AuditError("合同页清单必须逐页完整、唯一并保持原始 PDF 顺序")
    for page in inventory:
        tables = page["tables"]
        if [table["table_no"] for table in tables] != list(range(1, len(tables) + 1)):
            raise AuditError("页内表格编号必须按印刷顺序连续且唯一")


def validate_layout(layout: dict, inventory: dict) -> None:
    keys = ("table_no", "kind", "row_count")
    observed = [{key: table[key] for key in keys} for table in layout["tables"]]
    if observed != inventory["tables"]:
        # Do not supply the other model's count as the answer in retry feedback.
        raise AuditError("独立整页盘点与全页总览的表格/物理行清单不一致；请从本页原图重新盘点，不能猜测或漏行")
    for table in layout["tables"]:
        bounds = table["body_bounds"]
        if bounds["left"] >= bounds["right"] or bounds["top"] >= bounds["bottom"]:
            raise AuditError("表格正文范围必须是正面积矩形")


def table_schema(full: dict, kind: str, source_file: str, page_no: int,
                 table_no: int, row_ids: list[int]) -> dict:
    contract = full["properties"]["contract"]["properties"]
    item = deepcopy(contract["stores"]["items"] if kind == "stores" else
                    contract["sales_attachment"]["properties"]["records"]["items"])
    item["properties"].pop("line_no")
    item["required"].remove("line_no")
    if kind == "sales_attachment":
        item["properties"]["source_page"] = {"type": "integer", "const": page_no}
    item["properties"]["page_local_row_no"] = {"type": "integer", "enum": row_ids}
    item["required"].append("page_local_row_no")
    return _object({
        "source_file": {"type": "string", "const": source_file},
        "source_page": {"type": "integer", "const": page_no},
        "table_no": {"type": "integer", "const": table_no},
        "kind": {"type": "string", "const": kind},
        "records": {"type": "array", "items": item, "minItems": len(row_ids), "maxItems": len(row_ids)},
        "extraction_notes": {"type": "array", "items": {"type": "string"}},
    })


def validate_rows(value: dict, row_ids: list[int]) -> None:
    if [row["page_local_row_no"] for row in value["records"]] != row_ids:
        raise AuditError("合同表格块必须逐一保留请求的物理行编号、数量和顺序；不得漏行、重复或重排")
    from .product_rag import ean13_is_valid
    for row in value["records"]:
        barcode = row.get("barcode_69")
        if barcode and not ean13_is_valid(barcode):
            raise AuditError("合同表格块包含无效 EAN-13，必须重新查看当前原始单元格")


def merge_contract(core: dict, layouts: list[dict], fragments: list[dict]) -> dict:
    """Join structural row identities; equal-looking business rows remain distinct."""
    inventory = core["page_inventory"]
    validate_inventory(inventory, len(layouts))
    if [page["source_page"] for page in layouts] != list(range(1, len(layouts) + 1)):
        raise AuditError("合同整页盘点缺页、重复或顺序漂移")
    source = core["contract"]["source_file"]
    expected: list[tuple[int, int, str, int]] = []
    for page, initial in zip(layouts, inventory):
        if page["source_file"] != source:
            raise AuditError("合同盘点来源文件发生变化")
        validate_layout(page, initial)
        expected.extend((page["source_page"], table["table_no"], table["kind"], row_no)
                        for table in page["tables"] for row_no in range(1, table["row_count"] + 1))
    observed, stores, sales, notes = [], [], [], list(core.get("extraction_notes", []))
    for fragment in fragments:
        if fragment["source_file"] != source:
            raise AuditError("合同转录块来源文件发生变化")
        page, table, kind = fragment["source_page"], fragment["table_no"], fragment["kind"]
        destination = stores if kind == "stores" else sales
        for item in fragment["records"]:
            observed.append((page, table, kind, item["page_local_row_no"]))
            row = {key: deepcopy(value) for key, value in item.items() if key != "page_local_row_no"}
            if kind == "sales_attachment" and row.get("source_page") != page:
                raise AuditError("合同销售行的 PDF 页号与当前块不一致")
            row["line_no"] = len(destination) + 1
            destination.append(row)
        notes.extend(fragment.get("extraction_notes", []))
    if observed != expected:
        raise AuditError("合同表格拼接未完整覆盖全部页/表/物理行，或块发生重叠、错序、来源漂移")
    contract = deepcopy(core["contract"])
    sales_pages = sorted({row["source_page"] for row in sales})
    summary = contract["sales_attachment"]
    if bool(sales) != summary["present"] or sales_pages != summary["source_pages"]:
        raise AuditError("独立销售表盘点与合同总览的附件范围不一致")
    # Printed totals come from the all-page core reading, never sums or last-page selection.
    summary["records"] = sales
    contract["stores"] = stores
    return {"schema_version": core["schema_version"], "scenario": core["scenario"],
            "contract": contract, "extraction_notes": list(dict.fromkeys(notes))}


def _table_views(page: Path, table: dict, rows: list[int], root: Path) -> list[Path]:
    original = root / page.name
    shutil.copy2(page, original)
    with Image.open(page) as opened:
        # Keep the full original page as a fallback: approximate layout coordinates
        # cannot discard a merged cell, a nonuniform row, a header or an adjacent clause.
        rotation = {0: None, 90: Image.Transpose.ROTATE_270,
                    180: Image.Transpose.ROTATE_180, 270: Image.Transpose.ROTATE_90}
        operation = rotation[table["clockwise_rotation"]]
        upright = opened.copy() if operation is None else opened.transpose(operation)
        full = root / (page.stem + "--upright.png")
        upright.save(full, format="PNG")
        bounds = table["body_bounds"]
        span = bounds["bottom"] - bounds["top"]
        top = bounds["top"] + span * (rows[0] - 1) / table["row_count"]
        bottom = bounds["top"] + span * rows[-1] / table["row_count"]
        overlap = max(span / table["row_count"], (bottom - top) * .15)
        crop = (max(0, math.floor((bounds["left"] - .02) * upright.width)),
                max(0, math.floor((top - overlap) * upright.height)),
                min(upright.width, math.ceil((bounds["right"] + .02) * upright.width)),
                min(upright.height, math.ceil((bottom + overlap) * upright.height)))
        band = root / (page.stem + f"--rows-{rows[0]:03d}-{rows[-1]:03d}.png")
        upright.crop(crop).save(band, format="PNG")
    return [original, full, band]


def extract_contract_chunks(*, original_pdf: Path, page_images: list[Path], full_schema: Path,
                            stage: Callable, run: Callable, map_chunks: Callable) -> dict:
    from . import codex_runner as api
    full = json.loads(full_schema.read_text(encoding="utf-8"))

    def schema_file(root: Path, value: dict) -> Path:
        path = root / "schema.json"
        path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return path

    core_root, core_skill = stage("contract-core")
    images = api._copy_images(page_images, core_root)
    schema = schema_file(core_root, core_schema(full))
    prompt = (
        f"读取 `{core_skill / 'SKILL.md'}`。只输出 `{schema}` 要求的合同核心和整页清单，不转录门店/销售明细数组。\n"
        f"原 PDF 文件名必须为 {original_pdf.name}；附图按原始页序为 {json.dumps([p.name for p in images], ensure_ascii=False)}。\n"
        "逐页查看全部原图。contract 只保留明确合同核心条款：签约方、经销商/客户、期间、活动预算、活动内容、"
        "准确的核销/结算方式、陈列标准、申报金额、单位费用口径、总堆头数、印章、水印及条件性商品/促销要求。"
        "不要用结算单中的规则替代合同缺失条款。合同水印仅信息事实。只有明确按门店/堆头的单位费用才用 per_store/per_stack；"
        "只有总额用 total_only，口径无法确认用 unclear，这两种情况 fee_per_store=0，绝不除算。"
        "没有明确预算或总堆头数用 null。settlement_method 保留合同原文实质表述，不能只重复 fee_basis；缺失时明确说明。"
        "仅核心条款中明确的具体商品身份可使 requires_specific_products=true，保留原始 visible_text 与可见标识。"
        "全品牌/全系列及附加销售表不构成限定 SKU，未限定时两个商品数组为空。只有核心明确要求具名促销机制才设置 requires_promotion，"
        "否则 required_promotion=null。无法确认不得猜测。\n"
        "sales_attachment 只输出存在性、原始 PDF 页及可见印刷全文总计，不输出 records。"
        "不能把页小计/局部合计当全文总计，无法确定总计范围则相应总计为 null；绝不能加总、相乘或从其他文件填值。"
        "没有销售附件则 present=false、source_pages=[]、两项总计为 null。\n"
        "page_inventory 必须包含每一页（包括无表页），按原 PDF 页顺序。只盘点两类业务明细表：stores 门店名单和"
        "sales_attachment 销售明细。不要将核心字段表、费用汇总表当明细。每页按阅读顺序从 1 编 table_no；"
        "row_count 是实际印刷业务行数，不含表头、空白、合计和小计。不要把表格值放入清单，不得因编号重启或同值交易合并行。"
        "这是有限输出的总览；禁止输出 stores 或 records，后续在独立上下文逐页、逐块转录。"
    )

    def validate_core(value: dict) -> None:
        if value["contract"]["source_file"] != original_pdf.name:
            raise AuditError("合同核心来源文件必须保持原始 PDF basename")
        validate_inventory(value["page_inventory"], len(page_images))

    core = run(core_root, core_skill, schema, prompt, images, "合同核心与整页清单", validate_core)

    def read_layout(indexed: tuple[int, Path]) -> dict:
        page_no, page = indexed
        root, skill = stage(f"contract-layout-{page_no:03d}")
        images = api._copy_images([page], root)
        schema = schema_file(root, layout_schema(original_pdf.name, page_no))
        prompt = (
            f"读取 `{skill / 'SKILL.md'}`，返回 `{schema}`。只查看原 PDF {original_pdf.name} 第 {page_no} 页。"
            "独立检查完整页面，盘点全部门店名单(stores)和销售明细(sales_attachment)，无这两类表则 tables=[]。"
            "不要把核心字段表、费用汇总表列为明细。不提供其他提取结果，也不需要转录任何业务单元格。"
            "每页按阅读顺序从 1 编 table_no。逐行确认 row_count，排除表头、空白、合计和小计，同值交易仍分别计数。"
            "clockwise_rotation 是从附图顺时针旋转到文字正向的度数；body_bounds 是旋转后完整页面中"
            "只包含全部明细行的矩形，坐标按页面宽高归一化到 [0,1]（不含表头和合计）。"
            "必须检查页首、页尾、横向表及盖章覆盖区域，不能省略难读业务行；布局不能建立业务判断。"
        )
        return run(root, skill, schema, prompt, images, f"合同整页独立盘点 {page_no}/{len(page_images)}",
                   lambda value: validate_layout(value, core["page_inventory"][page_no - 1]))

    layouts = map_chunks(read_layout, list(enumerate(page_images, 1)))
    tasks = [(page["source_page"], table, rows)
             for page in layouts for table in page["tables"]
             for rows in chunk_sequence(list(range(1, table["row_count"] + 1)), CONTRACT_ROW_BATCH_SIZE)]

    def read_rows(task: tuple[int, dict, list[int]], suffix: str = "") -> list[dict]:
        page_no, table, rows = task
        root, skill = stage(f"contract-table-{page_no:03d}-{table['table_no']:03d}-{rows[0]:03d}{suffix}")
        images = _table_views(page_images[page_no - 1], table, rows, root)
        schema = schema_file(root, table_schema(full, table["kind"], original_pdf.name, page_no, table["table_no"], rows))
        prompt = (
            f"读取 `{skill / 'SKILL.md'}`，返回 `{schema}`。原 PDF {original_pdf.name} 第 {page_no} 页，"
            f"第 {table['table_no']} 个 {table['kind']} 明细表，共 {table['row_count']} 条印刷业务行。"
            f"本块只转录物理行 {json.dumps(rows)}，每行恰好一次，保持顺序。编号从本表首条业务行起，不把表头/合计/小计计入。"
            "原扫描、正向副本、带重叠的阅读辅助裁剪均来自同页像素，不是多份来源。裁剪只是大致定位，"
            "行高不均或合并单元格跨边界时必须回看完整原页；不能因为裁剪截断而填写缺失。"
            "相邻可见行只作定位，不得输出。不要合并相同值的交易，不按印刷编号重启而重排。"
            "只能原样转录当前行可见的名称、地址/数量，或销售客户、日期、商品编码/名称/69码、单位、数量、零售价及金额。"
            "无法辨认的业务字段使用 schema 允许的 null 并说明，不补值、不计算。只有原图明确合并单元格或表头作用域"
            "覆盖本行时才允许采用该印刷共同文字，并在 extraction_notes 记录来源关系。"
            "门店 stack_count 仅明确逐店数量或本页明确每店一个堆头时可填，否则 null。"
            "barcode_69 只有全部13位印刷可读且 EAN-13 有效时才填，否则 null。"
            "不要输出印刷合计行，不判断费用口径、促销要求或核销结果，不读取 Excel、目录、其他块输出或历史答案。"
        )
        try:
            return [run(root, skill, schema, prompt, images,
                        f"合同首轮表格 第{page_no}页 表{table['table_no']} 行{rows[0]}-{rows[-1]}",
                        lambda value: validate_rows(value, rows))]
        except api.CodexRequestConfigurationError:
            raise
        except api.CodexExtractionError:
            if len(rows) == 1:
                raise
            return [fragment for index, subset in enumerate(chunk_sequence(rows, (len(rows) + 1) // 2), 1)
                    for fragment in read_rows((page_no, table, subset), suffix + f"-s{index}")]

    fragments = [fragment for group in map_chunks(read_rows, tasks) for fragment in group]
    result = merge_contract(core, layouts, fragments)
    api._validate_contract_result({"contract_pdf": str(original_pdf)}, result, source_page_count=len(page_images))
    save_model_observations("promotional-display-contract", {
        "version": 1, "row_batch_size": CONTRACT_ROW_BATCH_SIZE,
        "page_inventory": core["page_inventory"], "independent_layouts": layouts,
        "fragments": fragments,
    })
    return result
