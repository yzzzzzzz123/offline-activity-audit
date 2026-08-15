# Promotional display audit rules

## Mandatory store controls

A store supports its contracted fee only when all are true:

- at least one submitted photo exists;
- visible date is inside the contract activity period;
- visible store/location is `exact` or explicitly `compatible` with the contract store;
- the required display standard is visible and marked `pass`;
- no exact or unresolved possible duplicate invalidates the evidence.

`filename_only`, `mismatch`, `unverifiable`, `fail`, and `uncertain` do not support the fee without supplemental evidence.

## Store mapping

- `exact`: the visible store name or authoritative address matches the contract.
- `compatible`: a documented abbreviation, address, or rename maps to the same store.
- `mismatch`: visible evidence points to another store or address.
- `filename_only`: the filename suggests the store but the photo has no independent visible identity.

## Sales support

- Verify Excel customer, period, SKU count, quantity total, and amount total directly.
- Distributor-level sales can corroborate that products moved during the period but cannot prove execution at each contracted store.
- Missing store-level sales is a limitation, not an automatic rejection when the contract only requires photo proof; surface it for policy review.

## Severity and conclusion

- High: missing photo, period mismatch/unverifiable, store mismatch/filename-only, failed/uncertain display standard, or exact duplicate reuse.
- Medium: compatible mapping lacks a supplied mapping document, EXIF/GPS is absent, or sales is aggregate-only.
- `pass`: every contracted store supports its fee.
- `partial_pass`: at least one store supports its fee and at least one requires supplement.
- `human_review`: contract rules or the store list cannot be extracted reliably.
