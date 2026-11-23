"""Whole-page sources, rendered one at a time with atomic page publication."""
import math
import os
from pathlib import Path
import shutil

from PIL import Image
import pymupdf

from .reading_contract import (INTERNAL_BUG, READ_OK, SECURITY_REJECTED,
                               SYSTEM_FAILURE, classify_reading_error,
                               failure_limitation)


def document_page_units(item, base, root: Path):
    """Render one complete page at a time and retain successful pages.

    A damaged page or an execution resource error must not erase pages already
    published for the same PDF. The failed page gets its own receipt unit. A
    systemic failure stops further rendering of this file; content failures
    continue so a later good page can still be read.
    """
    units = []
    with pymupdf.open(item.get('rendered_pdf') or item['path']) as document:
        page_total = len(document)
        # File-level receipts retain the whole page count even when a later
        # page fails and rendering stops at a system/resource boundary.
        item['pages_total'] = page_total
        for index in range(page_total):
            uid = f"{item['file_id']}-page-{index+1:04d}"
            target = root/(uid+'.jpg')
            pending = root/(uid+'.pending.jpg')
            page = pixmap = None
            stop_file = False
            try:
                page = document[index]
                pixels = math.ceil(page.rect.width*2)*math.ceil(page.rect.height*2)
                if pixels > Image.MAX_IMAGE_PIXELS:
                    from .reading_contract import ReadingSystemError
                    raise ReadingSystemError(
                        'PDF页面尺寸超出安全渲染范围',
                        code='resource_limit', retryable=True)
                # Disk/resource failures are execution faults, never missing
                # client evidence. Render only the current page.
                if shutil.disk_usage(root).free < pixels*3 + 65536:
                    from .reading_contract import ReadingSystemError
                    raise ReadingSystemError(
                        '材料读取磁盘空间不足，无法保存完整PDF页面',
                        code='storage_full', retryable=True)
                pixmap = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                pixmap.save(pending, jpg_quality=95)
                os.replace(pending, target)
                text = page.get_text(sort=True)
                units.append({**base, 'unit_id': uid, 'locator': f'第{index+1}页',
                              'image': target, 'native_facts': [text] if text.strip() else [],
                              'read_status': READ_OK})
            except Exception as exc:
                details = classify_reading_error(exc)
                units.append({
                    **base, 'unit_id': uid + '-unreadable',
                    'locator': f'第{index+1}页', 'image': None, 'native_facts': [],
                    'limitations': [failure_limitation(details)],
                    'read_status': details['status'],
                    'error_code': details['error_code'],
                    'retryable': details['retryable'],
                    'customer_actionable': details['customer_actionable'],
                })
                stop_file = details['status'] in {
                    SYSTEM_FAILURE, INTERNAL_BUG, SECURITY_REJECTED,
                }
            finally:
                pixmap = None
                page = None
                pending.unlink(missing_ok=True)
            if stop_file:
                break
    return units
