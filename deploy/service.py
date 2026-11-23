"""Native Linux service entry: private config -> the sole bundled HTTP service."""
from pathlib import Path
import os
import runpy
import sys

sys.dont_write_bytecode=True
from configuration import ROOT,load_environment

if __name__=='__main__':
    if (ROOT/'config/.delivery-draft.json').exists():
        raise RuntimeError('运行配置已生成；请先按 README 6.1 执行 configure 和 secrets，填写客户 RDS 信息。')
    load_environment()
    os.chdir(ROOT)
    sys.path.insert(0,str(ROOT/'skills/orchestrate-offline-audit/scripts'))
    if os.environ.get('OFFLINE_AUDIT_PRODUCT_DB_HOST','').startswith('replace-with-'):
        raise RuntimeError('请先填写客户 RDS 地址；当前配置仍为待初始化的交付配置。')
    if os.environ.get('OFFLINE_AUDIT_OSS_ENABLED')=='1':
        from audit_core.private_credentials import read_private_file
        from audit_core.model_settings import ModelSettingsStore
        from deployment_credentials import checked_product_oss_bytes
        if not read_private_file(os.environ['OFFLINE_AUDIT_PRODUCT_DB_PASSWORD_FILE']):
            raise RuntimeError('请执行 deploy/manage.py secrets，填写 RDS 只读账号密码。')
        ModelSettingsStore(ROOT/'worktrees').snapshot()
        checked_product_oss_bytes(read_private_file(os.environ['OFFLINE_AUDIT_PRODUCT_OSS_CREDENTIALS_FILE']))
    sys.argv=[str(ROOT/'skills/orchestrate-offline-audit/scripts/serve.py'),
        '--host',os.environ.get('OFFLINE_AUDIT_BIND_HOST','127.0.0.1'),
        '--port',os.environ.get('OFFLINE_AUDIT_PORT','8080')]
    runpy.run_path(sys.argv[0],run_name='__main__')
