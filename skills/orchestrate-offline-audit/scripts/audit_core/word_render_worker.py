"""Isolated, time-bounded document renderer. No model, macros or user sessions."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import uuid


def libreoffice(source, target, executable):
    with tempfile.TemporaryDirectory(prefix='word-profile-', dir=target.parent) as directory:
        profile = Path(directory)
        (profile/'user').mkdir()
        (profile/'user/registrymodifications.xcu').write_text(
            '<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry">'
            '<item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item>'
            '<item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item>'
            '</oor:items>', encoding='utf-8')
        command=[executable, '-env:UserInstallation='+profile.as_uri(), '--headless', '--norestore',
                 '--nodefault', '--nofirststartwizard', '--convert-to', 'pdf:writer_pdf_Export',
                 '--outdir', str(profile), str(source)]
        options={'stdout':subprocess.PIPE,'stderr':subprocess.PIPE}
        if os.name != 'nt': options['start_new_session']=True
        else: options['creationflags']=subprocess.CREATE_NO_WINDOW
        process=subprocess.Popen(command, **options)
        try:
            stdout,stderr=process.communicate(timeout=180)
        except BaseException:
            if os.name != 'nt': os.killpg(process.pid,signal.SIGKILL)
            else: process.kill()
            process.communicate()
            raise
        result=profile/(source.stem+'.pdf')
        if process.returncode or not result.is_file():
            raise RuntimeError('LibreOffice did not produce document pages')
        shutil.copyfile(result,target)
    return 'libreoffice-writer'


@contextmanager
def windows_document(source):
    import gc
    import pythoncom
    import win32api
    import win32com.client
    import win32job
    import win32process

    pythoncom.CoInitialize()
    app=book=None
    job=win32job.CreateJobObject(None,'Local\\offline-word-'+uuid.uuid4().hex)
    limits=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation)
    limits['BasicLimitInformation']['LimitFlags']=win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job,win32job.JobObjectExtendedLimitInformation,limits)
    try:
        before=set(win32process.EnumProcesses())
        app=win32com.client.DispatchEx('Word.Application')
        owned=[]
        for pid in set(win32process.EnumProcesses())-before:
            handle=None
            try:
                handle=win32api.OpenProcess(0x0400|0x0010|0x0100|0x0001,False,pid)
                name=Path(win32process.GetModuleFileNameEx(handle,0)).name.casefold()
                if name in {'wps.exe','winword.exe'}:
                    win32job.AssignProcessToJobObject(job,handle)
                    owned.append(pid)
            except Exception:
                # An unrelated process may exit or be inaccessible. It is not
                # ours to close; a missing owned Office process fails closed.
                continue
            finally:
                if handle is not None: handle.Close()
        if not owned or app.Documents.Count:
            raise RuntimeError('Cannot establish a separate empty Word session')
        app.Visible=False
        app.DisplayAlerts=0
        app.AutomationSecurity=3
        app.Options.UpdateLinksAtOpen=False
        # A non-secret sentinel prevents an interactive password dialog.
        book=app.Documents.Open(str(source),ConfirmConversions=False,ReadOnly=True,
            AddToRecentFiles=False,PasswordDocument='offline-audit-no-password',
            PasswordTemplate='offline-audit-no-password',Visible=False,OpenAndRepair=False)
        yield book
    finally:
        if book is not None:
            try: book.Close(0)
            except Exception: pass
        # Never Quit()/kill an existing user's Word or WPS process. The job
        # contains only processes observed being created by this DispatchEx.
        book=app=None
        gc.collect()
        job.Close()
        pythoncom.CoUninitialize()


def windows_word(source,target):
    with windows_document(source) as book:
        book.ExportAsFixedFormat(str(target),17)
    return 'word-com-isolated'


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--input',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--libreoffice')
    args=parser.parse_args()
    if args.output.exists(): raise RuntimeError('Output already exists')
    tool=(libreoffice(args.input,args.output,args.libreoffice) if args.libreoffice
          else windows_word(args.input,args.output))
    print(json.dumps({'tool':tool},ensure_ascii=True),flush=True)


if __name__=='__main__':
    main()
