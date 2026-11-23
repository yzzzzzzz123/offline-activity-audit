"""One existing-tools entry point for uploaded files; never invokes a model."""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from zipfile import ZipFile, ZIP_DEFLATED

from .archive_input import IMAGE_SUFFIXES, EXCEL_SUFFIXES
from .common import AuditError, sha256_file
from .material_intake import prepare_material_diagnosis
from .pdf_materials import prepare_units
from .reading_contract import aggregate_reading_status, derive_receipt
from .word_materials import WORD_SUFFIXES, libreoffice_path
from .workbench_store import atomic_write_json

SUPPORTED_SUFFIXES = IMAGE_SUFFIXES | EXCEL_SUFFIXES | WORD_SUFFIXES | {'.pdf', '.xls', '.zip'}


def staging_directory(output: Path) -> Path:
    """Return the one staging directory owned by a reader output.

    Linux systemd deployments commonly set ``PrivateTmp=true`` and
    ``ProtectSystem=strict``.  Reader staging must therefore stay inside the
    caller-selected output tree rather than relying on an implicit system TEMP
    directory.  Resolve the candidate and reject any path that escapes the
    output because of a link.
    """
    output = Path(output).absolute()
    staging = output / '.staging'
    if staging.resolve().parent != output.resolve():
        raise AuditError('材料读取staging必须位于本次输出目录内。')
    return staging


def read_material(source: Path, output: Path) -> dict:
    """Read a whole ZIP or one file using exactly the normal intake adapters.

    This produces source-indexed facts/full-page images, not a business result.
    CLI stdout contains the index, not thousands of spreadsheet rows.
    """
    source, output = Path(source).absolute(), Path(output).absolute()
    if source.is_symlink() or output.is_symlink() or output.exists():
        raise AuditError('原材料不能是链接，读取输出目录必须是新的独立目录。')
    if not source.is_file() or source.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise AuditError('请选择已支持的Word、PDF、Excel、图片或ZIP材料。')
    for parent in (*source.parents, *output.parents):
        if parent.is_symlink() or getattr(parent, 'is_junction', lambda: False)():
            raise AuditError('材料读取路径不能经过链接或目录联接。')
    original_sha = sha256_file(source)
    output.mkdir(parents=True)
    # A single file uses the same archive inventory rather than a second parser.
    # Staging lives inside the caller-selected output directory so Linux
    # systemd units (PrivateTmp/ProtectSystem=strict) and restricted Windows
    # accounts do not depend on an implicit system TEMP directory.
    staging = staging_directory(output)
    staging.mkdir()
    try:
        submitted = source
        if source.suffix.lower() != '.zip':
            submitted = staging/'single-material.zip'
            with ZipFile(submitted, 'w', ZIP_DEFLATED) as archive:
                archive.write(source, source.name)
        prepared = prepare_material_diagnosis(submitted, output/'prepared', original_error='', classify_by_content=True)
        archives = []
        for archive in prepared['archives']:
            units = prepare_units(archive, output/'units'/archive['archive_id'])
            receipts = [derive_receipt(item, units) for item in archive['inventory']
                        if item.get('kind') != 'container']
            archives.append({'archive_id': archive['archive_id'], 'source_archive': source.name,
                             'inventory': archive['inventory'], 'units': units,
                             'receipts': receipts,
                             'reading_status': aggregate_reading_status(receipts),
                             'workbook_readings': [str(path) for path in sorted(
                                 (output/'units'/archive['archive_id']).glob('*-workbook-reading.json'))]})
    except BaseException:
        # No manifest was published, so the staging ZIP is not evidence. Remove
        # only this reader-created directory; retain the source and output for
        # diagnosis according to the same non-destructive contract.
        shutil.rmtree(staging, ignore_errors=True)
        raise
    if sha256_file(source) != original_sha:
        raise AuditError('原材料在读取时发生变化，未发布读取清单。')
    file_receipts = [{'archive_id': archive['archive_id'], **receipt}
                     for archive in archives for receipt in archive['receipts']]
    reading_status = aggregate_reading_status(file_receipts)
    manifest = {'schema_version': '1.1', 'source_file': source.name, 'source_sha256': original_sha,
                'model_calls': 0, 'archives': archives,
                'file_receipts': file_receipts, 'reading_status': reading_status,
                'scope': '仅原始资料读取；不决定核销是否通过，不补造印章或字段。'}
    # Paths are locations, never inferred facts. Keep the same originals and
    # per-page/cell locators that the normal audit provider consumes.
    manifest_path = output/'reading-manifest.json'
    atomic_write_json(manifest_path, manifest)
    # Publish the manifest first. Failure to remove staging is a maintenance
    # signal only; it must not turn a complete read into a failed read.
    try:
        shutil.rmtree(staging)
    except OSError as exc:
        manifest['staging_cleanup_deferred'] = {
            'path': str(staging),
            'error_code': 'staging_cleanup_deferred',
            'reason': type(exc).__name__,
        }
        atomic_write_json(manifest_path, manifest)
    return {'manifest': str(manifest_path), 'model_calls': 0,
            'source_count': sum(len(a['inventory']) for a in archives),
            'unit_count': sum(len(a['units']) for a in archives),
            'reading_status': reading_status,
            'file_receipts': file_receipts,
            'limitations': ([{'source_file': i['source_file'], 'reasons': i['limitations']}
                            for a in archives for i in a['inventory'] if i.get('limitations')]
                           + [{'source_file': u['source_file'], 'locator': u['locator'], 'reasons': u['limitations']}
                              for a in archives for u in a['units'] if u.get('limitations')])}


def capabilities(*, family=None) -> dict:
    from .archive_input import MAX_ARCHIVE_FILES, MAX_MEMBER_BYTES, MAX_TOTAL_BYTES
    from .material_intake import MAX_VISUAL_PAGES
    from .pdf_materials import MAX_CELLS
    limits = {'archive_members': MAX_ARCHIVE_FILES, 'archive_member_bytes': MAX_MEMBER_BYTES,
              'archive_total_bytes': MAX_TOTAL_BYTES}
    if family in {None, 'spreadsheet'}:
        limits['spreadsheet_stored_cells'] = MAX_CELLS
    if family in {None, 'document'}:
        limits['pdf_pages'] = MAX_VISUAL_PAGES
    result = {'formats': sorted(SUPPORTED_SUFFIXES), 'model_calls': 0, 'resource_limits': limits,
              'note': '依赖及资源边界盘点不替代文件实测；不截断资料、输出半份读取清单或将资源不足写成客户缺件。'}
    if family in {None, 'document'}:
        office = libreoffice_path()
        result['word_renderer'] = ('libreoffice-writer' if office else
            'registered Word/WPS COM required' if os.name == 'nt' and importlib.util.find_spec('win32com') else None)
    return result


def main(argv=None, *, family=None) -> int:
    parser = argparse.ArgumentParser(description='现成材料读取工具：Word/PDF/Excel/图片/ZIP；不调用模型')
    parser.add_argument('--input', type=Path)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    from .material_skills import READERS
    family_suffixes = READERS.get('read-audit-' + str(family)) if family else None
    if family and family_suffixes is None:
        parser.error('未登记的材料读取类别')
    if args.check:
        result = capabilities(family=family)
        if family_suffixes is not None:
            result['formats'] = sorted(family_suffixes)
    else:
        if args.input is None or args.output is None:
            parser.error('--input 和 --output 必须同时提供')
        if family_suffixes is not None and args.input.suffix.lower() not in family_suffixes:
            parser.error('本Skill仅接受对应格式；ZIP请先走正常上传的安全解包入口。')
        result = read_material(args.input, args.output)
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0
