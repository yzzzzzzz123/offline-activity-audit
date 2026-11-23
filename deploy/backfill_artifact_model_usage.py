"""参半历史列表分模型用量补齐；仅新增派生摘要，不调用模型或改写封存档案。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="/data/offline-activity-audit")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--receipt")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--verify", action="store_true", help="仅回读已生成摘要，不下载档案、不写 OSS")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    if (os.name != "posix" or project != Path("/data/offline-activity-audit")
            or socket.gethostname() != "dingding-K3"):
        raise RuntimeError("此工具仅适用于已授权的参半原生服务器")
    plan_path = Path(args.plan).resolve()
    receipt_path = Path(args.receipt).resolve() if args.receipt else None
    if plan_path.is_relative_to(project) or receipt_path and receipt_path.is_relative_to(project):
        raise RuntimeError("计划和回执须放在项目外部")
    sys.path.insert(0, str(project / "deploy"))
    from configuration import load_environment
    environment = load_environment()
    sys.path.insert(0, str(project / "skills/orchestrate-offline-audit/scripts"))
    import audit_core
    staged = Path(__file__).resolve().parents[1] / "skills/orchestrate-offline-audit/scripts/audit_core"
    if staged != project / "skills/orchestrate-offline-audit/scripts/audit_core" and staged.is_dir():
        audit_core.__path__.insert(0, str(staged))
    from audit_core.artifact_oss import ArtifactOSS, canonical, PREFIX
    from audit_core.artifact_repository import ArtifactRepository
    credentials = Path(environment["OFFLINE_AUDIT_PRODUCT_OSS_CREDENTIALS_FILE"])
    previous_uid = os.geteuid()
    try:
        if previous_uid == 0:
            os.seteuid(credentials.stat().st_uid)
        storage = ArtifactOSS(credentials)
    finally:
        os.seteuid(previous_uid)
    repository = ArtifactRepository(storage, project / "worktrees")
    report = {"passed": False, "model_requests": 0, "callbacks": 0,
              "original_archives_modified": 0, "original_indexes_modified": 0, "rows": []}
    identity = {"schema_version": "1.0", "project": str(project),
                "hostname": socket.gethostname(), "prefix": PREFIX}
    try:
        if not args.apply and not args.verify:
            entries = repository.entries()
            plan = {**identity, "workspaces": [{"workspace_id": name,
                    "archive_sha256": entry["bundle"]["sha256"],
                    "index_sha256": hashlib.sha256(canonical(entry)).hexdigest()}
                    for name, entry in sorted(entries.items())]}
            plan_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            with plan_path.open("xb") as output:
                output.write(canonical(plan))
            plan_path.chmod(0o600)
            report.update(passed=True, planned=len(plan["workspaces"]))
        else:
            plan = json.loads(plan_path.read_text("utf-8"))
            if {key: plan.get(key) for key in identity} != identity:
                raise RuntimeError("派生摘要计划不属于当前部署")
            names = [row["workspace_id"] for row in plan["workspaces"]]
            if len(set(names)) != len(names):
                raise RuntimeError("派生摘要计划包含重复档案")
            for number, row in enumerate(plan["workspaces"], 1):
                name = row["workspace_id"]
                entry = repository.get(name)
                if (entry["bundle"]["sha256"] != row["archive_sha256"]
                        or hashlib.sha256(canonical(entry)).hexdigest() != row["index_sha256"]):
                    raise RuntimeError("计划生成后原档案索引已变化")
                if args.verify:
                    document = json.loads(storage.read(repository.model_usage_key(name, entry)))
                    repository._validate_model_usage(document, name, entry)
                else:
                    with repository.temporary("usage-backfill") as temporary:
                        workspace = repository.restore(entry, temporary / name)
                        document = repository.save_model_usage(name, workspace, entry)
                if canonical(repository.get(name)) != canonical(entry):
                    raise RuntimeError("原档案索引在处理期间发生变化")
                projected = repository.display_summary(name, entry)
                if projected["token_usage"]["by_model"] != document["usage"]["by_model"]:
                    raise RuntimeError("历史列表派生投影校验失败")
                result = {"workspace_id": name, "archive_sha256": row["archive_sha256"],
                          "models": [item["model"] for item in document["usage"]["by_model"]],
                          "total_tokens": (entry["summary"].get("token_usage") or {}).get("total_tokens"),
                          "original_index_preserved": True}
                report["rows"].append(result)
                print(json.dumps({"completed": number, "total": len(names), **result}, ensure_ascii=False), flush=True)
            report.update(passed=True, completed=len(names))
    finally:
        storage.close()
    if receipt_path:
        receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with receipt_path.open("xb") as output:
            output.write(canonical(report))
        receipt_path.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(json.dumps({"passed": False, "error_type": type(error).__name__,
                          "message": "历史用量补齐未完成；不改核销结论、不调用模型、不发送回调。"}, ensure_ascii=False))
        raise SystemExit(1)
