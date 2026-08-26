from __future__ import annotations

import argparse
import base64
import copy
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.common import (
    AuditError,
    load_json,
    sha256_file,
    validate_json,
    write_json,
)
from audit_core.product_rag import (
    clear_product_rag_cache,
    ean13_is_valid,
    load_product_rag,
)
from ingest_product_reference import (
    CATALOG_PATH,
    IMAGE_SUFFIXES,
    PRODUCTS_ROOT,
    SCHEMA_PATH,
    _acquire_lock,
    _catalog_product_directory,
    _relative_text,
    _release_lock,
    _write_catalog_atomic,
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
PRODUCT_CODE_PATTERN = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
)
WEIGHT_OR_VOLUME_PATTERN = re.compile(
    r"[（(]\s*([0-9]+(?:\.[0-9]+)?\s*(?:ml|g))\s*[）)]",
    re.IGNORECASE,
)
COUNT_PACKAGE_PATTERN = re.compile(
    r"[（(]\s*((?:单|双|[0-9]+)\s*支装)\s*[）)]"
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "以产品编码为唯一主键查询商品映射接口；产品编码固定，"
            "产品名称和69码只取该编码的唯一精确返回。"
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser(
        "fetch",
        help="按受控商品目录中的产品编码查询接口并保存无凭据响应",
    )
    fetch.add_argument("--output", required=True)
    fetch.add_argument("--product-code", action="append", default=[])
    fetch.add_argument("--request-gap-ms", type=int, default=120)

    plan = subparsers.add_parser(
        "plan",
        help="根据产品编码接口响应生成失败关闭的目录和catalog同步计划",
    )
    plan.add_argument("--mapping-file", required=True)
    plan.add_argument("--output", required=True)

    apply_parser = subparsers.add_parser(
        "apply",
        help="原子应用已验证的产品名称、69码和目录同步计划",
    )
    apply_parser.add_argument("--plan-file", required=True)
    apply_parser.add_argument("--confirm", action="store_true")

    register = subparsers.add_parser(
        "register",
        help="把同步计划覆盖但尚未登记的受控目录及图片加入正式catalog",
    )
    register.add_argument("--plan-file", required=True)
    register.add_argument("--confirm", action="store_true")
    return parser.parse_args()


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


def _read_secret(label: str, environment_name: str) -> str:
    value = os.environ.get(environment_name, "").strip()
    if not value:
        value = _windows_user_environment(environment_name)
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


def _signed_request(
    access_key: str,
    secret_key: str,
    product_code: str,
) -> urllib.request.Request:
    query = urllib.parse.urlencode({"product_code": product_code})
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


def _fetch_one(
    access_key: str,
    secret_key: str,
    product_code: str,
) -> dict[str, Any]:
    last_error: Exception | None = None
    for attempt in range(1, 4):
        request = _signed_request(access_key, secret_key, product_code)
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                payload = json.loads(response.read().decode("utf-8"))
                return {
                    "query_field": "product_code",
                    "product_code": product_code,
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
                "query_field": "product_code",
                "product_code": product_code,
                "http_status": int(exc.code),
                "api_status": payload.get("status"),
                "msg": payload.get("msg") or "http_error",
                "list": list((payload.get("data") or {}).get("list") or []),
            }
        except (OSError, TimeoutError, json.JSONDecodeError) as exc:
            last_error = exc
            if attempt < 3:
                time.sleep(0.3 * attempt)
    raise AuditError(
        "商品映射接口连续三次失败："
        f"product_code={product_code}，error={last_error}"
    )


def _write_neutral_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _parse_product_directory_name(directory: str) -> dict[str, str]:
    barcode, separator, remainder = directory.partition("__")
    if not separator:
        if PRODUCT_CODE_PATTERN.fullmatch(directory) is None:
            raise AuditError(f"商品目录名不符合三字段约定或产品编码暂存约定：{directory}")
        return {
            "barcode_69": "",
            "product_name": "",
            "product_code": directory,
        }
    product_name, last_separator, product_code = remainder.rpartition("__")
    if not last_separator or not barcode or not product_name:
        raise AuditError(f"商品目录名不符合三字段约定：{directory}")
    if PRODUCT_CODE_PATTERN.fullmatch(product_code) is None:
        raise AuditError(f"商品目录产品编码无效：{directory}")
    return {
        "barcode_69": barcode,
        "product_name": product_name,
        "product_code": product_code,
    }


def _parse_product_directory(path: Path) -> dict[str, Any]:
    identity = _parse_product_directory_name(path.name)
    if path.is_symlink() or not path.is_dir() or path.resolve().parent != PRODUCTS_ROOT.resolve():
        raise AuditError(f"商品目录不存在、越界或不是直接子目录：{path}")
    return {
        "path": path.resolve(),
        "directory": path.name,
        **identity,
    }


def _inventory() -> list[dict[str, Any]]:
    products = [
        _parse_product_directory(path)
        for path in sorted(PRODUCTS_ROOT.iterdir(), key=lambda item: item.name.casefold())
        if path.is_dir()
    ]
    by_code: dict[str, str] = {}
    for product in products:
        code = str(product["product_code"])
        if code in by_code:
            raise AuditError(
                f"受控目录产品编码重复：{code}="
                f"{by_code[code]} / {product['directory']}"
            )
        by_code[code] = str(product["directory"])
    return products


def _fetch(args: argparse.Namespace) -> dict[str, Any]:
    if args.request_gap_ms < 0 or args.request_gap_ms > 5_000:
        raise AuditError("request-gap-ms 必须在0到5000之间")
    inventory = _inventory()
    catalog_codes = {str(item["product_code"]) for item in inventory}
    product_codes = sorted(set(args.product_code) or catalog_codes)
    unknown = sorted(set(product_codes) - catalog_codes)
    if unknown:
        raise AuditError(f"请求含目录外产品编码：{unknown[0]}")

    access_key = _read_secret("Access Key", "CANBAN_API_ACCESS_KEY")
    secret_key = _read_secret("Secret Key", "CANBAN_API_SECRET_KEY")
    if not access_key or not secret_key:
        raise AuditError("Access Key 和 Secret Key 均不能为空")

    results: list[dict[str, Any]] = []
    for index, product_code in enumerate(product_codes, start=1):
        results.append(_fetch_one(access_key, secret_key, product_code))
        if index % 10 == 0 or index == len(product_codes):
            print(f"queried {index}/{len(product_codes)}", file=sys.stderr)
        if index != len(product_codes):
            time.sleep(args.request_gap_ms / 1000)
    output = Path(args.output).resolve()
    _write_neutral_json(output, results)
    return {
        "status": "fetched",
        "query_field": "product_code",
        "requested": len(product_codes),
        "http_200": sum(item["http_status"] == 200 for item in results),
        "api_200": sum(item["api_status"] == 200 for item in results),
        "output": str(output),
    }


def _read_mapping_value(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AuditError(f"mapping-file 无法读取为JSON：{path}") from exc
    if not isinstance(value, list):
        raise AuditError("mapping-file 顶层必须是数组")
    if not all(isinstance(item, dict) for item in value):
        raise AuditError("mapping-file 含非对象条目")
    return value


def _exact_mapping_index(value: list[dict[str, Any]]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    for item in value:
        code = str(
            item.get("product_code")
            or item.get("knowledge_product_code")
            or ""
        ).strip()
        if PRODUCT_CODE_PATTERN.fullmatch(code) is None:
            raise AuditError(f"mapping-file 产品编码无效：{code}")
        if code in result:
            raise AuditError(f"mapping-file 产品编码重复：{code}")
        api_status = item.get("api_status")
        if api_status not in (200, "200"):
            raise AuditError(f"接口未成功返回产品编码：{code}，status={api_status}")
        raw_rows = item.get("exact_rows")
        if raw_rows is None:
            raw_rows = item.get("list") or []
        candidates: dict[tuple[str, str, str], dict[str, str]] = {}
        for raw in raw_rows:
            if not isinstance(raw, dict):
                continue
            candidate_code = str(raw.get("product_code") or "").strip()
            candidate_name = str(raw.get("product_name") or "").strip()
            candidate_barcode = str(raw.get("barcode") or "").strip()
            if candidate_code != code:
                continue
            if not candidate_name or not candidate_barcode:
                continue
            key = (candidate_code, candidate_name, candidate_barcode)
            candidates[key] = {
                "product_code": candidate_code,
                "product_name": candidate_name,
                "barcode": candidate_barcode,
            }
        if len(candidates) != 1:
            raise AuditError(
                "产品编码没有唯一精确接口返回："
                f"{code}，exact_count={len(candidates)}"
            )
        candidate = next(iter(candidates.values()))
        if not ean13_is_valid(candidate["barcode"]):
            raise AuditError(
                f"接口69码未通过EAN-13校验：{code}={candidate['barcode']}"
            )
        result[code] = candidate
    return result


def _path_key(path: Path) -> str:
    return os.path.normcase(str(path.resolve()))


def _build_plan(args: argparse.Namespace) -> dict[str, Any]:
    mapping_path = Path(args.mapping_file).resolve()
    mappings = _exact_mapping_index(_read_mapping_value(mapping_path))
    inventory = _inventory()
    inventory_codes = {str(item["product_code"]) for item in inventory}
    missing = sorted(inventory_codes - set(mappings))
    if missing:
        raise AuditError(f"商品目录缺少产品编码接口返回：{missing[0]}")

    catalog = load_product_rag(SKILL_ROOT)
    catalog_codes = {str(item["product_code"]) for item in catalog["products"]}
    products: list[dict[str, Any]] = []
    target_keys: set[str] = set()
    source_keys = {_path_key(Path(item["path"])) for item in inventory}
    for item in inventory:
        code = str(item["product_code"])
        candidate = mappings[code]
        target = _catalog_product_directory(
            candidate["barcode"],
            candidate["product_name"],
            code,
        ).resolve()
        target_key = _path_key(target)
        if target_key in target_keys:
            raise AuditError(f"多个产品编码生成同一目标目录：{target.name}")
        target_keys.add(target_key)
        source = Path(item["path"])
        rename_required = source.name != target.name
        if rename_required and target.exists() and target_key not in source_keys:
            raise AuditError(f"目标目录已存在，拒绝覆盖：{target}")
        if len(str(target)) > 245:
            raise AuditError(f"目标商品目录路径过长：{target}")
        for child in source.iterdir():
            if child.is_file() and len(str(target / child.name)) > 259:
                raise AuditError(f"目标图片路径过长：{target / child.name}")
        products.append(
            {
                "product_code": code,
                "current_barcode_69": str(item["barcode_69"]),
                "current_product_name": str(item["product_name"]),
                "interface_barcode_69": candidate["barcode"],
                "interface_product_name": candidate["product_name"],
                "source_directory": source.name,
                "target_directory": target.name,
                "rename_required": rename_required,
                "catalog_registered": code in catalog_codes,
            }
        )

    plan = {
        "schema_version": "2.0",
        "identity_key": "product_code",
        "mapping_policy": (
            "产品编码固定并精确查询；产品名称和69码只取该编码的唯一接口返回；"
            "Windows禁用目录字符使用等义全角字符；图片文件名和内容不变。"
        ),
        "products": products,
    }
    output = Path(args.output).resolve()
    _write_neutral_json(output, plan)
    return {
        "status": "planned",
        "products": len(products),
        "directory_renames": sum(item["rename_required"] for item in products),
        "catalog_products": sum(item["catalog_registered"] for item in products),
        "unregistered_directories": sum(
            not item["catalog_registered"] for item in products
        ),
        "output": str(output),
    }


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)


def _updated_product_id(product_id: str, old_barcode: str, new_barcode: str) -> str:
    if old_barcode == new_barcode:
        return product_id
    if old_barcode not in product_id:
        raise AuditError(
            "69码变化但product_id不含旧69码，拒绝猜测新ID："
            f"{product_id}，old={old_barcode}，new={new_barcode}"
        )
    return product_id.replace(old_barcode, new_barcode, 1)


def _update_catalog_product(
    product: dict[str, Any],
    plan_item: dict[str, Any],
) -> tuple[bool, bool]:
    code = str(product["product_code"])
    old_name = str(product["product_name"])
    old_barcode = str(product["barcode_69"])
    new_name = str(plan_item["interface_product_name"])
    new_barcode = str(plan_item["interface_barcode_69"])
    source_directory = str(plan_item["source_directory"])
    target_directory = str(plan_item["target_directory"])

    aliases = [str(value) for value in product.get("aliases") or []]
    aliases = list(dict.fromkeys(value for value in aliases if value != new_name))
    if old_name != new_name:
        _append_unique(aliases, old_name)
    product["aliases"] = aliases
    product["product_name"] = new_name
    product["barcode_69"] = new_barcode
    product["product_id"] = _updated_product_id(
        str(product["product_id"]),
        old_barcode,
        new_barcode,
    )

    anchors = product["identity_anchors"]
    preserved = [
        str(value)
        for value in anchors.get("strong") or []
        if not str(value).startswith(("产品名称 ", "产品编码 ", "69码 "))
    ]
    strong = [
        f"产品名称 {new_name}",
        f"产品编码 {code}",
        f"69码 {new_barcode}",
    ]
    for alias_code in product.get("product_code_aliases") or []:
        _append_unique(strong, f"产品编码 {alias_code}")
    for value in preserved:
        _append_unique(strong, value)
    anchors["strong"] = strong

    for view in product.get("views") or []:
        relative = Path(str(view["image_file"]))
        if relative.parent.name != source_directory:
            raise AuditError(
                "catalog视图目录与产品编码计划不一致："
                f"{code}={relative.parent.name}!={source_directory}"
            )
        view["image_file"] = (
            relative.parent.parent / target_directory / relative.name
        ).as_posix()
        visible_anchors = [str(value) for value in view.get("visible_anchors") or []]
        view["visible_anchors"] = [
            f"69码 {new_barcode}"
            if old_barcode != new_barcode and value == f"69码 {old_barcode}"
            else value
            for value in visible_anchors
        ]
    return old_name != new_name, old_barcode != new_barcode


def _apply(args: argparse.Namespace) -> dict[str, Any]:
    plan_path = Path(args.plan_file).resolve()
    plan = load_json(plan_path)
    if (
        plan.get("schema_version") != "2.0"
        or plan.get("identity_key") != "product_code"
        or not isinstance(plan.get("products"), list)
    ):
        raise AuditError("plan-file 结构或产品编码主键声明无效")

    inventory = _inventory()
    inventory_by_code = {str(item["product_code"]): item for item in inventory}
    plan_by_code: dict[str, dict[str, Any]] = {}
    for raw in plan["products"]:
        if not isinstance(raw, dict):
            raise AuditError("plan-file 含非对象商品条目")
        code = str(raw.get("product_code") or "")
        if PRODUCT_CODE_PATTERN.fullmatch(code) is None or code in plan_by_code:
            raise AuditError(f"plan-file 产品编码无效或重复：{code}")
        if str(raw.get("interface_barcode_69") or "") == "":
            raise AuditError(f"plan-file 接口69码为空：{code}")
        if not ean13_is_valid(str(raw["interface_barcode_69"])):
            raise AuditError(f"plan-file 接口69码无效：{code}")
        expected_target = _catalog_product_directory(
            str(raw["interface_barcode_69"]),
            str(raw.get("interface_product_name") or ""),
            code,
        ).name
        if expected_target != str(raw.get("target_directory") or ""):
            raise AuditError(f"plan-file 目标目录不是接口字段的确定性结果：{code}")
        plan_by_code[code] = raw

    if set(plan_by_code) != set(inventory_by_code):
        missing = sorted(set(inventory_by_code) - set(plan_by_code))
        extra = sorted(set(plan_by_code) - set(inventory_by_code))
        raise AuditError(
            "plan-file 必须精确覆盖当前商品目录："
            f"missing={missing[:1]}，extra={extra[:1]}"
        )

    move_pairs: list[tuple[Path, Path]] = []
    source_keys = {_path_key(Path(item["path"])) for item in inventory}
    target_keys: set[str] = set()
    for code, item in inventory_by_code.items():
        raw = plan_by_code[code]
        if (
            str(raw.get("source_directory")) != str(item["directory"])
            or str(raw.get("current_barcode_69")) != str(item["barcode_69"])
            or str(raw.get("current_product_name")) != str(item["product_name"])
        ):
            raise AuditError(f"计划后商品目录状态发生变化：{code}")
        source = Path(item["path"])
        target = (PRODUCTS_ROOT / str(raw["target_directory"])).resolve()
        target_key = _path_key(target)
        if target_key in target_keys:
            raise AuditError(f"多个目录计划迁移到同一目标：{target}")
        target_keys.add(target_key)
        if source.name != target.name:
            if target.exists() and target_key not in source_keys:
                raise AuditError(f"迁移目标已存在，拒绝覆盖：{target}")
            move_pairs.append((source, target))

    catalog = copy.deepcopy(load_product_rag(SKILL_ROOT))
    catalog_by_code = {
        str(product["product_code"]): product for product in catalog["products"]
    }
    missing_catalog_plan = sorted(set(catalog_by_code) - set(plan_by_code))
    if missing_catalog_plan:
        raise AuditError(f"catalog商品不在同步计划中：{missing_catalog_plan[0]}")

    name_updates = 0
    barcode_updates = 0
    for code, product in catalog_by_code.items():
        name_changed, barcode_changed = _update_catalog_product(
            product,
            plan_by_code[code],
        )
        name_updates += int(name_changed)
        barcode_updates += int(barcode_changed)
    validate_json(catalog, SCHEMA_PATH)
    normalized_names = [
        str(product["product_name"]).strip().casefold()
        for product in catalog["products"]
    ]
    if len(normalized_names) != len(set(normalized_names)):
        raise AuditError("接口产品名称写入后造成catalog名称重复")

    if not args.confirm:
        return {
            "status": "dry_run",
            "products": len(inventory),
            "directory_moves": len(move_pairs),
            "catalog_name_updates": name_updates,
            "catalog_barcode_updates": barcode_updates,
            "unregistered_directories": len(inventory) - len(catalog["products"]),
        }

    token = secrets.token_hex(8)
    move_records = [
        {
            "source": source,
            "stage": PRODUCTS_ROOT / f".product-identity-{token}-{index:03d}",
            "target": target,
            "state": "source",
        }
        for index, (source, target) in enumerate(move_pairs, start=1)
    ]
    for record in move_records:
        if Path(record["stage"]).exists():
            raise AuditError(f"临时迁移目录已存在：{record['stage']}")

    descriptor = _acquire_lock()
    original_catalog = CATALOG_PATH.read_bytes()
    temporary_catalog = CATALOG_PATH.with_name(
        CATALOG_PATH.name + f".identity-{token}.tmp"
    )
    catalog_replaced = False
    try:
        for record in move_records:
            source = Path(record["source"])
            target = Path(record["target"])
            if not source.is_dir() or (
                target.exists() and _path_key(target) != _path_key(source)
            ):
                raise AuditError(f"加锁后目录状态发生变化：{source} -> {target}")
        write_json(temporary_catalog, catalog)
        validate_json(load_json(temporary_catalog), SCHEMA_PATH)

        for record in move_records:
            os.replace(record["source"], record["stage"])
            record["state"] = "stage"
        for record in move_records:
            os.replace(record["stage"], record["target"])
            record["state"] = "target"

        os.replace(temporary_catalog, CATALOG_PATH)
        catalog_replaced = True
        clear_product_rag_cache()
        load_product_rag(SKILL_ROOT)
    except Exception:
        temporary_catalog.unlink(missing_ok=True)
        if catalog_replaced:
            CATALOG_PATH.write_bytes(original_catalog)
            clear_product_rag_cache()
        for record in reversed(move_records):
            source = Path(record["source"])
            stage = Path(record["stage"])
            target = Path(record["target"])
            if record["state"] == "target" and target.exists() and not source.exists():
                os.replace(target, source)
                record["state"] = "source"
            elif record["state"] == "stage" and stage.exists() and not source.exists():
                os.replace(stage, source)
                record["state"] = "source"
        raise
    finally:
        _release_lock(descriptor)

    return {
        "status": "applied",
        "products": len(inventory),
        "directory_moves": len(move_pairs),
        "catalog_name_updates": name_updates,
        "catalog_barcode_updates": barcode_updates,
        "unregistered_directories": len(inventory) - len(catalog["products"]),
    }


def _specification_from_product_name(product_name: str) -> str:
    weight_or_volume = WEIGHT_OR_VOLUME_PATTERN.search(product_name)
    if weight_or_volume is not None:
        return re.sub(r"\s+", "", weight_or_volume.group(1))
    count_package = COUNT_PACKAGE_PATTERN.search(product_name)
    if count_package is not None:
        return re.sub(r"\s+", "", count_package.group(1))
    return "未标注"


def _product_code_slug(product_code: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", product_code.casefold()).strip("-")
    if not slug:
        raise AuditError(f"产品编码无法生成catalog ID：{product_code}")
    return slug


def _catalog_product_id(
    barcode: str,
    product_code: str,
    *,
    shared_barcode: bool,
) -> str:
    base = f"canban-{barcode}"
    if not shared_barcode:
        return base
    return f"{base}-{_product_code_slug(product_code)}"


def _catalog_views(directory: Path, product_code: str) -> list[dict[str, Any]]:
    children = sorted(directory.iterdir(), key=lambda item: item.name.casefold())
    nested = next((item for item in children if item.is_dir()), None)
    if nested is not None:
        raise AuditError(f"待登记商品目录不得含嵌套目录：{nested}")
    unsupported = next(
        (
            item
            for item in children
            if item.is_file() and item.suffix.casefold() not in IMAGE_SUFFIXES
        ),
        None,
    )
    if unsupported is not None:
        raise AuditError(f"待登记商品目录含非图片文件：{unsupported}")
    images = [
        item
        for item in children
        if item.is_file() and item.suffix.casefold() in IMAGE_SUFFIXES
    ]
    if not images:
        raise AuditError(f"待登记商品目录没有图片：{directory}")

    source_id = f"kb-{_product_code_slug(product_code)}"
    views: list[dict[str, Any]] = []
    for index, image_path in enumerate(images, start=1):
        if image_path.is_symlink():
            raise AuditError(f"待登记商品图片不得是符号链接：{image_path}")
        try:
            with Image.open(image_path) as image:
                width, height = image.size
                image.verify()
        except Exception as exc:
            raise AuditError(f"待登记商品图片无法验证：{image_path}") from exc
        views.append(
            {
                "view_id": f"{source_id}-v{index:02d}",
                "face": "unclassified",
                "image_file": _relative_text(image_path),
                "source_original_name": image_path.name,
                "sha256": sha256_file(image_path),
                "width": width,
                "height": height,
                "identity_strength": "unreviewed",
                "visible_anchors": [],
                "limitations": [
                    "未单独标注物理面；使用时必须与现场可见文字、69码或包装锚点逐图比较",
                    "原始素材来源未登记；仅保留受控目录内加入catalog时的原文件名",
                ],
            }
        )
    return views


def _new_catalog_product(
    *,
    plan_item: dict[str, Any],
    directory: Path,
    shared_barcode: bool,
) -> dict[str, Any]:
    product_code = str(plan_item["product_code"])
    product_name = str(plan_item["interface_product_name"])
    barcode = str(plan_item["interface_barcode_69"])
    specification = _specification_from_product_name(product_name)
    return {
        "product_id": _catalog_product_id(
            barcode,
            product_code,
            shared_barcode=shared_barcode,
        ),
        "brand": "参半",
        "product_name": product_name,
        "product_code": product_code,
        "barcode_69": barcode,
        "specification": specification,
        "variant": None,
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
            "supporting": [f"规格 {specification}"],
        },
        "non_identity_fields": [
            "防伪二维码及其内容",
            "批次、生产日期、限用日期等可变喷码",
            "包装折痕、破损、反光和拍摄背景",
        ],
        "sources": [],
        "views": _catalog_views(directory, product_code),
    }


def _register(args: argparse.Namespace) -> dict[str, Any]:
    plan_path = Path(args.plan_file).resolve()
    plan = load_json(plan_path)
    if (
        plan.get("schema_version") != "2.0"
        or plan.get("identity_key") != "product_code"
        or not isinstance(plan.get("products"), list)
    ):
        raise AuditError("plan-file 结构或产品编码主键声明无效")

    inventory = _inventory()
    inventory_by_code = {str(item["product_code"]): item for item in inventory}
    plan_by_code: dict[str, dict[str, Any]] = {}
    for raw in plan["products"]:
        if not isinstance(raw, dict):
            raise AuditError("plan-file 含非对象商品条目")
        code = str(raw.get("product_code") or "")
        if PRODUCT_CODE_PATTERN.fullmatch(code) is None or code in plan_by_code:
            raise AuditError(f"plan-file 产品编码无效或重复：{code}")
        barcode = str(raw.get("interface_barcode_69") or "")
        product_name = str(raw.get("interface_product_name") or "")
        if not product_name:
            raise AuditError(f"plan-file 接口产品名称为空：{code}")
        if not ean13_is_valid(barcode):
            raise AuditError(f"plan-file 接口69码无效：{code}")
        target = _catalog_product_directory(barcode, product_name, code).name
        if target != str(raw.get("target_directory") or ""):
            raise AuditError(f"plan-file 目标目录不是接口字段的确定性结果：{code}")
        plan_by_code[code] = raw

    if set(plan_by_code) != set(inventory_by_code):
        missing = sorted(set(inventory_by_code) - set(plan_by_code))
        extra = sorted(set(plan_by_code) - set(inventory_by_code))
        raise AuditError(
            "plan-file 必须精确覆盖当前商品目录："
            f"missing={missing[:1]}，extra={extra[:1]}"
        )
    for code, item in inventory_by_code.items():
        raw = plan_by_code[code]
        if str(item["directory"]) != str(raw["target_directory"]):
            raise AuditError(f"商品目录尚未按接口计划同步，拒绝登记：{code}")
        if str(item["barcode_69"]) != str(raw["interface_barcode_69"]):
            raise AuditError(f"商品目录69码与接口计划不一致：{code}")

    catalog = copy.deepcopy(load_product_rag(SKILL_ROOT))
    existing_codes = {
        str(product["product_code"]) for product in catalog["products"]
    }
    missing_codes = sorted(set(inventory_by_code) - existing_codes)
    barcode_counts = Counter(
        str(product["barcode_69"]) for product in catalog["products"]
    )
    barcode_counts.update(
        str(plan_by_code[code]["interface_barcode_69"])
        for code in missing_codes
    )

    additions = [
        _new_catalog_product(
            plan_item=plan_by_code[code],
            directory=Path(inventory_by_code[code]["path"]),
            shared_barcode=(
                barcode_counts[
                    str(plan_by_code[code]["interface_barcode_69"])
                ]
                > 1
            ),
        )
        for code in missing_codes
    ]
    catalog["products"].extend(additions)
    validate_json(catalog, SCHEMA_PATH)

    product_ids = [str(product["product_id"]) for product in catalog["products"]]
    product_names = [
        str(product["product_name"]).strip().casefold()
        for product in catalog["products"]
    ]
    product_codes = [
        str(product["product_code"]).strip().casefold()
        for product in catalog["products"]
    ]
    view_ids = [
        str(view["view_id"])
        for product in catalog["products"]
        for view in product.get("views") or []
    ]
    for values, label in (
        (product_ids, "product_id"),
        (product_names, "产品名称"),
        (product_codes, "产品编码"),
        (view_ids, "view_id"),
    ):
        if len(values) != len(set(values)):
            raise AuditError(f"登记后catalog的{label}重复")

    result = {
        "status": "dry_run" if not args.confirm else "registered",
        "catalog_products_before": len(catalog["products"]) - len(additions),
        "catalog_products_after": len(catalog["products"]),
        "registered_products": len(additions),
        "registered_views": sum(len(item["views"]) for item in additions),
        "remaining_unregistered_directories": (
            len(inventory) - len(catalog["products"])
        ),
    }
    if not args.confirm:
        return result

    descriptor = _acquire_lock()
    original_catalog = CATALOG_PATH.read_bytes()
    try:
        _write_catalog_atomic(catalog, original_catalog)
    finally:
        _release_lock(descriptor)
    return result


def main() -> int:
    args = _parse_args()
    if args.command == "fetch":
        result = _fetch(args)
    elif args.command == "plan":
        result = _build_plan(args)
    elif args.command == "apply":
        result = _apply(args)
    else:
        result = _register(args)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
