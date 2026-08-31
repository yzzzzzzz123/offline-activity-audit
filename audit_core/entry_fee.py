from __future__ import annotations

from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from .common import AuditError, json_number, money, normalize_text, now_utc, sha256_file


CENT = Decimal("0.01")


def _issue(
    code: str,
    title: str,
    source_files: list[str],
    observed: str,
    expected: str,
    impact: str,
    resubmission: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "title": title,
        "source_files": list(dict.fromkeys(source_files)),
        "observed": observed,
        "expected": expected,
        "impact": impact,
        "resubmission": resubmission,
        "severity": "high",
        "confidence": "high",
    }


def _control(controls: list[dict[str, str]], control_id: str, passed: bool, basis: str) -> None:
    controls.append(
        {"control_id": control_id, "status": "pass" if passed else "fail", "basis": basis}
    )


def _by_role(documents: list[dict[str, Any]], role: str) -> list[dict[str, Any]]:
    return [item for item in documents if str(item.get("role")) == role]


def _sources(documents: list[dict[str, Any]]) -> list[str]:
    return [str(item.get("source_file") or "") for item in documents]


def _decimal(value: Any, *, label: str) -> Decimal | None:
    if value is None:
        return None
    return money(value, label=label)


def _round(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None


def _score(left: Any, right: Any) -> float:
    a = normalize_text(left)
    b = normalize_text(right)
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 1.0
    return SequenceMatcher(None, a, b).ratio()


def _store_text(value: Any) -> str:
    text = normalize_text(value)
    for old, new in (("瓯", "欧"), ("购物广场", ""), ("吾悦广场", "吾悦")):
        text = text.replace(old, new)
    for token in ("家得福系统", "家得福", "系统", "门店"):
        text = text.replace(token, "")
    if text.endswith("店"):
        text = text[:-1]
    return text


def _store_score(photo: dict[str, Any], store: dict[str, Any]) -> float:
    photo_values = [
        photo.get("photo_store_name"),
        photo.get("photo_location"),
    ]
    store_values = [store.get("store_name"), store.get("full_address")]
    scores: list[float] = []
    for photo_value in photo_values:
        for store_value in store_values:
            raw_score = _score(photo_value, store_value)
            simplified_score = _score(_store_text(photo_value), _store_text(store_value))
            scores.append(max(raw_score, simplified_score))
    return max(scores, default=0.0)


def _match_store(
    photo: dict[str, Any],
    stores: list[dict[str, Any]],
) -> tuple[int | None, float, str]:
    ranked = sorted(
        ((_store_score(photo, store), index) for index, store in enumerate(stores)),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.62:
        return None, ranked[0][0] if ranked else 0.0, "照片水印未唯一指向合同门店"
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.08:
        return None, ranked[0][0], "照片水印对应多个合同门店，无法唯一收敛"
    return ranked[0][1], ranked[0][0], "照片水印唯一对应合同门店"


def _match_product(
    visible: dict[str, Any],
    items: list[dict[str, Any]],
) -> tuple[int | None, float]:
    visible_code = normalize_text(visible.get("product_code"))
    if visible_code:
        exact = [
            index for index, item in enumerate(items)
            if normalize_text(item.get("product_code")) == visible_code
        ]
        if len(exact) == 1:
            return exact[0], 1.0
    visible_text = " ".join(
        str(value or "")
        for value in (visible.get("product_name"), visible.get("visible_package_text"))
    )
    ranked = sorted(
        ((_score(visible_text, item.get("product_name")), index) for index, item in enumerate(items)),
        reverse=True,
    )
    if not ranked or ranked[0][0] < 0.72:
        return None, ranked[0][0] if ranked else 0.0
    if len(ranked) > 1 and ranked[0][0] - ranked[1][0] < 0.06:
        return None, ranked[0][0]
    return ranked[0][1], ranked[0][0]


def audit_entry_fee_case(
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    if str(case.get("scenario")) != "entry_fee":
        raise AuditError("进场费审核收到错误的场景类型")
    documents = list(evidence.get("documents") or [])
    contracts = _by_role(documents, "entry_fee_contract")
    photos = _by_role(documents, "shelf_photo")
    deduction_proofs = _by_role(documents, "system_deduction_proof")
    contract = contracts[0] if len(contracts) == 1 else None
    controls: list[dict[str, str]] = []
    issues: list[dict[str, Any]] = []

    missing: list[str] = []
    if len(contracts) != 1:
        missing.append("双方签章进场费合同")
    if not photos:
        missing.append("合同门店进场/上架水印照片")
    if not deduction_proofs:
        missing.append("系统扣款凭证")
    materials_pass = not missing
    _control(
        controls,
        "required_materials",
        materials_pass,
        "合同、上架照片和系统扣款凭证齐全" if materials_pass else "缺少：" + "、".join(missing),
    )
    if missing:
        issues.append(
            _issue(
                "required_materials_missing",
                "进场费资料不完整",
                _sources(documents),
                "缺少：" + "、".join(missing),
                "须同时提交双方签章进场费合同、合同门店水印上架照片及合同要求的系统扣款凭证。",
                "进场、实际扣款和金额之间未形成完整证据链。",
                "补交" + "、".join(missing) + "。",
            )
        )

    items = list(contract.get("contract_items") or []) if contract else []
    stores = list(contract.get("contract_stores") or []) if contract else []
    contract_total = _decimal(contract.get("contract_total_amount"), label="合同总额") if contract else None
    contract_pass = bool(contract) and all(
        (
            contract.get("signed_visible") == "visible",
            contract.get("party_a_seal_visible") == "visible",
            contract.get("party_b_seal_visible") == "visible",
            bool(contract.get("party_a")),
            bool(contract.get("party_b")),
            bool(contract.get("terminal_system")),
            bool(items),
            bool(stores),
            contract_total is not None,
            bool(contract.get("payment_method")),
            contract.get("actual_shelving_only_visible") == "visible",
            contract.get("shelf_photos_required_visible") == "visible",
            contract.get("system_deduction_proof_required_visible") == "visible",
        )
    )
    _control(
        controls,
        "contract_authority",
        contract_pass,
        (
            f"合同双方签章，列示{len(items)}个商品条码、{len(stores)}家门店并明确实际上架和扣款凭证要求"
            if contract_pass
            else "合同缺失，或双方签章、渠道、商品、门店、金额、支付/上架/扣款条款不完整"
        ),
    )
    if not contract_pass:
        issues.append(
            _issue(
                "contract_authority_invalid",
                "进场费合同权威条款不完整",
                _sources(contracts),
                controls[-1]["basis"],
                "双方签章合同须明确双方、渠道系统、商品条码、合同门店、条码费、总额、支付方式及实际上架和扣款凭证要求。",
                "无法确认费用授权、适用范围和支付条件。",
                "补交上述条款完整、双方签章清楚的进场费合同。",
            )
        )

    item_fees = [_decimal(item.get("barcode_fee"), label="合同条码费") for item in items]
    store_counts = [item.get("contracted_store_count") for item in items]
    fee_total = _round(sum((value or Decimal("0") for value in item_fees), Decimal("0")))
    amount_pass = (
        bool(items)
        and contract_total is not None
        and all(value is not None for value in item_fees)
        and all(int(value) == len(stores) for value in store_counts if value is not None)
        and len([value for value in store_counts if value is not None]) == len(items)
        and abs(fee_total - contract_total) <= CENT
    )
    amount_basis = (
        f"合同{len(items)}个商品条码，条码费逐行合计{fee_total}元，合同总额{contract_total}元；"
        f"每行门店数={store_counts}，该门店数仅核对覆盖范围、不重复乘入条码费"
    )
    _control(controls, "contract_amount", amount_pass, amount_basis)
    if not amount_pass:
        issues.append(
            _issue(
                "contract_amount_failed",
                "合同条码费无法按商品行闭环复算",
                _sources(contracts),
                amount_basis,
                "各商品条码费合计须等于合同总额；除非合同明确按店计费，门店数量不得再次乘入条码费。",
                "申报金额的合同上限无法确定。",
                "补正合同商品行、条码费、门店数或合同总额，使其可直接复算。",
            )
        )

    photo_path_by_name = {
        Path(item["path"]).name: Path(item["path"])
        for item in case.get("document_roles") or []
        if str(item.get("role")) == "shelf_photo"
    }
    hash_groups: dict[str, list[str]] = {}
    for source_name, source_path in photo_path_by_name.items():
        hash_groups.setdefault(sha256_file(source_path), []).append(source_name)
    duplicate_groups = [names for names in hash_groups.values() if len(names) > 1]
    source_integrity_pass = not duplicate_groups and len(photo_path_by_name) == len(photos)
    _control(
        controls,
        "source_integrity",
        source_integrity_pass,
        f"照片清单{len(photo_path_by_name)}份，视觉返回{len(photos)}份，重复内容组{len(duplicate_groups)}组",
    )
    if not source_integrity_pass:
        issues.append(
            _issue(
                "source_integrity_failed",
                "上架照片来源不完整或存在重复内容",
                [name for group in duplicate_groups for name in group],
                controls[-1]["basis"],
                "每份原始照片须唯一返回且不得用同一图片重复覆盖多个门店或商品。",
                "上架覆盖范围不能可靠确认。",
                "重交不重复、来源清楚且逐门店可识别的原始照片。",
            )
        )

    agreement_date = _date(contract.get("agreement_date")) if contract else None
    agreement_year = contract.get("agreement_year") if contract else None
    photo_records: list[dict[str, Any]] = []
    stores_with_valid_photo: set[int] = set()
    product_store_coverage: dict[int, set[int]] = {index: set() for index in range(len(items))}
    unmatched_sources: list[str] = []
    invalid_watermark_sources: list[str] = []
    for photo in photos:
        source = str(photo.get("source_file") or "")
        photo_date = _date(photo.get("photo_date"))
        date_ok = photo_date is not None
        if agreement_date is not None and photo_date is not None:
            date_ok = date_ok and photo_date >= agreement_date
        if agreement_year is not None and photo_date is not None:
            date_ok = date_ok and photo_date.year == int(agreement_year)
        visual_ok = all(
            photo.get(field) == "visible"
            for field in (
                "watermark_date_visible",
                "watermark_time_visible",
                "watermark_address_visible",
                "shelf_display_visible",
            )
        ) and bool(photo.get("photo_time")) and bool(photo.get("photo_location")) and date_ok
        store_index, store_match_score, store_basis = _match_store(photo, stores)
        if not visual_ok:
            invalid_watermark_sources.append(source)
        if store_index is None:
            unmatched_sources.append(source)
        if visual_ok and store_index is not None:
            stores_with_valid_photo.add(store_index)
            for visible_product in photo.get("visible_products") or []:
                product_index, _ = _match_product(visible_product, items)
                if product_index is not None:
                    product_store_coverage[product_index].add(store_index)
        photo_records.append(
            {
                "source_file": source,
                "visual_status": "pass" if visual_ok else "fail",
                "matched_store": stores[store_index].get("store_name") if store_index is not None else None,
                "store_match_score": round(store_match_score, 4),
                "store_match_basis": store_basis,
                "visible_product_count": len(photo.get("visible_products") or []),
            }
        )

    missing_store_names = [
        str(store.get("store_name") or f"合同第{index + 1}家门店")
        for index, store in enumerate(stores)
        if index not in stores_with_valid_photo
    ]
    complete_product_indexes = [
        index for index, coverage in product_store_coverage.items()
        if len(stores) > 0 and len(coverage) == len(stores)
    ]
    photo_pass = (
        bool(photos)
        and bool(stores)
        and bool(items)
        and not invalid_watermark_sources
        and not unmatched_sources
        and not missing_store_names
        and len(complete_product_indexes) == len(items)
    )
    coverage_basis = (
        f"合同{len(stores)}家门店、{len(items)}个商品条码；提交{len(photos)}张照片，"
        f"有效水印门店{len(stores_with_valid_photo)}家，缺失门店{len(missing_store_names)}家，"
        f"无法对应照片{len(unmatched_sources)}张，水印/货架不完整{len(invalid_watermark_sources)}张，"
        f"覆盖全部合同门店的商品条码{len(complete_product_indexes)}个"
    )
    _control(controls, "shelf_photo_coverage", photo_pass, coverage_basis)
    if not photo_pass:
        observed_parts = [coverage_basis]
        if missing_store_names:
            observed_parts.append("缺失合同门店：" + "、".join(missing_store_names))
        issues.append(
            _issue(
                "shelf_photo_coverage_failed",
                "上架照片未完整覆盖合同门店和商品",
                [*unmatched_sources, *invalid_watermark_sources],
                "；".join(observed_parts),
                "照片自身须显示合同期内日期、时间、地址/门店水印和货架商品；每个合同商品须在全部合同门店完成可核验上架。",
                "合同约定未全部上架只支持实际上架部分，当前不能证明全部条码费已满足条件。",
                "按缺失合同门店和商品补交水印清楚、能辨认商品的原始上架照片，并说明非同名门店的对应关系。",
            )
        )

    valid_proofs = [
        proof for proof in deduction_proofs
        if proof.get("deduction_proof_visible") == "visible"
        and proof.get("deduction_amount") is not None
        and bool(proof.get("deduction_subject"))
    ]
    proof_amounts = [
        _decimal(proof.get("deduction_amount"), label="系统扣款金额")
        for proof in valid_proofs
    ]
    deduction_total = _round(sum((value or Decimal("0") for value in proof_amounts), Decimal("0")))
    deduction_pass = bool(valid_proofs) and contract_total is not None and deduction_total > Decimal("0")
    _control(
        controls,
        "system_deduction_proof",
        deduction_pass,
        f"提交系统扣款凭证{len(deduction_proofs)}份，有效{len(valid_proofs)}份，可识别扣款合计{deduction_total}元",
    )
    if not deduction_pass:
        issues.append(
            _issue(
                "system_deduction_proof_missing",
                "缺少合同要求的系统扣款凭证",
                _sources(deduction_proofs),
                controls[-1]["basis"],
                "合同明确要求回传系统扣款凭证；凭证须清楚显示扣款主体、项目/渠道、日期和金额。",
                "无法确认条码上架费用已由渠道系统实际扣除。",
                "补交与本合同及家得福系统对应、主体和金额清楚的系统扣款凭证。",
            )
        )

    covered_fee = _round(
        sum((item_fees[index] or Decimal("0") for index in complete_product_indexes), Decimal("0"))
    )
    claimed = contract_total or Decimal("0")
    blocked = any(item["status"] != "pass" for item in controls)
    supported = (
        Decimal("0")
        if blocked
        else min(claimed, covered_fee, deduction_total)
    )
    held = max(claimed - supported, Decimal("0"))
    exceptions = [
        {
            "severity": item["severity"],
            "code": item["code"],
            "message": item["title"],
            "impact": item["impact"],
            "suggestion": item["resubmission"],
            "source": "、".join(item.get("source_files") or []),
        }
        for item in issues
    ]
    return {
        "schema_version": "2.1",
        "generated_at": now_utc(),
        "scenario": "entry_fee",
        "case_id": Path(case["source_archive"]).stem,
        "case_name": "进场费/条码费核销",
        "summary": {
            "conclusion": "human_review" if blocked else "pass",
            "decision_label": "资料需补正" if blocked else "可核销",
            "claimed_amount": json_number(claimed),
            "suggested_approved_amount": json_number(supported),
            "temporarily_held_amount": json_number(held),
            "high_exception_count": len(exceptions),
            "medium_exception_count": 0,
            "error_group_count": len(issues),
            "contract_product_count": len(items),
            "contract_store_count": len(stores),
            "photo_count": len(photos),
            "complete_product_count": len(complete_product_indexes),
            "contract_total_amount": json_number(contract_total) if contract_total is not None else None,
            "shelf_supported_amount": json_number(covered_fee),
            "system_deduction_amount": json_number(deduction_total),
        },
        "exceptions": exceptions,
        "entry_fee_audit": {
            "controls": controls,
            "issues": issues,
            "contract_items": items,
            "contract_stores": stores,
            "photo_reconciliation": photo_records,
            "complete_product_indexes": [index + 1 for index in complete_product_indexes],
            "missing_store_names": missing_store_names,
            "duplicate_photo_groups": duplicate_groups,
            "calculation_basis": (
                f"合同条码费合计{fee_total}元；全部门店上架完整的商品条码费{covered_fee}元；"
                f"系统扣款凭证金额{deduction_total}元；任一阻断控制失败则支持0元"
            ),
        },
    }
