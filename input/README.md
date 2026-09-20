# ZIP输入入口

本目录直接放置1至10个原始ZIP。程序安全盘点全部资料后，由AI按正文、图像和原始表格识别资料角色，逐项对照PDF“核销资料”列的七套清单，再识别以下七类。ZIP名、文件名、资料标题、费用项目名称和文件数量都不能直接决定类型；逐类比对结果及来源保存在 `material_matches`。

| 类型 | `--scenario` |
|---|---|
| 人员激励 | `personnel_incentive` |
| 陈列堆头 | `promotional_display` |
| KT板等物料制作 | `poster_material` |
| 搭赠（含额外搭赠、POS达标激励） | `giveaway_promotion` |
| 补差 | `price_difference_support` |
| 进场费 | `entry_fee` |
| 外采赠品（含客户自采赠品物料） | `self_procured_gift_material` |

只使用指定PDF的资料清单和审核要点，详见[项目说明](../README.md)。培训免POS和Excel；搭赠照片或小票二选一；物料制作不强制POS；其他类不自动继承人员类Excel要求；进场费无活动时限要求但须检查水印日期、地址和店名。

同类多个ZIP分别核验；一份来源可凭实际内容覆盖多个资料项，同一项的合同多页、照片多张合并计项。资料项必须与PDF清单不多不少，条件例外按PDF执行；缺项、多项、条件不明或未唯一匹配都直接核销失败，不先猜类型再审核。只有结算单和POS不能匹配任何完整清单。审核只覆盖本类及PDF明列的适用通用要点，每项一次，不添加其他检查。其他费用和维护费用不能作为兜底；历史样例保留原样。

```powershell
py -3 -B skills/orchestrate-offline-audit/scripts/run.py --run-id <YYYYMMDD-run-id> --producer-model codex
```

`--scenario`只在全部包内容分类后筛选，不能强制指定类型。每次独立发布 `worktrees/<压缩包名>-<上海时间>/`，包含一份自包含HTML、摘要、manifest/snapshot和证据，不发布工作簿、不覆盖历史。

OSS资料保存到独立的 [`input-oss/<job_id>/`](../input-oss/README.md)，不复制到本目录。直接input运行没有上游业务身份，不自动回调。
