"""小型、合成的 DB/OSS 夹具；不读取生产数据、图库备份或网络。"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import io

from PIL import Image

from audit_core.common import AuditError
from audit_core.product_database import catalog_from_rows
from audit_core.product_oss import json_bytes, manifest_key, product_prefix


def png_bytes(color="blue"):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, format="PNG")
    return buffer.getvalue()


class MemoryOSS:
    def __init__(self, objects):
        self.objects = objects
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def read(self, key, *, limit, output=None, expected_sha=None, expected_size=None):
        self.calls.append(key)
        if key not in self.objects:
            raise AuditError("OSS 测试对象不存在")
        data = self.objects[key]
        digest = hashlib.sha256(data).hexdigest()
        if len(data) > limit or expected_size not in (None, len(data)):
            raise AuditError("OSS 长度不一致")
        if expected_sha and digest != expected_sha:
            raise AuditError("OSS SHA-256 校验失败")
        if output is not None:
            output.write(data)
            return digest
        return data


def image_objects(catalog):
    objects = {}
    for product in catalog["products"]:
        views = []
        for view in product["views"]:
            data = png_bytes()
            key = product_prefix(product["product_code"]) + view["view_id"] + ".png"
            metadata = {"view_id": view["view_id"], "object_key": key, "sha256": hashlib.sha256(data).hexdigest(),
                        "size_bytes": len(data), "width": 8, "height": 8, "content_type": "image/png"}
            view.update(metadata)
            views.append(metadata)
            objects[key] = data
        product["image_manifest_key"] = manifest_key(product["product_code"])
        objects[product["image_manifest_key"]] = json_bytes({
            "schema_version": "1.0", "product_code": product["product_code"],
            "barcode_69": product["barcode_69"], "views": views,
        })
        for view in product["views"]:
            view["manifest_key"] = product["image_manifest_key"]
            view["manifest_sha256"] = hashlib.sha256(objects[product["image_manifest_key"]]).hexdigest()
    return objects


def matching_catalog():
    # 保留业务断言需要的少量示例名称和同码歧义，不复制生产知识库。
    rows = [
        ("CP-KQ-YG-0085", "6970356167341", "参半oralshark玫瑰清茶味净清新牙膏(180g)-线下", "sampleimg-003-v01"),
        ("CP-KQ-YG-0200", "6970356169338", "参半可益白牙膏葡萄知梨120g", "sample-020-v01"),
        ("CP-KQ-YG-0205", "6970356166832", "参半羟基磷灰石牙膏(100g)-线下", "sample-205-v01"),
        ("CP-KQ-YG-0260", "6970356162810", "参半沸石美白牙膏(140g)-线下", "sample-260-v01"),
        ("TEST-EDITION-260", "6970356162810", "参半限量客户定制礼盒华晨宇代言版本沸石美白牙膏(140g)-线上", "edition-260-v01"),
        ("TEST-CLEAN-001", "6970356162391", "参半沸石净齿牙膏(140g)-线下", "sample-clean-v01"),
        ("CP-KQ-YG-0016", "6970356164241", "参半oralshark凝香茉莉味氨基酸牙膏(180g)-线下", "sample-016-v01"),
        ("TEST-EDITION-016", "6970356164241", "参半限量客户定制套装凝香茉莉味氨基酸牙膏(180g)华晨宇代言人版", "edition-016-v01"),
        ("TEST-BUNDLE-001", "6970356166979", "参半牙膏3+2组合420g量贩装", "sample-009-v01"),
        ("CP-ETGJ-SDS-0021", "6978974201812", "参半儿童分龄护齿牙刷（单支装）小黄人联名款 黄色刷柄-线下", "kb-cp-etgj-sds-0021-v01"),
    ]
    catalog = catalog_from_rows([{"product_code": code, "barcode_69": barcode, "product_name": name}
                                 for code, barcode, name, _ in rows])
    by_code = {code: (barcode, view) for code, barcode, _, view in rows}
    for product in catalog["products"]:
        barcode, view = by_code[product["product_code"]]
        product["product_id"] = "canban-" + barcode
        if product["product_code"] == "CP-KQ-YG-0260":
            product["product_id"] += "-cp-kq-yg-0260"
        elif product["product_code"].startswith("TEST-EDITION"):
            product["product_id"] += "-edition"
        product["views"] = [{"view_id": view, "face": "unclassified", "identity_strength": "unreviewed",
                             "visible_anchors": []}]
        product["match_policy"] = "exact_or_candidate"
    image_objects(catalog)
    return deepcopy(catalog)
