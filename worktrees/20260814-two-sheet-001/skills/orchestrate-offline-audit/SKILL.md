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
7. Read [analysis-dimensions.md](references/analysis-dimensions.md) and evaluate all seven 1.4.1 controls separately for each scenario. A missing control must be reported as `证据不足` or `本案未触发`; it must never disappear from the result or be silently treated as passed.
8. Keep model work limited to evidence extraction. Require deterministic code to reread Excel, calculate quantities and amounts, validate both result schemas, generate the combined workbook, inventory formulas, and checkpoint the complete `output/` tree.
9. Summarize `通过`, `异常`, and `待补件` conclusions before presenting amounts. If a model extraction attempt fails, retry up to three total attempts, count every successful/failed invocation and its returned Token usage, and keep only this structured usage summary.
10. Export exactly one combined Excel workbook to `<output-dir>/<run-id>`. The workbook must use exactly one worksheet per included audit method: `人员激励核销` and `堆头核销` for the standard two-package run, with no auxiliary worksheets. Each scenario worksheet must contain its own amount summary, workflow, source-file reading inventory, core comparisons, bottom-level evidence, 1.4.1 controls, exceptions/supplements, and model-call/retry usage. Keep structured evidence and state only inside the checkpointed worktree; do not persist stdout/stderr, model event streams, or other run logs.

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
- Do not call a file authentic merely because its SHA-256 is stable or no duplicate was found. State whether EXIF/GPS, device information, watermark corroboration, digital signatures, and cross-activity history were actually available.
- Preserve failed-run inputs, structured evidence, and run state in the worktree checkpoint; do not persist logs or silently rerun under the same run ID.
