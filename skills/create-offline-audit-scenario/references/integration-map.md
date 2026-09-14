# 主流程集成图

只有一个新 Skill 目录，并不代表场景已经登记。相同场景 ID 必须由唯一正式命令端到端支持，激活才完整。

## 必需激活点

1. **安全分类及角色绑定——`audit_core/archive_input.py`**
   - 登记不会与现有类型冲突的 ZIP 名称标记；唯一标记确定场景，内容不能改类；
   - 正常材料每个必需角色恰好绑定一次，并保留已排除文件名；已知类型的缺件、内部文件误命名、重复角色或重复类型包进入材料诊断；
   - ZIP 名称无法唯一分类时完成必要安全校验后生成分类拒绝报告，不调用 AI 或解读材料，仍正常持久化和 OSS 回调；混合多包只打回未知包，其余已知类型照常；
   - 使用同一安全限制校验嵌套归档；不同安全路径的同名文件保留来源 ID，不能因 basename 相同丢弃；
   - 更新支持的场景顺序、重复标签、允许的 ZIP 最大数量和选定场景错误。

2. **视觉提取——`audit_core/codex_runner.py`**
   - 登记 Skill 目录及证据 schema；
   - 实现理由充分且最小的单阶段或多阶段提示图；
   - 只把允许的视觉来源复制到隔离模型工作区；
   - 校验 JSON 和完整来源覆盖，拒绝虚构或被静默遗漏的 basename；
   - 提示词不得接触表格、旧输出、缓存、最终判定及无关来源。

3. **确定性处理器——`audit_core/<scenario>.py` 和 `audit_core/orchestrator.py`**
   - 根据证据与确定性来源实现比较、规范化、算术、分组、支持金额、结论和补交要求；
   - 登记证据 schema、顺序、CLI 选择和处理器分派；
   - 可复用字符串和公式中不得包含样例值。

4. **结果契约——`contracts/audit-result.schema.json`**
   - 增加场景枚举和关闭的条件结果分支；
   - 要求场景专属核销数据，并禁止其他场景的载荷；
   - 增加 Schema 正向和反向测试。

5. **六列中间结果——`audit_core/report.py`**
   - 创建一个渲染器和一个准确工作表名称；
   - 内部结果保留来源顺序和原始值，不使用公式，并生成确定性的置信度、结论、影响及处理方式字段供技术审计；
   - 登记工作表顺序和验证。

6. **规范 HTML——`audit_core/html_report.py` 与
   `skills/orchestrate-offline-audit/assets/canban-audit-shell.html`**
   - 登记工作表/场景元数据、行分区、对象计数及完整结果/仅错误模式；
   - 在现有单一 HTML 中增加场景投影，执行[统一错误原因与处理方式规范](../../orchestrate-offline-audit/references/error-reasons.md)：错误原因只写失败事实，建议只写处理方式，业务文件区域只列相关实际 basename；不展开角色、路径、事实、数量、参考、比较及限制；
   - 完整来源及技术证据保留结构化内部结果；客户回调摘要只列具体原因，不拼接结论、影响、置信度或处理方式；
   - 有意提升模板版本和预期静态指纹，保留全部固定控制，并增加桌面/移动端及无横向滚动覆盖；
   - 绝不能绕过外壳指纹或重建第二个页面。

7. **项目契约与发现**
   - 更新 `skills/orchestrate-offline-audit/SKILL.md`、其路由规则和界面元数据；
   - 更新根目录 `AGENTS.md`、`README.md` 和 `input/README.md`，写明新登记场景、材料结构、选择 ID、证据边界及输出语义；
   - 更新本创建 Skill 的 `current-scenarios.md`，让下一次接入以新增场景检查冲突。

8. **测试与真实夹具**
   - 路由：有效、缺角色、重复角色、无类型标记、多类型标记、重复场景、不安全/嵌套 ZIP；确认无法唯一分类的包没有 AI 调用和视觉证据，已知缺件包仍调用 AI，混合多包仅拒绝未知包且其余结果不受影响；
   - 提取：证据 schema 校验、允许输入、虚构 basename 及完整覆盖；
   - 确定性规则：每项阻断控制、完全/模糊边界、金额上限/舍入、分组；
   - 输出：结果 schema、渲染器顺序、仅选定场景运行、组合运行、HTML 入口、数量、完整/仅错误行为、响应式布局及自包含；验证原因和处理方式分开、业务文件仅 basename、内部证据未丢失以及回调原因完整且无建议；
   - 隔离预期运行输入集后，使用代表性 ZIP 运行带 `--scenario <id>` 的正式命令。不得发布或提交客户证据。

## 激活门槛

从项目根目录运行全部命令：

```powershell
py -3 skills/create-offline-audit-scenario/scripts/check_scenario_integration.py --scenario <id> --skill <skill> --audit-module <module.py> --sheet-name <sheet>
py -3 C:\Users\EDY\.codex\skills\.system\skill-creator\scripts\quick_validate.py skills/<skill>
py -3 -B -m unittest discover -s tests -v
py -3 -B -m compileall -q audit_core skills
git diff --check
git status --short
```

还必须将每个 Skill 文本文件按严格 UTF-8 解码，并解析每个 JSON 文件。只有执行路径完整实现后才运行正式夹具；
失败运行不得发布任何内容，并必须删除临时解压、模型、工作簿和页面文件。

集成检查器只防止实现不完整，并不证明业务正确。不得用不会执行的字符串或只写文档的提及来满足检查；
必须检查并测试真实分派与行为。
