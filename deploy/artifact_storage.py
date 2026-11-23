"""参半产物 OSS 迁移 CLI：只处理明确计划，不调用 AI、勤策或业务回调。"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import threading
import urllib.request


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "upload", "verify", "prune"))
    parser.add_argument("--project", default="/data/offline-activity-audit")
    parser.add_argument("--plan", required=True)
    parser.add_argument("--receipt")
    parser.add_argument("--verification-receipt")
    args = parser.parse_args(argv)
    project = Path(args.project).resolve()
    if os.name != "posix" or project != Path("/data/offline-activity-audit") or socket.gethostname() != "dingding-K3":
        raise RuntimeError("本工具仅允许指定的参半原生服务器，不操作本机或宝塔")
    sys.path.insert(0, str(project/"deploy"))
    from configuration import load_environment
    environment = load_environment()
    sys.path.insert(0, str(project/"skills/orchestrate-offline-audit/scripts"))
    # 在发布暂存区验证候选时，只覆盖此次新增模块和精确候选；其他依赖仍取正式源码。
    import audit_core
    staged = Path(__file__).resolve().parents[1]/"skills/orchestrate-offline-audit/scripts/audit_core"
    if staged != project/"skills/orchestrate-offline-audit/scripts/audit_core" and staged.is_dir():
        audit_core.__path__.insert(0, str(staged))
    from audit_core.artifact_oss import ArtifactOSS, PREFIX, canonical
    from audit_core.artifact_repository import ArtifactRepository, content_inventory, tree_inventory
    from audit_core.workbench_server import WorkbenchCatalog, WorkbenchHTTPServer, Handler
    credentials = Path(environment["OFFLINE_AUDIT_PRODUCT_OSS_CREDENTIALS_FILE"])
    if os.geteuid() == 0:
        # 历史里有 root 创建的只读技术附件；不批量改其权限或复制密钥。
        # 仅凭据读取期间切换为该文件所属服务用户，随后恢复维护身份。
        owner = credentials.stat().st_uid
        os.seteuid(owner)
        try:
            storage = ArtifactOSS(credentials)
        finally:
            os.seteuid(0)
    else:
        storage = ArtifactOSS(credentials)
    repository = ArtifactRepository(storage, project/"worktrees")
    report = {"action": args.action, "hostname": socket.gethostname(), "prefix": PREFIX,
              "model_requests": 0, "callbacks": 0, "passed": False}
    plan_path = Path(args.plan).resolve()
    if plan_path.is_relative_to(project):
        raise RuntimeError("迁移计划和回执须置于项目外部的专用任务目录")
    try:
        if args.action == "plan":
            catalog = WorkbenchCatalog(repository.root)
            summaries = catalog.list_runs()
            if any(row.get("status") not in {"completed", "failed"} for row in summaries):
                raise RuntimeError("当前存在未完成核销，不生成清理计划")
            rows = []
            for summary in summaries:
                if summary.get("storage_type") != "worktree":
                    raise RuntimeError("检测到未适配的历史类型，未忽略任何档案")
                name = summary["workspace_id"]
                files = tree_inventory(repository._workspace(name))
                rows.append({"workspace_id": name, "summary": summary,
                             "inventory_sha256": hashlib.sha256(canonical(content_inventory(files))).hexdigest(),
                             "files": files})
            plan = {"schema_version": "1.0", "hostname": socket.gethostname(),
                    "project": str(project), "prefix": PREFIX, "workspaces": rows,
                    "sources": repository.source_inventory()}
            plan_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with plan_path.open("xb") as output:
                output.write(canonical(plan))
            plan_path.chmod(0o600)
            report.update(workspaces=len(rows), source_files=len(plan["sources"]),
                          source_bytes=sum(row["size"] for row in plan["sources"]),
                          archive_bytes=sum(row["size"] for run in rows for row in run["files"]), passed=True)
        else:
            plan_bytes = plan_path.read_bytes()
            plan_sha = hashlib.sha256(plan_bytes).hexdigest()
            plan = json.loads(plan_bytes)
            if (plan.get("schema_version") != "1.0" or plan.get("hostname") != socket.gethostname()
                    or plan.get("project") != str(project) or plan.get("prefix") != PREFIX):
                raise RuntimeError("计划不属于当前参半部署")
            report["plan_sha256"] = plan_sha
            if args.action == "upload":
                repository.upload_sources(plan["sources"])
                for number, row in enumerate(plan["workspaces"], 1):
                    name = row["workspace_id"]
                    current = content_inventory(tree_inventory(repository._workspace(name)))
                    if hashlib.sha256(canonical(current)).hexdigest() != row["inventory_sha256"]:
                        raise RuntimeError("计划生成后档案已变化，停止上传此单")
                    repository.archive(name, row["summary"], plan["sources"])
                    print(json.dumps({"uploaded": number, "total": len(plan["workspaces"]),
                                      "workspace_id": name}, ensure_ascii=False), flush=True)
                report.update(passed=True, workspaces=len(plan["workspaces"]), source_files=len(plan["sources"]))
            elif args.action == "verify":
                from audit_core.artifact_runtime import catalog_type, handler_type
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                # 空目录验证：不存在任何本地运行档案或原始资料，强制全部从 OSS 读取。
                with tempfile.TemporaryDirectory(prefix="offline-artifact-http-check-") as temporary:
                    isolated_repo = ArtifactRepository(storage, Path(temporary)/"worktrees")
                    catalog = catalog_type(WorkbenchCatalog, isolated_repo)(isolated_repo.root)
                    server = WorkbenchHTTPServer(("127.0.0.1", 0), handler_type(Handler, isolated_repo), catalog=catalog)
                    worker = threading.Thread(target=server.serve_forever, daemon=True)
                    worker.start()
                    try:
                        base = "http://127.0.0.1:"+str(server.server_address[1])
                        with opener.open(base+"/api/runs", timeout=120) as response:
                            listing = json.load(response)
                        expected = {row["workspace_id"] for row in plan["workspaces"]}
                        if not expected <= {row["workspace_id"] for row in listing["runs"]}:
                            raise RuntimeError("纯 OSS 目录未完整显示迁移档案")
                        from urllib.parse import quote
                        verified = []
                        for number, row in enumerate(plan["workspaces"], 1):
                            name = row["workspace_id"]
                            value = repository.verify(name)
                            encoded = quote(name, safe="")
                            with opener.open(base+"/api/runs/"+encoded+"/snapshot", timeout=180) as response:
                                snapshot = json.load(response)
                            if snapshot["run"]["workspace_id"] != name:
                                raise RuntimeError("纯 OSS 快照身份不符")
                            with opener.open(base+"/?run="+encoded, timeout=180) as response:
                                if response.status != 200 or b"<!doctype html" not in response.read().lower():
                                    raise RuntimeError("纯 OSS 核销页面不可读取")
                            with isolated_repo.request_scope():
                                workspace = isolated_repo.checkout(name)
                                cases = workspace/"analysis/input-cases.json"
                                file_id = None
                                if cases.is_file():
                                    raw = json.loads(cases.read_text("utf-8"))
                                    packets = (raw if isinstance(raw, list) else raw["archives"]
                                               if isinstance(raw, dict) and isinstance(raw.get("archives"), list)
                                               else list(raw.values()) if isinstance(raw, dict) else [])
                                    file_id = next((item.get("file_id") for packet in packets if isinstance(packet, dict)
                                        for item in packet.get("inventory", []) if isinstance(item, dict)
                                        and item.get("file_id") and item.get("sha256")), None)
                            if file_id is not None:
                                url = base+"/api/runs/"+encoded+"/materials/preview?download=1&file_id="+quote(file_id, safe="")
                                with opener.open(url, timeout=180) as response:
                                    if response.status != 200:
                                        raise RuntimeError("纯 OSS 原件下载不可用")
                                    while response.read(1024*1024):
                                        pass
                            verified.append(name)
                            print(json.dumps({"verified": number, "total": len(expected),
                                "workspace_id": name, "oss_only": True}, ensure_ascii=False), flush=True)
                        report.update(passed=True, oss_only_http=True, verified_workspaces=verified,
                                      local_business_files_in_verifier=0)
                    finally:
                        server.shutdown()
                        worker.join(timeout=10)
                        server.server_close()
            elif args.action == "prune":
                if not args.verification_receipt:
                    raise RuntimeError("清理必须提供本计划的纯 OSS 页面验证回执")
                verified = json.loads(Path(args.verification_receipt).read_text("utf-8"))
                if (not verified.get("passed") or not verified.get("oss_only_http")
                        or verified.get("plan_sha256") != plan_sha
                        or set(verified.get("verified_workspaces", [])) != {row["workspace_id"] for row in plan["workspaces"]}):
                    raise RuntimeError("未取得与本计划一致的完整验证回执，不删除")
                import subprocess
                state = subprocess.run(["systemctl", "is-active", "offline-activity-audit.service"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
                if state.stdout.strip() != "inactive":
                    raise RuntimeError("初次批量清理前必须通过空闲门禁并停止服务，避免删到正在读取的文件")
                cleaned = []
                for row in plan["workspaces"]:
                    if repository._workspace(row["workspace_id"]).exists():
                        cleaned.append(repository.prune_workspace(row["workspace_id"]))
                removed_sources = repository.prune_sources(plan["sources"])
                report.update(passed=True, cleaned=cleaned, removed_sources=removed_sources)
    finally:
        storage.close()
    if args.receipt:
        target = Path(args.receipt).resolve()
        if target.is_relative_to(project):
            raise RuntimeError("迁移回执不得放入源码或业务目录")
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with target.open("xb") as output:
            output.write(canonical(report))
        target.chmod(0o600)
    print(json.dumps(report, ensure_ascii=False), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        frames = __import__('traceback').extract_tb(error.__traceback__)
        where = [{"file": Path(frame.filename).name, "line": frame.lineno} for frame in frames[-4:]]
        detail = str(error) if type(error).__name__ == 'ArtifactStorageError' else None
        print(json.dumps({"passed": False, "error_type": type(error).__name__,
                          "message": "产物迁移未完成；未修改核销结论，未触发AI或回调，请检查具体阶段。",
                          "storage_error": detail, "where": where}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
