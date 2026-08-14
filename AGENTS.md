# Offline activity audit project

Keep extraction and judgment separate. Model or vision steps may only extract source facts into schema-validated JSON. Deterministic Python must read Excel files, aggregate rows, calculate differences and supported amounts, validate final results, hash evidence, and generate reports.

The supported scenarios are fixed:

- `personnel_incentive` routes to `skills/audit-personnel-incentive`.
- `promotional_display` routes to `skills/audit-promotional-display`.

Formal runs must start from the primary Git worktree through `main.py`. The trusted main entry creates one run-specific branch and linked worktree, executes inside that snapshot, checkpoints `output/`, and only then exports a copy. Do not run formal audits directly in the primary workspace or overwrite source evidence.

Never approve an amount solely because aggregate totals match. Preserve raw source paths, Excel row numbers, contract or settlement line numbers, explicit limitations, source hashes, run identity, snapshot commit, and checkpoint commit.
