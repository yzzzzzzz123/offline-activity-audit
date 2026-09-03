---
name: audit-promotional-display
description: Audit promotional or stack-display claims from a contract PDF, distributor sales Excel, field photos, and the maintained multimodal product knowledge base. Use when contract terms, sales identities, photo products, internal correspondence, display evidence, promotion, photo reuse, and supported amount must close into one human-readable six-column worksheet while full evidence remains in JSON.
---

# Audit Promotional Display

Create one audit row per contract merchant/store. A filename, total sales amount, generic brand scope, or similar package never proves that a store performed the contracted activity.

## Required reading

Read these files completely before extracting or judging:

- [audit-rules.md](references/audit-rules.md)
- [evidence.schema.json](references/evidence.schema.json)
- [contract-product-cells.schema.json](references/contract-product-cells.schema.json) for the mandatory focused second pass over dense attachment product cells
- the shared [product-rag.md](../../shared/canban-product-multimodal-knowledge-base/references/product-rag.md) for field-photo product identity

The runtime validates the project-level shared
[product-rag.json](../../shared/canban-product-multimodal-knowledge-base/references/product-rag.json),
uses only text visible in each submitted field photo to retrieve a small candidate set from the full
validated catalog, and supplies those candidates' registered multi-view images. This Skill is a
consumer of that identity ledger; it does not own a private catalog or product-image copy.

## Trust boundary

Vision AI may read:

- every contract PDF page at original resolution;
- submitted field photos;
- bounded repository-owned product-reference images during the final photo pass.

Vision AI must not read the sales Excel, prior outputs, caches, acceptance workbooks, or unrelated repository files. It extracts visible facts only and must not calculate a supported amount or make the final pass decision.

Product-reference images establish product identity only. They cannot establish the submitted store, date, location, display, promotion, price, photo originality, or amount, and they must never be returned as submitted `photo_files`.

## Visual extraction

Return evidence schema version `2.5`.

For the contract, extract only explicit core terms:

- contracting parties and the customer/distributor party;
- activity budget, execution period, activity content, the exact visible reimbursement/settlement method, display standard, and claimed amount;
- fee basis: `per_store`, `per_stack`, `total_only`, or `unclear`;
- total stack count and each listed store's stack count only when explicit;
- watermark visibility for informational trace only, and seal visibility as a mandatory control;
- every merchant/store in printed order;
- specific product and promotion requirements only when the core terms actually impose them.

A brand scope, whole series, broad category, activity wording, or appended product/sales table is not a specific core-contract SKU condition. If the core contract does not contain a checkable product code, sufficiently specific product name, or complete valid 69 code, set core-product knowledge to not applicable. Attachment rows still retain their own contract-side product identities and are reconciled separately.

Treat a printed sales-detail attachment as a separate transcription source under
`contract.sales_attachment`, never as a contract product or promotion requirement:

- without a row-level attachment, return `present=false`, empty `source_pages` and `records`, and null totals;
- with an attachment, return `present=true`, list distinct PDF page numbers in ascending order, and transcribe every row in printed order with consecutive `line_no` values starting at 1;
- for each row preserve `source_page`, customer name, business date, product code, product name, 69 code, unit, quantity, retail price, and row total amount;
- use null for any illegible business value, but always provide integer `line_no` and `source_page` values;
- before returning a null product name from a dense attachment table, crop or zoom that exact cell and inspect it again at original resolution. If the same row's product code, 69 code, quantity, price, or amount is legible, a null name requires an explicit second visual pass; do not treat the first OCR miss as a blank PDF cell;
- return a 69 code only when it is a complete valid EAN-13; all returned numeric values must be visibly printed and non-negative;
- copy `total_quantity` and `total_amount` only when the attachment prints those grand totals. Never sum detail rows, multiply quantity by price, infer a missing total, or turn a printed total line into another detail record.

Preserve attachment limitations in `extraction_notes`. Do not fill an attachment field from the separate distributor sales Excel or another file.

After the full contract pass, if `contract.sales_attachment.records` is nonempty, run one focused
second visual pass against the original-resolution PDF pages using
`contract-product-cells.schema.json`. Return every attachment row in the same line/page order and
independently re-read only its product-code, product-name, and 69-code cells. First-pass quantity,
price, and amount may locate the row but may not infer any target value; the sales Excel remains
absent. Before this pass, deterministically crop broad scan whitespace, derive all four orientations,
and attach the two landscape reading-direction alternatives plus overlapping row bands. When a red
seal crosses the table, also attach a same-pixel black-print view that suppresses saturated seal color,
crops the product-code/name/69-code columns, and enlarges the print. The model must choose the orientation with
upright print, trace each row horizontally from its same-PDF locator, confirm all three target cells in the
matching band, and compare seal-covered print across the color and black-print bands. Orientation and
color-separated alternatives are duplicate views of one source page, not additional evidence or rows.
For a dense row whose transaction
numerics are legible, a missing code, name, or 69 code in this focused pass is an extraction failure and must be
retried. Every nonempty 69 code must be a valid EAN-13. Apply only nonempty focused readings, retain genuine nulls as limitations, and validate the
full contract evidence again before auditing.

For every contract store, return exactly one photo review, including an empty review when no photo can be assigned. Preserve submitted basenames exactly. Extract:

- useful visible text, complete date, location, and their visual basis;
- whether the photo proves `1平米堆头`, `4纵陈列`, both, neither, or is unclear;
- a reliable vertical-facing count and corresponding left-to-right basis;
- explicit promotion signals separately from ordinary prices;
- risks and useful supplemental material.

After the complete photo/product pass, run a mandatory focused display-standard pass with
[display-standard-review.schema.json](references/display-standard-review.schema.json). Give it only the submitted field photos and the already
validated store/photo routing; do not give it product-reference images, Excel, contract product text,
or the earlier display conclusion. It must preserve the route and independently replace only
`display_observation`. This focused pass distinguishes an independent edge stack from the exposed side
panel of an already-counted package. Different submitted-brand SKUs, bundles, or package formats may
jointly form the four columns; four copies of one SKU are not required, while visibly unrelated brands
do not count. Evaluate multiple photos independently: any one routed photo may prove four columns, but
never sum partial counts across photos. A flush run of narrow side faces beside three front boxes stays
three unless seams, offsets, or an independent package face proves another stack. The complete photo
evidence is then revalidated.

After the focused model result, load
[display-standard-calibrations.json](references/display-standard-calibrations.json). A calibration is
user-accepted regression knowledge, not filename inference: apply it only when the immutable contract
store name and every routed photo's ordered SHA-256 match the registry exactly. Any byte change, added
or removed photo, reordered route, or different store disables the calibration and leaves the fresh
visual result in force. Record applied calibration IDs in extraction notes and revalidate the replaced
observation. Never use a calibration for a merely similar image.

When the contract store and a legible watermark location fully correspond, pass the location directly without a map call or a confidence issue. Whenever the two visible names differ, retain both values and invoke the fixed Baidu Maps Streamable HTTP MCP resolver through `map_search_places`; read its server-side AK from `BAIDU_MAPS_API_KEY` in the current process or, on Windows, the current user's environment configuration, never from a repository file or command-line argument, never expose the authenticated URL, and store returned coordinates as `BD09LL`. This user-level configuration lets routine audits run directly without another interactive terminal. A selected same POI, a verified mall/store parent-child relation, or selected POIs within 100 metres has location `confidence=high`, becomes `compatible`, and passes. Search results do not have to be independently unique: preserve any credibly unique side, require every unresolved side's selected candidate to meet the internal 0.60 name-relevance threshold, and require both sides to meet it when neither side is credibly unique; select the best pair only when it has one of those passing spatial relationships. A distant result, the 100-to-300-metre gray zone, no credible candidate pair, unavailable resolver, or missing comparable coordinates has `confidence=low`, becomes `location_unverified`, holds automatic settlement, and is shown as `门店地点低置信度` rather than an automatic wrong-watermark finding. For a uniquely resolved distant pair, preserve the distance and request either authoritative same/nearby evidence or a corrected-location photo. Keep internal relevance scores out of customer-facing output. Keep legacy `mismatch` readable but do not emit it for new determinations. If MCP is unavailable or inconclusive, the schema-validated [location-resolution-registry.json](references/location-resolution-registry.json) may provide a verified relationship; preserve its source labels, checked dates, address facts, and basis in the audit result and ledger. MCP/registry disagreement is low confidence and always goes to manual review. Use missing/unreadable-watermark wording only when no reliable location is visible, and never use a filename as location proof.

Resolve field-product identity independently from the contract and sales files, in this order:

1. transcribe useful packaging text, including partial name, specification, flavor/variant, bundle notation, product code, and complete barcode when visible;
2. use only that same-photo text to retrieve a small candidate set from the full validated knowledge base;
3. correspond the visible text to catalog-controlled names, aliases, specifications, variants, and packaging text, then compare those candidates' reference views with the submitted packaging;
4. return only a uniquely supported exact identity, a fuzzy identity, or no identity.

The field photo does not need to show a complete product name or a 69 code. A visible registered short code that belongs to one catalog product, such as `SP-1`, or another unique combination of partial name/specification/variant/bundle text may establish the text correspondence even when spacing, case, or the hyphen differs. A combination such as `3+2`, `420g`, and `量贩装` retrieves the registered `3+2` bundle. An exact result requires the field packaging to be broadly visually compatible with at least one registered multi-view reference, not pixel-identical: ordinary differences in angle, distance, lighting, shelf occlusion, or package pose are acceptable when the main color blocks, layout, bundle structure, and recognizable packaging features are alike and no visible feature conflicts. A short code shared by several products, such as the current `SP-4`, stays fuzzy until other visible text and packaging jointly disambiguate it. Product names use fuzzy compatibility throughout the audit and never require character-for-character equality. A supplied 69 code must exactly equal the valid catalog EAN-13. Source-local business product codes are preserved and compared strictly between the contract attachment and standalone sales Excel. For contract product → knowledge base, that business code must also exactly equal the selected catalog product's `product_code` or one of its `product_code_aliases`; do not require it to equal an unrelated `CP-...` main code when an exact alias exists, but fail when neither the main code nor an alias contains it. Product code and 69 code must jointly hit the same catalog product. Once those strict identifiers agree, differing product-name text is fuzzy-compatible auxiliary evidence and never becomes a separate mismatch error. If a product name is illegible or absent but strict identifiers and the independent transaction fields uniquely locate the row, record the name as unavailable auxiliary evidence and do not fail, recount, or request resubmission for that name. If the same strict code-plus-69 pair maps to multiple catalog variants, re-read the contract name cell and use a unique fuzzy-compatible name to choose among them; a visible name that the first OCR pass missed is an extraction defect, not a substitute for the strict code check. A packaging/code alias may be a fuzzy-name anchor when written in the product-description field, but never fills or rewrites a strict product-code field.

## Deterministic audit order

Python owns the decision in this exact order:

1. Treat the contract PDF as the main audit file. Audit exactly six mandatory visible controls separately: contracting party, activity budget, execution period, activity content, reimbursement/settlement method, and seal. Preserve watermark visibility only as informational extraction; it never affects status, confidence, amount, or resubmission.
2. Preserve the contract sales attachment by PDF page and printed line. Reconcile any concrete core-contract products and every attachment-row product to the validated knowledge base. Core products and attachment products remain semantically separate; an attachment row never creates a core product or promotion condition.
3. Resolve field-photo products independently: photo-visible text retrieves candidates from the full validated knowledge base, then candidate reference images are used only for packaging comparison. Separately compare the resolved product with contract product scope, and compare each photo to the contract merchant/location, period/date, activity/display/promotion requirements, and photo-reuse controls. A fuzzy photo identity remains unresolved; neither contract product wording nor sales Excel may select or upgrade it.
4. Read sales Excel cells directly and preserve Excel row, customer name, business date, product code, product name, 69 code, unit, quantity, retail price, and total amount. Check each row's own `quantity × retail price = total amount` and the printed totals deterministically.
5. Reconcile contract attachment → standalone sales Excel directly, contract row first, across eight displayed fields: customer name, business date, product code, product name, 69 code, quantity, retail price, and total amount. Preserve PDF page, attachment line, Excel row, and both sources' nine raw values. Product codes, 69 codes, and numeric fields are strict. Product name is a fuzzy auxiliary field: exact equality is only the score-1 special case, and a different, absent, or illegible name never owns pass/fail, problem counts, confidence, or resubmission once the row is uniquely located by strict identifiers and independent transaction facts. Unit remains visible but never participates in pairing, field results, pass/fail, confidence, problem counts, amounts, or resubmission. Do not rewrite the contract from Excel values.
6. Never reconcile standalone sales Excel to the knowledge base. Never reconcile field photos to standalone sales Excel, and never use Excel to select or upgrade a photo identity. If the contract has no row-level sales attachment, mark the row-level comparison unverifiable, list every unmatched Excel row, require the complete contract attachment, and block automatic reimbursement.
7. Calculate amount only from an explicit unit basis after all three independent gates pass: contract core/product identity, photo → knowledge base followed by photo product → contract scope, and Excel → contract attachment:
   - `per_store`: unit fee × passed stores;
   - `per_stack`: unit fee × explicit passed stack count;
   - `total_only` or `unclear`: never divide the total automatically; require manual confirmation;
   - cap the recommendation by the claim and by the explicit activity budget when present.
8. Validate the structured result before writing a workbook. Write values only, never formulas.

## Display and promotion

The display standard is an OR condition: clearly proving either `1平米堆头` or `4纵陈列` passes this control. `陈列符合` without a visible basis is insufficient. Four vertical facings must be simultaneously visible and countable left to right across the same physical display. Different submitted-brand SKUs, bundles, and package formats may jointly form four columns; do not require four copies of one SKU and do not count visibly unrelated neighboring brands. For multiple routed photos, evaluate each photo independently: any one photo may prove four columns, but partial counts cannot be added across photos. A narrow, angled edge stack counts only when separately bounded additional packages form their own repeated physical column beyond the adjacent front column. The exposed side panel of an already-counted front-facing box is part of that same package and never becomes another facing, even when that side panel repeats across shelf levels. A flush run of narrow side faces beside three front boxes remains three unless package seams, offsets, or another independent face proves a separate stack. Vertically stacked boxes, repeated shelf levels, a separate background shelf, and fully hidden columns cannot be added. One square metre needs visible scale, dimensions, or a complete-footprint basis.

Photo reuse is a separate anti-fraud control and never proves display compliance.

Promotion exists only with an explicit special price, old/new price, discount, gift, multi-buy, `1+1`, `3+2`, or value-pack signal. A normal price tag alone is not promotion. Promotion is mandatory only when the contract explicitly requires it.

## Human-readable worksheet

Keep a six-column management view, with contract-wide sections before the store rows and the total row after the last store:

- A — contract source and baseline: show the six core controls individually, contract core/attachment product-to-knowledge results, then each contract attachment record with PDF page, attachment line, and all nine raw fields. Watermark visibility may appear only as a clearly non-audited informational note.
- B — field-photo evidence: show every submitted basename, useful visible text, date, location, display conclusion and short visual basis, visible vertical count, cross-store reuse result, independently resolved catalog product, promotion result, and one plain product confidence label. Attachment/Excel detail rows explicitly state that photos do not participate.
- C — code-read sales information: show Excel internal arithmetic, then for each contract attachment row the unique Excel row and all nine raw fields. If no contract row exists, show the Excel row as unmatched rather than using another source as its baseline.
- D — directional comparisons: contract core status; each contract attachment product → knowledge base with canonical product and three identity-field results; photo-visible text → knowledge-base text plus packaging → reference images; resolved photo product → contract scope; contract → field photo; contract attachment → sales Excel with eight displayed field results. Unit remains visible only in the two source columns. Product code and 69 code use exact-match wording; product name uses fuzzy-compatible or auxiliary-unavailable wording and never `不匹配`; a fully passing relationship says `全部对应`. Do not render photo-to-Excel or standalone-Excel-to-knowledge comparisons.
- E — short fee basis, unit count × unit fee, and supported amount.
- F — `通过` or `暂不能核销`, followed by `要重新提交什么`. Group requests by source file so the reader sees at most one concrete request for sales Excel, one for the contract, and one for field photos. Name the affected Excel rows/fields or the exact photo content that must be visible.

Render contract-attachment records in their own contract-wide detail rows because they are the main row-level baseline; do not duplicate them inside each store. Show the knowledge-base product and product-code/name/69-code result on every attachment detail row, not only as a count in the overview. Do not render catalog candidate sets, hashes, long model reasoning, or a secondary unavailable file. Never tell the reader to consult JSON or an internal result: the delivered output is the reader's complete handoff. Preserve internal structured evidence for program validation without making it a reading prerequisite.

An error-focused HTML view displays only blocking errors and their source/baseline filenames, concrete differences, and resubmission actions. Keep recognized contract facts available to the audit logic, but hide the passing contract baseline, passing controls, audit process, and informational watermark policy from this view. Contract extraction must still drive every downstream comparison; hiding recognized facts is presentation only and never means the contract was not read.

Formal runs occur only through:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```
