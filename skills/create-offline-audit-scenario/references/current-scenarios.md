# Current registered scenarios

The main runtime currently supports exactly these five scenarios, in this order. A new scenario must
remain distinguishable from all five and must not change their authority chains implicitly.

| Order | Scenario ID | Business label | Skill | Required material shape | Authority chain | Customer object |
|---:|---|---|---|---|---|---|
| 1 | `personnel_incentive` | 人员激励 | `audit-personnel-incentive` | one sales Excel, no PDF, one identifiable settlement image, one or more transfer/payment images | sales Excel → shared product ledger → settlement lines → transfer events → claimed amount | settlement product |
| 2 | `promotional_display` | 堆头陈列 | `audit-promotional-display` | one contract PDF, one sales Excel, one or more field photos | contract core/attachment → product ledger; photo-visible text → full product ledger → bounded reference views, then resolved photo product → contract scope; Excel → contract attachment | contract store |
| 3 | `poster_material` | 展示道具（海报/物料制作） | `audit-poster-material` | one signed-contract image, one invoice/receipt image, one settlement image, one nested field-photo ZIP; no sales Excel | signed contract → invoice/receipt → stamped settlement → watermarked finished-product photos | grouped blocking error |
| 4 | `other_expense` | 其他费用 | `audit-other-expense` | outer marker `其他`; no Excel or nested ZIP; one promotional contract, one settlement, one or more support documents, optional activity/POS and special approval | visible fee substance → exclude established types → special approval for new type → signed contract → stamped settlement → type-specific support | grouped classification/approval/manual-action error |
| 5 | `maintenance_fee` | 维护费用 | `audit-maintenance-fee` | outer maintenance marker; no nested ZIP; at least one POS/settlement visual; zero or one POS Excel during intake so missing mandatory roles remain auditable | signed promotional contract → eligible POS scope; stamped POS ↔ POS Excel; POS Excel → stamped settlement; fee-specific support → contract period | grouped blocking error |

## Collision rules

- Classify by material roles, cardinality, nesting, and source content before using an outer ZIP name.
- A filename marker may disambiguate a role after the structural shape is satisfied; it never makes
  an incomplete package pass. The registered maintenance-fee classifier may route an incomplete but
  structurally recognizable package so missing roles become blocking report items.
- If a proposed shape can also satisfy an existing classifier, redesign the classifier around a
  stronger role signal or stop for a user decision. Classifier order is not a valid disambiguator.
- One input ZIP may map to only one scenario, and each registered scenario may appear at most once in
  a formal run.
- When another scene is activated, update the accepted ZIP maximum, scene ordering, CLI choices,
  main-page scenario counts, and all combination tests. Do not retain a previous hard-coded ZIP limit.

## What is shared and what stays separate

All scenarios share safe intake, visible-fact extraction boundaries, deterministic judgment,
schema-validated results, one formal CLI, and one canonical self-contained HTML. They do not share a
generic set of evidence fields, a single model prompt, a single amount formula, or a universal output
object. Multi-stage extraction is allowed when one scene needs bounded candidate retrieval or another
genuine evidence dependency.
