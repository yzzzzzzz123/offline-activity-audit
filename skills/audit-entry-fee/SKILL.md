---
name: audit-entry-fee
description: 使用已签署的进场合同、合同商品条码与门店范围、带水印货架照片及合同要求的系统扣款凭证，核销进场费和条码费材料包。仅用于已登记的 entry_fee 场景。
---

# 进场费核销

涉及商品知识时，先读取[数据库知识规则](../../shared/product-database/audit-knowledge.md)，统一通过
`audit_core.product_database.load_product_catalog` 查询数据库；需要参考图片时，只按数据库的 `image_manifest_key` 从私有 OSS 取图；不存在本地图库入口或回退。
沿用本场景原有业务证据链，数据源调整不额外增加商品主账检查条件。

生成客户错误原因和处理方式前，必须读取并执行[统一文案规范](../orchestrate-offline-audit/references/error-reasons.md)。
错误原因只写具体错误事实，建议只写处理方式；涉及的业务文件区域只列相关实际文件 basename；回调只保留原因。核销类型由 ZIP 名称的唯一已登记标记确定，AI 不得改类。ZIP 名称无法唯一分类时由编排器直接打回，不调用本 Skill 或 AI；已知类型的错件、缺件及内部文件误命名仍继续审核或材料诊断。

提取或判断任何案例前，必须完整读取 [audit-rules.md](references/audit-rules.md)。视觉阶段必须
符合 [evidence.schema.json](references/evidence.schema.json)；规范化来源定义位于
[scenario-manifest.json](references/scenario-manifest.json)。

本场景覆盖商品进入渠道门店时支付的条码费/上架费。已签署进场合同是相关方、终端系统、商品
条码、门店范围、每条码费用、总额上限、支付方式和进场后证据的权威来源。即使同一行还印有
合同门店数量，每个商品条码只印刷一次的费用也不能因此视为按门店收费。

AI 读取每个隔离的合同页面和货架照片，只提取可见事实，绝不把文件夹名当作业务证据。
确定性 Python 核对合同公式、相关方和印章，将货架照片水印地点与合同门店清单对应，核验每个
合同商品是否覆盖所需门店，筛查重复照片字节，验证合同要求的系统扣款凭证，并计算支持金额。
缺少扣款凭证或权威链不完整均为阻断项。

保留内部六列 `进场费核销` 结果用于验证；规范自包含 HTML 分别显示错误原因与处理方式，业务文件区域只列相关实际文件名。只能通过父级命令运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario entry_fee
```
