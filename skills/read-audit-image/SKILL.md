---
name: read-audit-image
description: 准备核销照片或截图的完整原图与来源索引，并按需提取可见事实。适用于PNG、JPEG、JFIF、WebP等图片；不凭文件名推断业务内容。
---

# 读取核销图片

需要准备图片或定位读取问题时使用本Skill。正常上传已准备原图时，直接按当前unit_id使用，不重复调用工具。

在项目根目录执行：

```powershell
python -B skills/read-audit-image/scripts/read.py --input "现场照片.png" --output "独立的新输出目录"
```

该命令只校验、复制完整图片并生成来源索引，不执行OCR或模型识别，不作核销判断。不得把它执行成功说成已识别水印、价格或印章。可见内容由现有模型读取阶段按[图片读取约定](references/runtime.md)和当前Schema提取，无须现场编写图片处理代码。

逐张使用完整原图；同图多个来源保留对应位置。不截掉水印、价签、边界或支付记录。后续核销只使用实际读取的证据，不从别图、文件名或历史单补写本图事实。
