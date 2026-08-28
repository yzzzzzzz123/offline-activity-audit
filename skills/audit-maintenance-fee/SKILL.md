---
name: audit-maintenance-fee
description: Audit maintenance-fee reimbursement packages that require dealer-stamped POS data, the corresponding POS spreadsheet, a company-template stamped settlement, a signed promotional contract, and fee-specific support. Use only for the registered maintenance_fee scenario; do not use for direct-operation, personnel-incentive, or other-expense packages.
---

# Audit Maintenance Fee

Audit one maintenance-fee claim as a closed evidence chain. The outer archive marker helps route the
package but never proves that the visible documents actually describe maintenance fees.

Read [audit-rules.md](references/audit-rules.md) completely before extracting or judging a case. The
visible-fact stage must conform to [evidence.schema.json](references/evidence.schema.json). The
normalized scenario contract and representative-package provenance are in
[scenario-manifest.json](references/scenario-manifest.json).

## Evidence chain

The required chain is:

1. dealer-stamped POS data;
2. the corresponding POS electronic spreadsheet;
3. fee-specific contracts, agreements, related documents, and activity-period photos when the
   agreed activity requires on-site execution;
4. a dealer-stamped settlement using the company template and showing itemized fees, the calculation
   method, and the claimed amount;
5. a signed promotional contract that establishes the activity scope, period, eligible POS scope,
   calculation rule, rate, and any amount ceiling.

Missing material is a blocking audit fact, not an intake crash, when the archive is otherwise
unambiguously marked and shaped as a maintenance-fee package. Every supplied visual source must be
bound exactly once and returned exactly once by the extraction stage.

## Trust boundary

- AI reads only the isolated visual files and extracts visible facts. It does not see the POS
  spreadsheet, calculate an amount, classify the final claim, or repair one source from another.
- Deterministic Python reads the POS spreadsheet, checks source coverage, recomputes totals and the
  contractual formula, compares parties/periods/fee nature, groups blocking issues, sets the
  supported amount, validates the result Schema, and publishes the report.
- A filename, archive label, matching total, or settlement statement cannot replace a missing
  contract, spreadsheet, dealer seal, calculation basis, or execution record.
- Preserve the original `other_expense` and `personnel_incentive` authority chains. A document that
  visibly states another fee nature is reported as a maintenance-fee classification conflict; it is
  not silently routed into that other scenario.

## Output contract

Create one error-only six-column sheet named `维护费用核销` inside the canonical combined workbook and
the single self-contained HTML. Each row is one grouped blocking issue with source, observed fact,
rule, conclusion, amount impact, and exact resubmission action. If every blocking control passes,
support the dealer-stamped settlement claim only when it equals the amount deterministically
recomputed from the signed contract and POS spreadsheet to the nearest cent.

Run this scenario only through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario maintenance_fee
```
