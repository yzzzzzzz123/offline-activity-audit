# Poster/material-production deterministic rules

## Evidence order

Use the signed contract as the business baseline. Audit in this order:

1. signed contract completeness and referenced attachments;
2. invoice or receipt title, date, item detail, and amount;
3. stamped settlement form, line items, period, and amount;
4. finished-product photos: watermark, period, location, content, placement, dimensions, and quantity coverage;
5. cross-document party, item, date, quantity, unit-price, any explicitly listed subtotal, and total reconciliation;
6. supported amount and error-only output.

No later source may rewrite an earlier source. A settlement total cannot fill missing invoice detail; a photo cannot create a missing contract store list; one sample photo cannot prove unsubmitted units.

## Contract

- Require a visible customer/distributor party, activity period, material item, quantity, price or amount basis, and customer signature/seal.
- Preserve each contracted item separately. Normalize only synonymous physical forms such as `台上架` and `台面展示架`; do not merge a lightbox with a counter display.
- If the contract says `见附件`, the referenced store list, quotation, design/specification, or other attachment is mandatory. A signed cover page does not prove the missing attachment.
- Contract dates, quantities, unit prices, subtotals, and totals are exact facts.

## Invoice or receipt

- Either an invoice or receipt is allowed.
- The title/payer may omit harmless legal suffixes such as `（个体工商户）` when the remaining name corresponds uniquely to the contract party.
- Require a legible issue date and total amount.
- The expense description must identify poster/material production. When the contract has more than one distinct item or unit basis, require an itemized ticket or attached detail containing description, quantity, unit price, and subtotal for each contracted item.
- A single generic line `物料制作` is not itemized evidence for multiple contracted materials even when its total equals the contract.
- A user-verified permanent-standard amount regression may replace only contract budget, invoice/receipt total, and settlement total when the complete ordered basename plus original-byte SHA-256 sequence of all three visual documents exactly matches `document-fact-calibrations.json`. Any changed, renamed, added, removed, or reordered document disables this calibration and leaves the new visual extraction in force. The registry cannot rewrite party, date, detail, seal, attachment, photo, or any other field; it is deterministic runtime data, never prompt context or customer-facing evidence.

## Settlement

- Recognize the company settlement form from its title and required business fields; do not infer a template match from a blank or generic page.
- Require customer/project, activity period, each material quantity and unit price, total amount, settlement date, and customer seal.
- Compare quantities, unit prices, any explicitly listed subtotals, and total exactly with the contract. A company settlement template may state quantity and unit price for each item plus one overall total without printing each computed line subtotal; that layout is not a defect by itself. Compare the total exactly with the invoice/receipt.

## Finished-product photos

For every relied-on photo, independently extract:

- exact basename;
- complete visible date and shooting time;
- visible location text identifying an address, store, or unique branch;
- material type and count actually visible;
- finished content and physical display position;
- visible dimension text, ruler, scale, or another reliable physical-size basis.

Rules:

- Date, time, and location must appear in the photo watermark. Filename or EXIF alone does not satisfy the visible-watermark requirement.
- Every visible date must fall inside the contract activity period.
- Store/branch wording may be uniquely compatible rather than character-for-character identical. A generic chain name without a branch or address is insufficient when multiple stores exist.
- Multiple photos may jointly prove content, placement, and dimensions, but the final set must cover all three.
- Count only independently visible finished units. Do not treat product packs placed on one display as multiple contracted displays.
- Quantity support cannot exceed the units and unique locations actually evidenced. Do not extrapolate from representative samples unless the contract explicitly authorizes sample acceptance.
- When a contract claims distribution across a store list, exact store coverage is unverifiable until that referenced list is present.
- A user-accepted permanent-standard quantity regression may replace only the declared aggregate material count and contributing-photo count when the complete ordered basename plus original-byte SHA-256 sequence exactly matches `field-photo-quantity-calibrations.json`. Any changed, renamed, added, removed, or reordered photo disables the calibration and leaves the new visual extraction in force. This registry is deterministic runtime data, never prompt context or customer-facing evidence.

## Grouping errors

Customer output groups defects once per source family:

- `合同材料不完整` for a missing referenced attachment or missing seal;
- `票据费用明细不完整` for missing item/quantity/unit-price/subtotal detail;
- `现场照片执行证据不完整` for watermark, period, location, quantity, content, placement, or dimension gaps;
- `结算单不合格` for template/field/seal defects;
- `金额或项目不一致` for exact cross-document conflicts.

One grouped issue may list several failed subcontrols. Do not create ten identical cards merely because ten photos share the same missing dimension evidence.

## Amount

Automatic support requires every mandatory control to pass. When any blocking issue remains:

- suggested approved amount is zero;
- temporarily held amount equals the claim amount;
- conclusion is `human_review`;
- resubmission names the exact missing source or visible fact.

If every control passes, support the lower nonnegative exact amount among the contract cap, settlement claim, and invoice/receipt total. Never infer a unit price by dividing a total.
