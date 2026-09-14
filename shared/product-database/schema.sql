CREATE DATABASE product_catalog
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_cs;

CREATE TABLE product_catalog.products (
  barcode_69 CHAR(13) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  product_name VARCHAR(512) NOT NULL,
  product_code VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
  image_manifest_key VARCHAR(1024) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin
    NULL DEFAULT NULL COMMENT '商品图片集：OSS 清单 Object Key，不存临时签名链接',
  PRIMARY KEY (product_code),
  KEY idx_products_barcode_69 (barcode_69)
) ENGINE=InnoDB;
