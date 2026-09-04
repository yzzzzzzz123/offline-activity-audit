---
name: orchestrate-offline-audit
description: Generate one verified persistent run archive for the fixed Canban offline-activity audit workbench from one to ten local ZIP submissions or one authenticated OSS ZIP event, covering personnel incentives, promotional/stack displays, poster/material production, specially approved other expenses, maintenance fees, extra giveaways, price-difference support, dealer POS-target incentives, entry/barcode fees, customer self-procured gift materials, or a selected combination. Use whenever local input or the OSS intake adapter supplies new offline audit materials and Codex must safely classify them, extract only visual facts with AI, deterministically close document/product/store/photo/amount controls, and publish schema-validated analysis, logs, DOM-data checkpoints, and a customer snapshot without regenerating the root HTML or using prior outputs or a gold workbook at runtime.
---

# Orchestrate Offline Audit

Use this Skill as the only formal entry for this project:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model> [--model <audit-model>] [--reasoning-effort <level>] [--scenario <scenario>]
```

Every Agent executing an audit must call this bundled runner. Do not call internal report/render
functions as a substitute, handwrite a run page, copy or patch a prior result, rebuild the frontend,
or introduce a second CLI, Git run branch, alternate worktree root, template-copy step, or post-run
replacement workflow. The runner publishes one versioned run archive; the root workbench stays fixed.
The authenticated OSS endpoint is allowed only as a transport adapter: it persists one verified ZIP
under `<project>/input-oss/<job_id>/` and launches this same bundled runner in a child process with that
job directory as `--input-dir`. Its external request contains only `verifyCode`, `fileId`, and
`downloadUrl`. After the durable formal snapshot is complete, the adapter sends the same `verifyCode`
and `fileId` plus the deterministic Chinese `result` to the configured HTTPS
`/api/v1/ai/analyze/callback`; callback retries never rerun this Skill.

## Main-flow task checklist

Every formal run uses this exact six-task main flow, in this order:

- [ ] `01 bootstrap` — 运行初始化：建立持久化运行目录。
- [ ] `02 intake` — 材料分流：识别场景与资料角色。
- [ ] `03 analysis` — AI 识别：提取材料中的可见事实。
- [ ] `04 evidence` — 证据校验：核对 Schema 与来源边界。
- [ ] `05 decision` — 核销决策：执行确定性业务规则。
- [ ] `06 verification` — 结果封存：验证页面投影与技术档案。

The ordered runtime source of truth is `audit_core.workbench_store.main_flow_task_list`. Persist the
same list in every new run manifest, expose it through the read-only workbench context and
`/api/config`, and render the overview from that list. Do not maintain a differently named, reordered,
or shortened frontend-only stage list. Multi-scenario runs are stage-batched, not scenario-interleaved:
finish AI recognition for every selected scenario before any evidence-validation event; finish evidence
validation for every scenario before any deterministic decision event; finish every decision before
report verification. The observable stage index must therefore be monotonic for the whole run.

## Input contract

Read only ZIP files directly inside `<project>/input/`. Require one to ten ZIPs. Read [routing-rules.md](references/routing-rules.md) before changing classification or extraction.

- A personnel-incentive ZIP contains exactly one sales Excel, no PDF, exactly one identifiable settlement image, and at least one transfer/red-packet screenshot.
- A promotional-display ZIP contains exactly one contract PDF, exactly one sales Excel, and at least one field photo.
- A poster/material-production ZIP contains one signed-contract image, one invoice/receipt image, one settlement-form image, and one nested field-photo ZIP; unrelated POS evidence is excluded.
- An other-expense ZIP is explicitly marked `其他`, has no Excel or nested ZIP, and contains exactly one promotional contract, exactly one settlement, at least one independent agreement/contract/invoice/receipt, plus any activity photos, POS, or special-approval evidence. The marker routes the package but never proves eligibility.
- A maintenance-fee ZIP is explicitly marked `维护费用` or `维护费`, has no nested ZIP, and contains at least one visual POS or settlement candidate. Bind a maximum of one POS spreadsheet, one promotional contract, and one settlement; missing mandatory roles remain blocking audit issues so the incomplete representative package can produce an exact resubmission list, while duplicate singleton candidates remain an intake error.
- An extra-giveaway ZIP is explicitly marked `额外搭赠` or `搭赠`, has no Excel or nested ZIP, and contains one or more visual documents. Every visual source is neutrally bound once; visible titles then distinguish the signed promotional contract, dealer-stamped settlement, system sales/delivery statement, store receipts, activity photos, support, or other document. Missing roles remain blocking report issues, while duplicate contract/settlement/delivery candidates fail evidence acceptance.
- A price-difference ZIP is explicitly marked `补差` or `价格补差`, contains one signed promotional contract, one settlement, stamped POS visuals, zero or one POS Excel, and one safely extracted activity-photo RAR. Missing Excel becomes a blocking issue; every contract/POS store must have its own in-period date/address/time-watermarked photo showing the contract activity price.
- A POS-target-incentive ZIP is explicitly marked `POS激励达标`, `POS达标激励`, or `POS激励`, has no nested archive, and binds at most one POS Excel, promotional contract, and settlement plus stamped POS and full-reduction activity proof. Missing roles remain blocking issues. The recipient is the dealer; the signed contract establishes approved-channel eligibility, tiers, rates and cap.
- An entry-fee ZIP is explicitly marked `进场费` or `条码费`, has no Excel/nested ZIP, contains one clearly named signed entry contract/product-promotion agreement and one safely extracted shelf-photo RAR. Bind any explicit system-deduction proof separately; missing proof remains a blocking issue because shelf photos cannot prove an actual system deduction.
- A self-procured-gift-material ZIP is explicitly marked `自采赠品物料`, `自采赠品`, or `自采物料`, binds one signed promotional contract, one customer-stamped settlement, one itemized invoice/receipt, payment records, stamped POS visuals, at most one modern POS spreadsheet, and exactly one legacy activity-return `.xls`. Extract every embedded return photo through its WPS DISPIMG relationship and retain workbook row/store hints only for routing. The legacy return workbook is not the POS spreadsheet; a missing modern POS spreadsheet remains blocking.
- Accept any supported type alone or one of each. Reject zero ZIPs, more than ten ZIPs, two ZIPs of the same type, unknown type, or ambiguous roles with a specific error. Use `--scenario other_expense`, `--scenario maintenance_fee`, `--scenario giveaway_promotion`, `--scenario price_difference_support`, `--scenario pos_target_incentive`, `--scenario entry_fee`, or `--scenario self_procured_gift_material` when only that submitted package must be published.
- Reject unsafe ZIP paths, links, encryption, duplicate/case-colliding paths, excessive expansion, suspicious compression ratios, and duplicate image basenames.

Extract only into a run-scoped temporary directory that is removed automatically. Do not create `input/.prepared`, `.audit-tmp`, a cache, or a persistent run directory.

## OSS transport adapter

When the source is an OSS object event, read [oss-intake.md](references/oss-intake.md) before changing
the HTTP contract, downloader, job store, or runner invocation. Keep the shared repository `input/`
untouched. The adapter owns authentication, idempotency, allowlisted HTTPS download, transport-level
response-size/ETag/SHA-256 checks, per-job persistent source staging, asynchronous status, deterministic
result projection, and callback delivery only; archive routing, AI
extraction, deterministic decisions, verification, and worktree publication remain owned by this
formal runner. Retain each verified original ZIP after success or failure without flattening job
directories or committing runtime files. Remove incomplete or transport-invalid downloads; extracted
sources, model workspaces, temporary projections, and result receipts remain run-scoped temporary data.

## Trust boundary

Keep vision and judgment separate because source facts and audit decisions have different reliability requirements.

AI may act only as the eyes:

- personnel: read the settlement image and transfer screenshots;
- display: read the contract PDF and field photos;
- poster/material: read the contract, invoice/receipt, settlement, and every finished-product field photo;
- maintenance fee: read only the stamped-POS visual, signed promotional contract, settlement, fee-specific documents, and activity photos; never expose the POS spreadsheet to AI;
- extra giveaway: read every isolated submitted visual document so generic camera-export names can be classified from visible content; never calculate or decide the claim in the visual stage;
- price difference: read the signed contract, settlement, stamped POS visuals, and every safely extracted field photo; never expose the POS spreadsheet or infer a support unit amount from the retail price reduction;
- POS target incentive: read the signed contract, stamped POS, dealer-stamped settlement, and activity photos/receipts/other proof; never expose the POS spreadsheet or treat settlement tiers as a substitute for contract authority;
- entry fee: read the signed entry contract, every safely extracted shelf photo and explicit system-deduction proof; treat folder/store hints as routing-only and never as visible business evidence;
- self-procured gift material: read the signed promotional contract, customer-stamped settlement, itemized invoice/receipt, each payment record, every stamped POS visual, and every activity photo extracted from the legacy return workbook; never expose or infer the POS spreadsheet, and treat workbook row/store/period hints as routing-only;
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
- extra-giveaway contract-first reconciliation: keep normal shipment value separate from the gift claim; compare contract period, eligible purchase/gift products and ratio to the stamped settlement, system sales/delivery statement, store receipts, and activity photos; deterministically verify zero-value receipt gifts, receipt-local ratio, product correspondence, shipment total, gift quantity × explicit unit value, budget, claim, and duplicate evidence;
- price-difference contract-first reconciliation: distinguish original price, activity price, and contract support unit amount; reconcile stamped POS to the electronic spreadsheet; require every activity store's own valid watermarked activity-price photo; calculate eligible POS quantity × contract support unit and apply contract quantity/budget limits before comparison with the stamped settlement;
- POS-target contract-first reconciliation: require dealer recipient and strategic/approved-special-channel eligibility; reconcile period/store/sales amounts between stamped POS and the unique electronic summary; require full-reduction activity existence proof; select the highest reached contract tier, multiply the POS base by its rate, apply the contract cap, and compare the stamped settlement;
- entry-fee contract-first reconciliation: verify both-party execution, channel, product-barcode rows, contracted stores, fee sum, payment method and actual-shelving conditions; uniquely match each photo's visible watermark to a contract store, require each contract product across all required stores, and cap support by the contract total and visible system-deduction amount without multiplying a per-barcode fee by store count;
- image hashes and duplicate screening;
- dates, quantities, rewards, transfers, claims, supported amounts, and final pass/supplement decisions;
- result-schema validation, temporary view-payload rendering/verification, persistent archive naming,
  append-only logs, DOM-data checkpoints, snapshot hashing, and atomic publication.

## Routing and audit

Process scenarios in this fixed order when both exist:

1. `personnel_incentive` → read [audit-personnel-incentive](../audit-personnel-incentive/SKILL.md) and its linked rules/schema completely.
2. `promotional_display` → read [audit-promotional-display](../audit-promotional-display/SKILL.md) and its linked rules/schema completely.
3. `poster_material` → read [audit-poster-material](../audit-poster-material/SKILL.md) and its linked rules/schema completely.
4. `other_expense` → read [audit-other-expense](../audit-other-expense/SKILL.md) and its linked rules/schema completely.
5. `maintenance_fee` → read [audit-maintenance-fee](../audit-maintenance-fee/SKILL.md) and its linked rules/schema completely.
6. `giveaway_promotion` → read [audit-giveaway-promotion](../audit-giveaway-promotion/SKILL.md) and its linked rules/schema completely.
7. `price_difference_support` → read [audit-price-difference-support](../audit-price-difference-support/SKILL.md) and its linked rules/schema completely.
8. `pos_target_incentive` → read [audit-pos-target-incentive](../audit-pos-target-incentive/SKILL.md) and its linked rules/schema completely.
9. `entry_fee` → read [audit-entry-fee](../audit-entry-fee/SKILL.md) and its linked rules/schema completely.
10. `self_procured_gift_material` → read [audit-self-procured-gift-material](../audit-self-procured-gift-material/SKILL.md) and its linked rules/schema completely.

Validate AI evidence before calculation and validate each deterministic result against
`contracts/audit-result.schema.json` before rendering. An aggregate match never substitutes for a
line/store control.

## Registering another scenario

Do not infer or create a new scenario during an ordinary formal run. When the user provides one new
representative ZIP plus a business prompt and asks to extend this system, use
[create-offline-audit-scenario](../create-offline-audit-scenario/SKILL.md). That workflow first checks
whether the package is an instance of `personnel_incentive`, `promotional_display`,
`poster_material`, `other_expense`, `maintenance_fee`, `giveaway_promotion`, `price_difference_support`, `pos_target_incentive`, `entry_fee`, or `self_procured_gift_material`; only a genuinely different reusable material/authority/decision chain gets a new
`audit-*` Skill. The new scenario must be registered from safe routing through deterministic audit,
result Schema, six-column intermediate, fixed-workbench projection, persistent trace, CLI selection, documentation, and tests
before it is advertised here or accepted by this runner.

## Persistent workbench contract

The only customer-facing HTML is the repository-root `offline-activity-audit.html`. It is the
persistent Audit System `2.7.1` shell served by the trusted local service and must not be regenerated,
copied, or patched by an audit run. `/` is the level-one management system; each catalog entry expands
through `/?run=<workspace-id>` into a level-two record. Completed records retain the approved original
error-desk interface, while incomplete records receive a truthful diagnostic page. A temporary
workbook and assembled legacy projection may be created from an empty `openpyxl.Workbook` only in
system temporary space for deterministic data shaping and verification. Never load a prior workbook
as a template, publish either temporary artifact, expose them to the model, or leave them behind after
success or failure. Legacy/acceptance workbooks are test-only.

The run-scoped workbook retains every deterministic source row and comparison for schema and coverage
verification. The fixed workbench defaults to an **error-only projection** of that verified payload,
and a sibling `正确检查项` ledger projects only independently passed subchecks from the same deterministic
scenario results. Group pass entries by audit type and Chinese business category; show the checked
subject, concise decision basis, source basenames when available, and one high/medium/low confidence.
A passed subcheck never changes the whole-record conclusion, hides an error, or implies approval.
Full contract baselines, audit-process narration, informational watermark policy, candidate sets,
similarity scores, product IDs, hashes, and model reasoning are not rendered in either customer view.

Each completed level-two record contains the fixed audit desk with two sibling result views. The
default error overview contains one continuously stacked list of every grouped customer error from
that run; the correct-check ledger contains ordered pass entries grouped by audit type:

- the home view keeps exactly four metrics in the approved style: `核销类型`, `错误总数`,
  `材料 / 结算错误`, and `商品 / 门店错误`; its total is the number of grouped customer actions, not
  the number of internal failed fields. It must not create scenario queues, entry buttons, or per-type
  subpages. All error cards render directly below the unchanged summary modules in source scenario order,
  with one continuous sequence and one `核销类型 · <业务类型>` badge per card;
- the correct-check view uses the same dark rail, cold-gray canvas, four-metric hierarchy, and stacked
  card grammar with a green pass accent. It includes one group for every submitted audit type even when
  that group has no independently confirmed pass item, and never creates a scenario-entry button or
  per-type subpage;
- both result views expose aligned filter panels with live hit counts and reset controls. Audit type is a
  fixed facet that always lists every type present in that view regardless of other active filters. The
  error confidence facet and the correct-check category/confidence facets retain only currently matching
  values, with per-option hit counts;
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
  and amount recalculation. It never merges with other-expense special-approval errors;
- extra giveaway renders only grouped blocking errors for required materials, fee nature, contract,
  settlement, system sales/delivery, party/period/product correspondence, receipt ratio and zero-value
  gifts, activity photos, duplicate evidence, shipment reconciliation, and gift-amount recalculation.
  Normal shipment value is never displayed as the gift claim;
- other expense renders only classification, special-approval, document, amount, or final manual-
  review actions. It first names every visible fee description that belongs to an established type;
  only a genuinely unclassifiable fee moves to the special-approval gate. Even when complete, the
  automatic supported amount remains zero and the result stays `待人工核定`.

Every error card must show its audit-type badge, exact problem file, comparison/baseline file when applicable,
concrete causal facts, and one usable handling action. A grouped card counts as one error even when
its expandable table contains many affected rows; for example, 36 contract-product code failures are
one `合同商品` card with 36 complete detail rows, not 36 separate error cards.

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

If the field-store and watermark names fully correspond, pass location directly without a map call or
location-confidence issue. For every legible name difference, invoke the fixed Baidu Maps Streamable
HTTP MCP through `map_search_places`, reading its server-side AK from `BAIDU_MAPS_API_KEY` in the current
process or, on Windows, the current user's environment configuration. This permits direct routine runs
without another interactive terminal; never read the AK from a repository file or command-line argument,
persist it in run outputs, or expose the authenticated URL. Store returned coordinates as `BD09LL`. A selected same
POI, a verified mall/store parent-child relation, or selected POIs within 100 metres is compatible with
high location confidence. The two searches need not be independently unique: preserve a credibly unique
side, require every unresolved side's selected candidate to meet the internal 0.60 name-relevance
threshold, and require both sides to meet it when neither is credibly unique; accept the best candidate
pair only when it forms a passing spatial relationship. A distant pair, the 100-to-300-metre gray zone, no credible candidate pair, unavailable
MCP, or missing comparable coordinates is `门店地点低置信度` / manual review, not an automatic
`门店水印错误`. A uniquely resolved distant pair keeps its measured distance and requests either
authoritative same/nearby evidence or corrected-location content. New determinations do not emit the
legacy `mismatch` result. A validated verified location-registry entry may resolve an unavailable or
inconclusive MCP result, but a resolver/registry conflict remains low-confidence/manual. Show the
provider, MCP attempt status, relationship, distance, confidence, threshold and concise evidence basis
in the ledger, but never expose the internal relevance score in customer-facing output.
Missing/unreadable watermarks remain separate, and filenames never prove or override
location.

Contract parties/customer, contract period/business date, and seal integrity remain global controls.
Contract watermark visibility is informational only. Product-reference views cannot prove a store,
date, display, promotion, price, originality,
or amount. An ordinary price is not promotion. Promotion requires an explicit special price, old/new
price, discount, gift, multi-buy, `1+1`, `3+2`, or value-pack signal and is mandatory only when the
core contract requires it. Calculate amount only from an explicit per-store or per-stack unit basis;
never divide a total automatically.

## Frozen persistent interface

The canonical customer page is the root `offline-activity-audit.html`. The trusted service in
`audit_core.workbench_server` serves its level-one system at `/`, injects the selected verified view
payload into the same fixed HTML response for `/?run=<workspace-id>`, and exposes relative,
read-only result APIs, the narrow manual-review marker mutation, and the separately authenticated OSS
transport endpoint. API contract `1.7` keeps business results read-only;
`POST /api/runs/<workspace-id>/manual-review` accepts only a boolean `reviewed`, while
`/api/intake/oss` and its authenticated job status resource belong to the transport adapter. Injection
changes the response, never the file on disk. The page reads the
run catalog, atomic snapshot, append-only events, observable log, manifest-approved analysis files,
and DOM-data checkpoints. A direct `file://` open redirects to loopback; LAN clients use the same
relative API paths. The page has no CDN, remote font, third-party script, or remote business-data
dependency.

For completed records, `view.pass_check_log` is a schema `1.0` read-only projection with `total`,
audit-type and scope counts, and ordered `groups[].items[]`. Each item contains only a generated check
id, Chinese category/title, subject, concise basis, source basenames/count, confidence, and a presentation
scope. The trusted server rebuilds it at read time from immutable
`analysis/results/<scenario>.json` when an older snapshot predates this field; it must never modify the
archived worktree or infer a pass from an absent/failed status.

Every completed level-one ledger record has one `人工核验` checkbox. Persist this operator annotation
atomically under `worktrees/.reviews/` without rewriting the run manifest, snapshot, AI conclusion, or
error cards. A checked record displays `已人工核验` and contributes zero to the level-one
`待人工核验` metric; unchecking restores its original error count. Running and failed records do not
offer this control.

The assets under `skills/orchestrate-offline-audit/assets/` are retained only as the deterministic
run-scoped view-payload compiler. `audit_core.html_report.create_html_report_from_workbook` may assemble
them in system temporary space, `verify_html_report` must still reject structural or data drift, and
the persistent runner extracts the verified `audit-data` payload before deleting the temporary page.
No assembled per-run HTML is published.

Do not handwrite or redesign the root page during an audit. Do not reproduce it from prose, a
screenshot, or prior output; do not patch it after a run; and do not add a new button, style, layout,
label, or interaction for an ordinary business request. The page may change only when the user
explicitly requests a frontend/workbench redesign. The same change must update its version, workbench
API contract, browser verification, affected tests, AGENTS/README, and this Skill contract.

Audit System `2.7.1` freezes the persistent two-level standard: the level-one page contains system
overview, full run ledger, and technical archives. The overview contains only aggregate metrics and the
newest run records; it must not render a runtime chain, stage nodes, or observable event stream. Its
`核销完成` metric shows the top-level input ZIP count from the newest completed run, not the cumulative
number of completed run records. Its silent probe reads only the run catalog and rerenders when the catalog signature changes. The canonical
level-one aggregate metric and completed-run count label present `error_count` as `待人工核验` only for
completed records that are not manually reviewed. Reviewed records contribute zero without changing
the underlying count or the level-two disposition content. The canonical six-stage checklist
remains in manifests, snapshots, `/api/config`, and running-record refresh logic; it
is not a level-one visualization. A running level-two record reloads only when its stage advances or it
enters a terminal state, never on a fixed timer. Inactive catalog discovery is silent, and the ledger
also retains its explicit manual refresh. A full-page reload always opens system overview; the ledger
state saved before opening a level-two record is consumed only once when returning. Module actions stay
exclusive: overview summaries have no record buttons, ledger rows only open records, and technical-
archive cards only open logs and checkpoints. Every run expands into its own
level-two page. A completed level-two page preserves the approved original dark rail and cold-gray grid
canvas. Its rail contains exactly `错误总览` and `正确检查项`: the first stays the default red-accented
view with four grouped-error metrics and vertically stacked error cards; the second is a green-accented
human-readable pass ledger grouped first by audit type and then by check category. Both views must
support combinable client-side filters, show the current match count, offer a one-action reset, and never
modify the underlying snapshot. Audit type is fixed: it always lists every type present in the view and
is never narrowed by another filter. The error overview also filters by `错误原因分类`, confidence, and
free text. Its categories use actionable fine-grained labels such as `陈列标准`, `活动日期`,
`门店地点低置信度`, `金额复算`, and `POS销售明细`, never broad umbrella labels. The pass ledger filters
by check category, confidence, and free text. Error-reason/check
category and confidence are linked facets: omit zero-result options under the other active conditions
and append the corresponding match count to each remaining option. Error-reason classification is a
deterministic client projection from immutable error rows and does not add or mutate an API field. All
grouped errors remain visible before filtering in the default overview. Each header keeps only the
audit-type and confidence badges. One or more category-value-only chips appear inside the error-reason
field without an `错误原因分类` prefix and use the exact same `eo-chip` styling as `陈列标准`; there is no
separate light category-chip treatment. Neither
view has a scenario
queue, entry button, or per-type subpage. A running or
failed page preserves only truthful status and diagnostic data. Customer output uses Chinese business
labels. Technical JSON is visible only in the explicitly labeled technical archive and must never
include model-private reasoning. Formal frontend delivery is PC-only: verify at 1440×960 and keep
desktop widths of 1280px or greater usable. Narrow-screen CSS is best-effort fallback and is not a
mobile configuration or acceptance requirement. Error cards must not drop a source file, cause, action,
impact, or conclusion.

When a result view has exactly one audit type, keep the audit-type select enabled with only that
concrete option and omit `全部核销方式`; multi-type views retain the all option. Error-card types and
facet types use the same canonical business label, and every error-reason category must resolve to at
least one card carrying a concrete type. Both sibling views share judgment-confidence logic. Prefer an
explicit AI `confidence_score` in `[0,1]`, bucketed high at `>=0.85`, medium at `>=0.60`, otherwise low.
If it is absent, score every level within its bucket using concrete evidence, corroboration, ambiguity,
and missing-material signals; do not assign every low item a constant `0.5` or every high item an
automatic `1`. Reserve `1` for an explicit fully-certain judgment. Cards always show level and score,
including `置信度 高：1`; confidence filters show only high/medium/low without numeric values. This
measures confidence that the item-level judgment is accurate, not severity or an internal
similarity/relevance score.

## Output and verification

Keep exactly one customer-facing HTML at:

`offline-activity-audit.html`

An ordinary run never rewrites that file. Publish its persistent archive to
`worktrees/<YYYYMMDD_HHMM_SS>-<audit-model>_<reasoning-effort>/`. Keep `producer-model` as provenance,
but name the directory with the actual audit model and primary reasoning effort. Require `run-id` to
begin with a valid `YYYYMMDD` business date and append the local task-start time. Remove safe model-name
separators when compacting the path label: `gpt-5.6-sol + xhigh` becomes
`20260902_1755_32-gpt5.6sol_xhigh/`, while `qwen3.8 + max` becomes
`20260902_1755_32-qwen3.8_max/`. Never emit `-1.1`, `-1.2`, or another revision suffix for a new
worktree and never overwrite an existing exact name. Preserve old-name worktrees and legacy HTML files
under their historical IDs as read-only compatibility inputs.

Each archive contains `manifest.json`, atomic `snapshot.json`, `logs/events.jsonl`, `logs/run.log`,
schema-validated `analysis/` files, and `dom/checkpoints/`. Do not publish a run HTML or workbook.
Before completion, verify business/result schemas, temporary six-column and HTML projections, complete
scenario/record coverage, the fixed workbench API payload, and all listed analysis/checkpoint hashes.
Confirm every temporary workbook, extracted source, model workspace, and temporary page is removed.
A failed run preserves only its safe diagnostic archive and must not claim a completed customer result.

Start the trusted service with:

```powershell
py -3 -B skills/orchestrate-offline-audit/scripts/serve.py --host 0.0.0.0 --port 8080
```

Add `--enable-oss-intake --oss-allowed-host <exact-host>` and set
`OFFLINE_AUDIT_OSS_WEBHOOK_SECRET` only when the authenticated OSS adapter is required. Never place the
secret or a pre-signed object URL in a command line, tracked file, job receipt, worktree, or log.

Verify `/api/config`, `/api/runs`, the selected run snapshot/log/events, every manifest-approved
analysis/checkpoint resource, legacy HTML compatibility, desktop rendering, scenario and technical-tab
interactions, refresh-to-overview behavior, and zero browser errors at 1440×960. When the user designates an
approved reference HTML, use it only for an explicit root-workbench redesign acceptance; runtime code
and model prompts must never use it to fill business evidence or run data.
