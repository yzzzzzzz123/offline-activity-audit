---
name: audit-entry-fee
description: Audit entry-fee and barcode-listing-fee packages using a signed entry contract, its product-barcode and contracted-store scope, watermarked shelf photos, and the system deduction proof required by the contract. Use only for the registered entry_fee scenario.
---

# Audit Entry Fee

Read [audit-rules.md](references/audit-rules.md) completely before extracting or judging a case. The
visual stage must conform to [evidence.schema.json](references/evidence.schema.json); normalized
provenance is in [scenario-manifest.json](references/scenario-manifest.json).

This scenario covers barcode/listing fees paid so products can enter channel stores. The signed entry
contract is the authority for parties, terminal system, product barcodes, store scope, fee per barcode,
total ceiling, payment method and post-entry evidence. A fee printed once per product barcode is not a
per-store fee merely because the same row also prints a contracted store count.

AI reads every isolated contract page and shelf photo, extracts visible facts only, and never uses a
folder name as business evidence. Deterministic Python checks the contract formula, parties and seals,
matches watermarked shelf locations to the contract store list, checks each contracted product across
the required stores, screens duplicate photo bytes, verifies the contract-required system deduction
proof, and calculates the supported amount. Missing deduction proof or an incomplete authority chain is
blocking.

Publish one error-only six-column `进场费核销` sheet in the canonical self-contained HTML. Run only
through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario entry_fee
```
