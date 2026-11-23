"""Apply a frozen, scoped native-service source release with code-only rollback.

Never migrate runtime data or private configuration. Run with the customer's
native Python 3.11. The manifest and archive are frozen on the source machine.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT=Path('/data/offline-activity-audit')
UNIT='offline-activity-audit.service'
CORE='skills/orchestrate-offline-audit/scripts/audit_core/'
HTML='skills/orchestrate-offline-audit/assets/offline-activity-audit.html'
ALLOWED={CORE+name for name in ('model_metrics.py','model_settings.py','model_catalog.py',
    'qwen_model.py','workbench_store.py','workbench_html.py','workbench_server.py')}|{HTML}


def digest(data):return hashlib.sha256(data).hexdigest()


def command(*args,check=True):
    return subprocess.run(args,check=check,stdout=subprocess.PIPE,stderr=subprocess.PIPE,
                          text=True,timeout=25)


def checked_path(root,name):
    pure=PurePosixPath(name)
    if name not in ALLOWED or pure.is_absolute() or '..' in pure.parts:
        raise ValueError('Source path outside the approved feature scope: '+name)
    current=root
    for part in pure.parts:
        current=current/part
        if current.is_symlink():raise ValueError('Source links are not allowed')
    if current.exists():
        metadata=current.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1:
            raise ValueError('Source is not a regular independent file')
    return current


def checked_inputs(manifest,archive,root=ROOT):
    if manifest.get('format')!=1 or manifest.get('target')!=str(ROOT) or manifest.get('service')!=UNIT:
        raise ValueError('Native deployment identity mismatch')
    if digest(archive)!=manifest.get('archive_sha256'):raise ValueError('Source archive checksum mismatch')
    rows=manifest['files']
    if {r['path'] for r in rows}!=ALLOWED or len(rows)!=len(ALLOWED):
        raise ValueError('This updater requires the exact approved eight-file release')
    payloads={}; originals={}
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        expected={'projects/offline/'+r['path'] for r in rows}
        members=[m for m in bundle.getmembers() if m.isfile()]
        if len(members)!=len(expected) or {m.name for m in members}!=expected:
            raise ValueError('Archive membership mismatch')
        if any(not m.isfile() and not m.isdir() for m in bundle.getmembers()):
            raise ValueError('Archive links are not allowed')
        for row in rows:
            name=row['path'];path=checked_path(root,name)
            data=bundle.extractfile('projects/offline/'+name).read()
            if digest(data)!=row['sha256'] or len(data)!=row['bytes']:
                raise ValueError('Payload checksum mismatch: '+name)
            if name.endswith('.py'):compile(data,str(path),'exec')
            old=path.read_bytes() if path.exists() else None
            if (digest(old) if old is not None else None)!=row['before_sha256']:
                raise ValueError('Published source changed since inspection: '+name)
            originals[name]=(old,path.stat() if path.exists() else path.parent.stat())
            payloads[name]=data
    return payloads,originals


def sync_directory(path):
    directory=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(directory)
    finally:os.close(directory)


def atomic(path,data,metadata):
    fd,temporary=tempfile.mkstemp(prefix='.source-update-',dir=path.parent)
    try:
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        os.chown(temporary,metadata.st_uid,metadata.st_gid)
        os.chmod(temporary,stat.S_IMODE(metadata.st_mode) if stat.S_ISREG(metadata.st_mode) else 0o644)
        os.replace(temporary,path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(temporary):os.unlink(temporary)


def rollback(root,originals,names):
    for name in reversed(names):
        old,metadata=originals[name];path=checked_path(root,name)
        if old is None:path.unlink(missing_ok=True)
        else:atomic(path,old,metadata)


def idle(root):
    result=command(str(root/'.venv/bin/python'),'-B',str(root/'deploy/healthcheck.py'),'--idle')
    if not json.loads(result.stdout).get('idle'):raise RuntimeError('Audit tasks are active; release deferred')


@contextlib.contextmanager
def pause_admission(address,port,label):
    # Block only this application's port, including existing client sockets.
    # Root's local verification stays available; rules are always removed.
    rules=[['INPUT','-p','tcp','-d',address,'--dport',str(port),'!','-i','lo',
            '-m','comment','--comment',label,'-j','REJECT','--reject-with','tcp-reset'],
           ['OUTPUT','-p','tcp','-d',address,'--dport',str(port),'-m','owner','!',
            '--uid-owner','0','-m','comment','--comment',label,
            '-j','REJECT','--reject-with','tcp-reset']]
    installed=[]
    try:
        for rule in rules:
            command('iptables','-w','5','-I',rule[0],'1',*rule[1:]);installed.append(rule)
        yield
    finally:
        errors=[]
        for rule in reversed(installed):
            result=command('iptables','-w','5','-D',*rule,check=False)
            if result.returncode:errors.append(rule[0])
        if errors:raise RuntimeError('Temporary admission rule cleanup failed: '+','.join(errors))


def get_json(base,path):
    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(base+path,timeout=8) as response:return json.load(response)


def verify_running(base):
    last=None
    for _ in range(20):
        try:
            config=get_json(base,'/api/config')
            settings=get_json(base,'/api/model-settings')
            runs=get_json(base,'/api/runs')['runs']
            if config.get('api_version')!='1.60':raise RuntimeError('API version readback failed')
            if len(settings.get('model_choices',[]))!=46:raise RuntimeError('Model catalogue readback failed')
            if 'local_upload' in config:raise RuntimeError('Excluded upload API configuration changed')
            if not all('token_usage' in row for row in runs):raise RuntimeError('Token usage readback failed')
            return {'api_version':config['api_version'],'model_choices':len(settings['model_choices']),
                    'records':len(runs),'token_usage_on_all_records':True,'upload_updates_excluded':True}
        except Exception as error:last=type(error).__name__;time.sleep(.5)
    raise RuntimeError('Runtime verification failed: '+str(last))


def apply(manifest,archive):
    import fcntl
    label=manifest['release_id']
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}',label):raise ValueError('Invalid release identifier')
    if ROOT.resolve()!=ROOT or not ROOT.is_dir():raise ValueError('Unexpected application root')
    payloads,originals=checked_inputs(manifest,archive)
    report={'release_id':label,'status':'validated','files':[],'administrator':'not-applicable: trusted-private-network application has no independent account domain'}
    with (ROOT/'.source-update.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if command('systemctl','is-active',UNIT).stdout.strip()!='active':raise RuntimeError('Audit service is not active')
        idle(ROOT)
        config={}
        for line in (ROOT/'config/application.env').read_text().splitlines():
            key,_,value=line.partition('=')
            if key in {'OFFLINE_AUDIT_BIND_HOST','OFFLINE_AUDIT_PORT'}:config[key]=value.strip().strip('\"\'')
        address=config['OFFLINE_AUDIT_BIND_HOST'];port=int(config.get('OFFLINE_AUDIT_PORT','8080'))
        if address!='192.168.2.187' or port!=8080:raise ValueError('Unexpected application listener')
        base='http://'+address+':'+str(port)
        old_settings=get_json(base,'/api/model-settings')
        identity=('provider','active_model','revision','has_api_key')
        model_before={key:old_settings.get(key) for key in identity}
        release=ROOT.parent/(ROOT.name+'-source-releases')/label
        release.mkdir(mode=0o700,parents=True,exist_ok=False);release.parent.chmod(0o700)
        for index,(name,(old,metadata)) in enumerate(originals.items()):
            if old is not None:
                saved=release/(str(index)+'.before');saved.write_bytes(old);saved.chmod(0o600)
        (release/'manifest.json').write_text(json.dumps(manifest,indent=2));(release/'manifest.json').chmod(0o600)
        written=[];stopped=False
        with pause_admission(address,port,label):
            idle(ROOT)
            try:
                stopped=True;command('systemctl','stop',UNIT)
                checked_inputs(manifest,archive)
                for row in manifest['files']:
                    name=row['path'];written.append(name)
                    atomic(checked_path(ROOT,name),payloads[name],originals[name][1])
                    actual=digest((ROOT/name).read_bytes())
                    if actual!=row['sha256']:raise RuntimeError('Source readback failed')
                    report['files'].append({'path':name,'source_sha256':row['sha256'],'actual_sha256':actual})
                command('systemctl','start',UNIT)
                report['runtime']=verify_running(base)
                new_settings=get_json(base,'/api/model-settings')
                if {key:new_settings.get(key) for key in identity}!=model_before:
                    raise RuntimeError('Existing model configuration changed during source release')
                report['runtime']['existing_model_configuration_preserved']=True
                report['status']='applied'
            except BaseException:
                if stopped:command('systemctl','stop',UNIT,check=False)
                rollback(ROOT,originals,written)
                if stopped:command('systemctl','start',UNIT)
                report['status']='rolled-back'
                (release/'report.json').write_text(json.dumps(report,indent=2));(release/'report.json').chmod(0o600)
                raise
        report['admission_restored']=True
        report['backup_directory']=str(release)
        (release/'report.json').write_text(json.dumps(report,indent=2));(release/'report.json').chmod(0o600)
        return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True);parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--apply',action='store_true');args=parser.parse_args()
    manifest=json.loads(args.manifest.read_text());archive=args.archive.read_bytes()
    if args.apply:print(json.dumps(apply(manifest,archive)))
    else:
        checked_inputs(manifest,archive)
        print(json.dumps({'status':'validated','files':len(manifest['files'])}))
