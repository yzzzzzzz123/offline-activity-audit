"""Bounded ZIP reception for the trusted workbench's manual upload form."""
from __future__ import annotations

import hashlib
from pathlib import Path
import re
from typing import BinaryIO
import zipfile
import zlib

from .archive_input import (ArchiveInputError, MAX_ARCHIVE_FILES, MAX_COMPRESSION_RATIO, MAX_MEMBER_BYTES,
                            MAX_TOTAL_BYTES, _open_zip, _zip_member_parts)
from .oss_intake import OSSIntakeRequestError, VERIFY_CODE_PATTERN
from .scenario_registry import SCENARIO_SPECS


def upload_metadata(filename: str, biz_type: str, request_id: str,
                    large_venue_fee: bool | None, cvs_otc_channel: bool = False, *,
                    verify_code: str | None = None, analyze_id: int | None = None) -> dict:
    if (not isinstance(filename, str) or not filename or len(filename) > 160
            or len(filename.encode('utf-8')) > 220 or filename.endswith((' ', '.'))
            or re.search(r'[\x00-\x1f<>:"/\\|?*]', filename)
            or re.match(r'(?i)^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', filename)
            or Path(filename).suffix.lower() != '.zip'):
        raise OSSIntakeRequestError('请选择文件名有效的 ZIP 资料包')
    scenarios = {spec.label: spec.scenario for spec in SCENARIO_SPECS}
    if biz_type not in scenarios:
        raise OSSIntakeRequestError('请明确选择本次核销类型')
    if not isinstance(request_id, str) or not re.fullmatch(r'[a-f0-9]{24}', request_id):
        raise OSSIntakeRequestError('上传提交标识无效，请重新选择文件')
    if large_venue_fee is not None and (type(large_venue_fee) is not bool or biz_type != '陈列堆头'):
        raise OSSIntakeRequestError('商场入场费选项只适用于陈列堆头')
    if verify_code is not None:
        if not isinstance(verify_code, str) or VERIFY_CODE_PATTERN.fullmatch(verify_code.strip()) is None:
            raise OSSIntakeRequestError('verifyCode 必须为 1～80 位 ASCII 字母、数字或 ._:-')
        verify_code = verify_code.strip()
    if analyze_id is not None and (type(analyze_id) is not int or not 1 <= analyze_id <= 9_223_372_036_854_775_807):
        raise OSSIntakeRequestError('analyzeId 必须是 64 位正整数')
    return {'basename': filename, 'biz_type': biz_type, 'scenario': scenarios[biz_type],
            'request_id': request_id, 'large_venue_fee': large_venue_fee,
            'verify_code': verify_code, 'analyze_id': analyze_id,
            'cvs_otc_channel': biz_type == '条码费' and cvs_otc_channel is True}


def receive_zip(stream: BinaryIO, length: int, destination: Path, maximum: int) -> str:
    if type(length) is not int or not 1 <= length <= maximum:
        raise OSSIntakeRequestError(f'资料包大小必须在 1 字节到 {maximum // (1024 * 1024)} MiB 之间')
    digest = hashlib.sha256()
    remaining = length
    with destination.open('xb') as output:
        while remaining:
            block = stream.read(min(1024 * 1024, remaining))
            if not block:
                raise OSSIntakeRequestError('文件未完整上传，请重新提交')
            output.write(block);digest.update(block);remaining -= len(block)
    try:
        with _open_zip(destination) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > MAX_ARCHIVE_FILES:
                raise OSSIntakeRequestError(f'ZIP 必须包含 1–{MAX_ARCHIVE_FILES} 个有效成员')
            seen, files, parents = set(), set(), set()
            total = 0
            for info in infos:
                parts = _zip_member_parts(info)
                key = '/'.join(parts).casefold()
                ancestors = {'/'.join(parts[:n]).casefold() for n in range(1, len(parts))}
                if key in seen or ancestors & files or (not info.is_dir() and key in parents):
                    raise OSSIntakeRequestError('ZIP 含重复、大小写冲突或文件与目录冲突的路径')
                seen.add(key);parents.update(ancestors)
                if info.is_dir():continue
                files.add(key);total += info.file_size
                if info.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_BYTES:
                    raise OSSIntakeRequestError('ZIP 展开后的文件体积超过处理上限')
                if info.file_size > 10 * 1024 * 1024 and info.file_size / max(1, info.compress_size) > MAX_COMPRESSION_RATIO:
                    raise OSSIntakeRequestError('ZIP 压缩比异常，请重新整理资料')
            if not files or archive.testzip() is not None:
                raise OSSIntakeRequestError('ZIP 为空或校验失败，请重新压缩后上传')
    except (ArchiveInputError, zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError, EOFError, zlib.error) as exc:
        if isinstance(exc, OSSIntakeRequestError):raise
        raise OSSIntakeRequestError('ZIP 格式、完整性或成员路径校验失败') from exc
    return digest.hexdigest()
