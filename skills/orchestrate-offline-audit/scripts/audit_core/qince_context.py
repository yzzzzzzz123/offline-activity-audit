"""Read only this verification's Qince details; never infer business standards.

The three query interfaces and v2 signature follow Qince's public documentation.
Credentials stay in environment/private files. Only selected, linked facts enter
the audit, separately from ZIP documents and observed photograph contents.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from .common import AuditError
from .private_credentials import environment_secret, read_private_file
from .paths import PROJECT_ROOT
from .qince_references import (REFERENCE_ENDPOINTS, REFERENCE_POLICY, page_spec,
                               response_rows, fetch_references, reference_sources)


VERIFICATION = "queryVerificationRegular"
APPLICATIONS = "queryRegularSaleActivities"
PLANS = "queryRegularSale"
ENDPOINTS = {
    VERIFICATION: "/api/cuxiao/verify/v1/" + VERIFICATION,
    APPLICATIONS: "/api/cuxiao/v1/" + APPLICATIONS,
    PLANS: "/api/cuxiao/v1/" + PLANS,
    **REFERENCE_ENDPOINTS,
}
MAX_BYTES = 8 * 1024 * 1024
# A 300-row detail page can take far longer than 30s to read; keep one
# bounded retry so a transient stall is visible instead of silently fatal.
QINCE_TIMEOUT = httpx.Timeout(90.0, connect=15.0)
POLICY = (
    "用户2026-11-03确认：本次核销关联的勤策核销明细、申请明细及方案可补充合同引用的终端、商品等明细。"
    "先结合本次合同原文确认引用及活动对应关系，再使用独立列出的勤策来源；已取得对应明细时，不能仅因ZIP没有单独附件就说附件未提供。"
    "合同图片未展示所引用的附件，不等于本次关联明细未提供；本单已取得精确关联的勤策终端、商品或核销明细时，应逐项使用其中实际字段核对，不能仅以合同图未列附件为由报告缺件或疑点。"
    "核对时具体引用本次合同、对应勤策核销行/申请行/方案以及相关照片的unit_id，说明哪些明细对应本次哪项约定；不能借用其他核销单、其他申请行或未关联方案补齐。"
    "核销行及实际客户是申报信息，不能自证合同约定；申请行的客户也不自动等于终端门店，参与客户数不等于门店名单。"
    "核销行的customer_id/customer_code/customer_name为本单客户身份，须通过activity_code/activity_cm_id精确关联申请行subId，"
    "再核对申请cusId/cusCode/cusName；相同客户在多个SKU行重复出现只计一次，不能将SKU行数当门店数。"
    "结合本次资料确认这些客户确为执行终端后，可使用其实际门店名单；只有客户或连锁品牌层级、关联缺失或冲突时不能当完整终端清单。"
    "主档、商品范围和计划不证明实际执行，不能补造照片水印、品牌、大小或POS字段；合同与明细冲突须如实说明。"
    "勤策中的“1平堆”等陈列类型或规格只表示本次约定，不能证明照片实际面积已达到；仍须根据对应照片的可辨认事实按本类原标准核对。"
    "明细确实未取得、关联不成立、核对所需字段缺失或来源冲突时，具体说明缺少哪项参照、哪两个来源不能对应或何处冲突，不猜通过，也不笼统重复要求补附件。"
    "没有提供或没有查到补充明细时沿用本次实际材料，不新增必交附件、字段、审批前置、状态审核、接口或其他审核项。"
    "每张照片继续完整分析，仅执行本类型原有审核；引用补充明细时同时保留本次合同及对应照片来源。"
)


# The current workbook explicitly compares system application and approval data.
from .fee_policy_source import WORKBOOK_CLARIFICATION
from .reference_matching import TEXT_MATCH_POLICY
POLICY = WORKBOOK_CLARIFICATION + TEXT_MATCH_POLICY + (
    " 本次勤策来源仍仅限verifyCode及申请行精确关联，申请行的客户也不自动等于终端门店。"
    "已取得对应明细，不能仅以合同图未列附件为由报告缺件或疑点；引用对应勤策核销行/申请行/方案以及相关照片的unit_id，"
    "不能借用其他核销单、其他申请行或未关联方案补齐。勤策空字段或尚未形成的批复只留技术记录，不列客户问题；实际有值漏用须重新核对，真实来源冲突须具体说明。"
    "申请中的1平堆等规格只表示本次约定，不能证明照片实际面积已达到；实际现场及门店范围仍以本次可靠事实核对。") + REFERENCE_POLICY
BUSINESS_COLUMNS = frozenset({"活动SKU","陈列、堆头类型","费用代垫经销商","活动执行开始日期","活动执行结束日期",
    "数量","单价","条形码","申请总金额","活动申请总金额","批复金额合计","奖励规则","赠品方案","搭赠规则"})
def business_columns(record):
    return [{k: column[k] for k in ("ext_key","ext_col","ext_value","ext_value_code","ext_value_name") if k in column}
        for column in record.get("extColumns",[]) if (column.get("ext_key") in BUSINESS_COLUMNS
        or column.get("ext_col") in {"ext_2412110257", "ext_2412110258"})
        ]


def _id(value) -> str:
    if value is None or isinstance(value, bool):
        return ""
    return str(value).strip()


def _rows(value) -> list[dict]:
    if isinstance(value, dict) and "return_code" in value:
        if str(value["return_code"]) != "0":
            raise AuditError("勤策查询未成功；未把接口错误当成客户缺资料。")
        value = value.get("response_data")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, TypeError):
            raise AuditError("勤策查询数据不是有效JSON；未输出原始响应。") from None
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise AuditError("勤策查询数据必须为明细列表；未输出原始响应。")
    return value


class QinceClient:
    def __init__(self, openid: str, appkey: str, host: str = "https://openapi.qince.com"):
        if not re.fullmatch(r"[0-9]{19}", openid) or not appkey:
            raise AuditError("勤策调用配置不完整，请核对企业OpenID及私有Appkey。")
        self.openid, self._appkey = openid, appkey
        if not isinstance(host, str):
            raise AuditError("勤策接口host须为企业HTTPS来源地址。")
        parsed = urlsplit(host)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")):
            raise AuditError("勤策接口host须为企业HTTPS来源地址。")
        self.host = host.rstrip("/")

    @classmethod
    def from_environment(cls):
        openid = environment_secret("OFFLINE_AUDIT_QINCE_OPENID")
        appkey = environment_secret("OFFLINE_AUDIT_QINCE_APPKEY")
        host = os.environ.get("OFFLINE_AUDIT_QINCE_HOST", "https://openapi.qince.com")
        if not openid and not appkey:
            configured_path = os.environ.get("OFFLINE_AUDIT_QINCE_CREDENTIALS_FILE")
            path = Path(configured_path) if configured_path else PROJECT_ROOT / "config/qince.json"
            if not configured_path and not path.is_file():
                return None
            try:
                config = json.loads(read_private_file(path))
                openid, appkey = config.get("openid", ""), config.get("appkey", "")
                host = config.get("host", host)
                if not isinstance(openid, str) or not isinstance(appkey, str):
                    raise ValueError()
            except (ValueError, AttributeError):
                raise AuditError("勤策私有配置格式无效；未输出凭据。") from None
            if not openid and not appkey:
                return None
        return cls(openid, appkey, host)

    def query(self, method: str, parameters: dict) -> list[dict]:
        from .execution_trace import record_step
        trace_id = uuid.uuid4().hex
        record_step("qince.request", "查询勤策：" + method, {"http_method": "POST", "endpoint": ENDPOINTS[method],
                    "source": {"name": "勤策开放接口", "host": self.host},
                    "business_parameters": parameters, "purpose": "取得本核销单关联的业务资料及只读复核参照"}, status="started", call_id=trace_id)
        body = json.dumps(parameters, ensure_ascii=False, separators=(",", ":"))
        timestamp, nonce, msg_id = str(int(time.time() * 1000)), secrets.token_hex(3), uuid.uuid4().hex
        digest = hashlib.sha256(
            f"data={body}&appkey={self._appkey}&nonce={nonce}&timestamp={timestamp}".encode("utf-8")
        ).hexdigest()
        url = f"{self.host}{ENDPOINTS[method]}/{self.openid}/{timestamp}/{nonce}/{msg_id}"
        def failure(kind: str, detail: str, exc: Exception | None = None):
            record_step("qince.failed", "勤策接口连接或响应失败",
                        {"method": method, "business_parameters": parameters, "attempt": attempt,
                         "error_kind": kind, "error_detail": detail}, status="failed", call_id=trace_id)
            message = {"timeout": "勤策查询超时",
                       "network": "勤策查询网络异常",
                       "http_status": "勤策查询返回异常HTTP状态",
                       "invalid_json": "勤策查询响应不是有效JSON"}.get(
                           kind, "勤策查询连接或响应异常")
            raise AuditError(f"{message}（{detail}）；未输出地址、签名或原始响应。") from exc

        for attempt in (1, 2):
            try:
                with httpx.Client(timeout=QINCE_TIMEOUT, trust_env=False, follow_redirects=False) as client:
                    with client.stream("POST", url, content=body.encode("utf-8"), headers={
                        "Content-Type": "application/json; charset=utf-8", "data-format": "json",
                        "encrypt-version": "v2", "digest": digest,
                    }) as response:
                        response.raise_for_status()
                        chunks, size = [], 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise AuditError("勤策响应过大，已停止读取。")
                            chunks.append(chunk)
                envelope = json.loads(b"".join(chunks), parse_float=str)
                break
            except httpx.HTTPStatusError as exc:
                status_code = exc.response.status_code
                if status_code < 500 or attempt == 2:
                    failure("http_status", f"HTTP {status_code}", exc)
            except httpx.TimeoutException as exc:
                if attempt == 2:
                    failure("timeout", type(exc).__name__, exc)
            except httpx.HTTPError as exc:
                if attempt == 2:
                    failure("network", type(exc).__name__, exc)
            except ValueError as exc:
                if attempt == 2:
                    failure("invalid_json", type(exc).__name__, exc)
        if not isinstance(envelope, dict) or "return_code" not in envelope:
            raise AuditError("勤策查询响应缺少结果码。")
        try:
            rows = response_rows(envelope) if method in REFERENCE_ENDPOINTS else _rows(envelope)
        except AuditError:
            record_step("qince.failed", "勤策接口结果码或明细格式未通过校验", {"method": method}, status="failed", call_id=trace_id)
            raise
        record_step("qince.response", "勤策返回：" + method, {"endpoint": ENDPOINTS[method], "business_parameters": parameters,
                    "row_count": len(rows), "rows": rows}, call_id=trace_id)
        return rows

    def pages(self, method: str, parameters: dict) -> list[dict]:
        if method in REFERENCE_ENDPOINTS:
            page_key, size_key, page_size = page_spec(method)
            if page_key is None:
                return self.query(method, parameters)
        else:
            page_key, size_key = ("page", "rows") if method == VERIFICATION else ("page_number", "page_size")
            page_size = 100
        rows, previous, expected_total = [], set(), None
        for page in range(1, 1001):
            pagination = {page_key: str(page)}
            if size_key:
                pagination[size_key] = str(page_size)
            batch = self.query(method, {**parameters, **pagination})
            total = getattr(batch, "total", None)
            if total is not None:
                if expected_total is not None and total != expected_total:
                    raise AuditError("勤策分页总数发生变化，无法确认明细完整性。")
                expected_total = total
            if not batch:
                if getattr(batch, "total", None) is not None and len(rows) != batch.total:
                    raise AuditError("勤策分页总数不一致，无法确认明细完整性。")
                return rows
            fingerprint = hashlib.sha256(json.dumps(batch, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            if fingerprint in previous:
                raise AuditError("勤策分页重复，无法确认明细完整性。")
            previous.add(fingerprint)
            rows.extend(batch)
            if total is not None and len(rows) > total:
                raise AuditError("勤策分页总数不一致，无法确认明细完整性。")
            if total is not None and len(rows) == total:
                return rows
            if len(batch) < page_size:
                if total is not None and len(rows) != total:
                    raise AuditError("勤策分页尚未完整返回，无法确认明细完整性。")
                return rows
        raise AuditError("勤策分页未结束，无法确认明细完整性。")

    def fetch(self, verify_code: str, *, include_references: bool = False, scenario: str | None = None) -> dict:
        verifications = [row for row in self.pages(VERIFICATION, {"verify_code": verify_code})
                         if _id(row.get("verify_code")) == verify_code]
        codes = sorted({_id(detail.get("activity_code")) for row in verifications
                        for detail in row.get("details", []) if _id(detail.get("activity_code"))})
        applications = []
        for code in codes:
            applications.extend(self.pages(APPLICATIONS, {"activity_code": code}))
        # Query a scheme only when the linked application inherits its products.
        linked = _linked_applications(verifications, applications)
        plans = []
        plan_codes = {_id(app.get("planCode")) for app, row in linked
                      if str(row.get("pdRangeType", "")) == "0" and _id(app.get("planCode"))}
        for code in sorted(plan_codes):
            plans.extend(self.pages(PLANS, {"plan_code": code}))
        from .execution_trace import record_step
        record_step("qince.linked", "勤策明细已按本单核销号与申请行精确关联", {"verify_code": verify_code,
            "join_fields": ["核销details.activity_code = 申请applyCode", "核销details.activity_cm_id = 申请subList.subId"],
            "verification_count": len(verifications), "linked_application_rows": [{"application_code": a.get("applyCode"), "row": r} for a,r in linked],
            "queried_plan_codes": sorted(plan_codes), "plan_count": len(plans)})
        result = {VERIFICATION: verifications, APPLICATIONS: applications, PLANS: plans}
        if include_references:
            from .qince_references import reference_needs
            return fetch_references(self, result, verify_code, linked, needs=reference_needs(scenario))
        return result


def _linked_applications(verifications: list[dict], applications: list[dict]) -> list[tuple[dict, dict]]:
    links = {(_id(detail.get("activity_code")), _id(detail.get("activity_cm_id")))
             for verification in verifications for detail in verification.get("details", [])
             if _id(detail.get("activity_code")) and _id(detail.get("activity_cm_id"))}
    result, seen = [], set()
    for application in applications:
        for row in application.get("subList", []):
            link = (_id(application.get("applyCode")), _id(row.get("subId")))
            if link not in links:
                continue
            if link in seen:
                # Duplicate/conflicting rows must not silently overwrite one another.
                previous = next(r for a, r in result if (_id(a.get("applyCode")), _id(r.get("subId"))) == link)
                if previous != row:
                    raise AuditError("勤策同一申请行返回冲突明细，无法确认本次关联。")
                continue
            seen.add(link)
            result.append((application, row))
    return result


def entry_context_sources(raw: dict, verify_code: str, archive_id: str) -> tuple[list[dict], list[dict]]:
    """Group the entry scope by customer without duplicating store/SKU prose.

    These remain linked customer facts, not an assertion of terminal status,
    signatures or execution. Every grouped row retains both original row IDs.
    Incomplete linkage falls back to the ordinary independent source records.
    """
    ordinary = context_sources(raw, verify_code, archive_id)
    verifications = [v for v in _rows(raw.get(VERIFICATION)) if _id(v.get("verify_code")) == verify_code]
    linked = _linked_applications(verifications, _rows(raw.get(APPLICATIONS)))
    applications = {(_id(a.get("applyCode")), _id(r.get("subId"))): r for a, r in linked}
    groups = {}
    for verification in verifications:
        for detail in verification.get("details", []):
            key = (_id(detail.get("activity_code")), _id(detail.get("activity_cm_id")))
            row = applications.get(key)
            customer_id = _id(detail.get("customer_id"))
            if (not row or not customer_id or customer_id != _id(row.get("cusId"))
                    or not _id(detail.get("customer_name")) or not _id(row.get("cusName"))
                    or _id(detail.get("customer_name")) != _id(row.get("cusName"))):
                return ordinary
            selected = {k: row[k] for k in ("pdCodes", "pdNames", "pdRangeType", "startDate", "endDate",
                                           "cmReceiveAddrName", "cmTypeNames", "exeRequire", "remark", "barcode")
                        if row.get(k) not in (None, "", []) and str(row[k]).strip()}
            sku = [{k: c[k] for k in ("ext_key", "ext_value", "ext_value_name", "ext_value_code") if k in c}
                   for c in row.get("extColumns", []) if c.get("ext_key") in {"活动SKU", "条形码", "活动执行开始日期", "活动执行结束日期"}
                   and any(c.get(k) for k in ("ext_value", "ext_value_name", "ext_value_code"))]
            if sku:
                selected["活动SKU"] = sku
            if not sku and not _id(row.get("pdNames")):
                return ordinary
            group = groups.setdefault(customer_id, {"customer_id": customer_id,
                "customer_name": detail["customer_name"], "linked_application_rows": []})
            group["linked_application_rows"].append({"verification_row_id": _id(detail.get("id")),
                "activity_code": key[0], "activity_cm_id": key[1], **selected})
    if not groups:
        return ordinary
    units, documents = [], []
    for number, group in enumerate(groups.values(), 1):
        uid = f"{archive_id}-qince-entry-{number:04d}"
        facts = [f"verify_code={json.dumps(verify_code, ensure_ascii=False)}",
                 "linked_customer=" + json.dumps(group, ensure_ascii=False, separators=(",", ":"))]
        units.append({"unit_id": uid, "file_id": uid, "source_file": "勤策进场关联明细",
            "locator": f"核销单{verify_code} · 客户{group['customer_name']}", "image": None,
            "native_facts": facts, "limitations": [], "source_kind": "qince"})
        documents.append({"unit_id": uid, "roles": ["勤策本次业务补充明细（独立来源）"],
                          "facts": facts, "limitations": []})
    # Keep the compact entry scope, and retain the new independent references.
    for unit, document in zip(*ordinary):
        if unit.get("qince_reference"):
            units.append(unit)
            documents.append(document)
    return units, documents


def context_sources(raw: dict, verify_code: str, archive_id: str) -> tuple[list[dict], list[dict]]:
    """Project only linked business facts; drop unrelated rows and all secrets."""
    if not isinstance(raw, dict) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,80}", verify_code):
        raise AuditError("勤策补充明细须明确对应本次核销单号。")
    verifications = [r for r in _rows(raw.get(VERIFICATION)) if _id(r.get("verify_code")) == verify_code]
    linked = _linked_applications(verifications, _rows(raw.get(APPLICATIONS)))
    units, documents = [], []

    def add(label: str, locator: str, values: dict):
        facts = [f"{key}={json.dumps(value, ensure_ascii=False, separators=(',', ':'))}"
                 for key, value in values.items()]
        if not facts:
            return
        uid = f"{archive_id}-qince-{len(units)+1:04d}"
        units.append({"unit_id": uid, "file_id": uid, "source_file": label, "locator": locator,
                      "image": None, "native_facts": facts, "limitations": [], "source_kind": "qince"})
        documents.append({"unit_id": uid, "roles": ["勤策本次业务补充明细（独立来源）"],
                          "facts": facts, "limitations": []})

    for verification in verifications:
        for detail in verification.get("details", []):
            keys = ("id", "activity_code", "activity_cm_id", "actual_cm_id", "actual_cm",
                    "customer_id", "customer_code", "customer_name", "execute_date",
                    "updealer_code", "updealer_name", "actual_dealer_code", "actual_dealer_name",
                    "init_amount", "audit_amount")
            add("勤策核销明细", f"核销单{verify_code} · 核销行{_id(detail.get('id'))}",
                {"verify_code": verify_code, **{k: detail[k] for k in keys if k in detail}})
    # Header amounts are emitted once per linked application, never once per SKU.
    seen_applications=set()
    for application,_row in linked:
        code=_id(application.get('applyCode'))
        if code in seen_applications:continue
        seen_applications.add(code)
        header={k:application[k] for k in ('applyCode','startDate','endDate','applyAmount','totalAmount','budgetAmount','advanceDealerName','dealerId','dealerCode','dealerName') if k in application}
        columns=business_columns(application)
        if columns:header['extColumns']=columns
        if len(header)>1:add('勤策申请主档',f'申请单{code} · 主档',{'verify_code':verify_code,**header})
    for verification in verifications:
        if 'verify_status' in verification:
            from .qince_approval_state import SOURCE_LABEL
            workflow = {k: verification[k] for k in
                        ('verify_status', 'reverse_status', 'verify_amount', 'audit_time') if k in verification}
            add(SOURCE_LABEL, f'核销单{verify_code} · 审批状态与当前节点',
                {'verify_code': verify_code, **workflow})
        approval={k:verification[k] for k in ('approvedAmount','approvalAmount','approvedTotalAmount','批复金额合计') if k in verification}
        # The TP header is the latest node total. It is final only after the
        # actual workflow is approved and normal, never while still pending.
        if (str(verification.get('verify_status')) == '1'
                and str(verification.get('reverse_status')) == '0'
                and verification.get('verify_amount') not in (None, '')):
            approval.update({k: verification[k] for k in
                             ('verify_amount', 'verify_status', 'reverse_status', 'audit_time') if k in verification})
        columns=[c for c in business_columns(verification) if c.get('ext_key')=='批复金额合计']
        if columns:approval['extColumns']=columns
        if approval:add('勤策核销批复',f'核销单{verify_code} · 批复',{'verify_code':verify_code,**approval})
    used_plans = set()
    for application, row in linked:
        keys = ("subId", "cusId", "cusCode", "cusName", "cmReceiveAddrName", "cmTypeNames", "startDate", "endDate", "itemName",
                "pdRangeType", "pdIds", "pdCodes", "pdNames", "pdTypesName", "pdBrandsName", "exeRequire", "remark",
                "quantity", "unitPrice", "barcode", "advanceDealerName", "dealerId", "dealerCode", "dealerName", "applyAmount")
        values = {"verify_code": verify_code, "applyCode": application["applyCode"],
                  **({"planCode": application['planCode']} if 'planCode' in application else {}),
                  **{k: row[k] for k in keys if k in row}}
        # Only the workbook's explicitly required business labels are forwarded;
        # phone, banking and unrelated custom fields remain excluded.
        selected = business_columns(row)
        if selected:
            values["extColumns"] = selected
        add("勤策申请明细", f"申请单{application['applyCode']} · 申请行{_id(row.get('subId'))}", values)
        if str(row.get("pdRangeType", "")) == "0":
            used_plans.add(_id(application.get("planCode")))
    for plan in _rows(raw.get(PLANS)):
        if _id(plan.get("code")) not in used_plans:
            continue
        products = [{k: p.get(k) for k in ("id", "code", "name") if p.get(k) is not None}
                    for p in plan.get("product_list", [])]
        add("勤策活动方案", f"方案{plan['code']}", {"code": plan["code"],
            "pd_range_type": plan.get("pd_range_type"),
            **{k: [{field: item.get(field) for field in ("id", "name") if item.get(field) is not None}
                    for item in plan.get(k, [])] for k in ("pd_type_list", "pd_brand_list")},
            "product_list": products})
    references = list(reference_sources(raw, verify_code, linked))
    link_groups = {}
    for _, _, values, _ in references:
        rows = values.get("application_rows")
        if rows:
            key = json.dumps(rows, ensure_ascii=False, separators=(",", ":"))
            link_groups.setdefault(key, []).append(values)
    for key, values_group in link_groups.items():
        if len(values_group) < 2 or len(key.encode("utf-8")) < 400:
            continue
        add("勤策参照申请关联索引", "本单补充参照的公共申请关联表", {
            "verify_code": verify_code, "application_rows": values_group[0]["application_rows"]})
        units[-1]["qince_reference"] = True
        shared_uid = units[-1]["unit_id"]
        for values in values_group:
            # Every original link stays in the public source above. Only the
            # repeated model delivery is replaced by its explicit source ID.
            values.pop("application_rows")
            values["application_reference_unit_id"] = shared_uid
    for label, locator, values, limitations in references:
        before = len(units)
        add(label, locator, values)
        if len(units) > before:
            units[-1]["qince_reference"] = True
            if values.get("application_reference_unit_id"):
                limitations = [*limitations, "application_reference_unit_id指向本单公共申请关联索引的unit_id；全部申请行原值保存在该独立来源，核对时联合引用，不将索引当作照片或执行事实。"]
            units[-1]["limitations"] = limitations
            documents[-1]["limitations"] = limitations
    return units, documents


def load_context(verify_code: str | None, data_path: str | Path | None = None, *, scenario: str | None = None) -> dict | None:
    from .execution_trace import record_step
    if data_path is not None:
        if not verify_code:
            raise AuditError("读取勤策补充明细时请明确本次核销单号；不从文件名推断。")
        path = Path(data_path)
        try:
            if path.stat().st_size > MAX_BYTES:
                raise ValueError()
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            raise AuditError("勤策补充明细文件无法读取或格式无效。") from None
        if not isinstance(value, dict):
            raise AuditError("勤策补充明细必须为JSON对象。")
        record_step("qince.input_file", "读取本次已提供的勤策明细文件", {"file": path.name,
                    "sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "data": value, "http_request_performed": False})
        return value
    if not verify_code:
        record_step("qince.skipped", "本次未查询勤策", {"reason": "未提供本单verifyCode，不根据文件名猜测查询身份"}, status="skipped")
        return None
    client = QinceClient.from_environment()
    if client is None:
        record_step("qince.skipped", "本次未查询勤策", {"reason": "未配置勤策查询连接，沿用本次实际提交材料"}, status="skipped")
        return None
    return client.fetch(verify_code, include_references=True, **({'scenario': scenario} if scenario is not None else {}))
