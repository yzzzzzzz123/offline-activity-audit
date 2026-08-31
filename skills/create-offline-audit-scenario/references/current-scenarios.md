# Current registered scenarios

The main runtime currently supports exactly these ten scenarios, in this order. A new scenario must
remain distinguishable from all ten and must not change their authority chains implicitly.

| Order | Scenario ID | Business label | Skill | Required material shape | Authority chain | Customer object |
|---:|---|---|---|---|---|---|
| 1 | `personnel_incentive` | 人员激励 | `audit-personnel-incentive` | one sales Excel, no PDF, one identifiable settlement image, one or more transfer/payment images | sales Excel → shared product ledger → settlement lines → transfer events → claimed amount | settlement product |
| 2 | `promotional_display` | 堆头陈列 | `audit-promotional-display` | one contract PDF, one sales Excel, one or more field photos | contract core/attachment → product ledger; photo-visible text → full product ledger → bounded reference views, then resolved photo product → contract scope; Excel → contract attachment | contract store |
| 3 | `poster_material` | 展示道具（海报/物料制作） | `audit-poster-material` | one signed-contract image, one invoice/receipt image, one settlement image, one nested field-photo ZIP; no sales Excel | signed contract → invoice/receipt → stamped settlement → watermarked finished-product photos | grouped blocking error |
| 4 | `other_expense` | 其他费用 | `audit-other-expense` | outer marker `其他`; no Excel or nested ZIP; one promotional contract, one settlement, one or more support documents, optional activity/POS and special approval | visible fee substance → exclude established types → special approval for new type → signed contract → stamped settlement → type-specific support | grouped classification/approval/manual-action error |
| 5 | `maintenance_fee` | 维护费用 | `audit-maintenance-fee` | outer maintenance marker; no nested ZIP; at least one POS/settlement visual; zero or one POS Excel during intake so missing mandatory roles remain auditable | signed promotional contract → eligible POS scope; stamped POS ↔ POS Excel; POS Excel → stamped settlement; fee-specific support → contract period | grouped blocking error |
| 6 | `giveaway_promotion` | 额外搭赠 | `audit-giveaway-promotion` | outer `额外搭赠`/`搭赠` marker; image/PDF-only with no nested ZIP; generic visual basenames permitted | signed promotional contract → stamped settlement; system sales/delivery statement → settlement shipment amount; contract products/ratio → store receipts; contract → activity photos; gift detail → budget/claim | grouped blocking error |
| 7 | `price_difference_support` | 价格补差 | `audit-price-difference-support` | outer `补差` marker; one signed promotional contract, one settlement, stamped POS visuals, zero or one POS Excel, and one field-photo RAR | signed contract → activity price/support unit/quantity/budget; stamped POS ↔ POS Excel; every-store photo → contract price/period; POS quantity × contract support unit → claim | grouped blocking error |
| 8 | `pos_target_incentive` | POS达标激励（经销商） | `audit-pos-target-incentive` | outer POS incentive marker; zero or one contract, settlement and POS Excel; stamped POS and optional activity proof visuals | signed contract → approved channel/dealer/tier/rate/cap; stamped POS ↔ POS Excel; activity proof → full-reduction existence; highest reached tier → claim | grouped blocking error |
| 9 | `entry_fee` | 进场费/条码费 | `audit-entry-fee` | outer entry-fee marker; one signed entry contract/product-promotion agreement and one safely extracted shelf-photo RAR; optional explicitly named deduction proof visuals | signed contract → product/barcode/store/fee scope; watermarked photos → actual product×store shelving; system deduction proof → actual deduction; contract item fees → claim | grouped blocking error |
| 10 | `self_procured_gift_material` | 客户自采赠品物料 | `audit-self-procured-gift-material` | outer self-procured-gift marker; one signed promotional contract, one customer-stamped settlement, one purchase invoice/receipt, stamped POS visuals, exactly one legacy activity-return `.xls`, and zero or one independent modern POS spreadsheet | signed contract/gift rule → purchase ticket/payment; stamped POS ↔ modern POS spreadsheet; every extracted return photo → own watermarks/promotion/product/gift material; gift ratio × eligible POS → material sufficiency; contract/ticket/settlement arithmetic → claim | grouped blocking error |

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
schema-validated results, one formal CLI, one fixed persistent workbench, and one versioned run archive per execution. They do not share a
generic set of evidence fields, a single model prompt, a single amount formula, or a universal output
object. Multi-stage extraction is allowed when one scene needs bounded candidate retrieval or another
genuine evidence dependency.
