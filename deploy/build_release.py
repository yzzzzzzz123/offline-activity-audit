"""Build a checked Linux/RDS delivery; include product OSS credentials only by explicit option."""
from __future__ import annotations

import argparse
from datetime import datetime,timezone
import hashlib
import io
import json
import os
from pathlib import Path,PurePosixPath
import stat
import sys
import tarfile

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'deploy'))
from deployment_credentials import PRODUCT_OSS_MEMBER,checked_product_oss_bytes,current_product_oss_bytes
from delivery_configuration import APPLICATION_MEMBER,DRAFT_MEMBER,prepared_configuration
ROOT_FILES={'README.md','AGENTS.md','pyproject.toml','.gitignore','.gitattributes'}
ROOT_DIRS={'skills','shared','tests','deploy','docs'}
EXCLUDED={'__pycache__','.pytest_cache','.ruff_cache','.mypy_cache','.venv','.git','runtime',
          'generated','verification','private','.docx-build'}


def files():
    selected=[]
    for name in sorted(ROOT_FILES):
        path=ROOT/name
        if path.is_file():selected.append(path)
    for directory in sorted(ROOT_DIRS):
        for folder,children,names in os.walk(ROOT/directory,followlinks=False):
            children[:]=[name for name in children if name not in EXCLUDED and not (Path(folder)/name).is_symlink()]
            for name in sorted(names):
                path=Path(folder)/name
                if path.is_symlink():raise RuntimeError('Source links require ownership review: '+str(path.relative_to(ROOT)))
                if name=='.env' or name.endswith(('.pyc','.pyo')) or name.startswith('.credentials'):continue
                if stat.S_ISREG(path.stat().st_mode):selected.append(path)
    return sorted(set(selected),key=lambda path:path.relative_to(ROOT).as_posix())


def build(output, *, include_product_oss_credentials=False, prepare_application_config=False):
    seed=ROOT/'deploy/seed/product-catalog.sql'
    sys.path.insert(0,str(ROOT/'deploy'))
    from rds import checked_backup
    _,database=checked_backup(seed)
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    product_oss=current_product_oss_bytes() if include_product_oss_credentials else None
    configuration=prepared_configuration(ROOT) if prepare_application_config else {}
    label='offline-activity-audit-linux-rds-'+('with-oss-' if product_oss else '')
    filename=label+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'.tar.gz'
    target=output/filename
    paths=files();manifest={}
    with tarfile.open(target,'x:gz',dereference=False) as archive:
        for path in paths:
            relative=path.relative_to(ROOT).as_posix();data=path.read_bytes()
            if data.startswith(b'version https://git-lfs.github.com/spec/v1'):
                raise RuntimeError('Missing LFS content: '+relative)
            manifest[relative]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            entry=tarfile.TarInfo(relative);entry.size=len(data);entry.mode=0o644
            archive.addfile(entry,io.BytesIO(data))
        if product_oss:
            manifest[PRODUCT_OSS_MEMBER]={'bytes':len(product_oss),'sha256':hashlib.sha256(product_oss).hexdigest()}
            entry=tarfile.TarInfo(PRODUCT_OSS_MEMBER);entry.size=len(product_oss);entry.mode=0o600
            archive.addfile(entry,io.BytesIO(product_oss))
        for name,data in configuration.items():
            manifest[name]={'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}
            entry=tarfile.TarInfo(name);entry.size=len(data);entry.mode=0o600
            archive.addfile(entry,io.BytesIO(data))
        payload=json.dumps({'format':1,'database':database,'files':manifest,
            'includes_product_oss_credentials':bool(product_oss),
            'includes_application_configuration':bool(configuration)},ensure_ascii=False,indent=2).encode('utf-8')
        entry=tarfile.TarInfo('DELIVERY-MANIFEST.json');entry.size=len(payload);entry.mode=0o644
        archive.addfile(entry,io.BytesIO(payload))
    digest=hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix(target.suffix+'.sha256').write_text(digest+'  '+target.name+'\n',encoding='utf-8')
    verify(target)
    print(json.dumps({'archive':str(target),'files':len(manifest),'bytes':target.stat().st_size,'database':database,
        'includes_product_oss_credentials':bool(product_oss),'sha256':digest},ensure_ascii=False))


def verify(path):
    with tarfile.open(path,'r:gz') as archive:
        names=archive.getnames()
        if len(names)!=len(set(names)):raise RuntimeError('Duplicate archive entries.')
        for name in names:
            pure=PurePosixPath(name)
            if pure.is_absolute() or '..' in pure.parts or not archive.getmember(name).isfile():raise RuntimeError('Unsafe archive entry.')
        manifest=json.load(archive.extractfile('DELIVERY-MANIFEST.json'))
        if set(names)!=set(manifest['files'])|{'DELIVERY-MANIFEST.json'}:raise RuntimeError('Manifest membership mismatch.')
        has_product_oss=PRODUCT_OSS_MEMBER in names
        if bool(manifest.get('includes_product_oss_credentials',False))!=has_product_oss:
            raise RuntimeError('Product OSS credential classification mismatch.')
        allowed_config={APPLICATION_MEMBER,DRAFT_MEMBER} if manifest.get('includes_application_configuration') else set()
        if {name for name in names if name.startswith('config/')}!=allowed_config:
            raise RuntimeError('Unexpected or incomplete application configuration members.')
        for name,expected in manifest['files'].items():
            value=archive.extractfile(name).read()
            if len(value)!=expected['bytes'] or hashlib.sha256(value).hexdigest()!=expected['sha256']:raise RuntimeError('File checksum mismatch: '+name)
            if name.split('/')[0] in {'input','worktrees','artifacts','.git'} or (name.startswith('config/') and name not in allowed_config) or Path(name).name=='.env':raise RuntimeError('Private/runtime content in source package.')
            if name in allowed_config and archive.getmember(name).mode!=0o600:
                raise RuntimeError('Application configuration must have mode 0600.')
            if 'private' in PurePosixPath(name).parts:
                if name!=PRODUCT_OSS_MEMBER or archive.getmember(name).mode != 0o600:
                    raise RuntimeError('Unapproved or incorrectly protected private delivery file.')
                checked_product_oss_bytes(value)
        if allowed_config:
            marker=json.load(archive.extractfile(DRAFT_MEMBER))
            if marker!={'format':1,'application_sha256':hashlib.sha256(archive.extractfile(APPLICATION_MEMBER).read()).hexdigest()}:
                raise RuntimeError('Prepared configuration marker mismatch.')
    print('Delivery files, membership and hashes verified.')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    group=parser.add_mutually_exclusive_group(required=True);group.add_argument('--output');group.add_argument('--check')
    parser.add_argument('--include-product-oss-credentials',action='store_true',
                        help='Explicitly include the current product OSS key in a private deployment bundle.')
    parser.add_argument('--prepare-application-config',action='store_true',help='Include the actual application.env with known OSS callback settings.')
    args=parser.parse_args()
    if args.check and (args.include_product_oss_credentials or args.prepare_application_config):parser.error('Build options apply only to --output.')
    try:verify(args.check) if args.check else build(args.output,include_product_oss_credentials=args.include_product_oss_credentials,prepare_application_config=args.prepare_application_config)
    except Exception as exc:print('Delivery failed: '+str(exc),file=sys.stderr);raise SystemExit(1)
