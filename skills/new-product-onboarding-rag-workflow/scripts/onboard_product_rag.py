from __future__ import annotations

import argparse
import base64
import copy
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import sys
import tempfile
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = SCRIPT_DIR.parents[2]
SHARED_RAG_ROOT = (
    PROJECT_ROOT / "shared" / "canban-product-multimodal-knowledge-base"
)
SHARED_RAG_SCRIPTS = SHARED_RAG_ROOT / "scripts"
for import_root in (PROJECT_ROOT, SHARED_RAG_SCRIPTS):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from audit_core.common import AuditError, load_json, sha256_file, validate_json
from audit_core.product_rag import (
    canonical_product_name,
    catalog_product_name_values,
    clear_product_rag_cache,
    ean13_is_valid,
    load_product_rag,
)
from ingest_product_reference import (
    CATALOG_PATH,
    KNOWLEDGE_ASSET_ROOT,
    KNOWLEDGE_ROOT,
    PRODUCTS_ROOT,
    SCHEMA_PATH,
    _acquire_lock,
    _catalog_product_directory,
    _relative_text,
    _release_lock,
    _write_catalog_atomic,
)


OBSERVATION_SCHEMA_PATH = SKILL_ROOT / "references" / "observation-manifest.schema.json"
API_ORIGIN = "https://api.canban.cn"
API_PATH = "/outapi/oms/product/sku/mapping"
SIGNATURE_METHOD = "HmacSHA256"
SIGNATURE_HEADER_NAMES = (
    "x-ca-key",
    "x-ca-nonce",
    "x-ca-signature-method",
    "x-ca-timestamp",
)
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
PRODUCT_CODE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
CATEGORY_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("facial_cleanser", re.compile(r"洁面乳|洗面奶")),
    ("shampoo", re.compile(r"洗发水|洗发露")),
    ("conditioner", re.compile(r"护发素|发膜")),
    ("body_wash", re.compile(r"沐浴露|沐浴乳")),
    ("mouthwash", re.compile(r"漱口水")),
    ("oral_spray", re.compile(r"口腔喷雾|口喷")),
    ("toothpaste", re.compile(r"牙膏")),
    ("toothbrush", re.compile(r"牙刷")),
    ("floss", re.compile(r"牙线棒|牙线")),
    ("repellent", re.compile(r"花露水|驱蚊")),
)
UNOBSERVED_MODIFIERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("套盒专用", re.compile(r"套盒专用|套装专用")),
    ("箱规", re.compile(r"(?:12|24|36|48)箱规|带中箱|卡箱|外箱")),
    ("升级", re.compile(r"升级版|升级款|升级配方|全新升级")),
    ("代言版本", re.compile(r"代言人|华晨宇")),
    ("客户专供", re.compile(r"专供|定制|思家客|赛维泰|倍加洁|永辉|大润发|克劳丽")),
    ("国际标签", re.compile(r"国际|标签|越南|菲律宾|马来西亚|泰国|新加坡|香港|韩国|日本|英国")),
    ("线上", re.compile(r"线上")),
)
CORE_REMOVALS = (
    "aboutfocus",
    "oralshark",
    "小箭头",
    "参半",
    "线下ka",
    "线下",
    "线上",
    "套盒专用",
    "套装专用",
    "带中箱",
    "全新配方",
    "升级配方",
    "升级版",
    "升级款",
    "成人",
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="按视觉名称和精确69码接入商品参考图，并与库存清单对账。"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    match = subparsers.add_parser("match", help="查询接口并生成无凭据接入计划")
    match.add_argument("--source-root", required=True)
    match.add_argument("--observations", required=True)
    match.add_argument("--output", required=True)
    match.add_argument("--request-gap-ms", type=int, default=120)

    apply_parser = subparsers.add_parser("apply", help="预演或原子应用已收敛计划")
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--confirm", action="store_true")

    reconcile = subparsers.add_parser("reconcile", help="对账权威库存清单与正式catalog")
    reconcile.add_argument("--workbook", required=True)
    reconcile.add_argument("--output")
    reconcile.add_argument("--out-of-stock-text", default="无库存，未发出")
    return parser.parse_args()


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _windows_user_environment(name: str) -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except (FileNotFoundError, OSError):
        return ""
    return str(value).strip()


def _read_api_secret(name: str) -> str:
    return os.environ.get(name, "").strip() or _windows_user_environment(name)


def _signature_headers(access_key: str, timestamp: str, nonce: str) -> dict[str, str]:
    canonical = "\n".join(
        (
            f"x-ca-key:{access_key}",
            f"x-ca-nonce:{nonce}",
            f"x-ca-signature-method:{SIGNATURE_METHOD}",
            f"x-ca-timestamp:{timestamp}",
        )
    )
    return {
        "canonical": canonical,
        "signature_headers": ",".join(SIGNATURE_HEADER_NAMES),
    }


def _signed_request(
    access_key: str,
    secret_key: str,
    barcode: str,
) -> urllib.request.Request:
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
    raise AuditError(f"商品映射接口连续三次失败：barcode={barcode}，error={last_error}")


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("參半", "参半")
    text = text.replace("双支", "2支").replace("单支", "1支").replace("只", "支")
    return re.sub(r"[^0-9a-z\u4e00-\u9fff+%]+", "", text)


def _core_text(value: Any) -> str:
    text = _normalize_text(value)
    for token in CORE_REMOVALS:
        text = text.replace(_normalize_text(token), "")
    text = re.sub(r"(?:12|24|36|48)箱规", "", text)
    return text


def _grams(value: Any) -> set[str]:
    text = _core_text(value)
    if len(text) < 2:
        return {text} if text else set()
    return {text[index : index + 2] for index in range(len(text) - 1)}


def _measurement_tokens(value: Any) -> set[str]:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    text = text.replace("双支", "2支").replace("单支", "1支").replace("只", "支")
    result: set[str] = set()
    for number_text, unit in re.findall(
        r"(\d+(?:\.\d+)?)\s*(ml|毫升|l|升|g|克|支|条|片|个)",
        text,
    ):
        number = float(number_text)
        canonical_unit = {"毫升": "ml", "升": "l", "克": "g"}.get(unit, unit)
        if canonical_unit == "l":
            number *= 1000
            canonical_unit = "ml"
        number_value = str(int(number)) if number.is_integer() else str(number)
        result.add(f"{number_value}{canonical_unit}")
    return result


def _category(value: Any) -> str | None:
    text = str(value or "")
    for label, pattern in CATEGORY_PATTERNS:
        if pattern.search(text):
            return label
    return None


def _candidate_metrics(
    observation: dict[str, Any],
    candidate: dict[str, str],
) -> dict[str, Any]:
    observed_context = " ".join(
        str(value or "")
        for value in (
            observation.get("observed_product_name"),
            observation.get("observed_specification"),
            observation.get("observed_variant"),
        )
    )
    candidate_name = str(candidate["product_name"])
    left = _core_text(observed_context)
    right = _core_text(candidate_name)
    sequence = SequenceMatcher(None, left, right).ratio() if left and right else 0.0
    left_grams = _grams(observed_context)
    right_grams = _grams(candidate_name)
    union = left_grams | right_grams
    jaccard = len(left_grams & right_grams) / len(union) if union else 0.0
    containment = bool(left and right and (left in right or right in left))

    observed_measurements = _measurement_tokens(observed_context)
    candidate_measurements = _measurement_tokens(candidate_name)
    measurement_conflict = bool(
        observed_measurements
        and candidate_measurements
        and not observed_measurements.intersection(candidate_measurements)
    )
    observed_category = _category(observed_context)
    candidate_category = _category(candidate_name)
    category_conflict = bool(
        observed_category
        and candidate_category
        and observed_category != candidate_category
    )

    variant = _normalize_text(observation.get("observed_variant"))
    variant_match = bool(variant and variant in _normalize_text(candidate_name))
    unobserved_modifiers = [
        label
        for label, pattern in UNOBSERVED_MODIFIERS
        if pattern.search(candidate_name) and not pattern.search(observed_context)
    ]
    score = 0.55 * sequence + 0.45 * jaccard
    if containment:
        score += 0.12
    if observed_measurements.intersection(candidate_measurements):
        score += 0.08
    if variant_match:
        score += 0.45
    compatible = (
        not category_conflict
        and not measurement_conflict
        and score >= 0.43
    )
    return {
        "score": round(score, 4),
        "sequence": round(sequence, 4),
        "bigram_jaccard": round(jaccard, 4),
        "variant_match": variant_match,
        "unobserved_modifiers": unobserved_modifiers,
        "modifier_penalty": len(unobserved_modifiers),
        "measurement_conflict": measurement_conflict,
        "category_conflict": category_conflict,
        "compatible": compatible,
    }


def _valid_exact_candidates(
    api_result: dict[str, Any],
    barcode: str,
) -> list[dict[str, str]]:
    result: list[dict[str, str]] = []
    seen_codes: set[str] = set()
    for raw in api_result.get("list") or []:
        if not isinstance(raw, dict):
            continue
        product_code = str(raw.get("product_code") or "").strip()
        product_name = str(raw.get("product_name") or "").strip()
        returned_barcode = str(raw.get("barcode") or "").strip()
        if (
            returned_barcode != barcode
            or not product_name
            or PRODUCT_CODE_PATTERN.fullmatch(product_code) is None
            or product_code.casefold() in seen_codes
        ):
            continue
        seen_codes.add(product_code.casefold())
        result.append(
            {
                "product_code": product_code,
                "product_name": product_name,
                "barcode": returned_barcode,
            }
        )
    return result


def _select_candidate(
    observation: dict[str, Any],
    candidates: list[dict[str, str]],
) -> tuple[dict[str, str] | None, str, str, list[dict[str, Any]]]:
    scored = [
        {**candidate, **_candidate_metrics(observation, candidate)}
        for candidate in candidates
    ]
    scored.sort(
        key=lambda item: (
            not bool(item["compatible"]),
            int(item["modifier_penalty"]),
            -float(item["score"]),
            str(item["product_code"]),
        )
    )
    if not scored:
        return None, "not_found", "接口未返回同码且三字段有效的候选", scored

    approved_code = str(observation.get("approved_product_code") or "").strip()
    if approved_code:
        approved = next(
            (item for item in scored if item["product_code"] == approved_code),
            None,
        )
        if approved is None:
            return None, "approval_invalid", "人工批准编码不在精确同码候选中", scored
        if not approved["compatible"]:
            return None, "approval_conflict", "人工批准候选与可见名称、品类或规格冲突", scored
        return approved, "explicit_approved", "人工确认了精确同码且名称相容的接口候选", scored

    compatible = [item for item in scored if item["compatible"]]
    if not compatible:
        return None, "name_conflict", "精确同码候选均与可见产品名、品类或规格不相容", scored
    if len(scored) == 1:
        return compatible[0], "api_unique", "接口仅返回一个精确同码且名称相容的候选", scored
    if len(compatible) == 1:
        return compatible[0], "name_unique", "同码候选中仅一个与可见名称、品类和规格相容", scored

    variant_matches = [item for item in compatible if item["variant_match"]]
    if len(variant_matches) == 1:
        return variant_matches[0], "variant_unique", "仅一个同码候选包含包装可见香型或款式", scored
    narrowed = variant_matches or compatible

    best_penalty = min(int(item["modifier_penalty"]) for item in narrowed)
    standard = [item for item in narrowed if int(item["modifier_penalty"]) == best_penalty]
    if len(standard) == 1:
        return (
            standard[0],
            "unmodified_unique",
            "仅一个名称相容候选不含包装未显示的专用、箱规、升级或渠道修饰",
            scored,
        )

    ranked = sorted(standard, key=lambda item: float(item["score"]), reverse=True)
    if len(ranked) == 1 or float(ranked[0]["score"]) - float(ranked[1]["score"]) >= 0.12:
        return ranked[0], "fuzzy_unique", "产品名称相容度相对次选形成安全唯一差值", scored
    return None, "ambiguous", "精确同码后仍有多个名称相容候选，禁止自动接入", scored


def _validate_source_root(path: Path) -> Path:
    source_root = path.resolve()
    knowledge_root = KNOWLEDGE_ROOT.resolve()
    if path.is_symlink() or not source_root.is_dir():
        raise AuditError(f"source-root 必须是存在的普通目录：{source_root}")
    if source_root == knowledge_root or not source_root.is_relative_to(knowledge_root):
        raise AuditError(
            "source-root 必须是商品知识库下的一个受控素材集合，不能是知识库根目录："
            f"{source_root}"
        )
    return source_root


def _inspect_source_folder(
    source_root: Path,
    observation: dict[str, Any],
) -> list[dict[str, Any]]:
    folder = (source_root / str(observation["source_folder"])).resolve()
    if (
        folder.is_symlink()
        or not folder.is_dir()
        or folder.parent != source_root.resolve()
    ):
        raise AuditError(f"观察项必须指向 source-root 的一个普通直接子目录：{folder}")
    children = sorted(folder.iterdir(), key=lambda item: item.name.casefold())
    nested = [item for item in children if item.is_dir()]
    if nested:
        raise AuditError(f"单商品文件夹不得嵌套目录：{nested[0]}")
    unsupported = [
        item
        for item in children
        if item.is_file() and item.suffix.lower() not in IMAGE_SUFFIXES
    ]
    if unsupported:
        raise AuditError(f"单商品文件夹含非图片文件：{unsupported[0]}")
    images = [item for item in children if item.is_file()]
    if not images:
        raise AuditError(f"单商品文件夹没有图片：{folder}")
    names = {item.name for item in images}
    for evidence_field in ("name_evidence_files", "barcode_evidence_files"):
        unknown = sorted(set(observation[evidence_field]) - names)
        if unknown:
            raise AuditError(
                f"{observation['source_folder']} 的 {evidence_field} 引用了不存在的文件："
                f"{unknown[0]}"
            )
    files: list[dict[str, Any]] = []
    for image_path in images:
        with Image.open(image_path) as image:
            width, height = image.size
        files.append(
            {
                "name": image_path.name,
                "sha256": sha256_file(image_path),
                "width": width,
                "height": height,
            }
        )
    return files


def _match(args: argparse.Namespace) -> int:
    if args.request_gap_ms < 0 or args.request_gap_ms > 5_000:
        raise AuditError("request-gap-ms 必须在0到5000之间")
    source_root = _validate_source_root(Path(args.source_root))
    observations_path = Path(args.observations).resolve()
    manifest = load_json(observations_path)
    validate_json(manifest, OBSERVATION_SCHEMA_PATH)
    observations = list(manifest["observations"])
    source_ids = [str(item["source_id"]).casefold() for item in observations]
    source_folders = [str(item["source_folder"]).casefold() for item in observations]
    if len(source_ids) != len(set(source_ids)):
        raise AuditError("观察清单 source_id 重复")
    if len(source_folders) != len(set(source_folders)):
        raise AuditError("观察清单 source_folder 重复")
    for observation in observations:
        barcode = str(observation["barcode_69"])
        if not ean13_is_valid(barcode):
            raise AuditError(
                f"观察清单69码未通过EAN-13校验：{observation['source_folder']}={barcode}"
            )

    access_key = _read_api_secret("CANBAN_API_ACCESS_KEY")
    secret_key = _read_api_secret("CANBAN_API_SECRET_KEY")
    if not access_key or not secret_key:
        raise AuditError(
            "缺少 CANBAN_API_ACCESS_KEY 或 CANBAN_API_SECRET_KEY；"
            "凭据只能来自进程或 Windows 用户环境"
        )

    api_results: dict[str, dict[str, Any]] = {}
    barcodes = list(dict.fromkeys(str(item["barcode_69"]) for item in observations))
    for index, barcode in enumerate(barcodes, start=1):
        api_results[barcode] = _fetch_one(access_key, secret_key, barcode)
        if index % 10 == 0 or index == len(barcodes):
            print(f"queried {index}/{len(barcodes)}", file=sys.stderr)
        if index != len(barcodes):
            time.sleep(args.request_gap_ms / 1000)

    plan_products: list[dict[str, Any]] = []
    for observation in observations:
        barcode = str(observation["barcode_69"])
        api_result = api_results[barcode]
        candidates = _valid_exact_candidates(api_result, barcode)
        selected, status, basis, scored = _select_candidate(observation, candidates)
        source_files = _inspect_source_folder(source_root, observation)
        selected_identity = None
        target_directory = None
        if selected is not None:
            selected_identity = {
                "product_code": str(selected["product_code"]),
                "product_name": str(selected["product_name"]),
                "barcode_69": barcode,
            }
            target_directory = _catalog_product_directory(
                barcode,
                selected_identity["product_name"],
                selected_identity["product_code"],
            ).name
        plan_products.append(
            {
                "source_folder": str(observation["source_folder"]),
                "source_id": str(observation["source_id"]),
                "collection": source_root.name,
                "brand": str(observation["brand"]).strip(),
                "observed_product_name": str(observation["observed_product_name"]).strip(),
                "observed_specification": str(observation["observed_specification"]).strip(),
                "observed_variant": observation.get("observed_variant"),
                "barcode_69": barcode,
                "name_evidence_files": list(observation["name_evidence_files"]),
                "barcode_evidence_files": list(observation["barcode_evidence_files"]),
                "source_files": source_files,
                "api_http_status": api_result.get("http_status"),
                "api_status": api_result.get("api_status"),
                "api_message": api_result.get("msg"),
                "selection_status": status,
                "selection_basis": basis,
                "selected": selected_identity,
                "target_directory": target_directory,
                "candidates": scored,
            }
        )

    plan = {
        "schema_version": "1.0",
        "kind": "new-product-onboarding-rag-plan",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "selection_policy": (
            "包装先视觉提取；接口69码精确过滤；产品名、品类、规格和可见款式唯一模糊匹配；"
            "未显示的套盒、箱规、升级、代言、专供或国际修饰不得抢占；不能唯一收敛即失败关闭。"
        ),
        "api": {
            "origin": API_ORIGIN,
            "path": API_PATH,
            "query_field": "barcode",
        },
        "catalog_sha256_before": sha256_file(CATALOG_PATH),
        "observations_sha256": sha256_file(observations_path),
        "source_root_relative": source_root.relative_to(
            KNOWLEDGE_ASSET_ROOT.resolve()
        ).as_posix(),
        "products": plan_products,
    }
    output = Path(args.output).resolve()
    _write_json(output, plan)
    unresolved = [item for item in plan_products if item["selected"] is None]
    result = {
        "status": "planned" if not unresolved else "unresolved",
        "products": len(plan_products),
        "resolved": len(plan_products) - len(unresolved),
        "unresolved": len(unresolved),
        "http_200": sum(item["api_http_status"] == 200 for item in plan_products),
        "api_200": sum(item["api_status"] == 200 for item in plan_products),
        "output": str(output),
    }
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0 if not unresolved else 2


def _safe_slug(value: str, *, max_length: int) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    if not slug:
        raise AuditError(f"值无法生成稳定ID：{value}")
    if len(slug) <= max_length:
        return slug
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{slug[: max_length - 11].rstrip('-')}-{digest}"


def _catalog_product_id(barcode: str, product_code: str) -> str:
    prefix = f"canban-{barcode}-"
    return prefix + _safe_slug(product_code, max_length=79 - len(prefix))


def _view_id_prefix(product_code: str) -> str:
    return "kb-" + _safe_slug(product_code, max_length=54)


def _verify_source_files(
    source_root: Path,
    item: dict[str, Any],
) -> tuple[Path, list[Path]]:
    folder = (source_root / str(item["source_folder"])).resolve()
    if folder.is_symlink() or not folder.is_dir() or folder.parent != source_root.resolve():
        raise AuditError(f"计划来源目录已失效或越界：{folder}")
    current = sorted(
        [child for child in folder.iterdir() if child.is_file()],
        key=lambda child: child.name.casefold(),
    )
    if any(child.suffix.lower() not in IMAGE_SUFFIXES for child in current):
        raise AuditError(f"计划后来源目录新增了非图片文件：{folder}")
    if any(child.is_dir() for child in folder.iterdir()):
        raise AuditError(f"计划后来源目录出现嵌套目录：{folder}")
    planned = {str(file_item["name"]): file_item for file_item in item["source_files"]}
    if [child.name for child in current] != sorted(planned, key=str.casefold):
        raise AuditError(f"计划后来源图片集合已变化：{folder}")
    for child in current:
        record = planned[child.name]
        if sha256_file(child) != str(record["sha256"]):
            raise AuditError(f"计划后来源图片哈希已变化：{child}")
        with Image.open(child) as image:
            if image.size != (int(record["width"]), int(record["height"])):
                raise AuditError(f"计划后来源图片尺寸已变化：{child}")
    return folder, current


def _catalog_entry(
    source_root: Path,
    item: dict[str, Any],
) -> tuple[dict[str, Any], Path, list[Path]]:
    selected = item.get("selected")
    if not isinstance(selected, dict):
        raise AuditError(f"计划含未收敛商品，禁止应用：{item.get('source_folder')}")
    product_code = str(selected.get("product_code") or "")
    product_name = str(selected.get("product_name") or "")
    barcode = str(selected.get("barcode_69") or "")
    if (
        PRODUCT_CODE_PATTERN.fullmatch(product_code) is None
        or not product_name
        or barcode != str(item.get("barcode_69") or "")
        or not ean13_is_valid(barcode)
    ):
        raise AuditError(f"计划三字段无效：{item.get('source_folder')}")
    source_folder, source_images = _verify_source_files(source_root, item)
    destination = _catalog_product_directory(barcode, product_name, product_code)
    if destination.name != str(item.get("target_directory") or ""):
        raise AuditError(f"计划目标目录与三字段不一致：{item.get('source_folder')}")

    name_evidence = set(str(value) for value in item["name_evidence_files"])
    barcode_evidence = set(str(value) for value in item["barcode_evidence_files"])
    source_record = {
        "source_id": str(item["source_id"]),
        "collection": str(item["collection"]),
        "source_folder": _relative_text(source_folder),
        "observed_product_name": str(item["observed_product_name"]),
        "observed_specification": str(item["observed_specification"]),
        "observed_variant": item.get("observed_variant"),
    }
    planned_files = {str(value["name"]): value for value in item["source_files"]}
    view_prefix = _view_id_prefix(product_code)
    views: list[dict[str, Any]] = []
    for index, source_image in enumerate(source_images, start=1):
        record = planned_files[source_image.name]
        anchors: list[str] = []
        if source_image.name in name_evidence:
            anchors.extend(
                [
                    f"包装可见产品名称 {item['observed_product_name']}",
                    f"包装可见规格 {item['observed_specification']}",
                ]
            )
            if item.get("observed_variant"):
                anchors.append(f"包装可见款式/香型 {item['observed_variant']}")
        if source_image.name in barcode_evidence:
            anchors.append(f"包装可见69码 {barcode}")
        face = "unclassified"
        if source_image.name in name_evidence:
            face = "front"
        if source_image.name in barcode_evidence:
            face = "barcode"
        views.append(
            {
                "view_id": f"{view_prefix}-v{index:02d}",
                "face": face,
                "image_file": _relative_text(destination / source_image.name),
                "source_original_name": source_image.name,
                "source_id": str(item["source_id"]),
                "source_collection": str(item["collection"]),
                "source_folder": _relative_text(source_folder),
                "sha256": str(record["sha256"]),
                "width": int(record["width"]),
                "height": int(record["height"]),
                "identity_strength": "strong" if anchors else "unreviewed",
                "visible_anchors": anchors,
                "limitations": [
                    (
                        "该视图只证明列出的包装可见锚点；正式三字段仍以精确69码和接口唯一名称匹配为准"
                        if anchors
                        else "未单独标注物理面；使用时必须与现场可见文字、69码或包装锚点逐图比较"
                    )
                ],
            }
        )
    supporting = [f"规格 {item['observed_specification']}"]
    if item.get("observed_variant"):
        supporting.append(f"款式/香型 {item['observed_variant']}")
    product = {
        "product_id": _catalog_product_id(barcode, product_code),
        "brand": str(item["brand"]),
        "product_name": product_name,
        "product_code": product_code,
        "barcode_69": barcode,
        "specification": str(item["observed_specification"]),
        "variant": item.get("observed_variant"),
        "aliases": [],
        "variant_aliases": [],
        "specification_aliases": [],
        "product_code_aliases": [],
        "match_policy": "exact_or_candidate",
        "identity_anchors": {
            "strong": [
                f"产品名称 {product_name}",
                f"产品编码 {product_code}",
                f"69码 {barcode}",
            ],
            "supporting": supporting,
        },
        "non_identity_fields": [
            "防伪二维码及其内容",
            "批次、生产日期、限用日期等可变喷码",
            "包装折痕、破损、反光和拍摄背景",
        ],
        "sources": [source_record],
        "views": views,
    }
    return product, destination, source_images


def _prepare_apply(plan: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if plan.get("schema_version") != "1.0" or plan.get("kind") != "new-product-onboarding-rag-plan":
        raise AuditError("plan-file 不是受支持的新商品接入计划")
    if sha256_file(CATALOG_PATH) != str(plan.get("catalog_sha256_before") or ""):
        raise AuditError("正式catalog自计划生成后已变化；必须重新match，禁止应用陈旧计划")
    source_relative = Path(str(plan.get("source_root_relative") or ""))
    if source_relative.is_absolute():
        raise AuditError("计划 source_root_relative 必须是共享知识库相对路径")
    source_root = (KNOWLEDGE_ASSET_ROOT / source_relative).resolve()
    _validate_source_root(source_root)
    catalog = copy.deepcopy(load_product_rag(KNOWLEDGE_ROOT))
    products = list(catalog["products"])

    existing_ids = {str(item["product_id"]).casefold() for item in products}
    existing_names = {str(item["product_name"]).strip().casefold() for item in products}
    existing_codes = {str(item["product_code"]).strip().casefold() for item in products}
    existing_sources = {
        str(source["source_id"]).casefold()
        for product in products
        for source in product.get("sources") or []
    }
    existing_views = {
        str(view["view_id"]).casefold()
        for product in products
        for view in product.get("views") or []
    }
    operations: list[dict[str, Any]] = []
    for item in plan.get("products") or []:
        product, destination, source_images = _catalog_entry(source_root, item)
        collision_values = (
            (str(product["product_id"]).casefold(), existing_ids, "product_id"),
            (str(product["product_name"]).strip().casefold(), existing_names, "产品名称"),
            (str(product["product_code"]).strip().casefold(), existing_codes, "产品编码"),
            (str(item["source_id"]).casefold(), existing_sources, "source_id"),
        )
        for value, existing, label in collision_values:
            if value in existing:
                raise AuditError(f"新商品{label}与catalog重复：{value}")
            existing.add(value)
        for view in product["views"]:
            view_id = str(view["view_id"]).casefold()
            if view_id in existing_views:
                raise AuditError(f"新商品view_id与catalog重复：{view_id}")
            existing_views.add(view_id)
        if destination.exists():
            raise AuditError(f"目标商品目录已存在，拒绝覆盖：{destination}")
        operations.append(
            {
                "item": item,
                "product": product,
                "destination": destination,
                "source_images": source_images,
            }
        )
    if len(operations) != len(plan.get("products") or []):
        raise AuditError("计划商品数量不完整")
    catalog["products"].extend(operation["product"] for operation in operations)
    validate_json(catalog, SCHEMA_PATH)
    return catalog, operations


def _validate_catalog_coverage() -> dict[str, int]:
    catalog = load_product_rag(KNOWLEDGE_ROOT)
    physical = {
        path.name
        for path in PRODUCTS_ROOT.iterdir()
        if path.is_dir() and not path.is_symlink()
    }
    referenced = {
        (KNOWLEDGE_ASSET_ROOT / str(view["image_file"])).resolve().parent.name
        for product in catalog["products"]
        for view in product.get("views") or []
    }
    if physical != referenced:
        missing = sorted(referenced - physical)
        extra = sorted(physical - referenced)
        raise AuditError(
            "products物理目录与catalog未一一覆盖："
            f"missing={missing[:1]}，extra={extra[:1]}"
        )
    return {
        "catalog_products": len(catalog["products"]),
        "catalog_views": sum(len(item.get("views") or []) for item in catalog["products"]),
        "physical_directories": len(physical),
    }


def _remove_created_directory(path: Path, allowed_names: set[str]) -> None:
    resolved = path.resolve()
    if resolved.parent != PRODUCTS_ROOT.resolve() or path.name not in allowed_names:
        raise AuditError(f"回滚路径越界，拒绝删除：{path}")
    if path.exists():
        shutil.rmtree(path)


def _apply(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan_file).resolve()
    plan = load_json(plan_path)
    catalog, operations = _prepare_apply(plan)
    result = {
        "status": "dry_run" if not args.confirm else "applied",
        "catalog_products_before": len(catalog["products"]) - len(operations),
        "catalog_products_after": len(catalog["products"]),
        "products_added": len(operations),
        "views_added": sum(len(operation["product"]["views"]) for operation in operations),
        "target_directories": [operation["destination"].name for operation in operations],
    }
    if not args.confirm:
        print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0

    descriptor = _acquire_lock()
    original_catalog = CATALOG_PATH.read_bytes()
    allowed_names = {operation["destination"].name for operation in operations}
    stage_root: Path | None = None
    moved: list[Path] = []
    catalog_replaced = False
    try:
        if sha256_file(CATALOG_PATH) != str(plan["catalog_sha256_before"]):
            raise AuditError("获得写锁后catalog已变化；必须重新match")
        stage_root = Path(
            tempfile.mkdtemp(prefix=".new-product-onboarding-", dir=KNOWLEDGE_ROOT)
        )
        for operation in operations:
            stage_destination = stage_root / operation["destination"].name
            stage_destination.mkdir()
            planned_files = {
                str(value["name"]): value
                for value in operation["item"]["source_files"]
            }
            for source_image in operation["source_images"]:
                target = stage_destination / source_image.name
                shutil.copy2(source_image, target)
                if sha256_file(target) != str(planned_files[source_image.name]["sha256"]):
                    raise AuditError(f"复制后图片哈希不一致：{source_image}")
        for operation in operations:
            staged = stage_root / operation["destination"].name
            os.replace(staged, operation["destination"])
            moved.append(operation["destination"])
        _write_catalog_atomic(catalog, original_catalog)
        catalog_replaced = True
        result.update(_validate_catalog_coverage())
    except Exception:
        if catalog_replaced:
            CATALOG_PATH.write_bytes(original_catalog)
            clear_product_rag_cache()
        for destination in reversed(moved):
            _remove_created_directory(destination, allowed_names)
        clear_product_rag_cache()
        raise
    finally:
        if stage_root is not None and stage_root.exists():
            shutil.rmtree(stage_root)
        _release_lock(descriptor)

    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


def _cell_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _name_compatibility(
    workbook_name: str,
    product: dict[str, Any],
) -> dict[str, Any]:
    exact = canonical_product_name(workbook_name) == canonical_product_name(product["product_name"])
    ranked: list[dict[str, Any]] = []
    observation = {
        "observed_product_name": workbook_name,
        "observed_specification": "",
        "observed_variant": None,
    }
    for candidate_name in catalog_product_name_values(product):
        metrics = _candidate_metrics(
            observation,
            {
                "product_code": str(product["product_code"]),
                "product_name": candidate_name,
                "barcode": str(product["barcode_69"]),
            },
        )
        ranked.append({"catalog_value": candidate_name, **metrics})
    ranked.sort(key=lambda item: -float(item["score"]))
    best = ranked[0] if ranked else {"compatible": False, "score": 0.0}
    return {
        "status": "exact" if exact else ("fuzzy" if best["compatible"] else "mismatch"),
        "compatible": bool(exact or best["compatible"]),
        "score": 1.0 if exact else float(best["score"]),
        "matched_catalog_value": str(best.get("catalog_value") or product["product_name"]),
    }


def _reconcile(args: argparse.Namespace) -> int:
    try:
        from openpyxl import load_workbook
    except ImportError as exc:
        raise AuditError("读取权威xlsx需要项目依赖 openpyxl") from exc

    workbook_path = Path(args.workbook).resolve()
    workbook = load_workbook(workbook_path, read_only=True, data_only=True)
    if len(workbook.worksheets) != 1:
        raise AuditError("权威清单必须且只能有一个工作表，避免隐式选表")
    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    try:
        header_values = next(rows)
    except StopIteration as exc:
        raise AuditError("权威清单为空") from exc
    headers = {_cell_text(value): index for index, value in enumerate(header_values)}
    required = {
        "*商品名称",
        "*商品编码/组合编码",
        "*申请数量",
        "快递单号",
        "69码",
    }
    missing_headers = sorted(required - set(headers))
    if missing_headers:
        raise AuditError(f"权威清单缺少列：{missing_headers[0]}")

    authority_rows: list[dict[str, Any]] = []
    for excel_row, values in enumerate(rows, start=2):
        product_code = _cell_text(values[headers["*商品编码/组合编码"]])
        if not product_code:
            continue
        authority_rows.append(
            {
                "excel_row": excel_row,
                "product_name": _cell_text(values[headers["*商品名称"]]),
                "product_code": product_code,
                "quantity": _cell_text(values[headers["*申请数量"]]),
                "tracking": _cell_text(values[headers["快递单号"]]),
                "barcode_69": _cell_text(values[headers["69码"]]),
            }
        )
    sheet_title = sheet.title
    workbook.close()
    stock_rows = [
        row
        for row in authority_rows
        if row["tracking"] != str(args.out_of_stock_text).strip()
    ]
    out_of_stock_rows = [row for row in authority_rows if row not in stock_rows]

    def duplicate_values(rows_value: list[dict[str, Any]], key: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in rows_value:
            value = str(row[key]).casefold()
            counts[value] = counts.get(value, 0) + 1
        return {value: count for value, count in counts.items() if count > 1}

    catalog = load_product_rag(KNOWLEDGE_ROOT)
    products = list(catalog["products"])
    by_code = {str(product["product_code"]).casefold(): product for product in products}
    stock_codes = {str(row["product_code"]).casefold() for row in stock_rows}
    matches: list[dict[str, Any]] = []
    missing_products: list[dict[str, Any]] = []
    field_mismatches: list[dict[str, Any]] = []
    for row in stock_rows:
        product = by_code.get(str(row["product_code"]).casefold())
        if product is None:
            missing_products.append(row)
            continue
        name_result = _name_compatibility(str(row["product_name"]), product)
        barcode_match = str(row["barcode_69"]) == str(product["barcode_69"])
        match = {
            "excel_row": row["excel_row"],
            "product_code": row["product_code"],
            "excel_product_name": row["product_name"],
            "catalog_product_name": str(product["product_name"]),
            "excel_barcode_69": row["barcode_69"],
            "catalog_barcode_69": str(product["barcode_69"]),
            "product_code_match": True,
            "barcode_match": barcode_match,
            "name_match_status": name_result["status"],
            "name_score": name_result["score"],
            "name_compatible": name_result["compatible"],
        }
        matches.append(match)
        if not barcode_match or not name_result["compatible"]:
            field_mismatches.append(match)

    catalog_extra = [
        {
            "product_code": str(product["product_code"]),
            "product_name": str(product["product_name"]),
            "barcode_69": str(product["barcode_69"]),
        }
        for product in products
        if str(product["product_code"]).casefold() not in stock_codes
    ]
    duplicate_stock_codes = duplicate_values(stock_rows, "product_code")
    duplicate_all_codes = duplicate_values(authority_rows, "product_code")
    passed = not any(
        (
            missing_products,
            catalog_extra,
            field_mismatches,
            duplicate_stock_codes,
            duplicate_all_codes,
        )
    ) and len(stock_rows) == len(products)
    result = {
        "passed": passed,
        "workbook": str(workbook_path),
        "sheet": sheet_title,
        "out_of_stock_marker": str(args.out_of_stock_text).strip(),
        "workbook_rows": len(authority_rows),
        "out_of_stock_rows": len(out_of_stock_rows),
        "expected_stock_products": len(stock_rows),
        "catalog_products": len(products),
        "catalog_views": sum(len(product.get("views") or []) for product in products),
        "matched_products": len(matches),
        "exact_barcode_matches": sum(item["barcode_match"] for item in matches),
        "exact_name_matches": sum(item["name_match_status"] == "exact" for item in matches),
        "fuzzy_name_matches": sum(item["name_match_status"] == "fuzzy" for item in matches),
        "missing_products": missing_products,
        "catalog_extra_products": catalog_extra,
        "field_mismatches": field_mismatches,
        "duplicate_stock_product_codes": duplicate_stock_codes,
        "duplicate_all_product_codes": duplicate_all_codes,
        "out_of_stock_products": out_of_stock_rows,
        "matches": matches,
    }
    if args.output:
        _write_json(Path(args.output).resolve(), result)
    print(
        json.dumps(
            {
                key: value
                for key, value in result.items()
                if key not in {"matches", "out_of_stock_products"}
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
    return 0 if passed else 2


def main() -> int:
    args = _parse_args()
    if args.command == "match":
        return _match(args)
    if args.command == "apply":
        return _apply(args)
    return _reconcile(args)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
