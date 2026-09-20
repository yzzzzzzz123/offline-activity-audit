# 陈列堆头：PDF第1页

范围：地堆、陈列、专架、端架。规则版本：`2026-09-18.pdf.3`。

本文件由 `scripts/sync_pdf_policy.py` 从 `audit_core/pdf_policy.py` 生成。修改时先核对原PDF，再同步。

## 核销资料

定类前按资料项严格匹配：必交项齐全、条件项按PDF执行、无清单外资料，且唯一匹配本类。资料项缺失、多余或适用条件不明不能进入审核；资料存在后的字段、签章、水印、金额等不合规由本Skill逐项核验。

- `display_photos`：活动期间带地址、拍摄时间水印的照片，清晰展示全貌、产品摆放、促销标识。适用条件：`always`。
- `atrium_agreement`：商场中庭大型活动现场展示的商场入场协议（仅大型活动现场需要）。适用条件：`atrium`。
- `settlement`：结算单（公司统一模板，客户盖章）。适用条件：`always`。
- `promotion_contract`：签章后的促销合同。适用条件：`always`。

一份文件可凭实际内容覆盖多个资料项；同一项的多页合同、多张照片或多店资料合并计项。不能把独立清单外资料笼统算作附件，也不按物理文件数量、命名或后缀选型。

## 唯一审核要点

- `submission_deadline`：核销自活动结束日起3个月内提交，超期不再核销。月份按日历月；必须有真实活动结束与提交日期，不能拿文件名或分析日期代替。进场费按原文“无时间要求”不作活动时限拒付，仍检查水印日期是否存在。 适用条件：`activity_deadline`；原文后果：`reject`。
- `review_deadline`：财务一审已超2个月且资料有误或缺失，不再补，直接0核销；只有同时具备时间和错误/缺失证据才触发。起算事件不清不能猜测。 适用条件：`always`；原文后果：`zero`。
- `scene_authenticity`：确保费用场景真实；张冠李戴不予核销，额度不返还。仅有模糊或读不清不能认定张冠李戴。 适用条件：`always`；原文后果：`reject`。
- `fraud`：有充分证据确认核销造假时0核销并记录罚款要求（先费用池后经销商额度）；不推测造假，不执行罚款、扣款或通知。 适用条件：`always`；原文后果：`zero`。
- `bi_quota`：BI额度不足不允许先做活动后补申请；以业务发生时的额度与申请、活动时间证据判断，不能用当前余额代替。 适用条件：`always`；原文后果：`reject`。
- `settlement_template_seal`：结算单须为公司统一模板并由客户盖章。 适用条件：`always`；原文后果：`missing_material`。
- `contract_signed`：促销合同已签章。 适用条件：`always`；原文后果：`missing_material`。
- `pos_fields`：POS必含5+2：活动时间、SKU/69码、商品名称、销售数量、销售单价、销售金额、销售金额及数量合计；缺项即不完整。 适用条件：`uses_pos`；原文后果：`missing_material`。
- `pos_seal`：要求提交的POS数据有经销商盖章。 适用条件：`required_pos`；原文后果：`missing_material`。
- `pos_arithmetic`：销售金额=数量×销售单价，并核对销售金额、数量合计。 适用条件：`uses_pos`；原文后果：`missing_material`。
- `application_quantity_price`：数量、单价与申请不符或填错，直接0核销。必须区分销售单价与奖励/补差单价，不把实际销量低于计划自行解释为填错；比较口径不清记无法核验。 适用条件：`uses_pos`；原文后果：`zero`。
- `display_watermark`：照片拍摄日期在执行周期内，地址与活动门店一致，日期和地址缺一即不完整。 适用条件：`always`；原文后果：`missing_material`。
- `display_brand`：申请的堆头或陈列大小范围内只能存在参半品牌，不能有其他产品；不得把申请区域外相邻货架算为混摆。 适用条件：`always`；原文后果：`missing_material`。
- `display_material_brand`：堆头陈列物料元素为小阔集团产品（参半、重点、小箭头）。 适用条件：`always`；原文后果：`missing_material`。
- `atrium_agreement`：商场中庭大型活动现场展示有商场入场协议；没有则缺资料。仅大型活动现场要求此项。 适用条件：`atrium`；原文后果：`missing_material`。
- `display_quantity_price`：数量、单价等明细与申请填错，直接0核销。 适用条件：`always`；原文后果：`zero`。
- `display_size`：按照申请的堆头大小与现场照片判断是否符合；没有可用规格或尺度时明确无法核验，不使用旧固定面积、列数标准。 适用条件：`always`；原文后果：`missing_material`。

`zero`只在明确不符合且证据充分时直接0核销；`reject`为不予核销；`missing_material`为资料/依据不满足。
无法核验不能写成造假、金额填错或已确认超期。BI额度、提交时间、一审时间、历史进场等缺少依据时如实记录，不猜日期或查历史报告补值。
不合规项在本次结果登记汇总；不自动扣款、罚款或发送人事通知。

## 适用条件

- `always`：必审；`activity_deadline`：进场费按无时间要求豁免，其他类适用。
- `not_training`：人员培训费豁免；其他费用用途不套用培训例外。
- `required_pos`：非培训人员、外采赠品、补差、搭赠、POS达标激励；`uses_pos`：本类实际涉及POS。
- `atrium`：商场中庭大型活动现场展示；`temporary_staff`：依活动申请说明有无临促字段确认；`cvs_otc`：CVS/OTC渠道。
- `red_packet`：红包截图；`full_reduction`：满减返还。
资料适用条件不明在分类阶段失败；其余审核条件缺少证据时如实记录问题并继续。补差满减返还证明是必交项，不受full_reduction标记豁免。

## 范围边界

只审核以上PDF要点。不得追加数据库商品登记、EAN校验位、固定堆头面积/列数、地图距离、旧合同预算公式、出库单、进场扣款证明、POS达标渠道/阶梯资格等要求。
搭赠照片或小票二选一；培训费可免POS及Excel；POS达标激励必交盖章POS及Excel，没有培训豁免，也不要求赠送规则、活动照片/小票或红包。物料制作不强制交POS；其余类型不自动继承Excel要求。
