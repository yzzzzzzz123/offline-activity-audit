"""Read-only submitted-file previews. No models, source systems or audit execution."""
from __future__ import annotations

import csv
import base64
import hashlib
from html import escape
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
from urllib.parse import urlencode
from xml.etree import ElementTree
import zipfile

MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_EXPANDED_BYTES = 128 * 1024 * 1024
MAX_ENTRIES = 20000
PAGE_ROWS = 100
MAX_COLUMNS = 100


class MaterialPreviewUnavailable(ValueError):
    pass


def _regular(path: Path, root: Path) -> Path:
    path, root = path.absolute(), root.absolute()
    if not path.is_relative_to(root):raise MaterialPreviewUnavailable('原文件位置不在本环境材料范围内')
    for part in [root, *reversed(path.relative_to(root).parents), path.relative_to(root)]:
        candidate = part if part == root else root / part
        if candidate.is_symlink() or getattr(candidate,'is_junction',lambda:False)():
            raise MaterialPreviewUnavailable('原文件路径不能经过链接')
    if not path.is_file():raise FileNotFoundError('本环境未保留对应原件')
    return path


def _name(value):
    if not isinstance(value,str):raise MaterialPreviewUnavailable('材料名称未留存')
    raw=value.replace('\\','/')
    parts=PurePosixPath(raw).parts
    if not parts or PurePosixPath(raw).is_absolute() or any(part in {'..','.'} or ':' in part or '\x00' in part for part in parts):
        raise MaterialPreviewUnavailable('材料路径不符合本单来源范围')
    return '/'.join(parts)


def _digest(value):return hashlib.sha256(value).hexdigest()


def _zip(source,*,bounded=True):
    archive=zipfile.ZipFile(source,metadata_encoding='gbk')
    entries=archive.infolist()
    if len(entries)>MAX_ENTRIES or (bounded and sum(item.file_size for item in entries)>MAX_EXPANDED_BYTES):
        archive.close();raise MaterialPreviewUnavailable('资料包过大，请下载原件查看')
    return archive


def _member(archive,name):
    found=[info for info in archive.infolist() if info.filename.replace('\\','/')==name]
    if len(found)!=1:raise MaterialPreviewUnavailable('原文件在资料包中缺失或存在同路径重名')
    info=found[0]
    _name(info.filename)
    if info.is_dir() or info.flag_bits&1 or stat.S_ISLNK(info.external_attr>>16):
        raise MaterialPreviewUnavailable('该材料不能直接读取')
    if info.file_size>MAX_FILE_BYTES:raise MaterialPreviewUnavailable('文件超过在线预览大小范围')
    with archive.open(info) as stream:
        value=stream.read(MAX_FILE_BYTES+1)
    if len(value)>MAX_FILE_BYTES:raise MaterialPreviewUnavailable('文件超过在线预览大小范围')
    return value


def original_material(workspace: Path, input_root: Path, file_id: str):
    if not isinstance(file_id,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,120}',file_id):
        raise ValueError('材料编号不正确')
    cases_path=_regular(workspace/'analysis/input-cases.json',workspace)
    if cases_path.stat().st_size>8*1024*1024:raise MaterialPreviewUnavailable('材料清单过大')
    cases=json.loads(cases_path.read_text('utf-8'))
    packets=cases if isinstance(cases,list) else cases.get('archives') if isinstance(cases,dict) and isinstance(cases.get('archives'),list) else list(cases.values()) if isinstance(cases,dict) else []
    matches=[(packet,item) for packet in packets if isinstance(packet,dict) for item in packet.get('inventory',[]) if isinstance(item,dict) and item.get('file_id')==file_id]
    if len(matches)!=1:raise MaterialPreviewUnavailable('材料编号不属于本单或不能唯一定位')
    packet,item=matches[0];filename=_name(item.get('source_file'))
    archive_name=Path(str(packet.get('source_archive','')).replace('\\','/')).name
    if not archive_name or not archive_name.lower().endswith('.zip'):
        raise MaterialPreviewUnavailable('该历史材料缺少可读取的原始ZIP资料包')
    archive_hash=packet.get('archive_sha256');file_hash=item.get('sha256')
    valid_hash=lambda value:isinstance(value,str) and re.fullmatch(r'[0-9a-fA-F]{64}',value)
    if not valid_hash(archive_hash) and not valid_hash(file_hash):
        raise MaterialPreviewUnavailable('本记录未留存原件指纹，无法确认预览来源')
    root=input_root.absolute();candidates=[];visited=0
    if not root.is_dir():raise FileNotFoundError('本环境未保留原资料包')
    if root.is_symlink() or getattr(root,'is_junction',lambda:False)():raise MaterialPreviewUnavailable('材料目录不能是链接')
    for directory,dirs,files in os.walk(root,followlinks=False):
        dirs[:]=[name for name in dirs if not (Path(directory)/name).is_symlink() and not getattr(Path(directory)/name,'is_junction',lambda:False)()]
        visited+=len(files)
        if visited>50000:raise MaterialPreviewUnavailable('材料目录较大，暂时无法定位原件')
        for name in files:
            if name.casefold()==archive_name.casefold():candidates.append(_regular(Path(directory)/name,root))
    if not candidates:raise FileNotFoundError('本环境未保留对应原始资料包')
    last_error=None
    for candidate in candidates:
        try:
            with candidate.open('rb') as source:
                if valid_hash(archive_hash):
                    digest=hashlib.file_digest(source,'sha256').hexdigest()
                    if digest.lower()!=archive_hash.lower():continue
                    source.seek(0)
                with _zip(source,bounded=False) as archive:
                    def read_item(target,depth=0):
                        if depth>4:raise MaterialPreviewUnavailable('材料嵌套层级过多')
                        target_name=_name(target.get('source_file'));parent=target.get('parent_source')
                        if not parent:return _member(archive,target_name)
                        parent=_name(parent)
                        parents=[row for row in packet['inventory'] if isinstance(row,dict) and row.get('source_file','').replace('\\','/')==parent]
                        if len(parents)!=1:raise MaterialPreviewUnavailable('未保存该附件的唯一来源文件')
                        parent_bytes=read_item(parents[0],depth+1)
                        with _zip(io.BytesIO(parent_bytes)) as inner:
                            if parent.lower().endswith('.zip'):
                                if not target_name.startswith(parent+'/'):raise MaterialPreviewUnavailable('附件与来源资料包不对应')
                                return _member(inner,target_name[len(parent)+1:])
                            wanted=target.get('sha256')
                            if not valid_hash(wanted):raise MaterialPreviewUnavailable('附件未保存独立内容指纹')
                            for info in inner.infolist():
                                if Path(info.filename).suffix.lower() not in {'.png','.jpg','.jpeg','.webp','.gif','.bmp'}:continue
                                value=_member(inner,info.filename)
                                if _digest(value).lower()==wanted.lower():return value
                            raise MaterialPreviewUnavailable('未在原始文档中找到对应嵌入图片')
                    value=read_item(item)
                if valid_hash(file_hash) and _digest(value).lower()!=file_hash.lower():
                    raise MaterialPreviewUnavailable('原件与本单留存指纹不一致，无法预览')
                return filename,value
        except (zipfile.BadZipFile,KeyError,OSError,MaterialPreviewUnavailable) as exc:last_error=exc
    if isinstance(last_error,MaterialPreviewUnavailable):raise last_error
    raise MaterialPreviewUnavailable('未找到与本单指纹一致的原资料包或文件')


def _html(title,body):
    return ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>'+escape(title)+'</title><style>body{margin:24px;font:14px/1.7 "Microsoft YaHei",sans-serif;color:#173743}h1{font-size:19px;overflow-wrap:anywhere}p{white-space:pre-wrap}table{border-collapse:collapse}td,th{padding:7px 10px;border:1px solid #dbe5e9;white-space:pre-wrap;max-width:480px;overflow-wrap:anywhere}th{background:#eff5f7}nav{display:flex;flex-wrap:wrap;gap:12px;margin:12px 0}a{color:#087ba5}pre{white-space:pre-wrap;overflow-wrap:anywhere}.note{color:#607782;font-size:12px}</style><h1>'+escape(title)+'</h1>'+body+'</html>').encode('utf-8')


def render_material(filename,value,*,page=1,sheet=0,route='',file_id=''):
    suffix=Path(filename).suffix.lower();name=PurePosixPath(filename).name
    if suffix in {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.tif','.tiff'}:
        from PIL import Image
        with Image.open(io.BytesIO(value)) as image:
            if image.width*image.height>40_000_000:raise MaterialPreviewUnavailable('图片尺寸过大，请下载原件查看')
            if suffix in {'.tif','.tiff'}:
                output=io.BytesIO();image.convert('RGB').save(output,format='PNG');return 'image/png',output.getvalue()
            image.verify()
        return {'.jpg':'image/jpeg','.jpeg':'image/jpeg','.png':'image/png','.gif':'image/gif','.webp':'image/webp','.bmp':'image/bmp'}[suffix],value
    if suffix=='.pdf':
        if not value.lstrip().startswith(b'%PDF-'):raise MaterialPreviewUnavailable('原文件不是可读取的PDF')
        import fitz
        with fitz.open(stream=value,filetype='pdf') as document:
            if document.needs_pass:raise MaterialPreviewUnavailable('PDF需要密码，请下载原件查看')
            if not 1<=page<=len(document):raise ValueError('PDF页码不正确')
            selected=document[page-1];scale=min(1.5,2000/max(selected.rect.width,selected.rect.height,1))
            pixels=selected.get_pixmap(matrix=fitz.Matrix(scale,scale),alpha=False).tobytes('png')
            link=lambda number:escape(route+'?'+urlencode({'file_id':file_id,'page':number}),quote=True)
            navigation='<nav>'+(f'<a href="{link(page-1)}">上一页</a>' if page>1 else '')+(f'<a href="{link(page+1)}">下一页</a>' if page<len(document) else '')+'</nav>'
            body=f'<p class="note">PDF原页预览 · 第 {page} / {len(document)} 页；本地渲染，不调用AI。</p>'+navigation+'<img alt="原始PDF第'+str(page)+'页" style="display:block;max-width:100%;height:auto" src="data:image/png;base64,'+base64.b64encode(pixels).decode('ascii')+'">'+navigation
        return 'text/html; charset=utf-8',_html(name,body)
    if suffix in {'.xlsx','.xlsm','.xls','.csv'}:
        rows=[];sheets=[];total=0;start=(page-1)*PAGE_ROWS
        if suffix=='.csv':
            text=value.decode('utf-8-sig',errors='replace');source=csv.reader(io.StringIO(text));sheets=['CSV'];selected='CSV'
            for index,row in enumerate(source):
                total=index+1
                if start<=index<start+PAGE_ROWS:rows.append((index+1,list(row)))
        elif zipfile.is_zipfile(io.BytesIO(value)):
            import openpyxl
            with _zip(io.BytesIO(value)):pass
            workbook=openpyxl.load_workbook(io.BytesIO(value),read_only=True,data_only=False,keep_links=False)
            try:
                sheets=workbook.sheetnames
                if not 0<=sheet<len(sheets):raise ValueError('工作表编号不正确')
                selected=sheets[sheet];table=workbook[selected];total=table.max_row or 0
                if start<total:
                    rows=[(index,list(row)) for index,row in enumerate(table.iter_rows(min_row=start+1,max_row=min(start+PAGE_ROWS,total),max_col=min(table.max_column or 1,MAX_COLUMNS),values_only=True),start+1)]
            finally:workbook.close()
        else:
            import xlrd
            workbook=xlrd.open_workbook(file_contents=value,on_demand=True)
            try:
                sheets=workbook.sheet_names()
                if not 0<=sheet<len(sheets):raise ValueError('工作表编号不正确')
                table=workbook.sheet_by_index(sheet);selected=table.name;total=table.nrows
                rows=[(index+1,table.row_values(index,0,min(table.ncols,MAX_COLUMNS))) for index in range(start,min(start+PAGE_ROWS,total))]
            finally:workbook.release_resources()
        link=lambda target_page,target_sheet:escape(route+'?'+urlencode({'file_id':file_id,'page':target_page,'sheet':target_sheet}),quote=True)
        tabs='<nav>'+''.join(f'<a href="{link(1,index)}">{escape(label)}</a>' for index,label in enumerate(sheets))+'</nav>'
        navigation='<nav>'+ (f'<a href="{link(page-1,sheet)}">上一页</a>' if page>1 else '')+(f'<a href="{link(page+1,sheet)}">下一页</a>' if start+PAGE_ROWS<total else '')+'</nav>'
        body=tabs+f'<p class="note">{escape(selected)} · 共 {total} 行 · 当前第 {page} 页，每页 {PAGE_ROWS} 行；最多显示前 {MAX_COLUMNS} 列。单元格按原文件内容显示，公式不执行。</p>'+navigation+'<table><tbody>'+''.join('<tr><th>'+str(index)+'</th>'+''.join('<td>'+escape('' if cell is None else str(cell))+'</td>' for cell in row[:MAX_COLUMNS])+'</tr>' for index,row in rows)+'</tbody></table>'+navigation
        return 'text/html; charset=utf-8',_html(name,body)
    if suffix=='.docx':
        with _zip(io.BytesIO(value)) as archive:document=_member(archive,'word/document.xml')
        if b'<!DOCTYPE' in document.upper() or b'<!ENTITY' in document.upper():raise MaterialPreviewUnavailable('Word文档包含不支持的外部声明')
        root=ElementTree.fromstring(document);namespace={'w':'http://schemas.openxmlformats.org/wordprocessingml/2006/main'}
        paragraphs=[''.join(node.text or '' for node in paragraph.findall('.//w:t',namespace)) for paragraph in root.findall('.//w:p',namespace)]
        return 'text/html; charset=utf-8',_html(name,'<p class="note">Word内容预览，保留段落文字；完整排版与图片请查看原件。</p>'+''.join('<p>'+escape(text)+'</p>' for text in paragraphs))
    if suffix in {'.txt','.json','.xml','.html','.htm','.svg','.md'}:
        return 'text/html; charset=utf-8',_html(name,'<pre>'+escape(value.decode('utf-8-sig',errors='replace'))+'</pre>')
    if suffix=='.zip':
        with _zip(io.BytesIO(value)) as archive:entries=[(info.filename,info.file_size) for info in archive.infolist() if not info.is_dir()]
        return 'text/html; charset=utf-8',_html(name,'<table><tr><th>包内文件</th><th>大小（字节）</th></tr>'+''.join('<tr><td>'+escape(file)+'</td><td>'+str(size)+'</td></tr>' for file,size in entries)+'</table>')
    raise MaterialPreviewUnavailable('此格式暂不支持在线预览，可下载原件查看')
