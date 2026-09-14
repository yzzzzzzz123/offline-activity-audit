"""商品参考图专用 OSS 适配器：固定前缀、私有认证、直连和完整字节校验。"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from typing import Any, BinaryIO

from .common import AuditError

PREFIX = "offline-verify/product-reference/"
BUCKET = "xiaokuo-dingding-xianxiahexiao"
REGION = "cn-shenzhen"
ENDPOINT = "https://oss-cn-shenzhen.aliyuncs.com"
SETTINGS = Path(__file__).resolve().parents[1] / "shared/product-database/product-images.json"
MAX_IMAGE_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 256 * 1024
OWNER = "offline-product-reference"


def json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")


def credential_path() -> Path:
    if os.name != "nt":
        raise AuditError("当前商品 OSS 凭据使用 Windows 当前用户 DPAPI；迁移主机须重新配置凭据")
    return Path(os.environ["LOCALAPPDATA"]) / "OfflineActivityAudit/ProductImages/credentials.dpapi"


def _crypt(data: bytes, *, decrypt: bool = False) -> bytes:
    if os.name != "nt":
        raise AuditError("当前凭据保护仅支持 Windows DPAPI")
    class Blob(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    output = Blob()
    library = ctypes.WinDLL("crypt32", use_last_error=True)
    function = library.CryptUnprotectData if decrypt else library.CryptProtectData
    function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                         ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    function.restype = wintypes.BOOL
    if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
        raise AuditError("商品 OSS 凭据加密或解密失败；必须使用配置凭据的 Windows 用户运行")
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        free = ctypes.WinDLL("kernel32").LocalFree
        free.argtypes = [ctypes.c_void_p]
        free.restype = ctypes.c_void_p
        free(output.data)


def save_credentials(value: dict, *, replace: bool = False) -> None:
    if set(value) != {"accessKeyId", "accessKeySecret"} or any(
        not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9]{16,128}", v) for v in value.values()
    ):
        raise AuditError("请输入只含 accessKeyId 和 accessKeySecret 的有效凭据 JSON")
    target = credential_path()
    if target.exists() and not replace:
        raise AuditError("私有凭据已存在；更新须显式使用 --replace-credentials")
    target.parent.mkdir(parents=True, exist_ok=True)
    identity = subprocess.run(["whoami.exe", "/user", "/fo", "csv", "/nh"], check=True,
                              capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    sid = re.search(r"S-1-\d+(?:-\d+)+", identity.stdout)
    if not sid:
        raise AuditError("无法确认当前 Windows 用户")
    subprocess.run(["icacls.exe", str(target.parent), "/inheritance:r", "/grant:r",
                    "*" + sid.group() + ":(OI)(CI)F", "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F"], check=True, capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)
    encrypted = _crypt(json_bytes(value))
    pending = target.with_suffix(".pending")
    with pending.open("xb") as handle:
        handle.write(encrypted)
    try:
        if json.loads(_crypt(pending.read_bytes(), decrypt=True)) != value:
            raise AuditError("私有凭据回读校验失败")
        pending.replace(target)
    finally:
        pending.unlink(missing_ok=True)


def require_configuration() -> None:
    if not SETTINGS.is_file():
        raise AuditError("商品 OSS 配置缺失；参考图片只允许读取 OSS")
    try:
        value = json.loads(SETTINGS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise AuditError("商品图片数据源配置损坏，未回退本地图片") from None
    if value != {"mode": "oss", "bucket": BUCKET, "endpoint": ENDPOINT, "prefix": PREFIX}:
        raise AuditError("商品图片数据源配置不符合已批准的 OSS 范围")


def product_prefix(code: str) -> str:
    if not isinstance(code, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", code):
        raise AuditError("商品编码不适合作为受控 OSS 目录名")
    return PREFIX + code + "/"


def manifest_key(code: str) -> str:
    return product_prefix(code) + "manifest.json"


def check_key(key: str) -> None:
    if not isinstance(key, str) or not key.startswith(PREFIX) or len(key.encode("utf-8")) > 1023:
        raise AuditError("商品 OSS 对象不在授权参考图前缀内")
    tail = key[len(PREFIX):].split("/")
    if len(tail) != 2 or any(not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", p) or p in {".", ".."} for p in tail):
        raise AuditError("商品 OSS 对象路径不安全")
    product_prefix(tail[0])


def error_code(exc: BaseException) -> str:
    for _ in range(6):
        code = getattr(exc, "code", None)
        if isinstance(code, str) and re.fullmatch(r"[A-Za-z0-9_]{1,80}", code):
            return code
        unwrap = getattr(exc, "unwrap", None)
        inner = unwrap() if callable(unwrap) else None
        if not isinstance(inner, BaseException) or inner is exc:
            break
        exc = inner
    return type(exc).__name__


class ProductOSS:
    def __init__(self):
        require_configuration()
        import alibabacloud_oss_v2 as oss
        import requests
        from alibabacloud_oss_v2.transport.requests_client import RequestsHttpClient

        try:
            credentials = json.loads(_crypt(credential_path().read_bytes(), decrypt=True))
        except (OSError, ValueError):
            raise AuditError("商品 OSS 私有凭据未配置或无法读取") from None
        self.oss = oss
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies.clear()
        transport = RequestsHttpClient(session=self.session, connect_timeout=15, readwrite_timeout=60,
                                       enabled_redirect=False, insecure_skip_verify=False)
        config = oss.config.load_default()
        config.region, config.endpoint = REGION, ENDPOINT
        config.credentials_provider = oss.credentials.StaticCredentialsProvider(
            credentials["accessKeyId"], credentials["accessKeySecret"])
        config.http_client = transport
        config.retry_max_attempts = 2
        config.enabled_redirect = False
        self.client = oss.Client(config)

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.session.close()

    def _call(self, function, request, *, missing_ok=False):
        try:
            return function(request)
        except Exception as exc:
            code = error_code(exc)
            if missing_ok and code == "NoSuchKey":
                return None
            raise AuditError(f"商品 OSS 请求失败（{code}），未使用旧图片或泄露原始认证响应") from None

    def head(self, key: str):
        check_key(key)
        return self._call(self.client.head_object, self.oss.HeadObjectRequest(bucket=BUCKET, key=key), missing_ok=True)

    def list_keys(self) -> list[str]:
        result, token = [], None
        while True:
            page = self._call(self.client.list_objects_v2, self.oss.ListObjectsV2Request(
                bucket=BUCKET, prefix=PREFIX, continuation_token=token, max_keys=1000))
            result.extend(item.key for item in page.contents or [])
            if len(result) > 10000:
                raise AuditError("参考图前缀对象过多，停止迁移以核对目标目录")
            if not page.is_truncated:
                return result
            token = page.next_continuation_token
            if not token:
                raise AuditError("商品 OSS 对象列表被截断")

    def versioning_status(self) -> str:
        result = self._call(self.client.get_bucket_versioning, self.oss.GetBucketVersioningRequest(bucket=BUCKET))
        return str(result.version_status or "Disabled")

    def read(self, key: str, *, limit: int, output: BinaryIO | None = None,
             expected_sha: str | None = None, expected_size: int | None = None) -> bytes | str:
        check_key(key)
        result = self._call(self.client.get_object, self.oss.GetObjectRequest(bucket=BUCKET, key=key))
        pieces, size, digest = [], 0, hashlib.sha256()
        try:
            with result.body as body:
                if result.content_length is None or result.content_length > limit or result.content_length < 1:
                    raise AuditError("商品 OSS 对象大小为空或超过上限")
                if expected_size is not None and result.content_length != expected_size:
                    raise AuditError("商品 OSS 对象长度与清单不一致")
                for chunk in body.iter_bytes(block_size=256 * 1024):
                    size += len(chunk)
                    if size > limit:
                        raise AuditError("商品 OSS 对象流超过读取上限")
                    digest.update(chunk)
                    if output is not None:
                        output.write(chunk)
                    else:
                        pieces.append(chunk)
                if size != result.content_length or (expected_sha and digest.hexdigest() != expected_sha):
                    raise AuditError("商品 OSS 对象 SHA-256 或完整长度校验失败")
        except AuditError:
            raise
        except Exception as exc:
            raise AuditError(f"商品 OSS 对象流读取失败（{error_code(exc)}）") from None
        return digest.hexdigest() if output is not None else b"".join(pieces)

    def put(self, key: str, body, *, size: int, sha: str, content_type: str, forbid_overwrite=True) -> None:
        check_key(key)
        self._call(self.client.put_object, self.oss.PutObjectRequest(
            bucket=BUCKET, key=key, body=body, content_length=size, content_type=content_type,
            metadata={"managed-by": OWNER, "sha256": sha}, cache_control="no-cache",
            acl="private", forbid_overwrite=forbid_overwrite))


def validate_manifest(data: bytes, product: dict) -> dict:
    try:
        value = json.loads(data)
        if set(value) != {"schema_version", "product_code", "barcode_69", "views"} or value["schema_version"] != "1.0":
            raise ValueError()
        if any(value[k] != product[k] for k in ("product_code", "barcode_69")):
            raise ValueError()
        views = value["views"]
        if not isinstance(views, list) or not 1 <= len(views) <= 32:
            raise ValueError()
        ids, keys = set(), set()
        for view in views:
            if set(view) != {"view_id", "object_key", "sha256", "size_bytes", "content_type", "width", "height"}:
                raise ValueError()
            check_key(view["object_key"])
            if not view["object_key"].startswith(product_prefix(product["product_code"])):
                raise ValueError()
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", view["view_id"]) or view["view_id"] in ids or view["object_key"] in keys:
                raise ValueError()
            if not re.fullmatch(r"[0-9a-f]{64}", view["sha256"]):
                raise ValueError()
            if type(view["size_bytes"]) is not int or not 1 <= view["size_bytes"] <= MAX_IMAGE_BYTES:
                raise ValueError()
            if view["content_type"] not in {"image/jpeg", "image/png", "image/webp"}:
                raise ValueError()
            if any(type(view[k]) is not int or not 1 <= view[k] <= 50000 for k in ("width", "height")):
                raise ValueError()
            if Path(view["object_key"]).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                raise ValueError()
            ids.add(view["view_id"])
            keys.add(view["object_key"])
        return value
    except (KeyError, TypeError, ValueError, UnicodeError):
        raise AuditError("商品 OSS 图片清单格式或数据库身份校验失败") from None


def reference_fingerprint(catalog: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """评测首尾流式核验 DB 关联的 OSS 清单及全部图片；不落地、不读取图库备份。"""
    from concurrent.futures import ThreadPoolExecutor
    if (catalog.get("data_source") or {}).get("type") != "mysql":
        raise AuditError("OSS 评测指纹只接受数据库快照")
    class Discard:
        def write(self, value):
            return len(value)

    def one(product):
        key = product.get("image_manifest_key")
        if key is None:
            return {}
        if key != manifest_key(product["product_code"]):
            raise AuditError("商品图片关联与 OSS 前缀不一致")
        signatures = {}
        with ProductOSS() as client:
            data = client.read(key, limit=MAX_MANIFEST_BYTES)
            value = validate_manifest(data, product)
            signatures["oss-manifests/" + key] = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for view in value["views"]:
                digest = client.read(view["object_key"], limit=MAX_IMAGE_BYTES, output=Discard(),
                                     expected_sha=view["sha256"], expected_size=view["size_bytes"])
                signatures["oss-images/" + view["object_key"]] = {"size": view["size_bytes"], "sha256": digest}
            if client.read(key, limit=MAX_MANIFEST_BYTES) != data:
                raise AuditError("生成评测指纹期间 OSS 清单改变，请重新核验")
        return signatures

    result = {}
    with ThreadPoolExecutor(max_workers=4) as executor:
        for signatures in executor.map(one, catalog["products"]):
            result.update(signatures)
    return result
