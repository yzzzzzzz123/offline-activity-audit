---
name: audit-personnel-incentive
description: Audit offline personnel-incentive reimbursement cases from sales Excel files, settlement sheets, and transfer screenshots. Use when a claim must reconcile every settlement product to exactly one sales SKU/barcode, trace quantities back to store-level Excel rows, verify reward calculations and transfer evidence, detect duplicate screenshot views, and produce a structured personnel-incentive audit result.
---

# Audit Personnel Incentive

Build an auditable chain from raw sales rows to settlement lines, payment evidence, and the claimed amount. Treat quantity reconciliation as the primary control; an amount-only match is insufficient.

## Required workflow

1. Inventory every submitted file and identify the sales workbook, final settlement sheet, and all transfer screenshots.
2. Read the complete sales workbook. Preserve the Excel row number, store, barcode, product name, quantity, unit price, and activity-period text for every detail row. Ignore subtotal, total, and blank rows.
3. Extract every settlement line from the final settlement sheet, including product name, sales quantity, reward unit price, reward amount, declared totals, actual claimed amount, activity period, customer, seal, and signing date.
4. Map each settlement line to exactly one Excel SKU, preferably by barcode. When the settlement sheet has no barcode, use the full product identity and record the selected Excel barcode, basis, and confidence. Never map two settlement lines to the same barcode or silently merge products.
5. Reconcile each mapped line independently:
   - Sum all store-level Excel rows for the mapped barcode.
   - Compare that sum with the settlement sales quantity.
   - Recalculate `quantity × reward unit price`.
   - Compare the recalculated amount with the settlement reward amount.
   - Retain the exact Excel source-row numbers and per-store quantities.
6. Reconcile the totals only after all individual lines pass or are explicitly marked as exceptions.
7. Calculate SHA-256 and pHash for the settlement/transfer images, then deduplicate mirrored sender/receiver views of the same transfer by visible business evidence. Keep the visible occurrence count and deduplication basis; a pHash near-match is only a review lead. Do not infer a store identity from amount alone.
8. Compare the multiset of store-level expected reward amounts with deduplicated transfers. If recipient/store identity or transfer date is not visible, report an identity limitation even when the amount multiset matches.
9. Compare the evidence-supported amount, settlement line total, transfer total, and actual claimed amount. Explain every difference, including underclaims and rounding/manual adjustments.
10. Return evidence JSON that conforms to [evidence.schema.json](references/evidence.schema.json). Then run `scripts/run_audit.py` through the project orchestrator to produce a result validated against `contracts/audit-result.schema.json`.
11. Return to the parent Skill for the complete 1.4.1 analysis. For personnel materials, distinguish the proven SKU/quantity/payment facts from unverified authenticity controls; missing EXIF/GPS, digital signatures, budget, invoice, contract, recipient identity, or transfer date must be shown explicitly rather than inferred from an amount match.

## Fail-closed rules

- Mark an unmatched or ambiguous settlement line for human review; do not choose the closest product silently.
- Treat missing quantity, unit price, or line amount as missing evidence.
- Treat a settlement total match as insufficient if any SKU line is unmatched or quantity-mismatched.
- Treat screenshot bubbles with the same amount as separate transfers unless the evidence supports pairing them as mirrored views.
- Treat amount-only transfer-to-store matching as identity-unverified.
- Preserve source values and distinguish extracted facts from calculated conclusions.
- Do not describe SHA-256, amount agreement, or screenshot deduplication as proof that a source file was never altered or that a transfer recipient/date is authentic.

## Output requirements

Include all of the following in the final audit:

- One row per settlement SKU with the mapped barcode, Excel quantity, settlement quantity, difference, source rows, reward calculation, and status.
- One row per raw store/SKU sales record so reviewers can trace the SKU totals.
- One row per store with expected reward, matched transfer evidence, amount difference, and identity status.
- Declared and recalculated totals, actual claimed amount, supported amount, suggested approval amount, exceptions, and supplement requests.
- A conclusion of `pass`, `conditional_pass`, `partial_pass`, or `human_review` based on the documented evidence, not on narrative plausibility.

Read [audit-rules.md](references/audit-rules.md) when deciding mappings, severity, supported amount, or conclusion.

## Formal execution

In the standard two-ZIP workflow, accept routing from the parent [orchestrate-offline-audit](../orchestrate-offline-audit/SKILL.md) Skill only after it has classified the package as `personnel_incentive` and frozen the case config.

Start formal work only through the project main entry so the trusted supervisor binds the run to a Git snapshot, dedicated branch, and linked worktree:

```powershell
py -3 main.py run --case <case.json> --evidence <evidence.json> --output-dir <external-output-dir> --run-id <unique-run-id>
```

Use `--agent` instead of `--evidence` only when Codex must extract the evidence first. The main entry executes this Skill inside `worktrees/<run-id>`, checkpoints the authoritative `output/` tree to `run/offline-audit/<run-id>`, and exports a hash-listed copy. Use `scripts/run_audit.py` directly only for isolated Skill development, never for a formal reimbursement decision.
