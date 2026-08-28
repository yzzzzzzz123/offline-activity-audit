---
name: orchestrate-offline-audit
description: Generate one verified self-contained Canban offline-activity reimbursement HTML from one to five ZIP submissions, covering personnel incentives, promotional/stack displays, poster/material production, specially approved other expenses, maintenance fees, or a selected combination. Use whenever input/ contains new offline audit materials and Codex must safely classify them, extract only visual facts with AI, deterministically close document/product/store/photo/amount controls, and publish the canonical local interface without using prior outputs or a gold workbook at runtime.
---

# Orchestrate Offline Audit

Use this Skill as the only formal entry for this project:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> [--scenario <scenario>]
```

Every Agent executing an audit must call this bundled runner. Do not call internal report/render
functions as a substitute, handwrite the final HTML, copy or patch a prior result, rebuild the frontend,
or introduce a second CLI, Git run branch, persistent snapshot, separate delivery directory,
template-copy step, or post-run replacement workflow. The runner publishes the one formal HTML.

## Input contract

Read only ZIP files directly inside `<project>/input/`. Require one to five ZIPs. Read [routing-rules.md](references/routing-rules.md) before changing classification or extraction.

- A personnel-incentive ZIP contains exactly one sales Excel, no PDF, exactly one identifiable settlement image, and at least one transfer/red-packet screenshot.
- A promotional-display ZIP contains exactly one contract PDF, exactly one sales Excel, and at least one field photo.
- A poster/material-production ZIP contains one signed-contract image, one invoice/receipt image, one settlement-form image, and one nested field-photo ZIP; unrelated POS evidence is excluded.
- An other-expense ZIP is explicitly marked `其他`, has no Excel or nested ZIP, and contains exactly one promotional contract, exactly one settlement, at least one independent agreement/contract/invoice/receipt, plus any activity photos, POS, or special-approval evidence. The marker routes the package but never proves eligibility.
- A maintenance-fee ZIP is explicitly marked `维护费用` or `维护费`, has no nested ZIP, and contains at least one visual POS or settlement candidate. Bind a maximum of one POS spreadsheet, one promotional contract, and one settlement; missing mandatory roles remain blocking audit issues so the incomplete representative package can produce an exact resubmission list, while duplicate singleton candidates remain an intake error.
- Accept any supported type alone or one of each. Reject zero ZIPs, more than five ZIPs, two ZIPs of the same type, unknown type, or ambiguous roles with a specific error. Use `--scenario other_expense` or `--scenario maintenance_fee` when only that submitted package must be published.
- Reject unsafe ZIP paths, links, encryption, duplicate/case-colliding paths, excessive expansion, suspicious compression ratios, and duplicate image basenames.

Extract only into a run-scoped temporary directory that is removed automatically. Do not create `input/.prepared`, `.audit-tmp`, a cache, or a persistent run directory.

## Trust boundary

Keep vision and judgment separate because source facts and audit decisions have different reliability requirements.

AI may act only as the eyes:

- personnel: read the settlement image and transfer screenshots;
- display: read the contract PDF and field photos;
- poster/material: read the contract, invoice/receipt, settlement, and every finished-product field photo;
- maintenance fee: read only the stamped-POS visual, signed promotional contract, settlement, fee-specific documents, and activity photos; never expose the POS spreadsheet to AI;
- return schema-valid JSON with visible text, dates, locations, packaging, display observations, transfer occurrences, limitations, and source basenames.

Do not give the AI a copy of the sales Excel. Instruct it not to read `input/`, `worktrees/`, caches, history, prior results, or the gold-standard workbook. The AI must not calculate supported amounts, choose Excel barcodes/names, or make the final reimbursement decision.

Deterministic Python owns all remaining work:

- safe ZIP validation and routing;
- direct Excel cell reading, original-name preservation, row aggregation, and store detail;
- shared product identity from `shared/canban-product-multimodal-knowledge-base`; personnel and
  promotional-display consume the same validated catalog, and no scenario Skill owns a duplicate;
- personnel sales-SKU reconciliation through the validated product knowledge base: valid 69 code
  must match exactly, then the original product name needs only one uniquely fuzzy-compatible match within
  that same-code candidate set; a failed product gate contributes no supported reward. A settlement
  line without a visible barcode may route to exactly one still-unused, knowledge-passed Excel SKU by
  exact unique quantity even when its OCR product name is poor; otherwise name remains fuzzy auxiliary
  evidence under the one-line/one-barcode constraint;
- display contract-first reconciliation: audit the six mandatory core contract controls individually
  (contracting party, activity budget, execution period, activity content, reimbursement/settlement
  method, and seal); retain watermark visibility only as informational extraction that never affects
  status, confidence, amount, or resubmission; map
  concrete core-contract products and every printed contract-attachment product to the validated
  knowledge base; independently extract each field photo's visible product text, use only that text
  to retrieve bounded candidates from the full validated catalog, compare the packaging with those
  candidates' reference images, and then compare the resolved photo product with contract scope;
  then compare standalone sales Excel
  directly with the contract attachment across eight checked fields; preserve unit as a displayed
  source fact only, without an Excel-to-knowledge or photo-to-Excel cross-link;
  after the complete display-photo pass, run a separate focused display-standard review over only the
  submitted photos and immutable store/photo routing. It replaces only the display observation,
  distinguishes a separately bounded edge stack from the attached side panel of one already-counted
  package, and revalidates the complete photo evidence;
- poster/material contract-first reconciliation: require all contract-referenced attachments; compare
  ticket and settlement line items, quantities, unit prices, subtotals, total, company, date, and
  seals; then require watermarked photo coverage for period, time, location, finished content,
  dimensions, placement, and every contracted unit/store without extrapolation;
- image hashes and duplicate screening;
- dates, quantities, rewards, transfers, claims, supported amounts, and final pass/supplement decisions;
- result-schema validation, temporary intermediate generation/verification, canonical HTML rendering,
  naming, static-shell verification, and atomic publication.

## Routing and audit

Process scenarios in this fixed order when both exist:

1. `personnel_incentive` → read [audit-personnel-incentive](../audit-personnel-incentive/SKILL.md) and its linked rules/schema completely.
2. `promotional_display` → read [audit-promotional-display](../audit-promotional-display/SKILL.md) and its linked rules/schema completely.
3. `poster_material` → read [audit-poster-material](../audit-poster-material/SKILL.md) and its linked rules/schema completely.
4. `other_expense` → read [audit-other-expense](../audit-other-expense/SKILL.md) and its linked rules/schema completely.
5. `maintenance_fee` → read [audit-maintenance-fee](../audit-maintenance-fee/SKILL.md) and its linked rules/schema completely.

Validate AI evidence before calculation and validate each deterministic result against
`contracts/audit-result.schema.json` before rendering. An aggregate match never substitutes for a
line/store control.

## Registering another scenario

Do not infer or create a new scenario during an ordinary formal run. When the user provides one new
representative ZIP plus a business prompt and asks to extend this system, use
[create-offline-audit-scenario](../create-offline-audit-scenario/SKILL.md). That workflow first checks
whether the package is an instance of `personnel_incentive`, `promotional_display`,
`poster_material`, `other_expense`, or `maintenance_fee`; only a genuinely different reusable material/authority/decision chain gets a new
`audit-*` Skill. The new scenario must be registered from safe routing through deterministic audit,
result Schema, six-column intermediate, canonical HTML, CLI selection, documentation, and tests
before it is advertised here or accepted by this runner.

## Canonical HTML contract

The formal result is one self-contained UTF-8 HTML file. A temporary workbook may be created from an
empty `openpyxl.Workbook` only as a run-scoped deterministic intermediate for data shaping and
verification. Never load a prior workbook as a template, publish the temporary workbook, expose it to
the model, or leave it behind after success or failure. Legacy/acceptance workbooks are test-only.

The run-scoped workbook retains every deterministic source row and comparison for schema and coverage
verification. The customer-facing HTML is an **error-only projection** of that verified payload:
passing rows, passing contract facts, the contract baseline, audit-process narration, and the
informational watermark policy are not rendered. Hiding them is presentation filtering only; they
remain available to every downstream decision and to the embedded verification payload.

The single HTML contains the fixed error desk and one error list for every submitted scenario:

- the home view shows exactly four metrics: `核销场景`, `错误总数`, `材料 / 结算错误`, and
  `商品 / 门店错误`; its total is the number of grouped customer actions, not the number of internal
  failed fields. Each submitted scenario has one real queue entry with its grouped error count and a
  direct button into that scenario;
- personnel renders only failed product rows plus failed settlement/payment rows. A product whose
  69 code, fuzzy-compatible name, quantity, and reward all correspond is hidden. The retained internal
  row still contains the original sales product, selected catalog code/name/69 code, settlement
  quantity/reward, transfer evidence, amount result, confidence, and decision basis;
- promotional display first groups contract-wide errors. All contract-attachment knowledge failures
  form one card with one expandable detail table preserving every PDF page/line, contract product,
  selected knowledge product, and strict-field cause. Contract-to-sales errors form a separate grouped
  card only when a strict field other than product name is unresolved or inconsistent. Beneath those
  cards, render only stores with their own photo/date/location/display/product error; never repeat a
  contract, knowledge, sales, or amount-wide failure inside every store;
- poster/material renders only its grouped blocking errors. It omits passing controls and unrelated
  files while preserving every affected basename, recognized fact, expected rule, reimbursement
  impact, confidence, and concrete resubmission action. The photo-evidence group retains one
  recognized-content entry per submitted photo;
- maintenance fee renders only grouped blocking errors for required materials, fee nature, POS seal,
  POS spreadsheet/correspondence, contract, settlement, party/period alignment, fee-specific support,
  and amount recalculation. It never merges with the other-expense special-approval queue;
- other expense renders only classification, special-approval, document, amount, or final manual-
  review actions. It first names every visible fee description that belongs to an established type;
  only a genuinely unclassifiable fee moves to the special-approval gate. Even when complete, the
  automatic supported amount remains zero and the result stays `待人工核定`.

Every error card must show its scope, exact problem file, comparison/baseline file when applicable,
concrete causal facts, and one usable handling action. A grouped card count is one queue item even when
its expandable table contains many affected rows; for example, 36 contract-product code failures are
one `合同商品` card with 36 complete detail rows, not 36 home-queue items.

Personnel product records use the uniquely selected knowledge-base product's complete authoritative
name internally and never add a `结算第N行` prefix. A unique fuzzy name is an accepted medium-confidence
match and is not an error or resubmission reason by itself. When product, quantity, and reward all
reconcile, the retained result says `商品、数量、奖励金额全部对应`; never weaken it to `可以对应`.

Keep these promotional-display meanings separate:

- core contract terms establish parties, activity period, display/promotion obligations, fee basis,
  stores, and an optional specific-product condition;
- only an explicit core term may populate the optional contract-product condition;
- `contract.sales_attachment` is a page- and row-preserving transcript of an appended sales table. It
  never creates a core contract product or promotion requirement, but its row products are still
  contract-side identities reconciled to the knowledge base. When absent, the standalone Excel has no
  row-level contract baseline: render the control as unverifiable and require the complete contract;
- field photos establish store/date/display/promotion facts and supply the only text allowed to retrieve
  their product candidates; repository product-reference views establish product identity only, while
  contract membership is checked separately after the photo identity is fixed;
- the standalone sales Excel and contract attachment retain their own original customer/date/product/
  unit/quantity/price/amount values and source row/page. Neither source has a store-authoritative
  column, so neither may be assigned to a store or presented as proof of store-level sales.

Across these business-source product comparisons, product names are fuzzy auxiliary evidence only.
When comparable product code and 69 code plus independent transaction facts uniquely locate a row, a
different, absent, or illegible name never creates its own error, confidence downgrade, problem count,
or resubmission request. A promotional-display error-focused HTML view shows only blocking errors and
their files, differences, and actions; keep passing contract facts and informational watermark policy
in the audit logic but hide them from that view.

If one exact product-code-plus-69-code pair returns multiple knowledge-base variants, use a uniquely
fuzzy-compatible source name to disambiguate them. Every dense contract sales attachment with detail
rows requires a second, focused visual pass using
[`contract-product-cells.schema.json`](../audit-promotional-display/references/contract-product-cells.schema.json):
derive four orientations from the original scan, attach the two lossless landscape reading directions
and overlapping row bands, and add a same-pixel black-print product-cell view wherever a red seal
crosses the table. Choose the view whose print is upright, use color separation only to suppress seal
strokes rather than invent characters, and
independently re-read the product-code, product-name, and 69-code cell for every attachment line, preserving
line/page order. This pass receives no sales Excel and may
use the first PDF pass's quantity/price/amount only to locate the row, never to infer any
target cell. Require every nonempty reread 69 code to pass EAN-13 validation. Replace first-pass product code/name/69-code values only with nonempty focused visual readings;
then revalidate the complete contract evidence. A visible name omitted by the first OCR pass is an
extraction defect, not a product-code mismatch.

Close promotional display in this fixed contract-led order:

1. **Contract core:** audit contracting party, activity budget, execution period, activity content,
   reimbursement/settlement method, and seal separately. Contract watermark visibility may be
   retained as an informational fact only and must never affect reimbursement or trigger resubmission.
2. **Contract → knowledge base:** reconcile concrete core products and every contract-attachment
   product. A valid 69 code must exactly equal the catalog 69 code. Preserve a source-local business
   product code for the strict contract-attachment-to-sales-Excel comparison; for knowledge
   reconciliation, require that code to exactly equal the selected product's `product_code` or
   `product_code_aliases`. A different catalog main code is allowed only through an exact registered
   code alias, and product code plus 69 code must jointly hit one product. Registered packaging aliases do not satisfy or rewrite a strict
   product-code field, but may be used as product-description text in fuzzy-name matching. Product
   names are fuzzy auxiliary evidence, never require exact equality, and never produce a separate
   mismatch error after comparable product code and 69 code agree. Preserve printed order, attachment
   `line_no`, and `source_page`.
3. **Photo → knowledge images → contract:** transcribe each photo's useful product text, retrieve a
   small candidate set from the full validated knowledge base using only that text, and require the
   submitted packaging to be broadly visually compatible with a registered multi-view reference for
   an exact identity. Angle, distance, lighting, shelf occlusion, and package pose may differ; do not
   require pixel identity when the core layout, color blocks, bundle structure, and recognizable
   features agree without conflict.
   Separately compare the resolved photo product with contract scope, and compare store, date,
   activity, display, promotion, and reuse to contract terms. A fuzzy photo identity stays unresolved;
   neither contract product wording nor Excel can select or upgrade it.
   Then run the focused display-standard schema over submitted photos only: three front boxes plus the
   exposed side panel of the third box remains three, while a fourth separately bounded stack of
   additional packages counts even when narrow or side-facing. Different submitted-brand SKUs,
   bundles, and package formats may jointly form four columns; any one routed photo may prove the
   standard, but never sum partial counts across photos or count unrelated neighboring brands. Preserve the established photo route
   and replace no field other than `display_observation`. Then apply the promotional-display Skill's
   user-accepted visual regression registry only when the contract store and ordered photo SHA-256 set
   match exactly; a one-byte or routing change must disable the calibration. Revalidate afterward.
   For poster/material quantity coverage, apply that Skill's separate accepted regression only when the
   complete ordered field-photo basename and SHA-256 sequence matches exactly. It may replace only the
   declared aggregate material-unit and contributing-photo counts; any byte, name, membership, or order
   change disables it. For difficult visual-document amounts that the user has separately verified as a
   permanent standard, apply the document-fact registry only when the complete ordered contract,
   invoice/receipt, and settlement basename plus SHA-256 sequence matches exactly; it may replace only
   the three declared amount fields. Any byte, name, membership, or order change disables it. None of
   these registries is model context or customer-facing evidence.
4. **Sales Excel → contract attachment:** check Excel internal arithmetic, then compare every paired
   row across customer name, business date, product code, product name, barcode, quantity, retail
   price, and total amount. Preserve both sources' unit values for display, but do not use unit in row
   pairing, field results, pass/fail, confidence, problem counts, amounts, or resubmission. Preserve
   both raw values, PDF page/line and Excel row; do not infer a missing field or rewrite the contract.

Standalone sales Excel is never reconciled to the knowledge base, and field photos are never
reconciled to sales Excel. Show every contract-attachment/Excel pair plus unmatched Excel rows, with
all nine raw source fields, the eight checked field results, and one overall `置信度：高/中/低`.
Render product code and 69 code as exact match/not match, product name as exact/fuzzy/not matched,
and a fully passing row as `全部对应`; never use vague `可以对应` wording. Every blocking item must
strictly name the problem file basename(s), the comparison/baseline file basename(s) when applicable,
the observed value or missing field in each source, and the concrete causal reason the relationship
cannot pass. Then name the exact PDF page, attachment line, Excel row/field, settlement line, transfer
screenshot, or photo content that must be resubmitted. Do not emit shorthand such as `A ↔ B 无法确认`
or a source-free sentence such as `Excel门店与收款人无法逐一确认`. Keep candidate sets, internal product IDs, RAG wording,
hashes, convergence, and model reasoning out of the customer interface.

For field-store errors, distinguish a legible but wrong watermark location from a missing/unreadable
watermark. A legible conflict is labeled `门店水印错误`, shows the contract store and photo watermark
location side by side, and requests corrected watermark content; it must never be described as a
clarity problem. The submitted filename cannot override a conflicting watermark.

Contract parties/customer, contract period/business date, and seal integrity remain global controls.
Contract watermark visibility is informational only. Product-reference views cannot prove a store,
date, display, promotion, price, originality,
or amount. An ordinary price is not promotion. Promotion requires an explicit special price, old/new
price, discount, gift, multi-buy, `1+1`, `3+2`, or value-pack signal and is mandatory only when the
core contract requires it. Calculate amount only from an explicit per-store or per-stack unit basis;
never divide a total automatically.

## Frozen interface asset

The canonical frontend bundle is rooted at
`skills/orchestrate-offline-audit/assets/canban-audit-shell.html` and contains exactly two maintained
components beside it: `error-only.css` and `error-only.js`. The production generator is
`audit_core.html_report.create_html_report_from_workbook`. It first assembles those three repository
assets, hashes the complete assembled shell and both style blocks, and may then replace only the
verified-data and integrity-hash slots. The components are build-time assets only; the published page
inlines them and has no sibling dependency. `audit_core.html_report.verify_html_report` must rebuild
the canonical bundle and reject a template-version mismatch, assembled-shell fingerprint mismatch,
style mismatch, external dependency, missing business content, unexpected control, or any byte outside
the designated injection slots.

Do not handwrite or redesign the page during an audit. Do not reproduce it from prose, a screenshot,
or prior output; do not patch generated HTML after the runner; and do not add a new button, style,
layout, label, or interaction for an ordinary business or bug-fix request. Future tasks must reuse the
canonical shell byte-for-byte outside the designated injection slots. The shell may change only when
the user explicitly requests a frontend redesign. The same redesign change must update the asset
version, expected fingerprint/structural verification, affected tests, and this Skill contract; never
weaken verification merely to accept a modified shell.

The generated page remains one `file://`-openable document with all business data, CSS, and JavaScript
inline and no server, network, CDN, font download, sibling Excel, JSON sidecar, or output asset
directory. Its fixed navigation, queue buttons, error-detail disclosure, back-to-home control,
back-to-top control, responsive behavior, copy, and exact button set come from the canonical bundle.

Template `3.2.0` freezes the approved error-only delivery standard for up to five submitted scenarios. Keep the white `参半` + small
lower-right `CANBAN` header, cold-gray grid canvas, dark clipped left rail, white clipped rejected-only
hero, four equal metric cards, error-composition strip, and one wide queue row per submitted scenario. The home
hero reads `核销错误处置总览`; the rail and queue show only grouped errors. Personnel uses the coral
accent, promotional display amber, display props teal, other-expense special approval purple, and maintenance-fee blue. Do not restore the former pass/object
dashboard, contract-baseline panels, passing records, search/filter toolbar, expand-all control, store
evidence wall, generic card pile, health ring, clock, online decoration, fake action, external asset,
or another presentation layer.

Each scenario page has one dark `核销错误清单` header followed by complete error cards. Cards use the
fixed order `问题文件`, optional `对照文件`, `错误原因`, `处理方式`. The contract-product card alone
may contain the native, initially collapsed detail disclosure with its complete error table. That
disclosure remains keyboard-operable. The only scenario navigation is the left-rail tab, queue-entry
button, and `返回错误总览`; `返回顶部` is the only floating button.

At 1120 px the grid may reflow; below 880 px the rail becomes a horizontal scenario strip; below
620 px the home, cards, and tables stack. At 390×844 the page must have no horizontal overflow and the
first scenario queue entry must remain in or immediately adjacent to the first viewport. Narrow-screen
detail tables become labeled blocks without dropping a PDF line, product, source file, cause, or action.

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

When the user designates an approved reference HTML as the delivery standard, that file is
acceptance-only and must never be read by the generator, copied into the output, exposed to the model,
or used to fill business evidence. After the bundled formal command publishes its candidate, run:

```powershell
py -3 -B skills/orchestrate-offline-audit/scripts/verify_delivery_standard.py `
  --reference <approved-reference.html> `
  --candidate <published-worktree.html>
```

Do not finish the task until this verifier returns `passed: true`. It requires the exact approved
error-only title, shell, CSS and 1440-pixel desktop screenshot; it also compares the rendered error
structure and business facts, then independently validates the candidate's complete source-derived
business invariants. It exercises scenario navigation and the native detail disclosure, requires zero
browser errors or HTTP(S) requests, and checks the candidate at 390×844 for horizontal overflow and
first-queue-entry placement. A failed comparison means the business extraction, deterministic result,
report projection, or canonical asset must be corrected and the formal run repeated; never patch the
published HTML.
