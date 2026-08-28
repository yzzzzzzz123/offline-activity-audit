# Shared product multimodal RAG rules

## Authority and storage

The maintained catalog is a project-level, repository-delivered multimodal product knowledge base.
Its root is `shared/canban-product-multimodal-knowledge-base`; personnel incentives, promotional
displays, and future in-scope consumers use this one validated identity ledger instead of keeping a
scenario-local copy.

- structured authority: `shared/canban-product-multimodal-knowledge-base/references/product-rag.json`;
- controlled product directories: `shared/canban-product-multimodal-knowledge-base/products/`;
- directory convention: `<interface barcode>__<Windows-safe interface product name>__<fixed product code>/`;
- registered reference images are integrity-checked before use.

Catalog `image_file` values are resolved from the project `shared/` directory and therefore retain
the `canban-product-multimodal-knowledge-base/` prefix. Reference images are available only to
bounded visual-identity passes; deterministic consumers may use the structured identity fields
without sending images to a model.

The product code is the immutable business key for interface synchronization. A barcode may be shared by multiple products, so it must not be used to recover or change the product code. Product name, aliases, barcode, variants, specifications, and reference views remain catalog-controlled identity fields.

## Cross-scenario business matching

For every audit consumer, product names use fuzzy compatibility and never require character-for-
character equality. An exact name is only the score-1 special case of the same rule. When comparable
sources provide a 69 code and product code in the same namespace, both identifiers stay strict and
must agree exactly before the relationship passes. Source-local business codes are compared between
their business documents. When a business source is reconciled to the catalog, its code must exactly
equal the selected product's `product_code` or one of its `product_code_aliases`; it need not equal an
unrelated catalog main code when an exact alias exists. Product code and 69 code must jointly hit the
same product before the relationship passes. Once the
comparable strict identifiers agree, differently written product names are fuzzy-compatible auxiliary
text and never create a standalone product-name mismatch error. If a name is absent or illegible but
strict identifiers and independent transaction facts uniquely locate a business row, keep the name as
unavailable auxiliary text and do not use it for status, confidence, problem counts, or resubmission.
If one exact product-code-plus-69-code pair has several catalog variants, a uniquely fuzzy-compatible
source name may disambiguate them. A visibly populated source-name cell omitted by OCR is an extraction defect and must be re-read;
it is not a product-code mismatch.

Catalog-controlled product-name aliases, variants, specifications, observed names, and visible
packaging/code aliases such as `SP-4` may participate in the fuzzy-name candidate set. A packaging/code
alias written in a product-description field is only a name anchor: it never fills, rewrites, or makes
a conflicting strict product-code field pass. Personnel sales Excel has no product-code column, so its
exact 69 code plus a unique fuzzy-compatible name selects the catalog product and returns that catalog
code for traceability. A personnel settlement line without visible identifiers may then map to that
knowledge-backed Excel SKU by fuzzy-compatible name, a unique quantity, and the one-line/one-barcode
constraint; such a deterministic mapping is medium confidence but is not a resubmission reason by
itself.

## Interface synchronization

Use
`shared/canban-product-multimodal-knowledge-base/scripts/sync_product_identity_by_code.py`
for knowledge-base naming maintenance:

1. Read the product code from the existing controlled directory. A newly staged directory may temporarily use only the product code as its name; its first successful synchronization must convert it to the complete three-field convention.
2. Query the mapping API with `product_code` only.
3. Accept exactly one response tuple whose returned product code exactly equals the query.
4. Fail closed on no exact tuple, multiple distinct exact tuples, an empty product name, or an invalid EAN-13 barcode.
5. Keep the product code and source relationships unchanged. Update the directory name, registered catalog name/barcode, identity anchors, and registered view paths from the exact response.
6. Preserve the API product name verbatim in the catalog. Replace only Windows-forbidden directory characters in the folder label; for example, `*` becomes `×` and `/` becomes `／`.
7. Do not rename image files or change image bytes. Reload the catalog after applying and verify every registered image path and SHA-256.

After directory synchronization, use the same tool's `register` command to add every plan-covered controlled directory that is still missing from the catalog. Registration must verify that the current directory is the deterministic target of the exact interface tuple, validate every image with Pillow, record dimensions and SHA-256, and atomically reload the catalog. When the original source collection is unavailable, keep `sources` empty rather than inventing source lineage; retain the current image filename and mark the view `unclassified` and `unreviewed` with an explicit source-lineage limitation.

Never query or reconcile this maintenance flow by the old barcode, and never use fuzzy product-name matching to choose an API row.

## Retrieval chain

For each submitted field-photo group, use this sequence only:

1. extract visible text anchors from the field photo;
2. query the catalog with complete barcode, product code, product name, specification, flavor/variant, and packaging text;
3. retain only the runtime's bounded candidate set, with up to four candidates per photo query and eight across one photo pass so a shared visible code can retain all plausible variants;
4. compare submitted packaging against the registered multi-view reference images;
5. return catalog IDs and registered view IDs only.

Do not scan arbitrary product directories from the model prompt. Do not treat every product with the same barcode as the same SKU. Do not return a product merely because a generic brand word or color is shared.

## Anchor strength

Strong anchors:

- complete valid visible barcode;
- visible registered product code or alias;
- sufficiently specific legal or registered product name;
- compatible specification, count, volume, flavor/variant, and bundle notation;
- distinctive packaging details confirmed against registered reference views.

Weak anchors that cannot identify a SKU alone:

- brand only;
- common colors;
- box, tube, or bottle shape;
- generic whitening, freshening, antibacterial, or audience wording;
- QR code;
- batch/date printing;
- background shelf or display furniture.

## Exact, candidate, and unmatched

Return `exact` only when one product is uniquely supported by one of these routes:

- a complete valid visible barcode and no unresolved same-code variant conflict;
- a visible registered product code or unique alias;
- a visible product code or fuzzy-compatible product name plus an independent compatible specification, variant, or package anchor;
- at least two independent field anchors that uniquely converge and are broadly visually compatible with one or more registered multi-view references. The comparison is not pixel-identical: normal angle, distance, lighting, shelf occlusion, or package-pose differences are allowed when core color blocks, layout, bundle structure, and recognizable features agree without conflict.

Bundle text may converge compositionally. For example, visible `3+2`, `420g`, and `量贩装` together retrieve the registered `3+2` 420g bundle even when the full product name and barcode are absent; broadly compatible packaging across its registered views is then sufficient for `exact`.

Return `candidate` when packaging is compatible but identity remains non-unique, including an unresolved same-barcode product group or shared alias. Return no hit when the submitted view does not support a catalog identity.

Candidate hits never pass the photo-product gate and never become exact merely because the same catalog ID appears in sales data.

## Photo-text-bounded retrieval and separation from business sources

The photo model never uses contract product wording or sales Excel to retrieve or identify a field product. A contract/attachment/Excel product name, code, or barcode cannot be copied into `visible_text`, `recognized_products`, or `visible_basis`.

The runtime first extracts useful text from each submitted field photo, including partial names,
codes, specifications, variants, bundle notation, and packaging text. It then retrieves a small
candidate set from the full validated catalog using only that same-photo text and supplies only those
candidates' registered views to the visual comparison pass. The model must not scan arbitrary product
directories or use a candidate that was not retrieved from the submitted photo's visible text.

Contract-product reconciliation and standalone sales-Excel reconciliation remain separate business
controls outside this photo-identity retrieval. After the photo identity is fixed, deterministic code
may compare that identity with contract scope. Standalone sales Excel is compared directly with the
corresponding contract-attachment row, not with a field photo. Neither contract nor Excel can select a
photo candidate or promote a candidate photo identity to exact.

The photo itself does not need to show a full product name or barcode. Exact identity requires useful
visible text to correspond uniquely to catalog-controlled text and the submitted packaging to be
broadly visually compatible with at least one registered multi-view reference. A fuzzy photo identity remains
unresolved. Generic brand text cannot create an exact identity or an unbounded candidate set.

## Evidence boundary

Reference images can support identity only. They cannot prove:

- field merchant, store, or location;
- field date;
- display area or vertical-display count;
- promotion or field price;
- originality or non-reuse of submitted photos;
- reimbursement amount.

Every `visible_basis` sentence must describe something visible in a submitted field photo. Reference-only content is never a field observation. Keep full retrieval candidates, registered identities, matched view IDs, integrity hashes, and limitations in the internal structured result. In Excel, show only the product, match level, score, and exact resubmission requirement; never direct the reader to the internal result.
