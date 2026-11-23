"""参半核销产物的私有 OSS 主存储，与商品参考图前缀严格隔离。"""
from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import re
import socket
import stat

from .common import AuditError

BUCKET = "xiaokuo-dingding-xianxiahexiao"
ENDPOINT = "https://oss-cn-shenzhen-internal.aliyuncs.com"
REGION = "cn-shenzhen"
PREFIX = "offline-verify/audit-artifacts/canban/"
OWNER = "offline-canban-artifacts-v1"
CONFIG_NAME = "artifact-oss.json"


class ArtifactStorageError(AuditError):
    pass


def canonical(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True,
                       separators=(",", ":")) + "\n").encode("utf-8")


def configuration(project: Path):
    """只有已绑定的参半原生部署可启用；普通环境变量不能误开。"""
    path = project / "config" / CONFIG_NAME
    if not path.exists():
        return None
    if (os.name != "posix" or project.resolve() != Path("/data/offline-activity-audit")
            or socket.gethostname() != "dingding-K3"):
        raise ArtifactStorageError("产物 OSS 配置不属于当前部署，禁止启用")
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ArtifactStorageError("产物 OSS 配置必须是普通文件")
    try:
        value = json.loads(path.read_text("utf-8"))
        expected = {"schema_version": "1.0", "deployment": "canban-dingding-K3",
                    "bucket": BUCKET, "endpoint": ENDPOINT, "prefix": PREFIX,
                    "enabled": True}
        if type(value.get("cleanup_enabled")) is not bool or {
                key: part for key, part in value.items() if key != "cleanup_enabled"} != expected:
            raise ValueError()
    except (OSError, ValueError):
        raise ArtifactStorageError("产物 OSS 配置不符合已批准范围") from None
    return value


def check_key(key):
    if (not isinstance(key, str) or not key.startswith(PREFIX)
            or len(key.encode("utf-8")) > 1023):
        raise ArtifactStorageError("产物对象越出专用 OSS 前缀")
    parts = key[len(PREFIX):].split("/")
    if (not parts or any(not re.fullmatch(r"[a-zA-Z0-9_.-]{1,180}", part)
                         or part in {".", ".."} for part in parts)):
        raise ArtifactStorageError("产物 OSS 对象路径不安全")


def safe_error(error):
    from .product_oss import error_code
    return error_code(error)


class ArtifactOSS:
    """接口仅暴露专用前缀；不改变 Bucket ACL、RAM、商品图片或原上传对象。"""
    def __init__(self, credentials_path: Path):
        from .private_credentials import read_private_file
        import alibabacloud_oss_v2 as oss
        import requests
        from alibabacloud_oss_v2.transport.requests_client import RequestsHttpClient
        try:
            values = json.loads(read_private_file(credentials_path))
            if set(values) != {"accessKeyId", "accessKeySecret"} or not all(
                    isinstance(v, str) and re.fullmatch(r"[A-Za-z0-9]{16,128}", v)
                    for v in values.values()):
                raise ValueError()
        except (ValueError, OSError):
            raise ArtifactStorageError("产物 OSS 私有凭据无法读取") from None
        self.oss = oss
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.proxies.clear()
        cfg = oss.config.load_default()
        cfg.region, cfg.endpoint = REGION, ENDPOINT
        cfg.credentials_provider = oss.credentials.StaticCredentialsProvider(
            values["accessKeyId"], values["accessKeySecret"])
        cfg.http_client = RequestsHttpClient(
            session=self.session, connect_timeout=15, readwrite_timeout=120,
            enabled_redirect=False, insecure_skip_verify=False)
        cfg.enabled_redirect, cfg.retry_max_attempts = False, 2
        self.client = oss.Client(cfg)
        values.clear()

    def close(self):
        self.session.close()

    def _call(self, method, key, request, *, missing=False):
        check_key(key)
        try:
            return method(request)
        except Exception as error:
            code = safe_error(error)
            if missing and code == "NoSuchKey":
                return None
            raise ArtifactStorageError("产物 OSS 请求失败（" + code + "）") from None

    def head(self, key):
        result = self._call(self.client.head_object, key,
            self.oss.HeadObjectRequest(bucket=BUCKET, key=key), missing=True)
        if result is None:
            return None
        return {"size": result.content_length, "metadata": dict(result.metadata or {})}

    def list(self, section):
        key = PREFIX + section + "/placeholder"
        check_key(key)
        token = None
        while True:
            page = self._call(self.client.list_objects_v2, key,
                self.oss.ListObjectsV2Request(bucket=BUCKET, prefix=PREFIX+section+"/",
                    continuation_token=token, max_keys=1000))
            for item in page.contents or []:
                check_key(item.key)
                yield {"key": item.key, "etag": item.etag, "size": item.size}
            if not page.is_truncated:
                return
            following = page.next_continuation_token
            if not following or following == token:
                raise ArtifactStorageError("OSS 目录分页未完整返回")
            token = following

    def read_to(self, key, output, *, expected_size=None, expected_sha=None,
                limit=4*1024**3):
        result = self._call(self.client.get_object, key,
            self.oss.GetObjectRequest(bucket=BUCKET, key=key))
        total, digest = 0, hashlib.sha256()
        try:
            with result.body as stream:
                length = result.content_length
                if (length is None or length < 0 or length > limit
                        or expected_size is not None and length != expected_size):
                    raise ArtifactStorageError("OSS 产物长度与清单不符")
                for piece in stream.iter_bytes(block_size=1024*1024):
                    total += len(piece)
                    if total > limit:
                        raise ArtifactStorageError("OSS 产物流超过清单范围")
                    output.write(piece)
                    digest.update(piece)
                if total != length or expected_sha and digest.hexdigest() != expected_sha:
                    raise ArtifactStorageError("OSS 产物完整长度或 SHA-256 校验失败")
        except ArtifactStorageError:
            raise
        except Exception as error:
            raise ArtifactStorageError("OSS 产物读取失败（"+safe_error(error)+"）") from None
        return digest.hexdigest()

    def read(self, key, *, limit=32*1024**2):
        output = io.BytesIO()
        self.read_to(key, output, limit=limit)
        return output.getvalue()

    def put_immutable(self, key, source, *, size, sha, content_type):
        """已有对象必须逐字节核验；永不以同名覆盖旧历史。"""
        prior = self.head(key)
        if prior is None:
            try:
                self._call(self.client.put_object, key,
                    self.oss.PutObjectRequest(bucket=BUCKET, key=key, body=source,
                        content_length=size, content_type=content_type, acl="private",
                        forbid_overwrite=True, cache_control="private, no-cache",
                        metadata={"managed-by": OWNER, "sha256": sha}))
            except ArtifactStorageError as error:
                if "FileAlreadyExists" not in str(error):
                    raise
            prior = self.head(key)
        if (prior is None or prior["size"] != size
                or prior["metadata"].get("managed-by") != OWNER
                or prior["metadata"].get("sha256") != sha):
            raise ArtifactStorageError("OSS 同名产物不属于本次完整内容，未覆盖")
        class Discard:
            def write(self, piece):
                return len(piece)
        self.read_to(key, Discard(), expected_size=size, expected_sha=sha,
                     limit=max(size, 1))

    def put_json(self, key, value):
        raw = canonical(value)
        self.put_immutable(key, io.BytesIO(raw), size=len(raw),
            sha=hashlib.sha256(raw).hexdigest(), content_type="application/json")

    def delete_index(self, key):
        if not key.startswith(PREFIX+"index/"):
            raise ArtifactStorageError("这里只允许删除已进入回收站的目录索引")
        self._call(self.client.delete_object, key,
            self.oss.DeleteObjectRequest(bucket=BUCKET, key=key))
