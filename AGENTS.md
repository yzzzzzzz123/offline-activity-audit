# Offline activity audit project

## Current registered scope

This repository audits five offline-activity scenarios only:

- `personnel_incentive` through `skills/audit-personnel-incentive`;
- `promotional_display` through `skills/audit-promotional-display`;
- `poster_material` through `skills/audit-poster-material`.
- `other_expense` through `skills/audit-other-expense`.
- `maintenance_fee` through `skills/audit-maintenance-fee`.

Personnel and promotional-display product identity share the project-level catalog at
`shared/canban-product-multimodal-knowledge-base`. No scenario Skill owns a private copy.

The only formal command is:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> [--scenario <scenario>]
```

`input/` must contain one to five ZIP files. Each supported scenario may appear at most once. Reject unknown, ambiguous, duplicate-type, or unsafe archives with a specific error. A maintenance-fee package whose marker and POS/settlement structure are unambiguous may continue to audit so missing mandatory roles become customer-facing blocking issues; ambiguous duplicate singleton roles still stop intake. `--scenario` may select one submitted type for a scenario-only formal result.

## New scenario onboarding

When the user supplies one representative ZIP plus a business prompt and asks to create another
audit scenario, use `skills/create-offline-audit-scenario/SKILL.md`. This is a development workflow,
not a formal audit run. First decide whether the request is a new reusable evidence/decision chain or
only another case of one of the five registered scenarios. Reuse or update the existing scenario
when its material roles, authority graph, deterministic controls, amount rule, and output object are
the same; never create one Skill per customer, month, activity number, or ZIP filename.

A genuinely new scenario is active only after its Skill package, safe ZIP classifier and role binder,
visual extraction adapter, source-coverage validation, deterministic handler, result Schema,
six-column intermediate renderer, canonical HTML subinterface, CLI choice, documentation, and
positive/negative tests are all integrated and verified. Until then, leave it explicitly unregistered;
do not route an unknown scenario through the personnel, display, or poster fallback path. The formal
entrypoint remains `orchestrate-offline-audit/scripts/run.py` after every onboarding.

## Trust boundary

Keep extraction and judgment separate. AI or vision steps may only extract visible facts into schema-validated JSON:

- personnel: settlement image and transfer/red-packet screenshots;
- display: contract PDF and field photos; the field-photo pass may additionally receive the
  repository-owned, hash-validated product-reference views under
  `shared/canban-product-multimodal-knowledge-base`, solely to retrieve and compare product identity.
- poster/material: signed promotional-contract image, invoice or receipt image, settlement-form
  image, and every watermarked finished-product field photo. POS sales and unrelated evidence are
  excluded from this visual pass.
- other expense: every deterministically bound promotional contract, settlement, supporting
  agreement/document, activity/POS proof, invoice/receipt, and special-approval candidate. The model
  extracts visible facts only; it does not decide classification, approve a new type, or calculate an
  approved amount.
- maintenance fee: every deterministically bound stamped-POS visual, signed promotional contract,
  settlement, fee-specific support, and activity photo. The POS spreadsheet is excluded from vision;
  deterministic Python reads it and compares its rows and totals with the stamped POS facts.

Product-reference views are not field evidence. They may resolve a catalog product name,
product code, and 69 code, but must never establish a store, date, display, promotion, price,
photo uniqueness, or reimbursement decision and must never be returned as submitted photo files.

Never give the model a sales Excel or access to repository inputs, prior results, caches, history, or the acceptance workbook. AI must not calculate amounts, select Excel product names, or make reimbursement decisions.

Deterministic Python must safely unpack and route ZIPs, read Excel cells, preserve original source names and rows, aggregate quantities, map products, calculate differences and supported amounts, detect duplicate images, validate results, and publish the one canonical self-contained HTML. A temporary workbook or other intermediate representation may exist only inside the run-scoped temporary area for deterministic rendering and verification; it is never a delivery file.

Across every scenario and every product-to-product comparison, a product name never has to be
character-for-character equal. Exact equality is only a score-1 special case; a unique, specification-
compatible fuzzy name is sufficient. When comparable business sources provide both a 69 code and a
product-code field, those two identifiers remain strict and must agree exactly; once they agree, a
differently written product name is shown as fuzzy-compatible and must never create a separate
`商品名称：不匹配` error. If the name is absent or illegible but strict identifiers and independent
transaction facts uniquely locate the row, record it only as unavailable auxiliary text; it must not
affect status, confidence, problem counts, or resubmission. A catalog packaging/code alias such as `SP-4` may support the fuzzy-name field when
it is visibly written as part of a product description, but it never substitutes for or rewrites a
strict business-file product-code field.

When one exact product-code-plus-69-code pair maps to several knowledge-base variants, a uniquely
fuzzy-compatible source name may disambiguate them. For every detected row in a dense contract-PDF
sales attachment, the formal vision run must perform a second, original-resolution cell pass dedicated
only to that row's printed product-code, product-name, and 69-code cells. Deterministic preparation must remove
broad scan whitespace, derive all four orientations, and provide the two lossless landscape reading
directions plus overlapping row bands. For bands crossed by a red seal, also provide a same-pixel
black-print crop that suppresses saturated seal color and enlarges the product-code/name/69-code cells; vision
chooses the upright print and confirms seal-covered characters across both views.
First-pass quantity, price, and amount may locate the row but must never supply or infer those three cells, and sales Excel or catalog data
must never be shown to this pass. Preserve identical page/line order, require every nonempty reread 69 code to pass EAN-13 validation, and retry when a visibly populated
dense row still returns a missing product code, name, or 69 code. Apply only non-empty second-pass readings and
revalidate the complete contract evidence before deterministic matching. Never turn an OCR omission
into a product-code mismatch.

For `personnel_incentive`, deterministic code must reconcile every aggregated sales-Excel SKU
through the same validated repository product knowledge base before it can support an incentive
amount. The personnel Excel has no product-code field: its valid EAN-13/69 code must exactly match
the catalog, and its original product name needs only one strong, unique fuzzy-compatible match
within only that exact-barcode candidate set. Same-code ambiguity, an invalid/unregistered 69 code,
or an incompatible name fails the product gate. Preserve the diagnostic mapping but cap the affected
SKU's supported reward at zero. Catalog identity comes from code; product-reference images are not
sent to the personnel vision pass and are not settlement or transfer evidence.

When a personnel settlement line has no visible barcode, an exact quantity that occurs in exactly one
still-unused, knowledge-passed Excel SKU is a deterministic medium-confidence route under the
one-line/one-barcode constraint. Settlement product-name OCR is fuzzy auxiliary evidence only: a
shortened, misspelled, or corrupted name cannot reject that unique route. When quantity is duplicated,
the name must still uniquely disambiguate; otherwise leave the line unresolved.

For `promotional_display`, the contract PDF is the authoritative business-audit file. Audit its six
mandatory controls separately: contracting party, activity budget, execution period, activity
content, reimbursement/settlement method, and seal. Watermark visibility may be retained only as an
informational fact; it must never affect status, confidence, amount, or resubmission. The validated repository product
knowledge base is authoritative only for product identity. Deterministic code must reconcile every
concrete product in the core contract and every printed contract sales-attachment row to that ledger.
A supplied valid EAN-13 must equal the catalog 69 code; source-local business product codes are
preserved and compared strictly between the contract attachment and sales Excel. In product-to-
knowledge reconciliation the same source code must exactly equal the selected catalog product's
`product_code` or `product_code_aliases`; an unrelated main code is acceptable only when the source
code is registered as an exact alias. Product code and 69 code must jointly identify the same product.
The product name is fuzzy auxiliary evidence and never
requires exact equality. Registered packaging aliases never make a strict contract, attachment,
settlement, or sales-file product-code field pass, although they may be fuzzy-name anchors in a
description field. A contract
product that cannot map uniquely, contains conflicting identifiers, has an invalid 69 code, or lacks
the identity needed for a unique match fails its contract gate. Raw contract values remain immutable
and must never be silently normalized into a pass.

Keep the core contract-product condition and the printed sales attachment semantically separate.
Only an explicit core term can populate `requires_specific_products` and become a contractual SKU
condition. `contract.sales_attachment` is a page- and row-preserving transcript of an appended sales
table; it never creates a core product requirement or promotion requirement, but every printed row is
still a contract-side product identity that must be reconciled to the knowledge base. Printed
attachment totals are transcribed when present but are never reconstructed by model arithmetic. When
no row-level attachment exists, the standalone sales Excel has no contractual row-level baseline:
mark that reconciliation unverifiable, require the complete contract attachment, and block automatic
reimbursement rather than substituting photos or the knowledge base.

Product names never require character-for-character equality; unique fuzzy compatibility is sufficient
for contract-product identity and contract-attachment-to-Excel name comparison. Supplied business-
file product codes and supplied 69 codes remain strict. A field photo does not need to show a full
product name or 69 code. First extract useful text from that photo, then use only that text to retrieve
a small candidate set from the full validated knowledge base, correspond the visible text to catalog-
controlled text, and compare the packaging with registered reference views. A visible catalog-unique
packaging alias such as `SP-1`, or another uniquely convergent combination of partial name,
specification, variant, and bundle text, may establish the text correspondence. `3+2`, `420g`, and
`量贩装` together may retrieve the registered `3+2` bundle. An exact result requires broad visual
compatibility with at least one registered multi-view reference, not pixel identity: angle, distance,
lighting, shelf occlusion, and package pose may differ when the core layout, color blocks, bundle
structure, and recognizable features agree without conflict. Contract membership is checked
separately after the photo identity is fixed. A fuzzy photo identity remains unresolved; neither
contract wording, sales Excel, nor another business source may promote it to exact.

For personnel output, a unique fuzzy product-name match is an accepted medium-confidence match, not
a resubmission reason by itself. When the 69 code is exact, the knowledge item is uniquely resolved,
and quantity and reward agree, show `置信度：中` with `无需重新提交`; request a clearer
settlement line only when the mapping itself remains low-confidence, unmatched, or ambiguous.

The display chain is contract-led and directional:

1. contract core and contract sales attachment → product knowledge base, once, for their own product identity;
2. field-photo visible text → bounded candidates from the full validated product knowledge base →
   registered reference-image comparison; then independently compare the resolved photo product with
   contract scope and the remaining photo facts with contract terms;
   after that complete pass, run a focused display-standard review with submitted photos and immutable
   store/photo routing only. It replaces only `display_observation`: three front-facing packages plus
   the exposed side panel of the third package is still three, while a separately bounded adjacent
   stack of additional packages counts as another column even if narrow or side-facing. Different
   submitted-brand SKUs and package formats may jointly form four columns; evaluate multiple photos
   independently, letting any one photo prove four but never summing partial counts or counting
   unrelated neighboring brands. A flush run of side faces beside three front boxes remains three
   without seams, offsets, or another independent package face. Apply accepted visual regression
   calibrations only when the contract store and ordered routed-photo SHA-256 values match exactly;
   any content or route change disables calibration. Revalidate the
   complete photo evidence after applying the focused result;
   poster/material quantity regressions use a separate registry and require the complete ordered
   field-photo basename and SHA-256 sequence to match exactly. They may replace only declared aggregate
   material-unit and contributing-photo counts; any byte, name, membership, or order change disables
   them. User-verified difficult visual-document amounts use a third registry and require the complete
   ordered contract, invoice/receipt, and settlement basename plus SHA-256 sequence to match exactly;
   it may replace only the three declared amount fields. Any byte, name, membership, or order change
   disables it. No calibration registry may enter a model prompt or customer page;
3. standalone sales Excel → contract sales attachment, row by row across customer name, business
   date, product code, product name, barcode, quantity, retail price, and total amount. Preserve each
   source's unit for display, but never use unit to pair rows, pass/fail a row, set confidence, or ask
   for resubmission.

The standalone sales Excel is never reconciled to the product knowledge base. Field photos are never
reconciled to the standalone sales Excel, and Excel may not help identify a photographed product. The
attachment and standalone Excel have no authoritative store column, so they must never be assigned to
a photographed store or presented as proof that a particular store sold a product. Store/date/
display/promotion evidence comes from the contract and field photos. Amount is automatic only for an
explicit per-store or per-stack unit basis; a total-only or unclear contract is never divided
automatically.

Never approve an amount only because totals match. Preserve source archive hashes, raw paths, Excel rows, contract or settlement lines, visible limitations, and per-item/per-store evidence.

For `poster_material`, close the chain `signed contract → invoice/receipt → stamped settlement →
watermarked finished-product photos`. Contract-referenced attachments are mandatory. A generic
ticket line such as `物料制作` does not satisfy itemized expense evidence when the contract lists
multiple materials. Photo evidence must visibly cover activity-period date, shooting time, location,
finished content, dimensions, placement, and the contracted unit/store quantity; never extrapolate
from samples. The customer-facing subinterface lists only grouped blocking errors, affected source
filenames, recognized facts, impact, confidence, and exact resubmission action. Passing controls and
unrelated files stay out of that list.

For `other_expense`, first compare every visible fee description with the established categories:
赠品、陈列堆头、人员激励、海报/物料制作、维护费用、补差、搭赠、POS达标激励 and 进场费.
The ZIP marker `其他` is routing only. A recognizable existing type must be reclassified; a genuinely
new type must have independent special approval naming the new type, approver, approval statement,
date, and approval mark. Then require a signed promotional contract, company-template settlement with
customer seal, and type-specific support. This special channel never produces an automatic approved
amount: preserve the settlement claim, keep the suggested amount at zero, and return an exact
classification/approval/resubmission action or `待人工核定`.

For `maintenance_fee`, keep the chain `dealer-stamped POS visual → POS electronic spreadsheet →
signed promotional contract → company-template dealer-stamped settlement → fee-specific support`.
The archive marker routes an otherwise distinguishable package but never proves fee nature. The
contract supplies activity scope, period, eligible POS scope, calculation method, rate, and ceiling;
deterministic code reads the electronic POS sheet, requires row/total correspondence with the stamped
POS data, and supports the settlement claim only when the contractual recalculation matches to RMB
0.01. Missing roles, another visible fee nature (including personnel incentive or direct-operation),
or an unreproducible amount blocks the full claim. `other_expense` remains a separate scenario.

## Output contract

Every Agent executing an audit must invoke the repository-bundled runner and no other production
entrypoint:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> [--scenario <scenario>]
```

An Agent must not call internal report/render functions as an alternative workflow, handwrite an
HTML result, copy a prior page, patch a generated page after the run, or rebuild the frontend from a
description or screenshot. The bundled runner is responsible for extraction, deterministic business
logic, rendering, verification, and atomic publication.

Publish exactly one formal delivery file per run:

`worktrees/<YYYYMMDD>-<producer-model>.html`

Do not publish an `.xlsx`, JSON sidecar, asset directory, or second scenario page. `producer-model`
must be supplied by the model that actually executes the run: Codex uses `codex`; another model uses
a clear safe label such as `qwen3.7`. `run-id` must begin with a valid `YYYYMMDD` business date; later
tracking text does not enter the delivery name. The first Codex result is
`20260818-codex.html`; same-date same-producer reruns become `20260818-codex-1.1.html`, then
`20260818-codex-1.2.html`. Never overwrite or refill a missing revision, and keep an independent
sequence for each producer. Do not create persistent run directories, Git run branches, repository
snapshots, caches, or another delivery location.

The HTML is the complete formal result. It must be one UTF-8, self-contained `file://` document with
all verified business data, CSS, JavaScript, and interaction state inline; it must require no server,
network, CDN, font download, sibling workbook, or separate asset. A temporary workbook may still be
created from empty state inside the run-scoped temporary directory as a deterministic data/rendering
intermediate. It must pass its internal verification, must never be exposed to the model, and must be
deleted before the run finishes. A failed run publishes nothing.

The one HTML is the fixed error-only desk plus one error list for every submitted scenario, not
separate files. Its home metrics are exactly `核销场景`, `错误总数`, `材料 / 结算错误`, and
`商品 / 门店错误`. Counts represent grouped customer actions, not internal failed fields. Passing
records, passing contract facts, the contract baseline, audit-process narration, and informational
watermark policy remain in deterministic evidence but are hidden from the customer view.

- personnel shows only failed product and settlement/payment rows;
- promotional display groups all contract-attachment knowledge failures into one expandable card,
  creates a separate sales card only for blocking strict-field differences, and then shows only stores
  with their own photo/date/location/display/product error. Never repeat upstream failures per store;
- poster/material shows only grouped blocking errors and preserves every affected photo basename and
  visible date/time/location, finished content, placement, and dimension limitation.

For promotional display, render evidence in this fixed order: contract core six controls; contract
core/attachment products → knowledge base; page- and line-preserving contract sales attachment;
photo-visible text → full validated knowledge base → retrieved reference images, followed separately by
resolved photo product → contract scope; standalone sales Excel → contract attachment;
amount and final action. Product code and valid 69 code are strict where supplied; only product names
may be uniquely fuzzy. Every attachment and Excel row is kept in source order with PDF page,
attachment line, Excel row, all nine source fields, and the eight actually checked field results;
unit remains visible as a source fact but is never described as matched or mismatched. If the contract has
no sales attachment, render the missing-baseline control as unverifiable and request a complete
contract PDF; do not use the knowledge base or photos as a fallback. Never use an attachment row to
create a core contract product or promotion condition. Never attach an attachment/Excel row to a
store: neither source has a store-authoritative column. Store performance remains grounded in the
contract and field photos.

Every product or relationship uses one overall `置信度：高/中/低`. In customer-facing output, show
product code and 69 code as exact match/not matched, and product name as fuzzy matched/unverifiable;
when strict comparable identifiers agree, never emit a separate product-name mismatch error;
when every required field passes, say `全部对应`. Never use vague `可以对应` wording or expose
similarity scores. Every blocking item must name the exact problem file basename(s), the exact
comparison/baseline file basename(s) when applicable, the observed value or missing field in each
source, and the causal reason the relationship cannot pass. Then tell the reader exactly which PDF
page, attachment line, Excel row/field, settlement line, transfer screenshot, or visible photo content
must be resubmitted. Never emit `A ↔ B 无法确认`, `Excel门店与收款人无法逐一确认`, or another
source-free shorthand. Never require the reader to consult audit JSON or expose candidate sets, hashes, product
IDs, convergence, RAG terminology, or model reasoning.

For a field-store mismatch, if the photo watermark location is legible but differs from the contract
store, label it `门店水印错误`, display both values and the affected photo basenames, and request a
photo with corrected watermark location. Do not call it unreadable or ask only for a clearer photo;
the filename cannot override a conflicting watermark. Reserve missing/unreadable wording for a photo
that truly lacks reliable visible location evidence.

The canonical interface bundle is rooted at
`skills/orchestrate-offline-audit/assets/canban-audit-shell.html` with the maintained
`error-only.css` and `error-only.js` components beside it. The production generator assembles and
hashes all three, then injects verified run data only through the designated data and integrity slots.
The published HTML inlines the components and has no sibling dependency. Every other assembled byte,
including template version, CSS, visible copy, layout, interactions, and button set, remains unchanged.
`audit_core.html_report.verify_html_report` must rebuild the bundle and verify its version, combined
style hash, and assembled-shell fingerprint before publication.

Future audit runs and ordinary feature/fix tasks must not restyle, regenerate, paraphrase, reorder, or
extend the canonical shell; they must not change its CSS, static DOM, visible copy, interaction model,
or add buttons. The shell may change only when the user explicitly asks for a frontend redesign. That
same redesign change must deliberately update the asset version, expected fingerprint/structural
verification, affected tests, and this output contract. Do not silently weaken or bypass the
verification to accept a changed shell.

Template `3.2.0` freezes the approved error-only delivery standard for every submitted supported scenario: white Canban header, cold-gray
grid, dark clipped rail, rejected-only hero, four error metrics, error-composition strip, wide scenario
queue rows, and complete error cards ordered as problem file, optional baseline, cause, and handling
action. The contract-product detail table is the only default-collapsed evidence disclosure. The only
controls are scenario tabs, queue-entry buttons, return-to-home buttons, that native disclosure, and
the floating back-to-top button. At 390 px the page has no horizontal overflow and the first scenario
entry stays in or immediately adjacent to the first viewport.

Legacy or acceptance workbooks under `worktrees/` are test-only, are not formal output, and must never
be read, copied, or used by runtime code or model prompts.

## Change verification

Run at least:

```powershell
py -3 -B -m unittest discover -s tests -v
py -3 -B -m compileall -q audit_core skills
```

Also validate all JSON files and Skill frontmatter, run the bundled formal command against the retained real ZIP inputs when the execution path changes, verify that only the canonical self-contained HTML is published and that its template version/static fingerprint pass, confirm every temporary workbook is removed, and finish with `git diff --check` and `git status --short`.

When a user designates an approved reference HTML, run
`skills/orchestrate-offline-audit/scripts/verify_delivery_standard.py` only after the formal runner has
published its candidate. The reference is acceptance-only: runtime code and model prompts must never
read or copy it. Do not finish until the exact approved shell/CSS and desktop pixels, rendered error
structure and business facts, candidate source-derived invariants, interactions, offline behavior,
and 390px responsive checks all pass.
