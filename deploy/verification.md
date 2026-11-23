# 交付验证记录

## 2026-11-16 核销材料读取稳定性与 Linux 发布门禁

| 项目 | 结果与范围 |
| --- | --- |
| 真实核销单覆盖 | 用户请求16单，本机找到14个原始ZIP；`HX202610080010`、`HX202610130053` 无原件，未读取或推测 |
| 读取层结果 | 14/14单 `reading_status=read_ok`，213个源文件、253个读取单元、`model_calls=0` |
| 真实格式逐一验证 | JPEG、PNG、PDF、XLSX、XLS、DOCX 六类各取一个真实文件，先核对SHA-256与v3清单一致，再走单文件 `read_material()`；六类全部 `read_ok`，`model_calls=0`；最终证据为 `runtime/material-reader-real-formats-20261116-v2/summary.json` |
| EXIF方向归一化 | `HX202610180011/小票1.jpg` 的 EXIF Orientation=6，任务副本由 2528×3507 归一化为 3507×2528，`read_ok`、`model_calls=0`；原图 SHA-256 与来源不变，证据为 `runtime/material-reader-real-orientation-20261116/reading-manifest.json` |
| 汇总修正 | v3原始汇总将 `.jpg -> jpeg` 别名误报为格式错配，并按14单计算请求数；v3b仅修正汇总为16请求/14本地/0格式错配，不重读、不覆盖v3清单 |
| POS直接失败根因 | 真实观测显示 `data_source=null`、`verification.rows=[]`、全部 `method=unavailable` 且未查库；原因是中文 `other_facts.kind` 在传输Schema中被丢失枚举后又被严格校验拒绝。已保留值对象enum、规范化已观察中文别名、单页失败保留其他页和查库链路；8项隔离测试通过，但尚未真实模型重跑。证据见技术档案所列三份 worktree model-observations |
| 读取稳定性 | 内容签名优先、别名不误报、逐文件/逐页/逐sheet回执、坏附件隔离、部分成功不冒充客户不可读、系统依赖错误按owner分离、安全异常整包传播、staging固定在输出目录、RAR只接受bsdtar/libarchive |
| Linux发布门禁 | 新增 `deploy/material_reader_preflight.py`：检查Linux/POSIX、Python版本、读取依赖、LibreOffice Writer/Calc隔离转换、RAR能力、CJK字体、Asia/Shanghai时区、worktrees写入、staging位置及可选真实材料smoke；失败非0，不调用模型/外部业务接口。Windows开发诊断实际为 `pass=7, fail=2, warn=1, exit=1`，失败项为LibreOffice和tzdata，证据为 `runtime/material-reader-preflight-windows-20261116/preflight.json` |
| 回归 | 16批隔离全量回归 `2577 tests / 18 skipped / 0 failures / 0 errors`；读取核心定向复跑40项通过。最终分批汇总为 `runtime/regression-reader-20261116/shards-final-v2/summary.json`，由UTF-16LE BOM日志重建 |
| 源码检查 | `sync_pdf_policy.py` 检查模式返回 `{"mode":"check","changed":[]}`；`compileall` 覆盖 skills/tests/shared/deploy，退出0 |
| 验证边界 | 本机为Windows；Linux实机、客户DOCX/BIFF XLS/嵌套RAR、真实OCR、商品库、业务审核、持久档案和回调均未执行。本记录不声称核销通过或Linux已实机验收 |

详细根因、设计、证据路径和现场待办见
[核销材料读取稳定性整改-20261116](../docs/技术档案/核销材料读取稳定性整改-20261116.md)。

发布前必须执行：

```bash
sudo -u offline-audit .venv/bin/python -B deploy/material_reader_preflight.py \
  --smoke-material /path/to/approved-test-material.zip
sudo -u offline-audit .venv/bin/python -B deploy/healthcheck.py --wait-idle 1800
```

只有预检退出0且 healthcheck 返回 `idle:true` 才能重启/发布；不得把缺少 LibreOffice、bsdtar、CJK字体、tzdata或worktrees权限记录成客户材料问题。

## 2026-10-23 历史交付验证

验证日期：2026-10-23。交付方式：Linux 原生 Python、systemd、阿里云 RDS MySQL 8.0/8.4。当前正式核销无需百度地图 MCP。

## 已完成

| 项目 | 结果与范围 |
| --- | --- |
| Linux 全量回归 | Python 3.14.4；937 项，成功，跳过 8 项平台相关检查；验证环境未安装 MCP SDK |
| Windows 全量回归 | 918 项，成功，跳过 11 项；随后新增的 RDS 迁移与配置检查定向运行 15 项，成功，跳过 3 项 POSIX 检查 |
| RDS 运行边界 | 配置库名与账号、CA/主机名校验、TLS 必须协商成功、只读一致性快照、读取不完整失败、Linux 无旧数据回退 |
| RDS 导入保护 | SQL 校验失败、目标非空、表结构不符均拒绝；四个字段任一不一致回滚，全部匹配才提交 |
| 商品 SQL 实际恢复 | MySQL 8.0.45，恢复到连接内临时表；130 行、四个字段整体哈希与源数据一致，未修改持久表 |
| Linux 原生入口 | 独立目录配置及启动 `deploy/service.py`；工作台、配置、目录和模型脱敏接口通过，未配置接收返回 503 |
| systemd | 按实际项目路径生成单元，`systemd-analyze verify` 通过；配置目录 0700、私有文件 0600 |
| OSS 凭据与交付保护 | 与 RDS 检查联合定向运行 28 项：Windows 成功并跳过 4 项 POSIX 检查，Linux 纳入上述全量回归；覆盖普通包排除密钥、授权包 0600、初始化导入、回车保留、已有配置保护与不安全权限拒绝 |
| 实际运行配置 | 已生成 `config/application.env`，预填源项目回调与接收设置；新增检查覆盖随包配置、初始化保留 OSS 配置、客户修改保护、未初始化拒绝启动、缺少 RDS/模型密钥拒绝启动；Windows 联合定向 28 项成功（4 项 POSIX 跳过） |
| 工作台浏览器 | 1440×960：目录、筛选、详情、正确检查、技术日志/断点、人工核验、删除冲突保护及静态/服务端一致性通过；控制台无错误 |
| 桌面布局 | 隔离演示记录验证 1280px 下总览、技术档案和更换模型，均无横向溢出 |
| 源文件 | JSON、Skill frontmatter、Python 编译及 `git diff --check` 通过 |
| 清理 | 通过归属和占用检查清理 7 个缓存目录，共 4,735,568 字节；最终复查无待清理项 |

SQL 的 SHA-256：

```text
963215ce8ac16f21f16384cc26bd46bfce44542634536a57536544f8094a16cd
```

商品四字段清单的 SHA-256：

```text
0e95e5d37ec5eae76f184b77e3fb0d32b00973430c7fd03d4b374e99a119e9b8
```

## 客户现场仍须验收

- 使用客户 RDS 内网地址、CA 和实际只读账号，执行 `deploy/rds.py verify --read-only` 及 `deploy/healthcheck.py`。本次未取得客户 RDS 凭据；MySQL 8.4 的实际实例连接需现场验证。
- 使用客户批准的测试材料、真实模型 Key、商品 OSS 只读 Key 和测试回调，验证完整下载、核销、持久档案及回调重试。本次未调用客户模型、OSS 或业务回调。
- 安装 README 所列 `bsdtar`、LibreOffice Writer/Calc、中文字体和 tzdata，使用含 DOCX、嵌套 RAR、真正 BIFF `.xls`、CJK 页面及图片行关联的代表性材料执行 `material_reader_preflight.py --smoke-material`。本次 Linux 实机环境未完成该转换验收；相关分支已做隔离单元验证，不能替代现场验收。
- 在客户目标系统安装并启用 systemd 单元，检查开机自启动、日志和维护窗口停止流程；本次未操作客户系统服务。

## 交付边界

源环境的原始 ZIP、核销历史、数据库、私有配置和运行服务均保留。普通交付包包含源码、模板、规则、迁移工具及受控商品 SQL；排除真实输入/历史、私有配置、虚拟环境、缓存和诊断产物。

用户随后明确要求复用源项目商品 OSS Key。本次专用 `with-oss` 包携带 `deploy/private/product-oss.json`，归档权限为 0600；初始化自动导入 Linux 私有配置，其他凭据仍不随包携带。打包和导入过程不显示密钥值，不发送 OSS 或业务回调请求。普通打包模式不包含该凭据。

用户进一步要求随包提供实际配置文件，本次包含 `config/application.env` 与初始化标记，均为 0600。回调使用参半（小阔）服务器专用的 HTTPS 地址，接收设置为开启；本机与宝塔按 README 6.3 使用独立的内网 HTTP 地址，不以交付包覆盖。客户 RDS 地址、账号、CA、RDS 密码和模型 Key 仍由部署人员使用现场信息完成初始化。配置生成不表示已连接客户 RDS，也不执行真实业务回调。

只交接 `.tar.gz` 即可部署。包内 `DELIVERY-MANIFEST.json` 用于文件完整性校验，同名 `.sha256` 是可选的整包传输校验文件，不参与服务运行。
