"""Archive writes survive short Windows read locks without losing old content."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from audit_core.workbench_store import atomic_write_text


class AtomicWorkbenchWriteTests(unittest.TestCase):
    def test_short_windows_permission_failure_retries_same_complete_file(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.json"
            target.write_text("old", encoding="utf-8")
            replace = os.replace
            denied = PermissionError("transient Windows reader")
            denied.winerror = 5
            attempts = []

            def replace_after_reader(source, destination):
                attempts.append(Path(source))
                self.assertEqual(target.read_text(encoding="utf-8"), "old")
                self.assertEqual(Path(source).read_text(encoding="utf-8"), "new\n")
                if len(attempts) == 1:
                    raise denied
                return replace(source, destination)

            with mock.patch("audit_core.workbench_store.os.replace", side_effect=replace_after_reader), \
                    mock.patch("audit_core.workbench_store.time.sleep") as sleep:
                atomic_write_text(target, "new\n")
            self.assertEqual(len(attempts), 2)
            self.assertEqual(attempts[0], attempts[1])
            sleep.assert_called_once_with(0.02)
            self.assertEqual(target.read_text(encoding="utf-8"), "new\n")
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_permanent_windows_denial_is_bounded_and_preserves_old_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.json"
            target.write_text("old", encoding="utf-8")
            denied = PermissionError("persistent Windows denial")
            denied.winerror = 32
            with mock.patch("audit_core.workbench_store.os.replace", side_effect=denied) as replace, \
                    mock.patch("audit_core.workbench_store.time.sleep") as sleep:
                with self.assertRaises(PermissionError) as raised:
                    atomic_write_text(target, "new")
            self.assertIs(raised.exception, denied)
            self.assertEqual(replace.call_count, 6)
            self.assertEqual(sleep.call_count, 5)
            self.assertLess(sum(call.args[0] for call in sleep.call_args_list), 1)
            self.assertEqual(target.read_text(encoding="utf-8"), "old")
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_other_filesystem_errors_are_not_retried(self):
        for error in (PermissionError("ordinary permissions"), OSError("disk failure")):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "manifest.json"
                target.write_text("old", encoding="utf-8")
                with mock.patch("audit_core.workbench_store.os.replace", side_effect=error) as replace, \
                        mock.patch("audit_core.workbench_store.time.sleep") as sleep:
                    with self.assertRaises(type(error)):
                        atomic_write_text(target, "new")
                replace.assert_called_once()
                sleep.assert_not_called()
                self.assertEqual(target.read_text(encoding="utf-8"), "old")
                self.assertFalse(list(Path(directory).glob("*.tmp")))

    @unittest.skipUnless(os.name == "nt", "requires a real Windows file sharing lock")
    def test_real_windows_handle_without_delete_sharing_can_release_between_attempts(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        create = kernel.CreateFileW
        create.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                           wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE)
        create.restype = wintypes.HANDLE
        close = kernel.CloseHandle
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "manifest.json"
            target.write_text("old", encoding="utf-8")
            handle = create(str(target), 0x80000000, 0x1 | 0x2, None, 3, 0x80, None)
            self.assertNotEqual(handle, wintypes.HANDLE(-1).value)
            replace = os.replace
            errors = []

            def replace_and_release(source, destination):
                nonlocal handle
                try:
                    return replace(source, destination)
                except PermissionError as exc:
                    errors.append(exc.winerror)
                    if handle is not None:
                        close(handle)
                        handle = None
                    raise

            try:
                with mock.patch("audit_core.workbench_store.os.replace", side_effect=replace_and_release):
                    atomic_write_text(target, "new")
            finally:
                if handle is not None:
                    close(handle)
            self.assertTrue(errors)
            self.assertTrue(all(code in (5, 32, 33) for code in errors))
            self.assertEqual(target.read_text(encoding="utf-8"), "new")
            self.assertFalse(list(Path(directory).glob("*.tmp")))
