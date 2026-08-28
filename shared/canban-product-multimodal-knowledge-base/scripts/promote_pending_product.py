from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from argparse import Namespace
from pathlib import Path
from typing import Any

from PIL import Image


SCRIPT_DIR = Path(__file__).resolve().parent
SKILL_ROOT = SCRIPT_DIR.parent
PROJECT_ROOT = SKILL_ROOT.parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from audit_core.common import AuditError, sha256_file, validate_json, write_json
from audit_core.product_rag import (
    clear_product_rag_cache,
    ean13_is_valid,
    load_pending_product_rag,
    load_product_rag,
)
from ingest_product_reference import (
    CATALOG_PATH,
    IMAGE_SUFFIXES,
    KNOWLEDGE_ASSET_ROOT,
    PENDING_MANIFEST,
    PENDING_ROOT,
    PENDING_SCHEMA_PATH,
    SCHEMA_PATH,
    SOURCE_ID_PATTERN,
    _acquire_lock,
    _catalog_product_directory,
    _new_product,
    _relative_text,
    _release_lock,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="将已经补齐有效69码的隔离商品原子晋升到正式多模态知识库。",
    )
    parser.add_argument("--pending-id", required=True)
    parser.add_argument("--barcode", required=True)
    return parser.parse_args()


def _pending_directory(item: dict[str, Any]) -> Path:
    views = list(item.get("views") or [])
    if not views:
        raise AuditError(f"待补商品没有参考图：{item['pending_id']}")
    directory = (KNOWLEDGE_ASSET_ROOT / str(views[0]["image_file"])).resolve().parent
    pending_root = PENDING_ROOT.resolve()
    if directory.parent != pending_root or directory.is_symlink() or not directory.is_dir():
        raise AuditError(f"待补商品目录不在受控隔离区：{directory}")
    if any(path.is_dir() for path in directory.iterdir()):
        raise AuditError(f"待补商品目录不得包含嵌套目录：{directory}")
    unsupported = [
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() not in IMAGE_SUFFIXES
    ]
    if unsupported:
        raise AuditError(f"待补商品目录包含非图片文件：{unsupported[0].name}")
    return directory


def _ordered_images(
    item: dict[str, Any],
    directory: Path,
) -> list[tuple[Path, dict[str, Any] | None]]:
    registered: list[tuple[Path, dict[str, Any] | None]] = []
    registered_names: set[str] = set()
    for view in item.get("views") or []:
        source = (KNOWLEDGE_ASSET_ROOT / str(view["image_file"])).resolve()
        if source.parent != directory or not source.is_file():
            raise AuditError(f"待补参考图不存在或越界：{source}")
        if sha256_file(source) != str(view["sha256"]):
            raise AuditError(f"待补参考图哈希不一致：{source.name}")
        registered.append((source, view))
        registered_names.add(source.name)
    supplements = [
        (path, None)
        for path in sorted(directory.iterdir(), key=lambda value: value.name.casefold())
        if path.is_file() and path.name not in registered_names
    ]
    if not supplements:
        raise AuditError(
            f"待补商品没有新增条码照片，拒绝仅凭手工输入晋升：{item['pending_id']}"
        )
    return [*registered, *supplements]


def _copy_promoted_views(
    *,
    item: dict[str, Any],
    barcode: str,
    directory: Path,
    ordered: list[tuple[Path, dict[str, Any] | None]],
) -> tuple[list[dict[str, Any]], list[Path]]:
    source_id = str(item["pending_id"])
    destination = _catalog_product_directory(
        barcode,
        str(item["product_name"]),
        str(item["product_code"]),
    )
    destination.mkdir(parents=True, exist_ok=True)
    targets = [
        destination / f"{source_id}-v{index:02d}{source.suffix.lower()}"
        for index, (source, _) in enumerate(ordered, start=1)
    ]
    collision = next((target for target in targets if target.exists()), None)
    if collision is not None:
        raise AuditError(f"正式参考图目标已存在，拒绝覆盖：{collision}")

    source_record = dict(item["source"])
    copied: list[Path] = []
    promoted: list[dict[str, Any]] = []
    try:
        for index, ((source, existing), target) in enumerate(
            zip(ordered, targets, strict=True),
            start=1,
        ):
            with Image.open(source) as image:
                width, height = image.size
            source_hash = sha256_file(source)
            shutil.copy2(source, target)
            copied.append(target)
            target_hash = sha256_file(target)
            if target_hash != source_hash:
                raise AuditError(f"晋升复制后图片哈希不一致：{source.name}")

            if existing is not None:
                view = dict(existing)
                view["view_id"] = f"{source_id}-v{index:02d}"
                view["image_file"] = _relative_text(target)
                view["sha256"] = target_hash
                view["width"] = width
                view["height"] = height
            else:
                view = {
                    "view_id": f"{source_id}-v{index:02d}",
                    "face": "barcode",
                    "image_file": _relative_text(target),
                    "source_original_name": source.name,
                    "source_id": source_id,
                    "source_collection": str(source_record["collection"]),
                    "source_folder": _relative_text(directory),
                    "sha256": target_hash,
                    "width": width,
                    "height": height,
                    "identity_strength": "strong",
                    "visible_anchors": [f"69码 {barcode}"],
                    "limitations": [],
                }
            promoted.append(view)
    except Exception:
        for target in reversed(copied):
            target.unlink(missing_ok=True)
        if destination.exists() and not any(destination.iterdir()):
            destination.rmdir()
        raise
    return promoted, copied


def _write_manifests(
    *,
    catalog: dict[str, Any],
    pending: dict[str, Any],
    original_catalog: bytes,
    original_pending: bytes,
) -> None:
    catalog_tmp = CATALOG_PATH.with_name(CATALOG_PATH.name + ".promote.tmp")
    pending_tmp = PENDING_MANIFEST.with_name(PENDING_MANIFEST.name + ".promote.tmp")
    try:
        validate_json(catalog, SCHEMA_PATH)
        validate_json(pending, PENDING_SCHEMA_PATH)
        write_json(catalog_tmp, catalog)
        write_json(pending_tmp, pending)
        os.replace(catalog_tmp, CATALOG_PATH)
        try:
            os.replace(pending_tmp, PENDING_MANIFEST)
        except Exception:
            CATALOG_PATH.write_bytes(original_catalog)
            raise
        clear_product_rag_cache()
        load_product_rag(SKILL_ROOT)
        load_pending_product_rag(SKILL_ROOT)
    except Exception:
        catalog_tmp.unlink(missing_ok=True)
        pending_tmp.unlink(missing_ok=True)
        CATALOG_PATH.write_bytes(original_catalog)
        PENDING_MANIFEST.write_bytes(original_pending)
        clear_product_rag_cache()
        raise


def _promote(args: argparse.Namespace) -> dict[str, Any]:
    if not SOURCE_ID_PATTERN.fullmatch(args.pending_id):
        raise AuditError("pending-id 必须是3-50位小写字母、数字或连字符")
    if not args.barcode.startswith("69") or not ean13_is_valid(args.barcode):
        raise AuditError(f"69码必须以69开头并通过EAN-13校验：{args.barcode}")

    original_catalog = CATALOG_PATH.read_bytes()
    original_pending = PENDING_MANIFEST.read_bytes()
    catalog = load_product_rag(SKILL_ROOT)
    pending = load_pending_product_rag(SKILL_ROOT)
    item = next(
        (
            candidate
            for candidate in pending["pending_products"]
            if str(candidate["pending_id"]) == args.pending_id
        ),
        None,
    )
    if item is None:
        raise AuditError(f"待补商品不存在：{args.pending_id}")
    if any(str(product["barcode_69"]) == args.barcode for product in catalog["products"]):
        raise AuditError(f"正式目录已经存在该69码，拒绝自动合并：{args.barcode}")
    if any(
        args.pending_id == str(source.get("source_id"))
        for product in catalog["products"]
        for source in product.get("sources") or []
    ):
        raise AuditError(f"正式目录已经存在该来源ID：{args.pending_id}")

    directory = _pending_directory(item)
    ordered = _ordered_images(item, directory)
    product_args = Namespace(
        barcode=args.barcode,
        product_name=str(item["product_name"]),
        product_code=str(item["product_code"]),
        specification=str(item["specification"]),
        variant=item.get("variant"),
        match_policy="exact_or_candidate",
    )
    product = _new_product(product_args)
    views, copied = _copy_promoted_views(
        item=item,
        barcode=args.barcode,
        directory=directory,
        ordered=ordered,
    )
    product["sources"].append(dict(item["source"]))
    product["views"].extend(views)
    catalog["products"].append(product)
    pending["pending_products"] = [
        candidate
        for candidate in pending["pending_products"]
        if str(candidate["pending_id"]) != args.pending_id
    ]

    try:
        _write_manifests(
            catalog=catalog,
            pending=pending,
            original_catalog=original_catalog,
            original_pending=original_pending,
        )
    except Exception:
        for target in reversed(copied):
            target.unlink(missing_ok=True)
        destination = _catalog_product_directory(
            args.barcode,
            str(item["product_name"]),
            str(item["product_code"]),
        )
        if destination.exists() and not any(destination.iterdir()):
            destination.rmdir()
        raise

    cleanup_warning: str | None = None
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        cleanup_warning = f"正式晋升已完成，但待补源目录未能清理：{exc}"
    return {
        "status": "promoted",
        "pending_id": args.pending_id,
        "product_id": product["product_id"],
        "barcode_69": args.barcode,
        "images_promoted": len(views),
        "cleanup_warning": cleanup_warning,
    }


def main() -> int:
    args = _parse_args()
    descriptor = _acquire_lock()
    try:
        result = _promote(args)
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
