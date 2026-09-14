"""核销商品主数据的只读数据库入口；不依赖图片库或历史输出。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any

from .common import AuditError

DATABASE_ROOT = Path(__file__).resolve().parents[1] / "shared" / "product-database"
PRODUCT_KNOWLEDGE_RULES = DATABASE_ROOT / "audit-knowledge.md"
MAX_PRODUCTS = 100_000
# 凭据只在容器内部读取。命令是固定值，SQL 通过 stdin 传入，不拼接业务输入。
_MYSQL_READER = (
    'export MYSQL_PWD="$(cat /run/secrets/viewer_password)"; exec '
    "mysql --no-defaults --protocol=tcp --host=127.0.0.1 --port=3306 "
    "--ssl-mode=REQUIRED --user=product_viewer --connect-timeout=10 "
    "--default-character-set=utf8mb4 --batch --raw --skip-column-names"
)
_SQL = """SET SESSION MAX_EXECUTION_TIME=15000;
SET TRANSACTION ISOLATION LEVEL REPEATABLE READ;
START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY;
SELECT JSON_OBJECT('row_count',COUNT(*)) FROM product_catalog.products;
SELECT JSON_OBJECT('barcode_69',barcode_69,'product_name',product_name,
                   'product_code',product_code,'image_manifest_key',image_manifest_key)
FROM product_catalog.products ORDER BY product_code LIMIT 100001;
COMMIT;
"""
_scope: ContextVar[dict[str, Any] | None] = ContextVar("audit_product_database", default=None)


def _read_database_rows() -> list[dict[str, str]]:
    executable = shutil.which("docker") or shutil.which("docker.exe")
    if not executable or not (DATABASE_ROOT / ".env").is_file():
        raise AuditError("商品数据库尚未配置：请检查 shared/product-database 的 Docker 服务与 .env")
    command = [executable, "compose", "--project-directory", str(DATABASE_ROOT),
               "--env-file", str(DATABASE_ROOT / ".env"), "-f", str(DATABASE_ROOT / "compose.yaml"),
               "exec", "-T", "mysql", "sh", "-c", _MYSQL_READER]
    try:
        completed = subprocess.run(
            command, input=_SQL.encode("utf-8"), capture_output=True, check=True,
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # 原始回包可能含连接/认证信息，不能写入模型上下文或业务档案。
        raise AuditError("商品数据库读取失败：请检查 Docker、MySQL 健康状态及只读账号；本次未使用旧商品数据") from None
    if len(completed.stdout) > 64 * 1024 * 1024:
        raise AuditError("商品数据库响应超过读取上限，本次未使用不完整数据")
    try:
        records = [json.loads(line) for line in completed.stdout.decode("utf-8").splitlines() if line.strip()]
        count = records[0]["row_count"]
        rows = records[1:]
        if type(count) is not int or not 1 <= count <= MAX_PRODUCTS or count != len(rows):
            raise ValueError("商品行数不完整或为空")
    except (UnicodeError, ValueError, IndexError, KeyError, TypeError):
        raise AuditError("商品数据库返回格式、行数或完整性不符合要求") from None
    return rows


def catalog_from_rows(rows: list[dict[str, str]]) -> dict[str, Any]:
    from .product_rag import ean13_is_valid

    if not rows or len(rows) > MAX_PRODUCTS:
        raise AuditError("商品数据库不能为空或超过读取上限")
    identities = []
    image_keys = {}
    seen: set[str] = set()
    for row in rows:
        required = {"barcode_69", "product_name", "product_code"}
        if not isinstance(row, dict) or set(row) not in (required, required | {"image_manifest_key"}):
            raise AuditError("商品数据库必须返回三个身份字段及可选商品图片集字段")
        if any(not isinstance(row[k], str) or not row[k].strip() or row[k] != row[k].strip() for k in required):
            raise AuditError("商品数据库含空值或非规范身份字段")
        code, barcode, name = row["product_code"], row["barcode_69"], row["product_name"]
        if not re.fullmatch(r"[\x21-\x7e]{1,64}", code) or code in seen:
            raise AuditError("商品数据库含无效或重复的商品编码")
        if len(name) > 512 or any(ord(c) < 32 for c in name):
            raise AuditError("商品数据库含无效商品名称")
        if not barcode.startswith("69") or not ean13_is_valid(barcode):
            raise AuditError(f"商品数据库69码未通过EAN-13校验：{code}")
        seen.add(code)
        image_key = row.get("image_manifest_key")
        if image_key is not None:
            from .product_oss import manifest_key
            if not isinstance(image_key, str) or image_key != manifest_key(code):
                raise AuditError(f"商品数据库图片集必须是本商品获准前缀的稳定 Object Key：{code}")
        image_keys[code] = image_key
        identities.append({k: row[k] for k in required})
    identities.sort(key=lambda row: row["product_code"])
    digest = hashlib.sha256(json.dumps(identities, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "schema_version": "3.0",
        "data_source": {"type": "mysql", "table": "product_catalog.products", "read_only": True,
                        "row_count": len(identities), "sha256": digest,
                        "image_manifest_keys_sha256": hashlib.sha256(json.dumps(
                            image_keys, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                        "read_at_utc": datetime.now(timezone.utc).isoformat()},
        "products": [{
            **row, "image_manifest_key": image_keys[row["product_code"]],
            "product_id": "db-" + hashlib.sha256(row["product_code"].encode("ascii")).hexdigest()[:24],
            # 兼容既有结果字段；数据库未登记的辅助信息保持空，不从图片库补齐。
            "specification": "", "variant": None, "aliases": [], "product_code_aliases": [],
            "specification_aliases": [], "variant_aliases": [], "sources": [], "views": [],
            "match_policy": "exact_or_candidate",
        } for row in identities],
    }


def load_product_catalog() -> dict[str, Any]:
    state = _scope.get()
    if state is None:
        return catalog_from_rows(_read_database_rows())
    if "catalog" not in state:
        state["catalog"] = catalog_from_rows(_read_database_rows())
    return deepcopy(state["catalog"])


@contextmanager
def product_catalog_scope():
    """同次核销复用一致快照；下次运行重新查询，进程不持有跨运行缓存。"""
    token = _scope.set({})
    try:
        from .product_image_runtime import product_image_scope
        with product_image_scope():
            yield
    finally:
        _scope.reset(token)


def with_product_catalog(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with product_catalog_scope():
            return function(*args, **kwargs)
    return wrapped


def main() -> int:
    try:
        catalog = load_product_catalog()
        print(json.dumps(catalog["data_source"], ensure_ascii=False))
        return 0
    except AuditError as exc:
        print(str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
