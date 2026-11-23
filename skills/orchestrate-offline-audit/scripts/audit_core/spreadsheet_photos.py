"""Read every workbook picture and retain its original sheet/cell placement."""
from __future__ import annotations

import io
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
import posixpath
import re
import shutil
import struct
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from zipfile import ZipFile, is_zipfile

from PIL import Image, UnidentifiedImageError
import xlrd.compdoc

from .legacy_activity_workbook import _convert_legacy_workbook, _is_ole_workbook
from .reading_contract import ReadingSecurityError, normalize_visual_orientation


DISPIMG = re.compile(r'^=(?:_xlfn\.)?DISPIMG\(\s*"([^"\r\n]+)"\s*[,;]', re.I)
PICTURE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _legacy_has_pictures(path: Path) -> bool:
    """Inspect BIFF drawing records without opening Excel or executing macros."""
    compound = xlrd.compdoc.CompDoc(path.read_bytes(), logfile=io.StringIO())
    stream = compound.get_named_stream("Workbook") or compound.get_named_stream("Book") or b""
    offset = 0
    while offset + 4 <= len(stream):
        record, size = struct.unpack_from("<HH", stream, offset)
        if record in {0x00EB, 0x00EC, 0x007F}:  # MSODRAWINGGROUP, MSODRAWING, IMDATA
            return True
        offset += 4 + size
    return False


def spreadsheet_source(path: Path, root: Path, file_id: str) -> tuple[Path, bool]:
    if path.suffix.lower() != ".xls":
        return path, False
    # WPS stores its cell-image ZIP inside OLE's ETCellImageData stream. ZIP
    # detection alone also recognizes that embedded central directory.
    zipped = not _is_ole_workbook(path) and is_zipfile(path)
    pictures = False if zipped else _legacy_has_pictures(path)
    if not zipped and not pictures:
        return path, False
    target = root / f"{file_id}-readable.xlsx"
    if zipped:
        # WPS often uses an .xls suffix for an OOXML package. A byte-for-byte
        # copy avoids Excel rewriting/removing its DISPIMG relationships.
        shutil.copyfile(path, target)
    else:
        _convert_legacy_workbook(path, target)
    return target, pictures


def validate_spreadsheet_archive(source) -> None:
    """Keep unsafe spreadsheet containers as package-level security failures."""
    from .archive_input import ArchiveInputError
    from .material_diagnostic_output import _validate_spreadsheet_archive

    try:
        _validate_spreadsheet_archive(source)
    except ArchiveInputError as exc:
        raise ReadingSecurityError(str(exc)) from exc


def _tag(element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def _part_elements(archive: ZipFile, part: str, tags: set[str]):
    """Stream selected XML subtrees without materializing a whole worksheet.

    Preserve children inside an active image/anchor, clear unrelated cells and
    detach completed elements so formatting-only rows cannot accumulate.
    """
    with archive.open(part) as source:
        stack, captured = [], None
        for event, element in ET.iterparse(source, events=('start', 'end')):
            if event == 'start':
                stack.append(element)
                if captured is None and _tag(element) in tags:
                    captured = element
                continue
            if element is captured:
                yield element
                captured = None
            if captured is None:
                element.clear()
                if len(stack) > 1:
                    stack[-2].remove(element)
            stack.pop()


def _relationships(archive: ZipFile, part: str, kind: str) -> dict:
    parent = PurePosixPath(part)
    name = str(parent.parent / "_rels" / (parent.name + ".rels"))
    if name not in archive.namelist():
        return {}
    result = {}
    seen = set()
    for element in _part_elements(archive, name, {'Relationship'}):
        rid = element.get("Id")
        if not rid:
            continue
        if rid in seen:
            raise ReadingSecurityError("电子表格图片关系编号重复，无法确定来源")
        seen.add(rid)
        # Only follow the relationship used at this level. For example,
        # workbook customXml may legitimately resolve outside xl/.
        relation_type = element.get("Type")
        if relation_type and relation_type != f"{REL_NS}/{kind}":
            continue
        target = unquote(element.get("Target", ""))
        if element.get("TargetMode", "").lower() == "external":
            result[rid] = None
            continue
        resolved = posixpath.normpath(posixpath.join(str(parent.parent), target)) if not target.startswith("/") else target.lstrip("/")
        if not resolved.startswith("xl/") or "\\" in resolved or ":" in resolved or ".." in PurePosixPath(resolved).parts:
            raise ReadingSecurityError("电子表格图片关系路径不安全")
        result[rid] = resolved
    return result


@contextmanager
def _photo_archive(path: Path):
    if not _is_ole_workbook(path):
        with ZipFile(path) as archive:
            yield archive
        return
    compound = xlrd.compdoc.CompDoc(path.read_bytes(), logfile=io.StringIO())
    payload = compound.get_named_stream("ETCellImageData")
    source = io.BytesIO(payload or b"")
    if payload:
        # Apply the same member, expansion and path limits as ordinary XLSX.
        validate_spreadsheet_archive(source)
    else:
        # Keep unresolved DISPIMG placements explicit even if the stream is
        # absent, rather than treating a formula string as a photo.
        with ZipFile(source, "w"):
            pass
    source.seek(0)
    with ZipFile(source) as archive:
        yield archive


def workbook_photo_units(path: Path, item: dict, root: Path, cells: list[dict]) -> list[dict]:
    """Emit one source per placement, including unresolved references and orphans."""
    units = []
    referenced = set()
    with _photo_archive(path) as archive:
        names = set(archive.namelist())

        def emit(media: str | None, locator: str, issue: str | None = None):
            uid = f"{item['file_id']}-photo-{len(units) + 1:04d}"
            target = None
            visual_normalization = None
            limitations = [issue] if issue else []
            if media:
                referenced.add(media)
            if media not in names:
                limitations.append("该位置引用的图片未随表格提供，无法读取照片内容")
            elif Path(media).suffix.lower() not in PICTURE_SUFFIXES:
                limitations.append("该位置的图片格式暂无法读取，不能根据表格文字代替照片分析")
            else:
                target = root / (uid + Path(media).suffix.lower())
                target.write_bytes(archive.read(media))
                try:
                    with Image.open(target) as picture:
                        picture.verify()
                    visual_normalization = normalize_visual_orientation(target)
                except (UnidentifiedImageError, OSError, ValueError):
                    target = None
                    limitations.append("该位置的图片内容损坏或不可读，无法分析照片内容")
            units.append({"unit_id": uid, "file_id": item["file_id"],
                          "source_file": item["source_file"], "locator": locator,
                          "image": target, "photo_reference": True,
                          "visual_normalization": visual_normalization,
                          "native_facts": ["表格图片来源位置：" + locator], "limitations": limitations})

        cell_images = {}
        cell_part = next((name for name in names if name.casefold() == "xl/cellimages.xml"), None)
        if cell_part:
            relationships = _relationships(archive, cell_part, "image")
            for element in _part_elements(archive, cell_part, {'cellImage'}):
                name = next((e.get("name") for e in element.iter() if _tag(e) == "cNvPr"), None)
                rid = next((e.get(f"{{{REL_NS}}}embed") for e in element.iter() if _tag(e) == "blip"), None)
                if name:
                    key = name.casefold()
                    if key in cell_images:
                        raise ReadingSecurityError("电子表格单元格图片ID重复，无法确定来源")
                    cell_images[key] = relationships.get(rid)
        for cell in cells:
            locator = f"{cell['sheet']}!R{cell['row']}C{cell['column']}（单元格图片）"
            emit(cell_images.get(cell["image_id"].casefold()), locator)

        workbook_rels = _relationships(archive, "xl/workbook.xml", "worksheet")
        if "xl/workbook.xml" in names:
            for sheet in ET.fromstring(archive.read("xl/workbook.xml")).iter():
                if _tag(sheet) != "sheet":
                    continue
                part = workbook_rels.get(sheet.get(f"{{{REL_NS}}}id"))
                if part not in names:
                    continue
                sheet_rels = _relationships(archive, part, "drawing")
                for drawing in _part_elements(archive, part, {'drawing'}):
                    if _tag(drawing) != "drawing":
                        continue
                    drawing_part = sheet_rels.get(drawing.get(f"{{{REL_NS}}}id"))
                    if drawing_part not in names:
                        emit(None, f"{sheet.get('name')}（图片位置无法读取）")
                        continue
                    drawing_rels = _relationships(archive, drawing_part, "image")
                    for index, anchor in enumerate(_part_elements(archive, drawing_part,
                            {'oneCellAnchor', 'twoCellAnchor', 'absoluteAnchor'}), 1):
                        origin = next((e for e in anchor if _tag(e) == "from"), None)
                        position = { _tag(e): e.text for e in origin } if origin is not None else {}
                        row, col = position.get("row"), position.get("col")
                        address = f"R{int(row)+1}C{int(col)+1}" if row is not None and col is not None else "位置未绑定行列"
                        for blip in anchor.iter():
                            if _tag(blip) == "blip":
                                rid = blip.get(f"{{{REL_NS}}}embed") or blip.get(f"{{{REL_NS}}}link")
                                emit(drawing_rels.get(rid), f"{sheet.get('name')}!{address}（浮动图片{index}）")
        # Never drop a media part just because its anchor is absent/broken.
        for media in sorted(names):
            if media.startswith("xl/media/") and not media.endswith("/") and media not in referenced:
                emit(media, media, "图片未能对应到工作表行列，仍须逐张分析实际画面")
    return units
