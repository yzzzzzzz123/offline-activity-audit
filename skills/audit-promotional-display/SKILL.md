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
- [product-rag.md](references/product-rag.md) for field-photo product identity

The runtime validates [product-rag.json](references/product-rag.json) and supplies only bounded catalog candidates and their registered multi-view images.

## Trust boundary

Vision AI may read:

- every contract PDF page at original resolution;
- submitted field photos;
- bounded repository-owned product-reference images during the final photo pass.

Vision AI must not read the sales Excel, prior outputs, caches, acceptance workbooks, or unrelated repository files. It extracts visible facts only and must not calculate a supported amount or make the final pass decision.

Product-reference images establish product identity only. They cannot establish the submitted store, date, location, display, promotion, price, photo originality, or amount, and they must never be returned as submitted `photo_files`.

## Visual extraction

Return evidence schema version `2.3`.

For the contract, extract only explicit core terms:

- contracting parties and the customer/distributor party;
- activity budget, execution period, activity content, display standard, claimed amount;
- fee basis: `per_store`, `per_stack`, `total_only`, or `unclear`;
- total stack count and each listed store's stack count only when explicit;
- watermark and seal visibility;
- every merchant/store in printed order;
- specific product and promotion requirements only when the core terms actually impose them.

A brand scope, whole series, broad category, activity wording, or appended product/sales table is not a specific contract SKU condition. If the contract does not contain a checkable product code, sufficiently specific product name, or complete valid 69 code, set product knowledge to not applicable.

For every contract store, return exactly one photo review, including an empty review when no photo can be assigned. Preserve submitted basenames exactly. Extract:

- useful visible text, complete date, location, and their visual basis;
- whether the photo proves `1平米堆头`, `4纵陈列`, both, neither, or is unclear;
- a reliable vertical-facing count and corresponding left-to-right basis;
- explicit promotion signals separately from ordinary prices;
- risks and useful supplemental material.

Follow the field-product chain in this order:

1. transcribe useful packaging text, name, specification, product code, and complete barcode;
2. retrieve only the bounded catalog candidates supplied by the runtime;
3. compare those reference views with the submitted packaging;
4. return only a uniquely supported exact identity, a fuzzy identity, or no identity.

The field photo does not need to show a complete product name or a 69 code. A visible registered short code that belongs to one catalog product, such as `SP-1`, is enough for an exact product identity even when spacing, case, or the hyphen differs. A short code shared by several products, such as the current `SP-4`, stays fuzzy until specification, flavor, clearer name text, or packaging disambiguates it. Product names may be fuzzy throughout the audit. A supplied product code and a supplied 69 code remain strict.

## Deterministic audit order

Python owns the decision in this exact order:

1. Read sales Excel cells directly and preserve customer, business date, product code, product name, 69 code, quantity/amount, and Excel row number.
2. Reconcile a contract product only when the contract contains a concrete checkable identity. Otherwise record `not_applicable`.
3. Resolve all photo hits through the catalog. A store's photo-product gate passes only when at least one catalog product exists and all returned hits are exact. Do not require a visible photo barcode: first determine the catalog product from visible name fragments, a catalog-unique short code, and packaging. A fuzzy photo identity makes later strict code and 69-code checks unavailable, not mismatched.
4. For every field product, use its catalog identity to locate only the relevant sales rows:
   - start a fresh sales-row identity check with the field product's exact catalog 69 code plus an exact or specification-compatible fuzzy product name; retain a row only when the sales catalog reconciliation independently resolves it to that same catalog product;
   - a short code such as `SP-3` may establish the photo product, but the same text inside a sales product name is never a sales-row locator and never overrides a different product name or 69 code;
   - product code is a strict verification field after row location, never an alternative locator that can bypass exact 69 code plus compatible name;
   - product code must strictly equal the catalog code or a registered alias; an unregistered distributor code such as `020...` fails even when name and 69 code agree;
   - 69 code must be a valid EAN-13 and strictly equal the catalog 69 code;
   - product name may be exact or fuzzy when specification/size/count remains compatible;
   - if no row has exact catalog 69 code, compatible name, and an independent resolution to the same product, report the valid sales row as missing; do not attach or rewrite a name-only, short-code-only, or different-barcode row;
   - every relevant row must pass. Show every relevant row; do not truncate to a sample or redirect the reader to JSON;
   - retain whole-file sales reconciliation as an internal diagnostic only. An unrelated bad row never becomes a global switch that fails every store.
5. Compare the files internally after the field-product chain is established:
   - contract ↔ photo: merchant/location, period/date, display requirement, promotion requirement, and optional concrete contract product;
   - photo ↔ sales: the catalog product determined from the photo against every relevant sales row's strict code, fuzzy-compatible name, and strict 69 code;
   - contract ↔ sales: signing party/customer, execution period/business date, and watermark/seal integrity.
6. Independently check submitted photo existence and cross-store exact/near reuse.
7. Calculate amount only from an explicit unit basis:
   - `per_store`: unit fee × passed stores;
   - `per_stack`: unit fee × explicit passed stack count;
   - `total_only` or `unclear`: never divide the total automatically; require manual confirmation;
   - cap the recommendation by the claim and by the explicit activity budget when present.
8. Validate the structured result before writing a workbook. Write values only, never formulas.

## Display and promotion

The display standard is an OR condition: clearly proving either `1平米堆头` or `4纵陈列` passes this control. `陈列符合` without a visible basis is insufficient. Four vertical facings must be simultaneously visible and countable left to right; vertically stacked boxes or different shelf levels cannot be added. One square metre needs visible scale, dimensions, or a complete-footprint basis.

Photo reuse is a separate anti-fraud control and never proves display compliance.

Promotion exists only with an explicit special price, old/new price, discount, gift, multi-buy, `1+1`, `3+2`, or value-pack signal. A normal price tag alone is not promotion. Promotion is mandatory only when the contract explicitly requires it.

## Human-readable worksheet

Keep one six-column row per contract store and write the total row immediately after the last store:

- A — contract summary: parties, budget/claim, period, activity content, merchant/store, stack count, watermark/seal, display, and any actual optional product or promotion terms. Put `盖章：是/否` on its own line immediately after the watermark line. If the contract does not require a specific product or promotion, omit that line entirely instead of writing a negative placeholder. Put full global terms in the first row; later rows may point to it and show only store-specific terms.
- B — field-photo summary: every submitted basename, useful visible text, date, location, display conclusion and short visual basis, visible vertical count, cross-store reuse result, catalog-backed product, promotion result, and one plain product confidence label.
- C — code-read sales information: customer and business date, followed by every sales row relevant to the field product. Within the existing cell, show the catalog product and corresponding Excel row as separate labeled field blocks for product code, product name, 69 code, and quantity; do not concatenate identities with slash separators. Follow the blocks with the three field results and one `置信度：高/中/低` label. Do not show unrelated rows and do not truncate relevant rows. The HTML view must render the same visible field blocks as compact key/value tables without adding or changing facts.
- D — exactly three short comparisons in evidence order: contract ↔ photo, photo ↔ sales, and contract ↔ sales. Give every relationship only one overall `置信度：高/中/低` label and one short reason. Exact, fuzzy, or mismatch wording may appear only in the field-specific explanation, not as a repeated overall match-result label.
- E — short fee basis, unit count × unit fee, and supported amount.
- F — `通过` or `暂不能核销`, followed by `要重新提交什么`. Group requests by source file so the reader sees at most one concrete request for sales Excel, one for the contract, and one for field photos. Name the affected Excel rows/fields or the exact photo content that must be visible.

Do not render catalog candidate sets, raw-versus-authority dumps, hashes, long model reasoning, or a secondary detail table in Excel. Never tell the reader to consult JSON, an internal result, or another detail file: the delivered workbook is the reader's complete handoff. Preserve internal structured evidence for program validation without making it a reading prerequisite.

Formal runs occur only through:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```
