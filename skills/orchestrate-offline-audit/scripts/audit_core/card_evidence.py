"""Read-only, source-local provenance for business cards, not new audit decisions.

Dependencies are explicit business roles. Never guess roles from filenames, fill
one document's facts from another, or silently substitute the whole case.
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable


def text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def basename(value: Any) -> str:
    return re.split(r"[\\/]", text(value))[-1]


def unique(values: Iterable[Any]) -> list[str]:
    return list(dict.fromkeys(text(value) for value in values if text(value)))


def row_numbers(values: Iterable[Any]) -> str:
    numbers = sorted({int(value) for value in values if str(value).isdigit()})
    groups: list[str] = []
    for value in numbers:
        if not groups or value != end + 1:
            start = value
            groups.append(str(value))
        else:
            groups[-1] = f"{start}–{value}"
        end = value
    return "、".join(groups)


ROLE_ALIASES = {
    "signed_promotional_contract": "contract", "promotional_contract": "contract",
    "entry_fee_contract": "contract", "contract": "contract",
    "settlement": "settlement", "stamped_pos_data": "pos_visual",
    "pos_data": "pos_visual", "pos_spreadsheet": "pos_sheet",
    "sales_delivery_statement": "delivery", "sales_delivery": "delivery",
    "store_receipt": "receipt", "activity_photo": "photo", "shelf_photo": "photo",
    "purchase_invoice_or_receipt": "purchase", "invoice_or_receipt": "purchase",
    "purchase_payment_record": "payment", "system_deduction_proof": "deduction",
    "supporting_document": "support", "sales_excel": "sales",
    "transfer": "transfer", "activity_return": "return_sheet",
}
ROLE_LABELS = {
    "contract": "合同基准", "settlement": "结算申报", "pos_visual": "盖章 POS",
    "pos_sheet": "POS 电子表", "delivery": "系统销售/出货明细", "receipt": "门店小票",
    "photo": "现场照片", "purchase": "采购票据", "payment": "采购付款记录",
    "deduction": "系统扣款凭证", "support": "专项支持材料", "sales": "销售 Excel",
    "transfer": "转账凭证", "return_sheet": "返图工作簿（只作来源路由）",
}
# Exact role sets, reviewed against the deterministic scenario control code.
CONTROL_ROLES = {
    "fee_nature": ("contract", "settlement"),
    "promotional_contract": ("contract",), "contract_authority": ("contract",),
    "contract_amount": ("contract",), "settlement": ("settlement",),
    "sales_delivery_statement": ("delivery",),
    "shipment_reconciliation": ("settlement", "delivery"),
    "receipt_execution": ("receipt", "contract"),
    "activity_execution": ("photo", "contract"),
    "activity_existence": ("photo", "receipt", "support"),
    "pos_visual_seal": ("pos_visual",), "pos_spreadsheet": ("pos_sheet",),
    "pos_correspondence": ("pos_visual", "pos_sheet"),
    "party_alignment": ("contract", "settlement", "pos_visual"),
    "period_alignment": ("contract", "settlement"),
    "dealer_recipient": ("contract", "settlement"), "price_terms": ("contract",),
    "system_deduction_proof": ("contract", "deduction"),
    "source_integrity": ("photo",),
    "shelf_photo_coverage": ("contract", "photo"),
    "all_store_photo_coverage": ("contract", "photo"),
    "activity_photo_coverage": ("contract", "photo", "return_sheet"),
    "contract_and_gift_rule": ("contract", "settlement"),
    "purchase_document": ("purchase", "payment", "contract", "settlement"),
    "gift_quantity_sufficiency": ("contract", "settlement", "pos_sheet"),
    "type_specific_support": ("support", "photo", "contract"),
    "amount_recalculation": ("contract", "settlement", "pos_visual", "pos_sheet"),
    "product_correspondence": ("contract", "settlement", "delivery", "receipt"),
}
CONTROL_OVERRIDES = {
    ("giveaway_promotion", "party_alignment"): ("contract", "settlement", "delivery", "receipt"),
    ("giveaway_promotion", "period_alignment"): ("contract", "settlement", "delivery", "receipt", "photo"),
    ("giveaway_promotion", "amount_recalculation"): ("contract", "settlement"),
    ("maintenance_fee", "party_alignment"): ("contract", "settlement", "pos_visual", "pos_sheet"),
    ("maintenance_fee", "period_alignment"): ("contract", "settlement", "pos_visual", "pos_sheet", "photo"),
    ("self_procured_gift_material", "amount_recalculation"): ("contract", "settlement", "purchase", "payment"),
}

FIELD_LABELS = {
    "title": "文件标题", "party_names": "主体", "party_a": "甲方", "party_b": "乙方",
    "customer_name": "客户名称", "dealer_name": "经销商", "store_name": "门店",
    "recipient_type": "激励对象类型", "activity_start": "活动开始", "activity_end": "活动结束",
    "agreement_date": "协议日期", "document_date": "单据日期", "fee_type": "费用类型",
    "activity_budget": "活动预算", "contract_budget": "合同预算", "total_amount": "总金额",
    "claimed_amount": "申报金额", "document_amount": "单据金额", "shipment_amount": "正常出货金额",
    "claimed_gift_amount": "赠品申报金额", "material_quantity": "物料数量",
    "material_unit_price": "物料单价", "payment_amount": "付款金额", "payment_time": "付款时间",
    "payer_name": "付款方", "payee_name": "收款方", "transaction_number": "交易编号",
    "gift_ratio_text": "买赠比例", "calculation_text": "计算原文", "calculation_method": "计算方式",
    "original_price": "原价", "activity_price": "活动价", "support_unit_amount": "每件补差",
    "planned_quantity": "计划数量", "settlement_quantity": "结算数量", "amount_ceiling": "金额上限",
    "pos_sales_amount": "POS 销售基数", "pos_total_sales_amount": "POS 合计金额",
    "pos_total_quantity": "POS 合计数量", "total_sales_amount": "电子表销售合计",
    "total_quantity": "明细数量合计", "printed_total_amount": "打印金额合计",
    "printed_total_quantity": "打印数量合计", "calculated_total_amount": "明细金额复算合计",
    "store_count": "门店数", "detail_row_count": "明细行数", "period_values": "填报业务期间",
    "store_values": "电子表门店", "customers": "销售客户", "target_tiers": "目标档位与比例",
    "receipt_number": "小票编号", "receipt_total_paid": "小票实付",
    "signed_visible": "签署/签章", "dealer_seal_visible": "经销商印章",
    "customer_seal_visible": "客户印章", "issuer_stamp_visible": "开票方印章",
    "party_a_seal_visible": "甲方印章", "party_b_seal_visible": "乙方印章",
    "company_template_visible": "统一模板", "seal_visible": "印章",
    "watermark_date": "水印日期", "watermark_time": "水印时间", "watermark_location": "水印地点",
    "photo_date": "照片日期", "photo_time": "照片时间", "photo_location": "照片地点",
    "activity_date": "现场日期", "activity_time": "现场时间", "activity_location": "现场地点",
    "activity_content": "活动内容", "display_standard": "陈列要求", "settlement_method": "核销方式",
    "fee_per_store": "单店/单堆标准金额", "declared_total_reward": "结算奖励合计",
    "declared_total_quantity": "结算数量合计", "external_formula_cells": "外部依赖单元格",
    "product_name": "商品名称", "product_code": "商品编码", "barcode_69": "69码",
    "barcode": "条码", "quantity": "数量", "unit_price": "单价", "amount": "金额",
    "retail_price": "零售价", "reward_amount": "奖励金额", "unit_reward": "单位奖励",
    "barcode_fee": "条码费", "contracted_store_count": "约定门店数",
    "contract_total_amount": "合同总额", "contract_stores": "合同门店清单",
    "photo_store_name": "照片水印门店", "visible_products": "照片可见商品",
    "visible_package_text": "可见包装文字", "watermark_date_visible": "日期水印可见性",
    "watermark_time_visible": "时间水印可见性", "watermark_address_visible": "地点水印可见性",
    "shelf_display_visible": "上架陈列可见性", "threshold_amount": "销售额门槛", "rate": "比例",
    "line_no": "项目序号", "full_address": "完整地址", "terminal_system": "终端系统",
    "store_type": "门店类型", "finished_effect_visible": "成品效果可见性",
    "display_position": "摆放位置", "promotion_mechanic": "促销机制",
    "promotion_content_visible": "促销内容可见性", "self_procured_material_visible": "自采物料可见性",
    "buy_quantity": "购买数量", "gift_quantity": "赠送数量", "limited_quantity_rule": "限量规则",
}
DISPLAY_FIELDS = {"business_date": "业务日期", **FIELD_LABELS}
STATUS_TEXT = {"visible": "可见", "not_visible": "不可见", "unclear": "不清楚",
               "not_applicable": "不适用", "exact": "一致", "fuzzy": "名称模糊辅助匹配",
               "dealer": "经销商", "per_stack": "按堆头计费", "per_store": "按门店计费"}


def display_value(value: Any) -> str:
    if value is None:
        return "未记录/未识别"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, list):
        return "；".join(display_value(item) for item in value) or "无"
    if isinstance(value, dict):
        return "，".join(f"{FIELD_LABELS.get(key, key)}={display_value(item)}" for key, item in value.items())
    return STATUS_TEXT.get(str(value), str(value))


def document_facts(document: dict[str, Any]) -> list[str]:
    facts = [f"{label}：{display_value(document[key])}" for key, label in FIELD_LABELS.items()
             if key in document and document[key] is not None]
    for collection in ("product_lines", "line_items", "contract_items", "lines"):
        for index, line in enumerate(document.get(collection) or [], 1):
            if not isinstance(line, dict):
                continue
            values = [f"{label}={display_value(line[key])}" for key, label in FIELD_LABELS.items()
                      if key in line and line[key] is not None]
            if values:
                facts.append(f"明细第{line.get('line_no', index)}行：" + "；".join(values))
    if not facts and document.get("visible_summary"):
        facts.append(text(document["visible_summary"]))
    if isinstance(document.get("activity_evidence"), dict):
        facts.extend(document_facts(document["activity_evidence"]))
    return facts


def document_locator(document: dict[str, Any], filename: str) -> str:
    if document.get("source_page"):
        return f"第 {document['source_page']} 页"
    if document.get("source_pages"):
        return "第 " + "、".join(map(str, document['source_pages'])) + " 页"
    if document.get("sheet"):
        rows = row_numbers(record.get("excel_row") for record in document.get("records") or [] if isinstance(record, dict))
        return text(document["sheet"]) + (f"，第 {rows} 行" if rows else "（原记录未保留明细行号）")
    if filename.lower().endswith(".pdf"):
        return "PDF 对应字段（原记录未保留页码）"
    if filename.lower().endswith((".xlsx", ".xls")):
        return "原记录未保留工作表/单元格定位"
    return "图片可见字段（原记录未保留区域坐标）"


class EvidenceContext:
    def __init__(self, scenario: str, result: dict[str, Any]):
        self.scenario = scenario
        self.result = result
        self.audit = result.get(f"{scenario}_audit") or result
        self.case = result.get("_presentation_case") or {}
        self.documents: dict[str, dict[str, Any]] = {}
        self.roles: dict[str, str] = {}
        self.derived: dict[str, tuple[str, str]] = {}
        raw = result.get("_presentation_evidence") or {}
        # Saved extraction is a companion source, never a replacement decision.
        for bundle in (raw, self.audit):
            for doc in bundle.get("documents") or []:
                self.add(doc)
            for key, role in (("contract", "contract"), ("settlement", "settlement"),
                              ("invoice_receipt", "purchase"), ("sales", "sales"),
                              ("pos_spreadsheet", "pos_sheet"), ("activity_return", "return_sheet")):
                if isinstance(bundle.get(key), dict):
                    self.add(bundle[key], role)
            for doc in bundle.get("field_photos") or []:
                self.add(doc, "photo")
        for entry in self.case.get("document_roles") or []:
            self.add({"source_file": entry.get("path"), "role": entry.get("role")})
        for key, role in (("sales_excel", "sales"), ("settlement_image", "settlement"),
                          ("transfer_images", "transfer"), ("contract_pdf", "contract"),
                          ("photo_files", "photo"), ("pos_spreadsheet", "pos_sheet"),
                          ("activity_return_workbook", "return_sheet")):
            values = self.case.get(key) or []
            for value in values if isinstance(values, list) else [values]:
                self.add({"source_file": value}, role)
        for transfer in result.get("transfer_evidence") or []:
            for filename in transfer.get("source_files") or []:
                self.add({"source_file": filename}, "transfer")
        activity_return = self.case.get("activity_return") or {}
        for record in activity_return.get("records") or []:
            photo = basename(record.get("photo_file"))
            parent = basename(activity_return.get("source_file"))
            if photo and parent:
                self.derived[photo] = (parent, f"{activity_return.get('sheet') or '工作表名未记录'}，第 {record.get('excel_row', '?')} 行嵌入图")
        for store in result.get("store_reconciliation") or []:
            for filename in store.get("photo_files") or []:
                self.add({"source_file": filename}, "photo")

    def add(self, document: dict[str, Any], role: str = "") -> None:
        name = basename(document.get("source_file"))
        if not name:
            return
        selected_role = role or ROLE_ALIASES.get(text(document.get("role")), "") or ROLE_ALIASES.get(text(document.get("document_type")), "")
        self.documents.setdefault(name, {}).update(document)
        if selected_role:
            self.roles[name] = selected_role

    def files(self, *roles: str) -> list[str]:
        return [name for name in self.documents if self.roles.get(name) in roles]

    def control_files(self, control: str) -> list[str]:
        if control == "required_materials":
            return list(self.documents)
        if control == "duplicate_evidence":
            # Giveaway hashes every visual source, including skills/orchestrate-offline-audit/references/contracts/receipts.
            declared = self.case.get("visual_files")
            return unique(map(basename, declared)) if declared else [name for name in self.documents if not name.lower().endswith((".xls", ".xlsx"))]
        roles = CONTROL_OVERRIDES.get((self.scenario, control), CONTROL_ROLES.get(control, ()))
        return self.files(*roles)

    def make(self, files: Iterable[str], *, purposes: dict[str, str] | None = None,
             facts: dict[str, list[str]] | None = None, locators: dict[str, str] | None = None,
             references: list[dict[str, Any]] | None = None, comparisons: list[str] | None = None,
             limitations: list[str] | None = None) -> dict[str, Any]:
        names = unique(basename(name) for name in files)
        sources = []
        originals = []
        gaps = list(limitations or [])
        for name in names:
            doc = self.documents.get(name, {})
            parent, origin_locator = self.derived.get(name, (name, ""))
            originals.append(parent)
            saved_facts = (facts or {}).get(name)
            if saved_facts is None:
                saved_facts = document_facts(doc)
            sources.append({
                "file": name, "original_file": parent,
                "kind": "derived" if parent != name else "submitted",
                "role": (purposes or {}).get(name) or ROLE_LABELS.get(self.roles.get(name, ""), "原检查列出的来源文件"),
                "locator": (locators or {}).get(name) or origin_locator or document_locator(doc, name),
                "facts": saved_facts or ["原记录没有保存此文件的独立字段值；不能从其他文件补入。"],
            })
        if not names:
            gaps.append("原记录未保留可追溯的文件关联，涉及文件总数无法确认；不能据此认为只核对了 0 份文件。")
        return {"schema_version": "1.0", "source_files": unique(originals),
                "source_file_count": len(unique(originals)), "sources": sources,
                "derived_file_count": sum(source["kind"] == "derived" for source in sources),
                "references": references or [], "comparisons": comparisons or [],
                "limitations": unique(gaps), "file_count_complete": bool(names)}


def knowledge_reference(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    facts = []
    for record in records:
        product = record.get("knowledge_product_id") or record.get("reference_product_id") or record.get("product_id")
        values = [f"商品 ID：{product}" if product else "",
                  f"名称：{record.get('knowledge_product_name') or record.get('product_name')}" if record.get('knowledge_product_name') or record.get('product_name') else "",
                  f"69码：{record.get('knowledge_barcode_69') or record.get('barcode_69')}" if record.get('knowledge_barcode_69') or record.get('barcode_69') else "",
                  f"商品编码：{record.get('knowledge_product_code') or record.get('product_code')}" if record.get('knowledge_product_code') or record.get('product_code') else ""]
        if any(values):
            facts.append("；".join(value for value in values if value))
        if record.get("matched_view_ids"):
            facts.append("参考图 ID：" + "、".join(record["matched_view_ids"]) + "（历史记录未保存图片文件路径）")
        facts.extend(record.get("visible_basis") or [])
    return [{"name": "参半商品资料 / 本次已保存商品匹配", "locator": "本次结果保存的商品匹配条目（不读取当前主数据重判历史结果）",
             "facts": facts or ["历史记录未保存命中商品/参考图明细。"],
             "scope": "仅作商品身份与包装参考，不是客户提交的现场、日期、门店或金额凭证。"}]


def map_reference(store: dict[str, Any]) -> list[dict[str, Any]]:
    location = store.get("location_resolution") or {}
    if not location:
        return []
    facts = []
    for key, label in (("contract_poi", "合同地点"), ("watermark_poi", "水印地点")):
        poi = location.get(key) or {}
        if poi:
            facts.append(f"{label}：{poi.get('name')}；地址：{poi.get('address')}；POI ID：{poi.get('poi_id')}；坐标：{poi.get('longitude')},{poi.get('latitude')}")
    if location.get("distance_meters") is not None:
        facts.append(f"两点直线距离：{location['distance_meters']} 米；邻近阈值：{location.get('nearby_pass_meters', '未记录')} 米")
    if location.get("basis"):
        facts.append(text(location["basis"]))
    return [{"name": "地图地点核验（本次保存的查询结果）", "locator": "合同门店与照片水印地点的候选配对",
             "facts": facts or ["原记录未保留 POI 明细。"], "scope": "辅助确认地点关系，不能替代现场照片。"}]


def period_facts(sales: dict[str, Any], field: str) -> list[str]:
    grouped: dict[str, list[Any]] = defaultdict(list)
    for record in sales.get("records") or []:
        grouped[text(record.get(field)) or "未填写/未识别"].append(record.get("excel_row"))
    return [f"第 {row_numbers(rows) or '未记录'} 行（{len(rows)} 行）：{value}" for value, rows in grouped.items()]


def enrich_pass_item(context: EvidenceContext, item: dict[str, Any]) -> None:
    result, audit = context.result, context.audit
    files = list(item.get("source_files") or [])
    facts: dict[str, list[str]] = {}
    locators: dict[str, str] = {}
    purposes: dict[str, str] = {}
    references: list[dict[str, Any]] = []
    comparisons: list[str] = []
    limitations: list[str] = []
    category = item.get("category")
    if item.get("control_id"):
        files = context.control_files(item["control_id"])
        if item['control_id'] == 'required_materials':
            purposes = {name: "材料清单核验：" + ROLE_LABELS.get(context.roles.get(name, ''), '已提交来源') for name in files}
        if item['control_id'] == 'pos_spreadsheet':
            sheet = audit.get('pos_spreadsheet') or {}
            if sheet:
                item['basis'] = f"本电子表明细 {sheet.get('detail_row_count', '未记录')} 行，门店 {sheet.get('store_count', '未记录')} 家；销售合计 {sheet.get('total_sales_amount', '未记录')} 元；外部依赖单元格：{display_value(sheet.get('external_formula_cells'))}。仅核验电子表内部，不代表已与盖章 POS 或合同对应。"
            else:
                limitations.append('历史结果未保存 POS 电子表的行数、门店数和合计字段，不能补写这些数值。')
        if item['control_id'] == 'pos_visual_seal':
            for name in files:
                doc = context.documents.get(name, {})
                facts[name] = [f"{FIELD_LABELS[key]}：{display_value(doc[key])}" for key in ('customer_name', 'dealer_name', 'dealer_seal_visible', 'customer_seal_visible', 'signed_visible') if key in doc and doc[key] is not None]
    elif context.scenario == "promotional_display":
        contract, sales = result.get("contract") or {}, result.get("sales") or {}
        contract_file = basename(contract.get("source_file"))
        sales_file = basename(sales.get("source_file"))
        if category == "合同核心字段":
            checks = (result.get('contract_core_reconciliation') or {}).get('field_checks') or []
            check = next((check for check in checks if item['title'] == f"{check.get('label')}核验通过"), {})
            field = check.get('field') or {"执行期间": "execution_period", "执行周期": "execution_period", "合同签订方": "contracting_party"}.get(check.get('label'))
            files = [contract_file]
            facts[contract_file] = [f"{check.get('label', '合同字段')}：{display_value(check.get('contract_value'))}"]
            if field in {'execution_period', 'contracting_party'}:
                files.append(sales_file)
                sale_field = 'period_text' if field == 'execution_period' else 'customer_name'
                field_label = '业务日期' if field == 'execution_period' else '客户名称'
                facts[sales_file] = period_facts(sales, sale_field) or [f"{field_label}：{display_value(check.get('comparison_value'))}（未保存逐行定位）"]
                locators[sales_file] = document_locator(sales, sales_file) + f"，“{field_label}”列（原记录未保留列字母）"
                item['subject'] = f"合同：{display_value(check.get('contract_value'))}；销售 Excel：{display_value(check.get('comparison_value'))}"
                if field == 'execution_period':
                    row_count = len(sales.get('records') or [])
                    item['basis'] = f"合同填写执行周期 {display_value(check.get('contract_value'))}；销售 Excel 的“业务日期”列已保存 {row_count} 行，逐组填报值及行号见下方。原检查仅确认可解析的填报期间落在合同周期内，不是逐笔实际交易日期核验。"
                    blanks = sum(not text(record.get('period_text')) for record in sales.get('records') or [])
                    if blanks:
                        limitations.append(f"另有 {blanks} 行业务日期为空；原检查按非空去重期间判断，不能声称全部销售行均已核实。")
                else:
                    item['basis'] = f"合同签订方：{display_value(check.get('contract_value'))}；销售客户：{display_value(check.get('comparison_value'))}。本项仅核对客户名称对应，不核实签署授权。"
            elif check.get('comparison_value') is not None:
                facts[contract_file].append("同一合同中的对照值/计费口径：" + display_value(check['comparison_value']))
            locators[contract_file] = f"合同“{check.get('label', '核心字段')}”（原记录未保留页码）"
        elif category == "销售明细":
            files = [sales_file]
            item['basis'] = text(sales.get('internal_basis')) or item['basis']
            for record in sales.get('records') or []:
                comparisons.append(f"{sales.get('sheet', '工作表名未记录')} 第 {record.get('excel_row', '?')} 行：数量 {record.get('quantity')} × 零售价 {record.get('retail_price')} = {record.get('calculated_total_amount')}；行内金额 {record.get('total_amount')}；差额 {record.get('line_amount_difference')}。")
        elif category == "合同附件与销售":
            files = [contract_file, sales_file]
            for record in (result.get('contract_attachment_sales_reconciliation') or {}).get('records') or []:
                if record.get('status') != 'pass':
                    continue
                prefix = f"合同 PDF 第 {record.get('contract_source_page', '?')} 页、附件第 {record.get('contract_line_no', '?')} 行 ↔ {sales.get('sheet', '工作表名未记录')} 第 {record.get('sales_excel_row', '?')} 行"
                comparisons.append(prefix + "：" + "；".join(f"{DISPLAY_FIELDS.get(field.get('field'), field.get('field'))}：合同 {display_value(field.get('contract_value'))} / Excel {display_value(field.get('sales_value'))}（{display_value(field.get('status'))}）" for field in record.get('field_comparisons') or []))
            locators[contract_file] = "合同销售附件；逐行页码、行号和双方值见对照明细"
        elif category == "照片重复":
            files = unique(name for store in result.get('store_reconciliation') or [] for name in store.get('photo_files') or [])
            facts = {basename(name): ["纳入本次图片内容去重集合；原结果未发现完全相同内容。"] for name in files}
            limitations.append("仅排除文件内容完全相同；不等于排除翻拍、近似图片或同场景重复使用。")
        else:
            store = next((store for store in result.get('store_reconciliation') or [] if store.get('contract_store_name') == item.get('subject')), {})
            photos = unique(map(basename, store.get('photo_files') or []))
            files = photos + ([contract_file] if category != '现场商品' else [])
            if category == '活动日期':
                facts[contract_file] = [f"合同执行周期：{contract.get('activity_start')} 至 {contract.get('activity_end')}"]
                comparisons = [f"本门店照片组识别日期：{store.get('visible_date') or '未记录'}；合同周期：{contract.get('activity_start')} 至 {contract.get('activity_end')}。"]
            elif category == '门店地点':
                facts[contract_file] = [f"合同门店清单第 {store.get('store_line_no', '?')} 项：{store.get('contract_store_name')}"]
                comparisons = [f"合同门店：{store.get('contract_store_name')}；照片组水印地点：{store.get('visible_location') or '未记录'}。"]
                references = map_reference(store)
            elif category == '陈列标准':
                facts[contract_file] = ["合同陈列要求：" + text(contract.get('display_standard'))]
                comparisons = [f"照片组可分辨列数：{store.get('display_vertical_facing_count', '未记录')}；{store.get('display_description', '')}"]
                item['basis'] = f"合同要求：{contract.get('display_standard') or '未记录'}；现场观察：{store.get('display_description') or '未记录'}"
            elif category == '现场商品':
                references = knowledge_reference(store.get('product_reference_hits') or [])
                comparisons = ["照片组可见文字：" + '、'.join(store.get('visible_text') or []), *[text(value) for value in store.get('visible_recognized_products') or []]]
            elif category == '合同商品范围':
                references = knowledge_reference(store.get('product_reference_hits') or [])
                comparisons = [text(store.get('photo_contract_product_basis'))]
            elif category == '金额规则':
                # The rule is contract units × contract fee, not a photo approval.
                files = [contract_file]
                facts[contract_file] = [f"门店：{store.get('contract_store_name')}；合同计费数量：{store.get('claim_units')}；标准：{contract.get('fee_per_store')} 元"]
                limitations.append("只复算合同计费口径；没有据此确认照片执行合格或批准本店核销金额。")
            if len(photos) > 1 and comparisons:
                limitations.append("上述现场观察按本门店照片组保存；原记录未区分每张图分别贡献哪个字段，不能将组内结论归给任意单张图。")
            for photo in photos:
                facts[photo] = ["本门店照片组成员；本卡使用的组级观察列在下方对照明细。"]
    elif context.scenario == 'personnel_incentive':
        sales, settlement = result.get('sales') or {}, result.get('settlement') or {}
        sale_file, settlement_file = basename(sales.get('source_file')), basename(settlement.get('source_file'))
        record = next((record for record in result.get('sku_reconciliation') or [] if item.get('subject') in [record.get('knowledge_product_name'), record.get('excel_product_name'), record.get('settlement_product_name')]), {})
        if record:
            rows = row_numbers(record.get('excel_rows') or [])
            locators[sale_file] = f"{sales.get('sheet') or '工作表名未记录'}，第 {rows or '未记录'} 行"
            locators[settlement_file] = f"结算单第 {record.get('line_no', '?')} 项（原记录未保留图像区域坐标）"
            facts[sale_file] = [f"商品名称：{record.get('excel_product_name')}；69码：{record.get('mapped_barcode')}；所列行数量合计：{record.get('excel_quantity')}"]
            facts[settlement_file] = [f"商品名称：{record.get('settlement_product_name')}；数量：{record.get('settlement_quantity')}；单位奖励：{record.get('unit_reward')} 元；该项金额：{record.get('settlement_reward_amount')} 元"]
            if category == '商品知识库':
                files = [sale_file]
                references = knowledge_reference([record])
                item['basis'] = text(record.get('knowledge_basis')) or item['basis']
            elif category == '结算商品对应':
                files = [sale_file, settlement_file]
                references = knowledge_reference([record])
                item['basis'] = text(record.get('mapping_basis')) or item['basis']
            elif category == '销售数量':
                files = [sale_file, settlement_file]
            elif category == '激励金额':
                files = [settlement_file]
        elif category == '转账金额':
            transfer = next((record for record in result.get('store_transfer_reconciliation') or [] if record.get('store_name') == item.get('subject')), {})
            evidence = next((record for record in result.get('transfer_evidence') or [] if record.get('transfer_id') == transfer.get('transfer_id')), {})
            files = [sale_file, settlement_file, *evidence.get('source_files', [])]
            records = [record for record in sales.get('records') or [] if record.get('store_name') == transfer.get('store_name')]
            locators[sale_file] = f"{sales.get('sheet') or '工作表名未记录'}，第 {row_numbers(record.get('excel_row') for record in records) or '未记录'} 行"
            facts[sale_file] = [f"门店：{transfer.get('store_name')}；销售数量合计：{transfer.get('excel_quantity')}"]
            facts[settlement_file] = [f"第 {record.get('line_no', '?')} 项：{record.get('settlement_product_name')}，单位奖励 {record.get('unit_reward')} 元" for record in result.get('sku_reconciliation') or []]
            for name in evidence.get('source_files') or []:
                facts[basename(name)] = [f"本次去重转账编号：{evidence.get('transfer_id')}；可见金额：{evidence.get('amount')} 元", text(evidence.get('dedup_basis'))]
            limitations.append("门店对应是按应付金额集合匹配，不是收款身份匹配；截图未证实的姓名、门店、完整日期仍需补交。")
    elif context.scenario == 'poster_material' and category == '现场水印':
        contract = audit.get('contract') or {}
        if contract.get('activity_start') and contract.get('activity_end'):
            files += context.files('contract')
            comparisons = [f"合同执行周期：{contract['activity_start']} 至 {contract['activity_end']}"]
        else:
            item['basis'] = item['basis'].replace('，日期位于合同执行期内。', '。只确认水印字段可见；合同周期缺失，未完成期间核对。')
            limitations.append("合同周期缺失，本卡不能证明照片日期在合同期内。")
    elif category == '门店返图':
        files += context.files('contract')
        photo = next((photo for photo in audit.get('photo_reconciliation') or [] if basename(photo.get('source_file')) in item.get('source_files', [])), {})
        comparisons = [f"合同门店：{photo.get('matched_store') or '未保存匹配名称'}；{photo.get('store_match_basis') or '原记录未保存门店名称对照过程'}"]
        limitations.append("只记录已通过的门店/照片子项；返图工作簿中的路由名称不是照片可见门店证据。")
    evidence = context.make(files, facts=facts, locators=locators, purposes=purposes,
                            references=references, comparisons=comparisons, limitations=limitations)
    if any(not text(name) for name in files):
        evidence['file_count_complete'] = False
        evidence['limitations'].append('本检查需要的部分来源文件名未保存在历史结果中；已列出文件不是完整来源总数。')
    item['evidence'] = evidence
    item['source_files'] = evidence['source_files']
    item['source_file_count'] = evidence['source_file_count']


ISSUE_CONTROLS = {
    'required_materials_missing': 'required_materials', 'fee_nature_conflict': 'fee_nature',
    'promotional_contract_invalid': 'promotional_contract', 'contract_authority_failed': 'contract_authority',
    'settlement_invalid': 'settlement', 'sales_delivery_invalid': 'sales_delivery_statement',
    'pos_visual_seal_missing': 'pos_visual_seal', 'pos_spreadsheet_invalid': 'pos_spreadsheet',
    'pos_correspondence_failed': 'pos_correspondence', 'party_alignment_failed': 'party_alignment',
    'period_alignment_failed': 'period_alignment', 'shipment_reconciliation_failed': 'shipment_reconciliation',
    'product_correspondence_failed': 'product_correspondence', 'receipt_execution_failed': 'receipt_execution',
    'activity_execution_missing': 'activity_execution', 'amount_recalculation_failed': 'amount_recalculation',
    'dealer_recipient_failed': 'dealer_recipient', 'activity_existence_failed': 'activity_existence',
    'shelf_photo_coverage_failed': 'shelf_photo_coverage', 'all_store_photo_coverage_failed': 'all_store_photo_coverage',
    'system_deduction_proof_missing': 'system_deduction_proof', 'contract_and_gift_rule_failed': 'contract_and_gift_rule',
    'purchase_document_failed': 'purchase_document', 'activity_photo_coverage_failed': 'activity_photo_coverage',
    'gift_quantity_sufficiency_failed': 'gift_quantity_sufficiency', 'type_specific_support_invalid': 'type_specific_support',
    'contract_authority_missing': 'contract_authority', 'dealer_recipient_unproven': 'dealer_recipient',
    'activity_existence_unproven': 'activity_existence', 'contract_gift_rule_invalid': 'contract_and_gift_rule',
    'purchase_document_invalid': 'purchase_document', 'type_specific_support_missing': 'type_specific_support',
}


def attach_sheet_evidence(sheet: dict[str, Any], context: EvidenceContext) -> None:
    issues = context.audit.get('issues') or []
    for row in sheet.get('rows') or []:
        if row.get('status') != 'issue':
            continue
        heading = re.sub(r'^(问题|错误项)：', '', text(row.get('heading')))
        issue = next((issue for issue in issues if issue.get('title') == heading), None)
        if issue:
            files = list(issue.get('source_files') or [])
            control = ISSUE_CONTROLS.get(issue.get('code'))
            if control:
                files += context.control_files(control)
            row['card_evidence'] = context.make(files, comparisons=[f"读到/缺失：{issue.get('observed')}", f"对照要求：{issue.get('expected')}"], limitations=["未提交的材料不计入文件数；补交要求见本卡处理方式。"])
        elif context.scenario == 'personnel_incentive':
            roles = ('settlement', 'transfer') if heading == '实际申请金额' else ('sales', 'transfer') if heading == '收款人与日期' else ('sales', 'settlement')
            row['card_evidence'] = context.make(context.files(*roles), comparisons=[text(value) for value in row.get('values') or [] if value])
        elif context.scenario == 'promotional_display' and row.get('section') == 'detail':
            store = next((store for store in context.result.get('store_reconciliation') or [] if store.get('contract_store_name') == heading), {})
            photos = store.get('photo_files') or []
            references = map_reference(store) if store.get('store_match') not in {'exact', 'compatible'} else []
            if store.get('photo_knowledge_match') not in {'exact', 'matched'}:
                references += knowledge_reference(store.get('product_reference_hits') or [])
            row['card_evidence'] = context.make([*photos, *context.files('contract')], references=references,
                comparisons=[f"合同门店：{store.get('contract_store_name', heading)}；照片组水印地点：{store.get('visible_location') or '未识别'}；照片组日期：{store.get('visible_date') or '未识别'}", text(store.get('display_description'))])
    if context.scenario == 'promotional_display':
        core_files = context.files('contract')
        checks = (context.result.get('contract_core_reconciliation') or {}).get('field_checks') or []
        if any(check.get('status') != 'pass' and check.get('field') in {'execution_period', 'contracting_party'} for check in checks):
            core_files += context.files('sales')
        sheet['card_evidence'] = {
            'core': context.make(core_files, comparisons=[f"{check.get('label')}：合同 {display_value(check.get('contract_value'))}；对照 {display_value(check.get('comparison_value'))}；{check.get('basis', '')}" for check in checks if check.get('status') != 'pass']),
            'knowledge': context.make(context.files('contract'), references=knowledge_reference((context.result.get('contract_attachment_product_knowledge') or {}).get('records') or [])),
            'sales': context.make(context.files('contract', 'sales')),
        }
