from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import stat
import sys
import tempfile
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO


MAX_ARCHIVE_FILES = 2_000
MAX_MEMBER_BYTES = 250 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200
COPY_CHUNK_BYTES = 1024 * 1024
SPOOL_MEMORY_BYTES = 16 * 1024 * 1024

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
SPREADSHEET_SUFFIXES = {".xlsx", ".xlsm", ".xls", ".csv"}
DOCUMENT_SUFFIXES = {".doc", ".docx", ".txt", ".rtf"}


class ProfileError(ValueError):
    """The representative ZIP is unsafe or cannot be inspected deterministically."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(COPY_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_zip(source: Path | BinaryIO) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(source, metadata_encoding="gbk")
    except (TypeError, UnicodeDecodeError):
        if hasattr(source, "seek"):
            source.seek(0)
        return zipfile.ZipFile(source)


def _safe_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw = info.filename.replace("\\", "/")
    if "\x00" in raw:
        raise ProfileError("ZIP 成员名包含 NUL 字节")
    pure = PurePosixPath(raw)
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if pure.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ProfileError(f"ZIP 包含不安全路径：{info.filename!r}")
    if ":" in parts[0]:
        raise ProfileError(f"ZIP 包含盘符路径：{info.filename!r}")
    invalid = set('<>:"|?*')
    if any(any(char in invalid for char in part) for part in parts):
        raise ProfileError(f"ZIP 文件名不受 Windows 支持：{info.filename!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ProfileError(f"ZIP 不接受符号链接：{info.filename!r}")
    if info.flag_bits & 0x1:
        raise ProfileError(f"ZIP 不接受加密成员：{info.filename!r}")
    return parts


def _kind(suffix: str) -> str:
    if suffix in IMAGE_SUFFIXES:
        return "image"
    if suffix in SPREADSHEET_SUFFIXES:
        return "spreadsheet"
    if suffix == ".pdf":
        return "pdf"
    if suffix in DOCUMENT_SUFFIXES:
        return "document"
    if suffix == ".zip":
        return "nested_archive"
    return "other"


def _validate_info(info: zipfile.ZipInfo, archive_label: str) -> tuple[str, ...]:
    parts = _safe_parts(info)
    if info.file_size > MAX_MEMBER_BYTES:
        raise ProfileError(
            f"ZIP 成员超过 {MAX_MEMBER_BYTES} 字节限制：{archive_label}!/{info.filename}"
        )
    if info.file_size and not info.is_dir():
        ratio = info.file_size / max(1, info.compress_size)
        if ratio > MAX_COMPRESSION_RATIO:
            raise ProfileError(
                f"ZIP 成员压缩比可疑（{ratio:.1f}）：{archive_label}!/{info.filename}"
            )
    return parts


def _copy_limited(source: BinaryIO, target: BinaryIO, limit: int) -> int:
    copied = 0
    while True:
        chunk = source.read(COPY_CHUNK_BYTES)
        if not chunk:
            return copied
        copied += len(chunk)
        if copied > limit:
            raise ProfileError(f"嵌套 ZIP 超过 {limit} 字节读取限制")
        target.write(chunk)


def _profile_open_archive(
    archive: zipfile.ZipFile,
    *,
    archive_label: str,
    depth: int,
    max_depth: int,
    budget: dict[str, int],
) -> dict[str, Any]:
    infos = archive.infolist()
    budget["members"] += len(infos)
    if budget["members"] > MAX_ARCHIVE_FILES:
        raise ProfileError(
            f"ZIP 递归成员过多（>{MAX_ARCHIVE_FILES}）：{archive_label}"
        )

    seen: set[str] = set()
    suffixes: Counter[str] = Counter()
    kinds: Counter[str] = Counter()
    members: list[dict[str, Any]] = []
    nested_archives: list[dict[str, Any]] = []

    for info in infos:
        parts = _validate_info(info, archive_label)
        normalized_path = "/".join(parts)
        collision_key = normalized_path.casefold()
        if collision_key in seen:
            raise ProfileError(
                f"ZIP 包含重复或大小写冲突路径：{archive_label}!/{normalized_path}"
            )
        seen.add(collision_key)
        if info.is_dir():
            continue

        budget["bytes"] += info.file_size
        if budget["bytes"] > MAX_TOTAL_BYTES:
            raise ProfileError(
                f"ZIP 递归展开总量超过 {MAX_TOTAL_BYTES} 字节：{archive_label}"
            )

        suffix = PurePosixPath(normalized_path).suffix.lower()
        member_kind = _kind(suffix)
        suffixes[suffix or "<none>"] += 1
        kinds[member_kind] += 1
        members.append(
            {
                "path": normalized_path,
                "basename": parts[-1],
                "suffix": suffix,
                "kind": member_kind,
                "uncompressed_bytes": info.file_size,
                "compressed_bytes": info.compress_size,
            }
        )

        if suffix == ".zip" and depth < max_depth:
            try:
                with archive.open(info, "r") as nested_source:
                    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MEMORY_BYTES) as nested_file:
                        copied = _copy_limited(nested_source, nested_file, MAX_MEMBER_BYTES)
                        if copied != info.file_size:
                            raise ProfileError(
                                f"嵌套 ZIP 读取长度不一致：{archive_label}!/{normalized_path}"
                            )
                        nested_file.seek(0)
                        with _open_zip(nested_file) as nested_zip:
                            nested_archives.append(
                                _profile_open_archive(
                                    nested_zip,
                                    archive_label=f"{archive_label}!/{normalized_path}",
                                    depth=depth + 1,
                                    max_depth=max_depth,
                                    budget=budget,
                                )
                            )
            except zipfile.BadZipFile as exc:
                raise ProfileError(
                    f"扩展名为 .zip 的成员不是有效 ZIP：{archive_label}!/{normalized_path}"
                ) from exc

    return {
        "archive_label": archive_label,
        "depth": depth,
        "member_count": len(infos),
        "file_count": len(members),
        "suffix_counts": dict(sorted(suffixes.items())),
        "kind_counts": dict(sorted(kinds.items())),
        "members": members,
        "nested_archives": nested_archives,
    }


def profile_archive(path: str | Path, *, max_depth: int = 2) -> dict[str, Any]:
    source = Path(path).resolve()
    if not source.is_file():
        raise ProfileError(f"找不到代表性 ZIP：{source}")
    if source.suffix.lower() != ".zip":
        raise ProfileError(f"代表性输入必须是 ZIP：{source.name}")
    if source.is_symlink():
        raise ProfileError(f"代表性 ZIP 不能是符号链接：{source.name}")
    if not 0 <= max_depth <= 3:
        raise ProfileError("max_depth 必须在 0～3 之间")

    budget = {"members": 0, "bytes": 0}
    try:
        with _open_zip(source) as archive:
            inventory = _profile_open_archive(
                archive,
                archive_label=source.name,
                depth=0,
                max_depth=max_depth,
                budget=budget,
            )
    except zipfile.BadZipFile as exc:
        raise ProfileError(f"不是有效 ZIP：{source.name}") from exc

    return {
        "schema_version": "1.0",
        "source_path": str(source),
        "source_name": source.name,
        "source_size": source.stat().st_size,
        "source_sha256": _sha256(source),
        "safety_limits": {
            "max_recursive_members": MAX_ARCHIVE_FILES,
            "max_member_bytes": MAX_MEMBER_BYTES,
            "max_recursive_uncompressed_bytes": MAX_TOTAL_BYTES,
            "max_compression_ratio": MAX_COMPRESSION_RATIO,
            "max_nested_depth": max_depth,
        },
        "recursive_member_count": budget["members"],
        "recursive_uncompressed_bytes": budget["bytes"],
        "inventory": inventory,
    }


def extract_outer_archive(path: str | Path, target: str | Path) -> Path:
    source = Path(path).resolve()
    destination_root = Path(target).resolve()
    if destination_root.exists() or destination_root.is_symlink():
        raise ProfileError(f"临时解压目标必须尚不存在：{destination_root}")

    profile_archive(source)
    destination_root.mkdir(parents=True)
    try:
        with _open_zip(source) as archive:
            for info in archive.infolist():
                parts = _validate_info(info, source.name)
                destination = destination_root.joinpath(*parts).resolve()
                try:
                    destination.relative_to(destination_root)
                except ValueError as exc:
                    raise ProfileError(f"ZIP 成员越过临时目录：{info.filename!r}") from exc
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as member_source, destination.open("xb") as member_target:
                    copied = _copy_limited(member_source, member_target, MAX_MEMBER_BYTES)
                if copied != info.file_size:
                    raise ProfileError(f"ZIP 成员解压长度不一致：{info.filename!r}")
    except Exception:
        shutil.rmtree(destination_root, ignore_errors=True)
        raise
    return destination_root


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="安全盘点一个新核销场景的代表性 ZIP，不执行场景分类或业务判断。"
    )
    parser.add_argument("zip_path", help="代表性 ZIP 的路径")
    parser.add_argument(
        "--max-depth",
        type=int,
        default=2,
        help="递归盘点嵌套 ZIP 的最大深度（0～3，默认 2）",
    )
    parser.add_argument(
        "--extract-to",
        help="可选：安全解压外层 ZIP 到一个尚不存在的临时目录",
    )
    parser.add_argument("--compact", action="store_true", help="输出紧凑 JSON")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _parser().parse_args(argv)
    try:
        result = profile_archive(args.zip_path, max_depth=args.max_depth)
        if args.extract_to:
            result["extracted_to"] = str(
                extract_outer_archive(args.zip_path, args.extract_to)
            )
    except (OSError, ProfileError, zipfile.BadZipFile) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
            sort_keys=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
