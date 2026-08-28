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

    if (
        any(marker in archive_text for marker in ("维护费用", "维护费"))
        and nested_archive_count == 0
        and image_count + pdf_count >= 1
        and maintenance_pos_count + maintenance_settlement_count >= 1
    ):
        return {**shape, "scenario": "maintenance_fee"}

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
    if not 1 <= len(archives) <= 5:
        raise ArchiveInputError(
            f"input/ 必须直接包含 1～5 个 ZIP；当前发现 {len(archives)} 个。"
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
