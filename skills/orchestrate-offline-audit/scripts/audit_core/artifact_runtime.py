"""可选的参半 OSS 档案目录；未配置时保留原工作台全部行为。"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import threading

from .artifact_oss import (ArtifactOSS, ArtifactStorageError, PREFIX,
                          canonical, configuration)
from .artifact_repository import ArtifactRepository, TERMINAL, index_key, plain, workspace_token
from .paths import PROJECT_ROOT


class StorageManager:
    def __init__(self, catalog, repository, *, cleanup_enabled=False):
        self.catalog, self.repository = catalog, repository
        self.cleanup_enabled = cleanup_enabled
        self.intake = None
        self.stop_event = threading.Event()
        self.thread = None
        self.state = {"mode": "oss-primary", "prefix": PREFIX,
                      "cleanup_enabled": cleanup_enabled, "last_error": None}

    def start(self, intake):
        self.intake = intake
        if not self.cleanup_enabled:
            return
        self.thread = threading.Thread(target=self.loop, name="artifact-oss-storage", daemon=True)
        self.thread.start()

    def sweep(self):
        repo = self.repository
        # 队列使用原子回执；不把网络传输放在队列锁里，不阻塞其他核销单。
        with repo.gate:
            local = {row["workspace_id"]: row for row in self.catalog.local_runs()}
        sources = repo.source_inventory()
        archived = 0
        for name, summary in local.items():
            if summary.get("status") not in TERMINAL or summary.get("storage_type") != "worktree":
                continue
            path = repo._workspace(name)
            if (path / "run.pid").exists():
                continue
            jobs = repo._jobs(name, summary.get("run_id"))
            value = repo.archive(name, summary, sources)
            if self.cleanup_enabled:
                with repo.workspace_lock(name):
                    repo._jobs(name, summary.get("run_id"))
                    repo.prune_workspace(name)
                    job_ids = {job.get("job_id") for job in jobs}
                    owned = [row for row in value["originals"]
                             if row["relative_path"].split("/")[0] in job_ids]
                    repo.prune_sources(owned)
            archived += 1
        if self.cleanup_enabled:
            for receipt in (repo.root/".intake/jobs").glob("*.json"):
                plain(receipt, root=repo.root/".intake")
                job = json.loads(receipt.read_text("utf-8"))
                if job.get("status") not in TERMINAL or (job.get("result") or {}).get("workspace_id"):
                    continue
                job_id = job.get("job_id")
                import re
                if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{24}", job_id) or receipt.name != job_id+".json":
                    raise ArtifactStorageError("早期失败任务归属不正确，未清理原件")
                owned = [row for row in sources if row["relative_path"].split("/")[0] == job_id
                         and (repo.input_root/row["relative_path"]).is_file()]
                repo.upload_sources(owned)
                repo.prune_sources(owned)
        self.state.update(last_error=None, last_archived_count=archived)
        return archived

    def loop(self):
        while not self.stop_event.is_set():
            try:
                self.sweep()
            except Exception as error:
                # 存储异常不伪装为核销失败，不重跑 AI，不丢弃尚未上传的原件。
                self.state["last_error"] = type(error).__name__
                print("产物OSS归档未完成；本地原件保持，稍后重试（"+type(error).__name__+"）", flush=True)
            self.stop_event.wait(20)

    def close(self):
        self.stop_event.set()
        if self.thread is not None:
            self.thread.join(timeout=15)
        if self.thread is None or not self.thread.is_alive():
            self.repository.storage.close()


def catalog_type(base, repository):
    """显式目录适配，不更换原 runner、模型或渲染器。"""
    class ArtifactCatalog(base):
        def __init__(self, root):
            super().__init__(root)
            self.artifact_repository = repository
            self.storage_manager = None

        def local_runs(self):
            return list(super()._catalog_state_once()[0])

        def _workspace(self, workspace_id):
            repository.lease(workspace_id)
            candidate = super()._workspace(workspace_id)
            if candidate.is_dir():
                return candidate
            return repository.checkout(workspace_id)

        def _catalog_state_once(self):
            with repository.gate:
                runs = {name: dict(repository.display_summary(name, value), artifact_storage="oss")
                        for name, value in repository.entries().items()}
                for name, row in runs.items():
                    row.update(self.review_state(name))
                for row in super()._catalog_state_once()[0]:
                    runs[row["workspace_id"]] = row
                ordered = tuple(sorted(runs.values(), key=lambda row: (
                    str(row.get("analysis_started_at") or row.get("created_at") or ""),
                    str(row.get("created_at") or ""), str(row.get("workspace_id") or "")), reverse=True))
                encoded = self._encode_catalog(ordered)
                return ordered, encoded, hashlib.sha256(encoded).hexdigest()

        def material_input_root(self, workspace_id, fallback):
            # 活动单仍用本地工作副本；已迁移原件仅在本次请求中取回。
            if (self.root / workspace_id).is_dir() and any(Path(fallback).rglob("*.zip")):
                return fallback
            return repository.preview_inputs(workspace_id)

        def delete_run(self, workspace_id, *, input_root):
            if (self.root / workspace_id).exists():
                with repository.workspace_lock(workspace_id), repository.gate:
                    archived = (repository.get(workspace_id)
                                if repository.storage.head(index_key(workspace_id)) is not None else None)
                    result = super().delete_run(workspace_id, input_root=input_root)
                    if archived is not None:
                        repository.storage.put_json(PREFIX+"deleted/"+workspace_token(workspace_id)+".json", archived)
                        repository.storage.delete_index(index_key(workspace_id))
                    return result
            from .workbench_delete import RunDeletionConflict
            with repository.workspace_lock(workspace_id), repository.gate, self._review_lock:
                value = repository.get(workspace_id)
                jobs = repository._jobs(workspace_id, value["summary"].get("run_id"))
                receipts = []
                for job in jobs:
                    job_id = job.get("job_id")
                    import re
                    if not isinstance(job_id, str) or not re.fullmatch(r"[a-f0-9]{24}", job_id):
                        raise RunDeletionConflict("关联任务归属不能确认，未删除")
                    source = plain(Path(input_root)/job_id, root=Path(input_root))
                    if source.exists() and any(source.iterdir()):
                        raise RunDeletionConflict("关联原件仍有本地副本，须先完成存储迁移")
                    receipts.append(plain(self.root/".intake/jobs"/(job_id+".json"), root=self.root/".intake"))
                # 回收站保留完整索引与不可变内容，避免删除共享的内容寻址原件。
                repository.storage.put_json(PREFIX+"deleted/"+workspace_token(workspace_id)+".json", value)
                for receipt in receipts:
                    receipt.unlink()
                review = plain(self._review_path(workspace_id), root=self.review_root)
                review.unlink(missing_ok=True)
                repository.storage.delete_index(index_key(workspace_id))
                self._catalog_document_cache = None
                return {"workspace_id": workspace_id, "deleted": True, "deleted_jobs": len(receipts)}

        def artifact_status(self):
            return dict(self.storage_manager.state) if self.storage_manager else {"mode": "oss-primary", "cleanup_enabled": False}

        def completion_document(self, jobs=(), *, now=None):
            from datetime import datetime, timezone
            from .completion_estimates import estimate
            now = now or datetime.now(timezone.utc)
            profiles = {}
            for name, entry in repository.entries().items():
                value = repository.statistical_profile(name, entry)
                if value:
                    profiles[name] = value
            for row in self.local_runs():
                name = row['workspace_id']
                path = repository._workspace(name)
                if path.is_dir():
                    value = repository._profile_from_workspace(name, path)
                    if value:
                        profiles[name] = value
            profiles = list(profiles.values())
            # 与原统计器相同的样本资格、实际进度与纯算术方法，数据源改为 OSS+活动单。
            history = [p for p in profiles if p['manifest'].get('status') == 'completed'
                       and not p['manifest'].get('demo') and not p['manifest'].get('resume_history')
                       and not p['interrupted'] and p['manifest'].get('analysis_started_at')
                       and p['start'] and p['end'] and now >= p['end'] > p['start'] and p['model'][1]]
            results = [estimate(p, history, now) for p in profiles
                       if p['manifest'].get('status') not in TERMINAL and not p['manifest'].get('demo')]
            by_run = {item['run_id']: item for item in results}
            for job in jobs:
                if job.get('status') not in {'accepted','downloading','downloaded','waiting_offpeak','running','callback'}:
                    continue
                run_id = job.get('run_id')
                if job.get('status') == 'callback':
                    results = [item for item in results if item.get('run_id') != run_id]
                    results.append({'job_id':job.get('job_id'), 'run_id':run_id, 'status':'sending_result',
                        'message':'正在向业务系统发送结果', 'sample_count':0, 'generated_at':now.isoformat()})
                elif run_id in by_run:
                    by_run[run_id]['job_id'] = job.get('job_id')
                else:
                    results.append({'job_id':job.get('job_id'), 'run_id':run_id,
                        'status':'queued' if job.get('status') != 'running' else 'preparing',
                        'message':'开始核销后更新预估', 'sample_count':0, 'generated_at':now.isoformat()})
            return {'generated_at':now.isoformat(), 'history_count':len(history), 'estimates':results}

    return ArtifactCatalog


def handler_type(base, repository):
    class ArtifactHandler(base):
        def _lease_record(self):
            from urllib.parse import unquote, urlsplit, parse_qs
            from .workbench_store import WORKSPACE_ID_PATTERN
            parsed = urlsplit(self.path)
            segments = parsed.path.split("/")
            name = None
            if segments[:3] == ["", "api", "runs"] and len(segments) >= 4:
                name = unquote(segments[3])
            elif parsed.path == "/":
                name = (parse_qs(parsed.query).get("run") or [None])[0]
            elif segments[:4] == ["", "api", "intake", "jobs"] and len(segments) == 5:
                intake = self.server.intake
                if intake is not None:
                    try:
                        name = str((intake.store.get(segments[4]).get("result") or {}).get("workspace_id") or "")
                    except (FileNotFoundError, ValueError):
                        pass
            if isinstance(name, str) and WORKSPACE_ID_PATTERN.fullmatch(name):
                repository.lease(name)

        def do_GET(self):
            with repository.request_scope():
                self._lease_record()
                from urllib.parse import urlsplit
                parsed = urlsplit(self.path)
                if parsed.path == '/api/completion-estimates' and not parsed.query:
                    jobs = self.server.intake.list(active_only=True) if self.server.intake is not None else []
                    self._json(self.server.catalog.completion_document(jobs))
                    return
                return super().do_GET()

        def do_HEAD(self):
            with repository.request_scope():
                self._lease_record()
                return super().do_HEAD()

        def do_POST(self):
            with repository.request_scope():
                self._lease_record()
                return super().do_POST()

        def do_DELETE(self):
            with repository.request_scope():
                self._lease_record()
                return super().do_DELETE()

        def _config(self):
            value = super()._config()
            value["artifact_storage"] = self.server.catalog.artifact_status()
            return value
    return ArtifactHandler


def create_catalog(root, base_catalog, base_handler):
    config = configuration(PROJECT_ROOT)
    if config is None:
        return base_catalog(root), base_handler
    credentials = Path(os.environ["OFFLINE_AUDIT_PRODUCT_OSS_CREDENTIALS_FILE"])
    storage = ArtifactOSS(credentials)
    repository = ArtifactRepository(storage, root)
    catalog = catalog_type(base_catalog, repository)(root)
    catalog.storage_manager = StorageManager(catalog, repository,
                                             cleanup_enabled=config["cleanup_enabled"])
    return catalog, handler_type(base_handler, repository)


def start_storage(catalog, intake):
    manager = getattr(catalog, "storage_manager", None)
    if manager is not None:
        manager.start(intake)


def close_storage(catalog):
    manager = getattr(catalog, "storage_manager", None)
    if manager is not None:
        manager.close()
