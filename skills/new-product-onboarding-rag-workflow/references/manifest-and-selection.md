# 观察清单与选择规则

每个直接商品文件夹对应一条清单记录。写入记录前检查图片。来源目录是不可变输入；`match`、`validate` 和 OSS 发布
都不得重命名、移动、删除或编辑来源目录。

## 必需字段

- `source_folder`：`--source-root` 的一个直接子目录；不得为嵌套或绝对路径。
- `source_id`：整个目录中唯一的稳定小写标识符。
- `brand`：目录中使用的可见品牌。
- `observed_product_name`：包装上的文字，不能使用数据库或 Excel 措辞。
- `observed_specification`：可见净含量或包装数量。
- `observed_variant`：可见香型、颜色、口味或其他变体；不存在时使用 `null`。
- `barcode_69`：条码下印刷的完整 13 位数字。
- `name_evidence_files`：一张或多张能读清观察名称的文件。
- `barcode_evidence_files`：一张或多张能读清完整条码数字的文件。

可选的 `approved_product_code` 只可用于已由人工确认的同条码候选。它不能引入数据库同码记录中不存在的编码，
也不能覆盖名称/品类/规格冲突。

## 示例结构

```json
{
  "schema_version": "1.0",
  "observations": [
    {
      "source_folder": "1",
      "source_id": "sample-001",
      "brand": "示例品牌",
      "observed_product_name": "示例保湿沐浴露",
      "observed_specification": "500ml",
      "observed_variant": "梨花香",
      "barcode_69": "69xxxxxxxxxxx",
      "name_evidence_files": ["front.jpg"],
      "barcode_evidence_files": ["back.jpg"]
    }
  ]
}
```

执行前将占位条码替换为实际可见且有效的 EAN-13。

## 候选优先级

脚本按以下顺序应用规则：

1. 返回条码完全一致，且三个字段有效、非空；
2. 品类及可见规格相容；
3. 存在唯一的可见变体/香型；
4. 唯一一个不含未观察到的专用、物流、升级、渠道或代言人修饰词的候选；
5. 模糊名称明显领先，并有安全差距；
6. 否则保持未解决。

仅在防止标准零售包装被静默映射到特殊候选时，才利用修饰词缺失；
这不代表可以推断任何不可见的包装事实。
