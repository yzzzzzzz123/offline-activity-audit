# ZIP 输入入口

把且仅把以下两个原始压缩包直接放在本目录，不要手工解压：

1. 一个人员激励核销 ZIP；
2. 一个堆头/陈列核销 ZIP。

然后在项目根运行：

```powershell
py -3 skills/orchestrate-offline-audit/scripts/run.py --run-id <唯一运行ID>
```

ZIP、自动解压内容和内部准备文件均被 Git 忽略。主 Skill 会自动识别类型，并分别调用人员激励与堆头子 Skill。
