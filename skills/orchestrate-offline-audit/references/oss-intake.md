# OSS 接收传输契约

本参考只用于未经身份验证的内网 OSS 到核销适配器。它不是另一条业务管线，HTTP 服务内部也绝不执行场景分析。

## 生命周期

1. 可信上游调用 `POST /api/intake/oss`，传入必填的 `verifyCode`、正整数 `analyzeId` 及临时 `downloadUrl`，以及必填非空字符串 `bizType`（业务类型）。
2. 服务校验准确的 HTTPS 下载主机，派生稳定的 `verifyCode:analyzeId` 业务身份；若最新 attempt 仍活动则复用，
   否则在 `worktrees/.intake/jobs/` 下创建持久 attempt 回执。最新 attempt 进入终态后再次提交，会创建新 attempt。
3. 单个下载 worker 按接收顺序尽快将对象下载到 `<project>/input/<job_id>/<object-basename>.zip`，
   记录响应 ETag 和实际大小、计算 SHA-256，并确认它是 ZIP。后续对象的下载不等待前一任务的正式核销或回调结束。
   完整预签名 URL 只保留到本地 ZIP 验证完成，绝不能写入回执、日志或已下载任务队列。
   不同 attempt 绝不共享目录，也不覆盖同名归档。
4. 核销 worker 在独立子进程中启动 `skills/orchestrate-offline-audit/scripts/run.py`，
   默认单并发；`OFFLINE_AUDIT_OSS_AUDIT_WORKERS=2` 时最多同时处理两单，超出部分继续排队。
   以持久 job 目录作为 `--input-dir`。
   深层归档安全、路由、证据提取、决策、验证和发布仍由正式 runner 负责。
5. 默认模式下，正式 worktree 快照持久化后，runner 在 worktree 写入 `ai-analysis-summary.md`。
   资料问题列出具体缺少、多余或不能确认的资料及相关文件；混合批次同时保留其他包的业务问题，AI正常完成即记completed。
   该文件执行[统一错误原因与处理方式规范](error-reasons.md)：按资料内容确定的核销类型分节，每个不同错误只列具体原因，保留必要业务文件 basename、缺失字段、实际与应有数值及差额。
   重复原因合并，但不得截断不同问题或文件组，也不得替换为“另有 N 项”等占位。不重复错误标题，不含总数概况、逐文件识别过程、完整证据链、通过事实、结论、影响、运行元数据、置信度或处理建议；处理方式仅在客户界面的独立字段展示。
   适配器严格只把 `verifyCode`、`analyzeId` 和该确定性中文 Markdown 作为 `result`，POST 到已配置的完整
   `/api/v1/ai/analyze/callback` URL。默认使用 HTTPS；可信内网 HTTP 必须明确选择启用。
   回调重试只重新投递已保存结果，绝不能重跑正式核销。
   明确配置的无回调部署跳过投递，将回调状态记录为 `not_required`，并以未经身份验证的任务状态轮询作为完成通道。
   明确配置的仅接收部署在验证持久化后停止，不启动正式 runner，也不创建正式 worktree。
   后续完成的 attempt 使用同一业务二元组发送最新结果，使上游记录可以被替换；更早 attempt 的本地回执、ZIP 和 worktree 保持不变。
6. 已验证原始 ZIP 在成功或失败后仍保留在 `input`，供本地追溯。
   删除不完整或传输无效的下载；解压目录、模型工作区及传输结果文件保持临时。
   成功任务指向普通本地 `worktrees/<YYYYMMDD_HHMM_SS>-<audit-model>_<reasoning-effort>/` 档案；完整时间戳取核销 worker 实际认领并预留该运行时的上海时间，绝不取 `verifyCode` 中的业务日期。并发同秒任务仍按现有原子预留规则获得不同目录。
7. 一级工作台通过 `GET /api/intake/jobs?completed=0` 展示未完成及失败 attempt：`accepted/downloading/downloaded` 标为“排队中”，`waiting_offpeak` 标为“等待空闲时段”（显示下一次开始时间与“立即分析”按钮），`running/callback` 标为“运行中”，`failed` 标为“失败”并显示安全失败原因。完成 attempt 不占用投递区，系统总览台账和技术档案均保留全部状态的 worktree。每张投递卡都提供同源确认的删除操作。
   删除排队任务会让 worker 跳过后续阶段；删除运行任务会先取消正式 runner、阻止尚未发出的回调，再删除该 job 的收据、专属 ZIP 和唯一关联未完成 worktree；删除失败任务会清除其收据、ZIP 和唯一关联运行 worktree（如有）。已经发送的回调不能撤回。

必须使用子进程边界，因为 CLI runner 会把进程范围的 stdout/stderr 重定向到运行日志；
若在 HTTP handler 线程中执行，并发访问日志会混入核销日志。
OSS 使用“单下载 worker → 有界核销 worker”两级流水线：下载可与前一任务的 AI/回调重叠。
`OFFLINE_AUDIT_OSS_AUDIT_WORKERS` 仅允许整数 1 或 2，未配置为 1；参半经用户授权启用 2，其他部署不随源码发布自动切换。
任务仍按队列领取，每单的输入、取消信号、模型快照、运行目录和回调结果独立；完成及回调先后可与接收顺序不同。
关闭队列后拒绝新任务，先处理已接收下载，再向全部核销 worker 发送退出标记，避免遗留已下载任务。
手动 CLI 运行仍遵循同一 worktree 预留及禁止覆盖行为。该并发上限不是进程内存硬限制，不包含测试脚本的内存停止线。

## 启用方式

默认仍为只读工作台。只有可信内网中才通过 `--enable-oss-intake` 或 `OFFLINE_AUDIT_OSS_ENABLED=1` 明确启用接收，并提供：

- 通过重复 `--oss-allowed-host` 或逗号分隔的 `OFFLINE_AUDIT_OSS_ALLOWED_HOSTS` 提供一个或多个准确下载主机；
- 默认通过 `--oss-callback-url` 或 `OFFLINE_AUDIT_OSS_CALLBACK_URL` 提供完整 HTTPS 回调 URL；其路径必须是 `/api/v1/ai/analyze/callback`；
- 只有可信私网回调没有 TLS 端点时，才使用 `--oss-allow-http-callback` 或 `OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK=1`；公网回调绝不能启用该例外；
- 只有调用方明确不接收回调时，才使用 `--oss-no-callback` 或 `OFFLINE_AUDIT_OSS_NO_CALLBACK=1`；不得默默用此模式替代默认投递；
- 只有当前阶段仅收集文件、不做分析时，才使用 `--oss-receive-only` 或 `OFFLINE_AUDIT_OSS_RECEIVE_ONLY=1`；该模式意味着不回调，也不调用正式 runner；
- 可选回调 Bearer 凭据：`OFFLINE_AUDIT_OSS_CALLBACK_TOKEN`；
- 可选核销并发：`OFFLINE_AUDIT_OSS_AUDIT_WORKERS=1` 或 `2`，默认 `1`；修改后须在活动任务完成后重启服务。`GET /api/config` 的 `audit_worker_count` 和 `single_audit_worker` 返回实际配置，双并发的 `pipeline` 为 `download_then_bounded_parallel_audit`；
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
  "downloadUrl": "https://exact-allowed-host/path/资料.zip?provider-signature=...",
  "bizType": "陈列堆头",
  "largeVenueFee": true,
  "cvsOtcChannel": false
}
```

- `verifyCode`、`analyzeId`、`downloadUrl`、`bizType` 必需。`verifyCode` 必须是安全、非空的 ASCII 标识符，`analyzeId` 必须是 64 位正整数。
- API `1.62` 新增可选 `cvsOtcChannel`，表示是否属于 CVS 或 OTC 渠道；只有 JSON 布尔值 `true` 为是，其他值（含未传、`false`、`null`、字符串、数字或容器）一律归一化为否。条码费直接采用归一化结果，不让AI从资料猜渠道；其他类型不增加进场费要求。`true` 按照片比例加全量终端库存明细盖章版分析；`false` 按全量门店上架照片分析，不要求另交库存。当前照片比例：少于20家全量，20至50家含50家至少80%，51至100家至少50%，大于100家至少50张；按门店比例向上取整且同店多图计一家，50家至少40家。完整说明见[进场费业务指南](../../audit-entry-fee/references/business-guide.md)。
- 归一化渠道值通过队列、`--cvs-otc-channel=true/false`、任务回执和正式manifest完整保存，审核Schema固定该条件。活动attempt从否改是或从是改否返回409，终态重交可创建新attempt；所有等价“否”的值合并为同一活动任务。不改既有二元组及对象指纹、三字段回调或历史档案；旧回执只读投影为否，不自动重审或补发。
- 用户2026-09-23说明 `largeVenueFee` 是否必传不是关键问题；保留下面的省略兼容处理，不再作为待确认或阻塞事项。
- API `1.53` 新增 `largeVenueFee`（是否包含大型活动场地费），传入时只接受JSON布尔值 `true`、`false`；字符串、数字和 `null` 返回400。陈列堆头为 `true` 时须交商场入场协议，为 `false` 时不要求。字段只确定条件，AI仍检查协议是否实际提供，不能重判活动规模或附加中庭位置条件；其他类型不新增资料项。为兼容旧调用允许省略，省略时不默认false，仍按已有资料确定适用条件，不能确认时说明暂时无法确定是否需要协议。
- 字段原值保存在任务回执和正式运行manifest，以 `--large-venue-fee=true/false` 传给正式runner，再约束陈列堆头的 `atrium`。不加入三字段回调，不改历史指纹或档案；旧回执只读补 `largeVenueFee=null` 表示未传。活动attempt修改此字段返回409；终态后可用新值创建下一attempt。
- 用户2026-09-21更正字段为业务类型 `bizType`；原误写的 `planType` 不作为新请求的别名或兜底，仅传旧字段按缺少bizType失败回调。混传时只使用bizType。
- API `1.59` 要求非空字符串 `bizType`（业务类型）。缺失、null、空字符串或纯空白时，仍受理业务身份以保存失败回执和回调，跳过ZIP下载及AI；终态为failed，failure.code=biz_type_unrecognized，回调result准确为“无法识别核销类型”。其他JSON类型返回400。字段原值精确命中八个规范值或明确兼容名称“堆头”“陈列”“物料制作”“pos达标激励”时，下载资料并调用正式runner；“堆头”“陈列”与“陈列堆头”均固定进入 `promotional_display` / `audit-promotional-display`，“物料制作”与“KT板等物料制作”均固定进入 `poster_material` / `audit-poster-material`；“pos达标激励”与“POS达标激励”均固定进入 `pos_target_incentive` / `audit-pos-target-incentive`。回执和档案保留原始 `bizType`，报告使用对应规范类型。用户2026-11-03确认的“物料制作”和“pos达标激励”仅为名称兼容，八类场景、资料要求及审核标准不变，不改写历史、不自动补跑或重发回调。其他非空值不下载、不创建正式worktree、不调用AI，正常完成，result.audit_required=false，回调result准确为“该业务类型无需AI核销”。资料缺少、多余只输出本类具体问题，状态completed。
- API `1.25` 起接收、回调和任务状态统一使用 `analyzeId`；旧 `fileId` 请求（含混传）返回 `400`。旧回执只读映射到新字段，内部指纹保持兼容，历史文件与回调不自动迁移或重发。
- URL 路径或响应 `Content-Disposition` 应保留 Windows 安全的 `.zip` basename，文件名不参与类型选择。
- `verifyCode` 中第一个有效 `YYYYMMDD` 作为运行业务日期；不存在时使用当前上海日期。
- 默认只接受 HTTPS 443 端口、准确配置主机名、安全重定向及公网 DNS 结果。下载前拒绝 userinfo、URL fragment、任意主机及私有/链路本地/loopback 解析。
- 下载使用专属直连 opener，显式禁用环境变量及 Windows 系统代理；不更改模型调用或全局代理；回调也使用自己独立的直连 opener。仍验证 TLS 证书和每次重定向目标。
- 连接失败按固定安全消息区分拒绝连接、超时、连接重置、域名解析、TLS 证书及握手失败；不保存底层异常原文或签名 URL。下载失败不自动重试，恢复连接后以新有效签名 URL 创建下一 attempt。

`verifyCode:analyzeId` 二元组及去除查询凭据的 URL 身份构成稳定提交指纹。
同一二元组与对象路径在签名刷新后再次提交，有两种结果：

- 最新 attempt 为 `accepted`、`downloading`、`downloaded`、`running` 或 `callback` 时，返回该 attempt，不排队重复 AI 工作；此时bizType更改返回409，不能悄悄替换正在审核的类型；
- 最新 attempt 为 `completed` 或 `failed` 后，创建 `attempt=2/3/...`，使用新的 24 位十六进制 job ID；命中八个规范值或“堆头”“陈列”“物料制作”“pos达标激励”兼容名称时创建独立 `input/<job_id>/` 目录及正式运行。

空值失败与其他非空类型的正常跳过都没有已下载ZIP或正式worktree；跳过结果保存在任务回执，可通过状态接口查询。状态查询保留具体失败与回调送达状态。回调失败保留输入失败原因，投递失败另记callback_failed。明确无回调或仅接收模式仍不下载空值任务，按配置跳过回调，状态保持failed。历史回执只读将planType映射为bizType，两者都没有时补null，不重新判定或重发。

同一二元组用于不同对象路径时仍返回 `409 Conflict`。业务记录有意移动到另一 OSS 路径时，应使用新的 `analyzeId`。

## 结果回调

默认模式在正式完成后，向已配置回调 URL 发送 UTF-8 JSON：

```json
{
  "verifyCode": "HX202603250014",
  "analyzeId": 123,
  "result": "# AI 小结\n\n## 人员激励核销\n\n- 未提交 POS 电子表。\n- 促销合同.jpg 的盖章区域模糊，无法辨认印章。"
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
`unclassified_archive_policy=complete_with_specific_material_issues` 表示资料未严格匹配时正常完成检查并输出具体资料问题；不进入对应业务审核。报告先持久化，再按三字段回调；模型或读取故障不能伪装成客户缺件。混合批次保留其他包的检查结果。

## 响应与状态

新接收任务返回 `202 Accepted`，包含安全任务投影及相对 `status_url`。`attempt=1` 表示第一次投递。
终态重放返回一个新任务，其中 `attempt>1`、`supersedes_job_id=<previous-job-id>`、`duplicate=false`、`rerun=true`。
活动重复提交返回当前任务，其中 `duplicate=true`、`rerun=false`。内部稳定身份哈希绝不暴露。
`/api/config` 报告terminal_replay_policy=new_attempt；request_fields列六字段，required_request_fields仍为verifyCode/analyzeId/downloadUrl/bizType，optional_request_fields为largeVenueFee和cvsOtcChannel。large_venue_fee_policy及cvs_otc_channel_policy分别声明两个字段的适用范围和归一化处理，两者省略语义不同。biz_type_policy=exact_value_skill_mapping_then_material_gate，biz_type_values列出八个规范值，biz_type_skill_map列出一一对应Skill；biz_type_aliases明确返回 `{"堆头":"陈列堆头","陈列":"陈列堆头","物料制作":"KT板等物料制作","pos达标激励":"POS达标激励"}`，只兼容这四个完整原值，不去空格或做模糊匹配；other_biz_type_policy=complete_without_ai。

无需 Authorization 头即可轮询 `GET /api/intake/jobs/<24-hex-job-id>`；`GET /api/intake/jobs?completed=0` 返回除 `completed` 外的任务供一级工作台刷新，`GET /api/intake/jobs?completed=1` 只返回完成任务，旧的 `GET /api/intake/jobs?active=1` 仍返回当前活动任务。状态顺序为：

`accepted → downloading → downloaded → waiting_offpeak → running → callback → completed | failed`

`downloaded` 表示 ZIP 已验证并持久落盘、预签名 URL 已从运行队列丢弃，正在等待空闲核销 worker。

`waiting_offpeak` 表示资料已就绪，但当前不在已配置的允许开始时段，尚未开始核销。默认采用 DeepSeek 官方错峰：
高峰为 UTC 01:00-04:00、06:00-10:00（周一至周五，中国法定节假日除外），即北京时间工作日
09:00-12:00、14:00-18:00；其余时段为空闲时段。手工上传与系统投递两条入口使用同一调度。
该状态记录 `offpeak_waiting_since`、`offpeak_next_start` 和 `offpeak_window`，不调用模型、不计费。
`GET /api/intake/jobs` 同时返回 `offpeak` 文档（`enabled`、`peak_now`、`offpeak_now`、`next_offpeak_start`、
`next_offpeak_label`、`seconds_to_next_offpeak`、`waiting`、`window`、`source`、`holiday_count`）供工作台显示。
剩余时间由服务端按同一时钟计算，页面不依赖浏览器时钟，避免电脑时间偏差导致倒计时错误。

左侧导航在“更换模型”下方提供“核销时间表”栏目，展示当前执行状态、下次开放时间、
每周开放时间条，以及所有 `waiting_offpeak` 单据及各自预计开始时间；栏目角标显示等待单数，
每张等待单也可直接点“立即分析”。时间表按当前已保存的执行计划展示，已开始的任务不受后续时段变化影响。

### 自定义核销时间

“核销时间表”栏目支持 `official`（官方错峰，默认）、`custom`（每周自定义）、`always`（全天核销）。
页面顶部“按空闲时段核销（默认）”和“全天即时核销”直接保存预设模式；按下后回读真实设置和当前状态。
两按钮可连续来回切换，点击立即更新时间条及选中态；保存请求串行提交最后一次选择，期间不禁用按钮或重建编辑表单，成功回读才提示已生效。队列用独立后台请求更新，不等待完整核销台账；过时 revision 的状态响应不能覆盖较新的设置。较早请求失败时若已有新操作，继续提交最新选择；同一失败不自动无限重试，409 冲突仍保留提示且不覆盖其他页面。
每周开放时段通过蓝色时间条编辑：左右端点调整起止，中间移动整段，接近整点 6 分钟内自动对准并显示对齐线，松开 5 分钟吸附；
方向键调整 5 分钟，Shift+方向键调整 1 分钟。窄时段完整显示起止时间。
辅助背景及 09:00、12:00、14:00、18:00 虚线按 DeepSeek 默认空闲/高峰表绘制。
拖动松开、逐日开关、添加/删除均自动合并保存；慢请求中允许继续调整，按新 revision 串行保存后续修改。
顶部统一“新增时段”取用区，拖到目标日期和时间位置放置，支持先点按钮再点日期行或键盘回车。
取消逐行加号及复制粘贴；点击时间条出现删除“×”，单击删除，点击其他位置收起。删除仅移除目标节点并立即提交后台保存，不重建其他日期或时间条；浮层优先于相邻时间条，点击范围至少32×32。时间表自适应宽度，不显示横向滚动条。后台轮询不重建表单，
拖动时减少非必要目录刷新，指针移动只更新当前时间条，松开后才提交配置。
页面移除手工保存按钮、独立精确调整、复制工作日、节假日设置和重复生效表格。
自定义按北京时间分别设置周一至周日，每天最多 8 段；`22:00` 至 `06:00` 表示从本日跨夜到次日，
全天使用 `00:00` 至 `24:00`。空日不开始新单，但前一日的跨夜时段可以延续。重叠、无效时间和整周全空均拒绝保存。
API 仍兼容 `YYYY-MM-DD` 节假日字段及既有环境变量/文件日期；界面暂不提供节假日配置，自动保存保留原值。
官方模式下既有登记节假日全天允许，自定义按已保存规则处理，不联网猜测法定日期。

- `GET /api/audit-schedule` 返回 `settings`、`status`、`read_only`、`confirmation_token`。
- `POST /api/audit-schedule` 要求当前工作台同源及请求头 `X-Offline-Audit-Schedule-Token`，JSON 字段严格为
  `revision`（GET 返回的整数）、`mode`、`weekly`（从周一到周日的 7 项数组，每项为 `[开始,结束]` 的数组）、
  `holiday_all_day`（布尔）和 `holidays`（补录日期数组）。成功返回 `settings`、`status`、`message`；
  无效配置返回 400，跨源或缺令牌返回 403，旧 revision 返回 409，不覆盖已保存设置。
- 配置原子保存在当前环境的 `worktrees/.settings/audit-schedule.json`；不含密钥，服务重启继续读取。
  正式环境的配置独立保留，不随源码覆盖。未启用接收服务的工作台只读，不能保存调度配置。
- 保存立即更新所有尚未开始单据（accepted/downloading/downloaded/waiting_offpeak）的时段开放时间并唤醒既有队列；实际标记 running 前在同一调度锁内重新检查最新设置，避免检查与起跑之间的配置变更漏判。系统投递及资料上传共用此规则。进入允许时段后按 worker 可用性排队开始，
  并不承诺所有单据同时起跑。已启动 runner 不暂停、不取消、不重新核销。
- `offpeak` 状态新增 `mode`、`effective_mode`、`mode_label`、`revision`、`allowed_now`、`weekly`、
  `holiday_all_day`、`holiday_dates`、`startup_override`。`allowed_now` 是新单能否起跑的权威值，
  `peak_now/offpeak_now` 仍表示官方计费参考，不代表自定义执行计划。
- 工作台保存的时间表是排队调度的最高优先级。旧 `--always-audit` / `OFFLINE_AUDIT_ALWAYS_AUDIT=1` 只在没有保存过时间表（revision=0）时提供启动默认值；保存后立即执行时间表，无须修改启动参数或重启。
- 改表撤销所有尚未开始单据的旧 `force_now` 标记。门禁只认可持久化记录里的当前标记，不信任队列旧副本；“立即分析”新操作与改表使用同一调度锁，避免旧放行覆盖新时间表。

实现位置：`audit_core/audit_schedule.py`（校验、持久化、每周/跨夜/节假日判定）、`oss_intake.py`
（本地上传和系统投递共用起跑门禁）、`workbench_server.py`（配置接口）、唯一客户 HTML（时间表和独立编辑表单）。
当前接入平台的实际计费以其账单为准，自定义时间本身不改变模型单价。

后台点按“立即分析”调用 `POST /api/intake/jobs/<24-hex-job-id>/run-now`（同源、JSON 只能为 `{}`），
把该 attempt 标记 `force_now=true` 并立即进入核销队列；已完成或正在运行的 attempt 返回冲突。
后台“全部立即分析”调用 `POST /api/intake/jobs/run-now`（同源、JSON 只能为 `{}`），
一次放行所有处于 `waiting_offpeak` 的 attempt；已在运行或已结束的记录不受影响，批量请求不会因单条状态变化而失败。
核销一旦开始即不再受时段限制：窗口关闭不会暂停、取消或重新排队，必须跑到本单结束。
服务重启后 `waiting_offpeak` 任务重新入队重新判定，不按“重启中断”封存为失败。

排队、运行和失败任务删除使用 `DELETE /api/intake/jobs/<24-hex-job-id>`，请求必须来自当前工作台同源、携带 `/api/config` 的删除确认令牌，并且 JSON 只能是 `{"confirm_job_id":"<同一个 job-id>"}`。服务必须确认 runner 已停止并证明所有资源唯一归属于该 attempt 后才返回成功；否则返回冲突且保留记录，不得只做前端隐藏。

默认模式下的已完成任务包含正式 runner 回执和已投递回调回执。
明确无回调任务记录 `callback.status=not_required`，并通过该状态 API 消费。
仅接收任务同样记录 `callback.status=not_required`，下载验证后立即完成，保持 `result=null`，
并通过 `delivery.input_file` 暴露持久文件。
失败任务包含有界且已去除 URL 的消息；如果正式 runner 在回调失败前已经完成，还包含保留的已完成结果。
任务状态属于运行元数据，绝不能表示为业务证据。下载验证成功后，`delivery` 还包含 `input` 下的本地持久
`input_directory` 和 `input_file`。

若服务在完成前重启，活动回执标记为失败，因为签名 URL 被有意设计为不持久化。
重启后绝不能静默重放可能产生费用的 AI 运行。检查终态回执；确需重新正式分析时，
上游使用足够长有效期的新 URL，再次发送相同 `verifyCode`、`analyzeId` 和对象路径。
这会创建下一个 attempt 及任务目录。此前验证通过的 ZIP 可以保留在原任务目录供诊断。

bizType已确定类型后，资料缺少、多余或适用条件不明时，正常输出本类型具体资料问题，manifest、snapshot、正式收据和OSS job记completed、failure为空、CLI退出0。只有指定类型资料门禁满足才进入对应业务审核，审核仅执行本类审核栏。业务问题和原文“0核销”只报具体错误，最终交人工复核。bizType输入失败或意外中断处理记运行失败；回调传输故障单独保留callback_failed和已完成结果。历史档案只读，不重写或重发。


API `1.55` 的 `/api/config.material_issue_file_policy=related_files_only_hide_archive_gaps` 声明资料问题只显示需补充或调整的相关文件；整份合同等整体缺失隐藏文件区域。该字段仅约定客户展示，不改变资料门禁、业务判断、三字段回调或原始证据。
