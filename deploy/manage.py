"""Native Linux configuration and systemd unit generation; no container runtime."""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
from pathlib import Path
import re
import ssl
import sys

from configuration import ROOT
from deployment_credentials import bundled_product_oss_bytes
from delivery_configuration import APPLICATION_MEMBER,DRAFT_MEMBER,read_delivery_draft


def private_write(path, value):
    path.parent.mkdir(parents=True,exist_ok=True)
    if path.is_symlink():raise RuntimeError('Configuration path must not be a link.')
    descriptor=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(descriptor,'w',encoding='utf-8',newline='\n') as stream:
        stream.write(value);stream.flush();os.fsync(stream.fileno())
    if os.name!='nt':path.chmod(0o600)


def configure(args):
    target=ROOT/'config'
    draft=read_delivery_draft(ROOT)
    address=ipaddress.ip_address(args.bind)
    if not (address.is_loopback or address.is_private) or address.is_unspecified:
        raise RuntimeError('Use a specific loopback/private IP. The intake endpoint is an internal service.')
    if not 1<=args.port<=65535 or not 1<=args.rds_port<=65535:raise RuntimeError('Invalid port.')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,253}',args.rds_host):raise RuntimeError('Invalid RDS endpoint.')
    if not re.fullmatch(r'[A-Za-z0-9_]{1,64}',args.rds_database) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}',args.rds_user):raise RuntimeError('Invalid dedicated database/account name.')
    ca=Path(args.rds_ca).read_text(encoding='utf-8')
    ssl.create_default_context(cadata=ca)
    product_oss=bundled_product_oss_bytes(ROOT)
    target.mkdir(mode=0o700,exist_ok=draft is not None)
    if os.name!='nt':target.chmod(0o700)
    (target/'secrets').mkdir(mode=0o700)
    for name in ('rds_password','ppio_api_key','dashscope_api_key'):
        private_write(target/'secrets'/(name+'.txt'),'\n')
    private_write(target/'secrets/product-oss.json',product_oss.decode('utf-8') if product_oss else '{}\n')
    private_write(target/'rds-ca.pem',ca)
    values={'OFFLINE_AUDIT_BIND_HOST':str(address),'OFFLINE_AUDIT_PORT':str(args.port),
        'OFFLINE_AUDIT_WORKBENCH_URL':f'http://{address}:{args.port}/',
        'OFFLINE_AUDIT_PRODUCT_DB_HOST':args.rds_host,'OFFLINE_AUDIT_PRODUCT_DB_PORT':str(args.rds_port),
        'OFFLINE_AUDIT_PRODUCT_DB_NAME':args.rds_database,'OFFLINE_AUDIT_PRODUCT_DB_USER':args.rds_user}
    lines=[]
    baseline=draft.decode('utf-8') if draft else (ROOT/'deploy/application.env.example').read_text(encoding='utf-8')
    for line in baseline.splitlines():
        key=line.partition('=')[0]
        lines.append(key+'='+values[key] if key in values else line)
    if draft is None:
        private_write(target/'application.env','\n'.join(lines)+'\n')
    else:
        if (ROOT/APPLICATION_MEMBER).read_bytes()!=draft:
            raise RuntimeError('Delivered configuration changed during initialization; existing values were preserved.')
        pending=target/'.application.env.ready'
        private_write(pending,'\n'.join(lines)+'\n')
        pending.replace(ROOT/APPLICATION_MEMBER)
        (ROOT/DRAFT_MEMBER).unlink()
    for name in ('input','worktrees'):
        path=ROOT/name
        if path.is_symlink():raise RuntimeError('Runtime directory must not be a link.')
        path.mkdir(exist_ok=True)
        if os.name!='nt':path.chmod(0o700)
    (ROOT/'worktrees/.service-home').mkdir(exist_ok=True,mode=0o700)
    print('Initialized config/application.env and 0600 private files. Fill RDS/model secrets before starting the service.')
    if product_oss:
        print('Imported the bundled product OSS credentials; no key needs to be entered again.')


def secret_configuration(_args):
    import json
    private=ROOT/'config/secrets'
    if not private.is_dir():raise RuntimeError('Run configure first.')
    updates={}
    for filename,prompt in [('rds_password.txt','RDS read-only account password'),
                            ('ppio_api_key.txt','PPIO API Key'),
                            ('dashscope_api_key.txt','Bailian API Key (optional)')]:
        value=getpass.getpass(prompt+' (blank keeps existing): ')
        if value:updates[filename]=value+'\n'
    ak=getpass.getpass('Read-only product OSS AccessKey ID (blank keeps existing): ').strip()
    if ak:
        secret=getpass.getpass('Read-only product OSS AccessKey Secret: ').strip()
        if any(not re.fullmatch(r'[A-Za-z0-9]{16,128}',v) for v in (ak,secret)):raise RuntimeError('Invalid OSS credential format.')
        updates['product-oss.json']=json.dumps({'accessKeyId':ak,'accessKeySecret':secret})+'\n'
    for name,value in updates.items():
        path=private/name
        if path.is_symlink() or not path.is_file():raise RuntimeError('Unsafe/missing private file.')
        with path.open('w',encoding='utf-8',newline='\n') as stream:stream.write(value);stream.flush();os.fsync(stream.fileno())
        if os.name!='nt':path.chmod(0o600)
    print('Private files updated. No credentials were displayed.')


def systemd(args):
    if not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',args.user):raise RuntimeError('Invalid service user.')
    if any(c in str(ROOT) for c in '\n\r"%'):raise RuntimeError('Unsupported service directory.')
    template=(ROOT/'deploy/offline-activity-audit.service.example').read_text(encoding='utf-8')
    value=template.replace('@PROJECT_ROOT@',str(ROOT)).replace('@PYTHON@',sys.executable).replace('@SERVICE_USER@',args.user)
    target=ROOT/'deploy/generated/offline-activity-audit.service'
    private_write(target,value)
    print('Generated '+str(target)+'. Install it under /etc/systemd/system after doctor passes.')


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    setup=sub.add_parser('configure');setup.add_argument('--bind',default='127.0.0.1');setup.add_argument('--port',type=int,default=8080)
    setup.add_argument('--rds-host',required=True);setup.add_argument('--rds-port',type=int,default=3306)
    setup.add_argument('--rds-database',default='product_catalog');setup.add_argument('--rds-user',default='product_viewer');setup.add_argument('--rds-ca',required=True)
    sub.add_parser('secrets')
    unit=sub.add_parser('systemd');unit.add_argument('--user',default='offline-audit')
    args=parser.parse_args()
    try:
        if args.command=='configure':configure(args)
        elif args.command=='secrets':secret_configuration(args)
        else:systemd(args)
        return 0
    except Exception as exc:
        print(str(exc) if isinstance(exc,RuntimeError) else 'Configuration failed: '+type(exc).__name__,file=sys.stderr);return 1


if __name__=='__main__':raise SystemExit(main())
