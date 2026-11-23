"""Sequential visual requests in the owning audit conversation, never collages."""
from copy import deepcopy
import json

from .common import AuditError
from .prompts import bound_prompt


def run(provider, root, skill, schema, prompt, *, images, label, validator, **options):
    """Read each requested original, then assemble the existing response in text.

    The first image introduces the task; later images continue the same visible
    conversation. This is also the fallback for old multi-image call sites.
    No image, rule, source position or business finding is silently dropped.
    """
    from .common import sha256_file
    from .extraction_chain import render_prompt
    from .execution_trace import record_step
    from .model_metrics import save_model_observations
    from .stage_context import reading_context

    observation_schema = {'type': 'object', 'required': ['facts', 'limitations'],
        'properties': {'facts': {'type': 'array', 'items': {'type': 'string'}},
                       'limitations': {'type': 'array', 'items': {'type': 'string'}},
                       'check_observations': {'type': 'array', 'items': {'type': 'string'}}}}
    original = render_prompt(prompt)
    observations = []
    seen = {}
    manifest = [{'image': path.name, 'sha256': sha256_file(path)} for path in images]
    record_step('audit.image_review.started', label + ' · 开始逐张核验原图',
                {'images': manifest, 'max_images_per_request': 1}, status='started')
    for index, (path, source) in enumerate(zip(images, manifest), 1):
        if sha256_file(path) != source['sha256']:
            raise AuditError('原图在逐张核验期间发生变化，未采用不同来源的结果。')
        if source['sha256'] in seen:
            prior = seen[source['sha256']]
            observations.append({**source, 'same_bytes_as': prior['image'],
                'facts': deepcopy(prior['facts']), 'limitations': deepcopy(prior['limitations'])})
            continue
        task = (original if index == 1 else
                '继续前一张开始的同一核验任务，沿用本单既有规则、来源和前序逐图记录。')
        image_prompt = bound_prompt(
            '本轮只读取并核验附件这一张原图，完成后再处理下一张；不读取或猜测尚未提供的图片。'
            '记录当前图可见事实、可支持当前任务检查的观察及具体看不清的限制。'
            '参考图和模板只作参照，不能充当本次现场事实；不能用其他图片补造本图内容。'
            '此时不汇总整单、不因其他图尚未提供而判断缺件。只返回本轮facts、limitations及可选check_observations。'
            '\n当前图片：{image}\n同一任务要求：{task}',
            image=json.dumps({'index': index, 'total': len(images), **source}, ensure_ascii=False),
            task=task, _audit_stage_context=reading_context())
        value = provider._call_stage(root.parent / (root.name + f'-single-image-{index:04d}'),
            skill, observation_schema, image_prompt, images=[path],
            label=label + f' · 单图核验 {index}/{len(images)}', validator=lambda value: value,
            transcription=True, max_attempts=1, allow_repair=False)
        if sha256_file(path) != source['sha256']:
            raise AuditError('原图在逐张核验期间发生变化，未采用不同来源的结果。')
        if not isinstance(value, dict) or not isinstance(value.get('facts'), list) or not isinstance(value.get('limitations'), list):
            raise AuditError('单图核验未返回可读的本图事实和读取限制。')
        observations.append({**source, **deepcopy(value)})
        seen[source['sha256']] = observations[-1]
        save_model_observations('single-image-review-' + root.name,
            {'max_images_per_request': 1, 'images': observations, 'total': len(images)})
    save_model_observations('single-image-review-' + root.name,
        {'max_images_per_request': 1, 'images': observations, 'total': len(images), 'complete': True})
    stage_context = getattr(prompt, 'partial_variables', {}).get('_audit_stage_context')
    assembled = bound_prompt(
        '{original}\n以上请求的原图已在本单同一会话逐张核验，以下是每张图的完整观察和来源。'
        '沿用这些逐图记录完成本轮原有JSON，不重新申请整包图片，不把参考图当作现场证据。'
        'same_bytes_as仅表示原图字节相同，各来源和位置仍分别保留，不得因此合并门店、付款记录或审核结论。'
        '\n逐图核验记录：{observations}', original=original,
        observations=json.dumps(observations, ensure_ascii=False, separators=(',', ':')),
        **({'_audit_stage_context': stage_context} if stage_context is not None else {}))
    return provider._call_stage(root, skill, schema, assembled, images=[],
                               label=label, validator=validator, **options)
