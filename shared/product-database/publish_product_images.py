"""经人工/Skill 身份确认后，按商品固定 OSS 路径预演或发布参考图片。"""
from __future__ import annotations

import argparse
import hashlib
import mimetypes
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1]))
from audit_core.common import AuditError, sha256_file
from audit_core.product_database import load_product_catalog
from audit_core.product_oss import (
    MAX_IMAGE_BYTES, MAX_MANIFEST_BYTES, OWNER, ProductOSS, json_bytes, manifest_key, product_prefix, validate_manifest,
)
from product_images_oss import Discard, database


def make_plan(product: dict, source_dir: Path, old: dict | None):
    from PIL import Image
    if not source_dir.is_dir() or source_dir.is_symlink() or (hasattr(source_dir, "is_junction") and source_dir.is_junction()):
        raise AuditError("图片来源必须是显式普通目录，不接受链接或联接")
    source_dir = source_dir.resolve()
    sources = sorted(source_dir.iterdir(), key=lambda path: path.name)
    if not 1 <= len(sources) <= 32:
        raise AuditError("来源目录必须只含本商品 1 至 32 张完整多视图图片")
    old_by_key = {v["object_key"]: v for v in (old or {}).get("views", [])}
    views = []
    for source in sources:
        if source.is_symlink() or not source.is_file() or source.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise AuditError("来源目录含非图片、子目录或链接；未上传")
        key = product_prefix(product["product_code"]) + source.name
        size = source.stat().st_size
        if not 1 <= size <= MAX_IMAGE_BYTES:
            raise AuditError("参考图片大小超出上限")
        with Image.open(source) as image:
            width, height = image.size
            content_type = Image.MIME.get(image.format)
            image.verify()
        views.append({"view_id": old_by_key.get(key, {}).get("view_id", source.stem), "object_key": key,
                      "sha256": sha256_file(source), "size_bytes": size, "content_type": content_type,
                      "width": width, "height": height})
    if not set(old_by_key).issubset({v["object_key"] for v in views}):
        raise AuditError("更新须沿用现有视图文件名并提供完整图片集；删除视图需另行明确处理，不能改名堆积版本")
    value = {"schema_version": "1.0", "product_code": product["product_code"], "barcode_69": product["barcode_69"], "views": views}
    validate_manifest(json_bytes(value), product)
    return value, sources


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--product-code", required=True)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    lock = None
    try:
        products = [p for p in load_product_catalog()["products"] if p["product_code"] == args.product_code]
        if len(products) != 1:
            raise AuditError("数据库未唯一登记该商品，不能发布图片")
        product = products[0]
        key = manifest_key(product["product_code"])
        if args.apply:
            lock_path = ROOT / "runtime/product-images" / (args.product_code + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = lock_path.open("x")
        with ProductOSS() as client:
            existing = client.head(key)
            if existing and (existing.metadata or {}).get("managed-by") != OWNER:
                raise AuditError("已有商品清单所有权不明，未覆盖")
            old_bytes = client.read(key, limit=MAX_MANIFEST_BYTES) if existing else None
            old = validate_manifest(old_bytes, product) if old_bytes else None
            plan, sources = make_plan(product, args.source_dir, old)
            print(json_bytes({"商品编码": args.product_code, "商品名称": product["product_name"],
                              "69码": product["barcode_69"], "图片集": key, "views": plan["views"],
                              "模式": "发布" if args.apply else "仅预览，尚未上传"}).decode("utf-8"), flush=True)
            if not args.apply:
                return 0
            # 全部目标先确认，再开始任何上传。应用只接收已由操作者确认身份的图片集。
            for view in plan["views"]:
                current = client.head(view["object_key"])
                if current and (current.metadata or {}).get("managed-by") != OWNER:
                    raise AuditError("图片目标不属于本参考图管理范围，未覆盖")
            fresh = next((p for p in load_product_catalog()["products"] if p["product_code"] == args.product_code), None)
            if fresh is None or any(fresh[k] != product[k] for k in ("product_name", "barcode_69", "image_manifest_key")):
                raise AuditError("数据库身份或图片地址发生变化，未上传")
            for view, source in zip(plan["views"], sources):
                if source.stat().st_size != view["size_bytes"] or sha256_file(source) != view["sha256"]:
                    raise AuditError("来源在预检后发生变化，未继续上传")
                with source.open("rb") as body:
                    client.put(view["object_key"], body, size=view["size_bytes"], sha=view["sha256"],
                               content_type=view["content_type"], forbid_overwrite=False)
                client.read(view["object_key"], limit=MAX_IMAGE_BYTES, output=Discard(),
                            expected_sha=view["sha256"], expected_size=view["size_bytes"])
            if existing and client.read(key, limit=MAX_MANIFEST_BYTES) != old_bytes:
                raise AuditError("发布期间清单被其他写入者修改，未覆盖清单；须核对后重试")
            data = json_bytes(plan)
            client.put(key, data, size=len(data), sha=hashlib.sha256(data).hexdigest(), content_type="application/json",
                       forbid_overwrite=not bool(existing))
            if client.read(key, limit=MAX_MANIFEST_BYTES) != data:
                raise AuditError("发布后清单回读失败")
        if product.get("image_manifest_key") is None:
            db = database()
            db.backup(argparse.Namespace(file=None))
            def literal(text):
                return "CONVERT(0x" + text.encode("utf-8").hex() + " USING utf8mb4)"
            db.query(f"UPDATE product_catalog.products SET image_manifest_key={literal(key)} "
                     f"WHERE product_code={literal(product['product_code'])} AND barcode_69={literal(product['barcode_69'])} "
                     f"AND BINARY product_name=BINARY {literal(product['product_name'])} AND image_manifest_key IS NULL;")
            fresh = next((p for p in load_product_catalog()["products"] if p["product_code"] == args.product_code), None)
            if not fresh or fresh.get("image_manifest_key") != key:
                raise AuditError("图片已上传，但数据库关联未通过回读；未声称已接入")
        print("图片、清单和数据库关联均验证通过。原始来源未删除；没有新建图片版本目录。")
        return 0
    except Exception as exc:
        print(str(exc) if isinstance(exc, AuditError) else f"图片发布失败（{type(exc).__name__}）；未输出认证信息", file=sys.stderr)
        return 1
    finally:
        if lock is not None:
            lock.close()
            lock_path.unlink()


if __name__ == "__main__":
    raise SystemExit(main())
