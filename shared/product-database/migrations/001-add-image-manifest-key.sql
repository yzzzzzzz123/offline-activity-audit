-- 仅新增可空字段；已有数据卷由 dbctl.py 检查后执行，不覆盖商品或图片地址。
SET SESSION lock_wait_timeout = 5;
ALTER TABLE product_catalog.products
  ADD COLUMN image_manifest_key VARCHAR(1024)
    CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL DEFAULT NULL
    COMMENT '商品图片集：OSS 清单 Object Key，不存临时签名链接',
  ALGORITHM=INSTANT;
