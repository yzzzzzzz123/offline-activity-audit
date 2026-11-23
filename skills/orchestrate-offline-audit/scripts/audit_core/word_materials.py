"""Deterministic Word-to-page preparation before any model reads materials."""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import pymupdf

from .archive_input import MAX_ARCHIVE_FILES, MAX_MEMBER_BYTES, MAX_TOTAL_BYTES, _zip_member_parts
from .common import AuditError, sha256_file
from .execution_trace import run_command
from .reading_contract import ReadingSystemError

WORD_SUFFIXES = {'.docx', '.doc', '.rtf'}
WORKER = Path(__file__).with_name('word_render_worker.py')


class WordMaterialError(AuditError):
    """An original Word package is demonstrably unreadable, not a missing tool."""


def _check_xml(package, name):
    class Target:
        root = None

        def start(self, tag, attributes):
            if self.root is None:
                self.root = tag
            if tag.endswith('}Relationship') and attributes.get('TargetMode') == 'External' and not attributes.get('Type', '').endswith('/hyperlink'):
                raise WordMaterialError('Word文档引用了未随文件上传的外部内容，无法完整读取。')

        def end(self, tag): pass
        def data(self, text): pass
        def doctype(self, name, public_id, system_id):
            raise WordMaterialError('Word文档XML包含非标准外部定义，未打开。')
        def close(self): return self.root

    # Validate XML syntax with bounded memory; do not construct a second full
    # document tree/text just to check a large package before page rendering.
    parser = ElementTree.XMLParser(target=Target())
    with package.open(name) as stream:
        for block in iter(lambda: stream.read(64 * 1024), b''):
            parser.feed(block)
    return parser.close()


def inspect_word(source: Path) -> dict:
    """Check original bytes without executing Office or guessing a document role."""
    with source.open('rb') as stream:
        signature = stream.read(8)
    # The suffix is a routing hint, not evidence.  Normal callers first create
    # a content-named copy, but direct callers can still pass a Word body with
    # a wrong extension; inspect the bytes rather than rejecting it by name.
    if signature.lstrip().startswith(b'{\\rtf'):
        return {'format': 'rtf', 'member_count': 0, 'expanded_bytes': 0}
    if signature.startswith(b'PK'):
        try:
            with ZipFile(source) as package:
                infos = package.infolist()
                names = [item.filename for item in infos]
                if len(names) != len(set(names)):
                    raise WordMaterialError('Word文档包含重复的内部文件，无法可靠读取。')
                if len(infos) > MAX_ARCHIVE_FILES or sum(i.file_size for i in infos) > MAX_TOTAL_BYTES:
                    raise WordMaterialError('Word文档展开内容超过现有材料安全上限。')
                for info in infos:
                    _zip_member_parts(info)
                    if info.file_size > MAX_MEMBER_BYTES:
                        raise WordMaterialError('Word文档内部文件超过现有材料安全上限。')
                    if info.flag_bits & 1:
                        raise WordMaterialError('Word文档已加密，无法读取内容。')
                if not {'[Content_Types].xml', 'word/document.xml'}.issubset(names):
                    raise WordMaterialError('文件不包含有效的Word文档结构。')
                _check_xml(package, '[Content_Types].xml')
                root = _check_xml(package, 'word/document.xml')
                if root != '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}document':
                    raise WordMaterialError('Word文档正文结构无法读取。')
                if package.testzip() is not None:
                    raise WordMaterialError('Word文档内部文件校验失败，无法完整读取。')
                # Uploaded content only: hyperlinks do not fetch anything, but
                # linked templates/images/OLE data must not be fetched at open.
                for name in names:
                    if name.endswith('.rels'):
                        _check_xml(package, name)
                return {'format': 'docx', 'member_count': len(infos),
                        'expanded_bytes': sum(i.file_size for i in infos)}
        except (BadZipFile, ElementTree.ParseError, EOFError) as exc:
            raise WordMaterialError('Word文档结构损坏，无法完整读取。') from exc
    if signature == bytes.fromhex('d0cf11e0a1b11ae1'):
        return {'format': 'doc', 'member_count': 0, 'expanded_bytes': 0}
    raise WordMaterialError('Word文件内容与格式不符，或已加密，无法直接读取。')


def libreoffice_path() -> str | None:
    executable = shutil.which('libreoffice') or shutil.which('soffice') or shutil.which('soffice.com')
    if executable:
        return executable
    if os.name == 'nt':
        for root in (os.environ.get('ProgramFiles'), os.environ.get('ProgramFiles(x86)')):
            if root:
                path = Path(root)/'LibreOffice/program/soffice.com'
                if path.is_file():
                    return str(path)
    return None


def render_word(source: Path, destination: Path, *, inspected: dict | None = None) -> dict:
    inspected = inspect_word(source) if inspected is None else inspected
    if destination.exists() or destination.is_symlink():
        raise AuditError('Word转换临时目录已存在，不覆盖已有资料。')
    destination.mkdir(parents=True)
    target = destination/'document.pdf'
    original_sha = sha256_file(source)
    office = libreoffice_path()
    if not office and os.name != 'nt':
        raise ReadingSystemError(
            '系统缺少Word读取工具：请安装LibreOffice Writer；不能将其记为客户多交资料。',
            code='renderer_missing', retryable=False)
    command = [sys.executable, '-B', str(WORKER), '--input', str(source.resolve()), '--output', str(target.resolve())]
    if office:
        command += ['--libreoffice', office]
    options = {'stdout': subprocess.PIPE, 'stderr': subprocess.PIPE, 'timeout': 200,
               'check': False, 'text': True, 'encoding': 'utf-8', 'errors': 'replace'}
    if os.name == 'nt':
        options['creationflags'] = subprocess.CREATE_NO_WINDOW
    try:
        result = run_command(command, **options)
    except subprocess.TimeoutExpired as exc:
        raise ReadingSystemError(
            'Word读取工具超时，未将其当作客户资料缺失。',
            code='renderer_timeout', retryable=True) from exc
    if result.returncode:
        raise ReadingSystemError(
            'Word读取工具执行失败；请检查转换组件，不能据此认定客户文件多余。',
            code='renderer_failed', retryable=True)
    try:
        details = json.loads(result.stdout)
        if not target.is_file() or target.is_symlink():
            raise ReadingSystemError('Word读取工具未生成完整页面。',
                                     code='renderer_output_missing')
        with pymupdf.open(target) as document:
            if document.needs_pass or len(document) == 0:
                raise ReadingSystemError('Word读取工具生成的页面无法读取。',
                                         code='renderer_output_invalid')
            pages = len(document)
    except (ValueError, pymupdf.FileDataError) as exc:
        raise ReadingSystemError(
            'Word读取工具返回格式无效，未交给模型。',
            code='renderer_output_invalid') from exc
    if sha256_file(source) != original_sha:
        raise ReadingSystemError('Word原件在读取时发生变化，未继续审核。',
                                 code='source_changed', retryable=True)
    return {**inspected, 'pdf': target.resolve(), 'pages': pages, 'tool': details['tool'],
            'source_sha256': original_sha, 'pdf_sha256': sha256_file(target)}
