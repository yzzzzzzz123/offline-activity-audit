---
name: read-audit-document
description: 读取核销Word与PDF的完整页面、原生文字、表格和签章来源。适用于DOCX、DOC、PDF文件无法读取或需要逐页读取，不负责业务审核。
---

# 读取核销 Word / PDF

需要读取Word或PDF时调用本工具；正常上传已调用时直接使用现有索引，不重复转换。不让模型现场写Word/PDF解析代码。

在项目根目录执行：

```powershell
python -B skills/read-audit-document/scripts/read.py --input "促销合同.docx" --output "独立的新输出目录"
```

工具只读原件，Word由隔离转换进程生成PDF，PDF完整逐页成像并保留原生文字。返回索引和页位置，不调用模型或发送回调。原生文字供定位，不能替代印章、遮挡或画面识别。

使用索引中的当前所需页面；同一资料多页仍保留全部页的覆盖。真正的损坏、打开密码与缺组件、权限、超时分开处理，不把工具故障当作业务缺件。Windows不退出用户已有Office会话；服务器使用LibreOffice Writer。

只有当前阶段需要模型读取页面时才加载[页面读取约定](references/runtime.md)，业务审核规则仍由已选核销Skill提供。

大PDF、多页扫描或资源异常时再看[大文件读取边界](references/large-files.md)。

已读取的活动日期先交宿主`activity_periods`只读工具处理，固定CLI为`skills/orchestrate-offline-audit/scripts/audit_tools.py --case "本单case.json" activity_periods`。工具联合本单已关联文书的正文和表格，保存完整原文，仅在同一资料有明确活动年份，或本单唯一合同月日完全对应时还原简写年份。明确分档逐段保留并检查间隙；签署、付款及申请日期不冒充结算日期。输出仅当前用途的日期和来源位置，原文证明留档，不重复喂长段文字；无歧义日期的精确比对由正常宿主阶段完成，不让模型再次推导。
