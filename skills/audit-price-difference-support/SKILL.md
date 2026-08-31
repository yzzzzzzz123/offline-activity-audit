---
name: audit-price-difference-support
description: Audit price-difference reimbursement packages using dealer-stamped POS, the corresponding POS spreadsheet, a company-template stamped settlement, all-store activity-price photos, and a signed promotional contract. Use only for the registered price_difference_support scenario.
---

# Audit Price Difference Support

Audit one price-difference claim as a closed evidence chain. Read
[audit-rules.md](references/audit-rules.md) completely before extracting or judging a case. Visible
facts must conform to [evidence.schema.json](references/evidence.schema.json). The normalized scenario
contract and representative-package provenance are in
[scenario-manifest.json](references/scenario-manifest.json).

The required chain is dealer-stamped POS data, its electronic spreadsheet, a dealer-stamped
company-template settlement, every activity store's dated/addressed/timed field photo clearly showing
the activity price, and an executed promotional contract. Missing material remains a blocking report
issue when the outer package is otherwise unambiguously a price-difference claim.

AI reads only isolated visual sources and extracts visible facts. Deterministic Python alone reads the
spreadsheet, checks exact source coverage, reconciles stores/products/periods/prices, recalculates the
contractual per-unit support, applies quantity and budget ceilings, and sets the supported amount. The
retail price reduction is not automatically the reimbursement rate: use only the signed contract's
explicit support unit amount.

Create one error-only six-column sheet named `价格补差核销` in the canonical workbook and single
self-contained HTML. Any blocking failure holds the entire dealer-stamped settlement claim; never
extrapolate submitted photos to stores without their own valid activity-period photo.

Run only through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario price_difference_support
```
