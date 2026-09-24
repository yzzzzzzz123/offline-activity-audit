"""Fixed business-type routing and the selected Skill's material gate and audit."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .common import AuditError, BizTypeRecognitionError, now_utc, validate_json
from .customer_language import customer_reason, numeric_problem, plain_text
from .error_reason import attach_error_reasons
from .material_intake import prepare_material_diagnosis
from .pdf_evidence import BIZ_TYPE_SCHEMA, audit_decision, classification_decision, validate, validate_documents
from .pdf_materials import prepare_units
from .pdf_policy import POLICY_VERSION
from .scenario_registry import SCENARIO_LABELS, SCENARIO_ORDER, scenario_for_biz_type
from .workflow import build_audit_chain, invoke_audit_chain


CHECK_CATEGORIES = {
    "settlement_template_seal": "结算单模板与盖章", "contract_signed": "促销合同签章",
    "pos_fields": "销售明细信息", "pos_arithmetic": "销售明细金额及合计",
    "pos_product_identity": "销售明细商品信息与商品库",
    "application_quantity_price": "申请数量与单价", "reward_unit_price": "奖励单价",
    "settlement_pos_quantity": "各商品销售数量", "claim_ceiling": "本次核销金额",
    "display_watermark": "陈列水印", "display_brand": "陈列产品品牌",
    "display_material_brand": "陈列物料上的品牌", "atrium_agreement": "商场入场协议",
    "display_quantity_price": "陈列数量与单价", "display_size": "申请陈列大小",
    "payment_company": "红包公司名称",
    "payment_claim_amount": "红包与核销金额", "staff_daily_photos": "临促每日照片",
    "settlement_seal": "结算单盖章",
    "invoice_details": "发票收据信息", "finished_photo": "物料成品照片",
    "gift_rule_present": "赠送规则", "gift_rule_quantity": "赠品数量",
    "gift_finished_photos": "外采赠品成品照片",
    "price_evidence": "活动价格证明", "full_reduction_evidence": "满减活动证明",
    "giveaway_evidence": "搭赠活动照片或小票", "entry_agreement": "产品推广协议",
    "entry_sku": "进场商品", "entry_watermark": "进场照片水印",
    "entry_duplicate": "不同申报门店的照片地址重复",
}


def _basename(value: str) -> str:
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


def _reason(value: str, units: list[dict]) -> str:
    return plain_text(value, units)


def card_evidence(ids: list[str], units: list[dict], documents: list[dict]) -> dict:
    by_id = {u["unit_id"]: u for u in units}
    facts = {d["unit_id"]: d for d in documents}
    sources = []
    for uid in ids:
        unit = by_id[uid]
        source = unit["source_file"]
        sources.append({"file": source, "original_file": source, "kind": "submitted",
                        "role": "本次核销资料", "locator": unit["locator"],
                        "facts": facts[uid]["facts"]})
    names = list(dict.fromkeys(s["original_file"] for s in sources))
    return {"schema_version": "1.0", "source_files": names, "source_file_count": len(names),
            "sources": sources, "derived_file_count": 0, "references": [],
            "comparisons": [], "limitations": [], "file_count_complete": True}


def build_view(packets: list[dict], rejection: dict | None) -> dict:
    sheets = []
    for packet in packets:
        result = packet.get("result")
        if result is None:
            continue
        scenario = result["scenario"]
        label = SCENARIO_LABELS[scenario]
        rows = []
        for check in result["checks"]:
            if check["status"] == "not_applicable":
                continue
            evidence = card_evidence(check["source_ids"], packet["units"], packet["documents"])
            evidence["comparisons"] = check.get("calculations") or []
            files = "、".join(_basename(f) for f in evidence["source_files"])
            reason = (numeric_problem(check, packet["units"])
                      if check["status"] == "fail" else None) or customer_reason(check, packet["units"])
            if files and not any(_basename(f) in reason for f in evidence["source_files"]):
                reason = f"{files}：{reason}"
            passed = check["status"] == "pass"
            category = CHECK_CATEGORIES[check["rule_id"]]
            heading = category + ("：暂时不能确认" if check["status"] == "unknown" else "：需要处理" if not passed else "：已核对")
            rows.append({"excel_row": len(rows) + 4, "kind": "record", "section": "detail",
                         "status": "pass" if passed else "issue",
                         "confidence": "low" if check["status"] == "unknown" else "high",
                         "heading": heading,
                         "check_category": category,
                         "numeric": bool(check.get("calculations")),
                         "rule_id": check["rule_id"], "card_evidence": evidence,
                         "error_reason": "" if passed else reason,
                         "error_reasons": [] if passed else [reason],
                         "values": [files or "目前没有对应资料可供核对", reason, plain_text(check["requirement"], packet["units"]),
                                    "已核对，符合要求" if passed else "暂时不能确认" if check["status"] == "unknown" else "需要处理",
                                    "",
                                    ""]})
        failures = sum(row["status"] == "issue" for row in rows)
        sheets.append({"name": label + "核销", "title": label + "核销", "scenario": scenario,
                       "projection_kind": "pdf_policy", "audit_type_label": label,
                       "source_archive": packet["source_archive"], "archive_id": packet["archive_id"],
                       "business_decision": result["summary"]["conclusion"], "note": "",
                       "headers": ["资料名称", "查到的情况", "要求", "结果", "按规则处理", "处理方式"],
                       "rows": rows, "audit_counts": {"source_row_count": len(rows), "error_count": failures,
                                                      "detail_error_count": failures, "context_error_count": 0}})
    if rejection:
        for archive in rejection["archives"]:
            packet = next(p for p in packets if p["archive_id"] == archive["archive_id"])
            classification = packet["classification"]
            evidence = card_evidence(classification["source_ids"], packet["units"], packet["documents"])
            reasons = [archive["source_archive"] + "：" + _reason(r, packet["units"])
                       for r in packet["route"]["reasons"]]
            scenario = archive.get("scenario")
            label = SCENARIO_LABELS[scenario] if scenario else "核销资料问题"
            sheets.append({"name": label, "title": label + "资料需要补充或调整", "scenario": scenario,
                           "confirmed_scenario": bool(scenario), "projection_kind": "classification_rejection",
                           "decision_source": rejection["decision_source"], "business_decision": "issues_found",
                           "audit_type_label": label, "source_archive": archive["source_archive"],
                           "note": ("已按业务类型完成资料检查，以下列出需要补充或调整的资料。" if scenario else
                                    "已完成资料检查；以下按各类清单说明资料问题，尚未确定本次核销类型。"),
                           "headers": ["业务文件", "错误原因", "处理方式"],
                           "rows": [{"excel_row": i + 4, "kind": "record", "section": "detail",
                                     "status": "issue", "confidence": "high", "heading": "核销资料需要补充或调整",
                                     "error_reason": reason, "error_reasons": [reason], "card_evidence": evidence,
                                     "values": [archive["source_archive"], reason, ""]}
                                    for i, reason in enumerate(reasons)],
                           "audit_counts": {"source_row_count": len(reasons), "error_count": len(reasons),
                                            "detail_error_count": len(reasons), "context_error_count": 0}})
    view = {"schema_version": "1.1", "title": "费用核销结果", "policy_version": POLICY_VERSION, "sheets": sheets}
    if rejection:
        view["classification_rejection"] = rejection
    return attach_error_reasons(view)


@dataclass
class PdfWorkflow:
    run_id: str
    producer_model: str
    input_dir: str | Path
    temporary_root: Path
    provider: Any
    scenario: str | None
    observer: Any
    biz_type: str | None = None
    large_venue_fee: bool | None = None
    selected_scenario: str | None = None
    intake_case: dict = field(default_factory=dict)
    packets: list[dict] = field(default_factory=list)
    receipt: dict = field(default_factory=dict)
    rejection: dict | None = None

    def emit(self, event: str, **payload):
        if self.observer:
            self.observer(event, payload)

    def intake(self):
        if self.large_venue_fee is not None and not isinstance(self.large_venue_fee, bool):
            raise AuditError("largeVenueFee 必须是布尔值 true 或 false")
        if self.scenario is not None and self.scenario not in SCENARIO_ORDER:
            raise AuditError("未登记的核销类型；当前只支持PDF八类")
        self.intake_case = prepare_material_diagnosis(
            self.input_dir, self.temporary_root / "sources", original_error="",
            classify_by_content=True,
        )
        self.emit("cases.prepared", cases={a["archive_id"]: a for a in self.intake_case["archives"]},
                  scenarios=[], classification_policy="biz_type" if self.biz_type is not None else "material_content")
        return self

    def analysis(self):
        self.emit("material_classification.started", archive_count=len(self.intake_case["archives"]),
                  large_venue_fee=self.large_venue_fee,
                  classification_policy="biz_type" if self.biz_type is not None else "material_content")
        if self.biz_type is not None:
            if not isinstance(self.biz_type, str) or not self.biz_type.strip():
                raise BizTypeRecognitionError()
            selection = {"scenario": scenario_for_biz_type(self.biz_type),
                         "reason": "按bizType八个规范值与Skill的固定映射精确匹配"}
            validate(selection, BIZ_TYPE_SCHEMA)
            self.emit("biz_type.resolved", biz_type=self.biz_type, **selection)
            self.selected_scenario = selection["scenario"]
            if self.selected_scenario is None:
                raise BizTypeRecognitionError()
            if self.scenario and self.scenario != self.selected_scenario:
                raise AuditError("--scenario与bizType指定的核销类型不一致")
        for archive in self.intake_case["archives"]:
            aid = archive["archive_id"]
            units = prepare_units(archive, self.temporary_root / f"pages-{aid}")
            reading = self.provider({"kind": "pdf_material_classification", "archive_id": aid,
                                     "units": units, "selected_scenario": self.selected_scenario,
                                     "large_venue_fee": self.large_venue_fee}, self.temporary_root)
            if set(reading) != {"documents", "classification"}:
                raise AuditError("资料检查必须返回完整材料事实和门禁比对结果")
            validate_documents({"documents": reading["documents"]}, units)
            route = classification_decision(reading["classification"], reading["documents"], self.selected_scenario,
                                            large_venue_fee=self.large_venue_fee)
            packet = {"archive_id": aid, "source_archive": Path(archive["source_archive"]).name,
                      "archive_sha256": archive["archive_sha256"], "units": units,
                      "large_venue_fee": self.large_venue_fee,
                      **reading, "route": route}
            self.packets.append(packet)
            if not route["matched"]:
                continue
            scenario = route["scenario"]
            if self.scenario and self.scenario != scenario:
                packet["excluded_by_selection"] = True
                continue
            self.emit("scenario.started", scenario=scenario)
            packet["audit_evidence"] = self.provider({"kind": "pdf_policy_audit", "archive_id": aid,
                "scenario": scenario, "flags": reading["classification"]["flags"],
                "large_venue_fee": self.large_venue_fee,
                "materials": reading["classification"]["materials"],
                "documents": reading["documents"], "units": units}, self.temporary_root)
        if self.scenario and not any("audit_evidence" in p for p in self.packets) and all(p["route"]["matched"] for p in self.packets):
            raise AuditError("资料内容未识别出指定核销类型；--scenario不能覆盖AI分类")
        return self

    def evidence(self):
        # Complete every extraction first; reject invalid evidence before any
        # decision is published, preserving the stage and source boundaries.
        for packet in self.packets:
            if "audit_evidence" in packet:
                audit_decision(packet["route"]["scenario"], packet["classification"]["flags"],
                               packet["audit_evidence"], packet["documents"], packet["classification"]["materials"])
        self.emit("pdf_materials.validated", packets=self.packets, policy_version=POLICY_VERSION)
        return self

    def decision(self):
        by_scenario = {}
        rejected = []
        for packet in self.packets:
            route = packet["route"]
            if not route["matched"]:
                code = ("material_gate_unmet" if self.selected_scenario else
                        "material_type_ambiguous" if packet["classification"]["candidate_scenarios"] else "material_type_unmatched")
                reason = packet["source_archive"] + "：" + "；".join(_reason(r, packet["units"]) for r in route["reasons"])
                rejected.append({"archive_id": packet["archive_id"], "source_archive": packet["source_archive"],
                    "archive_sha256": packet["archive_sha256"], "scenario": self.selected_scenario, "reason_code": code,
                    "matched_scenarios": packet["classification"]["candidate_scenarios"],
                    "reason": reason, "action": ("按指定业务类型补充或调整上述资料。" if self.selected_scenario else
                                                  "按PDF核销资料清单提供可明确对应一种费用类型的资料。")})
                continue
            if packet.get("excluded_by_selection"):
                continue
            scenario = route["scenario"]
            result = audit_decision(scenario, packet["classification"]["flags"],
                                    packet["audit_evidence"], packet["documents"], packet["classification"]["materials"])
            result.update(archive_id=packet["archive_id"], source_archive=packet["source_archive"])
            packet["result"] = result
            by_scenario.setdefault(scenario, []).append(result)
        for scenario, results in by_scenario.items():
            self.emit("result.validated", scenario=scenario, result={"kind": "pdf_policy", "scenario": scenario,
                      "policy_version": POLICY_VERSION, "archives": results,
                      "summary": {"error_count": sum(r["summary"]["error_count"] for r in results)}})
        if rejected:
            from .material_diagnostic_output import CLASSIFICATION_REJECTION_SCHEMA
            self.rejection = {"schema_version": "1.0", "kind": "classification_rejection",
                              "decision_source": "biz_type" if self.selected_scenario else "material_content", "generated_at": now_utc(),
                              "summary": {"conclusion": "issues_found"}, "archives": rejected}
            validate_json(self.rejection, CLASSIFICATION_REJECTION_SCHEMA)
            self.emit("classification_rejection.result_validated", result=self.rejection)
        return self

    def verification(self):
        view = build_view(self.packets, self.rejection)
        expected = sum("result" in p or not p["route"]["matched"] for p in self.packets)
        if len(view["sheets"]) != expected:
            raise AuditError("PDF审核页面没有完整覆盖本次材料包")
        verification = {"pdf_policy": {"version": POLICY_VERSION, "source_coverage_validated": True,
                         "classification_policy": "biz_type" if self.selected_scenario else "material_content", "schema_validated": True,
                         "archive_count": len(self.packets), "sheet_count": len(view["sheets"])}}
        self.emit("report.verified", verification=verification)
        scenarios = ([self.selected_scenario] if self.selected_scenario else
                     [s for s in SCENARIO_ORDER if any(p.get("result", {}).get("scenario") == s for p in self.packets)])
        self.receipt = {"run_id": self.run_id, "producer_model": self.producer_model,
                        "scenarios": scenarios, "view_payload": view, "verification": verification}
        return self

    def run(self):
        chain = build_audit_chain([(name, getattr(PdfWorkflow, name))
                                   for name in ("intake", "analysis", "evidence", "decision", "verification")])
        return invoke_audit_chain(chain, self).receipt
