"""Run model-written analysis against copies of this case's source facts."""
from pathlib import Path
import json
import os
import subprocess
import sys
import uuid


def run(case, code, source_ids):
    from .common import AuditError
    from .execution_trace import record_step
    from .model_metrics import current_analysis_root
    from .workbench_store import atomic_write_json
    from tempfile import TemporaryDirectory
    from contextlib import nullcontext
    from copy import deepcopy
    sources = {d['unit_id']: d for d in case['documents']}
    if not set(source_ids).issubset(sources):
        raise AuditError('临时工具只能读取本单来源，不能跨单或访问任意路径。')
    if not isinstance(code, str) or not code.strip():
        raise AuditError('临时工具需要本次模型生成的Python代码。')
    analysis = current_analysis_root()
    with (nullcontext(analysis / 'generated-tools') if analysis else TemporaryDirectory(prefix='audit-generated-')) as folder:
        root = Path(folder)
        root.mkdir(parents=True, exist_ok=True)
        if root.is_symlink(): raise AuditError('临时工具目录不允许链接。')
        identity = uuid.uuid4().hex
        script = root / (identity + '.py')
        script.write_text(code, encoding='utf-8')
        selected = source_ids or list(sources)
        payload = {'code': code, 'documents': [deepcopy(sources[uid]) for uid in selected],
                   'materials': deepcopy(case.get('materials', [])), 'flags': deepcopy(case.get('flags', {}))}
        record_step('audit.generated_tool.started', '执行模型生成本单临时分析工具', {
            'tool_id': identity, 'source_ids': selected, 'code': code,
            'original_sources_writable': False, 'network_available': False}, status='running')
        environment = {key: os.environ[key] for key in ('SystemRoot', 'WINDIR', 'TEMP', 'TMP') if key in os.environ}
        environment['PYTHONIOENCODING'] = 'utf-8'
        try:
            process = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8', str(Path(__file__).resolve())],
                input=json.dumps(payload, ensure_ascii=False), capture_output=True, text=True,
                encoding='utf-8', env=environment, timeout=30,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            result = json.loads(process.stdout) if process.stdout else {'ok': False, 'error': '临时工具未返回结果。'}
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            result = {'ok': False, 'error_type': type(error).__name__,
                      'error': '本次临时工具未完成；执行模型可调整工具或直接读取原资料处理。'}
        receipt = {'tool': 'case_python', 'tool_id': identity, 'model_calls': 0,
                   'complete': result.get('ok') is True, 'source_ids': selected,
                   'status': 'completed' if result.get('ok') else 'model_takeover',
                   'scope': '模型生成工具的派生结果，仅供本单复核，不是原始凭据或业务裁定。', **result}
        atomic_write_json(root / (identity + '.json'), receipt)
        record_step('audit.generated_tool.completed', '本单临时分析工具返回，交模型判断', receipt,
                    status='completed' if receipt['complete'] else 'needs_model')
        return receipt


def _worker():
    import ast
    import builtins
    import decimal
    import math
    import re
    import statistics
    from types import SimpleNamespace
    # Physical tool limits do not limit model context or decide business results.
    job = None
    if os.name == 'nt':
        import win32api
        import win32job
        job = win32job.CreateJobObject(None, '')
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
        limits['ProcessMemoryLimit'] = 512 * 1024 * 1024
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
        win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    else:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (30, 30))
    packet = json.load(sys.stdin)
    tree = ast.parse(packet['code'], filename='case-generated-tool')
    denied = (ast.Import, ast.ImportFrom, ast.ClassDef, ast.With, ast.AsyncWith, ast.Global, ast.Nonlocal)
    for node in ast.walk(tree):
        if isinstance(node, denied) or isinstance(node, ast.Attribute) and node.attr.startswith('_'):
            raise ValueError('临时分析只读取所给资料，不开放宿主导入、内部属性、文件或网络权限。')
        names = [getattr(node, key, '') for key in ('id', 'arg', 'name')]
        if any(isinstance(name, str) and name.startswith('__') for name in names):
            raise ValueError('临时分析不访问宿主内部名称。')
    allowed = ('abs','all','any','bool','dict','enumerate','filter','float','format','frozenset','int','isinstance',
               'len','list','map','max','min','next','pow','range','reversed','round','set','slice','sorted',
               'str','sum','tuple','zip','Exception','ValueError','TypeError')
    scope = {'__builtins__': {key: getattr(builtins, key) for key in allowed},
             'documents': packet['documents'], 'materials': packet['materials'], 'flags': packet['flags'],
             'Decimal': decimal.Decimal,
             'json': SimpleNamespace(loads=json.loads, dumps=json.dumps),
             're': SimpleNamespace(**{name: getattr(re, name) for name in
                 ('findall','finditer','fullmatch','match','search','split','sub','escape','compile','IGNORECASE','MULTILINE','DOTALL')}),
             'math': SimpleNamespace(**{name: getattr(math, name) for name in dir(math) if not name.startswith('_')}),
             'statistics': SimpleNamespace(mean=statistics.mean, median=statistics.median)}
    by_id = {d['unit_id']: d for d in packet['documents']}
    scope['source'] = lambda unit_id: by_id[unit_id]
    exec(compile(tree, 'case-generated-tool', 'exec'), scope, scope)
    if 'result' not in scope: raise ValueError('请将本单工具的可复核输出放在result变量。')
    sys.stdout.write(json.dumps({'ok': True, 'result': scope['result']}, ensure_ascii=False,
                               allow_nan=False, default=str))


if __name__ == '__main__':
    try:
        _worker()
    except Exception as error:
        sys.stdout.write(json.dumps({'ok': False, 'error_type': type(error).__name__, 'error': str(error)}, ensure_ascii=False))
