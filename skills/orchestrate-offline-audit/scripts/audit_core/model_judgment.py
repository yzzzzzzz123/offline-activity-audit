"""Run-owned model decisions, with deterministic helpers treated as advice.

Only a completed model self-check can publish an authoritative judgment. The
receipt binds the exact returned check to this run's original source corpus;
ordinary validator calls and historical records do not acquire that authority.
"""
from copy import deepcopy
import hashlib
import json
import re

from .common import AuditError


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':'), default=str).encode('utf-8')).hexdigest()


def context_key(scenario, flags, documents, materials):
    return digest({'scenario': scenario, 'flags': {k: v for k, v in flags.items() if not k.startswith('_')},
        'documents': documents, 'materials': materials})


def source_guard(value, documents, selected):
    """Bind this run's identities; quotation matching is model-owned advice.

    A lexical matcher cannot veto an executing model's completed judgment.
    Keep the exact candidate and original facts, with paths the model can review.
    Business/schema helper constraints are evaluated separately as advice.
    """
    from .model_authority import enabled
    if enabled():
        if not isinstance(value,dict) or not isinstance(value.get('checks'),list):
            raise AuditError('审核回答须包含可读取的检查项数组。')
        sources = {d['unit_id'] for d in documents}
        issues = []
        for index, check in enumerate(value['checks']):
            if not isinstance(check,dict) or not isinstance(check.get('rule_id'),str):
                raise AuditError('审核检查项缺少可读取的rule_id。')
            missing = [uid for uid in check.get('source_ids',[]) if uid not in sources]
            if missing:
                issues.append({'path':f'/checks/{index}/source_ids','rule_id':check['rule_id'],
                    'constraint':'unresolved_source_reference','source_ids':missing,
                    'guidance':'本单来源索引未定位这些标识，由执行模型结合原资料核实，不作业务否决。'})
        return {'status':'available','authoritative':False,'issues':issues}
    if (not isinstance(value, dict) or not isinstance(value.get('checks'), list)
            or any(not isinstance(c, dict) or not isinstance(c.get('rule_id'), str)
                   for c in value['checks'])):
        raise AuditError('审核回答须包含可读取的本阶段检查项。')
    ids = [c['rule_id'] for c in value['checks']]
    if len(ids) != len(set(ids)) or set(ids) != set(selected):
        raise AuditError('模型自测结果必须覆盖本阶段原审核项，不能串单、漏项或重复。')
    sources = {d['unit_id']: d for d in documents}
    normalize = lambda text: re.sub(r'\s+', '', str(text))
    issues = []
    def visit(item, path='', rule_id=None):
        if isinstance(item, dict):
            rule_id = item.get('rule_id', rule_id)
            for name, val in item.items():
                if name in {'source_ids', 'scope_source_ids'} and isinstance(val, list):
                    if not set(val).issubset(sources):
                        raise AuditError('模型不能引用本单原资料以外的来源。')
                if name == 'unit_id' and isinstance(val, str) and val not in sources:
                    raise AuditError('模型不能引用本单原资料以外的来源。')
            if isinstance(item.get('quote'), str) and isinstance(item.get('unit_id'), str):
                needle = normalize(item['quote'])
                facts = sources[item['unit_id']].get('facts', [])
                if not needle or not any(needle in normalize(fact) for fact in facts):
                    issues.append({'path': path + '/quote', 'rule_id': rule_id,
                        'constraint': 'quote_source_match', 'unit_id': item['unit_id'],
                        'quote': item['quote'], 'guidance': '程序未匹配到逐字片段；请结合该来源的完整原文自行复核。'
                        '这是辅助提示，执行模型确认后直接采用其判断，不要求修改到程序匹配成功。'})
            for name, val in item.items():
                visit(val, path + '/' + str(name).replace('~', '~0').replace('/', '~1'), rule_id)
        elif isinstance(item, list):
            for index, val in enumerate(item):
                visit(val, path + '/' + str(index), rule_id)
    visit(value)
    return {'status': 'model_takeover' if issues else 'available',
            'authoritative': False, 'issues': issues}


def provisional(case, value, selected, program):
    """A helper's semantic rejection is model input, never a stage gate."""
    source_advice = source_guard(value, case['documents'], selected)
    try:
        result = program() or {'checks': deepcopy(value['checks'])}
        advice = {'status': 'available', 'authoritative': False}
    except (AuditError, ValueError, KeyError, TypeError, ArithmeticError) as error:
        from .program_advice import error_advice
        result = {'checks': deepcopy(value['checks'])}
        advice = error_advice(error)
    if source_advice['issues']:
        advice.update(status='model_takeover', authoritative=False,
                      issues=[*advice.get('issues', []), *source_advice['issues']])
    return {**result, 'program_advice': advice}


def decision_check(scenario, check, documents):
    from .model_authority import enabled, decision
    if enabled():
        return decision(scenario,{'checks':[check]},documents)['checks'][0]
    from .pdf_policy import rules, SUBMITTED_SOURCE_CHECKS
    rule = next((r for r in rules(scenario) if r.id == check['rule_id']), None)
    requirement = rule.text if rule else SUBMITTED_SOURCE_CHECKS[check['rule_id']]['requirement']
    calculations = []
    from .pdf_evidence import calculate, document_map
    for comparison in check.get('comparisons', []):
        try:
            calculations.append(calculate(comparison, document_map(documents)))
        except (AuditError, ValueError, KeyError, TypeError, ArithmeticError):
            # A format unsupported by the calculator is not a business verdict.
            # Original operands and the model's completed judgment remain saved.
            continue
    return {**deepcopy(check), 'effect': rule.effect if rule else '', 'requirement': requirement,
            'comparisons': deepcopy(check.get('comparisons', [])), 'calculations': calculations}


def save_judgments(case, value, selected, *, authority='completed_model_self_check'):
    from .model_metrics import current_analysis_root
    from .workbench_store import atomic_write_json
    source_advice = source_guard(value, case['documents'], selected)
    root = current_analysis_root()
    if root is None:
        return  # Isolated callers have no run in which to publish authority.
    key = context_key(case['scenario'], case['flags'], case['documents'], case['materials'])
    for check in value['checks']:
        identity = digest(check)
        atomic_write_json(root / 'model-judgments' / key / (identity + '.json'), {
            'version': 1, 'context_sha256': key, 'check_sha256': identity,
            'check': check, 'decision': decision_check(case['scenario'], check, case['documents']),
            'authority': authority,
            'source_matching_advice': {**source_advice, 'issues': [i for i in source_advice['issues']
                if i['rule_id'] in (None, check['rule_id'])]}})


def approved_checks(scenario, flags, evidence, documents, materials, *, include_provisional=False):
    from .model_metrics import current_analysis_root
    root = current_analysis_root()
    if root is None:
        return {}
    key = context_key(scenario, flags, documents, materials)
    directory = root / 'model-judgments' / key
    result = {}
    for check in evidence['checks']:
        identity = digest(check)
        path = directory / (identity + '.json')
        if path.is_symlink():
            continue
        try:
            item = json.loads(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            continue
        authorities = {'completed_model_self_check', 'final_logic_review'}
        if include_provisional:
            authorities.add('provisional_model_result')
        if (item.get('authority') in authorities
                and item.get('context_sha256') == key and item.get('check_sha256') == identity
                and item.get('check') == check):
            source_guard({'checks': [check]}, documents, [check['rule_id']])
            result[check['rule_id']] = item['decision']
    return result


def apply_judgments(scenario, flags, evidence, documents, materials, units, selected, defer_native_pos, program):
    approved = approved_checks(scenario, flags, evidence, documents, materials, include_provisional=True)
    if not approved:
        return None
    source_guard(evidence, documents, selected or [c['rule_id'] for c in evidence['checks']])
    from .pdf_policy import POLICY_VERSION
    remaining = [c for c in evidence['checks'] if c['rule_id'] not in approved]
    result = program(scenario, flags, {**evidence, 'checks': remaining}, documents,
        materials, units=units, selected_rule_ids=[c['rule_id'] for c in remaining],
        defer_native_pos=defer_native_pos) if remaining else {
            'policy_version': POLICY_VERSION, 'scenario': scenario,
            **{key: deepcopy(val) for key, val in evidence.items() if key != 'checks'}}
    indexed = {c['rule_id']: c for c in result.get('checks', [])}
    indexed.update(approved)
    checks = [deepcopy(indexed[c['rule_id']]) for c in evidence['checks']]
    problems = [c for c in checks if c['status'] in {'fail', 'unknown'}]
    return {**result, 'checks': checks,
        'summary': {'conclusion': 'issues_found' if problems else 'no_issues_found',
                    'error_count': len(problems)},
        'noncompliance_registration': [c['reason'] for c in problems]}
