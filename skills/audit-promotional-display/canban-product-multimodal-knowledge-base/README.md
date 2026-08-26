# Canban Product Multimodal Knowledge Base

本目录保存商品参考图的受控副本，用于现场商品视觉检索和逐图比对。原始素材集合保持只读；知识库标准化不会改名、移动、删除或改写原始目录及图片。

## 当前内容

- `products/` 共有 118 个商品目录、499 个图片文件。
- 正式 `product-rag.json` 已登记全部 118 个商品目录和 499 张参考图；这些图片都会进入运行时检索。
- 其中后补的 14 个商品、65 张图片没有可核验的原始素材集合路径；catalog 不伪造 `source_id` 或 `source_folder`，仅保留加入 catalog 时的原图片文件名，并把视图标记为 `unclassified/unreviewed`。
- 118 个目录的产品编码均唯一；产品编码是目录与接口映射的固定主键。
- `8.19样品申请汇总表.xlsx` 及各原始素材集合保持不变。

正式目录事实保存在 `../references/product-rag.json`，并按 `../references/product-rag.schema.json` 校验。目录命名固定为：

`products/<接口69码>__<接口产品名称的Windows安全标签>__<固定产品编码>/`

产品名称只替换 Windows 禁止用于目录名的字符；例如接口原文中的 `*` 在目录名中显示为 `×`，`/` 显示为 `／`。catalog 的 `product_name` 保留接口原文，图片文件名和图片内容不变。

## 接口同步规则

1. 从现有知识库目录末尾读取产品编码，产品编码在本流程中固定不变。新加入的目录允许暂时只使用产品编码命名，首次同步后必须转换为完整三字段目录名。
2. 商品编码映射接口只能使用 `product_code` 参数查询；不得使用现有 69 码反查，也不得用产品名模糊搜索接口结果。
3. 只接受接口结果中 `product_code` 与查询值完全相等的记录。
4. 每个产品编码必须唯一收敛到一组产品编码、产品名称和 69 码；没有精确结果、多个不同精确结果、空名称或无效 EAN-13 均失败关闭。
5. 接口产品名称和 69 码更新目录；已登记商品同时更新 catalog、身份锚点和图片路径。旧名称仅作为 alias 保留，产品编码和 `sources[].source_folder` 不变。
6. 目录移动使用临时中间名并在失败时回滚；catalog 先做 Schema 校验，再原子替换，最后重新校验全部图片路径与 SHA-256。

维护脚本分为获取、计划和应用三步；密钥只从进程环境或 Windows 用户环境读取，不写入仓库：

```powershell
$mappingFile = Join-Path $env:TEMP 'canban-product-code-mapping.json'
$planFile = Join-Path $env:TEMP 'canban-product-identity-plan.json'

py -3 -B skills/audit-promotional-display/scripts/sync_product_identity_by_code.py `
  fetch --output $mappingFile

py -3 -B skills/audit-promotional-display/scripts/sync_product_identity_by_code.py `
  plan --mapping-file $mappingFile --output $planFile

# 不带 --confirm 时只预演，不改动目录或 catalog。
py -3 -B skills/audit-promotional-display/scripts/sync_product_identity_by_code.py `
  apply --plan-file $planFile

py -3 -B skills/audit-promotional-display/scripts/sync_product_identity_by_code.py `
  apply --plan-file $planFile --confirm

# 将计划覆盖但尚未登记的受控目录及图片加入正式 catalog；先预演，再确认。
py -3 -B skills/audit-promotional-display/scripts/sync_product_identity_by_code.py `
  register --plan-file $planFile

py -3 -B skills/audit-promotional-display/scripts/sync_product_identity_by_code.py `
  register --plan-file $planFile --confirm
```

## 识别边界

- 完整且校验通过的 69 码是强身份锚点，但同码多商品时还必须消歧。
- 明确产品编码，或正式产品名加另一项独立包装锚点，可支持精确比对。
- 品牌、颜色、盒型、通用功效词、防伪二维码、批次和日期喷码不能单独确定商品。
- 参考图只用于判断现场包装最可能对应哪个商品，不能证明门店、日期、陈列面积、4 纵陈列、促销、价格、照片唯一性或支持金额。
- 只有 catalog 已登记且路径、Schema、SHA-256 均通过校验的受控图片才进入运行时索引。

## 有界检索

运行时不会一次附加全库图片：

1. 视觉模型先从现场图提取真正可见的商品名、产品编码和完整有效 69 码。
2. 确定性代码按这些主锚点检索正式 catalog；仅有品牌、颜色或泛词时不召回。
3. 每张现场图最多保留 2 个文本候选，单次任务最多 8 个候选商品。
4. 每个候选最多附加 4 张参考图，单次参考图上限为 32 张。
5. 第二次视觉判断将现场图与候选参考图逐图比较；证据不足时返回候选或无命中。

## 维护边界

- 原始素材集合保持只读；catalog 的 `sources[].source_folder` 必须继续指向真实原始目录。
- 不得因知识库标准化而静默改名、移动或删除原始目录；如需修正原始数据，必须由用户另行明确授权。
- 新商品必须通过受控导入器逐商品复制到知识库；不得跨商品批量猜测身份。
- 每次维护后必须重新加载 catalog，验证全部图片路径和 SHA-256，并确认物理目录与正式 catalog 商品一一覆盖。

## Git LFS 交付

正式 JPG 参考图和权威样品 Excel 使用 Git LFS，代码、Schema、catalog 与说明文档使用普通 Git。新机器克隆后先执行：

```powershell
git lfs install
git lfs pull
git lfs fsck
```

若只下载到 LFS 指针，`load_product_rag` 会因图片 SHA-256 不一致而失败关闭，不能绕过校验继续运行。

## 单商品导入

只允许使用单商品导入器，每次传入一个已人工确认的直接子文件夹；导入器拒绝通配符和跨商品批量目录，逐张复制并核对哈希，全部成功后才更新一条目录记录。

```powershell
py -3 -B skills/audit-promotional-display/scripts/ingest_product_reference.py `
  --source-dir '<一个商品文件夹的完整路径>' `
  --source-collection '参半牙具' `
  --source-id 'dental-060' `
  --product-name '参半示例商品' `
  --product-code 'CP-EXAMPLE-0001' `
  --specification '100g' `
  --variant '示例香型' `
  --barcode '69xxxxxxxxxxx'
```

条码不可见或身份无法唯一确认时必须进入人工核对，不能根据相邻商品、接口第一条非精确结果或名称相似度强行补码。
