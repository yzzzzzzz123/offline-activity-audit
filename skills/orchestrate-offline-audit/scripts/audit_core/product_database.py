"""核销商品主数据的只读数据库入口；不依赖图片库或历史输出。"""
from __future__ import annotations

from .paths import PROJECT_ROOT

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
from typing import Any

from .common import AuditError

DATABASE_ROOT = PROJECT_ROOT / "shared" / "product-database"
PRODUCT_KNOWLEDGE_RULES = DATABASE_ROOT / "audit-knowledge.md"
MAX_PRODUCTS = 100_000
_scope: ContextVar[dict[str, Any] | None] = ContextVar("audit_product_database", default=None)


def _read_database_rows() -> list[dict[str, str]]:
    if not os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_HOST'):
        # Keep the already-running Windows source service operational during
        # handover. The Linux customer runtime has no container fallback.
        if os.name=='nt' and (DATABASE_ROOT/'.env').is_file():
            from .legacy_product_database import _read_database_rows as legacy_rows
            return legacy_rows()
        raise AuditError('商品 RDS 尚未配置：请配置主机、库名、只读账号、密码文件及数据库连接方式。')
    return _read_network_database_rows()


def _read_network_database_rows() -> list[dict[str, str]]:
    """Use the deployment's explicit transport mode and a read-only snapshot."""
    from .private_credentials import read_private_file
    import ssl
    try:
        import pymysql
        host = os.environ['OFFLINE_AUDIT_PRODUCT_DB_HOST']
        port = int(os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_PORT', '3306'))
        if not re.fullmatch(r'[A-Za-z0-9_.:-]{1,253}', host) or not 1 <= port <= 65535:
            raise ValueError()
        password = read_private_file(os.environ['OFFLINE_AUDIT_PRODUCT_DB_PASSWORD_FILE'])
        if not password:
            raise ValueError()
        transport = os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_SSL_MODE', 'VERIFY_IDENTITY')
        if transport == 'VERIFY_IDENTITY':
            ca = os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_CA_FILE')
            if not ca:
                raise ValueError('RDS CA required for VERIFY_IDENTITY')
            connection_options = {'ssl': ssl.create_default_context(cafile=ca)}
        elif transport == 'DISABLED':
            # An explicit deployment setting is required. Missing CA or a TLS
            # failure must never silently change an existing TLS connection.
            connection_options = {'ssl_disabled': True}
        else:
            raise ValueError('Unsupported database SSL mode')
        user = os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_USER','product_viewer')
        database = os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_NAME','product_catalog')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',user) or not re.fullmatch(r'[A-Za-z0-9_]{1,64}',database):
            raise ValueError()
        with pymysql.connect(host=host, port=port, user=user, password=password,
                             database=database, charset='utf8mb4', **connection_options,
                             connect_timeout=10, read_timeout=30, write_timeout=10,
                             local_infile=False, autocommit=False) as connection:
            with connection.cursor() as cursor:
                from .execution_trace import record_step
                def execute(statement):
                    record_step("database.statement", "执行MySQL只读会话SQL",
                                {"statement": statement, "transport_mode": transport}, status="started")
                    return cursor.execute(statement)
                execute("SHOW SESSION STATUS LIKE 'Ssl_cipher'")
                cipher = cursor.fetchone()
                if not cipher or (transport == 'VERIFY_IDENTITY' and not cipher[1]):
                    raise ValueError('TLS required')
                execute('SET SESSION MAX_EXECUTION_TIME=15000')
                execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ')
                execute('START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY')
                execute('SELECT COUNT(*) FROM products')
                count = cursor.fetchone()[0]
                execute("SELECT JSON_OBJECT('barcode_69',barcode_69,'product_name',product_name,"
                               "'product_code',product_code,'image_manifest_key',image_manifest_key) "
                               'FROM products ORDER BY product_code LIMIT 100001')
                documents = [row[0] for row in cursor.fetchall()]
                if not 1 <= count <= MAX_PRODUCTS or len(documents) != count or sum(len(d.encode('utf-8')) for d in documents) > 64*1024*1024:
                    raise ValueError('Incomplete catalog')
                rows = [json.loads(document) for document in documents]
                connection.commit()
        return rows
    except Exception:
        raise AuditError('商品数据库读取失败：请检查 MySQL、连接方式、只读密码文件及网络；未回退旧数据或输出认证信息。') from None


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
        "data_source": {"type": "mysql", "table": os.environ.get("OFFLINE_AUDIT_PRODUCT_DB_NAME","product_catalog")+".products", "read_only": True,
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
    from .execution_trace import record_step
    fresh = state is None or "catalog" not in state
    if fresh:
        configured_host = os.environ.get("OFFLINE_AUDIT_PRODUCT_DB_HOST", "")
        trace_host = (configured_host if re.fullmatch(r"[A-Za-z0-9_.:-]{1,253}", configured_host) else "未配置有效连接地址") if configured_host else (
            "本机源端 MySQL 服务" if os.name == "nt" and (DATABASE_ROOT / ".env").is_file() else "未配置连接地址")
        record_step("database.query", "读取本次商品数据库只读快照", {
            "source": {"name": "本部署商品主数据库", "database": os.environ.get("OFFLINE_AUDIT_PRODUCT_DB_NAME", "product_catalog"),
                       "host": trace_host,
                       "port": os.environ.get("OFFLINE_AUDIT_PRODUCT_DB_PORT", "3306")},
            "table": os.environ.get("OFFLINE_AUDIT_PRODUCT_DB_NAME", "product_catalog") + ".products",
            "fields": ["product_code", "product_name", "barcode_69", "image_manifest_key"],
            "condition": "全部商品，按product_code排序；不跨核销任务复用商品身份",
            "query_fields": "product_code,product_name,barcode_69,image_manifest_key；实际SQL另见数据库执行步骤",
            "read_only": True}, status="started")
        try:
            catalog = catalog_from_rows(_read_database_rows())
        except Exception as exc:
            record_step("database.failed", "商品数据库读取失败", {"error_type": type(exc).__name__}, status="failed")
            raise
        record_step("database.result", "商品数据库快照已读取并校验", {"data_source": catalog.get("data_source", {}),
            "rows": [{k: p.get(k) for k in ("product_code", "product_name", "barcode_69", "image_manifest_key")} for p in catalog.get("products", [])]})
        if state is None:
            return catalog
        state["catalog"] = catalog
    else:
        record_step("database.snapshot_reused", "复用本单已读取的商品一致快照", state["catalog"].get("data_source", {}))
    return deepcopy(state["catalog"])


@contextmanager
def product_catalog_scope():
    """商品身份/清单每次重查；已校验的参考图字节可跨运行复用。"""
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
