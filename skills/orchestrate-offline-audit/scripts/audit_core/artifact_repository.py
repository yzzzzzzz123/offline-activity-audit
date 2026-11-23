"""核销档案无损归档、按请求恢复和已验证原件的精确清理。"""
from __future__ import annotations

from contextlib import contextmanager
from collections import OrderedDict
import copy
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tarfile
import tempfile
import threading
import time

from .artifact_oss import ArtifactStorageError, PREFIX, canonical
from .workbench_store import WORKSPACE_ID_PATTERN

TERMINAL = {"completed", "failed"}


def digest_file(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def workspace_token(name):
    if not isinstance(name, str) or WORKSPACE_ID_PATTERN.fullmatch(name) is None:
        raise ArtifactStorageError("核销档案名称不安全")
    return hashlib.sha256(name.encode("utf-8")).hexdigest()


def index_key(name):
    return PREFIX + "index/" + workspace_token(name) + ".json"


def blob_key(digest):
    if not isinstance(digest, str) or len(digest) != 64 or any(
            value not in "0123456789abcdef" for value in digest):
        raise ArtifactStorageError("产物指纹格式不正确")
    return PREFIX + "blobs/" + digest[:2] + "/" + digest


def relative_name(name):
    if not isinstance(name, str) or not name or "\\" in name or "\x00" in name:
        raise ArtifactStorageError("档案成员名称不安全")
    parts = name.split("/")
    if PurePosixPath(name).is_absolute() or any(
            part in {"", ".", ".."} or ":" in part for part in parts):
        raise ArtifactStorageError("档案成员越出归属目录")
    return name


def plain(path, *, root):
    path, root = Path(path).absolute(), Path(root).absolute()
    if not path.is_relative_to(root):
        raise ArtifactStorageError("产物路径越出已核实的归属目录")
    for current in [root, *(root / parent for parent in reversed(path.relative_to(root).parents)), path]:
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
            raise ArtifactStorageError("产物路径经过链接，未自动迁移或删除")
        if current.exists() and not (current.is_dir() or current.is_file()):
            raise ArtifactStorageError("产物包含非常规文件")
    return path


def tree_inventory(root):
    root = plain(root, root=root)
    if not root.is_dir() or root.is_mount():
        raise ArtifactStorageError("档案不是普通的独立运行目录")
    result = []
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = plain(Path(directory) / name, root=root)
            if path.name == ".git" or path.is_dir() and path.is_mount():
                raise ArtifactStorageError("档案包含 Git 元数据或嵌套挂载，须单独处理")
            if path.is_file():
                before = path.stat()
                row = {"path": path.relative_to(root).as_posix(), "size": before.st_size,
                       "sha256": digest_file(path), "mtime_ns": before.st_mtime_ns,
                       "mode": stat.S_IMODE(before.st_mode)}
                after = path.stat()
                if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                    raise ArtifactStorageError("归档期间文件仍在变化，原文件保持")
                result.append(row)
    return sorted(result, key=lambda row: row["path"])


def content_inventory(rows):
    return [{key: row[key] for key in ("path", "size", "sha256")} for row in rows]


class ArtifactRepository:
    def __init__(self, storage, worktrees, *, temporary_root=None):
        self.storage, self.root = storage, Path(worktrees).resolve()
        self.project, self.input_root = self.root.parent, self.root.parent / "input"
        self.temporary_root = str(temporary_root) if temporary_root else None
        self.gate = threading.RLock()
        self._lock_guard, self._workspace_locks = threading.Lock(), {}
        self._local = threading.local()
        self._index_cache, self._source_cache = {}, {}
        self._statistics_cache = {}
        self._model_usage_cache = OrderedDict()

    @staticmethod
    def model_usage_key(name, entry):
        digest = entry["bundle"]["sha256"]
        blob_key(digest)
        return PREFIX + "model-usage/" + workspace_token(name) + "/" + digest + ".json"

    @staticmethod
    def _validate_model_usage(document, name, entry):
        from .model_metrics import _safe_attempt
        try:
            if (set(document) != {"schema_version", "workspace_id", "archive_sha256", "usage"}
                    or document["schema_version"] != "1.0"
                    or document["workspace_id"] != name
                    or document["archive_sha256"] != entry["bundle"]["sha256"]):
                raise ValueError()
            usage = document["usage"]
            counters = {"input_tokens", "cached_input_tokens", "output_tokens", "reasoning_tokens",
                        "total_tokens", "recorded_calls", "unrecorded_calls"}
            if not isinstance(usage, dict) or set(usage) != counters | {"status", "by_model"}:
                raise ValueError()
            rows = usage["by_model"]
            if not isinstance(rows, list):
                raise ValueError()
            models = set()
            for row in [usage, *rows]:
                if (not isinstance(row, dict)
                        or set(row) != counters | ({"status", "by_model"} if row is usage else {"status", "model"})
                        or row["status"] not in {"recorded", "partial", "unrecorded"}
                        or any(row[key] is not None and (type(row[key]) is not int or row[key] < 0)
                               for key in counters)):
                    raise ValueError()
                if row is not usage:
                    model = row["model"]
                    if (model in models or model != "unknown" and _safe_attempt({"model": model}).get("model") != model):
                        raise ValueError()
                    models.add(model)
            # 补的是分项投影；原索引已有的总量绝不被新配置或重新分组覆盖。
            original = entry["summary"].get("token_usage") or {}
            for field in counters:
                if original.get(field) is not None and original[field] != usage[field]:
                    raise ValueError()
        except (KeyError, TypeError, ValueError):
            raise ArtifactStorageError("分模型用量与原档案身份或原总量不一致") from None
        return usage

    def save_model_usage(self, name, workspace, entry):
        """从完整校验的原档案提取派生展示数据，不更改旧索引、档案或回调。"""
        from .model_metrics import summarize_model_usage
        self._validate(entry, index_key(name))
        if content_inventory(tree_inventory(workspace)) != content_inventory(entry["files"]):
            raise ArtifactStorageError("分模型用量来源与封存档案不一致")
        document = {"schema_version": "1.0", "workspace_id": name,
                    "archive_sha256": entry["bundle"]["sha256"],
                    "usage": summarize_model_usage(Path(workspace) / "analysis")}
        self._validate_model_usage(document, name, entry)
        key = self.model_usage_key(name, entry)
        self.storage.put_json(key, document)
        if canonical(json.loads(self.storage.read(key))) != canonical(document):
            raise ArtifactStorageError("分模型用量派生摘要回读不一致")
        with self.gate:
            self._model_usage_cache.pop(name, None)
        return document

    def display_summary(self, name, entry):
        """旧 OSS 列表只读补充分模型用量；GET 不下载整包、不写 OSS。"""
        summary = copy.deepcopy(entry["summary"])
        original = summary.get("token_usage") or {}
        if isinstance(original.get("by_model"), list):
            return summary
        digest = entry["bundle"]["sha256"]
        with self.gate:
            cached = self._model_usage_cache.get(name)
            if cached and cached[0] == digest and (cached[1] is not None or time.monotonic() < cached[2]):
                usage = cached[1]
                self._model_usage_cache.move_to_end(name)
            else:
                try:
                    key = self.model_usage_key(name, entry)
                    usage = (self._validate_model_usage(json.loads(self.storage.read(key)), name, entry)
                             if self.storage.head(key) is not None else None)
                except (ArtifactStorageError, ValueError, TypeError):
                    # 单条摘要不可用不拖垮全量台账，也不按主模型猜分配或补零。
                    summary["model_usage_status"] = "unavailable"
                    return summary
                self._model_usage_cache[name] = (digest, usage, time.monotonic() + 30)
                while len(self._model_usage_cache) > 512:
                    self._model_usage_cache.popitem(last=False)
            if usage is not None:
                summary["token_usage"] = {**original, "by_model": copy.deepcopy(usage["by_model"])}
                summary["model_usage_status"] = "recorded"
            else:
                summary["model_usage_status"] = "unrecorded"
        return summary

    @staticmethod
    def _profile_from_workspace(name, workspace):
        from .completion_estimates import _profile, _signature
        signature = _signature(workspace)
        if signature is None:
            return None
        value = _profile(str(workspace), signature)
        if value is not None:
            value = dict(value, workspace_id=name)
        return value

    @staticmethod
    def _encode_profile(value):
        if value is None:
            return None
        value = dict(value)
        for field in ('start', 'end'):
            value[field] = value[field].isoformat() if value[field] else None
        value['points'] = [[key, count, stamp.isoformat()] for key, count, stamp in value['points']]
        return value

    @staticmethod
    def _decode_profile(value):
        if value is None:
            return None
        from .run_timing import parse_time
        value = dict(value)
        for field in ('start', 'end'):
            value[field] = parse_time(value[field])
        value['points'] = [(key, count, parse_time(stamp)) for key, count, stamp in value['points']]
        value['model'], value['scenarios'] = tuple(value['model']), tuple(value['scenarios'])
        return value

    def save_statistics(self, name, workspace, entry):
        """只保存原统计器读取出的轻量事实，不重算核销结论或编造历史事件。"""
        profile = self._profile_from_workspace(name, workspace)
        document = {'schema_version': '1.0', 'workspace_id': name,
                    'archive_sha256': entry['bundle']['sha256'],
                    'profile': self._encode_profile(profile)}
        self.storage.put_json(PREFIX+'statistics/'+workspace_token(name)+'.json', document)
        self._statistics_cache[name] = (entry['bundle']['sha256'], profile)
        return profile

    def statistical_profile(self, name, entry):
        cached = self._statistics_cache.get(name)
        if cached is not None and cached[0] == entry['bundle']['sha256']:
            return cached[1]
        key = PREFIX+'statistics/'+workspace_token(name)+'.json'
        if self.storage.head(key) is not None:
            value = json.loads(self.storage.read(key))
            if (value.get('schema_version') != '1.0' or value.get('workspace_id') != name
                    or value.get('archive_sha256') != entry['bundle']['sha256']):
                raise ArtifactStorageError('历史统计与档案身份不一致')
            profile = self._decode_profile(value['profile'])
        else:
            # 旧档案只读降级：从已核验原件提取一次，留内存，不在 GET 中写 OSS。
            with self.temporary('statistics') as temporary:
                workspace = self.restore(entry, temporary/name)
                profile = self._profile_from_workspace(name, workspace)
        self._statistics_cache[name] = (entry['bundle']['sha256'], profile)
        return profile

    @contextmanager
    def temporary(self, purpose):
        """正常请求立即清理；崩溃残留只在拥有者锁释放后由下一次运行清理。"""
        if self.temporary_root is not None or os.name != "posix":
            with tempfile.TemporaryDirectory(prefix="offline-artifact-"+purpose+"-", dir=self.temporary_root) as value:
                yield Path(value)
            return
        import fcntl
        base = Path(tempfile.gettempdir()) / ("offline-audit-artifacts-"+str(os.getuid()))
        base.mkdir(mode=0o700, exist_ok=True)
        plain(base, root=base)
        if base.stat().st_uid != os.getuid() or stat.S_IMODE(base.stat().st_mode) != 0o700:
            raise ArtifactStorageError("临时空间归属或权限不正确")
        for child in base.iterdir():
            if not child.name.startswith("owned-") or child.is_symlink() or not child.is_dir():
                continue
            lock_path = child/".owner.lock"
            if lock_path.is_symlink() or not lock_path.is_file():
                continue
            try:
                previous = lock_path.open("rb")
            except FileNotFoundError:
                continue
            with previous:
                try:
                    fcntl.flock(previous, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    continue
                if previous.read() != b"offline-canban-artifact-temporary-v1\n":
                    continue
                plain(child, root=base)
                if child.resolve().parent != base.resolve():
                    raise ArtifactStorageError("残留临时目录越界，未删除")
                try:
                    shutil.rmtree(child)
                except FileNotFoundError:
                    pass
        path = Path(tempfile.mkdtemp(prefix="owned-"+purpose+"-", dir=base))
        lock_path = path/".owner.lock"
        with lock_path.open("xb+") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX)
            owner.write(b"offline-canban-artifact-temporary-v1\n")
            owner.flush()
            try:
                yield path
            finally:
                # 本次目录由 mkdtemp 创建；锁仍持有，不涉及其他任务或业务原件。
                shutil.rmtree(path)

    def _workspace(self, name):
        workspace_token(name)
        path = plain(self.root / name, root=self.root)
        if path.parent != self.root:
            raise ArtifactStorageError("核销运行目录越界")
        return path

    def workspace_lock(self, name):
        workspace_token(name)
        with self._lock_guard:
            return self._workspace_locks.setdefault(name, threading.RLock())

    def lease(self, name):
        scope = getattr(self._local, "scope", None)
        if scope is not None and name not in scope["leases"]:
            lock = self.workspace_lock(name)
            lock.acquire()
            scope["leases"][name] = lock

    def entries(self):
        with self.gate:
            result, live = {}, set()
            for row in self.storage.list("index"):
                key = row["key"]
                live.add(key)
                cached = self._index_cache.get(key)
                if cached is None or cached[0] != row["etag"]:
                    document = json.loads(self.storage.read(key))
                    self._validate(document, key)
                    cached = (row["etag"], document)
                    self._index_cache[key] = cached
                result[cached[1]["workspace_id"]] = cached[1]
            self._index_cache = {key: value for key, value in self._index_cache.items() if key in live}
            return result

    def get(self, name):
        key = index_key(name)
        if self.storage.head(key) is None:
            raise FileNotFoundError(name)
        value = json.loads(self.storage.read(key))
        self._validate(value, key)
        return value

    @staticmethod
    def _validate(value, key):
        try:
            if (value["schema_version"] != "1.0" or index_key(value["workspace_id"]) != key
                    or value["summary"]["workspace_id"] != value["workspace_id"]
                    or value["summary"]["status"] not in TERMINAL):
                raise ValueError()
            rows = value["files"]
            names = set()
            for row in rows:
                relative_name(row["path"])
                blob_key(row["sha256"])
                if row["path"] in names or type(row["size"]) is not int or row["size"] < 0:
                    raise ValueError()
                names.add(row["path"])
            if not {"manifest.json", "snapshot.json"} <= names:
                raise ValueError()
            if value["bundle"]["key"] != blob_key(value["bundle"]["sha256"]):
                raise ValueError()
            for original in value["originals"]:
                relative_name(original["relative_path"])
                if original["key"] != blob_key(original["sha256"]):
                    raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise ArtifactStorageError("OSS 核销目录清单未通过完整性与归属检查") from None

    def source_inventory(self):
        rows = []
        if not self.input_root.exists():
            return rows
        for directory, dirs, files in os.walk(self.input_root, followlinks=False):
            for name in dirs:
                plain(Path(directory) / name, root=self.input_root)
            for name in files:
                path = plain(Path(directory) / name, root=self.input_root)
                metadata = path.stat()
                relative = path.relative_to(self.input_root).as_posix()
                signature = (metadata.st_size, metadata.st_mtime_ns, metadata.st_ctime_ns)
                cached = self._source_cache.get(relative)
                if cached is None or cached[0] != signature:
                    digest = digest_file(path)
                    cached = (signature, digest)
                    self._source_cache[relative] = cached
                rows.append({"relative_path": relative, "name": path.name,
                             "size": metadata.st_size, "sha256": cached[1],
                             "key": blob_key(cached[1])})
        return sorted(rows, key=lambda row: row["relative_path"])

    def _upload_file(self, path, key, *, expected=None, content_type="application/octet-stream"):
        digest, size = digest_file(path), Path(path).stat().st_size
        if expected and (digest, size) != (expected["sha256"], expected["size"]):
            raise ArtifactStorageError("源文件与本次迁移清单不一致，未清理")
        with Path(path).open("rb") as stream:
            self.storage.put_immutable(key, stream, size=size, sha=digest, content_type=content_type)
        if digest_file(path) != digest:
            raise ArtifactStorageError("上传期间源文件变化，未发布迁移索引")
        return {"key": key, "sha256": digest, "size": size}

    def upload_sources(self, rows):
        for row in rows:
            path = plain(self.input_root / row["relative_path"], root=self.input_root)
            self._upload_file(path, row["key"], expected=row)
            token = hashlib.sha256(row["relative_path"].encode("utf-8")).hexdigest()
            self.storage.put_json(PREFIX + "inputs/" + token + ".json", row)

    def _jobs(self, name, run_id):
        jobs = []
        for path in (self.root / ".intake" / "jobs").glob("*.json"):
            plain(path, root=self.root / ".intake")
            value = json.loads(path.read_text("utf-8"))
            result = value.get("result") or {}
            exact = result.get("workspace_id") == name
            unresolved = not result.get("workspace_id") and value.get("run_id") == run_id
            if exact or unresolved:
                if value.get("status") not in TERMINAL:
                    raise ArtifactStorageError("关联下载、核销或回调仍在运行，未迁移此单")
                # run_id 可以复用；无明确 workspace_id 的终态回执不能据此归属并删除。
                if exact:
                    jobs.append(value)
        return jobs

    def archive(self, name, summary, sources):
        """上传和回读成功后最后发布索引；本函数不删除任何业务文件。"""
        with self.workspace_lock(name):
            workspace = self._workspace(name)
            manifest = json.loads((workspace / "manifest.json").read_text("utf-8"))
            if manifest.get("workspace_id") != name or manifest.get("status") not in TERMINAL:
                raise ArtifactStorageError("只迁移归属正确的终态档案")
            if (workspace / "run.pid").exists():
                raise ArtifactStorageError("核销进程尚未释放运行目录")
            jobs = self._jobs(name, manifest.get("run_id"))
            inventory = tree_inventory(workspace)
            if self.storage.head(index_key(name)) is not None:
                previous = self.get(name)
                if content_inventory(previous["files"]) != content_inventory(inventory):
                    raise ArtifactStorageError("OSS 已有不同内容的同名历史，禁止覆盖")
                self.save_statistics(name, workspace, previous)
                return previous
            wanted = set(manifest.get("source_archives") or [])
            cases_path = workspace / "analysis/input-cases.json"
            expected = set()
            if cases_path.is_file():
                cases = json.loads(cases_path.read_text("utf-8"))
                packets = (cases if isinstance(cases, list) else cases["archives"]
                           if isinstance(cases, dict) and isinstance(cases.get("archives"), list)
                           else list(cases.values()) if isinstance(cases, dict) else [])
                for packet in packets:
                    if isinstance(packet, dict):
                        wanted.add(Path(str(packet.get("source_archive", "")).replace("\\", "/")).name)
                        if packet.get("archive_sha256"):
                            expected.add(str(packet["archive_sha256"]).lower())
            job_ids = {job.get("job_id") for job in jobs}
            originals = [row for row in sources if row["sha256"] in expected
                         or row["name"] in wanted
                         or row["relative_path"].split("/")[0] in job_ids]
            if expected - {row["sha256"] for row in originals}:
                raise ArtifactStorageError("本单原始资料包指纹缺少对应原件，暂未迁移此单")
            self.upload_sources(originals)
            with self.temporary("upload") as temporary:
                bundle = temporary / "archive.tar.gz"
                with bundle.open("wb") as output:
                    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0, compresslevel=1) as compressed:
                        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
                            for row in inventory:
                                member = tarfile.TarInfo(row["path"])
                                member.size, member.mode = row["size"], row["mode"]
                                member.mtime = row["mtime_ns"] / 1_000_000_000
                                with (workspace / row["path"]).open("rb") as stream:
                                    archive.addfile(member, stream)
                if content_inventory(tree_inventory(workspace)) != content_inventory(inventory):
                    raise ArtifactStorageError("归档期间原件改变，未发布索引")
                descriptor = self._upload_file(bundle, blob_key(digest_file(bundle)),
                                               content_type="application/gzip")
            public_files = {}
            for row in inventory:
                if row["path"] in {"offline-activity-audit.html", "ai-analysis-summary.md"}:
                    key = PREFIX+"reports/"+workspace_token(name)+"/"+row["path"]
                    public_files[row["path"]] = self._upload_file(
                        workspace / row["path"], key, expected=row,
                        content_type="text/html; charset=utf-8" if row["path"].endswith(".html") else "text/markdown; charset=utf-8")
            value = {"schema_version": "1.0", "workspace_id": name,
                     "summary": dict(summary), "files": inventory, "bundle": descriptor,
                     "originals": originals, "reports": public_files, "job_receipts": jobs}
            self._validate(value, index_key(name))
            self.save_statistics(name, workspace, value)
            self.storage.put_json(index_key(name), value)
            if canonical(self.get(name)) != canonical(value):
                raise ArtifactStorageError("迁移索引回读不一致，原文件保持")
            return value

    def restore(self, value, destination):
        """逐成员手动恢复，拒绝链接、重复、越界与未登记文件，不使用 extractall。"""
        self._validate(value, index_key(value["workspace_id"]))
        destination = plain(Path(destination), root=Path(destination))
        destination.mkdir(mode=0o700, parents=True, exist_ok=True)
        rows = {row["path"]: row for row in value["files"]}
        with tempfile.TemporaryFile(dir=self.temporary_root) as output:
            bundle = value["bundle"]
            self.storage.read_to(bundle["key"], output, expected_size=bundle["size"],
                expected_sha=bundle["sha256"], limit=max(bundle["size"], 1))
            output.seek(0)
            seen = set()
            with tarfile.open(fileobj=output, mode="r|gz") as archive:
                for member in archive:
                    relative_name(member.name)
                    if not member.isfile() or member.name in seen or member.name not in rows:
                        raise ArtifactStorageError("OSS 档案成员未登记、重名或不是普通文件")
                    row = rows[member.name]
                    if member.size != row["size"]:
                        raise ArtifactStorageError("OSS 档案成员大小与清单不同")
                    target = destination / member.name
                    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                    plain(target, root=destination)
                    digest, total = hashlib.sha256(), 0
                    with archive.extractfile(member) as source, target.open("xb") as stream:
                        while piece := source.read(1024*1024):
                            total += len(piece)
                            if total > row["size"]:
                                raise ArtifactStorageError("OSS 档案成员流超过原始大小")
                            digest.update(piece)
                            stream.write(piece)
                    if total != row["size"] or digest.hexdigest() != row["sha256"]:
                        raise ArtifactStorageError("OSS 档案成员内容校验失败")
                    target.chmod(0o600)
                    os.utime(target, ns=(row["mtime_ns"], row["mtime_ns"]))
                    seen.add(member.name)
            if seen != set(rows):
                raise ArtifactStorageError("OSS 档案没有覆盖全部原始成员")
        manifest = json.loads((destination / "manifest.json").read_text("utf-8"))
        if manifest.get("workspace_id") != value["workspace_id"] or manifest.get("status") not in TERMINAL:
            raise ArtifactStorageError("恢复后的档案身份或状态不正确")
        return destination

    @contextmanager
    def request_scope(self):
        if getattr(self._local, "scope", None) is not None:
            yield
            return
        with self.temporary("read") as temporary:
            self._local.scope = {"root": temporary, "workspaces": {}, "inputs": {}, "leases": {}}
            try:
                yield
            finally:
                for lock in reversed(list(self._local.scope["leases"].values())):
                    lock.release()
                self._local.scope = None

    def checkout(self, name):
        scope = getattr(self._local, "scope", None)
        if scope is None:
            raise ArtifactStorageError("OSS 档案必须在有清理保障的读取范围内打开")
        self.lease(name)
        if name not in scope["workspaces"]:
            destination = scope["root"] / "worktrees" / name
            self.restore(self.get(name), destination)
            scope["workspaces"][name] = destination
        return scope["workspaces"][name]

    def preview_inputs(self, name):
        scope = getattr(self._local, "scope", None)
        if scope is None:
            raise ArtifactStorageError("原件读取缺少临时范围")
        if name not in scope["inputs"]:
            destination = scope["root"] / "inputs" / workspace_token(name)
            destination.mkdir(mode=0o700, parents=True)
            for index, row in enumerate(self.get(name)["originals"]):
                directory = destination / str(index)
                directory.mkdir(mode=0o700)
                target = directory / Path(row["relative_path"]).name
                with target.open("xb") as output:
                    self.storage.read_to(row["key"], output, expected_size=row["size"],
                        expected_sha=row["sha256"], limit=max(row["size"], 1))
            scope["inputs"][name] = destination
        return scope["inputs"][name]

    def verify(self, name):
        value = self.get(name)
        with self.temporary("verify") as temporary:
            self.restore(value, temporary / name)
        class Discard:
            def write(self, piece):
                return len(piece)
        for row in [*value["originals"], *value["reports"].values()]:
            self.storage.read_to(row["key"], Discard(), expected_size=row["size"],
                expected_sha=row["sha256"], limit=max(row["size"], 1))
        return value

    def prune_workspace(self, name):
        with self.workspace_lock(name):
            path = self._workspace(name)
            value = self.verify(name)
            with self.gate:
                if (path / "run.pid").exists():
                    raise ArtifactStorageError("核销进程仍持有本单，未清理")
                if content_inventory(tree_inventory(path)) != content_inventory(value["files"]):
                    raise ArtifactStorageError("本地档案与已验证的 OSS 版本不同，未清理")
                # 完整复核后只逐个删除本次清单的文件，不递归删除宽目录。
                for row in sorted(value["files"], key=lambda row: row["path"] in {"manifest.json", "snapshot.json"}):
                    target = plain(path / row["path"], root=path)
                    if digest_file(target) != row["sha256"]:
                        raise ArtifactStorageError("清理前原件再次发生变化，停止后续删除")
                    target.unlink()
                for directory, dirs, _ in os.walk(path, topdown=False):
                    for child in dirs:
                        plain(Path(directory) / child, root=path).rmdir()
                path.rmdir()
            return {"workspace_id": name, "files": len(value["files"]),
                    "bytes": sum(row["size"] for row in value["files"])}

    def prune_sources(self, rows):
        class Discard:
            def write(self, piece):
                return len(piece)
        removed = 0
        for row in rows:
            path = plain(self.input_root / relative_name(row["relative_path"]), root=self.input_root)
            if not path.exists():
                continue
            if digest_file(path) != row["sha256"] or path.stat().st_size != row["size"]:
                raise ArtifactStorageError("原始资料与已上传版本不一致，未清理")
            token = hashlib.sha256(row["relative_path"].encode("utf-8")).hexdigest()
            if canonical(json.loads(self.storage.read(PREFIX+"inputs/"+token+".json"))) != canonical(row):
                raise ArtifactStorageError("原件的远端位置索引未完整保留，未清理")
            self.storage.read_to(row["key"], Discard(), expected_size=row["size"],
                expected_sha=row["sha256"], limit=max(row["size"], 1))
            path.unlink()
            removed += 1
            parent = path.parent
            while parent != self.input_root and parent.is_relative_to(self.input_root):
                try:
                    parent.rmdir()
                except OSError:
                    break
                parent = parent.parent
        return removed
