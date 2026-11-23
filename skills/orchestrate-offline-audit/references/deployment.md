# 部署说明

客户环境固定为 **Linux 原生 Python + systemd + 阿里云 RDS MySQL 8.0/8.4**，不使用 Docker 部署。

部署、依赖、源码入口、模板路径、接口、数据库迁移和运维命令统一维护在[根目录 README](../../../README.md)，本文件不另存一套命令。

- 服务入口：`deploy/service.py`。
- 配置模板：`deploy/application.env.example`。
- 数据迁移：`deploy/rds.py`、`deploy/rds/schema.sql`、受控 `deploy/seed/`。
- 商品读取：`scripts/audit_core/product_database.py`，RDS TLS 只读连接。
- 材料读取发布门禁：`deploy/material_reader_preflight.py`。
- 业务约束与验证：[AGENTS.md](../../../AGENTS.md)。

历史源端 Windows 数据库工具只用于源端维护和备份，不作为客户 Linux 部署依赖。客户凭据、CA 和端点由部署人员现场配置，不复制源电脑的私有密钥。

## 材料读取系统依赖

Linux 发布机必须同时具备以下能力，缺一不能把错误归因于客户材料：

```bash
python3 --version                 # >=3.11
libreoffice --version             # Writer 与 Calc 均需可用
bsdtar --version                  # libarchive，用于嵌套 RAR
fc-list :lang=zh family            # 至少一个中文字体族
python -c 'from zoneinfo import ZoneInfo; ZoneInfo("Asia/Shanghai")'
```

Ubuntu/Debian 参考安装：

```bash
sudo apt-get install -y python3 python3-venv python3-pip \
  ca-certificates tzdata libarchive-tools \
  libreoffice-calc libreoffice-writer fonts-noto-cjk
```

只安装 GNU tar 不代表支持 RAR；只安装图片库不代表可以读取 DOCX 或需要转换的旧式工作簿。系统依赖、权限、字体、时区和磁盘/内存问题属于 `system_failure`，不能写成 `customer_unreadable`。

## 发布前门禁

```bash
cd /opt/offline-activity-audit
sudo -u offline-audit .venv/bin/python -B deploy/material_reader_preflight.py
sudo -u offline-audit .venv/bin/python -B deploy/material_reader_preflight.py \
  --smoke-material /path/to/approved-test-material.zip
```

预检输出 JSON，失败退出非 0。检查内容包括 Python 读取依赖、LibreOffice Writer/Calc 隔离转换、`bsdtar/libarchive`、CJK 字体、`Asia/Shanghai`、`worktrees` 可写、staging 位于调用方输出目录内，以及 POSIX 路径不依赖 PowerShell/Word COM。`--smoke-material` 使用正常 `read_material()` 入口，不调用模型、RDS、OSS、勤策或业务回调。

`--allow-non-linux` 只供开发诊断，不能作为发布通过。Windows 本机或没有 LibreOffice 的开发机结果不能冒充 Linux 实机验收。

## systemd 与临时目录

systemd 模板已经设置：

```ini
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=@PROJECT_ROOT@/input @PROJECT_ROOT@/worktrees
HOME=@PROJECT_ROOT@/worktrees/.service-home
UMask=0077
```

服务输出、材料读取 staging、Word/LibreOffice profile 都必须落在 `worktrees/` 或目标页面父目录，不依赖系统 `TEMP`。发布前和重启前执行：

```bash
sudo -u offline-audit .venv/bin/python -B deploy/healthcheck.py --wait-idle 1800
```

只有返回 `idle:true` 才能停止/重启；活动模型请求或核销任务期间重启会把任务错误中断。

## 验收边界

材料读取预检和 `read_material()` 成功只证明文件能打开并产生读取回执，不证明 OCR、商品库、业务审核、最终总结或三字段回调通过。Linux 实机仍需用客户批准的测试 ZIP 覆盖 DOCX、真正 BIFF `.xls`、嵌套 RAR、CJK 页面、扫描 PDF 和图片，并单独做模型、RDS、OSS 与回调联调。
