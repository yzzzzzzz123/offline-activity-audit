from __future__ import annotations

import shutil
import stat
import subprocess
import zipfile
from collections import Counter
from pathlib import Path, PurePosixPath
from typing import Any

from .common import AuditError, normalize_text, sha256_file
from .legacy_activity_workbook import extract_activity_return_workbook


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
EXCEL_SUFFIXES = {".xlsx", ".xlsm"}
VISUAL_DOCUMENT_SUFFIXES = IMAGE_SUFFIXES | {".pdf"}
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


def _archive_member_parts(raw_name: str, *, archive_label: str) -> tuple[str, ...]:
    raw = raw_name.replace("\\", "/")
    if "\x00" in raw:
        raise ArchiveInputError(f"{archive_label} 成员名包含 NUL 字节")
    pure = PurePosixPath(raw)
    parts = tuple(part for part in pure.parts if part not in {"", "."})
    if pure.is_absolute() or not parts or any(part == ".." for part in parts):
        raise ArchiveInputError(f"{archive_label} 包含不安全路径：{raw_name!r}")
    if ":" in parts[0]:
        raise ArchiveInputError(f"{archive_label} 包含盘符路径：{raw_name!r}")
    invalid = set('<>:"|?*')
    if any(any(char in invalid for char in part) for part in parts):
        raise ArchiveInputError(f"{archive_label} 文件名不受 Windows 支持：{raw_name!r}")
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
    raw_names = list(shape["names"])
    archive_text = normalize_text(path.stem)
    settlement_count = sum("结算单" in name or "结算表" in name for name in names)
    transfer_count = sum(
        any(marker in name for marker in ("红包", "转账", "付款", "收款"))
        for name in names
    )

    member_facts = [
        (
            normalize_text(PurePosixPath(name.replace("\\", "/")).name),
            PurePosixPath(name.replace("\\", "/")).suffix.lower(),
        )
        for name in raw_names
    ]
    poster_contract_count = sum(
        suffix in IMAGE_SUFFIXES and "合同" in name
        for name, suffix in member_facts
    )
    poster_invoice_count = sum(
        suffix in IMAGE_SUFFIXES and any(marker in name for marker in ("发票", "收据"))
        for name, suffix in member_facts
    )
    poster_settlement_count = sum(
        suffix in IMAGE_SUFFIXES and any(marker in name for marker in ("结算单", "结算表"))
        for name, suffix in member_facts
    )
    poster_photo_zip_count = sum(
        suffix == ".zip" and any(marker in name for marker in ("返图", "现场照片", "水印照片"))
        for name, suffix in member_facts
    )

    other_promotional_contract_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES and "促销合同" in name
        for name, suffix in member_facts
    )
    other_settlement_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("结算单", "结算表"))
        for name, suffix in member_facts
    )
    other_support_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("协议", "合同", "发票", "收据", "相关文件"))
        and "促销合同" not in name
        for name, suffix in member_facts
    )
    nested_archive_count = sum(suffix == ".zip" for _, suffix in member_facts)
    rar_count = sum(suffix == ".rar" for _, suffix in member_facts)
    legacy_xls_count = sum(suffix == ".xls" for _, suffix in member_facts)

    maintenance_pos_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("pos", "销售数据"))
        for name, suffix in member_facts
    )
    maintenance_settlement_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("结算单", "结算表"))
        for name, suffix in member_facts
    )

    price_contract_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("促销协议", "促销合同"))
        for name, suffix in member_facts
    )
    price_settlement_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("结算单", "结算表"))
        for name, suffix in member_facts
    )
    price_pos_visual_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("pos", "销售", "陈列销售"))
        for name, suffix in member_facts
    )
    price_photo_rar_count = sum(
        suffix == ".rar" and any(marker in name for marker in ("照片", "返图", "现场"))
        for name, suffix in member_facts
    )
    entry_contract_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(
            marker in name
            for marker in ("产品推广协议", "进场费合同", "条码费合同", "进场合同")
        )
        for name, suffix in member_facts
    )
    entry_photo_rar_count = sum(
        suffix == ".rar"
        and any(marker in name for marker in ("进场照片", "上架照片", "照片", "返图"))
        for name, suffix in member_facts
    )
    self_procured_contract_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("促销协议", "促销合同"))
        for name, suffix in member_facts
    )
    self_procured_settlement_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("结算单", "结算表"))
        for name, suffix in member_facts
    )
    self_procured_purchase_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("购买凭证", "发票", "收据"))
        for name, suffix in member_facts
    )
    self_procured_pos_count = sum(
        suffix in VISUAL_DOCUMENT_SUFFIXES
        and any(marker in name for marker in ("销售pos", "pos数据", "销售数据"))
        for name, suffix in member_facts
    )

    if (
        any(marker in archive_text for marker in ("价格补差", "补差"))
        and nested_archive_count == 0
        and rar_count == 1
        and excel_count <= 1
        and price_contract_count == 1
        and price_settlement_count == 1
        and price_pos_visual_count >= 1
        and price_photo_rar_count == 1
    ):
        return {**shape, "scenario": "price_difference_support"}

    if (
        any(marker in archive_text for marker in ("pos激励达标", "pos达标激励", "pos激励"))
        and nested_archive_count == 0
        and rar_count == 0
        and excel_count <= 1
        and image_count + pdf_count >= 1
    ):
        return {**shape, "scenario": "pos_target_incentive"}

    if (
        any(marker in archive_text for marker in ("自采赠品物料", "自采赠品", "自采物料"))
        and nested_archive_count == 0
        and rar_count == 0
        and legacy_xls_count == 1
        and excel_count <= 1
        and self_procured_contract_count == 1
        and self_procured_settlement_count == 1
        and self_procured_purchase_count == 1
        and self_procured_pos_count >= 1
    ):
        return {**shape, "scenario": "self_procured_gift_material"}

    if (
        any(marker in archive_text for marker in ("进场费", "条码费"))
        and nested_archive_count == 0
        and rar_count == 1
        and excel_count == 0
        and entry_contract_count == 1
        and entry_photo_rar_count == 1
    ):
        return {**shape, "scenario": "entry_fee"}

    if (
        any(marker in archive_text for marker in ("维护费用", "维护费"))
        and nested_archive_count == 0
        and image_count + pdf_count >= 1
        and maintenance_pos_count + maintenance_settlement_count >= 1
    ):
        return {**shape, "scenario": "maintenance_fee"}

    if (
        any(marker in archive_text for marker in ("额外搭赠", "搭赠"))
        and nested_archive_count == 0
        and excel_count == 0
        and image_count + pdf_count >= 1
    ):
        return {**shape, "scenario": "giveaway_promotion"}

    if (
        "其他" in archive_text
        and excel_count == 0
        and nested_archive_count == 0
        and image_count + pdf_count >= 4
        and other_promotional_contract_count == 1
        and other_settlement_count == 1
        and other_support_count >= 1
    ):
        return {**shape, "scenario": "other_expense"}

    if (
        excel_count == 0
        and image_count >= 3
        and poster_contract_count == 1
        and poster_invoice_count == 1
        and poster_settlement_count == 1
        and poster_photo_zip_count == 1
        and ("展示道具" in archive_text or "物料" in archive_text or "海报" in archive_text)
    ):
        return {**shape, "scenario": "poster_material"}

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
    if not 1 <= len(archives) <= 10:
        raise ArchiveInputError(
            f"input/ 必须直接包含 1～10 个 ZIP；当前发现 {len(archives)} 个。"
        )
    linked = [path.name for path in archives if path.is_symlink()]
    if linked:
        raise ArchiveInputError("ZIP 不能是符号链接：" + "、".join(linked))

    classified: dict[str, dict[str, Any]] = {}
    for archive in archives:
        item = _classify_archive(archive)
        scenario = str(item["scenario"])
        if scenario in classified:
            label = {
                "personnel_incentive": "人员激励",
                "promotional_display": "堆头陈列",
                "poster_material": "海报/物料制作",
                "other_expense": "其他费用",
                "maintenance_fee": "维护费用",
                "giveaway_promotion": "额外搭赠",
                "price_difference_support": "价格补差",
                "pos_target_incentive": "POS达标激励",
                "entry_fee": "进场费",
                "self_procured_gift_material": "自采赠品物料",
            }[scenario]
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


def _decode_archive_listing(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def _extract_rar_archive(archive_path: Path, target_root: Path) -> dict[str, Any]:
    """Extract one RAR through bsdtar after strict pre- and post-validation."""

    tar = shutil.which("tar")
    if tar is None:
        raise ArchiveInputError(
            f"无法读取现场照片 RAR：系统未提供 bsdtar/tar（{archive_path.name}）"
        )
    if target_root.exists() or target_root.is_symlink():
        raise ArchiveInputError(f"临时解压目录已存在：{target_root}")

    list_result = subprocess.run(
        [tar, "-tf", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if list_result.returncode != 0:
        detail = _decode_archive_listing(list_result.stderr).strip()[-1000:]
        raise ArchiveInputError(f"RAR 无法读取：{archive_path.name}；{detail}")
    names = [line.strip() for line in _decode_archive_listing(list_result.stdout).splitlines() if line.strip()]
    if not names:
        raise ArchiveInputError(f"RAR 没有成员：{archive_path.name}")
    if len(names) > MAX_ARCHIVE_FILES:
        raise ArchiveInputError(f"RAR 成员过多（{len(names)}）：{archive_path.name}")
    seen_names: set[str] = set()
    for name in names:
        parts = _archive_member_parts(name.rstrip("/"), archive_label="RAR")
        key = "/".join(parts).casefold()
        if key in seen_names:
            raise ArchiveInputError(f"RAR 路径重复或大小写冲突：{name!r}")
        seen_names.add(key)

    verbose_result = subprocess.run(
        [tar, "-tvf", str(archive_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if verbose_result.returncode != 0:
        detail = _decode_archive_listing(verbose_result.stderr).strip()[-1000:]
        raise ArchiveInputError(f"RAR 成员类型无法校验：{archive_path.name}；{detail}")
    type_lines = [line for line in _decode_archive_listing(verbose_result.stdout).splitlines() if line.strip()]
    unsafe_types = [line for line in type_lines if line[0] not in {"-", "d"}]
    if unsafe_types:
        raise ArchiveInputError(
            f"RAR 不接受链接或特殊成员：{archive_path.name}；{unsafe_types[0][:160]}"
        )

    target_root.mkdir(parents=True)
    target_resolved = target_root.resolve()
    try:
        extract_result = subprocess.run(
            [tar, "-xf", str(archive_path), "-C", str(target_root)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if extract_result.returncode != 0:
            detail = _decode_archive_listing(extract_result.stderr).strip()[-1000:]
            raise ArchiveInputError(f"RAR 解压失败：{archive_path.name}；{detail}")
        extracted: list[Path] = []
        total_bytes = 0
        for path in target_root.rglob("*"):
            resolved = path.resolve()
            if not _inside(resolved, target_resolved):
                raise ArchiveInputError(f"RAR 成员越过解压目录：{path}")
            if path.is_symlink() or (hasattr(path, "is_socket") and path.is_socket()):
                raise ArchiveInputError(f"RAR 不接受链接或特殊成员：{path.name}")
            if not path.is_file():
                continue
            size = path.stat().st_size
            if size > MAX_MEMBER_BYTES:
                raise ArchiveInputError(f"RAR 成员过大：{path.name}")
            total_bytes += size
            if total_bytes > MAX_TOTAL_BYTES:
                raise ArchiveInputError(f"RAR 解压总量过大：{archive_path.name}")
            extracted.append(resolved)
        if not extracted:
            raise ArchiveInputError(f"RAR 没有可读取文件：{archive_path.name}")
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
    selected_scenarios: set[str] | None = None,
) -> dict[str, dict[str, Any]]:
    classified = discover_archives(input_dir)
    temp = Path(temporary_root).resolve()
    temp.mkdir(parents=True, exist_ok=True)
    cases: dict[str, dict[str, Any]] = {}
    supported = {
        "personnel_incentive",
        "promotional_display",
        "poster_material",
        "other_expense",
        "maintenance_fee",
        "giveaway_promotion",
        "price_difference_support",
        "pos_target_incentive",
        "entry_fee",
        "self_procured_gift_material",
    }
    selected = supported if selected_scenarios is None else set(selected_scenarios)
    unknown = selected - supported
    if unknown:
        raise ArchiveInputError("不支持的核销场景：" + "、".join(sorted(unknown)))
    missing = selected - set(classified)
    if selected_scenarios is not None and missing:
        raise ArchiveInputError("input/ 未找到指定核销场景：" + "、".join(sorted(missing)))
    for scenario in (
        "personnel_incentive",
        "promotional_display",
        "poster_material",
        "other_expense",
        "maintenance_fee",
        "giveaway_promotion",
        "price_difference_support",
        "pos_target_incentive",
        "entry_fee",
        "self_procured_gift_material",
    ):
        if scenario not in selected:
            continue
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
        elif scenario == "promotional_display":
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "sales_excel": _one(excels, "销售 Excel", archive.name),
                "contract_pdf": _one(_material_files(root, {".pdf"}), "合同 PDF", archive.name),
                "photo_files": images,
            }
        elif scenario == "poster_material":
            def named_images(*markers: str) -> list[Path]:
                return [
                    path
                    for path in images
                    if any(marker in normalize_text(path.name) for marker in markers)
                ]

            contract_image = _one(named_images("合同"), "促销合同图片", archive.name)
            invoice_image = _one(named_images("发票", "收据"), "发票或收据图片", archive.name)
            settlement_image = _one(named_images("结算单", "结算表"), "结算单图片", archive.name)
            nested_archives = [
                path
                for path in _material_files(root, {".zip"})
                if any(
                    marker in normalize_text(path.name)
                    for marker in ("返图", "现场照片", "水印照片")
                )
            ]
            nested_archive = _one(nested_archives, "现场返图 ZIP", archive.name)
            photo_extraction = _extract_archive(
                nested_archive,
                temp / "poster_material_field_photos",
            )
            field_photos = _material_files(Path(photo_extraction["root"]), IMAGE_SUFFIXES)
            if not field_photos:
                raise ArchiveInputError(f"{archive.name} 的现场返图 ZIP 没有图片")
            _unique_image_names(field_photos, nested_archive.name)
            role_images = {contract_image, invoice_image, settlement_image}
            remaining_images = [path for path in images if path not in role_images]
            remaining_documents = [
                *remaining_images,
                *_material_files(root, {".pdf"}),
            ]
            contract_attachment_files = [
                path
                for path in remaining_documents
                if any(
                    marker in normalize_text(path.name)
                    for marker in ("附件", "门店清单", "报价", "规格")
                )
            ]
            excluded_files = [
                path
                for path in remaining_documents
                if path not in contract_attachment_files
            ]
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "contract_image": contract_image,
                "invoice_image": invoice_image,
                "settlement_image": settlement_image,
                "contract_attachment_files": contract_attachment_files,
                "field_photo_archive": nested_archive,
                "field_photo_archive_sha256": photo_extraction["archive_sha256"],
                "field_photo_files": field_photos,
                "excluded_files": excluded_files,
            }
        elif scenario == "maintenance_fee":
            documents = _material_files(root, VISUAL_DOCUMENT_SUFFIXES)
            document_names = [path.name.casefold() for path in documents]
            duplicate_document_names = sorted(
                {name for name in document_names if document_names.count(name) > 1}
            )
            if duplicate_document_names:
                raise ArchiveInputError(
                    f"{archive.name} 中存在同名维护费用资料，无法稳定绑定："
                    + "、".join(duplicate_document_names)
                )
            if len(excels) > 1:
                raise ArchiveInputError(
                    f"{archive.name} 中POS电子表应最多1份，实际{len(excels)}份："
                    + "、".join(path.name for path in excels)
                )

            def maintenance_named_documents(*markers: str) -> list[Path]:
                return [
                    path
                    for path in documents
                    if any(marker in normalize_text(path.name) for marker in markers)
                ]

            def optional_single(paths: list[Path], label: str) -> Path | None:
                if len(paths) > 1:
                    raise ArchiveInputError(
                        f"{archive.name} 中{label}应最多1份，实际{len(paths)}份："
                        + "、".join(path.name for path in paths)
                    )
                return paths[0] if paths else None

            settlement_document = optional_single(
                maintenance_named_documents("结算单", "结算表"),
                "结算单",
            )
            promotional_contract = optional_single(
                maintenance_named_documents("促销合同"),
                "签章促销合同",
            )
            reserved = {
                path
                for path in (settlement_document, promotional_contract)
                if path is not None
            }
            stamped_pos_files = [
                path
                for path in documents
                if path not in reserved
                and any(marker in normalize_text(path.name) for marker in ("pos", "销售数据"))
            ]
            reserved.update(stamped_pos_files)
            activity_files = [
                path
                for path in documents
                if path not in reserved
                and any(
                    marker in normalize_text(path.name)
                    for marker in ("活动照片", "现场照片", "返图", "现场", "活动")
                )
            ]
            reserved.update(activity_files)
            supporting_files = [
                path
                for path in documents
                if path not in reserved
                and any(
                    marker in normalize_text(path.name)
                    for marker in ("协议", "相关文件", "维护", "合同", "证明")
                )
            ]
            reserved.update(supporting_files)
            other_visual_files = [path for path in documents if path not in reserved]

            document_roles: list[dict[str, Any]] = []
            document_roles.extend(
                {"path": path, "role": "stamped_pos_data"}
                for path in stamped_pos_files
            )
            if promotional_contract is not None:
                document_roles.append(
                    {"path": promotional_contract, "role": "signed_promotional_contract"}
                )
            if settlement_document is not None:
                document_roles.append({"path": settlement_document, "role": "settlement"})
            document_roles.extend(
                {"path": path, "role": "supporting_document"}
                for path in supporting_files
            )
            document_roles.extend(
                {"path": path, "role": "activity_photo"}
                for path in activity_files
            )
            document_roles.extend(
                {"path": path, "role": "other"}
                for path in other_visual_files
            )
            bound = {Path(item["path"]) for item in document_roles}
            if bound != set(documents):
                missing_documents = sorted(path.name for path in set(documents) - bound)
                raise ArchiveInputError(
                    f"{archive.name} 存在未绑定的维护费用资料："
                    + "、".join(missing_documents)
                )
            all_files = [
                path
                for path in root.rglob("*")
                if path.is_file() and path not in documents and path not in excels
            ]
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "pos_spreadsheet": excels[0] if excels else None,
                "stamped_pos_files": stamped_pos_files,
                "promotional_contract": promotional_contract,
                "settlement_document": settlement_document,
                "supporting_documents": supporting_files,
                "activity_evidence_files": activity_files,
                "visual_files": documents,
                "document_roles": document_roles,
                "excluded_files": all_files,
            }
        elif scenario == "giveaway_promotion":
            documents = _material_files(root, VISUAL_DOCUMENT_SUFFIXES)
            document_names = [path.name.casefold() for path in documents]
            duplicate_document_names = sorted(
                {name for name in document_names if document_names.count(name) > 1}
            )
            if duplicate_document_names:
                raise ArchiveInputError(
                    f"{archive.name} 中存在同名额外搭赠资料，无法稳定绑定："
                    + "、".join(duplicate_document_names)
                )
            if not documents:
                raise ArchiveInputError(f"{archive.name} 没有可视核销资料")
            document_roles = [
                {"path": path, "role": "visual_document"}
                for path in documents
            ]
            all_files = [
                path
                for path in root.rglob("*")
                if path.is_file() and path not in documents
            ]
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "visual_files": documents,
                "document_roles": document_roles,
                "excluded_files": all_files,
            }
        elif scenario == "price_difference_support":
            documents = _material_files(root, VISUAL_DOCUMENT_SUFFIXES)
            if not documents:
                raise ArchiveInputError(f"{archive.name} 没有可视核销资料")
            lowered = [path.name.casefold() for path in documents]
            duplicates = sorted({name for name in lowered if lowered.count(name) > 1})
            if duplicates:
                raise ArchiveInputError(
                    f"{archive.name} 中存在同名价格补差资料，无法稳定绑定："
                    + "、".join(duplicates)
                )
            if len(excels) > 1:
                raise ArchiveInputError(
                    f"{archive.name} 中POS电子表应最多1份，实际{len(excels)}份："
                    + "、".join(path.name for path in excels)
                )

            def price_named(*markers: str) -> list[Path]:
                return [
                    path for path in documents
                    if any(marker in normalize_text(path.name) for marker in markers)
                ]

            contract = _one(price_named("促销协议", "促销合同"), "签章促销合同", archive.name)
            settlement = _one(price_named("结算单", "结算表"), "结算单", archive.name)
            reserved = {contract, settlement}
            pos_files = [
                path for path in documents
                if path not in reserved
                and any(marker in normalize_text(path.name) for marker in ("pos", "销售", "陈列销售"))
            ]
            if not pos_files:
                raise ArchiveInputError(f"{archive.name} 缺少名称可识别的盖章POS数据")
            reserved.update(pos_files)
            outer_other = [path for path in documents if path not in reserved]
            rar_candidates = [
                path for path in _material_files(root, {".rar"})
                if any(marker in normalize_text(path.name) for marker in ("照片", "返图", "现场"))
            ]
            photo_archive = _one(rar_candidates, "现场活动照片RAR", archive.name)
            photo_extraction = _extract_rar_archive(
                photo_archive,
                temp / "price_difference_support_field_photos",
            )
            activity_photos = _material_files(Path(photo_extraction["root"]), IMAGE_SUFFIXES)
            if not activity_photos:
                raise ArchiveInputError(f"{photo_archive.name} 没有现场活动图片")
            non_images = [
                path for path in Path(photo_extraction["root"]).rglob("*")
                if path.is_file() and path.suffix.lower() not in IMAGE_SUFFIXES
            ]
            if non_images:
                raise ArchiveInputError(
                    f"{photo_archive.name} 含非图片成员："
                    + "、".join(path.name for path in non_images)
                )
            _unique_image_names(activity_photos, photo_archive.name)
            document_roles: list[dict[str, Any]] = [
                {"path": contract, "role": "signed_promotional_contract"},
                {"path": settlement, "role": "settlement"},
                *({"path": path, "role": "stamped_pos_data"} for path in pos_files),
                *({"path": path, "role": "other"} for path in outer_other),
                *({"path": path, "role": "activity_photo"} for path in activity_photos),
            ]
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "pos_spreadsheet": excels[0] if excels else None,
                "promotional_contract": contract,
                "settlement_document": settlement,
                "stamped_pos_files": pos_files,
                "photo_archive": photo_archive,
                "photo_archive_sha256": photo_extraction["archive_sha256"],
                "activity_photo_files": activity_photos,
                "visual_files": [*documents, *activity_photos],
                "document_roles": document_roles,
                "excluded_files": [
                    path for path in root.rglob("*")
                    if path.is_file()
                    and path not in documents
                    and path not in excels
                    and path != photo_archive
                ],
            }
        elif scenario == "pos_target_incentive":
            documents = _material_files(root, VISUAL_DOCUMENT_SUFFIXES)
            if not documents:
                raise ArchiveInputError(f"{archive.name} 没有可视核销资料")
            lowered = [path.name.casefold() for path in documents]
            duplicates = sorted({name for name in lowered if lowered.count(name) > 1})
            if duplicates:
                raise ArchiveInputError(
                    f"{archive.name} 中存在同名POS达标激励资料，无法稳定绑定："
                    + "、".join(duplicates)
                )
            if len(excels) > 1:
                raise ArchiveInputError(
                    f"{archive.name} 中POS电子表应最多1份，实际{len(excels)}份："
                    + "、".join(path.name for path in excels)
                )

            def incentive_named(*markers: str) -> list[Path]:
                return [
                    path for path in documents
                    if any(marker in normalize_text(path.name) for marker in markers)
                ]

            settlement_candidates = incentive_named("结算单", "结算表")
            if len(settlement_candidates) > 1:
                raise ArchiveInputError(
                    f"{archive.name} 中结算单应最多1份，实际{len(settlement_candidates)}份："
                    + "、".join(path.name for path in settlement_candidates)
                )
            settlement = settlement_candidates[0] if settlement_candidates else None
            contract_candidates = incentive_named("促销合同", "促销协议")
            if len(contract_candidates) > 1:
                raise ArchiveInputError(
                    f"{archive.name} 中促销合同应最多1份，实际{len(contract_candidates)}份："
                    + "、".join(path.name for path in contract_candidates)
                )
            contract = contract_candidates[0] if contract_candidates else None
            reserved = {path for path in (settlement, contract) if path is not None}
            stamped_pos_files = [
                path for path in documents
                if path not in reserved
                and any(marker in normalize_text(path.name) for marker in ("pos", "销售", "数据"))
            ]
            reserved.update(stamped_pos_files)
            activity_files = [path for path in documents if path not in reserved]
            document_roles: list[dict[str, Any]] = []
            if contract is not None:
                document_roles.append({"path": contract, "role": "signed_promotional_contract"})
            if settlement is not None:
                document_roles.append({"path": settlement, "role": "settlement"})
            document_roles.extend(
                {"path": path, "role": "stamped_pos_data"}
                for path in stamped_pos_files
            )
            for path in activity_files:
                name = normalize_text(path.name)
                role = (
                    "store_receipt"
                    if any(marker in name for marker in ("小票", "收银", "购物凭证"))
                    else "activity_photo"
                    if any(marker in name for marker in ("照片", "现场", "返图", "活动"))
                    else "other_activity_proof"
                )
                document_roles.append({"path": path, "role": role})
            bound = {Path(item["path"]) for item in document_roles}
            if bound != set(documents):
                raise ArchiveInputError(
                    f"{archive.name} 存在未绑定的POS达标激励资料："
                    + "、".join(path.name for path in set(documents) - bound)
                )
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "pos_spreadsheet": excels[0] if excels else None,
                "promotional_contract": contract,
                "settlement_document": settlement,
                "stamped_pos_files": stamped_pos_files,
                "activity_evidence_files": activity_files,
                "visual_files": documents,
                "document_roles": document_roles,
                "excluded_files": [
                    path for path in root.rglob("*")
                    if path.is_file() and path not in documents and path not in excels
                ],
            }
        elif scenario == "entry_fee":
            documents = _material_files(root, VISUAL_DOCUMENT_SUFFIXES)
            if not documents:
                raise ArchiveInputError(f"{archive.name} 没有进场费合同或扣款凭证")
            lowered = [path.name.casefold() for path in documents]
            duplicates = sorted({name for name in lowered if lowered.count(name) > 1})
            if duplicates:
                raise ArchiveInputError(
                    f"{archive.name} 中存在同名进场费资料，无法稳定绑定："
                    + "、".join(duplicates)
                )

            def entry_named(*markers: str) -> list[Path]:
                return [
                    path for path in documents
                    if any(marker in normalize_text(path.name) for marker in markers)
                ]

            contract = _one(
                entry_named("产品推广协议", "进场费合同", "条码费合同", "进场合同"),
                "进场费合同",
                archive.name,
            )
            rar_candidates = [
                path for path in _material_files(root, {".rar"})
                if any(
                    marker in normalize_text(path.name)
                    for marker in ("进场照片", "上架照片", "照片", "返图")
                )
            ]
            photo_archive = _one(rar_candidates, "进场/上架照片RAR", archive.name)
            photo_extraction = _extract_rar_archive(
                photo_archive,
                temp / "entry_fee_shelf_photos",
            )
            photo_root = Path(photo_extraction["root"])
            shelf_photos = _material_files(photo_root, IMAGE_SUFFIXES)
            if not shelf_photos:
                raise ArchiveInputError(f"{photo_archive.name} 没有进场/上架照片")
            non_images = [
                path for path in photo_root.rglob("*")
                if path.is_file() and path.suffix.lower() not in IMAGE_SUFFIXES
            ]
            if non_images:
                raise ArchiveInputError(
                    f"{photo_archive.name} 含非图片成员："
                    + "、".join(path.name for path in non_images)
                )
            _unique_image_names(shelf_photos, photo_archive.name)

            outer_other = [path for path in documents if path != contract]
            deduction_proofs = [
                path for path in outer_other
                if any(
                    marker in normalize_text(path.name)
                    for marker in ("系统扣款", "扣款凭证", "上架凭证", "费用扣款")
                )
            ]
            remaining_outer = [path for path in outer_other if path not in deduction_proofs]
            document_roles: list[dict[str, Any]] = [
                {"path": contract, "role": "entry_fee_contract"},
                *(
                    {"path": path, "role": "system_deduction_proof"}
                    for path in deduction_proofs
                ),
                *(
                    {"path": path, "role": "other"}
                    for path in remaining_outer
                ),
            ]
            for path in shelf_photos:
                relative = path.relative_to(photo_root)
                parent_parts = list(relative.parts[:-1])
                document_roles.append(
                    {
                        "path": path,
                        "role": "shelf_photo",
                        "relative_path": relative.as_posix(),
                        "store_hint": parent_parts[-1] if parent_parts else None,
                    }
                )
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "entry_fee_contract": contract,
                "photo_archive": photo_archive,
                "photo_archive_sha256": photo_extraction["archive_sha256"],
                "shelf_photo_files": shelf_photos,
                "system_deduction_proof_files": deduction_proofs,
                "visual_files": [*documents, *shelf_photos],
                "document_roles": document_roles,
                "excluded_files": [
                    path for path in root.rglob("*")
                    if path.is_file()
                    and path not in documents
                    and path != photo_archive
                ],
            }
        elif scenario == "self_procured_gift_material":
            documents = _material_files(root, VISUAL_DOCUMENT_SUFFIXES)
            legacy_workbooks = _material_files(root, {".xls"})
            if len(excels) > 1:
                raise ArchiveInputError(
                    f"{archive.name} 中POS电子表应最多1份，实际{len(excels)}份："
                    + "、".join(path.name for path in excels)
                )

            def self_named(*markers: str) -> list[Path]:
                return [
                    path for path in documents
                    if any(marker in normalize_text(path.name) for marker in markers)
                ]

            contract = _one(self_named("促销协议", "促销合同"), "签章促销合同", archive.name)
            settlement = _one(self_named("结算单", "结算表"), "结算单", archive.name)
            purchase_receipt = _one(
                self_named("购买凭证", "发票", "收据"),
                "发票或收据",
                archive.name,
            )
            payment_records = [
                path for path in documents
                if path not in {contract, settlement, purchase_receipt}
                and any(marker in normalize_text(path.name) for marker in ("付款记录", "转账", "付款凭证"))
            ]
            reserved = {contract, settlement, purchase_receipt, *payment_records}
            stamped_pos_files = [
                path for path in documents
                if path not in reserved
                and any(marker in normalize_text(path.name) for marker in ("销售pos", "pos数据", "销售数据"))
            ]
            if not stamped_pos_files:
                raise ArchiveInputError(f"{archive.name} 缺少名称可识别的盖章POS数据")
            reserved.update(stamped_pos_files)
            unclassified_visuals = [path for path in documents if path not in reserved]
            activity_workbook = _one(legacy_workbooks, "活动返图.xls", archive.name)
            activity = extract_activity_return_workbook(
                activity_workbook,
                temp / "self_procured_gift_material_activity_photos",
            )
            photo_files = [Path(item["photo_file"]) for item in activity["records"]]
            _unique_image_names(photo_files, activity_workbook.name)
            document_roles: list[dict[str, Any]] = [
                {"path": contract, "role": "signed_promotional_contract"},
                {"path": settlement, "role": "settlement"},
                {"path": purchase_receipt, "role": "purchase_invoice_or_receipt"},
                *({"path": path, "role": "purchase_payment_record"} for path in payment_records),
                *({"path": path, "role": "stamped_pos_data"} for path in stamped_pos_files),
                *({"path": path, "role": "unclassified_visual"} for path in unclassified_visuals),
            ]
            for record in activity["records"]:
                document_roles.append(
                    {
                        "path": Path(record["photo_file"]),
                        "role": "activity_photo",
                        "store_hint": record["store_name"],
                        "period_hint": record["period_text"],
                        "customer_code_hint": record["customer_code"],
                        "activity_excel_row": record["excel_row"],
                    }
                )
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "promotional_contract": contract,
                "settlement_document": settlement,
                "purchase_invoice_or_receipt": purchase_receipt,
                "purchase_payment_records": payment_records,
                "stamped_pos_files": stamped_pos_files,
                "pos_spreadsheet": excels[0] if excels else None,
                "activity_return_workbook": activity_workbook,
                "activity_return": activity,
                "activity_photo_files": photo_files,
                "visual_files": [*documents, *photo_files],
                "document_roles": document_roles,
                "excluded_files": [
                    path for path in root.rglob("*")
                    if path.is_file()
                    and path not in documents
                    and path not in excels
                    and path != activity_workbook
                ],
            }
        else:
            documents = _material_files(root, VISUAL_DOCUMENT_SUFFIXES)
            document_names = [path.name.casefold() for path in documents]
            duplicate_document_names = sorted(
                {name for name in document_names if document_names.count(name) > 1}
            )
            if duplicate_document_names:
                raise ArchiveInputError(
                    f"{archive.name} 中存在同名其他费用资料，无法稳定绑定："
                    + "、".join(duplicate_document_names)
                )

            def named_documents(*markers: str) -> list[Path]:
                return [
                    path
                    for path in documents
                    if any(marker in normalize_text(path.name) for marker in markers)
                ]

            promotional_contract = _one(
                named_documents("促销合同"),
                "签章促销合同",
                archive.name,
            )
            settlement = _one(
                named_documents("结算单", "结算表"),
                "结算单",
                archive.name,
            )
            approval_files = [
                path
                for path in named_documents("特殊审批", "特批", "新增类型审批")
                if path not in {promotional_contract, settlement}
            ]
            supporting_files = [
                path
                for path in documents
                if path not in {promotional_contract, settlement}
                and path not in approval_files
                and any(
                    marker in normalize_text(path.name)
                    for marker in ("协议", "合同", "发票", "收据", "相关文件")
                )
            ]
            if not supporting_files:
                raise ArchiveInputError(
                    f"{archive.name} 缺少独立于促销合同和结算单的协议、合同、发票、收据或相关文件"
                )
            activity_files = [
                path
                for path in documents
                if path not in {promotional_contract, settlement}
                and path not in approval_files
                and path not in supporting_files
            ]
            document_roles: list[dict[str, Any]] = [
                {"path": promotional_contract, "role": "signed_promotional_contract"},
                {"path": settlement, "role": "settlement"},
            ]
            for path in supporting_files:
                normalized = normalize_text(path.name)
                role = (
                    "invoice_or_receipt"
                    if any(marker in normalized for marker in ("发票", "收据"))
                    else "supporting_document"
                )
                document_roles.append({"path": path, "role": role})
            document_roles.extend(
                {"path": path, "role": "special_approval"}
                for path in approval_files
            )
            for path in activity_files:
                role = "pos_data" if "pos" in normalize_text(path.name) else "activity_photo"
                document_roles.append({"path": path, "role": role})

            bound = {Path(item["path"]) for item in document_roles}
            if bound != set(documents):
                missing_documents = sorted(path.name for path in set(documents) - bound)
                raise ArchiveInputError(
                    f"{archive.name} 存在未绑定的其他费用资料：" + "、".join(missing_documents)
                )
            all_files = [
                path
                for path in root.rglob("*")
                if path.is_file() and path not in documents
            ]
            cases[scenario] = {
                "scenario": scenario,
                "source_archive": archive.resolve(),
                "archive_sha256": extracted["archive_sha256"],
                "source_root": root,
                "promotional_contract": promotional_contract,
                "settlement_document": settlement,
                "supporting_documents": supporting_files,
                "activity_evidence_files": activity_files,
                "special_approval_files": approval_files,
                "visual_files": documents,
                "document_roles": document_roles,
                "excluded_files": all_files,
            }
    return cases
