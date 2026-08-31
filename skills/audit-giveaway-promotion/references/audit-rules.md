# Extra-giveaway audit rules

## Scope and audit object

`giveaway_promotion` audits one extra-giveaway reimbursement package. It reimburses the separately
agreed value of promotional gift goods supplied in addition to normal sales. It is not the normal
shipment value and is independent from maintenance fees, special-approval other expenses, personnel
incentives, display fees, material production, or direct-operation fees.

The customer-facing object is one grouped blocking issue. The archive marker helps route the package
but never proves fee nature or eligibility.

## Required evidence

All roles below are blocking:

1. **Signed promotional contract**: exactly one visible contract executed by the dealer. It states
   the dealer, activity period, eligible purchased products, gift products, buy/gift ratio, gift
   quantity or calculable plan, gift unit value, activity budget, and reimbursement evidence.
2. **Stamped settlement**: exactly one company-form settlement bearing the dealer seal. It separates
   normal shipment value from the extra-gift claim and itemizes gift quantity, unit value,
   calculation, period, and claimed amount.
3. **System sales/delivery statement**: exactly one visible system or dealer sales/delivery statement
   with source rows and a normal-shipment total. The representative shape may be image-only; no
   spreadsheet is invented as a requirement.
4. **Store receipts**: one or more activity-period receipts. Each useful receipt shows at least one
   eligible paid product and one zero-value gift line. Paid and gift quantities must obey the contract
   ratio within that receipt; partial ratios are not added across unrelated receipts.
5. **Activity photos**: one or more photos showing the store or location, participating products, and
   the extra-giveaway execution during the contract period. A receipt is not an activity photo.

Supporting documents are allowed but cannot replace a required authority role. Missing roles become
formal grouped issues. More than one contract, settlement, or sales/delivery statement is ambiguous
and stops evidence acceptance.

## Visible-fact extraction

Return one `documents` item for every supplied visual source. Preserve the neutral intake
`role=visual_document` and classify `document_type` only from the visible title and content, never from
the camera-export filename. Do not copy values across files.

For every document preserve parties, dealer, store, title, date/period, visible seals, gift wording,
shipment amount, extra-gift amount, quantity, unit, unit value, formula, product rows, receipt number,
and limitations. Use `null`, `unclear`, or an explicit limitation where the image does not prove a
fact.

Classify product rows independently:

- `eligible_sale`: a paid product eligible to trigger a gift;
- `gift`: an extra or zero-value gift product;
- `shipment`: a normal system sales/delivery row when its gift status is not printed;
- `summary`: a printed total, which is not a detail row;
- `other`: another visible product row.

A dealer seal is visible only when the seal itself can be seen. A company name printed as text is not
a seal. A contract is executed only when a visible signature or seal is present. A store receipt gift
line must retain its printed amount, including `0.00`; do not infer zero from words such as `送`.

## Deterministic controls

- `required_materials`: all five required evidence families are present; singleton roles are unique.
- `fee_nature`: the contract and settlement visibly describe extra giveaway or gift support. Normal
  shipment, maintenance, personnel incentive, display, or another fee cannot be relabeled by the ZIP.
- `promotional_contract`: dealer execution, period, product scopes, ratio, gift plan, unit value,
  budget, and evidence requirements are visible.
- `settlement`: company structure, dealer seal, period, separate shipment and gift amounts, gift
  detail, calculation, and claim are visible.
- `sales_delivery_statement`: the statement identifies its dealer/store context, contains detail
  rows and a printed normal-shipment total, and carries a visible dealer/system confirmation mark when
  the document is not an independently verifiable system export.
- `party_alignment`: the dealer identity uniquely corresponds across contract, settlement, and
  sales/delivery statement. Store identities in the sales statement and submitted receipts must also
  be uniquely compatible when printed.
- `period_alignment`: settlement, receipts, sales/delivery date where applicable, and activity photos
  fall within the signed contract period. A filename or EXIF value is not a visible date.
- `shipment_reconciliation`: the system sales/delivery total equals the settlement's normal shipment
  amount to RMB 0.01. It is never treated as the gift claim.
- `product_correspondence`: sales/delivery and receipt paid products map uniquely to contract eligible
  products; gift rows map uniquely to contract gift products. Product names may be uniquely fuzzy and
  specification-compatible. Comparable printed product codes and valid 69 codes are strict.
- `receipt_execution`: every relied-on receipt has paid and zero-value gift lines and satisfies the
  explicit buy/gift ratio using its own quantities.
- `activity_execution`: at least one in-period activity photo visibly proves location, participating
  product, and extra-giveaway execution.
- `duplicate_evidence`: identical image bytes cannot prove separate roles or repeated execution.
- `amount_recalculation`: itemized gift quantity multiplied by the explicit gift unit value, or the
  sum of complete gift detail rows, equals the contract budget and stamped settlement claim. Never
  derive a unit value by dividing totals.

## Amount and outcome

The claim source is the dealer-stamped settlement's extra-gift amount. The normal shipment amount is
displayed only as a reconciliation fact. Use `ROUND_HALF_UP` to two decimals.

If every blocking control passes, support the settlement claim only when it equals the signed
contract budget and deterministic gift-detail recalculation. If any control fails or calculation is
not reproducible, support `0.00`, hold the entire claim, and return `human_review` with the label
`资料需补正`. A fully closed claim returns `pass` and `可核销`.

## Error grouping

Emit one issue per failed control, not one issue per field or receipt line. Every issue names the
affected business files, states the visible/deterministic cause, explains the reimbursement impact,
and requests the exact corrected or missing material. Do not expose prompts, schemas, temporary
paths, model reasoning, hashes, or other implementation terms in customer-facing rows.
