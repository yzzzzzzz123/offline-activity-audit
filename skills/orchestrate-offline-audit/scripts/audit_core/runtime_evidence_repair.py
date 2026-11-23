"""Data-only execution repair. No code generation, eval, shell, or source writes.

The agent supplies original field bindings; the host independently verifies
their source, coverage, roles, units and arithmetic through the normal checker.
Bindings are scoped to a run AND the complete immutable source document hash.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
import hashlib
import json
import re

from .common import AuditError

_STATE = ContextVar('audit_runtime_evidence_repairs', default=None)
ROLES = ('quantity', 'unit_price', 'headcount', 'days', 'daily_wage', 'total', 'claim')
FIELD_SCHEMA = {'type':'object','additionalProperties':False,
    'required':['role','fact_index','number','label'], 'properties':{
    'role':{'enum':list(ROLES)},'fact_index':{'type':'integer','minimum':0},
    'number':{'type':'string','minLength':1},'label':{'type':'string','minLength':1}}}
BINDING_SCHEMA = {'type':'object','additionalProperties':False,
    'required':['unit_id','fields','explanation'], 'properties':{
    'unit_id':{'type':'string','minLength':1},'fields':{'type':'array','minItems':3,'items':FIELD_SCHEMA},
    'explanation':{'type':'string','minLength':1}}}

@contextmanager
def repair_scope():
    token = _STATE.set({})
    try:yield
    finally:_STATE.reset(token)

def fingerprint(document):
    return hashlib.sha256(json.dumps(document,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def basis_for(documents):
    if len(documents)!=1 or _STATE.get() is None:return None
    value = _STATE.get().get(fingerprint(documents[0]))
    return deepcopy(value) if value else None

def claim_operand(operand, documents):
    from .pdf_evidence import _number
    for document in documents:
        basis = basis_for([document])
        if not basis:continue
        for claim in basis['claims']:
            if operand['unit_id']!=claim['unit_id'] or _number(operand['number'])!=_number(claim['number']):continue
            # A shared numeric value is insufficient: the operand's occurrence
            # must cover the actual claim label and number in the same fact.
            if operand['quote'] in claim['quote'] and basis['claim_label'] in operand['quote']:
                return True
    return False

def bind_settlement(binding, documents, materials):
    from .pdf_evidence import _number
    import jsonschema
    jsonschema.validate(binding,BINDING_SCHEMA)
    state = _STATE.get()
    if state is None:raise AuditError('临时字段绑定必须位于本单执行上下文')
    ids = {uid for m in materials if m['id']=='settlement' and m['state']=='present' for uid in m['source_ids']}
    source = [d for d in documents if d['unit_id'] in ids]
    if len(source)!=1 or source[0]['unit_id']!=binding['unit_id']:
        raise AuditError('临时单笔字段必须来自本次唯一完整结算单，不能跨页、跨角色或跨费用')
    document = source[0]
    if any(re.search(r'未完整|截断|缺行|部分.*未读',s) for s in document.get('limitations',[])):
        raise AuditError('不完整读取不能建立临时完整费用依据')
    fields = {}; used = set(); bound_spans={}; facts = document['facts']
    # These role constraints are independent of document-prefix parsing.
    labels = {'quantity':r'数量', 'unit_price':r'单价|金额|费用', 'headcount':r'人数|人员数量|临促数量',
              'days':r'天数', 'daily_wage':r'工资|日薪', 'total':r'合计|总计|总额',
              'claim':r'核销.*金额|实际结算金额|实报金额|申请.*核销'}
    for field in binding['fields']:
        role=field['role']; index=field['fact_index']; label=field['label']
        if role in fields or index>=len(facts):raise AuditError('临时字段角色重复或来源位置不存在')
        fact=facts[index]
        if label not in fact or not re.search(labels[role],label):raise AuditError('临时字段角色须由原始字段名称证明')
        number=field['number']; _number(number)
        pattern=re.escape(label)+r'[\s_＿]*(?:[:：=]|为|是)?[\s_＿¥￥]*('+re.escape(number)+r')(?![\d.,，])'
        matches=list(re.finditer(pattern,fact))
        if len(matches)!=1:raise AuditError('字段标签必须绑定本处真实数字，不能借用同值其他字段')
        bound_spans.setdefault(index,set()).add(matches[0].span(1))
        if role in {'unit_price','daily_wage'} and not re.match(r'[\s_＿]*元\s*[/／每]\s*[\u4e00-\u9fff]+',fact[matches[0].end():]):
            raise AuditError('临时单价须有原文每单位依据')
        fields[role]={'unit_id':document['unit_id'],'quote':fact,'number':number};used.add(index)
    profiles=[roles for roles in [('quantity','unit_price'),('headcount','days','daily_wage')]
              if set(roles).issubset(fields)]
    if len(profiles)!=1 or 'total' not in fields or set(fields)-set(profiles[0])-{'total','claim'}:
        raise AuditError('临时费用必须具备唯一完整计费因子和唯一合计')
    # Extra fees, totals or unknown table rows cannot disappear from the proof.
    from .comparison_policy import ORIGINAL_NUMBER
    numeric_fields=re.compile(r'(?:金额|费用|工资|单价|合计|总计|人数|数量|天数)[”"’\s_＿]*'
        r'(?:[:：=]|为|是)?[\s¥￥_＿]*('+ORIGINAL_NUMBER+r')(?![\d.,，])')
    table_markers=0;row_indices=[]
    for index,fact in enumerate(facts):
        # Cover every numeric field occurrence, not merely every selected fact.
        # Otherwise three multi-fee rows could be mixed by selecting one factor
        # from each and marking all of those rows as "used".
        if any(match.span(1) not in bound_spans.get(index,set()) for match in numeric_fields.finditer(fact)):
            raise AuditError('临时费用依据须覆盖每处真实数字字段，不能隐藏同一行的其他费用或计费因子')
        row_number=re.search(r'(?:第\s*(\d+)\s*行|(?:表格|明细)行\s*(\d+))',fact)
        if row_number:row_indices.append(int(row_number[1] or row_number[2]))
        if re.match(r'\s*(?:表格|明细表)(?:标题|表头|项目)\s*[:：]',fact):table_markers+=1
        if index in used:continue
        if re.search(r'(?:金额|费用|工资|单价|合计|总计|人数|数量|天数)\s*[”"’\s_＿]*(?:[:：=]|为|是)\s*[¥￥_＿]*\s*\d',fact):
            raise AuditError('临时费用依据不能遗漏额外字段或费用')
        if re.search(r'第\s*\d+\s*行|(?:表格|表内|明细)(?:行\s*\d*)?\s*[:：]',fact):
            if not re.search(r'活动(?:时间|日期|周期)\s*[:：]\s*[\d年月日\s\-—/._＿]+[。.]?$',fact):
                raise AuditError('未知明细行不能静默略过')
    if table_markers>1:raise AuditError('多表块不能作为唯一费用')
    if len(row_indices)!=len(set(row_indices)):raise AuditError('重复行号不能作为唯一费用')
    basis={'total':fields['total'],'factors':[fields[r] for r in profiles[0]],
           'claims':[fields['claim']] if 'claim' in fields else [],
           'claim_label':next((f['label'] for f in binding['fields'] if f['role']=='claim'),'')}
    key=fingerprint(document); previous=state.get(key);state[key]=basis
    return key,previous

def restore_binding(key, previous):
    if _STATE.get() is not None:
        if previous is None:_STATE.get().pop(key,None)
        else:_STATE.get()[key]=previous

def load_saved_bindings(documents, materials):
    """Replay same-run, exact-source repair data through today's full verifier."""
    from .model_metrics import current_analysis_root
    analysis=current_analysis_root();state=_STATE.get()
    if analysis is None or state is None:return
    signature=hashlib.sha256(json.dumps(documents,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
    marker='loaded:'+signature
    if marker in state:return
    state[marker]=True
    # Execution data already belongs to the ordinary trace. A maintenance todo
    # is optional and must never be required for a valid same-run checkpoint.
    for folder, pattern in (('execution-trace','step-*.json'),('repair-items','*.json')):
        directory=analysis/folder
        if directory.is_symlink():continue
        for path in sorted(directory.glob(pattern)):
            if path.is_symlink():continue
            with path.open(encoding='utf-8') as stream:
                if folder=='execution-trace':
                    # Trace metadata precedes potentially large model inputs.
                    # Read full content only for the small completion events.
                    prefix=stream.read(512)
                    if not re.search(r'"type"\s*:\s*"audit\.execution_repair\.completed"',prefix):continue
                    item=json.loads(prefix+stream.read())
                else:
                    item=json.load(stream)
            if folder=='execution-trace':
                if item.get('type')!='audit.execution_repair.completed':continue
                item=item.get('data',{})
                if item.get('original_validation_passed') is not True:continue
                binding=item.get('runtime_binding')
            else:
                # Compatibility for older same-run checkpoints; new todos do
                # not duplicate the binding or execution history.
                if item.get('runtime_status')!='recovered':continue
                binding=item.get('runtime_solution')
            if (item.get('source_fingerprint')==signature
                    and isinstance(binding,dict) and 'fields' in binding):
                bind_settlement(binding,documents,materials or [])
