# 按核销资料内容路由（规则版本2026-09-18.pdf.3）

| 核销类型 | 类型标识 | Skill |
|---|---|---|
| 人员激励 | `personnel_incentive` | `audit-personnel-incentive` |
| 陈列堆头 | `promotional_display` | `audit-promotional-display` |
| KT板等物料制作 | `poster_material` | `audit-poster-material` |
| 搭赠 | `giveaway_promotion` | `audit-giveaway-promotion` |
| 补差 | `price_difference_support` | `audit-price-difference-support` |
| 进场费 | `entry_fee` | `audit-entry-fee` |
| 外采赠品 | `self_procured_gift_material` | `audit-self-procured-gift-material` |
| POS达标激励 | `pos_target_incentive` | `audit-pos-target-incentive` |

ZIP名、目录名和文件名只保留作来源展示，绝不选择Skill。资料标题和申报费用项目名称也不能直接定类。按实际资料角色逐项对照PDF“核销资料”列的八套清单；不能只数PDF/图片/Excel，也不能凭一份共有结算单或POS直接分类。

安全盘点全部资料，整页读取PDF及图片、程序读取Excel/xls原始单元格，再由AI识别资料角色和资料间的实际对应关系。
先返回八类完整的 `material_matches`：每类全部资料项的存在状态、适用条件、来源，以及 `extra_materials` 清单外资料项。每类都必须将全部已读来源归入对应资料项或清单外资料，不得忽略额外文件或同页的额外资料。程序严格验证资料项相等：必交项齐全、条件项明确满足或按PDF豁免、清单外资料为空，且只有一个严格匹配的类型。缺一项、多一项、资料或适用条件不明都返回分类失败，不能带缺项定类。候选必须与严格比对结果一致；不得按命中数量、缺件最少或金额大小强制八选一。

唯一严格匹配后才进入对应Skill。只审核本类及PDF明列的适用通用要点，包括原文规定的签章、模板、水印、SKU和金额标准，每项一次，不漏不加，不将其他类型要求混入现有要点。条件例外和OR分支严格照PDF；详见各Skill的 references/audit-rules.md。

POS达标激励按新版PDF的独立清单匹配：盖章POS、POS数据Excel、统一模板结算单及签章促销合同。Excel必交，无培训豁免；不要求红包、赠送规则或活动照片/小票。搭赠另需赠送规则及活动照片或小票，不要求Excel。PDF搭赠副标题仍有POS达标激励文字，不作为合并两类的依据；缺少搭赠证据也不能改投POS达标激励。

不要把其他费用或维护费用作为兜底。改ZIP名不改变判断；同一资料项的合同多页、照片多张合并计项，一份来源可凭内容证明多个资料项。独立清单外票据或协议不能笼统并入附件。只有结算单和POS的包没有完整匹配类型，必须失败。

归档路径穿越、链接、嵌套深度/大小超限、损坏容器和传输/模型故障沿用安全失败路径。不能因未知文件名跳过原始资料读取。
