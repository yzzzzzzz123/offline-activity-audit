# Personnel incentive deterministic rules

## Mapping

- High confidence requires a barcode visibly printed on the settlement and present in the code-read Excel.
- Without a visible barcode, normalize the two product texts, require a reliable unique candidate under the remaining one-line/one-barcode constraint, and use quantity only as corroboration. Mark the result medium confidence even when quantity is unique.
- Never take an Excel product name or barcode from AI JSON.
- Never map two settlement lines to one barcode. Leave ambiguous or absent candidates unmatched.

## Quantity and reward

- Excel SKU quantity is the sum of all valid detail rows for that barcode.
- Quantity difference is `Excel quantity - settlement quantity`.
- Line amount difference is `settlement reward - settlement quantity × unit reward` with CNY 0.01 precision.
- Quantity-supported reward is `min(nonnegative Excel quantity, nonnegative settlement quantity) × unit reward` for a valid mapping.
- Suggested approval cannot exceed actual claim, quantity-supported reward, or deduplicated transfer total when transfer evidence exists.

## Transfers and identity

- One JSON transfer row represents one business event; `occurrence_count` records multiple visible views and does not multiply the amount.
- Pair sender/receiver views only with visible business evidence and retain the pairing limitation.
- Match stores by visible identity first. Amount-only matching verifies only the amount multiset.
- `identity_visible` plus a recipient name is not enough to verify a store; require `store_name` or a supplied correspondence.
- Require a full ISO date for date verification. Weekday/clock-only screenshots remain unverified.

## Outcome

- Unmapped SKU, duplicate mapping, quantity mismatch, period mismatch, line-calculation error, or payment shortfall is high severity.
- Medium-confidence mapping, hidden recipient/store relation, incomplete date, underclaim, or a declared-total discrepancy is unresolved even if the aggregate amount matches.
- Use `pass` only when every mandatory control is resolved; `conditional_pass` for fully amount-supported but identity/explanation limitations; `partial_pass` when only part is supported; otherwise `human_review`.
