from __future__ import annotations

import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = (
    PROJECT_ROOT / "skills" / "audit-promotional-display" / "scripts"
)
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from audit_core.common import AuditError
from ingest_product_reference import _catalog_product_directory
from sync_product_identity_by_code import (
    _catalog_product_id,
    _exact_mapping_index,
    _parse_product_directory_name,
    _specification_from_product_name,
)


class ProductIdentitySyncTests(unittest.TestCase):
    def test_registration_extracts_only_explicit_interface_specification(self) -> None:
        self.assertEqual(
            _specification_from_product_name("参半专护敏牙膏(140g)-线下"),
            "140g",
        )
        self.assertEqual(
            _specification_from_product_name("参半益生菌漱口水 海洋薄荷（250ml）-线下"),
            "250ml",
        )
        self.assertEqual(
            _specification_from_product_name("参半仙女棒牙刷(单支装)粉色-线下"),
            "单支装",
        )
        self.assertEqual(
            _specification_from_product_name("没有明确规格的接口名称"),
            "未标注",
        )

    def test_shared_barcode_product_id_includes_fixed_product_code(self) -> None:
        self.assertEqual(
            _catalog_product_id(
                "6970356166436",
                "CP-GJ-SDS-0105",
                shared_barcode=True,
            ),
            "canban-6970356166436-cp-gj-sds-0105",
        )
        self.assertEqual(
            _catalog_product_id(
                "6970356161691",
                "CP-KQ-YG-0206",
                shared_barcode=False,
            ),
            "canban-6970356161691",
        )

    def test_bare_product_code_directory_is_a_supported_staging_name(self) -> None:
        self.assertEqual(
            _parse_product_directory_name("CP-KQ-YG-0206"),
            {
                "barcode_69": "",
                "product_name": "",
                "product_code": "CP-KQ-YG-0206",
            },
        )

    def test_directory_keeps_interface_name_and_product_code(self) -> None:
        directory = _catalog_product_directory(
            "6970356166832",
            "参半 牙膏*联名/线下",
            "CP-KQ-YG-0205",
        )
        self.assertEqual(
            directory.name,
            "6970356166832__参半 牙膏×联名／线下__CP-KQ-YG-0205",
        )

    def test_exact_mapping_uses_only_same_product_code(self) -> None:
        result = _exact_mapping_index(
            [
                {
                    "product_code": "CP-KQ-YG-0205",
                    "api_status": 200,
                    "exact_rows": [
                        {
                            "product_code": "A-DIFFERENT-CODE",
                            "product_name": "不得误用的相似商品",
                            "barcode": "6970356169338",
                        },
                        {
                            "product_code": "CP-KQ-YG-0205",
                            "product_name": "参半羟基磷灰石牙膏100g满陇桂雨味",
                            "barcode": "6970356166832",
                        },
                    ],
                }
            ]
        )
        self.assertEqual(
            result["CP-KQ-YG-0205"],
            {
                "product_code": "CP-KQ-YG-0205",
                "product_name": "参半羟基磷灰石牙膏100g满陇桂雨味",
                "barcode": "6970356166832",
            },
        )

    def test_exact_mapping_rejects_no_same_code_result(self) -> None:
        with self.assertRaisesRegex(AuditError, "没有唯一精确接口返回"):
            _exact_mapping_index(
                [
                    {
                        "product_code": "CP-KQ-YG-0205",
                        "api_status": 200,
                        "exact_rows": [
                            {
                                "product_code": "A-DIFFERENT-CODE",
                                "product_name": "相似名称也不能使用",
                                "barcode": "6970356166832",
                            }
                        ],
                    }
                ]
            )

    def test_exact_mapping_rejects_ambiguous_same_code_results(self) -> None:
        with self.assertRaisesRegex(AuditError, "exact_count=2"):
            _exact_mapping_index(
                [
                    {
                        "product_code": "CP-KQ-YG-0205",
                        "api_status": 200,
                        "exact_rows": [
                            {
                                "product_code": "CP-KQ-YG-0205",
                                "product_name": "商品A",
                                "barcode": "6970356166832",
                            },
                            {
                                "product_code": "CP-KQ-YG-0205",
                                "product_name": "商品B",
                                "barcode": "6970356169338",
                            },
                        ],
                    }
                ]
            )

    def test_exact_mapping_rejects_invalid_ean13(self) -> None:
        with self.assertRaisesRegex(AuditError, "未通过EAN-13校验"):
            _exact_mapping_index(
                [
                    {
                        "product_code": "CP-KQ-YG-0205",
                        "api_status": 200,
                        "exact_rows": [
                            {
                                "product_code": "CP-KQ-YG-0205",
                                "product_name": "商品A",
                                "barcode": "6970356166833",
                            }
                        ],
                    }
                ]
            )


if __name__ == "__main__":
    unittest.main()
