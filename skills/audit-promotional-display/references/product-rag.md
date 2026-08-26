# Product multimodal RAG rules

## Authority and storage

The maintained catalog is a repository-delivered multimodal product knowledge base:

- structured authority: `references/product-rag.json`;
- controlled product directories: `canban-product-multimodal-knowledge-base/products/`;
- directory convention: `<interface barcode>__<Windows-safe interface product name>__<fixed product code>/`;
- registered reference images are integrity-checked before use.

The product code is the immutable business key for interface synchronization. A barcode may be shared by multiple products, so it must not be used to recover or change the product code. Product name, aliases, barcode, variants, specifications, and reference views remain catalog-controlled identity fields.

## Interface synchronization

Use `scripts/sync_product_identity_by_code.py` for knowledge-base naming maintenance:

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
3. retain only the runtime's bounded candidate set, normally no more than two candidates per photo query;
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
- at least two independent field anchors that uniquely converge and agree with one or more registered reference views.

Return `candidate` when packaging is compatible but identity remains non-unique, including an unresolved same-barcode product group or shared alias. Return no hit when the submitted view does not support a catalog identity.

Candidate hits never pass the photo-product gate and never become exact merely because the same catalog ID appears in sales data.

## Separation from sales and contract

The photo model never reads sales Excel. A sales product name, code, or barcode cannot be copied into `visible_text`, `recognized_products`, or `visible_basis`.

Deterministic code separately checks sales rows:

- code strictly equals the registered code or alias;
- barcode strictly equals a valid catalog barcode;
- name is exact or uniquely fuzzy-compatible with the strict code/barcode product.

Only after both sales and photo identities pass their own knowledge checks may the runtime compare catalog products, names, codes, and barcodes. The photo itself does not need to show a barcode: an exact photo identity supplies the catalog barcode used for the sales comparison. A fuzzy photo identity leaves that later barcode check unavailable. Concrete contract products follow the same catalog route; generic brand scope is not applicable.

## Evidence boundary

Reference images can support identity only. They cannot prove:

- field merchant, store, or location;
- field date;
- display area or vertical-display count;
- promotion or field price;
- originality or non-reuse of submitted photos;
- reimbursement amount.

Every `visible_basis` sentence must describe something visible in a submitted field photo. Reference-only content is never a field observation. Keep full retrieval candidates, registered identities, matched view IDs, integrity hashes, and limitations in the internal structured result. In Excel, show only the product, match level, score, and exact resubmission requirement; never direct the reader to the internal result.
