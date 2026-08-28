---
name: audit-other-expense
description: Audit offline reimbursement packages declared as other expenses when the fee cannot fit an established category and a specially approved new type is required. Use for ZIPs explicitly submitted as 其他费用; do not use as a fallback for recognizable赠品、陈列堆头、人员激励、海报/物料制作、维护费用、补差、搭赠、POS达标激励或进场费材料。
---

# Audit Other Expense

Audit one package declared as `其他费用`. Treat `其他` as an exception path, never as a convenient
catch-all.

## Evidence chain

Use the direction:

`资料实质 → 现有费用类型排除 → 特殊审批新增类型 → 签章促销合同 → 统一模板盖章结算单 → 按新类型提供的协议/相关文件/活动期照片/POS等证明`

Read [references/audit-rules.md](references/audit-rules.md) completely before extracting or judging
evidence. Visible facts must conform to
[references/evidence.schema.json](references/evidence.schema.json). The normalized onboarding
contract is [references/scenario-manifest.json](references/scenario-manifest.json).

## Trust boundary

- AI reads only the visual files bound by deterministic intake and extracts visible facts. It does
  not classify the final expense, approve a new type, calculate an approved amount, or decide the
  reimbursement result.
- Deterministic code owns source-role coverage, known-type keyword normalization, date and amount
  comparison, approval gating, grouped errors, conclusions, and publication.
- A ZIP name containing `其他` is only a routing marker. It cannot prove that the expense is new,
  repair a missing approval, or override a recognizable existing fee type.
- Never copy representative customer names, amounts, dates, activity numbers, or filenames into
  reusable rules.

## Required outcome

If any claimed line belongs to an established fee type, reject the `其他费用` classification and name
the existing type. If the fee is genuinely new but lacks visible special approval that names the new
type, return `需特殊审批`. Only after that gate may the remaining document-completeness controls be
considered.

This scenario never creates an automatic reimbursement recommendation. The settlement amount is
preserved as the claim, while the automatically supported amount stays zero for human approval.

Run it only through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> --scenario other_expense
```
