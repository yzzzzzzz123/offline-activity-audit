---
name: audit-pos-target-incentive
description: Audit dealer-recipient POS target incentives calculated from qualifying POS sales and signed-contract threshold rates, including stamped POS, the corresponding spreadsheet, a stamped company-template settlement, activity-existence proof for full-reduction promotions, and a signed promotional contract. Use only for pos_target_incentive.
---

# Audit POS Target Incentive

Read [audit-rules.md](references/audit-rules.md) completely before extracting or judging a case. The
visible stage must conform to [evidence.schema.json](references/evidence.schema.json); normalized
provenance is in [scenario-manifest.json](references/scenario-manifest.json).

The incentive recipient is the dealer, not the terminal store. The signed promotional contract must
establish approved channel eligibility, POS scope, activity period, target tiers, rate, cap, recipient,
and activity mechanic. Dealer-stamped POS and its electronic workbook establish the sales base. For a
full-reduction promotion, dated/addressed/timed field photos, receipts, or another source must prove
the activity actually existed. A dealer-stamped company-template settlement supplies the claim.

AI reads isolated visual sources only. Deterministic Python reads the workbook, checks source
coverage and POS correspondence, selects the highest contract tier actually reached, applies its rate
and cap, compares the settlement, and sets the supported amount. Missing authority or activity proof
is a blocking report issue, not a reason to borrow a rule from the settlement.

Publish one error-only six-column `POS达标激励核销` sheet in the canonical self-contained HTML. Run
only through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario pos_target_incentive
```
