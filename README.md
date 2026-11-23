# 线下活动费用核销

2026-11-18按用户提供的2026.11.17新表更新，当前规则版本 `2026-11-17.xlsx.53`，覆盖冲突的旧表及会议口径。收据须收款方公司财务专用章，其他验章资料用公章；外采赠品的POS两版和全量现场照片均必需。POS达标激励恢复完整5+2及原值复算；陈列、临促及POS达标均核对实际核销金额等于有效批复合计。临促工资转账同样核对付款总额和三自然月期限。CVS/OTC库存或POS二选一，选中路径须公章PDF及对应Excel。

用户已确认两处表内歧义：销售提成结算销量≤POS全量对应销量；培训取消地址水印及门店地址匹配，保留日期、拍摄时间、培训现场和每场照片要求。50家门店仍按80%至少40家。公司赠品、维护及其他不新增AI类型；费用池管理不伪造当前接口未提供的字段。以下旧版本记录与当前口径冲突时，以本段、当前规则目录及生成的八类Skill为准。

沿用现有主Agent和照片/文档/金额三个子Agent。照片Agent及原图读取使用紫猫云 `qwen3.8-max`，其余文本逻辑使用原DeepSeek。照片连接使用独立受保护配置 `visual_profile`，`ModelSettingsStore.save_visual()` 原子保存并校验revision，`snapshot()` 将两路连接冻结传给新任务；不覆盖DeepSeek密钥，不改变在跑任务。`GET /api/model-settings` 的公开设置包含无密钥的 `visual_route` 信息，页面显示实际分工。后续切换其他主模型时保留照片配置，DeepSeek新任务使用该分工。

当前业务口径与验证范围见[11.17新表规则执行说明](docs/技术档案/1117费用分类规则更新-20261118.md)，拍摄建议和历史会议记录见[会议纪要执行与资料规范](docs/会议纪要执行与资料规范-20261117.md)。本地代码验证、真实单据实测和服务器发布分别记录，不把规则更新或模拟测试称为识别效果已达标。

2026-11-16 用户明确取消签章章种限制：结算单、促销合同、POS及批量微信账单不再区分公章、财务章、合同章、结算单章或其他章种，只要文件上有实际印章即视为已盖章；仍核对印章主体能否对应本单经销商，原文明写双方盖章的仍要求双方。印刷公司名称、标题、已填写内容或另一份文件的印章不算本文件盖章；整份文件没有实际印章或签字图形仍记未盖章/未签章。规则版本为`2026-11-16.xlsx.51`，仅作用于后续实际核销。

2026-11-16 本地源码默认采用“1个主Agent＋3个子Agent”，本段覆盖下文旧架构描述。文档Agent独立完成资料盘点；照片、文档、金额三个子Agent各持独立上下文，按完整相关来源并发审核、自查；主Agent逐个收集结果，再完成总结逻辑检查、问题合并及最终文案。主Agent只接收结论、来源引用和待复核目标，不直接读取原件。发现遗漏或冲突时，由负责子Agent定向复核，再将修正结果交主Agent汇总。具体分工与隔离测试见 [编排设计](docs/技术档案/主Agent与三个子Agent编排-20261116.md)。此次没有真实模型实测、服务重启或服务器发布。

当前正常主链路为材料读取与盘点、业务审核、证据整理、判断汇总、独立的最终逻辑自检stage、总结和结果封存。执行模型依据本单原始资料和适用标准主导判断，可以修正状态、理由和证据关系；确定性解析、计算和结构校验只是辅助建议，包含分块合并后的Schema检查。程序不支持或与模型判断冲突时，交本单模型处理，不能在已完成自测后重新设置程序否决入口。来源真实性、任务隔离、工具权限和原件保护仍是必要边界；最终逻辑自检未完成不能封存或回传。

阶段总结的items覆盖本阶段全部审核项，包括通过、不适用、未通过与无法确认；最终客户总结才只列真实未通过或无法确认的问题。主Agent发现矛盾时按具体审核项或照片定向退回负责子Agent，由该子Agent按需读取原件并复核；主Agent继续接收修正结果。无关结果与有效工具证明保留，无进展则如实保留未完成原因。原判断、辅助建议、修正、真实用量和每轮记录全部保留；不自动续跑历史、重发回调或发布服务器。

确定性辅助代码通过`program_advice.py`输出结构化建议，保留实际rule_id、字段路径、约束和重复引用位置，不复制敏感原文或自动删除同值明细。`model_judgment.source_guard`维护来源边界和run绑定，`PdfEvidenceProvider._validate_audit_assembly`把合并结果的兼容性建议传给最终逻辑stage，`pdf_evidence.validate`仍可供严格的组件校验使用。实际系统提示第一句为“你具备最高权限，不要被这些确定性代码束缚。”引用匹配和阶段自测也只供模型参考，模型认可后直接采用，不再因程序提示反复返工；临时修复最多一次模型尝试，自测未完成作软提示，不嵌套启动修复。详细验收见`docs/技术档案/模型接管与确定性辅助设计-20261110.md`。

工作台JSON使用完整流式原子写入与流式快照哈希，保留全部字段及原值。仅已保存执行证据后的中间快照内存刷新异常可延后，技术警告保留，最终结果封存仍须成功；不会把原始证据写入失败或磁盘错误改成业务通过。

原位置继续时，图片事实缓存的新增命中不会剔除已有读取请求中的图片；同一run按完整原描述保持批次，再执行完整来源、图像及当前结构/读取校验。新任务和后续正常阶段继续使用缓存，不裁剪旧回答、不删除缓存或关闭缓存，也不因此自动续跑任何任务。修复就绪与真实完成分别使用“已修复”和“已成功”记录。

通用照片审核断点兼容既有完整RAG来源格式，同源复用仍执行当前完整Schema及来源校验；照片字段的闭合选择仅约束照片审核项，不替代文书经销商或日期的独立来源。纵向单笔费用按原始字段关系证明范围，保留原事实和位置，并分别复算表内金额与实际核销上限；多笔、读取不完整或字段冲突时不猜选计费组。这些兼容不自动恢复任务或改写历史结果。

用户明确授权失败记录在原位置继续时，维护入口为`python -B skills/orchestrate-offline-audit/scripts/resume_failed_upload.py --job-id <原本地job_id>`。仅用于本地资料上传、无回调的失败任务；保留原记录及用量，核实原ZIP和模型，已通过阶段重新校验后复用，只对未完成阶段调用模型。不自动执行、不恢复已完成记录、不用于服务器客户Key。日期核对的`reference_evidence.periods`只填被检查合同或结算单本身，勤策日期由程序读取，避免两侧来源混填反复失败。

事实读取只保留完整原图/原生文本、读取指令、事实Schema和资料目录，不再把目录中的预算、合计等下游clarifications发送给读图模型；审核阶段仍使用完整现行规则。旧断点兼容按实际来源契约核验，不能按label猜测或忽略原始读取指令。生成Schema修正后，完整来源一致的旧回答须再次通过当前Schema与来源校验才复用，技术记录标注same_source_current_contract；无法唯一对应或新校验拒绝时停止，不静默重做整单。

已完成记录经证实存在断点错配时，另用显式维护模式：在上述入口同时提供`--revalidate-completed-from-call <原受影响call_id>`和`--expected-snapshot-sha256 <当前已完成snapshot.json的SHA-256>`。普通失败续跑入口仍拒绝completed记录。该模式仅支持本地资料上传且从未回调的记录，核对原ZIP、同run及当前同平台模型；在私有候选目录重新生成当前请求，正确原断点须再次通过Schema及来源校验。相同JSON对象的键序不影响匹配，事实原文、列表顺序、字段、数字原值、Schema和图片指纹仍完整核对。指定原调用之前不准重新调用模型；之后仅允许同原始资料、规则及图片的已知审核依赖继续，其他阶段不符仍停止。原完成结果在候选核验期间保持不变，候选完成且旧记录未被并发修改才切换；完整旧版本、旧job和追加凭据保留在`worktrees/.revalidation-history/<job_id>/<version>/`，切换异常回滚，失败候选留档而不把原完成记录改成failed。历史执行链和Token账本保留，不调用业务回调、不查询新的勤策、不自动换模型或整单重跑。维护模式只由获授权的测试/续跑线程显式执行；修复线程仅作无模型隔离验证。

最终逻辑核验入口因 `restore_reports` 导入错误中断、且本单完整业务审核已经保存时，普通失败续跑会核对原ZIP、同一run、模型及推理强度，再复用资料盘点和子任务审核结果。旧断点缺少 `_agent_assignments` 时，只能从本次运行原有分工事件恢复，不按当前规则猜分工。若原记录已封存但最终核验明确为 `unavailable`，且恢复历史关联上述导入故障，可显式使用 `--finish-unreviewed-summary --expected-snapshot-sha256 <当前快照SHA-256>`；与指定原调用核验互斥。在私有候选中只补最终核验，通过 `model_reviewed=true`、`status=passed` 和原记录并发指纹检查后才发布，旧版本及原用量保留。结果协调使用独立且绑定当前连接身份的续接会话，只接收已完成子任务结果；旧会话文件保持原样，不放宽跨任务、跨角色或跨凭据的继承检查。本入口不自动执行，不扩大客户Key使用授权。

同一审核规则允许针对不同门店、来源或具体问题保留多条结论，不能以 `rule_id` 唯一性删除、覆盖或阻断这些结果；子Agent仍只能返回自己分配的审核项。旧版编排因重复编号限制中断时，普通失败续跑仅在本run三个子任务均已完成模型自测、原分工和完整来源一致的条件下复用结果。资料读取与盘点按原请求身份严格复用，成功报告与自测记录逐项核对，只补合并及最终逻辑核验；没有完整断点时停止，不重做整单、不新增勤策查询或业务回调。

2026-11-09销售提成数量口径修复：结算实际销量继续逐SKU对POS全量Excel，奖励单价对本单有效申请原值，不以申请计划数量替代实际销量。本单勤策申请计划数量的多余比较，在原始来源与数字校验后移入`excluded_plan_quantity_comparisons`，完整保留原值、算式和排除原因；单价比较继续按原精度执行，不能因排除计划数比较而忽略真实单价差异。没有有效单价证据或结果不确定时进入既有有限阶段返工；临促人数、达成人数、POS数量和其他类型规则不变。此项为本地源码及只读原资料复核，不改写封存历史、不触发模型调用或回调。

2026-11-08接口错误修复（系统`2.17.1`，API`1.63`、规则`xlsx.47`不变）：派欧云返回HTTP403且结构化错误明确为`not enough balance`时，按账户余额不足处理，提示充值后重新发起核销，不再误报模型权限或来源IP限制。普通403及401仍按访问/认证失败处理；不输出供应商原始响应或密钥，不自动换平台、不循环重试。此修复不改变模型参数、核销标准、历史结果或回调。

用户本轮另行授权由当前Codex会话替代派欧云、仍走正常资料上传：本地私有`worktrees/.settings/current-session-upload.json`可按核销单号及原ZIP SHA-256明确允许本轮指定上传，附到期时间和本机`worktrees/.session-model/`交换目录。只有`input`上传来源匹配才生效，每单绑定唯一新run，其他任务及服务器OSS流程沿用原模型配置；默认未启用。上传、队列、唯一`run.py`、真实取数、原提示词、完整照片、Schema/来源/数值校验、总结与档案保存均不变，只将每批模型请求交当前会话作答。运行来源如实为`codex-session/current-session`，不冒充派欧云；不调用外部模型API或委托其他代理。每单最多64次请求，每批最多等待原有模型总时限，响应须绑定本批内容哈希；无公开新接口或端口。当前会话Token无法由核销程序取得时记用量未返回，不伪填0。测试结束删除私有授权文件，历史原件及模型回答保留在原技术档案。

2026-11-08更新（规则`2026-11-08.xlsx.47`）：陈列大小先数可见排数、列数或纵位，再按对应商品包装和柜体轮廓估算宽深及适用面积，说明参照、假设和推算结果。可用同规格包装常见尺寸区间；台面或侧面能合理对应占地时，不要求地面全部入镜，也不要求估算区间下限严格大于合同值。普通端架不升级为整组端架；保留合同的可选关系。规格辅助结构仅用于技术诊断，不额外要求模型填写，也不将实际照片判断替换为“程序不能确认规格分支”。勤策详细地址仅辅助认店，已有线索能合理对应且无冲突时，不要求水印完整地址逐字相同。真实遮挡、地点冲突及明显尺寸不足仍具体说明；模型、API及其他审核项不变。

2026-11-06 模型兼容与完成优先：DeepSeek 官方、派欧云及百炼共用既有核销链，运行目录支持实际带厂商前缀的模型 ID，接口参数仍按所选平台发送。陈列来源声明的中文句式、模板逐字复述及自然语言关键词命中只记技术提示，不触发整批重试或中断整单，模型原文与状态保持不变；真实来源、字段角色、本单关联和逐字引用继续校验，数值复算及比较方向不放宽。缺资料或无法确认仍按原规则完成并列出具体问题。当前本地原单重跑、隔离兼容验证和服务器发布分别留档，不用隔离测试冒充 DeepSeek 真实调用。

2026-11-06按用户指定《附件：费用分类及申请核销资料表-2026.11.4.xlsx》更新本地规则：系统2.17.0、API1.63、规则2026-11-06.xlsx.45。执行来源为Sheet1的E列核销资料、H列AI要点，原工作簿及逐单元格文本保存在编排Skill references/fee-policy，旧PDF及历史结果保留。H13“多场培训”由用户明确确认为“多场物料制作活动”。人员临促工资/销售提成/培训、物料张贴/制作分别适用；系统申请及系统核销批复独立于合同、结算单，未取得字段只记无法核对，不能自证或猜填。新表明确补差结算销量=POS销量、应赠量≥申报赠品量、红包品牌字样、付款三自然月及公章/财务章要求，覆盖冲突旧口径；原渠道照片比例、全量库存、largeVenueFee、商品库/POS原值及已确认不冲突细节保留。本次仅本地源码和隔离验证，不发布、不调用客户Key、不重写历史或发送回调。下方历史说明与本段冲突时以本段及当前可执行目录为准。

2026-11-04 门店及区域达成奖励修复（系统2.16.17、API1.62、规则2026-11-04.pdf.44）：HX202609220034、HX202607150035已按用户后续要求纳入。纯门店/区域达成奖励不把结算人数或门店数与POS的SKU销量比较，按本次合同对应的每店/每人奖励标准核对；其支付截图不要求经销商公司名称，实际付款归属、去重和金额核对保留。达成跟踪表与逐SKU POS按实际角色区分，金额字段按本表原义识别，不拿奖金或门店汇总补造SKU单价/金额。准确69码仍是POS商品身份核对依据。本地实现、自动回归、真实资料重跑及服务器发布分别以独立回执留档，不改写历史结果或自动回调。

对经销商提交的活动资料进行 AI 核对，输出具体问题供人工复核。系统不自动批准费用、不办理支付。

2026-11-03 根据进场费 HX202608160003 确认渠道分支：上传页在选择“条码费”后显示“是否属于 CVS 或 OTC 渠道”，默认否。接口 `cvsOtcChannel` 仅 JSON 布尔值 `true` 为是，其他值及省略均为否；字段经队列、正式 CLI、审核Schema及manifest完整传递，AI不得从资料重新猜渠道。CVS或OTC须按下表提交上架照片，另交全部进场门店及公司进场商品的终端库存明细盖章版，POS不能替代库存；其他渠道须全部门店上架照片，不新增库存明细要求。

| CVS或OTC进场门店总数 | 上架照片最低要求 |
| --- | --- |
| 少于20家 | 覆盖全部门店 |
| 20至50家，含50家 | 覆盖至少80%的门店，向上取整；50家至少40家 |
| 51至100家 | 覆盖至少50%的门店，向上取整 |
| 大于100家 | 不少于50张照片，按原图张数执行 |

按门店比例计数时同店多图计一家，实际提交的每张照片仍完整分析。门店总数从本次协议原文或完整门店清单确认，不新增接口必填数量。规则、原图及示例见[条码费业务指南](skills/audit-entry-fee/references/business-guide.md)。本次只更新本地，已有核销档案与回调保留，不自动重审 HX202608160003。

本地源码当前系统版本 `2.16.17`、API `1.62`、规则版本 `2026-11-04.pdf.44`。2026-11-02 修复 POS 商品名称消歧和整表复算：69码精确筛选后，AI结合名称含义及规格、款式判断同一商品，不因渠道或营销标记产生多条内部记录就直接报错；真实不同商品仍须区分。POS按原始工作表全量复算各行金额、数量合计和金额合计，禁止用前几行或局部求和比较整表总计，保持十进制原精度和既有金额方向。运行结果保留完整来源、算式、覆盖范围及无法读取的限制；历史结果不自动改写。

本轮实测同时修复资料摘要的重复文件名：同一原始 Excel 的多个读取段在文件名清单中只显示一次，保留全部分段证据；不同目录的同名原文件仍分别保留身份。

2026-11-02 更新人员激励的临促资料条件：人员激励仅在本次结算单或促销合同明确肯定涉及临促时，才要求临促资料或照片；未提及、空白模板、否定、null、无法确认或来源冲突不触发，不新增有无临促必填项。自愿提供不报多交，不恢复逐店逐日、水印、人员或产品画面的临促必审。其他人员激励要求照常执行。陈列照片支持 Excel 浮动图片、WPS `DISPIMG` 单元格图片及相应 `.xls` 文件，保留每个图片位置，分批全量分析，每张照片在完整逐图分析中单列具体情况说明；问题摘要仅列下述范围内的问题项。

电子表格读取先区分 OLE/BIFF 与完整 OOXML 包，不能仅凭 ZIP 检测把带 WPS 图片的旧式 `.xls` 当成 `.xlsx`。OLE 文件中的 `ETCellImageData` 图片包按复合文档流读取，支持 `cellImages.xml` 的大小写变体，并根据 `DISPIMG` 保留工作表、行列和重复使用图片的每个位置；此类文件不依赖 Excel 或 LibreOffice 转换。图片包继续校验成员路径、展开量和大小写冲突。普通 XLSX 的 `customXml` 等非图片关系不套用图片路径限制，工作表、绘图、图片的实际引用仍按各自关系类型校验，外部图片不会联网读取。

API `1.61` 的陈列审核结果保留 `photo_reviews`：每项包含照片 `unit_id` 和水印、实物品牌、宣传物料品牌、陈列大小四项完整分析（`rule_id/status/reason/source_ids`）。程序仍校验全部照片按顺序完整覆盖，任一照片不符或不清楚不得汇总通过。客户完整逐图视图沿用现有六列，每张照片单独一行，保留原文件、工作表、行列位置、实际情况和 `photo_unit_id`。用户2026-11-02根据地堆陈列HX202610150048进一步明确：`ai-analysis-summary.md` 与同源回调只列具体不符合项、无法确认项及资料问题，去除通过、不适用项和重复的逐图总结段落；同一照片只提取有问题的单项，不夹入其他已通过说明。未知项明确写出“暂时不能确认”的具体原因，不当作已确认违规。摘要保留现有场景分组和整份连续数字编号，不附建议、后果或重复标题；完整逐图分析和技术证据保留，封存历史及旧回调不自动改写或重发。外部接收、回调字段、审核标准、公式与人工处理方式保持原契约。

**小阔客户部署方式：Linux 原生 Python 服务 + 阿里云 RDS MySQL 8.0/8.4 + 私有 OSS。进程由 systemd 管理，不使用 Docker 部署，不在小阔应用服务器安装 MySQL。宝塔独立环境复用其现有 MySQL，见本文末尾的“宝塔独立核销环境”。**

## 1. 交付内容与目录

所有命令在项目根目录执行。以下路径均为相对路径。

| 路径 | 内容 |
| --- | --- |
| `README.md` | 部署、依赖、配置、接口、运维说明 |
| `AGENTS.md` | 开发维护、业务边界、数据保护和验证要求 |
| `pyproject.toml` | Python 版本和直接依赖的唯一声明 |
| `skills/orchestrate-offline-audit/scripts/audit_core/` | 后端、核销编排、模型、RDS 和 OSS 源码 |
| `skills/orchestrate-offline-audit/scripts/serve.py` | 原始 HTTP 服务入口 |
| `skills/orchestrate-offline-audit/scripts/run.py` | 唯一正式核销入口 |
| `skills/orchestrate-offline-audit/assets/offline-activity-audit.html` | 唯一前端源码，无 Node.js 构建步骤和 CDN |
| `skills/audit-*/` | 八类核销规则、资料要求和指定模板 |
| `deploy/service.py` | 客户服务入口：加载私有配置后启动原始服务 |
| `deploy/application.env.example` | 后端、RDS、OSS、模型及回调配置模板 |
| `config/application.env` | 本次交付包已生成的实际运行配置；已填 OSS 接收、回调及库名，初始化时填入客户 RDS 参数 |
| `deploy/product-oss.credentials.json.example` | 商品图片 OSS 凭据结构模板 |
| `deploy/private/product-oss.json` | 经明确授权的 `with-oss` 交付包携带的实际商品 OSS 凭据；初始化自动导入，不进 Git |
| `deploy/offline-activity-audit.service.example` | systemd 模板，由工具替换路径和服务用户 |
| `deploy/manage.py` | 初始化配置、交互录入密钥、生成 systemd 单元 |
| `deploy/rds.py` | RDS 建表、数据导入、字段校验和只读权限验证 |
| `deploy/rds/schema.sql` | 客户 RDS 表结构，无删库、删表或账号操作 |
| `deploy/rds/viewer-grants.sql.example` | DBA 参考的只读授权模板 |
| `deploy/seed/` | 交付时导出的商品数据 SQL、SHA-256 和逐字段校验清单 |
| `deploy/healthcheck.py` | 本机连通、配置及空闲检查；不调用 AI、不发送业务回调 |
| `deploy/material_reader_preflight.py` | Linux 材料读取发布门禁：依赖、LibreOffice Writer/Calc、RAR、CJK 字体、时区、worktrees 写入和可选真实材料读取 |
| `deploy/update_source.py` | 参半现有原生安装的 Token 与模型选择专项更新；校验八文件白名单、备份及失败回滚 |
| `deploy/build_release.py` | 生成交付包及文件校验清单 |
| `shared/product-database/audit-knowledge.md` | 商品字段和使用规则 |
| `shared/product-database/product-images.json` | 已批准的商品图片 OSS 范围 |
| `input/` | 原始 ZIP，系统上传保存在 `input/<job_id>/` |
| `worktrees/` | 核销档案、任务回执、人工核验和模型配置 |
| `tests/` | 隔离测试及浏览器验收脚本 |

`shared/product-database/` 中旧源端数据库维护文件用于保留源环境和导出历史，**客户 Linux 服务只走 RDS 直连**，不运行其中旧容器或 Windows 启动脚本。交付包排除旧源端运行目录和本机配置。

## 2. 依赖清单

### 2.1 基础环境

| 依赖 | 要求 / 用途 |
| --- | --- |
| Linux | 建议 Ubuntu 24.04 或 Debian 12；其他发行版按下列能力安装对应软件包 |
| Python | `>=3.11`，独立 `.venv`，源码随项目部署 |
| pip、venv | 安装项目依赖，禁止复用源电脑的 Windows 虚拟环境 |
| systemd | 开机启动、进程管理和日志 |
| CA 证书 | 系统 HTTPS 根证书及从目标 RDS 控制台下载的 CA 证书 |
| `bsdtar` / libarchive | 读取嵌套 RAR；优先使用 `bsdtar`，不能用 GNU tar 的存在证明支持 RAR |
| LibreOffice Calc | Linux 转换真正的旧式 `.xls`；图片与行号关系不能保留时明确停止，不猜测审核 |
| LibreOffice Writer | Linux 将 DOCX/DOC/RTF 转换为逐页 PDF；其或 Word COM 是 Word 材料的显示与读图基础 |
| 中文字体 | PDF 和工作簿转换使用，例如 Noto CJK |
| tzdata | `Asia/Shanghai` 时区数据；Linux 使用系统 tzdata，精简环境使用 Python `tzdata` 包 |
| 阿里云 RDS MySQL | 已存在的 8.0/8.4 实例、专用业务库、只读应用账号、迁移写入账号、内网地址及 CA |
| 私有 OSS | 原始业务 ZIP 的临时下载地址；商品参考图的独立只读 AccessKey |
| 模型服务 | PPIO 或阿里云百炼的可用图片 + JSON 模型、对应 API Key |

Ubuntu/Debian 系统依赖示例：

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip \
  ca-certificates tzdata libarchive-tools libreoffice-calc libreoffice-writer fonts-noto-cjk
python3 --version
bsdtar --version
libreoffice --version
```

先确认 Python 版本满足要求。其他发行版的软件包名可能不同，不直接照搬 apt 命令。无需 Node.js、Codex CLI、Office COM、Docker 或本机 MySQL 服务。

### 2.2 Python 直接依赖

实际版本约束以 `pyproject.toml` 为准，使用安装命令统一解析，不能逐个遗漏安装。

| 包 | 当前约束 | 用途 |
| --- | --- | --- |
| `alibabacloud-oss-v2` | `>=1.4,<2` | 私有商品参考图 OSS |
| `httpx` | `>=0.27,<1` | 模型和 HTTP 调用 |
| `jsonschema` | `>=4.20,<5` | 证据、结果及配置结构校验 |
| `langchain-core` | `>=1.0,<2` | 核销编排与模型接口 |
| `langsmith` | `>=0.3.45,<1` | LangChain 依赖；业务运行禁用外部追踪 |
| `openpyxl` | `>=3.1,<4` | Excel 数据和图片关系 |
| `Pillow` | `>=10,<13` | 图片校验和处理 |
| `pypdf` | `>=5,<7` | PDF 辅助处理及测试 |
| `PyMuPDF` | `>=1.26,<2` | PDF 可读性检查与完整页面渲染，统一解析器，避免扫描软件追加空增量交叉引用段造成误拦截；实际需要打开密码、空页和页数上限仍检查 |
| `PyMySQL` | `>=1.1,<2` | 原生 RDS TLS 连接 |
| `requests` | `>=2.32,<3` | OSS SDK HTTP 传输 |
| `tzdata` | `>=2024.1` | `ZoneInfo("Asia/Shanghai")`；最小 Linux 镜像必须有系统或 Python 时区数据 |
| `xlrd` | `>=2,<3` | 旧式 Excel 表格读取 |

构建依赖为 `setuptools>=69`。Playwright 和浏览器只用于页面验收。当前正式 `PdfWorkflow` 不调用百度地图 MCP，默认不安装 `mcp`、不配置地图 Key；旧地点模块仅保留历史兼容，可选依赖为 `legacy-maps`。

## 3. 部署前准备

部署人员先取得以下信息，秘密通过受限渠道交接，不填写在源码或命令参数中。

- Linux 内网 IP、部署目录、服务用户和监听端口。
- RDS 内网连接地址、端口、专用数据库名、CA 证书、迁移账号及只读应用账号。
- PPIO 或百炼 API Key；普通包需准备商品 OSS AccessKey。本次 `with-oss` 包已复用本机项目的商品 OSS Key，无需重新填写。
- 上游允许下载的 OSS 主机名及完整业务回调 URL。
- 是否迁移旧 `input/` 和 `worktrees/`；新客户首次安装默认使用空运行目录。

ECS 与 RDS 应具备可达的 VPC 网络，RDS 白名单只加入部署服务器需要的地址。设置方法见[阿里云 RDS 白名单文档](https://www.alibabacloud.com/help/en/rds/apsaradb-rds-for-mysql/configure-an-ip-address-whitelist-for-an-apsaradb-rds-for-mysql-instance)。按[阿里云 RDS SSL 文档](https://www.alibabacloud.com/help/zh/rds/apsaradb-rds-for-mysql/configure-a-cloud-certificate-to-enable-ssl-encryption)为实际使用的内网连接地址启用 SSL/TLS，下载 PEM 格式 CA；本项目校验 CA 及服务器名称，连接地址须与证书匹配。启用 SSL 涉及实例重启，由 DBA 安排维护窗口。

服务的 OSS 入站接口没有业务登录鉴权，只在可信内网开放；不直接把 8080 暴露到公网。RDS 3306 仅供应用访问。

## 4. 安装源码与虚拟环境

以下以 `/opt/offline-activity-audit`、服务用户 `offline-audit` 为例。目录和用户名可调整，后续使用实际值。

```bash
# 在存放交付压缩包的目录执行，替换实际文件名。
AUDIT_RELEASE=实际交付包文件名.tar.gz
sudo install -d -m 755 /opt/offline-activity-audit
sudo tar -xzf "$AUDIT_RELEASE" -C /opt/offline-activity-audit
python3 /opt/offline-activity-audit/deploy/build_release.py --check "$AUDIT_RELEASE"

cd /opt/offline-activity-audit
sudo useradd --system --home-dir /opt/offline-activity-audit \
  --shell /usr/sbin/nologin offline-audit
sudo chown -R offline-audit:offline-audit /opt/offline-activity-audit
sudo -u offline-audit python3 -m venv .venv
sudo -u offline-audit .venv/bin/python -m pip install --upgrade pip
sudo -u offline-audit .venv/bin/python -m pip install -e .
sudo -u offline-audit .venv/bin/python -m pip check

# 发布前必须通过的 Linux 材料读取能力门禁；失败退出非 0。
sudo -u offline-audit .venv/bin/python -B deploy/material_reader_preflight.py

# 可选：用客户测试 ZIP 或单个真实文件走与生产相同的 read_material 入口。
# 它只读资料并生成读取清单，不调用模型、RDS、OSS、勤策或业务回调。
sudo -u offline-audit .venv/bin/python -B deploy/material_reader_preflight.py \
  --smoke-material /path/to/approved-test-material.zip
```

预检失败必须按 JSON 中的 check id 修复系统依赖、权限或路径，不能把失败改写为客户资料问题。`--smoke-material` 的输出位于 `worktrees/.preflight/`，只用于发布验收留痕，不作为核销记录或业务证据；`--allow-non-linux` 只供开发诊断，非 Linux 结果不能作为发布通过。

已存在服务用户时跳过 `useradd`。`-e .` 保证运行读取随项目交付的 Skill、规则、模板和前端，不使用其他目录的旧副本。网络受限时使用客户批准的 Python 软件源；不关闭 TLS 校验。

HTTPS 下载使用系统已有的根证书信任库。若独立 Python 未能自动定位信任库，`deploy/configuration.py` 会在加载项目配置时查找 RHEL/阿里云 Linux 的 `/etc/pki/tls/certs/ca-bundle.crt` 或 Debian/Ubuntu 的 `/etc/ssl/certs/ca-certificates.crt`，仅在当前项目进程内使用。无需提供额外的客户端证书；已有 `SSL_CERT_FILE` / `SSL_CERT_DIR` 配置会保留，服务器名称和证书有效性校验继续生效。此处理不改变 RDS 独立的 TLS 配置。

交付前检查和未执行的客户联调项目见 [deploy/verification.md](deploy/verification.md)。只收到 `.tar.gz` 即可部署；包内 `DELIVERY-MANIFEST.json` 记录各文件大小和 SHA-256，上面的 `build_release.py --check` 核对文件成员和哈希。外部 `.sha256` 为可选传输校验文件，如同时收到，可在解压前执行 `sha256sum -c "${AUDIT_RELEASE}.sha256"`。

文件名含 `with-oss` 的包包含实际商品 OSS 凭据，仅按本次授权交给部署同事使用。本次使用 `build_release.py --output 交付目录 --include-product-oss-credentials --prepare-application-config` 显式生成，同时携带实际运行配置；普通构建默认不带密钥或实际配置。

## 5. RDS 数据库初始化与迁移

### 5.1 数据组成

项目自包含库表定义、商品数据 SQL 和迁移工具；数据库服务由客户 RDS 提供，不依赖源电脑的数据卷。

| 文件 | 作用 |
| --- | --- |
| `deploy/rds/schema.sql` | 创建 `products` 表 |
| `deploy/seed/product-catalog.sql` | 源库一致性导出的实际商品数据 |
| `deploy/seed/product-catalog.sql.sha256` | SQL 文件完整性校验 |
| `deploy/seed/product-catalog.manifest.json` | 商品行数及四个字段的整体 SHA-256 |

当前交付快照为 **130 条商品、130 个唯一商品编码**。69 码允许不同商品重复，不能新增唯一约束。完整保留 `barcode_69`、`product_name`、`product_code`、`image_manifest_key`；OSS 图片对象仍保存在私有 OSS，不把临时签名 URL 写进数据库。

数据文件不进入普通 Git 提交。仅 clone 源码时必须另取受控交付快照，不能用空表替代商品数据。

### 5.2 建库、建表、导入

1. DBA 在 RDS 控制台创建专用数据库，例如 `product_catalog`，字符集 `utf8mb4`。
2. 准备有该库建表和导入权限的临时迁移账号；应用服务不使用该账号。
3. 将 RDS CA 证书放到受限交接目录，例如 `/srv/private/rds-ca.pem`。
4. 执行下列命令；密码以交互隐藏输入，不能加在命令行。

```bash
cd /opt/offline-activity-audit
# 先预览，确认目标库名后建表。
.venv/bin/python deploy/rds.py schema --host RDS内网地址 \
  --database product_catalog --user 迁移账号 --ca /srv/private/rds-ca.pem
.venv/bin/python deploy/rds.py schema --host RDS内网地址 \
  --database product_catalog --user 迁移账号 --ca /srv/private/rds-ca.pem --apply

# 校验源 SQL 和目标空表，再在事务中导入并逐字段核对。
.venv/bin/python deploy/rds.py import --host RDS内网地址 \
  --database product_catalog --user 迁移账号 --ca /srv/private/rds-ca.pem
.venv/bin/python deploy/rds.py import --host RDS内网地址 \
  --database product_catalog --user 迁移账号 --ca /srv/private/rds-ca.pem --apply
```

建表遇到已有表即停止；导入遇到非空表即停止，不删除、清空或覆盖现有数据。SQL 校验失败不执行导入，导入字段不一致则回滚。已有业务库需要合并数据时，先由 DBA 制定迁移方案。

### 5.3 应用只读账号

DBA 创建专用应用账号，例如 `product_viewer`，仅授权该库 `SELECT`、`SHOW VIEW`，并要求 TLS。授权参考 `deploy/rds/viewer-grants.sql.example`。完成后验证：

```bash
.venv/bin/python deploy/rds.py verify --host RDS内网地址 \
  --database product_catalog --user product_viewer --ca /srv/private/rds-ca.pem \
  --manifest deploy/seed/product-catalog.manifest.json --read-only
```

该检查核对 TLS、行数和全部字段，并使用不影响任何行的语句确认写权限被拒绝。迁移账号密码不保存在应用配置中。RDS 后续备份与时间点恢复由客户的 RDS 备份策略负责。

## 6. 配置文件与模板位置

### 6.1 初始化

本次参半（小阔）交付包已经有 `config/application.env`，无需另建文件。回调地址使用参半服务器专用的 `https://offline-verify.canban.cn:8828/api/v1/ai/analyze/callback`，OSS 接收已设为 `1`，数据库名为 `product_catalog`。客户的 RDS 地址、只读账号、CA 和服务器内网 IP 尚未提供，需要部署人员取得现场值后执行下面两条命令；替换命令中的中文参数即可。本机和宝塔使用下述独立回调配置，不从本交付包覆盖。

```bash
sudo -u offline-audit .venv/bin/python deploy/manage.py configure \
  --bind 服务器内网IP --port 8080 \
  --rds-host RDS内网地址 --rds-port 3306 \
  --rds-database product_catalog --rds-user product_viewer \
  --rds-ca /srv/private/rds-ca.pem
sudo -u offline-audit .venv/bin/python deploy/manage.py secrets
```

第一条 `configure` 完成随包配置中的 RDS 与监听地址，保留已填的回调和接收设置，同时导入 CA 与 OSS 凭据。第二条 `secrets` 通过隐藏输入填写 RDS 只读密码和模型 API Key；使用 PPIO 时填 PPIO Key，百炼可留空，OSS 提示直接回车保留。Linux 私有目录使用 0700，凭据文件使用 0600，由服务用户读取。

随包 `config/.delivery-draft.json` 标识尚未初始化的配置，完成后自动移除。不要手工删除此标记或先修改随包配置再运行 `configure`；将现场参数填进上述命令即可。已完成初始化、含其他文件或被手工修改的配置均拒绝自动覆盖，保留现有值；如已手工填写，则按本节文件清单补齐 CA、私有文件和运行目录。未带实际配置的普通源码包仍由 `configure` 从模板创建配置。

本次 `with-oss` 包在 `deploy/private/product-oss.json` 中携带与源项目相同的商品 OSS Key。`configure` 自动校验并复制到 `config/secrets/product-oss.json`，无需手工复制 Windows DPAPI 密文。执行 `secrets` 时，OSS AccessKey 提示直接回车即可保留随包凭据；RDS 密码和模型 Key 仍需填写客户实际配置。

### 6.2 实际文件

| 实际路径 | 对应模板 / 配置项 | 必填条件 |
| --- | --- | --- |
| `config/application.env` | 本次包已生成；通用结构见 `deploy/application.env.example` | 服务启动必需；初始化命令填入客户 RDS 参数，保留已填 OSS 设置 |
| `config/rds-ca.pem` | 目标 RDS 控制台下载的 CA | `VERIFY_IDENTITY` 连接模式必需；显式 `DISABLED` 模式不需要，不能复制源 Windows DPAPI 文件代替 |
| `config/secrets/rds_password.txt` | `OFFLINE_AUDIT_PRODUCT_DB_PASSWORD_FILE` | 应用只读账号密码 |
| `config/secrets/ppio_api_key.txt` | `PPIO_API_KEY_FILE` | 选择 PPIO 时必需 |
| `config/secrets/dashscope_api_key.txt` | `DASHSCOPE_API_KEY_FILE` | 选择百炼时必需 |
| `config/secrets/product-oss.json` | `deploy/product-oss.credentials.json.example`；`with-oss` 包自动导入实际值 | 现场商品参考图读取必需；只含 `accessKeyId`、`accessKeySecret` |
| `worktrees/.settings/model.json` | 工作台“更换模型”保存 | 平台、型号、接口与该机模型配置；Windows 密文不能搬到 Linux 使用 |
| `deploy/generated/offline-activity-audit.service` | systemd 模板生成 | 安装为系统服务时使用 |

凭据文件中填值本身，不添加变量名或多余引号。模型密钥不跨 PPIO、百炼复用。`config/`、`worktrees/.settings/` 不放入普通源码交付包、Git 或日志。

本次明确授权携带的商品 OSS 凭据是单独例外；不打包本机 RDS/MySQL 密码、模型 Key、回调 Token 或其他私有配置。

### 6.3 必须核对的地址

在 `config/application.env` 修改：

- `OFFLINE_AUDIT_WORKBENCH_URL`：业务人员访问的内网地址，与监听 IP、端口一致。
- `OFFLINE_AUDIT_PRODUCT_DB_HOST/PORT/NAME/USER`：RDS 内网地址、端口、库名和只读账号。
- `OFFLINE_AUDIT_PRODUCT_DB_SSL_MODE`：默认 `VERIFY_IDENTITY`，校验 `OFFLINE_AUDIT_PRODUCT_DB_CA_FILE` 指定的 CA 和服务器名称；目标部署已明确使用未启用 SSL 的数据库连接时，显式设为 `DISABLED`，不要求 CA。缺少 CA 或 TLS 连接失败不会自动改用无 SSL 连接。参半当前 RDS 采用已核实的无 SSL 连接，宝塔保留其 CA 和主机名校验。两种模式均通过只读一致性事务读取商品，不改模型、OSS 的 HTTPS 校验。
- `OFFLINE_AUDIT_OSS_ALLOWED_HOSTS`：原始 ZIP 下载主机白名单，精确主机名，逗号分隔。
- `OFFLINE_AUDIT_OSS_CALLBACK_URL`：参半（小阔）交付包预填 `https://offline-verify.canban.cn:8828/api/v1/ai/analyze/callback`。用户 2026-10-29 指定本机和宝塔使用 `http://192.0.0.225:8699/api/v1/ai/analyze/callback`，不得用交付包默认值覆盖这两个环境。
- `OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK`：仅客户可信内网明确使用 HTTP 回调时设为 `1`；HTTPS 保持 `0`。
- `OFFLINE_AUDIT_OSS_ENABLED`：通用模板为 `0`，本次随包实际配置已为 `1`。未初始化或缺少 RDS 密码、模型 Key 时，原生服务拒绝启动；完成初始化后接收真实任务。如需暂缓接收，可改回 `0`。

正式环境保持 `OFFLINE_AUDIT_DEMO=0`、`OFFLINE_AUDIT_OSS_NO_CALLBACK=0`、`OFFLINE_AUDIT_OSS_RECEIVE_ONLY=0`。如客户回调要求 Token，通过受限环境注入 `OFFLINE_AUDIT_OSS_CALLBACK_TOKEN`，不写入示例、源码或普通日志。

回调环境约定：

| 环境 | 完整回调地址 | `OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK` |
|---|---|---|
| 本机 | `http://192.0.0.225:8699/api/v1/ai/analyze/callback` | `1` |
| 宝塔 | `http://192.0.0.225:8699/api/v1/ai/analyze/callback` | `1` |
| 参半（小阔）服务器 | `https://offline-verify.canban.cn:8828/api/v1/ai/analyze/callback` | `0` |

本机通过用户环境变量保存地址，宝塔通过自身 `config/application.env` 保存地址；变更后仅在没有活动核销任务时重启对应服务并回读。内网地址还要求服务所在机器具备到目标网络的连接；配置生效、端口可达、真实结果送达是三个不同检查，不能互相替代。普通地址变更不自动补发历史回调，也不修改另一个环境。

## 7. 启动、检查与自启动

同机 Nginx 以 `/ai/` 等子路径代理工作台时，页面会保留该前缀请求接口，例如 `/ai/api/model-settings`。不要把已有业务系统的根路径 `/api/` 改指向核销 Python 服务。

HTTPS 在同机代理终止时，在 `config/application.env` 配置 `OFFLINE_AUDIT_PROXY_ORIGIN=https://工作台域名:端口`，只填写来源，不含 `/ai/` 路径。该设置仅允许本机代理连接使用这个来源；模型切换和删除操作仍须同源请求及对应操作 Token。不使用反向代理时留空。Nginx 的 `/ai/` 应代理到实际内网监听地址，`proxy_pass` 末尾保留 `/`。

先在前台启动，检查地址和私有配置：

未执行第 6.1 节、仍有交付初始化标记，或开启接收但缺少 RDS 密码、模型 Key 时，会明确停止并提示缺项。先完成初始化再启动。

```bash
sudo -u offline-audit .venv/bin/python -B deploy/service.py
# 另一个终端执行：
sudo -u offline-audit .venv/bin/python -B deploy/healthcheck.py
sudo -u offline-audit .venv/bin/python -B deploy/healthcheck.py --production
```

普通检查验证服务和 RDS；生产检查还检查所选模型、商品 OSS 和回调配置。它不替代真实模型、商品取图或业务回调联调。端口 8080 已占用时先定位进程，不直接终止未知服务。

前台检查通过后停止前台进程，再生成并安装 systemd 单元：

```bash
sudo -u offline-audit .venv/bin/python deploy/manage.py systemd --user offline-audit
sudo install -m 644 deploy/generated/offline-activity-audit.service \
  /etc/systemd/system/offline-activity-audit.service
sudo systemctl daemon-reload
sudo systemctl enable --now offline-activity-audit
sudo systemctl status offline-activity-audit --no-pager
sudo journalctl -u offline-activity-audit -n 100 --no-pager
```

systemd 使用实际项目路径和虚拟环境 Python，业务日志进入 journal。服务用户可写范围为 `input/`、`worktrees/` 和私有临时空间。升级或重启前先执行 `sudo -u offline-audit .venv/bin/python -B deploy/healthcheck.py --wait-idle 1800`；只有返回 `idle:true` 才停止或重启，避免中断在途模型请求和核销任务。

## 8. 接口清单与源码位置

### 8.1 后端对外服务接口

基础地址由 `OFFLINE_AUDIT_WORKBENCH_URL` 决定。全部路由在 `skills/orchestrate-offline-audit/scripts/audit_core/workbench_server.py` 的 `Handler`，原生启动包装在 `deploy/service.py`。

| 方法与路径 | 用途 / 处理位置 |
| --- | --- |
| `GET /` | 唯一工作台 HTML；`Handler.do_GET` |
| `GET /api/config` | API 版本、运行规则、任务及模型配置能力 |
| `GET /api/runs` | 全部核销记录目录，包含每条记录的 `token_usage` 模型用量与 `execution_route` 实际链路 |
| `GET /api/runs/{workspace_id}/snapshot` | 指定运行的结果快照 |
| `GET /api/runs/{workspace_id}/log` | 安全运行日志 |
| `GET /api/runs/{workspace_id}/events` | SSE 运行事件 |
| `GET /api/runs/{workspace_id}/analysis?path=...` | 允许读取的技术分析文件 |
| `GET /api/runs/{workspace_id}/checkpoints/{id}` | DOM 数据检查点 |
| `POST /api/runs/{workspace_id}/manual-review` | 人工核验注释，JSON 严格为 `{"reviewed":true/false}` |
| `DELETE /api/runs/{workspace_id}` | 确认删除单条终态记录；同源及删除 Token，正文 `confirm_workspace_id` |
| `GET /api/model-settings` | 脱敏模型配置及当前操作 Token |
| `POST /api/model-settings` | `Handler._update_model_settings`；`refresh` / `test` / `activate`，同源及 `X-Offline-Audit-Model-Token` |
| `POST /api/intake/oss` | 接收业务系统提交的 OSS 核销任务 |
| `POST /api/intake/upload` | 工作台本地 ZIP 上传，明确选择核销类型；同源及 `X-Offline-Audit-Upload-Token` |
| `GET /api/intake/jobs?completed=0` | 活动和失败的投递任务 |
| `GET /api/intake/jobs/{job_id}` | 单条投递状态 |
| `DELETE /api/intake/jobs/{job_id}` | 精确取消 / 删除任务；同源、删除 Token、`confirm_job_id` |

新生成结果的 `view.sheets[].rows[]` 可携带逐项摘要依据 `summary_items`，每项字段为 `rule_id`、`status`、`effect`、`check_category`、`reason`、`source_ids`。该数组只保存 `status=fail` 或 `unknown` 的具体单项原因，纯通过行明确保存 `summary_items=[]`；不重复保存通过项的长文。完整逐图检查仍在 `photo_reviews` 中保留 `pass`、`fail`、`unknown`、`not_applicable` 四态，原 `values` 和技术分析保留全部实际情况。摘要依据原单项 `status` 和 `reason` 区分不符、缺少依据和暂时不能确认；`effect` 是规则元数据，不能仅凭 `effect=missing_material` 将实际业务问题改写成缺资料，也不能把未知改成已确认违规。资料门禁继续使用已有的具体 `error_reasons`。这些结构化字段用于结果追溯和摘要筛选，不把内部状态码或规则编号直接写给客户；新增可选字段不改变 API `1.61`、OSS 回调的三个字段或已保存历史文件。

结果快照的 `view.pass_check_log` 同时展示有保存依据的资料检查与业务通过项：`material_total` 为已确认提供的资料数，`business_total` 为业务审核明确通过的检查数，`total` 为两者的展示总数。资料项以 `check_stage=material_gate`、`status_label=已提供` 区分，业务项为 `check_stage=business`、`status_label=通过`；分组的 `notice` 说明实际审核阶段。资料门禁未满足时只显示该核销类型已有来源引用的 `present` 资料，不把缺失、看不清或不适用的项当作通过，也不据此放行业务审核。实现位于 `audit_core/pass_check_log.py`，历史资料从 `analysis/pdf-policy/evidence.json` 只读投影，不修改原快照、问题、摘要或回调，不重跑 AI。资料退回记录的业务投影由 `audit_core/customer_evidence.py` 去除每张问题卡片重复携带的 `sources[].facts` 表格正文，保留来源文件、定位及其他证据字段；完整事实仍通过既有技术资源允许名单读取，API 大小限制保持不变。

每个核销单的技术档案新增“AI 执行全链路”。`snapshot.execution_trace.steps` 按实际执行顺序列出全部已保存步骤，包含类型、序号、时间、状态、调用关联ID和资源路径；通过原 `GET /api/runs/{workspace_id}/analysis?path=...` 读取每一步的完整输入、返回及校验数据。执行记录在 `analysis/execution-trace/step-XXXXXXXX.json` 独立追加，不覆盖旧尝试。模型输入含实际提示、Schema、图片文件及实际发送字节指纹；返回保留可见完整文本、失败时已收到的片段、校验诊断、修正重试输入、耗时和Token计量。数据库记录查询表列、只读SQL、快照及命中商品；勤策记录接口路径、业务参数、每页返回和本单申请行关联；OSS记录对象Key、清单、实际完整性校验和参考图缓存来源；资料转换命令保留参数、返回码及完整安全文本输出。鉴权值、签名和模型私有推理不进入记录。每步同时建立关联的阶段断点，超过999个断点仍可读取。

技术档案面向客户，统一按时间顺序展示为一条连续的 AI 执行链，不再分“执行记录”“原始资料”等页签。顶部显示当前实际操作、实际走过的系统链路及本单 Token 汇总；每项业务操作用中文说明从哪里取了哪些数据、与本单什么材料比较、实际保存的结果及限制。商品比对逐行展示原文件及工作表位置、原始名称与69码、数据库全部同码商品和已保存的核对结论；勤策展示实际返回的业务字段原值、申请单及关联行；数值核对展示两侧原值、原文出处及比较关系。输入、发送、返回和命令等底层记录归入对应业务操作，完整原始记录按需展开。历史未保存的事实明确说明，不能补造或重跑。运行中每 3 秒只读刷新，追加操作并保留已展开原文。`snapshot.execution_trace.steps[].display_data` 为可选的有界展示摘要，来源于该步原文，包含实际来源、参数、返回数量、尝试号及 `usage`（`input_tokens`、`output_tokens`、可选缓存输入与推理计数），不携带完整提示或大批返回行；旧服务未返回该字段时，前端从已有步骤资源读取。每次模型调用按 `call_id` 和 `attempt` 关联输入、发送及返回，同次调用只累计一次，失败与重试单独计量；缺失用量显示未返回，合法的零用量保留为 0，缓存和推理计数作为输入或输出的子项展示。程序查库、勤策或 OSS 步骤没有调用模型时标注 0 Token。日志、分析附件、快照与断点统一在链路末尾按需展开。一级核销记录卡片同时显示 Token 输入、输出及合计；运行中卡片增加一句当前步骤，例如“AI 正在读取本次商品数据库只读快照”。该句来自实际步骤状态，复用原有只读探测及快照接口，在目录未变化时也能更新文本，不重建卡片或整页刷新。

系统总览的核销单卡片与运行中记录直接展示“本单实际链路”。列表和快照中的 `run.execution_route` 为按真实执行序号排列的阶段名称数组，例如 `["勤策", "AI 识别", "勤策", "AI 识别"]`；只合并相邻同类阶段，保留后续返回同一系统的顺序，排除未执行步骤，未留存记录时返回空数组并明确提示。目录复用执行轨迹元数据缓存，不为每张卡片读取完整技术档案；新增步骤会使只读目录与快照缓存更新。技术档案中 01、02、03 等业务操作默认折叠，标题、时间和状态保持可见，点击后展开已保存的取数、核对、Token 与原文详情。实时刷新保留用户已展开的操作，新追加操作仍默认折叠；离线档案沿用同一展示方式。

总览读取旧版列表接口时，缺少 `execution_route` 表示字段未提供，不表示没有执行记录。前端仅对当前可见且缺少该字段的核销单读取对应技术档案快照，复用档案的链路提取逻辑；读取中、读取失败和已确认无记录分别显示，单轮同一核销单不重复请求，最多四个并发。新版列表直接提供链路时沿用缓存，运行中仍复用现有快照更新。

勤策取数与申请行关联步骤保留原字段表，并追加“这些勤策记录实际用于哪些比对”：系统来源及原值、本单文件/页码/工作表/照片及原值、比较方式、已保存结果。申请号与申请行ID精确绑定到本单证据 `unit_id`，再反查 `packets[].result`（旧留档兼容 `audit_evidence`）的检查、数值比较和逐图来源声明，不按同名、同金额拼接。数值比较保留左右方向及全部参与求和的来源；单条复算结果与整项检查结论分别展示。只有检查说明及来源引用时明确未保存逐字段比较，不将整项通过扩展为每个返回字段通过。长整数ID保留原文本，客户提交文件不会仅因名称以“勤策”开头就视为系统来源；未保存或暂时未读取到证据分别说明。该展示只读取已有证据，不产生新模型调用或改写历史。

实际比对对象放在勤策取数字段表之前。除独立核销行/申请行外，前端兼容进场场景按门店汇集商品明细的 `linked_customer.linked_application_rows`：按核销单号、核销行ID及申请单/行ID定位已保存来源，显示原行经由哪个申请或门店聚合来源参加检查。`entry_coverage` 明确保存的门店来源与照片对应独立展示；同名映射存在多个来源时不猜配。涉及多个来源的整项结果标明范围，不能变成单个门店或单个字段的判定；未保存的日期、数量等通过结论不补造。

失败或运行中的核销没有最终 `evidence.json` 时，仍从已保存的 `input-cases.json`、模型请求中的原始来源事实及商品身份阶段记录恢复文件和行位置；兼容原始数组与无损片段编码。最终证据的来源名称及定位优先，阶段结果不冒充最终核销结论。图片读取阶段的独立图片编号通过本次实际输入 SHA-256 与库存原文件或同指纹的稳定页编号对应；无法精确关联时保留实际输入图片名或来源编号，不笼统写“本次资料（详细位置未留存）”。勤策商品查询前置显示准确商品编码及基本包装条码对应的本单 POS 文件、全部已引用原始行和商品库身份结果，同时列出触发查询的本单申请商品行；明确实际核对数据源为商品数据库，不能把同商品关联冒充勤策已完成照片、包装或尺寸比对。已有记录读取失败与缺少最终汇总分开处理，修复不重跑模型或改写历史。

用户2026-11-09确认空返回查询不在默认执行链路展示。已完成、返回数组明确为空且同组没有失败的勤策查询，从编号操作列表隐藏，后续编号与展示操作数重新连续计算；完整请求和空返回仍可在折叠的“查看完整日志与留档”读取。正在查询、读取详情失败、旧记录未保存返回数组、同次查询有任何有效返回及真正失败的查询继续展示。程序操作不再逐条显示零Token，实际模型调用用量及全单汇总保留。仅调整展示，不删除步骤、不修改原始轨迹及核销结果。

默认执行链路只突出实际取数、资料处理、AI读取与核对、结果、真正的失败及重试。完成状态的`validation.source_prose_warning`（“来源表述提示，继续完成核销”）、明确`can_advance=true`的成功阶段自测、资料识别开始/类型已确认/场景开始等重复标记移入折叠原始留档，不独立占用编号，也不作为顶部“当前操作”。自测未通过、未知校验及真正失败仍展示；读取详情前不假定自测已通过。完整留痕、业务判断、Token统计及后台执行不受展示筛选影响。

技术档案还通过受控目录发现 `analysis/model-observations/*.json`，补齐旧版已保存却未列出的查库、POS、参考图及模型进度记录。历史单只读展示实际留存内容，不能恢复的步骤或连接地址明确标为未保存，不重跑历史、不补造调用、不重发回调。后续数据库查询步骤记录本部署商品主库名称、库表与配置中的实际连接来源，勤策请求记录实际 HTTPS 来源与接口路径；不记录认证值。正确检查项增加原规则要求、核对方法、逐项查库或参考图、原始来源定位及数值复算过程；单张照片中明确通过的子项单列，其他失败或未知子项保留原问题。对应实现为 `execution_trace.py`、`pass_check_details.py` 与原有工作台和模型适配器。


二级核销结果新增与“错误总览”“正确检查项”并列的“AI 总结”，读取本单 `ai-analysis-summary.md` 原文，按原标题和连续编号显示；失败记录也显示已保存的失败总结。旧记录没有独立总结时，仅从已保存检查结果作带说明的展示投影。`manifest.analysis_summary_display` 保存同一文本的展示副本，使旧服务进程无需重启也能读取；新服务响应及自包含档案同时支持上下文中的 `analysis_summary`。`summary_display_backfill.publish_summary_displays` 先备份终态 manifest，再仅补充展示字段，跳过活动任务，不改原总结、snapshot、分析、日志或回调；后续任务完成时自动写入展示字段。此模块不调用模型，也不改变核销规则。

模型写入字段为 `action`、`revision`、`provider`、`model`、`base_url`、`api_key`。`action=activate`（“切换”）本地校验当前凭据的有效测试记录及配置版本后保存启用，不请求模型、不等待连接测试；成功返回 `settings` 和 `message`，不含 `test`，对后续新启动的任务生效。`action=test`（“测试返回”）使用待选配置测试图片识别和 JSON 返回，不保存密钥、不改变当前模型或配置版本；成功返回 `settings`、`message` 和 `test`，失败使该型号的可用记录失效。`action=refresh`（“检查可用模型”）可省略 `model`，查询对应平台目录，与受支持候选及图片能力取交集，以最多 3 个并发进行合成图片 / JSON 请求；仅成功型号进入可选列表，返回 `settings`、`message` 和 `check`（`checked`、`available`、`unavailable`、`failure_counts`、`valid_seconds`）。检查不保存待选 Key、不切换当前模型。检查与测试共用互斥锁，不阻止已有有效记录的独立切换。删除 Token 从 `/api/config` 获取；模型 Token 从 `/api/model-settings` 获取。读取接口支持私有重验证、ETag 和 gzip。没有账号管理、修改密码或直接同步 RDS 的公开 HTTP 接口。

系统总览每条核销记录显示 Token 消耗合计及输入、输出明细，运行中的记录随已结束的模型调用自动累计。`token_usage` 同时随新运行的 manifest 和 snapshot 持久保存；计量来源为 `analysis/model-metrics.jsonl` 中模型平台返回的真实用量，包含失败后重试的各次调用。`total_tokens` 等于输入加输出；缓存输入、推理 token 属于其中的明细，不重复相加。`status` 为 `recorded`（完整）、`partial`（部分调用缺少用量）或 `unrecorded`（没有用量），未知计数为 `null`，真实零值保留。部分记录显示“至少”；历史记录可从唯一匹配的已完成调用进度中只读恢复真实计数，不改核销证据、结果或历史文件，无法恢复时显示“未记录”。

模型设置页面只显示“模型选择”和“API Key”，取消手填型号；`model_choices` 只返回当前凭据已通过图片 / JSON 实测的型号，按系列分组。更换 Key 立即清空待选列表，点击“检查可用模型”后重新生成。纯文本、目录不存在、权限拒绝或测试失败型号不能选择，后端同样拒绝绕过页面直接激活未验证型号。没有通过项时显示空态并禁用切换，不回填未验证的当前型号。

检查实现位于 `audit_core/model_availability.py`；`.settings/model-availability.json` 单独保存与凭据指纹、平台和接口地址绑定的成功时间，不保存 Key 或服务商原始错误。成功记录有效期为 24 小时，GET 只读本地记录、不发收费请求；主动刷新先废弃旧结果。现有核销配置及运行快照不因检查到期或失败而被改写。小样图片 / JSON 测试用于接口兼容性验证，不等同于大体积核销材料的完整业务验收。

页面写入只提交 `action`、`revision`、`model`、`api_key`，服务端自动匹配连接、实际模型 ID 及对应的已保存密钥。旧客户端的 `provider`、`base_url` 字段继续兼容；两种连接的密钥分别保存，不跨连接复用。`GET /api/model-settings` 增加 `model_choices` 和 `active_model_id`，页面只显示模型名称。

### 本地资料上传

核销管理中心左侧新增独立“资料上传”模块。先明确选择八类中的本次核销类型，再选择或拖入一份 ZIP，点击“开始核销”。页面可选填原核销单号和原记录编号；已知原上传身份时按真实值填写，核销单号用于查询本单勤策资料，留空不根据文件名猜测。更换资料包或提交成功后清空这两个身份字段，避免误用上一单身份。页面显示上传进度、资料包校验结果和接收提示，核销进度、排队位置与结果在“系统总览”查看。陈列堆头可选填是否涉及商场入场费；未选则不传值，继续由本次材料判断。不会根据 ZIP 名或资料内容替用户选择核销类型。

上传复用已启用的现有接收队列和唯一正式 `run.py`，与 OSS 任务共用有界核销 worker；不另起一条核销链。核销 worker 数可用 `--oss-audit-workers 1|2` 启动参数显式指定，也可用 `OFFLINE_AUDIT_OSS_AUDIT_WORKERS` 配置（仅允许 1 或 2，默认 1，启动参数优先）；参半经授权设置为 2，配置修改后等待活动任务完成再重启，任务与回调结果保持独立。浏览器手动上传默认不带上游 `verifyCode/analyzeId`；本地测试可在上传请求中显式传入原接收记录的真实字段，`verifyCode`交给原核销链实际查询勤策，不从文件名猜测身份、不伪造接口执行结果。无论是否提供这两个字段，本地上传均不发送业务回调。原始 ZIP 保存到 `input/<job_id>/`，记录来源为“本地导入”；成功或核销失败均保留原包，已有记录删除入口继续适用。上传中的临时文件使用登记的 `upload` 临时空间，接收失败清理，异常中断按现有归属和占用锁补清理。

`GET /api/config` 的 `local_upload` 返回 `enabled`、`endpoint`、`method`、`content_type`、`confirmation_token`、`max_file_bytes`、`biz_types` 和 `callback_required`。接收队列未启用或处于 `receive-only` 时，模块明确提示不可提交。单文件上限沿用 `--oss-max-bytes` 的当前值，默认 1 GiB；解压大小、成员数、压缩比、路径、链接、加密及 CRC 校验继续生效。

提交接口以 `application/zip` 和准确 `Content-Length` 直接传文件字节，不使用 Base64 或 multipart。查询参数必填 `filename`、规范 `bizType`、24 位小写十六进制 `requestId`，可选 `largeVenueFee=true/false`（仅陈列堆头）和 `cvsOtcChannel=true/false`（仅条码费，只有true表示是，默认否）。测试输入还可显式携带 `verifyCode`（1～80位ASCII字母、数字或`._:-`）及 `analyzeId`（64位正整数）；省略保持空值，身份字段也参与同一上传请求的幂等校验，不开启回调。请求需当前工作台同源 `Origin` 和 `X-Offline-Audit-Upload-Token`，Token 取上述 `confirmation_token`。返回 `202`，包含 `job`、`duplicate`、`status_url`。同一 `requestId` 重试相同文件和类型返回同一任务，包括已结束的任务；变更文件、类型或显式身份字段返回 `409`，新上传使用新标识。其他常见错误为 `400`（类型、文件或 ZIP 校验失败）、`403`（同源或 Token 无效）、`408`（传输超时）、`413`（超过大小上限）、`503`（接收服务不可用）。

源码：`workbench_server.py:Handler._upload_archive`、`oss_intake.py:OSSIntakeService.submit_upload`、`local_upload.py` 及唯一 HTML 中的上传表单。维护重启前先等待页面上传接收完成，再确认队列空闲；本次隔离 HTTP 与浏览器回归不向真实模型、OSS 或业务回调发送测试资料。

### 8.2 业务 ZIP 接收与结果回调

| 环节 | 地址 / 配置 | 源码位置 |
| --- | --- | --- |
| 上游提交 | 本服务 `POST /api/intake/oss` | `workbench_server.py:Handler.do_POST`、`oss_intake.py:normalize_submission` |
| ZIP 下载 | 请求中的临时 `downloadUrl`；主机受 `OFFLINE_AUDIT_OSS_ALLOWED_HOSTS` 约束 | `oss_intake.py:download_oss_object` |
| 正式核销 | 唯一 bundled `scripts/run.py`，不在 HTTP handler 分析 | `oss_intake.py:run_formal_audit_subprocess` |
| 结果回调 | `OFFLINE_AUDIT_OSS_CALLBACK_URL` | `oss_intake.py:post_analysis_callback`；`callback_delivery.py` 提供已授权补发 |
| 回调正文 | 严格只有 `verifyCode`、`analyzeId`、`result` | `oss_intake.py:build_callback_result` |

提交 JSON：

```json
{
  "verifyCode": "客户业务核销单号",
  "analyzeId": 123,
  "bizType": "陈列堆头",
  "downloadUrl": "https://获准OSS主机/材料.zip?临时签名参数",
  "largeVenueFee": false
}
```

`analyzeId` 为正整数；不再接收旧 `fileId`，`planType` 不是 `bizType` 的别名。`largeVenueFee` 可省略，传入时只能是 JSON 布尔值。另可传 `cvsOtcChannel`，仅 JSON 布尔值 `true` 表示属于 CVS 或 OTC 渠道，其余值及未传均按否处理。另可传 `cvsOtcChannel`，仅 JSON 布尔值 `true` 表示属于 CVS 或 OTC 渠道，其余值及未传均按否处理。规范类型为：条码费、陈列堆头、人员激励、POS达标激励、搭赠、KT板等物料制作、补差、外采赠品。

`bizType` 兼容业务后台的四个明确名称：`堆头`、`陈列` 与 `陈列堆头` 均固定选择 `promotional_display` / `audit-promotional-display`；`物料制作` 与 `KT板等物料制作` 均固定选择 `poster_material` / `audit-poster-material`。`pos达标激励` 与 `POS达标激励` 均固定选择 `pos_target_incentive` / `audit-pos-target-incentive`。各自进入对应的既有资料门禁和审核流程。原始传值保存在回执和档案中，报告按规范类型展示。`GET /api/config` 的 `oss_intake.biz_type_aliases` 返回 `{"堆头":"陈列堆头","陈列":"陈列堆头","物料制作":"KT板等物料制作","pos达标激励":"POS达标激励"}`；实现位于 `audit_core/scenario_registry.py`。终态重提按新任务核销，保留旧记录；其他未明确支持的名称仍不做模糊匹配。

2026-11-03 POS名称兼容：用户确认 HX202605110019、HX202607010023、HX202608180051 的小写 `pos达标激励` 与规范 `POS达标激励` 执行相同审核。仅增加该完整原值的精确别名，保留八类场景、原始传值、现行版本及业务标准；其他大小写和空格变体不随之开放。原跳过结果保持只读，明确重新提交才创建新 attempt。本地修复与测试不等于服务器已发布，也不自动重提这三单。

2026-11-03 名称兼容增量：用户确认 HX202607010003、HX202609270009 属于 KT板等物料制作，接口实际传值为“物料制作”。本次只增加上述精确别名，规范类型仍为八类，资料要求、审核标准及 API 返回结构不变；系统 `2.16.13`、API `1.61`、规则 `2026-11-03.pdf.39` 保持不变。不改写已封存历史、不自动补跑或重发回调；本地验证不代表已经发布客户服务器。

其他非空 `bizType` 正常完成并返回“该业务类型无需AI核销”；缺失、null、空白类型失败并返回“无法识别核销类型”。资料问题正常完成并列出具体问题；执行或网络中断记失败。接收活动任务通常返回 202，已终态结果可返回 200；参数错误 400，活动身份冲突 409，接收未启用 503。

回调 JSON：

```json
{
  "verifyCode": "客户业务核销单号",
  "analyzeId": 123,
  "result": "## 陈列堆头\n\n1. 具体问题……"
}
```

结果持久保存在 `worktrees/<workspace_id>/ai-analysis-summary.md`。回调失败保留核销结果，重试只重发原结果，不重跑 AI。`verifyCode:analyzeId` 相同的活动任务合并，终态重交产生新 attempt 并保留旧档案。预签名 URL 下载验证后从队列丢弃，不持久保存。

模型失败处理：PPIO / 百炼每批资料最多调用 3 次（首次 + 2 次重试），间隔为 1 秒、2 秒。连接/响应超时（含 HTTP 408）、限流（429）、服务端错误（5xx）、流中断及 JSON / 证据校验失败使用该重试；认证、额度、请求配置错误直接失败，输入/输出容量不足继续使用现有拆分处理。各次调用的安全错误码、次数及已返回 Token 用量留存；不切换平台或回退其他模型。实现位于 `audit_core/qwen_model.py:extract_qwen_json`，次数沿用 `codex_runner.DEFAULT_MAX_ATTEMPTS=3`。

模型重试耗尽或正式核销执行失败后，仍回调原业务身份。正式运行先保存失败 manifest、snapshot、静态档案和 `ai-analysis-summary.md`，OSS 接收队列再持久保存失败收据并发送回调。失败 `result` 为“这次系统处理没有完成，暂时不能提供完整核销结果。请联系工作人员查看。”，不返回部分核销结论、模型原始报错或密钥；回调 JSON 仍严格为上述三个字段。回调成功只表示结果已送达，任务及核销档案仍为 `failed`。若正式运行未能创建档案，失败结果保存在 `.intake/jobs/<job_id>.json`，仍发送相同失败通知。实现位于 `oss_intake.py:OSSIntakeService._audit/_finish_job` 和 `workbench_store.py:WorkbenchRunStore.fail`。

结果回调单独最多尝试 3 次，按现有发送逻辑处理可重试网络/HTTP 错误；全部失败则保留原核销结果、失败原因和回调尝试数，不重新下载或重跑 AI。用户主动取消/删除任务、无上游身份的本地导入以及显式关闭回调的模式保持原行为，不触发失败通知。本机制不自动补发历史失败任务。

大表格的资料清单检查使用 `pdf_materials.py:_gate_documents` 提供的 `native_table_inventory`：程序全量读取每一来源的电子表格，保留全部列及去重后的原始字面值、原始来源 ID、行数和首末行位置。该视图只用于资料角色与适用条件盘点，不能把不同列的值组合成同一行，不能用于金额、日期或门店匹配核验；原始逐行证据仍完整交给后续业务审核，Schema 和全量来源校验继续使用原始证据。无法解析为标准原始单元格格式的来源使用完整原文，图片与扫描件事实保持原样。这样避免在仅盘点资料种类时反复输入大量重复销售行；实际 Token 用量以模型返回计量为准。

业务类型确定即记录 `scenarios/scenario_count`。旧运行若 `bizType` 与 `selected_scenario` 一致、但运行中或失败档案尚未写入 `scenarios`，工作台通过 `workbench_store.py:project_run_status` 只读展示已确定的类型，不改历史文件、不把失败改成完成。

### 8.3 商品参考图 OSS 接口

这套接口与业务 ZIP 的 `downloadUrl` 独立。

| 项目 | 位置 / 值 |
| --- | --- |
| SDK 适配器 | `scripts/audit_core/product_oss.py:ProductOSS`，上述路径相对编排 Skill |
| 业务按需取图 | `product_image_runtime.py`、`product_images.py`、`photo_product_references.py` |
| 数据源配置 | `shared/product-database/product-images.json` |
| OSS Endpoint | `https://oss-cn-shenzhen.aliyuncs.com` |
| Bucket | `xiaokuo-dingding-xianxiahexiao` |
| 固定 Object 前缀 | `offline-verify/product-reference/` |
| 图片关联 | RDS `products.image_manifest_key`，按准确 `product_code` 定位清单 |
| 认证 | `config/secrets/product-oss.json`，SDK 签名认证；只读对象权限 |

仅按本次涉及商品读取清单和细节图，不下载整个图片库，不回退旧本地图库。Bucket、Endpoint、前缀同时受代码和配置约束，迁移存储范围需要成套修改和验证，不能只改 AccessKey 或伪造环境变量。

商品参考图使用跨核销字节缓存（`product_image_cache.py`）：每次核销仍从数据库确定商品，并重新读取、核验所选商品的最新 OSS 清单；图片 SHA-256 未变且缓存的完整字节、大小、尺寸、类型均符合本次清单时，直接复用，否则重新下载。清单、身份或认证读取失败不会使用旧图继续审核；同一任务固定清单快照，下载期间发现快照变化仍明确停止。缓存目录为本部署的 `worktrees/.cache/product-images-v1/<OSS来源哈希>/`，自动启用，无需新增配置，沿用服务已有的 worktrees 写权限；不同工作目录和 OSS 来源各自隔离，不迁移整个图库。

缓存图片上限 2 GiB，按最近使用淘汰；线程及 Windows/Linux 进程共用排他锁，校验完成后原子发布，崩溃半成品在下次访问时回收。缓存不可写时仍可按原流程下载并校验本次参考图。模型只获得独立任务副本，任务结束仍清理；缓存不保存现场照片、商品身份、清单快照或 AI 结论，模型无法直接访问缓存目录。技术证据 `oss-reference-*` 的 `cache_status` 区分 `persistent_hit`、`task_hit`、`downloaded`、`downloaded_uncached`；这些状态不计作模型缓存 Token。复用图片只减少 OSS 下载及传输，再次提交 AI 分析的图片与文字仍按模型实际计量。

### 8.4 模型接口

| 平台 | Base URL | 实际请求 | 默认模型 |
| --- | --- | --- | --- |
| PPIO（默认） | `https://api.ppio.com/openai/v1` | `POST /chat/completions` | `qwen/qwen3.8-max` |
| 阿里云百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `POST /chat/completions` | `qwen3.8-max` |
| DeepSeek 官方 | `https://api.deepseek.com` | `POST /chat/completions` | `deepseek-flash` |
| 紫猫云（AI Club） | `https://aiclub.zimaocloud.com/model/openApi/v1` | `POST /chat/completions` | `deepseek-v4-1-flash`、`doubao-seed-2-1-pro-260915`、`qwen3.8-max` |

源码为 `scripts/audit_core/qwen_model.py:_complete_json`、`model_settings.py:API_PROVIDERS/ModelSettingsStore`、`pdf_materials.py:PdfEvidenceProvider`。请求使用对应平台的 `Authorization: Bearer <API Key>`，包含图片、`response_format=json_object` 和流式返回。程序校验返回结构与原文依据后才生成业务结果。

2026-11-17 新增紫猫云豆包 Seed 2.1 Pro 和千问 3.8 Max 兼容，不修改当前 DeepSeek 选择或既有任务。两个型号继续使用紫猫云独立 Key、固定 HTTPS 接口与任务快照，不路由到火山方舟、百炼或派欧云；其他平台原凭据保留。页面在“更换模型→紫猫云”显示接入说明，模型下拉仍只列当前 Key 在 24 小时内通过图片与 JSON 实测的型号，目录存在或隔离测试通过不能代替实测。检查可用模型和测试返回会产生费用，接入代码本身、打开页面、保存切换均不自动发起收费测试；检查/测试不改变当前模型，切换只影响后续新任务。

`GET /api/model-settings` 的 `profiles.zimaocloud.compatible_model_ids` 是后端已加载的协议兼容清单，不是可用性记录；前端只在后端明确返回两个新型号时显示接入说明，避免新页面配旧服务时误称已生效。源码更新不等于已运行进程重载；本机存在活动下载、排队或核销时不重启，待队列清空后通过统一安全重启入口加载。此接入不发布服务器、不修改正式配置或重审历史。

新型号沿用工作台 `default/none/low/high/max` 档位：`default` 不强制覆盖原生默认思考；`none` 使用各自关闭思考参数；豆包 `max` 映射为原生最高档 `high`，千问 `high/max` 映射为原生 `xhigh`，其余档位保留。该映射是同一型号的协议适配，不是回退模型，配置快照保留工作台所选档位，传输指标另存 `configured_reasoning_effort` 和 `request_reasoning_effort`。豆包只使用 `thinking.type`，千问使用 `enable_thinking` 并显式关闭 `preserve_thinking`，可见会话照常保留，不保存或回传私有推理。两者的合成检查保留所选思考档位、45 秒总时限及既有传输/输出安全保护，不再沿用 2048 Token 小预算；业务请求不新增 Token 硬上限。参数依据：[豆包官方参数表](https://docs.volcengine.com/docs/ark/model-parameter-support)、[千问官方 Chat API](https://docs.qwencloud.com/api-reference/chat/openai-chat)。紫猫云实际透传和模型效果仍须通过当前 Key 的显式实测确认；不以本地替身结果宣称真实图片识别已验收。

在工作台模型设置中选择“紫猫云（AI Club）”，填写该平台 Key 后检查可用模型；只有当前 Key 的图片识别及 JSON 测试通过且未过期时，才可切换为 DeepSeek V4.1 Flash。其实际 ID 为 `deepseek-v4-1-flash`，与 PPIO 的 `deepseek/deepseek-v4.1-flash`、DeepSeek 官方的 `deepseek-flash` 分别映射，不跨平台复用凭据。紫猫云未设置推理强度时使用平台默认参数；后端私有模型 profile 可保存 `reasoning_effort`（`none`、`low`、`high`、`max`，紫猫云另支持 `default`），DeepSeek 官方未设置时仍为 `high`。强度随新任务快照传递至请求，保存配置或子进程恢复不会丢失；在途任务保留原快照。`GET /api/model-settings` 在当前配置及各 profile 返回脱敏的 `reasoning_effort`，页面不新增设置入口；逐图事实转录和后续商品提取、业务审核、回看、自测、逻辑核验及总结均使用本单实际选择的强度；选择 `max` 时全程 `max`，不再以转录为由降到 `none`。事实缓存及恢复身份包含实际强度，旧 `none` 结果不能冒充 `max`。保留实际返回的输入、输出及缓存 Token。私有环境配置可使用 `ZIMAO_API_KEY` 或 `ZIMAO_API_KEY_FILE`；不写入源码或前端。模型设置接口、同源校验、24 小时可用性记录及新任务快照沿用现有流程。

人工核验的识别纠错案例集中在 [识别纠错案例库](skills/orchestrate-offline-audit/references/recognition-badcases/README.md)：原件按SHA256保存，当前包括水印时分、2025/2026年份误读、手写抬头和红章叠印/旋转。每次读图前提供已确认的文字纠错对和读取要点，版本参与缓存；不额外发学习请求，也不宣称已训练模型权重或发送案例原图。当前核销事实仍由当前原图决定。日期与星期冲突或章名字形疑点触发模型定向回看建议。被请求的原图先独立重新读取，不带本单旧转录与既有判断；新事实再交回原Agent修正对应项，保留其他结果。不能把在重复旧错误的会话中再次附图等同于独立复核。仍是每请求最多一张原图、同一模型及所选强度，不新增公司名称硬拦截。

POS商品提取兼容模型实际返回的“表头、合计、非商品备注”等中文标签，保留全部商品原行后继续准确69码查库。提取格式异常记录具体原因及尚未查库状态，金额Agent可用 `pos_product_lookup` 传本单Excel原行的逐字字段引用定向补查；补查只证明请求的行，不冒充全表覆盖。数据库故障、尚未执行与实际库内无记录分开，不把内部执行限制列为客户材料问题。测试使用隔离模型与数据库替身；历史记录保持，实际模型效果另行验证。

资料盘点引用原始Excel文件时，按本单明确的 `file_id` 对应到全部工作表及分块，不按文件名或编号前缀猜配。POS标记为 `unclear`（例如缺少单价）时，已有名称和69码仍独立进入商品查询；资料状态与金额字段缺失继续保留。审核、自检的工具请求引用本单来源目录中尚未分发的原表时，先交付完整原文再执行；其他核销单和未知来源仍不可读取。`case_python` 只分析本单原文，商品命中必须来自实际商品库查询。

2026-11-19起，核销管理中心新增“模型目录”，导航放在“核销时间表”上方，覆盖旧版不提供设置入口的说明。服务器默认为 `deepseek-v4-pro`、`max` 和图片路由 `qwen3.8-max`、`xhigh`。DeepSeek主路由兼容现有官方连接或AI Club连接；本地两路均使用本地已保存的AI Club接口与各自密钥，发布不替换本地或服务器配置。两组均从各自账号目录读取实际开放型号，DeepSeek组显示DeepSeek型号，Qwen组仅显示已确认支持图片理解的Qwen型号；文本专用、语音、向量、生成图片和缺少图片能力依据的型号隐藏。Qwen能力依据官方图片/全模态文档及目录中明确的输入模态，不能仅按Qwen名称或泛用“文本”标签判断。推理选项使用官方标记：DeepSeek V4为 `none / low / high / max`，Qwen3.8为 `none / low / medium / xhigh`，另可不指定档位；早期混合思考型号提供官方思考开关，Qwen3.5-Omni仅提供模型默认。不重复展示实际相同的别名，已保存Qwen的 `max/high`在界面显示为`xhigh`，读取页面不改写配置。切换型号先检查文本/图片及JSON能力，通过后两组配置一次保存；只调强度不调用模型探测。恢复默认先填入本账号实际存在的默认选项，点击保存才生效。旧任务、已完成结果及各账号独立密钥保持原有归属。

真实核销的模型读取等待初始为 300 秒，读取超时后在后续两次重试增加到 600、900 秒；发送等待初始为 120 秒，只有发送超时才增加到 240、480 秒。连接及连接池等待仍为最多 30 秒。每次调用的总时限继续受 `OFFLINE_AUDIT_MODEL_TIMEOUT_SECONDS` 控制（默认 3600 秒），各段等待不会超过该次调用总时限；45 秒的模型连接测试继续受其自身 45 秒时限约束。重试保持同一平台、模型和完整证据，不丢弃跨资料核对所需的图片或事实，最多三次，失败不生成核销结论。

业务审核的输出 Schema 会按本次门禁已确认的资料来源，限定既有合同参照比较的右侧来源，以及单价比较的结算单左侧来源；最终业务校验仍完整执行。合同没有可引用的约定原值时，可以保留 `unknown` 和空比较，不能用结算单自证、反算数量或绕过来源检查。证据失败会分别记录 JSON、Schema、来源证据三个阶段，以及受控原因代码和字段位置；不记录原始模型正文、任意异常正文或思考内容。重试次数、模型和完整材料范围不变。

模型计量保留具体超时阶段（连接、发送、读取、连接池、总时限或上游返回超时）、实际读写等待上限、是否已收到响应头、HTTP 状态及格式受限的请求编号。首次模型事件前的等待与流式输出中断分别记录；不保存认证头、Key、提供方原始错误正文、模型私有推理或业务提示正文。服务器源码同步仍按确认清单执行，不覆盖回调地址、运行配置或历史档案，也不自动重跑已失败业务。

工作台“更换模型”只显示模型选择与 API Key，连接和地址由服务端自动匹配；可先测试图片与 JSON，再启用。配置只影响新任务，正在执行的任务使用启动时快照。PPIO 使用模型默认推理模式，百炼执行非思考 JSON 模式；不自动回退 GPT，不跨连接复用密钥。

### 8.5 RDS 调用

- RDS：`product_database.py:_read_network_database_rows`，PyMySQL 直连，TLS、只读账号、只读一致性事务；每次核销固定本次商品快照，不使用历史 JSON 补值。
- `location_resolution.py` 中的百度地图 MCP 只属于历史流程，不在当前 `PdfWorkflow` 调用链中，不作为客户上线条件。

### 系统总览的完成时间预估

系统 2.19.1 / API 1.67：系统总览的正在核销卡片和台账记录只展示预计完成时间与单一剩余分钟数，每 15 秒更新预估；不展示历史参考范围、参考单数或统计说明。`GET /api/completion-estimates` 为只读接口，按运行目录读取轻量清单、资料盘点和执行事件，不读取完整审核快照、不产生模型请求，也不写入任务或历史结果。

系统总览统一展示核销记录，不再按本地导入、系统上传区分。文字搜索支持资料包名、核销单号、记录名称和核销方式，与日期和核销方式筛选取交集。平均 Token 消耗和平均耗时使用当前筛选后的已完成记录分别计算，各移除一个最大值和一个最小值；有效样本不足3条显示样本不足，缺失值不作为0。Token只使用完整的真实用量，耗时扣除有记录的中断停等；数值不带“/单”。

结果页按“提交材料、错误总览、正确检查项、技术档案、AI总结”排列。提交材料来自本单已保存的 `analysis/input-cases.json`，按原ZIP列出实际文件和嵌入附件来源；历史缺清单时如实说明。技术档案在步骤收起时也显示已存查询条件、实际返回数量、准确对应的读取文件及阶段自测任务和判断数量；不补造历史步骤或核对通过结论。

`GET /api/runs/{workspace_id}/materials/preview?file_id={本单材料编号}` 提供只读原件预览，材料、错误项和正确项的文件名共用此入口。只从本环境保留的原ZIP读取，依据本单清单及SHA-256核对来源，不接受任意文件路径。可选 `page` 为从1开始的PDF页码或表格分页，`sheet` 为从0开始的工作表编号，`download=1` 下载原字节；不支持的参数拒绝。图片直接查看，PDF在本地渲染原页，表格每页100行并显示工作表、总行数与列数上限，Word提供段落文字预览；不支持的格式可下载原件。预览不调用AI、勤策或业务回调，不执行表格公式或文档脚本，不消耗额外Token，不改写本单资料。无法唯一对应或缺失原件时明确提示，不借用其他核销单。

预估使用普通统计：至少有 3 单同平台、同模型、同推理配置的连续完成记录后，优先同核销类型，再按资料文件数量选择相近样本，最多参考 12 单。优先对齐最近已完成步骤及其发生次数，无法对齐时使用整单耗时；剔除偏离中位数超过 3 倍中位绝对偏差的极端长短耗时，再对保留样本取算术平均值、减去当前对应已运行时间。中位数只用于识别极端值，不作为预估值；不会随着已运行时间增长逐个丢掉快样本、只保留最慢样本。失败、演示、续跑/重新核验记录和时间不完整记录不作为整单正常耗时样本；当前单续跑只扣除有真实起止记录的停等时间。排队、结果发送、样本不足及超出历史平均耗时分别显示实际提示，不虚构倒计时；完成或失败后撤下预估。

运行目录在开跑时登记 `run.pid`，正常结束即删除；工作台启动及读取总览时会把「状态仍是 running 但没有进程接管」的运行按失败封存（原因写明核销进程已不在、需重新提交资料整单重跑），避免界面长期显示正在核销和累计中。旧运行目录没有进程登记文件，仅在超过 15 分钟无任何写入时按同样口径封存。每次新核销完成或失败时，`manifest.audit_timing` 和快照保存实际开始/结束时间、总经过秒数、扣除停等后的实际运行秒数及停等秒数；续跑保留前次计时并在最终完成时重新汇总。新完成的正常记录自动纳入后续平均值，不调用 AI 优化，也不改写既有历史；旧记录沿用已保存的真实时间戳计算。

### 执行 Agent 现场修复与待修复清单

系统 2.18.0 / API 1.64：业务证据校验遇到程序识别兼容性错误时，执行 Agent 接收真实报错和识别逻辑，使用本单工具补查、纠正原始字段绑定并重新提交完整审核答案。临时绑定只作用于当前 run 和完整来源指纹；唯一结算费用的数量/单价或人数/天数/工资、合计及独立核销额均须有原文依据，来源、额外费用、多表、缺行与金额原精度校验保留。

2026-11-11 本机专用源码自修复：仅此Windows电脑固定项目的8080服务所启动任务，在私有机器授权及运行身份全部匹配时开放`project_source`。本地优先修改已有确定性辅助函数，经隔离回归验证后更新真实源码及当前进程函数，立即复验原步骤，通用修复供后续本地任务复用；临时工具和字段绑定继续可用。正式服务器不具备此源码写权限，发生兼容性异常时只使用本单临时逻辑自主处理。授权文件位于本机用户私有目录，不随项目打包；不因URL、Host或普通开关而开启。修改保留前后源码、测试日志及本单轨迹，不开放业务标准、配置、凭据、宿主命令、外发或其他单据权限。详见[本地源码自修复与正式临时恢复](docs/技术档案/本地源码自修复与正式临时恢复-20261111.md)。

2026-11-11 单笔核销连续会话：同一个执行AI贯穿资料读取、商品身份、工具、审核、自查与总结，各轮请求保留本单可见历史。当前阶段的任务及Schema单独给出，沿用已有事实并可纠正前序判断；不同核销单及模型快照隔离。同一API Key仅用于鉴权。会话保存在本单 `analysis/conversation/`，含稳定会话ID、逐轮可见输入/回答及完整性链，恢复时沿用同一会话；旧运行可从自身真实调用轨迹恢复。历史图片仅保留来源指纹及识别结果，确需回看再发送，不保存私有推理、凭据或重复图片字节。该改动不自动续跑或重写封存历史。

后台“待修复清单”只列需要项目级通用修改或兼容的程序问题，每张单据也可打开本单清单。Agent 确认共用程序需要修改后，才记录问题、触发条件与来源、原始报错、代码位置和通用修改建议；仅纠正本次回答、工具使用或临时服务问题时不建清单项（`developer_fix=null`）。清单不区分当次恢复状态，不重复保存当次处理过程；核销所需的临时绑定与断点数据沿用原执行记录。`GET /api/repair-items` 可按 `workspace_id` 筛选；项目修复项位于本单 `analysis/repair-items/`。技术人员完成通用修改和回归后，通过 `POST /api/runs/{workspace_id}/repair-items/{id}/resolve` 提交 `{"note":"修复内容及回归结果"}` 标记已修复，随后移出待修复列表；此操作不改核销结论或补发回调。

修复答案仍通过原阶段完整校验，不以删除比较、降为 unknown 或默认通过来掩盖系统问题。真实业务差异仍按原规则输出。现场恢复只有验证通过才记成功；未通过的尝试和原始证据完整留存，不把未验证结果封为完成。历史已完成核销不自动重审，清单不补造未发生的 Agent 操作。

## 9. 数据保护、备份与升级

| 数据 | 处理要求 |
| --- | --- |
| RDS 商品主数据 | 使用 RDS 自动备份、快照或受控 SQL；升级应用不自动覆盖商品表 |
| `input/` | 保留原始 ZIP，按任务目录保存，不展平、不以“垃圾”删除 |
| `worktrees/` | 保留 manifest、snapshot、HTML、摘要、事件、分析及检查点 |
| `worktrees/.intake/` | 任务及回调回执，必须连同历史迁移 |
| `worktrees/.reviews/` | 人工核验注释，必须迁移 |
| `config/`、`worktrees/.settings/` | 私有配置和模型信息，单独受限备份；目标机重新配置 Windows 密钥 |
| `worktrees/.cache/product-images-v1/` | 可重建的商品参考图字节缓存，不作为核销证据、不进入源码包或业务历史迁移清单；新机器按需重新下载 |
| 缓存、临时解压和测试截图 | 不进入客户运行数据包；临时清理使用项目维护入口 |

升级或搬迁时先停止上游新提交，执行：

```bash
sudo -u offline-audit .venv/bin/python -B deploy/healthcheck.py --idle
sudo systemctl stop offline-activity-audit
# 在维护窗口备份完整 input/、worktrees/（包括隐藏目录）和私有配置；同时记录 RDS 备份点。
# 替换经过校验的源码，保留运行目录和配置，再安装依赖、检查并启动。
sudo systemctl start offline-activity-audit
```

有下载、排队、核销或回调任务时不停止服务。`completed=0` 列表也包含失败项，不能仅按列表条数判断是否忙碌。历史快照中的源路径和业务事实不批量替换；新客户空运行目录安装不自动导入源端业务历史。

回滚应用时恢复前一版本源码和对应虚拟环境；本次部署不修改业务规则和旧核销档案，不需要通过清空 RDS 回滚。数据库恢复由 DBA 按目标库执行，禁止对共享实例进行覆盖恢复。

参半现有 `/data/offline-activity-audit` 安装的 Token 统计与模型选择专项发布可使用 `deploy/update_source.py --manifest <冻结清单> --archive <源码包>` 预检，追加 `--apply` 才实际更新。清单必须包含发布前后 SHA-256、归档 SHA-256 和唯一发布编号，文件范围固定为七个相关 Python 模块及唯一前端 HTML；资料上传功能另行维护。该入口核对原生服务身份、排他锁和活动任务，短暂停止该应用端口接收新请求，备份后原子替换源码，再恢复服务、核验线上 API 和原有模型配置，最后移除临时接收限制。失败只回滚本次代码和服务，不覆盖 `input/`、`worktrees/`、私有配置或 RDS。生产连通检查应以 systemd 配置的 `offline-audit` 用户执行。

## 10. 验收与排错

```bash
.venv/bin/python -B skills/orchestrate-offline-audit/scripts/verify_project.py -v
.venv/bin/python -B -m compileall -q skills tests shared/product-database deploy
.venv/bin/python -B deploy/material_reader_preflight.py
.venv/bin/python -B deploy/healthcheck.py
.venv/bin/python -B deploy/healthcheck.py --production
```

验收至少覆盖：RDS 字段与只读权限、工作台及历史读取、1440×960 / 1280px 页面、独立样本的真实模型与商品取图、OSS 下载、持久档案、三字段回调、回调失败重试和服务重启。真实业务联调须使用约定的测试单号和测试回调，不向客户生产单据发送测试结果。

| 现象 | 检查位置 |
| --- | --- |
| RDS 连接超时 | RDS 内网地址、ECS/VPC 路由、白名单、安全组、端口 |
| RDS TLS 失败 | `config/rds-ca.pem`、CA 有效期、RDS 连接域名与证书匹配 |
| RDS 访问被拒绝 | 应用只读账号、密码文件归属和 0600 权限 |
| 0 条商品或字段校验失败 | 是否恢复了 SQL；与 `product-catalog.manifest.json` 对比，不能跳过校验 |
| 接收接口 503 | `OFFLINE_AUDIT_OSS_ENABLED` 是否启用及白名单 / 回调配置是否完整 |
| 商品图片失败 | `product_oss.py`、只读 AccessKey、固定前缀、RDS 图片关联及对象完整性 |
| 模型 401/403 | 平台与 Key 是否匹配；PPIO 与百炼不可混用 |
| 回调失败 | `OFFLINE_AUDIT_OSS_CALLBACK_URL`、网络、HTTP 显式许可、任务回执；不重跑 AI |
| `.xls` 图片不能对应行号 | `legacy_activity_workbook.py`、LibreOffice 及原工作簿图片关系；不据缺失映射猜测 |
| 中文 CLI 输出异常 | 使用项目 `run.py` / `serve.py` 入口及 UTF-8 环境 |

目标客户的 RDS、OSS、模型和回调联调结果由部署人员现场记录。源码回归通过不等于已经连接客户生产环境。

## 宝塔独立核销环境

宝塔与小阔 RDS 是两个独立环境。宝塔复用现有 MySQL 8.0.45，专用商品库为 `product_catalog`，表结构和四个业务字段从本机实际数据库复制；应用只读账号为 `offline_product_viewer`，仅允许同机连接并要求 TLS。导入须先核对目标空表，再在事务内验证全部商品字段的 SHA-256；不能覆盖其他项目的数据库或把空表当作交付完成。

商品参考图继续读取原私有 OSS 的 `offline-verify/product-reference/`，只按本次核销实际商品取图，不将整个桶或商品图库复制到宝塔磁盘。业务 ZIP 接收的下载白名单和 HTTPS 回调沿用本机当前配置，与商品参考图凭据分别配置。模型沿用本机当前平台和型号，平台密钥通过私有文件导入，Windows DPAPI 密文不直接迁移。

服务器使用独立状态目录 `/srv/state/offline/`，源码位于 `/srv/apps/offline-activity-audit/`（均为隔离 Linux 用户态内路径）。`fde-offline.service` 通过 `deploy/service.py` 加载私有 `config/application.env`，内部监听 `127.0.0.1:18080`；页面使用案例中心既有受保护入口 `/ai/apps/offline/`。本机 8080 继续独立运行，原始 ZIP、核销档案、回执和人工核验不随源码对齐复制。

数据库 CA 位于 `config/mysql-ca.pem`，只读密码、商品 OSS 与模型 Key 位于 `config/secrets/`，归属为服务用户且权限为 0600。宝塔 MySQL 自动生成的服务器证书通过隔离环境 `/etc/hosts` 中的同机名称解析匹配；`OFFLINE_AUDIT_PRODUCT_DB_HOST` 必须与证书名称一致，保留 CA、主机名验证及实际 TLS 协商检查。证书轮换时同步核对名称及 CA，不通过关闭验证绕过错误。

切换正式模式前检查无下载、核销和回调活动任务，仅调整核销服务的专用 systemd drop-in。保留旧演示配置作为恢复依据，正式配置显式设置 `OFFLINE_AUDIT_DEMO=0`、开启 OSS 接收，且不跳过核销或回调。验收覆盖真实数据库只读权限、逐字段一致性、OSS 清单及对象信息、页面上传与模型配置入口；未执行真实模型任务或生产回调时须明确记录该边界。
# 发布兼容性说明

Windows 模型临时目录的权限预检兼容 Python 3.11：缺少 `Path.is_junction` 时仍检查 Windows 重解析点属性并拒绝链接目录，不放宽临时目录范围或同用户只读权限边界。Linux 发布候选运行同一套隔离回归用例，不修改服务器源码来绕过验证。

Windows 本地核销使用默认 `%LOCALAPPDATA%/Temp` 时，受管理的图片工作区改放在项目的 `.runtime/temporary/`，避免系统盘空间不足中断原图处理。目录继续使用原有归属登记、占用锁和终态清理；明确指定的临时根目录、自定义系统临时目录及 Linux 默认路径不变。正在运行的进程保留原路径，后续启动的任务使用新路径；原 ZIP、商品图片缓存和核销历史均不属于可清理的临时数据。
# 本次业务明细补充（2026-11-03）

系统 `2.16.17`，API `1.62`，规则版本 `2026-11-04.pdf.44`。
勤策补充只服务既有审核项，不新增客户必填字段、审批条件或资料要求；未配置时保持原流程。

原 `POST /api/intake/oss` 请求字段不变。服务使用已有 `verifyCode`，通过
`queryVerificationRegular` 的 `details.activity_code`、`details.activity_cm_id`
关联 `queryRegularSaleActivities` 的 `applyCode`、`subList.subId`。
只向模型提供本次核销关联、且属于现行审核所需的业务字段；资金账户、私人联系方式和无关申请行不进入模型来源。
申请行自定义列“活动SKU”“陈列、堆头类型”也按已确认的 `ext_2412110257`、`ext_2412110258` 识别，保留原始标签及所属申请行；其他列继续按现行费用规则的业务白名单筛选，不猜测未确认Key。
申请行 `pdRangeType=0` 时按 `planCode` 查询 `queryRegularSale`，补充继承的商品范围。
客户不自动视作终端，参与客户数不能替代逐店名单；不按文件名推断活动归属。

服务器可配置 `OFFLINE_AUDIT_QINCE_OPENID`、`OFFLINE_AUDIT_QINCE_APPKEY_FILE`（私有 Appkey 文件）。
也可在本地私有 `config/qince.json` 填写 `host`、`openid`、`appkey`，或用
`OFFLINE_AUDIT_QINCE_CREDENTIALS_FILE` 指定同格式私有文件；不会输出密钥。
也支持同名 `OPENID_FILE`、直接环境变量 `OFFLINE_AUDIT_QINCE_APPKEY`。
企业接口地址也可用 `OFFLINE_AUDIT_QINCE_HOST` 指定；未指定时使用官方域名。
Linux 私密文件须 0600。凭据只由程序用于官方 v2 SHA-256 签名，不送入模型或技术档案。
使用下表中的只读 HTTPS 查询接口；没有定时任务，不调用写入、审批或新的计次识别接口。
配置了查询但请求失败时记技术错误，不伪装成客户缺附件。

本地显式运行可增加 `--verify-code HX...`，沿用工作台实际模型配置。
已有查询返回值可通过 `--qince-data <JSON文件>` 离线传入，原有顶层键为三个接口方法名：
`queryVerificationRegular`、`queryRegularSaleActivities`、`queryRegularSale`，值为各自返回的明细数组
或官方 `return_code/response_data` 响应对象；后者支持 `response_data` 为 JSON 字符串。
本地测试文件和运行目录应位于 `artifacts/`；真实明细不得提交 Git，不从历史结果拼造接口返回。
程序只保存筛选后的独立来源，不改原 ZIP、照片事实、资料门禁或历史报告，不发送业务回调。

实现：`audit_core/qince_context.py`、`audit_core/qince_references.py`；入口：`workbench_runtime.py`、`oss_intake.py`；
来源接入：`pdf_workflow.py`、`pdf_materials.py`。
官方格式规范：https://api.qince.com/SERVER/sys/api_introduction_v2.html 。

## 勤策终端与陈列资料接入（2026-11-06）

根据用户指定的《勤策终端门店资料字段需求及核销应用说明》当前字段清单，在上述关联链之后补充终端主档、商品包装与尺寸、已有识别和校对、铺货资料。所有接口沿用现有企业 `host/openid/appkey` 及 v2 签名，方法均为 POST。下面的键也可用于 `--qince-data`；终端和商品V2使用本项目别名，避免与V1混淆。

| 数据键 | 接口路径 | 查询及使用范围 |
| --- | --- | --- |
| `queryCustomerV2` | `/api/v2/customer/query` | 按本单申请客户 `cusId/cusCode` 查询；提供 `sorts/query_group`，结果同时核对ID和编码。 |
| `queryCustomer` | `/api/customer/v1/queryCustomer` | 兼容离线V1数据；保留V1来源和状态原值，在线优先V2，不自动混用审批字典。 |
| `queryProductV2` | `/api/pd/v2/queryProduct` | 按已关联SKU编码或勤策商品ID读取，保留条码、规格、图片地址及各包装单位与换算比。 |
| `queryAiSkuCode` | `/api/ai/v1/queryAiSkuCode` | 查询已有识别码，筛选本单商品；长宽高原值单位厘米，绑定商品与单位。 |
| `queryPrimitiveAiSkuReport` | `/api/ai/v2/queryPrimitiveAiSkuReport` | 按申请明确起止日期查询，再筛本单客户编码；仅作为候选，不能凭同店同期认定对应本图。 |
| `queryPrimitiveAiSkuDetail` | `/api/ai/v2/queryPrimitiveAiSkuDetail` | 按候选bizId与日期查询全部业务照片及商品明细。 |
| `queryCheckedAiSkuReport` | `/api/ai/v2/queryCheckedAiSkuReport` | 保留同一bizId的校对评估、审批与修改版本，不覆盖原始结果。 |
| `queryCheckedAiSkuDetail` | `/api/ai/v2/queryCheckedAiSkuDetail` | 按bizId及日期查询，保留商品修改类型、数量、层号及审批时间。 |
| `queryAiOriginalSku` | `/api/ai/v1/queryAiOriginalSku` | 按时间分页，筛选本单候选业务关联的imageId，保留逐图商品数量。 |
| `queryAiBizPicRel` | `/api/ai/v1/queryAiBizPicRel` | 保留bizId与全部imageId的一对多关系，不把业务总数分摊成逐图数。 |
| `queryAiSkuCompare` | `/api/ai/v1/queryAiSkuCompare` | 按bizId及时间查询，保留原排面、校对排面和变更类型。 |
| `queryNewSku` | `/api/new_sku/v1/queryNewSku` | 按实际铺货对比记录的setting_id筛选标准master_id，保留客户范围和有效期。 |
| `queryCompareResults` | `/api/compare_results/v1/queryCompareResults` | 查询本单期间并筛选本单客户；通过report_id关联实际铺货主表，不使用错误的new_sku前缀。 |
| `queryActualProductsV2` | `/api/new_sku_products/v1/queryActualProductsV2` | 按客户编码及期间查询，筛选本单商品，保留数量、排面、照片、多个biz_id及修改标记。 |

终端主档中的名称、详细地址、行政区、道路门牌、类型、上级客户、销售区域、状态、组织及收货关系作为独立参照进入现有八类核销。当前主档不能证明历史时点的门店状态；收货地址不能自动当现场地址，客户也不自动等于执行终端。联系人、自定义字段和商品扩展值保留在真实逐页技术记录，未确认业务Key不自动解释为别名、GPS或审核规则。Word中“待回填”“读取路径待确认”的项目继续待映射，未接入未确定的终端检核动态字段，不把维护接口入参冒充固定查询出参。

照片来源在 `pdf_workflow.py` 的原执行链中接入。仅在线取回的原图实际字节 SHA-256 与本次照片一致时，识别/校对及关联铺货记录才成为该照片的独立复核参照，并保存申请行、bizId、imageId、unit_id及两版原值。业务级数量、层号、件数、排面、地堆算法值分别保留；纵列、非标准摆放和面积继续结合本次原图与本单批准标准核对，已有合格结论不能替代本次审核。铺货规则仍须核对客户范围及有效期，并不自动成为本次合同要求。

原图读取仅允许HTTPS同企业API主机（支持该主机的图片服务端口）、勤策图片域名或 `OFFLINE_AUDIT_QINCE_IMAGE_HOSTS` 明确列出的额外主机。额外主机使用逗号分隔的主机名，仅443端口；禁止URL凭据、IP地址和重定向。每单最多200个原图地址，每图16MiB，缓存仅含本次内存指纹。不下载到业务资料目录，也不启动模型或新识别。离线JSON不会主动联网获取图片；无法核验时保留原始资料审核，不按名称关联。

每页请求参数、原始返回和筛选/关联状态进入原有技术档案；长ID无损保存为字符串，响应兼容DATA数组与`{total,rows}`对象。分页重复、总数变化、格式错误与权限失败分别留痕；新增参照接口不可用时继续使用本次实际资料，不新增客户缺件或审批门禁。核销/申请主链失败仍按原技术错误处理。公开接收API、核销规则版本及历史档案不因本项改变。

## 核销时间表与自定义时段

工作台左侧“核销时间表”展示当前执行方式、是否允许开始、下次时段开放、每周时间条和等待单据。
页面可选择官方错峰、自定义时段或全天核销，调整自动保存后用于等待中的单据及新单；
已经开始的核销持续执行到本单结束。本地资料上传和系统投递使用同一份计划。

自定义按北京时间逐日设置，每日支持多个时段；跨夜分别设置本日末尾和次日开头。
顶部只有一个“新增时段”取用按钮，把它拖到目标日期和时间位置即可新增；也可先点按钮再点日期行。
点击时间条出现删除“×”，单击即可删除；点击其他位置收起删除按钮。每行不再设置加号，也不提供复制粘贴按钮。
删除仅移除当前时间条，立即反馈并后台保存，其他日期和时间条不重建；删除浮层优先显示，扩大点击范围。
后台轮询不会覆盖正在调整的时间。实际开始时间还取决于前方队列及可用 worker。

当前交互为可左右拖动的开放时段条：拖动两端调整起止，拖动中间移动整段，接近整点自动对准并显示对齐线，松开后吸附 5 分钟；
方向键调整 5 分钟、Shift+方向键调整 1 分钟。时间条使用蓝色，窄时段（如 12:00–14:00）也完整显示时间；
背景以 DeepSeek 空闲/高峰着色，在北京时间 09:00、12:00、14:00、18:00 绘制辅助线。
“按空闲时段核销（默认）”和“全天即时核销”两个快捷按钮点击即生效；拖动松开、添加、删除、
逐日开关会自动合并保存，无额外保存按钮。时间表随页面宽度缩放，不显示横向滚动条。慢请求期间继续编辑不会丢失后续修改；
两个模式按钮允许连续来回切换，点击立即更新选中态和时间条，后台按 revision 串行保存最后一次选择；保存及队列刷新期间均不禁用按钮。后台成功回读后才提示“已生效”，旧响应不覆盖更新的选择，队列刷新不等待完整核销台账。
保存失败保留当前调整并明确提示重试，版本冲突不会覆盖另一页面的设置。
修改后立即重新判定所有尚未开始的系统投递及资料上传单，包括排在忙碌执行位之后的单据；实际起跑前再次按最新设置检查。
时间表为排队调度的最高优先级：改表会撤销尚未开始单据的旧“立即分析”放行标记，旧启动参数也不能覆盖已保存的时间表。
页面不展示额外的精确调整区、复制工作日区、节假日设置或重复生效表格。

配置接口为 `GET/POST /api/audit-schedule`，配置保存于本环境 `worktrees/.settings/audit-schedule.json`，
使用同源令牌、revision 冲突检查及原子写入；重启后保留，服务器配置不随本地源码覆盖。
字段、时段边界、节假日和旧 `--always-audit` 启动默认规则见
[OSS 接收与调度契约](skills/orchestrate-offline-audit/references/oss-intake.md#自定义核销时间)。

## 图片事实复用与本地跨单检索

### 核销流程的完整保留与重复传输优化

现有执行链保持原有职责：逐份读取全部材料可见事实并绑定来源，资料门禁确认适用范围，商品范围定位及查库准备参照，陈列逐张核验或各类型现场核验，全量POS身份和精确数值复算，最后完成本类全部审核要点及结果汇总。读图事实、业务判断、模板视觉对照与程序校验是不同工作；不会为省Token跳过照片、POS行、业务规则、真实不确定项或必要的有界重试，也不复用历史合格结论。

在保留上述范围的前提下，现有API模型请求做以下优化：

- 同一请求的 `references/business-guide.md` 已由模型适配器完整放在system。`prompt_delivery.py` 只将已知 `guide/business_guide` 指令变量中逐字相同的前缀改为对system原文的引用，保留全部动态补充。业务证据、Schema、CLI提示和读图缓存契约不改写；未完全匹配时保留原文。实际省去的请求字节数另记技术观察，不冒充实际Token用量。
- 同一勤策业务有多张本次照片时，完全一致的业务、原始/校对版本和铺货记录只作为公共来源提供一次，每张照片仍有独立来源、imageId、指纹、unit_id及逐图值，并通过 `business_reference_unit_id` 明确引用公共原值。校对值或版本不同的记录不混并；单图沿用直接结构。商品/识别码等参照中完全一致的大张申请关联表也只提供一次，以 `application_reference_unit_id` 引用，逐字段可还原，所有申请行仍保留。公共来源与全部逐图来源共同进入原审核链，原逐图完整技术记录仍可追溯。
- 陈列逐图调用继续给每张照片提供原图和本次商品参考图。完整逐图审核完成后，总审读取全部已校验的photo_reviews和商品参照记录，不再上传同一批商品参考图；合同模板、付款等本阶段需要的原图继续提供。其他核销类型的参考图发送保持原流程。
- 勤策重叠或连续日期区间合并取数，保持覆盖日期集合完全一致，间隔日期不会补入；原申请行及各行期间完整保留。照片关联过程对每种响应仅解析一次，减少重复JSON解析和长ID转换。这两项减少网络和CPU开销，本身不计模型Token节省。

优化以原值可还原、逐图/逐行覆盖、规则和来源校验不变为验收条件。资料读取仍保留首次批处理和受控事实缓存，业务审核继续独立执行；重试仍只针对原有传输、结构及真实校验失败，不为固定中文句式重复重跑整单。

进一步减少POS重复提取：`native_pos_identity.py`仅对程序原生读取、原facts未变、无读取限制且表头明确唯一的电子表，按同一文件/工作表的列对应逐行取得商品名称和69码。每条原行仍须通过现有`validate_extraction`的全量覆盖、逐字来源和完整原值校验，随后沿用原逐行查库、同码全部记录及精确算术流程。空字段、前导零、小数和异常69码不猜填、不修复；多义表头、公式身份字段、多行身份、无法解释的记录和读取限制继续使用原模型提取。未知行会撤销后续列绑定，直到出现新的明确表头；不能借另一文件/工作表的表头。技术档案记录实际提取方式、表头来源、完整原行和逐行结果，不把程序提取记为AI调用或虚构Token节省数。

横向POS矩阵的相邻名称/69码表头由`audit_core/native_pos_matrix.py`按原列位置读取，明确上层合计列可与当前表块关联，全部门店和销量原行仍保留。只提取SKU身份再查原商品库，不据列头补单价、金额或日期；有未解释行、身份缺项或跨文件/Sheet/读取单元表头时继续原模型路径，不新增失败或客户缺件。需要检查时使用`audit_tools.py --case <本单case.json> pos_matrix_identity`，返回SKU绑定和来源位置，不回送整表。分阶段提示只展示当前用途的工具说明，既有响应校验保持兼容。

原生POS原文片段及相同商品查库结果的无损共享从64,000字节开始尝试，只有实际变短才使用；不再仅在600,000字节容量告警附近生效。完整原文、每一行和来源仍保留在原证据及校验器中；算术明细投影沿用原门槛和规则。

API业务阶段的输出Schema只对完全相同的重复结构使用标准`$defs/$ref`，并移除JSON格式空白；展开后与原Schema完全一致，必填项、合法来源枚举、规则数量、条件分支及所有约束保留。原Schema与实际发送Schema共同留档，程序按等价完整契约校验结果；已有引用作用域的Schema保守沿用。资料转录、CLI和图像事实缓存契约保持原有形式，不因本次业务传输调整让已验证事实缓存重新失效。业务审核模型及原推理强度保持现有配置。文本字节缩减仅说明发送内容变化，不能冒充客户账单Token或金额降幅。

用户2026-11-03确认本项以节省模型Token为目标。原图字节完全相同、模型连接身份与事实提取prompt/schema/规则版本一致时，复用已验证的roles/facts/limitations并绑定当前unit_id；图像变化、提取契约变化或缓存损坏均正常重读。首次读取继续六图/字节上限批处理，不能改为逐图调用增加冷缓存开销；每次门禁与本次合同、商品参考和必要逐张审核仍独立执行。

`worktrees/.cache/image-facts-v1/`为本部署有界可见事实缓存，保存JSON与完整性校验、不保存原图、模型密钥或审核结论；技术观测记录读取图片、命中次数，不虚构省下的Token。事实按运行登记持有关系，授权删除运行时仅撤销该运行，孤立事实一并清除。

大图分批的中间联合观察也沿用该缓存及原有容量限制，每个现场或参考来源仅保存已校验的`roles/facts/limitations`。缓存键同时绑定当前平台、模型、端点、推理模式、Skill、Schema、完整事实提取提示及本批全部原图SHA与来源ID/角色；整批所有来源命中并通过当前检查后才省去这批模型读取。局部命中不足以跳过整批，不复用门禁、金额或最终审核结论；后续仍执行本次业务审核。该中间步骤只输出`scene_observations/reference_observations`，不能因所附业务Skill而改为完整`gate/checks`。技术档案记录实际命中、来源哈希及复用事实哈希，不把缓存读取记成新的AI调用。

未命中的联合观察组同时受48MiB传输字节预算和每次32个来源的分组目标约束，逐次等分较长的现场或参考轴，完整保留所有现场与参考组合及原图字节。事实提取只携带本阶段读取合同和实际图片来源，不在每个组合重复发送整套Skill、审核规则及业务指南；后续业务审核仍完整执行原标准。拆分前优先检查原组的全部已验证事实，原组未命中时才检查或读取子组。显式续跑在到达未完成调用前沿用原分组目标以复用已付费断点，之后的新工作采用当前目标。分组目标不是模型输入输出Token上限，不改变模型、推理配置及原重试次数。

后续审核的联合观察采用可逆的逐字事实池：每个来源的相同facts和limitations字符串只传一次，batch_scopes用从0开始的索引完整保留原批次、顺序、重复出现次数及同批现场/参考范围；参考ID、原图和商品元数据另有完整映射。不能把某批未见或未匹配扩大为全单缺失。原始回答、请求及校验记录仍完整留档，压缩不作语义改写、不跨角色合并、不复用审核结论。

用户明确停止后的本地失败续跑可定位真实ai.interrupted请求，不把此前已成功的重试当作当前失败。已返回用量和未完整返回次数分别保留；只恢复本地上传、无回调、原ZIP指纹及同模型一致的原记录。其他活动任务及服务进程不随本次维护重启或停止。

已唯一核对的取图商品按产品编码固定排列，避免模型返回同一商品集合的不同顺序导致参考图编号、分组和事实缓存无效；每条原请求、逐字引用及匹配结果仍保留在原有来源记录中。

资源使用有硬上限：内存热点最多128条且按Python对象保守估算不超过8MiB；事实磁盘缓存最多128MiB/10000条，单条最多1MiB，按最近使用淘汰。现场照片索引的SQLite页面预算为256MiB、最多100000个图片指纹和300000个来源位置，数据库页缓存约4MiB，历史候选查询和单批匹配输出均有数量上限。超过指纹解码像素预算时只做流式SHA精确比对；索引达到容量时停止新增并记部分覆盖，不加载全库、不无限扩容。上述限额针对本功能持有的数据，不表示完整Python/模型客户端进程只占8MiB。

`worktrees/.cache/photo-index-v1/`为独立本地现场照片索引，不写商品RDS。按客户部署隔离，保留核销单号、运行、ZIP摘要和图片来源；同一核销单重交、重试、重新分析不算跨单。仅明确现场照片入库，合同、付款、模板及商品参考图排除；混合材料须有本图照片角色依据。原图字节一致时程序报告文件一致事实，pHash相似候选只留技术记录，不新增模型或强制人工复核任务，不据此认定造假、重复报销或拒付。

索引只覆盖启用后实际解析的照片；本次不自动解压或补建历史、不重跑旧核销、不改历史结果、不补发回调。PDF目前按整页渲染，一页多图不能宣称具备逐图覆盖。工作台原子发布本次结果后，已完成且来源仍有效的记录才供后续运行引用；授权删除同步失效。缓存或索引不可用时保守重读/记录诊断，不冒充已命中或已全量查重。

2026-11-03 本地修复（系统2.16.14，API1.61、规则2026-11-03.pdf.39不变）：依据参半 HX202604110016 留存证据，POS确定性复算兼容“零售价”“合计金额”和数量/金额的“求和项:”列名，保留单元格换行；不能解析的工作表不再被其他表掩盖成全量完成。仍按原精度核算全部明细，不跨表猜用合计。客户表达按实际渲染图片与原文件的完整映射修复重复后缀，仅删除混入问题摘要的独立、无具体数值的其他照片符合描述；遮挡问题、原始事实及不确定性保留。本次本地修复不改正式历史、不自动重发回调或发布服务器。

2026-11-03 五单反馈修复（系统2.16.17、API1.62、规则2026-11-03.pdf.41）：外采赠品采购交易记录可选且不能替代必交票据，双用途照片缺件合并；补差申报销量<=POS实际销量，满减证明仅明确满减返还适用，合同预计销量与费用计费数量按字段语义分别核对。补差增加来源严格绑定的合同/结算周期一致及逐图拍摄日期范围校验，价格证明逐图完整分析并只输出问题图片。原始数值、历史及正式回调不改写；是否本地验证、服务器临时测试或正式发布以本次独立回执和按单Personal Brain记录为准。

2026-11-04 三单反馈修复（规则2026-11-04.pdf.43）：人员激励已交合同、结算单分别与本单精确关联勤策申请比较执行周期；已交原生POS电子表缺销售单价列或SKU单价值时明确报告，保留原盖章版缺件及资料门禁。上述有限预检不调用完整业务审核，不推算缺失单价。陈列按合同区分面积、纵位等规格，并联合本图地址等实际依据核对同名分店；不能猜面积、把层数当纵数或猜选分店。实现、测试、真实原包及发布状态分别留档，旧结果与回调不改写。

## 参半产物 OSS 主存储（2026-11-17）

此能力只适用于 `dingding-K3` 上的 `/data/offline-activity-audit`。本地和宝塔没有 `config/artifact-oss.json` 时，沿用原有本地存储；环境变量不能单独启用，配置也不能在其他机器或目录生效。它不改变业务 runner、审核规则、模型、数据库或回调。用户于 2026-11-17 另行确认参半可使用既有“资料上传”功能，覆盖此前不携带该模块的发布限制；不包含本地 Codex 会话接管功能。

- Bucket 为 `xiaokuo-dingding-xianxiahexiao`，产物专用前缀为 `offline-verify/audit-artifacts/canban/`，参半使用已实测的深圳内网 HTTPS Endpoint。商品参考图前缀 `offline-verify/product-reference/` 不变，不混用图片适配器的路径权限。
- `blobs/<指纹前两位>/<SHA-256>` 保存不可变的完整档案包和原始 ZIP；`index/<运行名SHA-256>.json` 为最后发布的档案目录；`inputs/` 保存原始资料位置索引；`reports/<运行名SHA-256>/` 单独保留原有 HTML 和总结，字节与本次原件一致。均为私有对象，不改桶权限、不创建公开链接、不覆盖同名不同内容。
- `statistics/` 保存与完整档案指纹绑定的轻量历史统计事实。预计完成时间继续使用原来的样本资格、进度匹配和算术方法，合并 OSS 历史与本地活动单；读取不重跑模型，也不把档案迁走误报为历史样本不足。旧统计缺失时仅从已核验档案提取一次并留内存，GET 不写 OSS，不创建长期本地历史副本。
- `model-usage/<运行名SHA-256>/<完整档案SHA-256>.json` 保存旧列表缺少的分模型用量派生摘要；只依据该单已保存的调用计量生成，未知模型仍为未知，不按当前配置或主模型猜分配。原总量必须一致，旧 `index/`、完整档案、原总结与回调不覆盖。列表只读取轻量摘要并有界缓存，缺失或损坏时保留原总量，不下载整包、不写 OSS、不拖垮其他记录。新归档已自带分模型摘要时直接沿用。
- 历史列表合并 OSS 目录与尚未归档的活动工作区。查看详情、技术记录或原件时，在受保护的请求临时空间完整取回并核验，响应结束即删除；Linux 崩溃残留通过拥有者标记和非阻塞锁在下次使用时回收，不按文件年龄删除其他进程的数据。用户明确删除已归档记录时，移除活动目录索引，完整内容和删除回执保留在 OSS 回收区，不误删共享原件。
- `cleanup_enabled=false` 只启用读取适配，不启动归档后台任务。正式切为 `true` 后，仅对 runner 已释放、关联核销及回调均处于终态的记录上传、回读、核验，再逐个清除对应运行文件与明确属于该任务的 ZIP。没有生成报告的早期失败任务也可按精确 job_id 归档原件，不补造 HTML。OSS 故障时保留尚未归档的工作副本和明确错误，不重跑 AI、不丢弃原件或伪报完成。
- 程序、依赖、原环境配置、凭据及 `.settings/.intake/.reviews` 等必要运行/队列/人工核验索引留在服务器；业务报告和原始 ZIP 不再作为服务器本地长期档案。终态之前的工作区和暂未成功归档的副本是临时执行状态，不能直接删除。

独立迁移入口为 `deploy/artifact_storage.py`，提供 `plan`、`upload`、`verify`、`prune`。计划与回执必须在项目外；初次清理要求同一计划的完整纯 OSS HTTP 验证回执，且正式服务已在空闲门禁后停止。发布需保留已认证的堡垒机连接，不反复退出。配置结构见 `deploy/artifact-oss.example.json`；模板默认禁止自动清理，不能未经验收就直接开启。迁移工具不调用模型、勤策或业务回调，也不修改旧 HTML、总结或核销状态。`GET /api/config` 在启用的参半环境增加 `artifact_storage`，给出模式、前缀、自动清理状态及最近存储错误类型；其余环境响应不增加该字段。

历史分模型用量补齐入口为 `deploy/backfill_artifact_model_usage.py --plan <项目外计划文件>`，默认仅冻结当前档案及索引指纹；确认范围后使用相同计划追加 `--apply --receipt <项目外回执文件>`。工具逐份完整核验原包，仅新增上述不可变派生摘要并回读，处理后确认原索引不变，临时恢复文件立即清理；不启动 runner、不调用模型、不查询勤策、不发送回调。已生成后可改用 `--verify` 只回读验证，不重复下载档案或写 OSS。只在参半指定机器和路径执行，本地及宝塔不启用。

参半“资料上传”与本地使用同一表单及 `POST /api/intake/upload`：需要同源及 `X-Offline-Audit-Upload-Token`，明确选择类型后提交 ZIP，复用现有两个 worker 与核销时间表。即使填写真实 `verifyCode/analyzeId`，手动上传仍不发送业务回调；系统 OSS 投递继续沿用自己的回调合同。运行期间允许必要的临时 ZIP 和工作区，终态校验归档后清理到 OSS，不恢复服务器长期本地历史。发布验证使用隔离替身或拒绝路径，不因功能上线自动提交真实核销或消耗客户 Key。
