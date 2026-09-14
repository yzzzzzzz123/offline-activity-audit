---
name: audit-maintenance-fee
description: 核销需要经销商盖章 POS 数据、对应 POS 电子表格、公司模板盖章结算单、已签署促销合同及费用专项支持资料的维护费用报销材料包。仅用于已登记的 maintenance_fee 场景；不得用于直营、人员激励或其他费用材料包。
---

# 维护费用核销

涉及商品知识时，先读取[数据库知识规则](../../shared/product-database/audit-knowledge.md)，统一通过
`audit_core.product_database.load_product_catalog` 查询数据库；需要参考图片时，只按数据库的 `image_manifest_key` 从私有 OSS 取图；不存在本地图库入口或回退。
沿用本场景原有业务证据链，数据源调整不额外增加商品主账检查条件。

生成客户错误原因和处理方式前，必须读取并执行[统一文案规范](../orchestrate-offline-audit/references/error-reasons.md)。
错误原因只写具体错误事实，建议只写处理方式；涉及的业务文件区域只列相关实际文件 basename；回调只保留原因。核销类型由 ZIP 名称的唯一已登记标记确定，AI 不得改类。ZIP 名称无法唯一分类时由编排器直接打回，不调用本 Skill 或 AI；已知类型的错件、缺件及内部文件误命名仍继续审核或材料诊断。

将一项维护费用申报作为闭合证据链核销。外层 ZIP 名称的唯一维护费用标记锁定本 Skill；文档
可见内容是否符合维护费用要求仍由本场景规则核验，不能据此改选另一种核销类型。

提取或判断任何案例前，必须完整读取 [audit-rules.md](references/audit-rules.md)。可见事实阶段
必须符合 [evidence.schema.json](references/evidence.schema.json)。规范化场景合同及代表性材料包
来源位于 [scenario-manifest.json](references/scenario-manifest.json)。

## 证据链

必备证据链为：

1. 经销商盖章 POS 数据；
2. 对应的 POS 电子表格；
3. 费用专项合同、协议、相关文件，以及约定活动要求现场执行时的活动期照片；
4. 使用公司模板并由经销商盖章的结算单，显示分项费用、计算方法和申报金额；
5. 已签署促销合同，明确活动范围、期间、符合条件的 POS 范围、计算规则、费率及任何金额上限。

如果 ZIP 名称明确标记为维护费用，缺失资料是阻断性核销事实，不应导致接入
崩溃。每个已提交视觉来源必须只绑定一次，并由提取阶段只返回一次。

## 信任边界

- AI 只读取隔离的视觉文件并提取可见事实。它看不到 POS 电子表格，不计算金额、不分类最终
  申报，也不使用一个来源修补另一个来源。
- 确定性 Python 读取 POS 电子表格、检查来源覆盖、复算合计和合同公式、比较相关方/期间/费用
  性质、分组阻断问题、设置支持金额、验证结果 Schema 并发布报告。
- 文件名、压缩包标签、合计一致或结算单陈述，都不能替代缺失的合同、电子表格、经销商印章、
  计算依据或执行记录。
- 保留原有 `other_expense` 和 `personnel_incentive` 权威链。文档可见内容明确表示另一种费用性质
  时，报告为维护费用分类冲突；不得静默路由到另一个场景。

## 输出合同

在内部六列工作簿投影中保留来源、观察、规则、结论、金额影响和处理操作。单一自包含 HTML 的
`维护费用核销` 错误原因只写具体缺失、不可读或差异，对应建议单独写在处理方式。只有全部
阻断控制通过，且经销商盖章结算申报与根据已签署合同和 POS 电子表格确定性复算的金额精确到分
一致时，才支持该申报。

本场景只能通过父级命令运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario maintenance_fee
```
