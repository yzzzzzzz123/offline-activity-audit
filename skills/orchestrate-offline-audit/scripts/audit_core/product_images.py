"""商品参考图片唯一入口：数据库身份 + 私有 OSS 清单，不提供本地图库读取。"""
from __future__ import annotations

from typing import Any

from .common import AuditError
from .product_oss import require_configuration


def attach_product_reference_images(catalog: dict[str, Any]) -> dict[str, Any]:
    require_configuration()
    if (catalog.get("data_source") or {}).get("type") != "mysql":
        raise AuditError("商品参考图只接受本次 MySQL 商品快照")
    from .product_image_runtime import attach_oss_images
    return attach_oss_images(catalog)
