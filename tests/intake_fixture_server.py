from __future__ import annotations

import argparse
import tempfile
import threading
from pathlib import Path

from audit_core.oss_intake import (
    OSSIntakeCancelledError,
    OSSIntakeConfig,
    OSSIntakeService,
)
from audit_core.workbench_server import Handler, WorkbenchCatalog, WorkbenchHTTPServer
from audit_core.workbench_store import atomic_write_json, main_flow_task_list


HOST = "audit-materials.oss-cn-hangzhou.aliyuncs.com"
COMPLETED_JOB_ID = "c" * 24
FAILED_JOB_ID = "f" * 24
COMPLETED_WORKSPACE_ID = "20260909_0900_00-codex_high"
FAILED_WORKSPACE_ID = "20260909_0901_00-codex_high"
RUNNING_WORKSPACE_ID = "OSS运行资料-20260910_0902_00"
INPUT_WORKSPACE_ID = "input运行资料-20260910_0903_00"
ORPHAN_JOB_ID = "d" * 24


def payload(verify_code: str, analyze_id: int, name: str) -> dict:
    return {
        "verifyCode": verify_code,
        "analyzeId": analyze_id,
        "downloadUrl": f"https://{HOST}/incoming/{name}.zip?signature=fixture",
    }


def write_run(
    root: Path,
    *,
    workspace_id: str,
    run_id: str,
    status: str,
    created_at: str,
    failure: dict | None = None,
    input_source: str = "oss",
) -> None:
    workspace = root / workspace_id
    workspace.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        workspace / "manifest.json",
        {
            "schema_version": "1.2",
            "workspace_id": workspace_id,
            "run_id": run_id,
            "business_date": "20260909",
            "producer_model": "codex",
            "audit_model": "codex",
            "reasoning_effort": "high",
            "input_source": input_source,
            "source_archives": [f"{workspace_id.split('-')[0]}.zip"],
            "status": status,
            "created_at": created_at,
            "updated_at": created_at,
            "completed_at": created_at if status == "completed" else None,
            "scenarios": ["personnel_incentive"],
            "scenario_count": 1,
            "error_count": 0 if status == "completed" else 1,
            "snapshot_sha256": None,
            "failure": failure,
            "main_flow_tasks": main_flow_task_list(),
        },
    )


def write_terminal_job(
    root: Path,
    input_root: Path,
    *,
    job_id: str,
    workspace_id: str,
    run_id: str,
    verify_code: str,
    analyze_id: int,
    basename: str,
    status: str,
    created_at: str,
    failure: dict | None = None,
) -> None:
    source = input_root / job_id
    source.mkdir(parents=True, exist_ok=True)
    archive = source / basename
    archive.write_bytes(b"PK\x03\x04terminal-browser-fixture")
    atomic_write_json(
        root / ".intake" / "jobs" / f"{job_id}.json",
        {
            "schema_version": "1.3",
            "job_id": job_id,
            "identity_job_id": job_id,
            "attempt": 1,
            "supersedes_job_id": None,
            "rerun": False,
            "status": status,
            "created_at": created_at,
            "updated_at": created_at,
            "finished_at": created_at,
            "verifyCode": verify_code,
            "analyzeId": analyze_id,
            "run_id": run_id,
            "producer_model": "codex",
            "scenario": "personnel_incentive",
            "object": {
                "bucket": HOST.split(".", 1)[0],
                "object_key": f"incoming/{basename}",
                "basename": basename,
                "etag": "fixture",
                "size": archive.stat().st_size,
                "sha256": "0" * 64,
            },
            "delivery": {
                "bytes": archive.stat().st_size,
                "sha256": "0" * 64,
                "etag": "fixture",
                "basename": basename,
            },
            "result": {"workspace_id": workspace_id, "status": status},
            "callback": {
                "required": False,
                "status": "not_required",
                "attempts": 0,
                "http_status": None,
                "delivered_at": None,
            },
            "failure": failure,
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="启动含运行、排队、失败和完成 OSS 任务的隔离浏览器夹具"
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()

    temporary = tempfile.TemporaryDirectory(prefix="offline-audit-intake-browser-")
    root = Path(temporary.name) / "worktrees"
    input_root = Path(temporary.name) / "input-oss"
    root.mkdir(parents=True)

    failed_reason = {
        "code": "fixture_failure",
        "message": "ZIP 内没有可识别的核销材料，请检查压缩包内容后重新投递。",
    }
    write_run(
        root,
        workspace_id=COMPLETED_WORKSPACE_ID,
        run_id="fixture-completed-run",
        status="completed",
        created_at="2026-09-09T01:00:00+00:00",
    )
    write_run(
        root,
        workspace_id=FAILED_WORKSPACE_ID,
        run_id="fixture-failed-run",
        status="failed",
        created_at="2026-09-09T01:01:00+00:00",
        failure=failed_reason,
    )
    write_terminal_job(
        root,
        input_root,
        job_id=COMPLETED_JOB_ID,
        workspace_id=COMPLETED_WORKSPACE_ID,
        run_id="fixture-completed-run",
        verify_code="HX202609090099",
        analyze_id=799,
        basename="已完成投递.zip",
        status="completed",
        created_at="2026-09-09T01:00:00+00:00",
    )
    write_terminal_job(
        root,
        input_root,
        job_id=FAILED_JOB_ID,
        workspace_id=FAILED_WORKSPACE_ID,
        run_id="fixture-failed-run",
        verify_code="HX202609090098",
        analyze_id=798,
        basename="无效材料.zip",
        status="failed",
        created_at="2026-09-09T01:01:00+00:00",
        failure=failed_reason,
    )

    runner_started = threading.Event()
    write_terminal_job(
        root, input_root, job_id=ORPHAN_JOB_ID, workspace_id="",
        run_id="fixture-orphan", verify_code="HX202609090097", analyze_id=797,
        basename="下载失败.zip", status="failed", created_at="2026-09-09T01:02:00+00:00",
        failure=failed_reason,
    )
    write_run(root, workspace_id=INPUT_WORKSPACE_ID, run_id="20260910-input",
              status="running", created_at="2026-09-10T01:03:00+00:00", input_source="input")

    def downloader(submission, destination, _config):  # type: ignore[no-untyped-def]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"PK\x03\x04browser-fixture")
        return {
            "bytes": destination.stat().st_size,
            "sha256": "0" * 64,
            "etag": "fixture",
            "basename": destination.name,
            "input_file": str(destination),
        }

    def runner(**kwargs):  # type: ignore[no-untyped-def]
        write_run(root, workspace_id=RUNNING_WORKSPACE_ID, run_id=kwargs["run_id"],
                  status="running", created_at="2026-09-10T01:02:00+00:00")
        runner_started.set()
        kwargs["cancel_event"].wait()
        raise OSSIntakeCancelledError("投递任务已取消")

    intake = OSSIntakeService(
        worktrees_root=root,
        input_root=input_root,
        config=OSSIntakeConfig(allowed_hosts=(HOST,), callback_required=False),
        downloader=downloader,
        runner=runner,
        url_validator=lambda value, _config: value,
    )
    intake.submit(payload("HX202609090101", 701, "人员激励-运行中"))
    if not runner_started.wait(timeout=5):
        raise RuntimeError("运行中夹具未及时启动")
    second, _ = intake.submit(payload("HX202609090102", 702, "堆头陈列-排队中"))
    deadline = threading.Event()
    for _ in range(100):
        if intake.get(str(second["job_id"])).get("status") == "downloaded":
            break
        deadline.wait(0.02)
    else:
        raise RuntimeError("排队夹具未及时落盘")

    server = WorkbenchHTTPServer(
        (args.host, args.port),
        Handler,
        catalog=WorkbenchCatalog(root),
        intake=intake,
    )
    try:
        server.serve_forever()
    finally:
        server.server_close()
        temporary.cleanup()


if __name__ == "__main__":
    main()
