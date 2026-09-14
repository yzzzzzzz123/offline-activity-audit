---
name: audit-pos-target-incentive
description: 核销以达标 POS 销售额和已签署合同门槛费率计算、激励对象为经销商的 POS 达标激励，包括盖章 POS、对应电子表格、盖章公司模板结算单、满减促销活动存在证明和已签署促销合同。仅用于 pos_target_incentive。
---

# POS 达标激励核销

涉及商品知识时，先读取[数据库知识规则](../../shared/product-database/audit-knowledge.md)，统一通过
`audit_core.product_database.load_product_catalog` 查询数据库；需要参考图片时，只按数据库的 `image_manifest_key` 从私有 OSS 取图；不存在本地图库入口或回退。
沿用本场景原有业务证据链，数据源调整不额外增加商品主账检查条件。

生成客户错误原因和处理方式前，必须读取并执行[统一文案规范](../orchestrate-offline-audit/references/error-reasons.md)。
错误原因只写具体错误事实，建议只写处理方式；涉及的业务文件区域只列相关实际文件 basename；回调只保留原因。核销类型由 ZIP 名称的唯一已登记标记确定，AI 不得改类。ZIP 名称无法唯一分类时由编排器直接打回，不调用本 Skill 或 AI；已知类型的错件、缺件及内部文件误命名仍继续审核或材料诊断。

提取或判断任何案例前，必须完整读取 [audit-rules.md](references/audit-rules.md)。可见事实阶段必须
符合 [evidence.schema.json](references/evidence.schema.json)；规范化来源定义位于
[scenario-manifest.json](references/scenario-manifest.json)。

激励对象是经销商，不是终端门店。已签署促销合同必须确立已批准渠道资格、POS 范围、活动期间、
目标档位、费率、上限、激励对象和活动机制。经销商盖章 POS 及其电子工作簿确立销售基数。对于
满减促销，必须由带日期/地址/时间的现场照片、小票或其他来源证明活动确实存在。经销商盖章的
公司模板结算单提供申报金额。

AI 只读取隔离的视觉来源。确定性 Python 读取工作簿、检查来源覆盖及 POS 对应、选择实际达到的
最高合同档位、应用其费率和上限、比较结算单并设置支持金额。缺少权威依据或活动证明是报告中
的阻断问题，不能因此从结算单借用规则。

保留内部六列 `POS达标激励核销` 结果用于验证；规范自包含 HTML 分别显示错误原因与处理方式，业务文件区域只列相关实际文件名。只能通过父级命令运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario pos_target_incentive
```
