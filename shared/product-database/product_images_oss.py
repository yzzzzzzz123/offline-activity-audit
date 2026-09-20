"""商品 OSS 凭据配置与只读完整性核验；不包含本地图库迁移入口。"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import getpass
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT.parents[1] / 'skills/orchestrate-offline-audit/scripts'))
from audit_core.common import AuditError
from audit_core.product_database import load_product_catalog
from audit_core.product_oss import (
    PREFIX, MAX_IMAGE_BYTES, MAX_MANIFEST_BYTES,
    ProductOSS, json_bytes, manifest_key, save_credentials, validate_manifest,
)


def database():
    spec = importlib.util.spec_from_file_location("product_dbctl", ROOT / "dbctl.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Discard(io.RawIOBase):
    def write(self, value):
        return len(value)


def verify_remote(*, images: bool, workers: int):
    """不依赖本地图片或迁移回执，直接从本次数据库验证当前 OSS 资料。"""
    catalog = load_product_catalog()
    def one(product):
        key = product.get("image_manifest_key")
        if key != manifest_key(product["product_code"]):
            raise AuditError("商品尚未关联 OSS 图片集")
        with ProductOSS() as client:
            data = client.read(key, limit=MAX_MANIFEST_BYTES)
            value = validate_manifest(data, product)
            if images:
                for view in value["views"]:
                    client.read(view["object_key"], limit=MAX_IMAGE_BYTES, output=Discard(),
                                expected_sha=view["sha256"], expected_size=view["size_bytes"])
            return {"product_code": product["product_code"], "image_manifest_key": key,
                    "manifest_sha256": hashlib.sha256(data).hexdigest(), "image_count": len(value["views"]),
                    "image_bytes": sum(v["size_bytes"] for v in value["views"])}
    records = []
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(one, product) for product in catalog["products"]]
        for future in as_completed(futures):
            records.append(future.result())
            if len(records) % 10 == 0:
                print(f"已验证商品 {len(records)}/{len(futures)}", flush=True)
    after = load_product_catalog()
    if any(after["data_source"][k] != catalog["data_source"][k] for k in ("sha256", "image_manifest_keys_sha256")):
        raise AuditError("验证期间数据库身份或图片关联改变，请重新验证")
    value = {"status": "passed", "prefix": PREFIX, "image_bytes_verified": images,
             "product_count": len(records), "image_count": sum(r["image_count"] for r in records),
             "image_bytes": sum(r["image_bytes"] for r in records), "database": catalog["data_source"],
             "verified_at_utc": datetime.now(timezone.utc).isoformat(), "products": records}
    target = ROOT / "runtime/product-images/verification.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = target.with_suffix(".pending")
    pending.write_bytes(json_bytes(value))
    pending.replace(target)
    print(json.dumps({k: v for k, v in value.items() if k not in {"database", "products"}}, ensure_ascii=False), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["configure", "verify"])
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--replace-credentials", action="store_true")
    parser.add_argument("--workers", type=int, choices=range(1, 5), default=4)
    parser.add_argument("--images", action="store_true", help="逐张流式回读校验，不保存图片")
    args = parser.parse_args()
    try:
        if args.command == "verify":
            verify_remote(images=args.images, workers=args.workers)
            return 0
        if not args.apply:
            raise AuditError("配置私有凭据需要 --apply")
        value = json.loads(getpass.getpass("凭据 JSON（不回显）: "))
        save_credentials(value, replace=args.replace_credentials)
        print("凭据已用当前用户 DPAPI 加密保存至仓库外；商品图固定使用 OSS。")
        return 0
    except Exception as exc:
        print(str(exc) if isinstance(exc, AuditError) else f"商品图片操作失败（{type(exc).__name__}），未输出原始响应或凭据", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
