---
name: audit-personnel-incentive
description: Audit personnel-incentive claims from one sales Excel, one settlement image, and transfer/red-packet screenshots. Use whenever sales products must first pass the repository product-knowledge gate by exact 69 code plus exact or uniquely fuzzy product name, each settlement product must then be deterministically mapped, quantities and rewards reconciled line by line, transfer views deduplicated, claim totals compared, and identity/date limitations shown in the fixed personnel audit worksheet.
---

# Audit Personnel Incentive

Build the chain `Excel sales rows → product knowledge base → settlement lines → transfer events → claimed amount`. Amount agreement alone does not validate product identity, recipient identity, store correspondence, or transfer date.

## AI extraction

Read [evidence.schema.json](references/evidence.schema.json) and [audit-rules.md](references/audit-rules.md) before extracting.

Give the AI only the settlement image and transfer screenshots. It must return schema version `2.0` and may extract only visible facts:

- settlement customer, activity dates, declared totals, actual claim, seal/date visibility, and every product line in printed order;
- `barcode_visible` only when the settlement itself visibly prints a legible barcode;
- each distinct transfer event once, with amount, source files, occurrence count, visible recipient/store/date fields, and the basis for pairing mirrored chat bubbles;
- limitations and uncertain text.

The AI must not receive or read the sales Excel, infer a barcode from product/quantity, calculate approval, or mark the case passed.

## Deterministic audit

Python reads every valid sales-Excel detail row and preserves source row, period, store, barcode, original product name, quantity, unit price, and amount cell. Before comparing the settlement, aggregate each Excel barcode and reconcile it with the validated repository product knowledge base:

- the Excel 69 code must be a valid EAN-13 and exactly equal a catalog 69 code;
- only products under that exact 69 code are eligible name candidates;
- the Excel product name may match a catalog-controlled name/alias exactly or by one strong, unique fuzzy match;
- the personnel Excel has no product-code field, so product code is returned from the selected knowledge product and is not required as an input key;
- an absent/unregistered/invalid 69 code, incompatible name, or same-code ambiguity fails the knowledge gate and contributes no supported reward until corrected.

For every settlement line in order:

1. Use a settlement-visible barcode as a high-confidence direct mapping only when it exists in the code-read Excel.
2. Otherwise compare normalized product identity and quantity against unused, knowledge-reconciled Excel SKUs under a one-line/one-barcode constraint. A deterministic text/quantity match remains medium confidence because the source settlement did not show the barcode.
3. Sum all Excel rows for the selected barcode and keep per-store quantities/source rows.
4. Compare Excel quantity with settlement quantity.
5. Calculate `settlement quantity × reward unit price` and compare it with the settlement line reward.
6. Calculate the supported line amount from the lower nonnegative quantity only for a valid Excel mapping whose knowledge status is `matched` or `fuzzy_matched`.

After all lines, compare calculated reward, settlement line total, declared total, deduplicated transfer total, and actual claim. Deduplicate only when visible evidence supports two views of one business transfer; repeated equal amounts remain separate events unless pairing evidence exists.

Recipient/store/date rules are independent:

- a visible chat contact is not automatically a store mapping;
- an amount-only multiset match verifies amounts only;
- a weekday or clock is not a complete transaction date;
- do not write identity/date verified unless recipient, store correspondence, and complete date are all visible for every required transfer.

## Worksheet contract

Generate one product row per settlement line, then `合计`, `实际申请金额`, and `收款人与日期` rows. Preserve settlement order regardless of Excel order.

- Use the uniquely selected knowledge-base product's complete authoritative name as each `结算第N行` row title. Keep the settlement image's original recognized product text in the visual-evidence column; only fall back to that recognized text as the title when no knowledge product was uniquely established.
- Show the original Excel product/barcode/quantity, selected knowledge-base product code/name/69 code, exact 69-code result, exact-or-fuzzy product-name result, calculated reward, vision-AI product/quantity/reward, and both differences.
- Show only one overall label per product: `置信度：高`, `置信度：中`, or `置信度：低`. Exact, fuzzy, or mismatch wording belongs only in the short field-by-field explanation and is not repeated as an overall match-result label. A permitted unique fuzzy knowledge-name match is medium confidence, while a settlement line without a visible barcode remains medium confidence even when its amount matches.
- Use a concrete supplement statement for an unmapped product, quantity/reward difference, claim difference, missing recipient/store mapping, or incomplete date.
- Calculate every amount in Python and write values only; never use workbook formulas.
- Freeze only the two title rows and row 3 header (`A4`); product and reconciliation rows must remain scrollable.

Formal runs occur only through the parent command:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```
