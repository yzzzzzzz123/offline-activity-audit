# Offline activity audit project

## Current registered scope

This repository audits ten offline-activity scenarios only:

- `personnel_incentive` through `skills/audit-personnel-incentive`;
- `promotional_display` through `skills/audit-promotional-display`;
- `poster_material` through `skills/audit-poster-material`.
- `other_expense` through `skills/audit-other-expense`.
- `maintenance_fee` through `skills/audit-maintenance-fee`.
- `giveaway_promotion` through `skills/audit-giveaway-promotion`.
- `price_difference_support` through `skills/audit-price-difference-support`.
- `pos_target_incentive` through `skills/audit-pos-target-incentive`.
- `entry_fee` through `skills/audit-entry-fee`.
- `self_procured_gift_material` through `skills/audit-self-procured-gift-material`.

Personnel and promotional-display product identity share the project-level catalog at
`shared/canban-product-multimodal-knowledge-base`. No scenario Skill owns a private copy.

The only formal audit runner is:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> [--model <audit-model>] [--reasoning-effort <level>] [--scenario <scenario>]
```

`input/` must contain one to ten ZIP files. Each supported scenario may appear at most once. Reject unknown, ambiguous, duplicate-type, or unsafe archives with a specific error. A maintenance-fee package whose marker and POS/settlement structure are unambiguous may continue to audit so missing mandatory roles become customer-facing blocking issues; ambiguous duplicate singleton roles still stop intake. A marked all-visual extra-giveaway package may similarly continue so generic camera filenames are classified from visible content and missing roles become blocking report issues; duplicate contract, settlement, or delivery candidates stop evidence acceptance. A marked price-difference package accepts zero or one POS Excel so a missing electronic sheet becomes a blocking report issue, while singleton-role ambiguity still stops intake. A marked POS-target-incentive package likewise accepts missing singleton roles so the contract/activity-proof gaps remain reportable. A marked entry-fee package requires one unique contract and one safely extractable shelf-photo RAR; missing system deduction proof remains a blocking audit issue. A marked self-procured-gift-material package requires one legacy activity-return `.xls`, extracts every embedded DISPIMG photo for visual review, and never treats that workbook as the POS electronic spreadsheet. `--scenario` may select one submitted type for a scenario-only formal result.

The authenticated `POST /api/intake/oss` endpoint is a transport adapter, not another audit pipeline. Its production request contract is exactly `verifyCode`, positive-integer `fileId`, and the temporary pre-signed HTTPS `downloadUrl`; the stable internal idempotency identity is the unchanged `verifyCode:fileId` pair. Keep the signed URL in memory only, persist the verified original archive under `input-oss/<job_id>/`, and invoke the bundled `skills/orchestrate-offline-audit/scripts/run.py` in a child process with that job directory as `--input-dir`. After the formal run and its worktree snapshot are durably complete, POST exactly `verifyCode`, `fileId`, and a deterministic Chinese `result` string to the configured full HTTPS `/api/v1/ai/analyze/callback` URL. Callback retry must retry delivery only and must never download again or rerun AI; a callback failure retains the completed worktree and is recorded separately as `callback_failed`. Preserve the same safe archive routing, six-stage observer flow, AI/deterministic trust boundary, worktree layout, and no-overwrite rule as a manual run. Never copy an OSS object into the shared repository `input/`, flatten different jobs into one directory, persist its signed URL or callback token, accept an arbitrary/non-allowlisted download host, return `downloadUrl` in the callback, or implement business analysis in the HTTP handler. Job receipts live only under hidden `worktrees/.intake/jobs/`, and the visible catalog continues to list formal worktrees only. Verified OSS ZIPs remain in `input-oss` after completion or failure; partial or transport-invalid downloads are removed, while extracted sources and all other run intermediates remain temporary.

## New scenario onboarding

When the user supplies one representative ZIP plus a business prompt and asks to create another
audit scenario, use `skills/create-offline-audit-scenario/SKILL.md`. This is a development workflow,
not a formal audit run. First decide whether the request is a new reusable evidence/decision chain or
only another case of one of the ten registered scenarios. Reuse or update the existing scenario
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
- entry fee: the signed entry contract/product-promotion agreement, every safely extracted shelf
  photo, and every explicit system-deduction proof. Folder/store hints are routing-only and never
  establish a visible store, location, date, time, product, or activity.

Product-reference views are not field evidence. They may resolve a catalog product name,
product code, and 69 code, but must never establish a store, date, display, promotion, price,
photo uniqueness, or reimbursement decision and must never be returned as submitted photo files.

Never give the model a sales Excel or access to repository inputs, prior results, caches, history, or the acceptance workbook. AI must not calculate amounts, select Excel product names, or make reimbursement decisions.

Deterministic Python must safely unpack and route ZIPs, read Excel cells, preserve original source names and rows, aggregate quantities, map products, calculate differences and supported amounts, detect duplicate images, validate results, and publish a versioned run snapshot into `worktrees/`. The only customer-facing page is the persistent root `offline-activity-audit.html`, served by the trusted local workbench service. A temporary workbook or assembled legacy projection may exist only inside the run-scoped temporary area for deterministic rendering and verification; neither is a delivery file.

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
赠品、陈列堆头、人员激励、海报/物料制作、维护费用、补差、额外搭赠、POS达标激励 and 进场费.
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

For `giveaway_promotion`, keep the chain `dealer-executed extra-giveaway contract → dealer-stamped
settlement → system sales/delivery statement → store receipts → activity photos`. Keep the normal
shipment amount separate from the extra-gift claim. Deterministic code must check dealer and store
identities, contract period, eligible purchased and gift products, explicit buy/gift ratio,
zero-value gift rows on each receipt, system shipment total, gift quantities and explicit unit value,
contract budget, stamped claim, and exact duplicate images. Product names may be uniquely fuzzy and
specification-compatible; comparable product codes and 69 codes remain strict. A receipt cannot
replace the required activity photo. Missing roles or any failed control holds the full gift claim.

For `price_difference_support`, keep the chain `signed promotional contract → dealer-stamped POS
visual ↔ POS electronic spreadsheet → company-template dealer-stamped settlement → every-store
activity-price photos`. Treat original price, activity price, and the contract support unit amount as
three distinct facts; never derive the reimbursement unit from the retail price reduction. Every POS
or contract store needs its own in-period photo with visible date, address, shooting time, and activity
price. Calculate eligible POS quantity × contract support unit, capped by contract quantity and budget;
any missing role, incomplete store coverage, or failed reconciliation holds the full claim.

For `pos_target_incentive`, the incentive recipient is the dealer. Keep the chain `signed contract →
approved strategic/special-channel eligibility and target tiers → dealer-stamped POS visual ↔ POS
electronic spreadsheet → full-reduction activity proof → dealer-stamped company-template settlement`.
The settlement cannot establish missing contract rates. Deterministic code selects the highest reached
contract tier, calculates eligible POS × rate, applies the contract cap, and compares the claim. Missing
contract or activity proof holds the full claim even when POS and settlement arithmetic appear correct.

For `entry_fee`, keep the chain `both-party signed entry contract → contract product/barcode and store
scope → watermarked shelf photos → system deduction proof`. The contract is the authority for the fee
per product barcode and total. A product row's store count is a coverage requirement and must not be
multiplied into a barcode fee unless the contract explicitly states per-store charging. Photos must be
matched from their own visible watermarks, not folder names; every contract product must be visibly
shelved across the required contract stores when the contract limits support to actual shelving.
Missing system deduction proof, incomplete store/product coverage, or a failed contract calculation
holds the full claim.

## Output contract

Every Agent executing an audit must invoke the repository-bundled runner and no other production
entrypoint:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> [--model <audit-model>] [--reasoning-effort <level>] [--scenario <scenario>]
```

An Agent must not call internal report/render functions as an alternative workflow, handwrite a run
result page, copy a prior page, patch the persistent root page during a run, or rebuild the frontend
from a description or screenshot. The bundled runner is responsible for extraction, deterministic
business logic, verification, append-only event logging, DOM-data checkpoints, and atomic snapshot
publication.

The repository contains exactly one customer-facing HTML entrypoint:

`offline-activity-audit.html`

It is a persistent, versioned two-level system shell and is never regenerated or filled on disk by an
ordinary audit run. The trusted local service serves it at `http://127.0.0.1:8080/` and on the
machine's approved LAN address while exposing read-only result APIs, the narrow same-origin
manual-review marker mutation, and the separately authenticated OSS intake endpoint. It must not use a CDN,
downloaded font, third-party script, or remote business-data dependency.

Each formal run publishes one persistent run directory instead of another HTML:

`worktrees/<YYYYMMDD_HHMM_SS>-<audit-model>_<reasoning-effort>/`

The directory contains `manifest.json`, an atomic `snapshot.json`, append-only
`logs/events.jsonl`, observable `logs/run.log`, schema-validated files under `analysis/`, and replayable
data checkpoints under `dom/checkpoints/`. It must not contain a generated customer HTML or a
published workbook. `producer-model` remains required provenance, while the path suffix records the
actual audit model and primary reasoning effort. `run-id` must begin with a valid `YYYYMMDD` business
date; append the local task-start time as `_HHMM_SS`. Compact safe model labels by removing separators
such as the hyphens in `gpt-5.6-sol`, so examples include
`20260902_1755_32-gpt5.6sol_xhigh/` and `20260902_1755_32-qwen3.8_max/`. Never use `-1.1`,
`-1.2`, or another revision suffix for new worktrees and never overwrite an existing exact name.
Existing legacy HTML files and old-name directories retain their historical IDs and remain read-only
compatibility inputs for the workbench catalog.

The fixed page has a one-to-many, two-level information architecture. `/` is the level-one audit
management system with overview, complete run ledger, and technical archives. The overview contains
aggregate metrics and recent run records only; it must not render a runtime chain, stage nodes, or an
observable event stream.
Every catalog entry expands through `/?run=<workspace-id>` into its own level-two record. A completed
record must use the approved original error-desk interface and show all of that run's grouped customer
errors in one continuously stacked list. It must not classify errors into scenario queues or require a
second click to enter a scenario; every error card carries one small Chinese audit-type label;
a running or failed record gets a status/diagnostic level-two page and must not impersonate a completed
business result. Level-two pages return to the level-one ledger and may switch directly to another run.
Counts inside the completed error desk represent grouped customer actions, not internal failed fields.
Passing records and passing contract facts remain hidden from the default error overview, but each
completed record also has a sibling `正确检查项` ledger. It may expose only independently passed
subchecks projected from the same deterministic result, grouped by audit type and labeled with a
Chinese category, check title, subject, concise basis, source basenames, and confidence. A passed
subcheck never changes the record conclusion or suppresses a blocking error. Contract baselines,
audit-process narration, and informational watermark policy remain outside both customer result
views. Explicit technical archives may expose schema-validated evidence, deterministic
result objects, verification receipts, observable events, and DOM-data checkpoints; they must never
expose model-private reasoning or treat an AI narrative as decision authority.

A temporary workbook and assembled legacy projection may be created from empty state inside a
run-scoped system temporary directory solely to shape and verify the customer view payload. They must
never be exposed to the model or published, and must be deleted before the run finishes. A failed run
keeps its manifest, safe logs, analysis already validated, and failure checkpoint for diagnosis, but
must not claim a completed business result.

- personnel shows only failed product and settlement/payment rows;
- promotional display groups all contract-attachment knowledge failures into one expandable card,
  creates a separate sales card only for blocking strict-field differences, and then shows only stores
  with their own photo/date/location/display/product error. Never repeat upstream failures per store;
- poster/material shows only grouped blocking errors and preserves every affected photo basename and
  visible date/time/location, finished content, placement, and dimension limitation.
- entry fee shows only grouped contract-authority, fee-calculation, store/product shelf-photo,
  system-deduction, and source-integrity errors.

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

When a field-store name fully corresponds to the visible watermark location, pass the location
directly without a map call or a location-confidence issue. For every legible name difference, preserve
the contract store, visible watermark location, and affected photo basenames, then run the configured
fixed Baidu Maps MCP resolver through `map_search_places`; persist returned coordinates as `BD09LL`.
Read `BAIDU_MAPS_API_KEY` from the current process first and, on Windows, from the current user's
environment configuration second so routine runs need no separate interactive terminal. The OS user
configuration is the only allowed persistent secret location; never copy the AK into a repository file,
command-line argument, worktree, result, log, or displayed authenticated URL.
A selected same POI, a verified mall/store parent-child relationship, or two selected
POIs no more than 100 metres apart is `compatible` with high location confidence. Search results do
not need to be independently unique: preserve any credibly unique side, and require every unresolved
side's selected candidate to meet the internal 0.60 name-relevance threshold; when both sides are
unresolved, both selected candidates must meet it. Accept the best such pair when it forms one of those
passing spatial relationships. A distant pair, the 100-to-300-metre gray zone, no credible
candidate pair, an unavailable MCP, or missing comparable coordinates is `location_unverified` with
low location confidence: hold automatic settlement and show
`门店地点低置信度`, never an automatic wrong-watermark finding. Preserve a distant pair's measured
distance and request either authoritative same/nearby evidence or corrected-location content. Keep
legacy `mismatch` readable but do not emit it for new determinations. If MCP is unavailable or
inconclusive, an entry in the schema-validated verified location registry may supply the relationship;
its publisher, checked date, address facts, and basis must remain in the result and six-column ledger.
A conflict between MCP and the registry is low-confidence and always goes to manual review. Filenames
never prove or override a location, and a truly missing/unreadable watermark remains a separate
evidence gap.

The canonical customer interface is the root `offline-activity-audit.html`, currently Audit System
`2.7.1`. Its level-one system, approved original level-two audit desk, copy, layout,
customer/technical separation, run-history behavior, relative API contract, responsive behavior, and
controls remain unchanged during an ordinary audit. It reads only the trusted service's `/api/config`,
`/api/runs`, run `snapshot`, `log`, `events`, approved `analysis`, and `checkpoints` resources. The
trusted server injects the selected run payload into the fixed HTML response for `/?run=<workspace-id>`
without modifying the file on disk. Direct `file://` opening redirects to the loopback service; LAN
clients use the same relative endpoints. Future audit runs must never rewrite this file.

The older bundle under `skills/orchestrate-offline-audit/assets/` remains a deterministic, run-scoped
view-payload compiler and verifier. `audit_core.html_report` may assemble it only in system temporary
space to prove the six-column projection, extract the verified `audit-data` payload, and then delete the
temporary HTML. It is not the customer entrypoint and must never be published into `worktrees/`.

Future ordinary feature/fix tasks must not restyle or extend the root system or its approved original
record view. They may change only when the user explicitly requests a frontend redesign or system
behavior change. That same change must update the system version, workbench API contract, affected
tests, PC browser verification at 1440×960, this output contract, the orchestration Skill, and
README; never weaken verification to accept a changed page.

Audit System `2.7.1` freezes the persistent audit-ledger standard: a level-one management center with
system overview, full audit ledger, and technical archive. The overview contains only aggregate metrics
and recent run records. It does not show a runtime chain, stage nodes, or observable events, and its
silent probe reads only `/api/runs`; visible overview content updates when the run-catalog signature
changes. The system-overview `核销完成` metric shows the top-level input ZIP count from the newest
completed run, not the cumulative number of completed run records. The level-one aggregate metric and
completed-run count label present `error_count` as `待人工核验` only while a completed record is not
manually reviewed; reviewed records contribute zero to that metric while the underlying count and
level-two disposition content remain unchanged. The canonical
six-task main-flow checklist comes from
`audit_core.workbench_store.main_flow_task_list`, is persisted in every new manifest, and is exposed by
the read-only workbench context and `/api/config` for technical contracts and running-record refresh;
the frontend must not maintain a differently ordered stage list. Multi-scenario execution is stage-
batched: every analysis event precedes every evidence validation event, every evidence event precedes
every deterministic decision event, and verification comes last, so the run-level stage index never
regresses. A running level-two record may probe silently, but visible content updates only when the
selected workspace, terminal status, or stage progress index changes. Events within the same stage
never reload that record, and a running level-two record never reloads on a fixed timer. A full-page
reload always opens system overview; the ledger state saved before opening a level-two record is
consumed only once when returning. Module actions never cross-nest: overview summaries have no record
buttons, ledger rows only open the record, and technical-archive cards only open logs and checkpoints.
The system also has one approved original audit-desk level-two page per completed run and a truthful
diagnostic level-two page for incomplete runs. The record view retains its dark rail and cold-gray grid
canvas. The rail has exactly two sibling result views: the default red-accented error overview retains
its four grouped-error metrics and stacked error cards; the green-accented correct-check ledger groups
all independently passed subchecks by audit type and then by Chinese business category. Each pass entry
shows the checked subject, concise deterministic basis, available source basenames, and confidence.
Whole-record caveats and filter-behavior explanations remain internal rules and are not rendered as
auxiliary customer-interface copy. Both result views offer aligned client-side combined filters. Audit
type always lists every type present in its view and is never narrowed by another condition. The error
overview additionally filters by `错误原因分类`, confidence, and free text; its categories use actionable
fine-grained labels such as `陈列标准`, `活动日期`, `门店地点低置信度`, `金额复算`, and
`POS销售明细`, never broad umbrella labels. The pass ledger filters by Chinese check category,
confidence, and free text. Error-reason/check category and confidence are linked
facets: omit zero-result options under the other current conditions and show a live result count beside
every remaining option. Both views report the overall live match count and reset without mutating the
snapshot. All grouped errors remain directly below the error summary modules. Each card header carries
only its `核销类型 · <业务类型>` and confidence badges. One or more category-value-only chips appear
inside the error-reason field without an `错误原因分类` prefix and use the exact same `eo-chip` styling as
`陈列标准`; there is no separate light category-chip treatment.
When a view has exactly one audit type, its enabled audit-type select contains only that concrete type
and omits the `全部核销方式` option; multi-type views retain the all option. Card audit-type values and
facet audit-type values must use the same canonical business labels. Every error-reason category must
belong to at least one card with a concrete audit type, and the client rejects orphan type labels.
Both sibling views use the same judgment-confidence presentation. An explicit AI `confidence_score`
in `[0,1]` takes precedence and is bucketed as high at `>=0.85`, medium at `>=0.60`, otherwise low.
Without an explicit score, every level receives a bounded score from the concrete judgment context
(specific numeric/location evidence, corroboration, ambiguity, and missing material), rather than a
constant `0.5` or an automatic high-confidence `1`. Only an explicit fully-certain judgment may use
`1`. Cards always render both level and score, including `置信度 高：1`; confidence filter options
contain only high/medium/low and never numeric scores. This is confidence that the item-level judgment
is accurate, not error severity or an internal retrieval/name-relevance score.
There are no scenario queues, scenario-entry buttons, or per-type subpages in either sibling view.
The workbench API contract is `1.7`: a completed snapshot's `view.pass_check_log` uses schema `1.0`
with `total`, type/scope counts, and ordered `groups[].items[]`; the server reconstructs this projection
from immutable `analysis/results/<scenario>.json` for older archives without rewriting their worktrees.
Error-reason classification is a deterministic client projection from the same immutable error rows and
does not add or mutate an API business field. Completed ledger records expose a mutable
`manual_reviewed` annotation through `POST /api/runs/<workspace-id>/manual-review`; store it atomically
under `worktrees/.reviews/`, never rewrite the business manifest/snapshot, and exclude reviewed records'
error counts from the level-one pending-manual-review metric. Unchecking restores the pending count.
Formal frontend delivery is PC-only: verify at 1440×960
and keep desktop widths of 1280px or greater usable. Narrow-screen CSS is best-effort fallback, not a
mobile configuration or acceptance promise.
Customer output must translate engineering enums and keys into business-readable Chinese; technical
JSON stays inside explicitly labeled technical views.

Legacy HTML files already under `worktrees/` are read-only compatibility results and may be projected by
the trusted server without modification. Legacy or acceptance workbooks remain test-only and must never
be read, copied, or used by runtime code or model prompts.

## Change verification

Run at least:

```powershell
py -3 -B -m unittest discover -s tests -v
py -3 -B -m compileall -q audit_core skills
```

Also validate all JSON files and Skill frontmatter, run the bundled formal command against retained real
ZIP inputs when the execution path changes, verify that no per-run HTML or workbook is published, every
temporary workbook/page/source tree is removed, and the worktree contains an atomic manifest/snapshot,
append-only events, safe observable log, analysis index, and DOM-data checkpoints. Start the trusted
service on loopback and `0.0.0.0`, exercise the relative APIs and SSE, verify OSS intake authentication,
three-field idempotency, allowlisted URL and response-integrity validation, status polling, per-job
`input-oss` persistence without overwrite, partial-download cleanup, exact callback payload, callback
failure retention, and delegation to the bundled
runner, and verify the fixed workbench at
1440×960 desktop with zero console/page errors before finishing with `git diff --check` and
`git status --short`.

When a user designates an approved reference HTML, use it only for explicit workbench redesign
acceptance. Runtime code, model prompts, evidence extraction, and run snapshots must never read or copy
it. Compare the root fixed page after the local service loads a source-derived test snapshot; do not
compare or patch a generated per-run page.
