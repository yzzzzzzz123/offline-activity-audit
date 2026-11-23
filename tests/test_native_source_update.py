"""Native release guards and code-only rollback, without host/service access."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest.mock import patch

SPEC=importlib.util.spec_from_file_location('native_source_update',Path(__file__).resolve().parents[1]/'deploy/update_source.py')
updater=importlib.util.module_from_spec(SPEC);SPEC.loader.exec_module(updater)


class NativeSourceUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name);stream=io.BytesIO();rows=[]
        with tarfile.open(fileobj=stream,mode='w') as bundle:
            for name in sorted(updater.ALLOWED):
                path=self.root/name;path.parent.mkdir(parents=True,exist_ok=True)
                old=b'# previous\n';new=b'# next\n';path.write_bytes(old)
                row={'path':name,'before_sha256':hashlib.sha256(old).hexdigest(),
                     'sha256':hashlib.sha256(new).hexdigest(),'bytes':len(new)}
                rows.append(row);member=tarfile.TarInfo('projects/offline/'+name);member.size=len(new)
                bundle.addfile(member,io.BytesIO(new))
        self.archive=stream.getvalue()
        self.manifest={'format':1,'target':str(updater.ROOT),'service':updater.UNIT,
                       'files':rows,'archive_sha256':hashlib.sha256(self.archive).hexdigest()}

    def test_changed_published_source_refuses_release_before_writes(self):
        name=self.manifest['files'][0]['path'];(self.root/name).write_bytes(b'# concurrent change\n')
        with self.assertRaisesRegex(ValueError,'changed since inspection'):
            updater.checked_inputs(self.manifest,self.archive,self.root)
        self.assertEqual((self.root/name).read_bytes(),b'# concurrent change\n')

    def test_private_or_material_upload_paths_are_rejected(self):
        for name in ('config/application.env','worktrees/record/manifest.json',
                     updater.CORE+'local_upload.py','../outside.py'):
            with self.assertRaises(ValueError):updater.checked_path(self.root,name)

    def test_partial_code_write_rollback_preserves_business_data(self):
        payloads,originals=updater.checked_inputs(self.manifest,self.archive,self.root)
        business=self.root/'worktrees/record/manifest.json';business.parent.mkdir(parents=True)
        business.write_bytes(b'preserve private history')
        name=self.manifest['files'][0]['path'];(self.root/name).write_bytes(payloads[name])
        with patch.object(updater.os,'chown',create=True),patch.object(updater,'sync_directory'):
            updater.rollback(self.root,originals,[name])
        self.assertEqual((self.root/name).read_bytes(),b'# previous\n')
        self.assertEqual(business.read_bytes(),b'preserve private history')


if __name__=='__main__':unittest.main()
