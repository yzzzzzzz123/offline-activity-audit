---
name: orchestrate-offline-audit
description: Generate one verified self-contained Canban offline-activity reimbursement HTML from one or two ZIP submissions, covering personnel incentives, promotional/stack displays, or both. Use whenever input/ contains new offline audit materials and Codex must safely classify them, extract only visual facts with AI, deterministically read sales Excel, close product/store/amount controls, and publish the canonical local interface without using prior outputs or a gold workbook at runtime.
---

# Orchestrate Offline Audit

Use this Skill as the only formal entry for this project:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```

Every Agent executing an audit must call this bundled runner. Do not call internal report/render
functions as a substitute, handwrite the final HTML, copy or patch a prior result, rebuild the frontend,
or introduce a second CLI, Git run branch, persistent snapshot, separate delivery directory,
template-copy step, or post-run replacement workflow. The runner publishes the one formal HTML.

## Input contract

Read only ZIP files directly inside `<project>/input/`. Require one or two ZIPs. Read [routing-rules.md](references/routing-rules.md) before changing classification or extraction.

- A personnel-incentive ZIP contains exactly one sales Excel, no PDF, exactly one identifiable settlement image, and at least one transfer/red-packet screenshot.
- A promotional-display ZIP contains exactly one contract PDF, exactly one sales Excel, and at least one field photo.
- Accept either type alone or one of each. Reject zero ZIPs, more than two ZIPs, two ZIPs of the same type, unknown type, ambiguous roles, or missing required material with a specific error.
- Reject unsafe ZIP paths, links, encryption, duplicate/case-colliding paths, excessive expansion, suspicious compression ratios, and duplicate image basenames.

Extract only into a run-scoped temporary directory that is removed automatically. Do not create `input/.prepared`, `.audit-tmp`, a cache, or a persistent run directory.

## Trust boundary

Keep vision and judgment separate because source facts and audit decisions have different reliability requirements.

AI may act only as the eyes:

- personnel: read the settlement image and transfer screenshots;
- display: read the contract PDF and field photos;
- return schema-valid JSON with visible text, dates, locations, packaging, display observations, transfer occurrences, limitations, and source basenames.

Do not give the AI a copy of the sales Excel. Instruct it not to read `input/`, `worktrees/`, caches, history, prior results, or the gold-standard workbook. The AI must not calculate supported amounts, choose Excel barcodes/names, or make the final reimbursement decision.

Deterministic Python owns all remaining work:

- safe ZIP validation and routing;
- direct Excel cell reading, original-name preservation, row aggregation, and store detail;
- personnel sales-SKU reconciliation through the validated product knowledge base: valid 69 code
  must match exactly, then the original product name must match exactly or uniquely fuzzily within
  that same-code candidate set; a failed product gate contributes no supported reward;
- display sales-row reconciliation with strict registered product code and valid 69 code plus exact
  or unique fuzzy product name; the same independent reconciliation for every printed contract
  sales-attachment row; conditional core-contract-product reconciliation; field-photo text retrieval
  followed by bounded reference-image comparison; a unique visible registered short code such as
  `SP-1` is enough without a full name or visible photo barcode; then compare the resolved catalog
  product's strict code and 69 code with both sales sources while allowing fuzzy names;
- image hashes and duplicate screening;
- dates, quantities, rewards, transfers, claims, supported amounts, and final pass/supplement decisions;
- result-schema validation, temporary intermediate generation/verification, canonical HTML rendering,
  naming, static-shell verification, and atomic publication.

## Routing and audit

Process scenarios in this fixed order when both exist:

1. `personnel_incentive` → read [audit-personnel-incentive](../audit-personnel-incentive/SKILL.md) and its linked rules/schema completely.
2. `promotional_display` → read [audit-promotional-display](../audit-promotional-display/SKILL.md) and its linked rules/schema completely.

Validate AI evidence before calculation and validate each deterministic result against
`contracts/audit-result.schema.json` before rendering. An aggregate match never substitutes for a
line/store control.

## Canonical HTML contract

The formal result is one self-contained UTF-8 HTML file. A temporary workbook may be created from an
empty `openpyxl.Workbook` only as a run-scoped deterministic intermediate for data shaping and
verification. Never load a prior workbook as a template, publish the temporary workbook, expose it to
the model, or leave it behind after success or failure. Legacy/acceptance workbooks are test-only.

The single HTML contains a main interface and the submitted scenario subinterfaces:

- the main interface shows run identity, overall conclusion, scenario entry points, counts, supported/
  held amounts, and the concrete materials that must be resubmitted;
- the personnel-incentive subinterface contains product reconciliation plus one settlement-and-payment
  area for totals, actual application, recipient, store correspondence, and complete transfer date;
- the promotional-display subinterface contains a campaign/core-contract overview, conditional
  contract sales-attachment detail, product correspondence, store/photo reconciliation, and campaign
  settlement. Contract-wide facts and final settlement stay outside the store list.

Personnel product records must show the original sales-Excel product, selected catalog product code/
name/69 code, exact barcode result, exact-or-unique-fuzzy name result, settlement quantity/reward,
transfer evidence, amount result, confidence, and concrete resubmission action. Use the selected
catalog product's complete authoritative name as the heading when uniquely resolved; do not add a
`结算第N行` prefix. A unique fuzzy name is an accepted medium-confidence match and is not by itself a
reason to resubmit when strict 69 code, quantity, reward, recipient/store correspondence, and complete
date pass.

Render `销售Excel + 商品知识库（代码核验）` as one six-column source-comparison table: 来源、商品名称、
商品编码、69码、数量 / 奖励、匹配结果. Put the Excel quantity and calculated reward in the shared
quantity/reward column, and put the name/barcode comparison and knowledge confidence in the two source
rows of the shared result column. The desktop table must wrap inside its panel without horizontal
scrolling; below 780 px it must become labeled source blocks, and below 520 px a single-column block.

Keep these promotional-display meanings separate:

- core contract terms establish parties, activity period, display/promotion obligations, fee basis,
  stores, and an optional specific-product condition;
- only an explicit core term may populate the optional contract-product condition;
- `contract.sales_attachment` is a page- and row-preserving transcript of an appended sales table. It
  never creates a contract product or promotion requirement. When absent, its dependent controls are
  conditionally not applicable and no empty attachment-detail table is rendered;
- field photos establish store/date/display/promotion facts and the visible product chain; repository
  product-reference views establish product identity only;
- the standalone sales Excel and contract attachment retain their own original customer/date/product/
  unit/quantity/price/amount values and source row/page. Neither source has a store-authoritative
  column, so neither may be assigned to a store or presented as proof of store-level sales.

Close the promotional-display product chain as four explicit links:

1. **Photo → knowledge base:** use visible packaging, a complete valid barcode, or a catalog-unique
   short code plus compatible packaging to establish the catalog product. A fuzzy photo identity makes
   strict downstream code/barcode checks unavailable rather than mismatched.
2. **Knowledge base → contract sales attachment:** when present, reconcile every attachment record
   independently. Supplied product code/registered alias and valid 69 code are strict; product name may
   be exact or uniquely fuzzy. Preserve printed order, attachment `line_no`, and `source_page`.
3. **Knowledge base → standalone sales Excel:** reconcile every original Excel row independently under
   the same strict-code/strict-69/fuzzy-name rule. A photo short code never locates or rewrites an
   Excel row, and an unregistered `020...` code fails even when name and barcode look plausible.
4. **Contract attachment → standalone sales Excel:** compare only rows that independently resolve to
   the same catalog product, then compare the original customer, business date, product fields, unit,
   quantity, retail price, and amount where both sources actually provide them. Do not infer a missing
   field, calculate an absent total, or convert this customer/activity-period evidence into store sales.

Show one product per row with the authoritative catalog identity once, followed by the source values,
source page/Excel row, field-level agreements/differences, and one overall `置信度：高/中/低`. Do not
truncate relevant rows, use a bare `未匹配`, or repeat identical standard identities in every source
column. When a source fails, name the exact PDF page, attachment line, Excel row/field, settlement line,
transfer screenshot, or photo content that must be resubmitted. Keep candidate sets, internal product
IDs, RAG wording, hashes, convergence, and model reasoning out of the customer interface.

Contract parties/customer, contract period/business date, and watermark/seal integrity remain global
controls. Product-reference views cannot prove a store, date, display, promotion, price, originality,
or amount. An ordinary price is not promotion. Promotion requires an explicit special price, old/new
price, discount, gift, multi-buy, `1+1`, `3+2`, or value-pack signal and is mandatory only when the
core contract requires it. Calculate amount only from an explicit per-store or per-stack unit basis;
never divide a total automatically.

## Frozen interface asset

The only canonical frontend asset is
`skills/orchestrate-offline-audit/assets/canban-audit-shell.html`. The production generator is
`audit_core.html_report.create_html_report_from_workbook`; it may replace only the asset's designated
verified-data and integrity-hash injection slots. Every other asset byte—including the fixed template
version marker, CSS, static DOM, visible
copy, layout, JavaScript interactions, and the exact button set—must be copied unchanged into every
result. The production verifier is `audit_core.html_report.verify_html_report`; it must reject a
template-version mismatch, static-shell fingerprint mismatch, external dependency, missing business
content, or unexpected control before publication.

Do not handwrite or redesign the page during an audit. Do not reproduce it from prose, a screenshot,
or prior output; do not patch generated HTML after the runner; and do not add a new button, style,
layout, label, or interaction for an ordinary business or bug-fix request. Future tasks must reuse the
canonical shell byte-for-byte outside the designated injection slots. The shell may change only when
the user explicitly requests a frontend redesign. The same redesign change must update the asset
version, expected fingerprint/structural verification, affected tests, and this Skill contract; never
weaken verification merely to accept a modified shell.

The generated page must remain one `file://`-openable document with all business data, CSS, and
JavaScript inline and no server, network, CDN, font download, sibling Excel, JSON sidecar, or asset
directory. Its fixed main/personnel/display navigation, search/filter/detail interactions, responsive
behavior, visible copy, and buttons come from the canonical asset and are not recreated by the Agent.

## Output and verification

Publish exactly one file directly to:

`worktrees/<YYYYMMDD>-<producer-model>.html`

Do not publish an `.xlsx`, JSON sidecar, second scenario page, or asset directory. `producer-model` is
required provenance supplied by the executing model: use `codex` for Codex and an explicit safe label
such as `qwen3.7` for another model. Require `run-id` to begin with a valid `YYYYMMDD` business date
and omit the rest of the run identity from the delivery filename. The first Codex result is
`20260818-codex.html`. Never overwrite: another result for the same date and producer becomes
`20260818-codex-1.1.html`, then `20260818-codex-1.2.html`; keep an independent monotonic sequence for
each producer and never refill a deleted revision.

Before publication, verify the deterministic business/result schemas, any temporary intermediate,
complete scenario/record coverage, UTF-8/self-contained output, canonical asset version, static-shell
fingerprint, fixed controls, and zero external dependencies. Confirm the final run publishes only the
HTML and removes every temporary workbook, extracted source, model workspace, and temporary page. A
failed run must leave no formal output.
