# Promotional display deterministic rules

## Decision order

The product knowledge base is the authoritative product-identity ledger. The audit order is fixed:

1. contract core six mandatory controls;
2. concrete core-contract products and every contract-attachment product → product knowledge;
3. field photo visible text → full validated product knowledge, then field packaging → retrieved reference images; separately compare the resolved product with contract scope;
4. sales Excel internal arithmetic and contract attachment → sales Excel across eight checked fields,
   with unit retained for display only;
5. explicit fee calculation and final decision.

No amount, filename, sales row, or generic name resemblance may bypass a failed contract-product or
photo-product check. Sales Excel never supplies a catalog identity and never participates in photo
identity. A sales mismatch is reported against the contract attachment and must not rewrite contract
facts.

## Contract extraction and interpretation

- Preserve all visible contracting parties. `customer_name` is the customer/distributor expected to appear in the sales file, chosen from contract text only.
- Preserve activity budget, execution period, activity content, the exact reimbursement/settlement method, display standard, claimed amount, listed merchants/stores, total stack count, per-store stack counts, watermark, and seal. Contract watermark visibility is informational only and never participates in reimbursement.
- For every dense printed attachment, the full-contract extraction is followed by a focused
  original-resolution reread of the product-code, product-name, and 69-code cell on every row, using
  `contract-product-cells.schema.json`. Preserve the same line/page order. First-pass transaction
  numerics may locate a row, but neither sales Excel nor product knowledge may supply, correct,
  or suggest any reread identity field. Any apparently missing code, name, or 69 code beside legible numeric cells
  must trigger another focused attempt; a first-pass OCR omission is not proof that the PDF cell is
  blank. Every nonempty reread 69 code must pass EAN-13 validation.
- Classify fee wording exactly:
  - `per_store`: an explicit unit fee for each listed store;
  - `per_stack`: an explicit unit fee for each stack/display unit;
  - `total_only`: a total budget or claim with no unit allocation;
  - `unclear`: the unit basis cannot be established.
- Never infer a unit fee by dividing a total.
- Set a store's `stack_count` only when explicit or when the contract explicitly establishes one stack per listed store.
- A whole-brand phrase, whole series, broad category, activity wording, or appended sales/product table is not a narrow contract SKU condition.
- Set `requires_specific_products=true` only for a concrete code, sufficiently specific product name, or complete valid 69 code in a core contract term.
- Set `requires_promotion=true` only when a core term explicitly requires a promotion mechanic.

## Contract products → product knowledge

The knowledge base is authoritative only for product identity. Reconcile these two contract-side
sources separately:

- concrete product identities imposed by core contract terms;
- every row in the printed contract sales attachment.

All supplied identity fields are preserved. A supplied 69 code must be a valid exact EAN-13 and must
match the catalog barcode. A source-local business product code is preserved for strict comparison
between the contract attachment and sales Excel. For product → knowledge reconciliation, it must also
exactly match the selected catalog product's `product_code` or `product_code_aliases`; a different
catalog-internal main code is acceptable only when the source code is registered as an exact alias.
Product code and 69 code must jointly identify the same catalog product. Product-name text is fuzzy auxiliary evidence and never requires character-for-character
equality. Once comparable business product code and 69 code agree, do not emit a separate product-name
mismatch or fail the row because its names are written differently, absent, or illegible. When strict
identifiers and independent transaction facts uniquely locate the row, record an unavailable name only
as auxiliary information and never count it as a problem or resubmission item. A packaging/code alias may
participate only as text in the fuzzy-name field or in field-photo recognition and never substitutes
for a strict product-code field. `matched` and `fuzzy_matched` pass; ambiguous, conflicting, missing, invalid, or
unregistered identities fail their own contract-side row. An attachment row remains sales evidence and
never becomes a core product or promotion requirement.

When one exact product-code-plus-69-code pair has multiple catalog variants, use the contract
attachment's fuzzy-compatible product name to disambiguate them. If that name was returned null while the original PDF cell visibly
contains text, classify the cause as contract-PDF extraction failure and re-read the cell; do not call
it a source-product-code-to-knowledge mismatch.

## Sales Excel → contract attachment

Read every sales row directly from source cells and preserve:

- Excel row number;
- customer and business date;
- raw product code, product name, and 69 code;
- unit, quantity, retail price, and total amount when present.

AI evidence contains no sales identity and cannot override these values.

First check the Excel file internally: each row's quantity × retail price must equal its total amount,
and any printed totals must agree with the detail rows. Then pair each contract attachment row to at
most one unused Excel row and compare eight fields: customer name, business date, product code,
product name, 69 code, quantity, retail price, and total amount. Contract values are the baseline.
Codes, barcodes, quantities, prices, and amounts are strict; customer names remain conservatively
fuzzy, while product names are auxiliary fuzzy text and never create a separate mismatch, missing-name
failure, problem count, or resubmission request once strict identifiers and transaction facts locate the
row; dates may use semantically equivalent formats. Preserve both raw unit
values for display, but unit never participates in pairing, a field result, pass/fail, confidence,
problem counts, reimbursement, or resubmission. Preserve PDF page, attachment line, Excel row, every
checked field status, contract row arithmetic, and printed totals. An unmatched contract row or Excel
row is visible and blocks automatic reimbursement.

Do not reconcile standalone sales Excel to the knowledge base. Do not compare it with field photos and
do not use it to identify or upgrade a photographed product. If the contract has no row-level sales
attachment, every Excel row lacks a contract baseline: return `unverifiable`, list the rows, request a
complete contract attachment, and do not fall back to product knowledge or photos.

## Contract product ↔ product knowledge

- `not_applicable`: the core contract contains no concrete checkable product identity;
- `pass`: every concrete contract identity uniquely maps by explicit fields, with conservative fuzzy name matching allowed only when supplied strict fields stay consistent;
- `fail`: a supplied contract code/name/barcode is missing from, ambiguous in, or conflicts with the catalog.

When applicable, each required core-contract product ID must occur in exact field-photo hits. Sales
Excel is not part of this product-identity gate.

## Field photo ↔ product knowledge

Field product identity is resolved independently from contract and sales product text:

1. extract useful visible text and identity anchors from each submitted photo;
2. use only that same-photo text to retrieve a small candidate set from the full validated catalog;
3. correspond the text to catalog-controlled names, aliases, specifications, variants, and packaging text, then compare submitted packaging with registered reference views;
4. return exact, fuzzy, or unmatched identity.

The catalog owns product name, code, aliases, barcode, and reference-view identity. Model text cannot rewrite them.

- `exact`: visible text uniquely corresponds to one catalog product and the submitted packaging is broadly visually compatible with at least one registered multi-view reference. The text route may be a registered short code or alias that belongs to one product (for example `SP-1`), a complete valid visible 69 code, or another uniquely convergent combination of partial name, specification, flavor/variant, bundle notation, and packaging text; for example, `3+2` + `420g` + `量贩装` retrieves the registered `3+2` bundle. A full product name or 69 code is not required. Visual compatibility is not pixel equality: perspective, distance, lighting, shelf occlusion, and package pose may differ when the core color blocks, layout, bundle structure, and recognizable features agree and there is no conflict;
- `candidate`: packaging or fuzzy name text is compatible but the product is not unique; a shared code such as the current `SP-4` remains here until another visible fact disambiguates it;
- `unmatched`: no catalog product is supported.

The `现场商品知识库` control passes when at least one product is present and every returned hit is exact by the text-plus-reference-image chain. Contract membership is a separate control: after identity is fixed, every exact hit must satisfy the applicable contract product scope. Thus a field product may pass knowledge-base identity while still failing `现场商品与合同`. A fuzzy result never becomes exact because the same product appears in the contract or Excel. Brand, color, shape, generic claims, QR code, batch/date printing, background, or visual resemblance without corresponding field text cannot establish a product.

## Directional reconciliation

### Contract ↔ sales

- Compare each sales customer to the contract customer/parties after conservative company-suffix normalization. Exact or uniquely fuzzy entity correspondence passes; mismatch fails; missing evidence is unverifiable.
- Parse each sales business date or year-month. Every parsed interval must be fully covered by the contract execution period. Outside dates fail; missing/unparseable dates are unverifiable.
- Report seal as the mandatory integrity control: visibly absent fails and unverifiable prevents automatic pass. Watermark may be reported separately only as an informational fact; absent or unverifiable watermark never changes status, confidence, amount, or resubmission.
- Any mismatch fails this global reconciliation. Any unverifiable mandatory item prevents automatic pass.

### Contract ↔ field photo

For each store compare:

- submitted photo exists;
- complete visible date is within the contract period;
- visible location corresponds to the contract merchant/store;
- display evidence proves the required OR branch;
- explicit promotion is visible when the contract requires it;
- concrete required contract product is present when applicable.

The deterministic location result may be `exact`, `compatible`, `mismatch`, or `filename_only`. A filename alone never passes. A filename may only bridge an independently visible watermark location to a contract name when both are strongly corroborated and there is no numbered-branch conflict. If the contract names `二店`, the visible watermark location must show that same branch number. Keep two failure classes distinct: `mismatch` means the watermark location is already legible but conflicts with the contract store, so report `门店水印错误`, show both exact values, and require a photo with the correct watermark location; `filename_only` means the watermark location is absent or unreadable, so request visible watermark location evidence. Never describe a legible mismatch as `没看清` or ask only for a clearer photo.

### Prohibited cross-links

- Field photos are not compared to sales Excel because the submitted Excel has no authoritative store
  column and is not the contract baseline for visual execution.
- Sales Excel is not compared to product knowledge; its product fields are compared directly with the
  corresponding contract attachment row.
- Excel values never select a photo candidate, resolve photo ambiguity, or rewrite contract values.

## Display standard

The display rule is an OR condition:

- `stack_1sqm`: submitted view provides visible scale, dimensions, or complete-footprint evidence for roughly one square metre;
- `four_vertical`: at least four distinct physical columns/facings are simultaneously visible and countable left to right across the same display; a perspective-compressed or partly side-facing edge stack counts only when separately bounded additional packages form their own repeated column beyond the adjacent front column;
- `both`: both branches are proven.

`standard_evidence=meets` pairs only with `stack_1sqm`, `four_vertical`, or `both`; `does_not_meet` pairs only with `none`; `unclear` pairs only with `unclear`.

Different submitted-brand SKUs, bundles, and package formats may jointly form the four columns; four copies of one SKU are not required, while visibly unrelated neighboring brands do not count. Evaluate multiple routed photos independently: any one photo may prove four columns, but never add partial counts across photos. Do not count the exposed side panel of an already-counted front-facing package as another facing, even when that side panel repeats across shelf levels. A flush run of narrow side faces beside three front boxes remains three unless package seams, offsets, or another independent face proves a separate stack. Do not add vertically stacked boxes, reuse one placement across shelf levels, combine a separate background shelf, or infer a fully hidden column. Do not discard a separately bounded edge stack merely because it is angled. A generic `陈列符合` statement is not a visible basis.

The accepted visual-regression registry may replace the focused observation only when the immutable
contract store and every ordered routed-photo SHA-256 match one registry item exactly. The calibration
is invalid after any byte, photo-set, order, or store-route change. Record the calibration ID and run
the same structured display validation after replacement; never match by filename or visual similarity.

## Photo reuse

Deterministic code calculates exact and perceptual image fingerprints across submitted field photos:

- byte-exact cross-store reuse fails;
- unresolved near-identical cross-store reuse candidates prevent automatic pass;
- no finding passes only the reuse control.

Reuse evidence is retained in JSON. The workbook shows only `未发现`, `发现`, `疑似`, or `无法判断`, never raw hashes or pair calculations. Photo reuse is independent of display compliance.

## Promotion

Promotion requires an explicit visible signal: special price wording, old/new price, discount, gift, multi-buy, `1+1`, `3+2`, or value-pack wording. A normal price tag is not promotion. If visibility is insufficient, return `无法判断` with the limitation.

## Amount and final store control

For each store, automatic support requires all applicable controls:

- contract ↔ sales global reconciliation passes;
- all six mandatory contract core controls pass;
- optional contract product knowledge passes;
- contract attachment product knowledge passes when an attachment exists;
- submitted photo exists and is bound to the correct store;
- visible date and location pass;
- display passes;
- photo reuse passes;
- photo knowledge is exact, and the separately resolved photo product satisfies the applicable contract scope;
- explicit promotion passes when required;
- contract attachment → sales Excel eight-field reconciliation passes; unit is display-only;
- sales Excel internal arithmetic passes;
- amount has an explicit unit basis.

Amount calculation:

- `per_store`: `1 × unit fee` for each passed store;
- `per_stack`: `explicit store stack count × unit fee` for each passed store;
- `total_only` or `unclear`: zero automatic allocation and manual confirmation;
- total recommendation is capped by supported amount, claimed amount, and explicit activity budget when present.

Missing, mismatch, uncertain, fuzzy, duplicate, or manual-allocation controls support zero until resolved.

## Workbook visibility

The six visible columns are a complete management handoff:

1. contract;
2. field photos;
3. sales Excel;
4. directional comparison with the contract;
5. amount;
6. conclusion and no more than three next steps.

Render in this order: contract core six controls; contract product knowledge; contract attachment
summary and one detail row per attachment/Excel pair (plus unmatched Excel rows); store/photo rows;
final amount. Do not expose candidate sets, exact/perceptual hashes, or complete model reasoning. The
output must stand alone for its reader: never write `详见审计JSON`, `见内部结果`, or another
unavailable-file reference. Every attachment detail shows all nine raw fields, the knowledge-base
standard product and its product-code/name/69-code result, all eight attachment-to-Excel field results,
one overall confidence, PDF page/line, Excel row, and the exact content to resubmit when needed.
Product code and 69 code are shown as exact match/not matched; product name is shown only as fuzzy-
compatible or auxiliary-unavailable and never as a standalone mismatch. A fully passing row says
`全部对应` rather than vague `可以对应` wording. An error-focused HTML projection renders only blocking
errors, source/baseline filenames, concrete differences, and resubmission actions. Passing contract
facts and informational watermark policy remain available to the audit logic but are hidden from this
projection; presentation filtering must not weaken contract-led comparisons.
