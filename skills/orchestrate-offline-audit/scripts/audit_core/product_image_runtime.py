"""按核销任务固定 OSS 清单快照；只下载被选中的参考图，退出即清理。"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import shutil
from threading import RLock

from .common import AuditError, sha256_file
from .run_temporary import ManagedTemporaryDirectory, current_temporary_scope
from .product_oss import ProductOSS, MAX_IMAGE_BYTES, MAX_MANIFEST_BYTES, manifest_key, validate_manifest


@dataclass
class ImageState:
    lock: RLock = field(default_factory=RLock)
    manifests: dict = field(default_factory=dict)
    temporary: ManagedTemporaryDirectory | None = None
    temporary_scope: Path = field(default_factory=current_temporary_scope)


_STATE: ContextVar[ImageState | None] = ContextVar("product_oss_images", default=None)


@contextmanager
def product_image_scope():
    state = ImageState()
    token = _STATE.set(state)
    try:
        yield state
    finally:
        try:
            if state.temporary:
                state.temporary.cleanup()
        finally:
            _STATE.reset(token)


def _manifest(product: dict, state: ImageState) -> tuple[dict, str]:
    key = product.get("image_manifest_key")
    if key != manifest_key(product["product_code"]):
        raise AuditError("商品数据库没有本商品准确的 OSS 清单地址")
    identity = (product["product_code"], product["barcode_69"], key)
    with state.lock:
        if identity not in state.manifests:
            with ProductOSS() as client:
                data = client.read(key, limit=MAX_MANIFEST_BYTES)
            value = validate_manifest(data, product)
            state.manifests[identity] = (value, hashlib.sha256(data).hexdigest())
        return deepcopy(state.manifests[identity])


def attach_oss_images(catalog: dict) -> dict:
    result = deepcopy(catalog)
    state = _STATE.get() or ImageState()
    for product in result.get("products") or []:
        product["views"] = []
        if not product.get("image_manifest_key"):
            product["match_policy"] = "candidate_only"
            continue
        manifest, digest = _manifest(product, state)
        product["image_manifest_sha256"] = digest
        product["views"] = [{**view, "image_file": view["object_key"],
                             "manifest_key": product["image_manifest_key"], "manifest_sha256": digest,
                             "face": "unclassified", "identity_strength": "unreviewed", "visible_anchors": []}
                            for view in manifest["views"]]
        product["match_policy"] = "exact_or_candidate"
    return result


def _download(view: dict, target: Path) -> None:
    from PIL import Image
    pending = target.with_suffix(target.suffix + ".part")
    if target.exists() or pending.exists() or target.is_symlink() or pending.is_symlink():
        raise AuditError("商品参考图下载目标已存在，未覆盖")
    try:
        with ProductOSS() as client, pending.open("xb") as output:
            client.read(view["object_key"], limit=MAX_IMAGE_BYTES, output=output,
                        expected_sha=view["sha256"], expected_size=view["size_bytes"])
        with Image.open(pending) as picture:
            if picture.size != (view["width"], view["height"]):
                raise AuditError("商品 OSS 图片尺寸与清单不一致")
            if Image.MIME.get(picture.format) != view["content_type"]:
                raise AuditError("商品 OSS 图片类型与清单不一致")
            picture.verify()
        pending.replace(target)
    except AuditError:
        raise
    except Exception:
        raise AuditError("商品 OSS 图片解码或临时写入失败") from None
    finally:
        pending.unlink(missing_ok=True)


def copy_oss_image(product: dict, view: dict, target: Path) -> None:
    state = _STATE.get()
    if state is None:
        _download(view, target)
    else:
        # 同任务在并行门店步骤共享已验证临时字节；模型只能读取它自己的副本。
        with state.lock:
            if state.temporary is None:
                state.temporary = ManagedTemporaryDirectory("product-images", scope=state.temporary_scope)
            cached = Path(state.temporary.name) / (view["sha256"] + Path(view["object_key"]).suffix.lower())
            if not cached.exists():
                # 同一路径覆盖期间最多重读一次原清单及图片，仍不一致就失败关闭，绝不拼用新旧视图。
                try:
                    _download(view, cached)
                except AuditError:
                    with ProductOSS() as client:
                        data = client.read(view["manifest_key"], limit=MAX_MANIFEST_BYTES)
                    validate_manifest(data, product)
                    if hashlib.sha256(data).hexdigest() != view["manifest_sha256"]:
                        raise AuditError("商品参考图正在更新，清单快照已变化；本次未混用新旧图片，请重新核验") from None
                    _download(view, cached)
            if sha256_file(cached) != view["sha256"]:
                raise AuditError("任务内参考图片缓存 SHA-256 校验失败")
            if target.exists() or target.is_symlink():
                raise AuditError("模型参考图片目标已存在，未覆盖")
            shutil.copy2(cached, target)
    # 来源留在内部技术证据，不保存签名 URL、密钥或图片字节。
    from .model_metrics import save_model_observations
    name = "oss-reference-" + hashlib.sha256((product["product_code"] + view["view_id"]).encode()).hexdigest()[:24]
    save_model_observations(name, {"source": "oss", "product_code": product["product_code"],
                                  "barcode_69": product["barcode_69"], "view_id": view["view_id"],
                                  **{k: view[k] for k in ("object_key", "sha256", "size_bytes", "manifest_key", "manifest_sha256")}})
