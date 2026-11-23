"""One result-only coordinator and three independently scoped audit workers.

Routing is an execution boundary, never a business verdict. Whole sources are
selected by material role; no rows, photos or source values are truncated.
"""
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from copy import copy, deepcopy
from dataclasses import asdict
from threading import RLock

from .common import AuditError
from .run_conversation import agent_scope


WORKERS = ('visual', 'documents', 'numbers')
LABELS = {'main': '主Agent·结果汇总', 'visual': '照片Agent',
          'documents': '文档Agent', 'numbers': '金额Agent'}
PHOTO_ROLES = {'display_photos', 'finished_photos', 'giveaway_evidence', 'price_evidence',
    'full_reduction_evidence', 'gift_finished_photos', 'gift_store_photos', 'staff_photos',
    'training_photos', 'shelf_photos'}
PHOTO_RULES = {'photo_activity_dates', 'entry_duplicate', 'entry_photo_coverage',
               'entry_watermark', 'entry_sku', 'entry_application_products', 'photo_authenticity'}
DOCUMENT_RULES = {'entry_agreement', 'entry_inventory', 'atrium_agreement',
                  'application_activity_dates', 'receipt_finance_seal'}
NUMBER_GROUPS = {'budget', 'pos', 'settlement', 'pos_settlement', 'pos_quantity', 'payment', 'invoice'}


def photo_policy():
    """Load the one maintained policy, including it in the actual model request."""
    from .pdf_materials import ORCHESTRATOR_SKILL
    text = (ORCHESTRATOR_SKILL/'SKILL.md').read_text('utf-8')
    return text.split('## 通用照片造假判断', 1)[1].split('\n## ', 1)[0].strip()


def owner(rule_id):
    from .audit_rag import ROLE_GROUPS
    if rule_id in DOCUMENT_RULES:
        return 'documents'
    if rule_id in PHOTO_RULES or rule_id in ROLE_GROUPS['photos'][1]:
        return 'visual'
    if any(rule_id in ROLE_GROUPS[group][1] for group in NUMBER_GROUPS):
        return 'numbers'
    return 'documents'


def rule_specs(scenario):
    from .pdf_policy import rules, SUBMITTED_SOURCE_CHECKS
    values = [asdict(r) for r in rules(scenario)]
    ids = {v['id'] for v in values}
    if scenario == 'personnel_incentive' and 'application_activity_dates' not in ids:
        values.append({'id': 'application_activity_dates',
                       'text': SUBMITTED_SOURCE_CHECKS['application_activity_dates']['requirement']})
    values.append({'id': 'photo_authenticity', 'text': photo_policy()})
    return values


def source_catalog(case):
    from .material_sources import resolve_source_ids
    units = {u['unit_id']: u for u in case.get('units', [])}
    roles = {}
    for material in case.get('materials', []):
        for uid in resolve_source_ids(case, material.get('source_ids', [])):
            roles.setdefault(uid, set()).add(material['id'])
    return [{'unit_id': d['unit_id'], 'roles': sorted(set(d.get('roles', [])) | roles.get(d['unit_id'], set())),
             'source_file': units.get(d['unit_id'], {}).get('source_file', ''),
             'locator': units.get(d['unit_id'], {}).get('locator', ''),
             'detected_format': units.get(d['unit_id'], {}).get('detected_format', 'unknown'),
             'has_image': bool(units.get(d['unit_id'], {}).get('image'))}
            for d in case['documents']]


def scoped_case(case, agent_id, *, extra_ids=()):
    from .audit_rag import ROLE_GROUPS, APPLICATION_LABELS
    specs = [s for s in rule_specs(case['scenario']) if owner(s['id']) == agent_id]
    selected = {s['id'] for s in specs}
    roles = set()
    for dependencies, ids in ROLE_GROUPS.values():
        if selected & ids:
            roles.update(dependencies)
    if agent_id == 'visual':
        roles.update(PHOTO_ROLES | {'promotion_contract', 'entry_agreement', 'activity_application'})
        # Entry's shared role group also includes unrelated inventory/POS.
        roles.difference_update({'entry_system', 'pos', 'pos_excel'})
    if agent_id == 'documents':
        roles.difference_update(PHOTO_ROLES | {'pos_excel'})
        roles.update({'entry_system', 'entry_agreement'})
        # Alternatives need their actual sources in the material specialist's
        # packet. This is limited to these rules, not all unrelated photos.
        if 'gift_activity_evidence' in selected:
            roles.update({'gift_activity_evidence', 'gift_finished_photos', 'gift_store_photos', 'pos', 'pos_excel'})
        if 'entry_inventory' in selected:
            roles.update({'pos', 'pos_excel'})
    if agent_id == 'numbers':
        roles.update({'purchase_transaction_record', 'approved_claim'})
    catalog = source_catalog(case)
    ids = {item['unit_id'] for item in catalog if set(item['roles']) & roles}
    # Inventory's unclassified documents are owned by the documents specialist;
    # every other worker can explicitly ask for them using the same-run index.
    if agent_id == 'documents':
        known_roles = set().union(*(entry[0] for entry in ROLE_GROUPS.values())) | {'approved_claim', 'purchase_transaction_record'}
        ids.update(item['unit_id'] for item in catalog if not set(item['roles']) & known_roles)
    for unit in case.get('units', []):
        if unit.get('source_kind') != 'qince':
            continue
        label = unit.get('source_file', '')
        allowed = set(APPLICATION_LABELS)
        if agent_id == 'numbers':
            allowed.update({'勤策核销明细', '勤策核销批复', '勤策核销审批状态'})
        if agent_id == 'visual':
            allowed.update({'勤策终端主档', '勤策商品包装参照', '勤策商品识别码与尺寸',
                            '勤策本图识别与校对参照'})
        if label in allowed:
            ids.add(unit['unit_id'])
    ids.update(extra_ids)
    scoped = deepcopy({k: v for k, v in case.items() if k not in {'documents', 'units'}})
    scoped.update(documents=deepcopy([d for d in case['documents'] if d['unit_id'] in ids]),
                  units=deepcopy([u for u in case.get('units', []) if u['unit_id'] in ids]))
    scoped.update(_agent_id=agent_id, _agent_rule_specs=specs, _agent_source_catalog=catalog)
    from .tool_observations import _identity
    scoped['_agent_corpus_fingerprint'] = _identity(case)
    if agent_id != 'visual':
        scoped.pop('_photo_reuse_advice', None)
    if agent_id != 'documents':
        scoped.pop('_submitted_source_advice', None)
    return scoped


def worker_provider(provider, agent_id):
    """Share immutable settings, never mutable evidence or conversation dictionaries."""
    worker = copy(provider)
    worker._agent_id = agent_id
    if agent_id == 'visual' and getattr(provider.connection, 'visual', None):
        worker.connection = provider.connection.visual
        worker.model, worker.effort = worker.connection.model, worker.connection.reasoning_effort
    worker._agent_conversations = {}
    session = getattr(provider, '_agent_conversations', {}).get(agent_id)
    if session is not None:
        worker._agent_conversations[agent_id] = session
    worker.__dict__.pop('_run_conversation', None)
    worker.__dict__.pop('_visual_provider', None)
    worker._audit_cases = {}
    worker._soft_check_advice = []
    worker._agent_reports = {}
    return worker


def expand_sources(case, full_case, requested):
    known = {d['unit_id'] for d in full_case['documents']}
    if not isinstance(requested, list) or any(uid not in known for uid in requested):
        raise AuditError('子Agent只能请求当前核销单来源目录中的资料。')
    present = {d['unit_id'] for d in case['documents']}
    added = set(requested) - present
    case['documents'].extend(deepcopy([d for d in full_case['documents'] if d['unit_id'] in added]))
    case['units'].extend(deepcopy([u for u in full_case.get('units', []) if u['unit_id'] in added]))
    return sorted(added)


def expand_tool_sources(provider, case, requests):
    """Deliver same-case catalog sources before a model-selected tool runs."""
    if not case.get('_agent_id') or not getattr(provider, '_agent_expand_sources', None):
        return []
    known = {item['unit_id'] for item in case.get('_agent_source_catalog', [])}
    wanted = []
    for request in requests if isinstance(requests, list) else []:
        if not isinstance(request, dict):
            continue
        sources = request.get('source_ids', [])
        products = request.get('products', [])
        references = list(sources) if isinstance(sources, list) else []
        if isinstance(products, list):
            references.extend(product.get('unit_id') for product in products if isinstance(product, dict))
        for reference in references:
            if isinstance(reference, str) and reference in known and reference not in wanted:
                wanted.append(reference)
    added = provider._agent_expand_sources(case, wanted) if wanted else []
    if added:
        from .execution_trace import record_step
        record_step('audit.agent_sources_expanded', '按工具请求补齐本单已有完整来源', {
            'agent_id': case['_agent_id'], 'source_ids': added,
            'scope': '仅当前核销单来源目录，其他单据与未知来源仍不允许读取'})
    return added


def check_report(value, selected):
    """Reject cross-role writes; keep every conclusion for the assigned rules.

    A rule can have separate conclusions for different stores, sources or
    aspects of one document. Its ID identifies the responsibility, not a unique
    result row. Neither collapse those rows nor treat them as another writer.
    """
    if not isinstance(value, dict) or not isinstance(value.get('checks'), list):
        raise AuditError('子Agent须返回可读取的逐项结果。')
    groups = [value['checks'], *[p['checks'] for p in value.get('photo_reviews', [])]]
    for checks in groups:
        ids = [c.get('rule_id') for c in checks]
        foreign = set(ids) - set(selected)
        if foreign:
            raise AuditError('子Agent不能覆盖其他角色的审核项：' + '、'.join(sorted(foreign)))


def report_for(case, value, advice=()):
    selected = [s['id'] for s in case['_agent_rule_specs']]
    if case['_agent_id'] != 'visual' and value.get('photo_reviews'):
        raise AuditError('照片逐图判断只允许照片Agent返回，其他角色不能代写。')
    check_report(value, selected)
    return {'archive_id': case['archive_id'], 'agent_id': case['_agent_id'],
            'assigned_rule_ids': selected, 'result': deepcopy(value),
            'pending_rule_ids': [r for r in selected if r not in {c['rule_id'] for c in value['checks']}],
            'source_ids': [d['unit_id'] for d in case['documents']], 'stage_advice': deepcopy(list(advice))}


def run(provider, case, root):
    from .model_owned_audit import run_single
    from .model_metrics import save_model_observations
    from .execution_trace import record_step
    from .model_authority import execution
    provider._audit_cases[case['archive_id']] = deepcopy(case)
    workers = {role: worker_provider(provider, role) for role in WORKERS}
    tool_lock = RLock()
    from .run_conversation import for_provider
    for worker in workers.values():
        worker._agent_tool_lock = tool_lock
        # Establish contained paths sequentially before concurrent requests.
        # Resolving partly-created Windows parents from several threads races.
        for_provider(worker)
    scopes = {role: scoped_case(case, role) for role in WORKERS}
    photo_report = Future()
    record_step('audit.agents.started', '主Agent分派三个独立审核任务', {
        'archive_id': case['archive_id'], 'workers': [
            {'agent_id': role, 'rule_ids': [s['id'] for s in scopes[role]['_agent_rule_specs']],
             'source_ids': [d['unit_id'] for d in scopes[role]['documents']]} for role in WORKERS],
        'main_receives_raw_sources': False})

    def execute(role):
        worker, scoped = workers[role], scopes[role]
        worker._agent_expand_sources = lambda target, requested: expand_sources(target, case, requested)
        with agent_scope(role), execution():
            try:
                if role == 'documents' and any(s['id'] == 'gift_activity_evidence' for s in scoped['_agent_rule_specs']):
                    # This aggregate rule combines POS coverage with the photo
                    # specialist's actual answer, never a second photo verdict.
                    scoped['_photo_agent_report'] = deepcopy(photo_report.result())
                value = run_single(worker, scoped, root/('agent-'+role))
                report = report_for(scoped, value, worker._soft_check_advice)
                save_model_observations('agent-report-'+case['archive_id']+'-'+role, report)
                record_step('audit.agents.worker_completed', LABELS[role]+'完成本职审核', report)
                if role == 'visual':
                    photo_report.set_result(report)
                return report
            except BaseException as error:
                if role == 'visual' and not photo_report.done():
                    photo_report.set_exception(error)
                raise

    reports = []
    failures = []
    # Each submitted context carries the shared, locked trace/usage sinks and
    # its own ContextVars. No conversation lock is shared across worker roles.
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix='audit-agent') as pool:
        futures = {role: pool.submit(copy_context().run, execute, role) for role in WORKERS}
        for role in WORKERS:
            try:
                report = futures[role].result()
                reports.append(report)
                record_step('audit.agents.collected', '主Agent接收'+LABELS[role]+'结果', {
                    'archive_id': case['archive_id'], 'worker_agent_id': role,
                    'checks': report['result']['checks'], 'pending_rule_ids': report['pending_rule_ids']})
            except Exception as error:
                from .execution_diagnostics import _text
                failures.append((role, error))
                record_step('audit.agents.worker_failed', LABELS[role]+'执行未完成',
                    {'archive_id': case['archive_id'], 'worker_agent_id': role, 'error': _text(error)}, status='failed')
    provider._agent_reports = getattr(provider, '_agent_reports', {})
    provider._agent_reports[case['archive_id']] = reports
    provider._agent_workers = getattr(provider, '_agent_workers', {})
    provider._agent_workers[case['archive_id']] = workers
    if failures:
        # Preserve successful reports above; never turn an API failure into a
        # customer defect or a silently complete partial audit.
        error = failures[0][1]
        error.add_note('子Agent执行未完成：'+'、'.join(LABELS[role] for role, _ in failures))
        raise error
    checks, photos = [], []
    for report in reports:
        checks.extend(deepcopy(report['result']['checks']))
        photos.extend(deepcopy(report['result'].get('photo_reviews', [])))
        provider._soft_check_advice.extend(report['stage_advice'])
    value = {'checks': checks, '_agent_owners': {c['rule_id']: owner(c['rule_id']) for c in checks},
             '_agent_mode': 'main_three', '_agent_assignments': {
                 report['agent_id']: report['assigned_rule_ids'] for report in reports}}
    from .audit_rag import EXTRA_OWNER
    for report in reports:
        for key, ids in EXTRA_OWNER.items():
            if key in report['result'] and ids & set(report['assigned_rule_ids']):
                value[key] = deepcopy(report['result'][key])
    if photos:
        value['photo_reviews'] = photos
    return value


def summary_reports(provider):
    """Main receives results and evidence references, not worker prompts/tool dumps."""
    reports = []
    for aid, saved in getattr(provider, '_agent_reports', {}).items():
        for report in saved:
            def project(check):
                return {k: deepcopy(check[k]) for k in ('rule_id', 'status', 'reason', 'source_ids') if k in check}
            value = report['result']
            reports.append({'archive_id': aid, 'agent_id': report['agent_id'],
                'assigned_rule_ids': report['assigned_rule_ids'], 'pending_rule_ids': report['pending_rule_ids'],
                'checks': [project(c) for c in value['checks']],
                'photo_reviews': [{'unit_id': p['unit_id'], 'checks': [project(c) for c in p['checks']]}
                                  for p in value.get('photo_reviews', [])],
                'stage_advice': deepcopy(report['stage_advice'])})
    return reports


def sync_reworked_report(provider, aid, evidence):
    from .model_metrics import save_model_observations
    for report in getattr(provider, '_agent_reports', {}).get(aid, []):
        selected = report['assigned_rule_ids']
        report['result']['checks'] = [deepcopy(c) for c in evidence['checks'] if c['rule_id'] in selected]
        report['result']['photo_reviews'] = [
            {'unit_id': p['unit_id'], 'checks': [deepcopy(c) for c in p['checks'] if c['rule_id'] in selected]}
            for p in evidence.get('photo_reviews', []) if any(c['rule_id'] in selected for c in p['checks'])]
        report['pending_rule_ids'] = [r for r in selected if r not in {c['rule_id'] for c in report['result']['checks']}]
        save_model_observations('agent-report-'+aid+'-'+report['agent_id'], report)


def restore_reports(provider, packets):
    """Resume from saved child results only; never import a raw-source chat."""
    provider._agent_reports = getattr(provider, '_agent_reports', {})
    for packet in packets:
        evidence = packet.get('audit_evidence', {})
        aid = packet['archive_id']
        if evidence.get('_agent_mode') != 'main_three':
            continue
        if aid not in provider._agent_reports:
            provider._agent_reports[aid] = [
                {'archive_id': aid, 'agent_id': role, 'assigned_rule_ids': ids,
                 'result': {'checks': []}, 'stage_advice': [], 'pending_rule_ids': []}
                for role, ids in evidence['_agent_assignments'].items()]
        sync_reworked_report(provider, aid, evidence)
