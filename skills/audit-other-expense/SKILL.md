---
name: audit-other-expense
description: 核销明确申报为其他费用、无法归入既有类别且需要特殊批准新类型的线下报销材料包。用于明确以“其他费用”提交的 ZIP；不得作为可识别的赠品、陈列堆头、人员激励、海报/物料制作、维护费用、补差、搭赠、POS 达标激励或进场费材料的兜底路径。
---

# 其他费用核销

涉及商品知识时，先读取[数据库知识规则](../../shared/product-database/audit-knowledge.md)，统一通过
`audit_core.product_database.load_product_catalog` 查询数据库；需要参考图片时，只按数据库的 `image_manifest_key` 从私有 OSS 取图；不存在本地图库入口或回退。
沿用本场景原有业务证据链，数据源调整不额外增加商品主账检查条件。

生成客户错误原因和处理方式前，必须读取并执行[统一文案规范](../orchestrate-offline-audit/references/error-reasons.md)。
错误原因只写具体错误事实，建议只写处理方式；涉及的业务文件区域只列相关实际文件 basename；回调只保留原因。核销类型由 ZIP 名称的唯一已登记标记确定，AI 不得改类。ZIP 名称无法唯一分类时由编排器直接打回，不调用本 Skill 或 AI；已知类型的错件、缺件及内部文件误命名仍继续审核或材料诊断。

核销一个申报为 `其他费用` 的材料包。将 `其他` 视为例外路径，绝不能作为方便的兜底分类。

## 证据链

使用以下方向：

`资料实质 → 现有费用类型排除 → 特殊审批新增类型 → 签章促销合同 → 统一模板盖章结算单 → 按新类型提供的协议/相关文件/活动期照片/POS等证明`

提取或判断证据前，必须完整读取 [references/audit-rules.md](references/audit-rules.md)。可见事实
必须符合 [references/evidence.schema.json](references/evidence.schema.json)。规范化接入合同为
[references/scenario-manifest.json](references/scenario-manifest.json)。

## 信任边界

- AI 只读取由确定性接入绑定的视觉文件并提取可见事实。它不分类最终费用、不批准新类型、不
  计算核准金额，也不决定报销结果。
- 确定性代码负责来源角色覆盖、已知类型关键词规范化、日期和金额比较、审批门禁、错误分组、
  结论及发布。
- ZIP 名称包含 `其他` 只是路由标记。它不能证明费用属于新类型、修复缺失审批或覆盖可识别的
  现有费用类型。
- 绝不能把代表性客户名称、金额、日期、活动编号或文件名复制到可复用规则中。

## 必须得到的结果

任何申报明细属于既有费用类型时，在所选 `其他费用` 规则内记录类型不相容及具体费用事实，不自动改用另一类 Skill。
费用确属新类型但缺少明确写出新类型的可见特殊审批时，内部结果为 `需特殊审批`；客户原因只写缺失的审批证明，对应补交要求写处理方式。只有通过该门禁后，才能继续考虑其余文档
完整性控制。

本场景绝不自动给出报销通过金额。结算单金额作为申报金额保留，自动支持金额始终为零，交由
人工审批。

只能通过父级命令运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> --scenario other_expense
```
