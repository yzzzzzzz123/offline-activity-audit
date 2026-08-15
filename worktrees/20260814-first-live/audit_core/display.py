from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from .common import (
    AuditError,
    exception,
    json_number,
    money,
    normalize_text,
    now_utc,
    resolve_case_file,
    unique_by,
)
from .excel_sources import image_inventory, pdf_inventory, read_display_sales


PASS_STORE_MATCHES = {"exact", "compatible"}


def _photo_path(photo_dir: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = photo_dir / candidate
    return candidate.resolve()


def audit_display_case(
    case_path: str | Path,
    case: dict[str, Any],
    evidence: dict[str, Any],
) -> dict[str, Any]:
    sales_path = resolve_case_file(case_path, case, "sales_excel")
    photo_dir = resolve_case_file(case_path, case, "photo_dir")
    contract_path = resolve_case_file(case_path, case, "contract_pdf")
    if sales_path is None or photo_dir is None or contract_path is None:
        raise AuditError("Promotional display case requires sales_excel, photo_dir, and contract_pdf")
    sales = read_display_sales(sales_path)
    photos = image_inventory(photo_dir)
    pdf = pdf_inventory(contract_path)
    contract = evidence["contract"]
    contract_stores = sorted(contract["stores"], key=lambda item: int(item["line_no"]))
    review_rows = evidence["photo_reviews"]
    store_map = unique_by(contract_stores, "line_no", "contract store line number")
    review_map = unique_by(review_rows, "store_line_no", "photo review store line number")
    exceptions: list[dict[str, Any]] = []
    fee = money(contract["fee_per_store"], label="fee per store")
    claimed = money(contract["claimed_amount"], label="claimed amount")
    start = date.fromisoformat(contract["activity_start"])
    end = date.fromisoformat(contract["activity_end"])
    inventory_names = {item["file_name"] for item in photos["files"]}
    store_results: list[dict[str, Any]] = []
    referenced_files: list[str] = []

    for line_no, store in store_map.items():
        review = review_map.get(line_no)
        if review is None:
            review = {
                "store_line_no": line_no,
                "contract_store_name": store["store_name"],
                "photo_files": [],
                "visible_date": None,
                "visible_location": None,
                "period_match": "unverifiable",
                "store_match": "filename_only",
                "display_match": "uncertain",
                "duplicate_check": "unverifiable",
                "mapping_basis": None,
                "risk_notes": ["未提交该合同门店的结构化照片核验结果"],
                "supplement_advice": ["补充带门头、日期地点水印和完整陈列的照片"],
            }
        contract_name_match = normalize_text(review["contract_store_name"]) == normalize_text(store["store_name"])
        if not contract_name_match:
            exceptions.append(
                exception(
                    "high",
                    "PHOTO_REVIEW_CONTRACT_STORE_CONFLICT",
                    f"合同第{line_no}家门店名称与照片核验行中的合同门店名称不一致。",
                    "照片可能绑定到错误门店。",
                    "按合同原始序号重新绑定照片。",
                    source=f"contract store line {line_no}",
                )
            )

        photo_files = list(review.get("photo_files") or [])
        referenced_files.extend(photo_files)
        missing_files: list[str] = []
        for value in photo_files:
            path = _photo_path(photo_dir, value)
            if path.name not in inventory_names or not path.exists():
                missing_files.append(value)
        visible_date = review.get("visible_date")
        date_inside = False
        if visible_date:
            try:
                date_inside = start <= date.fromisoformat(visible_date) <= end
            except ValueError:
                date_inside = False
        period_pass = review.get("period_match") == "match" and date_inside
        store_pass = review.get("store_match") in PASS_STORE_MATCHES
        display_pass = review.get("display_match") == "pass"
        duplicate_pass = review.get("duplicate_check") == "none"
        file_pass = bool(photo_files) and not missing_files
        passed = all([file_pass, period_pass, store_pass, display_pass, duplicate_pass])
        status = "pass" if passed else "supplement"
        supported = fee if passed else Decimal("0")
        risk_notes = list(review.get("risk_notes") or [])
        supplement = list(review.get("supplement_advice") or [])
        if missing_files:
            risk_notes.append("结构化核验引用了不存在的照片文件：" + "、".join(missing_files))
            supplement.append("补齐缺失原始照片文件")
        store_results.append(
            {
                "store_line_no": line_no,
                "contract_store_name": store["store_name"],
                "photo_files": photo_files,
                "photo_count": len(photo_files),
                "visible_date": visible_date,
                "visible_location": review.get("visible_location"),
                "period_match": review.get("period_match"),
                "store_match": review.get("store_match"),
                "display_match": review.get("display_match"),
                "duplicate_check": review.get("duplicate_check"),
                "mapping_basis": review.get("mapping_basis"),
                "status": status,
                "supported_amount": json_number(supported),
                "risk_notes": risk_notes,
                "supplement_advice": supplement,
            }
        )
        if not passed:
            failed_controls: list[str] = []
            if not file_pass:
                failed_controls.append("照片缺失")
            if not period_pass:
                failed_controls.append("期间未通过")
            if not store_pass:
                failed_controls.append("门店未匹配")
            if not display_pass:
                failed_controls.append("陈列标准未通过")
            if not duplicate_pass:
                failed_controls.append("重复检查未通过")
            exceptions.append(
                exception(
                    "high",
                    "STORE_DISPLAY_EVIDENCE_INCOMPLETE",
                    f"合同第{line_no}家“{store['store_name']}”未通过：{'、'.join(failed_controls)}。",
                    f"暂不支持该店{fee}元费用。",
                    "；".join(supplement) or "补充满足合同标准的完整证据。",
                    source="、".join(photo_files) or f"contract store line {line_no}",
                )
            )

    unknown_reviews = sorted(set(review_map) - set(store_map))
    if unknown_reviews:
        exceptions.append(
            exception(
                "high",
                "PHOTO_REVIEW_UNKNOWN_CONTRACT_LINE",
                f"照片核验包含合同不存在的门店序号：{unknown_reviews}。",
                "可能存在错绑或多报门店。",
                "按合同门店清单重新编号。",
            )
        )

    unreferenced_photos = sorted(inventory_names - {Path(value).name for value in referenced_files})
    if unreferenced_photos:
        exceptions.append(
            exception(
                "medium",
                "UNREFERENCED_PHOTO_FILES",
                f"有{len(unreferenced_photos)}张照片未绑定到任何合同门店。",
                "材料可能遗漏核验或重复提交。",
                "确认照片用途并绑定合同门店或从核销包中移除。",
                source="、".join(unreferenced_photos),
            )
        )
    if photos["exact_duplicate_groups"]:
        exceptions.append(
            exception(
                "high",
                "EXACT_DUPLICATE_PHOTOS",
                f"发现{len(photos['exact_duplicate_groups'])}组SHA-256完全重复照片。",
                "可能存在重复核销或跨门店复用。",
                "核对重复照片对应的门店与原始文件。",
                source=str(photos["exact_duplicate_groups"]),
            )
        )
    if photos["files_with_exif_datetime"] == 0 or photos["files_with_gps"] == 0:
        exceptions.append(
            exception(
                "medium",
                "PHOTO_ORIGINAL_METADATA_ABSENT",
                "照片缺少可用的EXIF拍摄时间或GPS，主要依赖画面水印。",
                "无法从原始文件元数据独立验证拍摄时空。",
                "保留原始拍摄文件，或补充平台定位/签到记录。",
            )
        )
    if not sales["store_level_available"]:
        exceptions.append(
            exception(
                "medium",
                "SALES_DATA_NOT_STORE_LEVEL",
                "销售Excel为经销商汇总，不能拆分到合同中的各门店。",
                "销售数据只能证明活动期总体销售，不能独立证明逐店执行。",
                "如政策要求逐店销售佐证，补交门店级POS或出库明细。",
                source=sales["source_file"],
            )
        )
    if sales["external_formula_cells"]:
        exceptions.append(
            exception(
                "medium",
                "SALES_EXCEL_EXTERNAL_LINKS",
                f"销售Excel有{len(sales['external_formula_cells'])}个公式引用缺失的外部工作簿。",
                "部分单位或零售价无法在当前文件内独立重算。",
                "补交被引用主数据或将外链转为可追溯静态值。",
                source=",".join(sales["external_formula_cells"][:20]),
            )
        )

    supported = sum((money(item["supported_amount"]) for item in store_results), Decimal("0"))
    suggested = min(claimed, supported)
    passed_count = sum(item["status"] == "pass" for item in store_results)
    supplement_count = len(store_results) - passed_count
    if not store_results:
        conclusion = "human_review"
    elif supplement_count:
        conclusion = "partial_pass" if passed_count else "human_review"
    else:
        conclusion = "pass"
    high_count = sum(item["severity"] == "high" for item in exceptions)
    medium_count = sum(item["severity"] == "medium" for item in exceptions)
    expected_claim = fee * Decimal(len(contract_stores))
    if claimed != expected_claim:
        exceptions.append(
            exception(
                "high",
                "CONTRACT_CLAIM_CALCULATION_MISMATCH",
                f"申报金额{claimed}不等于{len(contract_stores)}家×{fee}元={expected_claim}元。",
                "合同门店数、单店标准或申报金额至少一项不一致。",
                "更正合同/申报明细。",
            )
        )
        high_count += 1

    return {
        "schema_version": "1.0",
        "generated_at": now_utc(),
        "scenario": "promotional_display",
        "case_id": str(case.get("case_id") or "promotional-display"),
        "case_name": str(case.get("case_name") or contract.get("customer_name") or "堆头核销"),
        "summary": {
            "conclusion": conclusion,
            "claimed_amount": json_number(claimed),
            "contract_store_count": len(contract_stores),
            "fee_per_store": json_number(fee),
            "expected_contract_amount": json_number(expected_claim),
            "passed_store_count": passed_count,
            "supplement_store_count": supplement_count,
            "supported_amount": json_number(supported),
            "suggested_approved_amount": json_number(suggested),
            "temporarily_held_amount": json_number(claimed - suggested),
            "photo_count": photos["file_count"],
            "sales_sku_count": sales["sku_count"],
            "sales_quantity": sales["total_quantity"],
            "sales_retail_amount": sales["retail_amount"],
            "high_exception_count": high_count,
            "medium_exception_count": medium_count,
        },
        "contract": contract,
        "contract_pdf": pdf,
        "sales": sales,
        "photo_inventory": photos,
        "store_reconciliation": store_results,
        "unreferenced_photos": unreferenced_photos,
        "exceptions": exceptions,
    }
