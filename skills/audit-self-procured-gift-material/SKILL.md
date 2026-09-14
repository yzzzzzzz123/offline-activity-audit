---
name: audit-self-procured-gift-material
description: 使用已签署促销合同、分项发票或收据、明确的消费者赠送规则、经销商盖章 POS 及其电子表格、全部门店带水印活动照片和公司模板客户盖章结算单，核销凳子、收纳篮等客户自采促销赠品物料。仅用于 self_procured_gift_material。
---

# 客户自采赠品物料核销

涉及商品知识时，先读取[数据库知识规则](../../shared/product-database/audit-knowledge.md)，统一通过
`audit_core.product_database.load_product_catalog` 查询数据库；需要参考图片时，只按数据库的 `image_manifest_key` 从私有 OSS 取图；不存在本地图库入口或回退。
沿用本场景原有业务证据链，数据源调整不额外增加商品主账检查条件。

生成客户错误原因和处理方式前，必须读取并执行[统一文案规范](../orchestrate-offline-audit/references/error-reasons.md)。
错误原因只写具体错误事实，建议只写处理方式；涉及的业务文件区域只列相关实际文件 basename；回调只保留原因。核销类型由 ZIP 名称的唯一已登记标记确定，AI 不得改类。ZIP 名称无法唯一分类时由编排器直接打回，不调用本 Skill 或 AI；已知类型的错件、缺件及内部文件误命名仍继续审核或材料诊断。

提取或判断任何案例前，必须完整读取 [audit-rules.md](references/audit-rules.md)。视觉阶段必须
符合 [evidence.schema.json](references/evidence.schema.json)；规范化来源定义位于
[scenario-manifest.json](references/scenario-manifest.json)。

本场景覆盖由客户自行采购并在商品促销中赠送消费者的物料，例如凳子或收纳篮。它不同于终端
设计/印刷的宣传单页、终端制作的陈列道具、公司商品额外搭赠和进场费/条码费。

AI 只读取隔离的视觉来源。它提取合同、结算单、票据、付款记录、盖章 POS 和每张内嵌活动照片
的事实；活动工作簿中的行/门店提示只用于路由。确定性 Python 读取工作簿结构，检查准确视觉
来源覆盖和重复字节，将盖章 POS 与必需 POS 电子表对应，验证已写明的赠送规则，核查每家活动
门店自身水印及可见促销/物料，核对合同、票据、付款和结算单，并计算支持金额。缺少 POS 电子
数据或全门店照片证据不完整均为阻断项。

保留内部六列 `自采赠品物料核销` 结果用于验证；规范自包含 HTML 分别显示错误原因与处理方式，业务文件区域只列相关实际文件名。只能通过父级命令运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario self_procured_gift_material
```
