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

Never approve an amount only because totals match. Preserve source archive hashes, raw paths, Excel rows, contract or settlement lines, visible limitations, and per-item/per-store evidence.

## Output contract

Create every result from an empty workbook and write it directly to:

`worktrees/<YYYYMMDD>-<producer-model>.xlsx`

`producer-model` 必须由实际执行本次任务的模型明确传入：Codex 使用 `codex`，其他模型
使用可识别的安全标签，例如 `qwen3.7`。`run-id` 必须以有效的 `YYYYMMDD` 业务日期
开头，后续追踪标识不进入最终文件名。首次 Codex 结果例如 `20260818-codex.xlsx`；
同日期、同模型再次生成（包括同一输入重跑）时依次使用
`20260818-codex-1.1.xlsx`、`20260818-codex-1.2.xlsx`。

Never overwrite an existing result. Keep a separate revision sequence for every date and producer model. Do not create persistent run directories, Git run branches, repository snapshots, caches, or separate delivery locations.

The workbook has one six-column sheet per submitted scenario. With both scenarios, sheet order is `人员激励核销`, then `堆头核销`. It must contain no formulas or formula-error values.

The workbook under `worktrees/` whose name includes `已追加产品促销` is acceptance-only. Runtime code and model prompts must never open, copy, or depend on it.

## Change verification

Run at least:

```powershell
py -3 -B -m unittest discover -s tests -v
py -3 -B -m compileall -q audit_core skills
```

Also validate all JSON files and Skill frontmatter, run the formal command against the retained real ZIP inputs when the execution path changes, verify the generated workbook, and finish with `git diff --check` and `git status --short`.
