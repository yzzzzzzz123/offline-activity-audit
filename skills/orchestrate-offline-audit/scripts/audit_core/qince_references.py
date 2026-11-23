"""Qince's confirmed read-only fields, scoped to the current application rows.

No scheduler, recognition invocation or write endpoint is provided here. Raw
responses stay in the execution trace; only linked reference facts reach models.
"""
from __future__ import annotations

from datetime import date
import hashlib
import ipaddress
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from .common import AuditError
from .pdf_policy import LOCATION_REFERENCE_CLARIFICATION
from .execution_trace import record_step, safe_value

CUSTOMERS = "queryCustomerV2"
CUSTOMERS_V1 = "queryCustomer"
PRODUCTS = "queryProductV2"
SKU_CODES = "queryAiSkuCode"
PRIMITIVE_REPORT = "queryPrimitiveAiSkuReport"
PRIMITIVE_DETAIL = "queryPrimitiveAiSkuDetail"
CHECKED_REPORT = "queryCheckedAiSkuReport"
CHECKED_DETAIL = "queryCheckedAiSkuDetail"
ORIGINAL = "queryAiOriginalSku"
PICTURES = "queryAiBizPicRel"
COMPARE = "queryAiSkuCompare"
STANDARDS = "queryNewSku"
RESULTS = "queryCompareResults"
ACTUAL = "queryActualProductsV2"

REFERENCE_ENDPOINTS = {
    CUSTOMERS: "/api/v2/customer/query",
    CUSTOMERS_V1: "/api/customer/v1/queryCustomer",
    PRODUCTS: "/api/pd/v2/queryProduct",
    **{key: "/api/ai/v2/" + key for key in
       (PRIMITIVE_REPORT, PRIMITIVE_DETAIL, CHECKED_REPORT, CHECKED_DETAIL)},
    **{key: "/api/ai/v1/" + key for key in (SKU_CODES, ORIGINAL, PICTURES, COMPARE)},
    STANDARDS: "/api/new_sku/v1/queryNewSku",
    RESULTS: "/api/compare_results/v1/queryCompareResults",
    ACTUAL: "/api/new_sku_products/v1/queryActualProductsV2",
}

REFERENCE_POLICY = LOCATION_REFERENCE_CLARIFICATION + (
    " 终端主档仅作身份、地址、行政区及关系参照，收货地址不自动等于现场地址；客户类型未知时不能认定为执行门店。"
    "商品主档、识别码及长宽高须对应同一商品和包装单位；AI识别码不是零售条码，尺寸单位为厘米，不能混用单件、盒、箱。"
    "原始识别、校对、铺货上报与本次照片事实分别保留。仅有同店同日期、图片文件名或bizId不能证明对应本图；"
    "已识别结果只有与本次原图内容指纹核验一致后才附逐图参照，仍须复核本图，不能沿用历史合格结论。"
    "layers是层号，productNum是件数，row_num是排面，均不能直接当纵列数；location是提交位置，position不是商品几何坐标。"
    "纵列定义、目标列数、特殊摆放与检测框等未确认Key不猜填，继续按本单已批准标准核对；遮挡和尺度不足保留无法确认。"
    "铺货标准的生效日期与客户范围必须适用本单；独立铺货要求不自动升级为本次合同要求。原始与校对值同时保留，"
    "审批枚举按接口版本理解，不据其新增核销门禁。接口空值、未授权、未维护及无法关联分别记技术限制，不报成客户漏交资料。"
)

TERMINAL_FIELDS = (
    "id cm_id cm_code cm_name cm_addr cm_mss_province cm_mss_city cm_mss_area cm_mss_street "
    "cm_road_msg cm_house_number cm_type_id cm_type_code cm_type cm_business_type cm_rel_level_id cm_uper_customer"
).split()
PRODUCT_FIELDS = "prd_waiqin365_id prd_id prd_code prd_name prd_barcode prd_brand prd_spec prd_short_name".split()
REPORT_FIELDS = ("bizId customCode customName sourceFormId formDataId sourceAssemblyName sourceFunction "
                 "displayType checkStatus createTime imageArea imageLength imageWidth groundProductNum "
                 "skuProductCount skuBpProductCount skuJpProductCount skuUnknownCount imgUrl location "
                 "agentJudgeResult modifyAgentJudgeResult errReport modifyDate approvalStatus approvalDate").split()
DETAIL_FIELDS = "bizId modifyDate approvalStatus approvalDate".split()
IMAGE_FIELDS = ["imageId", "imageUrl"]
DETECTED_FIELDS = "productId productCode productName productNum layers bjpType type changeType".split()
IDENTITY_KEYS = re.compile(r"(?:^id$|Id$|Ids$|_id$|_ids$)")


def ident(value):
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, float):
        raise AuditError("勤策ID不得使用浮点数，避免长编号精度损失。")
    return str(value).strip()


def lossless_ids(value, key=""):
    if isinstance(value, dict):
        return {k: lossless_ids(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [lossless_ids(v, key) for v in value]
    if IDENTITY_KEYS.search(key) and value is not None:
        return ident(value)
    return value


class PageRows(list):
    def __init__(self, rows, total=None):
        super().__init__(rows)
        self.total = total


def response_rows(value):
    """Accept documented DATA arrays and the AI v2 {total, rows} object."""
    if isinstance(value, dict) and "return_code" in value:
        if str(value["return_code"]) != "0":
            code = str(value["return_code"])
            error = AuditError("勤策只读参照查询未成功；已保存技术状态，不作为客户缺件。")
            error.qince_code = code if re.fullmatch(r"\d{1,6}", code) else "unknown"
            raise error
        value = value.get("response_data")
    if isinstance(value, str):
        try:
            value = json.loads(value, parse_float=str)
        except ValueError:
            raise AuditError("勤策只读参照响应不是有效JSON。") from None
    total = None
    if isinstance(value, dict):
        if "rows" not in value:
            raise AuditError("勤策只读参照响应缺少rows，不能当作空数据。")
        total = value.get("total")
        if total is not None:
            if isinstance(total, bool) or not re.fullmatch(r"\d+", str(total)):
                raise AuditError("勤策分页总数格式无效。")
            total = int(total)
        value = value["rows"]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise AuditError("勤策只读参照响应必须为明细列表。")
    return PageRows(lossless_ids(value), total)


def reference_rows(raw, method):
    if method not in raw:
        return []
    try:
        return response_rows(raw[method])
    except AuditError as exc:
        record_step("qince.reference_unavailable", "勤策补充参照未取得有效数据", {
            "method": method, "code": getattr(exc, "qince_code", None),
            "reason": str(exc)}, status="failed")
        return []


def page_spec(method):
    if method == SKU_CODES:
        return None, None, 0
    if method in (PRIMITIVE_REPORT, PRIMITIVE_DETAIL, CHECKED_REPORT, CHECKED_DETAIL):
        return "page", "rows", 100
    if method in (ORIGINAL, PICTURES, COMPARE):
        return "page", None, 1000 if method == PICTURES else 100
    if method in (CUSTOMERS, CUSTOMERS_V1, PRODUCTS):
        return "page_number", None, 1000
    return "page_number", "page_length", 100


def pick(row, keys):
    return {key: lossless_ids(row[key], key) for key in keys if row.get(key) not in (None, "", [])}


def detail_fields(row):
    return {**pick(row, DETAIL_FIELDS),
            "images": [pick(r, IMAGE_FIELDS) for r in row.get("images", [])],
            "products": [pick(r, DETECTED_FIELDS) for r in row.get("products", [])]}


def many(value):
    if isinstance(value, list):
        return [ident(v) for v in value if ident(v)]
    return [v.strip() for v in re.split(r"[,，]", ident(value)) if v.strip()]


def product_scope(linked, raw):
    ids, codes = set(), set()
    used_plans = set()
    for app, row in linked:
        ids.update(many(row.get("pdIds"))); codes.update(many(row.get("pdCodes")))
        for column in row.get("extColumns", []):
            if column.get("ext_col") == "ext_2412110257" or column.get("ext_key") == "活动SKU":
                codes.update(many(column.get("ext_value_code")))
        if str(row.get("pdRangeType", "")) == "0":
            used_plans.add(ident(app.get("planCode")))
    # Plans are already fetched only for the exact linked application rows.
    for plan in response_rows(raw.get("queryRegularSale", [])):
        if ident(plan.get("code")) in used_plans:
            for product in plan.get("product_list", []):
                if ident(product.get("id")): ids.add(ident(product["id"]))
                if ident(product.get("code")): codes.add(ident(product["code"]))
    return ids, codes


def matches(row, ids, codes, id_key, code_key):
    rid, code = ident(row.get(id_key)), ident(row.get(code_key))
    return bool((rid and rid in ids) or (code and code in codes))


def windows(linked):
    result = set()
    for app, row in linked:
        start, end = str(row.get("startDate") or app.get("startDate") or "")[:10], str(row.get("endDate") or app.get("endDate") or "")[:10]
        try:
            if date.fromisoformat(start) <= date.fromisoformat(end):
                result.add((start, end))
        except ValueError:
            continue
    merged = []
    for start, end in sorted(result):
        if merged and date.fromisoformat(start).toordinal() <= date.fromisoformat(merged[-1][1]).toordinal() + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


class LiveReferences(dict):
    """Only live responses may retrieve remote photos; offline JSON stays offline."""
    def __init__(self, raw, api_host=None):
        super().__init__(raw)
        self.photo_hashes = {}
        self.api_hostname = urlsplit(api_host).hostname if isinstance(api_host, str) else None


def eligible_links(raw, verify_code, linked):
    details = [d for v in response_rows(raw.get("queryVerificationRegular", []))
               if ident(v.get("verify_code")) == verify_code for d in v.get("details", [])]
    result = []
    for app, row in linked:
        related = [d for d in details if ident(d.get("activity_code")) == ident(app.get("applyCode"))
                   and ident(d.get("activity_cm_id")) == ident(row.get("subId"))]
        conflicts = [d for d in related if any(ident(d.get(left)) and ident(row.get(right)) and
                     ident(d[left]) != ident(row[right]) for left, right in
                     (("customer_id", "cusId"), ("customer_code", "cusCode")))]
        if conflicts:
            record_step("qince.reference_conflict", "本单核销客户与关联申请客户不一致", {
                "verify_code": verify_code, "applyCode": app.get("applyCode"), "subId": row.get("subId"),
                "reason": "冲突的客户关联不用于补取终端、商品或照片参照"}, status="failed")
        elif related:
            result.append((app, row))
    return result


def reference_needs(scenario):
    """Explicit auxiliary consumers; an unspecified purpose performs no query.

    Application dealer/period/amount fields come from the two linked core
    queries. POS identity uses the configured product database, not a second
    Qince master lookup. Visual store checks alone may use terminal identity;
    display size estimation alone may use product packaging/dimensions.
    """
    return frozenset({
        'promotional_display': {'terminals', 'products', 'dimensions'},
        'price_difference_support': {'terminals'},
        'giveaway_promotion': {'terminals'},
        'self_procured_gift_material': {'terminals'},
        'entry_fee': {'terminals'},
    }.get(scenario, ()))


def fetch_references(client, raw, verify_code, linked, *, needs=None):
    needs = reference_needs(None) if needs is None else frozenset(needs)
    if not needs <= {'terminals', 'products', 'dimensions', 'historical_photos'}:
        raise AuditError('勤策补充取数用途未登记。')
    result = LiveReferences(raw, client.host)
    # Appending refreshed rows must not mutate an already provided snapshot.
    for method in REFERENCE_ENDPOINTS:
        if isinstance(result.get(method), list):
            result[method] = list(result[method])
    linked = eligible_links(raw, verify_code, linked)
    if not linked:
        return result
    statuses = []
    def query(method, params, predicate=lambda row: True):
        try:
            rows = client.pages(method, params)
            selected = [row for row in rows if predicate(row)]
            result.setdefault(method, []).extend(selected)
            statuses.append({"method": method, "status": "available" if selected else "empty_or_unlinked", "selected": len(selected)})
            return selected
        except AuditError as exc:
            statuses.append({"method": method, "status": "query_failed", "code": getattr(exc, "qince_code", None)})
            record_step("qince.reference_unavailable", "勤策补充参照未取得", statuses[-1], status="failed")
            return []

    # Query by a stable ID/code, never by a name or a guessed terminal list.
    customers = {(ident(r.get("cusId")), ident(r.get("cusCode"))) for _, r in linked}
    for cid, code in sorted(customers) if 'terminals' in needs else []:
        if not (cid or code): continue
        params = {"id": cid} if cid else {"cm_code": code}
        params.update(sorts=[{"field_key": "create_time", "type": "desc"}], query_group=[])
        query(CUSTOMERS, params, lambda r, cid=cid, code=code:
              (not cid or ident(r.get("id")) == cid) and (not code or ident(r.get("cm_code")) == code))
    ids, codes = product_scope(linked, raw)
    for code in sorted(codes) if 'products' in needs else []:
        query(PRODUCTS, {"prd_code": code}, lambda r, code=code: ident(r.get("prd_code")) == code)
    # IDs are queried as Qince IDs, not as third-party prd_id values.
    fetched_ids = {ident(r.get("prd_waiqin365_id")) for r in result.get(PRODUCTS, [])}
    for pid in sorted(ids - fetched_ids) if 'products' in needs else []:
        query(PRODUCTS, {"prd_waiqin365_id": pid}, lambda r, pid=pid: ident(r.get("prd_waiqin365_id")) == pid)
    if 'dimensions' in needs and (ids or codes):
        query(SKU_CODES, {}, lambda r: matches(r, ids, codes, "productId", "productCode"))
    customer_codes = {code for _, code in customers if code}
    customer_ids = {cid for cid, _ in customers if cid}
    # Compatibility reader remains available for explicitly supplied historical
    # snapshots. Normal runs do not query these optional report families.
    for start, end in windows(linked) if 'historical_photos' in needs else []:
        params = {"startDate": start, "endDate": end}
        reports = query(PRIMITIVE_REPORT, params, lambda r: ident(r.get("customCode")) in customer_codes)
        biz_ids = {ident(r.get("bizId")) for r in reports if ident(r.get("bizId"))}
        if biz_ids:
            for method in (PRIMITIVE_DETAIL, CHECKED_DETAIL):
                for biz in sorted(biz_ids):
                    query(method, {**params, "bizId": biz}, lambda r, biz=biz: ident(r.get("bizId")) == biz)
            query(CHECKED_REPORT, params, lambda r: ident(r.get("bizId")) in biz_ids)
            times = {"startTime": start + " 00:00:00", "endTime": end + " 23:59:59"}
            relations = query(PICTURES, times, lambda r: ident(r.get("bizId")) in biz_ids)
            image_ids = {i for r in relations for i in many(r.get("imageIds"))}
            if image_ids:
                query(ORIGINAL, times, lambda r: ident(r.get("imageId")) in image_ids)
            for biz in sorted(biz_ids):
                query(COMPARE, {**times, "bizId": biz}, lambda r, biz=biz:
                      ident(r.get("bizId")) == biz or any(ident(p.get("bizId")) == biz for p in r.get("productDetailList", [])))
        # Creation time here is the submission time, never the standard's validity.
        actual_params = {"start_create_date": start, "end_create_date": end}
        for code in sorted(customer_codes):
            query(ACTUAL, {**actual_params, "cm_code": code}, lambda r, code=code:
                  ident(r.get("cm_code")) == code and matches(r, ids, codes, "product_id", "product_code"))
        query(RESULTS, actual_params, lambda r:
              matches(r, customer_ids, customer_codes, "cm_id", "cm_code"))
    if 'historical_photos' in needs and result.get(RESULTS):
        standard_ids = {ident(r.get("setting_id")) for r in result[RESULTS]}
        # The standard query has no documented ID filter. Filter its actual ID
        # locally and keep the original response in the per-page technical trace.
        query(STANDARDS, {}, lambda r: ident(r.get("master_id")) in standard_ids)
    for key in REFERENCE_ENDPOINTS:
        if key in result:
            unique = {json.dumps(row, sort_keys=True, ensure_ascii=False): row for row in result[key]}
            result[key] = list(unique.values())
    result["reference_status"] = statuses
    result["reference_needs"] = sorted(needs)
    record_step("qince.references", "按本类审核用途查询勤策补充参照", {"verify_code": verify_code,
        "reference_needs": sorted(needs), "rows": statuses,
        "query_count": len(statuses), "auxiliary_queries_skipped": not needs,
        "purpose": "实际返回原值保存在逐页记录；照片需核验本次原图，未确认动态Key不猜填"})
    return result


def reference_sources(raw, verify_code, linked):
    """Yield allowlisted facts with their exact application-row provenance."""
    linked = eligible_links(raw, verify_code, linked)
    if not linked: return
    customer_ids = {ident(r.get("cusId")) for _, r in linked} - {""}
    customer_codes = {ident(r.get("cusCode")) for _, r in linked} - {""}
    ids, codes = product_scope(linked, raw)
    links = [{"applyCode": a["applyCode"], "subId": ident(r.get("subId")), "cusId": ident(r.get("cusId")),
              "cusCode": ident(r.get("cusCode"))} for a, r in linked]
    def records(method):
        return reference_rows(raw, method)
    def source(method, row, fields, label, limitation, row_links=links):
        return label, REFERENCE_ENDPOINTS[method], {"verify_code": verify_code, "method": method,
            "application_rows": row_links, "fields": safe_value(fields)}, [limitation]
    for method in (CUSTOMERS, CUSTOMERS_V1):
        for row in records(method):
            matching = [link for link in links if
                        (not link["cusId"] or ident(row.get("id")) == link["cusId"]) and
                        (not link["cusCode"] or ident(row.get("cm_code")) == link["cusCode"]) and
                        (link["cusId"] or link["cusCode"])]
            if not matching: continue
            fields = pick(row, TERMINAL_FIELDS)
            fields["deliverys"] = [pick(r, "cm_delivery_waiqin365_id cm_delivery_addr cm_delivery_main".split()) for r in row.get("deliverys", [])]
            fields["cm_receive_info"] = [pick(r, "cm_waiqin_365_id cm_code cm_is_default".split()) for r in row.get("cm_receive_info", [])]
            yield source(method, row, fields, "勤策终端主档", "当前主档版本，不自动证明活动发生时的名称、地址或现场执行；联系人及未映射自定义值仅在原始技术记录查证。", matching)
    for row in records(PRODUCTS):
        if not matches(row, ids, codes, "prd_waiqin365_id", "prd_code"): continue
        fields = pick(row, PRODUCT_FIELDS)
        fields["specs"] = [pick(r, ["spec_name", "spec_value"]) for r in row.get("specs", [])]
        fields["prd_units"] = [pick(r, "prd_unit_waiqin365_id prd_unit_name prd_barcode prd_is_base prd_ratio".split()) for r in row.get("prd_units", [])]
        yield source(PRODUCTS, row, fields, "勤策商品包装参照", "商品库身份与本次原图仍须实际核对；图片URL只是参照定位，未下载不能称已查看。")
    for row in records(SKU_CODES):
        if matches(row, ids, codes, "productId", "productCode"):
            yield source(SKU_CODES, row, pick(row, "productId productCode productName skuCode unitId unitName bjpType lengthWidthAndHeight".split()),
                         "勤策商品识别码与尺寸", "长宽高原值单位为厘米，须对应商品及unitId；竞品无单位或尺寸时不推算。")
    # Candidate results cannot be asserted to belong to this photo yet.


def photo_digest(raw, url):
    """Bounded original-photo reads from Qince image domains, with no redirects."""
    if not isinstance(raw, LiveReferences) or not isinstance(url, str): return None
    if url in raw.photo_hashes: return raw.photo_hashes[url]
    if len(raw.photo_hashes) >= 200: return None
    raw.photo_hashes[url] = None
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    configured = {h.strip().lower() for h in os.environ.get("OFFLINE_AUDIT_QINCE_IMAGE_HOSTS", "").split(",") if h.strip()}
    try:
        ipaddress.ip_address(host)
        return None
    except ValueError:
        pass
    same_api_host = bool(raw.api_hostname and host == raw.api_hostname)
    if (parsed.scheme != "https" or parsed.username or parsed.password
            or (not same_api_host and port not in (None, 443))
            or not (same_api_host or host.endswith(".waiqin365.com") or host.endswith(".qince.com") or host in configured)):
        record_step("qince.photo_unlinked", "勤策原图地址尚未通过读取范围校验", {"reason": "仅允许HTTPS同企业接口主机、勤策图片域名或明确配置的图片主机；未按文件名关联"}, status="skipped")
        return None
    try:
        digest, size = hashlib.sha256(), 0
        with httpx.Client(timeout=15, trust_env=False, follow_redirects=False) as client:
            with client.stream("GET", url) as response:
                response.raise_for_status()
                if response.status_code != 200 or not response.headers.get("content-type", "").startswith("image/"):
                    return None
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > 16 * 1024 * 1024: return None
                    digest.update(chunk)
        if size:
            raw.photo_hashes[url] = digest.hexdigest()
    except (httpx.HTTPError, ValueError):
        pass
    record_step("qince.photo_fingerprint", "读取勤策原图并计算内容指纹", {"url": url,
        "sha256": raw.photo_hashes[url], "purpose": "只用于与本次原图逐字节对应，不复用旧审核结论"},
        status="completed" if raw.photo_hashes[url] else "failed")
    return raw.photo_hashes[url]


def photo_reference_sources(raw, verify_code, archive_id, units):
    """Attach existing recognition values only after original bytes match a unit."""
    from pathlib import Path
    from .qince_context import _linked_applications, _rows, APPLICATIONS, VERIFICATION
    tables = {}
    def rows(method):
        if method not in tables:
            tables[method] = reference_rows(raw, method)
        return tables[method]
    verifications = [r for r in _rows(raw.get(VERIFICATION)) if ident(r.get("verify_code")) == verify_code]
    linked = _linked_applications(verifications, _rows(raw.get(APPLICATIONS)))
    linked = eligible_links(raw, verify_code, linked)
    codes = {ident(r.get("cusCode")) for _, r in linked} - {""}
    reports = [r for r in rows(PRIMITIVE_REPORT) if ident(r.get("customCode")) in codes]
    if not reports: return [], []
    unit_hashes = {}
    for unit in units:
        if unit.get("image"):
            path = Path(unit["image"])
            if path.is_file():
                unit_hashes.setdefault(hashlib.sha256(path.read_bytes()).hexdigest(), []).append(unit["unit_id"])
    result_units, documents, records = [], [], []
    for report in reports:
        biz = ident(report.get("bizId"))
        details = [r for r in rows(PRIMITIVE_DETAIL) if ident(r.get("bizId")) == biz]
        images = [image for row in details for image in row.get("images", [])]
        # A report-level composite picture is never assumed to be an imageId.
        for image in images:
            fingerprint = photo_digest(raw, image.get("imageUrl"))
            matched = unit_hashes.get(fingerprint, [])
            if not matched: continue
            image_id = ident(image.get("imageId"))
            original = [{**pick(r, IMAGE_FIELDS), "products": [pick(p, DETECTED_FIELDS) for p in r.get("products", [])]}
                        for r in rows(ORIGINAL) if ident(r.get("imageId")) == image_id]
            checked = [r for r in rows(CHECKED_DETAIL) if ident(r.get("bizId")) == biz and
                       any(ident(i.get("imageId")) == image_id for i in r.get("images", []))]
            comparisons = [r for r in rows(COMPARE) if ident(r.get("bizId")) == biz or
                           any(ident(p.get("bizId")) == biz for p in r.get("productDetailList", []))]
            fields = {"verify_code": verify_code, "bizId": biz, "imageId": image_id, "photo_unit_ids": matched,
                "application_rows": [{"applyCode": a.get("applyCode"), "subId": ident(r.get("subId")),
                    "cusId": ident(r.get("cusId")), "cusCode": ident(r.get("cusCode"))} for a, r in linked
                    if ident(r.get("cusCode")) == ident(report.get("customCode"))],
                "sha256": fingerprint, "report": pick(report, REPORT_FIELDS),
                "business_details": [detail_fields(r) for r in details],
                "original_photo": original, "checked_details": [detail_fields(r) for r in checked],
                "checked_reports": [pick(r, REPORT_FIELDS) for r in rows(CHECKED_REPORT) if ident(r.get("bizId")) == biz],
                "facing_comparisons": [{**pick(r, ["bizId"]), "productDetailList": [
                    pick(p, "bizId productId originalCount totalCount changeType".split()) for p in r.get("productDetailList", [])
                    if ident(p.get("bizId")) == biz]} for r in comparisons],
                "reference_only": True, "contract_requirement_confirmed": False}
            actual = [r for r in rows(ACTUAL) if biz in many(r.get("biz_id")) and ident(r.get("cm_code")) == ident(report.get("customCode"))]
            fields["actual_reports"] = [pick(r, "id master_id cm_id cm_code product_id product_code num row_num pic biz_id data_id data_type is_add is_update".split()) for r in actual]
            master_ids = {ident(r.get("master_id")) for r in actual}
            comparisons = [r for r in rows(RESULTS) if ident(r.get("report_id")) in master_ids and ident(r.get("cm_code")) == ident(report.get("customCode"))]
            fields["distribution_comparisons"] = [pick(r, "setting_id compare_id report_id cm_id cm_code must_products groups must_total_row_num must_actual_total_row_num".split()) for r in comparisons]
            standard_ids = {ident(r.get("setting_id")) for r in comparisons}
            fields["distribution_standards"] = [pick(r, "master_id cus_range_type cus_range_id start_date end_date must_products groups".split()) for r in rows(STANDARDS) if ident(r.get("master_id")) in standard_ids]
            records.append(safe_value(lossless_ids(fields)))
            # Technical originals retain the exact per-image operation. Model
            # delivery below shares identical business records within this run.
            record_step("qince.photo_linked", "勤策识别记录已与本次原图内容对应", fields)
    common_keys = ("application_rows", "report", "business_details", "checked_reports", "facing_comparisons",
                   "reference_only", "contract_requirement_confirmed", "actual_reports",
                   "distribution_comparisons", "distribution_standards")
    groups = {}
    for fields in records:
        key = json.dumps({"verify_code": fields["verify_code"], "bizId": fields["bizId"],
                          **{k: fields[k] for k in common_keys}}, sort_keys=True, ensure_ascii=False)
        groups.setdefault(key, []).append(fields)

    def emit(fields, *, shared=False):
        uid = f"{archive_id}-qince-{'biz' if shared else 'photo'}-{len(result_units)+1:04d}"
        facts = ["qince_photo_reference=" + json.dumps(fields, ensure_ascii=False, separators=(",", ":"))]
        limitations = ["原图内容指纹一致，仅证明图片来源对应。业务级数量不能分摊为逐图数量；层号、排面和件数不等于纵列；仍按本单标准复核当前照片。铺货标准另须核对客户范围及有效期，不能自动成为合同要求。"]
        if fields.get("business_reference_unit_id"):
            limitations.append("公共业务原值完整保存在business_reference_unit_id指定的本次独立来源；本图核对须联合该来源，不按引用缺省猜值。")
        result_units.append({"unit_id": uid, "file_id": uid,
            "source_file": "勤策本图共用业务参照" if shared else "勤策本图识别与校对参照",
            "locator": f"业务{fields['bizId']}" + (" · 公共业务原值" if shared else f" · 照片{fields['imageId']}"),
            "image": None, "native_facts": facts, "limitations": limitations,
            "source_kind": "qince", "qince_reference": True})
        documents.append({"unit_id": uid, "roles": ["勤策本次业务补充明细（独立来源）"], "facts": facts, "limitations": limitations})
        return uid

    for group in groups.values():
        if len(group) == 1:
            emit(group[0])
            continue
        keys = list(common_keys)
        if all(fields["checked_details"] == group[0]["checked_details"] for fields in group):
            keys.append("checked_details")
        shared = {"verify_code": group[0]["verify_code"], "bizId": group[0]["bizId"],
                  **{key: group[0][key] for key in keys}}
        shared_uid = emit(shared, shared=True)
        for fields in group:
            emit({**{key: value for key, value in fields.items() if key not in keys},
                  "business_reference_unit_id": shared_uid})
    return result_units, documents
