"""Select bundled reader instructions from actual source metadata, never names.

This registry contains paths only. Unselected Skill/reference bodies are not
opened, and no reading instructions are injected into business audit stages.
"""
from pathlib import Path

from .common import AuditError
from .paths import SKILL_ROOT
from .archive_input import EXCEL_SUFFIXES, IMAGE_SUFFIXES
from .word_materials import WORD_SUFFIXES


READERS = {
    'read-audit-spreadsheet': EXCEL_SUFFIXES | {'.xls'},
    'read-audit-document': WORD_SUFFIXES | {'.pdf'},
    'read-audit-image': IMAGE_SUFFIXES,
}


def reading_skills(unit):
    if not unit.get('image'):
        return []  # native cells are already program-read; no model OCR
    suffix = Path(unit.get('source_file', '')).suffix.lower()
    if unit.get('photo_reference') or suffix in READERS['read-audit-spreadsheet']:
        return ['read-audit-spreadsheet', 'read-audit-image']
    return [key for key, suffixes in READERS.items() if suffix in suffixes] or ['read-audit-image']


def runtime_instructions(names):
    requested = set(names)
    if not requested.issubset(READERS):
        raise AuditError('未登记的材料读取Skill，不能从业务资料指定任意文件。')
    return '\n'.join((SKILL_ROOT.parent / key / 'references/runtime.md').read_text(encoding='utf-8').strip()
                     for key in READERS if key in requested)
