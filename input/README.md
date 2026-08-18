# ZIP 输入入口

本目录直接放置一至两个原始 ZIP，不要手工解压或放入其他文件：

- 人员激励包：1 份销售 Excel、1 张名称可识别的结算单图片、至少 1 张转账或红包截图，不含 PDF；
- 堆头陈列包：1 份销售 Excel、1 份合同 PDF、至少 1 张现场照片。

可以单独提交任一类型，也可以两类各一份；同一类型最多一份。

在项目根运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-run-id> --producer-model <producer-model>
```

程序会安全检查、自动分类并在临时目录解压。类型未知、同类重复、材料角色不明确或关键材料缺失时会停止并提示补充。
最终文件名只使用业务日期与实际模型名，例如 `20260818-codex.xlsx`；同日期、同模型重跑时追加 `-1.1`、`-1.2`。
