# 商品数据库与参考图片

客户环境使用阿里云 RDS MySQL 8.0/8.4。安装、配置、迁移、接口和运维步骤统一见项目根目录 [README.md](../../README.md)，不在此重复部署命令。

## 数据库

| 项目 | 说明 |
| --- | --- |
| 正式表结构 | [deploy/rds/schema.sql](../../deploy/rds/schema.sql) |
| 迁移与验收工具 | [deploy/rds.py](../../deploy/rds.py) |
| 商品数据交付 | `deploy/seed/product-catalog.sql` 及随附 SHA-256、逐字段清单 |
| 连接配置模板 | [deploy/application.env.example](../../deploy/application.env.example) |
| 正式读取入口 | `audit_core.product_database.load_product_catalog` |
| 业务使用边界 | [audit-knowledge.md](audit-knowledge.md) |

库名由 `OFFLINE_AUDIT_PRODUCT_DB_NAME` 配置，表名为 `products`。应用使用专用只读账号，通过 CA 与服务器名称校验的 TLS 连接；同次核销共用只读一致性快照，下次重新读取。连接故障、空表和不完整数据属于系统错误，不读取旧 JSON 补值。

| 字段 | 类型与约束 |
| --- | --- |
| `barcode_69` | `CHAR(13)`，非空；允许不同商品编码共用条码 |
| `product_name` | `VARCHAR(512)`，非空 |
| `product_code` | `VARCHAR(64)`，主键，区分大小写 |
| `image_manifest_key` | `VARCHAR(1024)`，允许 `NULL`；商品清单的稳定 OSS Object Key |

当前受控迁移快照包含 130 个商品编码、119 个不同条码。导入工具校验全部四个字段，不清空或覆盖目标已有数据。RDS 商品维护由 DBA 使用独立写入账号处理，应用配置不保存迁移或管理账号密码。

## 商品参考图

配置范围见 [product-images.json](product-images.json)，运行适配器为 `audit_core/product_oss.py`。

- Endpoint：`https://oss-cn-shenzhen.aliyuncs.com`。
- Bucket：`xiaokuo-dingding-xianxiahexiao`。
- 固定目录：`offline-verify/product-reference/<product_code>/`。
- 商品清单：上述目录内的 `manifest.json`，数据库只保存其稳定 Object Key，不保存临时签名 URL。
- Linux 凭据：`config/secrets/product-oss.json`，仅含 `accessKeyId`、`accessKeySecret`，权限为 0600。

先从本次合同、协议及申请说明唯一确定商品，再读取这些商品的清单及细节图；不扫描整库、不按门店重复下载同一商品。清单、图片大小与 SHA-256 必须匹配，技术读取错误不能冒充“库内无参考商品”。完整业务边界见 [audit-knowledge.md](audit-knowledge.md)。

2026-11-02新增跨核销参考图字节缓存：每次重新读取数据库及所选商品的最新 OSS 清单，仅复用本部署 `worktrees/.cache/product-images-v1/` 中满足本次清单 SHA-256、大小、尺寸、类型的图片，变化或损坏时重拉。缓存上限2 GiB，按最近使用淘汰；清单读取失败不回退旧图，不缓存商品身份或 AI 结论。模型仍使用独立任务副本，图片重复提交模型仍产生相应用量。缓存不作为商品库或历史证据迁移，完整机制见根目录 README 8.3。

商品参考图沿用同一商品的固定 Object Key，不创建版本目录。变更先取得具体商品的写入授权，上传并回读验证图片，再更新清单，最后由 DBA 核对 RDS 关联。同一商品更新串行执行；历史档案可以保留对象路径、哈希和使用记录，覆盖后的旧参考图不承诺可恢复。原始业务 ZIP 和核销档案不采用图片覆盖策略。

## 源环境兼容文件

本目录的 `compose.yaml`、`dbctl.py`、`schema.sql`、`my.ini`、旧迁移和 Windows 脚本仅为原 Windows 源环境保留。`publish_product_images.py` 的旧数据库自动关联也属于源端维护流程，不作为客户 RDS 写入入口。客户运行服务、初始化数据库及后续迁移均使用根目录 `deploy/` 工具。

源端服务交接期间仍需读取原数据库，不能把其配置、数据卷或凭据当作无关文件删除。Windows DPAPI 密文不能直接搬到 Linux；客户按根目录 README 重新录入凭据。
