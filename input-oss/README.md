# OSS 自动输入目录

启用 `POST /api/intake/oss` 后，上游使用 `verifyCode`、`analyzeId`、`downloadUrl` 提交任务，
服务会把每个通过传输校验的原始 ZIP 持久保存为：

```text
input-oss/<job_id>/<OSS 对象原始文件名>.zip
```

每个 `job_id` 使用独立子目录，因此同名对象不会覆盖。正式核销 runner 直接把对应子目录作为
本次运行的 `--input-dir`；人工材料仍只放在相邻的 `input/`，两类输入不会混跑。

原始 ZIP 在任务完成或失败后都会保留，便于追溯和人工复核。服务记录下载响应的 ETag、
实际大小和自行计算的 SHA-256；下载未完成、超过上限或 ZIP 格式校验失败时，残缺文件会删除。
正式 worktree 完成后，服务使用相同 `verifyCode`、`analyzeId` 和中文 `result` 调用配置的
`/api/v1/ai/analyze/callback`。回调失败不会删除原包或已完成 worktree，也不会重新运行 AI。
解压内容、模型工作区、临时工作簿、页面投影和子进程结果回执仍属于运行期临时产物，结束后
自动清理。

运行产生的目录和 ZIP 不进入 Git；本说明文件是本目录唯一受跟踪文件。删除历史原包前，先
确认对应任务已结束并按业务留存要求处理。删除 `input-oss` 中的 ZIP 不会删除已经生成的
`worktrees`，但会失去原始 OSS 输入副本。
