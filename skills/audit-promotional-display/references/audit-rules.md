# Promotional display deterministic rules

## Decision order

The product knowledge base is the authoritative product-identity ledger. The audit order is fixed:

1. optional concrete contract product ↔ product knowledge;
2. field-photo text retrieval ↔ bounded catalog candidates ↔ reference-image comparison;
3. each field product ↔ all relevant sales Excel rows ↔ product knowledge;
4. contract ↔ photo, photo ↔ sales, and contract ↔ sales;
5. explicit fee calculation and final store decision.

No amount, filename, or generic name resemblance may bypass a failed product check. A whole-file sales
reconciliation may be retained as an internal diagnostic, but a row unrelated to a store's field product
must not fail that store.

## Contract extraction and interpretation

- Preserve all visible contracting parties. `customer_name` is the customer/distributor expected to appear in the sales file, chosen from contract text only.
- Preserve activity budget, execution period, activity content, display standard, claimed amount, listed merchants/stores, total stack count, per-store stack counts, watermark, and seal.
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

## Sales Excel ↔ product knowledge

Read every sales row directly from source cells and preserve:

- Excel row number;
- customer and business date;
- raw product code, product name, and 69 code;
- quantity, price, and amount when present.

AI evidence contains no sales identity and cannot override these values.

All three product identity fields are mandatory:

- product code must equal the catalog's registered product code or a registered code alias;
- 69 code must be a complete valid EAN-13 and equal the catalog barcode;
- product name may equal a registered name/alias or uniquely fuzzy-match the same strict code-and-barcode product when size, count, and volume remain compatible.

Deterministic outcomes:

- `matched`: code, name, and 69 code are all exact for one catalog product;
- `fuzzy_matched`: code and 69 code are strict; the name uniquely and conservatively fuzzy-matches that same product;
- `unmatched`: no supplied identity field finds a catalog product;
- `ambiguous`: more than one product remains after permitted checks;
- `conflict`: fields point to different products, a required field is missing, the EAN-13 is invalid, or the code/barcode does not strictly agree.

Only `matched` and `fuzzy_matched` pass. An external distributor code that is not a registered alias fails even when name and barcode appear plausible. A field-photo short code such as `SP-3` stops at the photo-to-catalog boundary. Sales-row identity starts again with exact catalog 69 code plus an exact or specification-compatible fuzzy name, and the row must independently resolve to the same catalog product. The same short-code text inside a sales name is never a locator. A name-only, short-code-only, wrong-barcode, or unresolved shared-barcode row is not the corresponding sales row and must not be rewritten as the field product. Whole-file outcomes remain diagnostic; store approval depends on every valid sales row selected by that store's field-photo product, not on unrelated rows.

The structured result retains all source values, resolved catalog values, field comparisons, candidates, and basis for program validation. In the workbook, every relevant row shows the original Excel identity, the field-photo product's catalog identity, all three field results, overall result/confidence, and an exact correction when needed. Do not truncate relevant rows and do not tell the reader to consult JSON or another internal file.

## Contract product ↔ product knowledge

- `not_applicable`: the core contract contains no concrete checkable product identity;
- `pass`: every concrete contract identity uniquely maps by explicit fields, with conservative fuzzy name matching allowed only when supplied strict fields stay consistent;
- `fail`: a supplied contract code/name/barcode is missing from, ambiguous in, or conflicts with the catalog.

When applicable, each required contract product ID must occur in both passed sales rows and exact field-photo hits.

## Field photo ↔ product knowledge

Field product identity follows four stages:

1. extract useful visible text and identity anchors from submitted photos;
2. retrieve a bounded catalog candidate set;
3. compare submitted packaging with registered reference views;
4. return exact, fuzzy, or unmatched identity.

The catalog owns product name, code, aliases, barcode, and reference-view identity. Model text cannot rewrite them.

- `exact`: a visible registered short code or alias that belongs to one catalog product (for example `SP-1`), a complete valid visible 69 code, or other visible name/packaging facts that uniquely identify one product; a full product name is not required;
- `candidate`: packaging or fuzzy name text is compatible but the product is not unique; a shared code such as the current `SP-4` remains here until another visible fact disambiguates it;
- `unmatched`: no catalog product is supported.

A store's photo-product gate passes only when at least one product is present and every returned hit is exact. A fuzzy result never becomes exact merely because the same product appears in Excel. Brand, color, shape, generic claims, QR code, batch/date printing, or background alone cannot establish a product.

## Internal three-way reconciliation

### Contract ↔ sales

- Compare each sales customer to the contract customer/parties after conservative company-suffix normalization. Exact or uniquely fuzzy entity correspondence passes; mismatch fails; missing evidence is unverifiable.
- Parse each sales business date or year-month. Every parsed interval must be fully covered by the contract execution period. Outside dates fail; missing/unparseable dates are unverifiable.
- Report watermark and seal separately. Contract integrity passes when at least one is visibly present, fails when both are visibly absent, and is unverifiable when neither state can be established.
- Any mismatch fails this global reconciliation. Any unverifiable mandatory item prevents automatic pass.

### Contract ↔ field photo

For each store compare:

- submitted photo exists;
- complete visible date is within the contract period;
- visible location corresponds to the contract merchant/store;
- display evidence proves the required OR branch;
- explicit promotion is visible when the contract requires it;
- concrete required contract product is present when applicable.

The deterministic location result may be `exact`, `compatible`, `mismatch`, or `filename_only`. A filename alone never passes. A filename may only bridge an independently visible location to a contract name when both are strongly corroborated and there is no numbered-branch conflict. If the contract names `二店`, the visible location must show that same branch number.

### Field photo ↔ sales

Compare only catalog-backed identities:

- first use each photo's catalog product to locate relevant Excel rows by exact catalog 69 code plus exact or fuzzy-compatible product name; require the row's independent catalog reconciliation to resolve to that same product, and fail closed when a shared 69 code remains ambiguous;
- every relevant row must pass; show every relevant row rather than a sample;
- product names may be fuzzy-compatible for that same product and never need character-for-character equality;
- a supplied product code is verified only after row identity is established and must strictly equal the registered code or registered alias. It is not a row locator. `020...` and other unregistered distributor codes fail and are never translated silently;
- a supplied 69 code must be strictly equal;
- photo short codes never select sales rows. If no exact-69 plus compatible-name row resolves to the field product, report a missing valid sales row and do not instruct the reader to mutate an unrelated row;
- the field photo itself does not need to show a 69 code. First determine the catalog product from visible text/short code/packaging, then compare that product's registered 69 code with sales Excel. If the photo product is still fuzzy, report the 69-code comparison as unavailable rather than mismatched.

Direct filename matching, raw-text-only matching, or package resemblance outside the catalog cannot pass this step.

## Display standard

The display rule is an OR condition:

- `stack_1sqm`: submitted view provides visible scale, dimensions, or complete-footprint evidence for roughly one square metre;
- `four_vertical`: at least four distinct columns/facings are simultaneously visible and countable left to right;
- `both`: both branches are proven.

`standard_evidence=meets` pairs only with `stack_1sqm`, `four_vertical`, or `both`; `does_not_meet` pairs only with `none`; `unclear` pairs only with `unclear`.

Do not add vertically stacked boxes, different shelf levels, or different angles into a four-column count. A generic `陈列符合` statement is not a visible basis.

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
- optional contract product knowledge passes;
- submitted photo exists and is bound to the correct store;
- visible date and location pass;
- display passes;
- photo reuse passes;
- photo knowledge and photo ↔ sales product reconciliation pass;
- explicit promotion passes when required;
- amount has an explicit unit basis.

Amount calculation:

- `per_store`: `1 × unit fee` for each passed store;
- `per_stack`: `explicit store stack count × unit fee` for each passed store;
- `total_only` or `unclear`: zero automatic allocation and manual confirmation;
- total recommendation is capped by supported amount, claimed amount, and explicit activity budget when present.

Missing, mismatch, uncertain, fuzzy, duplicate, or manual-allocation controls support zero until resolved.

## Workbook visibility

The six visible columns are a management summary, not the evidence ledger:

1. contract;
2. field photos;
3. sales Excel rows relevant to the field products;
4. three internal comparisons;
5. amount;
6. conclusion and no more than three next steps.

Do not append a second detail table. Do not expose candidate sets, exact/perceptual hashes, complete model reasoning, or unrelated whole-file sales problems. Keep them in the schema-validated internal result for traceability. The workbook must stand alone for its reader: never write `详见审计JSON`, `见内部结果`, or another unavailable-file reference. For each displayed product, show every relevant Excel row, three plain field results, one overall `匹配结果`/`置信度`, and the exact file/content to resubmit when needed.
