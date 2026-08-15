---
name: orchestrate-offline-audit
description: Orchestrate a complete offline activity reimbursement audit from exactly two ZIP submissions. Use when the input directory contains one personnel-incentive package and one promotional-display/堆头 package and Codex must safely unpack them, identify each scenario, create an isolated Git worktree, invoke the matching child Skill for each case, validate deterministic results, and export one combined delivery.
---

# Orchestrate Offline Audit

Use this Skill as the only user-facing entry for the standard two-package workflow. Keep routing, worktree allocation, child-Skill execution, result validation, and export under one run identity.

## Workflow

1. Require exactly two `.zip` files directly under the selected `input/` directory. Ignore the tracked `input/README.md` and the internal `.prepared/` directory.
2. Run the deterministic archive input gate. It must reject path traversal, symlinks, encryption, duplicate paths, unsafe Windows names, suspicious expansion, missing files, ambiguous routing, or two packages of the same type.
3. Classify exactly one package as `personnel_incentive` and exactly one as `promotional_display`. Read [routing-rules.md](references/routing-rules.md) before interpreting or changing classification behavior.
4. Generate frozen case JSON and a batch JSON under `input/.prepared/<run-id>/`. Preserve the source ZIP SHA-256 and every extracted-file SHA-256 in `input-manifest.json`.
5. Create one linked worktree at `worktrees/<run-id>` on branch `run/offline-audit/<run-id>`. Do not run the formal audit in the primary worktree.
6. Route by scenario:
   - For `personnel_incentive`, read [audit-personnel-incentive](../audit-personnel-incentive/SKILL.md) completely and apply its SKU-by-SKU quantity and transfer-evidence workflow.
   - For `promotional_display`, read [audit-promotional-display](../audit-promotional-display/SKILL.md) completely and apply its store-by-store contract, photo, date, location, display, duplicate, and sales-support workflow.
7. Keep model work limited to evidence extraction. Require deterministic code to reread Excel, calculate quantities and amounts, validate both result schemas, generate the combined workbook, inventory formulas, and checkpoint the complete `output/` tree.
8. Export the checkpointed output to `<output-dir>/<run-id>` with `export-receipt.json`. Never treat the external copy as more authoritative than the worktree checkpoint.

## Commands

Run the standard workflow from the project root:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <unique-run-id>
```

The default input directory is `<project>/input`; the default external output root is the sibling directory `audit-output`.

Validate and prepare the two ZIP files without starting Codex or allocating a worktree:

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <unique-run-id> --prepare-only
```

Use `--input-dir`, `--output-dir`, or `--model` only when the default location or model must change.

## Fail-closed rules

- Do not guess an archive type when classification scores tie or required evidence classes are missing.
- Do not route one archive to both child Skills or allow two archives to use the same child Skill.
- Do not bypass a child Skill's evidence schema, rules, or deterministic runner.
- Do not accept aggregate amount agreement as a substitute for SKU-level or store-level controls.
- Preserve failed-run inputs and logs in the run worktree checkpoint; do not silently rerun under the same run ID.
