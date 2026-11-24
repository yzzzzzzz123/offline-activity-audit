"""Re-read a requested original without the case's previous OCR or judgments.

The owning agent receives the new, source-bound observations afterwards. This
breaks confirmation loops such as reading 2025 as 2026 and then reusing that
same year while supposedly checking the image again with high effort.
"""
from copy import deepcopy
import json

from .common import AuditError, sha256_file
from .prompts import bound_prompt


def _red_ink_view(source, destination, clockwise=0):
    """Isolate red pigment from this original; black text and rules fall away."""
    from PIL import Image, ImageChops, ImageOps
    with Image.open(source) as original:
        if original.width * original.height > 24_000_000:
            return None
        red, green, blue = original.convert('RGB').split()
    dominance = ImageChops.darker(ImageChops.subtract(red, green), ImageChops.subtract(red, blue))
    mask = dominance.point(lambda x: 255 if x >= 24 else 0)
    bounds = mask.getbbox()
    if not bounds or mask.histogram()[255] < 24:
        return None
    # The mask selects a region only; retain faint strokes in the actual view.
    detail = ImageOps.invert(ImageOps.autocontrast(dominance.crop(bounds)))
    if clockwise in (90, 180, 270):
        detail = detail.rotate(-clockwise, expand=True)
    destination.parent.mkdir(parents=True, exist_ok=True)
    detail.save(destination, 'PNG')
    return bounds


def run(provider, root, skill, schema, prompt, *, images, label, validator, **options):
    from .extraction_chain import render_prompt
    from .execution_trace import record_step
    from .model_metrics import save_model_observations
    from .pdf_policy import COMPANY_TEXT_READING_GUIDANCE, PHOTO_TIME_READING_GUIDANCE
    from .recognition_examples import load_examples, study_prompt
    from .run_conversation import isolated, current_agent_id
    if not hasattr(provider, '_recognition_study'):
        provider._recognition_study=load_examples()

    read_schema={'type':'object','required':['facts','limitations'],'additionalProperties':False,
                 'properties':{'facts':{'type':'array','items':{'type':'string'}},
                               'limitations':{'type':'array','items':{'type':'string'}},
                               'inspect_red_ink':{'type':'boolean'},
                               'red_ink_clockwise':{'type':'integer','enum':[0,90,180,270]}}}
    manifest=[{'image':path.name,'sha256':sha256_file(path)} for path in images]
    if current_agent_id() != 'visual' and getattr(provider.connection, 'visual', None):
        record_step('audit.image_delegated', '委托照片Agent独立读取图片后交回本职判断',
            {'requesting_agent': current_agent_id(), 'reading_agent': 'visual',
             'reading_model': provider.connection.visual.model, 'images': manifest,
             'business_task_transferred': False})
    record_step('audit.image_review.started',label+' · 独立重新读取指定原图',
        {'images':manifest,'max_images_per_request':1,'previous_case_transcription_supplied':False},status='started')
    readings=[];seen={}
    for index,(path,source) in enumerate(zip(images,manifest),1):
        if sha256_file(path)!=source['sha256']:
            raise AuditError('定向重读期间原图内容变化，未采用结果。')
        if source['sha256'] in seen:
            previous = seen[source['sha256']]
            readings.append({**deepcopy(previous), **source, 'same_bytes_as':previous['image']})
            continue
        reading=bound_prompt(
            '{study}\n独立重读当前附件的可见事实。没有提供本单初读结果或既有结论，'
            '不要猜测它们，也不判断核销是否通过。'
            '完整转录本图可见的水印年月日、星期、时分、门店名称、手写抬头、'
            '红章可辨核心字号及实际字样、价签、表格行原值和签章/版式位置；'
            '没有的内容不补，模糊处指出位置及可见片段，不根据当前年份或参考案例补写。'
            '区分原图写了什么与你的推测，返回facts和limitations。已见实际红章但旋转、叠印或缺墨使'
            '核心字形难辨时可另设inspect_red_ink=true，申请查看同一原件分离红色印文后的局部；'
            '可用red_ink_clockwise指定转正所需的顺时针角度0/90/180/270。'
            '\n时间字形读取：{time}\n手写与章印读取：{company}\n当前原图：{source}',
            study=study_prompt([],getattr(provider,'_recognition_study',None)),
            time=PHOTO_TIME_READING_GUIDANCE,company=COMPANY_TEXT_READING_GUIDANCE,
            source=json.dumps(source,ensure_ascii=False))
        with isolated():
            value=provider._call(root.parent/(root.name+f'-independent-read-{index:04d}'),
                skill,read_schema,reading,images=[path],label=label+f' · 独立原图重读 {index}/{len(images)}',
                validator=lambda answer:answer,transcription=True,max_attempts=1,allow_repair=False)
        if value.get('inspect_red_ink') is True:
            view_root = root.parent / (root.name + f'-red-ink-{index:04d}')
            image = view_root / 'red-ink.png'
            try:
                angle = value.get('red_ink_clockwise', 0)
                angle = angle if angle in (90, 180, 270) else 0
                bounds = _red_ink_view(path, image, angle)
                if bounds:
                    seal_prompt = bound_prompt(
                        '仅独立辨认这张同页红色笔画辅助视图中的印文字形。黑色表格线已被抑制，'
                        '不同位置的红字可能并非同一印章。按字形方向和圆弧分段阅读，输出可辨核心字号、'
                        '仍有缺墨的具体位置。只报图中可见片段，不猜公司全名。')
                    with isolated():
                        detail = provider._call(view_root, skill, read_schema, seal_prompt, images=[image],
                            label=label+' · 同页红色印文复看', validator=lambda answer:answer,
                            transcription=True, max_attempts=1, allow_repair=False)
                    value['seal_detail'] = {'source_sha256':source['sha256'], 'crop_box':list(bounds), 'clockwise':angle,
                        'facts':detail['facts'], 'limitations':detail['limitations']}
            except Exception as error:
                # This optional view can fail independently of the original.
                # Keep its already-read evidence and continue the owning agent.
                record_step('audit.seal_view_unavailable', '印文辅助视图未完成，继续使用原图读取结果',
                            {'image':source['image'], 'error_type':type(error).__name__}, status='skipped')
        if sha256_file(path)!=source['sha256']:
            raise AuditError('定向重读期间原图内容变化，未采用结果。')
        readings.append({**source,**deepcopy(value)});seen[source['sha256']]=readings[-1]
    save_model_observations('independent-original-review-'+root.name,
        {'images':readings,'previous_case_transcription_supplied':False,'max_images_per_request':1,
         'same_run_only':True,'automatic_business_verdict':False})
    context=getattr(prompt,'partial_variables',{}).get('_audit_stage_context')
    continued=bound_prompt('{original}\n指定原图已在不带本单旧转录与既有结论的独立上下文重新读取。'
        '下面是新取得的来源绑定事实；若与初读矛盾，结合原始字形读取及具体限制修正对应判断，'
        '不要因旧转录在前文重复多次就把它当成已核实的事实。'
        '只更新受影响的照片或审核项，保留其他已完成结果；新读取不是程序生成的通过结论。'
        '\n指定原图独立重读结果：{readings}',original=render_prompt(prompt),
        readings=json.dumps(readings,ensure_ascii=False,separators=(',',':')),
        **({'_audit_stage_context':context} if context is not None else {}))
    return provider._call_stage(root,skill,schema,continued,images=[],label=label,validator=validator,**options)
