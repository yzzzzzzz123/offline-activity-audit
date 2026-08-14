from __future__ import annotations

from pathlib import Path
from typing import Any


DIMENSION_SPECS = [
    (
        "1",
        "真实性验证",
        "文件是否被篡改/伪造",
        "EXIF信息完整性、图像指纹比对、水印一致性、数字签名验证",
    ),
    (
        "2",
        "真实性验证",
        "照片是否为现场实拍",
        "GPS坐标与申报门店匹配、拍摄时间在活动周期内、设备信息一致",
    ),
    (
        "3",
        "真实性验证",
        "文件是否重复使用",
        "感知哈希（pHash）去重、跨活动文件复用检测",
    ),
    (
        "4",
        "合规性审查",
        "文件格式是否符合要求",
        "文件类型、分辨率、大小是否在允许范围内",
    ),
    (
        "5",
        "合规性审查",
        "费用金额是否合理",
        "费用明细金额与活动预算比较，偏差超过30%标记异常",
    ),
    (
        "6",
        "合规性审查",
        "票据信息是否完整",
        "发票代码、号码、金额、日期、购销方信息完整性",
    ),
    (
        "7",
        "合规性审查",
        "合同条款是否匹配",
        "合同约定金额不低于发票总额，付款周期与开票日期一致",
    ),
]


def _file_name(value: Any) -> str:
    return Path(str(value or "")).name


def _scenario_manifest(manifest: dict[str, Any] | None, scenario: str) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        return {}
    extractions = manifest.get("extractions")
    if not isinstance(extractions, dict):
        return {}
    value = extractions.get(scenario)
    return value if isinstance(value, dict) else {}


def _manifest_file_records(
    manifest: dict[str, Any] | None,
    scenario: str,
) -> list[dict[str, Any]]:
    value = _scenario_manifest(manifest, scenario).get("files")
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _duplicate_hash_count(records: list[dict[str, Any]]) -> int:
    hashes = [str(item.get("sha256") or "") for item in records]
    return len(hashes) - len(set(value for value in hashes if value))


def _personnel_rows(
    result: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    summary = result["summary"]
    settlement = result["settlement"]
    sales = result["sales"]
    transfers = result.get("transfer_evidence") or []
    images = result.get("image_inventory") or {}
    image_files = images.get("files") or []
    image_exact_groups = len(images.get("exact_duplicate_groups") or [])
    image_phash_candidates = len(images.get("phash_candidate_pairs") or [])
    image_exif_count = int(images.get("files_with_exif_datetime") or 0)
    image_gps_count = int(images.get("files_with_gps") or 0)
    records = _manifest_file_records(manifest, "personnel_incentive")
    transfer_files = sorted(
        {
            _file_name(file_name)
            for item in transfers
            for file_name in (item.get("source_files") or [])
            if file_name
        }
    )
    file_scope = (
        f"{_file_name(sales.get('source_file'))}；"
        f"{_file_name(settlement.get('source_file'))}；"
        f"转账截图{len(transfer_files) or 2}张"
    )
    identity_visible = sum(bool(item.get("identity_visible")) for item in transfers)
    date_visible = sum(bool(item.get("event_date_visible")) for item in transfers)
    duplicate_hashes = _duplicate_hash_count(records)
    claimed = float(summary["claimed_amount"])
    line_total = float(summary["settlement_line_reward"])
    amount_difference = claimed - line_total
    difference_rate = abs(amount_difference) / line_total if line_total else None
    rate_text = f"{difference_rate:.2%}" if difference_rate is not None else "无法计算"

    values = [
        (
            file_scope,
            "固定全部源文件SHA-256；读取结算单和转账截图可见内容；核对Excel公式与数值。",
            "证据不足",
            f"本包{len(records) or 4}个源文件已固定哈希；{len(image_files)}张图片中EXIF时间{image_exif_count}张、GPS {image_gps_count}张；但未提供数字签名、可信时间戳或像素级篡改检测结果。",
            "SHA-256只证明收到后的字节身份，不能证明提交前从未被修改；需原始文件、数字签名或专业取证结果。",
            "人员激励-图片文件检查",
        ),
        (
            "本场景没有现场陈列照片；结算单和转账截图",
            "检查转账截图中的身份、日期等可见字段，不把文件名或金额当作现场证明。",
            "本案未触发",
            f"{len(transfers)}笔去重转账中，身份可见{identity_visible}笔、日期可见{date_visible}笔；材料不是门店现场实拍照片。",
            "若人员激励制度要求现场执行照片，应另行提交带可信时间、地点和设备信息的原始照片。",
            "人员激励-转账凭证",
        ),
        (
            file_scope,
            "比较图片SHA-256并计算pHash近似指纹；按转账事件合并同一笔的发送/接收画面。",
            "部分验证",
            f"源文件哈希重复数为{duplicate_hashes}，图片完全重复组{image_exact_groups}、pHash近似候选{image_phash_candidates}组；转账画面按业务事件去重后为{len(transfers)}笔。pHash候选需结合transfer_id判断，同一聊天内容的多张截图可能合理近似；没有跨活动历史库。",
            "pHash候选只提示画面近似，不能直接判为重复造假；本次不能排除历史活动复用。",
            "人员激励-图片文件检查",
        ),
        (
            file_scope,
            "ZIP入口校验文件可读性、单文件大小和安全解压；Excel与图片均已成功读取。",
            "部分验证",
            f"收到{len(records) or 4}个文件，销售Excel有效明细{summary['excel_detail_row_count']}行；但业务未提供允许的图片分辨率和文件大小阈值。",
            "可读取不等于完全符合格式制度；需补充允许类型、最低分辨率和大小范围。",
            "文件读取清单",
        ),
        (
            f"{_file_name(settlement.get('source_file'))}；{_file_name(sales.get('source_file'))}；转账截图",
            "逐SKU计算数量×奖励单价，并比较结算行总额、转账总额和申报金额。",
            "部分验证",
            f"10条SKU计算及结算行总额均为{line_total:.2f}元，去重转账总额为{float(summary['transfer_total']):.2f}元；申报{claimed:.2f}元，差额{amount_difference:.2f}元（{rate_text}）。未提供独立活动预算。",
            "30%预算规则不能在没有预算表时判定；现有结果只证明结算、销售和转账之间的勾稽关系。",
            "人员激励-SKU逐项",
        ),
        (
            "本包未提交发票",
            "盘点文件类型，未把结算单或转账截图误认作发票。",
            "本案未触发",
            "人员激励包包含销售Excel、结算单和转账截图，没有发票代码、号码、购销方等可核字段。",
            "如公司制度规定人员激励必须附发票，应改判为证据不足并要求补交发票。",
            "文件读取清单",
        ),
        (
            "本包未提交合同；使用最终结算单及转账证明",
            "检查活动期间、SKU数量、奖励单价、结算金额和转账金额；没有把结算单等同于合同。",
            "本案未触发",
            f"可验证{settlement.get('activity_start')}至{settlement.get('activity_end')}的结算口径，但无合同金额、付款周期或开票日期可比。",
            "如人员激励需要合同约束，应补充合同、付款周期及相应票据后再判断合同条款。",
            "人员激励-SKU逐项",
        ),
    ]
    return _apply_specs("人员激励", values)


def _display_rows(
    result: dict[str, Any],
    manifest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    summary = result["summary"]
    contract = result["contract"]
    sales = result["sales"]
    photos = result["photo_inventory"]
    stores = result["store_reconciliation"]
    records = _manifest_file_records(manifest, "promotional_display")
    photo_files = photos.get("files") or []
    widths = [int(item["width"]) for item in photo_files if item.get("width")]
    heights = [int(item["height"]) for item in photo_files if item.get("height")]
    sizes = [int(item["size"]) for item in photo_files if item.get("size")]
    dimension_text = (
        f"宽{min(widths)}–{max(widths)}像素、高{min(heights)}–{max(heights)}像素，"
        f"大小{min(sizes)}–{max(sizes)}字节"
        if widths and heights and sizes
        else "照片尺寸或大小未完整取得"
    )
    exact_groups = len(photos.get("exact_duplicate_groups") or [])
    similar_groups = len(photos.get("phash_candidate_pairs") or [])
    exif_count = int(photos.get("files_with_exif_datetime") or 0)
    gps_count = int(photos.get("files_with_gps") or 0)
    passed = int(summary["passed_store_count"])
    total_stores = int(summary["contract_store_count"])
    period_pass = sum(item.get("period_match") == "match" for item in stores)
    store_pass = sum(item.get("store_match") in {"exact", "compatible"} for item in stores)
    display_pass = sum(item.get("display_match") == "pass" for item in stores)
    expected = float(summary["expected_contract_amount"])
    claimed = float(summary["claimed_amount"])
    contract_difference = claimed - expected
    contract_rate = abs(contract_difference) / expected if expected else None
    rate_text = f"{contract_rate:.2%}" if contract_rate is not None else "无法计算"
    file_scope = (
        f"{_file_name(contract.get('source_file'))}；"
        f"{_file_name(sales.get('source_file'))}；现场照片{len(photo_files)}张"
    )

    values = [
        (
            file_scope,
            "固定源文件SHA-256；对照片计算dHash；读取EXIF、画面日期地点水印，并逐店检查水印与合同门店的一致性。",
            "部分验证",
            f"{len(photo_files)}张照片中EXIF时间{exif_count}张、GPS {gps_count}张；完全重复组{exact_groups}、pHash近似候选{similar_groups}组。未提供数字签名或像素级篡改鉴定。",
            "画面水印是可见线索，不是独立可信定位；哈希不能证明提交前未修改。需原始照片、可信时间/GPS或专业取证。",
            "堆头-照片文件检查",
        ),
        (
            f"合同{total_stores}家门店与{len(photo_files)}张现场照片",
            "逐店读取画面可见日期和地点，比较活动期间、合同门店名称/地点及陈列标准；不从文件名推断日期或GPS。",
            "部分通过",
            f"活动期匹配{period_pass}/{total_stores}家，门店地点匹配或兼容{store_pass}/{total_stores}家，陈列可确认{display_pass}/{total_stores}家；综合{passed}/{total_stores}家通过、{total_stores-passed}家补证。GPS与设备信息均不可用。",
            "6家存在日期、地点或陈列证据缺口；需原始照片、可信定位/时间或正式门店地址映射。",
            "堆头-逐店核验",
        ),
        (
            f"现场照片{len(photo_files)}张",
            "用SHA-256查完全重复，用dHash筛查视觉相同候选，并检查同一照片是否绑定多个合同门店。",
            "部分验证",
            f"本包完全重复组{exact_groups}、pHash近似候选{similar_groups}组；没有跨活动历史指纹库。",
            "“本包未发现重复”不等于“历史活动从未使用”；跨活动复用必须接入历史SHA/pHash库。",
            "堆头-照片文件检查",
        ),
        (
            file_scope,
            "确认PDF、XLSX、JPG均可打开；记录页数、工作表、照片分辨率和文件大小；ZIP入口执行安全大小限制。",
            "部分验证",
            f"收到{len(records) or len(photo_files) + 2}个文件：合同PDF 1份、销售Excel 1份、JPG {len(photo_files)}张；照片{dimension_text}。业务未提供允许阈值。",
            "已证明可读性和实际尺寸，不能在没有最低分辨率/大小标准时宣称完全合规。",
            "文件读取清单",
        ),
        (
            f"{_file_name(contract.get('source_file'))}；申报金额",
            "以本次唯一可得的合同约定金额作为合同口径，比较申报金额；另按逐店证据计算支持金额。",
            "部分验证",
            f"合同口径{expected:.2f}元，申报{claimed:.2f}元，差额{contract_difference:.2f}元（{rate_text}，未超过30%）；但独立活动预算表未提交。逐店证据仅支持{float(summary['supported_amount']):.2f}元。",
            "合同金额是现有口径，不等同于独立预算验证；若制度要求30%预算规则，需补充批准预算。",
            "核销总览",
        ),
        (
            "本包未提交发票",
            "盘点文件类型，未把合同、销售Excel或现场照片误认作发票。",
            "本案未触发",
            "没有发票代码、号码、金额、日期和购销方字段可检查。",
            "如堆头付款制度要求发票，应改判为证据不足并补交发票。",
            "文件读取清单",
        ),
        (
            f"{_file_name(contract.get('source_file'))}；销售Excel；逐店照片",
            "读取合同活动期、20家门店、单店1000元、陈列标准和总金额，并逐店比较照片；同时检查销售期间。",
            "部分通过",
            f"合同{total_stores}家×{float(summary['fee_per_store']):.2f}元={expected:.2f}元与申报一致；逐店证据{passed}家通过、{total_stores-passed}家补证。未提交发票，合同付款周期与开票日期无法比较。",
            f"合同执行条款只完成部分验证；{total_stores-passed}家补证，付款周期/开票一致性需在有发票后再检查。",
            "堆头-逐店核验",
        ),
    ]
    return _apply_specs("堆头", values)


def _apply_specs(
    scenario_label: str,
    values: list[tuple[str, str, str, str, str, str]],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for spec, value in zip(DIMENSION_SPECS, values, strict=True):
        number, dimension, content, standard = spec
        files, executed, status, basis, limitation, detail_sheet = value
        rows.append(
            {
                "dimension_no": number,
                "analysis_dimension": dimension,
                "check_content": content,
                "proposal_standard": standard,
                "scenario": scenario_label,
                "files": files,
                "executed_check": executed,
                "status": status,
                "basis": basis,
                "limitation": limitation,
                "detail_sheet": detail_sheet,
            }
        )
    return rows


def build_analysis_dimension_rows(
    results: list[dict[str, Any]],
    manifest: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    by_scenario = {str(item.get("scenario")): item for item in results}
    scenario_rows: dict[str, list[dict[str, Any]]] = {}
    if "personnel_incentive" in by_scenario:
        scenario_rows["personnel_incentive"] = _personnel_rows(
            by_scenario["personnel_incentive"], manifest
        )
    if "promotional_display" in by_scenario:
        scenario_rows["promotional_display"] = _display_rows(
            by_scenario["promotional_display"], manifest
        )

    rows: list[dict[str, Any]] = []
    for index in range(len(DIMENSION_SPECS)):
        for scenario in ("personnel_incentive", "promotional_display"):
            if scenario in scenario_rows:
                rows.append(scenario_rows[scenario][index])
    return rows
