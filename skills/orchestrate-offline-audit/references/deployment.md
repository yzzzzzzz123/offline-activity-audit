# 线下活动核销项目迁移：交给 AI 的执行手册

> 目标：迁移到 Linux 服务器。代码、项目技能、配置模板和部署脚本统一放在 `offline-activity-audit` 内；不能直接内置的环境，由接手 AI 到目标机安装、配置并验证。
>
> 本文件是执行说明，不是整个项目部署完成证明。2026-09-11 核实：独立商品 MySQL 已在 Windows Docker Desktop 的 Linux 容器内运行，配置和备份恢复工具位于 `shared/product-database/`；核销服务仍为 Windows 原生进程，Linux 全场景尚未验收，尚未生成离线镜像包。

## 1. 直接复制给接手 AI 的提示词

```text
请完整读取当前项目根目录 AGENTS.md、skills/orchestrate-offline-audit/references/deployment.md、pyproject.toml，
以及 shared/product-database/README.md，并以当前源码和目标机实测为准。

请把整个 offline-activity-audit 项目部署到当前 Linux 服务器：
1. 先盘点源项目、未提交文件、运行历史、OSS 商品图片、独立 MySQL 及所需凭据。
2. 自行检测并安装缺失环境，优先生成项目内的 Dockerfile、Compose 配置、
   配置模板、备份恢复脚本和检查脚本；所有新增交付文件放在本项目内。
   独立商品数据库已有 Compose 和 dbctl.py，先复用并验证，不重复创建空库代替数据迁移。
   不依赖源电脑的 Python、Codex 桌面目录、其他项目、私人全局技能或绝对路径。
3. 保留核销业务规则、模型 gpt-6-astra、推理档位 medium、原始材料和历史结果。
   只做迁移所需的平台适配，不重设计页面，不改变请求、回调及业务判断。
4. 处理 Linux 的 RAR 解压、旧 .xls 嵌入图片读取、Codex 命令兼容和只读沙箱。
   用真实保留材料验证；不能把工具安装成功或页面能打开当成十类核销全部可用。
5. 迁移 product_catalog.products 的实际数据，保持产品编码固定和只读查看权限。
   MySQL 是唯一商品身份来源，同时迁移 image_manifest_key；参考图继续使用私有 OSS，不新增本地回退或定时同步。
6. 保留 worktrees、隐藏任务收据、人工核验记录、统一 input；OSS 商品图保留远端，目标机重新配置只读访问。
   先在隔离端口和独立数据目录验证，不影响源端正常服务。
7. 完成数据核对、Linux 测试、浏览器验证、隔离的 AI 与 OSS 回调闭环、重启及恢复演练。
   最后按实际授权安排单实例切换；没有切换授权就交付可核验的待切换状态。

不要只给安装命令或计划，请在授权范围内连续执行。
只有目标信息或凭据缺失、需要本人登录、缺少系统权限、或缺少实际生产切换授权时，
才提出具体问题；已授权步骤不重复确认，其他独立工作继续完成。
密钥通过受限配置注入，不写进文档、Git、普通迁移包、镜像或日志。
最终报告区分已通过、失败和未执行，列明文件位置、连接方式、剩余问题和回滚方法。
```

## 2. 当前事实与目标边界

环境基线来自 2026-09-11，商品数据源边界于 2026-09-14 更新；迁移当天必须重新检查。

商品 OSS 凭据目前使用 Windows 当前用户 DPAPI。Linux 部署必须先实现受控凭据适配并验证，不能直接复制 DPAPI 密文或回退本地图片；未适配时如实报告未具备正式取图能力。

| 项目 | 当前状态 | 迁移要求 |
|---|---|---|
| 源目录 | Windows `D:\codex\offline-activity-audit`；WSL `/mnt/d/codex/offline-activity-audit` | 自动定位目标目录，新脚本不写死源路径 |
| Git | `https://github.com/yzzzzzzz123/offline-activity-audit.git` | 核对远程、分支、提交及未提交/未跟踪文件；普通 clone 不包含后两者 |
| 核销服务 | Windows 上的 Python 原生服务，局域网 `8080` | Linux 独立运行并可自启动 |
| 前端 | `skills/orchestrate-offline-audit/assets/offline-activity-audit.html` | 保留唯一前端和历史静态档案 |
| 源码版本 | Audit System `2.11.22`、API `1.35` | 以 `skills/orchestrate-offline-audit/scripts/audit_core/workbench_html.py` 为准；旧进程可能仍加载旧版本 |
| AI 默认值 | `gpt-6-astra` / `medium` | 以 `skills/orchestrate-offline-audit/scripts/audit_core/codex_runner.py` 为准，不沿用旧文档的 `high` |
| 商品主数据及参考图 | MySQL 商品身份 + 私有 OSS 多视图 | 迁移数据库及配置；只读核验 OSS，不迁入本地图库或 GitHub 灾备 |
| 独立数据库 | Docker MySQL `8.0.45`，容器 `offline-product-catalog-mysql-1`，局域网端口 `3307` | 使用项目内 `dbctl.py` 导出、恢复；旧原生服务已停止并禁用，原数据保留作回滚 |
| 商品表 | `product_catalog.products`，130 行 | 三身份字段 `barcode_69`、`product_name`、`product_code` 和图片关联 `image_manifest_key`；编码主键固定，条码允许重复 |
| 数据库职责 | 核销商品身份唯一来源 | 保持 TLS 只读及任务内快照；不读历史 JSON 或本地参考图库 |
| Docker | 独立商品数据库已部署，Compose 固定镜像版本及摘要；恢复和容器重建已实测 | Linux 复用 `shared/product-database/`；核销应用容器尚未交付，无离线镜像包 |

Windows 虚拟环境和二进制工具不能直接复制到 Linux 使用；按目标架构重新安装或构建。AI、OSS 和业务回调仍需网络，这不是离线模型部署。

## 3. 依赖清单：先读源码，再安装

| 能力 | 权威文件/目录 | 现场检查 |
|---|---|---|
| 业务范围与验收规则 | `AGENTS.md` | 完整读取，以当前要求为准 |
| Python 环境 | `pyproject.toml` | Python `>=3.11`；直接依赖 httpx、jsonschema、mcp、openpyxl、Pillow、pypdf |
| 唯一正式 runner | `skills/orchestrate-offline-audit/scripts/run.py` | 正式核销只能由此入口执行 |
| 工作台 | `skills/orchestrate-offline-audit/scripts/audit_core/workbench_server.py` | `python -m audit_core.workbench_server` |
| 模型执行与隔离 | `skills/orchestrate-offline-audit/scripts/audit_core/codex_runner.py`、`skills/orchestrate-offline-audit/scripts/audit_core/codex_environment.py` | CLI 参数、认证、模型目录、代理、只读沙箱 |
| RAR | `skills/orchestrate-offline-audit/scripts/audit_core/archive_input.py`、`skills/orchestrate-offline-audit/scripts/audit_core/material_intake.py` | 当前调用 `tar`，预期为 bsdtar/libarchive 行为 |
| 旧工作簿和图片 | `skills/orchestrate-offline-audit/scripts/audit_core/legacy_activity_workbook.py` | 当前通过 PowerShell + Excel COM 转换 `.xls` |
| 地点核验 | `skills/orchestrate-offline-audit/scripts/audit_core/location_resolution.py` | 百度地图 MCP、`BAIDU_MAPS_API_KEY` |
| 商品知识入口 | `skills/orchestrate-offline-audit/scripts/audit_core/product_database.py`、`skills/orchestrate-offline-audit/scripts/audit_core/product_images.py` | 只读 MySQL + 私有 OSS；不依赖图库备份 |
| 商品图片维护 | `shared/product-database/publish_product_images.py` | 明确商品及完整图片集，先预演，获得写入授权才发布 |
| 独立数据库 | `shared/product-database/` | 使用 `compose.yaml` 和 `dbctl.py`；原 `my.ini`、`.ps1` 和 Windows portproxy 不搬到 Linux 运行 |

Node.js 只在选择 npm 安装 CLI 时需要，不是当前前端的构建要求。Playwright/浏览器用于验收，不是线上服务的必要运行依赖。现场追踪新增依赖，不把此清单当成永久固定版本。

## 4. 源端交接：代码、数据和凭据

### 4.1 必须迁移的内容

- `skills/orchestrate-offline-audit/scripts/audit_core/`、`skills/`、`shared/`、根 HTML、`tests/`、项目配置和文档：交付当前工作副本，包括必要的未提交文件，不能被远端旧提交覆盖。
- 商品图片不随运行副本迁移：保留 OSS 原对象及数据库图片关联；GitHub 图库是完全独立的远端灾备，不检出、不作为依赖。
- `worktrees/`：包括 `.intake/`、`.reviews/` 等隐藏目录，不仅是可见运行文件夹。
- `input/`：保留已落盘原始材料及子目录，不展平、不覆盖、不重新下载。
- 独立 MySQL：执行 `shared/product-database/dbctl.py backup`，复制新 SQL 和 `.sql.sha256`，携带当前 `schema.sql` 和初始化脚本。正式数据在 Docker 命名卷中，复制项目目录不自动复制该卷；只有 `schema.sql` 不等于数据备份。

先检查 `.gitignore`、`.gitattributes` 和 `git status --short`。普通 clone 不包含被忽略的历史和数据库；使用 Git/LFS 配合独立数据归档。遇到链接或外部路径，先核对归属，不跟随链接打包其他项目或用户目录。

生成相对路径、大小、SHA-256 交付清单，目标端逐项核对。记录数据库导出文件哈希及行数/字段统计。

### 4.2 不直接搬运的内容

- Windows `.venv`、缓存、临时解压目录和模型临时工作目录：目标平台重新生成。
- `shared/product-database/runtime/data/`：源端保留作回滚，不能直接当作跨平台恢复方案。
- 密码、AccessKey、模型登录令牌：不进入普通归档、源码、镜像或公开日志。
- 外部桌面程序、全局私人技能和其他项目目录：由目标环境独立安装或通过项目内依赖替代。

### 4.3 冻结与一致性

初次预拷贝和目标环境准备可以在源端运行时进行。最终备份前停止上游新提交，等下载、排队、核销和回调中的任务全部结束，再停止源服务并做最终同步。

`/api/intake/jobs?completed=0` 也包含失败任务，不能用数量判断是否忙碌。按当前状态枚举区分活动与终态，同时检查手工 input 运行。没有恢复方案时，不能把活动任务搬到另一台机器继续执行。

历史文件中的源路径和时间属于证据，不批量替换；若旧路径影响读取，修复只读兼容投影并保持历史字节不变。

### 4.4 凭据从哪里取得

- 数据库：源 Windows 用户 `%LOCALAPPDATA%\OfflineActivityAudit\ProductDatabase\credentials.json` 可安全交接原账号；当前容器密码文件位于同目录的 `docker/`。`viewer_password` 用于查看，`admin_password` 用于容器内管理。目标通过受限渠道接收凭据、重新执行 `dbctl.py configure`，不复制带 Windows 密码路径的 `.env`，不输出密码值。
- 地图及商品维护接口：源进程环境或 Windows 当前用户环境。Linux 没有注册表回退，必须注入目标服务的实际进程环境。
- 模型：在目标运行用户或容器内完成登录，默认不搬运源电脑的认证缓存。
- 凭据由授权人员通过受限配置交接；不进入普通迁移包。若源机器之后不可访问，提前完成安全交接，不能假定项目目录自带密码。

## 5. Linux 现场配置

### 5.1 部署方式与交付位置

优先 Docker Engine + Compose。先识别发行版、CPU 架构、磁盘、内存、端口和 sudo 权限，再按官方支持方式安装。Linux 服务器使用 Docker Engine，不要求 Docker Desktop。

独立数据库的 Compose 和管理脚本已经在 `shared/product-database/`，直接复用。核销应用的部署内容由接手 AI 现场生成，下面仅为应用的目标结构，不代表当前已经存在：

```text
offline-activity-audit/
  deploy/
    Dockerfile
    compose.yaml
    .env.example              # 非敏感配置和占位符
    scripts/                  # 初始化、检查、启停、备份、恢复
    data/                     # 如使用项目内绑定卷，在此保存数据
    backups/                  # 受控备份，排除 Git 和镜像构建上下文
  skills/orchestrate-offline-audit/references/
    deployment.md            # 当前部署说明
  artifacts/deployment/
    迁移验收报告.md             # 现场完成后生成，不提前填写“通过”
```

配置使用项目相对路径、容器固定路径或环境变量，不依赖源机绝对路径。敏感配置通过目标受限凭据文件/secret 注入；模板留在项目内，不携带秘密。

固定实测镜像版本或摘要，不使用浮动 `latest`。以 `pyproject.toml` 安装依赖，验证后记录实际版本。明确持久化数据库、材料、档案和必要认证目录，验证重建容器后仍可用。不挂载整个宿主用户目录或 Docker socket 给核销容器。

暂时无法容器化时，可在项目内 `.venv` 和工具目录先验证，并生成 systemd 配置；如实标记尚非容器部署，仍完成同样的业务和数据验收。

### 5.2 RAR：GNU tar 不等于 bsdtar

Linux 默认 `tar` 常为 GNU tar。安装 libarchive/bsdtar，并在两个调用位置统一选择正确工具；优先解析 `bsdtar`，保留兼容回退。不替换系统 `/usr/bin/tar`。

用真实 RAR 验证列表、中文文件名、尺寸、类型和解压结果。路径穿越、链接、重复路径和解压预算检查继续生效，不能为了解压成功跳过安全盘点。

### 5.3 旧 `.xls`：图片关系必须完整

`self_procured_gift_material` 当前依赖 Windows + Excel COM。真实样本为 OLE/BIFF `.xls`，不能通过改后缀或直接交给 openpyxl 解决。

选择可在 Linux 运行的读取/转换方案，与 Windows 参考结果核对：

1. 原文件只读，只在任务临时目录转换。
2. 商品行、69 码、编码、数量、金额和公式含义保持一致。
3. `DISPIMG` 图片 ID、关系文件、实际图片及行到图片映射完整保留。
4. 逐张核对图片数量和哈希；若存在图像变换，验证无损性与映射，不能只比较数量。
5. 经正式材料解析入口验证；工具失败不能伪装成客户漏交图片。

LibreOffice 等工具返回成功不等于保留了特殊图片关系。若该场景尚未通过，继续完成其他独立工作，但标记其未就绪，不能宣称十类核销全部可用，也不能暗中依赖源 Windows 电脑远程转换。

### 5.4 Codex CLI、认证和只读沙箱

将目标架构的 CLI 安装到镜像或项目受控工具目录，通过 `OFFLINE_AUDIT_CODEX` 指向实际文件。不能把 `codex.exe` 搬到 Linux。固定经过验证的版本，并检查：

- `codex debug models --bundled` 返回可解析目录，包含 `gpt-6-astra`。
- `codex exec` 支持当前 runner 要求的图片、JSON Schema、只读沙箱、临时会话及配置参数。
- 认证在实际服务用户下有效，不仅是部署终端的另一用户已登录。
- 模型临时材料可读、写入被拒绝，各阶段只获得允许的材料。
- 不以 `--privileged` 或关闭沙箱掩盖权限问题；必要兼容调整必须实测边界。

无浏览器服务器可按官方方式运行 `codex login --device-auth`，由用户完成网页登录。CLI 版本、登录状态和模型目录检查不能代替实际模型调用。现有 Windows 沙箱预检在 Linux 直接返回，必须补充 Linux 实测，不能将跳过算作通过。

### 5.5 网络和代理

分别检查目标机、容器、Docker 守护进程和模型子进程的代理，测试镜像仓库、AI、OSS 和回调。源机检查曾发现 Docker 使用不可达的 `127.0.0.1:10808`；这只是排障线索，不是目标配置。容器中的 loopback 指容器自身。

模型代理使用 `OFFLINE_AUDIT_MODEL_PROXY`。保留 OSS 下载和回调的独立直连行为，不用全局代理修改掩盖问题。云服务器不一定能访问 `192.0.0.225:8699`，先验证路由或取得目标可达的回调地址。

## 6. 配置契约

填入实际地址，并确保变量传给服务进程。仅写配置文件、没有被服务加载，不算配置完成。

| 环境变量 | 基线/用途 |
|---|---|
| `OFFLINE_AUDIT_WORKBENCH_URL` | `http://<目标服务器可达内网IP>:8080/`，不能填 `0.0.0.0` |
| `OFFLINE_AUDIT_OSS_ENABLED` | 正式服务 `1`；隔离验收按测试步骤控制 |
| `OFFLINE_AUDIT_OSS_ALLOWED_HOSTS` | `xiaokuo-dingding-xianxiahexiao.oss-cn-shenzhen.aliyuncs.com,oss-cn-shenzhen.aliyuncs.com` |
| `OFFLINE_AUDIT_OSS_PRODUCER_MODEL` | `codex` |
| `OFFLINE_AUDIT_MODEL` | `gpt-6-astra` |
| `OFFLINE_AUDIT_REASONING_EFFORT` | `medium` |
| `OFFLINE_AUDIT_CODEX` | 目标 CLI 实际路径 |
| `OFFLINE_AUDIT_OSS_CALLBACK_URL` | 源端为 `http://192.0.0.225:8699/api/v1/ai/analyze/callback`，须验证目标可达性 |
| `OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK` | 仅可信内网 HTTP 回调设 `1`；改用 HTTPS 时调整 |
| `OFFLINE_AUDIT_OSS_CALLBACK_TOKEN` | 仅上游要求时配置，不在示例中填秘密 |
| `OFFLINE_AUDIT_OSS_NO_CALLBACK` | 正式服务不启用 |
| `OFFLINE_AUDIT_OSS_RECEIVE_ONLY` | 正式服务不启用 |
| `OFFLINE_AUDIT_OSS_ALLOW_PRIVATE_HOSTS` | 仅明确私网 OSS 下载需求时启用，不等于允许内网回调 |
| `BAIDU_MAPS_API_KEY` | 地点解析秘密配置 |
| `OFFLINE_AUDIT_LOCATION_MCP_TIMEOUT_SECONDS` | 可选，以源码支持范围为准 |
| `OFFLINE_AUDIT_MODEL_PROXY` | 可选，只填目标实际可达代理 |
| `CANBAN_API_ACCESS_KEY`、`CANBAN_API_SECRET_KEY` | 仅显式商品维护需要，不要求新增自动同步 |

MySQL连接由 `shared/product-database` 的Docker及只读账号配置管理；当前PDF现场SKU对照已复用该入口，并按本次唯一商品编码读取私有OSS细节图。不能添加任意 `DB_*` 变量替代现有入口。模型账号使用目标认证机制，不把聊天登录令牌当作 API key。

业务ZIP下载使用上游临时签名URL；商品参考图则由Windows宿主使用现有DPAPI私有凭据按需读取，这是两套独立配置。模型不接触凭据。Linux部署目前只覆盖数据库，商品图片认证仍须适配，不能宣称仅部署数据库即可完成现场SKU对照。

## 7. 独立 MySQL 迁移

详细命令见 [独立商品数据库 README](../../../shared/product-database/README.md)，不要另外发明不一致的部署方式。

当前 Windows 连接路径为 `192.0.0.148:3307 → Windows portproxy → 127.0.0.1:13307 → MySQL 容器:3306`，用于兼容本机 WSL mirrored 网络。Linux 直接使用目标局域网 IP 的 `3307:3306` 映射，不需要这一 Windows 转发层。

1. 核实源版本、库表、行数和权限。执行 `dbctl.py check`，连接正式容器，不把已停止的原生快照当作最新数据库，也不操作其他 `3306` 实例。
2. 执行 `dbctl.py backup`，复制新生成的 `runtime/backups/*.sql` 和对应 `.sql.sha256`。工具通过容器内 mysqldump 二进制输出一致性数据备份；结构与账号由项目的初始化文件提供。修改过表结构时先核对 `schema.sql`。
3. 目标安装 Docker Engine、Compose 和 Python；复用已经固定 `8.0.45` 及摘要的镜像。需要升级时查官方支持路径并单独验证，不默认拉取 `latest`。
4. 在目标安全交接凭据，执行 `dbctl.py configure --bind <目标实际内网IP> --port 3307`、`start`、`restore <SQL路径>`。恢复要求新卷中的空表及匹配校验文件；不导入旧 `mysql` 系统库。`product_viewer` 仅有本库 SELECT/SHOW VIEW，要求 TLS；root 仅供容器内本地管理。
5. 核对全部三字段、130 行和 130 个唯一商品编码基线，以及可重复的 69 码。迁移当天数据有变化时以源快照为准，不强行改回 130 行。
6. 从另一台局域网电脑测试 DBeaver：MySQL 直连、目标 IP、外部 `3307`、库 `product_catalog`、用户 `product_viewer`。关闭 SSH 隧道，驱动 `sslMode=REQUIRED`；查询成功，UPDATE/DELETE 被拒绝。
7. Docker 下同时检查端口映射、TLS、实际来源 IP、账号 Host 匹配和防火墙。转发可能改变来源地址，不能机械照抄旧网段或为排错开放远程 root。
8. 源端已完成 130 行逐字段恢复、失败导入整批回滚、容器重建和数据持久化演练；目标 Linux 仍须验证自身网络、权限、自启动和恢复结果。Docker 命名卷不在项目文件夹内，不能省略数据备份。

该三字段表不能替代知识库里的别名、图片特征和来源证据。环境迁移不自动授权删除 JSON/图片、改产品编码、开启定时同步或实施 OSS 图片存储。

## 8. 启动入口与业务契约

以下在目标环境的项目根目录执行。容器中包装为服务入口；原生部署使用项目 `.venv` 的 Python。示例尖括号内容必须替换为现场值；`run-id` 必须以 `YYYYMMDD` 日期开头，并为本次验收使用独立标识。

```bash
python -m audit_core.workbench_server --host 0.0.0.0 --port 8080

python -B skills/orchestrate-offline-audit/scripts/run.py \
  --run-id <独立验收ID> --producer-model codex \
  --biz-type "<用户已确认的核销方式>" \
  --model gpt-6-astra --reasoning-effort medium \
  --input-dir <隔离测试材料目录> --worktrees <隔离测试输出目录>
```

本地ZIP在运行前须先询问用户本次核销方式，确认后传入--biz-type，不再自动分类；类型不同的包分别运行。交互终端未传方式时等待选择，非交互未传方式时停止，不创建记录。工作台是否接收 OSS 由上节配置控制。隔离测试不要继承源端生产接收/回调配置。`input` 和 `worktrees` 要挂载到源码实际使用的位置；只声明任意 `/data` 并不意味着程序会使用它，必须检查实际落盘位置。

保留以下行为：

- 入站 `POST /api/intake/oss` 接收必填的 `verifyCode`、正整数 `analyzeId`、临时 `downloadUrl` 及必填非空字符串 `bizType`（业务类型），保持既有内网无鉴权契约。
- ZIP 安全下载到 `input/<job_id>/`；一个下载 worker、一个核销 worker，核销串行。
- ZIP 名唯一确定类型。“物料”“费用”的优先级及冲突退回按当前代码执行；类型无法确认算失败并回传原因。
- 同一业务二元组终态重交创建新 attempt、job 和运行，不覆盖旧记录。
- 回调 `/api/v1/ai/analyze/callback` 仅发送 `verifyCode`、`analyzeId`、`result`，result 为本次运行的业务 Markdown 小结。
- 回调重试只重发结果，不重新下载、不重跑 AI、不为旧历史补发回调。
- 历史结果保留只读，仅迁移环境不刷新旧 HTML 或业务证据。
- 页面和 API 仅在可信私网开放。检查宿主防火墙及 Docker 的实际转发规则，不仅凭 UFW 表面配置判断隔离成功。

## 9. 执行顺序和验收

### A. 环境与平台

记录架构、发行版、Python/CLI/MySQL/解压工具版本、运行用户和路径。验证项目内技能可读、模型可用性、Linux 只读沙箱、RAR、真实旧工作簿与图片关系。

### B. 隔离部署

使用仅 loopback 暴露且未被占用的端口，例如 `18080`、`13307`。使用独立输入、输出和数据库目录；不能让两实例共享生产可写历史目录或接收同一业务流量。

### C. 数据与代码

核对文件哈希、数据库全部字段值及知识库登记图片；检查 LFS 缺失、悬空链接、Windows 运行路径和意外打包的秘密。在 Linux 执行：

```bash
python -B -m unittest discover -s tests -v
python -B -m compileall -q audit_core skills
git diff --check
git status --short
```

按 `AGENTS.md` 校验 JSON 和 Skill frontmatter，记录失败及跳过原因。只在 Windows 跑过的检查不能写成 Linux 已通过。

### D. 业务、页面与恢复

- 经 bundled runner 用保留真实材料验证各场景，覆盖 `.xls`、RAR、合同扫描、参考图和中文长路径。没有材料或未执行的场景如实标记。
- 用新测试运行和受控回调接收端验证，不将测试结果发送给真实业务身份。
- 覆盖 OSS 下载、AI、持久结果、三字段回调、失败原因、回调重试和终态重提；页面打开不等于闭环通过。
- 在 1440×960 和 1280px 桌面验证概览、完整台账、错误/正确检查项、运行中/失败、历史静态页、刷新及 API/SSE，控制台无异常。
- 重启服务/容器后历史、任务收据、数据库数据和必要认证仍可用；自启动实际有效。
- 在独立恢复目录完成备份恢复演练，不使用生产目录做破坏性测试。

### E. 单实例切换与回滚

实际生产切换已授权后：暂停上游新提交 → 等源任务结束 → 停源服务 → 最终备份与增量核对 → 启动目标正式实例 → 调整上游地址 → 用获准测试业务身份验证闭环。

失败时先停目标正式接收，保留新增数据和日志，核对已发回调，再恢复源端和上游地址。不能默认目标处理过的任务可安全重跑。任何时刻只有一台正式实例接收业务；目标验收前保留源数据和回滚点。

## 10. 接手 AI 的最终交付

现场生成 `artifacts/deployment/迁移验收报告.md`，包括：

- 目标目录、架构、锁定版本、新增配置和脚本位置。
- 工作台 URL、MySQL 地址/端口/库/用户名、凭据配置位置，不输出密码。
- 实际采用 Docker 还是原生部署，服务名、自启动与持久化位置。
- 代码差异、未提交文件处理、历史迁移范围、文件和数据库核对结果。
- 每类核销及每项环境能力的“已通过 / 失败 / 未执行”，附证据位置。
- 已验证的启停、日志查看、备份、恢复和回滚命令。
- 当前为“环境已准备、待切换”还是“已正式切换”，源端状态及剩余事项。

若以后需要脱网安装，再导出已验证镜像或依赖包到项目内受控目录，记录架构、版本、哈希和导入命令。Docker 配置、源码归档或已安装 Docker，都不等于完整离线迁移包。

## 11. 官方资料

- [Docker Engine 安装](https://docs.docker.com/engine/install/)与[Linux Compose 插件](https://docs.docker.com/compose/install/linux/)：按目标发行版配置。
- [Docker 数据卷](https://docs.docker.com/engine/storage/volumes/)：容器与持久数据分别管理。
- [MySQL 官方镜像](https://hub.docker.com/_/mysql)：版本、初始化、数据目录和密码文件。
- [Codex CLI](https://learn.chatgpt.com/docs/codex/cli)与[认证说明](https://learn.chatgpt.com/docs/auth)：安装、登录及无浏览器认证。

资料用于核对环境行为，不代替当前源码和目标机实测。遇到不兼容，应定位和修复，不能靠更换模型、放宽业务规则或跳过失败场景宣称迁移成功。
