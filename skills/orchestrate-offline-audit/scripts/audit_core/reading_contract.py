"""Stable, cross-platform contract for selecting and reporting material readers.

The business audit is deliberately not decided here. This module only keeps
format detection, failure ownership and per-file receipts independent from any
Windows-only API. A missing converter or an OS permission problem is a system
failure, never a customer "missing material" finding.
"""
from __future__ import annotations

from copy import deepcopy
import errno
import io
import mmap
import os
from pathlib import Path
import shutil
import subprocess
from zipfile import BadZipFile, ZipFile

from .common import AuditError


READ_OK = "read_ok"
PARTIALLY_READ = "partially_read"
CUSTOMER_UNREADABLE = "customer_unreadable"
PROTECTED = "protected"
UNSUPPORTED = "unsupported"
SYSTEM_FAILURE = "system_failure"
INTERNAL_BUG = "internal_bug"
SECURITY_REJECTED = "security_rejected"

FAILURE_STATUSES = {
    CUSTOMER_UNREADABLE, PROTECTED, UNSUPPORTED, SYSTEM_FAILURE,
    INTERNAL_BUG, SECURITY_REJECTED,
}

_FORMAT_SUFFIXES = {
    "pdf": ".pdf", "jpeg": ".jpg", "png": ".png", "webp": ".webp",
    "gif": ".gif", "bmp": ".bmp", "tiff": ".tiff",
}
_IMAGE_FORMATS = {"jpeg", "png", "webp", "gif", "bmp", "tiff"}
IMAGE_FORMATS = frozenset(_IMAGE_FORMATS)
_WORD_FORMATS = {"docx", "doc", "rtf"}
_SPREADSHEET_FORMATS = {"xlsx", "xls"}


class ReadingSystemError(AuditError):
    """The environment or an execution resource prevented a complete read."""

    def __init__(self, message: str, *, code: str = "system_failure",
                 retryable: bool = True):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ReadingSecurityError(AuditError):
    """A path, archive member or external relationship violated safety rules."""

    def __init__(self, message: str):
        super().__init__(message)
        self.code = "security_rejected"
        self.retryable = False


def _zip_kind(path: Path) -> str | None:
    try:
        with ZipFile(path) as archive:
            names = {name.casefold() for name in archive.namelist()}
    except (BadZipFile, OSError, ValueError):
        return None
    if "word/document.xml" in names:
        return "docx"
    if "xl/workbook.xml" in names:
        return "xlsx"
    return "zip"


def _ole_kind(path: Path) -> str:
    """Distinguish a Word document from an Excel workbook inside an OLE file.

    A declared suffix is not enough here: real submissions sometimes carry a
    Word document as ``.docx`` or an old BIFF workbook as ``.xlsx``.  The OLE
    directory names are metadata, so this is still format detection and does
    not infer which business role the file has.
    """
    try:
        from xlrd.compdoc import CompDoc

        with path.open("rb") as stream, mmap.mmap(
                stream.fileno(), 0, access=mmap.ACCESS_READ) as data:
            compound = CompDoc(data, logfile=io.StringIO())
            names = {
                str(node.name).casefold()
                for node in compound.dirlist
                if getattr(node, "name", "")
            }
    except MemoryError:
        raise
    except Exception:
        # A malformed or encrypted OLE container is not a reason to abort the
        # whole submission during format detection.  xlrd.compdoc raises its
        # own CompDocError (not a subclass of ValueError), so keep this narrow
        # detector tolerant while still allowing MemoryError to propagate.
        return "cfb"
    if "worddocument" in names:
        return "doc"
    if "workbook" in names or "book" in names:
        return "xls"
    # Encrypted OOXML is an OLE container too, but it cannot be decoded as a
    # normal Word/Excel file and must remain an explicit unsupported/protected
    # source rather than being guessed.
    return "cfb"


def sniff_format(path: Path, declared_suffix: str | None = None) -> str:
    """Identify content by signature, using the suffix only to disambiguate OLE.

    This is format detection, not a material-role guess. It deliberately does
    not classify any file as a photo, contract or settlement.
    """
    path = Path(path)
    suffix = (declared_suffix or path.suffix).lower()
    try:
        with path.open("rb") as stream:
            head = stream.read(64)
    except OSError:
        raise
    if head.startswith(b"%PDF-"):
        return "pdf"
    if head.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if head.startswith(b"GIF87a") or head.startswith(b"GIF89a"):
        return "gif"
    if head.startswith(b"BM"):
        return "bmp"
    if head.startswith(b"II*\x00") or head.startswith(b"MM\x00*"):
        return "tiff"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "webp"
    if head.startswith(b"PK\x03\x04") or head.startswith(b"PK\x05\x06") or head.startswith(b"PK\x07\x08"):
        return _zip_kind(path) or "zip"
    if head.startswith(bytes.fromhex("d0cf11e0a1b11ae1")):
        ole_kind = _ole_kind(path)
        if ole_kind != "cfb":
            return ole_kind
        if suffix == ".doc":
            return "doc"
        if suffix in {".xls", ".xlsx", ".xlsm"}:
            return "xls"
        return "cfb"
    if head.lstrip().startswith(b"{\\rtf"):
        return "rtf"
    if head.startswith(b"Rar!\x1a\x07"):
        return "rar"
    return "unknown"


def reader_for_format(fmt: str, declared_suffix: str) -> dict[str, str | bool]:
    """Return the adapter and a content-derived suffix for a sniffed format."""
    suffix = declared_suffix.lower()
    if fmt == "pdf":
        kind, adapter, reading_suffix = "visual", "pdf", ".pdf"
    elif fmt in _IMAGE_FORMATS:
        kind, adapter, reading_suffix = "visual", "image", _FORMAT_SUFFIXES[fmt]
    elif fmt in _WORD_FORMATS:
        kind, adapter = "word_document", "word"
        reading_suffix = ".docx" if fmt == "docx" else ".doc" if fmt == "doc" else ".rtf"
    elif fmt in _SPREADSHEET_FORMATS:
        kind, adapter = "spreadsheet", "spreadsheet"
        reading_suffix = ".xlsx" if fmt == "xlsx" else ".xls"
    elif fmt == "zip" and suffix in {".docx", ".doc"}:
        kind, adapter, reading_suffix = "word_document", "word", suffix
    elif fmt == "zip" and suffix in {".xlsx", ".xlsm", ".xls"}:
        kind, adapter, reading_suffix = "spreadsheet", "spreadsheet", suffix
    elif fmt in {"zip", "rar"}:
        kind, adapter, reading_suffix = "container", "archive", suffix
    elif fmt == "cfb" and suffix in {".doc", ".docx"}:
        kind, adapter, reading_suffix = "word_document", "word", ".doc"
    elif fmt == "cfb" and suffix in {".xls", ".xlsx", ".xlsm"}:
        kind, adapter, reading_suffix = "spreadsheet", "spreadsheet", ".xls"
    else:
        # No trustworthy signature: retain the declared suffix so the existing
        # adapter can report a content error instead of silently ignoring it.
        kind, adapter, reading_suffix = "unsupported", "unsupported", suffix
        if suffix in {".pdf"}:
            kind, adapter = "visual", "pdf"
        elif suffix in {".jpg", ".jpeg", ".jfif", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff"}:
            kind, adapter = "visual", "image"
        elif suffix in {".docx", ".doc"}:
            kind, adapter = "word_document", "word"
        elif suffix in {".xlsx", ".xlsm", ".xls"}:
            kind, adapter = "spreadsheet", "spreadsheet"
        elif suffix in {".zip", ".rar"}:
            kind, adapter = "container", "archive"
    suffix_aliases = {
        ".jpeg": ".jpg", ".jfif": ".jpg",
        ".tif": ".tiff",
        ".xlsm": ".xlsx",
    }
    equivalent = suffix_aliases.get(suffix, suffix) == suffix_aliases.get(reading_suffix, reading_suffix)
    return {
        "kind": kind,
        "adapter": adapter,
        "reading_suffix": reading_suffix,
        "format_mismatch": (
            fmt != "unknown"
            and not equivalent
        ),
    }


def normalize_reading_path(item: dict, root: Path) -> Path:
    """Return bytes with a suffix that matches the detected reader.

    The original inventory path, source name and SHA-256 remain authoritative.
    Some customer submissions have a valid file body but an incorrect extension
    (for example a DOCX named ``.pdf`` or a BIFF workbook named ``.xlsx``).
    Readers may legitimately use the extension to choose a decoder, so a
    byte-for-byte temporary copy with the detected suffix is required instead
    of guessing from the filename or modifying the original.
    """
    source = Path(item["path"])
    reading_suffix = str(item.get("reading_suffix") or source.suffix).lower()
    if not reading_suffix.startswith("."):
        raise ReadingSystemError("材料读取格式没有可用的内容后缀", code="unsupported_format",
                                 retryable=False)
    if reading_suffix == source.suffix.lower():
        return source
    target_root = Path(root)
    target_root.mkdir(parents=True, exist_ok=True)
    target = target_root / f"{item['file_id']}-content{reading_suffix}"
    if target.exists() or target.is_symlink():
        raise ReadingSystemError("材料读取临时副本已存在，拒绝覆盖", code="staging_conflict",
                                 retryable=False)
    shutil.copyfile(source, target)
    return target


def normalize_visual_orientation(path: Path) -> dict:
    """Apply EXIF orientation to a task-owned visual copy, never the original.

    Smartphone photos can store landscape pixels plus an EXIF rotation tag.
    Viewers may rotate them automatically, while model APIs and image loaders
    do not consistently do so. Normalize the bytes used for visual reading so
    the model sees upright text and store scenes without changing the source
    file, its hash or its audit provenance.
    """
    from PIL import Image, ImageOps

    path = Path(path)
    with Image.open(path) as image:
        orientation = int(image.getexif().get(274, 1) or 1)
        original_size = [image.width, image.height]
        if orientation not in {2, 3, 4, 5, 6, 7, 8}:
            return {
                "applied": False,
                "orientation": orientation,
                "original_size": original_size,
                "normalized_size": original_size,
            }
        image.load()
        format_name = image.format
        if not format_name:
            format_name = Image.registered_extensions().get(path.suffix.lower())
        if not format_name:
            raise ReadingSystemError(
                "图片带有方向标记，但没有可用的编码格式",
                code="unsupported_visual_orientation",
                retryable=False,
            )
        oriented = ImageOps.exif_transpose(image)
        options = {}
        if format_name == "JPEG":
            if oriented.mode not in {"RGB", "L"}:
                oriented = oriented.convert("RGB")
            options = {"quality": 95, "subsampling": 0}
        elif format_name == "WEBP":
            options = {"quality": 95}
        normalized_size = [oriented.width, oriented.height]

    target = path.with_name(path.name + ".orientation.tmp")
    try:
        oriented.save(target, format=format_name, **options)
        os.replace(target, path)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {
        "applied": True,
        "orientation": orientation,
        "original_size": original_size,
        "normalized_size": normalized_size,
    }


def classify_reading_error(exc: Exception) -> dict:
    """Assign ownership without exposing raw decoder messages to customer copy."""
    if isinstance(exc, ReadingSecurityError):
        return {"status": SECURITY_REJECTED, "error_code": exc.code,
                "retryable": False, "customer_actionable": False,
                "reason": str(exc)}
    if isinstance(exc, ReadingSystemError):
        return {"status": SYSTEM_FAILURE, "error_code": exc.code,
                "retryable": exc.retryable, "customer_actionable": False,
                "reason": str(exc)}
    # A malformed image/PDF stream is evidence of an unreadable customer file,
    # not an operating-system or permission failure. Keep this check before
    # OSError because Pillow's UnidentifiedImageError subclasses OSError.
    module, name = type(exc).__module__, type(exc).__name__
    if (module == "PIL" and name == "UnidentifiedImageError") or (
            module.startswith("pymupdf")
            and name in {"FileDataError", "EmptyFileError", "JM_Exc_FileDataError"}) or (
            module.startswith("pypdf")
            and name in {"PyPdfError", "PdfReadError", "PdfStreamError",
                         "EmptyFileError", "PageSizeNotDefinedError"}):
        return {"status": CUSTOMER_UNREADABLE, "error_code": "unreadable_content",
                "retryable": False, "customer_actionable": True,
                "reason": "材料内容无法解码或页面结构损坏。"}
    if module.startswith("pypdf") and name in {"WrongPasswordError", "FileNotDecryptedError"}:
        return {"status": PROTECTED, "error_code": "protected_document",
                "retryable": False, "customer_actionable": True,
                "reason": "PDF 已加密，无法读取页面内容。"}
    if isinstance(exc, PermissionError):
        return {"status": SYSTEM_FAILURE, "error_code": "permission_denied",
                "retryable": False, "customer_actionable": False,
                "reason": "材料读取进程没有访问权限"}
    if isinstance(exc, FileNotFoundError):
        return {"status": SYSTEM_FAILURE, "error_code": "source_disappeared",
                "retryable": True, "customer_actionable": False,
                "reason": "待读文件在读取过程中不存在"}
    if isinstance(exc, IsADirectoryError):
        return {"status": SYSTEM_FAILURE, "error_code": "source_replaced",
                "retryable": False, "customer_actionable": False,
                "reason": "待读文件路径被目录替代"}
    if isinstance(exc, MemoryError):
        return {"status": SYSTEM_FAILURE, "error_code": "memory_exhausted",
                "retryable": True, "customer_actionable": False,
                "reason": "材料读取内存不足"}
    if isinstance(exc, (TimeoutError, subprocess.TimeoutExpired)):
        return {"status": SYSTEM_FAILURE, "error_code": "read_timeout",
                "retryable": True, "customer_actionable": False,
                "reason": "材料读取超时"}
    if isinstance(exc, (ModuleNotFoundError, ImportError)):
        return {"status": SYSTEM_FAILURE, "error_code": "dependency_missing",
                "retryable": False, "customer_actionable": False,
                "reason": "材料读取依赖未安装"}
    if isinstance(exc, subprocess.SubprocessError):
        return {"status": SYSTEM_FAILURE, "error_code": "subprocess_failed",
                "retryable": True, "customer_actionable": False,
                "reason": "材料转换子进程执行失败"}
    if isinstance(exc, OSError):
        code = "os_error"
        reason = "材料读取发生系统文件错误"
        if exc.errno == errno.ENOSPC:
            code, reason = "storage_full", "材料读取可写空间不足"
        elif exc.errno in {errno.EACCES, errno.EPERM}:
            code, reason = "permission_denied", "材料读取进程没有访问权限"
        elif exc.errno == errno.ENOENT:
            code, reason, retryable = "source_disappeared", "待读文件在读取过程中不存在", True
            return {"status": SYSTEM_FAILURE, "error_code": code,
                    "retryable": retryable, "customer_actionable": False,
                    "reason": reason}
        elif exc.errno == errno.EIO:
            code, reason = "io_error", "材料读取发生存储 I/O 错误"
        return {"status": SYSTEM_FAILURE, "error_code": code,
                "retryable": True, "customer_actionable": False,
                "reason": reason}
    if isinstance(exc, AuditError):
        text = str(exc)
        system_markers = (
            "读取工具", "转换器", "转换失败", "LibreOffice", "磁盘空间",
            "超出安全读取范围", "超出安全渲染范围", "内存", "依赖",
            "连接", "超时", "系统缺少", "无法保存完整",
        )
        if any(marker in text for marker in system_markers):
            return {"status": SYSTEM_FAILURE, "error_code": "execution_resource_failure",
                    "retryable": True, "customer_actionable": False,
                    "reason": text}
        return {"status": CUSTOMER_UNREADABLE, "error_code": "unreadable_content",
                "retryable": False, "customer_actionable": True,
                "reason": text}
    return {"status": INTERNAL_BUG, "error_code": "internal_exception",
            "retryable": False, "customer_actionable": False,
            "reason": "材料读取程序内部异常：" + type(exc).__name__}


def failure_limitation(details: dict) -> str:
    if details["status"] == SYSTEM_FAILURE:
        return ("材料读取程序未完成：" + details["reason"]
                + "；属系统或执行问题，不能据此认定客户缺件。")
    if details["status"] == INTERNAL_BUG:
        return details["reason"] + "；未将文件判为客户缺失，需查看技术记录。"
    if details["status"] == SECURITY_REJECTED:
        return "材料安全校验未通过：" + details["reason"]
    return details["reason"]


def mark_reading_failure(item: dict, exc: Exception, *, adapter: str | None = None) -> dict:
    details = classify_reading_error(exc)
    if adapter:
        item["adapter"] = adapter
    item.update({
        "status": details["status"],
        "error_code": details["error_code"],
        "retryable": details["retryable"],
        "customer_actionable": details["customer_actionable"],
    })
    note = failure_limitation(details)
    limitations = item.setdefault("limitations", [])
    if note not in limitations:
        limitations.append(note)
    if details["status"] == SECURITY_REJECTED:
        raise ReadingSecurityError(note)
    return details


def derive_receipt(item: dict, units: list[dict]) -> dict:
    """Build one stable file-level receipt from an inventory item and its units."""
    matching = [unit for unit in units if unit.get("file_id") == item.get("file_id")]
    successful = [unit for unit in matching if unit.get("read_status", READ_OK) == READ_OK]
    failed = [unit for unit in matching if unit.get("read_status") in FAILURE_STATUSES]
    limitations = list(dict.fromkeys([
        *(item.get("limitations") or []),
        *(reason for unit in matching for reason in (unit.get("limitations") or [])),
    ]))
    status = item.get("status")
    if status not in FAILURE_STATUSES:
        if item.get("kind") == "unsupported" or item.get("adapter") == "unsupported":
            status = UNSUPPORTED
        elif failed and successful:
            status = PARTIALLY_READ
        elif failed and not successful:
            status = failed[0]["read_status"]
        elif limitations:
            status = CUSTOMER_UNREADABLE
        elif matching:
            status = READ_OK
        elif item.get("kind") == "container":
            status = READ_OK
        else:
            status = SYSTEM_FAILURE
    failure = next((unit for unit in failed), {})
    is_spreadsheet = item.get("adapter") == "spreadsheet" or item.get("kind") == "spreadsheet"
    is_page_source = (
        bool(item.get("rendered_pdf"))
        or item.get("adapter") in {"pdf", "word"}
        or item.get("kind") == "word_document"
        or str(item.get("reading_suffix") or "").lower() == ".pdf"
    )
    page_units = [
        unit for unit in matching
        if is_page_source and str(unit.get("locator") or "").startswith("第")
    ]
    page_ok = [unit for unit in page_units if unit.get("read_status", READ_OK) == READ_OK]
    sheets = {
        unit.get("locator") for unit in matching
        if is_spreadsheet and unit.get("locator")
        and str(unit.get("locator")) != "电子表格"
        and not unit.get("photo_reference")
        and unit.get("read_status", READ_OK) == READ_OK
    }
    # A receipt with both readable and failed units is usable evidence, but it
    # is not a "please re-upload the whole file" finding. The failed unit and
    # its limitation remain visible; only the file-level action flag is
    # suppressed so downstream copy does not overstate what the customer must
    # fix. A file with no successful units stays actionable when the decoder
    # proves the content itself is unreadable.
    actionable = status != PARTIALLY_READ and bool(
        failure.get("customer_actionable") or item.get("customer_actionable")
        or status in {CUSTOMER_UNREADABLE, PROTECTED, UNSUPPORTED}
    )
    receipt = {
        "file_id": item.get("file_id"),
        "source_file": item.get("source_file"),
        "declared_format": item.get("declared_format") or item.get("suffix"),
        "sniffed_format": item.get("sniffed_format") or "unknown",
        "adapter": item.get("adapter") or item.get("kind"),
        "status": status,
        "pages_read": len(page_ok),
        "pages_total": (item.get("pages_total") or len(page_units) or None)
        if is_page_source else None,
        "sheet_count": len(sheets) or None,
        "image_count": sum(1 for unit in matching if unit.get("image")),
        "unit_count": len(matching),
        "error_code": failure.get("error_code") or item.get("error_code"),
        "retryable": bool(failure.get("retryable") or item.get("retryable")),
        "customer_actionable": actionable,
        "elapsed_seconds": round(float(item.get("elapsed_seconds") or 0.0), 3),
        "attempts": int(item.get("attempts") or 1),
        "limitations": limitations,
    }
    if status == SYSTEM_FAILURE:
        receipt["customer_actionable"] = False
    return receipt


def aggregate_reading_status(receipts: list[dict]) -> str:
    statuses = {receipt.get("status") for receipt in receipts}
    if SYSTEM_FAILURE in statuses or INTERNAL_BUG in statuses or SECURITY_REJECTED in statuses:
        return SYSTEM_FAILURE
    if PARTIALLY_READ in statuses:
        return PARTIALLY_READ
    # One bad attachment must not erase the fact that other attachments were
    # read successfully. System/security failures still take precedence above
    # because those mean the execution itself did not complete.
    if READ_OK in statuses and statuses & {CUSTOMER_UNREADABLE, PROTECTED, UNSUPPORTED}:
        return PARTIALLY_READ
    if CUSTOMER_UNREADABLE in statuses or PROTECTED in statuses:
        return CUSTOMER_UNREADABLE
    if UNSUPPORTED in statuses:
        return UNSUPPORTED
    return READ_OK


def reading_policy_context(policy):
    """Keep the material catalogue and provenance, omit downstream audit prose.

    Required transcription fields live in the reader's explicit instructions
    and output schema. Changing those still invalidates a saved reading. The
    catalogue's broad clarifications govern later gates/audits, not pixels.
    """
    if not isinstance(policy,dict):
        raise AuditError('原始资料读取须提供明确资料目录')
    return {key:deepcopy(value) for key,value in policy.items() if key!='clarifications'}
