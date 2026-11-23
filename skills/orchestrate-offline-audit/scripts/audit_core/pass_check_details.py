"""Project saved verification facts onto passed cards; never make new decisions."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re


def _read(path: Path) -> dict:
    try:
        if path.is_symlink():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def load_packet_observations(workspace: Path, packets: list[dict]) -> list[dict]:
    """Exact archive identity and known filenames only; the sealed files stay untouched."""
    directory = workspace / "analysis" / "model-observations"
    if directory.is_symlink():
        return packets
    for packet in packets:
        archive = packet.get("archive_id", "")
        if not isinstance(archive, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,48}", archive):
            continue
        observations = {}
        for prefix in ("pos-product-identity", "photo-product-selection"):
            value = _read(directory / f"{prefix}-{archive}.json")
            if value:
                observations[prefix] = value
        if observations:
            packet["_presentation_observations"] = observations
    return packets


def _source(packet: dict, uid: str) -> str:
    units = [u for u in packet.get("units", []) if isinstance(u, dict) and u.get("unit_id") == uid]
    if len(units) != 1:
        return ""
    unit = units[0]
    name = str(unit.get("source_file") or "").replace("\\", "/").rsplit("/", 1)[-1]
    return " · ".join(str(v) for v in (name, unit.get("locator")) if v)


def enrich_pdf_item(item: dict, row: dict, packet: dict | None) -> None:
    values = row.get("values") or []
    if len(values) > 2 and isinstance(values[2], str):
        item["requirement"] = values[2]
    if not packet:
        return
    observations = packet.get("_presentation_observations") or {}
    if item.get("control_id") == "pos_product_identity":
        verified = (observations.get("pos-product-identity") or {}).get("verification") or {}
        rows = verified.get("rows") or []
        # Only an explicitly passed complete verification can substantiate a passed card.
        if not verified.get("applicable") or verified.get("limitations") or not rows or any(r.get("status") != "pass" for r in rows):
            return
        details = []
        for result in rows:
            location = _source(packet, result.get("unit_id"))
            submitted = result.get("submitted") or {}
            references = result.get("references") or ([result["reference"]] if result.get("reference") else [])
            if not location or not submitted.get("barcode_69") or not references or any(p.get("barcode_69") != submitted["barcode_69"] for p in references):
                return
            details.append({"subject": location, "observed": f"{submitted.get('product_name') or ''}；69码 {submitted['barcode_69']}",
                            "compared": "；".join(f"{p.get('product_code') or ''} / {p.get('product_name') or ''} / 69码 {p.get('barcode_69') or ''}" for p in references),
                            "result": f"69码一致；保留全部 {len(references)} 条同码商品记录"})
        item["verification_method"] = "逐行读取本次 POS Excel 的原始名称和完整69码，按69码精确查询本次商品数据库快照；同码的内部商品记录全部保留，经销商名称或包装描述差异不另判不符。"
        item["verification_details"] = details
    else:
        selection = observations.get("photo-product-selection") or {}
        if item.get("control_id") not in (selection.get("rule_ids") or []):
            return
        references = [r for r in selection.get("references", []) if isinstance(r, dict) and not r.get("unavailable") and r.get("image") and r.get("view_id")]
        if not references:
            return
        item["verification_method"] = "先按本次合同或协议中的商品标识定位商品，再读取该商品对应的 OSS 多视图参考图片，供本次现场照片核对。参考图用于辨认商品，不能证明现场门店、日期、数量或费用。"
        item["verification_details"] = [{"subject": r.get("product_name") or r["product_code"],
                                         "observed": f"商品编码 {r['product_code']}；69码 {r.get('barcode_69') or '记录未包含'}",
                                         "compared": f"OSS参考视图 {r['view_id']}；输入图片 {r['image']}",
                                         "result": "已作为本次参考图输入；现场是否符合以本项已保存结论为准"} for r in references]


def passed_photo_subchecks(sheets: list[dict], packets: list[dict]) -> list[dict]:
    """A failed photo may still contain individually validated passed subchecks."""
    items = []
    for sheet in sheets:
        matches = [p for p in packets if p.get("archive_id") == sheet.get("archive_id") and (p.get("result") or {}).get("scenario") == sheet.get("scenario")]
        if len(matches) != 1:
            continue
        packet = matches[0]
        by_id = {r.get("photo_unit_id"): r for r in sheet.get("rows", []) if r.get("photo_unit_id")}
        for review in (packet.get("result") or {}).get("photo_reviews", []):
            row = by_id.get(review.get("unit_id"))
            if not row:
                continue
            # Replace aggregated passed cards with individual check facts too.
            for check in review.get("checks", []):
                if check.get("status") != "pass" or not check.get("reason"):
                    continue
                location = _source(packet, review.get("unit_id"))
                if not location:
                    continue
                from .pdf_workflow import CHECK_CATEGORIES
                category = CHECK_CATEGORIES.get(check.get("rule_id"))
                if not category:
                    continue
                item = {"category": category, "title": category + "核验通过", "subject": location,
                        "basis": check["reason"], "control_id": check["rule_id"], "archive_id": packet["archive_id"],
                        "scope": "store", "confidence": "high", "source_files": deepcopy((row.get("card_evidence") or {}).get("source_files") or []),
                        "evidence": deepcopy(row.get("card_evidence") or {}), "requirement": check.get("requirement") or "",
                        "verification_method": "按本张照片逐项核对，只将明确通过的子项列在此处；本图其他不符合或无法确认的子项仍保留在错误总览。"}
                enrich_pdf_item(item, {"values": []}, packet)
                items.append(item)
    return items
