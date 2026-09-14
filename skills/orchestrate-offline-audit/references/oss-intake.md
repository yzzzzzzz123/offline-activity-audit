# OSS 接收传输契约

本参考只用于未经身份验证的内网 OSS 到核销适配器。它不是另一条业务管线，HTTP 服务内部也绝不执行场景分析。

## 生命周期

1. 可信上游调用 `POST /api/intake/oss`，并严格只传 `verifyCode`、正整数 `analyzeId` 及临时 `downloadUrl`。
2. 服务校验准确的 HTTPS 下载主机，派生稳定的 `verifyCode:analyzeId` 业务身份；若最新 attempt 仍活动则复用，
   否则在 `worktrees/.intake/jobs/` 下创建持久 attempt 回执。最新 attempt 进入终态后再次提交，会创建新 attempt。
3. 单个下载 worker 按接收顺序尽快将对象下载到 `<project>/input-oss/<job_id>/<object-basename>.zip`，
   记录响应 ETag 和实际大小、计算 SHA-256，并确认它是 ZIP。后续对象的下载不等待前一任务的正式核销或回调结束。
   完整预签名 URL 只保留到本地 ZIP 验证完成，绝不能写入回执、日志或已下载任务队列。
   不同 attempt 绝不共享目录，也不覆盖同名归档。
4. 单个核销 worker 严格串行地在子进程中启动 `skills/orchestrate-offline-audit/scripts/run.py`，
   以持久 job 目录作为 `--input-dir`。
   深层归档安全、路由、证据提取、决策、验证和发布仍由正式 runner 负责。
5. 默认模式下，正式 worktree 快照持久化后，runner 在 worktree 写入 `ai-analysis-summary.md`。
   该文件执行[统一错误原因与处理方式规范](error-reasons.md)：按 ZIP 名称确定的场景分节，每个不同错误只列具体原因，保留必要业务文件 basename、缺失字段、实际与应有数值及差额。
   重复原因合并，但不得截断不同问题或文件组，也不得替换为“另有 N 项”等占位。不重复错误标题，不含总数概况、逐文件识别过程、完整证据链、通过事实、结论、影响、运行元数据、置信度或处理建议；处理方式仅在客户界面的独立字段展示。
   适配器严格只把 `verifyCode`、`analyzeId` 和该确定性中文 Markdown 作为 `result`，POST 到已配置的完整
   `/api/v1/ai/analyze/callback` URL。默认使用 HTTPS；可信内网 HTTP 必须明确选择启用。
   回调重试只重新投递已保存结果，绝不能重跑正式核销。
   明确配置的无回调部署跳过投递，将回调状态记录为 `not_required`，并以未经身份验证的任务状态轮询作为完成通道。
   明确配置的仅接收部署在验证持久化后停止，不启动正式 runner，也不创建正式 worktree。
   后续完成的 attempt 使用同一业务二元组发送最新结果，使上游记录可以被替换；更早 attempt 的本地回执、ZIP 和 worktree 保持不变。
6. 已验证原始 ZIP 在成功或失败后仍保留在 `input-oss`，供本地追溯。
   删除不完整或传输无效的下载；解压目录、模型工作区及传输结果文件保持临时。
   成功任务指向普通本地 `worktrees/<YYYYMMDD_HHMM_SS>-<audit-model>_<reasoning-effort>/` 档案；完整时间戳取单核销 worker 实际认领并预留该运行时的上海时间，绝不取 `verifyCode` 中的业务日期。
7. 一级工作台通过 `GET /api/intake/jobs?completed=0` 展示未完成及失败 attempt：`accepted/downloading/downloaded` 标为“排队中”，`running/callback` 标为“运行中”，`failed` 标为“失败”并显示安全失败原因。完成 attempt 不占用投递区，“最近核销记录”和核销台账都只显示已完成运行，技术档案保留全状态。每张投递卡都提供同源确认的删除操作。
   删除排队任务会让 worker 跳过后续阶段；删除运行任务会先取消正式 runner、阻止尚未发出的回调，再删除该 job 的收据、专属 ZIP 和唯一关联未完成 worktree；删除失败任务会清除其收据、ZIP 和唯一关联运行 worktree（如有）。已经发送的回调不能撤回。

必须使用子进程边界，因为 CLI runner 会把进程范围的 stdout/stderr 重定向到运行日志；
若在 HTTP handler 线程中执行，并发访问日志会混入核销日志。
OSS 使用“单下载 worker → 单核销 worker”两级流水线：下载可与前一任务的 AI/回调重叠，正式核销仍严格串行，
避免多个长时视觉模型任务竞争。手动 CLI 运行仍遵循同一 worktree 预留及禁止覆盖行为。

## 启用方式

默认仍为只读工作台。只有可信内网中才通过 `--enable-oss-intake` 或 `OFFLINE_AUDIT_OSS_ENABLED=1` 明确启用接收，并提供：

- 通过重复 `--oss-allowed-host` 或逗号分隔的 `OFFLINE_AUDIT_OSS_ALLOWED_HOSTS` 提供一个或多个准确下载主机；
- 默认通过 `--oss-callback-url` 或 `OFFLINE_AUDIT_OSS_CALLBACK_URL` 提供完整 HTTPS 回调 URL；其路径必须是 `/api/v1/ai/analyze/callback`；
- 只有可信私网回调没有 TLS 端点时，才使用 `--oss-allow-http-callback` 或 `OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK=1`；公网回调绝不能启用该例外；
- 只有调用方明确不接收回调时，才使用 `--oss-no-callback` 或 `OFFLINE_AUDIT_OSS_NO_CALLBACK=1`；不得默默用此模式替代默认投递；
- 只有当前阶段仅收集文件、不做分析时，才使用 `--oss-receive-only` 或 `OFFLINE_AUDIT_OSS_RECEIVE_ONLY=1`；该模式意味着不回调，也不调用正式 runner；
- 可选回调 Bearer 凭据：`OFFLINE_AUDIT_OSS_CALLBACK_TOKEN`；
- 可选项：`OFFLINE_AUDIT_OSS_PRODUCER_MODEL`、`OFFLINE_AUDIT_OSS_MAX_BYTES`、`OFFLINE_AUDIT_OSS_DOWNLOAD_TIMEOUT`、`OFFLINE_AUDIT_OSS_CALLBACK_TIMEOUT`、`OFFLINE_AUDIT_OSS_CALLBACK_ATTEMPTS` 及 `OFFLINE_AUDIT_OSS_ALLOW_PRIVATE_HOSTS`。

提交和任务状态轮询刻意不要求 Authorization 头。不得把内置 HTTP listener 直接暴露到公网。
任何跨网络暴露前，必须在前面部署带身份验证的 HTTPS API 网关、反向代理或 VPN。
除非为准确白名单私网端点明确启用，否则拒绝 OSS 解析到私有地址。

## 请求

`POST /api/intake/oss` 接受一个不超过 64 KiB 的 UTF-8 `application/json` 对象：

```json
{
  "verifyCode": "HX202603250014",
  "analyzeId": 123,
  "downloadUrl": "https://exact-allowed-host/path/维护费用.zip?provider-signature=..."
}
```

- 三个字段都必需。`verifyCode` 必须是安全、非空的 ASCII 标识符，`analyzeId` 必须是 64 位正整数。
- API `1.25` 起接收、回调和任务状态统一使用 `analyzeId`；旧 `fileId` 请求（含混传）返回 `400`。旧回执只读映射到新字段，内部指纹保持兼容，历史文件与回调不自动迁移或重发。
- URL 路径或响应 `Content-Disposition` 应保留 Windows 安全的 `.zip` basename，其中包含正式分类器需要的业务场景标记。
- `verifyCode` 中第一个有效 `YYYYMMDD` 作为运行业务日期；不存在时使用当前上海日期。
- 默认只接受 HTTPS 443 端口、准确配置主机名、安全重定向及公网 DNS 结果。下载前拒绝 userinfo、URL fragment、任意主机及私有/链路本地/loopback 解析。
- 下载使用专属直连 opener，显式禁用环境变量及 Windows 系统代理；不更改模型调用或全局代理；回调也使用自己独立的直连 opener。仍验证 TLS 证书和每次重定向目标。
- 连接失败按固定安全消息区分拒绝连接、超时、连接重置、域名解析、TLS 证书及握手失败；不保存底层异常原文或签名 URL。下载失败不自动重试，恢复连接后以新有效签名 URL 创建下一 attempt。

`verifyCode:analyzeId` 二元组及去除查询凭据的 URL 身份构成稳定提交指纹。
同一二元组与对象路径在签名刷新后再次提交，有两种结果：

- 最新 attempt 为 `accepted`、`downloading`、`downloaded`、`running` 或 `callback` 时，返回该 attempt，不排队重复 AI 工作；
- 最新 attempt 为 `completed` 或 `failed` 后，创建 `attempt=2/3/...`，使用新的 24 位十六进制 job ID、独立 `input-oss/<job_id>/` 目录及全新正式运行。

同一二元组用于不同对象路径时仍返回 `409 Conflict`。业务记录有意移动到另一 OSS 路径时，应使用新的 `analyzeId`。

## 结果回调

默认模式在正式完成后，向已配置回调 URL 发送 UTF-8 JSON：

```json
{
  "verifyCode": "HX202603250014",
  "analyzeId": 123,
  "result": "# AI 小结\n\n## 维护费用核销\n\n- 未提交 POS 电子表。\n- 促销合同.jpg 的盖章区域模糊，无法辨认印章。"
}
```

`result` 只能从同一 worktree 的 `ai-analysis-summary.md` 读取；内容按场景分节，依共享规范只列具体原因，不重复错误标题，不拼接处理方式或完整逐文件证据链。`callback_result_format=scenario_error_facts_markdown` 的字段及三字段回调契约保持不变。
回调使用 `Idempotency-Key: <verifyCode>:<analyzeId>:<回调正文 sha256 的前 16 位十六进制>`，接受任意 `2xx`，
并可对网络错误、`408`、`429` 和 `5xx` 重试。它绝不包含 `downloadUrl`、本地绝对路径或回调凭据。
回调失败状态为 `callback_failed`；保留已完成结果，绝不能只为再次投递而重跑 AI。

## 已完成报告补发

本地 `input` 复跑没有上游 `verifyCode` / `analyzeId`，不会自动回调。客户明确要求交付该报告时，使用 `scripts/deliver_callback.py --workspace-id <workspace-id> --verify-code <verifyCode> --analyze-id <analyzeId>` 预览；同一参数加 `--apply` 进行投递。
只接受 completed 且身份、snapshot 哈希和摘要路径/大小/SHA-256 一致的 worktree。摘要过长时拒发，绝不截断。业务标识由已核对的请求或操作员明确提供，不从名称推断。
工具复用既有回调环境配置及 `post_analysis_callback`，使用独立直连、相同三字段与幂等键，不重跑 AI、不下载、不修改业务历史。
独立回执保存在 `worktrees/.intake/callback-deliveries/`，同一目标及结果已经 delivered 时跳过；失败只重试投递，互斥保护避免并发双发。回执不含地址、凭据或任意响应正文。
API `1.32` 的 `oss_intake.callback_transport=direct` 表示普通 OSS 自动回调和显式补发都绕过环境与系统代理。
`unclassified_archive_policy=fail_before_ai_and_callback_reason` 表示 ZIP 名称无法唯一确定核销类型时，在必要归档安全校验后直接生成分类拒绝报告，不调用 AI。该报告以核销失败状态先持久化，再按同一三字段回调契约发送；不能当作模型执行失败或声称材料已经分析。已知类型错件、缺件继续分析；混合多包只打回无法分类的 ZIP，其他包照常处理。

## 响应与状态

新接收任务返回 `202 Accepted`，包含安全任务投影及相对 `status_url`。`attempt=1` 表示第一次投递。
终态重放返回一个新任务，其中 `attempt>1`、`supersedes_job_id=<previous-job-id>`、`duplicate=false`、`rerun=true`。
活动重复提交返回当前任务，其中 `duplicate=true`、`rerun=false`。内部稳定身份哈希绝不暴露。
`/api/config` 报告 `terminal_replay_policy=new_attempt`。

无需 Authorization 头即可轮询 `GET /api/intake/jobs/<24-hex-job-id>`；`GET /api/intake/jobs?completed=0` 返回除 `completed` 外的任务供一级工作台刷新，`GET /api/intake/jobs?completed=1` 只返回完成任务，旧的 `GET /api/intake/jobs?active=1` 仍返回当前活动任务。状态顺序为：

`accepted → downloading → downloaded → running → callback → completed | failed`

`downloaded` 表示 ZIP 已验证并持久落盘、预签名 URL 已从运行队列丢弃，正在等待单个核销 worker。

排队、运行和失败任务删除使用 `DELETE /api/intake/jobs/<24-hex-job-id>`，请求必须来自当前工作台同源、携带 `/api/config` 的删除确认令牌，并且 JSON 只能是 `{"confirm_job_id":"<同一个 job-id>"}`。服务必须确认 runner 已停止并证明所有资源唯一归属于该 attempt 后才返回成功；否则返回冲突且保留记录，不得只做前端隐藏。

默认模式下的已完成任务包含正式 runner 回执和已投递回调回执。
明确无回调任务记录 `callback.status=not_required`，并通过该状态 API 消费。
仅接收任务同样记录 `callback.status=not_required`，下载验证后立即完成，保持 `result=null`，
并通过 `delivery.input_file` 暴露持久文件。
失败任务包含有界且已去除 URL 的消息；如果正式 runner 在回调失败前已经完成，还包含保留的已完成结果。
任务状态属于运行元数据，绝不能表示为业务证据。下载验证成功后，`delivery` 还包含 `input-oss` 下的本地持久
`input_directory` 和 `input_file`。

若服务在完成前重启，活动回执标记为失败，因为签名 URL 被有意设计为不持久化。
重启后绝不能静默重放可能产生费用的 AI 运行。检查终态回执；确需重新正式分析时，
上游使用足够长有效期的新 URL，再次发送相同 `verifyCode`、`analyzeId` 和对象路径。
这会创建下一个 attempt 及任务目录。此前验证通过的 ZIP 可以保留在原任务目录供诊断。

核销类型是开始业务核验的前提。ZIP 名称未注明类型或包含多个具体类型时，该包核销失败，不调用 AI；运行 manifest、snapshot、正式收据及 OSS job 均为 `failed`，失败代码为 `classification_failed`，不发布 `run.completed`。CLI 以退出码 2 返回已保存的失败收据，OSS 仍按原三字段契约回传 `核销失败：核销方式无法确认` 及全部具体原因；回传成功只表示送达，不能把核销改成完成。回传失败单独记为 `callback_failed`，原核销失败与原因继续保留。混合批次中已知类型照常检查并保留结果，只要仍有无法分类的包，整次记录为失败。类型已确定且业务核验执行完成后，材料缺失、金额差异等才作为完成结果中的失败检查项展示，与正确检查项并列。旧档案若曾把分类退回标成 completed，工作台只读投影为失败，不重写历史证据或自动重发回调。
