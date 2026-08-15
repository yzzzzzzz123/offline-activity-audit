from __future__ import annotations

import shutil
import stat
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .common import (
    AuditError,
    clean_identifier,
    normalize_text,
    sha256_file,
    write_json,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = PROJECT_ROOT / "input"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
MAX_ARCHIVE_FILES = 2_000
MAX_MEMBER_BYTES = 250 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_COMPRESSION_RATIO = 200


class ArchiveInputError(AuditError):
    """Raised when the two-ZIP input contract cannot be satisfied safely."""


def _open_zip(path: Path) -> zipfile.ZipFile:
    """Read legacy Chinese ZIP names as CP936 when the UTF-8 flag is absent."""

    try:
        return zipfile.ZipFile(path, metadata_encoding="gbk")
    except (TypeError, UnicodeDecodeError):
        return zipfile.ZipFile(path)


def _is_inside(candidate: Path, parent: Path) -> bool:
    try:
        candidate.relative_to(parent)
    except ValueError:
        return False
    return True


def _zip_member_parts(info: zipfile.ZipInfo) -> tuple[str, ...]:
    raw = info.filename.replace("\\", "/")
    if "\x00" in raw:
        raise ArchiveInputError("ZIP member contains a NUL byte")
    pure = PurePosixPath(raw)
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if pure.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ArchiveInputError(f"Unsafe ZIP member path: {info.filename!r}")
    if ":" in parts[0]:
        raise ArchiveInputError(f"ZIP member contains a drive-qualified path: {info.filename!r}")
    invalid_windows = set('<>:"|?*')
    if any(any(character in invalid_windows for character in part) for part in parts):
        raise ArchiveInputError(f"ZIP member contains an unsupported filename: {info.filename!r}")
    unix_mode = info.external_attr >> 16
    if stat.S_ISLNK(unix_mode):
        raise ArchiveInputError(f"ZIP symbolic links are not accepted: {info.filename!r}")
    if info.flag_bits & 0x1:
        raise ArchiveInputError(f"Encrypted ZIP members are not accepted: {info.filename!r}")
    return parts


def _archive_shape(path: Path) -> dict[str, Any]:
    try:
        with _open_zip(path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ArchiveInputError(
                    f"Archive contains too many members ({len(infos)}): {path.name}"
                )
            suffixes = Counter(
                PurePosixPath(info.filename.replace("\\", "/")).suffix.lower()
                for info in infos
                if not info.is_dir()
            )
            names = " ".join(info.filename for info in infos)
    except zipfile.BadZipFile as exc:
        raise ArchiveInputError(f"Invalid ZIP archive: {path}") from exc
    return {
        "path": path,
        "member_count": len(infos),
        "suffixes": dict(suffixes),
        "normalized_text": normalize_text(f"{path.stem} {names}"),
    }


def _classify_archive(path: Path) -> dict[str, Any]:
    shape = _archive_shape(path)
    text = str(shape["normalized_text"])
    suffixes = dict(shape["suffixes"])
    image_count = sum(int(suffixes.get(suffix, 0)) for suffix in IMAGE_SUFFIXES)
    excel_count = sum(int(suffixes.get(suffix, 0)) for suffix in EXCEL_SUFFIXES)
    pdf_count = int(suffixes.get(".pdf", 0))

    personnel_score = 0
    display_score = 0
    if "人员激励" in text:
        personnel_score += 10
    if "结算单" in text:
        personnel_score += 4
    if "红包" in text or "转账" in text:
        personnel_score += 3
    if excel_count == 1 and pdf_count == 0 and 2 <= image_count <= 10:
        personnel_score += 2

    if "堆头" in text or "陈列" in text:
        display_score += 10
    if "合同" in text:
        display_score += 3
    if pdf_count == 1 and excel_count == 1 and image_count >= 3:
        display_score += 5

    if personnel_score == display_score or max(personnel_score, display_score) < 5:
        raise ArchiveInputError(
            f"Cannot classify archive safely: {path.name}; "
            f"personnel_score={personnel_score}, display_score={display_score}"
        )
    scenario = (
        "personnel_incentive"
        if personnel_score > display_score
        else "promotional_display"
    )
    return {
        **shape,
        "scenario": scenario,
        "personnel_score": personnel_score,
        "display_score": display_score,
        "image_count": image_count,
        "excel_count": excel_count,
        "pdf_count": pdf_count,
    }


def discover_archives(input_dir: str | Path) -> dict[str, dict[str, Any]]:
    requested_root = Path(input_dir)
    if requested_root.is_symlink():
        raise ArchiveInputError("Input directory cannot be a symbolic link")
    root = requested_root.resolve()
    if not root.is_dir():
        raise ArchiveInputError(f"Input directory does not exist: {root}")
    archives = sorted(
        [path for path in root.iterdir() if path.is_file() and path.suffix.lower() == ".zip"],
        key=lambda value: value.name.casefold(),
    )
    if len(archives) != 2:
        raise ArchiveInputError(
            f"Input directory must contain exactly two ZIP files; found {len(archives)} in {root}"
        )
    linked_archives = [path.name for path in archives if path.is_symlink()]
    if linked_archives:
        raise ArchiveInputError(
            "ZIP inputs cannot be symbolic links: " + ", ".join(linked_archives)
        )
    classified: dict[str, dict[str, Any]] = {}
    for archive in archives:
        item = _classify_archive(archive)
        scenario = str(item["scenario"])
        if scenario in classified:
            raise ArchiveInputError(
                f"Both ZIP files were classified as {scenario}; one personnel incentive "
                "archive and one promotional display archive are required"
            )
        classified[scenario] = item
    expected = {"personnel_incentive", "promotional_display"}
    if set(classified) != expected:
        raise ArchiveInputError(
            "The two ZIP files must resolve to one personnel incentive case and one display case"
        )
    return classified


def _extract_archive(archive_path: Path, target_root: Path) -> dict[str, Any]:
    if target_root.exists() or target_root.is_symlink():
        raise ArchiveInputError(f"Extraction target already exists: {target_root}")
    target_root.mkdir(parents=True)
    total_bytes = 0
    extracted_files: list[dict[str, Any]] = []
    casefold_targets: set[str] = set()
    try:
        with _open_zip(archive_path) as archive:
            infos = archive.infolist()
            if len(infos) > MAX_ARCHIVE_FILES:
                raise ArchiveInputError(
                    f"Archive contains too many members ({len(infos)}): {archive_path.name}"
                )
            for info in infos:
                parts = _zip_member_parts(info)
                destination = (target_root.joinpath(*parts)).resolve()
                if not _is_inside(destination, target_root.resolve()):
                    raise ArchiveInputError(f"ZIP member escaped extraction root: {info.filename!r}")
                target_key = str(destination).casefold()
                if target_key in casefold_targets:
                    raise ArchiveInputError(
                        f"ZIP contains a duplicate or case-colliding path: {info.filename!r}"
                    )
                casefold_targets.add(target_key)
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ArchiveInputError(
                        f"ZIP member is too large ({info.file_size} bytes): {info.filename!r}"
                    )
                total_bytes += info.file_size
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ArchiveInputError(
                        f"Archive expands beyond {MAX_TOTAL_BYTES} bytes: {archive_path.name}"
                    )
                if (
                    info.compress_size > 0
                    and info.file_size > 10 * 1024 * 1024
                    and info.file_size / info.compress_size > MAX_COMPRESSION_RATIO
                ):
                    raise ArchiveInputError(
                        f"Suspicious compression ratio for ZIP member: {info.filename!r}"
                    )
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info, "r") as source, destination.open("xb") as sink:
                    shutil.copyfileobj(source, sink, length=1024 * 1024)
                if destination.stat().st_size != info.file_size:
                    raise ArchiveInputError(
                        f"Extracted size differs from ZIP metadata: {info.filename!r}"
                    )
                extracted_files.append(
                    {
                        "path": destination.relative_to(target_root).as_posix(),
                        "bytes": destination.stat().st_size,
                        "sha256": sha256_file(destination),
                    }
                )
    except Exception:
        if target_root.is_dir():
            shutil.rmtree(target_root)
        raise
    return {
        "archive": str(archive_path),
        "archive_bytes": archive_path.stat().st_size,
        "archive_sha256": sha256_file(archive_path),
        "extraction_root": str(target_root),
        "file_count": len(extracted_files),
        "expanded_bytes": total_bytes,
        "files": extracted_files,
    }


def _files_with_suffixes(root: Path, suffixes: set[str]) -> list[Path]:
    return sorted(
        [
            path
            for path in root.rglob("*")
            if path.is_file()
            and path.suffix.lower() in suffixes
            and not path.name.startswith("~$")
            and "__MACOSX" not in path.parts
        ],
        key=lambda value: str(value).casefold(),
    )


def _require_one(paths: list[Path], label: str, archive_name: str) -> Path:
    if len(paths) != 1:
        rendered = [path.name for path in paths]
        raise ArchiveInputError(
            f"Expected exactly one {label} in {archive_name}; found {len(paths)}: {rendered}"
        )
    return paths[0]


def _relative(path: Path, root: Path) -> str:
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ArchiveInputError(f"Prepared source escaped its case root: {path}") from exc


def _personnel_case(
    *,
    archive: Path,
    source_root: Path,
    case_dir: Path,
) -> dict[str, Any]:
    excel = _require_one(
        _files_with_suffixes(source_root, EXCEL_SUFFIXES),
        "sales Excel workbook",
        archive.name,
    )
    images = _files_with_suffixes(source_root, IMAGE_SUFFIXES)
    if len(images) < 2:
        raise ArchiveInputError(
            f"Personnel incentive archive requires a settlement image and transfer images: {archive.name}"
        )
    settlement_candidates = [
        image
        for image in images
        if "结算" in normalize_text(image.stem) or "settlement" in normalize_text(image.stem)
    ]
    settlement = _require_one(
        settlement_candidates,
        "final settlement image",
        archive.name,
    )
    transfers = [image for image in images if image != settlement]
    if not transfers:
        raise ArchiveInputError(f"No transfer evidence images found in {archive.name}")
    case_id = clean_identifier(f"{archive.stem}-personnel-incentive")
    case = {
        "schema_version": "1.0",
        "case_id": case_id,
        "case_name": archive.stem,
        "scenario": "personnel_incentive",
        "case_root": str(source_root.resolve()),
        "files": {
            "sales_excel": _relative(excel, source_root),
            "settlement_image": _relative(settlement, source_root),
            "transfer_images": [_relative(path, source_root) for path in transfers],
        },
        "rules": {
            "expect_all_sales_skus_matched": True,
            "transfer_evidence_required": True,
        },
    }
    case_path = case_dir / "personnel-incentive-case.json"
    write_json(case_path, case)
    return {
        "scenario": "personnel_incentive",
        "case_id": case_id,
        "case_path": str(case_path),
        "source_files": {
            "sales_excel": str(excel),
            "settlement_image": str(settlement),
            "transfer_images": [str(path) for path in transfers],
        },
    }


def _flatten_photos(images: list[Path], destination: Path) -> Path:
    destination.mkdir(parents=True)
    used_names: set[str] = set()
    for index, source in enumerate(images, start=1):
        name = source.name
        key = name.casefold()
        if key in used_names:
            name = f"{index:03d}-{name}"
            key = name.casefold()
        if key in used_names:
            raise ArchiveInputError(f"Could not assign a unique photo filename for {source}")
        used_names.add(key)
        shutil.copy2(source, destination / name)
    return destination


def _display_photo_dir(source_root: Path, images: list[Path]) -> Path:
    parents = {image.parent.resolve() for image in images}
    if len(parents) == 1:
        return next(iter(parents))
    return _flatten_photos(images, source_root / "__normalized_photos__")


def _display_case(
    *,
    archive: Path,
    source_root: Path,
    case_dir: Path,
) -> dict[str, Any]:
    excel = _require_one(
        _files_with_suffixes(source_root, EXCEL_SUFFIXES),
        "sales Excel workbook",
        archive.name,
    )
    contract = _require_one(
        _files_with_suffixes(source_root, {".pdf"}),
        "contract PDF",
        archive.name,
    )
    images = _files_with_suffixes(source_root, IMAGE_SUFFIXES)
    if not images:
        raise ArchiveInputError(f"No display photos found in {archive.name}")
    photo_dir = _display_photo_dir(source_root, images)
    case_id = clean_identifier(f"{archive.stem}-promotional-display")
    case = {
        "schema_version": "1.0",
        "case_id": case_id,
        "case_name": archive.stem,
        "scenario": "promotional_display",
        "case_root": str(source_root.resolve()),
        "files": {
            "contract_pdf": _relative(contract, source_root),
            "sales_excel": _relative(excel, source_root),
            "photo_dir": _relative(photo_dir, source_root),
        },
    }
    case_path = case_dir / "promotional-display-case.json"
    write_json(case_path, case)
    return {
        "scenario": "promotional_display",
        "case_id": case_id,
        "case_path": str(case_path),
        "source_files": {
            "contract_pdf": str(contract),
            "sales_excel": str(excel),
            "photo_dir": str(photo_dir),
            "photo_count": len(images),
        },
    }


def prepare_archive_batch(
    *,
    input_dir: str | Path,
    run_id: str,
) -> dict[str, Any]:
    requested_root = Path(input_dir)
    if requested_root.is_symlink():
        raise ArchiveInputError("Input directory cannot be a symbolic link")
    root = requested_root.resolve()
    classified = discover_archives(root)
    normalized_run_id = clean_identifier(run_id)
    if normalized_run_id != run_id.strip():
        raise ArchiveInputError("run-id is unsafe for prepared input allocation")
    prepared_base = (root / ".prepared").resolve()
    prepared_root = (prepared_base / normalized_run_id).resolve()
    if prepared_root.parent != prepared_base:
        raise ArchiveInputError("Prepared input path escaped the input directory")
    if prepared_root.exists() or prepared_root.is_symlink():
        raise ArchiveInputError(
            f"Prepared input already exists for run-id {run_id}: {prepared_root}"
        )
    sources_root = prepared_root / "sources"
    cases_root = prepared_root / "cases"
    cases_root.mkdir(parents=True)

    extractions: dict[str, Any] = {}
    case_records: dict[str, dict[str, Any]] = {}
    try:
        for scenario in ("personnel_incentive", "promotional_display"):
            archive_info = classified[scenario]
            archive_path = Path(str(archive_info["path"])).resolve()
            source_root = sources_root / scenario
            extractions[scenario] = _extract_archive(archive_path, source_root)
            if scenario == "personnel_incentive":
                case_records[scenario] = _personnel_case(
                    archive=archive_path,
                    source_root=source_root,
                    case_dir=cases_root,
                )
            else:
                case_records[scenario] = _display_case(
                    archive=archive_path,
                    source_root=source_root,
                    case_dir=cases_root,
                )

        batch = {
            "schema_version": "1.0",
            "batch_id": clean_identifier(f"offline-audit-{run_id}"),
            "excel_name": f"{run_id}-线下活动核销结果.xlsx",
            "cases": [
                {"case": case_records["personnel_incentive"]["case_path"]},
                {"case": case_records["promotional_display"]["case_path"]},
            ],
        }
        batch_path = prepared_root / "batch.json"
        write_json(batch_path, batch)
        manifest = {
            "schema_version": "1.0",
            "run_id": run_id,
            "input_dir": str(root),
            "prepared_root": str(prepared_root),
            "batch_path": str(batch_path),
            "routing": {
                scenario: {
                    "archive": str(classified[scenario]["path"]),
                    "archive_sha256": extractions[scenario]["archive_sha256"],
                    "scenario": scenario,
                    "child_skill": (
                        "audit-personnel-incentive"
                        if scenario == "personnel_incentive"
                        else "audit-promotional-display"
                    ),
                    "case_path": case_records[scenario]["case_path"],
                }
                for scenario in ("personnel_incentive", "promotional_display")
            },
            "extractions": extractions,
        }
        manifest_path = prepared_root / "input-manifest.json"
        write_json(manifest_path, manifest)
        return {
            "run_id": run_id,
            "input_dir": str(root),
            "prepared_root": str(prepared_root),
            "batch_path": str(batch_path),
            "manifest_path": str(manifest_path),
            "routing": manifest["routing"],
        }
    except Exception:
        if prepared_root.is_dir():
            shutil.rmtree(prepared_root)
        raise
