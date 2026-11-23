"""Read-only compatibility for the existing Windows source service; not a Linux deployment path."""
import os,shutil,subprocess,json
from .paths import PROJECT_ROOT
from .common import AuditError
DATABASE_ROOT=PROJECT_ROOT/'shared/product-database'
MAX_PRODUCTS=100000
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
def _read_database_rows() -> list[dict[str, str]]:
    executable = shutil.which("docker") or shutil.which("docker.exe")
    if not executable or not (DATABASE_ROOT / ".env").is_file():
        raise AuditError("商品数据库尚未配置：请检查 shared/product-database 的 Docker 服务与 .env")
    command = [executable, "compose", "--project-directory", str(DATABASE_ROOT),
               "--env-file", str(DATABASE_ROOT / ".env"), "-f", str(DATABASE_ROOT / "compose.yaml"),
               "exec", "-T", "mysql", "sh", "-c", _MYSQL_READER]
    try:
        from .execution_trace import record_step
        record_step("database.statement", "执行Windows源端商品库只读SQL", {"sql": _SQL, "transport": "Docker exec / MySQL CLI",
                    "read_only": True, "credential_values_recorded": False}, status="started")
        from .execution_trace import run_command
        completed = run_command(
            command, input=_SQL.encode("utf-8"), capture_output=True, check=True,
            timeout=30, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0, trace_output=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        record_step("database.statement_failed", "商品库命令执行失败", {"error_type": type(exc).__name__,
                    "return_code": getattr(exc, "returncode", None)}, status="failed")
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
    record_step("database.statement_completed", "商品库SQL返回格式及行数校验通过", {"row_count": count, "return_code": completed.returncode})
    return rows
