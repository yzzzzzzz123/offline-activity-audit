# Offline activity audit project

## Fixed scope

This repository audits two offline-activity scenarios only:

- `personnel_incentive` through `skills/audit-personnel-incentive`;
- `promotional_display` through `skills/audit-promotional-display`.

The only formal command is:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```

`input/` must contain one or two ZIP files. Each supported scenario may appear at most once. Reject unknown, ambiguous, duplicate-type, missing-material, or unsafe archives with a specific error.

## Trust boundary

Keep extraction and judgment separate. AI or vision steps may only extract visible facts into schema-validated JSON:

- personnel: settlement image and transfer/red-packet screenshots;
- display: contract PDF and field photos; the field-photo pass may additionally receive the
  repository-owned, hash-validated product-reference views under
  `skills/audit-promotional-display`, solely to retrieve and compare product identity.

Product-reference views are not field evidence. They may resolve a catalog product name,
product code, and 69 code, but must never establish a store, date, display, promotion, price,
photo uniqueness, or reimbursement decision and must never be returned as submitted photo files.

Never give the model a sales Excel or access to repository inputs, prior results, caches, history, or the acceptance workbook. AI must not calculate amounts, select Excel product names, or make reimbursement decisions.

Deterministic Python must safely unpack and route ZIPs, read Excel cells, preserve original source names and rows, aggregate quantities, map products, calculate differences and supported amounts, detect duplicate images, validate results, and publish the one canonical self-contained HTML. A temporary workbook or other intermediate representation may exist only inside the run-scoped temporary area for deterministic rendering and verification; it is never a delivery file.

For `personnel_incentive`, deterministic code must reconcile every aggregated sales-Excel SKU
through the same validated repository product knowledge base before it can support an incentive
amount. The personnel Excel has no product-code field: its valid EAN-13/69 code must exactly match
the catalog, and its original product name must match exactly or by one strong, unique fuzzy match
within only that exact-barcode candidate set. Same-code ambiguity, an invalid/unregistered 69 code,
or an incompatible name fails the product gate. Preserve the diagnostic mapping but cap the affected
SKU's supported reward at zero. Catalog identity comes from code; product-reference images are not
sent to the personnel vision pass and are not settlement or transfer evidence.

For `promotional_display`, the validated repository product knowledge base is the authoritative
product-identity ledger. Deterministic code must reconcile every standalone sales-Excel row and every
printed contract sales-attachment row separately by its source product code, product name, and 69
code: supplied product code and valid EAN-13 remain strict, while the product name may be exact or
uniquely fuzzy for that same strict product. It must also reconcile any specifically required product
from the core contract terms and resolve every field-photo product through the same catalog. A row
that cannot map to the catalog, maps ambiguously, contains conflicting identifiers, has an invalid
69 code, or lacks the identity needed for a unique match fails only its own source gate. An
unregistered external product code fails even when the name and barcode appear plausible. Raw source
values remain immutable and must never be silently normalized into a pass.

Keep the core contract-product condition and the printed sales attachment semantically separate.
Only an explicit core term can populate `requires_specific_products` and become a contractual SKU
condition. `contract.sales_attachment` is a page- and row-preserving transcript of an appended sales
table; it never creates a product requirement or promotion requirement. When no attachment exists,
the attachment-dependent controls are conditionally not applicable rather than failed. Printed
attachment totals are transcribed when present but are never reconstructed by model arithmetic.

Product names never require character-for-character equality; unique fuzzy compatibility is allowed
throughout. Supplied product codes/registered aliases and supplied 69 codes remain strict. A field
photo does not need to show a full product name or 69 code: a visible catalog-unique short code such
as `SP-1` can establish the product. Only after the photo product is exact may deterministic code use
that catalog product's registered 69 code for the sales comparison. A fuzzy photo identity makes the
69-code comparison unavailable, never mismatched merely because no barcode is visible.

For personnel output, a unique fuzzy product-name match is an accepted medium-confidence match, not
a resubmission reason by itself. When the 69 code is exact, the knowledge item is uniquely resolved,
and quantity and reward agree, show `置信度：中` with `无需重新提交`; request a clearer
settlement line only when the mapping itself remains low-confidence, unmatched, or ambiguous.

The display product chain must close four explicit links: field photo → product knowledge base,
knowledge base → contract sales attachment when present, knowledge base → standalone sales Excel,
and contract sales attachment → standalone sales Excel when present. The last link compares original
customer/date/product/quantity/price/amount fields only where both sources actually provide them; it
does not invent missing values or totals. The attachment and standalone Excel have no authoritative
store column, so both support only customer-level activity-period and product-sales evidence. They
must never be assigned to a photographed store or presented as proof that a particular store sold a
product. Store/date/display/promotion evidence still comes from the core contract and field photos.
Amount is automatic only for an explicit per-store or per-stack unit basis; a total-only or unclear
contract is never divided automatically.

Never approve an amount only because totals match. Preserve source archive hashes, raw paths, Excel rows, contract or settlement lines, visible limitations, and per-item/per-store evidence.

## Output contract

Every Agent executing an audit must invoke the repository-bundled runner and no other production
entrypoint:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
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

The one HTML contains a main reconciliation interface plus scenario subinterfaces, not separate
files:

- the main interface shows run identity, overall conclusion, scenario entry points, counts, amounts,
  and a plain-language list of materials that must be resubmitted;
- the personnel-incentive subinterface shows product reconciliation, the selected knowledge-base
  identity, settlement-image and transfer evidence, amount comparison, and one combined settlement-
  and-payment area for totals, actual application, recipient, store correspondence, and complete date;
- the promotional-display subinterface shows the campaign/core-contract overview, conditional sales-
  attachment detail, product correspondence, store/photo reconciliation, and campaign settlement.
  Contract-wide facts and final settlement remain outside the store list.

For promotional display, show the four product links without collapsing them into one unexplained
status: photo → knowledge base, knowledge base → contract sales attachment, knowledge base →
standalone sales Excel, and attachment → standalone sales Excel. Product code and valid 69 code are
strict where supplied; only product names may be uniquely fuzzy. Every relevant source row is kept in
printed/source order with its page or Excel row and concrete differing fields. If the contract has no
sales attachment, omit its detail table and mark only the two attachment-dependent controls
conditionally not applicable. Never use an attachment row to create a core contract product or
promotion condition. Never attach an attachment/Excel row to a store: neither source has a
store-authoritative column, so they support customer-level activity-period and product-sales evidence
only. Store performance remains grounded in the core contract and field photos.

Every product or relationship uses one overall `置信度：高/中/低`. Exact, fuzzy, or mismatch wording
belongs only in the short field-level explanation. Tell the reader exactly which PDF page, attachment
line, Excel row/field, settlement line, transfer screenshot, or visible photo content must be
resubmitted. Never require the reader to consult audit JSON or expose candidate sets, hashes, product
IDs, convergence, RAG terminology, or model reasoning.

The canonical interface asset is
`skills/orchestrate-offline-audit/assets/canban-audit-shell.html`. The production generator
`audit_core.html_report.create_html_report_from_workbook` may inject verified run data only through
the asset's designated data and integrity-hash slots. Every other template byte—including the fixed
template-version marker, CSS, DOM structure,
visible interface copy, layout, JavaScript interactions, and the exact button set—must be reused
unchanged. `audit_core.html_report.verify_html_report` must verify the canonical template version and
static-shell fingerprint before publication.

Future audit runs and ordinary feature/fix tasks must not restyle, regenerate, paraphrase, reorder, or
extend the canonical shell; they must not change its CSS, static DOM, visible copy, interaction model,
or add buttons. The shell may change only when the user explicitly asks for a frontend redesign. That
same redesign change must deliberately update the asset version, expected fingerprint/structural
verification, affected tests, and this output contract. Do not silently weaken or bypass the
verification to accept a changed shell.

Legacy or acceptance workbooks under `worktrees/` are test-only, are not formal output, and must never
be read, copied, or used by runtime code or model prompts.

## Change verification

Run at least:

```powershell
py -3 -B -m unittest discover -s tests -v
py -3 -B -m compileall -q audit_core skills
```

Also validate all JSON files and Skill frontmatter, run the bundled formal command against the retained real ZIP inputs when the execution path changes, verify that only the canonical self-contained HTML is published and that its template version/static fingerprint pass, confirm every temporary workbook is removed, and finish with `git diff --check` and `git status --short`.
