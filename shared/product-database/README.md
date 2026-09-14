# 独立商品数据库：Docker 部署与搬迁

2026-09-11 已将这个独立数据库切换到 Docker，MySQL 固定为 `8.0.45`，镜像摘要固定在 `compose.yaml`。现有 130 条商品已逐字段核对一致，TLS、只读权限、新数据卷恢复、导入失败回滚及容器重建后的数据保留均已验证。

核销程序及各 Skill 的商品身份分析现统一读取此表。`audit_core.product_database` 使用
`product_viewer` 通过 TLS 在只读事务中查询；同次运行共用快照，下次重新读取。
参考图片按数据库的图片集地址从私有 OSS 按需读取，旧 JSON 不再提供文字主数据或编码别名。
完整边界见 [audit-knowledge.md](audit-knowledge.md)。数据库不存图片，不自动同步外部接口。

在项目根目录执行 `py -3 -B -m audit_core.product_database` 可验证真实只读入口，输出行数、读取时间与内容哈希。
失败会明确报错，不能回退旧 JSON；正式运行将本次快照保存到技术档案的 `analysis/product-database.json`。

## DBeaver 连接不变

| 项目 | 当前值 |
|---|---|
| 驱动 / 连接方式 | MySQL / 主机直连 |
| 主机 | `192.0.0.148` |
| 端口 | `3307` |
| 数据库 / 表 | `product_catalog` / `products` |
| 用户 | `product_viewer` |
| 密码 | 沿用原密码 |
| SSH | 关闭隧道 |
| SSL | 启用，驱动属性 `sslMode=REQUIRED` |

当前表包含三个商品身份字段和一个可空的图片集字段：

| 字段 | 中文含义 | 类型与约束 |
|---|---|---|
| `barcode_69` | 69 码 | `CHAR(13)`，非空 |
| `product_name` | 商品名称 | `VARCHAR(512)`，非空 |
| `product_code` | 商品编码 | `VARCHAR(64)`，主键，区分大小写 |
| `image_manifest_key` | 商品图片集 | `VARCHAR(1024)`，允许 `NULL`，默认 `NULL`，`utf8mb4_bin` |

同一 69 码可以对应多个编码。迁移核对基线为 130 个编码、119 个不同条码；后续身份以实时数据库为准，不读取历史 JSON 或图库备份。

### 商品图片集字段与 OSS 读取

2026-09-14 新增 `image_manifest_key`，原有商品此列初始全部为 `NULL`，表示“未配置图片集”。
随后完成 130 个商品、537 张参考图片及 130 份清单迁移，并逐张从 OSS 回读校验 SHA-256。
字段的中文注释为“商品图片集：OSS 清单 Object Key，不存临时签名链接”。
DBeaver 刷新表结构并重新查询数据后，可看到原有三列后新增此列；本次未增加核销页面或图片预览控件。

实际存储形式（一个商品的稳定图片集地址）：

```text
offline-verify/product-reference/CP-KQ-KP-0015/manifest.json
```

这是单份图片清单的稳定 Object Key，不是图片本体、OSS 文件夹或有效期 60 分钟的下载 URL。
清单逐张列出视图 ID、Object Key、文件大小、图片类型、尺寸和 SHA-256，不凭文件序号伪造正面/侧面标签。
Bucket、地域、凭据由独立存储配置和受控读取程序管理，不重复写进商品行；使用 SDK 私有签名请求，不持久化下载 URL。
清单中的视图需按准确商品编码和 69 码关联，并经上传结果及哈希验证后才允许填写本字段；
本字段不赋予自动上传权限，也不会因增加字段就自动从 OSS 取图。
核销入口在同一只读事务读取三个身份字段及图片集元数据，两类哈希分别保存。

用户已确认商品图片不做版本管理。OSS 接入后，每个商品使用固定目录
`offline-verify/product-reference/<product_code>/`，同一视图沿用原 Object Key，新图片直接覆盖旧图片，
`manifest.json` 也在原路径更新；数据库的图片集路径无需随图片更新改变。
不创建 `v1/v2`、时间戳版本目录或应用侧历史图片副本，不额外维护一套测试参考图库。

实现时必须先完成该商品图片上传和完整性校验，再更新清单内的图片条目及 SHA-256；
同商品更新应串行。核销使用任务内已经校验的临时副本，不在同一任务中途替换；
下载遇到清单与图片哈希不一致时，重新读取清单并有限重试，仍不一致则报系统读取失败，
不能混用新旧图片或跳过完整性检查。任务结束后清理临时副本，下一任务读取最新清单。

历史核销可以保留当时的对象路径、哈希和使用记录，但覆盖后无法仅凭哈希恢复旧参考图，
不得承诺按旧图完整复现。此策略只针对商品参考图，不覆盖核销档案或原始业务 ZIP。
日常发布只覆盖明确授权商品的固定 Object Key，已有对象必须有本工具管理标记；不覆盖其他文件。
不授权擅自更改现有 Bucket 的版本控制或其他桶级策略。原始素材集合、输入 ZIP 和历史核销档案保留。

运行配置为本目录 `product-images.json`。凭据位于仓库外
`%LOCALAPPDATA%/OfflineActivityAudit/ProductImages/credentials.dpapi`，使用当前 Windows 用户 DPAPI 加密，
目录 ACL 限当前用户、SYSTEM 和管理员。迁移机器或更换运行账号后，重新配置凭据，不能直接复制密文当作跨机凭据。
此图片凭据后端目前是 Windows 实现；下文 Linux 步骤只覆盖数据库，不代表 OSS 运行凭据已支持 Linux。

在项目根目录执行：

```powershell
py -3 -B shared/product-database/product_images_oss.py configure --apply
py -3 -B shared/product-database/product_images_oss.py verify
```

`configure` 使用不回显输入的两字段凭据 JSON，不把密钥放命令行；轮换需加 `--replace-credentials`。
一次性本地迁移、关联和图库清理入口已退役；不恢复本地图库，也不读取旧迁移回执作为运行事实。
配置文件缺失或内容不符合已批准 OSS 范围时直接失败，不能切换到本地模式。
日常只读检查（不依赖本地图）：`py -3 -B shared/product-database/product_images_oss.py verify`；
需要逐张重新下载并核验全部字节时加 `--images`，流式校验不会保存图片。
日后更新单个商品，先按新品 Skill 独立确认包装与数据库身份，再使用
`publish_product_images.py --product-code <准确编码> --source-dir <完整图片集目录>` 预演；
获得该商品写入授权后加 `--apply`。保留原视图文件名以覆盖同一对象，不创建图片版本目录，原始素材不删除。
运行时仅下载选中的参考图片；每商品最多四张。任务缓存与模型副本都在临时空间，正常和异常退出清理，
不得保存到 `input`、`worktrees` 或共享图片目录。强制结束进程/断电留下的已登记临时目录，在下次正式 runner 启动时核实归属和占用锁后补清理；活动任务及归属不明目录保留。

新数据卷由 `schema.sql` 直接创建四列。已有数据卷不会重跑初始化 SQL，必须用增量迁移：

```powershell
py -3 -B dbctl.py migrate-image-manifest
py -3 -B dbctl.py backup
py -3 -B dbctl.py migrate-image-manifest --apply
```

第一条仅预览。应用时采用 `ALGORITHM=INSTANT`，元数据锁等待上限 5 秒；不重启容器、不重建表、
不更新原有商品。应用后核对身份字段内容哈希和行数、字段定义及初始空值。
相同字段已存在时重复执行无修改；已有字段类型不符时拒绝覆盖。遇到并发写入导致校验变化时报告错误，
不自动回滚 DDL、清空图片地址或恢复旧数据。

`product_viewer` 仅有本库 `SELECT`、`SHOW VIEW` 权限，强制 TLS；root 仅允许在容器内本地管理。容器端允许查看用户经过转发连接，访问来源由宿主端口绑定和防火墙限制。

## 文件与数据位置

- `compose.yaml`：MySQL 镜像、持久数据卷、密码文件、端口、健康检查和重启策略。
- `schema.sql`、`init-viewer.sh`：全新数据卷首次启动时创建库、四字段表及只读账号。
- `migrations/001-add-image-manifest-key.sql`：已有数据卷的图片集字段增量迁移，由管理工具检查后执行。
- `dbctl.py`：配置、启停、检查、图片集字段迁移、备份和恢复工具，只用 Python 标准库。
- `.env.example`：非敏感配置模板；实际 `.env` 为当前宿主配置，不提交 Git，不直接搬到 Linux。
- `runtime/backups/*.sql` 与配套 `.sql.sha256`：可搬迁的商品数据备份，已排除 Git，需单独复制。
- `test_dbctl.py`：备份覆盖保护、完整性、恢复保护及图片集迁移的测试。
- `switch-to-docker.ps1`：本次 Windows 原生服务切换脚本，已执行；Linux 不使用它。

**实际运行数据保存在 Docker 命名卷 `offline-product-catalog_mysql_data`，不会因为复制项目目录而自动复制。搬迁前必须执行备份，并携带 SQL 和校验文件。** 数据卷不依赖本机原生 MySQL 安装；容器重建会继续使用原数据卷。

密码文件通过 Compose secrets 挂载，位于操作系统当前用户的受限配置目录，不进入源码、镜像或普通迁移包：

- 当前 Windows：`%LOCALAPPDATA%\OfflineActivityAudit\ProductDatabase\docker\root_password.txt` 和 `viewer_password.txt`。
- Linux：`~/.local/share/offline-activity-audit/product-database/docker/`。
- 原 Windows `credentials.json` 位于上述 `ProductDatabase` 目录，仍可用于安全交接原账号密码。

## 当前 Windows 运行方式

容器名为 `offline-product-catalog-mysql-1`。本机 WSL 使用 mirrored 网络，当前通过 Windows 持久端口转发提供局域网连接：

```text
192.0.0.148:3307 → Windows portproxy → 127.0.0.1:13307 → MySQL 容器:3306
```

`.env` 因此设为 `MYSQL_BIND_ADDRESS=127.0.0.1`、`MYSQL_PORT=13307`；DBeaver 仍填 `3307`。防火墙规则 `OfflineProductCatalog-MySQL-3307-LAN` 允许 `192.0.0.0/24` 访问该 LAN 端口。IP 变化时同步更新端口转发的监听地址、防火墙和客户端地址。

Docker Desktop 已配置为当前用户登录后启动；容器设置 `restart: unless-stopped`。这是登录后启动 Docker 的方式，不能当作 Windows 无人登录时的系统服务保证。Linux 上使用 Docker Engine 系统服务即可由系统启动。

原 Windows 服务 `OfflineProductCatalog` 已停止并禁用自启动。`my.ini`、`enable-lan.ps1` 及 `runtime/data/` 仅保留作源端回滚，不是当前运行环境；原有其他 MySQL 实例和核销应用未改变。正常使用不要再次执行旧 `enable-lan.ps1`。

在本目录打开 PowerShell：

```powershell
py -3 -B dbctl.py status
py -3 -B dbctl.py check
py -3 -B dbctl.py backup
```

启停用 `py -3 -B dbctl.py start` / `stop`，日志用 `docker compose logs --tail 100 mysql`。手工停止后需要执行 `start` 恢复运行。

## 搬到 Linux

目标机安装 Docker Engine、Docker Compose 插件和 Python 3.11 或更高版本。数据库容器自带 MySQL，无须在目标宿主安装 MySQL Server、客户端或 Python MySQL 驱动。

1. 在源端执行 `dbctl.py backup`，记下本次 SQL 文件名。复制本目录的部署文件、该 SQL 及同名 `.sql.sha256`；普通 Git clone 不会携带被忽略的备份。旧 `runtime/data/` 不作为跨平台恢复来源。
2. 要保持 DBeaver 原密码，通过受限渠道将原 `credentials.json` 放到目标运行用户的 `~/.local/share/offline-activity-audit/product-database/credentials.json`，目录权限 `700`、文件权限 `600`。不要放进项目。如果不交接原凭据，`configure` 会生成新密码，DBeaver 要相应更新。
3. 在全新目标目录生成自己的 `.env`，不要沿用 Windows 的私有密码路径。已有目标数据库时先备份，不能覆盖配置或删除数据卷。
4. 按以下命令启动并恢复。将示例 IP 换成目标机实际局域网 IP，将备份文件名换成刚导出的文件名。

```bash
cd /目标路径/offline-activity-audit/shared/product-database
python3 -B dbctl.py configure --bind 192.0.0.20 --port 3307
python3 -B dbctl.py start
python3 -B dbctl.py restore runtime/backups/product-catalog-实际时间.sql
python3 -B dbctl.py check
python3 -B dbctl.py backup
```

`configure` 会自动读取上一步标准私有目录中的 `credentials.json`，也可通过 `--credentials /受限路径/credentials.json` 指定。其参数只传文件路径，不传明文密码。`.env` 已存在时拒绝覆盖。

Linux 直接映射 `目标内网IP:3307 → 容器:3306`，不需要 Windows portproxy、`.ps1`、原 MySQL 安装路径或源电脑保持开机。数据库用户固定为 `product_viewer`，库表和四字段结构保持一致。已有旧数据卷须先执行上述增量迁移；全新数据卷由新版 `schema.sql` 创建。

配置 Docker 服务随系统启动，并限制该端口仅在可信局域网可达；检查 Docker 实际转发规则，不只检查 UFW 状态。在另一台电脑用 DBeaver 连接目标 IP、3307、原库和账号，确认行数、全部字段、TLS 和只读权限。目标恢复及连接验收通过后再停止源实例。

## 备份、恢复与回滚约束

备份导出商品表数据，建库建表结构由版本化的 `schema.sql` 提供，用户权限由初始化脚本重建，不迁移 MySQL 系统账号库。若以后变更表结构，必须先同步 `schema.sql` 和迁移说明。

备份使用显式列名：原三字段备份可恢复到新版空表，图片集列默认 `NULL`；新版备份包含图片集字段，
必须恢复到已含该字段的空表，不能直接导入未迁移的旧结构。

`restore` 只接受有匹配 SHA-256 校验文件的本工具备份，目标 `products` 表必须为空；已有数据时拒绝导入。导入使用事务，SQL 出错时整批回滚。密码文件用于首次初始化，修改文件不会自动修改已有数据库用户密码。

普通 `stop`、`start` 和容器重建保留数据。不要执行 `docker compose down --volumes`，也不要手工删除正式数据卷。卷丢失时用全新卷启动、再恢复已校验 SQL。

若当前 Windows 切换后需要回滚，在无新增数据库修改或已妥善备份增量的前提下，管理员 PowerShell 先移除本库端口转发：

```powershell
netsh interface portproxy delete v4tov4 listenaddress=192.0.0.148 listenport=3307 protocol=tcp
py -3 -B dbctl.py stop
powershell -NoProfile -ExecutionPolicy Bypass -File .\enable-lan.ps1
```

只移除本库的精确地址与端口，不重置其他 portproxy。旧原生数据是切换时快照，不能覆盖容器中后续新增或修改的数据。本次验证记录和源快照位于 `runtime/`。

若目标无法联网拉镜像，可在源端额外用 `docker image save` 导出 Compose 中固定摘要对应的镜像，目标 `docker image load` 后启动；镜像不包含数据库数据，仍须恢复 SQL。本次未额外生成大型离线镜像包。
