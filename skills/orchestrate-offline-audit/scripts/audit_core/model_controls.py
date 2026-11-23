"""Account-bound model/effort controls for DeepSeek and the Qwen image route."""
import hashlib
import json
import time

import httpx

from .model_catalog import QWEN_GROUPS, public_model_id
from .model_settings import ModelSettingsConflict, ModelSettingsError, api_selection

DEFAULT_ROUTES = {'deepseek_model':'deepseek-v4-pro', 'deepseek_effort':'max',
                  'qwen_model':'qwen3.8-max', 'qwen_effort':'xhigh'}

# Official image-input families; /models alone does not prove image support.
# https://help.aliyun.com/zh/model-studio/vision
# https://help.aliyun.com/zh/model-studio/qwen-omni
# https://docs.qwencloud.com/token-plan/best-practices/add-vision-skill
QWEN_IMAGE_MODELS = frozenset(model for names in QWEN_GROUPS.values()
    for model in names if not model.startswith('qwen3.7-max'))


def supports_images(item):
    model = public_model_id(item['id']).lower()
    if (not model.startswith('qwen') or model.startswith(('qwen3.7-max', 'qwen3-max'))
            or any(part in model for part in ('embedding', 'asr', 'tts', 'rerank', 'image'))):
        return False
    # Explicit machine-readable input metadata takes precedence over a known
    # family. Generic gateway tags such as "文本" are not input declarations.
    declarations = []
    for value in (item.get('input_modalities'), (item.get('modalities') or {}).get('input')
                  if isinstance(item.get('modalities'), dict) else None,
                  (item.get('architecture') or {}).get('input_modalities')
                  if isinstance(item.get('architecture'), dict) else None):
        if isinstance(value, list):
            declarations.append('image' in value)
    for value in (item.get('supports_vision'), (item.get('capabilities') or {}).get('vision')
                  if isinstance(item.get('capabilities'), dict) else None):
        if type(value) is bool:
            declarations.append(value)
    if declarations:
        return all(declarations)
    return model in QWEN_IMAGE_MODELS


def effort_choices(model, *, deepseek=False):
    """Expose actual levels, not several aliases for the same provider level."""
    if deepseek and (model.lower().startswith('deepseek-v4') or model == 'deepseek-flash'):
        values = [('none','none'), ('low','low'), ('high','high'), ('max','max')]
    elif model.startswith('qwen3.8-'):
        values = [('default','default（模型默认）'), ('none','none'), ('low','low'),
                  ('medium','medium'), ('xhigh','xhigh')]
    elif model.startswith('qwen3.5-omni-'):
        values = [('default','default（模型默认）')]
    elif deepseek:
        values = [('default','default（模型默认）'), ('none','thinking.type=disabled'),
                  ('enabled','thinking.type=enabled')]
    elif model in QWEN_IMAGE_MODELS and model.startswith(('qwen3.5-', 'qwen3.6-', 'qwen3.7-', 'qwen3-vl-')):
        # These earlier families expose a thinking switch/budget, not the
        # 3.8 effort enum. Do not invent low/high/max API support for them.
        values = [('default','default（模型默认）'), ('none','enable_thinking=false'),
                  ('enabled','enable_thinking=true')]
    else:
        values = [('default','default（模型默认）')]
    return [{'value':value, 'label':label} for value,label in values]


def canonical_effort(model, effort):
    if model.startswith('qwen3.8-') and effort in {'high','max'}:
        return 'xhigh'
    return effort


def _routes(store):
    selection = store.snapshot()
    if (selection.provider not in {'deepseek', 'zimaocloud'}
            or not selection.model.lower().startswith('deepseek') or not selection.visual):
        raise ModelSettingsError('请先配置DeepSeek与独立的Qwen连接。')
    if not public_model_id(selection.visual.model).startswith('qwen'):
        raise ModelSettingsError('图片连接只开放Qwen模型。')
    return selection, selection.visual


def current_controls(store):
    main, visual = _routes(store)
    return {'deepseek_model':main.model, 'deepseek_effort':main.reasoning_effort,
            'qwen_model':visual.model, 'qwen_effort':canonical_effort(visual.model, visual.reasoning_effort)}


def _directory(selection, transport):
    from .qwen_model import QwenRequestError, _http_error
    try:
        with httpx.Client(timeout=30,trust_env=False,follow_redirects=False,transport=transport) as client:
            with client.stream('GET',selection.base_url.rstrip('/')+'/models',
                    headers={'Authorization':'Bearer '+selection.api_key}) as response:
                if response.status_code != 200:
                    raise _http_error(response.status_code,b'',selection.provider)
                raw=bytearray()
                for chunk in response.iter_bytes():
                    raw.extend(chunk)
                    if len(raw)>4*1024*1024:
                        raise ValueError()
        value=json.loads(raw)
        rows=value['data']
        if not isinstance(rows,list):
            raise ValueError()
        return [row for row in rows if isinstance(row,dict) and isinstance(row.get('id'),str)]
    except (httpx.HTTPError,ValueError,KeyError,TypeError):
        raise QwenRequestError('模型目录暂时无法读取，请稍后刷新。','configuration') from None


def catalog(store, *, refresh=False, transport=None):
    main, visual = _routes(store)
    identity=hashlib.sha256(json.dumps([(item.provider,item.base_url,item.api_key)
        for item in (main,visual)]).encode()).hexdigest()
    cached=getattr(store,'_route_catalog',None)
    if not refresh and cached and cached['identity']==identity and time.monotonic()-cached['time']<300:
        return cached['value']
    rows={}
    for group,selection in (('deepseek',main),('qwen',visual)):
        result=[]; seen=set()
        for item in _directory(selection,transport):
            model=item['id']; public=public_model_id(model)
            if model in seen or item.get('status',1)!=1:
                continue
            if group=='deepseek' and not public.lower().startswith('deepseek'):
                continue
            if group=='qwen' and not supports_images(item):
                continue
            seen.add(model)
            result.append({'id':model,'label':item.get('name') or public,
                'efforts':effort_choices(public,deepseek=group=='deepseek'),
                'enabled':True})
        if not result:
            raise ModelSettingsError('当前账号没有返回可用的'+('DeepSeek' if group=='deepseek' else 'Qwen')+'型号。')
        rows[group]=result
    defaults=dict(DEFAULT_ROUTES)
    # Defaults must exist in this account; never borrow another provider/key.
    for group, current in (('deepseek', main), ('qwen', visual)):
        if defaults[group+'_model'] not in {row['id'] for row in rows[group]}:
            row=next((row for row in rows[group] if row['id']==current.model), rows[group][0])
            defaults[group+'_model']=row['id']
            levels={item['value'] for item in row['efforts']}
            defaults[group+'_effort']=canonical_effort(row['id'], current.reasoning_effort)
            if defaults[group+'_effort'] not in levels:
                defaults[group+'_effort']=row['efforts'][-1]['value']
    value={**rows,'defaults':defaults}
    store._route_catalog={'identity':identity,'time':time.monotonic(),'value':value}
    return value


def probe_text_json(selection):
    """Verify a text route without asking a text-only Pro model to read pixels."""
    from .qwen_model import complete_json, QwenRequestError
    answer,metrics=complete_json(selection,[{'type':'text','text':'仅返回JSON：{"ok":true}。'}],
        system='这是文本与JSON连接检查。仅返回要求的JSON。',timeout_seconds=45)
    try:
        value=json.loads(answer)
    except (ValueError,TypeError):
        value=None
    if value!={'ok':True}:
        raise QwenRequestError('DeepSeek文本与JSON连接检查未通过，已保留原配置。','invalid_output')
    return {'model':selection.model,'reasoning_effort':selection.reasoning_effort,
            'elapsed_seconds':metrics.get('elapsed_seconds')}


def save_controls(store, payload, *, transport=None, probe=None):
    """Validate against each account, probe changed models, then write once."""
    expected=payload['revision']
    if type(expected) is not int or store.public()['revision']!=expected:
        raise ModelSettingsConflict('模型配置已在其他页面改变，请刷新后再操作。')
    main,visual=_routes(store)
    choices=catalog(store,transport=transport)
    selected=[]
    for group,current in (('deepseek',main),('qwen',visual)):
        model=payload[group+'_model']; effort=payload[group+'_effort']
        if not isinstance(model,str) or not isinstance(effort,str):
            raise ModelSettingsError('模型与推理强度必须是选项中的文字。')
        row=next((row for row in choices[group] if row['id']==model and row['enabled']),None)
        if not row or effort not in {item['value'] for item in row['efforts']}:
            raise ModelSettingsError('请选择当前账号开放的模型及该型号支持的推理强度。')
        if model==current.model and effort==canonical_effort(model,current.reasoning_effort):
            # Display legacy max/high as xhigh without rewriting an untouched
            # route when only the other route is saved.
            effort=current.reasoning_effort
        selected.append(api_selection({'provider':current.provider,'model':model,
            'base_url':current.base_url,'api_key':current.api_key,'reasoning_effort':effort}))
    from .qwen_model import probe_qwen
    checks=[]
    for group,current,candidate in zip(('deepseek','qwen'),(main,visual),selected):
        if current.model!=candidate.model:
            if probe:
                probe(candidate,group)
            elif group=='deepseek':
                probe_text_json(candidate)
            else:
                probe_qwen(candidate)
            checks.append({'route':group,'model':candidate.model,'passed':True})
    settings=store.save_routes(*selected,expected_revision=expected)
    return {'settings':settings,'controls':current_controls(store),'catalog':choices,
            'checks':checks,'message':'模型和推理强度已保存，新启动的核销使用此配置。'}
