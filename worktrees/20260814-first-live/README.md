# 线下活动核销

项目提供一个主编排 Skill、两个业务子 Skill、一个主程序入口和统一输出机制：

- `orchestrate-offline-audit`：读取 `input/` 中的两个 ZIP，安全解压并按场景调用子 Skill；
- `audit-personnel-incentive`：人员激励，强制按结算单 SKU 与销售 Excel 数量逐项核对；
- `audit-promotional-display`：堆头活动，按合同门店逐店检查照片、日期、地点、陈列和销售支撑；
- `main.py`：可信主入口，负责分配 Git linked worktree、调用 Skill、校验结果并导出交付物。

## 正式运行

标准入口只需要把一个人员激励 ZIP 和一个堆头 ZIP 放进 [input](input/README.md)，然后运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id 20260814-001
```

等价的主程序命令：

```powershell
py -3 main.py input --run-id 20260814-001
```

默认从 `<project>/input` 读取，默认导出到项目同级的 `audit-output/<run-id>`。主 Skill 自动调用 Codex 提取证据并路由两个子 Skill。

只检查、解压和生成路由配置，不启动 Codex 或 worktree：

```powershell
py -3 main.py input --run-id 20260814-check --prepare-only
```

以下接口保留给已经准备好 case/evidence JSON 的高级调用。

人员激励或单个堆头案件：

```powershell
py -3 main.py run `
  --case C:\audit-input\case.json `
  --evidence C:\audit-input\evidence.json `
  --output-dir C:\audit-deliveries `
  --run-id 20260814-001
```

没有预提取的 evidence JSON 时，可使用 `--agent` 让 Codex 按对应 Skill 提取证据：

```powershell
py -3 main.py run --case C:\audit-input\case.json --agent `
  --output-dir C:\audit-deliveries --run-id 20260814-001
```

批量核销：

```powershell
py -3 main.py batch `
  --batch C:\audit-input\batch.json `
  --output-dir C:\audit-deliveries `
  --run-id 20260814-batch-001
```

`--output-dir` 必须位于项目 Git 仓库之外，且 `<output-dir>/<run-id>` 不得预先存在。

## Worktree 生命周期

每次运行固定绑定：

- worktree：`worktrees/<run-id>`；
- 分支：`run/offline-audit/<run-id>`；
- 代码快照：由隔离 Git index 生成，不修改、不 stash、不覆盖主工作区；
- 正式产物：先写入 worktree 的 `output/`，成功或失败后均形成 Git checkpoint；
- 外部交付：checkpoint 成功后复制到 `<output-dir>/<run-id>`。

主入口会拒绝重复 run-id、残留分支、残留 worktree、危险未跟踪文件以及运行过程中发生的主工作区漂移。

查看运行：

```powershell
py -3 main.py worktree list
py -3 main.py worktree status --run-id 20260814-001
```

删除已导出且 Git 状态干净的运行：

```powershell
py -3 main.py worktree remove `
  --run-id 20260814-001 `
  --confirm-run-id 20260814-001
```

删除会移除该 linked worktree 和专属运行分支；外部导出目录不会被自动删除。

## 输出结构

```text
output/
├── worktree-preflight.json
├── worktree-allocation.json
├── worktree-run-state.json
├── input-archive-manifest.json       # 主 Skill 的 ZIP 哈希、解压清单与路由
├── skill-routing.json                # 主 Skill → 两个子 Skill 的调用关系
├── delivery.json
├── <核销报告>.xlsx
├── workbook-verification.json        # 批量运行
├── runs/                              # 冻结输入、日志和结构化结果
└── supervisor/
    ├── stdout.log
    └── stderr.log
```

外部导出目录另含 `export-receipt.json`，记录 checkpoint commit、文件清单和 SHA-256。

证据 JSON 的字段合同位于两个 Skill 的 `references/evidence.schema.json`；最终结果统一通过 `contracts/audit-result.schema.json` 校验。
