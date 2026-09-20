# 线下活动费用核销

系统版本 **2.12.3**，工作台API **1.44**。以用户指定PDF为唯一审核标准，按提交资料内容识别八种核销方式。

## 项目目录

代码、规则和模板按 Skill 归属存放；启动命令、Python 包名 `audit_core` 和工作台网址保持一致。

```text
offline-activity-audit/
├─ skills/
│  ├─ orchestrate-offline-audit/
│  │  ├─ SKILL.md
│  │  ├─ scripts/             # 正式入口、维护命令和 audit_core 核销引擎
│  │  ├─ references/          # contracts、pdf-policy、部署及故障记录
│  │  └─ assets/              # 唯一客户 HTML；legacy/ 保留旧视图编译资源
│  ├─ audit-*/               # 各核销类型的 Skill、证据结构和规则
│  ├─ create-offline-audit-scenario/  # 场景维护和规则同步脚本
│  ├─ new-product-onboarding-rag-workflow/ # 独立商品入库维护
│  └─ project-workflow-showcase/     # 工作流展示工具及 assets/ 展示页
├─ shared/product-database/  # 独立商品数据库及维护工具
├─ tests/                   # 跨 Skill 回归；fixtures/ 仅供验收测试
├─ input/                   # 本地原始 ZIP
├─ input-oss/               # 按任务隔离的 OSS 原始 ZIP
├─ worktrees/               # 正式运行档案
└─ artifacts/               # 本地诊断与备份
```

运行及维护从 [编排 Skill](skills/orchestrate-offline-audit/SKILL.md) 进入；
迁移部署参考 [部署说明](skills/orchestrate-offline-audit/references/deployment.md)。
仅移动源码和资源，不迁移或改写输入、历史运行、数据库、凭据及 Git 中保留的历史图库。
验收工作簿保留在 `tests/fixtures/`，不进入模型可读的 Skill 目录。

## 当前规则（2026-09-18）

唯一业务标准为用户指定的《费用核销类型-资料与标准清单-20260918.pdf》第1页。程序规则表位于 `skills/orchestrate-offline-audit/scripts/audit_core/pdf_policy.py`；八份Skill的清单与审核要点由 `skills/create-offline-audit-scenario/scripts/sync_pdf_policy.py` 同步。新规则取代旧十类业务规则及2026-09-11的ZIP名称路由。

| 核销类型 | 类型标识 | Skill |
|---|---|---|
| 人员激励 | `personnel_incentive` | `audit-personnel-incentive` |
| 陈列堆头 | `promotional_display` | `audit-promotional-display` |
| KT板等物料制作 | `poster_material` | `audit-poster-material` |
| 搭赠 | `giveaway_promotion` | `audit-giveaway-promotion` |
| 补差 | `price_difference_support` | `audit-price-difference-support` |
| 进场费 | `entry_fee` | `audit-entry-fee` |
| 外采赠品 | `self_procured_gift_material` | `audit-self-procured-gift-material` |
| POS达标激励 | `pos_target_incentive` | `audit-pos-target-incentive` |

新版PDF单列“POS达标激励”：必交盖章POS及Excel、结算单和签章促销合同；“搭赠”要求赠送规则及活动照片或小票，不要求Excel。虽搭赠副标题仍含POS达标激励文字，两类依实际资料清单独立匹配，不按标题合并。客户自采赠品物料纳入“外采赠品”。其他费用、维护费用不再是可选核销类型，不能作为兜底。历史处理器及资源仅保留供旧结果兼容/回归校验，不能接回正式新流程。

1. 安全盘点每个ZIP内全部资料，读取PDF完整页、图片及电子表原始单元格；ZIP名和文件名不参与选型。
2. 按用户2026-09-18澄清，先根据正文、图片和原始表格识别资料角色，再逐项对照PDF“核销资料”列的八套清单确定类型。ZIP名、文件名、资料标题和费用项目名称均不能直接决定类型。每次识别必须保存八类完整的 `material_matches`（逐项存在情况、条件、来源和资料组合匹配依据）及各类 `extra_materials`（清单外资料项及来源），不能先按费用名称定候选，再只核对已选类型；空候选或多候选也不得跳过清单比对。不得因为某类资料要求少而将缺件改投该类。
3. 按用户2026-09-18确认，必须按PDF资料项严格相等：必交项齐全、条件项按PDF满足或豁免、没有清单外资料，且唯一匹配一个类型，才进入对应Skill。缺一项、多一项、资料角色或适用条件无法确认，均返回核销失败；不能先猜类型再带缺项进入审核。只有结算单和POS的包不匹配任何完整清单，必须失败。
4. 对匹配包只审核PDF列明的本类型要点、资料标准及适用通用标准，每项一次、不多不少。输出Schema限定本类型rule_id和准确项数，程序另校验遗漏、重复、额外或跨类型规则；不能在已有rule_id内夹带其他检查或擅自加严原文。数量金额引用原文由程序复算，字段/视觉事实保留来源。缺少判断依据记无法核验，不伪造通过或归咎客户造假。
5. 计数单位是PDF资料项，同一份合同多页、同类照片多张合并计项，一份来源可凭实际内容证明多个资料项；同类型多个包分别审核。每类比对必须覆盖全部已读来源，归入对应清单或清单外资料。独立清单外资料即使在同一页也算多项，不能笼统并入附件或佐证。文件名、后缀、费用名称和物理文件数量不能代替资料项比对。

保留PDF条件：培训可免POS和Excel；临促每天有人与产品的水印照；商场中庭大型活动现场另有入场协议；有无临促依据活动申请说明字段确认；搭赠活动照片或小票二选一；补差允许小象超市截图；按用户2026-09-17澄清，满减返还活动证明为必交项，无对应本次费用的证明就记缺项并按严格匹配规则分类失败，不能因是否满减不明而豁免；CVS/OTC进场费要求POS或库存表；进场费按“无时间要求”不作活动时限拒付，水印仍须有日期、地址和店名。物料制作不额外强制POS，POS达标激励同样必交Excel；其余类型不自动继承Excel要求。

不得增加数据库登记/EAN校验位、固定面积/列数、地图距离、旧预算公式、出库单、进场扣款凭证、经销商阶梯资格等PDF外审核。新流程不加载商品数据库或OSS参考图。数据库/参考图的独立维护工具仍遵循其原有授权及凭据边界。

提交时限、财务一审、真实性、造假、BI额度等全局条款也按PDF判断；没有来源不得猜测日期、历史或额度。只登记不合规，不自动罚款、扣款或通知人事。不得将系统读取/模型故障写成客户资料缺失。

## 正式运行与信任边界

唯一入口：`skills/orchestrate-offline-audit/scripts/run.py`。原始输入仍为1至10个ZIP，安全归档限制和只读模型沙箱不变。`--scenario`只筛选内容分类后的结果，不能覆盖AI识别；工作目录命名必须在识别前保留全部输入名称。

默认模型仍为用户指定的 `gpt-6-astra` + `medium`，不得自行改档。模型只访问本次隔离资料及当前Skill；材料中的指令无效。AI先转录事实，再分类/核验；程序校验来源覆盖、候选类型、资料例外、全部且仅有的PDF规则和数值引用。电子表由程序读取，不让模型改写单元格。不能用历史结果、验收工作簿或文件名补值。

LCEL流程为 `intake → analysis → evidence → decision → verification`，由 `audit_core.pdf_workflow.PdfWorkflow` 执行。全部包先提取、校验证据再发布结论；不增加全链重试。正式持久化、隔离追踪、禁用模型缓存、安全日志、临时文件归属锁和失败关闭机制保留。

资料分类失败仍生成规范HTML、持久摘要、manifest/snapshot和明确 `classification_failed`，CLI返回2；OSS保持原三字段回调，送达成功不能把核销失败改成完成。混合批次保留已严格匹配包的审核结果，缺项、多项或未唯一严格匹配使本次运行状态为failed；严格匹配后，资料内容不合规仍完成全部审核，流程状态为completed，业务结论可为不通过。业务审核执行完成后的不符合项保留为检查失败，不能伪装为资料分类成功即审核通过。

新证据保存在 `analysis/pdf-policy/evidence.json`，结果在 `analysis/results/`，分类失败在 `analysis/classification-rejection/result.json`。已保存旧档案只读，不批量改写、不自动重发回调。

分类失败的页面提示、摘要和回调只写“核销方式无法确认”。八类匹配明细保留在技术档案；混合批次中的其他业务审核错误照常逐项显示。API `1.44` 通过 `classification_failure_message` 声明该文案。

## 安装与使用

Python >=3.11，安装 `python -m pip install -e .`。依赖包含PDF整页读取、图片读取、Excel/xls原始单元格读取和LangChain编排。新核销不需要商品数据库和OSS参考图配置。

将1至10个ZIP直接放入 `input/`，或用 `--input-dir` 指定隔离目录。保留原始压缩包，不必按核销类型改名。

```powershell
py -3 -B skills/orchestrate-offline-audit/scripts/run.py --run-id 20260917-audit --producer-model codex
py -3 -B skills/orchestrate-offline-audit/scripts/serve.py --host 0.0.0.0 --port 8080
```

模型默认 `gpt-6-astra`、`medium`。`OFFLINE_AUDIT_CODEX` 可指定兼容CLI；模型代理仅由 `OFFLINE_AUDIT_MODEL_PROXY` 显式配置，不改全局代理或OSS连接。单次模型总时限默认3600秒，连续已证实连接故障默认120秒；分别通过 `OFFLINE_AUDIT_MODEL_TIMEOUT_SECONDS`、`OFFLINE_AUDIT_MODEL_TRANSPORT_TIMEOUT_SECONDS` 配置。

`--scenario`只接受上表八个标识，且只在AI内容分类后筛选。改ZIP名称不能改变结果。CLI输出持久收据；资料不匹配退出2，执行错误退出1。

每次运行生成独立 `worktrees/<资料包名>-<时间>/`，其中包含唯一自包含HTML、错误摘要、不可变结果/证据、manifest/snapshot、事件、日志和检查点。工作台保留已有总览/技术档案与错误/正确检查两个结果视图；不重写历史报告。

## OSS 自动投递、状态查询与可选回调

一次 OSS 请求对应一份 ZIP 和一次正式核销运行。上游只提交 `verifyCode`、`analyzeId`、
`downloadUrl` 三个业务字段。单个下载 worker 按接收顺序尽快下载并校验 ZIP，持久保存到
`input-oss/<job_id>/<原始文件名>.zip`；下载与前一任务的正式核销、回调并行，不需要等待 AI 完成。
单个核销 worker 再严格串行地把每个已下载任务目录作为 `--input-dir` 调用唯一正式 `run.py`，
因此不会并发运行多个视觉模型任务。默认模式在分析完成并原子保存 worktree 后，额外生成
`worktrees/<workspace-id>/ai-analysis-summary.md`；该文件是给业务部门阅读的排版化 AI 小结：仅按场景分节并逐条列具体错误原因，保留定位问题所需的业务文件 basename、缺失字段和必要差值；仅合并完全相同的原因，不合并不同对象的同类错误，不截断。小结不包含场景/错误总数、运行元数据、置信度或补交建议。随后向上游的 `POST /api/v1/ai/analyze/callback` 回传同一个
`verifyCode`、`analyzeId`，并将这份 Markdown 小结作为中文 `result`。
材料诊断的小结保留每项缺失材料，以及每组重复文件/角色冲突的全部原始文件路径；相同错误类型合并不能丢失不同文件组。
显式启用 `--oss-no-callback`（或 `OFFLINE_AUDIT_OSS_NO_CALLBACK=1`）时，服务接收并处理
相同三字段请求，但不发送结果回调；调用方通过任务状态接口查询完成状态。
显式启用 `--oss-receive-only`（或 `OFFLINE_AUDIT_OSS_RECEIVE_ONLY=1`）时，服务只下载、
校验并持久保存 ZIP，不启动正式核销，也不发送结果回调。

同一 `verifyCode:analyzeId` 在当前尝试仍为 `accepted`、`downloading`、`downloaded`、`running` 或 `callback`
时只返回该任务，不会并发重复分析；当最近一次尝试已经 `completed` 或 `failed` 后，同一对象
路径再次投递会创建新的尝试号、任务号、独立 `input-oss` 目录和 worktree，重新下载并运行 AI。
新回调仍携带原 `verifyCode` 与 `analyzeId`，因此上游可用最新 `result` 覆盖业务结果；本项目不覆盖
或删除旧输入、旧任务收据和旧 worktree，仍保留完整审计历史。

启动前配置 OSS 下载域名白名单和完整回调地址。入站接口不要求鉴权，只应暴露在受信任的
内部局域网，不得直接开放到公网：

```powershell
$env:OFFLINE_AUDIT_OSS_CALLBACK_URL = "https://业务系统.example/api/v1/ai/analyze/callback"
# 如果业务回调接口要求 Bearer Token，再设置这一项：
$env:OFFLINE_AUDIT_OSS_CALLBACK_TOKEN = "请替换为回调密钥"

py -3 -B skills/orchestrate-offline-audit/scripts/serve.py `
  --host 0.0.0.0 `
  --port 8080 `
  --enable-oss-intake `
  --oss-allowed-host audit-materials.oss-cn-hangzhou.aliyuncs.com
```

`OFFLINE_AUDIT_OSS_CALLBACK_URL` 默认必须是完整 HTTPS 地址，且路径固定为
`/api/v1/ai/analyze/callback`；只有相对路径还不足以发送回调。也可以用
`--oss-callback-url` 传入。受信任局域网内的业务接口若只有 HTTP，必须同时显式设置
`OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK=1` 或追加 `--oss-allow-http-callback`，且不得把该
例外用于公网。`OFFLINE_AUDIT_OSS_ALLOWED_HOSTS` 可用逗号分隔多个精确下载
域名；下载上限、下载超时、回调超时和回调次数分别由
`OFFLINE_AUDIT_OSS_MAX_BYTES`、`OFFLINE_AUDIT_OSS_DOWNLOAD_TIMEOUT`、
`OFFLINE_AUDIT_OSS_CALLBACK_TIMEOUT`、`OFFLINE_AUDIT_OSS_CALLBACK_ATTEMPTS` 调整。
OSS 下载使用独立直连连接，不读取 `HTTP_PROXY`、`HTTPS_PROXY` 或 Windows 系统代理；
白名单、HTTPS/TLS 校验及每次重定向校验仍然生效。结果回调也使用自己的独立直连连接；模型和系统代理配置不变。API `1.42` 的 `oss_intake.callback_transport` 为 `direct`。
下载连接错误区分拒绝连接、超时、连接重置、域名解析和 TLS 失败，诊断消息不包含签名 URL 或底层异常原文。
下载失败的 attempt 不自动重试；上游使用新的有效签名链接重新提交相同业务二元组及对象路径，创建下一次 attempt。
仅在调用方明确不接收结果回调时，启动参数追加 `--oss-no-callback`，此时可不配置
`OFFLINE_AUDIT_OSS_CALLBACK_URL`；不要静默省略回调并冒充默认正式模式。
若当前阶段只收集原始 ZIP，改用 `--oss-receive-only`；任务状态在下载校验完成后直接变为
`completed`，`delivery.input_file` 给出 `input-oss/<job_id>/` 下的持久文件位置。

上游向本项目提交：

```http
POST /api/intake/oss HTTP/1.1
Content-Type: application/json; charset=utf-8

{
  "verifyCode": "HX202603250014",
  "analyzeId": 123,
  "downloadUrl": "https://白名单OSS域名/人员激励.zip?临时签名"
}
```

默认模式正式分析完成后，本项目向配置的业务系统回调：

```http
POST /api/v1/ai/analyze/callback HTTP/1.1
Content-Type: application/json; charset=utf-8
Idempotency-Key: HX202603250014:123:<result-sha256-16>

{
  "verifyCode": "HX202603250014",
  "analyzeId": 123,
  "result": "# AI 小结\n\n## 人员激励\n\n- POS电子表缺少销售数量合计。\n- 促销合同.jpg 的签章区域模糊，无法辨认。"
}
```

- `verifyCode:analyzeId` 是稳定业务身份。同一对象路径在最新尝试运行中重复投递只返回原任务；最新尝试终态后重复投递会生成 `attempt=2/3/...` 的新任务并重新分析。响应中的 `rerun=true` 表示本次已创建覆盖业务结果的新尝试，`supersedes_job_id` 指向上一尝试；同一组合改投另一个对象路径仍返回 `409`。
- `downloadUrl` 必须是白名单域名的 HTTPS 443 地址。临时签名查询串只保存在内存，不进入任务收据、日志、worktree 或结果回调。
- 下载响应或 URL 路径应保留 Windows 安全的原始 `.zip` 文件名及业务场景标记。服务记录响应 ETag、实际字节数和自行计算的 SHA-256，并验证文件确实为 ZIP；残缺或无效下载会删除。
- `verifyCode` 中首个有效 `YYYYMMDD` 用作运行业务日期；没有有效日期时按上海时区收件当天生成。`analyzeId` 必须是 64 位正整数。
- API `1.25` 将接收、回调及任务状态中的 `fileId` 更名为 `analyzeId`；旧参数请求返回 `400` 更名提示。历史回执只读映射为 `analyzeId`，原有身份指纹和重投链保持兼容，不重写旧回执或重发历史回调。
- `result` 直接读取正式 worktree 根目录的 `ai-analysis-summary.md`，不由 HTTP handler 另做业务分析；内容按场景标题和错误项目符号排版，完整保留各对象的具体错误原因，文件仅用 basename，不重复错误标题或附加说明。它不包含 `downloadUrl`、本地绝对路径、密钥、总数概况、完整证据长文、运行元数据、置信度或补交建议，也不得以“另有 N 项”省略错误类型。
- 回调以 `2xx` 为成功；`Idempotency-Key` 由 `verifyCode:analyzeId` 和三字段回调正文摘要组成，使不同分析结果可更新上游、同一结果的传输重试仍可去重。网络错误、`408`、`429` 和 `5xx` 按配置有限重试；重试只重新投递已保存结果，绝不重新运行 AI。
- 默认模式状态依次为 `accepted → downloading → downloaded → running → callback → completed | failed`。`downloaded` 表示临时签名已不再需要、ZIP 已安全落盘并等待单个核销 worker；下载 worker 可继续接收后续 ZIP。显式无回调模式跳过 `callback`，任务完成后将回调状态记为 `not_required`；只接收模式为 `accepted → downloading → completed | failed`，不创建核销 worktree。分析成功但默认模式回调失败时，任务记录为 `callback_failed`，已完成的 worktree 和原始 ZIP 仍然保留。
- 状态查询为无需鉴权的 `GET /api/intake/jobs/<job_id>`；API `1.42` 的一级工作台轮询 `GET /api/intake/jobs?completed=0`，只展示尚未完成及失败的 OSS attempt：`accepted/downloading/downloaded` 统一显示为“排队中”，`running/callback` 显示为“运行中”，`failed` 显示为“失败”并保留失败原因。为不中断升级时正在处理的真实任务，页面在旧 API `1.19` 服务尚未安全重启时会回退读取完整列表并在本地排除 `completed`。完成的 attempt 不再占用投递区，其正式运行在系统总览台账和技术档案中保留；两个列表覆盖所有运行状态。`GET /api/intake/jobs?active=1` 仍可用于只读活动任务检查。任务收据位于隐藏的 `worktrees/.intake/jobs/`。
- 投递区的排队、运行和失败卡片始终提供“删除记录”。同源确认的 `DELETE /api/intake/jobs/<job_id>` 会让排队任务从 worker 队列跳过；运行任务先取消专属正式 runner、阻止尚未发出的回调，再删除该 attempt 的收据、`input-oss/<job_id>/` 和唯一未完成 worktree；失败任务则删除其收据、ZIP 和唯一关联运行档案（如有）。已经发出的业务回调不能撤回。完成记录及非 OSS 失败记录仍通过原运行删除接口处理。

本地 `input` 运行只生成报告，因为它没有 OSS 请求中的上游业务身份。需要向客户交付一份已完成报告时，使用补发工具，显式指定正确的 workspace 和业务二元组：

```powershell
py -3 -B skills/orchestrate-offline-audit/scripts/deliver_callback.py --workspace-id <workspace-id> --verify-code <verifyCode> --analyze-id <analyzeId>
# 核对预览后，以相同参数增加 --apply 投递；使用既有 OFFLINE_AUDIT_OSS_CALLBACK_* 环境配置。
```

默认仅校验和预览，不发送。`--apply` 校验完成状态、身份、摘要与快照哈希后，只投递已保存的中文报告，不下载或重跑 AI，不改历史结果。
回执位于 `worktrees/.intake/callback-deliveries/`；同一目标、同一结果已经成功时再次执行会跳过发送。失败保留回执，可用相同命令仅重试投递。
该命令不会从压缩包或目录名称猜测上游 ID，也不会为其他本地运行自动回调。

已有完成态 worktree 可按当前小结合同安全回填；命令只重写 `ai-analysis-summary.md` 及其在 `manifest.json`、`snapshot.json` 中的大小和 SHA-256，不改业务证据或结论，也不会补发历史回调：

```powershell
py -3 -B skills/orchestrate-offline-audit/scripts/backfill_analysis_summaries.py --apply
```

用户要求已完成记录按最新规则统一整理时，可以同时刷新摘要、规范离线页面和客户数量投影。先预览，再使用新的备份目录执行：

```powershell
py -3 -B skills/orchestrate-offline-audit/scripts/backfill_analysis_summaries.py --refresh-archives
py -3 -B skills/orchestrate-offline-audit/scripts/backfill_analysis_summaries.py --refresh-archives --backup-dir artifacts/completed-refresh-backup --apply
```

刷新只处理已完成记录，写入前备份全部将变更文件；原始 `snapshot.view`、业务计数、核销结果、证据、日志及检查点保持不变。`manifest.customer_projection` 用规则版本与快照哈希绑定客户显示数量。重复执行无变化时不会重写档案，也不调用 AI 或补发回调。

代码暂时保留旧一期事件体的兼容解析，但正式 OSS 对接和测试合同均以上述三个字段为准。
Windows 上工作台端口采用独占绑定；若已有旧服务占用相同端口，新进程会明确启动失败，
避免请求被旧版本进程接收。

## 维护与验证

- `skills/orchestrate-offline-audit/scripts/audit_core/pdf_policy.py`：唯一八类清单与PDF审核要点。
- `pdf_materials.py`：整页/表格读取、内容分类和选定Skill调用。
- `pdf_evidence.py`：严格Schema、来源引用及数值复算。
- `pdf_workflow.py`：分类失败/对应Skill审核、结果与持久页面投影。
- `skills/create-offline-audit-scenario/references/legacy/`：旧十类资源，仅供历史兼容回归，不能驱动新运行。

```powershell
py -3 -B skills/create-offline-audit-scenario/scripts/sync_pdf_policy.py
py -3 -B skills/orchestrate-offline-audit/scripts/verify_project.py -v
py -3 -B -m compileall -q skills tests shared/product-database
```

执行流程更改还需真实ZIP的bundled runner验证、1440×960与1280px桌面工作台检查。安装Playwright及Chromium后，可用 `python -B tests/pdf_workbench_smoke.py` 在隔离夹具中验证八类结果、失败文案、服务与静态档案一致性。结果必须严格只含PDF审核要点；不得用旧测试的额外审核反向扩大范围。验收工作簿只用于测试。
