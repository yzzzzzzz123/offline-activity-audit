"""RAR capability detection must follow the executable's real implementation."""
from __future__ import annotations

import unittest
from unittest.mock import patch

from audit_core import archive_tools


class RarToolSelectionTests(unittest.TestCase):
    def test_bsdtar_is_selected_by_name(self):
        with patch.object(archive_tools.shutil, "which",
                          side_effect=lambda name: "/usr/bin/bsdtar" if name == "bsdtar" else None):
            self.assertEqual(archive_tools.rar_tool(), "/usr/bin/bsdtar")

    def test_generic_tar_is_rejected_when_it_is_gnu_tar(self):
        with patch.object(archive_tools.shutil, "which",
                          side_effect=lambda name: "/usr/bin/tar" if name == "tar" else None), \
                patch.object(archive_tools, "_version_text",
                             return_value="tar (GNU tar) 1.35"):
            self.assertIsNone(archive_tools.rar_tool())

    def test_generic_tar_is_accepted_only_for_libarchive(self):
        with patch.object(archive_tools.shutil, "which",
                          side_effect=lambda name: "/usr/bin/tar" if name == "tar" else None), \
                patch.object(archive_tools, "_version_text",
                             return_value="bsdtar 3.7.4 - libarchive 3.7.4"):
            self.assertEqual(archive_tools.rar_tool(), "/usr/bin/tar")

    def test_no_extractor_returns_none(self):
        with patch.object(archive_tools.shutil, "which", return_value=None):
            self.assertIsNone(archive_tools.rar_tool())


if __name__ == "__main__":
    unittest.main()
