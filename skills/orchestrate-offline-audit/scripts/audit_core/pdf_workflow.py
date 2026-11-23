"""Fixed business-type routing and the selected Skill's material gate and audit."""
from __future__ import annotations

from dataclasses import dataclass, field
from copy import deepcopy
from pathlib import Path
from typing import Any

from .common import AuditError, BizTypeRecognitionError, now_utc, validate_json
from .customer_language import customer_reason, numeric_problem, plain_text
from .error_reason import attach_error_reasons
from .material_intake import prepare_material_diagnosis
from .material_issue_files import attach_material_issue_files
from .pdf_evidence import BIZ_TYPE_SCHEMA, audit_decision, classification_decision, validate, validate_documents
from .pdf_materials import prepare_units
from .pdf_policy import POLICY_VERSION
from .scenario_registry import SCENARIO_LABELS, SCENARIO_ORDER, scenario_for_biz_type
from .workflow import build_audit_chain, invoke_audit_chain
from .qince_context import context_sources, entry_context_sources
from .qince_references import photo_reference_sources
from .cross_claim_photos import inspect_and_record
from .submitted_source_checks import submitted_source_checks


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
    "settlement_activity_dates": "结算单与合同活动周期",
    "photo_activity_dates": "照片拍摄日期与活动周期",
    "application_activity_dates": "勤策申请与合同结算活动周期",
    "giveaway_evidence": "搭赠活动照片或小票", "entry_agreement": "产品推广协议",
    "entry_sku": "进场商品", "entry_watermark": "进场照片水印",
    "entry_duplicate": "不同申报门店的照片地址重复",
    "entry_photo_coverage": "上架照片门店覆盖",
    "entry_inventory": "全量进场库存或POS证明",
    "stamped_document_pdf": "验章资料PDF格式",
    "gift_activity_evidence": "外采赠品执行证明",
}
CHECK_CATEGORIES.update({
    'settlement_dealer':'结算单经销商名称','settlement_application_dates':'结算单与系统申请周期',
    'contract_dealer':'促销合同经销商名称','contract_budget_amount':'合同预算与系统申请总金额',
    'contract_activity_dates':'合同与系统申请周期','claim_ceiling':'核销金额与系统批复合计',
    'settlement_calculation':'结算单金额计算','application_claim_ceiling':'核销金额与系统申请金额',
    'staff_headcount':'临促人数','staff_total_unit_price':'临促申请单价与工资天数',
    'payment_company':'激励付款品牌字样','payment_claim_amount':'付款与核销金额',
    'payment_activity_dates':'付款日期','payment_statement_seal':'批量微信账单盖章',
    'payment_statement_dealer':'批量微信账单经销商名称','staff_photo_content':'临促每日现场照片',
    'training_photo_content':'各场培训现场照片','pos_seal':'POS经销商盖章','pos_dealer':'POS经销商名称',
    'application_products':'系统申请商品对应','invoice_amount':'票据金额','posting_before_after':'张贴前后照片',
    'material_list_amount':'物料制作清单金额','gift_store_photo_content':'自采赠品门店照片',
    'price_matches_contract':'活动价格与合同促销价','activity_photo_content':'活动照片日期与门店',
    'pos_sales_amount':'POS实际销售金额','target_reward':'POS达标奖励计算',
    'entry_application_products':'推广协议与系统申请商品',
    'pos_activity_dates':'销售明细与结算活动周期',
    'contract_reward_unit_price':'合同奖励单价与系统申请',
    'receipt_finance_seal':'收据收款方财务专用章',
    'poster_payment_dates':'海报张贴费用支付日期',
})


def _basename(value: str) -> str:
    return str(value).replace("\\", "/").rsplit("/", 1)[-1]


def _source_location(source_file: str, locator: str) -> str:
    """Keep meaningful positions; a standalone image's locator is its filename."""
    name = _basename(source_file)
    position = str(locator).replace("\\", "/")
    if not position or position in {str(source_file).replace("\\", "/"), name}:
        return name
    return name + " · " + str(locator)


def _reason(value: str, units: list[dict]) -> str:
    return plain_text(value, units)


def _summary_item(check: dict, units: list[dict], *, reason: str | None = None) -> dict:
    """Keep each decision separate from the full human-readable photo findings."""
    from .model_authority import enabled
    return {"rule_id": check["rule_id"], "status": check["status"],
            "effect": check.get("effect", ""),
            "check_category": CHECK_CATEGORIES.get(check["rule_id"],check["rule_id"]),
            "reason": (plain_text(check['reason'],units) if enabled() else customer_reason(check, units)) if reason is None else reason,
            "source_ids": list(check["source_ids"])}


def card_evidence(ids: list[str], units: list[dict], documents: list[dict]) -> dict:
    by_id = {u["unit_id"]: u for u in units}
    facts = {d["unit_id"]: d for d in documents}
    sources = []
    for uid in ids:
        if uid not in by_id or uid not in facts:
            continue  # Preserve unresolved model references in its raw judgment, never invent a source.
        unit = by_id[uid]
        source = unit["source_file"]
        sources.append({"file": source, "original_file": source, "kind": "submitted",
                        "role": "勤策业务明细" if unit.get("source_kind") == "qince" else "本次核销资料", "locator": unit["locator"],
                        "facts": facts[uid]["facts"]})
    names = list(dict.fromkeys(s["original_file"] for s in sources))
    return {"schema_version": "1.0", "source_files": names, "source_file_count": len(names),
            "sources": sources, "derived_file_count": 0, "references": [],
            "comparisons": [], "limitations": [], "file_count_complete": True}


def _scene_units(units: list[dict], materials: list[dict], documents: list[dict]) -> list[dict]:
    """Index submitted scene photos only; never use filenames to infer roles."""
    photo_roles = {"display_photos", "shelf_photos", "finished_photos", "gift_finished_photos",
                   "gift_store_photos", "giveaway_evidence", "staff_photos", "price_evidence",
                   "full_reduction_evidence"}
    mixed_roles = {"giveaway_evidence", "staff_photos", "price_evidence", "full_reduction_evidence"}
    assignments: dict[str, set[str]] = {}
    for material in materials:
        if material["state"] not in {"present", "unclear"}:
            continue
        for uid in material["source_ids"]:
            assignments.setdefault(uid, set()).add(material["id"])
    facts = {d["unit_id"]: d for d in documents}
    result = []
    for unit in units:
        roles = assignments.get(unit["unit_id"], set())
        # A whole PDF page that also contains a contract/payment is not an
        # independently extracted scene photo and must not pollute this index.
        if not unit.get("image") or not roles or roles - photo_roles:
            continue
        role = next((name for name in sorted(roles) if name not in mixed_roles), sorted(roles)[0])
        observed = {str(r).strip().lower() for r in facts.get(unit["unit_id"], {}).get("roles", [])}
        scene = bool(observed & {"photo", "scene_photo", "field_photo", "display_photo",
                                 "现场照片", "活动照片", "陈列照片"})
        result.append({**unit, "role": role, "scene_photo": scene})
    return result


def _reuse_rows(packet: dict, start: int) -> list[dict]:
    from .model_authority import enabled
    if enabled():
        return []
    """Publish exact file identity, never a pHash score as a business problem."""
    rows = []
    by_id = {unit["unit_id"]: unit for unit in packet["units"]}
    for photo in (packet.get("photo_reuse") or {}).get("photos", []):
        matches = photo.get("exact_reuse") or []
        uid = photo.get("unit_id")
        if not matches or uid not in by_id:
            continue
        unit = by_id[uid]
        location = _source_location(unit["source_file"], unit["locator"])
        other = "；".join("核销单「" + str(m["verify_code"]) + "」的"
                          + _source_location(m["source_file"], m["locator"]) for m in matches)
        reason = location + "与" + other + "原图文件完全相同。"
        item = {"rule_id": "cross_claim_photo_reuse", "status": "fail", "effect": "",
                "check_category": "跨单原图重复", "reason": reason, "source_ids": [uid]}
        rows.append({"excel_row": start + len(rows), "kind": "record", "section": "detail",
                     "status": "issue", "confidence": "high", "heading": "跨单原图重复",
                     "check_category": "跨单原图重复", "rule_id": "cross_claim_photo_reuse",
                     "numeric": False, "summary_items": [item], "photo_unit_id": uid,
                     "historical_photo_matches": matches,
                     "card_evidence": card_evidence([uid], packet["units"], packet["documents"]),
                     "error_reason": reason, "error_reasons": [reason],
                     "values": [location, reason, "本地比对原图文件，排除同一核销单的重交与重跑",
                                "已发现原图重复", "", ""]})
    return rows


def _with_submitted_checks(result: dict, observations: list[dict], reviews: dict | None = None) -> dict:
    """Retain program advice unless this run's model explicitly reviewed it."""
    result = deepcopy(result)
    from .model_authority import enabled
    if enabled():
        # Source tools supply observations to the AI; only its returned checks
        # enter the customer decision. No program-generated fail/unknown rows.
        return result
    for rule_id in dict.fromkeys(check["rule_id"] for check in observations):
        group = [check for check in observations if check["rule_id"] == rule_id]
        original = next((check for check in result["checks"] if check["rule_id"] == rule_id), None)
        merged = deepcopy(group[0])
        reasons = ([original["reason"]] if original and original["status"] == "fail" else [])
        merged.update(reason="；".join(dict.fromkeys(reasons + [check["reason"] for check in group])),
                      source_ids=sorted({uid for check in group for uid in check["source_ids"]}
                                        | (set(original["source_ids"]) if reasons else set())),
                      status="fail" if any(check["status"] == "fail" for check in group) or reasons else "unknown")
        if reviews and rule_id in reviews:
            merged = deepcopy(reviews[rule_id])
        result["checks"] = [check for check in result["checks"] if check["rule_id"] != rule_id] + [merged]
    problems = [check for check in result["checks"] if check["status"] in {"fail", "unknown"}]
    result["summary"] = {"conclusion": "issues_found" if problems else "no_issues_found", "error_count": len(problems)}
    result["noncompliance_registration"] = [check["reason"] for check in problems]
    return result


def _submitted_checks(packet: dict) -> list[dict]:
    return _with_submitted_checks({'checks': []}, packet.get('submitted_source_checks', []),
                                 packet.get('submitted_check_reviews'))['checks']


def _submitted_check_rows(packet: dict, start: int) -> list[dict]:
    rows = []
    for check in _submitted_checks(packet):
        if check['status'] not in {'fail', 'unknown'}:
            continue
        reason = customer_reason(check, packet["units"])
        rows.append({"excel_row": start + len(rows), "kind": "record", "section": "detail",
                     "status": "issue", "confidence": "low" if check["status"] == "unknown" else "high",
                     "heading": CHECK_CATEGORIES[check["rule_id"]], "check_category": CHECK_CATEGORIES[check["rule_id"]],
                     "rule_id": check["rule_id"], "numeric": False,
                     "summary_items": [_summary_item(check, packet["units"], reason=reason)],
                     "error_reason": reason, "error_reasons": [reason],
                     "card_evidence": card_evidence(check["source_ids"], packet["units"], packet["documents"]),
                     "business_source_files": list(dict.fromkeys(unit["source_file"] for unit in packet["units"]
                         if unit["unit_id"] in check["source_ids"] and unit.get("source_kind") != "qince")),
                     "values": ["已提交资料", reason, "暂时不能确认" if check["status"] == "unknown" else "需要处理"]})
    return rows


def build_view(packets: list[dict], rejection: dict | None) -> dict:
    sheets = []
    for packet in packets:
        result = packet.get("result")
        if result is None:
            continue
        scenario = result["scenario"]
        label = SCENARIO_LABELS[scenario]
        rows = []
        for review in result.get("photo_reviews", []):
            unit = next((unit for unit in packet["units"] if unit["unit_id"] == review["unit_id"]),
                        {'source_file':review['unit_id'], 'locator':'模型照片引用（尚未定位到本单原图）'})
            observations = review["checks"]
            status = "fail" if any(check["status"] == "fail" for check in observations) else "unknown" if any(check["status"] == "unknown" for check in observations) else "pass"
            passed = status == "pass"
            sources = list(dict.fromkeys(uid for check in observations for uid in check["source_ids"]))
            evidence = card_evidence(sources, packet["units"], packet["documents"])
            findings = "；".join(CHECK_CATEGORIES.get(check["rule_id"],check.get('requirement') or '模型核对项') + "：" + plain_text(check["reason"], packet["units"])
                                 for check in observations)
            location = _source_location(unit["source_file"], unit["locator"])
            issue_checks = [check for check in observations if check["status"] in {"fail", "unknown"}]
            problems = "；".join(CHECK_CATEGORIES.get(check["rule_id"],check.get('requirement') or '模型核对项') + "：" + plain_text(check["reason"], packet["units"])
                                 for check in issue_checks)
            reason = location + "：" + problems if problems else ""
            rows.append({"excel_row": len(rows) + 4, "kind": "record", "section": "detail",
                         "status": "pass" if passed else "issue", "confidence": "low" if status == "unknown" else "high",
                         "heading": "陈列照片逐张分析：" + unit["locator"], "check_category": "陈列照片逐张分析",
                         "numeric": False, "rule_id": "display_photo_review", "photo_unit_id": review["unit_id"],
                         "summary_items": [_summary_item(check, packet["units"]) for check in issue_checks],
                         "card_evidence": evidence, "error_reason": "" if passed else reason,
                         "error_reasons": [] if passed else [reason],
                         "business_source_files": [unit["source_file"]] if issue_checks else [],
                         "values": [location, findings, "逐张按现有陈列照片要求核对水印、实物品牌、宣传物料及大小",
                                    "已核对，符合要求" if passed else "暂时不能确认" if status == "unknown" else "需要处理", "", ""]})
        for check in result["checks"]:
            if check["status"] == "not_applicable":
                continue
            if result.get("photo_reviews") and check["rule_id"] in {
                    "display_watermark", "display_brand", "display_material_brand", "display_size"}:
                continue
            evidence = card_evidence(check["source_ids"], packet["units"], packet["documents"])
            evidence["comparisons"] = check.get("calculations") or []
            files = "、".join(_basename(f) for f in evidence["source_files"])
            from .model_authority import enabled
            reason = plain_text(check['reason'],packet['units']) if enabled() else ((numeric_problem(check, packet["units"])
                      if check["status"] == "fail" else None) or customer_reason(check, packet["units"])
                      )
            if not enabled() and files and not any(_basename(f) in reason for f in evidence["source_files"]):
                reason = f"{files}：{reason}"
            passed = check["status"] == "pass"
            category = CHECK_CATEGORIES.get(check["rule_id"],check["rule_id"])
            heading = category + ("：暂时不能确认" if check["status"] == "unknown" else "：需要处理" if not passed else "：已核对")
            rows.append({"excel_row": len(rows) + 4, "kind": "record", "section": "detail",
                         "status": "pass" if passed else "issue",
                         "confidence": "low" if check["status"] == "unknown" else "high",
                         "heading": heading,
                         "check_category": category,
                         "numeric": bool(check.get("calculations")),
                         "rule_id": check["rule_id"], "card_evidence": evidence,
                         "summary_items": [_summary_item(check, packet["units"], reason=reason)] if not passed else [],
                         "error_reason": "" if passed else reason,
                         "error_reasons": [] if passed else [reason],
                         "values": [files or "目前没有对应资料可供核对", reason, plain_text(check["requirement"], packet["units"]),
                                    "已核对，符合要求" if passed else "暂时不能确认" if check["status"] == "unknown" else "需要处理",
                                    "",
                                    ""]})
        rows.extend(_reuse_rows(packet, len(rows) + 4))
        failures = sum(row["status"] == "issue" for row in rows)
        sheets.append({"name": label + "核销", "title": label + "核销", "scenario": scenario,
                       "projection_kind": "pdf_policy", "audit_type_label": label,
                       "source_archive": packet["source_archive"], "archive_id": packet["archive_id"],
                       "business_decision": "issues_found" if failures else result["summary"]["conclusion"], "note": "",
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
                           "archive_id": archive["archive_id"],
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
        for sheet in sheets:
            if sheet.get("projection_kind") != "classification_rejection":
                continue
            packet = next(p for p in packets if p["archive_id"] == sheet["archive_id"])
            extra = _submitted_check_rows(packet, len(sheet["rows"]) + 4)
            extra.extend(_reuse_rows(packet, len(sheet["rows"]) + len(extra) + 4))
            sheet["rows"].extend(extra)
            for count in ("source_row_count", "error_count", "detail_error_count"):
                sheet["audit_counts"][count] += len(extra)
    from .model_authority import enabled
    return attach_material_issue_files(view if enabled() else attach_error_reasons(view), packets)


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
    cvs_otc_channel: bool = False
    verify_code: str | None = None
    qince_data: dict | None = None
    worktrees_root: Path | None = None
    workspace_id: str | None = None
    selected_scenario: str | None = None
    intake_case: dict = field(default_factory=dict)
    packets: list[dict] = field(default_factory=list)
    receipt: dict = field(default_factory=dict)
    rejection: dict | None = None
    logic_review_result: dict = field(default_factory=dict)
    summary_review_result: dict | None = None
    summary_review_error: str | None = None
    selection_diagnostics: list[dict] = field(default_factory=list)

    def emit(self, event: str, **payload):
        from .execution_trace import record_step
        record_step("workflow." + event, "核销阶段：" + event, payload)
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
        if self.qince_data is not None and len(self.intake_case["archives"]) != 1:
            raise AuditError("带勤策明细时请按核销单分别运行，避免多包串用业务资料。")
        self.emit("cases.prepared", cases={a["archive_id"]: a for a in self.intake_case["archives"]},
                  scenarios=[], classification_policy="biz_type" if self.biz_type is not None else "material_content")
        return self

    def analysis(self):
        self.emit("material_classification.started", archive_count=len(self.intake_case["archives"]),
                  large_venue_fee=self.large_venue_fee,
                  cvs_otc_channel=self.cvs_otc_channel is True,
                  classification_policy="biz_type" if self.biz_type is not None else "material_content")
        if self.biz_type is not None:
            if not isinstance(self.biz_type, str) or not self.biz_type.strip():
                raise BizTypeRecognitionError()
            selection = {"scenario": scenario_for_biz_type(self.biz_type),
                         "reason": "按bizType规范值及已确认别名与Skill的固定映射精确匹配"}
            validate(selection, BIZ_TYPE_SCHEMA)
            self.emit("biz_type.resolved", biz_type=self.biz_type, **selection)
            self.selected_scenario = selection["scenario"]
            if self.selected_scenario is None:
                raise BizTypeRecognitionError()
            if self.scenario and self.scenario != self.selected_scenario:
                # Explicit inputs can disagree; keep both facts and let the
                # confirmed bizType drive this run instead of stopping it.
                self.selection_diagnostics.append({
                    "kind": "scenario_selection_mismatch",
                    "requested_scenario": self.scenario,
                    "biz_type_scenario": self.selected_scenario,
                    "resolution": "按bizType继续，保留--scenario输入差异供复核",
                })
                self.emit("scenario.selection_mismatch", requested=self.scenario,
                          resolved=self.selected_scenario)
        # bizType is the confirmed explicit business type; --scenario remains
        # an advisory selection and must not override it.
        requested_scenario = self.selected_scenario or self.scenario
        for archive in self.intake_case["archives"]:
            aid = archive["archive_id"]
            units = prepare_units(archive, self.temporary_root / f"pages-{aid}")
            # Entry business details can replace the protocol's scope source.
            # They must be visible to its material gate, not injected only after
            # a missing-protocol rejection has already closed that gate.
            entry_context_loaded = self.selected_scenario == "entry_fee" and self.qince_data is not None
            if entry_context_loaded:
                context_units, _ = entry_context_sources(self.qince_data, self.verify_code or "", aid)
                units.extend(context_units)
            reading = self.provider({"kind": "pdf_material_classification", "archive_id": aid,
                                     "units": units, "selected_scenario": self.selected_scenario,
                                     "worktrees_root": self.worktrees_root,
                                     "run_id": self.run_id, "workspace_id": self.workspace_id,
                                     "large_venue_fee": self.large_venue_fee,
                                     "cvs_otc_channel": self.cvs_otc_channel is True}, self.temporary_root)
            if set(reading) != {"documents", "classification"}:
                missing = {"documents", "classification"} - set(reading)
                if missing:
                    # Only an unreadable answer is an execution boundary; extra
                    # fields are kept for the model's own later review.
                    raise AuditError("资料读取结果缺少可读取字段：" + "、".join(sorted(missing)))
            validate_documents({"documents": reading["documents"]}, units)
            route = classification_decision(reading["classification"], reading["documents"], self.selected_scenario,
                                            large_venue_fee=self.large_venue_fee,
                                            cvs_otc_channel=self.cvs_otc_channel is True)
            packet = {"archive_id": aid, "source_archive": Path(archive["source_archive"]).name,
                      "archive_sha256": archive["archive_sha256"], "units": units,
                      "large_venue_fee": self.large_venue_fee,
                      "cvs_otc_channel": self.cvs_otc_channel is True,
                      **reading, "route": route}
            self.packets.append(packet)
            if self.worktrees_root is not None:
                packet["photo_reuse"] = inspect_and_record(
                    self.worktrees_root, verify_code=self.verify_code, run_id=self.run_id,
                    workspace_id=self.workspace_id, archive_sha256=archive["archive_sha256"],
                    units=_scene_units(units, reading["classification"]["materials"], reading["documents"]),
                    recognition_version=POLICY_VERSION,
                )
            scenario = route.get("scenario") or self.selected_scenario
            if requested_scenario and requested_scenario != scenario:
                packet["excluded_by_selection"] = True
                continue
            if scenario and self.qince_data is not None and not entry_context_loaded:
                extra_units, extra_documents = context_sources(self.qince_data, self.verify_code or "", aid)
                units.extend(extra_units)
                reading["documents"].extend(extra_documents)
                validate_documents({"documents": reading["documents"]}, units)
            if scenario and self.qince_data is not None:
                photo_units, photo_documents = photo_reference_sources(
                    self.qince_data, self.verify_code or "", aid, units)
                units.extend(photo_units)
                reading["documents"].extend(photo_documents)
                validate_documents({"documents": reading["documents"]}, units)
            if scenario:
                from .model_authority import enabled
                def source_advice():
                    return submitted_source_checks(scenario, reading["classification"]["flags"],
                        reading["documents"], reading["classification"]["materials"], units)
                if enabled():
                    from .audit_tools import assist
                    packet["submitted_source_checks"] = assist('submitted_source_advice',source_advice,fallback=[])
                else:
                    packet["submitted_source_checks"] = source_advice()
            if not route["matched"]:
                continue
            self.emit("scenario.started", scenario=scenario)
            packet["audit_evidence"] = self.provider({"kind": "pdf_policy_audit", "archive_id": aid,
                "scenario": scenario, "flags": reading["classification"]["flags"],
                "large_venue_fee": self.large_venue_fee,
                "cvs_otc_channel": self.cvs_otc_channel is True,
                "worktrees_root": self.worktrees_root,
                "run_id": self.run_id, "workspace_id": self.workspace_id,
                "materials": reading["classification"]["materials"],
                "documents": reading["documents"], "units": units,
                "_submitted_source_advice":packet.get('submitted_source_checks',[]),
                "_photo_reuse_advice":packet.get('photo_reuse',{})}, self.temporary_root)
        if requested_scenario and not any("audit_evidence" in p for p in self.packets) and all(p["route"]["matched"] for p in self.packets):
            self.selection_diagnostics.append({
                "kind": "scenario_content_mismatch",
                "requested_scenario": requested_scenario,
                "reason": "资料内容未识别出指定核销类型；保留AI分类结果，不覆盖模型判断",
            })
            self.emit("scenario.content_mismatch", requested=requested_scenario)
        return self

    def evidence(self):
        # Complete every extraction first; reject invalid evidence before any
        # decision is published, preserving the stage and source boundaries.
        for packet in self.packets:
            if "audit_evidence" in packet:
                audit_decision(packet["route"]["scenario"], packet["classification"]["flags"],
                               packet["audit_evidence"], packet["documents"], packet["classification"]["materials"], units=packet["units"])
        self.emit("pdf_materials.validated", packets=self.packets, policy_version=POLICY_VERSION)
        return self

    def decision(self):
        by_scenario = {}
        rejected = []
        self.rejection = None
        for packet in self.packets:
            route = packet["route"]
            if not route["matched"]:
                code = ("material_gate_unmet" if self.selected_scenario else
                        "material_type_ambiguous" if packet["classification"]["candidate_scenarios"] else "material_type_unmatched")
                reasons = list(route["reasons"]) + [check["reason"] for check in _submitted_checks(packet)
                                                     if check['status'] in {'fail', 'unknown'}]
                reason = packet["source_archive"] + "：" + "；".join(_reason(r, packet["units"]) for r in reasons)
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
                                    packet["audit_evidence"], packet["documents"], packet["classification"]["materials"], units=packet["units"])
            result = _with_submitted_checks(result, packet.get("submitted_source_checks", []),
                                            packet.get('submitted_check_reviews'))
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

    def logic_review(self):
        from .final_logic_review import run
        return run(self)

    def verification(self):
        view = build_view(self.packets, self.rejection)
        expected = sum("result" in p or not p["route"]["matched"] for p in self.packets)
        from .model_authority import enabled
        verification = {"pdf_policy": {"version": POLICY_VERSION, "source_coverage_validated": True,
                         "classification_policy": "biz_type" if self.selected_scenario else "material_content", "schema_validated": not enabled(),
                         "business_authority":"executing_model" if enabled() else "legacy_policy",
                         "deterministic_business_gates":not enabled(),
                         "archive_count": len(self.packets), "sheet_count": len(view["sheets"])}}
        # Page coverage is a reported fact. A missing projection is described
        # in the receipt and must not abort the audit or the callback.
        verification["coverage"] = {"expected_sheets": expected,
            "actual_sheets": len(view["sheets"]),
            "complete": len(view["sheets"]) == expected}
        if self.selection_diagnostics:
            verification["selection_diagnostics"] = deepcopy(self.selection_diagnostics)
        verification['logic_review'] = deepcopy(self.logic_review_result)
        # The review must see the fully assembled customer draft, including
        # program-added source labels, after every business check has finished.
        # logic_review already reviewed this draft. Sealing never starts a
        # second review, asks for images, or restarts business auditing.
        reviewed_summary = self.summary_review_result
        summary_error = self.summary_review_error
        if (not isinstance(reviewed_summary, dict)
                or not isinstance(reviewed_summary.get('text'), str)
                or not reviewed_summary['text'].strip()
                or (reviewed_summary.get('metadata') or {}).get('status') != 'reviewed'):
            # The model review is advice for the reader; when it is not usable
            # keep the program-rendered summary and record the real outcome.
            reviewed_summary = None
            verification['summary_review'] = {'version': 1, 'status': 'unreviewed',
                'reason': '模型未返回可用核验稿，保留系统生成的问题摘要。'}
            if summary_error:
                verification['summary_review']['error'] = summary_error
        if reviewed_summary is not None:
            verification['summary_review'] = reviewed_summary['metadata']
        self.emit("report.verified", verification=verification)
        scenarios = ([self.selected_scenario] if self.selected_scenario else
                     [s for s in SCENARIO_ORDER if any(p.get("result", {}).get("scenario") == s for p in self.packets)])
        self.receipt = {"run_id": self.run_id, "producer_model": self.producer_model,
                        "scenarios": scenarios, "view_payload": view, "verification": verification}
        if reviewed_summary is not None:
            self.receipt['reviewed_analysis_summary'] = reviewed_summary['text']
        return self

    def run(self):
        chain = build_audit_chain([(name, getattr(PdfWorkflow, name))
                                    for name in ("intake", "analysis", "evidence", "decision", "logic_review", "verification")])
        from .model_authority import execution
        from contextlib import nullcontext
        # Literal/legacy evidence importers do not run an executing AI. They
        # retain their import contract; all normal uploads use the live provider.
        with execution() if callable(getattr(self.provider,'_call',None)) else nullcontext():
            return invoke_audit_chain(chain, self).receipt
