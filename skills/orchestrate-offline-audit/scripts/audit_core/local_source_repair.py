"""Operator-authorized source maintenance for this Windows 8080 installation.

The broker and its policy cannot be edited by the execution model. Server
deployments cannot acquire this capability from a URL, request body or flag.
"""
from __future__ import annotations

import ast
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import uuid

from .common import AuditError
from .paths import PROJECT_ROOT

LOCAL_PROJECT = Path('D:/codex/offline-activity-audit')
CORE = Path('skills/orchestrate-offline-audit/scripts/audit_core')
EDITABLE = frozenset({'comparison_policy.py', 'qince_amount_basis.py', 'activity_periods.py',
    'native_pos_fields.py', 'native_pos_matrix.py', 'pos_arithmetic.py', 'reference_matching.py',
    'spreadsheet_rows.py', 'price_support_dates.py'})
READABLE = EDITABLE | {'pdf_evidence.py', 'model_judgment.py', 'program_advice.py', 'audit_rag.py'}
PROTECTED_FUNCTIONS = {'expected_operator', 'settlement_purpose'}
REGRESSION_TESTS = ('tests.test_vertical_settlement_field_groups',
    'tests.test_application_total_source_contract', 'tests.test_pos_arithmetic_coverage',
    'tests.test_pdf_policy', 'tests.test_program_advice')
PURE_IMPORTS = {'re', 'json', 'math', 'statistics', 'decimal', 'fractions', 'collections',
    'copy', 'datetime', 'calendar', 'itertools', 'functools', 'operator', 'unicodedata'}
FORBIDDEN_NAMES = {'os', 'sys', 'subprocess', 'socket', 'requests', 'httpx', 'urllib', 'Path',
    'open', 'eval', 'exec', 'compile', '__import__', 'getattr', 'setattr', 'delattr',
    'globals', 'locals', 'vars', 'input', 'exit', 'quit', 'breakpoint', 'builtins',
    'SystemExit', 'KeyboardInterrupt', 'GeneratorExit',
    'PROJECT_ROOT', 'SKILL_ROOT', 'INPUT_ROOT', 'RUNTIME_ROOT', 'POLICY_ROOT', 'CONTRACTS_ROOT'}
FORBIDDEN_ATTRS = {'system', 'popen', 'spawn', 'environ', 'getenv', 'read_text', 'write_text',
    'read_bytes', 'write_bytes', 'unlink', 'rmdir', 'mkdir', 'rename', 'touch', 'chmod',
    'connect', 'urlopen', 'import_module', 'attrgetter', 'methodcaller', 'f_globals',
    'f_locals', 'gi_frame', 'cr_frame', 'tb_frame', 'mro'}


def policy_path():
    return Path(os.environ.get('LOCALAPPDATA', ''))/'CodexPrivate/OfflineAudit/local-source-repair.json'


def machine_fingerprint():
    if os.name != 'nt':
        return None
    import winreg
    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SOFTWARE\Microsoft\Cryptography',
                        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
        identity = winreg.QueryValueEx(key, 'MachineGuid')[0]
    return hashlib.sha256(str(identity).encode()).hexdigest()


def _parent_is_local_service():
    # Fixed OS metadata query; no command or path supplied by a model is used.
    query = (f"Get-CimInstance Win32_Process -Filter 'ProcessId={os.getppid()}' | "
             "Select-Object -ExpandProperty CommandLine | ConvertTo-Json -Compress")
    result = subprocess.run([r'C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe',
        '-NoProfile', '-NonInteractive', '-Command', query], capture_output=True, text=True,
        timeout=15, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        return False
    command = json.loads(result.stdout or 'null')
    if not isinstance(command, str):
        return False
    import ctypes
    from ctypes import wintypes
    argc = ctypes.c_int()
    split = ctypes.windll.shell32.CommandLineToArgvW
    split.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    split.restype = ctypes.POINTER(wintypes.LPWSTR)
    argv = split(command, ctypes.byref(argc))
    if not argv:
        return False
    try:
        parts = [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(argv, ctypes.c_void_p))
    entries = [PROJECT_ROOT/'skills/orchestrate-offline-audit/scripts/serve.py', PROJECT_ROOT/'deploy/service.py']
    if not any(os.path.normcase(str(p.resolve())) in {os.path.normcase(x) for x in parts} for p in entries):
        return False
    return '--port' in parts and parts[parts.index('--port')+1] == '8080'


def authorized():
    try:
        if os.name != 'nt' or not os.environ.get('LOCALAPPDATA') or PROJECT_ROOT.resolve() != LOCAL_PROJECT.resolve():
            return False
        policy = json.loads(policy_path().read_text('utf-8'))
        if (policy.get('enabled') is not True or policy.get('machine_sha256') != machine_fingerprint()
                or Path(policy.get('project_root', '')).resolve() != PROJECT_ROOT.resolve()
                or policy.get('port') != 8080):
            return False
        if Path(sys.argv[0]).resolve() != PROJECT_ROOT/'skills/orchestrate-offline-audit/scripts/run.py':
            return False
        from .model_metrics import current_analysis_root
        root = current_analysis_root()
        if root is None or root.name != 'analysis' or not root.resolve().is_relative_to(PROJECT_ROOT/'worktrees'):
            return False
        return _parent_is_local_service()
    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
        return False


def protocol():
    if not authorized():
        return '正式/未授权环境无项目源码写权限；保留本单case_python、临时字段绑定和模型自主处理，不修改共享源码。'
    return ('本机已授权project_source工具：确定性工具出现兼容性错误时，优先修复已有项目源码，'
        '不要另起一套临时实现反复处理。先读取报错对应函数，再修改同一函数并验证，成功后原步骤立即使用新函数。'
        '临时工具和字段绑定仍保留，可用于验证或当前资料确有特殊性时处理。'
        '调用tool=project_source、fields=[]、source_ids=[]，code为JSON。'
        '读取：{"action":"read","module":"comparison_policy.py","function":"_claim_operand"}；'
        '省略module列出可修复模块，省略function查看该模块函数目录。'
        '修改：{"action":"patch","module":"comparison_policy.py","expected_sha256":"读取返回的指纹",'
        '"reason":"通用兼容原因","changes":[{"function":"_claim_operand","source":"完整同签名函数源码"}],'
        '"probes":[{"function":"_claim_operand","args":[],"kwargs":{},"expected":null}]}。'
        '只修已有确定性函数的实现，不改业务标准、权限边界、配置、原件或其他单据。'
        '工具在隔离副本运行固定回归和可选探针，验证通过才原子写入真实源码并更新当前进程函数；'
        '失败保留原代码并返回原因，成功的通用修复供后续本地任务复用，服务器永不开放。')


def _path(module):
    if module not in READABLE or Path(module).name != module:
        raise AuditError('不允许读取或修改此源码模块。')
    path = PROJECT_ROOT/CORE/module
    if (path.is_symlink() or not path.resolve().is_relative_to(PROJECT_ROOT.resolve())
            or path.resolve().parent != (PROJECT_ROOT/CORE).resolve() or path.stat().st_nlink != 1):
        raise AuditError('源码路径必须是项目内独立的普通文件。')
    return path


def _functions(source):
    return {node.name: node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef)}


def _changed_source(source, changes):
    functions = _functions(source)
    lines = source.splitlines(keepends=True)
    edits, seen = [], set()
    for change in changes:
        name = change['function']
        old = functions.get(name)
        if old is None or name in seen or name in PROTECTED_FUNCTIONS or old.decorator_list:
            raise AuditError('只能修改目录中已存在、未受保护且不带装饰器的确定性函数。')
        seen.add(name)
        replacement = textwrap.dedent(change['source']).strip()+'\n'
        tree = ast.parse(replacement)
        if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
            raise AuditError('修改必须是一个完整的既有函数，不允许顶层执行代码。')
        new = tree.body[0]
        before, after = deepcopy(old), deepcopy(new)
        before.body = []; after.body = []
        if ast.dump(before, include_attributes=False) != ast.dump(after, include_attributes=False):
            raise AuditError('源码修复保留既有函数名称、签名、装饰器和返回声明。')
        old_imports = {ast.dump(n) for n in ast.walk(old) if isinstance(n, (ast.Import, ast.ImportFrom))}
        for node in ast.walk(new):
            if isinstance(node, ast.Name) and (node.id in FORBIDDEN_NAMES or node.id.startswith('__')):
                raise AuditError('确定性源码修复不开放宿主、密钥、动态执行或文件网络权限。')
            if isinstance(node, ast.Attribute) and (node.attr.startswith('__') or node.attr in FORBIDDEN_ATTRS):
                raise AuditError('确定性源码修复不开放内部属性或宿主读写。')
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                raise AuditError('确定性函数不能改写模块或其他任务的全局状态。')
            if isinstance(node, (ast.Import, ast.ImportFrom)) and ast.dump(node) not in old_imports:
                names = [a.name.split('.')[0] for a in node.names] if isinstance(node, ast.Import) else [(node.module or '').split('.')[0]]
                if getattr(node, 'level', 0) or not set(names).issubset(PURE_IMPORTS):
                    raise AuditError('新增导入仅限纯计算和文本处理标准库。')
        edits.append((old.lineno-1, old.end_lineno, replacement.replace('\n', '\r\n') if '\r\n' in source else replacement))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    updated = ''.join(lines)
    compile(updated, '<local-source-repair>', 'exec')
    return updated, seen


@contextmanager
def _lock():
    import msvcrt
    path = policy_path().with_name('source-repair.lock')
    with path.open('a+b') as stream:
        stream.seek(0); stream.write(b'0'); stream.flush(); stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise AuditError('其他本地任务正在验证源码修复，请重新读取当前版本后再试。') from exc
        try:
            yield
        finally:
            stream.seek(0); msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _write(path, contents):
    temporary = path.with_name(path.name+'.source-repair-'+uuid.uuid4().hex+'.tmp')
    try:
        with temporary.open('xb') as output:
            output.write(contents); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():temporary.unlink()


_RUNNER = '''import importlib.abc, importlib.util, json, pathlib, sys, unittest
stage, project, module, probes, suites = sys.argv[1:]
project = pathlib.Path(project); core = project/'skills/orchestrate-offline-audit/scripts/audit_core'
sys.path.insert(0, str(project))
class Loader(importlib.abc.Loader):
 def create_module(self, spec): return None
 def exec_module(self, obj):
  name = obj.__name__.split('.')[-1]; filename = '__init__.py' if obj.__name__=='audit_core' else name+'.py'
  obj.__file__=str(core/filename)
  if obj.__name__=='audit_core':obj.__path__=[str(core)]
  exec(compile((pathlib.Path(stage)/filename).read_text('utf-8'),obj.__file__,'exec'),obj.__dict__)
class Finder(importlib.abc.MetaPathFinder):
 def find_spec(self,name,path=None,target=None):
  if name=='audit_core' or name.startswith('audit_core.'):
   return importlib.util.spec_from_loader(name,Loader(),is_package=name=='audit_core')
sys.meta_path.insert(0,Finder())
result=unittest.TextTestRunner(verbosity=1).run(unittest.defaultTestLoader.loadTestsFromNames(json.loads(suites)))
if not result.wasSuccessful():sys.exit(1)
import importlib
subject=importlib.import_module('audit_core.'+module[:-3])
for probe in json.loads(pathlib.Path(probes).read_text('utf-8')):
 actual=getattr(subject,probe['function'])(*probe.get('args',[]),**probe.get('kwargs',{}))
 if actual!=probe['expected']:raise AssertionError('compatibility probe failed: '+probe['function'])
'''


def _test(folder, path, candidate, probes):
    stage = folder/'audit_core'; stage.mkdir()
    for original in (PROJECT_ROOT/CORE).glob('*.py'):
        (stage/original.name).write_bytes(original.read_bytes())
    if candidate is not None:(stage/path.name).write_bytes(candidate.encode('utf-8'))
    runner = folder/'verify.py'; runner.write_text(_RUNNER, encoding='utf-8')
    probe_file = folder/'probes.json'; probe_file.write_text(json.dumps(probes,ensure_ascii=False),encoding='utf-8')
    env = {key:value for key,value in os.environ.items() if not key.startswith('OFFLINE_AUDIT') and key != 'PYTHONPATH'}
    env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONIOENCODING='utf-8', PYTHONNOUSERSITE='1')
    completed = subprocess.run([sys.executable, '-B', str(runner), str(stage), str(PROJECT_ROOT), path.name,
        str(probe_file), json.dumps(REGRESSION_TESTS)], cwd=folder, env=env,
        capture_output=True, encoding='utf-8', timeout=120, creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    (folder/'tests.log').write_text(completed.stdout+completed.stderr, encoding='utf-8')
    return {'passed':completed.returncode == 0, 'exit_code':completed.returncode,
            'log':str(folder/'tests.log'), 'feedback':(completed.stdout+completed.stderr) if completed.returncode else ''}


def _live_functions(module, updated, names):
    """Update existing function objects so imported aliases also see the repair."""
    loaded = sys.modules.get('audit_core.'+module[:-3])
    if loaded is None:return []
    import __future__
    definitions = _functions(updated)
    replacements = []
    for name in names:
        old = getattr(loaded, name)
        node = deepcopy(definitions[name])
        node.args.defaults = []; node.args.kw_defaults = [None]*len(node.args.kwonlyargs)
        node.decorator_list = []
        tree = ast.fix_missing_locations(ast.Module(body=[node], type_ignores=[]))
        namespace = dict(loaded.__dict__)
        exec(compile(tree, loaded.__file__, 'exec', flags=__future__.annotations.compiler_flag), namespace)
        new = namespace[name]
        if old.__code__.co_freevars != new.__code__.co_freevars:
            raise AuditError('函数闭包变化无法原位应用，请保留原实现。')
        replacements.append((old, old.__code__, old.__doc__, new.__code__, new.__doc__))
    return replacements


def _run(case, code):
    request = json.loads(code)
    if request.get('action') not in {'read','patch'}:raise AuditError('源码工具仅支持读取与确定性函数修复。')
    if not request.get('module') and request['action']=='read':
        return {'tool':'project_source','editable_modules':sorted(EDITABLE),'server_writable':False}
    path = _path(request['module']); original = path.read_bytes()
    source = original.decode('utf-8'); digest = hashlib.sha256(original).hexdigest()
    if request['action']=='read':
        functions = _functions(source); selected = request.get('function')
        if selected and selected not in functions:raise AuditError('指定函数不存在。')
        return {'tool':'project_source','module':path.name,'sha256':digest,
            'functions':[{'name':n.name,'line':n.lineno,'editable':path.name in EDITABLE and n.name not in PROTECTED_FUNCTIONS and not n.decorator_list} for n in functions.values()],
            **({'source':ast.get_source_segment(source,functions[selected])} if selected else {})}
    if path.name not in EDITABLE:raise AuditError('权限、来源边界、规则及执行器模块不能由执行模型改写。')
    changes=request.get('changes'); probes=request.get('probes',[])
    if not isinstance(changes,list) or not changes or not request.get('reason'):raise AuditError('源码修改需要通用修复说明与既有函数修改。')
    if request.get('expected_sha256') != digest:raise AuditError('源码已变化，请重新读取；不覆盖其他任务或人工修改。')
    updated,names = _changed_source(source,changes)
    if updated == source:return {'tool':'project_source','status':'unchanged','source_code_modified':False}
    if not isinstance(probes,list) or any(not isinstance(p,dict) or p.get('function') not in names or 'expected' not in p for p in probes):raise AuditError('兼容性探针仅调用本次修改函数并明确预期结果。')
    from .model_metrics import current_analysis_root
    from .execution_trace import record_step
    root = current_analysis_root()
    if root is None:raise AuditError('源码修复必须绑定当前核销运行。')
    transaction=uuid.uuid4().hex; folder=PROJECT_ROOT/'.runtime/source-repairs'/transaction
    with _lock():
        if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise AuditError('源码版本冲突，未写入修改。')
        folder.mkdir(parents=True); (folder/'before.py').write_bytes(path.read_bytes())
        (folder/'candidate.py').write_bytes(updated.encode('utf-8'))
        receipt={'id':transaction,'module':path.name,'functions':sorted(names),'reason':request['reason'],
            'analysis_root':str(root),'before_sha256':digest,'after_sha256':hashlib.sha256(updated.encode()).hexdigest(),
            'source_code_modified':False,'at':datetime.now(timezone.utc).isoformat(),'model_calls':0}
        receipt['status']='validating'
        (folder/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        record_step('audit.local_source_repair.started','验证本地已有确定性源码的兼容性修改',receipt,status='started')
        (folder/'baseline').mkdir(); baseline=_test(folder/'baseline',path,None,[])
        if not baseline['passed']:
            receipt.update(status='baseline_failed',validation=baseline)
        else:
            (folder/'candidate').mkdir(); validation=_test(folder/'candidate',path,updated,probes)
            receipt.update(status='validation_failed',validation=validation)
            if validation['passed']:
                swaps=_live_functions(path.name,updated,names)
                if hashlib.sha256(path.read_bytes()).hexdigest()!=digest:raise AuditError('验证期间源码变化，未覆盖。')
                previous=path.read_bytes()
                try:
                    _write(path,updated.encode('utf-8'))
                    for fn,old_code,old_doc,new_code,new_doc in swaps:fn.__code__=new_code;fn.__doc__=new_doc
                    receipt.update(status='applied',source_code_modified=True,live_functions_updated=len(swaps))
                    (folder/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                    record_step('audit.local_source_repair','本地执行模型修复既有确定性源码',receipt,status='completed')
                except Exception:
                    for fn,old_code,old_doc,_,_ in swaps:fn.__code__=old_code;fn.__doc__=old_doc
                    if hashlib.sha256(path.read_bytes()).hexdigest()==receipt['after_sha256']:_write(path,previous)
                    receipt.update(status='rolled_back',source_code_modified=False)
                    (folder/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
                    raise
                return {'tool':'project_source',**receipt,'receipt':str(folder/'receipt.json')}
        (folder/'receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        record_step('audit.local_source_repair','本地执行模型修复既有确定性源码',receipt,
                    status='completed' if receipt['source_code_modified'] else 'needs_model')
        return {'tool':'project_source',**receipt,'receipt':str(folder/'receipt.json')}


def run(case, code):
    if not authorized():raise AuditError('项目源码自修复仅授权本机8080核销进程，正式服务器无源码写权限。')
    try:
        return _run(case,code)
    except (AuditError,ValueError,KeyError,TypeError,SyntaxError,OSError,subprocess.SubprocessError) as error:
        from .execution_diagnostics import _text
        from .execution_trace import record_step
        result={'tool':'project_source','status':'model_takeover','source_code_modified':False,
                'error_type':type(error).__name__,'error':_text(error)}
        record_step('audit.local_source_repair.rejected','本地源码修改未应用，交模型调整',result,status='needs_model')
        return result
