---
name: audit-promotional-display
description: Audit offline promotional display and stack-display reimbursement cases from contracts, store lists, sales Excel files, and现场/陈列 photos. Use when each contracted store must be checked for activity period, visible date and location, store-name mapping, display standard, photo completeness and duplication, sales-data limitations, and store-by-store supported reimbursement.
---

# Audit Promotional Display

Build a store-by-store evidence chain from the contract to submitted photos and sales support. A filename or aggregate amount alone never proves execution at a contracted store.

## Required workflow

1. Inventory the contract/PDF, settlement pages, sales workbook, and every submitted display photo.
2. Extract the contract customer, activity dates, display standard, fee per store, claimed amount, and the complete numbered store list.
3. Inspect every photo at original resolution. Record its filename, visible date, visible location/store watermark, scene completeness, product/display evidence, and any quality limitation.
4. Bind photos to contract stores by contract line number and store identity. A filename is only a lead; it is not independent location or date evidence.
5. For every contracted store, decide each control separately:
   - photo present;
   - activity-period match;
   - contract-store match (`exact`, `compatible`, `mismatch`, or `filename_only`);
   - display-standard match (`pass`, `fail`, or `uncertain`);
   - exact/possible duplicate status.
6. Hash all files with SHA-256 and calculate pHash (with dHash as an auxiliary fingerprint). Record exact duplicates separately from pHash near-duplicate candidates. Compare against a historical fingerprint library only when one is actually supplied; otherwise report cross-activity reuse as unverified.
7. Read the sales workbook directly and verify customer, period, SKU count, quantity, and amount. State whether sales data is store-level or only distributor-level.
8. Award the per-store supported amount only when every mandatory store control passes. Otherwise set that store to supplement/review and state the exact missing evidence.
9. Return evidence JSON that conforms to [evidence.schema.json](references/evidence.schema.json). Then run `scripts/run_audit.py` through the project orchestrator; validate the result against `contracts/audit-result.schema.json`.
10. Return to the parent Skill for the complete 1.4.1 analysis. Report EXIF/GPS, visible watermark, image fingerprints, file readability, budget/contract comparison, invoice presence, and contract terms as separate controls with explicit capability limits.

## Fail-closed rules

- Do not infer date or GPS from a filename.
- Do not call a store matched when the visible location conflicts with the contract unless an explicit address/rename mapping is supplied.
- Do not call a display compliant when the required area, facings, layers, or other contract standard is not visible.
- Do not use distributor-level sales totals as proof that each individual store executed the display.
- Do not pass a missing contracted store merely because the overall photo count equals the store count.
- Keep `unverifiable` distinct from `mismatch` and explain the supplement needed for either result.
- Treat SHA-256/dHash as duplicate-screening evidence only. If the run has no trusted EXIF/GPS, device information, digital signature, pixel-level forgery analysis, or historical fingerprint library, state those gaps and do not claim full authenticity verification.

## Output requirements

Include one row per contract store with its photo files, visible date/location, period result, store mapping, display result, duplicate result, supported amount, risk, and supplement request. Also include sales-workbook checks, file hashes, aggregate counts, claimed amount, supported amount, and a conclusion of `pass`, `partial_pass`, or `human_review`.

Read [audit-rules.md](references/audit-rules.md) when determining mandatory controls, store-name compatibility, severity, supported amount, or conclusion.

## Formal execution

In the standard two-ZIP workflow, accept routing from the parent [orchestrate-offline-audit](../orchestrate-offline-audit/SKILL.md) Skill only after it has classified the package as `promotional_display` and frozen the case config.

Start formal work only through the project main entry so the trusted supervisor binds the run to a Git snapshot, dedicated branch, and linked worktree:

```powershell
py -3 main.py run --case <case.json> --evidence <evidence.json> --output-dir <external-output-dir> --run-id <unique-run-id>
```

Use `--agent` instead of `--evidence` only when Codex must extract the evidence first. The main entry executes this Skill inside `worktrees/<run-id>`, checkpoints the authoritative `output/` tree to `run/offline-audit/<run-id>`, and exports a hash-listed copy. Use `scripts/run_audit.py` directly only for isolated Skill development, never for a formal reimbursement decision.
