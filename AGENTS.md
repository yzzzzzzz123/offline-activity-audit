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

Deterministic Python must safely unpack and route ZIPs, read Excel cells, preserve original source names and rows, aggregate quantities, map products, calculate differences and supported amounts, detect duplicate images, validate results, and generate the workbook.

For `personnel_incentive`, deterministic code must reconcile every aggregated sales-Excel SKU
through the same validated repository product knowledge base before it can support an incentive
amount. The personnel Excel has no product-code field: its valid EAN-13/69 code must exactly match
the catalog, and its original product name must match exactly or by one strong, unique fuzzy match
within only that exact-barcode candidate set. Same-code ambiguity, an invalid/unregistered 69 code,
or an incompatible name fails the product gate. Preserve the diagnostic mapping but cap the affected
SKU's supported reward at zero. Catalog identity comes from code; product-reference images are not
sent to the personnel vision pass and are not settlement or transfer evidence.

For `promotional_display`, the validated repository product knowledge base is the authoritative
product-identity ledger. Deterministic code must first reconcile every sales-Excel row by its
source product code, product name, and 69 code: product code and valid EAN-13 must strictly match,
while the product name may be exact or uniquely fuzzy for that same strict product. It must then
reconcile any specifically required contract
product; and resolve every field-photo product through the same catalog. Only then may it compare
submitted files by catalog product IDs, compatible names, and strict 69 codes. A sales row that cannot map to the catalog, maps
ambiguously, has conflicting identifiers, has an invalid 69 code, or lacks a required identity
field fails the sales-file knowledge gate. An unregistered external product code fails even when
the name and barcode appear plausible. Raw source values remain immutable and must never be silently
normalized into a pass.

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

The same display audit must then close three internal comparisons: contract signing party/period/
integrity against sales customer/business date, contract store/date/display/promotion/optional
product against field photos, and sales identity against photo identity. Amount is automatic only
for an explicit per-store or per-stack unit basis; a total-only or unclear contract is never divided
automatically.

Never approve an amount only because totals match. Preserve source archive hashes, raw paths, Excel rows, contract or settlement lines, visible limitations, and per-item/per-store evidence.

## Output contract

Create every result from an empty workbook and publish two sibling delivery files directly to:

`worktrees/<YYYYMMDD>-<producer-model>.xlsx`

`worktrees/<YYYYMMDD>-<producer-model>.html`

`producer-model` 必须由实际执行本次任务的模型明确传入：Codex 使用 `codex`，其他模型
使用可识别的安全标签，例如 `qwen3.7`。`run-id` 必须以有效的 `YYYYMMDD` 业务日期
开头，后续追踪标识不进入最终文件名。首次 Codex 结果例如 `20260818-codex.xlsx`；
同日期、同模型再次生成（包括同一输入重跑）时依次使用
`20260818-codex-1.1.xlsx`、`20260818-codex-1.2.xlsx`。

Never overwrite an existing result. The Excel and HTML from one run must use the same stem and revision. Keep a separate revision sequence for every date and producer model, and treat either extension as reserving that revision. Revision labels are monotonic audit identities: if an older workbook or page is moved or deleted, select one greater than the highest remaining revision and never refill the missing label. Do not create persistent run directories, Git run branches, repository snapshots, caches, or separate delivery locations.

The workbook has one six-column sheet per submitted scenario. With both scenarios, sheet order is `人员激励核销`, then `堆头核销`. It must contain no formulas or formula-error values. The personnel
sheet shows the Excel product, selected knowledge-base product code/name/69 code, exact barcode
result, exact-or-fuzzy name result, settlement comparison, amount comparison, and concrete
resubmission action in the existing six columns. Each personnel product row title uses only the complete
name of the uniquely selected knowledge-base product, without a `结算第N行` prefix; the settlement image's recognized name stays in the
visual-evidence column and becomes the title only when no knowledge product was uniquely resolved. The display
sheet keeps only the fixed six-column contract-store rows followed immediately by its total row.
It is a human-readable management summary: A shows contract terms, B shows field-photo facts and the
catalog product established from them, C shows only the sales rows relevant to that field product,
D shows exactly the three internal comparisons, E shows the short amount calculation, and F shows
the decision plus at most one concrete resubmission request per source file type. Never append a
secondary sales-detail table. In the first contract row, the seal result must occupy its own
`盖章：是/否` line immediately after the watermark line. Omit the product or promotion line entirely
when the contract does not contain that optional requirement; never print a negative placeholder.
Within column C, render each field-photo catalog product and its corresponding sales row as separate
labeled code/name/69-code/quantity blocks inside the existing cell, rather than a slash-delimited
identity sentence. The HTML view renders those same visible fields as compact key/value tables. The
field photo establishes the catalog product first. Sales-row
selection then starts again from that product's exact registered 69 code plus an exact or
fuzzy-compatible product name; the row must independently resolve to the same catalog product before
its product-code cell is strictly verified. Every relevant row must be shown without arbitrary top-N truncation and must
say what Excel contains, what the catalog product contains, and which fields agree or differ; a bare
`未匹配` is forbidden. A bad sales row unrelated to the store's field product remains an internal
diagnostic and must not fail every contract store.
A catalog-unique registered short code may establish the photo product, but it stops at that boundary.
The same short code occurring inside a sales product name must never locate a row, override a different
69 code/name, or justify rewriting an independently different product row. If no exact-69 plus
compatible-name row resolves to the photo product, report that the valid sales row is missing.
Full field comparisons, candidate sets, image hashes, visual bases, and reasoning boundaries remain
in the validated audit JSON and must not be dumped into Excel cells.

Every product or relationship shown in Excel uses one overall status only: `置信度：高`,
`置信度：中`, or `置信度：低`. Exact, fuzzy, or mismatch wording may remain only in the short
field-by-field explanation and must not be repeated as a separate overall match label. Excel must use plain language and tell the reader exactly what to resubmit, naming
the file type, Excel rows/fields, or visible photo content. The workbook is the reader's complete
handoff: never write `详见审计JSON`, `见内部结果`, or point to another unavailable file. Engineering
terms such as candidate hit, RAG, product ID, convergence, raw hash, or SKU stay out of cells.

The HTML is a second deterministic view of the just-generated workbook, not a second audit result.
It must embed the complete visible six-column content of every workbook row in one UTF-8 file and
must not add, omit, reinterpret, truncate, or independently calculate any business fact. It must
open directly from disk without a server, network, CDN, font download, external JavaScript, or
separate asset directory. Provide ordinary-reader controls for scenario switching, keyword search,
pass/supplement/summary filtering, compact view, expand/collapse, opening the sibling Excel, printing,
copying a row conclusion, and returning to the top. The same plain-language and forbidden-engineering-
term rules apply to both formats. Generate and verify both temporary files before publishing either;
a failed run must leave neither final sibling behind.

Treat the HTML as a quiet internal review tool, not a marketing page or a decorative dashboard.
Use a compact Chinese header, a desktop overview sidebar plus reading workspace, a cool neutral
background, one deep-green interaction accent, and semantic colors only for pass, supplement, and
summary states. Use one offline sans-serif font stack, 12px panel corners, and 8px control corners.
Do not add a hero banner, calligraphy, English eyebrow labels, decorative watermarks or dots,
textures, gradients, circular row numbers, heavy shadows, or staged entry animation. The first
record must be visible in the initial desktop viewport and in a 390px mobile viewport; the page must
not overflow horizontally, and source filenames must wrap instead of being clipped. Keep row kind
and business status independent so a summary row that requests material remains visible under the
supplement filter. Honor reduced-motion preferences and avoid continuous scroll listeners.

The workbook under `worktrees/` whose name includes `已追加产品促销` is acceptance-only. Runtime code and model prompts must never open, copy, or depend on it.

## Change verification

Run at least:

```powershell
py -3 -B -m unittest discover -s tests -v
py -3 -B -m compileall -q audit_core skills
```

Also validate all JSON files and Skill frontmatter, run the formal command against the retained real ZIP inputs when the execution path changes, verify the generated workbook, and finish with `git diff --check` and `git status --short`.
