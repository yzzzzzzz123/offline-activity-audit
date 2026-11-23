"""Local sharing violations must not restart a real model request."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audit_core.model_metrics import collect_model_artifacts, save_model_observations
from audit_core.model_settings import api_selection
from audit_core.qwen_model import extract_qwen_json


class ProgressWriteTests(unittest.TestCase):
    def test_observation_streams_complete_json_without_building_a_second_full_string(self):
        payload = {'rows': [{'row': n, '原文': '保持完整数值与来源'} for n in range(2000)]}
        expected = json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2)+'\n'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with collect_model_artifacts(root), patch('audit_core.model_metrics.json.dumps',
                    side_effect=AssertionError('do not build the whole observation as a second string')):
                save_model_observations('complete-source', payload)
            self.assertEqual((root/'model-observations/complete-source.json').read_text(encoding='utf-8'), expected)

    def test_stream_failure_preserves_previous_evidence_and_cleans_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with collect_model_artifacts(root):
                save_model_observations('accepted-source', {'original': True})
                with self.assertRaises(ValueError):
                    save_model_observations('accepted-source', {'many': list(range(1000)), 'invalid': float('nan')})
            self.assertEqual(json.loads((root/'model-observations/accepted-source.json').read_text()), {'original': True})
            self.assertFalse(list((root/'model-observations').glob('*.tmp')))

    def test_transient_sharing_violation_retries_only_local_atomic_replace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replace = Path.replace
            attempts = []
            def busy_once(source, target):
                attempts.append(source)
                if len(attempts) == 1:
                    raise PermissionError("synthetic sharing violation")
                return replace(source, target)
            with collect_model_artifacts(root), patch.object(Path, "replace", busy_once), patch("audit_core.model_metrics.time.sleep"):
                save_model_observations("call-progress-test", {"model_event_count": 3})
            self.assertEqual(len(attempts), 2)
            self.assertEqual(json.loads((root / "model-observations/call-progress-test.json").read_text()), {"model_event_count": 3})

    def test_permanently_busy_evidence_stays_strict_and_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with collect_model_artifacts(root):
                save_model_observations("accepted-evidence", {"fact": "original"})
                with patch.object(Path, "replace", side_effect=PermissionError("busy")), patch("audit_core.model_metrics.time.sleep"):
                    with self.assertRaises(PermissionError):
                        save_model_observations("accepted-evidence", {"fact": "new"})
            self.assertEqual(json.loads((root / "model-observations/accepted-evidence.json").read_text()), {"fact": "original"})
            self.assertEqual(list((root / "model-observations").glob("*.tmp")), [])

    def test_progress_write_failure_does_not_repeat_model_call_or_drop_final_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            schema = root / "schema.json"
            schema.write_text(json.dumps({"type": "object", "properties": {"ok": {"const": True}},
                                          "required": ["ok"], "additionalProperties": False}))
            selection = api_selection({"provider": "ppio", "api_key": "sk_synthetic_never_send"})
            def complete(selection, content, **kwargs):
                kwargs["progress"]({"model_event_count": 1})
                return '{"ok":true}', {"input_tokens": 10, "output_tokens": 5}
            with collect_model_artifacts(root / "analysis"), patch("audit_core.qwen_model.complete_json", side_effect=complete) as call, patch(
                    "audit_core.model_metrics.save_model_observations", side_effect=PermissionError("busy")), patch(
                    "httpx.AsyncClient", side_effect=AssertionError("no network")):
                value = extract_qwen_json(connection=selection, root=root / "model", skill=root,
                                         schema_path=schema, prompt="Synthetic evidence", images=[], label="隔离进度写入", validator=None)
            self.assertEqual(value, {"ok": True})
            self.assertEqual(call.call_count, 1)
            rows = [json.loads(line) for line in (root / "analysis/model-metrics.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[0]["status"], "success")
            self.assertEqual(rows[0]["usage"], {"input_tokens": 10, "output_tokens": 5})


if __name__ == "__main__":
    unittest.main()
