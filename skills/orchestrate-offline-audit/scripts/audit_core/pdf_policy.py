"""The eight material lists and audit points in the user-approved one-page PDF.

This catalogue is the allow-list for new audits. Historical processors are not
part of this policy. Presence of a file is distinct from validity of its contents.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace


POLICY_VERSION = "2026-09-23.pdf.33"
PRODUCT_PHOTO_CLARIFICATION = (
    "用户2026-09-22确认：所有已有的现场照片对应SKU检查共用按需取图流程。"
    "先从品牌方本次合同、产品推广协议及其申请说明确定本次需核对的商品；"
    "优先用明确的产品编码，或用产品名称加69码共同锁定数据库中唯一产品编码。"
    "用户同日进一步确认：商品名称允许简称，不要求逐字相同；只要本次资料能确定唯一对应商品即可，不强制补齐另一标识字段。"
    "用库内轻量身份信息核对名称含义，保留规格、款式和组合装等区别；有多个可能商品或明确冲突时不能猜选。"
    "多个标识冲突、同名同码仍对应多个编码或资料无法唯一定位时，说明具体参照缺口，不扩大候选下载。"
    "用户同日进一步确认：本次商品在库内未找到时，结果直接写‘库内无参考商品’，保留对应商品名称或编码。"
    "只说明这个情况，不写成客户缺交照片、商品未上架或商品不合格；继续其他检查，不据此决定是否核销。"
    "锁定商品并按编码去重后，只读取这些商品的数据库OSS清单和对应细节图，禁止每次下载整库上千张图片再找商品，"
    "也不能先遍历全部OSS图片清单或按品牌批量取图。同一核销单跨门店、跨照片复用已验证的同一商品图片。"
    "参考图仅用于辨认产品外观；现场是否出现及是否清晰可见，必须由本次实际照片证明，"
    "不能把合同列有商品或参考图清晰当成现场已出现，不能据参考图补写模糊的现场文字。"
    "不强制现场拍清条码数字，照片取图本身不新增商品字段必交或EAN校验位审核；POS商品库一致性按另行确认的通用规则执行。"
    "仅在本类已有照片商品检查范围使用；仅检查照片有无、品牌、水印、价格等时不追加全部SKU覆盖要求，"
    "外采赠品不强制属于我方商品库。参考图缺失不是客户缺交资料；技术连接、完整性或认证故障按系统错误处理。"
)
ENTRY_SKU_CLARIFICATION = (
    "用户2026-09-22确认：条码费按每家门店核对本次产品推广协议约定的全部商品，"
    "每个对应SKU须在该店实际照片中清晰可见，允许同店多张照片合起来补齐。"
    "未拍到或看不清的商品逐店逐项说明，不能以另店照片补齐，也不能只检查已经拍到的部分商品后通过。"
    "不规定固定照片张数，不强制拍清条码数字；商品外观按共用的数据库OSS按需取图流程对照。"
)
SOURCE_TITLE = "费用核销类型-资料与标准清单-20260918.pdf"
BIZ_TYPE_NAME_CLARIFICATION = (
    "用户2026-09-21确认：业务系统bizType中的‘条码费’对应原PDF‘进场费’。"
    "用户2026-09-22最终更正并要求长期记住：进场费等于条码费，不等于入场费；不能把入场费直接归为本类。"
    "当前业务类型名称统一为‘条码费’，沿用entry_fee及audit-entry-fee的资料门禁和审核标准。"
    "原PDF及业务资料中的‘进场费’按条码费理解，不作为新bizType请求的匹配别名；审核出处保留PDF原名称。"
)
ROUTING_CLARIFICATION = (
    '用户2026-09-21最终确认：bizType为必填字符串，只允许条码费、陈列堆头、人员激励、POS达标激励、搭赠、KT板等物料制作、补差、外采赠品这八个规范值进入AI核销。'
    '程序按原值精确匹配、一一对应八个Skill，不调用AI选型，不接受旧称、同义词、内部标识、大小写变体或模糊匹配。'
    '命中后AI只核对指定Skill资料门禁；资料内容、ZIP名和文件名不能改变类型。'
    '缺少、多余及条件不明逐项列问题并正常完成，不判核销失败；门禁满足后才业务审核。'
    '其他非空bizType正常完成，不下载资料、不调用AI，回调result精确为“该业务类型无需AI核销”。'
    '缺失、null、空字符串或纯空白仍失败，回调result精确为“无法识别核销类型”。'
    '非字符串返回400。'
    '未携带bizType的历史直接input兼容入口保留内容分类，新OSS任务不能回退。'
)
RESULT_CLARIFICATION = (
    '用户2026-09-21确认：AI只列具体错误点，之后由人工复核，不决定整单或部分费用是否核销。'
    'bizType命中八个规范值后，程序固定选择对应Skill；资料缺少、多余、条件不明和业务错误均正常完成并列出具体问题，不改选类型，门禁不满足不进入业务审核。'
    '其他非空值不进入AI核销，正常完成并回调“该业务类型无需AI核销”。'
    '缺失、null或空白失败并回调“无法识别核销类型”；网络、代理、断电、程序或AI中断记failed。'
    '审核栏的“直接0核销”等后果仅保留原文出处；页面、摘要和回调一致。'
)
AUDIT_SCOPE_CLARIFICATION = (
    "用户2026-09-21最新明确：PDF核销资料栏只用于门禁，按本类要求盘点资料是否提供、条件分支是否明确；"
    "进入核销业务后，执行本类‘审核要点/标准’栏及用户明确确认的补充。用户2026-09-22新增的POS通用标准适用于全部涉及POS的核销类型；不从资料栏、页首说明或其他文字自行推导检查。"
    "资料清单保留原说明用于辨认资料角色，不能据其中的模板、签章、水印、金额等描述新增审核项，"
    "也不能把内容未核验或不合规倒推为文件未提交。其他类型的审核要点不得移用。"
    "其余文字统一作为可选业务背景，有则了解，没有不影响判断；不从背景推导缺件、拒付、0核销或其他后果。"
    "此前扩出的通用真实性/造假、POS盖章及双版一致性、红包与申请金额比较等独立项目，不再进入本轮审核；"
    "是否保留任何检查，以本类型审核栏及用户明确确认的补充为准。"
)
POS_CLARIFICATION = (
    "此前确认的POS两版资料门禁保留：要求POS的类型须有Excel版及盖章版；培训可免两版，"
    "选择提交须成套；CVS/OTC条码费可用库存表，选择POS时须两版。按实际资料内容识别，不凭文件名。"
    "按用户最新‘只看审核要点’要求，资料栏的盖章、双版描述不自动成为盖章或两版一致性的独立审核项；"
    "POS字段、计算及商品库一致性按用户2026-09-22最新确认的通用标准执行，与结算单的对应关系仍按各类已有审核项执行。"
    "用户同日进一步明确：先有Excel版，再由同一Excel增加盖章形成盖章版；数据以Excel版为准。"
    "POS字段、商品名称和69码、销量及金额取Excel原值；盖章版用于既有资料门禁，不用于覆盖Excel或另报数据冲突。"
)
POS_COMMON_CLARIFICATION = (
    "用户2026-09-22最新确认：基础通用审核项适用于所有核销方式，所有涉及POS数据的类型均执行同一标准，"
    "包括人员激励、物料制作、补差、搭赠等，不因本类审核栏未重复列出而免除。"
    "POS必含5+2项：①活动时间；②商品SKU/编码（69码）；③商品名称；④销售数量；⑤销售单价；"
    "⑥销售金额；⑦销售金额及数量合计。缺项即不完整；已提供但缺字段须指出具体缺项，不能说整份POS未交。"
    "用户同日最终明确：只必含上述5+2项，额外的产品编码及其他字段不审核；无论该编码称我方编码还是商超编号，缺失、归属不明或与库内对不上均不报错。"
    "此确认覆盖此前‘填写产品编码就必须与库内一致’的口径，不再要求辨认额外编码归属，也不以额外编码筛选或排除商品。"
    "商品名称与69码仍实际查询商品库，须唯一对应同一商品；名称允许简称和模糊匹配，明确规格、款式等不能冲突，69码须与库内原值一致，不能截断、改写或模糊匹配。"
    "销售金额仍按已确认的不小于口径复算：列示金额≥数量×销售单价，列示金额合计≥明细金额之和；数量合计须相等，按十进制原精度处理。"
    "用户明确先有Excel版，再在相同内容上加盖章；所有POS数据以Excel版为准。字段完整性、商品查库、复算、与结算单销量比较和赠品公式的实际销量均读取Excel。"
    "盖章版保留既有资料门禁，不覆盖Excel、不以它补齐Excel缺项，不另立两版差异问题；Excel不可读时说明Excel具体缺口，不能回退盖章版猜数。"
    "不以合同、结算单或库内记录补写Excel缺少的字段。"
    "仅查商品身份时读取轻量数据库字段，不访问OSS图片清单或下载商品图片；现场照片外观核对另按本次商品范围按需取图。"
    "库内未找到对应商品时直接说明‘库内无参考商品’，保留原商品名称或编码；多个候选、标识冲突和系统故障须区分，不能都说库内无商品。"
    "不涉及POS时不要求新交POS；原本无需POS的类型实际提供POS时纳入通用检查，资料按已有两版成套口径盘点。"
    "本次未恢复POS印章、两版一致性或EAN校验位的独立审核，也不新增其他基础通用项目。"
)
PAYMENT_COMPANY_CLARIFICATION = (
    "用户2026-09-22明确：人员激励大额红包所需公司名称指本次经销商公司名称，执行口径据此更正。"
    "结合本次合同中的经销商主体、结算单客户名称等原文确认对应关系，不能用产品品牌名或无法对应主体的群名、个人昵称替代经销商公司名称。"
    "截图确实没写公司名称时指出缺少经销商公司名称；已见名称但与本次经销商的对应关系不清楚时说明具体无法确认的内容。"
    "不自定必须完整工商全称或额外证明文件的要求；原PDF的文字仅保留作出处，最新执行以本次用户确认的经销商主体为准。"
)
PAYMENT_CLARIFICATION = (
    "按人员激励审核要点2及用户2026-09-22最新确认：经销商公司名称要求按每一笔实际红包金额判断，"
    "单笔超过1000元须有经销商公司名称；单笔等于或低于1000元不触发此要求，不能因多笔合计超过1000元扩大要求。"
    "金额比较按用户2026-09-22确认执行：本次实际红包合计大于等于品牌方本次核销金额即可，少于才报金额不足。用于发放人员激励的微信转账截图同样按此业务用途识别，不能仅因界面写‘转账’就改为临促工资。实际工资打款不套用红包检查。"
    "用户2026-09-21确认：存在多笔红包时，由AI根据本次资料内容判断哪些红包属于本次核销，"
    "汇总这些红包的金额，再与结算单本次核销金额比较，不要求每笔红包等于核销总额。"
    "不能为凑齐核销金额任意挑选红包；同一笔红包的重复截图只计一次，不凭金额相同就认定为同一笔。"
    "用户进一步确认跨截图补全：一图展示红包A及半个B，另一图展示一个红包时，须结合原图重叠内容和可见记录判断它是B的补充还是另一笔C；"
    "若为B的补充只计A、B两笔，若能确认是C则分别计入，不能按图片张数或可见片段数算红包笔数。"
    "先联合查看全部相关截图，再判断资料是否足够，单张截断但其他图已补全不算缺资料。"
    "同一笔付款的发出记录与收款确认属于同一交易，只计一次；结合聊天对象、上下文及可见记录匹配，"
    "不同付款即使金额相同也分别计算。分多笔的原因不作无依据推测，也不作为新增审核项目。"
    "逐笔引用金额及来源，由程序相加；归属或金额看不清时说明具体不能确认的内容，不猜数或判为未提交。"
    "资料栏‘金额>=活动申请金额’及此前围绕该资料说明的解释，在最新只看审核栏的范围下不另立审核项。"
)
BRAND_CONTRACT_CLARIFICATION = (
    "用户2026-09-22确认：八类核销统一按品牌方审核经销商申报的关系理解，品牌方出具的本次合同是批准标准的来源，"
    "经销商提供的结算单只记录本次申报，不能用其自行填写的申请值、数量、单价、金额或赠送规则证明申报符合标准。"
    "本类已有审核项需要的申请/约定数量、奖励或补差单价、金额、商品、活动门店、期间、陈列规格及赠送规则，"
    "均从对应本次活动的品牌方合同读取；条码费使用本类已要求的产品推广协议，不另加促销合同。"
    "合同提供约定标准，不证明实际已经销售、付款或赠出；实际销售、支付和现场情况仍分别读取销售明细、支付凭证和照片，"
    "结算单数值仍是待核对的申报值。结算销量仍与POS销量核对；红包合计与本次核销金额按用户2026-09-22确认的不小于关系核对。"
    "合同未写、看不清、存在冲突或不能对应本次项目时，说明具体无法确认的约定，不能改用结算单自证或用合同总额代替本次金额。"
    "本次统一的是已有审核项的基准来源，不新增审核项、必交申请表、合同字段或数量上限，也不把计划数量自动当作实际销量。"
)
AMOUNT_CLARIFICATION = (
    "用户2026-09-22确认：我们是品牌方，已有金额审核按经销商本次有效凭证金额大于等于品牌方本次核销金额判断。"
    "品牌方本次核销100元时，经销商本次凭证100元、100.1元、101元均通过，99.99元不通过；"
    "不能反写成经销商申报金额大于等于凭证金额。人员激励按本次实际各笔红包去重后求和，合计不少于结算单本次核销金额即可。"
    "合同批准额度与凭证实际付款分别核对，既有核销金额不得超过合同本次申请金额的要求保留；合同额度不冒充实际支付凭证。"
    "已有POS金额复算同样按不小于处理：列示销售金额大于等于数量乘销售单价，列示金额合计大于等于对应明细金额之和。"
    "逐字引用原始数值，用十进制原精度计算比较，不设一分钱或固定比例容差，不先四舍五入掩盖不足。"
    "用户同日最终明确：申报单价须与合同对应单价相等，不能用金额占优放宽单价或数量一致要求。"
    "最终结算金额允许抹零或四舍五入，但仅限品牌方占优：结算金额不能高于已有审核项依据约定和有效凭证核得的原精度金额；"
    "例如核得100.005元时，结算100元可接受，结算100.01元不可接受。仍比较原始金额，不先把参照值四舍五入来掩盖向上多付；不据此自创各类结算公式。"
    "除用户另行确认的POS通用复算外，不为原本没有金额比较的类型新增审核项、凭证或公式。"
)
UNIT_PRICE_CLARIFICATION = (
    "用户2026-09-22最终纠正：合同单价与结算单申报单价必须相等，覆盖此前对少报单价允许的理解。"
    "品牌方本次合同约定每件2元时，结算单申报2元才符合；1.9元、2.0001元或2.01元均不符。"
    "适用于人员激励、搭赠、补差、POS达标激励已有的奖励/补差单价核对，以及陈列堆头已有的陈列单价核对。"
    "按同一商品或项目、同一单位比较，left引用结算单申报单价，right引用品牌方本次合同约定单价，value_kind=unit_price、operator=eq。"
    "不能拿POS销售单价替代合同奖励/补差单价，不能用结算单自填的申请单价自证。"
    "只允许最终结算金额在品牌方占优方向抹零或四舍五入，不允许单价差异借此通过，也不放宽数量一致要求；最终金额按已有金额审核项的原精度参照比较，不新增无依据公式。"
    "按十进制原精度比较，不设置容差；合同缺少对应约定或单位不能确认时说明具体无法核对的内容，不猜测通过。"
)
DISPLAY_BRAND_CLARIFICATION = (
    "用户2026-09-22确认陈列品牌范围按照审核要点原文执行：第2项的申请区域内实物货品只允许参半，"
    "第3项的堆头陈列宣传物料元素允许小阔集团的参半、重点、小箭头。"
    "宣传物料的品牌范围不能扩展为实物货品允许集团品牌混摆；申请区域外相邻货架不计入混摆。"
)
STAFF_PHOTO_CLARIFICATION = (
    "用户2026-09-22明确临促照片按每店每天全量核对：本次有临促的每家门店，在合同安排临促的每一天均须有对应照片，"
    "照片带水印，画面含临促人员和产品。只确认整场活动每天有照片不能证明各门店每天均已覆盖。"
    "有无临促、临促门店及各店临促日期以品牌方本次合同中的活动申请说明和明确安排为准，先按门店列出应覆盖的日期，再对应本次实际照片。"
    "某店某日的缺口不能用其他门店或其他日期的照片补足，照片张数相等也不能代替门店与日期的对应。"
    "缺照片时列明具体门店和日期；已交照片缺水印、缺人员或产品画面时分别指出，不说整份照片资料未提交。"
    "合同安排、照片日期或门店归属看不清时说明具体无法确认的内容，不猜测缺交或通过；不能仅因没照片就认定没有临促。"
    "不另增每位临促个人逐日照片或固定张数要求，不把本规则扩展为其他核销类型都要逐日拍照。"
)
# Each numeric comparison declares what it measures. In particular, monetary
# POS arithmetic must not relax quantity totals in the same PDF audit point.
COMPARISON_OPERATORS = {
    "pos_arithmetic": {"amount": "ge", "quantity": "eq"},
    "payment_claim_amount": {"amount": "le"},
    "claim_ceiling": {"amount": "le"},
    "reward_unit_price": {"unit_price": "eq"},
    "settlement_pos_quantity": {"quantity": "eq"},
    "application_quantity_price": {"quantity": "eq", "unit_price": "eq"},
    "display_quantity_price": {"quantity": "eq", "unit_price": "eq"},
    "gift_rule_quantity": {"quantity": "eq"},
}
GIFT_CONTRACT_CLARIFICATION = (
    "用户2026-09-22确认：外采赠品数量核对以品牌方本次促销合同约定的赠送规则和数量为准。"
    "合同直接约定本次赠品数量时引用该约定；按销量计算时，实际销量来自本次POS，购买门槛、赠送比例及计算范围和单位来自合同。"
    "结算单中的赠品数量仅为经销商申报数，经销商备注的赠送规则不能替代或覆盖合同标准；发票采购数也不等于实际赠出数。"
    "合同的约定数量不能写成已实际赠出数量；缺少可核对的申报或执行数量时说明具体依据缺口，不新增发放记录作为必交项。"
    "逐笔或汇总、是否取整等算法按本次合同实际写明的规则执行，合同未说明且会影响结果时记无法核验，不自定统一公式。"
)
PERSONNEL_CONTRACT_CLARIFICATION = (
    "人员激励审核栏已有的申请/活动约定参照值，按用户已确认来源从本次促销合同读取；"
    "结算单提供本次实际申报数量、奖励单价、销量和核销金额，不能用结算单自证批准数值。"
    "不新增独立申请表；合同未写、看不清或金额对应不明确时，针对已有审核项说明参照缺口，"
    "不以合同总额自动代替本次申请金额。这仅说明原有标准的数据来源，不增加合同模板或签章检查；"
    "其他类型按用户2026-09-22确认的品牌方合同基准原则执行各自已有审核项。"
)
PERSONNEL_CONTRACT_COMPARISON_RULES = frozenset({
    "application_quantity_price", "reward_unit_price", "claim_ceiling",
})
SKU_CONTRACT_SCENARIOS = frozenset({
    "giveaway_promotion", "price_difference_support", "pos_target_incentive",
})
SKU_CONTRACT_CLARIFICATION = (
    "用户2026-09-21确认：搭赠、补差、POS达标激励的本次申请金额及对应商品的奖励或补差单价，"
    "均从本次促销合同读取，与结算单本次核销金额及对应商品申报单价比较。"
    "商品按实际条码对应，不能用结算单自填的申请金额、申请单价或POS销售单价代替合同约定。"
    "合同未写、看不清或不能确定对应本次活动及商品时，说明具体无法确认的内容，"
    "不能把合同总额自动作为本次申请金额，也不新增独立申请表。"
    "此确认只说明本类审核第1项①③已有比较的数据来源，不增加合同模板或签章检查，"
    "其余已有审核项需要的约定值按用户2026-09-22确认从品牌方合同读取；结算销量仍按原审核要点与POS销量比较。"
)
CONTRACT_COMPARISON_RULES = {
    "personnel_incentive": PERSONNEL_CONTRACT_COMPARISON_RULES,
    **{scenario: frozenset({"application_quantity_price", "reward_unit_price", "claim_ceiling"})
       for scenario in SKU_CONTRACT_SCENARIOS},
    "promotional_display": frozenset({"display_quantity_price"}),
    "self_procured_gift_material": frozenset({"gift_rule_quantity"}),
}
# A successful visual/scope comparison needs the brand's reference as well as
# the execution evidence. Missing visible fields can still fail independently.
CONTRACT_REFERENCE_RULES = {
    "promotional_display": frozenset({"display_watermark", "display_brand", "display_size"}),
    "personnel_incentive": frozenset({"staff_daily_photos"}),
    "giveaway_promotion": frozenset({"giveaway_evidence"}),
    "self_procured_gift_material": frozenset({"gift_finished_photos"}),
    "entry_fee": frozenset({"entry_sku", "entry_watermark"}),
}
INVOICE_DETAILS_CLARIFICATION = (
    "用户2026-09-23确认：KT板等物料制作的发票或收据明细允许结合本次合同、结算单认定齐全。"
    "票据只写‘物料制作’和总金额，而本次合同、结算单已能确认对应的具体制作项目时，"
    "不得仅因票据未逐项列出而报缺少明细，也不要求另补一份票据明细附件。"
    "先读取票据原文，再联合读取能对应本次制作业务的合同、结算单；保留各文件实际记载的事实，"
    "不能把合同或结算单内容写成票据原文，source_ids须包含本次票据及实际用于补充明细的来源。"
    "联合读取后确实没有明细时说明具体缺项；内容看不清、冲突或不能确认对应关系时说明具体无法确认之处，不猜测齐全。"
    "本项仅解释KT物料已有票据明细检查，不免除发票或收据资料及其抬头、金额、开票时间检查，"
    "不新增制作数量、单价、金额比较或固定明细字段，也不扩展外采赠品等其他类型的票据审核。"
)
TEMPLATE_CLARIFICATION = (
    "用户2026-09-21确认：陈列堆头、KT板等物料制作的结算单及条码费的产品推广协议必须使用公司指定模板。"
    "用户2026-09-22已明确指定input根目录八个ZIP内的相应样张作为模板参考，已分别提取并写入各自Skill的references/template-reference.md，"
    "不再等待另交公司空白范本。陈列使用合同PDF原第2页结算单，KT使用独立结算单，条码费使用四页产品推广协议（原件2026V2）。"
    "须对照指定参考核对固定版式、栏目及条款；客户自行调整排版或栏目位置，即使栏目和内容齐全也不符合要求，"
    "不能以信息齐全代替使用规定模板。正常填写的主体、业务数值、商品和门店不要求与样张相同；扫描角度、缩放或旋转不是擅改模板。"
    "模板只提供版式参考，样本金额、门店、日期和签章不证明本次业务，不进入本次来源或数值比较；不能把其他未指定历史文件自行当标准。"
    "指定参考无法读取、看不清或无法确认适用版本时，模板是否符合要求记无法核验，说明具体对照缺口，"
    "不能猜测通过或认定客户用错模板，也不能算成客户缺交资料或要求客户每单另交空白模板。"
    "只解释这三类审核栏已有的模板要求，不扩展其他类型，也不改变原有盖章检查。"
)
SEAL_CLARIFICATION = (
    "用户2026-09-21确认：审核要点要求盖章或签章但未限定章的种类时，默认有盖章即可，"
    "不额外要求必须是公章、财务专用章或合同专用章；后续业务明确调整时再修改。"
    "已能确认盖章时，不因无法区分章的种类认定不合格或无法核验；盖章本身看不清时说明具体情况。"
    "原文明写的客户盖章、双方盖章仍按原要求核对，不能把双方盖章改为任意一方盖章。"
    "本口径仅解释已有签章审核，不新增其他类型的印章检查，不因缺章把已交文件说成未交。"
)
ATRIUM_CLARIFICATION = (
    "用户2026-09-23确认：AI分析接口新增largeVenueFee，表示是否包含大型活动场地费，取值为布尔值true或false。"
    "陈列堆头直接按该字段确定是否需要商场入场协议：true时必交，false时不要求提供；内部atrium原样采用该布尔值。"
    "字段已传时不再由AI判断活动规模，不以照片观感、位置、面积、人数或费用覆盖字段，也不另加中庭位置条件。"
    "字段只确定适用条件，协议是否提交仍读取本次资料，不凭字段生成协议存在的证据。其他核销类型不因此新增资料项。"
    "兼容未传字段的旧调用，沿用此前资料明确证明的中庭大型活动条件：明确属于时atrium=true，明确不属于时false，不能确认时null；不默认false。"
    "适用条件未明确时说明暂时无法确定是否需要入场协议，不直接报缺少协议，也不猜测豁免。"
    "需要但未交协议时列出‘缺少商场入场协议’，正常完成资料检查，不记核销失败。"
)
DISPLAY_SIZE_CLARIFICATION = (
    "用户2026-09-21确认：陈列堆头申请的尺寸、规格从本次促销合同读取，再与本次对应的现场照片核对。"
    "保留合同实际写明的规格含义和单位，不用结算单或照片反推申请尺寸，不自行给‘标准堆头’补固定尺寸。"
    "由AI结合合同约定及照片可见事实判断；合同未写清、看不清、对应项目不明或照片不足以判断时，"
    "说明具体无法确认的内容，不猜尺寸，也不直接判尺寸不符。"
    "不另增申请表或专门测量照片作为必交资料，不自定面积、列数或误差阈值。"
    "用户2026-09-22进一步确认：本类已有审核需要的数量、单价、门店和执行期间同样从品牌方本次合同读取。"
)
ENTRY_ADDRESS_DUPLICATE_CLARIFICATION = (
    "用户2026-09-21确认：条码费（原PDF进场费）审核第3项的‘存量是否重复’，检查本次核销单照片水印中的门店地址，"
    "不是查商品以前是否上架、费用是否申请或核销过。"
    "用户2026-09-22进一步确认：允许同店多图；已确认属于同一家门店的照片可以展示不同商品、货架或角度，"
    "水印地址相同不报重复。只有不同申报门店使用同一水印地址时才提示核对。"
    "先结合本次产品推广协议的门店清单及实际照片内容确认各图归属，按门店归组，再比较不同申报门店的水印地址。"
    "不能仅凭地址相同就合并为同店，也不能只用文件夹名或文件名证明归属；不新增照片店名或其他必填字段。"
    "不同门店地址相同时列明对应申报门店、实际相同地址及全部相关来源；归属不能确认时说明具体哪几张照片无法对应门店，"
    "不能猜测为同店后通过，也不能直接报不同门店地址重复。"
    "不能只因同一城市或同一道路就认定同一门店地址；水印模糊或地址不全时说明具体不能确认的内容。"
    "只报不同申报门店使用相同地址的事实，不据此推断重复核销或造假，不要求客户补交历史上架、申请或核销记录。"
    "用户进一步确认：比较范围仅限本次核销单，不读取或比较其他核销单、历史提交的照片；"
    "即使同批处理多张核销单，也须逐单独立查重，不能把不同单据的地址混在一起。"
    "没有其他核销单或历史照片不属于资料缺失或无法核验的原因。"
)
PHOTO_WATERMARK_CLARIFICATION = (
    "用户2026-09-21确认：搭赠、补差、外采赠品已有照片审核中的水印，均须包含拍摄时间和门店地址，缺一即不完整。"
    "具体指出哪份照片缺哪项；看不清时说明无法确认，不把已交照片说成未交。"
    "补差的满减证明采用现场照片时同样适用；采用小票等其他证明时不套用照片水印要求，线上小象超市截图的既有例外保留。"
    "本次只明确水印的组成，不新增日期范围、地址一致性、定位距离或固定水印样式的检查，"
    "也不另立外采赠品门店现场照片的独立审核项，不扩展KT或条码费的水印要求；人员临促照片按用户2026-09-22单独确认执行。"
)
GIVEAWAY_EVIDENCE_CLARIFICATION = (
    "用户2026-09-21明确搭赠审核第2项的‘关键信息’：全量活动照片或小票二选一。"
    "照片须带水印，展示搭赠现场或消费者参与，二者展示任一即可，不强制同时出现。"
    "小票须显示购买产品、搭赠产品、购买时间、门店，四项均须能确认。"
    "用户2026-09-22进一步确认：每家门店可分别选择照片或小票，同一次搭赠核销允许不同门店混合提交。"
    "各店符合对应要求的证明合起来覆盖本次活动门店即可，不要求整单统一一种，也不要求每一种证明单独覆盖全部门店。"
    "两类来源共同归入同一个搭赠活动证明资料项，混合提交本身不算多交，不能因甲店交照片就要求乙店也交照片。"
    "照片、小票分别按各自已有要求检查；不要求两类同时提交，不把小票四项套作每张照片的字段要求，也不要求小票带水印。"
    "全量按本次已明确的活动范围核对，不自定固定照片张数或每笔交易必交小票的要求，不能仅凭局部照片推定全量。"
    "同一张小票的多张图片可相互补全，先合并查看再判断缺项，不能用另一笔小票的信息补缺。"
    "用户另已确认：照片水印须包含拍摄时间和门店地址，缺一即不完整。"
    "已交照片或小票但缺水印、画面或字段时，逐项说明具体缺少或看不清的内容，不说文件未提交。"
    "这是用户对本类已有审核要点的明确解释，不表示可以把其他资料栏描述自动扩成审核标准。"
)


@dataclass(frozen=True)
class Requirement:
    id: str
    text: str
    when: str = "always"


@dataclass(frozen=True)
class Rule:
    id: str
    text: str
    effect: str = "missing_material"
    when: str = "always"
    numeric: bool = False
    source: str = ""


FLAGS = (
    "training", "temporary_staff", "atrium", "cvs_otc", "online",
    "full_reduction", "uses_pos", "photo_evidence", "red_packet",
)

# Display names only; this table does not add material or audit requirements.
MATERIAL_LABELS = {
    "pos": "盖章版销售明细（POS）", "pos_excel": "Excel版销售明细（POS）",
    "settlement": "结算单", "promotion_contract": "促销合同", "payment": "红包或工资打款截图",
    "staff_photos": "临促每天的活动照片", "display_photos": "陈列活动照片",
    "atrium_agreement": "商场入场协议", "invoice": "发票或收据", "finished_photos": "物料成品照片",
    "gift_rule": "活动赠送规则", "gift_finished_photos": "外采赠品成品照片",
    "gift_store_photos": "活动门店现场照片", "price_evidence": "活动价格证明",
    "full_reduction_evidence": "满减活动证明", "giveaway_evidence": "搭赠活动照片或小票",
    "entry_agreement": "产品推广协议", "entry_system": "进场商品销售明细或库存表",
    "shelf_photos": "商品上架照片",
}

BASE_MATERIALS = (
    Requirement("settlement", "结算单（公司统一模板，客户盖章）"),
    Requirement("promotion_contract", "签章后的促销合同"),
)
POS_MATERIAL = Requirement("pos", "经销商盖章 POS 数据（客户公章或财务章的盖章版，与Excel版完整内容一致）")
POS_EXCEL = Requirement("pos_excel", "POS 明细 Excel 版（与盖章版成套提供，完整内容一致）")
OPTIONAL_POS = (Requirement("pos", POS_MATERIAL.text + "；实际涉及POS时提供", "uses_pos"),
                Requirement("pos_excel", POS_EXCEL.text + "；实际涉及POS时提供", "uses_pos"))
GIFT_RULE = Requirement("gift_rule", "核销时备注清楚活动赠送规则")
INVOICE = Requirement("invoice", "发票或收据，抬头为公司名称，含明细、金额、开票时间")

MATERIALS = {
    "promotional_display": (
        *OPTIONAL_POS,
        Requirement("display_photos", "活动期间带地址、拍摄时间水印的照片，清晰展示全貌、产品摆放、促销标识"),
        Requirement("atrium_agreement", "商场入场协议（largeVenueFee=true时必交，false时不要求提供；未传字段的旧调用沿用中庭大型活动条件）", "atrium"),
        *BASE_MATERIALS,
    ),
    "personnel_incentive": (
        Requirement("pos", POS_MATERIAL.text + "；培训费可不提供", "not_training"),
        Requirement("pos_excel", POS_EXCEL.text + "；培训费可不提供", "not_training"),
        Requirement("payment", "红包截图或临促工资打款截图（有无临促读取本次促销合同中的活动申请说明确认）"),
        Requirement("staff_photos", "各临促门店在本次安排临促的每一天的水印照片，含临促人员和产品（有无临促及门店日期安排读取本次促销合同中的活动申请说明确认）", "temporary_staff"),
        *BASE_MATERIALS,
    ),
    "poster_material": (
        *OPTIONAL_POS,
        INVOICE,
        Requirement("finished_photos", "成品水印照片，地址和拍摄时间，展示尺寸、内容、展示位置"),
        *BASE_MATERIALS,
    ),
    "self_procured_gift_material": (
        INVOICE, GIFT_RULE, POS_MATERIAL, POS_EXCEL,
        Requirement("gift_finished_photos", "包含外采赠品的成品水印照片"),
        Requirement("gift_store_photos", "活动期间所有门店现场水印照片，清晰展示促销内容和客户自采物料"),
        *BASE_MATERIALS,
    ),
    "price_difference_support": (
        POS_MATERIAL, POS_EXCEL,
        Requirement("price_evidence", "所有活动门店清晰展示活动价格的现场水印照片，或线上小象超市截图"),
        Requirement("full_reduction_evidence", "满减返还活动存在证明（现场水印照片、小票等，须对应本次费用，可与价格证明共用同一来源）"),
        *BASE_MATERIALS,
    ),
    "giveaway_promotion": (
        POS_MATERIAL, POS_EXCEL, GIFT_RULE,
        Requirement("giveaway_evidence", "活动水印照片或小票按门店二选一，允许不同门店混合提交并共同覆盖全量活动门店，合并计为同一资料项；照片展示搭赠现场或消费者参与；小票显示购买产品、搭赠产品、购买时间、门店"),
        *BASE_MATERIALS,
    ),
    "entry_fee": (
        Requirement("entry_agreement", "产品推广协议（公司统一标准模板，双方盖章）"),
        Requirement("entry_system", "带公司进场产品的 POS 或终端库存表（适用于 CVS/OTC 渠道；选择POS时由下列POS两版共同证明）", "cvs_otc"),
        *OPTIONAL_POS,
        Requirement("shelf_photos", "日期、地址、店名水印照片，清晰可辨具体 SKU"),
    ),
    "pos_target_incentive": (
        POS_MATERIAL, POS_EXCEL,
        *BASE_MATERIALS,
    ),
}

# Shared POS checks were explicitly extended to all eight types by the user.
POS_RULES = (
    Rule("pos_fields", "以POS Excel版为准，只检查5+2项：活动时间、商品SKU/编码（69码）、商品名称、销售数量、销售单价、销售金额及销售金额和数量合计，缺项即不完整。额外产品编码及其他列不审核。", when="uses_pos"),
    Rule("pos_arithmetic", "用户2026-09-22确认：POS列示销售金额须大于等于数量×销售单价，列示金额合计须大于等于对应明细金额之和；数量合计仍须相等。逐字引用原始数值，由程序按十进制原精度复算，不用舍入或容差掩盖不足。", when="uses_pos", numeric=True),
    Rule("pos_product_identity", "以POS Excel中的商品名称加69码查询商品库，名称允许模糊匹配且须唯一对应，69码须准确一致。额外产品编码即使与我方库不一致也不审核、不报错、不参与定位。缺失字段不从库内或盖章版补写；库内没有对应商品时说明‘库内无参考商品’并保留商品名称或69码。", when="uses_pos"),
)
SKU_RULES = (
    Rule("reward_unit_price", "用户2026-09-22最终确认：结算单申报的奖励/补差单价须等于品牌方本次合同对应69码的约定单价；少于或高于均不符，不将POS零售价当作奖励单价，最终金额抹零不能放宽单价一致要求。", "zero", "not_training", True),
    Rule("settlement_pos_quantity", "结算单每个SKU销售数量与POS Excel版对应销售数量一致；不能用盖章版数值覆盖Excel。", "zero", "not_training", True),
    Rule("claim_ceiling", "结算单本次核销金额小于等于申请金额。", "missing_material", "always", True),
)
QUANTITY_PRICE_RULE = Rule("application_quantity_price", "数量、单价明确填错时记录具体错误点，交由人工复核；按本类审核栏已明确的数量、单价及对应资料核对。用户2026-09-22最终确认：申报单价必须等于对应合同约定，少于也不符；数量仍按原有一致要求核对。仅最终结算金额允许在品牌方占优方向抹零或四舍五入。不得把实际销量低于计划自动认定填错，不增加其他数量或价格比较。", "zero", "uses_pos", True)
SETTLEMENT_TEMPLATE_RULE = Rule("settlement_template_seal", "结算单非公司统一模板或未由客户盖章，按缺资料处理；仅本类审核栏明确要求时执行。" + TEMPLATE_CLARIFICATION + SEAL_CLARIFICATION)

RULES = {
    "promotional_display": (
        *POS_RULES,
        Rule("display_watermark", "照片水印包含拍摄日期和地址；拍摄日期在品牌方本次合同的执行周期内，地址与合同约定的活动门店一致，缺一即不完整。约定看不清或无法对应本次时说明参照缺口，不以结算单或照片自证约定。"),
        Rule("display_brand", "申请的堆头或陈列大小范围内实物货品只能存在参半品牌，不能有其他产品；不把申请区域外相邻货架算作混摆。" + DISPLAY_BRAND_CLARIFICATION),
        Rule("display_material_brand", "堆头陈列宣传物料元素为小阔集团产品（参半、重点、小箭头）；此处物料元素的允许范围不改变第2项实物货品仅参半的要求。"),
        Rule("atrium_agreement", "包含大型活动场地费但没有商场入场协议，按缺资料处理。" + ATRIUM_CLARIFICATION, when="atrium"),
        SETTLEMENT_TEMPLATE_RULE,
        Rule("display_quantity_price", "数量、单价等明细按同一项目、含义及单位一致的原值核对。用户2026-09-22最终确认：结算单申报单价必须等于品牌方本次合同约定，少于也不符；数量仍按原有一致要求核对。仅最终结算金额允许在品牌方占优方向抹零或四舍五入。明确不符时记录具体错误点，交由人工复核。", "zero", numeric=True),
        Rule("display_size", "按申请的堆头大小与照片判断是否符合。" + DISPLAY_SIZE_CLARIFICATION),
    ),
    "personnel_incentive": (
        *POS_RULES, *SKU_RULES,
        Rule("payment_company", "逐笔核对红包：单笔金额超过1000元须注明本次经销商公司名称，缺名称按缺资料处理；单笔等于或低于1000元不强制名称，不因多笔合计超过1000元追加名称要求。先结合全部相关截图判断每笔的完整信息，金额看不清不能按小额免除。" + PAYMENT_COMPANY_CLARIFICATION, when="red_packet"),
        Rule("payment_claim_amount", "根据资料内容判断属于本次核销的红包，将各笔金额去重后合计。用户2026-09-22确认：本次红包合计须大于等于结算单本次核销金额，少于时报告金额不足；多于或相等均通过本项。不把每笔红包分别与核销总额比较，不以合同申请金额代替本次核销金额；归属或金额无法确认时说明具体原因，不猜测通过。", when="red_packet", numeric=True),
        Rule("staff_daily_photos", "临促照片按每家门店、每一天全量核对；缺少任一门店任一临促日的照片，或照片缺水印、临促人员或产品画面，逐项记录具体问题。" + STAFF_PHOTO_CLARIFICATION, when="temporary_staff"),
        QUANTITY_PRICE_RULE,
    ),
    "poster_material": (
        *POS_RULES,
        Rule("invoice_details", "发票/收据抬头为经销商公司，并有金额和开票时间；制作明细可结合本次合同、结算单确认，联合核对后仍缺少时具体说明。" + INVOICE_DETAILS_CLARIFICATION),
        Rule("finished_photo", "未提供成品照片，按缺资料处理；本条仅检查成品照片是否提供，不从资料栏扩出水印、尺寸、内容或展示位置审核。"),
        SETTLEMENT_TEMPLATE_RULE,
    ),
    "self_procured_gift_material": (
        Rule("gift_rule_present", "核销时未备注赠送规则，按缺资料处理。"),
        *POS_RULES,
        Rule("gift_rule_quantity", "按品牌方本次合同的赠送规则及数量约定核对经销商申报的赠品数量；不能自定公式，也不能把发票采购数当作实际赠出数。" + GIFT_CONTRACT_CLARIFICATION, numeric=True),
        Rule("gift_finished_photos", "按门店提供全量成品水印照片，包含外采赠品；缺少按缺资料处理。不从资料栏另加促销内容、客户自采物料或活动期间检查。" + PHOTO_WATERMARK_CLARIFICATION),
        Rule("settlement_seal", "结算单未由客户盖章，按缺资料处理；本类审核栏未要求统一模板，不检查模板。" + SEAL_CLARIFICATION),
        Rule("contract_signed", "促销合同未签章，按缺资料处理。" + SEAL_CLARIFICATION),
    ),
    "price_difference_support": (
        *POS_RULES, *SKU_RULES,
        Rule("full_reduction_evidence", "满减返还须有现场水印照片、小票等活动存在证明；无对应证明按缺资料处理。" + PHOTO_WATERMARK_CLARIFICATION),
        Rule("price_evidence", "门店照片缺水印或活动价格不清晰，按缺资料处理；线上小象超市截图不套用线下水印格式。不从资料栏另加门店全量覆盖审核。" + PHOTO_WATERMARK_CLARIFICATION),
        QUANTITY_PRICE_RULE,
    ),
    "giveaway_promotion": (
        *POS_RULES, *SKU_RULES,
        Rule("giveaway_evidence", "活动照片与小票均无，或关键信息缺失，按缺资料处理。" + GIVEAWAY_EVIDENCE_CLARIFICATION),
        QUANTITY_PRICE_RULE,
    ),
    "entry_fee": (
        *POS_RULES,
        Rule("entry_agreement", "产品推广协议非公司统一模板或未双方盖章，按缺资料处理。" + TEMPLATE_CLARIFICATION + SEAL_CLARIFICATION),
        Rule("entry_sku", "照片中的具体SKU须可辨识并与品牌方本次产品推广协议约定的商品一致；已交照片但商品看不清时如实说明，不说照片未交。协议约定不明时说明具体参照缺口。" + ENTRY_SKU_CLARIFICATION),
        Rule("entry_watermark", "水印中的门店地址与品牌方本次产品推广协议约定的门店地址一致；约定不明时说明参照缺口，不从资料栏增加日期、店名必填或活动周期检查。"),
        Rule("entry_duplicate", ENTRY_ADDRESS_DUPLICATE_CLARIFICATION),
    ),
    "pos_target_incentive": (*POS_RULES, *SKU_RULES, QUANTITY_PRICE_RULE),
}

# Literal audit-column point numbers allow every runtime check to be traced.
RULE_POINTS = {
    "promotional_display": {"display_watermark": "1", "display_brand": "2", "display_material_brand": "3", "atrium_agreement": "4", "settlement_template_seal": "5", "display_quantity_price": "6", "display_size": "7"},
    "personnel_incentive": {"pos_fields": "1", "pos_arithmetic": "1", "reward_unit_price": "1①", "settlement_pos_quantity": "1②", "claim_ceiling": "1③", "payment_company": "2", "payment_claim_amount": "2", "staff_daily_photos": "3", "application_quantity_price": "4"},
    "poster_material": {"invoice_details": "1", "finished_photo": "2", "settlement_template_seal": "3"},
    "self_procured_gift_material": {"gift_rule_present": "1", "pos_fields": "2", "pos_arithmetic": "2", "gift_rule_quantity": "2", "gift_finished_photos": "3", "settlement_seal": "4", "contract_signed": "4"},
    "price_difference_support": {"pos_fields": "1", "pos_arithmetic": "1", "reward_unit_price": "1①", "settlement_pos_quantity": "1②", "claim_ceiling": "1③", "full_reduction_evidence": "2", "price_evidence": "3", "application_quantity_price": "4"},
    "giveaway_promotion": {"pos_fields": "1", "pos_arithmetic": "1", "reward_unit_price": "1①", "settlement_pos_quantity": "1②", "claim_ceiling": "1③", "giveaway_evidence": "2", "application_quantity_price": "3"},
    "entry_fee": {"entry_agreement": "1", "entry_sku": "2", "entry_watermark": "3", "entry_duplicate": "3（存量判重）"},
    "pos_target_incentive": {"pos_fields": "1", "pos_arithmetic": "1", "reward_unit_price": "1①", "settlement_pos_quantity": "1②", "claim_ceiling": "1③", "application_quantity_price": "2"},
}

MATERIAL_RULE_IDS = {
    "pos_excel": "pos_fields", "staff_photos": "staff_daily_photos", "display_photos": "display_watermark",
    "atrium_agreement": "atrium_agreement", "invoice": "invoice_details", "finished_photos": "finished_photo",
    "gift_rule": "gift_rule_present", "gift_finished_photos": "gift_finished_photos",
    "price_evidence": "price_evidence", "full_reduction_evidence": "full_reduction_evidence",
    "giveaway_evidence": "giveaway_evidence", "entry_agreement": "entry_agreement", "shelf_photos": "entry_watermark",
}


def applicability(when: str, scenario: str, flags: dict) -> bool | None:
    if when == "always":
        return True
    if when == "not_training":
        if scenario != "personnel_incentive":
            return True
        return not flags["training"] if flags.get("training") is not None else None
    if when == "required_pos":
        if scenario == "personnel_incentive":
            return applicability("not_training", scenario, flags)
        return scenario in {"self_procured_gift_material", "price_difference_support", "giveaway_promotion", "pos_target_incentive"}
    if when == "entry_pos":
        if flags.get("cvs_otc") is False:
            return False
        if flags.get("cvs_otc") is None:
            return None
        return flags.get("uses_pos")
    return flags.get(when)


def requirements(scenario: str) -> tuple[Requirement, ...]:
    return MATERIALS[scenario]


def rules(scenario: str) -> tuple[Rule, ...]:
    from .scenario_registry import SCENARIO_LABELS
    common_ids = {r.id for r in POS_RULES}
    if {r.id for r in RULES[scenario]} != set(RULE_POINTS[scenario]) | common_ids:
        raise ValueError("审核项目须逐项对应本类PDF审核要点，不能从资料清单生成")
    source_label = "进场费" if scenario == "entry_fee" else SCENARIO_LABELS[scenario]
    result = tuple(replace(r, source=("用户2026-09-22确认·所有核销方式POS通用标准" if r.id in common_ids
                                     else f"PDF第1页·{source_label}·审核要点{RULE_POINTS[scenario][r.id]}"))
                   for r in RULES[scenario])
    if scenario in CONTRACT_COMPARISON_RULES:
        result = tuple(replace(rule, text=rule.text +
            " 用户2026-09-22确认：批准的数量、单价或金额参照值从品牌方本次促销合同读取，"
            "不能取结算单自行填写的申请值或POS销售单价代替约定；合同未写、看不清或对应不明时记无法核验，"
            "不以合同总额自动代替本次申请金额。")
            if rule.id in CONTRACT_COMPARISON_RULES[scenario] and rule.id != "gift_rule_quantity" else rule for rule in result)
    if scenario == "poster_material":
        result = tuple(replace(rule, source=rule.source + "及用户2026-09-23确认的合同、结算单补充明细口径")
                       if rule.id == "invoice_details" else rule for rule in result)
    return result


def catalogue() -> dict:
    from .scenario_registry import SCENARIO_LABELS
    return {"version": POLICY_VERSION, "source": SOURCE_TITLE, "page": 1,
            "clarifications": [BIZ_TYPE_NAME_CLARIFICATION, RESULT_CLARIFICATION, POS_CLARIFICATION, POS_COMMON_CLARIFICATION, PAYMENT_CLARIFICATION, PERSONNEL_CONTRACT_CLARIFICATION,
                               SKU_CONTRACT_CLARIFICATION, BRAND_CONTRACT_CLARIFICATION, GIFT_CONTRACT_CLARIFICATION, AMOUNT_CLARIFICATION,
                               UNIT_PRICE_CLARIFICATION, DISPLAY_BRAND_CLARIFICATION, STAFF_PHOTO_CLARIFICATION, PAYMENT_COMPANY_CLARIFICATION,
                               AUDIT_SCOPE_CLARIFICATION, ATRIUM_CLARIFICATION, PRODUCT_PHOTO_CLARIFICATION,
                               INVOICE_DETAILS_CLARIFICATION],
            "types": {key: {"label": SCENARIO_LABELS[key],
                            "materials": [asdict(r) for r in value],
                            "audit_points": [asdict(r) for r in rules(key)]}
                      for key, value in MATERIALS.items()}}


def material_catalogue() -> dict:
    """Only the PDF material column is used to compare candidate types."""
    policy = catalogue()
    return {**policy, "types": {
        scenario: {"label": entry["label"], "materials": entry["materials"]}
        for scenario, entry in policy["types"].items()
    }}
