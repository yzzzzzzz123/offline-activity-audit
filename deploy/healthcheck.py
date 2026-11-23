"""Local readiness/idle checks. Never invoke AI, download a ZIP or send a callback.

`--wait-idle N` is the restart gate. systemd stops the whole control group
(`KillMode=control-group`), so a restart during an audit kills the in-flight
model request and the claim ends as "服务在任务完成前重启" (2026-11-12
HX202608170019 / HX202611060019 / HX202610090047). Publish only after this
reports idle; it reads job and run state and changes nothing itself.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import time
import urllib.request

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'skills/orchestrate-offline-audit/scripts'))
from configuration import load_environment


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--production',action='store_true');parser.add_argument('--idle',action='store_true')
    parser.add_argument('--wait-idle',type=int,metavar='SECONDS',default=None,
        help='block until idle for up to SECONDS, then exit 0 (idle) or 1 (still busy)')
    parser.add_argument('--interval',type=int,default=15,help='poll interval used by --wait-idle')
    args=parser.parse_args();report={'checks':[],'errors':[]}
    try:
        load_environment()
        base=os.environ['OFFLINE_AUDIT_WORKBENCH_URL'].rstrip('/')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        busy=activity(opener,base)
        if args.wait_idle is not None:
            deadline=time.monotonic()+max(args.wait_idle,0)
            while busy and time.monotonic()<deadline:
                print(json.dumps({'idle':False,'busy':busy,'remaining_seconds':round(deadline-time.monotonic(),1)},ensure_ascii=False),flush=True)
                time.sleep(max(args.interval,1))
                busy=activity(opener,base)
            report.update(idle=not busy,busy=busy)
            print(json.dumps(report,ensure_ascii=False))
            return 0 if not busy else 1
        report['idle']=not busy
        report['busy']=busy
        if args.idle:print(json.dumps(report));return 0 if not busy else 1
        from audit_core.product_database import load_product_catalog
        catalog=load_product_catalog();report['database_rows']=catalog['data_source']['row_count']
        report['database_transport']=os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_SSL_MODE','VERIFY_IDENTITY')
        report['checks'].append('Configured database transport, read-only snapshot and product field validation')
        from audit_core.model_settings import ModelSettingsStore
        settings=ModelSettingsStore(ROOT/'worktrees').public()
        if args.production:
            if not settings['profiles'][settings['provider']]['has_api_key']:report['errors'].append('Configure the selected model API key.')
            from audit_core.private_credentials import read_private_file
            value=json.loads(read_private_file(os.environ['OFFLINE_AUDIT_PRODUCT_OSS_CREDENTIALS_FILE']))
            if set(value)!={'accessKeyId','accessKeySecret'} or not all(value.values()):report['errors'].append('Configure read-only product OSS credentials.')
            if os.environ.get('OFFLINE_AUDIT_DEMO','0')!='0':report['errors'].append('Production must not enable demo mode.')
            if os.environ.get('OFFLINE_AUDIT_OSS_NO_CALLBACK','0')!='0' or os.environ.get('OFFLINE_AUDIT_OSS_RECEIVE_ONLY','0')!='0':report['errors'].append('Production must not skip audit/callback delivery.')
            if not os.environ.get('OFFLINE_AUDIT_OSS_CALLBACK_URL'):report['errors'].append('Configure the customer callback endpoint before accepting tasks.')
        report['checks'].append('No AI or external business callback was executed by this check')
    except Exception as exc:report['errors'].append(type(exc).__name__+'; inspect private configuration, file permissions and RDS connectivity.')
    report['passed']=not report['errors'];print(json.dumps(report,ensure_ascii=False));return 0 if report['passed'] else 1


def activity(opener,base):
    """The claims a restart would interrupt right now (read-only)."""
    with opener.open(base+'/api/config',timeout=5) as response:config=json.load(response)
    jobs=[]
    if config.get('oss_intake',{}).get('enabled'):
        with opener.open(base+'/api/intake/jobs?completed=0',timeout=5) as response:jobs=json.load(response)
    if isinstance(jobs,dict):jobs=jobs.get('jobs',[])
    active={'accepted','downloading','downloaded','running','callback'}
    busy=[{'verifyCode':row.get('verifyCode'),'analyzeId':row.get('analyzeId'),
           'status':row.get('status'),'run_id':row.get('run_id')}
          for row in jobs if row.get('status') in active]
    for path in (ROOT/'worktrees').glob('*/manifest.json'):
        manifest=json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('status') not in ('completed','failed'):
            busy.append({'source':'local','run_id':manifest.get('run_id'),'status':manifest.get('status')})
    return busy


if __name__=='__main__':raise SystemExit(main())
