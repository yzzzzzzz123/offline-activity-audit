# 当前八类核销

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

唯一规则来源为用户指定PDF；详见 `skills/orchestrate-offline-audit/scripts/audit_core/pdf_policy.py`。新版PDF单列POS达标激励，必交盖章POS及Excel、结算单和签章促销合同；搭赠另需赠送规则及活动照片或小票。原搭赠副标题保留的POS达标激励文字不再作为合并依据。其他费用和维护费用退出活跃范围。
