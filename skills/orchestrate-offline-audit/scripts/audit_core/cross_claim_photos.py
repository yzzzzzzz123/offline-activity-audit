"""Deployment-local scene-photo fingerprints. No models, originals or verdicts.

Only newly processed images are indexed. A byte-identical match is a provenance
fact, not a fraud finding; bounded perceptual matches are technical candidates.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import stat
import statistics
import time

from PIL import Image, ImageOps


ALGORITHM_VERSION = "scene-sha256-phash64-thumbnail-exif-v1"
SCHEMA_VERSION = 1
APPLICATION_ID = 0x50484931
SCENE_ROLES = frozenset({"display_photos", "shelf_photos", "finished_photos",
                         "gift_finished_photos", "gift_store_photos"})
MIXED_ROLES = frozenset({"giveaway_evidence", "staff_photos", "photos",
                         "price_evidence", "full_reduction_evidence"})
EXCLUDED_ROLES = frozenset({"reference", "product_reference", "template", "contract",
    "promotion_contract", "settlement", "payment", "payment_evidence", "invoice",
    "receipt", "pos", "pos_excel"})
MAX_IMAGE_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 12_000_000
MAX_UNITS = 1000
MAX_MATCHES = 10
MAX_TOTAL_MATCHES = 100
MAX_NEAR_IMAGES_PER_BATCH = 32
MAX_SOURCE_ROWS = 64
MAX_BAND_ROWS = 32
MAX_IMAGES = 100_000
MAX_OCCURRENCES = 300_000
MAX_REMOVED_RUNS = 100_000
MAX_DATABASE_BYTES = 256 * 1024 * 1024
MAX_WAL_BYTES = 16 * 1024 * 1024
JOURNAL_SIZE_BYTES = 8 * 1024 * 1024
WRITE_BATCH_SIZE = 16
NEAR_DISTANCE = 8
SAME_CLAIM_DISTANCE = NEAR_DISTANCE
MAX_SAME_CLAIM_PAIRS = 20
_HEX = re.compile(r"^[0-9a-f]{64}$")
_CLAIM = re.compile(r"^[A-Za-z0-9._:-]{1,80}$")
_CODES = {
    "PHOTO_INDEX_UNAVAILABLE": "照片索引暂不可用，本次照片仍按原流程分析。",
    "PHOTO_INDEX_INPUT_INVALID": "照片索引来源信息不完整，本次未建立索引。",
    "PHOTO_INDEX_RUN_REMOVED": "该运行来源已移除，未重新登记照片。",
    "PHOTO_IMAGE_UNAVAILABLE": "照片指纹读取失败，未将其视为已完成查重。",
}


def _diagnostic(code, unit_id=None):
    value = {"code": code, "message": _CODES[code]}
    if unit_id is not None:
        value["unit_id"] = unit_id
    return value


def _text(value, limit, *, required=True):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError("invalid source metadata")
    if required and not value.strip():
        raise ValueError("empty source metadata")
    return value


def _component(value):
    value = _text(value, 240)
    if value in {".", ".."} or any(c in value for c in "/\\:") or value.startswith("."):
        raise ValueError("invalid workspace identity")
    return value


def _plain(path, *, directory=False):
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
        raise OSError("linked photo index path")
    if directory and not stat.S_ISDIR(info.st_mode):
        raise OSError("invalid photo index directory")
    if not directory and (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1):
        raise OSError("invalid photo index file")
    return info


def _safe_chain(path):
    for parent in reversed([path, *path.parents]):
        if parent.exists() or parent.is_symlink():
            _plain(parent, directory=True)


def index_directory(scope):
    """Return a path only; never create a directory or open a database."""
    return Path(scope).absolute() / ".cache" / "photo-index-v1"


def _directory(scope, *, create):
    scope = Path(scope).absolute()
    _safe_chain(scope)
    _plain(scope, directory=True)
    root = index_directory(scope)
    for part in [root.parent, root]:
        if not part.exists():
            if not create:
                return None
            part.mkdir(mode=0o700, exist_ok=True)
        _plain(part, directory=True)
    # Never change permissions on shared worktrees/.cache or on the scope.
    if os.name != "nt":
        if root.stat().st_uid != os.getuid():
            raise OSError("photo index owned by another user")
        root.chmod(0o700)
    return root


def _removed(connection, run_id):
    return connection.execute("SELECT 1 FROM removed_runs WHERE run_id=?", (run_id,)).fetchone() is not None


def _database_paths(root):
    return [root / name for name in ["index.sqlite", "index.sqlite-wal", "index.sqlite-shm", "index.sqlite-journal"]]


def _continue_transaction(connection, root):
    """Bound write batches; a blocked checkpoint stops further WAL growth.

    The WAL threshold is a continuation gate, not a SQLite per-byte hard cap:
    at most one bounded write batch is committed before checking it again.
    """
    connection.commit()
    connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    wal = root / "index.sqlite-wal"
    if wal.exists() and _plain(wal).st_size > MAX_WAL_BYTES:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        if wal.exists() and wal.stat().st_size > MAX_WAL_BYTES:
            raise ValueError("photo index WAL budget")
    connection.execute("BEGIN IMMEDIATE")


def _schema(connection):
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    if application == 0 and version == 0:
        if connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
            raise ValueError("not a photo index")
        statements = [
            "CREATE TABLE counters (id INTEGER PRIMARY KEY CHECK(id=1), images INTEGER NOT NULL, occurrences INTEGER NOT NULL, removed_runs INTEGER NOT NULL)",
            "INSERT INTO counters VALUES (1,0,0,0)",
            "CREATE TABLE removed_runs (run_id TEXT PRIMARY KEY)",
            "CREATE TABLE images (sha256 TEXT NOT NULL, algorithm TEXT NOT NULL, phash TEXT, width INTEGER NOT NULL, height INTEGER NOT NULL, size INTEGER NOT NULL, PRIMARY KEY(sha256,algorithm))",
            "CREATE TABLE bands (algorithm TEXT NOT NULL, band INTEGER NOT NULL, value TEXT NOT NULL, sha256 TEXT NOT NULL, PRIMARY KEY(algorithm,band,value,sha256), FOREIGN KEY(sha256,algorithm) REFERENCES images(sha256,algorithm) ON DELETE CASCADE)",
            "CREATE TABLE occurrences (id INTEGER PRIMARY KEY, run_id TEXT NOT NULL, workspace_id TEXT NOT NULL, verify_code TEXT, archive_sha256 TEXT NOT NULL, unit_id TEXT NOT NULL, source_file TEXT NOT NULL, locator TEXT NOT NULL, role TEXT NOT NULL, sha256 TEXT NOT NULL, algorithm TEXT NOT NULL, recognition_version TEXT NOT NULL, created_at INTEGER NOT NULL, UNIQUE(run_id,archive_sha256,unit_id), FOREIGN KEY(sha256,algorithm) REFERENCES images(sha256,algorithm))",
            "CREATE INDEX occurrences_image ON occurrences(sha256,algorithm,verify_code,id)",
            "CREATE INDEX occurrences_run ON occurrences(run_id)",
        ]
        for statement in statements:
            connection.execute(statement)
        for table in ("images", "occurrences", "removed_runs"):
            for action, sign in (("INSERT", "+"), ("DELETE", "-")):
                connection.execute(f"CREATE TRIGGER count_{table}_{action.lower()} AFTER {action} ON {table} BEGIN UPDATE counters SET {table}={table}{sign}1 WHERE id=1; END")
        connection.execute(f"PRAGMA application_id={APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    elif application != APPLICATION_ID or version != SCHEMA_VERSION:
        raise ValueError("unsupported photo index version")


@contextmanager
def _index_lock(root):
    """Bounded process/thread lock also serializes SQLite's first WAL setup."""
    path = root / "index.lock"
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(b"0")
    _plain(path)
    with path.open("r+b") as stream:
        deadline = time.monotonic() + 0.8
        if os.name == "nt":
            import msvcrt
        else:
            import fcntl
        while True:
            try:
                stream.seek(0)
                if os.name == "nt":
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError("photo index lock timeout") from None
                time.sleep(0.025)
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


@contextmanager
def _database(root, *, create):
    with _index_lock(root):
        with _open_database(root, create=create) as connection:
            yield connection


@contextmanager
def _open_database(root, *, create):
    paths = _database_paths(root)
    database = paths[0]
    if not database.exists():
        if not create:
            yield None
            return
        try:
            descriptor = os.open(database, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(descriptor)
    for path in paths:
        if path.exists() or path.is_symlink():
            _plain(path)
    connection = sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=0.8)
    connection.row_factory = sqlite3.Row
    validated = False
    try:
        connection.execute("PRAGMA busy_timeout=800")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA cache_size=-4096")
        connection.execute("PRAGMA temp_store=FILE")
        identity = (connection.execute("PRAGMA application_id").fetchone()[0],
                    connection.execute("PRAGMA user_version").fetchone()[0])
        if identity not in {(0, 0), (APPLICATION_ID, SCHEMA_VERSION)}:
            raise ValueError("unsupported photo index version")
        if identity == (0, 0) and connection.execute("SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1").fetchone():
            raise ValueError("not a photo index")
        validated = True
        if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower() != "wal":
            raise ValueError("photo index WAL unavailable")
        page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count={MAX_DATABASE_BYTES // page_size}")
        connection.execute(f"PRAGMA journal_size_limit={JOURNAL_SIZE_BYTES}")
        connection.execute("PRAGMA wal_autocheckpoint=256")
        wal = root / "index.sqlite-wal"
        if wal.exists() and _plain(wal).st_size > MAX_WAL_BYTES:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            if wal.exists() and wal.stat().st_size > MAX_WAL_BYTES:
                raise ValueError("photo index WAL budget")
        connection.execute("BEGIN IMMEDIATE")
        _schema(connection)
        yield connection
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()
        if validated and os.name != "nt":
            for path in paths:
                if path.exists():
                    _plain(path)
                    path.chmod(0o600)


def _fingerprint(path):
    path = Path(path)
    _safe_chain(path.absolute().parent)
    initial = _plain(path)
    if not 0 < initial.st_size <= MAX_IMAGE_BYTES:
        raise ValueError("image byte limit")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags), "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (initial.st_dev, initial.st_ino, initial.st_size) != (opened.st_dev, opened.st_ino, opened.st_size):
            raise OSError("image changed before reading")
        digest = hashlib.sha256()
        read_bytes = 0
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            read_bytes += len(block)
            if read_bytes > MAX_IMAGE_BYTES:
                raise ValueError("image grew past byte limit")
            digest.update(block)
        stream.seek(0)
        pixels = None
        near_skipped_reason = None
        with Image.open(stream) as image:
            width, height = image.size
            if image.getexif().get(274) in {5, 6, 7, 8}:
                width, height = height, width
            if width * height > MAX_IMAGE_PIXELS or getattr(image, "n_frames", 1) != 1:
                near_skipped_reason = "image_pixels_or_frames"
            else:
                # JPEG draft avoids full-size decoding where supported. Shrink
                # before EXIF transpose; full-sized copies are never retained.
                image.draft("L", (128, 128))
                image.thumbnail((128, 128), Image.Resampling.LANCZOS)
                oriented = ImageOps.exif_transpose(image)
                grayscale = oriented.convert("L").resize((32, 32), Image.Resampling.LANCZOS)
                flattened = getattr(grayscale, "get_flattened_data", None)
                pixels = list(flattened() if callable(flattened) else grayscale.getdata())
        final = path.stat()
        if (initial.st_dev, initial.st_ino, initial.st_size, initial.st_mtime_ns) != (final.st_dev, final.st_ino, final.st_size, final.st_mtime_ns):
            raise OSError("image changed during reading")
    phash = None
    if pixels is not None:
        cosine = [[math.cos((2 * position + 1) * frequency * math.pi / 64) for position in range(32)] for frequency in range(8)]
        rows = [[sum(pixels[row * 32 + column] * cosine[x][column] for column in range(32)) for x in range(8)] for row in range(32)]
        values = [sum(rows[row][x] * cosine[y][row] for row in range(32)) for y in range(8) for x in range(8)]
        middle = statistics.median(values[1:])
        number = 0
        for value in values:
            number = (number << 1) | int(value > middle)
        phash = f"{number:016x}"
    return {"sha256": digest.hexdigest(), "phash": phash, "width": width, "height": height,
            "size": initial.st_size, "near_skipped_reason": near_skipped_reason}


def _eligible(unit):
    role = unit.get("role")
    roles = unit.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    if not isinstance(roles, (list, tuple)) or not all(isinstance(value, str) for value in roles):
        return False
    if unit.get("reference") or role in EXCLUDED_ROLES or EXCLUDED_ROLES.intersection(roles):
        return False
    return role in SCENE_ROLES or (role in MIXED_ROLES and unit.get("scene_photo") is True)


def _live_source(scope, root, row):
    """A deleted, running, failed or unrelated worktree cannot become a reference."""
    try:
        workspace = scope / _component(row["workspace_id"])
        _plain(workspace, directory=True)
        path = workspace / "manifest.json"
        if _plain(path).st_size > 1024 * 1024:
            return False
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        return (value.get("status") == "completed" and value.get("run_id") == row["run_id"]
                and value.get("workspace_id") == row["workspace_id"])
    except (OSError, ValueError, TypeError):
        return False


def _sources(connection, scope, root, sha256, verify_code, run_id):
    if not verify_code:
        return [], False
    rows = connection.execute(
        "SELECT * FROM occurrences o WHERE sha256=? AND algorithm=? AND verify_code IS NOT NULL AND verify_code<>? AND run_id<>? AND NOT EXISTS (SELECT 1 FROM removed_runs r WHERE r.run_id=o.run_id) ORDER BY id DESC LIMIT ?",
        (sha256, ALGORITHM_VERSION, verify_code, run_id, MAX_SOURCE_ROWS + 1)).fetchall()
    matches = []
    for row in rows[:MAX_SOURCE_ROWS]:
        if not _live_source(scope, root, row):
            continue
        matches.append({key: row[key] for key in ("run_id", "workspace_id", "verify_code", "archive_sha256", "unit_id", "source_file", "locator", "role", "sha256", "algorithm", "recognition_version")})
        if len(matches) == MAX_MATCHES:
            break
    return matches, len(rows) > MAX_SOURCE_ROWS or len(matches) >= MAX_MATCHES


def _matches(connection, scope, root, fingerprint, verify_code, run_id, *, near_enabled=True):
    exact, limited = _sources(connection, scope, root, fingerprint["sha256"], verify_code, run_id)
    candidates = {}
    if verify_code and fingerprint["phash"] and near_enabled:
        for band in range(4):
            rows = connection.execute(
                "SELECT i.sha256,i.phash FROM bands b JOIN images i ON i.sha256=b.sha256 AND i.algorithm=b.algorithm WHERE b.algorithm=? AND b.band=? AND b.value=? AND b.sha256<>? ORDER BY b.sha256 LIMIT ?",
                (ALGORITHM_VERSION, band, fingerprint["phash"][band * 4:band * 4 + 4], fingerprint["sha256"], MAX_BAND_ROWS + 1)).fetchall()
            limited = limited or len(rows) > MAX_BAND_ROWS
            for row in rows[:MAX_BAND_ROWS]:
                distance = (int(row["phash"], 16) ^ int(fingerprint["phash"], 16)).bit_count()
                if distance <= NEAR_DISTANCE:
                    candidates[row["sha256"]] = distance
    near = []
    for sha256, distance in sorted(candidates.items(), key=lambda pair: (pair[1], pair[0])):
        sources, truncated = _sources(connection, scope, root, sha256, verify_code, run_id)
        limited = limited or truncated
        for source in sources:
            near.append({**source, "distance": distance, "technical_only": True})
            if len(near) == MAX_MATCHES:
                return exact, near, True
    return exact, near, limited or not near_enabled


def same_claim_candidates(prepared):
    """Perceptual near-duplicates inside one claim.

    Two photos of the same shelf shot twice for two different stores look
    identical but carry different watermarks, so byte identity and watermark
    address both miss them. This reports technical candidates only; the
    executing model decides what they mean.
    """
    values = [(unit["unit_id"], fingerprint["phash"])
              for unit, _source, _locator, fingerprint, _photo in prepared
              if fingerprint.get("phash")]
    pairs = []
    for index, (left_id, left) in enumerate(values):
        for right_id, right in values[index + 1:]:
            distance = (int(left, 16) ^ int(right, 16)).bit_count()
            if distance <= SAME_CLAIM_DISTANCE:
                pairs.append({"unit_ids": sorted([left_id, right_id]), "distance": distance})
    pairs.sort(key=lambda item: (item["distance"], item["unit_ids"]))
    return pairs[:MAX_SAME_CLAIM_PAIRS]


def model_advice(advice, units=(), documents=()):
    """Attach readable source labels and facts to technical near-pair IDs.

    The candidate remains a technical observation. This helper only lets the
    executing model find the two photos and compare their visible watermarks
    and scene details without changing either source fact or adding a verdict.
    """
    if not isinstance(advice, dict):
        return {}
    candidates = advice.get("same_claim_candidates") or []
    if not isinstance(candidates, list) or not candidates:
        return advice
    units_by_id = {unit.get("unit_id"): unit for unit in (units or ()) if isinstance(unit, dict)}
    documents_by_id = {document.get("unit_id"): document
                       for document in (documents or ()) if isinstance(document, dict)}
    enriched = []
    for pair in candidates:
        if not isinstance(pair, dict):
            continue
        photos = []
        for unit_id in pair.get("unit_ids") or []:
            unit = units_by_id.get(unit_id, {})
            document = documents_by_id.get(unit_id, {})
            photos.append({
                "unit_id": unit_id,
                "source_file": unit.get("source_file"),
                "locator": unit.get("locator"),
                "roles": list(document.get("roles") or []),
                "facts": list(document.get("facts") or []),
                "limitations": list(document.get("limitations") or []),
            })
        enriched.append({**pair, "photos": photos})
    return {**advice, "same_claim_candidates": enriched}


def inspect_and_record(scope, *, verify_code, run_id, archive_sha256, units,
                       recognition_version="", workspace_id=None):
    """Hash eligible current images, inspect bounded local history, then register.

    Mixed photo/document roles require scene_photo=True from explicit facts.
    An absent business identity is stored as unknown and never cross-claimed.
    No fact/AI result is cached here; recognition_version is provenance only.
    """
    result = {"status": "ready", "algorithm_version": ALGORITHM_VERSION,
              "coverage": {"mode": "newly_processed_only", "status": "partial", "historical_backfill": False,
                           "historical_sources": "completed_workspaces_only", "near_search_exhaustive": False,
                           "candidate_limit_per_band": MAX_BAND_ROWS, "indexed": 0, "skipped": 0,
                           "limit_reached": [], "limits": {"images": MAX_IMAGES, "occurrences": MAX_OCCURRENCES,
                           "database_bytes": MAX_DATABASE_BYTES, "output_matches": MAX_TOTAL_MATCHES,
                           "near_images_per_batch": MAX_NEAR_IMAGES_PER_BATCH}},
              "photos": [], "diagnostics": []}
    result["same_claim_candidates"] = []
    try:
        scope = Path(scope).absolute()
        run_id = _text(run_id, 240)
        workspace_id = _component(workspace_id if workspace_id is not None else run_id)
        recognition_version = _text(recognition_version, 512, required=False)
        if not isinstance(archive_sha256, str) or not _HEX.fullmatch(archive_sha256):
            raise ValueError("invalid archive hash")
        if verify_code in (None, ""):
            verify_code = None
        elif not isinstance(verify_code, str) or not _CLAIM.fullmatch(verify_code):
            raise ValueError("invalid business identity")
        if not isinstance(units, (list, tuple)) or len(units) > MAX_UNITS:
            raise ValueError("invalid unit collection")
        prepared = []
        seen = set()
        for unit in units:
            if not isinstance(unit, dict):
                raise ValueError("invalid unit")
            uid = _text(unit.get("unit_id"), 240)
            if uid in seen:
                raise ValueError("duplicate unit")
            seen.add(uid)
            photo = {"unit_id": uid, "status": "skipped", "exact_reuse": [], "near_candidates": []}
            result["photos"].append(photo)
            if not _eligible(unit):
                result["coverage"]["skipped"] += 1
                continue
            source_file = _text(unit.get("source_file"), 1024)
            locator = _text(unit.get("locator", ""), 1024, required=False)
            try:
                fingerprint = _fingerprint(unit.get("image"))
            except (OSError, ValueError, TypeError, Image.DecompressionBombError):
                photo["status"] = "unavailable"
                result["status"] = "degraded"
                result["diagnostics"].append(_diagnostic("PHOTO_IMAGE_UNAVAILABLE", uid))
                continue
            photo["sha256"] = fingerprint["sha256"]
            photo["status"] = "prepared"
            if fingerprint["near_skipped_reason"]:
                photo["near_skipped_reason"] = fingerprint["near_skipped_reason"]
                result["coverage"]["limit_reached"].append(fingerprint["near_skipped_reason"])
            prepared.append((unit, source_file, locator, fingerprint, photo))
        if not prepared:
            if result["status"] == "ready":
                result["status"] = "skipped"
            return result
        near_pairs = same_claim_candidates(prepared)
        result["same_claim_candidates"] = near_pairs
        by_photo = {photo.get("unit_id"): photo for photo in result["photos"]
                    if photo.get("status") != "skipped"}
        for pair in near_pairs:
            left, right = pair["unit_ids"]
            for uid, other in ((left, right), (right, left)):
                photo = by_photo.get(uid)
                if photo is not None:
                    photo.setdefault("same_claim_near", []).append(
                        {"unit_id": other, "distance": pair["distance"]})
        root = _directory(scope, create=True)
        with _database(root, create=True) as connection:
            if _removed(connection, run_id):
                raise ValueError("removed run")
            output_remaining = MAX_TOTAL_MATCHES
            near_remaining = MAX_NEAR_IMAGES_PER_BATCH
            indexed = 0
            for unit, source_file, locator, fingerprint, photo in prepared:
                if output_remaining:
                    near_enabled = near_remaining > 0
                    if fingerprint["phash"] and verify_code:
                        if near_enabled:
                            near_remaining -= 1
                        else:
                            result["coverage"]["limit_reached"].append("near_search_work")
                    exact, near, limited = _matches(connection, scope, root, fingerprint, verify_code, run_id, near_enabled=near_enabled)
                    if len(exact) + len(near) > output_remaining:
                        limited = True
                    exact = exact[:output_remaining]
                    near = near[:max(0, output_remaining - len(exact))]
                    output_remaining -= len(exact) + len(near)
                else:
                    exact, near, limited = [], [], True
                    result["coverage"]["limit_reached"].append("output_matches")
                photo.update(status="exact_reuse" if exact else "indexed", exact_reuse=exact,
                             near_candidates=near, candidates_limited=limited)
                counts = connection.execute("SELECT images,occurrences FROM counters WHERE id=1").fetchone()
                known_image = connection.execute("SELECT 1 FROM images WHERE sha256=? AND algorithm=?", (fingerprint["sha256"], ALGORITHM_VERSION)).fetchone()
                known_occurrence = connection.execute("SELECT sha256,algorithm FROM occurrences WHERE run_id=? AND archive_sha256=? AND unit_id=?", (run_id, archive_sha256, unit["unit_id"])).fetchone()
                limit = "images" if not known_image and counts["images"] >= MAX_IMAGES else "occurrences" if not known_occurrence and counts["occurrences"] >= MAX_OCCURRENCES else None
                if limit:
                    result["status"] = "partial"
                    result["coverage"]["limit_reached"].append(limit)
                    photo.update(status="exact_reuse" if exact else "not_indexed", indexed=False)
                    if known_occurrence and known_occurrence["sha256"] != fingerprint["sha256"]:
                        connection.execute("DELETE FROM occurrences WHERE run_id=? AND archive_sha256=? AND unit_id=?", (run_id, archive_sha256, unit["unit_id"]))
                    continue
                connection.execute("INSERT OR IGNORE INTO images VALUES (?,?,?,?,?,?)",
                    (fingerprint["sha256"], ALGORITHM_VERSION, fingerprint["phash"], fingerprint["width"], fingerprint["height"], fingerprint["size"]))
                for band in range(4) if fingerprint["phash"] else ():
                    connection.execute("INSERT OR IGNORE INTO bands VALUES (?,?,?,?)",
                        (ALGORITHM_VERSION, band, fingerprint["phash"][band * 4:band * 4 + 4], fingerprint["sha256"]))
                connection.execute("INSERT INTO occurrences(run_id,workspace_id,verify_code,archive_sha256,unit_id,source_file,locator,role,sha256,algorithm,recognition_version,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,archive_sha256,unit_id) DO UPDATE SET workspace_id=excluded.workspace_id,verify_code=excluded.verify_code,source_file=excluded.source_file,locator=excluded.locator,role=excluded.role,sha256=excluded.sha256,algorithm=excluded.algorithm,recognition_version=excluded.recognition_version",
                    (run_id, workspace_id, verify_code, archive_sha256, unit["unit_id"], source_file, locator, unit["role"], fingerprint["sha256"], ALGORITHM_VERSION, recognition_version, int(time.time())))
                photo["indexed"] = True
                indexed += 1
                if indexed % WRITE_BATCH_SIZE == 0:
                    _continue_transaction(connection, root)
        result["coverage"]["indexed"] = indexed
        result["coverage"]["limit_reached"] = sorted(set(result["coverage"]["limit_reached"]))
        result["coverage"]["status"] = "partial" if result["coverage"]["limit_reached"] or result["status"] == "degraded" else "current_batch_indexed"
    except (OSError, sqlite3.Error, ValueError, TypeError, OverflowError):
        result["status"] = "degraded"
        result["coverage"]["indexed"] = 0
        for photo in result["photos"]:
            if photo["status"] != "skipped":
                photo.update(status="unavailable", indexed=False, exact_reuse=[], near_candidates=[])
        result["diagnostics"].append(_diagnostic("PHOTO_INDEX_UNAVAILABLE"))
    return result


def remove_run(scope, run_id):
    """Hide this run durably, remove its occurrences, retain other same-SHA uses.

    No database is created if absent. source_hidden=False on failure means the
    caller must keep the source. A bounded SQL tombstone blocks late writers.
    """
    result = {"status": "ready", "removed_occurrences": 0, "source_hidden": False, "diagnostics": []}
    try:
        run_id = _text(run_id, 240)
        root = _directory(scope, create=False)
        if root is None or not (root / "index.sqlite").exists():
            result["source_hidden"] = True
            return result
        with _database(root, create=False) as connection:
            if connection is None:
                result["source_hidden"] = True
                return result
            if not _removed(connection, run_id):
                count = connection.execute("SELECT removed_runs FROM counters WHERE id=1").fetchone()[0]
                if count >= MAX_REMOVED_RUNS:
                    raise ValueError("removed run budget")
                connection.execute("INSERT INTO removed_runs VALUES (?)", (run_id,))
            connection.commit()
            result["source_hidden"] = True
            _continue_transaction(connection, root)
            # Delete unshared images with their occurrences in bounded chunks;
            # never fetch the full set of a large run into Python.
            while True:
                rows = connection.execute("SELECT id,sha256,algorithm FROM occurrences WHERE run_id=? LIMIT 128", (run_id,)).fetchall()
                if not rows:
                    break
                for row in rows:
                    connection.execute("DELETE FROM occurrences WHERE id=?", (row["id"],))
                    result["removed_occurrences"] += 1
                    connection.execute("DELETE FROM images WHERE sha256=? AND algorithm=? AND NOT EXISTS (SELECT 1 FROM occurrences WHERE sha256=? AND algorithm=?)", (row["sha256"], row["algorithm"], row["sha256"], row["algorithm"]))
                _continue_transaction(connection, root)
        result["source_hidden"] = True
    except (OSError, sqlite3.Error, ValueError, TypeError):
        result["status"] = "degraded"
        result["removed_occurrences"] = 0
        result["diagnostics"].append(_diagnostic("PHOTO_INDEX_UNAVAILABLE"))
    return result
