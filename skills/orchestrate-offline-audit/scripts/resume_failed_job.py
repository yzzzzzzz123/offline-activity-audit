"""在原 run 目录继续一单失败的投递任务，并按投递要求发送结果回调。

只复用本单已有且已校验的读取结果，不改写核销结论；模型、ZIP 指纹、勤策快照
任一不一致就停止，不做猜测补齐。
"""
from pathlib import Path
import argparse
import json
import os
import sys
from urllib.parse import urlsplit

sys.dont_write_bytecode = True
SCRIPTS = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPTS.parents[2]
sys.path.insert(0, str(SCRIPTS))
sys.path.insert(0, str(PROJECT_ROOT / 'deploy'))

from audit_core.failed_upload_resume import resume_failed_job
from audit_core.oss_intake import OSSIntakeConfig, post_analysis_callback
from audit_core.workbench_store import DEFAULT_WORKTREES_ROOT


def _flag(name):
    return (os.environ.get(name) or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def callback_configuration():
    """与正式服务同一份配置：部署环境变量决定回调地址与重试次数。"""
    from configuration import load_environment
    load_environment()
    url = (os.environ.get('OFFLINE_AUDIT_OSS_CALLBACK_URL') or '').strip()
    if not url:
        raise SystemExit('未配置 OFFLINE_AUDIT_OSS_CALLBACK_URL，不能发送结果回调')
    hosts = tuple(value.strip() for value in
                  (os.environ.get('OFFLINE_AUDIT_OSS_ALLOWED_HOSTS') or '').split(',') if value.strip())
    return OSSIntakeConfig(
        allowed_hosts=hosts or (urlsplit(url).hostname or 'callback.invalid',),
        producer_model=os.environ.get('OFFLINE_AUDIT_OSS_PRODUCER_MODEL') or 'codex',
        callback_url=url,
        allow_http_callback=_flag('OFFLINE_AUDIT_OSS_ALLOW_HTTP_CALLBACK'),
    )


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id', required=True)
    parser.add_argument('--worktrees', type=Path, default=DEFAULT_WORKTREES_ROOT)
    parser.add_argument('--no-callback', action='store_true')
    args = parser.parse_args()
    sender = config = None
    if not args.no_callback:
        config = callback_configuration()
        sender = post_analysis_callback
    result = resume_failed_job(args.job_id, args.worktrees,
                              callback_sender=sender, callback_config=config)
    job = json.loads((Path(args.worktrees).resolve() / '.intake/jobs'
                      / ('%s.json' % args.job_id)).read_text(encoding='utf-8'))
    print(json.dumps({'job_id': args.job_id, 'verifyCode': job.get('verifyCode'),
                      'status': job.get('status'), 'callback': job.get('callback'),
                      'worktree': result.get('worktree'),
                      'result_status': result.get('status')}, ensure_ascii=False))
