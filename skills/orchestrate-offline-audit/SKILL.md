---
name: orchestrate-offline-audit
description: Generate a fresh offline-activity reimbursement Excel from one or two ZIP submissions, covering personnel incentives, promotional/stack displays, or both. Use whenever input/ contains new offline audit materials and Codex must safely classify them, extract only visual facts with AI, deterministically read sales Excel, calculate and judge every item/store, and produce the fixed six-column Chinese workbook without using prior outputs or a gold workbook at runtime.
---

# Orchestrate Offline Audit

Use this Skill as the only formal entry for this project:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <run-id> --producer-model <producer-model>
```

Do not introduce a second formal CLI, Git run branch, persistent snapshot, separate delivery directory, template-copy step, or post-run replacement workflow.

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
- barcode/product correspondence and exact/candidate/unmatched classification;
- image hashes and duplicate screening;
- dates, quantities, rewards, transfers, claims, supported amounts, and final pass/supplement decisions;
- result-schema validation, workbook generation, naming, and workbook verification.

## Routing and audit

Process scenarios in this fixed order when both exist:

1. `personnel_incentive` → read [audit-personnel-incentive](../audit-personnel-incentive/SKILL.md) and its linked rules/schema completely.
2. `promotional_display` → read [audit-promotional-display](../audit-promotional-display/SKILL.md) and its linked rules/schema completely.

Validate AI evidence before calculation and validate each deterministic result against `contracts/audit-result.schema.json` before writing Excel. An aggregate match never substitutes for a line/store control.

## Workbook contract

Create a new workbook from an empty `openpyxl.Workbook`; never load, copy, edit, or use an existing `.xlsx` as a template. The acceptance workbook under `worktrees/` is test-only.

- Use exactly six columns and one sheet per submitted type.
- With both types, sheet order is `人员激励核销`, then `堆头核销`.
- With one type, include only its sheet.
- Freeze only the two merged title rows and row 3 header (`A4`) on every sheet; never freeze data rows.
- Reproduce the two merged title rows, header colors, wrapping, borders, widths, row heights, tab colors, frozen panes, filters, status fills, and total row defined by deterministic report code.
- Use no formulas. Verification must find zero formula cells and zero formula-error values.

Personnel rows:

- one row per settlement product;
- compare code-read Excel barcode/quantity/calculated reward with AI-read settlement quantity/reward;
- then add total, actual-claim, and recipient/date rows;
- when an amount/quantity matches but the product mapping is below high confidence, write exactly `金额匹配，商品身份未验证`;
- never mark recipient/store/date verified unless recipient, store correspondence, and complete transfer date are all visible.

Display rows:

- one row per contract store;
- column A: contract store, per-store fee, display requirement;
- column B: code-read customer/period, exact source-cell product names, `明确对应/候选对应/未匹配`, and the store-level-sales limitation;
- column C, in strict order: file, recognized date, recognized location, display-standard result, concrete visual basis, cross-store photo-reuse result, recognized products, promotion information;
- column D: photo versus contract date, location, display standard, and photo reuse, with the last two controls kept separate;
- column E: deterministic supported amount;
- column F: `通过` or the exact supplement request.

Visible products come only from photo packaging. Excel names come only from original cells read by code. An ordinary price is not a promotion. Treat only explicit special price, old/new price, discount, gift, multi-buy, `1+1`, `3+2`, or value-pack evidence as promotion. Product/promotion recognition is auxiliary unless the contract explicitly requires it.

## Output and verification

Write directly to:

`worktrees/<YYYYMMDD>-<producer-model>.xlsx`

`producer-model` is required provenance supplied by the executing model: use `codex` for Codex and an explicit model label such as `qwen3.7` for another model. Normalize it to lowercase safe filename characters; never guess another model's identity. Require `run-id` to begin with a valid `YYYYMMDD` business date and omit the rest of the run identity from the delivery filename. The first Codex result is `20260818-codex.xlsx`. Never overwrite: another result for the same date and producer, including a rerun of the same input, becomes `20260818-codex-1.1.xlsx`, then `20260818-codex-1.2.xlsx`; keep an independent sequence for each producer. Publish only after the temporary workbook passes sheet order, six-column shape, merge, `A4` freeze, filter, formula, and error checks. A failed run must not leave a partial final filename.
