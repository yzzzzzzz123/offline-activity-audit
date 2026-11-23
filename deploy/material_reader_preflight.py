"""Release gate for native Linux material reading.

This script checks the actual host capabilities used by the deterministic
material readers. It never calls a model, RDS, OSS, Qince, or a business
callback. A failed check exits non-zero so deployment cannot treat a missing
system dependency as a customer material problem.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import importlib
import importlib.metadata
import json
import os
from pathlib import Path
import shutil
import signal
import stat
import subprocess
import sys
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "skills" / "orchestrate-offline-audit" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


MODULES = (
    ("pymupdf", "PyMuPDF", "PDF打开、逐页渲染和文本读取"),
    ("PIL", "Pillow", "图片完整性、尺寸和格式读取"),
    ("openpyxl", "openpyxl", "XLSX/OOXML表格读取"),
    ("xlrd", "xlrd", "旧式BIFF .xls表格读取"),
    ("pypdf", "pypdf", "PDF辅助结构读取"),
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _check(check_id: str, status: str, message: str, evidence=None) -> dict:
    return {
        "id": check_id,
        "status": status,
        "message": message,
        "evidence": evidence or {},
    }


def _error_text(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"[:1200]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _remove_owned_directory(path: Path, *, parent: Path, prefix: str) -> dict:
    """Remove one preflight-owned directory and verify the removal.

    The caller may only remove a direct child of the preflight directory whose
    name has the expected prefix. This deliberately avoids broad glob cleanup
    so a concurrent release check or a real customer worktree cannot be
    deleted by a stale self-check.
    """
    target = Path(path)
    evidence = {
        "path": str(target),
        "removed": False,
        "remaining": True,
        "created": False,
    }
    try:
        parent_resolved = Path(parent).resolve(strict=False)
        target_resolved = target.resolve(strict=False)
        if target_resolved.parent != parent_resolved or not target_resolved.name.startswith(prefix):
            raise RuntimeError("refusing to delete an unexpected preflight path")
        evidence["created"] = target_resolved.exists()
        if target_resolved.exists():
            shutil.rmtree(target_resolved)
        evidence["removed"] = not target_resolved.exists()
        evidence["remaining"] = target_resolved.exists()
        # Remove the shared .preflight container only when it is now empty.
        if target_resolved.parent.name == ".preflight" and target_resolved.parent.is_dir():
            try:
                target_resolved.parent.rmdir()
            except OSError:
                pass
    except Exception as exc:
        evidence["error"] = _error_text(exc)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        evidence["removed"] = not target.exists()
        evidence["remaining"] = target.exists()
    return evidence


def probe_platform(*, allow_non_linux: bool = False) -> dict:
    """Require a POSIX Linux host for a release pass."""
    evidence = {
        "sys_platform": sys.platform,
        "os_name": os.name,
        "machine": os.uname().machine if hasattr(os, "uname") else None,
    }
    if sys.platform.startswith("linux") and os.name == "posix":
        return _check("platform-linux", "pass", "运行于Linux/POSIX原生环境", evidence)
    if allow_non_linux:
        return _check(
            "platform-linux",
            "warn",
            "当前不是Linux；仅允许开发诊断，不能作为Linux发布验收。",
            evidence,
        )
    return _check(
        "platform-linux",
        "fail",
        "发布门禁要求Linux原生Python；当前主机不是Linux。",
        evidence,
    )


def probe_python() -> dict:
    version = ".".join(str(value) for value in sys.version_info[:3])
    if sys.version_info >= (3, 11):
        return _check(
            "python-version",
            "pass",
            "Python版本满足>=3.11",
            {"version": version, "executable": sys.executable},
        )
    return _check(
        "python-version",
        "fail",
        "Python版本必须>=3.11",
        {"version": version, "executable": sys.executable},
    )


def probe_modules() -> dict:
    rows = []
    failed = []
    for module_name, distribution, purpose in MODULES:
        row = {
            "module": module_name,
            "distribution": distribution,
            "purpose": purpose,
        }
        try:
            importlib.import_module(module_name)
            row["version"] = importlib.metadata.version(distribution)
            row["status"] = "pass"
        except Exception as exc:
            row["status"] = "fail"
            row["error"] = _error_text(exc)
            failed.append(module_name)
        rows.append(row)
    status = "fail" if failed else "pass"
    message = (
        "材料读取Python依赖无法导入：" + "、".join(failed)
        if failed
        else "材料读取Python依赖均可导入"
    )
    return _check("python-reader-dependencies", status, message, {"modules": rows})


def _minimal_docx(path: Path) -> None:
    with ZipFile(path, "w", ZIP_DEFLATED) as archive:
        archive.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
            '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            '<Override PartName="/word/document.xml" '
            'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
            "</Types>",
        )
        archive.writestr(
            "_rels/.rels",
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
            'Target="word/document.xml"/>'
            "</Relationships>",
        )
        archive.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body><w:p><w:r><w:t>核销材料读取预检</w:t></w:r></w:p></w:body></w:document>",
        )


def _minimal_xlsx(path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "预检"
    sheet.append(["材料", "结果"])
    sheet.append(["中文字体", "预检"])
    workbook.save(path)
    workbook.close()


def _run_office_command(command: list[str], *, timeout: int = 180) -> dict:
    """Run one isolated LibreOffice conversion and always reap its process."""
    options = {
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "env": {**os.environ, "SAL_USE_VCLPLUGIN": os.environ.get("SAL_USE_VCLPLUGIN", "svp")},
    }
    if os.name == "posix":
        options["start_new_session"] = True
    elif os.name == "nt":
        options["creationflags"] = subprocess.CREATE_NO_WINDOW
    process = subprocess.Popen(command, **options)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except BaseException:
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                process.kill()
        else:
            process.kill()
        process.communicate()
        raise
    return {
        "returncode": process.returncode,
        "stdout": (stdout or b"").decode("utf-8", errors="replace")[-1200:],
        "stderr": (stderr or b"").decode("utf-8", errors="replace")[-1200:],
    }


def probe_libreoffice(worktree_root: Path) -> dict:
    """Convert one synthetic DOCX and one XLSX to prove Writer and Calc."""
    from audit_core.word_materials import libreoffice_path

    executable = libreoffice_path()
    if not executable:
        return _check(
            "libreoffice-writer-calc",
            "fail",
            "未找到LibreOffice；无法读取DOCX或需要转换的旧式工作簿。",
            {"required": "libreoffice / soffice"},
        )
    if not worktree_root.is_dir():
        return _check(
            "libreoffice-writer-calc",
            "fail",
            "LibreOffice存在，但worktrees不存在，无法执行隔离转换预检。",
            {"executable": executable, "worktree_root": str(worktree_root)},
        )
    base = worktree_root / ".preflight" / f"office-{_stamp()}-{uuid4().hex[:8]}"
    profile = base / "profile"
    output = base / "output"
    cleanup = {
        "path": str(base),
        "removed": True,
        "remaining": False,
        "created": False,
    }
    try:
        profile.mkdir(parents=True)
        output.mkdir()
        cleanup.update({"removed": False, "remaining": True, "created": True})
        docx = base / "probe.docx"
        xlsx = base / "probe.xlsx"
        _minimal_docx(docx)
        _minimal_xlsx(xlsx)
        profile_uri = profile.resolve().as_uri()
        writer = _run_office_command(
            [
                executable,
                "-env:UserInstallation=" + profile_uri,
                "--headless",
                "--norestore",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                "pdf:writer_pdf_Export",
                "--outdir",
                str(output),
                str(docx),
            ]
        )
        writer_target = output / "probe.pdf"
        writer_ok = (
            writer["returncode"] == 0
            and writer_target.is_file()
            and writer_target.stat().st_size > 100
            and writer_target.read_bytes()[:5] == b"%PDF-"
        )
        calc = _run_office_command(
            [
                executable,
                "-env:UserInstallation=" + profile_uri,
                "--headless",
                "--norestore",
                "--nodefault",
                "--nofirststartwizard",
                "--convert-to",
                "csv",
                "--outdir",
                str(output),
                str(xlsx),
            ]
        )
        calc_target = output / "probe.csv"
        calc_ok = (
            calc["returncode"] == 0
            and calc_target.is_file()
            and calc_target.stat().st_size > 0
        )
        evidence = {
            "executable": executable,
            "writer_returncode": writer["returncode"],
            "writer_output": str(writer_target),
            "writer_bytes": writer_target.stat().st_size if writer_target.is_file() else 0,
            "calc_returncode": calc["returncode"],
            "calc_output": str(calc_target),
            "calc_bytes": calc_target.stat().st_size if calc_target.is_file() else 0,
            "stdout_tail": {"writer": writer["stdout"], "calc": calc["stdout"]},
            "stderr_tail": {"writer": writer["stderr"], "calc": calc["stderr"]},
            "probe_directory": str(base),
            "cleanup": cleanup,
            "model_calls": 0,
        }
        if writer_ok and calc_ok:
            return _check(
                "libreoffice-writer-calc",
                "pass",
                "LibreOffice Writer与Calc的隔离转换均成功",
                evidence,
            )
        failures = []
        if not writer_ok:
            failures.append("Writer未能生成有效PDF")
        if not calc_ok:
            failures.append("Calc未能生成CSV")
        return _check(
            "libreoffice-writer-calc",
            "fail",
            "；".join(failures) + "；不得记为客户资料问题。",
            evidence,
        )
    except Exception as exc:
        return _check(
            "libreoffice-writer-calc",
            "fail",
            "LibreOffice能力预检异常；属于系统依赖或执行环境问题。",
            {
                "executable": executable,
                "error": _error_text(exc),
                "cleanup": cleanup,
                "model_calls": 0,
            },
        )
    finally:
        if cleanup["created"]:
            cleanup.update(
                _remove_owned_directory(
                    base,
                    parent=base.parent,
                    prefix="office-",
                )
            )


def probe_rar() -> dict:
    try:
        from audit_core.archive_tools import _version_text, rar_tool

        executable = rar_tool()
        if not executable:
            return _check(
                "rar-libarchive",
                "fail",
                "未提供可读取RAR的bsdtar/libarchive；GNU tar不代表支持RAR。",
                {"required": "bsdtar (libarchive)"},
            )
        version = _version_text(executable)
        name = Path(executable).name.casefold()
        if "bsdtar" not in name and "bsdtar" not in version.casefold() and "libarchive" not in version.casefold():
            return _check(
                "rar-libarchive",
                "fail",
                "检测到的tar不是libarchive实现，不能用于RAR。",
                {"executable": executable, "version": version[:500]},
            )
        return _check(
            "rar-libarchive",
            "pass",
            "已检测到libarchive-backed RAR读取工具",
            {"executable": executable, "version": version[:500]},
        )
    except Exception as exc:
        return _check(
            "rar-libarchive",
            "fail",
            "RAR能力探测失败。",
            {"error": _error_text(exc)},
        )


def _font_families_from_fc_list() -> list[str]:
    executable = shutil.which("fc-list")
    if not executable:
        return []
    result = subprocess.run(
        [executable, ":lang=zh", "family"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=15,
        check=False,
    )
    if result.returncode:
        return []
    values = []
    for line in (result.stdout or b"").decode("utf-8", errors="replace").splitlines():
        value = line.strip()
        if value and value not in values:
            values.append(value)
    return values


def _font_files_from_known_directories() -> list[str]:
    roots = [
        Path("/usr/share/fonts"),
        Path("/usr/local/share/fonts"),
        Path.home() / ".local/share/fonts",
        Path("C:/Windows/Fonts"),
        Path("/System/Library/Fonts"),
    ]
    tokens = (
        "notosanscjk",
        "notoserifcjk",
        "sourcehansans",
        "sourcehanserif",
        "sourcehancjk",
        "wqy",
        "wenquanyi",
        "simsun",
        "simhei",
        "msyh",
        "yahei",
        "dengxian",
        "fangsong",
        "kaiti",
    )
    found = []
    for root in roots:
        if not root.is_dir():
            continue
        try:
            for path in root.rglob("*"):
                if not path.is_file():
                    continue
                name = path.name.casefold()
                if any(token in name for token in tokens):
                    found.append(str(path))
                    if len(found) >= 12:
                        return found
        except OSError:
            continue
    return found


def probe_cjk_fonts() -> dict:
    families = []
    error = None
    file_error = None
    try:
        families = _font_families_from_fc_list()
    except Exception as exc:
        error = _error_text(exc)
    try:
        files = [] if families else _font_files_from_known_directories()
    except Exception as exc:
        files = []
        file_error = _error_text(exc)
    evidence = {
        "fc_list_families": families[:12],
        "font_files": files,
        "fc_list_error": error,
        "font_directory_error": file_error,
    }
    if families or files:
        return _check(
            "cjk-fonts",
            "pass",
            "已检测到中文字体；PDF/Word转换和页面渲染具备字体基础。",
            evidence,
        )
    return _check(
        "cjk-fonts",
        "fail",
        "未检测到中文字体；请安装fonts-noto-cjk或等效字体。",
        evidence,
    )


def probe_timezone() -> dict:
    try:
        from zoneinfo import TZPATH, ZoneInfo

        zone = ZoneInfo("Asia/Shanghai")
        offset = zone.utcoffset(datetime(2026, 7, 1))
        evidence = {
            "zone": "Asia/Shanghai",
            "utc_offset_seconds": int(offset.total_seconds()) if offset is not None else None,
            "tzpath": list(TZPATH),
        }
        if offset is None or int(offset.total_seconds()) != 8 * 60 * 60:
            return _check(
                "timezone-asia-shanghai",
                "fail",
                "Asia/Shanghai时区数据异常。",
                evidence,
            )
        return _check(
            "timezone-asia-shanghai",
            "pass",
            "ZoneInfo可加载Asia/Shanghai时区。",
            evidence,
        )
    except Exception as exc:
        return _check(
            "timezone-asia-shanghai",
            "fail",
            "缺少时区数据；安装系统tzdata或Python tzdata包。",
            {"error": _error_text(exc)},
        )


def probe_worktree(worktree_root: Path) -> dict:
    root = Path(worktree_root).absolute()
    evidence = {"worktree_root": str(root)}
    if not root.exists() or not root.is_dir():
        return _check(
            "worktree-writable",
            "fail",
            "worktrees运行目录不存在；先完成部署初始化。",
            evidence,
        )
    linked = [
        str(path)
        for path in (root, *root.parents)
        if path.is_symlink() or getattr(path, "is_junction", lambda: False)()
    ]
    if linked:
        return _check(
            "worktree-writable",
            "fail",
            "worktrees路径经过链接或目录联接，材料读取将拒绝写入。",
            {**evidence, "linked_components": linked},
        )
    candidate = root / f".preflight-write-{uuid4().hex}"
    probe = candidate / "probe.bin"
    replaced = candidate / "probe.replaced"
    try:
        candidate.mkdir(mode=0o700)
        payload = b"offline-activity-audit-material-reader-preflight\n"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(probe, flags, 0o600)
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(probe, replaced)
        if replaced.read_bytes() != payload:
            raise OSError("回读内容不一致")
        return _check(
            "worktree-writable",
            "pass",
            "worktrees可创建、写入、原子替换并回读预检文件。",
            {
                **evidence,
                "probe_directory": str(candidate),
                "mode": oct(stat.S_IMODE(candidate.stat().st_mode)),
            },
        )
    except Exception as exc:
        return _check(
            "worktree-writable",
            "fail",
            "worktrees不可写；属于部署权限问题，不是客户材料问题。",
            {**evidence, "probe_directory": str(candidate), "error": _error_text(exc)},
        )
    finally:
        shutil.rmtree(candidate, ignore_errors=True)


def probe_staging_layout(worktree_root: Path) -> dict:
    try:
        from audit_core.material_reader import staging_directory

        output = Path(worktree_root).absolute() / ".preflight-caller-output"
        staging = staging_directory(output)
        if staging.parent != output.absolute() or staging.name != ".staging":
            raise RuntimeError("staging未落在调用方输出目录")
        return _check(
            "staging-inside-output",
            "pass",
            "材料读取staging固定位于调用方输出目录内，不依赖系统TEMP。",
            {
                "caller_output": str(output),
                "staging": str(staging),
                "system_temp_not_used": True,
            },
        )
    except Exception as exc:
        return _check(
            "staging-inside-output",
            "fail",
            "材料读取staging目录契约异常。",
            {"error": _error_text(exc)},
        )


def probe_posix_native() -> dict:
    try:
        from audit_core import legacy_activity_workbook, material_reader, pdf_materials
        from audit_core import reading_contract, word_materials, workbook_reading

        renderer = material_reader.capabilities(family="document").get("word_renderer")
        com_loaded = "win32com" in sys.modules
        evidence = {
            "os_name": os.name,
            "word_renderer": renderer,
            "win32com_loaded": com_loaded,
            "modules": [
                material_reader.__name__,
                word_materials.__name__,
                legacy_activity_workbook.__name__,
                workbook_reading.__name__,
                pdf_materials.__name__,
                reading_contract.__name__,
            ],
        }
        if os.name == "posix" and (
            com_loaded or (renderer and "COM" in str(renderer))
        ):
            return _check(
                "posix-native-readers",
                "fail",
                "POSIX材料读取路径仍依赖Word COM/PowerShell。",
                evidence,
            )
        return _check(
            "posix-native-readers",
            "pass",
            "材料读取模块可在当前POSIX路径导入；不依赖PowerShell或Word COM。",
            evidence,
        )
    except Exception as exc:
        return _check(
            "posix-native-readers",
            "fail",
            "材料读取模块无法在当前环境导入。",
            {"error": _error_text(exc)},
        )


def run_smoke_material(
    source: Path,
    worktree_root: Path,
    *,
    keep_output: bool = False,
) -> dict:
    """Read one caller-selected source and remove its diagnostic output.

    The report keeps a compact receipt and visual-normalization summary so a
    release gate can inspect the result after the temporary directory is gone.
    ``keep_output`` exists only for explicit local diagnosis; normal preflight
    and self-check runs must not leave test files in ``worktrees``.
    """
    source = Path(source).absolute()
    root = Path(worktree_root).absolute()
    run_root = root / ".preflight" / f"material-read-{_stamp()}-{uuid4().hex[:8]}"
    output = run_root / "output"
    cleanup = {
        "path": str(run_root),
        "removed": False,
        "remaining": True,
        "created": False,
    }
    report = {
        "source": str(source),
        "output": str(output),
        "manifest_retained": bool(keep_output),
        "cleanup": cleanup,
        "model_calls": 0,
        "status": "fail",
    }
    try:
        if not source.is_file() or source.is_symlink():
            raise ValueError("smoke material必须是可读取的普通文件")
        resolved_root = root.resolve()
        if resolved_root not in run_root.resolve().parents:
            raise RuntimeError("smoke输出未落在worktrees内")
        run_root.mkdir(parents=True)
        cleanup.update({"removed": False, "remaining": True, "created": True})
        from audit_core.material_reader import read_material

        result = read_material(source, output)
        manifest_path = Path(result.get("manifest") or "")
        if not manifest_path.is_file():
            raise RuntimeError("read_material未发布reading-manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        receipts = result.get("file_receipts") or []
        receipt_statuses = Counter(item.get("status") for item in receipts)
        visual_normalizations = []
        for archive in manifest.get("archives") or []:
            for unit in archive.get("units") or []:
                normalization = unit.get("visual_normalization")
                if normalization:
                    visual_normalizations.append(
                        {
                            "source_file": unit.get("source_file"),
                            "locator": unit.get("locator"),
                            "visual_normalization": normalization,
                        }
                    )
        report.update(
            {
                "manifest": str(manifest_path),
                "manifest_sha256": _sha256(manifest_path),
                "reading_status": result.get("reading_status"),
                "source_count": result.get("source_count"),
                "unit_count": result.get("unit_count"),
                "result_model_calls": result.get("model_calls"),
                "manifest_model_calls": manifest.get("model_calls"),
                "receipt_statuses": dict(sorted(receipt_statuses.items())),
                "receipts": [
                    {
                        "source_file": item.get("source_file"),
                        "declared_format": item.get("declared_format"),
                        "sniffed_format": item.get("sniffed_format"),
                        "adapter": item.get("adapter"),
                        "status": item.get("status"),
                        "pages_read": item.get("pages_read"),
                        "pages_total": item.get("pages_total"),
                        "sheet_count": item.get("sheet_count"),
                        "error_code": item.get("error_code"),
                        "limitations": item.get("limitations") or [],
                    }
                    for item in receipts
                ],
                "visual_normalizations": visual_normalizations,
                "limitations": result.get("limitations") or [],
            }
        )
        problems = []
        if result.get("model_calls") != 0 or manifest.get("model_calls") != 0:
            problems.append("出现非0模型调用")
        if not receipts:
            problems.append("没有逐文件读取回执")
        if result.get("reading_status") != "read_ok":
            problems.append("整体读取状态不是read_ok")
        if any(item.get("status") != "read_ok" for item in receipts):
            problems.append("存在非read_ok文件回执")
        if int(result.get("source_count") or 0) <= 0:
            problems.append("source_count为0")
        if int(result.get("unit_count") or 0) <= 0:
            problems.append("unit_count为0")
        if problems:
            report["problems"] = problems
            report["message"] = "；".join(problems)
        else:
            report["status"] = "pass"
            report["message"] = "真实材料经正常read_material入口返回完整读取回执"
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        report["error"] = str(exc)[:1200]
        report["customer_actionable"] = bool(
            getattr(exc, "customer_actionable", False)
        )
        report["message"] = "smoke读取未完成；按系统/内部/文件状态如实留痕，不补造读取结果。"
    finally:
        if keep_output:
            cleanup.update(
                {
                    "removed": False,
                    "remaining": run_root.exists(),
                    "retained": True,
                }
            )
        elif cleanup["created"]:
            cleanup.update(
                _remove_owned_directory(
                    run_root,
                    parent=run_root.parent,
                    prefix="material-read-",
                )
            )
            if not cleanup.get("removed"):
                report["status"] = "fail"
                report["problems"] = [
                    *(report.get("problems") or []),
                    "smoke测试输出未清理",
                ]
                report["message"] = "smoke读取完成但临时输出未清理；按发布失败处理。"
    return report


def build_report(
    *,
    worktree_root: Path | str = ROOT / "worktrees",
    smoke_material: Path | str | None = None,
    allow_non_linux: bool = False,
    keep_smoke_output: bool = False,
) -> dict:
    """Run every release check and return one machine-readable report."""
    root = Path(worktree_root).absolute()
    checks = [
        probe_platform(allow_non_linux=allow_non_linux),
        probe_python(),
        probe_modules(),
        probe_worktree(root),
        probe_staging_layout(root),
        probe_libreoffice(root),
        probe_rar(),
        probe_cjk_fonts(),
        probe_timezone(),
        probe_posix_native(),
    ]
    smoke = None
    if smoke_material is not None:
        smoke = run_smoke_material(
            Path(smoke_material),
            root,
            keep_output=keep_smoke_output,
        )
        checks.append(
            _check(
                "smoke-material",
                smoke["status"],
                smoke.get("message", "真实材料smoke读取"),
                {
                    key: smoke.get(key)
                    for key in (
                        "source",
                        "output",
                        "manifest",
                        "manifest_sha256",
                        "manifest_retained",
                        "reading_status",
                        "source_count",
                        "unit_count",
                        "model_calls",
                        "receipt_statuses",
                        "visual_normalizations",
                        "cleanup",
                        "error_type",
                        "error",
                    )
                    if key in smoke
                },
            )
        )
    cleanup_rows = {}
    for item in checks:
        cleanup = (item.get("evidence") or {}).get("cleanup")
        if cleanup:
            cleanup_rows[item["id"]] = cleanup
    retained = [
        check_id
        for check_id, cleanup in cleanup_rows.items()
        if cleanup.get("retained")
    ]
    failed_cleanup = [
        check_id
        for check_id, cleanup in cleanup_rows.items()
        if not cleanup.get("retained") and not cleanup.get("removed")
    ]
    if failed_cleanup:
        cleanup_status = "fail"
        cleanup_message = "预检测试文件未完全删除"
    elif retained:
        cleanup_status = "warn"
        cleanup_message = "显式诊断保留了smoke输出；发布前必须重新运行默认清理模式"
    else:
        cleanup_status = "pass"
        cleanup_message = "预检创建的Office和smoke测试文件已删除"
    checks.append(
        _check(
            "test-material-cleanup",
            cleanup_status,
            cleanup_message,
            {
                "directories": cleanup_rows,
                "failed": failed_cleanup,
                "retained": retained,
            },
        )
    )
    failures = [item["id"] for item in checks if item["status"] == "fail"]
    warnings = [item["id"] for item in checks if item["status"] == "warn"]
    counts = Counter(item["status"] for item in checks)
    return {
        "schema_version": "1.0",
        "checked_at": _utc_now(),
        "scope": (
            "Linux材料读取发布预检；仅检查本机依赖、路径及可选真实文件读取。"
            "不调用模型、RDS、OSS、勤策或业务回调，不重跑核销。"
        ),
        "platform": {
            "sys_platform": sys.platform,
            "os_name": os.name,
            "python": ".".join(str(value) for value in sys.version_info[:3]),
            "executable": sys.executable,
        },
        "worktree_root": str(root),
        "checks": checks,
        "smoke": smoke,
        "summary": dict(sorted(counts.items())),
        "failures": failures,
        "warnings": warnings,
        "model_calls": 0,
        "test_material_retained": bool(retained),
        "passed": not failures,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--worktree-root",
        type=Path,
        default=ROOT / "worktrees",
        help="部署运行目录，默认项目worktrees/",
    )
    parser.add_argument(
        "--smoke-material",
        type=Path,
        help="可选：用正常read_material入口读取一个真实ZIP或单文件",
    )
    parser.add_argument(
        "--allow-non-linux",
        action="store_true",
        help="仅用于开发诊断；非Linux结果不能作为发布通过",
    )
    parser.add_argument(
        "--keep-smoke-output",
        action="store_true",
        help="仅用于本机诊断：保留smoke输出；发布门禁必须使用默认清理模式",
    )
    args = parser.parse_args(argv)
    report = build_report(
        worktree_root=args.worktree_root,
        smoke_material=args.smoke_material,
        allow_non_linux=args.allow_non_linux,
        keep_smoke_output=args.keep_smoke_output,
    )
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
