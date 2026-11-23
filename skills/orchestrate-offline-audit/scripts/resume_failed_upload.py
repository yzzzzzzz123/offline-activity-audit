"""Continue an explicitly selected local failed upload in the same record."""
from pathlib import Path
import argparse
import sys

sys.dont_write_bytecode=True
sys.path.insert(0,str(Path(__file__).resolve().parent))
from audit_core.failed_upload_resume import resume_local_upload
from audit_core.workbench_store import DEFAULT_WORKTREES_ROOT

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id',required=True)
    parser.add_argument('--worktrees',type=Path,default=DEFAULT_WORKTREES_ROOT)
    parser.add_argument('--revalidate-completed-from-call', help='显式指定已完成记录中受影响的原call_id；只核验同来源依赖')
    parser.add_argument('--expected-snapshot-sha256', help='重新核验前已完成snapshot.json的SHA-256')
    parser.add_argument('--finish-unreviewed-summary',action='store_true',help='补完本地原记录尚未完成的最终逻辑核验，保留已完成审核')
    args=parser.parse_args()
    if args.revalidate_completed_from_call or args.finish_unreviewed_summary:
        if args.revalidate_completed_from_call and args.finish_unreviewed_summary:
            parser.error('指定原调用核验与补完最终核验不能同时使用')
        if not args.expected_snapshot_sha256:
            parser.error('重新核验已完成记录必须提供 --expected-snapshot-sha256')
        from audit_core.completed_upload_revalidation import revalidate_completed_upload
        result=revalidate_completed_upload(args.job_id,args.worktrees,
            from_call_id=args.revalidate_completed_from_call,
            expected_snapshot_sha256=args.expected_snapshot_sha256,
            finish_unreviewed=args.finish_unreviewed_summary)
    else:
        if args.expected_snapshot_sha256:
            parser.error('--expected-snapshot-sha256 仅用于显式重新核验已完成记录')
        result=resume_local_upload(args.job_id,args.worktrees)
    print({'status':result['status'],'workspace_id':result['workspace_id']})
