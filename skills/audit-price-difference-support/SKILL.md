---
name: audit-price-difference-support
description: 使用经销商盖章 POS、对应 POS 电子表格、公司模板盖章结算单、全部门店活动价照片和已签署促销合同，核销价格补差报销材料包。仅用于已登记的 price_difference_support 场景。
---

# 价格补差核销

涉及商品知识时，先读取[数据库知识规则](../../shared/product-database/audit-knowledge.md)，统一通过
`audit_core.product_database.load_product_catalog` 查询数据库；需要参考图片时，只按数据库的 `image_manifest_key` 从私有 OSS 取图；不存在本地图库入口或回退。
沿用本场景原有业务证据链，数据源调整不额外增加商品主账检查条件。

生成客户错误原因和处理方式前，必须读取并执行[统一文案规范](../orchestrate-offline-audit/references/error-reasons.md)。
错误原因只写具体错误事实，建议只写处理方式；涉及的业务文件区域只列相关实际文件 basename；回调只保留原因。核销类型由 ZIP 名称的唯一已登记标记确定，AI 不得改类。ZIP 名称无法唯一分类时由编排器直接打回，不调用本 Skill 或 AI；已知类型的错件、缺件及内部文件误命名仍继续审核或材料诊断。

以闭合证据链核销一项价格补差申报。提取或判断任何案例前，必须完整读取
[audit-rules.md](references/audit-rules.md)。可见事实必须符合
[evidence.schema.json](references/evidence.schema.json)。规范化场景合同及代表性材料包来源位于
[scenario-manifest.json](references/scenario-manifest.json)。

必备证据链包括：经销商盖章 POS 数据、对应电子表格、经销商盖章的公司模板结算单、每家活动
门店清楚展示活动价格且带日期/地址/时间的现场照片，以及已经签署的促销合同。如果外层材料包
能够明确识别为价格补差申报，缺失资料应保留为报告中的阻断问题。

AI 只读取隔离的视觉来源并提取可见事实。只有确定性 Python 可以读取电子表格、检查准确来源
覆盖、核对门店/商品/期间/价格、复算合同单位支持金额、应用数量和预算上限，并设置支持金额。
零售价降幅不会自动成为报销费率：只能使用已签署合同明确写出的支持单价。

在内部工作簿中保留 `价格补差核销` 六列投影用于验证；单一自包含 HTML 分别显示具体错误原因及处理方式，业务文件区域只列相关实际文件名。任何阻断
失败都暂缓经销商盖章结算单的全部申报金额；绝不能把已提交照片外推到没有自身有效活动期照片
的门店。

只能通过父级命令运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario price_difference_support
```
