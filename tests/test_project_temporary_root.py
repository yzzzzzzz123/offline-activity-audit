"""Project-volume temporary images keep the existing lease and scope boundary."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core import run_temporary as runtime


class ProjectTemporaryRootTests(unittest.TestCase):
    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory()
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name).resolve()
        self.project = self.root / 'project'
        self.project.mkdir()

    @unittest.skipUnless(os.name == 'nt', 'Windows local disk placement')
    def test_default_workspace_is_on_project_volume_and_cleans_original_bytes(self):
        local_data = self.root / 'fake-local-appdata'
        default_temp = local_data / 'Temp'
        default_temp.mkdir(parents=True)
        with patch.object(runtime, 'PROJECT_ROOT', self.project), \
             patch.dict(os.environ, {'LOCALAPPDATA': str(local_data)}), \
             patch.object(runtime.tempfile, 'gettempdir', return_value=str(default_temp)):
            value = runtime.ManagedTemporaryDirectory('product-images', scope=self.project / 'worktrees')
            self.assertEqual(value.root, self.project / '.runtime' / 'temporary')
            with value as directory:
                (Path(directory) / 'original.png').write_bytes(b'unchanged image bytes')
                self.assertTrue(value.container.exists())
            self.assertFalse(value.container.exists())
            self.assertEqual(list(value.root.iterdir()), [])

    @unittest.skipUnless(os.name == 'nt', 'Windows local disk placement')
    def test_active_lease_is_protected_and_abandoned_project_workspace_is_collected(self):
        scope = self.project / 'worktrees'
        with patch.object(runtime, 'PROJECT_ROOT', self.project):
            value = runtime.ManagedTemporaryDirectory('render', scope=scope)
            (Path(value.name) / 'original.png').write_bytes(b'preserved while active')
            self.assertEqual(runtime.cleanup_abandoned_temporaries(scope=scope), {'removed': 0, 'busy': 1, 'deferred': 0})
            value._lease.close()
            value._closed = True
            self.assertEqual(runtime.cleanup_abandoned_temporaries(scope=scope), {'removed': 1, 'busy': 0, 'deferred': 0})
            self.assertFalse(value.container.exists())

    def test_explicit_temporary_root_and_non_windows_default_are_unchanged(self):
        with patch.object(runtime, 'PROJECT_ROOT', self.project):
            self.assertEqual(runtime._temp_root(self.root), self.root)
            with patch.object(runtime.tempfile, 'gettempdir', return_value=str(self.root)):
                self.assertEqual(runtime._temp_root(None), self.root)
            if os.name != 'nt':
                self.assertEqual(runtime._temp_root(None), Path(tempfile.gettempdir()).resolve())
        self.assertFalse((self.project / '.runtime').exists())


if __name__ == '__main__':
    unittest.main()
