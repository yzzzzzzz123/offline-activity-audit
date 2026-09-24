from __future__ import annotations

import shutil
import subprocess
from struct import error as StructError
import warnings
import zipfile
import zlib
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader
from pypdf.errors import PyPdfError

from .archive_input import (
    EXCEL_SUFFIXES,
    IMAGE_SUFFIXES,
    MAX_ARCHIVE_FILES,
    MAX_MEMBER_BYTES,
    MAX_TOTAL_BYTES,
    SCENARIO_MARKERS,
    VISUAL_DOCUMENT_SUFFIXES,
    ArchiveInputError,
    _archive_member_parts,
    _decode_archive_listing,
    _extract_archive,
    _extract_rar_archive,
    _open_zip,
    _zip_member_parts,
    discover_zip_paths,
    scenario_from_archive_name,
    scenarios_from_archive_name,
)
from .common import sha256_file
from .legacy_activity_workbook import LegacyWorkbookMaterialError, extract_activity_return_workbook


MAX_NESTED_ARCHIVE_DEPTH = 4
MAX_DIAGNOSTIC_FILES = MAX_ARCHIVE_FILES
MAX_DIAGNOSTIC_BYTES = MAX_TOTAL_BYTES
MAX_VISUAL_PAGES = 500
def _scenario_hint(name: str) -> str | None:
    """Compatibility field: its declared route now comes only from the ZIP name."""
    return scenario_from_archive_name(name)


class _ExtractionBudget:
    def __init__(self) -> None:
        self.members = 0
        self.bytes = 0

    def reserve(self, members: int, byte_count: int) -> None:
        if self.members + members > MAX_DIAGNOSTIC_FILES:
            raise ArchiveInputError("材料诊断的归档总成员数超限")
        if self.bytes + byte_count > MAX_DIAGNOSTIC_BYTES:
            raise ArchiveInputError("材料诊断的归档解压总量超限")
        self.members += members
        self.bytes += byte_count


def _preflight_zip(path: Path, budget: _ExtractionBudget) -> None:
    try:
        with _open_zip(path) as archive:
            infos = archive.infolist()
            total = 0
            for info in infos:
                _zip_member_parts(info)
                if info.is_dir():
                    continue
                if info.file_size > MAX_MEMBER_BYTES:
                    raise ArchiveInputError(f"ZIP 成员过大：{info.filename!r}")
                total += info.file_size
            budget.reserve(len(infos), total)
    except (zipfile.BadZipFile, EOFError) as exc:
        raise ArchiveInputError(f"不是有效 ZIP：{path.name}") from exc


def _preflight_rar(path: Path, budget: _ExtractionBudget) -> bool:
    """Account for bsdtar's declared sizes before extracting a nested RAR."""
    tar = shutil.which("tar")
    if tar is None:
        raise ArchiveInputError(f"无法读取现场照片 RAR：系统未提供 bsdtar/tar（{path.name}）")
    result = subprocess.run(
        [tar, "-tvf", str(path)], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False,
    )
    if result.returncode != 0:
        raise ArchiveInputError(f"RAR 无法读取：{path.name}")
    lines = [line for line in _decode_archive_listing(result.stdout).splitlines() if line.strip()]
    total = 0
    for line in lines:
        fields = line.split(None, 8)
        if not fields or fields[0][0] not in {"-", "d"}:
            raise ArchiveInputError(f"RAR 不接受链接或特殊成员：{path.name}")
        # bsdtar: mode links owner group bytes month day year/time name.
        if len(fields) != 9 or not fields[1].isdigit() or not fields[4].isdigit():
            raise ArchiveInputError(f"RAR 成员尺寸无法安全校验：{path.name}")
        _archive_member_parts(fields[8].rstrip("/"), archive_label="RAR")
        size = int(fields[4])
        if size > MAX_MEMBER_BYTES:
            raise ArchiveInputError(f"RAR 成员过大：{path.name}")
        total += size
    budget.reserve(len(lines), total)
    return bool(lines)


def _is_content_decode_error(error: Exception) -> bool:
    """Only known format/decoder failures describe customer material.

    Missing files, permissions, OS errors, exhausted resources, dependencies,
    and unexpected implementation failures must abort the execution instead.
    Pillow sometimes uses an errno-less OSError for malformed image streams.
    """
    if isinstance(error, (PermissionError, FileNotFoundError)):
        return False
    if isinstance(error, OSError):
        return (
            (type(error) is OSError or isinstance(error, UnidentifiedImageError))
            and error.errno is None and getattr(error, "winerror", None) is None
        )
    return isinstance(error, (
        ValueError, EOFError, SyntaxError, StructError, zlib.error, PyPdfError,
        Image.DecompressionBombError, Image.DecompressionBombWarning,
    ))


def _visual_limitations(path: Path) -> list[str]:
    if path.suffix.lower() in IMAGE_SUFFIXES:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as picture:
                    picture.verify()
        except Exception as exc:
            if not _is_content_decode_error(exc):
                raise
            return ["图片无法安全读取，无法识别其中的材料内容"]
    else:
        try:
            reader = PdfReader(path, strict=True)
            if reader.is_encrypted:
                return ["PDF 已加密，无法读取其中的材料内容"]
            pages = len(reader.pages)
            if pages == 0:
                return ["PDF 没有可读取页面"]
            if pages > MAX_VISUAL_PAGES:
                return [f"PDF 超过单份材料可读取的 {MAX_VISUAL_PAGES} 页上限"]
        except Exception as exc:
            if not _is_content_decode_error(exc):
                raise
            return ["PDF 无法读取，无法识别其中的材料内容"]
    return []


def _is_activity_return_workbook(path: Path) -> bool:
    """Only known legacy activity-table structure may invoke the XLS adapter.

    This deliberately does not infer a workbook role from its filename. Generic
    sales spreadsheets and unrecognized workbooks remain deterministic inputs.
    """
    with path.open("rb") as source:
        signature = source.read(8)
        if signature != bytes.fromhex("d0cf11e0a1b11ae1"):
            return False
        content = signature + source.read()
    required = (
        "活动周期", "陈列图片", "客户编码", "客户名称",
        "费用代垫经销商", "费用代垫经销商编码", "DISPIMG",
    )
    return all(
        token.encode("utf-16le") in content or token.encode("utf-8") in content
        for token in required
    )


def prepare_classification_rejection(
    source: Path, temporary_root: Path, *, archive_id: str,
) -> dict[str, Any]:
    """Validate archive safety, without reading business content or calling AI.

    Unknown names never enter material diagnosis. Nested containers still use
    the same safety limits as known submissions, so unsafe uploads cannot be
    mislabeled as a correctable naming problem.
    """
    matches = scenarios_from_archive_name(source.name)
    if len(matches) == 1:
        raise ArchiveInputError("核销方式已确定，不能生成分类打回结果")
    if source.is_symlink():
        raise ArchiveInputError("ZIP 不能是符号链接")
    if temporary_root.exists() or temporary_root.is_symlink():
        raise ArchiveInputError("分类校验临时目录已存在")
    temporary_root.mkdir(parents=True)
    budget = _ExtractionBudget()
    counter = 0

    def visit(container: Path, depth: int) -> None:
        nonlocal counter
        if depth > MAX_NESTED_ARCHIVE_DEPTH:
            raise ArchiveInputError("材料诊断的嵌套归档层数超限")
        target = temporary_root / f"container-{counter:04d}"
        counter += 1
        if container.suffix.lower() == ".zip":
            _preflight_zip(container, budget)
            extracted = _extract_archive(container, target)
        else:
            if not _preflight_rar(container, budget):
                return
            extracted = _extract_rar_archive(container, target)
        for path in extracted["files"]:
            if path.suffix.lower() in {".zip", ".rar"}:
                visit(path, depth + 1)

    try:
        visit(source, 0)
        return {
            "archive_id": archive_id,
            "source_archive": source.name,
            "archive_sha256": sha256_file(source),
            "scenario": None,
            "reason_code": "ambiguous_scenario_markers" if matches else "missing_scenario_marker",
            "matched_scenarios": list(matches),
        }
    finally:
        shutil.rmtree(temporary_root, ignore_errors=True)


def prepare_material_diagnosis(
    input_dir: str | Path,
    temporary_root: str | Path,
    *,
    original_error: str | Exception,
    selected_scenarios: set[str] | None = None,
    classify_by_content: bool = False,
) -> dict[str, Any]:
    """Inventory safely submitted materials without binding a principal file.

    All archives finish safety checks before this function returns. Consumers
    must only expose readable ``visual`` entries to the model; spreadsheet
    bodies and original extraction directories stay outside its sandbox.
    """
    if selected_scenarios is not None:
        unknown = set(selected_scenarios) - set(SCENARIO_MARKERS)
        if unknown:
            raise ArchiveInputError("不支持的核销场景：" + "、".join(sorted(unknown)))
    sources = discover_zip_paths(input_dir)
    requested_temp = Path(temporary_root)
    if requested_temp.is_symlink():
        raise ArchiveInputError("材料诊断临时目录不能是符号链接")
    temp = requested_temp.resolve()
    temp.mkdir(parents=True, exist_ok=True)
    destination = temp / "material_diagnostic"
    if destination.exists() or destination.is_symlink():
        raise ArchiveInputError("材料诊断临时解压目录已存在")
    destination.mkdir()
    budget = _ExtractionBudget()
    archives: list[dict[str, Any]] = []
    try:
        for archive_index, source in enumerate(sources, 1):
            archive_id = f"a{archive_index:03d}"
            inventory: list[dict[str, Any]] = []
            entry: dict[str, Any] = {
                "archive_id": archive_id,
                "source_archive": source.resolve(),
                "archive_sha256": sha256_file(source),
                "scenario_hint": None if classify_by_content else _scenario_hint(source.name),
                "inventory": inventory,
            }
            archives.append(entry)

            def add_file(path: Path, source_file: str, parent_source: str | None) -> dict[str, Any]:
                suffix = path.suffix.lower()
                kind = (
                    "visual" if suffix in VISUAL_DOCUMENT_SUFFIXES
                    else "spreadsheet" if suffix in EXCEL_SUFFIXES | {".xls"}
                    else "container" if suffix in {".zip", ".rar"}
                    else "unsupported"
                )
                item: dict[str, Any] = {
                    "file_id": f"{archive_id}-f{len(inventory) + 1:04d}",
                    "source_file": source_file,
                    "path": path.resolve(),
                    "sha256": sha256_file(path),
                    "suffix": suffix,
                    "kind": kind,
                    "parent_source": parent_source,
                    "limitations": [],
                }
                inventory.append(item)
                if kind == "unsupported":
                    item["limitations"].append("此文件格式暂不支持材料内容识别")
                return item

            def visit(container: Path, depth: int, prefix: str, parent: str | None) -> None:
                if depth > MAX_NESTED_ARCHIVE_DEPTH:
                    raise ArchiveInputError("材料诊断的嵌套归档层数超限")
                extraction_root = destination / archive_id / f"container-{visit.counter:04d}"
                visit.counter += 1
                if container.suffix.lower() == ".zip":
                    _preflight_zip(container, budget)
                    extracted = _extract_archive(container, extraction_root)
                else:
                    if not _preflight_rar(container, budget):
                        return
                    extracted = _extract_rar_archive(container, extraction_root)
                extracted_root = Path(extracted["root"])
                # Keep every submitted file, including unsupported files. Ignore
                # no evidence merely because its name resembles OS metadata.
                for path in sorted(extracted["files"], key=lambda value: str(value).casefold()):
                    relative = Path(path).relative_to(extracted_root).as_posix()
                    source_file = f"{prefix}/{relative}" if prefix else relative
                    item = add_file(Path(path), source_file, parent)
                    if item["kind"] == "container":
                        visit(Path(path), depth + 1, source_file, source_file)

            visit.counter = 1
            visit(source, 0, "", None)

        # Defer any document parsing/conversion until every nested archive has
        # passed extraction and the aggregate safety budget.
        for entry in archives:
            inventory = entry["inventory"]
            for item in list(inventory):
                path = Path(item["path"])
                if item["kind"] == "visual":
                    item["limitations"].extend(_visual_limitations(path))
                elif item["suffix"] == ".xls":
                    if not _is_activity_return_workbook(path):
                        if not classify_by_content:
                            item["limitations"].append("未确认此旧式工作簿为活动返图结构；未将表格内容交给 AI")
                        continue
                    workbook_root = destination / entry["archive_id"] / f"workbook-{item['file_id']}"
                    try:
                        activity = extract_activity_return_workbook(path, workbook_root)
                    except LegacyWorkbookMaterialError as exc:
                        item["limitations"].append(str(exc) + "；本工作簿内照片无法确认完整绑定，未将表格内容交给 AI")
                        if workbook_root.exists():
                            shutil.rmtree(workbook_root)
                        continue
                    for record in activity["records"]:
                        photo = Path(record["photo_file"])
                        budget.reserve(1, photo.stat().st_size)
                        source_file = f"{item['source_file']}/{photo.name}"
                        inventory.append({
                            "file_id": f"{entry['archive_id']}-f{len(inventory) + 1:04d}",
                            "source_file": source_file,
                            "path": photo.resolve(),
                            "sha256": sha256_file(photo),
                            "suffix": photo.suffix.lower(),
                            "kind": "visual",
                            "parent_source": item["source_file"],
                            "limitations": _visual_limitations(photo),
                        })
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return {
        "kind": "material_diagnostic",
        "source_archives": [source.name for source in sources],
        "original_intake_error": str(original_error),
        "archives": archives,
    }
