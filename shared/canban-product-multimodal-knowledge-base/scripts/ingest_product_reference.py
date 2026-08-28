from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any

from PIL import Image


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.common import AuditError, load_json, sha256_file, validate_json, write_json
from audit_core.product_rag import (
    clear_product_rag_cache,
    ean13_is_valid,
    load_pending_product_rag,
    load_product_rag,
)


CATALOG_PATH = SKILL_ROOT / "references" / "product-rag.json"
SCHEMA_PATH = SKILL_ROOT / "references" / "product-rag.schema.json"
KNOWLEDGE_ROOT = SKILL_ROOT
KNOWLEDGE_ASSET_ROOT = KNOWLEDGE_ROOT.parent
PRODUCTS_ROOT = KNOWLEDGE_ROOT / "products"
PENDING_ROOT = KNOWLEDGE_ROOT / "待补69码"
PENDING_MANIFEST = KNOWLEDGE_ROOT / "pending-barcode.json"
PENDING_SCHEMA_PATH = SKILL_ROOT / "references" / "product-rag-pending.schema.json"
LOCK_PATH = KNOWLEDGE_ROOT / ".ingest.lock"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
SOURCE_COLLECTIONS = {
    "参半牙具": SKILL_ROOT / "参半牙具",
    "参半样品": SKILL_ROOT / "参半样品",
    "参半样品图（31个）": SKILL_ROOT / "参半样品图（31个）",
}
SOURCE_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{2,49}$")
FOLDER_CONTROL_CHARS = re.compile(r"[\x00-\x1f]+")
WINDOWS_FOLDER_REPLACEMENTS = str.maketrans(
    {
        "\\": "＼",
        "/": "／",
        ":": "：",
        "*": "×",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
)
PRODUCT_CODE_FOLDER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_PRODUCT_FOLDER_LABEL_LENGTH = 80


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="一次只导入一个已人工确认的参半商品素材文件夹。",
    )
    parser.add_argument("--source-dir", required=True)
    parser.add_argument("--source-collection", required=True, choices=sorted(SOURCE_COLLECTIONS))
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--product-name", required=True)
    parser.add_argument("--product-code", default="未标注")
    parser.add_argument("--specification", required=True)
    parser.add_argument("--variant")
    parser.add_argument(
        "--match-policy",
        choices=("exact_or_candidate", "candidate_only"),
        default="exact_or_candidate",
    )
    identity = parser.add_mutually_exclusive_group(required=True)
    identity.add_argument("--barcode")
    identity.add_argument("--pending-barcode", action="store_true")
    parser.add_argument(
        "--pending-reason",
        default="本地参考图未露出可核验的完整69码，禁止猜测，等待补充背标证据。",
    )
    return parser.parse_args()


def _relative_text(path: Path) -> str:
    resolved = path.resolve()
    if not resolved.is_relative_to(KNOWLEDGE_ROOT):
        raise AuditError(f"路径越出共享商品知识库：{resolved}")
    return resolved.relative_to(KNOWLEDGE_ASSET_ROOT).as_posix()


def _safe_product_folder_label(product_name: str) -> str:
    """Keep the authoritative product name readable while making it Windows-safe.

    Product-code synchronization treats the interface product name as an
    authoritative field.  Preserve its spaces, punctuation, and channel text;
    translate only characters Windows forbids in a directory name.  Refuse an
    overlong label instead of silently truncating a business field.
    """

    label = FOLDER_CONTROL_CHARS.sub("-", product_name.strip())
    label = label.translate(WINDOWS_FOLDER_REPLACEMENTS).rstrip(". ")
    if not label:
        raise AuditError("产品名无法生成安全的知识库目录标签")
    if len(label) > MAX_PRODUCT_FOLDER_LABEL_LENGTH:
        raise AuditError(
            "产品名超过知识库目录标签长度上限，禁止静默截断："
            f"{len(label)}>{MAX_PRODUCT_FOLDER_LABEL_LENGTH}"
        )
    return label


def _catalog_product_directory(
    barcode: str,
    product_name: str,
    product_code: str = "未标注",
) -> Path:
    base = f"{barcode}__{_safe_product_folder_label(product_name)}"
    if product_code == "未标注":
        return PRODUCTS_ROOT / base
    if PRODUCT_CODE_FOLDER_PATTERN.fullmatch(product_code) is None:
        raise AuditError(f"产品编码不能安全用于知识库目录名：{product_code}")
    return PRODUCTS_ROOT / f"{base}__{product_code}"


def _pending_product_directory(source_id: str, product_name: str) -> Path:
    return PENDING_ROOT / f"{source_id}__{_safe_product_folder_label(product_name)}"


def _validate_source(args: argparse.Namespace) -> tuple[Path, list[Path]]:
    if not SOURCE_ID_PATTERN.fullmatch(args.source_id):
        raise AuditError("source-id 必须是 3-50 位小写字母、数字或连字符")
    source = Path(args.source_dir).resolve()
    expected_parent = SOURCE_COLLECTIONS[args.source_collection].resolve()
    if source.is_symlink() or not source.is_dir():
        raise AuditError(f"来源必须是存在的普通目录：{source}")
    if source.parent != expected_parent:
        raise AuditError(
            "每次只能导入所选来源集合的一个直接子文件夹："
            f"source={source}，expected_parent={expected_parent}"
        )
    children = sorted(source.iterdir(), key=lambda item: item.name.casefold())
    directories = [item for item in children if item.is_dir()]
    if directories:
        raise AuditError(f"单商品来源文件夹内不得再嵌套目录：{directories[0].name}")
    unsupported = [
        item for item in children
        if item.is_file() and item.suffix.lower() not in IMAGE_SUFFIXES
    ]
    if unsupported:
        raise AuditError(f"单商品来源文件夹含非图片文件：{unsupported[0].name}")
    images = [item for item in children if item.is_file()]
    if not images:
        raise AuditError(f"单商品来源文件夹没有图片：{source}")
    return source, images


def _acquire_lock() -> int:
    KNOWLEDGE_ROOT.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise AuditError("另一个商品导入仍在进行；本次拒绝并发处理") from exc
    os.write(descriptor, str(os.getpid()).encode("ascii"))
    return descriptor


def _release_lock(descriptor: int) -> None:
    os.close(descriptor)
    LOCK_PATH.unlink(missing_ok=True)


def _copy_views(
    *,
    images: list[Path],
    destination: Path,
    source_id: str,
    collection: str,
    source_folder: str,
) -> tuple[list[dict[str, Any]], list[Path]]:
    destination.mkdir(parents=True, exist_ok=True)
    targets = [
        destination / f"{source_id}-v{index:02d}{image.suffix.lower()}"
        for index, image in enumerate(images, start=1)
    ]
    collision = next((target for target in targets if target.exists()), None)
    if collision is not None:
        raise AuditError(f"目标视图已存在，拒绝覆盖：{collision}")

    views: list[dict[str, Any]] = []
    copied: list[Path] = []
    try:
        for index, (source, target) in enumerate(zip(images, targets, strict=True), start=1):
            with Image.open(source) as image:
                width, height = image.size
            source_hash = sha256_file(source)
            shutil.copy2(source, target)
            copied.append(target)
            target_hash = sha256_file(target)
            if target_hash != source_hash:
                raise AuditError(f"复制后图片哈希不一致：{source.name}")
            views.append(
                {
                    "view_id": f"{source_id}-v{index:02d}",
                    "face": "unclassified",
                    "image_file": _relative_text(target),
                    "source_original_name": source.name,
                    "source_id": source_id,
                    "source_collection": collection,
                    "source_folder": source_folder,
                    "sha256": target_hash,
                    "width": width,
                    "height": height,
                    "identity_strength": "unreviewed",
                    "visible_anchors": [],
                    "limitations": [
                        "未单独标注物理面；使用时必须与现场可见文字、69码或包装锚点逐图比较"
                    ],
                }
            )
    except Exception:
        for target in reversed(copied):
            target.unlink(missing_ok=True)
        raise
    return views, copied


def _append_unique(values: list[str], value: str | None) -> None:
    if value and value not in values:
        values.append(value)


def _source_record(args: argparse.Namespace, source: Path) -> dict[str, Any]:
    return {
        "source_id": args.source_id,
        "collection": args.source_collection,
        "source_folder": _relative_text(source),
        "observed_product_name": args.product_name,
        "observed_specification": args.specification,
        "observed_variant": args.variant,
    }


def _new_product(args: argparse.Namespace) -> dict[str, Any]:
    assert args.barcode is not None
    strong = [f"69码 {args.barcode}", f"产品名称 {args.product_name}"]
    if args.product_code != "未标注":
        strong.append(f"产品编码 {args.product_code}")
    supporting = [f"规格 {args.specification}"]
    if args.variant:
        supporting.append(f"款式/香型 {args.variant}")
    return {
        "product_id": f"canban-{args.barcode}",
        "brand": "参半",
        "product_name": args.product_name,
        "product_code": args.product_code,
        "barcode_69": args.barcode,
        "specification": args.specification,
        "variant": args.variant,
        "aliases": [],
        "variant_aliases": [],
        "specification_aliases": [],
        "product_code_aliases": [],
        "match_policy": args.match_policy,
        "identity_anchors": {
            "strong": strong,
            "supporting": supporting,
        },
        "non_identity_fields": [
            "防伪二维码及其内容",
            "批次、生产日期、限用日期等可变喷码",
            "包装折痕、破损、反光和拍摄背景",
        ],
        "sources": [],
        "views": [],
    }


def _merge_observation(product: dict[str, Any], args: argparse.Namespace) -> None:
    aliases = product.setdefault("aliases", [])
    variants = product.setdefault("variant_aliases", [])
    specifications = product.setdefault("specification_aliases", [])
    product_codes = product.setdefault("product_code_aliases", [])
    product.setdefault("sources", [])
    product.setdefault("match_policy", "exact_or_candidate")

    if args.product_name != product["product_name"]:
        _append_unique(aliases, args.product_name)
    if args.variant != product.get("variant"):
        _append_unique(variants, args.variant)
    if args.specification != product["specification"]:
        _append_unique(specifications, args.specification)
        product["match_policy"] = "candidate_only"
    if args.product_code != "未标注":
        if product["product_code"] == "未标注":
            product["product_code"] = args.product_code
            _append_unique(product["identity_anchors"]["strong"], f"产品编码 {args.product_code}")
        elif args.product_code != product["product_code"]:
            _append_unique(product_codes, args.product_code)
            product["match_policy"] = "candidate_only"
    if args.match_policy == "candidate_only":
        product["match_policy"] = "candidate_only"


def _write_catalog_atomic(catalog: dict[str, Any], original_bytes: bytes) -> None:
    temporary = CATALOG_PATH.with_name(CATALOG_PATH.name + ".tmp")
    write_json(temporary, catalog)
    try:
        candidate = load_json(temporary)
        validate_json(candidate, SCHEMA_PATH)
        os.replace(temporary, CATALOG_PATH)
        clear_product_rag_cache()
        load_product_rag(SKILL_ROOT)
    except Exception:
        temporary.unlink(missing_ok=True)
        CATALOG_PATH.write_bytes(original_bytes)
        clear_product_rag_cache()
        raise


def _ingest_catalog_product(
    args: argparse.Namespace,
    source: Path,
    images: list[Path],
) -> dict[str, Any]:
    assert args.barcode is not None
    if not args.barcode.startswith("69") or not ean13_is_valid(args.barcode):
        raise AuditError(f"69码必须以69开头并通过EAN-13校验：{args.barcode}")

    original_bytes = CATALOG_PATH.read_bytes()
    catalog = load_product_rag(SKILL_ROOT)
    products = list(catalog.get("products") or [])
    if any(
        args.source_id == str(source_item.get("source_id"))
        for product_item in products
        for source_item in product_item.get("sources") or []
    ):
        raise AuditError(f"source-id 已导入：{args.source_id}")

    product = next(
        (item for item in products if str(item["barcode_69"]) == args.barcode),
        None,
    )
    created = product is None
    if product is None:
        product = _new_product(args)
        products.append(product)
        catalog["products"] = products
    else:
        if (
            str(product["product_code"]) == "未标注"
            and args.product_code != "未标注"
            and product.get("views")
        ):
            raise AuditError(
                "已有商品从未标注补入产品编码时必须先使用受控商品编码同步器迁移旧目录"
            )
        _merge_observation(product, args)

    source_folder = _relative_text(source)
    # A product directory is already the asset boundary.  Keep the validated
    # barcode first for deterministic lookup, then freeze a readable product
    # name in the directory label; do not add a redundant images/ level.
    destination = _catalog_product_directory(
        args.barcode,
        str(product["product_name"]),
        str(product["product_code"]),
    )
    views, copied = _copy_views(
        images=images,
        destination=destination,
        source_id=args.source_id,
        collection=args.source_collection,
        source_folder=source_folder,
    )
    try:
        product.setdefault("sources", []).append(_source_record(args, source))
        product.setdefault("views", []).extend(views)
        _write_catalog_atomic(catalog, original_bytes)
    except Exception:
        for target in reversed(copied):
            target.unlink(missing_ok=True)
        raise
    return {
        "status": "created" if created else "merged",
        "product_id": product["product_id"],
        "barcode_69": args.barcode,
        "source_id": args.source_id,
        "images_added": len(views),
        "total_views": len(product["views"]),
        "match_policy": product.get("match_policy", "exact_or_candidate"),
    }


def _load_pending_manifest() -> dict[str, Any]:
    return load_pending_product_rag(SKILL_ROOT)


def _write_pending_atomic(value: dict[str, Any]) -> None:
    validate_json(value, PENDING_SCHEMA_PATH)
    temporary = PENDING_MANIFEST.with_name(PENDING_MANIFEST.name + ".tmp")
    write_json(temporary, value)
    os.replace(temporary, PENDING_MANIFEST)


def _ingest_pending_product(
    args: argparse.Namespace,
    source: Path,
    images: list[Path],
) -> dict[str, Any]:
    manifest = _load_pending_manifest()
    items = manifest["pending_products"]
    if any(str(item.get("pending_id")) == args.source_id for item in items):
        raise AuditError(f"待补69码 source-id 已导入：{args.source_id}")
    source_folder = _relative_text(source)
    destination = _pending_product_directory(args.source_id, args.product_name)
    views, copied = _copy_views(
        images=images,
        destination=destination,
        source_id=args.source_id,
        collection=args.source_collection,
        source_folder=source_folder,
    )
    try:
        items.append(
            {
                "pending_id": args.source_id,
                "brand": "参半",
                "product_name": args.product_name,
                "product_code": args.product_code,
                "specification": args.specification,
                "variant": args.variant,
                "reason": args.pending_reason,
                "source": _source_record(args, source),
                "views": views,
            }
        )
        _write_pending_atomic(manifest)
    except Exception:
        for target in reversed(copied):
            target.unlink(missing_ok=True)
        raise
    return {
        "status": "pending_barcode",
        "pending_id": args.source_id,
        "source_id": args.source_id,
        "images_added": len(views),
    }


def main() -> int:
    args = _parse_args()
    source, images = _validate_source(args)
    descriptor = _acquire_lock()
    try:
        if args.pending_barcode:
            result = _ingest_pending_product(args, source, images)
        else:
            result = _ingest_catalog_product(args, source, images)
    finally:
        _release_lock(descriptor)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except AuditError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(2) from exc
