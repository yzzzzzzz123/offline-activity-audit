"""Keep TP workflow state distinct from the latest node amount and final approval.

Official TP query: verify_status=3 means in progress; verify_amount and
details.audit_amount describe the latest node, not necessarily final approval.
This guard preserves a real pending state without declaring client data absent.
"""
from decimal import Decimal, InvalidOperation
import json


SOURCE_LABEL = '勤策核销审批状态'
STATUS_NAMES = {'0': '待审批', '1': '已审批', '2': '被打回',
                '3': '审批中', '4': '已作废', '5': '已撤回'}


def constrain_approval_state(check, documents, units):
    if check['rule_id'] != 'claim_ceiling' or check['status'] == 'not_applicable':
        return None
    ids = {u['unit_id'] for u in units or []
           if u.get('source_kind') == 'qince' and u.get('source_file') == SOURCE_LABEL}
    states = []
    for document in documents:
        if document['unit_id'] not in ids:
            continue
        fields = {}
        for fact in document['facts']:
            key, sep, raw = fact.partition('=')
            if sep:
                try:
                    fields[key] = json.loads(raw)
                except (ValueError, TypeError):
                    pass
        if 'verify_status' in fields:
            states.append((document['unit_id'], fields))
    if not states:
        return None
    signatures = {(str(s.get('verify_code')), str(s['verify_status']),
                   str(s.get('reverse_status')), str(s.get('verify_amount'))) for _, s in states}
    if len(signatures) != 1:
        reason = '勤策返回的核销审批状态或当前节点金额存在冲突，暂时不能确认最终批复金额。'
    else:
        fields = states[0][1]
        status = str(fields['verify_status'])
        reverse = str(fields.get('reverse_status', '0'))
        if reverse in {'1', '2'}:
            reason = '勤策核销单已有红冲状态，不能将当前节点金额作为有效的最终批复金额。'
        elif status == '1':
            return None  # Actual independent amount/source checks still apply.
        else:
            state_name = STATUS_NAMES.get(status, '未识别的审批状态')
            amount = fields.get('verify_amount')
            try:
                valid = not isinstance(amount, bool) and Decimal(str(amount)).is_finite()
            except (InvalidOperation, ValueError):
                valid = False
            amount_text = f'，当前审批节点金额为{amount}元' if valid else ''
            reason = f'勤策核销单目前为{state_name}{amount_text}；尚未形成有效的最终批复金额，本项待审批完成后核对。'
    return {**check, 'status': 'unknown', 'reason': reason, 'comparisons': [],
            'source_ids': list(dict.fromkeys(check.get('source_ids', []) + [uid for uid, _ in states]))}
