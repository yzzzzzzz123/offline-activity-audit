"""Portable management of the independent MySQL container; Python stdlib only."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
PASSWORD_PATTERN = re.compile(r"[A-Za-z0-9_-]{24,128}\Z")
SQL_PREFIX = 'export MYSQL_PWD="$(cat /run/secrets/root_password)"; exec '
IMAGE_MANIFEST_MIGRATION = ROOT / "migrations" / "001-add-image-manifest-key.sql"
IMAGE_MANIFEST_DEFINITION = {
    "type": "varchar", "length": 1024, "nullable": "YES", "default": None,
    "charset": "utf8mb4", "collation": "utf8mb4_bin",
    "comment": "商品图片集：OSS 清单 Object Key，不存临时签名链接",
}


def docker_command(*args: str) -> list[str]:
    executable = shutil.which("docker") or shutil.which("docker.exe")
    if not executable:
        raise RuntimeError("Docker CLI is not installed or is missing from PATH.")
    return [executable, "compose", "--project-directory", str(ROOT),
            "--env-file", str(ROOT / ".env"), "-f", str(ROOT / "compose.yaml"), *args]


def execute(*args: str, **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(docker_command(*args), check=True, **kwargs)


def query(sql: str) -> str:
    result = execute("exec", "-T", "mysql", "sh", "-c", SQL_PREFIX +
                     "mysql --no-defaults --protocol=socket --user=root "
                     "--default-character-set=utf8mb4 --batch --raw --skip-column-names",
                     input=sql.encode("utf-8"), stdout=subprocess.PIPE,
                     stderr=subprocess.PIPE, timeout=30)
    return result.stdout.decode("utf-8").strip()


def image_manifest_definition() -> dict | None:
    result = query("""SELECT JSON_OBJECT(
        'type', DATA_TYPE, 'length', CHARACTER_MAXIMUM_LENGTH,
        'nullable', IS_NULLABLE, 'default', COLUMN_DEFAULT,
        'charset', CHARACTER_SET_NAME, 'collation', COLLATION_NAME,
        'comment', COLUMN_COMMENT)
        FROM information_schema.COLUMNS
        WHERE TABLE_SCHEMA='product_catalog' AND TABLE_NAME='products'
          AND COLUMN_NAME='image_manifest_key';""")
    return json.loads(result) if result else None


def product_identity_summary() -> dict:
    rows = [json.loads(line) for line in query("""SELECT JSON_OBJECT(
        'barcode_69', barcode_69, 'product_name', product_name, 'product_code', product_code)
        FROM product_catalog.products ORDER BY product_code;""").splitlines()]
    rows.sort(key=lambda row: row["product_code"])
    digest = hashlib.sha256(json.dumps(rows, ensure_ascii=False, sort_keys=True,
                                      separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"row_count": len(rows), "sha256": digest}


def migrate_image_manifest(args) -> None:
    """显式应用一个可重复执行的增量迁移；不上传图片或填充任何商品地址。"""
    definition = image_manifest_definition()
    if definition is not None:
        if definition != IMAGE_MANIFEST_DEFINITION:
            raise RuntimeError("商品图片集字段已存在但结构不符合约定，未修改或覆盖。")
        print("商品图片集字段已存在且结构正确，本次未修改任何数据。")
        return
    if not args.apply:
        print("待新增：product_catalog.products.image_manifest_key，VARCHAR(1024)，允许 NULL。")
        print("仅预览；使用 --apply 执行。不会上传图片、写入示例链接或改变商品身份字段。")
        return
    before = product_identity_summary()
    query(IMAGE_MANIFEST_MIGRATION.read_text(encoding="utf-8"))
    if image_manifest_definition() != IMAGE_MANIFEST_DEFINITION:
        raise RuntimeError("迁移后字段结构未通过校验；请检查当前表结构，未自动回退或覆盖数据。")
    after = product_identity_summary()
    if after != before:
        raise RuntimeError("迁移前后商品身份数据发生变化；请核对并发写入，未自动回退或覆盖数据。")
    configured = query("SELECT COUNT(*) FROM product_catalog.products WHERE image_manifest_key IS NOT NULL;")
    if configured != "0":
        raise RuntimeError("新字段已有非空值；请核对并发写入，本工具未写入或清除任何图片地址。")
    print("已新增商品图片集字段；原商品身份及行数不变，全部新字段为 NULL。")
    print(json.dumps(after, ensure_ascii=False))


def configure(args) -> None:
    env_path = ROOT / ".env"
    if env_path.exists():
        raise RuntimeError(".env already exists. Edit its host/port directly; existing secrets were preserved.")
    if os.name == "nt":
        private_root = Path(os.environ["LOCALAPPDATA"]) / "OfflineActivityAudit" / "ProductDatabase"
    else:
        private_root = Path.home() / ".local" / "share" / "offline-activity-audit" / "product-database"
    secret_dir = private_root / "docker"
    source = Path(args.credentials).resolve() if args.credentials else private_root / "credentials.json"
    if args.credentials and not source.is_file():
        raise RuntimeError("The requested credentials file does not exist.")
    credentials = json.loads(source.read_text(encoding="utf-8-sig")) if source.is_file() else {}
    root_password = credentials.get("admin_password") or secrets.token_urlsafe(32)
    viewer_password = credentials.get("viewer_password") or secrets.token_urlsafe(24)
    for password in (root_password, viewer_password):
        if not isinstance(password, str) or not PASSWORD_PATTERN.fullmatch(password):
            raise RuntimeError("Credential password format is incompatible with the container initializer.")
    secret_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"],
                                  check=True, capture_output=True, text=True)
        sid = re.search(r"S-1-\d+(?:-\d+)+", identity.stdout)
        if not sid:
            raise RuntimeError("Cannot determine the current Windows user SID.")
        subprocess.run(["icacls.exe", str(secret_dir), "/inheritance:r", "/grant:r",
                        "*" + sid.group() + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                        "*S-1-5-32-544:(OI)(CI)F"], check=True, stdout=subprocess.DEVNULL)
    else:
        secret_dir.chmod(0o700)
    for name, value in (("root_password", root_password), ("viewer_password", viewer_password)):
        path = secret_dir / (name + ".txt")
        if path.exists():
            if path.read_text(encoding="utf-8").strip() != value:
                raise RuntimeError("Existing secrets differ; refusing to overwrite an initialized credential.")
        else:
            with path.open("x", encoding="utf-8", newline="\n") as handle:
                handle.write(value + "\n")
            if os.name != "nt":
                path.chmod(0o600)
    secret_text = secret_dir.as_posix()
    if any(character in secret_text for character in "\n\r'$\""):
        raise RuntimeError("Private directory contains unsupported .env characters.")
    env_path.write_text(f"MYSQL_BIND_ADDRESS={args.bind}\nMYSQL_PORT={args.port}\n"
                        f"PRODUCT_DB_SECRETS_DIR='{secret_text}'\n", encoding="utf-8")
    print("Configuration written:", env_path)
    print("Private password files:", secret_dir)


def backup(args) -> None:
    destination = Path(args.file).resolve() if args.file else ROOT / "runtime" / "backups" / (
        "product-catalog-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".sql")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # CREATE/DROP TABLE statements are intentionally omitted: restore only targets an empty products table.
    command = SQL_PREFIX + (
        "mysqldump --no-defaults --protocol=socket --user=root --single-transaction "
        "--quick --no-tablespaces --set-gtid-purged=OFF --no-create-info "
        "--skip-add-locks --skip-disable-keys --complete-insert --hex-blob "
        "--default-character-set=utf8mb4 product_catalog products")
    try:
        with destination.open("xb") as output:
            execute("exec", "-T", "mysql", "sh", "-c", command, stdout=output, timeout=120)
    except Exception:
        # Do not delete a preexisting destination when exclusive creation was rejected.
        if 'output' in locals():
            destination.unlink(missing_ok=True)
        raise
    digest = hashlib.sha256(destination.read_bytes()).hexdigest()
    destination.with_suffix(destination.suffix + ".sha256").write_text(
        digest + "  " + destination.name + "\n", encoding="utf-8")
    print("Data backup:", destination)
    print("SHA-256:", digest)


def restore(args) -> None:
    source = Path(args.file).resolve()
    if query("SELECT COUNT(*) FROM product_catalog.products;") != "0":
        raise RuntimeError("Restore requires an empty products table; existing data was preserved.")
    checksum = source.with_suffix(source.suffix + ".sha256")
    if not checksum.is_file():
        raise RuntimeError("Missing .sql.sha256 file.")
    checksum_parts = checksum.read_text(encoding="utf-8").split()
    if not checksum_parts or not re.fullmatch(r"[0-9a-f]{64}", checksum_parts[0]):
        raise RuntimeError("Invalid backup checksum file.")
    expected = checksum_parts[0]
    data = source.read_bytes()
    if hashlib.sha256(data).hexdigest() != expected:
        raise RuntimeError("Backup checksum mismatch.")
    execute("exec", "-T", "mysql", "sh", "-c", SQL_PREFIX +
            "mysql --no-defaults --protocol=socket --user=root "
            "--default-character-set=utf8mb4 --database=product_catalog",
            input=b"START TRANSACTION;\n" + data + b"\nCOMMIT;\n", timeout=120)
    print("Restored rows:", query("SELECT COUNT(*) FROM product_catalog.products;"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("configure")
    setup.add_argument("--credentials", help="Optional existing private credentials.json; never copied into the project")
    setup.add_argument("--bind", default="127.0.0.1")
    setup.add_argument("--port", type=int, default=3307)
    commands.add_parser("start")
    commands.add_parser("stop")
    commands.add_parser("status")
    commands.add_parser("check")
    migration = commands.add_parser("migrate-image-manifest", help="预览或新增商品图片集字段，不填充链接")
    migration.add_argument("--apply", action="store_true", help="显式应用增量迁移")
    export = commands.add_parser("backup")
    export.add_argument("--file")
    import_command = commands.add_parser("restore")
    import_command.add_argument("file")
    args = parser.parse_args()
    try:
        if args.command == "configure":
            import ipaddress
            ipaddress.ip_address(args.bind)
            if not 1 <= args.port <= 65535:
                raise RuntimeError("Port must be between 1 and 65535.")
            configure(args)
        elif args.command == "start":
            execute("up", "-d", "--wait", "--wait-timeout", "180")
        elif args.command == "stop":
            execute("stop")
        elif args.command == "status":
            execute("ps")
        elif args.command == "check":
            print(query("SELECT VERSION(),COUNT(*),COUNT(DISTINCT product_code),"
                        "COUNT(DISTINCT barcode_69) FROM product_catalog.products;"))
        elif args.command == "backup":
            backup(args)
        elif args.command == "migrate-image-manifest":
            migrate_image_manifest(args)
        else:
            restore(args)
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        # subprocess output may contain SQL/credentials; do not include it in diagnostics.
        print("Database operation failed:", str(exc) if not isinstance(exc, subprocess.SubprocessError)
              else type(exc).__name__, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
