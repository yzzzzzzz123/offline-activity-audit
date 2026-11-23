# 服务器展示模式

用户2026-10-20要求服务器核销页面提供展示样例，不额外接 MySQL，不执行真实核销。系统 2.16.6 / API 1.58 使用显式演示入口：

演示数据仅供服务器页面使用。本地正式地址 `http://192.0.0.158:8080/` 保留原有核销记录，不启用演示模式，不导入样例。开发验证只使用临时端口和内存样例，不改动本地 8080 服务的数据或启动参数。

```text
python -B skills/orchestrate-offline-audit/scripts/serve.py --demo --host 0.0.0.0 --port 18080
```

也可设置 `OFFLINE_AUDIT_DEMO=1`。演示启动参数仅支持 `--demo`、`--host`、`--port`；服务器如覆盖原服务启动命令，使用只包含这些参数的独立 systemd drop-in，保留原 unit 作为恢复依据。不能同时传正式 OSS 或 runner 参数。

演示数据来自 `assets/demo/records.json`，为六组明确标注的虚构业务样例；不读取历史核销、客户上传或测试夹具。复用正式唯一 HTML，通过既有上下文和 audit-data 数据槽展示列表、日期/来源/类型筛选、问题明细、正确检查项及只读技术档案。两组没有问题的样例也只表示展示状态，不能用于确认真实材料正确或准予结算。

演示进程不构造正式 WorkbenchHTTPServer 或 OSSIntakeService，不调用 runner、MySQL 查询或业务回调，不落盘创建核销 worktree/input。正式模式默认不变。核销记录、人工核验和删除接口继续只读，所有相关写入请求返回 403，不能通过 analysis 参数读取任意文件。

按用户同日的后续要求，移除页面顶部“演示数据”及其整段说明，恢复左侧“更换模型”模块。模型配置复用正式 ModelSettingsStore、平台独立密钥、版本检查与同源/Token 保护；“切换”只本地校验并保存启用配置，“测试返回”只测试待选配置、不保存或切换，只有用户手动点击“测试返回”才调用模型。不在启动或展示样例时调用模型，不因此启动核销任务。界面不能把配置保存伪装为连接测试成功。

接口合同：

- `/api/config`：`demo=true`、`read_only=true`，`mysql_enabled/model_enabled/callback_enabled/oss_intake_enabled=false`，核验及删除 `writable=false`。
- `/api/runs`：六条 `demo=true` 的记录，数量与详情问题数一致。
- `/api/runs/<样例ID>/snapshot`：与正式页面相同的 `run/view/analysis_files/dom_checkpoints/recent_events` 结构，附 `demo=true`；`completed` 是 UI 展示状态，页面显示“演示记录”。
- `/api/runs/<样例ID>/analysis?path=demo/record.json`：只返回该虚构样例；其他资源或 ID 返回 404。
- `/api/runs/<样例ID>/log`：明确说明未执行真实核销的展示说明。
- `/api/intake/jobs`：空列表，不接收上传。
- `/api/model-settings`：恢复正式配置的 GET/POST 合同与请求保护；模型配置保存在服务器独立状态中，不迁移本地密钥。配置及手动连接测试不改变演示记录，也不启用真实核销。

部署只同步本功能源码及虚构资产，保留服务器私密配置、业务历史和 Git worktree 元数据。重启前按同步流程确认无活动核销/下载/回调；只调整核销服务，不重启其他项目。

验证命令：

```text
python -B skills/orchestrate-offline-audit/scripts/verify_project.py -v
python -B tests/workbench_demo_browser_smoke.py
```
