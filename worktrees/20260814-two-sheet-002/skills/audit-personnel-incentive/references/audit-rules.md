# Personnel incentive audit rules

## Identity and mapping

- Prefer barcode equality. If the settlement has no barcode, require a unique full-product mapping and record the selected barcode.
- Product-family words alone are insufficient when multiple sales SKUs share that family. A missing barcode may be completed at medium confidence only when the normalized settlement product core is contained in the Excel product name, the settlement quantity exactly equals the deterministic Excel SKU aggregate, and that barcode is unique across all remaining unmapped settlement lines and Excel SKUs. Record that the source settlement did not print the barcode.
- One settlement line maps to one barcode, and one barcode maps to at most one settlement line.
- Preserve the settlement line order even when the Excel SKU order differs.

## Quantity and amount controls

- Excel SKU quantity is the sum of all valid detail rows for the mapped barcode.
- A quantity line passes only when `Excel quantity - settlement quantity = 0`.
- A reward line passes only when `settlement quantity × unit reward - settlement reward amount = 0` within CNY 0.01.
- Quantity-supported reward equals `min(nonnegative Excel quantity, nonnegative settlement quantity) × unit reward` for each valid mapping.
- Suggested approval cannot exceed the actual claimed amount, quantity-supported reward, or deduplicated transfer total when payment evidence is mandatory.

## Transfer controls

- Collapse a sent/received pair only when evidence identifies it as two views of one transfer. Retain the occurrence count.
- Match by visible store/recipient identity first. Amount-only matching verifies only the amount multiset.
- Duplicate expected amounts require count-based matching and remain identity-ambiguous when names are absent.

## Severity and conclusion

- High: unmapped/duplicate SKU, quantity mismatch, period mismatch, missing mandatory quantity, or overclaim above supported evidence.
- Medium: settlement declared total differs from line recalculation, actual claim differs from line total, transfer date/store identity is hidden, or mapping confidence is not high.
- `pass`: all mandatory controls pass and no unresolved exception exists.
- `conditional_pass`: quantities and supported amount cover the claim, but a non-overclaim explanation or secondary identity evidence is missing.
- `partial_pass`: only part of the claim is supported.
- `human_review`: evidence is too ambiguous to calculate a reliable supported amount.
