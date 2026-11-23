---
name: read-audit-spreadsheet
description: 读取核销Excel原生单元格、全部工作表及内嵌图片，定位表头、公式缓存和图片来源。适用于XLSX、XLSM、XLS，不负责决定核销结论。
---

# 读取核销 Excel

需要读Excel时使用本Skill。不要现场编写解析或转换代码；正常资料上传已自动调用同一底层工具，无需再读一次。

在项目根目录执行固定入口：

```powershell
python -B skills/read-audit-spreadsheet/scripts/read.py --input "销售明细.xlsx" --output "独立的新输出目录"
```

工具不调用模型、不回调、不改原件。终端仅返回读取索引路径、数量及限制；按当前审核项读取索引中相关Sheet、行或图片，不能把整份清单重新喂给模型。ZIP包先走正常安全解包，再按实际文件格式选择读取工具。

- 全部Sheet（含隐藏表）、已存行列和图片均须留档；不把首页、预览或部分数据当完整明细。
- 原生文字、条码、数字和公式缓存用程序结果。合并单元格记录范围和锚点，不给空格自动填数字；不执行公式、宏或外链。
- 特殊分隔符、换行及控制字符由固定工具使用JSON单元格编码，调用共享解析器还原原值；不要手工按竖线切列、重复反转义或把转义后的引用当成新的业务文字。前导零、引号、反斜杠与数字原精度必须保留。
- 每张浮动图片、WPS单元格图片分别保留位置；同图多处使用仍分别留来源。表格作者标注不能冒充照片可见文字。
- `<file_id>-workbook-reading.json`记录各Sheet状态、表块行范围及图片unit_id。按此核对覆盖，再调用既有POS复算或商品查库工具，不让模型手算整表。

只需检查POS电子版5+2字段时，调用现成工具，不逐行重写盘点结论：

```powershell
python -B skills/orchestrate-offline-audit/scripts/audit_tools.py --case "本单case.json" pos_field_inventory
```

`complete=true`表示程序已扫描全部对应原生行，保留各表头、活动时间、明细范围和合计来源，可沿用`check`。`complete=false`只表示当前布局尚不能由程序完整证明，继续按原资料审核，不据此报缺件或失败。该工具不替代商品身份、金额差异及活动周期对齐。长单正常流程会自动复用这一盘点，不重复调用；没有本单已保存case时不自行拼造资料或扫描其他核销单。

遇到商品名称和69码横排在相邻两行、下面各行是门店销量的矩阵，正常流程先按原列头提取商品身份并继续查库，无需逐店重复抄名称与条码。需要检查绑定时调用：

```powershell
python -B skills/orchestrate-offline-audit/scripts/audit_tools.py --case "本单case.json" pos_matrix_identity
```

工具只返回SKU列的原名、原69码和来源，不重新发送整张矩阵。各文件、Sheet及表块独立绑定，所有原行仍保留；多级表头中的明确合计列可按原列位置关联，不能据此补价格或金额。名称/条码错位、缺项、跨读取单元或存在未解释行时，保留原模型读取路径，不按列排序猜配或把未支持布局写成客户缺件。

仅在模型读取表内照片时加载[表内图片读取约定](references/runtime.md)。需要业务结论时回到本次已选核销Skill，不在读取阶段判断通过或缺件。

遇到几万行、宽表、错误范围或资源问题时，按[大表读取边界](references/large-files.md)处理，不提前把这份排查说明加载到普通审核阶段。

仅维护兼容性时，可用项目的`skills/orchestrate-offline-audit/scripts/check_material_corpus.py --input "原包目录" --output "新检查目录"`去重检查留存原件。该工具逐包逐文件复用正常上传读取器，保留关联及问题并自动清理转换副本；不调用模型。单笔核销不能因此扫描或加载其他核销单。
