---
name: audit-self-procured-gift-material
description: Audit customer self-procured promotional gift materials such as stools or storage baskets using a signed promotional contract, itemized invoice or receipt, stated consumer-gift rule, dealer-stamped POS plus its spreadsheet, all-store watermarked activity photos, and a company-template customer-stamped settlement. Use only for self_procured_gift_material.
---

# Audit Self-Procured Gift Material

Read [audit-rules.md](references/audit-rules.md) completely before extracting or judging a case. The
visual stage must conform to [evidence.schema.json](references/evidence.schema.json); normalized
provenance is in [scenario-manifest.json](references/scenario-manifest.json).

This scenario covers material purchased by the customer and given to consumers in a product
promotion, for example stools or storage baskets. It is distinct from terminal-designed/printed
leaflets, terminal-made display props, company-product extra giveaways, and entry/barcode fees.

AI reads isolated visual sources only. It extracts contract, settlement, receipt, payment, stamped
POS, and every embedded activity-photo fact; activity-workbook row/store hints are routing-only.
Deterministic Python reads the workbook structure, checks exact visual-source coverage and duplicate
bytes, reconciles stamped POS with the required POS electronic sheet, validates the stated gift rule,
checks every activity store's own watermark and visible promotion/material, reconciles contract,
receipt, payments and settlement, and calculates the supported amount. Missing POS electronic data or
incomplete all-store photo evidence is blocking.

Publish one error-only six-column `自采赠品物料核销` sheet in the canonical self-contained HTML. Run
only through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-id> --producer-model <executor> --scenario self_procured_gift_material
```
