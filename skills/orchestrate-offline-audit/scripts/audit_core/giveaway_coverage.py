"""Compute missing activity stores from cited scope and photo/receipt facts."""
from __future__ import annotations

import json

from .common import AuditError


def linked_activity_customer_names(units):
    rows = []
    for unit in units or []:
        if unit.get("source_kind") != "qince":
            continue
        values = {}
        for fact in unit.get("native_facts", []):
            key, separator, value = fact.partition("=")
            if separator:
                try:
                    values[key] = json.loads(value)
                except (ValueError, TypeError):
                    continue
        rows.append(values)
    compact = [r["linked_customer"] for r in rows if isinstance(r.get("linked_customer"), dict)]
    if compact:
        identities = {}
        for customer in compact:
            cid, name = customer.get("customer_id"), customer.get("customer_name")
            if not cid or not name or not customer.get("linked_application_rows") or (
                    cid in identities and identities[cid] != name):
                return False
            identities[cid] = name
        return set(identities.values())
    applications = {(str(r.get("applyCode", "")), str(r.get("subId", ""))): r
                    for r in rows if r.get("applyCode") and r.get("subId")}
    verification = [r for r in rows if r.get("activity_code") and r.get("activity_cm_id")]
    if not verification:
        return None
    names, identities = set(), {}
    for row in verification:
        app = applications.get((str(row["activity_code"]), str(row["activity_cm_id"])))
        customer = str(row.get("customer_id", ""))
        name = row.get("customer_name")
        if (not app or not customer or customer != str(app.get("cusId", ""))
                or not name or name != app.get("cusName")
                or (customer in identities and identities[customer] != name)):
            return False
        identities[customer] = name
        names.add(name)
    return names


def giveaway_coverage_check(evidence, documents, materials, units):
    sources = {d["unit_id"]: d for d in documents}
    contracts = {uid for m in materials if m["id"] == "promotion_contract"
                 and m["state"] == "present" for uid in m["source_ids"]}
    linked = {u["unit_id"] for u in units or [] if u.get("source_kind") == "qince"}
    scope = evidence["scope_source_ids"]
    if not set(scope).issubset(contracts | linked) or not set(scope).issubset(sources):
        raise AuditError("搭赠门店范围须引用本次合同及精确关联勤策明细，不能用POS或文件名自证")
    stores = []
    for store in evidence["stores"]:
        name, reference = store["name"], store["source"]
        uid, quote = reference["unit_id"], reference["quote"]
        if (uid not in scope or name in stores or not name.strip() or name != name.strip()
                or name not in quote or quote not in "\n".join(sources[uid]["facts"])):
            raise AuditError("搭赠活动门店须按原文逐店引用并去重，不能编造门店或跨单补齐")
        stores.append(name)
    linked_names = linked_activity_customer_names(units)
    if evidence["scope_complete"] and isinstance(linked_names, set) and set(stores) != linked_names:
        raise AuditError("完整搭赠门店名单须覆盖本单核销行精确关联申请行的全部客户门店；同一客户多个SKU只计一家")
    proof_ids = {uid for m in materials if m["id"] == "giveaway_evidence"
                 and m["state"] in {"present", "unclear"} for uid in m["source_ids"]}
    proofs = evidence["evidence"]
    if {p["unit_id"] for p in proofs} != proof_ids or not proof_ids.issubset(sources):
        raise AuditError("须逐一盘点全部搭赠照片及小票，不能遗漏、抽样或虚构证明")
    covered, unresolved, invalid, seen = set(), [], [], set()
    for proof in proofs:
        uid, name = proof["unit_id"], proof["store_name"]
        key = (uid, name)
        if key in seen:
            raise AuditError("同一搭赠证明与门店对应关系不能重复计数")
        seen.add(key)
        quote = proof["store_quote"]
        if name is not None and (name not in stores or not quote
                or quote not in "\n".join(sources[uid]["facts"])):
            raise AuditError("搭赠证明的门店归属须有本张实际正文依据，不能用文件名或别店证明")
        if name is None:
            unresolved.append(proof["reason"])
        elif proof["valid"]:
            covered.add(name)
        else:
            invalid.append(name + "：" + proof["reason"])
    missing = [name for name in stores if name not in covered]
    complete = evidence["scope_complete"] and bool(stores) and bool(scope) and linked_names is not False
    reason = f"本次已列明{len(stores)}家活动门店，已确认{len(covered)}家具有有效活动照片或小票。"
    if missing:
        reason += ("尚未确认有效证明的门店：" if unresolved or not complete else
                   "缺少有效活动照片或小票的门店：") + "、".join(missing) + "。"
    details = list(dict.fromkeys([*invalid, *unresolved, *evidence["limitations"]]))
    if details:
        reason += "；".join(details)
    if not complete:
        reason += "本次完整终端范围尚未确认，不能把POS出现的其他客户自动补入活动名单。"
    unknown = not complete or bool(unresolved) or bool(evidence["limitations"])
    return {"status": "unknown" if unknown else "fail" if missing or invalid else "pass",
            "reason": reason, "source_ids": list(dict.fromkeys([*scope, *(p["unit_id"] for p in proofs)])),
            "comparisons": [], "coverage": {"scope_complete": complete, "total_stores": len(stores),
                "covered_stores": sorted(covered), "missing_stores": missing,
                "unresolved_evidence": len(unresolved)}}
