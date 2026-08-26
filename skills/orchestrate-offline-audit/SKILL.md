---
name: orchestrate-offline-audit
description: Generate a fresh offline-activity reimbursement Excel plus a self-contained local HTML viewer from one or two ZIP submissions, covering personnel incentives, promotional/stack displays, or both. Use whenever input/ contains new offline audit materials and Codex must safely classify them, extract only visual facts with AI, deterministically read sales Excel, calculate and judge every item/store, and publish the fixed six-column Chinese result without using prior outputs or a gold workbook at runtime.
---

# Orchestrate Offline Audit

Use this Skill as the only formal entry for this project:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```

Do not introduce a second formal CLI, Git run branch, persistent snapshot, separate delivery directory, template-copy step, or post-run replacement workflow. The same formal run publishes both formats.

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
  or unique fuzzy product name; conditional contract-product reconciliation; field-photo
  text retrieval followed by bounded reference-image comparison; a unique visible registered short
  code such as `SP-1` is enough without a full name or visible photo barcode; then compare the
  resolved catalog product's strict code and 69 code with sales while allowing fuzzy names;
- image hashes and duplicate screening;
- dates, quantities, rewards, transfers, claims, supported amounts, and final pass/supplement decisions;
- result-schema validation, workbook generation, naming, and workbook verification.

## Routing and audit

Process scenarios in this fixed order when both exist:

1. `personnel_incentive` → read [audit-personnel-incentive](../audit-personnel-incentive/SKILL.md) and its linked rules/schema completely.
2. `promotional_display` → read [audit-promotional-display](../audit-promotional-display/SKILL.md) and its linked rules/schema completely.

Validate AI evidence before calculation and validate each deterministic result against `contracts/audit-result.schema.json` before writing Excel. An aggregate match never substitutes for a line/store control.

## Workbook and local-view contract

Create a new workbook from an empty `openpyxl.Workbook`; never load, copy, edit, or use an existing `.xlsx` as a template. The acceptance workbook under `worktrees/` is test-only.

- Use exactly six columns and one sheet per submitted type.
- With both types, sheet order is `人员激励核销`, then `堆头核销`.
- With one type, include only its sheet.
- Freeze only the two merged title rows and row 3 header (`A4`) on every sheet; never freeze data rows.
- Reproduce the two merged title rows, header colors, wrapping, borders, widths, row heights, tab colors, frozen panes, filters, status fills, and total row defined by deterministic report code.
- Use no formulas. Verification must find zero formula cells and zero formula-error values.

Personnel rows:

- one row per settlement product;
- title each `结算第N行` row with the uniquely selected knowledge-base product's complete authoritative name; retain the settlement image's recognized short/original name only in the visual-evidence column, and use it as the title only when no knowledge product was uniquely established;
- first show the code-read Excel product and the selected knowledge product code/name/69 code;
- require an exact valid 69-code match and an exact or unique fuzzy product-name match before the
  Excel quantity can support reward, then compare it with the AI-read settlement quantity/reward;
- treat a unique fuzzy product-name correspondence as an accepted medium-confidence result; when
  69 code, quantity, and reward pass, write `要重新提交：不用` rather than requesting a clearer
  settlement line solely because the name is not character-for-character equal;
- then add total, actual-claim, and recipient/date rows;
- show every product with only one overall `置信度：高`, `置信度：中`, or `置信度：低` label; keep exact/fuzzy/mismatch wording only in the short field comparison, followed by exactly what must be resubmitted or `不用`;
- never mark recipient/store/date verified unless recipient, store correspondence, and complete transfer date are all visible.

Display rows:

- one row per contract store;
- column A: human summary of contracting parties, activity budget/claim, execution period, activity
  content, merchants/stores, stack counts, watermark/seal, display, and optional product/promotion
  terms; render the seal result as a separate `盖章：是/否` line immediately after the watermark
  line; omit a product or promotion line when that optional requirement is absent instead of showing
  a negative placeholder; put full global terms in the first row and only store-specific terms afterward;
- column B: submitted basenames, useful visible text, date, location, display result and short visual
  basis, visible vertical count, cross-store reuse result, catalog-backed product, promotion,
  match result, and high/medium/low confidence;
- column C: code-read customer and business date, then every sales row related to the field-photo
  product. Within the existing cell, show the catalog product and corresponding Excel row as separate
  labeled product-code/product-name/69-code/quantity blocks instead of slash-delimited identity text;
  the HTML view renders the same visible fields as compact key/value tables. Follow them with all three
  field results and one `置信度：高/中/低` label. Do not show unrelated rows or truncate relevant rows;
- column D: exactly three concise internal comparisons — contract ↔ photo, photo ↔ sales, and
  contract ↔ sales — each shown with one `置信度：高/中/低` label and one short reason; exact, fuzzy,
  or mismatch wording stays only in the field-specific explanation;
- column E: explicit fee basis, unit count × unit fee, and supported amount;
- column F: `通过` or `暂不能核销`, followed by concrete resubmission requests grouped as at most
  one sales-Excel request, one contract request, and one field-photo request. State the affected
  rows/fields or the exact photo content that must be visible.

Do not append another detail table. Write the total row immediately after the last store. Excel is
the reader's complete plain-language handoff: do not render full candidate sets,
raw-versus-authority dumps, exact/perceptual hashes, or long model reasoning, and never tell the
reader to consult JSON, an internal result, or another unavailable detail file. Internal structured
evidence remains only for deterministic validation and traceability. Never replace a sales problem
with a bare `未匹配`.

Visible products come only from photo packaging. Excel identities come only from original cells read
by code. The validated product catalog is the mandatory identity ledger: first determine each field
product, then locate sales rows afresh by its exact catalog 69 code plus a compatible name and require
each row to resolve independently to that same catalog product before strictly verifying the product
code. A photo short code never directly selects a sales row. A bad unrelated
sales row remains an internal whole-file diagnostic and cannot fail every store. A specific contract
product also maps first; a generic whole-brand scope is not applicable. Product code and 69 code are
strict; only the name may be exact or uniquely fuzzy. An unregistered `020...` distributor code fails
even when name and 69 code agree. Check
the photo's 69 code only after the photo has identified a catalog product: use that catalog product's
registered 69 code for the sales comparison, and mark the comparison unavailable when photo identity
is still fuzzy. Check
contract parties/customer, contract period/business date, and watermark/seal integrity before store
approval. Product-reference views cannot prove any non-identity control. An ordinary price is not a
promotion. Treat only explicit special price, old/new price, discount, gift, multi-buy, `1+1`,
`3+2`, or value-pack evidence as promotion; promotion remains conditional on the contract. Calculate
amount only from an explicit per-store or per-stack unit basis; never divide a total automatically.

After the temporary workbook passes verification, deterministically render its complete visible
six-column values into one sibling UTF-8 HTML file. The HTML is only a reader-friendly projection:
it must preserve every sheet, header, row, line break, conclusion, and resubmission request exactly
and must not introduce a second calculation or judgment path. Keep all CSS, JavaScript, and data
inline so the page opens from `file://` without a server, network, CDN, font download, or asset folder.
Provide buttons for scenario switching, keyword search, conclusion filtering, compact display,
expand/collapse, copying a row conclusion, opening the sibling Excel, printing, and returning to the
top. Apply the same no-truncation, plain-language, and forbidden-engineering-term rules as Excel.

## Output and verification

Write the same stem directly to:

`worktrees/<YYYYMMDD>-<producer-model>.xlsx`

`worktrees/<YYYYMMDD>-<producer-model>.html`

`producer-model` is required provenance supplied by the executing model: use `codex` for Codex and an explicit model label such as `qwen3.7` for another model. Normalize it to lowercase safe filename characters; never guess another model's identity. Require `run-id` to begin with a valid `YYYYMMDD` business date and omit the rest of the run identity from the delivery filename. The first Codex result is `20260818-codex.xlsx` plus `20260818-codex.html`. Never overwrite: another result for the same date and producer, including a rerun of the same input, becomes the sibling pair `20260818-codex-1.1.xlsx/.html`, then `20260818-codex-1.2.xlsx/.html`; keep an independent sequence for each producer, and let either extension reserve its revision. Publish only after the temporary workbook passes sheet order, six-column shape, merge, `A4` freeze, filter, formula, and error checks and the temporary HTML passes sheet/row equality, button, UTF-8, and zero-external-dependency checks. A failed run must not leave either final sibling behind.
