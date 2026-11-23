"""Run-owned helper observations, bound to the exact case sources.

These are model inputs, never automatic business verdicts. Saved POS proofs
from older workers are readable only after full current-source revalidation.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re

from .common import AuditError


def _digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str).encode()).hexdigest()


def _identity(case):
    corpus = case.get('_agent_corpus_fingerprint')
    if case.get('_agent_id') in {'visual', 'documents', 'numbers'} and isinstance(corpus, str) and re.fullmatch(r'[a-f0-9]{64}', corpus):
        return corpus
    return _digest({'archive_id':case.get('archive_id'), 'documents':case['documents'],
                    'materials':case.get('materials',[]), 'units':[
                        {k:u[k] for k in ('unit_id','source_kind','source_file','locator','file_id') if k in u}
                        for u in case.get('units',[])]})


def _implementation():
    from .local_source_repair import EDITABLE
    core=Path(__file__).resolve().parent
    names=EDITABLE|{'audit_tools.py','case_python.py','pdf_evidence.py',
                    'photo_product_references.py','product_reference_batches.py'}
    return _digest({name:hashlib.sha256((core/name).read_bytes()).hexdigest() for name in sorted(names)})


def _folder(case):
    from .model_metrics import current_analysis_root
    root=current_analysis_root()
    folder = root/'tool-observations'/_digest(case.get('archive_id'))/_identity(case) if root else None
    if folder and case.get('_agent_id') in {'visual', 'documents', 'numbers'}:
        folder = folder/'agents'/case['_agent_id']
    return folder


def remember(case, name, *, fields, code, source_ids, result, parameters=None):
    if name=='project_source' or not case.get('archive_id'):
        return
    request={'tool':name,'fields':list(fields),'source_ids':list(source_ids)}
    if parameters is not None:request['parameters']=deepcopy(parameters)
    if code is not None:request['code_sha256']=_digest(code)
    record={'version':1,'archive_id':case['archive_id'],'source_fingerprint':_identity(case),
        'request':request,'result':deepcopy(result),'authoritative':False,
        'implementation_fingerprint':_implementation()}
    if case.get('_agent_id'):
        record.update(agent_id=case['_agent_id'], execution_source_ids=[d['unit_id'] for d in case['documents']])
    key=_digest(request)
    folder=_folder(case)
    if folder:
        from .workbench_store import atomic_write_json
        atomic_write_json(folder/(key+'.json'),record)


def _legacy_pos(case):
    from .model_metrics import current_analysis_root
    root=current_analysis_root();aid=case.get('archive_id','')
    if root is None or not re.fullmatch(r'a[0-9]+',aid):return None
    path=root/'model-observations'/('tool-pos-calculation-'+aid+'.json')
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):return None
    from .pos_arithmetic import prepare_pos_arithmetic
    from .pdf_evidence import calculate,document_map
    saved=json.loads(path.read_text('utf-8'))
    plan=prepare_pos_arithmetic(case['documents'],case['materials'],case.get('units'),
                                amount_only=case.get('scenario') == 'pos_target_incentive')
    if plan is None or saved.get('plan')!=plan:return None
    sources=document_map(case['documents'])
    calculations=[calculate(comparison,sources) for comparison in plan['comparisons']]
    if saved.get('calculations')!=calculations:return None
    return {'version':1,'archive_id':aid,'source_fingerprint':_identity(case),
        'request':{'tool':'pos_calculation','fields':[],'source_ids':plan['source_ids']},
        'result':{'tool':'pos_calculation','complete':plan['complete'],'source_ids':plan['source_ids'],
            'calculation_count':len(calculations),'difference_count':sum(not c['matches'] for c in calculations),
            'limitations':plan['limitations'],'full_original_calculations_saved':True},
        'authoritative':False,'revalidated_against_current_sources':True,
        'implementation_fingerprint':_implementation(),
        'proof_artifact':str(path.relative_to(root))}


def observations(case):
    """Reuse this run's tool evidence after source checks; never change a check."""
    identity=_identity(case);implementation=_implementation();records={}
    def accept(record):
        if (record.get('version')==1 and record.get('archive_id')==case.get('archive_id')
                and record.get('source_fingerprint')==identity
                and record.get('implementation_fingerprint')==implementation):
            records[_digest(record['request'])]=deepcopy(record)
    folder=_folder(case)
    if folder and folder.is_dir():
        for path in sorted(folder.glob('*.json')):
            if path.is_symlink() or not path.resolve().is_relative_to(folder.resolve()):continue
            try:accept(json.loads(path.read_text('utf-8')))
            except (OSError,ValueError,KeyError,TypeError):continue
    for name,restore in [('pos_calculation',_legacy_pos),('product_identity',_legacy_identity)]:
        if case.get('_agent_id') == 'documents' or (case.get('_agent_id') == 'visual' and name == 'pos_calculation'):
            continue
        if any(r['request']['tool']==name for r in records.values()):continue
        try:
            legacy=restore(case)
            if legacy:accept(legacy)
        except (AuditError,OSError,ValueError,KeyError,TypeError,ArithmeticError):
            pass  # An unverifiable old proof cannot establish a completed check.
    return list(records.values())


def _legacy_identity(case):
    from .model_metrics import current_analysis_root
    root=current_analysis_root();aid=case.get('archive_id','')
    if root is None or not re.fullmatch(r'a[0-9]+',aid):return None
    paths=[root/'model-observations'/(prefix+aid+'.json')
           for prefix in ('photo-product-selection-','photo-product-database-')]
    if any(not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(root.resolve()) for p in paths):return None
    saved,catalog=[json.loads(path.read_text('utf-8')) for path in paths]
    from .photo_product_references import validate_selection,resolve_requests
    from .product_reference_batches import product_identity_support
    selection={'requests':[r['request'] for r in saved['resolutions']],'limitations':saved['limitations']}
    validate_selection(selection,case['documents'],saved['rule_ids'])
    products,resolutions=resolve_requests(selection,{'products':catalog['selected_products']})
    if resolutions!=saved['resolutions'] or any(r['status']!='resolved' for r in resolutions):return None
    support=product_identity_support(saved)
    by_code={p['product_code']:p for p in products}
    for identity in support.get('identities',[]):
        product=by_code.get(identity['product_code'])
        if not product or any(product.get(k)!=v for k,v in identity.items()):return None
    if {i['product_code'] for i in support.get('identities',[])}!=set(by_code):return None
    source_ids=sorted({f['unit_id'] for request in selection['requests']
        for k in ('product_code','product_name','barcode_69') if (f:=request[k])})
    return {'version':1,'archive_id':aid,'source_fingerprint':_identity(case),
        'request':{'tool':'product_identity','fields':[],'source_ids':source_ids},
        'result':support,'authoritative':False,'revalidated_against_current_sources':True,
        'implementation_fingerprint':_implementation(),
        'proof_artifacts':[str(p.relative_to(root)) for p in paths]}


def identity_support(case, records):
    return case.get('_product_identity_support') or next(
        (r['result'] for r in records if r['request']['tool']=='product_identity'),{})
