from __future__ import annotations

import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.common import AuditError, load_json, sha256_file, validate_json, write_json
from audit_core.product_rag import clear_product_rag_cache, load_product_rag
from ingest_product_reference import (
    CATALOG_PATH,
    PRODUCTS_ROOT,
    SCHEMA_PATH,
    SOURCE_COLLECTIONS,
    _acquire_lock,
    _catalog_product_directory,
    _release_lock,
)


API_ORIGIN = "https://api.canban.cn"
API_PATH = "/outapi/oms/product/sku/mapping"
SIGNATURE_METHOD = "HmacSHA256"
SIGNATURE_HEADER_NAMES = (
    "x-ca-key",
    "x-ca-nonce",
    "x-ca-signature-method",
    "x-ca-timestamp",
)
PRODUCT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
INVALID_FOLDER_ENDING = re.compile(r"[ .]$")
INTERNATIONAL_PATTERN = re.compile(
    r"国际|标签|越南|菲律宾|马来西亚|泰国|新加坡|哈萨克斯坦|香港|"
    r"韩国|日本|英国|\bTC\b|KKV",
    re.IGNORECASE,
)
CUSTOMER_PATTERN = re.compile(
    r"思家客|赛维泰|倍加洁|永辉|大润发|克劳丽|专供|定制",
    re.IGNORECASE,
)
LOGISTICS_PATTERN = re.compile(
    r"(?:24|36|48)箱规|带中箱|外箱|纸箱|裸支|卡箱",
    re.IGNORECASE,
)
UPGRADE_PATTERN = re.compile(r"升级版|升级款")
SPOKESPERSON_PATTERN = re.compile(r"代言人|华晨宇")
CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("mouthwash", re.compile(r"漱口水")),
    ("oral_spray", re.compile(r"口腔喷雾|口喷")),
    ("toothpaste", re.compile(r"牙膏")),
    ("toothbrush", re.compile(r"牙刷")),
    ("floss", re.compile(r"牙线棒|牙线")),
    ("tooth_powder", re.compile(r"洁牙粉|牙粉")),
    ("repellent", re.compile(r"花露水|驱蚊")),
)
GENERIC_NAME_PARTS = (
    "参半",
    "oralshark",
    "益生菌",
    "清新",
    "便携",
    "产品",
)
COLOR_CHARACTERS = frozenset("粉紫蓝黄绿白黑灰棕红橙青")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "按69码查询受控商品编码，生成一对多消歧计划，并在显式确认后原子迁移目录。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="查询目录中全部69码并保存无凭据响应")
    fetch.add_argument("--output", required=True)
    fetch.add_argument("--barcode", action="append", default=[])
    fetch.add_argument("--request-gap-ms", type=int, default=120)

    plan = subparsers.add_parser("plan", help="根据接口响应生成失败关闭的迁移计划")
    plan.add_argument("--mapping-file", required=True)
    plan.add_argument("--output", required=True)

    apply_parser = subparsers.add_parser("apply", help="应用已显式确认的迁移计划")
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument(
        "--approved-status",
        action="append",
        choices=("api_unique", "relevant_unique", "offline_unique", "metadata_unique"),
        default=[],
        help="允许自动应用的建议状态；可重复。explicit_approved 始终允许。",
    )
    apply_parser.add_argument(
        "--approve",
        action="append",
        default=[],
        metavar="69码=产品编码",
        help="人工核验后显式批准一个接口候选；可重复。",
    )
    apply_parser.add_argument(
        "--reject-barcode",
        action="append",
        default=[],
        help="即使有自动建议也保持未迁移；可重复。",
    )
    apply_parser.add_argument("--confirm", action="store_true")
    return parser.parse_args()


def _read_secret(label: str, environment_name: str) -> str:
    value = os.environ.get(environment_name, "").strip()
    if value:
        return value
    return getpass.getpass(f"{label}: ").strip()


def _signature_headers(access_key: str, timestamp: str, nonce: str) -> dict[str, str]:
    canonical_headers = "\n".join(
        (
            f"x-ca-key:{access_key}",
            f"x-ca-nonce:{nonce}",
            f"x-ca-signature-method:{SIGNATURE_METHOD}",
            f"x-ca-timestamp:{timestamp}",
        )
    )
    return {
        "canonical": canonical_headers,
        "signature_headers": ",".join(SIGNATURE_HEADER_NAMES),
    }


def _signed_request(access_key: str, secret_key: str, barcode: str) -> urllib.request.Request:
    query = urllib.parse.urlencode({"barcode": barcode})
    resource = f"{API_PATH}?{query}"
    timestamp = str(int(time.time() * 1000))
    nonce = secrets.token_hex(16)
    signature_values = _signature_headers(access_key, timestamp, nonce)
    string_to_sign = "\n".join(
        (
            "GET",
            "application/json",
            "",
            "",
            "",
            signature_values["canonical"],
            resource,
        )
    )
    signature = base64.b64encode(
        hmac.new(
            secret_key.encode("utf-8"),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).digest()
    ).decode("ascii")
    return urllib.request.Request(
        f"{API_ORIGIN}{resource}",
        method="GET",
        headers={
            "Accept": "application/json",
            "X-Ca-Key": access_key,
            "X-Ca-Timestamp": timestamp,
            "X-Ca-Nonce": nonce,
            "X-Ca-Signature-Method": SIGNATURE_METHOD,
            "X-Ca-Signature-Headers": signature_values["signature_headers"],
            "X-Ca-Signature": signature,
        },
    )


def _fetch_one(access_key: str, secret_key: str, barcode: str) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        request = _signed_request(access_key, secret_key, barcode)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return {
                    "barcode": barcode,
                    "http_status": int(response.status),
                    "api_status": payload.get("status"),
                    "msg": payload.get("msg"),
                    "list": list((payload.get("data") or {}).get("list") or []),
                }
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            try:
                payload = json.loads(body)
            except json.JSONDecodeError:
                payload = {}
            return {
                "barcode": barcode,
                "http_status": int(exc.code),
                "api_status": payload.get("status"),
                "msg": payload.get("msg") or "http_error",
                "list": list((payload.get("data") or {}).get("list") or []),
            }
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.3 * attempt)
    raise AuditError(f"商品编码接口连续三次失败：barcode={barcode}，error={last_error}")


def _write_private_neutral_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _fetch(args: argparse.Namespace) -> dict[str, Any]:
    if args.request_gap_ms < 0 or args.request_gap_ms > 5_000:
        raise AuditError("request-gap-ms 必须在0到5000之间")
    catalog = load_product_rag(SKILL_ROOT)
    catalog_barcodes = {str(item["barcode_69"]) for item in catalog["products"]}
    barcodes = sorted(set(args.barcode) or catalog_barcodes)
    unknown = sorted(set(barcodes) - catalog_barcodes)
    if unknown:
        raise AuditError(f"请求含目录外69码：{unknown[0]}")
    access_key = _read_secret("Access Key", "CANBAN_API_ACCESS_KEY")
    secret_key = _read_secret("Secret Key", "CANBAN_API_SECRET_KEY")
    if not access_key or not secret_key:
        raise AuditError("Access Key 和 Secret Key 均不能为空")

    results: list[dict[str, Any]] = []
    for index, barcode in enumerate(barcodes, start=1):
        result = _fetch_one(access_key, secret_key, barcode)
        results.append(result)
        if index % 10 == 0 or index == len(barcodes):
            print(f"mapped {index}/{len(barcodes)}", file=sys.stderr)
        if index != len(barcodes):
            time.sleep(args.request_gap_ms / 1000)
    output = Path(args.output).resolve()
    _write_private_neutral_json(output, results)
    return {
        "status": "fetched",
        "requested": len(barcodes),
        "http_200": sum(item["http_status"] == 200 for item in results),
        "api_200": sum(item["api_status"] == 200 for item in results),
        "output": str(output),
    }


def _normalize_text(value: Any) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", normalized)


def _core_name(value: Any) -> str:
    normalized = _normalize_text(value)
    for part in GENERIC_NAME_PARTS:
        normalized = normalized.replace(_normalize_text(part), "")
    return normalized


def _category(value: Any) -> str | None:
    text = str(value or "")
    for label, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _metadata_text(product: dict[str, Any]) -> str:
    values = [
        product.get("product_name"),
        product.get("specification"),
        product.get("variant"),
        *(product.get("aliases") or []),
        *(product.get("variant_aliases") or []),
        *(product.get("specification_aliases") or []),
        *(item.get("source_folder") for item in product.get("sources") or []),
    ]
    return " ".join(str(value) for value in values if value)


def _candidate_relevance(product: dict[str, Any], candidate: dict[str, str]) -> float:
    product_name = _core_name(product.get("product_name"))
    candidate_name = _core_name(candidate.get("product_name"))
    if not product_name or not candidate_name:
        return 0.0
    product_category = _category(product.get("product_name"))
    candidate_category = _category(candidate.get("product_name"))
    if product_category and candidate_category and product_category != candidate_category:
        return 0.0
    score = SequenceMatcher(None, product_name, candidate_name).ratio()
    if product_name in candidate_name or candidate_name in product_name:
        score += 0.35
    metadata = _normalize_text(_metadata_text(product))
    candidate_text = _normalize_text(candidate.get("product_name"))
    flavor_tokens = {
        token
        for token in re.findall(r"[\u4e00-\u9fff]{2,8}", str(product.get("variant") or ""))
        if token not in {"联名", "包装", "香型", "双色", "三色", "四色", "五色"}
    }
    score += min(
        0.3,
        0.08 * sum(_normalize_text(token) in candidate_text for token in flavor_tokens),
    )
    specification_numbers = set(re.findall(r"\d+", str(product.get("specification") or "")))
    candidate_numbers = set(re.findall(r"\d+", str(candidate.get("product_name") or "")))
    if specification_numbers and specification_numbers & candidate_numbers:
        score += 0.15
    variant_colors = {
        value
        for value in str(product.get("variant") or "")
        if value in COLOR_CHARACTERS
    }
    source_colors = {
        value
        for source in product.get("sources") or []
        for value in str(source.get("source_folder") or "")
        if value in COLOR_CHARACTERS
    }
    # Use color only when the product-level variant is explicit and no source
    # folder introduces a conflicting color.  This keeps multi-color source
    # aggregations ambiguous instead of forcing a convenient-looking code.
    if variant_colors and not (source_colors - variant_colors):
        candidate_colors = {
            value for value in str(candidate.get("product_name") or "")
            if value in COLOR_CHARACTERS
        }
        if candidate_colors and variant_colors <= candidate_colors:
            score += 0.45
        elif candidate_colors:
            score -= 0.25 * len(variant_colors - candidate_colors)
    if candidate_text and candidate_text in metadata:
        score += 0.1
    return score


def _channel_rank(candidate: dict[str, str]) -> int:
    name = str(candidate["product_name"])
    code = str(candidate["product_code"])
    if INTERNATIONAL_PATTERN.search(name) or code.startswith("YH-"):
        return 4
    if CUSTOMER_PATTERN.search(name) or code.startswith("CP-TZ-"):
        return 3
    if "线上" in name:
        return 2
    if "线下" in name:
        return 0
    return 1


def _modifier_rank(product: dict[str, Any], candidate: dict[str, str]) -> int:
    metadata = _metadata_text(product)
    name = str(candidate["product_name"])
    rank = 0
    if LOGISTICS_PATTERN.search(name):
        rank += 2
    if UPGRADE_PATTERN.search(name) and not UPGRADE_PATTERN.search(metadata):
        rank += 2
    if SPOKESPERSON_PATTERN.search(name) and not SPOKESPERSON_PATTERN.search(metadata):
        rank += 2
    return rank


def _valid_candidates(mapping: dict[str, Any], barcode: str) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw in mapping.get("list") or []:
        if not isinstance(raw, dict):
            continue
        code = str(raw.get("product_code") or "").strip()
        name = str(raw.get("product_name") or "").strip()
        item_barcode = str(raw.get("barcode") or "").strip()
        if not code or not name or item_barcode != barcode or code in seen:
            continue
        if PRODUCT_CODE_PATTERN.fullmatch(code) is None:
            continue
        seen.add(code)
        result.append({"product_code": code, "product_name": name, "barcode": barcode})
    return result


def _suggest_product_code(
    product: dict[str, Any],
    candidates: list[dict[str, str]],
) -> tuple[str | None, str, str, list[dict[str, Any]]]:
    scored = [
        {
            **candidate,
            "relevance": round(_candidate_relevance(product, candidate), 4),
            "channel_rank": _channel_rank(candidate),
            "modifier_rank": _modifier_rank(product, candidate),
        }
        for candidate in candidates
    ]
    if not scored:
        return None, "not_found", "接口未返回可验证的同码商品编码", scored
    if len(scored) == 1:
        return (
            str(scored[0]["product_code"]),
            "api_unique",
            "接口对该69码仅返回一个有效商品编码",
            scored,
        )

    relevant = [item for item in scored if float(item["relevance"]) >= 0.48]
    if len(relevant) == 1:
        return (
            str(relevant[0]["product_code"]),
            "relevant_unique",
            "多条映射中仅一个候选与目录产品名/品类/规格相容",
            scored,
        )
    if not relevant:
        return None, "ambiguous", "没有候选达到产品元数据相容阈值", scored

    best_channel = min(int(item["channel_rank"]) for item in relevant)
    channel_candidates = [
        item for item in relevant if int(item["channel_rank"]) == best_channel
    ]
    if len(channel_candidates) == 1:
        return (
            str(channel_candidates[0]["product_code"]),
            "offline_unique",
            "相容候选中仅一个符合标准线下优先级",
            scored,
        )

    best_modifier = min(int(item["modifier_rank"]) for item in channel_candidates)
    generic_candidates = [
        item for item in channel_candidates if int(item["modifier_rank"]) == best_modifier
    ]
    ranked = sorted(generic_candidates, key=lambda item: float(item["relevance"]), reverse=True)
    if len(ranked) == 1:
        return (
            str(ranked[0]["product_code"]),
            "metadata_unique",
            "标准线下候选中仅一个不带未证实的箱规/升级/代言修饰",
            scored,
        )
    if float(ranked[0]["relevance"]) - float(ranked[1]["relevance"]) >= 0.2:
        return (
            str(ranked[0]["product_code"]),
            "metadata_unique",
            "标准线下候选的产品元数据相容度显著高于次选",
            scored,
        )
    return None, "ambiguous", "同码仍有多个无法由现有包装元数据区分的合法候选", scored


def _mapping_index(value: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(value, list):
        raise AuditError("mapping-file 顶层必须是数组")
    result: dict[str, dict[str, Any]] = {}
    for item in value:
        if not isinstance(item, dict):
            raise AuditError("mapping-file 含非对象条目")
        barcode = str(item.get("barcode") or "")
        if not barcode or barcode in result:
            raise AuditError(f"mapping-file 69码为空或重复：{barcode}")
        result[barcode] = item
    return result


def _build_plan(args: argparse.Namespace) -> dict[str, Any]:
    catalog = load_product_rag(SKILL_ROOT)
    mapping_path = Path(args.mapping_file).resolve()
    try:
        mapping_value = json.loads(mapping_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"mapping-file 无法读取为JSON：{mapping_path}") from exc
    mapping = _mapping_index(mapping_value)
    products: list[dict[str, Any]] = []
    for product in catalog["products"]:
        barcode = str(product["barcode_69"])
        raw_mapping = mapping.get(barcode, {})
        candidates = _valid_candidates(raw_mapping, barcode)
        selected, status, basis, scored = _suggest_product_code(product, candidates)
        products.append(
            {
                "barcode_69": barcode,
                "product_name": str(product["product_name"]),
                "specification": str(product["specification"]),
                "variant": product.get("variant"),
                "current_product_code": str(product["product_code"]),
                "api_status": raw_mapping.get("api_status"),
                "selection_status": status,
                "selection_basis": basis,
                "suggested_product_code": selected,
                "approved_product_code": None,
                "candidates": scored,
            }
        )
    plan = {
        "schema_version": "1.0",
        "selection_policy": (
            "同码严格校验；产品名/品类/规格相容；标准线下成品优先；"
            "国际标签、客户专供、运输箱规、升级版和代言版本不得在无证据时抢占主编码；"
            "不能唯一收敛即保持ambiguous。"
        ),
        "products": products,
    }
    output = Path(args.output).resolve()
    _write_private_neutral_json(output, plan)
    counts: dict[str, int] = {}
    for item in products:
        key = str(item["selection_status"])
        counts[key] = counts.get(key, 0) + 1
    return {"status": "planned", "products": len(products), "counts": counts, "output": str(output)}


def _safe_product_code(value: Any) -> str:
    code = str(value or "").strip()
    if PRODUCT_CODE_PATTERN.fullmatch(code) is None:
        raise AuditError(f"产品编码不能安全用于目录名：{code}")
    if INVALID_FOLDER_ENDING.search(code):
        raise AuditError(f"产品编码不能以空格或点结尾：{code}")
    return code


def _source_directory_target(source: Path, product_code: str) -> Path:
    suffix = f"+{product_code}"
    if source.name.endswith(suffix):
        return source
    return source.with_name(source.name + suffix)


def _ensure_controlled_directory(path: Path, parent: Path, label: str) -> Path:
    resolved = path.resolve()
    parent_resolved = parent.resolve()
    if path.is_symlink() or not path.is_dir() or resolved.parent != parent_resolved:
        raise AuditError(f"{label}目录不存在、越界或不是直接子目录：{path}")
    return resolved


def _verify_source_mirror(
    product: dict[str, Any],
    source_record: dict[str, Any],
    directory: Path,
) -> None:
    source_id = str(source_record["source_id"])
    views = [
        view
        for view in product.get("views") or []
        if str(view.get("source_id")) == source_id
    ]
    if not views:
        raise AuditError(f"来源没有对应正式视图：{source_id}")
    for view in views:
        source_image = directory / str(view["source_original_name"])
        if not source_image.is_file():
            raise AuditError(f"来源镜像缺少原图：{source_image}")
        if sha256_file(source_image) != str(view["sha256"]):
            raise AuditError(f"来源镜像与正式视图哈希不一致：{source_image}")


def _locate_live_source_directory(
    product: dict[str, Any],
    source_record: dict[str, Any],
) -> tuple[Path, str]:
    source_text = str(source_record["source_folder"])
    collection = source_text.split("/", maxsplit=1)[0]
    collection_root = SOURCE_COLLECTIONS.get(collection)
    if collection_root is None:
        raise AuditError(f"来源集合不受控：{source_text}")
    declared = (SKILL_ROOT / source_text).resolve()
    if declared.is_dir():
        directory = _ensure_controlled_directory(
            declared,
            collection_root,
            "原始素材",
        )
        _verify_source_mirror(product, source_record, directory)
        return directory, collection

    dental_root = SOURCE_COLLECTIONS["参半牙具"]
    fallback = (dental_root / Path(source_text).name).resolve()
    directory = _ensure_controlled_directory(
        fallback,
        dental_root,
        "参半牙具镜像",
    )
    _verify_source_mirror(product, source_record, directory)
    return directory, "参半牙具"


def _selection_from_plan(
    item: dict[str, Any],
    approved_statuses: set[str],
) -> str | None:
    candidate_codes = {
        str(candidate.get("product_code"))
        for candidate in item.get("candidates") or []
        if candidate.get("product_code")
    }
    explicit = item.get("approved_product_code")
    if explicit:
        code = _safe_product_code(explicit)
        if code not in candidate_codes:
            raise AuditError(
                f"显式批准编码不在该69码接口候选中：{item.get('barcode_69')}={code}"
            )
        return code
    status = str(item.get("selection_status") or "")
    suggested = item.get("suggested_product_code")
    if status in approved_statuses and suggested:
        code = _safe_product_code(suggested)
        if code not in candidate_codes:
            raise AuditError(
                f"建议编码不在该69码接口候选中：{item.get('barcode_69')}={code}"
            )
        return code
    return None


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _apply(args: argparse.Namespace) -> dict[str, Any]:
    plan_path = Path(args.plan_file).resolve()
    plan = load_json(plan_path)
    if plan.get("schema_version") != "1.0" or not isinstance(plan.get("products"), list):
        raise AuditError("plan-file 结构无效")
    approved_statuses = set(args.approved_status)
    manual_approvals: dict[str, str] = {}
    for raw in args.approve:
        barcode, separator, code = str(raw).partition("=")
        barcode = barcode.strip()
        if separator != "=" or not barcode or barcode in manual_approvals:
            raise AuditError(f"approve 必须是唯一的69码=产品编码：{raw}")
        manual_approvals[barcode] = _safe_product_code(code)
    rejected = {str(value).strip() for value in args.reject_barcode if str(value).strip()}
    overlap = sorted(set(manual_approvals) & rejected)
    if overlap:
        raise AuditError(f"同一69码不能同时批准和拒绝：{overlap[0]}")
    selections: dict[str, str] = {}
    plan_items: dict[str, dict[str, Any]] = {}
    seen_plan_barcodes: set[str] = set()
    for item in plan["products"]:
        barcode = str(item.get("barcode_69") or "")
        if not barcode or barcode in seen_plan_barcodes:
            raise AuditError(f"plan-file 69码为空或重复：{barcode}")
        seen_plan_barcodes.add(barcode)
        plan_items[barcode] = item
        if barcode in rejected:
            continue
        if barcode in manual_approvals:
            candidate_codes = {
                str(candidate.get("product_code"))
                for candidate in item.get("candidates") or []
                if candidate.get("product_code")
            }
            selected = manual_approvals[barcode]
            if selected not in candidate_codes:
                raise AuditError(
                    f"人工批准编码不在该69码接口候选中：{barcode}={selected}"
                )
        else:
            selected = _selection_from_plan(item, approved_statuses)
        if selected:
            selections[barcode] = selected
    unknown_controls = sorted((set(manual_approvals) | rejected) - {
        str(item.get("barcode_69") or "") for item in plan["products"]
    })
    if unknown_controls:
        raise AuditError(f"批准或拒绝项不在计划中：{unknown_controls[0]}")
    if not selections:
        raise AuditError("计划中没有任何已批准迁移项")

    catalog = load_product_rag(SKILL_ROOT)
    by_barcode = {str(item["barcode_69"]): item for item in catalog["products"]}
    unknown = sorted(set(selections) - set(by_barcode))
    if unknown:
        raise AuditError(f"计划包含目录外69码：{unknown[0]}")

    move_pairs: list[tuple[Path, Path]] = []
    path_replacements: dict[str, str] = {}
    collection_replacements: dict[str, str] = {}
    migrated_products: list[tuple[dict[str, Any], str, dict[str, Any]]] = []
    target_keys: set[str] = set()
    selected_barcodes = set(selections)
    for barcode, product_code in sorted(selections.items()):
        product = by_barcode[barcode]
        views = list(product.get("views") or [])
        if not views:
            raise AuditError(f"商品没有参考图，不能迁移：{barcode}")
        formal_parents = {
            (SKILL_ROOT / str(view["image_file"])).resolve().parent for view in views
        }
        if len(formal_parents) != 1:
            raise AuditError(f"同一商品参考图不在一个正式目录：{barcode}")
        formal_source = _ensure_controlled_directory(
            next(iter(formal_parents)),
            PRODUCTS_ROOT,
            "正式知识库",
        )
        formal_target = _catalog_product_directory(
            barcode,
            str(product["product_name"]),
            product_code,
        ).resolve()
        if formal_source != formal_target:
            move_pairs.append((formal_source, formal_target))
            path_replacements[formal_source.relative_to(SKILL_ROOT.resolve()).as_posix()] = (
                formal_target.relative_to(SKILL_ROOT.resolve()).as_posix()
            )

        for source_record in product.get("sources") or []:
            source_text = str(source_record["source_folder"])
            source_path, live_collection = _locate_live_source_directory(
                product,
                source_record,
            )
            source_target = _source_directory_target(source_path, product_code).resolve()
            if source_path != source_target:
                move_pairs.append((source_path, source_target))
            path_replacements[source_text] = source_target.relative_to(
                SKILL_ROOT.resolve()
            ).as_posix()
            collection_replacements[source_text] = live_collection
        migrated_products.append((product, product_code, plan_items[barcode]))

    # Historical source collections were consolidated into 参半牙具 before
    # this migration.  Rebind every remaining catalog source to the verified
    # live mirror even when its product code is still unresolved.
    for product in catalog["products"]:
        if str(product["barcode_69"]) in selected_barcodes:
            continue
        for source_record in product.get("sources") or []:
            source_text = str(source_record["source_folder"])
            source_path, live_collection = _locate_live_source_directory(
                product,
                source_record,
            )
            path_replacements[source_text] = source_path.relative_to(
                SKILL_ROOT.resolve()
            ).as_posix()
            collection_replacements[source_text] = live_collection

    unique_sources: set[str] = set()
    for source, target in move_pairs:
        source_key = os.path.normcase(str(source))
        target_key = os.path.normcase(str(target))
        if source_key in unique_sources:
            raise AuditError(f"同一目录被重复计划迁移：{source}")
        unique_sources.add(source_key)
        if target_key in target_keys:
            raise AuditError(f"多个目录计划迁移到同一目标：{target}")
        target_keys.add(target_key)
        if target.exists():
            raise AuditError(f"迁移目标已存在，拒绝覆盖：{target}")
        if len(str(target)) > 245:
            raise AuditError(f"迁移目标路径过长：{target}")

    if not args.confirm:
        return {
            "status": "dry_run",
            "approved_products": len(migrated_products),
            "directory_moves": len(move_pairs),
            "unapproved_products": len(catalog["products"]) - len(migrated_products),
        }

    descriptor = _acquire_lock()
    original_catalog = CATALOG_PATH.read_bytes()
    completed_moves: list[tuple[Path, Path]] = []
    try:
        for source, target in move_pairs:
            if not source.is_dir() or target.exists():
                raise AuditError(f"加锁后目录状态发生变化，拒绝迁移：{source} -> {target}")
        for source, target in move_pairs:
            os.replace(source, target)
            completed_moves.append((source, target))

        for product, product_code, plan_item in migrated_products:
            old_code = str(product.get("product_code") or "")
            aliases = product.setdefault("product_code_aliases", [])
            if old_code and old_code != "未标注" and old_code != product_code:
                _append_unique(aliases, old_code)
            interface_candidates = {
                str(candidate.get("product_code"))
                for candidate in plan_item.get("candidates") or []
                if candidate.get("product_code")
            }
            previous_catalog_code = str(plan_item.get("current_product_code") or "")
            aliases[:] = [
                value
                for value in aliases
                if value != product_code
                and (
                    value not in interface_candidates
                    or value == previous_catalog_code
                )
            ]
            product["product_code"] = product_code
            strong = product["identity_anchors"]["strong"]
            strong[:] = [
                value for value in strong if not str(value).startswith("产品编码 ")
            ]
            _append_unique(strong, f"产品编码 {product_code}")
            for alias in aliases:
                _append_unique(strong, f"产品编码 {alias}")
        for product in catalog["products"]:
            for source in product.get("sources") or []:
                source_text = str(source["source_folder"])
                source["source_folder"] = path_replacements.get(source_text, source_text)
                source["collection"] = collection_replacements.get(
                    source_text,
                    str(source["collection"]),
                )
            for view in product.get("views") or []:
                image_text = str(view["image_file"])
                image_path = Path(image_text)
                parent_text = image_path.parent.as_posix()
                if parent_text in path_replacements:
                    view["image_file"] = (
                        Path(path_replacements[parent_text]) / image_path.name
                    ).as_posix()
                source_text = str(view["source_folder"])
                view["source_folder"] = path_replacements.get(source_text, source_text)
                view["source_collection"] = collection_replacements.get(
                    source_text,
                    str(view["source_collection"]),
                )

        temporary = CATALOG_PATH.with_name(CATALOG_PATH.name + ".product-code.tmp")
        write_json(temporary, catalog)
        candidate = load_json(temporary)
        validate_json(candidate, SCHEMA_PATH)
        os.replace(temporary, CATALOG_PATH)
        clear_product_rag_cache()
        load_product_rag(SKILL_ROOT)
    except Exception:
        CATALOG_PATH.with_name(CATALOG_PATH.name + ".product-code.tmp").unlink(
            missing_ok=True
        )
        CATALOG_PATH.write_bytes(original_catalog)
        clear_product_rag_cache()
        for source, target in reversed(completed_moves):
            if target.exists() and not source.exists():
                os.replace(target, source)
        raise
    finally:
        _release_lock(descriptor)

    return {
        "status": "applied",
        "migrated_products": len(migrated_products),
        "directory_moves": len(move_pairs),
        "source_folder_relinks": sum(
            old != new
            for old, new in path_replacements.items()
            if not old.startswith("canban-product-multimodal-knowledge-base/")
        ),
        "unapproved_products": len(catalog["products"]) - len(migrated_products),
    }


def main() -> int:
    args = _parse_args()
    if args.command == "fetch":
        result = _fetch(args)
    elif args.command == "plan":
        result = _build_plan(args)
    else:
        result = _apply(args)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
