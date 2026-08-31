---
name: audit-giveaway-promotion
description: Audit extra-giveaway reimbursement packages using a signed promotional contract, stamped settlement, system sales/delivery statement, store receipts, and activity photos. Use only for the registered giveaway_promotion scenario; do not route it through maintenance, other-expense, display, personnel, or direct-operation audits.
---

# Audit Extra Giveaway Promotion

Audit one `giveaway_promotion` claim as an independent extra-giveaway reimbursement chain. The
archive marker `额外搭赠` or `搭赠` routes a structurally compatible all-visual package, but the
visible contract and settlement must establish that the claim is specifically for extra gifts.

Read [audit-rules.md](references/audit-rules.md) completely before extracting or judging a case. The
visible-fact stage must conform to [evidence.schema.json](references/evidence.schema.json). The
normalized design contract and representative ZIP provenance are in
[scenario-manifest.json](references/scenario-manifest.json).

## Evidence chain

The required chain is:

1. one dealer-executed promotional contract defining eligible purchased products, gift products,
   buy/gift ratio, activity period, gift budget, and reimbursement evidence;
2. one dealer-stamped company settlement identifying normal shipment value, extra-gift claim,
   itemized gift quantities, unit values, and calculation;
3. one system sales/delivery statement proving the normal shipment rows and total used by the
   settlement;
4. one or more store receipts within the contract period, each independently showing eligible paid
   products and zero-value gift lines that satisfy the contract ratio;
5. one or more activity-period photos proving the store, participating products, and extra-giveaway
   execution.

All supplied visual files are first bound to the neutral `visual_document` intake role because
camera-export filenames may carry no business meaning. AI reads the visible title and facts only;
deterministic validation then requires every source exactly once and uniquely binds the contract,
settlement, system statement, receipt, photo, support, or other visible document type. A duplicate
singleton business role is ambiguous and stops extraction. A missing role remains a grouped blocking
issue in the formal result.

## Trust boundary

- AI reads only the isolated submitted images or rendered PDF pages and returns visible facts. It
  never approves reimbursement, performs arithmetic, reads `input/` or prior outputs, or copies a
  value from another source.
- Deterministic Python validates source coverage and role cardinality, checks hashes, parties,
  periods, product correspondence, receipt ratios, zero-value gifts, shipment totals, gift-plan
  arithmetic, caps, conclusions, result Schema, and publication.
- Normal sales/shipment value is context, not the extra-gift claim. A matching total cannot replace a
  missing contract, settlement detail, receipt, activity photo, gift quantity, or unit value.
- Product names are unique fuzzy auxiliary evidence. When comparable sources both print a product
  code or 69 code, those identifiers must agree exactly.

## Output contract

Create one error-only six-column sheet named `额外搭赠核销` in the canonical combined workbook and the
single self-contained HTML. Each row is one grouped blocking issue with problem source, observed
fact, governing rule, conclusion, amount impact, and exact resubmission action.

The claim comes from the dealer-stamped settlement's extra-gift amount, never its normal shipment
amount. Support the full claim only when every blocking control passes and the itemized gift
calculation equals both the signed contract budget and stamped settlement claim to RMB 0.01;
otherwise support zero and hold the full claim.

Run only through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario giveaway_promotion
```
