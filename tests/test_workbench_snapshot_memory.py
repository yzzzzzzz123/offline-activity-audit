"""Bound snapshot serialization peaks and keep final sealing mandatory."""
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import json

from audit_core.execution_trace import collect_execution_trace, record_step
from audit_core.workbench_store import WorkbenchRunStore, atomic_write_json, _json_text


class WorkbenchSnapshotMemoryTests(TestCase):
    def store(self,root):
        return WorkbenchRunStore(root/'fixture-run',run_id='20261109-fixture',producer_model='ppio',
            root_html=Path('preview.html'),workbench_url='http://127.0.0.1:8080/')

    def test_streamed_json_preserves_original_format_values_and_complete_content(self):
        payload={'中文字段':'原始金额不变 17.123456789', 'rows':[{'index':i,'quote':'原文'*40} for i in range(1200)],
                 'path':Path('来源/材料.xlsx'), 'nested':{'zero':0,'empty':None,'flag':False}}
        expected=_json_text(payload)
        with TemporaryDirectory() as temporary:
            path=Path(temporary)/'snapshot.json'
            with patch('audit_core.workbench_store._json_text',side_effect=MemoryError('Monolithic serialization forbidden')):
                atomic_write_json(path,payload)
            self.assertEqual(path.read_text(encoding='utf-8'),expected)

    def test_partial_json_allocation_failure_preserves_old_file_and_removes_only_temporary(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);path=root/'snapshot.json';path.write_text('{"old":true}',encoding='utf-8')
            before=path.read_bytes()
            def interrupted(value,stream,**kwargs):
                stream.write('{"partial":');raise MemoryError('Synthetic allocation failure')
            with patch('audit_core.workbench_store.json.dump',side_effect=interrupted):
                with self.assertRaises(MemoryError):atomic_write_json(path,{'new':True})
            self.assertEqual(path.read_bytes(),before)
            self.assertEqual([p.name for p in root.iterdir()],['snapshot.json'])

    def test_streamed_json_retains_bounded_windows_sharing_retry(self):
        with TemporaryDirectory() as temporary:
            path=Path(temporary)/'snapshot.json'
            from audit_core import workbench_store
            replace=workbench_store.os.replace;attempts=[]
            def busy_then_replace(source,target):
                attempts.append(1)
                if len(attempts)<3:
                    error=PermissionError('Synthetic sharing lock');error.winerror=32;raise error
                replace(source,target)
            with patch('audit_core.workbench_store.os.replace',side_effect=busy_then_replace),patch('audit_core.workbench_store.time.sleep') as sleep:
                atomic_write_json(path,{'完整':'内容'})
            self.assertEqual(len(attempts),3)
            self.assertEqual(sleep.call_count,2)
            self.assertEqual(json.loads(path.read_text(encoding='utf-8')),{'完整':'内容'})

    def test_intermediate_snapshot_memory_error_preserves_primary_step_and_business_status(self):
        with TemporaryDirectory() as temporary:
            root=Path(temporary);store=self.store(root)
            store._write_snapshot();store._write_manifest();old=store.snapshot_path.read_bytes()
            from audit_core import workbench_store
            write=workbench_store.atomic_write_json
            def fail_snapshot(path,value):
                if path==store.snapshot_path:raise MemoryError('Synthetic projection allocation failure')
                return write(path,value)
            with collect_execution_trace(store.analysis_root,observer=store.observe):
                with patch('audit_core.workbench_store.atomic_write_json',side_effect=fail_snapshot):
                    meta=record_step('validation.source_prose_warning','来源表述提示，继续完成核销',{'extra_model_calls':0})
                self.assertEqual(store.snapshot_path.read_bytes(),old)
                self.assertEqual(store.manifest['status'],'running')
                self.assertEqual(store.manifest['snapshot_refresh_warning'],'intermediate_memory_pressure')
                self.assertTrue((store.workspace/meta['path']).is_file())
                record_step('audit.same_run_stage_reused','当前来源校验完成',{'model_calls':0})
            snapshot=json.loads(store.snapshot_path.read_text(encoding='utf-8'))
            self.assertEqual(snapshot['execution_trace']['count'],2)
            self.assertEqual(snapshot['run']['status'],'running')
            self.assertEqual(snapshot['run']['snapshot_refresh_warning'],'intermediate_memory_pressure')
            self.assertEqual(json.loads((store.workspace/meta['path']).read_text(encoding='utf-8'))['data'],{'extra_model_calls':0})

    def test_final_snapshot_failure_is_not_swallowed_or_published_as_complete(self):
        with TemporaryDirectory() as temporary:
            store=self.store(Path(temporary))
            with patch.object(store,'_write_snapshot',side_effect=MemoryError('Final sealing must fail')):
                with self.assertRaises(MemoryError):
                    store.complete(view_payload={'schema_version':'1.1','sheets':[]},scenarios=[],verification={})
            saved=json.loads(store.manifest_path.read_text(encoding='utf-8'))
            self.assertNotEqual(saved['status'],'completed')

    def test_non_memory_snapshot_and_primary_checkpoint_failures_still_propagate(self):
        for target,error in (('_write_snapshot',OSError('Disk write failed')),
                             ('write_checkpoint',MemoryError('Primary checkpoint allocation failed'))):
            with self.subTest(target=target),TemporaryDirectory() as temporary:
                store=self.store(Path(temporary))
                with collect_execution_trace(store.analysis_root,observer=store.observe),patch.object(store,target,side_effect=error):
                    with self.assertRaises(type(error)):
                        record_step('validation.done','真实校验已执行',{'model_calls':0})
                self.assertEqual(len(list((store.analysis_root/'execution-trace').glob('step-*.json'))),1)

    def test_snapshot_hash_is_streamed_without_loading_another_whole_file(self):
        with TemporaryDirectory() as temporary:
            store=self.store(Path(temporary));read=Path.read_bytes
            def forbid_snapshot(path):
                if path==store.snapshot_path:raise AssertionError('Whole snapshot read forbidden')
                return read(path)
            with patch.object(Path,'read_bytes',forbid_snapshot):store._write_snapshot()
            self.assertEqual(len(store.manifest['snapshot_sha256']),64)
