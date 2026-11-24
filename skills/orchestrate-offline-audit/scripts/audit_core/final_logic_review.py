"""Text-only final summary checks with isolated rework of conflicting targets."""
from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path

from .common import AuditError
from .model_judgment import digest, save_judgments, source_guard


SKILL = Path(__file__).resolve().parents[3] / 'audit-final-logic'


def packet_case(packet, provider):
    from .qince_amount_basis import approval_scope_flags
    from .material_sources import inventory_notes
    saved = getattr(provider, '_audit_cases', {}).get(packet['archive_id'])
    case = {'archive_id': packet['archive_id'], 'scenario': packet['route'].get('scenario'),
        'flags': packet['classification']['flags'], 'materials': packet['classification']['materials'],
        'documents': packet['documents'], 'units': packet['units'],
        'inventory_notes': inventory_notes(packet['classification'], packet['route'].get('scenario'))}
    if saved and (saved['documents'], saved['materials']) == (case['documents'], case['materials']):
        case.update({k: v for k, v in saved.items() if k.startswith('_')})
    from .model_authority import enabled
    if not enabled():
        case['flags'] = approval_scope_flags(case['flags'], case['documents'], case['materials'], case['units'])
    return case


def context(packets, provider):
    from .stage_context import compile_context
    from .pdf_policy import rules, requirements, SUBMITTED_SOURCE_CHECKS
    from .pdf_workflow import _submitted_checks
    from .audit_rag import reference_fields, reference_view
    from .tool_observations import observations,identity_support
    archives, targets, sources = [], {}, {}
    for packet in packets:
        if packet.get('excluded_by_selection'):
            continue
        aid = packet['archive_id']; case = packet_case(packet, provider)
        gate = aid + ':materials'
        targets[gate] = {'archive_id': aid, 'kind': 'materials'}
        checks = [{'target_id': gate, 'kind': 'materials', 'route': packet['route'],
            'classification': packet['classification']}]
        model_ids = {c['rule_id'] for c in packet.get('audit_evidence', {}).get('checks', [])}
        current_checks = packet.get('result', {}).get('checks', _submitted_checks(packet))
        for check in current_checks:
            key = aid + ':check:' + check['rule_id']
            kind = 'check' if check['rule_id'] in model_ids else 'submitted'
            targets[key] = {'archive_id': aid, 'kind': kind, 'rule_id': check['rule_id']}
            checks.append({'target_id': key, 'kind': kind, 'check': check})
        for photo in packet.get('audit_evidence', {}).get('photo_reviews', []):
            for check in photo['checks']:
                key = aid + ':photo:' + photo['unit_id'] + ':' + check['rule_id']
                targets[key] = {'archive_id': aid, 'kind': 'photo', 'rule_id': check['rule_id'],
                                'photo_unit_id': photo['unit_id']}
                checks.append({'target_id': key, 'kind': 'photo', 'photo_unit_id': photo['unit_id'], 'check': check})
        units = {u['unit_id']: u for u in case['units']}
        index = []
        for document in case['documents']:
            uid = document['unit_id']; unit = units.get(uid, {})
            sid = aid + ':' + uid
            path = unit.get('image')
            image_path = Path(path) if path else None
            sources[sid] = {'archive_id': aid, 'document': document,
                            'image': image_path if image_path and image_path.is_file() else None}
            index.append({'source_id': sid, 'unit_id': uid, 'file': unit.get('source_file', ''),
                'locator': unit.get('locator', ''), 'roles': document.get('roles', []),
                'limitations': document.get('limitations', []), 'has_image': sources[sid]['image'] is not None})
        registered = {r.id for r in rules(case['scenario'])} if case['scenario'] else set()
        from .model_authority import enabled
        selected = [r.id for r in rules(case['scenario'])] if enabled() and case['scenario'] else [c['rule_id'] for c in packet.get('result', {}).get('checks', []) if c['rule_id'] in registered]
        source_advice = source_guard(packet.get('audit_evidence', {'checks': []}), case['documents'], model_ids)
        tool_proofs=observations(case)
        archives.append({'archive_id': aid, 'source_archive': packet['source_archive'],
            'scenario': case['scenario'], 'checks': checks, 'source_index': index,
            'rules': compile_context(case['scenario'], selected, case['flags']) if selected else None,
            'material_requirements': [asdict(r) for r in requirements(case['scenario'])] if case['scenario'] else [],
            'submitted_source_requirements': SUBMITTED_SOURCE_CHECKS,
            'submitted_source_advice': packet.get('submitted_source_checks', []),
            'reference_fields': reference_view(reference_fields(case['documents'], case['units'])),
            'product_identity_support': identity_support(case,tool_proofs),
            'tool_observations':tool_proofs,
            'stage_advice': [a for a in getattr(provider, '_soft_check_advice', []) if a['archive_id'] == aid],
            **({'source_matching_advice': source_advice} if source_advice['issues'] else {})})
    return {'archives': archives}, targets, sources


def response_schema(targets, sources):
    strings = lambda values: {'type': 'array', 'uniqueItems': True,
        'items': {'type': 'string', 'enum': list(values)}}
    return {'type': 'object', 'additionalProperties': False,
        'required': ['ready', 'findings', 'notes'], 'properties': {
            'ready': {'type': 'boolean'}, 'notes': {'type': 'array', 'items': {'type': 'string'}},
            'findings': {'type': 'array', 'items': {'type': 'object', 'additionalProperties': False,
                'required': ['target_ids', 'reason'], 'properties': {
                    'target_ids': {**strings(targets), 'minItems': 1},
                    'reason': {'type': 'string', 'minLength': 1}}}},
            'source_requests': strings(sources),
            'image_requests': strings(k for k, v in sources.items() if v['image'] is not None)}}


def review(provider, packets, root, responses):
    from jsonschema import Draft202012Validator, ValidationError
    from .prompts import bound_prompt
    from .model_metrics import save_model_observations
    from .execution_trace import record_step
    from .pdf_materials import _prompt_documents
    request, targets, sources = context(packets, provider)
    request['previous_rework'] = responses
    schema = response_schema(targets, sources)
    supplied, shown, images = set(), set(), []
    read_number = 0
    def validate(answer):
        from .model_authority import enabled, transport_schema, validate_transport
        if enabled():
            validate_transport(answer,transport_schema(schema))
            reads = answer.get('source_requests', []) or answer.get('image_requests', [])
            if answer['ready'] and (answer['findings'] or reads):
                # The model kept concerns while also passing; its own concerns
                # win. This is not a program veto, only a readable answer.
                answer['ready'] = False
            return answer
        try:
            Draft202012Validator(schema).validate(answer)
        except ValidationError as error:
            raise AuditError('最终逻辑自检响应不符合本单来源与复核目标约定。') from error
        reads = answer.get('source_requests', []) or answer.get('image_requests', [])
        if answer['ready'] and (answer['findings'] or reads):
            raise AuditError('最终逻辑自检不能同时通过并要求继续读证据或打回复核。')
        if not answer['ready'] and not answer['findings'] and not reads:
            raise AuditError('最终逻辑自检未完成时须说明本单需复核的审核项或待读来源。')
        return answer
    while True:
        read_number += 1
        call_root = root / f'read-{read_number:03d}'
        prompt = bound_prompt('调用核销最终逻辑自检Skill。按照Skill的边界和分场景样例，自主检查本单：\n{request}\n'
            '事实不足先请求已有source_id；发现实际矛盾用findings打回对应target_id。'
            '合法的真实unknown或客户问题可以保留；中途软门禁不是必须接受的结论。'
            '必须结合tool_observations核对结论是否已经过时；已有完整复算结果时，'
            '不得仅因旧回答称尚未返回而保留无法确认，应打回对应审核项让执行模型结合结果重新判断。'
            '真实看不清与内部复核未完成分别说明，不能把内部未执行事项包装为客户资料问题。'
            '引用匹配及结构检查仅供参考，由你结合对应原始来源作最终判断；确认没问题就通过，'
            '不必为了消除程序提示反复打回。'
            '只返回约定JSON，不输出私有推理。',
            request=json.dumps(request, ensure_ascii=False, separators=(',', ':'), default=str))
        save_model_observations('final-logic-input-' + root.name + '-' + str(read_number), request)
        answer = provider._call(call_root, SKILL, schema, prompt, images=images,
            label='最终逻辑自校验', validator=validate, max_attempts=1, allow_repair=True)
        validate(answer)
        save_model_observations('final-logic-result-' + root.name + '-' + str(read_number), answer)
        text_ids = set(answer.get('source_requests', [])) - supplied
        image_ids = set(answer.get('image_requests', [])) - shown
        if not text_ids and not image_ids:
            if answer.get('source_requests') or answer.get('image_requests'):
                answer.setdefault('notes', []).append(
                    '模型重复请求已提供过的来源，没有取得新依据；按当前结论继续，保留该待核对事项。')
                record_step('audit.final_logic.repeated_read', '最终逻辑自检重复请求已提供的来源',
                    {'source_ids': list(answer.get('source_requests') or []),
                     'image_source_ids': list(answer.get('image_requests') or [])})
            return answer, targets
        supplied.update(text_ids); shown.update(image_ids)
        requested_sources = [{'source_id': sid, **deepcopy(sources[sid]['document'])}
                             for sid in sorted(supplied)]
        request['requested_sources'] = json.loads(_prompt_documents(requested_sources))
        images = [sources[sid]['image'] for sid in sorted(shown)]
        request['provided_images'] = [{'source_id': sid, 'image': sources[sid]['image'].name} for sid in sorted(shown)]
        record_step('audit.final_logic.source_read', '最终逻辑自检按需读取原始证据', {
            'source_ids': sorted(text_ids), 'image_source_ids': sorted(image_ids)})


def rework(provider, packets, findings, targets, root):
    from .agent_orchestration import owner, scoped_case, worker_provider, rule_specs, sync_reworked_report, expand_sources
    from .run_conversation import agent_scope
    from .model_self_check import review_stage
    from .pdf_evidence import _merge_photo_reviews
    from .execution_trace import record_step
    from .pdf_workflow import _submitted_checks
    from .pdf_policy import rules, SUBMITTED_SOURCE_CHECKS, SUBMITTED_SOURCE_CLARIFICATION
    grouped = {}
    for finding in findings:
        for target_id in finding['target_ids']:
            target = targets[target_id]
            packet = next(p for p in packets if p['archive_id'] == target['archive_id'])
            team = packet.get('audit_evidence', {}).get('_agent_mode') == 'main_three'
            role = owner(target.get('rule_id', '')) if team else 'main'
            key = (target['archive_id'], target['kind'], target.get('photo_unit_id'), role)
            group = grouped.setdefault(key, {'targets': [], 'feedback': []})
            group['targets'].append(target)
            group['feedback'].append(finding['reason'])
    responses = []
    for index, ((aid, kind, photo_id, role), group) in enumerate(grouped.items(), 1):
        packet = next(p for p in packets if p['archive_id'] == aid)
        feedback = {'findings': list(dict.fromkeys(group['feedback'])), 'photo_unit_id': photo_id,
                    'submitted_source_advice': packet.get('submitted_source_checks', [])}
        stage_root = root / f'target-{index:03d}'
        def current():
            if kind == 'materials': return packet['classification']
            return {'audit_evidence': packet.get('audit_evidence'),
                    'submitted_check_reviews': packet.get('submitted_check_reviews', {})}
        before = deepcopy(current())
        if kind == 'materials':
            provider.rework_materials(packet, stage_root, feedback)
        else:
            case = packet_case(packet, provider)
            reviewer = provider
            if role != 'main':
                reviewer = getattr(provider, '_agent_workers', {}).get(aid, {}).get(role)
                if reviewer is None:
                    reviewer = worker_provider(provider, role)
                # A targeted rework reopens only the responsible worker's sources.
                relevant_ids = {uid for c in packet['audit_evidence']['checks']
                    if c['rule_id'] in {t['rule_id'] for t in group['targets']} for uid in c.get('source_ids', [])}
                full_case = case
                case = scoped_case(full_case, role, extra_ids=relevant_ids | ({photo_id} if photo_id else set()))
                if role != 'visual':
                    visual_report = next((r for r in getattr(provider, '_agent_reports', {}).get(aid, [])
                                          if r['agent_id'] == 'visual'), None)
                    if visual_report:
                        case['_photo_agent_report'] = deepcopy(visual_report)
                reviewer._agent_expand_sources = lambda target, requested: expand_sources(target, full_case, requested)
            original = packet['audit_evidence'] if kind != 'submitted' else {'checks': _submitted_checks(packet)}
            photo = next((p for p in original.get('photo_reviews', []) if p['unit_id'] == photo_id), None)
            if kind == 'photo' and photo is None:
                raise AuditError('定向照片复核必须对应本单已有的逐图结果。')
            selected = list(dict.fromkeys(t['rule_id'] for t in group['targets']))
            checks = (photo or original)['checks']
            value = {**deepcopy(original), 'checks': [deepcopy(c) for c in checks if c['rule_id'] in selected]}
            value.pop('photo_reviews', None)
            photos = {p['unit_id'] for p in original.get('photo_reviews', [])}
            if photo_id:
                # Only this photo and its non-photo supporting documents are
                # visible to its rework step. Tool proofs still use the intact
                # run-owned case identity, so completed tools remain reusable.
                case['_rework_documents'] = [d for d in case['documents']
                    if d['unit_id'] == photo_id or d['unit_id'] not in photos]
            def validate(candidate, ids):
                source_guard(candidate, case['documents'], ids)
                from .model_authority import enabled
                if photo_id and not enabled():
                    for check in candidate['checks']:
                        if set(check['source_ids']).intersection(photos - {photo_id}):
                            raise AuditError('本张照片的复核不能借用其他照片来源。')
                return {'checks': candidate['checks']}
            relevant = {uid for c in value['checks'] for uid in c['source_ids']}
            images = [u['image'] for u in case['units'] if u.get('image') and
                      (u['unit_id'] == photo_id if photo_id else u['unit_id'] in relevant)]
            rule_context = None
            registered = {r.id for r in rules(case['scenario'])}
            auxiliary = set(selected) - registered
            if auxiliary:
                specs = {s['id']: s for s in rule_specs(case['scenario'])} if role != 'main' else {}
                if not auxiliary.issubset(set(SUBMITTED_SOURCE_CHECKS) | set(specs)):
                    raise AuditError('复核项目须有本单已登记的原审核依据。')
                rule_context = {'version': 1, 'phase': 'audit', 'scenario': case['scenario'],
                    'rule_ids': selected, 'rules': [specs[key] if key in specs else
                        {'id': key, **SUBMITTED_SOURCE_CHECKS[key]} for key in selected],
                    'policies': {'source_scope': SUBMITTED_SOURCE_CLARIFICATION},
                    'max_text_bytes': None, 'max_output_tokens': None}
            with agent_scope(role):
                candidate = review_stage(reviewer, case, value, validate(value, selected), stage_root,
                    selected, validate, images=images, feedback=feedback, required=True, rule_context=rule_context)
            validate(candidate, selected)
            revised = {c['rule_id']: c for c in candidate['checks'] if c['rule_id'] in selected}
            destination = photo if photo_id else original
            destination['checks'] = [deepcopy(revised.get(c['rule_id'], c)) for c in destination['checks']]
            existing = {c['rule_id'] for c in destination['checks']}
            destination['checks'].extend(deepcopy(c) for key, c in revised.items() if key not in existing)
            if not photo_id:
                observed_ids = {c['rule_id'] for c in packet.get('submitted_source_checks', [])}
                packet.setdefault('submitted_check_reviews', {}).update(
                    {key: deepcopy(check) for key, check in revised.items() if key in observed_ids})
            if photo_id:
                _merge_photo_reviews([c for c in original['checks'] if c['rule_id'] in selected],
                                     original['photo_reviews'])
            save_judgments(case, original, [c['rule_id'] for c in original['checks']], authority='provisional_model_result')
            sync_reworked_report(provider, aid, packet['audit_evidence'])
        after = current()
        response = {'archive_id': aid, 'kind': kind, 'feedback': feedback,
                    'changed': before != after, 'before': before, 'after': deepcopy(after)}
        responses.append(response)
        record_step('audit.final_logic.rework_result', '按最终逻辑自检反馈复核原审核项', response)
    return responses


def summary_targets(packets):
    """Expose completed conclusions and their exact rework addresses, no raw sources."""
    records, targets = [], {}
    for packet in packets:
        if packet.get('excluded_by_selection'):
            continue
        aid = packet['archive_id']
        if isinstance(packet.get('classification'), dict):
            material_id = aid + ':materials'
            targets[material_id] = {'archive_id': aid, 'kind': 'materials'}
            records.append({'target_id': material_id, 'archive_id': aid, 'kind': 'materials',
                'reason': packet['classification'].get('reason', ''),
                'materials': [{k: m[k] for k in ('id', 'state') if k in m}
                              for m in packet['classification'].get('materials', [])]})
        model_ids = {c['rule_id'] for c in packet.get('audit_evidence', {}).get('checks', [])}
        from .pdf_workflow import _submitted_checks
        checks = packet.get('result', {}).get('checks', _submitted_checks(packet))
        for check in checks:
            target_id = aid + ':check:' + check['rule_id']
            target = {'archive_id': aid, 'kind': 'check' if check['rule_id'] in model_ids else 'submitted',
                      'rule_id': check['rule_id']}
            targets[target_id] = target
            records.append({'target_id': target_id, **target,
                **{k: deepcopy(check[k]) for k in ('status', 'reason', 'source_ids') if k in check}})
        for photo in packet.get('audit_evidence', {}).get('photo_reviews', []):
            for check in photo['checks']:
                target_id = aid + ':photo:' + photo['unit_id'] + ':' + check['rule_id']
                target = {'archive_id': aid, 'kind': 'photo', 'rule_id': check['rule_id'],
                          'photo_unit_id': photo['unit_id']}
                targets[target_id] = target
                records.append({'target_id': target_id, **target,
                    **{k: deepcopy(check[k]) for k in ('status', 'reason', 'source_ids') if k in check}})
        # A worker omission remains a pending execution target, not a fabricated
        # customer failure or an implicit pass.
        for role, rule_ids in packet.get('audit_evidence', {}).get('_agent_assignments', {}).items():
            for rule_id in rule_ids:
                target_id = aid + ':check:' + rule_id
                if target_id not in targets:
                    target = {'archive_id': aid, 'kind': 'check', 'rule_id': rule_id}
                    targets[target_id] = target
                    records.append({'target_id': target_id, **target, 'agent_id': role,
                                    'execution_status': 'pending', 'reason': '子Agent尚未返回该审核项。'})
    return records, targets


def _progress_state(packets, provider, target_ids, targets):
    """Changed wording alone cannot restart the same unresolved evidence loop."""
    from .tool_observations import observations
    state = []
    for target_id in sorted(target_ids):
        target = targets[target_id]
        packet = next(p for p in packets if p['archive_id'] == target['archive_id'])
        if target['kind'] == 'materials':
            value = {k: packet['classification'].get(k) for k in ('materials', 'flags')}
        else:
            if target['kind'] == 'photo':
                checks = next(p for p in packet['audit_evidence']['photo_reviews']
                              if p['unit_id'] == target['photo_unit_id'])['checks']
            else:
                checks = packet.get('result', {}).get('checks', [])
            check = next((c for c in checks if c['rule_id'] == target['rule_id']), {})
            value = {k: deepcopy(check.get(k)) for k in ('status', 'source_ids', 'comparisons',
                'calculations', 'reference_evidence', 'source_assertions') if k in check}
        tool_case = packet_case(packet, provider)
        if packet.get('audit_evidence', {}).get('_agent_mode') == 'main_three':
            from .agent_orchestration import scoped_case, owner
            tool_case = scoped_case(tool_case, owner(target.get('rule_id', '')))
        state.append({'target_id': target_id, 'value': value,
            'tool_results': [r['result'] for r in observations(tool_case)]})
    return state


def run(flow):
    """Check summary text, rework only named conflicts, then check the updated text."""
    provider = flow.provider
    if not callable(getattr(provider, '_call', None)):
        flow.logic_review_result = {'status': 'external_evidence_provider', 'model_reviewed': False}
        return flow
    from .model_metrics import save_model_observations
    from .execution_trace import record_step
    from .execution_diagnostics import _text
    from .pdf_workflow import build_view
    from .agent_orchestration import restore_reports
    restore_reports(provider, flow.packets)
    flow.emit('logic_review.started', scope='summary_with_targeted_rework', images_sent=0)
    flow.summary_review_result = None
    flow.summary_review_error = None
    rounds, rework_rounds, seen = 0, 0, set()

    def finish(status, *, model_reviewed, notes=(), reason='', error=None):
        flow.logic_review_result = {'scope': 'summary_with_targeted_rework', 'images_sent': 0,
            'rework_policy': 'affected_targets_only', 'rounds': rounds, 'rework_rounds': rework_rounds,
            'skill': 'audit-final-logic', 'status': status, 'model_reviewed': model_reviewed,
            'notes': list(notes), 'reason': reason}
        if error is not None:
            flow.logic_review_result['error'] = _text(error)
        save_model_observations('final-logic-summary', flow.logic_review_result)
        flow.emit('logic_review.completed', review=flow.logic_review_result)
        return flow

    while True:
        rounds += 1
        records, targets = summary_targets(flow.packets)
        view = build_view(flow.packets, flow.rejection)
        # Addresses refer to saved conclusions, not a raw-source or image
        # index. Only the separate rework step can reopen the selected sources.
        view['review_targets'] = records
        round_root = flow.temporary_root / f'final-logic-summary-{rounds:03d}'
        try:
            result = provider.review_summary(view, round_root)
            flow.summary_review_result = result
            metadata = (result.get('metadata') or {}) if isinstance(result, dict) else {}
            findings = metadata.get('findings') or []
            usable = (isinstance(result, dict) and isinstance(result.get('text'), str)
                      and bool(result['text'].strip()) and metadata.get('status') == 'reviewed'
                      and not findings)
        except Exception as error:
            flow.summary_review_result = None
            flow.summary_review_error = _text(error)
            record_step('audit.final_logic.unavailable', '总结逻辑自校验未完成，保留原稿',
                        {'error': _text(error), 'scope': 'summary_with_targeted_rework'})
            return finish('unavailable', model_reviewed=False, error=error,
                          reason='总结逻辑自校验未完成，保留原稿和已有审核结论。')
        if usable:
            return finish('passed', model_reviewed=True, notes=metadata.get('issues') or [],
                          reason='总结与已完成审核结论一致，相关矛盾已定向复核。')
        if not findings:
            return finish('not_passed', model_reviewed=bool(metadata), notes=metadata.get('issues') or [],
                          reason='总结仍有未解决事项，未给出可执行的具体复核目标，保留原稿及核验记录。')
        try:
            for finding in findings:
                if (not isinstance(finding, dict) or not isinstance(finding.get('target_ids'), list)
                        or not finding['target_ids'] or not set(finding['target_ids']).issubset(targets)
                        or not isinstance(finding.get('reason'), str) or not finding['reason'].strip()):
                    raise AuditError('只能重跑本单明确指出的矛盾审核项或照片。')
            target_ids = {target_id for f in findings for target_id in f['target_ids']}
            signature = digest(_progress_state(flow.packets, provider, target_ids, targets))
            if signature in seen:
                return finish('not_passed', model_reviewed=True,
                    notes=[f['reason'] for f in findings],
                    reason='相同目标的结论与依据未取得新进展，停止重复返工并保留未解决矛盾。')
            seen.add(signature)
            flow.emit('logic_review.rework', findings=findings, round=rounds,
                      rework_policy='affected_targets_only')
            rework_rounds += 1
            try:
                responses = rework(provider, flow.packets, findings, targets, round_root/'rework')
            finally:
                # Preserve any successfully completed target even if a later
                # target in this round fails. This only rebuilds saved results.
                flow.evidence()
                flow.decision()
            save_model_observations('final-logic-targeted-rework-' + str(rework_rounds), responses)
        except Exception as error:
            record_step('audit.final_logic.rework_incomplete', '矛盾项定向复核未完成，保留其他结果',
                        {'error': _text(error), 'findings': findings})
            return finish('not_passed', model_reviewed=True, notes=[f['reason'] for f in findings],
                          error=error, reason='仅相关矛盾项复核未完成，其他已完成审核结果保留。')
