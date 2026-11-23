"""Read retained originals with the upload tools, without AI or business writes.

Deduplicate bytes, keep every path association, and release extracted/rendered
files after each archive/file. The report is tool compatibility, not AI approval.
"""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import tempfile
import time

from .common import sha256_file
from .material_intake import prepare_material_diagnosis
from .material_reader import read_material
from .workbench_store import atomic_write_json


def inspect_corpus(source: Path, output: Path, *, inventory_only=False):
    source, output = source.resolve(), output.absolute()
    if output.exists() or output.is_symlink():
        raise ValueError('兼容性检查输出必须是新的独立目录')
    output.mkdir(parents=True)
    grouped = {}
    for path in sorted(source.rglob('*.zip')):
        if path.is_symlink():
            continue
        digest = sha256_file(path)
        grouped.setdefault(digest, []).append(path)
    report = {'scope': '仅固定工具完整读取，不调用模型、不核销、不回调、不修改原件',
              'model_calls': 0, 'archive_path_count': sum(map(len, grouped.values())),
              'unique_archives': len(grouped), 'archives': [], 'files': []}
    code_paths = [Path(__file__).with_name(name) for name in
                  ('material_corpus.py', 'material_reader.py', 'material_intake.py', 'archive_input.py',
                   'workbook_reading.py', 'spreadsheet_rows.py', 'spreadsheet_photos.py', 'document_pages.py',
                   'word_materials.py', 'word_render_worker.py', 'pdf_materials.py')]
    report['reader_source_sha256'] = {p.name: sha256_file(p) for p in code_paths}
    seen = {}
    started = time.monotonic()
    def publish():
        report['elapsed_seconds'] = round(time.monotonic() - started, 3)
        report['file_status_counts'] = dict(Counter(f['status'] for f in report['files']))
        report['suffix_counts'] = dict(Counter(f['suffix'] for f in report['files']))
        report['reader_sources_changed'] = [p.name for p in code_paths
            if sha256_file(p) != report['reader_source_sha256'][p.name]]
        atomic_write_json(output/'compatibility.json', report)
    for index, (digest, paths) in enumerate(grouped.items(), 1):
        archive_result = {'sha256': digest, 'paths': [str(p.relative_to(source)) for p in paths], 'status': 'reading'}
        report['archives'].append(archive_result)
        print(json.dumps({'archive': index, 'total': len(grouped), 'file': paths[0].name}, ensure_ascii=False), flush=True)
        try:
            with tempfile.TemporaryDirectory(prefix='corpus-', dir=output) as directory:
                root = Path(directory)
                prepared = prepare_material_diagnosis(paths[0], root/'inventory', original_error='',
                                                     classify_by_content=True, read_content=False)
                for archive in prepared['archives']:
                    for item in archive['inventory']:
                        if item['kind'] == 'container':
                            continue  # Its children are already in the full safety-checked inventory.
                        path = Path(item['path'])
                        identity = (item['sha256'], item['suffix'])
                        association = {'archive_sha256': digest, 'source_file': item['source_file']}
                        if identity in seen:
                            seen[identity]['sources'].append(association)
                            continue
                        result = {'sha256': identity[0], 'suffix': identity[1], 'bytes': path.stat().st_size,
                                  'kind': item['kind'], 'sources': [association], 'status': 'inventoried'}
                        seen[identity] = result
                        report['files'].append(result)
                        if inventory_only:
                            continue
                        if item['kind'] == 'unsupported':
                            result['status'] = 'unsupported'
                            continue
                        before = time.monotonic()
                        try:
                            with tempfile.TemporaryDirectory(prefix='read-', dir=root) as file_dir:
                                receipt = read_material(path, Path(file_dir)/'output')
                                manifest = json.loads(Path(receipt['manifest']).read_text(encoding='utf-8'))
                                units = [u for a in manifest['archives'] for u in a['units']]
                                result.update(status='limited' if receipt['limitations'] else 'read',
                                              units=len(units), native_rows=sum(len(u.get('native_facts', [])) for u in units),
                                              images=sum(bool(u.get('image')) for u in units),
                                              limitations=receipt['limitations'])
                        except Exception as exc:
                            result.update(status='error', error_type=type(exc).__name__, error=str(exc))
                        result['seconds'] = round(time.monotonic() - before, 3)
                        publish()
                archive_result['file_status_counts'] = dict(Counter(f['status'] for f in report['files']
                    if any(s['archive_sha256'] == digest for s in f['sources'])))
                archive_result['status'] = ('inventoried' if inventory_only else
                    'read' if set(archive_result['file_status_counts']) <= {'read'} else 'completed_with_findings')
                if sha256_file(paths[0]) != digest:
                    raise RuntimeError('读取过程中原始归档发生变化')
        except Exception as exc:
            archive_result.update(status='error', error_type=type(exc).__name__, error=str(exc))
        publish()
    publish()
    return {'report': str(output/'compatibility.json'), 'unique_archives': len(grouped),
            'unique_files': len(seen), 'statuses': report.get('file_status_counts', {}), 'model_calls': 0}


def main(argv=None):
    parser = argparse.ArgumentParser(description='原材料兼容性检查；只读、不调用模型')
    parser.add_argument('--input', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--inventory-only', action='store_true')
    args = parser.parse_args(argv)
    import sys
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    print(json.dumps(inspect_corpus(args.input, args.output, inventory_only=args.inventory_only), ensure_ascii=False))
    return 0
