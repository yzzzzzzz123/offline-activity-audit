from __future__ import annotations

import shutil
import stat
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .common import AuditError, normalize_text, sha256_file


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
MAX_ARCHIVE_FILES = 2_000
MAX_MEMBER_BYTES = 250 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


class ArchiveInputError(AuditError):
    """Raised when a ZIP cannot be routed or extracted safely."""


def _open_zip(path: Path) -> zipfile.ZipFile:
    try:
        return zipfile.ZipFile(path, metadata_encoding="gbk")
    except (TypeError, UnicodeDecodeError):
        return zipfile.ZipFile(path)


def _zip_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw = info.filename.replace("\\", "/")
    if "\x00" in raw:
        raise ArchiveInputError("ZIP 成员名包含 NUL 字节")
    pure = PurePosixPath(raw)
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if pure.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ArchiveInputError(f"ZIP 包含不安全路径：{info.filename!r}")
    if ":" in parts[0]:
        raise ArchiveInputError(f"ZIP 包含盘符路径：{info.filename!r}")
    invalid = set('<>:"|?*')
    if any(any(char in invalid for char in part) for part in parts):
        raise ArchiveInputError(f"ZIP 文件名不受 Windows 支持：{info.filename!r}")
    if stat.S_ISLNK(info.external_attr >> 16):
        raise ArchiveInputError(f"ZIP 不接受符号链接：{info.filename!r}")
    if info.flag_bits & 0x1:
        raise ArchiveInputError(f"ZIP 不接受加密成员：{info.filename!r}")
    return parts


def _archive_shape(path: Path) -> dict[str, Any]:
    try:
        with _open_zip(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ArchiveInputError(
                    f"ZIP 成员过多（{len(infos)}）：{path.name}"
                )
            names: list[str] = []
            suffixes: Counter[str] = Counter()
            for info in infos:
                _zip_member_parts(info)
                if info.is_dir():
                    continue
                names.append(info.filename)
                suffixes[PurePosixPath(info.filename.replace("\\", "/")).suffix.lower()] += 1
    except zipfile.BadZipFile as exc:
        raise ArchiveInputError(f"不是有效 ZIP：{path.name}") from exc

    image_count = sum(suffixes[suffix] for suffix in IMAGE_SUFFIXES)
    excel_count = sum(suffixes[suffix] for suffix in EXCEL_SUFFIXES)
    pdf_count = suffixes[".pdf"]
    normalized_names = [normalize_text(PurePosixPath(name).name) for name in names]
    return {
        "path": path.resolve(),
        "member_count": len(infos),
        "names": names,
        "normalized_names": normalized_names,
        "image_count": image_count,
        "excel_count": excel_count,
        "pdf_count": pdf_count,
        "suffixes": dict(suffixes),
    }


def _classify_archive(path: Path) -> dict[str, Any]:
    shape = _archive_shape(path)
    excel_count = int(shape["excel_count"])
    pdf_count = int(shape["pdf_count"])
    image_count = int(shape["image_count"])
    names = list(shape["normalized_names"])
    archive_text = normalize_text(path.stem)
    settlement_count = sum("结算单" in name or "结算表" in name for name in names)
    transfer_count = sum(
        any(marker in name for marker in ("红包", "转账", "付款", "收款"))
        for name in names
    )

    if excel_count == 1 and pdf_count == 1 and image_count >= 1:
        return {**shape, "scenario": "promotional_display"}

    if excel_count == 1 and pdf_count == 0 and image_count >= 2:
        personnel_named = "人员激励" in archive_text or settlement_count == 1
        if personnel_named and settlement_count == 1 and transfer_count >= 1:
            return {**shape, "scenario": "personnel_incentive"}
        raise ArchiveInputError(
            f"疑似人员激励包但材料角色不明确：{path.name}；"
            f"结算单候选={settlement_count}，转账截图候选={transfer_count}。"
            "需提供且仅提供一张名称可识别的结算单，以及至少一张红包/转账凭证。"
        )

    missing: list[str] = []
    if excel_count != 1:
        missing.append(f"Excel 应为1份、实际{excel_count}份")
    if pdf_count == 0 and image_count < 2:
        missing.append(f"人员类图片至少2张、实际{image_count}张")
    if pdf_count > 0 and pdf_count != 1:
        missing.append(f"堆头合同 PDF 应为1份、实际{pdf_count}份")
    if pdf_count == 1 and image_count < 1:
        missing.append("堆头现场照片缺失")
    detail = "；".join(missing) or (
        f"Excel={excel_count}，PDF={pdf_count}，图片={image_count}"
    )
    raise ArchiveInputError(f"无法判断材料类型或材料缺失：{path.name}；{detail}")


def discover_archives(input_dir: str | Path) -> dict[str, dict[str, Any]]:
    requested = Path(input_dir)
    if requested.is_symlink():
        raise ArchiveInputError("input 目录不能是符号链接")
    root = requested.resolve()
    if not root.is_dir():
        raise ArchiveInputError(f"input 目录不存在：{root}")
    archives = sorted(
        (
            path
            for path in root.iterdir()
            if path.is_file() and path.suffix.lower() == ".zip"
        ),
        key=lambda path: path.name.casefold(),
    )
    if not 1 <= len(archives) <= 2:
        raise ArchiveInputError(
            f"input/ 必须直接包含 1～2 个 ZIP；当前发现 {len(archives)} 个。"
        )
    linked = [path.name for path in archives if path.is_symlink()]
    if linked:
        raise ArchiveInputError("ZIP 不能是符号链接：" + "、".join(linked))

    classified: dict[str, dict[str, Any]] = {}
    for archive in archives:
        item = _classify_archive(archive)
        scenario = str(item["scenario"])
        if scenario in classified:
            label = "人员激励" if scenario == "personnel_incentive" else "堆头陈列"
            raise ArchiveInputError(
                f"同类材料重复：发现两份{label} ZIP；同一类型最多提交一份。"
            )
        classified[scenario] = item
    return classified


def _inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _extract_archive(archive_path: Path, target_root: Path) -> dict[str, Any]:
    if target_root.exists() or target_root.is_symlink():
        raise ArchiveInputError(f"临时解压目录已存在：{target_root}")
    target_root.mkdir(parents=True)
    target_resolved = target_root.resolve()
    total_bytes = 0
    seen_targets: set[str] = set()
    extracted: list[Path] = []
    try:
        with _open_zip(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ArchiveInputError(f"ZIP 成员过多：{archive_path.name}")
            for info in infos:
                parts = _zip_member_parts(info)
                destination = target_root.joinpath(*parts).resolve()
                if not _inside(destination, target_resolved):
                    raise ArchiveInputError(f"ZIP 成员越过解压目录：{info.filename!r}")
                key = str(destination).casefold()
                if key in seen_targets:
                    raise ArchiveInputError(f"ZIP 路径重复或大小写冲突：{info.filename!r}")
                seen_targets.add(key)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ArchiveInputError(f"ZIP 成员过大：{info.filename!r}")
                total_bytes += info.file_size
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ArchiveInputError(f"ZIP 解压总量过大：{archive_path.name}")
                if (
                    info.compress_size > 0
                    and info.file_size > 10 * 1024 * 1024
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ArchiveInputError(f"ZIP 压缩比异常：{info.filename!r}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as source, destination.open("xb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
                if destination.stat().st_size != info.file_size:
                    raise ArchiveInputError(f"ZIP 成员解压尺寸不一致：{info.filename!r}")
                extracted.append(destination)
    except Exception:
        shutil.rmtree(target_root, ignore_errors=True)
        raise
    return {
        "archive": archive_path.resolve(),
        "archive_sha256": sha256_file(archive_path),
        "root": target_resolved,
        "files": extracted,
    }


def _material_files(root: Path, suffixes: set[str]) -> list[Path]:
    return sorted(
        (
            path.resolve()
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in suffixes
            and not path.name.startswith("~$")
            and "__MACOSX" not in path.parts
        ),
        key=lambda path: str(path).casefold(),
    )


def _one(paths: list[Path], label: str, archive_name: str) -> Path:
    if len(paths) != 1:
        raise ArchiveInputError(
            f"{archive_name} 中{label}应为1份，实际{len(paths)}份："
            + "、".join(path.name for path in paths)
        )
    return paths[0]


def _unique_image_names(images: list[Path], archive_name: str) -> None:
    lowered = [path.name.casefold() for path in images]
    duplicates = sorted({name for name in lowered if lowered.count(name) > 1})
    if duplicates:
        raise ArchiveInputError(
            f"{archive_name} 中存在同名现场图片，无法稳定绑定：" + "、".join(duplicates)
        )


def prepare_cases(
    input_dir: str | Path,
    temporary_root: str | Path,
) -> dict[str, dict[str, Any]]:
    classified = discover_archives(input_dir)
    temp = Path(temporary_root).resolve()
    temp.mkdir(parents=True, exist_ok=True)
    cases: dict[str, dict[str, Any]] = {}
    for scenario in ("personnel_incentive", "promotional_display"):
        if scenario not in classified:
            continue
        archive = Path(classified[scenario]["path"])
        extracted = _extract_archive(archive, temp / scenario)
        root = Path(extracted["root"])
        excels = _material_files(root, EXCEL_SUFFIXES)
        images = _material_files(root, IMAGE_SUFFIXES)
        _unique_image_names(images, archive.name)
        if scenario == "personnel_incentive":
            sales = _one(excels, "销售 Excel", archive.name)
            settlements = [
                path
                for path in images
                if "结算单" in normalize_text(path.name)
                or "结算表" in normalize_text(path.name)
            ]
            settlement = _one(settlements, "结算单图片", archive.name)
            transfers = [path for path in images if path != settlement]
            if not transfers:
                raise ArchiveInputError(f"{archive.name} 缺少转账/红包截图")
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "sales_excel": sales,
                "settlement_image": settlement,
                "transfer_images": transfers,
            }
        else:
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "sales_excel": _one(excels, "销售 Excel", archive.name),
                "contract_pdf": _one(_material_files(root, {".pdf"}), "合同 PDF", archive.name),
                "photo_files": images,
            }
    return cases
