# Personnel incentive deterministic rules

## Mapping

- Before settlement mapping, every aggregated Excel SKU must pass the formal product knowledge base: the source 69 code is a valid EAN-13 and matches exactly, then the source product name matches exactly or uniquely fuzzily only within that same-code candidate set.
- The personnel Excel does not supply a product code. Return the selected knowledge product code for traceability, but do not invent or require an Excel code field.
- If the exact 69 code has no catalog product, the name conflicts, or multiple same-code products cannot be uniquely distinguished by name, keep the diagnostic candidate set but fail the knowledge gate. The affected SKU supports zero reward until the Excel or knowledge base is corrected.
- High confidence requires a barcode visibly printed on the settlement and present in the code-read Excel.
- Without a visible barcode, normalize the settlement text against the knowledge-backed Excel/catalog names, require a reliable unique candidate under the remaining one-line/one-barcode constraint, and use quantity only as corroboration. Mark the result medium confidence even when quantity is unique.
- Never take an Excel product name or barcode from AI JSON.
- Never map two settlement lines to one barcode. Leave ambiguous or absent candidates unmatched.

## Quantity and reward

- Excel SKU quantity is the sum of all valid detail rows for that barcode.
- Quantity difference is `Excel quantity - settlement quantity`.
- Line amount difference is `settlement reward - settlement quantity × unit reward` with CNY 0.01 precision.
- Quantity-supported reward is `min(nonnegative Excel quantity, nonnegative settlement quantity) × unit reward` only for a valid mapping whose Excel SKU also passed the product-knowledge gate.
- Suggested approval cannot exceed actual claim, quantity-supported reward, or deduplicated transfer total when transfer evidence exists.

## Transfers and identity

- One JSON transfer row represents one business event; `occurrence_count` records multiple visible views and does not multiply the amount.
- Pair sender/receiver views only with visible business evidence and retain the pairing limitation.
- Match stores by visible identity first. Amount-only matching verifies only the amount multiset.
- `identity_visible` plus a recipient name is not enough to verify a store; require `store_name` or a supplied correspondence.
- Require a full ISO date for date verification. Weekday/clock-only screenshots remain unverified.

## Outcome

- Knowledge-gate failure, unmapped SKU, duplicate mapping, quantity mismatch, period mismatch, line-calculation error, or payment shortfall is high severity.
- Medium-confidence mapping, hidden recipient/store relation, incomplete date, underclaim, or a declared-total discrepancy is unresolved even if the aggregate amount matches.
- Use `pass` only when every mandatory control is resolved; `conditional_pass` for fully amount-supported but identity/explanation limitations; `partial_pass` when only part is supported; otherwise `human_review`.

## Human-facing row name

- When a settlement line uniquely resolves to a knowledge-base product, use that product's complete authoritative name in the `结算第N行` title.
- Keep the settlement image's original recognized product text in the visual-evidence column so the reader can still see what the submitted material actually said.
- If the knowledge product is not uniquely resolved, do not borrow an Excel or candidate name as authority; retain the recognized settlement text in the title and show the failed match separately.
