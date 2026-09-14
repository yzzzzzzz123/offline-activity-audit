---
name: new-product-onboarding-rag-workflow
description: 先独立观察新品包装与有效 EAN-13，再从商品数据库同码候选中唯一匹配身份，预演并向私有 OSS 发布参考图片。用于新品图片接入及库存工作簿对账，不更改商品身份，不用于现场核销证据。
---

# 新商品接入 RAG 工作流

以仓库项目根目录作为工作目录。操作前阅读其中的 `AGENTS.md`。

先读取[数据库知识规则](../../shared/product-database/audit-knowledge.md)。
商品名称、编码和69码统一来自 MySQL `product_catalog.products`；通过
`audit_core.product_database.load_product_catalog` 查询。参考图只存放在私有 OSS；
不读取本地知识库或远端灾备。没有数据库记录时先完成数据库登记。

## 身份门槛

1. 检查每个直接样品文件夹及其相关视图。只记录实际可见的商品名称、规格或变体，以及完整条码。条码必须以 `69` 开头并通过 EAN-13 校验。
2. 打开权威工作簿前，先完成视觉观察清单。Excel 是独立对账来源，不能用来填补不可读的包装信息。
3. 读取本次数据库快照，仅保留 `barcode_69` 与观察条码准确一致、且三个身份字段有效的记录。
4. 在条码完全一致的集合中，要求有唯一一个商品名称模糊相容候选；不要求名称逐字相同。可见的变体或香型文字可以消歧。未看到的 `套盒专用`、`箱规`、`升级配方`、`代言人`、国际标签或客户定制版本等修饰词不能在平局中胜出。
5. 数据库无对应记录、条码无效、名称冲突或多候选未唯一收敛时保持未解决。准确区分待登记主数据与包装图片不清，不能任取第一条记录。

创建或审查观察清单时，阅读 [manifest-and-selection.md](references/manifest-and-selection.md)，
并使用 [observation-manifest.schema.json](references/observation-manifest.schema.json) 校验。

## 确定性工作流

### 唯一图片来源：OSS

先完成上述独立视觉观察和数据库身份门槛，不得仅凭目录名认定商品。
`shared/product-database/product-images.json` 是必需的 OSS 配置，缺失时停止。参考图使用
`offline-verify/product-reference/<product_code>/manifest.json`，不再写入本地常驻 `products/`。
完整图片集放在操作者明确指定的单商品来源目录；更新同一视图沿用原文件名，不创建版本目录。
先预演并审查准确商品编码、69码、来源图片及目标 Object Key；只有身份与写入范围获准才加 `--apply`：

```powershell
py -3 -B shared/product-database/publish_product_images.py --product-code '<准确编码>' --source-dir '<已确认的完整多视图目录>'
py -3 -B shared/product-database/publish_product_images.py --product-code '<准确编码>' --source-dir '<已确认的完整多视图目录>' --apply
```

此命令只维护图片和必要的 `image_manifest_key` 关联，不修改商品名称、编码或69码。
先上传并逐张回读校验，再发布固定清单。原始素材不删除；图片删除、跨商品覆盖及桶级设置不在权限内。
旧视图不得改名产生历史副本。强制中断留下发布锁时，先确认无同商品发布任务，再清理准确锁文件；不得自动抢锁。
多机维护须协调为单一写入方；工具的同商品互斥是本机锁，不承诺分布式事务。

### 只读匹配与发布前核验

原始素材是操作者明确指定的输入目录，不是运行知识库，不得修改或删除。先完成视觉观察，再生成只读计划：

```powershell
py -3 -B skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py match --source-root '<逐商品素材目录>' --observations '<观察清单.json>' --output '<接入计划.json>'
py -3 -B skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py validate --plan-file '<接入计划.json>'
```

计划版本为 `2.0`，记录数据库身份及图片关联哈希、所选三字段、固定 OSS 清单 Object Key 和来源图片哈希。
审查 `selection_status`、`selection_basis` 及所有同码候选；无法唯一收敛时不发布。
`validate` 重新查库、重新匹配观察并核验原始图片，始终不上传。计划陈旧、身份被修改或来源图片改变时重新 `match`。
通过后仍须使用上面的 `publish_product_images.py` 逐商品预演；只有写入获准才加 `--apply`。
旧本地目录 `apply` 命令已移除，不能用旧计划恢复本地图库。

## 工作簿对账

导入后，将数据库记录与权威工作簿中有库存资格的行比较。商品编码和 69 码必须严格一致；
同一严格商品的名称只需模糊相容，不要求逐字相同。默认排除标记是快递单号列中的准确文字 `无库存，未发出`。

```powershell
py -3 -B skills/new-product-onboarding-rag-workflow/scripts/onboard_product_rag.py reconcile `
  --workbook '<权威清单.xlsx>' `
  --output '<对账结果.json>'
```

只有以下条件全部满足才能宣布完成：`passed=true`；预期库存数量等于目录数量；`missing_products`、
`catalog_extra_products`、`field_mismatches` 及重复编码列表均为空；数据库行数与当前快照一致。
图片完整性单独执行 `py -3 -B shared/product-database/product_images_oss.py verify --images`，
从数据库关联的 OSS 清单流式回读图片并校验 SHA-256，不下载常驻图库。库存对账通过不等于图片发布成功。
